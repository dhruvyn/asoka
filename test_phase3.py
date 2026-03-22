"""
test_phase3.py

Phase 3 validation — batchqueue/ DB layer + salesforce/writer.py

Tests (run from the asoka/ directory):
    python test_phase3.py

What this covers:
  1. create_batch() + get_batch() round-trip
  2. create_operation_rows() + get_operations() round-trip
  3. acquire_locks() — clean (no conflicts) acquires locks
  4. acquire_locks() — conflict detected, returns False
  5. release_locks() clears conflict so second batch can proceed
  6. SF writer execute_update — update Account.Description (live SF)
  7. SF writer execute_create — create a test Opportunity (live SF)
"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_phase3")

# ── boot sequence ─────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

import db.connection as db_conn
import salesforce.client as sf_client
from startup.sync import full_sync
from knowledge.loader import init_knowledge
from config import cfg

logger.info("=== Phase 3 Test: batchqueue/ + salesforce/writer ===")

db_conn.init(cfg.db_path)
sf_client.init()
logger.info("Running full_sync...")
full_sync()
logger.info("Initializing knowledge store...")
init_knowledge()
logger.info("Boot complete — beginning tests")

# ── clean up any leftover state from prior runs ───────────────────────────────
_conn = db_conn.get_connection()
_conn.execute("DELETE FROM locks WHERE batch_id IN (SELECT batch_id FROM batches WHERE user_id IN ('U_TEST_P3','U_TEST_P3_B'))")
_conn.execute("DELETE FROM operations WHERE batch_id IN (SELECT batch_id FROM batches WHERE user_id IN ('U_TEST_P3','U_TEST_P3_B'))")
_conn.execute("DELETE FROM batches WHERE user_id IN ('U_TEST_P3','U_TEST_P3_B')")
_conn.commit()
logger.info("Cleaned up prior test state")

# ── imports that require boot ──────────────────────────────────────────────────
from dataclasses import dataclass, field as dc_field
from orchestrator.planner import Operation, BatchPlan
from orchestrator.intent import IntentResult
from batchqueue.batch import create_batch, get_batch, update_batch_status
from batchqueue.operations import create_operation_rows, get_operations, update_operation_status
from batchqueue.locks import acquire_locks, release_locks, check_conflicts
from salesforce.writer import execute_update, execute_create
from salesforce.query import find_records, soql as run_soql

# ── build a minimal fake IntentResult for plan construction ───────────────────
def _fake_intent(user_id: str = "U_TEST_P3") -> IntentResult:
    return IntentResult(
        intent_type="WRITE",
        objects=["Opportunity"],
        record_hints=[],
        field_hints=[],
        summary="Phase 3 test plan",
        raw_message="test",
        user_id=user_id,
    )


def _fake_plan(ops: list[Operation], user_id: str = "U_TEST_P3") -> BatchPlan:
    return BatchPlan(
        summary="Phase 3 test plan",
        operations=ops,
        assumptions=["This is a test batch"],
        risks=[],
        pre_flight_issues=[],
        intent=_fake_intent(user_id),
    )


# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 1: create_batch() + get_batch() round-trip")
print("="*60)

ops_t1 = [
    Operation(order=1, object_api_name="Opportunity", method="update",
              record_id="001FAKERECORDID123456", payload={"StageName": "Proposal"},
              reason="test"),
]
plan_t1 = _fake_plan(ops_t1)
bid1 = create_batch("U_TEST_P3", "C_TEST_CHAN", plan_t1)
print(f"  batch_id: {bid1}")
assert bid1 and len(bid1) == 36, "Expected UUID"

batch = get_batch(bid1)
assert batch is not None
assert batch["user_id"] == "U_TEST_P3"
assert batch["status"] == "PENDING_APPROVAL"
assert batch["summary"] == "Phase 3 test plan"
assert isinstance(batch["assumptions"], list)
print(f"  status  : {batch['status']}")
print("  OK create_batch + get_batch")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 2: get_operations() round-trip")
print("="*60)

ops_from_db = get_operations(bid1)
assert len(ops_from_db) == 1
op = ops_from_db[0]
assert op["batch_id"] == bid1
assert op["sequence_order"] == 1
assert op["operation_type"] == "UPDATE"
assert op["target_object"] == "Opportunity"
assert op["target_record_id"] == "001FAKERECORDID123456"
assert op["payload"] == {"StageName": "Proposal"}
assert op["status"] == "PENDING"
print(f"  operation: {op['operation_type']} {op['target_object']} seq={op['sequence_order']}")
print(f"  payload  : {op['payload']}")
print("  OK get_operations round-trip")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 3: acquire_locks() - clean case")
print("="*60)

ok = acquire_locks(bid1, ops_t1)
assert ok is True, f"Expected True (no conflict), got {ok}"
print(f"  acquire_locks -> {ok}")
print("  OK locks acquired")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 4: acquire_locks() - conflict detected")
print("="*60)

# A second batch targeting the same record should fail to acquire
ops_t4 = [
    Operation(order=1, object_api_name="Opportunity", method="update",
              record_id="001FAKERECORDID123456", payload={"Amount": 9999},
              reason="conflict test"),
]
plan_t4 = _fake_plan(ops_t4, user_id="U_TEST_P3_B")
bid4 = create_batch("U_TEST_P3_B", "C_TEST_CHAN", plan_t4)

conflict_result = acquire_locks(bid4, ops_t4)
assert conflict_result is False, f"Expected False (conflict), got {conflict_result}"
print(f"  acquire_locks for second batch -> {conflict_result}")
print("  OK conflict correctly detected")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 5: release_locks() clears conflict")
print("="*60)

release_locks(bid1)
# Now the second batch should be able to acquire
ok_after_release = acquire_locks(bid4, ops_t4)
assert ok_after_release is True, f"Expected True after release, got {ok_after_release}"
print(f"  after release, second batch acquire -> {ok_after_release}")
# Clean up
release_locks(bid4)
print("  OK release_locks cleared conflict")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 6: SF writer execute_update (live Salesforce)")
print("="*60)

# Find TechGlobal Solutions Account ID
accts = find_records("Account", fields=["Id", "Name", "Description"], limit=5)
if not accts:
    print("  SKIP: no Account records found in Salesforce")
else:
    account = accts[0]
    acct_id = account["Id"]
    original_desc = account.get("Description") or ""
    print(f"  Account: {account['Name']} | Id: {acct_id}")

    test_desc = "ASOKA_TEST: phase3 writer test"
    result = execute_update("Account", acct_id, {"Description": test_desc})
    assert result["id"] == acct_id
    assert result["status"] in (200, 204)
    print(f"  update result: {result}")

    # Verify the update took effect
    updated = find_records("Account", fields=["Id", "Description"],
                           where=f"Id = '{acct_id}'", limit=1)
    assert updated and updated[0].get("Description") == test_desc, \
        f"Description mismatch: {updated}"
    print(f"  verified Description = {test_desc!r}")

    # Restore original value
    execute_update("Account", acct_id, {"Description": original_desc or None})
    print(f"  restored Description to original")
    print("  OK execute_update round-trip")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("TEST 7: SF writer execute_create (live Salesforce)")
print("="*60)

# Find the TechGlobal Account ID to link the Opportunity
accts = find_records("Account", fields=["Id", "Name"], limit=1)
if not accts:
    print("  SKIP: no Account records found in Salesforce")
else:
    acct_id = accts[0]["Id"]
    test_opp = {
        "Name": "ASOKA_TEST_phase3",
        "AccountId": acct_id,
        "StageName": "Prospecting",
        "CloseDate": "2026-12-31",
    }
    result = execute_create("Opportunity", test_opp)
    assert result.get("success") is True, f"Create failed: {result}"
    new_id = result["id"]
    print(f"  created Opportunity Id: {new_id}")

    # Verify it exists
    verify = run_soql(f"SELECT Id, Name, StageName FROM Opportunity WHERE Id = '{new_id}'")
    assert verify and verify[0]["Name"] == "ASOKA_TEST_phase3"
    print(f"  verified: {verify[0]}")
    print("  OK execute_create (Opportunity left in org for Phase 4 executor test)")
    print(f"  NOTE: new Opportunity ID = {new_id}")

# ─────────────────────────────────────────────────────────────────────────────

print("\n" + "="*60)
print("ALL PHASE 3 TESTS PASSED")
print("="*60 + "\n")
