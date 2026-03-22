"""
slack/listener.py

Slack Bolt app — registers all event handlers and action handlers.

Handlers:
  message event (DM)        — route to orchestrator.core.handle()
  /reset slash command      — end session + clear state
  approve_batch action      — handle_approve() → execute batch
  deny_batch action         — handle_deny() → mark batch denied

The Bolt App object is created here and imported by main.py which wraps
it in a SocketModeHandler for WebSocket-based event delivery.
"""

import logging

from slack_bolt import App

from config import cfg
from orchestrator.core import handle, end_session
from batchqueue.batch import create_batch
from approval.formatter import format_approval_request
from approval.handler import handle_approve, handle_deny
from slack.messenger import send_dm, send_to_coworker, update_message

logger = logging.getLogger(__name__)

# ── Bolt app ──────────────────────────────────────────────────────────────────

app = App(token=cfg.slack_bot_token, token_verification_enabled=False)


# ─────────────────────────────────────────────────────────────────────────────
# DM message handler
# ─────────────────────────────────────────────────────────────────────────────

@app.event("message")
def on_message(event, say, client):
    """
    Handle incoming DM messages from users.

    Ignores:
      - bot messages (subtype == "bot_message" or bot_id present)
      - message edits / deletions (subtype != None and not bot)
      - non-DM channels (channel_type != "im")

    Flow:
      1. Pass message to orchestrator handle()
      2. Send the result text back to the user
      3. If SEND_FOR_APPROVAL: persist batch + forward to coworker
    """
    # Filter out bot messages and non-DM events
    subtype = event.get("subtype")
    if subtype in ("bot_message", "message_changed", "message_deleted"):
        return
    if event.get("bot_id"):
        return
    if event.get("channel_type") != "im":
        return

    user_id = event["user"]
    text = event.get("text", "").strip()
    channel = event["channel"]

    if not text:
        return

    logger.info("on_message | user=%s | text=%r", user_id, text[:80])

    try:
        result = handle(text, user_id)
    except Exception as exc:
        logger.error("on_message: orchestrator error | user=%s | error=%s", user_id, exc, exc_info=True)
        say(":x: Something went wrong. Please try again or use `/reset`.")
        return

    # Send the main response to the user
    say(result.text)

    # If the plan was confirmed, persist the batch and forward for approval
    if result.intent_type == "SEND_FOR_APPROVAL" and result.plan is not None:
        try:
            batch_id = create_batch(user_id, channel, result.plan)
            fallback, blocks = format_approval_request(batch_id, result.plan, user_id)
            send_to_coworker(fallback, blocks)
            logger.info(
                "on_message: batch sent for approval | batch=%s | user=%s",
                batch_id, user_id,
            )
        except Exception as exc:
            logger.error(
                "on_message: failed to send for approval | user=%s | error=%s",
                user_id, exc, exc_info=True,
            )
            send_dm(
                user_id,
                ":x: Your plan was confirmed but we failed to send it for approval. "
                "Please try again.",
            )


# ─────────────────────────────────────────────────────────────────────────────
# /reset slash command
# ─────────────────────────────────────────────────────────────────────────────

@app.command("/reset")
def on_reset(ack, body, respond):
    """
    End the current session: synthesize knowledge, clear state.
    Responds ephemerally so only the user sees the confirmation.
    """
    ack()
    user_id = body["user_id"]
    logger.info("on_reset | user=%s", user_id)

    try:
        end_session(user_id)
        respond("Session reset. I've cleared your conversation history and saved any new knowledge.")
    except Exception as exc:
        logger.error("on_reset: error | user=%s | error=%s", user_id, exc, exc_info=True)
        respond(":x: Reset encountered an error, but your session has been cleared.")


# ─────────────────────────────────────────────────────────────────────────────
# Approval action handlers
# ─────────────────────────────────────────────────────────────────────────────

@app.action("approve_batch")
def on_approve_batch(ack, body, client):
    """
    Coworker clicked Approve on an approval request message.

    Flow:
      1. ACK immediately (Slack requires < 3s)
      2. Call handle_approve() — validates, executes, notifies requester
      3. Update the approval message to show the outcome
    """
    ack()

    approver_id = body["user"]["id"]
    message_ts = body["message"]["ts"]
    channel_id = body["channel"]["id"]
    batch_id = body["actions"][0]["value"]

    logger.info(
        "on_approve_batch | batch=%s | approver=%s", batch_id[:8], approver_id
    )

    def _notify(user_id, text, blocks):
        send_dm(user_id, text, blocks)

    status_msg = handle_approve(
        batch_id=batch_id,
        approver_id=approver_id,
        message_ts=message_ts,
        channel_id=channel_id,
        notify_fn=_notify,
    )

    # Update the original approval message so coworker sees the outcome
    try:
        update_message(channel_id, message_ts, status_msg)
    except Exception as exc:
        logger.warning(
            "on_approve_batch: failed to update message | batch=%s | error=%s",
            batch_id[:8], exc,
        )


@app.action("deny_batch")
def on_deny_batch(ack, body, client):
    """
    Coworker clicked Deny on an approval request message.

    Flow:
      1. ACK immediately
      2. Call handle_deny() — validates, marks DENIED, notifies requester
      3. Update the approval message to show the outcome
    """
    ack()

    denier_id = body["user"]["id"]
    message_ts = body["message"]["ts"]
    channel_id = body["channel"]["id"]
    batch_id = body["actions"][0]["value"]

    logger.info(
        "on_deny_batch | batch=%s | denier=%s", batch_id[:8], denier_id
    )

    def _notify(user_id, text, blocks):
        send_dm(user_id, text, blocks)

    status_msg = handle_deny(
        batch_id=batch_id,
        denier_id=denier_id,
        reason="",  # future: prompt for reason via modal
        notify_fn=_notify,
    )

    # Update the original approval message
    try:
        update_message(channel_id, message_ts, status_msg)
    except Exception as exc:
        logger.warning(
            "on_deny_batch: failed to update message | batch=%s | error=%s",
            batch_id[:8], exc,
        )
