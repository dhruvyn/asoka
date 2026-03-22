"""
batchqueue/batch.py

SQLite persistence for Batch records.

A Batch tracks the full lifecycle of a write plan — from the moment it is
sent for approval through execution and completion.

Status flow:
  PENDING_APPROVAL — sent to coworker, awaiting Approve/Deny
  APPROVED         — coworker approved; executor picks it up
  EXECUTING        — executor is running operations
  COMPLETED        — all operations finished successfully
  DENIED           — coworker denied it
  EXPIRED          — approval TTL elapsed without a response
  FAILED           — one or more operations failed during execution
"""

import json
import logging
import uuid
from datetime import datetime

from db.connection import get_connection
from batchqueue.operations import create_operation_rows

logger = logging.getLogger(__name__)


def create_batch(
    user_id: str,
    conversation_id: str,
    plan,
    status: str = "PENDING_APPROVAL",
) -> str:
    """
    Persist a BatchPlan to the database.

    Inserts one row into `batches` and one row per operation into `operations`.

    Args:
        user_id:         Slack user ID of the requester
        conversation_id: Slack DM channel ID (used when sending result notifications)
        plan:            BatchPlan dataclass from orchestrator/planner.py
        status:          initial batch status (default: PENDING_APPROVAL)

    Returns:
        batch_id (UUID string)
    """
    batch_id = str(uuid.uuid4())
    conn = get_connection()

    conn.execute(
        """
        INSERT INTO batches (
            batch_id, user_id, conversation_id, status, summary, assumptions
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            user_id,
            conversation_id,
            status,
            plan.summary,
            json.dumps(plan.assumptions or []),
        ),
    )
    conn.commit()

    create_operation_rows(batch_id, plan.operations)

    logger.info(
        "create_batch: batch_id=%s | user=%s | ops=%d | status=%s",
        batch_id, user_id, len(plan.operations), status,
    )
    return batch_id


def get_batch(batch_id: str) -> dict | None:
    """
    Fetch a batch row by ID. Returns None if not found.
    assumptions is deserialized from JSON to a list.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()

    if row is None:
        return None

    r = dict(row)
    r["assumptions"] = json.loads(r.get("assumptions") or "[]")
    return r


def update_batch_status(
    batch_id: str,
    status: str,
    approved_by: str | None = None,
    denial_reason: str | None = None,
    approver_message_ts: str | None = None,
) -> None:
    """
    Update a batch's status and optional approval/denial metadata.
    Always updates updated_at to now. Sets approved_at when status=APPROVED.
    """
    conn = get_connection()
    approved_at = datetime.utcnow() if status == "APPROVED" else None

    conn.execute(
        """
        UPDATE batches
        SET status=?, updated_at=?,
            approved_at=COALESCE(?, approved_at),
            approved_by=COALESCE(?, approved_by),
            denial_reason=COALESCE(?, denial_reason),
            approver_message_ts=COALESCE(?, approver_message_ts)
        WHERE batch_id=?
        """,
        (
            status,
            datetime.utcnow(),
            approved_at,
            approved_by,
            denial_reason,
            approver_message_ts,
            batch_id,
        ),
    )
    conn.commit()
    logger.info("update_batch_status: batch=%s → %s", batch_id[:8], status)


def get_pending_approval_batches() -> list[dict]:
    """
    Return all PENDING_APPROVAL batches, ordered oldest first.
    Used by the TTL checker to send reminders and expire stale approvals.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM batches WHERE status = 'PENDING_APPROVAL' ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_approved_batches() -> list[dict]:
    """
    Return all APPROVED batches waiting for the executor, ordered oldest first.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM batches WHERE status = 'APPROVED' ORDER BY updated_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]
