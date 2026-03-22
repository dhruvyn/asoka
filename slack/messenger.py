"""
slack/messenger.py

Thin wrapper around Slack WebClient for the three outbound message operations
Asoka needs:

  send_dm(user_id, text, blocks)       — DM to a specific user
  update_message(channel, ts, text, blocks) — edit an existing message
  send_to_coworker(text, blocks)       — DM to the coworker (cfg.coworker_slack_id)

All functions raise SlackApiError on failure — callers are responsible for
catching and surfacing errors.
"""

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import cfg

logger = logging.getLogger(__name__)

# Single client instance shared by all three functions.
_client: WebClient | None = None


def _get_client() -> WebClient:
    global _client
    if _client is None:
        _client = WebClient(token=cfg.slack_bot_token)
    return _client


def send_dm(user_id: str, text: str, blocks: list[dict] | None = None) -> None:
    """
    Send a direct message to a Slack user.

    Args:
        user_id: Slack user ID (e.g. "U04XXXXXXX")
        text:    Fallback plain text (required; shown in notifications)
        blocks:  Optional Block Kit payload for rich formatting
    """
    client = _get_client()
    kwargs: dict = {"channel": user_id, "text": text}
    if blocks:
        kwargs["blocks"] = blocks

    try:
        client.chat_postMessage(**kwargs)
        logger.info("send_dm: sent | user=%s", user_id)
    except SlackApiError as e:
        logger.error("send_dm: failed | user=%s | error=%s", user_id, e.response["error"])
        raise


def update_message(
    channel: str, ts: str, text: str, blocks: list[dict] | None = None
) -> None:
    """
    Update (edit) an existing Slack message in-place.

    Args:
        channel: Slack channel ID where the message lives
        ts:      Message timestamp (the unique message ID in Slack)
        text:    New fallback plain text
        blocks:  Optional Block Kit replacement payload
    """
    client = _get_client()
    kwargs: dict = {"channel": channel, "ts": ts, "text": text}
    if blocks:
        kwargs["blocks"] = blocks

    try:
        client.chat_update(**kwargs)
        logger.info("update_message: updated | channel=%s | ts=%s", channel, ts)
    except SlackApiError as e:
        logger.error(
            "update_message: failed | channel=%s | ts=%s | error=%s",
            channel, ts, e.response["error"],
        )
        raise


def send_to_coworker(text: str, blocks: list[dict] | None = None) -> None:
    """
    Send a DM to the designated coworker (approval agent).

    Uses cfg.coworker_slack_id — configured via COWORKER_SLACK_ID in .env.
    """
    send_dm(cfg.coworker_slack_id, text, blocks)
    logger.info("send_to_coworker: sent | coworker=%s", cfg.coworker_slack_id)
