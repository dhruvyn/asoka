"""
test_phase1.py

Phase 1 validation — salesforce/query.py

Tests (run from the asoka/ directory):
    python test_phase1.py

What this covers:
  1. get_record   — fetch a single Account by ID with specific fields
  2. find_records — list Accounts with limit, no WHERE
  3. find_records — filter by WHERE clause (active Users)
  4. find_records — ORDER BY + limit
  5. soql         — raw SOQL with relationship traversal
  6. get_record   — None returned for a bad ID (not a crash)

Requires:
  - .env populated with real Salesforce credentials
  - Salesforce sandbox with Account, Opportunity, Case, User data
"""

import logging
import sys
import os

# ── run from asoka/ ──────────────────────────────────────────────────────────
# If invoked as  python test_phase1.py  from inside asoka/, this adds asoka/
# to sys.path so all module imports work.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_phase1")

# ── boot sequence (same order as main.py will use) ───────────────────────────
from dotenv import load_dotenv
load_dotenv()

import db.connection as db_conn
import salesforce.client as sf_client
from startup.sync import full_sync
from config import cfg

logger.info("=== Phase 1 Test: salesforce/query.py ===")

# Init DB
db_conn.init(cfg.db_path)
logger.info("DB initialized")

# Init Salesforce
sf_client.init()
logger.info("Salesforce connected")

# Sync schema into SQLite
logger.info("Running full_sync (this may take 10–20 seconds)...")
full_sync()
logger.info("Sync complete")

# ── now import query (requires SF client to be initialized) ──────────────────
from salesforce.query import get_record, find_records, soql

print("\n" + "="*60)
print("TEST 1: find_records — list 3 Accounts (no filter)")
print("="*60)
accounts = find_records(
    "Account",
    fields=["Id", "Name", "Type", "Account_Tier__c"],
    limit=3,
)
for a in accounts:
    print(f"  {a.get('Id')}  {a.get('Name')}  Type={a.get('Type')}  Tier={a.get('Account_Tier__c')}")
assert isinstance(accounts, list), "Expected list"
print(f"  OK returned {len(accounts)} record(s)")

print("\n" + "="*60)
print("TEST 2: get_record — fetch first Account by ID with specific fields")
print("="*60)
if accounts:
    first_id = accounts[0]["Id"]
    record = get_record("Account", first_id, ["Id", "Name", "Type", "OwnerId"])
    print(f"  {record}")
    assert record is not None, "Expected a record"
    assert record.get("Id") == first_id, "ID mismatch"
    assert "attributes" not in record, "attributes key should be stripped"
    print(f"  OK fetched record, attributes key absent")
else:
    print("  SKIP: no accounts available to test get_record")

print("\n" + "="*60)
print("TEST 3: find_records — active Users with manager field")
print("="*60)
users = find_records(
    "User",
    fields=["Id", "Name", "IsActive", "ManagerId"],
    where="IsActive = true",
    limit=5,
    order_by="Name ASC",
)
for u in users:
    print(f"  {u.get('Name')}  active={u.get('IsActive')}  manager={u.get('ManagerId')}")
assert isinstance(users, list), "Expected list"
print(f"  OK returned {len(users)} active user(s)")

print("\n" + "="*60)
print("TEST 4: find_records — Opportunities ordered by CreatedDate")
print("="*60)
opps = find_records(
    "Opportunity",
    fields=["Id", "Name", "StageName", "Amount"],
    limit=3,
    order_by="CreatedDate DESC",
)
for o in opps:
    print(f"  {o.get('Name')}  stage={o.get('StageName')}  amount={o.get('Amount')}")
print(f"  OK returned {len(opps)} opportunity(ies)")

print("\n" + "="*60)
print("TEST 5: soql — raw query with relationship traversal")
print("="*60)
results = soql(
    "SELECT Id, Name, Owner.Name FROM Account LIMIT 3"
)
for r in results:
    owner = r.get("Owner", {})
    if isinstance(owner, dict):
        owner_name = owner.get("Name", "—")
    else:
        owner_name = str(owner)
    print(f"  {r.get('Name')}  owner={owner_name}")
assert isinstance(results, list), "Expected list"
print(f"  OK soql returned {len(results)} record(s)")

print("\n" + "="*60)
print("TEST 6: get_record — bad ID returns None (no crash)")
print("="*60)
missing = get_record("Account", "001000000000000FAKE")
assert missing is None, f"Expected None, got {missing}"
print("  OK bad ID returned None")

print("\n" + "="*60)
print("ALL PHASE 1 TESTS PASSED")
print("="*60 + "\n")
