"""
approval/handler.py

Handles coworker Approve / Deny actions on write plans.

Called by slack/listener.py when the coworker clicks a button on an
approval request message.

handle_approve():
  1. Validate batch is in PENDING_APPROVAL state
  2. Update batch status → APPROVED
  3. Execute the batch (synchronous — runs SF operations inline)
  4. Notify the requester of the result via DM
  5. Update the approval message to show the outcome

handle_deny():
  1. Validate batch is in PENDING_APPROVAL state
  2. Update batch status → DENIED with reason
  3. Release any locks (defensive — locks aren't normally held at this stage)
  4. Notify the requester via DM
"""

import logging

from batchqueue.batch import get_batch, update_batch_status
from batchqueue.executor import execute_batch
from batchqueue.locks import release_locks
from batchqueue.operations import get_operations

logger = logging.getLogger(__name__)


def handle_approve(
    batch_id: str,
    approver_id: str,
    message_ts: str,
    channel_id: str,
    notify_fn=None,
) -> str:
    """
    Approve a pending batch and execute it.

    Args:
        batch_id:    UUID of the batch to approve
        approver_id: Slack user ID of the coworker approving
        message_ts:  Slack message ts of the approval request (for updating the message)
        channel_id:  Slack channel of the approval request message
        notify_fn:   optional callable(user_id, text, blocks) for sending DMs
                     (injected for testability; slack/listener.py passes messenger.send_dm)

    Returns:
        Status string for updating the approval message.
    """
    batch = get_batch(batch_id)
    if not batch:
        logger.warning("handle_approve: batch not found | batch=%s", batch_id)
        return f"Batch `{batch_id[:8]}` not found."

    if batch["status"] != "PENDING_APPROVAL":
        logger.warning(
            "handle_approve: wrong status | batch=%s | status=%s",
            batch_id, batch["status"],
        )
        return f"Batch is already *{batch['status']}* — cannot approve."

    update_batch_status(
        batch_id,
        "APPROVED",
        approved_by=approver_id,
        approver_message_ts=message_ts,
    )
    logger.info("handle_approve: batch approved | batch=%s | by=%s", batch_id, approver_id)

    try:
        result = execute_batch(batch_id)

        ops = get_operations(batch_id)
        completed = sum(1 for o in ops if o["status"] == "COMPLETED")
        status_msg = (
            f":white_check_mark: *Approved and executed* by <@{approver_id}> — "
            f"{completed}/{len(ops)} operation(s) completed."
        )

        if notify_fn:
            from approval.formatter import format_execution_result
            ops = get_operations(batch_id)
            fallback, blocks = format_execution_result(
                batch_id, "COMPLETED", ops, plan_summary=batch.get("summary", ""),
            )
            notify_fn(batch["user_id"], fallback, blocks)

        logger.info(
            "handle_approve: execution complete | batch=%s | ops=%d",
            batch_id, result["operations_run"],
        )
        return status_msg

    except Exception as exc:
        logger.error(
            "handle_approve: execution failed | batch=%s | error=%s",
            batch_id, exc, exc_info=True,
        )
        if notify_fn:
            notify_fn(
                batch["user_id"],
                f":x: Your plan was approved but execution failed: {exc}",
                None,
            )
        return f":x: Approved but execution failed: {exc}"


def handle_deny(
    batch_id: str,
    denier_id: str,
    reason: str = "",
    notify_fn=None,
) -> str:
    """
    Deny a pending batch.

    Args:
        batch_id:  UUID of the batch to deny
        denier_id: Slack user ID of the coworker denying
        reason:    optional free-text reason
        notify_fn: optional callable(user_id, text, blocks) for sending DMs

    Returns:
        Status string for updating the approval message.
    """
    batch = get_batch(batch_id)
    if not batch:
        logger.warning("handle_deny: batch not found | batch=%s", batch_id)
        return f"Batch `{batch_id[:8]}` not found."

    if batch["status"] != "PENDING_APPROVAL":
        logger.warning(
            "handle_deny: wrong status | batch=%s | status=%s",
            batch_id, batch["status"],
        )
        return f"Batch is already *{batch['status']}* — cannot deny."

    denial_reason = reason or "No reason provided."
    update_batch_status(batch_id, "DENIED", denial_reason=denial_reason)
    release_locks(batch_id)  # defensive — locks shouldn't be held here

    logger.info(
        "handle_deny: batch denied | batch=%s | by=%s | reason=%r",
        batch_id, denier_id, denial_reason,
    )

    if notify_fn:
        from approval.formatter import format_denial_notice
        fallback, blocks = format_denial_notice(
            batch_id,
            plan_summary=batch.get("summary", ""),
            denied_by=denier_id,
            reason=denial_reason,
        )
        notify_fn(batch["user_id"], fallback, blocks)

    return f":no_entry: *Denied* by <@{denier_id}>. Reason: {denial_reason}"
