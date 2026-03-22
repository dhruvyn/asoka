"""
context/structural.py

Responsibilities:
  - Provide read-only query functions against the SQLite structural store
  - Answer schema questions the orchestrator needs before calling Claude:
      "what fields does Account have?"
      "what are the picklist values for StageName?"
      "what validation rules exist on Opportunity?"
      "what objects are related to Account?"
      "is this field deprecated?"

This file never writes to SQLite. It only reads.
All writes go through startup/sync.py.

Every function returns plain Python objects (dicts, lists, dataclasses) —
never raw sqlite3.Row objects. This keeps callers decoupled from the DB layer.

Input:  initialized SQLite connection from db/connection.py
Output: structured Python data ready for injection into Claude prompts

Usage:
    from context.structural import (
        get_object_fields,
        get_object_summary,
        get_relationships,
        get_validation_rules,
        get_role_hierarchy,
        field_exists,
        is_field_deprecated,
    )
"""

import json
import logging
from dataclasses import dataclass

from db.connection import get_connection

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Return types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FieldInfo:
    """One field on a Salesforce object — ready for prompt injection."""
    api_name: str
    label: str
    data_type: str
    is_required: bool
    is_editable: bool
    is_custom: bool
    is_deprecated: bool
    description: str | None
    picklist_values: list[str] | None
    reference_to: str | None
    relationship_name: str | None


@dataclass
class RelationshipInfo:
    """One parent↔child edge between two objects."""
    parent_object: str
    child_object: str
    field_api_name: str
    relationship_type: str        # "Lookup" or "MasterDetail"
    relationship_name: str | None


@dataclass
class ValidationRuleInfo:
    """One Salesforce validation rule — used for pre-flight checks."""
    rule_id: str
    rule_name: str
    active: bool
    description: str | None
    error_message: str | None
    formula: str | None


@dataclass
class RoleInfo:
    """One node in the role hierarchy tree."""
    role_id: str
    role_name: str
    parent_role_id: str | None


# ─────────────────────────────────────────────────────────────────────────────
# Field queries
# ─────────────────────────────────────────────────────────────────────────────

def get_object_fields(object_api_name: str) -> list[FieldInfo]:
    """
    Return all fields for a given Salesforce object.

    Used by the orchestrator to build the schema section of Claude prompts.
    Ordered by: custom fields last (standard fields first), then alphabetically.
    This ordering surfaces the most universally relevant fields at the top.

    Returns an empty list (not an error) if the object doesn't exist in the
    structural store — the orchestrator handles the "unknown object" case.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            field_api_name, label, data_type,
            is_required, is_editable, is_custom, is_deprecated,
            description, picklist_values, reference_to, relationship_name
        FROM fields
        WHERE object_api_name = ?
        ORDER BY is_custom ASC, field_api_name ASC
        """,
        (object_api_name,),
    ).fetchall()

    result = []
    for r in rows:
        result.append(FieldInfo(
            api_name=r["field_api_name"],
            label=r["label"],
            data_type=r["data_type"],
            is_required=bool(r["is_required"]),
            is_editable=bool(r["is_editable"]),
            is_custom=bool(r["is_custom"]),
            is_deprecated=bool(r["is_deprecated"]),
            description=r["description"],
            picklist_values=json.loads(r["picklist_values"]) if r["picklist_values"] else None,
            reference_to=r["reference_to"],
            relationship_name=r["relationship_name"],
        ))

    logger.debug("get_object_fields(%s): %d fields", object_api_name, len(result))
    return result


def get_editable_fields(object_api_name: str) -> list[FieldInfo]:
    """
    Return only fields the bot can write to: editable and not deprecated.

    Used during plan generation — Claude should only propose writes to
    fields that are actually writable. System fields (Id, CreatedDate) and
    deprecated fields (ARR_Legacy__c) are excluded.
    """
    return [
        f for f in get_object_fields(object_api_name)
        if f.is_editable and not f.is_deprecated
    ]


def get_object_summary(object_api_name: str) -> dict | None:
    """
    Return a compact summary of one object: label, is_custom, last_synced_at,
    field count, required field count.

    Used by the orchestrator for lightweight object awareness without pulling
    the full field list.

    Returns None if the object is not in the structural store.
    """
    conn = get_connection()
    obj_row = conn.execute(
        "SELECT api_name, label, is_custom, last_synced_at FROM objects WHERE api_name = ?",
        (object_api_name,),
    ).fetchone()

    if obj_row is None:
        logger.debug("get_object_summary(%s): object not found", object_api_name)
        return None

    counts = conn.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(is_required) as required_count,
            SUM(is_deprecated) as deprecated_count
        FROM fields
        WHERE object_api_name = ?
        """,
        (object_api_name,),
    ).fetchone()

    return {
        "api_name": obj_row["api_name"],
        "label": obj_row["label"],
        "is_custom": bool(obj_row["is_custom"]),
        "last_synced_at": obj_row["last_synced_at"],
        "field_count": counts["total"],
        "required_field_count": counts["required_count"] or 0,
        "deprecated_field_count": counts["deprecated_count"] or 0,
    }


def get_all_objects() -> list[dict]:
    """
    Return a summary row for every object in the structural store.
    Used by the orchestrator's intent extraction to present Claude with
    the full list of known objects when disambiguating a vague request.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT api_name, label, is_custom FROM objects ORDER BY is_custom ASC, api_name ASC"
    ).fetchall()
    return [
        {"api_name": r["api_name"], "label": r["label"], "is_custom": bool(r["is_custom"])}
        for r in rows
    ]


def field_exists(object_api_name: str, field_api_name: str) -> bool:
    """
    Check whether a field exists on an object in the structural store.
    Fast point lookup — used during pre-flight validation.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM fields WHERE object_api_name = ? AND field_api_name = ?",
        (object_api_name, field_api_name),
    ).fetchone()
    return row is not None


def is_field_deprecated(object_api_name: str, field_api_name: str) -> bool:
    """
    Return True if this field is flagged as deprecated.
    Used during plan generation to reject writes to deprecated fields
    before they even reach the approval stage.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT is_deprecated FROM fields WHERE object_api_name = ? AND field_api_name = ?",
        (object_api_name, field_api_name),
    ).fetchone()
    if row is None:
        return False
    return bool(row["is_deprecated"])


def get_picklist_values(object_api_name: str, field_api_name: str) -> list[str] | None:
    """
    Return the active picklist values for a field, or None if not a picklist.
    Used during plan generation to validate proposed picklist values.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT picklist_values FROM fields WHERE object_api_name = ? AND field_api_name = ?",
        (object_api_name, field_api_name),
    ).fetchone()
    if row is None or row["picklist_values"] is None:
        return None
    return json.loads(row["picklist_values"])


# ─────────────────────────────────────────────────────────────────────────────
# Relationship queries
# ─────────────────────────────────────────────────────────────────────────────

def get_relationships(object_api_name: str) -> list[RelationshipInfo]:
    """
    Return all relationships where this object is the parent OR the child.

    "Parent" means: Account → Opportunity (Account has many Opportunities).
    "Child" means: Opportunity → Account (Opportunity belongs to Account via AccountId).

    The UNION covers both sides so the orchestrator gets the full picture
    of how an object connects to the rest of the schema.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT parent_object, child_object, field_api_name, relationship_type, relationship_name
        FROM relationships
        WHERE parent_object = ? OR child_object = ?
        ORDER BY parent_object, child_object
        """,
        (object_api_name, object_api_name),
    ).fetchall()

    return [
        RelationshipInfo(
            parent_object=r["parent_object"],
            child_object=r["child_object"],
            field_api_name=r["field_api_name"],
            relationship_type=r["relationship_type"],
            relationship_name=r["relationship_name"],
        )
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Validation rule queries
# ─────────────────────────────────────────────────────────────────────────────

def get_validation_rules(object_api_name: str) -> list[ValidationRuleInfo]:
    """
    Return all ACTIVE validation rules for an object.

    Only active rules matter for pre-flight checks — inactive rules
    won't fire in Salesforce, so proposing values that violate them is fine.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT rule_id, rule_name, active, description, error_message, formula
        FROM validation_rules
        WHERE object_api_name = ? AND active = 1
        ORDER BY rule_name
        """,
        (object_api_name,),
    ).fetchall()

    return [
        ValidationRuleInfo(
            rule_id=r["rule_id"],
            rule_name=r["rule_name"],
            active=bool(r["active"]),
            description=r["description"],
            error_message=r["error_message"],
            formula=r["formula"],
        )
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Role hierarchy queries
# ─────────────────────────────────────────────────────────────────────────────

def get_role_hierarchy() -> list[RoleInfo]:
    """
    Return the full role hierarchy, ordered parent-before-child.

    Returned as a flat list — the parent_role_id on each row lets the
    orchestrator reconstruct the tree or present it as a chain.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT role_id, role_name, parent_role_id FROM role_hierarchy"
    ).fetchall()

    return [
        RoleInfo(
            role_id=r["role_id"],
            role_name=r["role_name"],
            parent_role_id=r["parent_role_id"],
        )
        for r in rows
    ]


def get_role_chain(role_name: str) -> list[str]:
    """
    Return the chain of role names from the given role up to the root.

    Example: get_role_chain("Account Executive")
             → ["Account Executive", "Sales Manager", "VP Sales"]

    Used by the orchestrator to explain the reporting chain in plain English
    when generating user deactivation plans.
    """
    roles = {r.role_name: r for r in get_role_hierarchy()}
    chain = []
    current = roles.get(role_name)
    seen = set()

    while current and current.role_name not in seen:
        chain.append(current.role_name)
        seen.add(current.role_name)
        # Find parent by role_id
        parent = next(
            (r for r in roles.values() if r.role_id == current.parent_role_id),
            None,
        )
        current = parent

    return chain
