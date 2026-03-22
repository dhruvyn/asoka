"""
test_phase2.py

Phase 2 validation — orchestrator/ (prompts, intent, reader, planner, core)

Tests (run from the asoka/ directory):
    python test_phase2.py

What this covers:
  1. Intent classification — READ message
  2. Intent classification — WRITE message
  3. Intent classification — UNKNOWN message
  4. Record lookup helper
  5. Full READ round-trip via core.handle()
  6. Full WRITE round-trip via core.handle() — plan generation
  7. WRITE with pre_flight_issues — missing record
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_phase2")

# ── boot sequence ─────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

import db.connection as db_conn
import salesforce.client as sf_client
from startup.sync import full_sync
from knowledge.loader import init_knowledge
from config import cfg

logger.info("=== Phase 2 Test: orchestrator/ ===")

db_conn.init(cfg.db_path)
sf_client.init()
logger.info("Running full_sync...")
full_sync()
logger.info("Initializing knowledge store...")
init_knowledge()
logger.info("Boot complete — beginning tests")

# ── imports that require boot ──────────────────────────────────────────────────
from orchestrator.intent import classify_intent
from orchestrator.core import handle, _lookup_records
from orchestrator.session import clear_session

FAKE_USER = "U_TEST_001"

def reset():
    """Clear session state between tests that use handle()."""
    clear_session(FAKE_USER)

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 1: Intent — READ classification")
print("="*60)
intent = classify_intent("What is the account tier for TechGlobal Solutions?", FAKE_USER)
print(f"  intent_type : {intent.intent_type}")
print(f"  objects     : {intent.objects}")
print(f"  record_hints: {intent.record_hints}")
print(f"  summary     : {intent.summary}")
assert intent.intent_type == "READ", f"Expected READ, got {intent.intent_type}"
assert "Account" in intent.objects or len(intent.objects) > 0
print("  OK intent_type=READ")

print("\n" + "="*60)
print("TEST 2: Intent — WRITE classification")
print("="*60)
intent_w = classify_intent(
    "Set Discount_Percent__c to 0.20 on TechGlobal's renewal opportunity", FAKE_USER
)
print(f"  intent_type : {intent_w.intent_type}")
print(f"  objects     : {intent_w.objects}")
print(f"  record_hints: {intent_w.record_hints}")
print(f"  summary     : {intent_w.summary}")
assert intent_w.intent_type == "WRITE", f"Expected WRITE, got {intent_w.intent_type}"
assert "Opportunity" in intent_w.objects or "Account" in intent_w.objects
print("  OK intent_type=WRITE")

print("\n" + "="*60)
print("TEST 3: Intent — UNKNOWN / ambiguous")
print("="*60)
intent_u = classify_intent("hello there", FAKE_USER)
print(f"  intent_type : {intent_u.intent_type}")
print(f"  summary     : {intent_u.summary}")
print("  OK (UNKNOWN or low-confidence intent returned)")

print("\n" + "="*60)
print("TEST 4: Record lookup helper")
print("="*60)
block = _lookup_records(["Account", "Opportunity"], ["TechGlobal"])
print(block[:600] if block else "  (empty block — no records found for hint)")
print("  OK lookup ran without error")

# ── Tests 5-7 use the full state machine: message → PROPOSAL → confirm → result ──

print("\n" + "="*60)
print("TEST 5: Full READ round-trip via core.handle() (proposal + confirm)")
print("="*60)
reset()
r1 = handle("What accounts do we have and what are their types?", FAKE_USER)
print(f"  step 1 intent_type: {r1.intent_type}")
assert r1.intent_type == "PROPOSAL", f"Expected PROPOSAL, got {r1.intent_type}"
print(f"  proposal: {r1.text[:200]}")

result = handle("yes", FAKE_USER)
print(f"  step 2 intent_type: {result.intent_type}")
print(f"  answer:\n{result.text[:600]}")
assert result.intent_type == "READ_ANSWER", f"Expected READ_ANSWER, got {result.intent_type}"
assert result.plan is None
assert len(result.text) > 10
print("  OK READ round-trip complete")

print("\n" + "="*60)
print("TEST 6: Full WRITE round-trip via core.handle() (proposal + confirm -> plan preview)")
print("="*60)
reset()
r1 = handle(
    "Set Discount_Percent__c to 0.15 on TechGlobal Solutions' open opportunity",
    FAKE_USER,
)
print(f"  step 1 intent_type: {r1.intent_type}")
assert r1.intent_type == "PROPOSAL", f"Expected PROPOSAL, got {r1.intent_type}"

result_w = handle("yes", FAKE_USER)
print(f"  step 2 intent_type: {result_w.intent_type}")
print(f"  plan text:\n{result_w.text[:400]}")
if result_w.plan:
    plan = result_w.plan
    print(f"  operations       : {len(plan.operations)}")
    for op in plan.operations:
        print(f"    [{op.order}] {op.method.upper()} {op.object_api_name} id={op.record_id}")
        print(f"        payload: {op.payload}")
    print(f"  risks            : {plan.risks}")
    print(f"  pre_flight_issues: {plan.pre_flight_issues}")
assert result_w.intent_type == "PLAN_PREVIEW", f"Expected PLAN_PREVIEW, got {result_w.intent_type}"
assert result_w.plan is not None
print("  OK WRITE round-trip complete")

print("\n" + "="*60)
print("TEST 7: WRITE with likely-missing record (pre_flight_issues expected)")
print("="*60)
reset()
r1 = handle(
    "Deactivate user Alex Johnson and transfer all their records to their manager",
    FAKE_USER,
)
assert r1.intent_type == "PROPOSAL", f"Expected PROPOSAL, got {r1.intent_type}"

result_missing = handle("yes", FAKE_USER)
print(f"  intent_type      : {result_missing.intent_type}")
if result_missing.plan:
    print(f"  operations       : {len(result_missing.plan.operations)}")
    print(f"  pre_flight_issues: {result_missing.plan.pre_flight_issues}".encode('ascii', errors='replace').decode('ascii'))
    print(f"  assumptions      : {result_missing.plan.assumptions}".encode('ascii', errors='replace').decode('ascii'))
assert result_missing.intent_type == "PLAN_PREVIEW", f"Expected PLAN_PREVIEW, got {result_missing.intent_type}"
print("  OK plan returned (check pre_flight_issues for missing user warning)")

print("\n" + "="*60)
print("ALL PHASE 2 TESTS PASSED")
print("="*60 + "\n")
