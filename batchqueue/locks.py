"""
batchqueue/locks.py

Record-level lock management for write batch execution.

Locks prevent two batches from editing the same record concurrently.
A lock is acquired just before execution starts and released when the
batch reaches a terminal state (COMPLETED, FAILED, DENIED, EXPIRED).

Lock lifecycle:
  acquire_locks()   — called by executor before starting; returns False on conflict
  release_locks()   — called after batch reaches any terminal state
  check_conflicts() — returns conflicting lock rows for a set of operations

Lock types:
  "RECORD"   — locks a specific (object, record_id) pair for UPDATE ops
  "METADATA" — reserved for future field/object metadata write operations

CREATE operations (record_id=None) are not locked — creates can't conflict
with updates to existing records.
"""

import logging
import uuid

from db.connection import get_connection

logger = logging.getLogger(__name__)

# Batch statuses that actively hold locks
_ACTIVE_STATUSES = ("PENDING_APPROVAL", "APPROVED", "EXECUTING")


def acquire_locks(batch_id: str, operations: list) -> bool:
    """
    Attempt to acquire RECORD locks for all UPDATE operations in a batch.

    Checks for conflicts first — if any target (object, record_id) is already
    locked by another active batch, returns False without acquiring anything.

    Only operations with method="update" and a non-null record_id are locked.

    Returns True if all locks were acquired (or no locks were needed),
    False if a conflict was detected (caller should not proceed with execution).
    """
    conn = get_connection()

    targets = [
        (op.object_api_name, op.record_id)
        for op in operations
        if getattr(op, "record_id", None) and getattr(op, "method", "").lower() == "update"
    ]

    if not targets:
        logger.info("acquire_locks: no lockable operations | batch=%s", batch_id)
        return True

    conflicts = _find_conflicts(targets, exclude_batch_id=batch_id)
    if conflicts:
        logger.warning(
            "acquire_locks: %d conflicts | batch=%s | conflicting_batches=%s",
            len(conflicts),
            batch_id,
            list({c["batch_id"] for c in conflicts}),
        )
        return False

    for obj, record_id in targets:
        conn.execute(
            """
            INSERT INTO locks (lock_id, batch_id, object_api_name, record_id, lock_type)
            VALUES (?, ?, ?, ?, 'RECORD')
            """,
            (str(uuid.uuid4()), batch_id, obj, record_id),
        )
    conn.commit()

    logger.info(
        "acquire_locks: %d locks acquired | batch=%s", len(targets), batch_id
    )
    return True


def release_locks(batch_id: str) -> None:
    """
    Release all locks held by a batch.

    Safe to call multiple times — deletes 0 rows if locks are already gone.
    """
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM locks WHERE batch_id = ?", (batch_id,)
    )
    conn.commit()
    logger.info(
        "release_locks: %d locks released | batch=%s", cursor.rowcount, batch_id
    )


def check_conflicts(operations: list) -> list[dict]:
    """
    Return existing lock rows that conflict with the given operations.

    Used for pre-flight checks and informational reporting.
    Does NOT acquire any locks.
    """
    targets = [
        (op.object_api_name, op.record_id)
        for op in operations
        if getattr(op, "record_id", None)
    ]
    return _find_conflicts(targets, exclude_batch_id=None)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_conflicts(
    targets: list[tuple[str, str]],
    exclude_batch_id: str | None,
) -> list[dict]:
    """
    Return any active locks on the given (object, record_id) pairs.

    Active = batch status in PENDING_APPROVAL, APPROVED, or EXECUTING.
    Optionally exclude a specific batch_id (our own batch) from the check.
    """
    if not targets:
        return []

    conn = get_connection()
    placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
    conflicts = []

    for obj, record_id in targets:
        if exclude_batch_id:
            rows = conn.execute(
                f"""
                SELECT l.*, b.status
                FROM locks l
                JOIN batches b ON l.batch_id = b.batch_id
                WHERE l.object_api_name = ?
                  AND l.record_id = ?
                  AND l.batch_id != ?
                  AND b.status IN ({placeholders})
                """,
                (obj, record_id, exclude_batch_id, *_ACTIVE_STATUSES),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT l.*, b.status
                FROM locks l
                JOIN batches b ON l.batch_id = b.batch_id
                WHERE l.object_api_name = ?
                  AND l.record_id = ?
                  AND b.status IN ({placeholders})
                """,
                (obj, record_id, *_ACTIVE_STATUSES),
            ).fetchall()

        conflicts.extend([dict(r) for r in rows])

    return conflicts
