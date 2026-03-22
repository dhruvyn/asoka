"""
salesforce/query.py

Responsibilities:
  - Fetch live records from Salesforce via SOQL
  - Three access patterns: single record by ID, filtered search, raw SOQL
  - Strip Salesforce internal metadata (the "attributes" key) from all results
  - Return plain dicts — callers never touch the simple-salesforce response directly

Why this file exists alongside context/structural.py:
  structural.py answers SCHEMA questions from SQLite (cached at startup):
    "what fields does Account have?", "what validation rules exist?"
  query.py answers DATA questions from Salesforce (live, per-request):
    "what is the Name of Account 001XYZ?", "which Opportunities are Closed Won?"

The separation keeps schema (static) and data (dynamic) concerns in different places.
query.py is never called at startup — only by the orchestrator during a user request.

Input:  object name + field list + optional WHERE clause
Output: list[dict] or dict | None — plain Python, no SF SDK types exposed

Usage:
    from salesforce.query import get_record, find_records, soql

    record  = get_record("Account", "001ABC...", ["Name", "Type", "OwnerId"])
    records = find_records("Opportunity", ["Name", "Amount"],
                           where="StageName = 'Closed Won'", limit=20)
    results = soql("SELECT Id, Name FROM User WHERE IsActive = true LIMIT 10")
"""

import logging
from simple_salesforce import SalesforceResourceNotFound

from salesforce.client import get_client

logger = logging.getLogger(__name__)

# Hard cap — prevents accidentally huge result sets being pumped into a Claude prompt.
# The orchestrator should always pass an explicit limit; this is the safety ceiling.
_MAX_LIMIT = 200


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_record(
    object_name: str,
    record_id: str,
    fields: list[str] | None = None,
) -> dict | None:
    """
    Fetch a single Salesforce record by its record ID.

    When fields is provided, builds a SOQL SELECT so only those fields are
    returned — keeps response payloads small when we only need a few values.

    When fields is None, uses the REST object endpoint (GET /sobjects/{Object}/{id})
    which returns all fields. Use this only when you genuinely need everything.

    Args:
        object_name: Salesforce object API name, e.g. "Account"
        record_id:   15 or 18-character Salesforce record ID
        fields:      field API names to return; None → all fields

    Returns:
        Dict of field → value with "attributes" key removed.
        None if the record does not exist (Salesforce returns 404).
    """
    sf = get_client()

    try:
        if fields:
            # SOQL is more predictable for field selection than the REST ?fields= param
            select = ", ".join(fields)
            result = sf.query(
                f"SELECT {select} FROM {object_name} WHERE Id = '{record_id}' LIMIT 1"
            )
            records = result.get("records", [])
            if not records:
                logger.warning("Record not found: %s/%s", object_name, record_id)
                return None
            return _strip_attributes(records[0])
        else:
            # REST endpoint: GET /sobjects/{Object}/{id} — returns all fields
            raw = getattr(sf, object_name).get(record_id)
            return _strip_attributes(raw)

    except SalesforceResourceNotFound:
        logger.warning("Record not found: %s/%s", object_name, record_id)
        return None


def find_records(
    object_name: str,
    fields: list[str] | None = None,
    where: str | None = None,
    limit: int = 50,
    order_by: str | None = None,
) -> list[dict]:
    """
    Run a SOQL SELECT and return matching records.

    Builds the SOQL string from parts so the orchestrator doesn't write raw SOQL
    for the common case. Claude provides field names and WHERE conditions derived
    from the user's message and the context store.

    Args:
        object_name: SF object API name, e.g. "Opportunity"
        fields:      field API names to SELECT; defaults to ["Id"] if None
        where:       SOQL WHERE body (no "WHERE" keyword), e.g.:
                       "StageName = 'Closed Won' AND OwnerId = '005ABC'"
        limit:       max records to return; capped at _MAX_LIMIT (200)
        order_by:    SOQL ORDER BY body, e.g. "CreatedDate DESC"

    Returns:
        List of record dicts (attributes stripped). Empty list if no matches.
    """
    sf = get_client()

    select_fields = ", ".join(fields) if fields else "Id"
    effective_limit = min(limit, _MAX_LIMIT)

    parts = [
        f"SELECT {select_fields}",
        f"FROM {object_name}",
    ]
    if where:
        parts.append(f"WHERE {where}")
    if order_by:
        parts.append(f"ORDER BY {order_by}")
    parts.append(f"LIMIT {effective_limit}")

    query_string = " ".join(parts)
    logger.debug("find_records SOQL: %s", query_string)

    result = sf.query(query_string)
    records = [_strip_attributes(r) for r in result.get("records", [])]

    logger.info(
        "find_records %s: %d/%d records returned",
        object_name, len(records), result.get("totalSize", 0),
    )
    return records


def soql(query_string: str) -> list[dict]:
    """
    Execute a raw SOQL string and return all records.

    Used when the orchestrator needs complex queries that find_records() can't
    express — e.g. relationship traversal (Account.Owner.Name), aggregate
    functions (COUNT), or subqueries.

    The query string is built by Claude via the orchestrator. We don't validate
    or sanitize it here — the orchestrator prompts are responsible for safe SOQL.

    Args:
        query_string: complete, valid SOQL statement

    Returns:
        List of record dicts (attributes stripped). Empty list if no results.
    """
    sf = get_client()

    logger.debug("soql: %s", query_string[:200])

    result = sf.query(query_string)
    records = [_strip_attributes(r) for r in result.get("records", [])]

    logger.info("soql: %d/%d records", len(records), result.get("totalSize", 0))
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Internal helper
# ─────────────────────────────────────────────────────────────────────────────

def _strip_attributes(record: dict) -> dict:
    """
    Remove the "attributes" key that Salesforce injects into every record.

    Every simple-salesforce response includes:
        {
          "attributes": {"type": "Account", "url": "/services/data/.../Account/001..."},
          "Id": "001...",
          "Name": "Acme Corp",
          ...
        }

    We strip "attributes" before returning so callers get clean field→value dicts.
    This also prevents the attributes dict from appearing in Claude prompts.
    """
    return {k: v for k, v in record.items() if k != "attributes"}
