"""
orchestrator/planner.py

Responsibilities:
  - Handle WRITE intents: context → Claude → structured BatchPlan
  - Accept the IntentResult from intent.py and the records block from core.py
  - Build schema context WITH validation rules (needed for pre-flight checks)
  - Call Claude with schema + records + user request
  - Parse Claude's JSON output into Operation and BatchPlan dataclasses
  - Return a BatchPlan ready for the approval formatter

The BatchPlan is the central artifact of the write flow. It is created here,
passed to approval/formatter.py for Slack display, and eventually consumed
by batchqueue/executor.py for execution.

Operation represents one atomic Salesforce API call:
  method=update: PATCH /sobjects/{Object}/{id} with payload
  method=create: POST /sobjects/{Object} with payload

BatchPlan groups operations in execution order with metadata the approver
needs: assumptions, risks, and pre_flight_issues that block execution.

Input:
    intent:        IntentResult from intent.py
    records_block: pre-formatted string of live SF records (from core._lookup_records)
Output:
    BatchPlan dataclass
"""

import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from config import cfg
from context.retriever import build_context
from orchestrator.intent import IntentResult
from orchestrator.prompts import SYSTEM_PROMPT, build_planner_prompt

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
_MODEL = "claude-sonnet-4-6"


# ─────────────────────────────────────────────────────────────────────────────
# Output types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Operation:
    """
    One atomic Salesforce write: a single API call.

    order:         execution sequence (1-indexed). Operations run in this order.
                   Template references like {{op_1.result.id}} resolve after
                   order=1 completes — used when a create must precede an update.
    object_api_name: the Salesforce object (Account, Opportunity, Case, User)
    method:        "create" (POST) or "update" (PATCH)
    record_id:     18-char SF ID for updates; None for creates
    payload:       field API names → values to write
    reason:        Claude's explanation of why this operation is needed
    """
    order: int
    object_api_name: str
    method: str                  # "create" | "update"
    record_id: str | None        # None for creates
    payload: dict
    reason: str


@dataclass
class BatchPlan:
    """
    A complete write plan: ordered operations + metadata for the approver.

    summary:          one-sentence description (shown at top of approval message)
    operations:       list of Operation, sorted by order
    assumptions:      things Claude assumed that aren't explicit in the request
    risks:            validation rules or policy constraints that could affect execution
    pre_flight_issues: blockers that must be resolved before execution can proceed
                       (missing records, permission gaps, rule violations)
                       If non-empty, the plan CANNOT be approved as-is.
    intent:           the original IntentResult (preserved for the executor)
    """
    summary: str
    operations: list[Operation]
    assumptions: list[str]
    risks: list[str]
    pre_flight_issues: list[str]
    intent: IntentResult


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_plan(
    intent: IntentResult,
    records_block: str,
    knowledge_block: str = "",
) -> BatchPlan:
    """
    Generate a BatchPlan from a WRITE intent.

    Flow:
      1. Build schema context WITH validation rules — Claude needs these to
         avoid proposing writes that would be rejected by Salesforce
      2. Combine with pre-fetched records (which carry real record IDs)
      3. Call Claude with the planner prompt
      4. Parse the JSON response into BatchPlan + Operation dataclasses

    Args:
        intent:          classified WRITE intent from classify_intent()
        records_block:   formatted live SF records string from core._lookup_records()
        knowledge_block: session knowledge rendered for prompt injection —
                         field corrections prevent wrong API names in payloads,
                         validation rules already observed surface as known constraints

    Returns:
        BatchPlan. If pre_flight_issues is non-empty, the plan is blocked
        and should not proceed to execution without human review.
    """
    logger.info(
        "Generating plan | objects=%s | user=%s",
        intent.objects, intent.user_id,
    )

    # Include validation rules so Claude can check for conflicts
    bundle = build_context(
        objects=intent.objects,
        query=intent.raw_message,
        include_validation_rules=True,
        stage="write",
    )
    context_block = bundle.to_prompt_block()

    prompt = build_planner_prompt(
        message=intent.raw_message,
        context_block=context_block,
        records_block=records_block,
        knowledge_block=knowledge_block,
    )

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=2048,
        temperature=0,   # deterministic — plans should be consistent
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()
    logger.debug("Planner raw response: %s", raw_text[:500])

    parsed = _extract_json(raw_text)
    plan = _parse_plan(parsed, intent)

    logger.info(
        "Plan generated | ops=%d | issues=%d | risks=%d",
        len(plan.operations), len(plan.pre_flight_issues), len(plan.risks),
    )
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_plan(parsed: dict, intent: IntentResult) -> BatchPlan:
    """
    Convert the parsed JSON dict from Claude into a BatchPlan dataclass.

    Defensively handles missing or malformed fields — if Claude omits a field,
    we use a safe default rather than crashing. The pre_flight_issues list is
    always preserved: if Claude flagged issues, the executor must not run.
    """
    raw_ops = parsed.get("operations") or []
    operations = []

    for op in raw_ops:
        operations.append(Operation(
            order=int(op.get("order", 1)),
            object_api_name=op.get("object", ""),
            method=op.get("method", "update").lower(),
            record_id=op.get("record_id") or None,   # normalise empty string → None
            payload=op.get("payload") or {},
            reason=op.get("reason", ""),
        ))

    # Sort by order so executor can rely on list position
    operations.sort(key=lambda o: o.order)

    return BatchPlan(
        summary=parsed.get("summary", intent.summary),
        operations=operations,
        assumptions=parsed.get("assumptions") or [],
        risks=parsed.get("risks") or [],
        pre_flight_issues=parsed.get("pre_flight_issues") or [],
        intent=intent,
    )


def _extract_json(text: str) -> dict:
    """
    Parse JSON from Claude's response, handling optional markdown code fences.

    Tries direct parse first, then extracts from ```json ... ``` as fallback.

    Raises:
        ValueError if neither approach succeeds.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse JSON from Claude planner response. "
        f"First 500 chars: {text[:500]}"
    )
