"""
salesforce/describe.py

Responsibilities:
  - Call Salesforce Describe API for each core object → extract fields + relationships
  - Call Salesforce Tooling API → extract validation rules per object
  - Call SOQL on UserRole → extract role hierarchy
  - Return structured Python dataclasses ready for startup/sync.py to write to SQLite

This file only READS from Salesforce. It never writes. It never touches SQLite.
startup/sync.py owns the writing. This separation keeps each file's job small.

Input:  an authenticated Salesforce client from salesforce/client.py
Output: four dataclass types — ObjectMeta, FieldMeta, RelationshipMeta,
        ValidationRuleMeta, RoleHierarchyMeta

The four core objects we describe:
  Account, Opportunity, Case, User
  These are exactly the objects in the appendix. The list is defined once
  here as CORE_OBJECTS — if the org ever adds a 5th object we care about,
  this is the only place to add it.

Describe API path in simple-salesforce:
  sf.Account.describe()  →  dict with 'fields', 'childRelationships', etc.

Tooling API path (validation rules):
  sf.toolingexecute("query/?q=SELECT...")  →  dict with 'records'

SOQL path (role hierarchy):
  sf.query("SELECT Id, Name, ParentRoleId FROM UserRole")
"""

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote
from simple_salesforce import Salesforce

logger = logging.getLogger(__name__)

# The only objects this bot manages. Order doesn't matter here.
CORE_OBJECTS = ["Account", "Opportunity", "Case", "User"]

# Keywords in a field description that mark it as deprecated.
# Checked case-insensitively. If any appear, is_deprecated is set True.
_DEPRECATION_KEYWORDS = ("deprecated", "legacy", "do not use", "do not write")


# ─────────────────────────────────────────────────────────────────────────────
# Return types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ObjectMeta:
    api_name: str
    label: str
    is_custom: bool


@dataclass
class FieldMeta:
    object_api_name: str
    field_api_name: str
    label: str
    data_type: str        # SF type string: "string", "currency", "picklist", etc.
    is_required: bool
    is_editable: bool
    is_custom: bool
    is_deprecated: bool   # derived from description keywords
    description: str | None
    picklist_values: list[str] | None   # active values only; None if not a picklist
    reference_to: str | None            # first target object for Lookup/MasterDetail
    relationship_name: str | None       # SOQL traversal name, e.g. "Account"


@dataclass
class RelationshipMeta:
    parent_object: str
    child_object: str
    field_api_name: str
    relationship_type: str   # "Lookup" or "MasterDetail"
    relationship_name: str | None


@dataclass
class ValidationRuleMeta:
    rule_id: str
    object_api_name: str
    rule_name: str
    active: bool
    description: str | None
    error_message: str | None
    formula: str | None      # the errorConditionFormula — what triggers the rule


@dataclass
class RoleHierarchyMeta:
    role_id: str
    role_name: str
    parent_role_id: str | None   # None for top-level roles (VP Sales, Support Manager)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────

def describe_object(sf: Salesforce, object_name: str) -> tuple[
    ObjectMeta,
    list[FieldMeta],
    list[RelationshipMeta],
]:
    """
    Call sf.<ObjectName>.describe() and return structured metadata.

    Returns a 3-tuple:
      - ObjectMeta: one row for the objects table
      - list[FieldMeta]: one row per field for the fields table
      - list[RelationshipMeta]: one row per child relationship for the relationships table

    The describe() response is a large dict. We extract only what the bot needs.
    """
    logger.info("Describing object: %s", object_name)

    # simple-salesforce: getattr(sf, object_name) returns an SFType instance,
    # then .describe() makes the GET /services/data/vXX/sobjects/{Object}/describe call
    raw = getattr(sf, object_name).describe()

    obj = ObjectMeta(
        api_name=raw["name"],
        label=raw["label"],
        is_custom=raw["custom"],
    )

    fields = [_parse_field(object_name, f) for f in raw["fields"]]

    relationships = _parse_relationships(object_name, raw.get("childRelationships", []))

    logger.info(
        "Described %s: %d fields, %d child relationships",
        object_name, len(fields), len(relationships)
    )
    return obj, fields, relationships


def fetch_validation_rules(sf: Salesforce) -> list[ValidationRuleMeta]:
    """
    Fetch all validation rules for CORE_OBJECTS via the Salesforce Tooling API.

    Why Tooling API and not REST API?
      Validation rules are metadata, not data. They live in Salesforce's
      metadata layer. The REST API's SOQL only sees data records. The
      Tooling API exposes metadata objects — including ValidationRule —
      as queryable entities with their formula text.

    Two-phase query — required by the Salesforce Tooling API constraint:
      The Metadata compound field (which contains errorConditionFormula) can
      only be selected when the query returns exactly one row (LIMIT 1).
      Selecting Metadata across multiple rows raises MALFORMED_QUERY.

      Phase 1: bulk query — get all rule IDs + basic fields (no Metadata)
      Phase 2: per-rule query — fetch Metadata for each ID individually
                                to retrieve errorConditionFormula

    The formula is the actual validation logic — the expression that, when
    it evaluates to TRUE, causes Salesforce to reject the write and show
    ErrorMessage. Claude uses this for pre-flight validation checks.
    """
    obj_list = ", ".join(f"'{o}'" for o in CORE_OBJECTS)

    # ── Phase 1: bulk fetch of all rules (no formula) ─────────────────────────
    bulk_soql = (
        "SELECT Id, EntityDefinition.QualifiedApiName, ValidationName, "
        "Active, Description, ErrorMessage "
        "FROM ValidationRule "
        f"WHERE EntityDefinition.QualifiedApiName IN ({obj_list})"
    )

    logger.info("Fetching validation rules via Tooling API for: %s", CORE_OBJECTS)
    bulk_result = sf.toolingexecute(f"query/?q={quote(bulk_soql)}")
    raw_rules = bulk_result.get("records", [])
    logger.info("Found %d validation rules — fetching formulas...", len(raw_rules))

    # ── Phase 2: per-rule Metadata fetch to get errorConditionFormula ──────────
    rules = []
    for r in raw_rules:
        rule_id = r["Id"]
        formula = None

        try:
            meta_soql = (
                f"SELECT Metadata FROM ValidationRule "
                f"WHERE Id = '{rule_id}' LIMIT 1"
            )
            meta_result = sf.toolingexecute(f"query/?q={quote(meta_soql)}")
            if meta_result.get("records"):
                metadata = meta_result["records"][0].get("Metadata") or {}
                formula = metadata.get("errorConditionFormula")
        except Exception as exc:
            # Formula is best-effort — a rule without a formula is still useful
            logger.warning(
                "Could not fetch formula for rule %s (%s): %s",
                r.get("ValidationName"), rule_id, exc,
            )

        obj_def = r.get("EntityDefinition") or {}
        rules.append(ValidationRuleMeta(
            rule_id=rule_id,
            object_api_name=obj_def.get("QualifiedApiName", ""),
            rule_name=r["ValidationName"],
            active=bool(r["Active"]),
            description=r.get("Description"),
            error_message=r.get("ErrorMessage"),
            formula=formula,
        ))

    logger.info("Validation rules ready: %d total", len(rules))
    return rules


def fetch_role_hierarchy(sf: Salesforce) -> list[RoleHierarchyMeta]:
    """
    Fetch all UserRole records via standard SOQL.

    UserRole is a standard Salesforce object accessible via the REST API.
    ParentRoleId is NULL for top-level roles (VP Sales, Support Manager).
    The bot uses this to understand the org's reporting chain — primarily
    for resolving "transfer to manager" in the User Deactivation workflow.
    """
    logger.info("Fetching role hierarchy")

    result = sf.query("SELECT Id, Name, ParentRoleId FROM UserRole")

    roles = []
    for r in result["records"]:
        roles.append(RoleHierarchyMeta(
            role_id=r["Id"],
            role_name=r["Name"],
            parent_role_id=r.get("ParentRoleId"),  # None if top-level
        ))

    logger.info("Found %d roles", len(roles))
    return roles


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_field(object_api_name: str, f: dict) -> FieldMeta:
    """
    Extract a FieldMeta from one entry in the describe() 'fields' array.

    Key decisions:

    is_required:
      A field is "required" if nillable=False AND defaultedOnCreate=False.
      nillable=False alone isn't enough — system fields like Id and CreatedDate
      are nillable=False but are auto-populated by Salesforce, so they aren't
      "required from the user's perspective."
      defaultedOnCreate=True means SF will fill it in even if you don't provide it.

    is_editable:
      updateable=True in the describe response. Fields like Id, CreatedDate,
      SystemModstamp are updateable=False — the bot must never try to write them.

    picklist_values:
      Only active picklist values are stored. Inactive values exist in SF for
      historical records but must not be used for new ones.

    reference_to:
      referenceTo is a list (polymorphic lookups can point to multiple objects).
      We take the first entry. For all fields in our schema, lookups are
      single-target (AccountId → Account, OwnerId → User).

    is_deprecated:
      Checked against the field's inlineHelpText (the Description field in the
      SF UI). If the description contains any deprecation keyword, we flag it.
      ARR_Legacy__c will be caught this way when the sandbox has its description set.
    """
    description = f.get("inlineHelpText") or None   # normalize empty string → None

    # Check description text for deprecation signals
    is_deprecated = False
    if description:
        desc_lower = description.lower()
        if any(kw in desc_lower for kw in _DEPRECATION_KEYWORDS):
            is_deprecated = True
            logger.debug("Field %s.%s flagged as deprecated", object_api_name, f["name"])

    # Picklist values: only store the active ones
    picklist_values = None
    if f["type"] in ("picklist", "multipicklist"):
        picklist_values = [
            pv["value"]
            for pv in f.get("picklistValues", [])
            if pv.get("active", True)
        ]

    # Lookup target: take first entry (or None for non-lookup fields)
    ref_to = f.get("referenceTo") or []
    reference_to = ref_to[0] if ref_to else None

    return FieldMeta(
        object_api_name=object_api_name,
        field_api_name=f["name"],
        label=f["label"],
        data_type=f["type"],
        is_required=(not f["nillable"] and not f["defaultedOnCreate"]),
        is_editable=f["updateable"],
        is_custom=f["custom"],
        is_deprecated=is_deprecated,
        description=description,
        picklist_values=picklist_values,
        reference_to=reference_to,
        relationship_name=f.get("relationshipName"),
    )


def _parse_relationships(
    parent_object: str,
    child_relationships: list[dict],
) -> list[RelationshipMeta]:
    """
    Extract RelationshipMeta entries from the 'childRelationships' array
    of a describe() response.

    childRelationships describes the parent side: "Account has many Opportunities
    via the AccountId field." Each entry tells us the child object name, the
    field on the child that points here, and whether it's a MasterDetail
    (cascadeDelete=True) or Lookup (cascadeDelete=False).

    We skip entries with no relationshipName — these are system-level
    relationships not relevant to our context building.
    """
    results = []
    for rel in child_relationships:
        if not rel.get("relationshipName"):
            continue   # unnamed relationships aren't traversable in SOQL

        rel_type = "MasterDetail" if rel.get("cascadeDelete") else "Lookup"

        results.append(RelationshipMeta(
            parent_object=parent_object,
            child_object=rel["childSObject"],
            field_api_name=rel["field"],
            relationship_type=rel_type,
            relationship_name=rel["relationshipName"],
        ))
    return results
