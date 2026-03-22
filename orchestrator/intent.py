"""
orchestrator/intent.py

Responsibilities:
  - Send the user's message to Claude for intent classification
  - Return a structured IntentResult dataclass
  - Extract: intent type (READ/WRITE/UNKNOWN), SF objects involved,
    record name hints to search for, and a plain summary

This is always the first Claude call in any request. Its output drives
every subsequent decision:
  - intent_type determines which path core.py takes (reader vs planner)
  - objects determines which schema context to pull from structural.py
  - record_hints determines which live records to fetch from query.py
    before the second Claude call

Input:  raw user message string + user_id
Output: IntentResult dataclass

The Anthropic client is instantiated fresh per module import using the
API key from config. This is lightweight — no persistent connection is
opened until messages.create() is called.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anthropic

from config import cfg
from context.structural import get_all_objects, get_object_fields
from orchestrator.prompts import SYSTEM_PROMPT, build_intent_prompt

if TYPE_CHECKING:
    from orchestrator.session import ConversationContext

logger = logging.getLogger(__name__)

# System-managed fields that add noise to intent classification.
# These are auto-populated by Salesforce and users never mention them by name.
_SNAPSHOT_NOISE = {
    "CreatedById", "LastModifiedById", "SystemModstamp",
    "LastModifiedDate", "CreatedDate", "IsDeleted",
    "LastActivityDate", "LastViewedDate", "LastReferencedDate",
    "LastCURequestDate", "LastCUUpdateDate", "PhotoUrl", "IndividualId",
}

# Maximum fields shown per object in the intent snapshot.
_SNAPSHOT_MAX_FIELDS = 12

# Maximum picklist values to display before truncating.
_SNAPSHOT_MAX_PICKLIST = 5

# Module-level client — one Anthropic client for the process lifetime.
# anthropic.Anthropic() is cheap to create (just stores the API key),
# but creating it once avoids repeated config lookups.
_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

# Model used for all Claude calls in the orchestrator.
# claude-sonnet-4-6: fast, capable, good at structured JSON output.
_MODEL = "claude-sonnet-4-6"


@dataclass
class IntentResult:
    """
    Structured output from Claude's intent classification step.

    Fields:
        intent_type:   "READ" | "WRITE" | "MIXED" | "UNKNOWN"
        objects:       SF object API names involved (validated against SQLite)
        record_hints:  company/person names to search for in Salesforce
                       before the next Claude call (so it gets real record IDs)
        field_hints:   field API names mentioned or implied in the message
                       (e.g. "discount" → "Discount_Percent__c")
                       validated against the snapshot — unknowns dropped
        summary:       one-sentence description of what the user wants
        raw_message:   original user message, preserved for logging + prompts
        user_id:       Slack user ID of the requester
    """
    intent_type: str
    objects: list[str]
    record_hints: list[str]
    field_hints: list[str]
    summary: str
    raw_message: str
    user_id: str


def classify_intent(
    message: str,
    user_id: str,
    context: "ConversationContext | None" = None,
) -> IntentResult:
    """
    Call Claude to classify the user's message into a structured intent.

    Makes one Claude API call with temperature=0 for deterministic JSON output.
    The response is expected to be JSON only (enforced by the prompt), which
    is then parsed into an IntentResult.

    The available object list and field snapshot are fetched from SQLite at
    call time — they reflect whatever was synced from this CRM org.

    Args:
        message:  raw user message from Slack
        user_id:  Slack user ID of the requester
        context:  optional ConversationContext from earlier turns — injected
                  into the prompt as a hint block so corrections don't lose
                  accumulated signal (objects, hints, field references)

    Returns:
        IntentResult with intent_type, objects, record_hints, field_hints, summary
    """
    logger.info("Classifying intent | user=%s | message=%r", user_id, message[:80])

    # Fetch available objects and build the rich schema snapshot
    available_objects = [o["api_name"] for o in get_all_objects()]
    valid_objects = set(available_objects)

    snapshot, valid_fields = _build_schema_snapshot(available_objects)
    prompt = build_intent_prompt(message, snapshot, context)

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=600,       # slightly more room for field_hints array
        temperature=0,        # deterministic — same message → same classification
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()
    logger.debug("Intent raw response: %s", raw_text[:300])

    parsed = _extract_json(raw_text)

    # Normalise and validate the parsed output
    intent_type = parsed.get("intent_type", "UNKNOWN").upper()
    if intent_type not in ("READ", "WRITE", "MIXED", "UNKNOWN"):
        logger.warning("Unexpected intent_type %r, defaulting to UNKNOWN", intent_type)
        intent_type = "UNKNOWN"

    # Filter objects to only those that exist in the DB — drops hallucinations
    raw_objects = parsed.get("objects") or []
    objects = [o for o in raw_objects if o in valid_objects]

    record_hints = parsed.get("record_hints") or []

    # Validate field_hints against the snapshot — drops hallucinated field names
    raw_field_hints = parsed.get("field_hints") or []
    field_hints = [f for f in raw_field_hints if f in valid_fields]

    summary = parsed.get("summary", message[:100])

    result = IntentResult(
        intent_type=intent_type,
        objects=objects,
        record_hints=record_hints,
        field_hints=field_hints,
        summary=summary,
        raw_message=message,
        user_id=user_id,
    )

    logger.info(
        "Intent classified | type=%s | objects=%s | hints=%s | fields=%s",
        result.intent_type, result.objects, result.record_hints, result.field_hints,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_schema_snapshot(object_api_names: list[str]) -> tuple[str, set[str]]:
    """
    Build a compact per-object field summary for the intent classification prompt.

    Returns:
        (snapshot_str, valid_fields) where:
          snapshot_str — formatted text block injected into the prompt
          valid_fields — flat set of all field API names in the snapshot,
                         used to validate Claude's field_hints output

    Snapshot rules:
      - Include: non-deprecated fields that are editable OR are Id/Name/Subject
      - Exclude: _SNAPSHOT_NOISE system fields
      - Order: custom fields first (highest domain value), then alphabetical
      - Cap: _SNAPSHOT_MAX_FIELDS per object
      - Picklist values: show first _SNAPSHOT_MAX_PICKLIST, append "..." if more
    """
    lines: list[str] = []
    valid_fields: set[str] = set()

    for obj in object_api_names:
        all_fields = get_object_fields(obj)

        # Filter to fields worth showing
        snapshot_fields = [
            f for f in all_fields
            if not f.is_deprecated
            and f.api_name not in _SNAPSHOT_NOISE
            and (f.is_editable or f.api_name in {"Id", "Name", "Subject"})
        ]

        # Custom fields first, then alphabetical within each group
        snapshot_fields.sort(key=lambda f: (not f.is_custom, f.api_name))
        snapshot_fields = snapshot_fields[:_SNAPSHOT_MAX_FIELDS]

        if not snapshot_fields:
            lines.append(f"{obj}: (no accessible fields)")
            lines.append("")
            continue

        lines.append(f"{obj}:")
        for f in snapshot_fields:
            valid_fields.add(f.api_name)
            custom_mark = "*" if f.is_custom else ""
            # Build descriptor: type + picklist / reference + required flag
            if f.data_type == "reference" and f.reference_to:
                type_str = f"→ {f.reference_to}"
            elif f.data_type == "picklist" and f.picklist_values:
                vals = f.picklist_values[:_SNAPSHOT_MAX_PICKLIST]
                suffix = "/..." if len(f.picklist_values) > _SNAPSHOT_MAX_PICKLIST else ""
                type_str = f"picklist: {'/'.join(vals)}{suffix}"
            else:
                type_str = f.data_type

            req_flag = " — required" if f.is_required else ""
            lines.append(
                f"  {f.api_name}{custom_mark} ({f.label}, {type_str}){req_flag}"
            )
        lines.append("")

    return "\n".join(lines).strip(), valid_fields


def _extract_json(text: str) -> dict:
    """
    Parse JSON from Claude's response, handling optional markdown code fences.

    Claude is instructed to return JSON only, but occasionally wraps it in
    ```json ... ``` fences. This function tries direct parse first, then
    extracts from fences as a fallback.

    Raises:
        ValueError if neither approach succeeds.
    """
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract from ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse JSON from Claude intent response. "
        f"First 300 chars: {text[:300]}"
    )
