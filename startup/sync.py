"""
startup/sync.py

Responsibilities:
  - Orchestrate the full Salesforce → SQLite sync at bot startup
  - For each core object: describe → upsert objects, fields, relationships
  - Fetch validation rules → upsert validation_rules
  - Fetch role hierarchy → upsert role_hierarchy
  - Expose resync_object() for targeted re-sync after a metadata write

Two modes:
  full_sync()       — called once at startup, syncs all CORE_OBJECTS
  resync_object()   — called after a successful metadata write on one object,
                      re-describes just that object and updates SQLite

Why upsert (INSERT OR REPLACE) instead of DELETE + INSERT?
  DELETE + INSERT would wipe and rewrite every row on every startup.
  INSERT OR REPLACE (SQLite's upsert) checks the primary key: if a row
  with that key exists, it replaces it in place; if not, it inserts.
  This preserves rows for objects we don't re-describe (e.g. if a future
  object is added mid-session) and is safe to call repeatedly.

Input:  authenticated Salesforce client + initialized SQLite connection
Output: SQLite tables fully populated — bot is ready to answer schema questions

Usage:
    from startup.sync import full_sync, resync_object

    full_sync()                    # call once at startup in main.py
    resync_object("Opportunity")   # call after a metadata write
"""

import json
import logging
from datetime import datetime, timezone

from db.connection import get_connection
from salesforce.client import get_client
from salesforce.describe import (
    CORE_OBJECTS,
    describe_object,
    fetch_validation_rules,
    fetch_role_hierarchy,
    ObjectMeta,
    FieldMeta,
    RelationshipMeta,
    ValidationRuleMeta,
    RoleHierarchyMeta,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def full_sync() -> None:
    """
    Sync all CORE_OBJECTS from Salesforce into SQLite.
    Also syncs validation rules and role hierarchy.
    Called once in main.py before Slack starts listening.

    Order matters:
      1. Objects first — fields have a FK to objects
      2. Fields + relationships — depend on objects existing
      3. Validation rules — independent, but logically after schema
      4. Role hierarchy — independent
    """
    logger.info("Starting full Salesforce schema sync")
    sf = get_client()
    conn = get_connection()

    for obj_name in CORE_OBJECTS:
        obj_meta, field_metas, rel_metas = describe_object(sf, obj_name)
        _upsert_object(conn, obj_meta)
        _upsert_fields(conn, obj_name, field_metas)
        _upsert_relationships(conn, obj_name, rel_metas)

    _upsert_validation_rules(conn, fetch_validation_rules(sf))
    _upsert_role_hierarchy(conn, fetch_role_hierarchy(sf))

    conn.commit()
    logger.info("Full sync complete")


def resync_object(object_api_name: str) -> None:
    """
    Re-describe a single object and update its rows in SQLite.
    Called immediately after any successful metadata write (field create/update/delete)
    so the structural store never serves stale schema to the orchestrator.

    Does NOT re-sync validation rules or role hierarchy — those are unaffected
    by record-level metadata changes.
    """
    logger.info("Re-syncing object: %s", object_api_name)
    sf = get_client()
    conn = get_connection()

    obj_meta, field_metas, rel_metas = describe_object(sf, object_api_name)
    _upsert_object(conn, obj_meta)
    _upsert_fields(conn, object_api_name, field_metas)
    _upsert_relationships(conn, object_api_name, rel_metas)

    conn.commit()
    logger.info("Re-sync complete for: %s", object_api_name)


# ─────────────────────────────────────────────────────────────────────────────
# Upsert helpers — one per table
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_object(conn, meta: ObjectMeta) -> None:
    """
    INSERT OR REPLACE a single row into the objects table.

    INSERT OR REPLACE works by checking the PRIMARY KEY (api_name).
    If a row with that api_name exists, it is deleted and re-inserted
    with the new values. If not, a fresh row is created.

    last_synced_at is always set to now (UTC) — this timestamp tells
    the orchestrator when this object's schema was last refreshed.
    """
    # SQLite PARSE_DECLTYPES expects "YYYY-MM-DD HH:MM:SS" (space, not T)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT OR REPLACE INTO objects
            (api_name, label, is_custom, last_synced_at)
        VALUES (?, ?, ?, ?)
        """,
        (meta.api_name, meta.label, int(meta.is_custom), now),
    )
    logger.debug("Upserted object: %s", meta.api_name)


def _upsert_fields(conn, object_api_name: str, fields: list[FieldMeta]) -> None:
    """
    Upsert all fields for one object.

    Strategy: delete all existing field rows for this object first, then
    insert fresh. This handles the case where a field was deleted from
    Salesforce — a pure upsert would leave the old row in place. Deleting
    first and re-inserting ensures the local cache exactly mirrors Salesforce.

    This is safe because we're inside a transaction that only commits after
    all objects have been synced — if something fails mid-way, no partial
    state is committed.

    picklist_values is serialized to JSON string for storage.
    All booleans are stored as 0/1 integers (SQLite has no boolean type).
    """
    conn.execute(
        "DELETE FROM fields WHERE object_api_name = ?",
        (object_api_name,)
    )

    conn.executemany(
        """
        INSERT INTO fields (
            object_api_name, field_api_name, label, data_type,
            is_required, is_editable, is_custom, is_deprecated,
            description, picklist_values, reference_to, relationship_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                f.object_api_name,
                f.field_api_name,
                f.label,
                f.data_type,
                int(f.is_required),
                int(f.is_editable),
                int(f.is_custom),
                int(f.is_deprecated),
                f.description,
                json.dumps(f.picklist_values) if f.picklist_values is not None else None,
                f.reference_to,
                f.relationship_name,
            )
            for f in fields
        ],
    )
    logger.debug("Upserted %d fields for %s", len(fields), object_api_name)


def _upsert_relationships(
    conn, object_api_name: str, relationships: list[RelationshipMeta]
) -> None:
    """
    Upsert child relationships for one object.

    Same delete-then-insert strategy as fields — ensures deleted
    relationships don't linger in the cache.

    Note: we delete WHERE parent_object = object_api_name because
    describe_object() returns child relationships from the PARENT's
    perspective. We're not touching rows where this object appears
    as the child_object (those are owned by the parent's sync).
    """
    conn.execute(
        "DELETE FROM relationships WHERE parent_object = ?",
        (object_api_name,)
    )

    if relationships:
        conn.executemany(
            """
            INSERT INTO relationships (
                parent_object, child_object, field_api_name,
                relationship_type, relationship_name
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    r.parent_object,
                    r.child_object,
                    r.field_api_name,
                    r.relationship_type,
                    r.relationship_name,
                )
                for r in relationships
            ],
        )
    logger.debug(
        "Upserted %d relationships for %s", len(relationships), object_api_name
    )


def _upsert_validation_rules(conn, rules: list[ValidationRuleMeta]) -> None:
    """
    Replace all validation rules with the freshly fetched set.

    Full replace strategy: delete everything, re-insert all.
    Validation rules are fetched for all objects in one Tooling API call,
    so it's simpler to wipe and reload the whole table than to diff per-object.
    """
    conn.execute("DELETE FROM validation_rules")

    if rules:
        conn.executemany(
            """
            INSERT INTO validation_rules (
                rule_id, object_api_name, rule_name, active,
                description, error_message, formula
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.rule_id,
                    r.object_api_name,
                    r.rule_name,
                    int(r.active),
                    r.description,
                    r.error_message,
                    r.formula,
                )
                for r in rules
            ],
        )
    logger.info("Upserted %d validation rules", len(rules))


def _upsert_role_hierarchy(conn, roles: list[RoleHierarchyMeta]) -> None:
    """
    Replace the full role hierarchy.

    Full replace strategy: delete everything, re-insert all.

    Insertion order matters because of the self-referencing FK
    (parent_role_id → role_id). We must insert parent roles before
    their children, or the FK constraint fires.

    Topological sort: roles with parent_role_id=None go first (roots),
    then roles whose parent is already inserted, recursively.
    We use a simple multi-pass approach: keep trying to insert remaining
    rows until all are inserted or no progress is made (cycle detection).
    """
    conn.execute("DELETE FROM role_hierarchy")

    if not roles:
        return

    # Build a set of all role_ids for fast parent-existence checking
    remaining = list(roles)
    inserted_ids: set[str] = set()
    max_passes = len(roles) + 1   # safety: more passes than rows = cycle

    for _ in range(max_passes):
        if not remaining:
            break

        inserted_this_pass = []
        still_remaining = []

        for role in remaining:
            # Insert if: root (no parent) OR parent already inserted
            if role.parent_role_id is None or role.parent_role_id in inserted_ids:
                conn.execute(
                    """
                    INSERT INTO role_hierarchy (role_id, role_name, parent_role_id)
                    VALUES (?, ?, ?)
                    """,
                    (role.role_id, role.role_name, role.parent_role_id),
                )
                inserted_ids.add(role.role_id)
                inserted_this_pass.append(role)
            else:
                still_remaining.append(role)

        remaining = still_remaining

        if not inserted_this_pass and remaining:
            # No progress made — cycle in role data, log and break
            logger.warning(
                "Role hierarchy has unresolvable references: %s",
                [r.role_id for r in remaining]
            )
            break

    logger.info("Upserted %d roles", len(inserted_ids))
