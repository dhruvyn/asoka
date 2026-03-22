"""
approval/formatter.py

Slack Block Kit message formatters for the approval flow.

format_approval_request() — sent to the coworker when a plan needs approval
format_execution_result() — sent to the requester after execution completes
format_denial_notice()    — sent to the requester when the plan is denied

All functions return (fallback_text, blocks) tuples:
  fallback_text: plain-text summary for notifications/accessibility
  blocks:        list of Slack Block Kit dicts for rich formatting

Block Kit limits: max 50 blocks per message; text fields max 3000 chars.
"""

import logging

logger = logging.getLogger(__name__)

# Max chars for any single text block before truncation
_MAX_TEXT = 2800


def format_approval_request(
    batch_id: str,
    plan,
    requester_id: str,
) -> tuple[str, list[dict]]:
    """
    Format a BatchPlan as a Slack approval request for the coworker.

    Includes:
      - Who requested it
      - Plan summary
      - All operations with fields and reasons
      - Assumptions, risks, pre-flight issues
      - Approve / Deny action buttons (value = batch_id)

    Args:
        batch_id:     UUID of the persisted batch
        plan:         BatchPlan dataclass from orchestrator/planner.py
        requester_id: Slack user ID of the person who created the plan

    Returns:
        (fallback_text, blocks)
    """
    fallback = f"Approval request from <@{requester_id}>: {plan.summary}"
    blocks: list[dict] = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "Write Plan — Approval Required"},
    })

    # Requester + summary
    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Requested by:* <@{requester_id}>"},
            {"type": "mrkdwn", "text": f"*Batch:* `{batch_id[:8]}...`"},
        ],
    })
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Plan:* {plan.summary}"},
    })

    blocks.append({"type": "divider"})

    # Operations
    if plan.operations:
        op_lines = [f"*Operations ({len(plan.operations)}):*"]
        for op in plan.operations:
            record_ref = op.record_id or "(new record)"
            op_lines.append(f"  {op.order}. `{op.method.upper()}` *{op.object_api_name}* {record_ref}")
            for field, val in op.payload.items():
                op_lines.append(f"       `{field}` = `{val}`")
            op_lines.append(f"       _{op.reason}_")

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate("\n".join(op_lines)),
            },
        })

    # Assumptions
    if plan.assumptions:
        lines = ["*Assumptions:*"] + [f"  • {a}" for a in plan.assumptions]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })

    # Risks
    if plan.risks:
        lines = ["*Risks:*"] + [f"  • {r}" for r in plan.risks]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate("\n".join(lines))},
        })

    # Pre-flight blockers (shown prominently)
    if plan.pre_flight_issues:
        lines = [":warning: *Blockers (must resolve before execution):*"] + \
                [f"  • {i}" for i in plan.pre_flight_issues]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate("\n".join(lines))},
        })

    blocks.append({"type": "divider"})

    # Action buttons
    deny_text = "Deny"
    if plan.pre_flight_issues:
        deny_text = "Deny (has blockers)"

    blocks.append({
        "type": "actions",
        "block_id": f"approval_{batch_id}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve"},
                "style": "primary",
                "action_id": "approve_batch",
                "value": batch_id,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Approve this plan?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": f"This will execute {len(plan.operations)} operation(s) on Salesforce.",
                    },
                    "confirm": {"type": "plain_text", "text": "Yes, execute"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": deny_text},
                "style": "danger",
                "action_id": "deny_batch",
                "value": batch_id,
            },
        ],
    })

    return fallback, blocks


def format_execution_result(
    batch_id: str,
    status: str,
    operations: list[dict],
    plan_summary: str = "",
) -> tuple[str, list[dict]]:
    """
    Format an execution result notification for the requester.

    Args:
        batch_id:     UUID of the executed batch
        status:       "COMPLETED" | "FAILED"
        operations:   list of operation dicts from get_operations()
        plan_summary: original plan summary for context

    Returns:
        (fallback_text, blocks)
    """
    icon = ":white_check_mark:" if status == "COMPLETED" else ":x:"
    status_label = "Executed successfully" if status == "COMPLETED" else "Execution failed"
    fallback = f"{status_label}: {plan_summary or batch_id[:8]}"

    blocks: list[dict] = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"{icon} {status_label}"},
    })

    if plan_summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Plan:* {plan_summary}"},
        })

    if operations:
        lines = [f"*Operations ({len(operations)}):*"]
        for op in operations:
            op_icon = (
                ":white_check_mark:" if op["status"] == "COMPLETED"
                else ":x:" if op["status"] == "FAILED"
                else ":fast_forward:"
            )
            lines.append(
                f"  {op_icon} {op['sequence_order']}. "
                f"`{op['operation_type']}` *{op['target_object']}*"
                + (f" `{op['target_record_id']}`" if op["target_record_id"] else "")
            )
            if op.get("result"):
                result = op["result"]
                if isinstance(result, dict) and result.get("id"):
                    lines.append(f"       Result ID: `{result['id']}`")
            if op.get("error"):
                lines.append(f"       Error: _{op['error']}_")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate("\n".join(lines))},
        })

    return fallback, blocks


def format_denial_notice(
    batch_id: str,
    plan_summary: str,
    denied_by: str,
    reason: str = "",
) -> tuple[str, list[dict]]:
    """
    Format a denial notice for the requester.

    Returns:
        (fallback_text, blocks)
    """
    fallback = f"Your plan was denied by <@{denied_by}>."
    reason_text = reason or "No reason provided."

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":no_entry: *Plan denied* by <@{denied_by}>\n"
                    f"*Plan:* {plan_summary}\n"
                    f"*Reason:* {reason_text}"
                ),
            },
        }
    ]
    return fallback, blocks


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _truncate(text: str) -> str:
    """Truncate to Slack's block text limit with a notice."""
    if len(text) <= _MAX_TEXT:
        return text
    return text[:_MAX_TEXT] + "\n_(truncated)_"
