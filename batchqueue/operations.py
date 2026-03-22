"""
batchqueue/operations.py

SQLite persistence for individual batch operation rows.

Each row in the `operations` table represents one atomic Salesforce write.
The executor reads these rows, executes them in sequence_order, and writes
results/errors back via update_operation_status().

operation_type mapping (DB uses uppercase):
  Operation.method="update" → operation_type="UPDATE"
  Operation.method="create" → operation_type="CREATE"
"""

import json
import logging
import uuid
from datetime import datetime

from db.connection import get_connection

logger = logging.getLogger(__name__)


def create_operation_rows(batch_id: str, operations: list) -> list[str]:
    """
    Insert all operations from a BatchPlan into the operations table.

    Accepts Operation dataclass objects (from orchestrator/planner.py).
    Returns list of operation_ids in execution order.
    """
    conn = get_connection()
    ids = []

    for op in sorted(operations, key=lambda o: o.order):
        op_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO operations (
                operation_id, batch_id, sequence_order, operation_type,
                target_object, target_record_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                op_id,
                batch_id,
                op.order,
                op.method.upper(),          # "create" → "CREATE"
                op.object_api_name,
                op.record_id,               # None for creates
                json.dumps(op.payload),
            ),
        )
        ids.append(op_id)

    conn.commit()
    logger.info(
        "create_operation_rows: %d operations inserted | batch=%s", len(ids), batch_id
    )
    return ids


def get_operations(batch_id: str) -> list[dict]:
    """
    Fetch all operations for a batch, ordered by sequence_order.

    Returns list of dicts with payload and result deserialized from JSON.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM operations WHERE batch_id = ? ORDER BY sequence_order ASC",
        (batch_id,),
    ).fetchall()

    result = []
    for row in rows:
        r = dict(row)
        r["payload"] = json.loads(r["payload"] or "{}")
        r["result"] = json.loads(r["result"]) if r.get("result") else None
        result.append(r)

    return result


def update_operation_status(
    operation_id: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """
    Update an operation's status after execution.

    Args:
        operation_id: UUID of the operation row
        status:       "COMPLETED" | "FAILED" | "SKIPPED"
        result:       dict returned by SF on success
        error:        error string on failure
    """
    conn = get_connection()
    executed_at = datetime.utcnow() if status != "SKIPPED" else None
    conn.execute(
        """
        UPDATE operations
        SET status=?, result=?, error=?, executed_at=?
        WHERE operation_id=?
        """,
        (
            status,
            json.dumps(result) if result is not None else None,
            error,
            executed_at,
            operation_id,
        ),
    )
    conn.commit()
    logger.info("update_operation_status: op=%s → %s", operation_id[:8], status)
