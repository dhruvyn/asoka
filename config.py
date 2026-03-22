"""
config.py

Responsibilities:
  - Load environment variables from .env once at import time
  - Validate that every required variable is present
  - Expose a single typed Config dataclass that every module imports

Why a dataclass instead of reading os.environ directly?
  If any module reads os.environ["SLACK_BOT_TOKEN"] directly:
    - typos in key names fail silently (KeyError at runtime, not startup)
    - no central list of what the app needs
    - no type coercion (everything is a string, including "48" for hours)
  A Config object gives: one validated source of truth, typed fields,
  and a startup crash with a clear message if anything is missing.

Input:  .env file in the working directory (loaded by python-dotenv)
Output: a Config instance at module level — import and use directly

Usage:
    from config import cfg

    print(cfg.slack_bot_token)
    print(cfg.approval_expiry_hours)   # already an int, not "48"
"""

import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

# Load .env into os.environ before we read anything.
# override=False means real environment variables take precedence over .env —
# useful when deploying: set real env vars in the host and .env is ignored.
load_dotenv(override=False)

logger = logging.getLogger(__name__)


def _require(key: str) -> str:
    """
    Read an environment variable. Raise clearly if it is missing or empty.

    Called for every required variable during Config.__post_init__.
    The bot crashes at startup — not mid-operation — if anything is missing.
    """
    value = os.environ.get(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is missing or empty. "
            f"Check your .env file."
        )
    return value


def _optional(key: str, default: str) -> str:
    """Read an optional environment variable, returning default if absent."""
    return os.environ.get(key, default).strip() or default


@dataclass(frozen=True)
class Config:
    """
    Typed, immutable configuration for the entire bot.

    frozen=True means no module can accidentally mutate cfg after startup.
    Every field is set once in __post_init__ and never changes.
    """

    # --- Slack ---
    slack_bot_token: str       # xoxb-... used to call Slack APIs (send messages)
    slack_app_token: str       # xapp-... used for Socket Mode (receive events)
    coworker_slack_id: str     # Slack user ID of the human approver, e.g. "U04XXXXXXX"

    # --- Salesforce ---
    sf_username: str
    sf_password: str
    sf_security_token: str     # appended to password for IP-unrestricted auth
    sf_domain: str             # "test" for sandbox, "login" for production

    # --- Anthropic ---
    anthropic_api_key: str

    # --- App paths ---
    db_path: str               # SQLite file, e.g. "asoka.db"
    chroma_path: str           # ChromaDB persistence directory
    knowledge_path: str        # path to rules.md

    # --- TTL thresholds (stored as int hours) ---
    approval_reminder_hours: int   # send reminder after this many hours
    approval_expiry_hours: int     # auto-expire batch after this many hours

    def __post_init__(self) -> None:
        """
        Validate all fields immediately after the dataclass is constructed.

        dataclass(frozen=True) sets fields via object.__setattr__ internally,
        so __post_init__ is the right place to run validation logic.
        We don't raise here — _require() raises for us.
        """
        # Sanity check: reminder must be before expiry
        if self.approval_reminder_hours >= self.approval_expiry_hours:
            raise ValueError(
                f"APPROVAL_REMINDER_HOURS ({self.approval_reminder_hours}) "
                f"must be less than APPROVAL_EXPIRY_HOURS ({self.approval_expiry_hours})."
            )


def _load() -> Config:
    """
    Build the Config object by reading environment variables.
    Called once at module import time. The result is stored in `cfg`.
    """
    return Config(
        # Slack
        slack_bot_token=_require("SLACK_BOT_TOKEN"),
        slack_app_token=_require("SLACK_APP_TOKEN"),
        coworker_slack_id=_require("COWORKER_SLACK_ID"),

        # Salesforce
        sf_username=_require("SF_USERNAME"),
        sf_password=_require("SF_PASSWORD"),
        sf_security_token=_require("SF_SECURITY_TOKEN"),
        sf_domain=_optional("SF_DOMAIN", "test"),

        # Anthropic
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),

        # App paths
        db_path=_optional("DB_PATH", "asoka.db"),
        chroma_path=_optional("CHROMA_PATH", "chroma_store"),
        knowledge_path=_optional("KNOWLEDGE_PATH", "knowledge/rules.md"),

        # TTL — coerce from string to int, with sensible defaults
        approval_reminder_hours=int(_optional("APPROVAL_REMINDER_HOURS", "24")),
        approval_expiry_hours=int(_optional("APPROVAL_EXPIRY_HOURS", "48")),
    )


# The single instance every module imports.
# Constructed once when Python first imports this module.
cfg: Config = _load()

logger.debug(
    "Config loaded | db=%s | chroma=%s | sf_domain=%s | coworker=%s",
    cfg.db_path, cfg.chroma_path, cfg.sf_domain, cfg.coworker_slack_id,
)
