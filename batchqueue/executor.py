"""
batchqueue/executor.py

Batch execution engine — the final step in the write approval flow.

Called by approval/handler.py after a coworker approves a plan.

Flow:
  1. Load batch + operations from SQLite
  2. Acquire record locks (fail fast if conflict)
  3. Set batch status = EXECUTING
  4. For each operation in sequence_order:
       a. Resolve {{op_N.result.id}} template references in payload
       b. Call salesforce/writer.py (execute_update or execute_create)
       c. Store result or error in the operation row
       d. If an operation fails: mark remaining ops SKIPPED, batch FAILED
  5. All ops complete → batch COMPLETED
  6. Release locks (always, even on failure — try/finally)

Template references:
  A CREATE operation may produce an ID that a subsequent UPDATE needs.
  Payload values like "{{op_1.result.id}}" are replaced with the actual
  SF ID returned from operation 1 before operation 2 is executed.
"""

import logging
import re

from batchqueue.batch import get_batch, update_batch_status
from batchqueue.locks import acquire_locks, release_locks
from batchqueue.operations import get_operations, update_operation_status
from salesforce.writer import execute_update, execute_create

logger = logging.getLogger(__name__)


def execute_batch(batch_id: str) -> dict:
    """
    Execute all operations in an APPROVED batch.

    Args:
        batch_id: UUID of the batch to execute

    Returns:
        dict with keys: batch_id, status, operations_run, results

    Raises:
        ValueError if batch not found or not in APPROVED status
        The batch is marked FAILED if any operation raises an exception.
    """
    batch = get_batch(batch_id)
    if not batch:
        raise ValueError(f"Batch {batch_id} not found")
    if batch["status"] != "APPROVED":
        raise ValueError(
            f"Batch {batch_id} cannot be executed (status={batch['status']})"
        )

    operations = get_operations(batch_id)
    if not operations:
        logger.warning("execute_batch: no operations | batch=%s", batch_id)
        update_batch_status(batch_id, "COMPLETED")
        return {"batch_id": batch_id, "status": "COMPLETED", "operations_run": 0, "results": {}}

    # Build lightweight op objects for lock acquisition
    lock_targets = [_OpProxy(op) for op in operations]
    if not acquire_locks(batch_id, lock_targets):
        raise ValueError(
            f"Batch {batch_id} could not acquire locks — conflicting batch is active"
        )

    update_batch_status(batch_id, "EXECUTING")
    logger.info(
        "execute_batch: starting | batch=%s | ops=%d", batch_id, len(operations)
    )

    # operation order → SF result dict (for template resolution)
    results: dict[int, dict] = {}

    try:
        for op in operations:
            op_id = op["operation_id"]
            op_type = op["operation_type"]
            obj = op["target_object"]
            record_id = op["target_record_id"]
            sequence = op["sequence_order"]

            # Resolve any {{op_N.result.id}} references before executing
            payload = _resolve_templates(op["payload"], results)
            if record_id and "{{" in str(record_id):
                resolved = _resolve_templates({"_rid": record_id}, results)
                record_id = resolved["_rid"]

            logger.info(
                "execute_batch: executing op %d | %s %s id=%s | batch=%s",
                sequence, op_type, obj, record_id, batch_id,
            )

            if op_type == "UPDATE":
                if not record_id:
                    raise ValueError(
                        f"UPDATE operation {op_id} has no record_id — cannot execute"
                    )
                sf_result = execute_update(obj, record_id, payload)

            elif op_type == "CREATE":
                sf_result = execute_create(obj, payload)

            else:
                logger.warning(
                    "execute_batch: unknown operation_type=%s op=%s — skipping",
                    op_type, op_id,
                )
                update_operation_status(op_id, "SKIPPED")
                continue

            results[sequence] = sf_result
            update_operation_status(op_id, "COMPLETED", result=sf_result)
            logger.info(
                "execute_batch: op %d COMPLETED | result=%s | batch=%s",
                sequence, sf_result, batch_id,
            )

        update_batch_status(batch_id, "COMPLETED")
        logger.info("execute_batch: COMPLETED | batch=%s | ops=%d", batch_id, len(operations))

    except Exception as exc:
        logger.error(
            "execute_batch: FAILED | batch=%s | error=%s", batch_id, exc, exc_info=True
        )
        # Mark all still-PENDING operations as SKIPPED
        for op in operations:
            if op["status"] == "PENDING":
                update_operation_status(op["operation_id"], "SKIPPED")
        update_batch_status(batch_id, "FAILED")
        raise

    finally:
        release_locks(batch_id)

    return {
        "batch_id": batch_id,
        "status": "COMPLETED",
        "operations_run": len(results),
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

class _OpProxy:
    """
    Lightweight proxy so lock acquisition can call op.record_id / op.method
    without needing to import the Operation dataclass here.
    """
    def __init__(self, op_dict: dict):
        self.object_api_name = op_dict["target_object"]
        self.record_id = op_dict["target_record_id"]
        self.method = op_dict["operation_type"].lower()  # "update" or "create"


def _resolve_templates(payload: dict, results: dict[int, dict]) -> dict:
    """
    Replace {{op_N.result.FIELD}} template references with actual values
    from prior operation results.

    Example: {"AccountId": "{{op_1.result.id}}"} → {"AccountId": "001abc..."}

    Unknown references (op not yet executed, or field not in result) are left
    as-is so the error surfaces clearly when SF rejects the malformed payload.
    """
    _TEMPLATE_RE = re.compile(r"\{\{op_(\d+)\.result\.(\w+)\}\}")

    resolved = {}
    for key, value in payload.items():
        if isinstance(value, str) and "{{" in value:
            def _replace(match: re.Match) -> str:
                order = int(match.group(1))
                field = match.group(2)
                if order in results and field in results[order]:
                    return str(results[order][field])
                logger.warning(
                    "_resolve_templates: ref {{op_%d.result.%s}} not resolvable yet",
                    order, field,
                )
                return match.group(0)  # leave unchanged

            value = _TEMPLATE_RE.sub(_replace, value)
        resolved[key] = value

    return resolved
