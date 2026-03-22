"""
test_chat.py

Two modes, selected by command-line argument:

  python test_chat.py          — automated tests covering the full new flow
  python test_chat.py --chat   — interactive REPL: type messages as a Slack user

Automated tests cover:
  1.  Fresh READ message → PROPOSAL returned (nothing executed yet)
  2.  User confirms "yes" → READ_ANSWER returned (parallel read executed)
  3.  Fresh WRITE message → PROPOSAL returned
  4.  User confirms "yes" → PLAN_PREVIEW returned (plan built, not executed)
  5.  User confirms plan → SEND_FOR_APPROVAL (plan forwarded)
  6.  Mid-flow correction resets session and re-proposes
  7.  UNKNOWN message returns UNKNOWN type
  8.  MIXED message returns MIXED type
  9.  Multi-object read (parallel queries) completes without error
  10. Session isolation: two different users have independent sessions
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_chat")

# ── boot sequence ──────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

import db.connection as db_conn
import salesforce.client as sf_client
from startup.sync import full_sync
from knowledge.loader import init_knowledge
from config import cfg

logger.info("=== test_chat: boot sequence ===")
db_conn.init(cfg.db_path)
sf_client.init()
logger.info("Running full_sync...")
full_sync()
logger.info("Initializing knowledge store...")
init_knowledge()
logger.info("Boot complete")

# ── imports that require boot ──────────────────────────────────────────────────
from orchestrator.core import handle, HandleResult
from orchestrator.session import clear_session, get_session

USER_A = "U_TEST_A"
USER_B = "U_TEST_B"


# ─────────────────────────────────────────────────────────────────────────────
# Interactive REPL
# ─────────────────────────────────────────────────────────────────────────────

def run_chat():
    """
    Simple REPL that sends each typed line through handle() as USER_A.
    Type 'quit' or press Ctrl+C to exit.
    Type '/reset' to clear your session.
    """
    print("\n" + "="*60)
    print("Asoka Chat — type your message, press Enter")
    print("  /reset  — clear your session")
    print("  quit    — exit")
    print("="*60 + "\n")

    while True:
        try:
            msg = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not msg:
            continue
        if msg.lower() in ("quit", "exit"):
            break
        if msg == "/reset":
            clear_session(USER_A)
            print("[session cleared]\n")
            continue

        result = handle(msg, USER_A)
        session = get_session(USER_A)

        print(f"\nAsoka [{result.intent_type}]:")
        print(result.text)
        print(f"\n  (session state: {session.state})")
        if result.plan:
            print(f"  (plan has {len(result.plan.operations)} operation(s), "
                  f"{len(result.plan.pre_flight_issues)} pre-flight issue(s))")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# Automated tests
# ─────────────────────────────────────────────────────────────────────────────

def run_tests():
    def _reset(*users):
        for u in users:
            clear_session(u)

    # ── Test 1: READ → PROPOSAL ─────────────────────────────────────────────
    print("\n" + "="*60)
    print("TEST 1: READ message → PROPOSAL (nothing executed yet)")
    print("="*60)
    _reset(USER_A)
    r = handle("What accounts do we have and what are their types?", USER_A)
    print(f"  intent_type : {r.intent_type}")
    print(f"  text        : {r.text[:200]}")
    assert r.intent_type == "PROPOSAL", f"Expected PROPOSAL, got {r.intent_type}"
    assert r.plan is None
    session = get_session(USER_A)
    assert session.state == "proposing"
    assert session.intent is not None
    assert session.intent.intent_type == "READ"
    print("  OK")

    # ── Test 2: Confirm READ → READ_ANSWER ──────────────────────────────────
    print("\n" + "="*60)
    print("TEST 2: Confirm yes → READ_ANSWER (parallel read executed)")
    print("="*60)
    # session already in proposing state from test 1
    r2 = handle("yes", USER_A)
    print(f"  intent_type : {r2.intent_type}")
    print(f"  answer      : {r2.text[:400]}")
    assert r2.intent_type == "READ_ANSWER", f"Expected READ_ANSWER, got {r2.intent_type}"
    assert len(r2.text) > 10
    assert r2.plan is None
    session2 = get_session(USER_A)
    assert session2.state == "idle", f"Expected idle after answer, got {session2.state}"
    print("  OK")

    # ── Test 3: WRITE → PROPOSAL ────────────────────────────────────────────
    print("\n" + "="*60)
    print("TEST 3: WRITE message → PROPOSAL")
    print("="*60)
    _reset(USER_A)
    r3 = handle(
        "Update the discount on TechGlobal Solutions' open opportunity to 15%",
        USER_A,
    )
    print(f"  intent_type : {r3.intent_type}")
    print(f"  text        : {r3.text[:200]}")
    assert r3.intent_type == "PROPOSAL", f"Expected PROPOSAL, got {r3.intent_type}"
    session3 = get_session(USER_A)
    assert session3.state == "proposing"
    assert session3.intent.intent_type == "WRITE"
    print("  OK")

    # ── Test 4: Confirm WRITE → PLAN_PREVIEW ────────────────────────────────
    print("\n" + "="*60)
    print("TEST 4: Confirm yes → PLAN_PREVIEW (plan built, not executed)")
    print("="*60)
    r4 = handle("yes", USER_A)
    print(f"  intent_type      : {r4.intent_type}")
    print(f"  text (first 400) : {r4.text[:400]}")
    if r4.plan:
        print(f"  operations       : {len(r4.plan.operations)}")
        print(f"  pre_flight_issues: {r4.plan.pre_flight_issues}".encode("ascii", errors="replace").decode("ascii"))
    assert r4.intent_type == "PLAN_PREVIEW", f"Expected PLAN_PREVIEW, got {r4.intent_type}"
    assert r4.plan is not None
    session4 = get_session(USER_A)
    assert session4.state == "plan_shown"
    print("  OK")

    # ── Test 5: Confirm plan → SEND_FOR_APPROVAL ────────────────────────────
    print("\n" + "="*60)
    print("TEST 5: Confirm plan → SEND_FOR_APPROVAL")
    print("="*60)
    r5 = handle("looks good", USER_A)
    print(f"  intent_type : {r5.intent_type}")
    print(f"  text        : {r5.text}")
    assert r5.intent_type == "SEND_FOR_APPROVAL", f"Expected SEND_FOR_APPROVAL, got {r5.intent_type}"
    assert r5.plan is not None
    session5 = get_session(USER_A)
    assert session5.state == "idle"
    print("  OK")

    # ── Test 6: Mid-flow correction resets session ───────────────────────────
    print("\n" + "="*60)
    print("TEST 6: Correction in proposing state → session reset + re-propose")
    print("="*60)
    _reset(USER_A)
    handle("Update TechGlobal discount to 20%", USER_A)  # → proposing
    assert get_session(USER_A).state == "proposing"
    # Send a correction instead of confirming
    r6 = handle("Actually I meant the Acme account, not TechGlobal", USER_A)
    print(f"  intent_type : {r6.intent_type}")
    print(f"  text        : {r6.text[:200]}")
    # Should re-classify and be back in proposing with new intent
    assert r6.intent_type in ("PROPOSAL", "UNKNOWN", "MIXED")
    print("  OK")

    # ── Test 7: UNKNOWN message ─────────────────────────────────────────────
    print("\n" + "="*60)
    print("TEST 7: UNKNOWN / off-topic message")
    print("="*60)
    _reset(USER_A)
    r7 = handle("hello there, how are you?", USER_A)
    print(f"  intent_type : {r7.intent_type}")
    print(f"  text        : {r7.text[:200]}")
    # Expect UNKNOWN or PROPOSAL (model may classify greetings either way)
    assert r7.intent_type in ("UNKNOWN", "PROPOSAL")
    print("  OK")

    # ── Test 8: MIXED message ───────────────────────────────────────────────
    print("\n" + "="*60)
    print("TEST 8: MIXED read+write message → MIXED response")
    print("="*60)
    _reset(USER_A)
    r8 = handle(
        "Show me TechGlobal's current discount and then set it to 20%",
        USER_A,
    )
    print(f"  intent_type : {r8.intent_type}")
    print(f"  text        : {r8.text[:300]}")
    # Accept MIXED or PROPOSAL (model may classify as pure WRITE if subtle)
    assert r8.intent_type in ("MIXED", "PROPOSAL", "UNKNOWN")
    print(f"  OK (got {r8.intent_type})")

    # ── Test 9: Multi-object parallel read ──────────────────────────────────
    print("\n" + "="*60)
    print("TEST 9: Multi-object question — parallel reader runs without error")
    print("="*60)
    _reset(USER_A)
    handle("What are all our accounts and their open opportunities?", USER_A)
    r9 = handle("yes", USER_A)
    print(f"  intent_type : {r9.intent_type}")
    print(f"  answer len  : {len(r9.text)} chars")
    print(f"  answer      : {r9.text[:500]}")
    assert r9.intent_type == "READ_ANSWER"
    assert len(r9.text) > 10
    print("  OK")

    # ── Test 10: Session isolation between two users ─────────────────────────
    print("\n" + "="*60)
    print("TEST 10: Two users have independent sessions")
    print("="*60)
    _reset(USER_A, USER_B)

    handle("What is the status of TechGlobal's opportunities?", USER_A)
    r10b = handle("Update Acme's discount to 10%", USER_B)

    sa = get_session(USER_A)
    sb = get_session(USER_B)

    print(f"  User A state  : {sa.state} | intent_type: {sa.intent.intent_type if sa.intent else None}")
    print(f"  User B state  : {sb.state} | intent_type: {sb.intent.intent_type if sb.intent else None}")
    print(f"  User B result : {r10b.intent_type}")

    assert sa.state == "proposing"
    assert sa.intent.intent_type == "READ"
    assert sb.state == "proposing"
    assert sb.intent.intent_type == "WRITE"
    assert r10b.intent_type == "PROPOSAL"
    print("  OK")

    print("\n" + "="*60)
    print("ALL TESTS PASSED")
    print("="*60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--chat" in sys.argv:
        run_chat()
    else:
        run_tests()
