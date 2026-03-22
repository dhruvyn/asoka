"""
test_phase4.py

Phase 4 validation — executor + approval layer

Tests (run from the asoka/ directory):
    python test_phase4.py

What this covers:
  1. Execute a CREATE batch (live SF) — creates a test Opportunity
  2. Execute an UPDATE batch (live SF) — updates the Opportunity we just created
  3. Template reference resolution ({{op_1.result.id}} in a chained plan)
  4. Execution failure handling — bad record_id marks batch FAILED
  5. format_approval_request() block structure
  6. handle_approve() DB state changes (with mock notify_fn)
  7. handle_deny() DB state changes (with mock notify_fn)
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_phase4")

# ── boot sequence ─────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

import db.connection as db_conn
import salesforce.client as sf_client
from startup.sync import full_sync
from knowledge.loader import init_knowledge
from config import cfg

logger.info("=== Phase 4 Test: executor + approval layer ===")

db_conn.init(cfg.db_path)
sf_client.init()
logger.info("Running full_sync...")
full_sync()
logger.info("Initializing knowledge store...")
init_knowledge()
logger.info("Boot complete — beginning tests")

# ── clean up prior test batches ───────────────────────────────────────────────
_conn = db_conn.get_connection()
_conn.execute("DELETE FROM locks WHERE batch_id IN (SELECT batch_id FROM batches WHERE user_id LIKE 'U_TEST_P4%')")
_conn.execute("DELETE FROM operations WHERE batch_id IN (SELECT batch_id FROM batches WHERE user_id LIKE 'U_TEST_P4%')")
_conn.execute("DELETE FROM batches WHERE user_id LIKE 'U_TEST_P4%'")
_conn.commit()
logger.info("Cleaned up prior test state")

# ── imports ────────────────────────────────────────────────────────────────────
from orchestrator.planner import Operation, BatchPlan
from orchestrator.intent import IntentResult
from batchqueue.batch import create_batch, get_batch, update_batch_status
from batchqueue.operations import get_operations
from batchqueue.executor import execute_batch
from approval.formatter import format_approval_request, format_execution_result
from approval.handler import handle_approve, handle_deny
from salesforce.query import find_records, soql as run_soql


def _fake_intent(user_id: str = "U_TEST_P4") -> IntentResult:
    return IntentResult(
        intent_type="WRITE",
        objects=["Opportunity"],
        record_hints=[],
        field_hints=[],
        summary="Phase 4 test plan",
        raw_message="test",
        user_id=user_id,
    )


def _fake_plan(ops: list[Operation], summary: str = "Phase 4 test plan",
               user_id: str = "U_TEST_P4") -> BatchPlan:
    return BatchPlan(
        summary=summary,
        operations=ops,
        assumptions=["Test assumption"],
        risks=[],
        pre_flight_issues=[],
        intent=_fake_intent(user_id),
    )


# ── get TechGlobal Account ID (needed for creating Opportunities) ─────────────
accts = find_records("Account", fields=["Id", "Name"], limit=1)
if not accts:
    print("FATAL: no Account records found — cannot run Phase 4 tests")
    sys.exit(1)
ACCT_ID = accts[0]["Id"]
print(f"\nUsing Account: {accts[0]['Name']} | Id: {ACCT_ID}\n")

# ─────────────────────────────────────────────────────────────────────────────

print("="*60)
print("TEST 1: Execute CREATE batch (live SF)")
print("="*60)

create_op = Operation(
    order=1, object_api_name="Opportunity", method="create",
    record_id=None,
    payload={
        "Name": "ASOKA_TEST_phase4_exec",
        "AccountId": ACCT_ID,
        "StageName": "Prospecting",
        "CloseDate": "2026-12-31",
    },
    reason="Phase 4 executor test create",
)
plan1 = _fake_plan([create_op], summary="Create test Opportunity")
bid1 = create_batch("U_TEST_P4", "C_FAKE", plan1)
update_batch_status(bid1, "APPROVED", approved_by="U_COWORKER")

result1 = execute_batch(bid1)
print(f"  result   : {result1['status']}")
print(f"  ops run  : {result1['operations_run']}")
print(f"  SF result: {result1['results'].get(1)}")

assert result1["status"] == "COMPLETED"
assert result1["operations_run"] == 1
new_opp_id = result1["results"][1].get("id")
assert new_opp_id, "Expected a new record ID from CREATE"

batch_db = get_batch(bid1)
assert batch_db["status"] == "COMPLETED", f"Expected COMPLETED, got {batch_db['status']}"
ops_db = get_operations(bid1)
assert ops_db[0]["status"] == "COMPLETED"

# Verify in Salesforce
opp_check = run_soql(f"SELECT Id, Name FROM Opportunity WHERE Id = '{new_opp_id}'")
assert opp_check and opp_check[0]["Name"] == "ASOKA_TEST_phase4_exec"
print(f"  verified in SF: {opp_check[0]}")
print("  OK CREATE batch executed and verified in Salesforce")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 2: Execute UPDATE batch (live SF)")
print("="*60)

update_op = Operation(
    order=1, object_api_name="Opportunity", method="update",
    record_id=new_opp_id,
    payload={"StageName": "Proposal", "Amount": 50000},
    reason="Phase 4 executor test update",
)
plan2 = _fake_plan([update_op], summary="Update test Opportunity")
bid2 = create_batch("U_TEST_P4_B", "C_FAKE", plan2)
update_batch_status(bid2, "APPROVED", approved_by="U_COWORKER")

result2 = execute_batch(bid2)
assert result2["status"] == "COMPLETED"
print(f"  result   : {result2['status']}")
print(f"  SF result: {result2['results'].get(1)}")

updated_check = run_soql(f"SELECT Id, StageName, Amount FROM Opportunity WHERE Id = '{new_opp_id}'")
assert updated_check[0]["StageName"] == "Proposal"
assert updated_check[0]["Amount"] == 50000.0
print(f"  verified in SF: {updated_check[0]}")
print("  OK UPDATE batch executed and verified in Salesforce")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 3: Template reference resolution")
print("="*60)

# Plan: CREATE Opportunity, then UPDATE it using {{op_1.result.id}}
create_op2 = Operation(
    order=1, object_api_name="Opportunity", method="create",
    record_id=None,
    payload={
        "Name": "ASOKA_TEST_phase4_template",
        "AccountId": ACCT_ID,
        "StageName": "Prospecting",
        "CloseDate": "2026-12-31",
    },
    reason="Template test: create first",
)
update_op2 = Operation(
    order=2, object_api_name="Opportunity", method="update",
    record_id="{{op_1.result.id}}",   # template reference
    payload={"Description": "template_test_confirmed"},
    reason="Template test: update using op_1 result",
)
plan3 = _fake_plan([create_op2, update_op2], summary="Template ref test")
bid3 = create_batch("U_TEST_P4_C", "C_FAKE", plan3)
update_batch_status(bid3, "APPROVED", approved_by="U_COWORKER")

result3 = execute_batch(bid3)
print(f"  result   : {result3['status']}")
print(f"  ops run  : {result3['operations_run']}")
assert result3["status"] == "COMPLETED"
assert result3["operations_run"] == 2

template_id = result3["results"][1]["id"]
desc_check = run_soql(
    f"SELECT Id, Description FROM Opportunity WHERE Id = '{template_id}'"
)
assert desc_check and desc_check[0]["Description"] == "template_test_confirmed"
print(f"  template resolved and verified in SF: {desc_check[0]}")
print("  OK template reference resolved correctly")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 4: Execution failure handling (bad record ID)")
print("="*60)

bad_op = Operation(
    order=1, object_api_name="Opportunity", method="update",
    record_id="001DOESNOTEXIST00000",
    payload={"StageName": "Closed Won"},
    reason="should fail",
)
plan4 = _fake_plan([bad_op], summary="Fail test")
bid4 = create_batch("U_TEST_P4_D", "C_FAKE", plan4)
update_batch_status(bid4, "APPROVED", approved_by="U_COWORKER")

try:
    execute_batch(bid4)
    assert False, "Expected execute_batch to raise an exception"
except Exception as e:
    print(f"  exception raised (expected): {type(e).__name__}")

batch4_db = get_batch(bid4)
assert batch4_db["status"] == "FAILED", f"Expected FAILED, got {batch4_db['status']}"
ops4_db = get_operations(bid4)
# The failed op may be FAILED; remaining ops should be SKIPPED
statuses = [o["status"] for o in ops4_db]
assert all(s in ("FAILED", "SKIPPED") for s in statuses), f"Unexpected statuses: {statuses}"
print(f"  batch status: {batch4_db['status']}")
print(f"  op statuses : {statuses}")
print("  OK failure marks batch FAILED and remaining ops SKIPPED")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 5: format_approval_request() block structure")
print("="*60)

ops5 = [Operation(order=1, object_api_name="Opportunity", method="update",
                  record_id="001TEST", payload={"Discount_Percent__c": 0.15},
                  reason="test formatting")]
plan5 = _fake_plan(ops5, summary="Format test plan")
bid5 = create_batch("U_TEST_P4_E", "C_FAKE", plan5)

fallback, blocks = format_approval_request(bid5, plan5, "U_REQUESTER")
print(f"  fallback text: {fallback[:80]}")
print(f"  block count  : {len(blocks)}")

# Must have header + action buttons
block_types = [b["type"] for b in blocks]
assert "header" in block_types, "Missing header block"
assert "actions" in block_types, "Missing actions block"

# Actions block must have approve + deny buttons
actions = next(b for b in blocks if b["type"] == "actions")
action_ids = [e["action_id"] for e in actions["elements"]]
assert "approve_batch" in action_ids
assert "deny_batch" in action_ids
# Button values must be the batch_id
for e in actions["elements"]:
    assert e["value"] == bid5, f"Button value mismatch: {e['value']} != {bid5}"

print(f"  actions    : {action_ids}")
print("  OK approval request blocks are well-formed")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 6: handle_approve() DB state changes")
print("="*60)

notified: list[dict] = []
def _mock_notify(user_id, text, blocks):
    notified.append({"user_id": user_id, "text": text})

ops6 = [Operation(order=1, object_api_name="Opportunity", method="update",
                  record_id=new_opp_id,
                  payload={"Description": "handle_approve_test"},
                  reason="test handle_approve")]
plan6 = _fake_plan(ops6, summary="Approve handler test")
bid6 = create_batch("U_TEST_P4_F", "C_FAKE", plan6)

status_msg = handle_approve(bid6, "U_COWORKER", "ts_fake", "C_FAKE",
                             notify_fn=_mock_notify)
print(f"  status msg : {status_msg[:80]}")
batch6 = get_batch(bid6)
assert batch6["status"] == "COMPLETED", f"Expected COMPLETED, got {batch6['status']}"
assert batch6["approved_by"] == "U_COWORKER"
assert len(notified) == 1
print(f"  batch status  : {batch6['status']}")
print(f"  notification  : user={notified[0]['user_id']}")
print("  OK handle_approve updated DB and sent notification")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 7: handle_deny() DB state changes")
print("="*60)

notified_deny: list[dict] = []
def _mock_notify_deny(user_id, text, blocks):
    notified_deny.append({"user_id": user_id, "text": text})

ops7 = [Operation(order=1, object_api_name="Opportunity", method="update",
                  record_id=new_opp_id,
                  payload={"Amount": 999},
                  reason="test handle_deny")]
plan7 = _fake_plan(ops7, summary="Deny handler test")
bid7 = create_batch("U_TEST_P4_G", "C_FAKE", plan7)

status_msg = handle_deny(bid7, "U_COWORKER", reason="Not approved",
                          notify_fn=_mock_notify_deny)
print(f"  status msg : {status_msg[:80]}")
batch7 = get_batch(bid7)
assert batch7["status"] == "DENIED", f"Expected DENIED, got {batch7['status']}"
assert batch7["denial_reason"] == "Not approved"
assert len(notified_deny) == 1
print(f"  batch status  : {batch7['status']}")
print(f"  denial reason : {batch7['denial_reason']}")
print("  OK handle_deny updated DB and sent notification")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("ALL PHASE 4 TESTS PASSED")
print("="*60 + "\n")
