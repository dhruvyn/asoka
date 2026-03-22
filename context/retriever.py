"""
context/retriever.py

Responsibilities:
  - Accept the intent classification output from orchestrator/intent.py
  - Query both context stores in parallel (structural + semantic)
  - Return a single ContextBundle dataclass ready for prompt injection

This is the orchestrator's single entry point into the context layer.
It answers: "given what the user wants, what does Claude need to know?"

The ContextBundle contains everything the orchestrator needs to build
both read and write prompts — schema for every relevant object, policy
rules from ChromaDB, and validation constraints for pre-flight checks.

Input:
    objects:  list of SF object names identified during intent extraction
    query:    the user's original message (used for semantic search)
    include_validation_rules: True for write requests (pre-flight checks)

Output:
    ContextBundle dataclass with:
      - fields per object
      - relationships per object
      - validation rules per object (write requests only)
      - role hierarchy (always included — small, often useful)
      - semantic policy chunks relevant to the query
      - a prompt_block() method that serializes everything to a string

Usage:
    from context.retriever import build_context

    bundle = build_context(
        objects=["Opportunity", "Account"],
        query="update the discount on the Acme renewal",
        include_validation_rules=True,
    )
    prompt_text = bundle.to_prompt_block()
"""

import logging
from dataclasses import dataclass, field

from context.structural import (
    get_object_fields,
    get_relationships,
    get_validation_rules,
    get_role_hierarchy,
    FieldInfo,
    RelationshipInfo,
    ValidationRuleInfo,
    RoleInfo,
)
from context.semantic import get_relevant_rules, format_rules_for_prompt
from knowledge.loader import query_org_knowledge

logger = logging.getLogger(__name__)


@dataclass
class ObjectContext:
    """Schema context for one Salesforce object."""
    api_name: str
    fields: list[FieldInfo]
    relationships: list[RelationshipInfo]
    validation_rules: list[ValidationRuleInfo]   # empty list if not requested


@dataclass
class ContextBundle:
    """
    Everything the orchestrator needs to build an informed Claude prompt.

    Produced by build_context() and consumed by orchestrator/planner.py
    and orchestrator/intent.py. Never constructed directly by the orchestrator.
    """
    objects: list[ObjectContext]
    role_hierarchy: list[RoleInfo]
    policy_chunks: list[dict]        # raw chunks from ChromaDB (rules collection)
    org_knowledge_chunks: list[dict] = None  # chunks from org_knowledge collection

    def to_prompt_block(self) -> str:
        """
        Serialize the full context bundle into a structured string for
        injection into a Claude prompt.

        Format:
            === Schema Context ===
            Object: Account
              Fields: ...
              Relationships: ...
              Validation Rules: ...

            === Role Hierarchy ===
            ...

            === Business Rules & Policies ===
            ...
        """
        sections = []

        # ── Schema section ────────────────────────────────────────────────────
        if self.objects:
            schema_lines = ["=== Schema Context ===\n"]
            for obj_ctx in self.objects:
                schema_lines.append(f"Object: {obj_ctx.api_name}")

                # Fields table — only show editable non-deprecated fields in
                # detail; flag others briefly to avoid flooding the prompt
                editable = [f for f in obj_ctx.fields if f.is_editable and not f.is_deprecated]
                deprecated = [f for f in obj_ctx.fields if f.is_deprecated]
                readonly = [f for f in obj_ctx.fields if not f.is_editable]

                schema_lines.append("  Fields (editable):")
                for f in editable:
                    line = f"    - {f.api_name} ({f.data_type})"
                    if f.is_required:
                        line += " [REQUIRED]"
                    if f.picklist_values:
                        line += f" — values: {', '.join(f.picklist_values)}"
                    if f.reference_to:
                        line += f" → {f.reference_to}"
                    if f.description:
                        line += f" | Note: {f.description}"
                    schema_lines.append(line)

                if deprecated:
                    schema_lines.append("  Fields (DEPRECATED — do not use):")
                    for f in deprecated:
                        schema_lines.append(f"    - {f.api_name}")

                if readonly:
                    names = ", ".join(f.api_name for f in readonly)
                    schema_lines.append(f"  Fields (read-only, system-managed): {names}")

                # Relationships
                if obj_ctx.relationships:
                    schema_lines.append("  Relationships:")
                    for r in obj_ctx.relationships:
                        direction = (
                            f"{r.parent_object} → {r.child_object}"
                            if r.parent_object == obj_ctx.api_name
                            else f"{r.child_object} → {r.parent_object}"
                        )
                        schema_lines.append(
                            f"    - {direction} via {r.field_api_name} ({r.relationship_type})"
                        )

                # Validation rules
                if obj_ctx.validation_rules:
                    schema_lines.append("  Active Validation Rules:")
                    for vr in obj_ctx.validation_rules:
                        schema_lines.append(f"    - {vr.rule_name}: {vr.formula}")
                        if vr.error_message:
                            schema_lines.append(f"      Error: {vr.error_message}")

                schema_lines.append("")  # blank line between objects

            sections.append("\n".join(schema_lines).strip())

        # ── Role hierarchy section ────────────────────────────────────────────
        if self.role_hierarchy:
            role_lines = ["=== Role Hierarchy ==="]
            # Build a simple indented tree from the flat list
            role_map = {r.role_id: r for r in self.role_hierarchy}
            roots = [r for r in self.role_hierarchy if r.parent_role_id is None]

            def _render_tree(role: RoleInfo, depth: int) -> list[str]:
                indent = "  " * depth
                lines = [f"{indent}- {role.role_name}"]
                children = [r for r in self.role_hierarchy if r.parent_role_id == role.role_id]
                for child in children:
                    lines.extend(_render_tree(child, depth + 1))
                return lines

            for root in roots:
                role_lines.extend(_render_tree(root, 0))

            sections.append("\n".join(role_lines))

        # ── Policy / rules section ────────────────────────────────────────────
        rules_block = format_rules_for_prompt(self.policy_chunks)
        if rules_block:
            sections.append(rules_block)

        # ── Learned org knowledge section ─────────────────────────────────────
        if self.org_knowledge_chunks:
            ok_lines = ["=== Learned Org Knowledge ===\n"]
            for chunk in self.org_knowledge_chunks:
                ctype = chunk.get("chunk_type", "misc")
                ok_lines.append(f"[{ctype}] {chunk['text']}")
                ok_lines.append("")
            sections.append("\n".join(ok_lines).strip())

        return "\n\n".join(sections)


# Chunk types fetched from org_knowledge per prompt stage.
_ORG_KNOWLEDGE_STAGE_FILTERS = {
    "read":  ["field_correction", "schema_note"],
    "write": ["validation_rule", "field_correction"],
}


def build_context(
    objects: list[str],
    query: str,
    include_validation_rules: bool = False,
    n_policy_chunks: int = 5,
    stage: str | None = None,
) -> ContextBundle:
    """
    Build a ContextBundle for the given objects and query.

    Args:
        objects:                  SF object API names from intent extraction
        query:                    user's original message for semantic search
        include_validation_rules: True for write intents (pre-flight checks)
        n_policy_chunks:          number of policy chunks to retrieve
        stage:                    "read" or "write" — when set, fetches relevant
                                  chunks from the org_knowledge collection using
                                  stage-appropriate chunk_type filters:
                                    read  → field_correction, schema_note
                                    write → validation_rule, field_correction

    The two store queries are logically independent and fast enough to
    run sequentially — both complete in milliseconds against local stores.
    No network calls here.
    """
    logger.info(
        "Building context | objects=%s | write=%s | stage=%s | query=%r",
        objects, include_validation_rules, stage, query[:60]
    )

    # ── Structural store ──────────────────────────────────────────────────────
    object_contexts = []
    for obj_name in objects:
        fields = get_object_fields(obj_name)
        relationships = get_relationships(obj_name)
        val_rules = get_validation_rules(obj_name) if include_validation_rules else []

        object_contexts.append(ObjectContext(
            api_name=obj_name,
            fields=fields,
            relationships=relationships,
            validation_rules=val_rules,
        ))

        logger.debug(
            "Object %s: %d fields, %d rels, %d rules",
            obj_name, len(fields), len(relationships), len(val_rules)
        )

    # Role hierarchy is always included — it's small and often needed
    roles = get_role_hierarchy()

    # ── Semantic store (rules) ────────────────────────────────────────────────
    policy_chunks = get_relevant_rules(query, n=n_policy_chunks)

    # ── Org knowledge store (learned corrections / rules) ─────────────────────
    org_chunks: list[dict] = []
    if stage and stage in _ORG_KNOWLEDGE_STAGE_FILTERS:
        chunk_types = _ORG_KNOWLEDGE_STAGE_FILTERS[stage]
        try:
            org_chunks = query_org_knowledge(
                text=query, n_results=4, chunk_types=chunk_types
            )
            logger.debug(
                "Org knowledge: %d chunks fetched | stage=%s", len(org_chunks), stage
            )
        except Exception as exc:
            logger.warning("Org knowledge query failed (non-fatal): %s", exc)

    bundle = ContextBundle(
        objects=object_contexts,
        role_hierarchy=roles,
        policy_chunks=policy_chunks,
        org_knowledge_chunks=org_chunks or None,
    )

    logger.info(
        "Context built | %d objects | %d roles | %d policy | %d org chunks",
        len(object_contexts), len(roles), len(policy_chunks), len(org_chunks)
    )
    return bundle
