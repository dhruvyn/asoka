"""
main.py

Asoka — Salesforce CRM assistant for Slack.

Startup sequence:
  1. Load config (validates all required env vars)
  2. Connect to Salesforce
  3. Run full schema sync (objects + fields + validation rules + roles → SQLite)
  4. Initialize knowledge store (ChromaDB)
  5. Start Slack SocketModeHandler (connects via WebSocket, no public URL needed)

Run:
    python main.py
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

# ── Load config first — crashes early if any env var is missing ───────────────
from dotenv import load_dotenv
load_dotenv()

from config import cfg

logger.info("=== Asoka starting up ===")

# ── Database ──────────────────────────────────────────────────────────────────
import db.connection as db_conn
db_conn.init(cfg.db_path)
logger.info("Database ready | path=%s", cfg.db_path)

# ── Salesforce ────────────────────────────────────────────────────────────────
import salesforce.client as sf_client
sf_client.init()
logger.info("Salesforce connected")

# ── Schema sync ───────────────────────────────────────────────────────────────
from startup.sync import full_sync
logger.info("Running full schema sync...")
full_sync()
logger.info("Schema sync complete")

# ── Knowledge store ───────────────────────────────────────────────────────────
from knowledge.loader import init_knowledge
init_knowledge()
logger.info("Knowledge store ready")

# ── Start Slack ───────────────────────────────────────────────────────────────
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack.listener import app

logger.info("Starting Slack Socket Mode handler...")
handler = SocketModeHandler(app, cfg.slack_app_token)
from slack_sdk import WebClient
wc = WebClient(token=cfg.slack_bot_token)
print(wc.auth_test())

logger.info("=== Asoka is running — waiting for messages ===")
handler.start()
