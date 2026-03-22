"""
orchestrator/core.py

Single public entry point for the entire orchestrator layer.

Session state machine
─────────────────────
  idle        — conversational mode; has history from prior turns in this session
  proposing   — intent classified, proposal shown to user, awaiting confirmation
  plan_shown  — WRITE plan generated, shown to user, awaiting final confirmation

State transitions:
  idle (no history)  + any message         → classify → propose  → proposing
  idle (has history) + UNKNOWN/follow-up   → answer from history → idle
  idle (has history) + READ/WRITE message  → classify → propose  → proposing
  proposing          + confirmation        → execute read OR build plan
    READ result   → answer stored in history → idle (history preserved)
    WRITE result  → plan_shown
  proposing          + correction/other    → re-classify          → proposing
  plan_shown         + confirmation        → forward to coworker  → idle
  plan_shown         + correction/other    → re-classify          → proposing

HandleResult types:
  "PROPOSAL"          user sees proposal, must confirm before anything runs
  "READ_ANSWER"       read completed, text contains the answer
  "PLAN_PREVIEW"      WRITE plan built and shown, user must confirm to forward
  "SEND_FOR_APPROVAL" plan forwarded to coworker
  "CONVERSATION"      follow-up answered from history (no new SF fetch)
  "MIXED"             mixed read+write, user asked to split
  "UNKNOWN"           can't classify and no history to draw from

Input:  raw message string + Slack user_id
Output: HandleResult dataclass
"""

import logging
from dataclasses import dataclass

import anthropic

from config import cfg
from context.structural import get_object_fields, field_exists
from orchestrator.intent import classify_intent, IntentResult
from orchestrator.reader import handle_read, ReadResult, _filter_ok_records
from orchestrator.planner import generate_plan, BatchPlan
from orchestrator.session import (
    get_session, update_session, clear_session,
    reset_to_idle, ConversationHistory,
)
from orchestrator.prompts import SYSTEM_PROMPT, build_conversation_followup_prompt
from salesforce.query import find_records
from knowledge.synthesizer import synthesize_session
from knowledge.loader import add_org_knowledge
from knowledge.conflicts import write_conflicts

logger = logging.getLogger(__name__)

# One Anthropic client for the module — used only by _answer_from_history.
_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
_MODEL = "claude-sonnet-4-6"

# Max non-priority fields pulled per object in the initial lookup.
_MAX_LOOKUP_FIELDS = 15

_CONFIRM_WORDS = {
    "yes", "yeah", "yep", "yup", "y",
    "ok", "okay", "k",
    "sure", "go", "go ahead",
    "confirm", "confirmed",
    "looks good", "looks right",
    "do it", "proceed",
    "approved", "approve",
    "sounds good", "correct",
    "right", "perfect", "exactly",
    "send it", "send",
}


@dataclass
class HandleResult:
    """
    Output of handle(). Consumed by the Slack listener.

    intent_type:  one of the seven types listed in the module docstring
    text:         message to send to the user in Slack
    plan:         populated for PLAN_PREVIEW and SEND_FOR_APPROVAL
    """
    intent_type: str
    text: str
    plan: BatchPlan | None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def end_session(user_id: str, session_id: str | None = None) -> None:
    """
    Gracefully end a session: synthesize accumulated knowledge into persistent
    OrgKnowledge, log any conflicts for coworker review, then wipe the session.

    Called by the Slack listener on /reset. Uses the user_id as the session_id
    if no explicit session_id is provided.

    Synthesis is best-effort — if it fails the session is still cleared.
    """
    session = get_session(user_id)
    sid = session_id or user_id

    if not session.knowledge.is_empty() or not session.history.is_empty():
        try:
            org_knowledge, conflicts = synthesize_session(
                session_id=sid,
                history=session.history,
                knowledge=session.knowledge,
            )
            if not org_knowledge.is_empty():
                written = add_org_knowledge(org_knowledge.chunks)
                logger.info(
                    "end_session: %d chunks written to org_knowledge | user=%s",
                    written, user_id,
                )
            if conflicts:
                conflict_ids = write_conflicts(conflicts)
                logger.warning(
                    "end_session: %d knowledge conflicts need coworker review | ids=%s | user=%s",
                    len(conflicts), conflict_ids, user_id,
                )
                # TODO: surface conflict notification to coworker via Slack
        except Exception as exc:
            logger.error(
                "end_session: synthesis failed (session will still clear) | user=%s | error=%s",
                user_id, exc,
            )

    clear_session(user_id)
    logger.info("end_session: session cleared | user=%s", user_id)


def handle(message: str, user_id: str) -> HandleResult:
    """
    Process one user message through the session state machine.
    The only function the Slack listener calls.
    """
    logger.info("handle() | user=%s | message=%r", user_id, message[:80])

    session = get_session(user_id)
    logger.info("Session state | user=%s | state=%s | history_turns=%d",
                user_id, session.state, len(session.history.turns))

    # ── Confirming a proposal ──────────────────────────────────────────────
    if session.state == "proposing" and _is_confirmation(message):
        return _execute_confirmed(session, user_id)

    # ── Confirming a plan preview → forward to coworker ───────────────────
    if session.state == "plan_shown" and _is_confirmation(message):
        plan = session.plan
        session.history.add_turn("user", message, "CONFIRM")
        session.history.add_turn(
            "assistant",
            "Plan forwarded to your manager for approval.",
            "SEND_FOR_APPROVAL",
        )
        reset_to_idle(user_id)
        return HandleResult(
            intent_type="SEND_FOR_APPROVAL",
            text=(
                "Plan forwarded to your manager for approval. "
                "You'll be notified once they approve or deny it."
            ),
            plan=plan,
        )

    # ── Mid-flow correction — reset flow state, keep history + context ────
    if session.state != "idle":
        logger.info(
            "Non-confirmation in state=%s — resetting to idle | user=%s",
            session.state, user_id,
        )
        reset_to_idle(user_id)

    return _classify_and_propose(message, user_id)


# ─────────────────────────────────────────────────────────────────────────────
# Internal: flow steps
# ─────────────────────────────────────────────────────────────────────────────

def _classify_and_propose(message: str, user_id: str) -> HandleResult:
    """
    Classify intent and return a proposal for the user to confirm,
    OR answer directly from history if the message is a conversational follow-up.
    """
    session = get_session(user_id)
    intent = classify_intent(message, user_id, context=session.context)

    # Merge new signal into conversation context
    field_hints_by_obj: dict[str, list[str]] = {
        obj: list(intent.field_hints)
        for obj in intent.objects
        if intent.field_hints
    }
    session.context.merge(intent.objects, intent.record_hints, field_hints_by_obj)

    # UNKNOWN with history → answer conversationally from what we already know
    if intent.intent_type == "UNKNOWN" and not session.history.is_empty():
        logger.info("UNKNOWN intent with history — answering as follow-up | user=%s", user_id)
        answer = _answer_from_history(message, session.history)
        session.history.add_turn("user", message, "CONVERSATION")
        session.history.add_turn("assistant", answer, "CONVERSATION")
        return HandleResult(intent_type="CONVERSATION", text=answer, plan=None)

    if intent.intent_type == "UNKNOWN":
        return HandleResult(
            intent_type="UNKNOWN",
            text=(
                "I'm not sure what you'd like me to do. "
                "Could you be more specific? For example:\n"
                '  "What is the tier of TechGlobal?" or\n'
                '  "Update the discount on the Acme renewal to 15%".'
            ),
            plan=None,
        )

    if intent.intent_type == "MIXED":
        return HandleResult(
            intent_type="MIXED",
            text=(
                "Your message seems to ask for both a read *and* a write. "
                "Please send them as two separate messages so I can handle "
                "each one safely:\n"
                "1. First ask your question (read)\n"
                "2. Then make your change request (write)"
            ),
            plan=None,
        )

    # Store intent, record user message in history, transition to proposing
    session.history.add_turn("user", message, intent.intent_type)
    update_session(user_id, state="proposing", intent=intent)

    proposal = _build_proposal(intent)
    return HandleResult(intent_type="PROPOSAL", text=proposal, plan=None)


def _execute_confirmed(session, user_id: str) -> HandleResult:
    """
    User confirmed the proposal — execute it.

    READ:  fresh lookup + history data → reader → answer → back to idle with history
    WRITE: fresh lookup → generate plan → show preview
    """
    intent: IntentResult = session.intent
    fresh_records = _lookup_records(intent.objects, intent.record_hints)

    # Seed the reader with both fresh lookup AND all previously fetched data
    # so it can use Account IDs / record IDs discovered in prior turns.
    if session.history.accumulated_data and fresh_records:
        combined_records = (
            session.history.accumulated_data + "\n\n" + fresh_records
        ).strip()
    else:
        combined_records = fresh_records or session.history.accumulated_data

    knowledge_block = session.knowledge.to_prompt_block(mode="inject")

    if intent.intent_type == "READ":
        result = handle_read(intent, combined_records, knowledge_block=knowledge_block)

        # Store filtered records (OK sections only) into history —
        # error lines are noise for future turns
        filtered = _filter_ok_records(result.full_records)
        session.history.add_data(filtered)
        session.history.add_turn("assistant", result.answer, "READ_ANSWER")

        # Merge session knowledge extracted during this READ
        if not result.session_knowledge.is_empty():
            session.knowledge.merge(result.session_knowledge)
            logger.info(
                "Session knowledge updated | user=%s | fc=%d rc=%d vr=%d meta=%d eq=%d",
                user_id,
                len(session.knowledge.field_corrections),
                len(session.knowledge.relationship_corrections),
                len(session.knowledge.validation_rules),
                len(session.knowledge.metadata),
                len(session.knowledge.error_queries),
            )

        reset_to_idle(user_id)
        return HandleResult(intent_type="READ_ANSWER", text=result.answer, plan=None)

    # WRITE path — generate plan and show for second confirmation
    plan = generate_plan(intent, combined_records, knowledge_block=knowledge_block)
    update_session(user_id, state="plan_shown", plan=plan, records_block=combined_records)

    # Extract validation rules and blockers from the plan into session knowledge
    write_rules = [r for r in (plan.risks or []) if r not in session.knowledge.validation_rules]
    write_issues = [i for i in (plan.pre_flight_issues or []) if i not in session.knowledge.validation_rules]
    for rule in write_rules + write_issues:
        session.knowledge.validation_rules.append(rule)
    if write_rules or write_issues:
        logger.info(
            "Session knowledge: added %d rules from WRITE plan | user=%s",
            len(write_rules) + len(write_issues), user_id,
        )

    preview = _build_plan_preview(plan)
    return HandleResult(intent_type="PLAN_PREVIEW", text=preview, plan=plan)


# ─────────────────────────────────────────────────────────────────────────────
# Internal: conversation follow-up
# ─────────────────────────────────────────────────────────────────────────────

def _answer_from_history(message: str, history: ConversationHistory) -> str:
    """
    Answer a conversational follow-up using accumulated history and SF data.
    One Claude call — no new Salesforce queries.
    """
    prompt = build_conversation_followup_prompt(
        message=message,
        history_block=history.to_prompt_block(),
    )
    response = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Internal: text builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_proposal(intent: IntentResult) -> str:
    action = "read from" if intent.intent_type == "READ" else "update"
    objects_str = ", ".join(intent.objects) if intent.objects else "CRM records"
    hints_str = (
        " (" + ", ".join(intent.record_hints) + ")"
        if intent.record_hints else ""
    )
    lines = [
        f"I understand you want me to *{action}* {objects_str}{hints_str}.",
        f"Summary: _{intent.summary}_",
        "",
        'Reply *yes* to proceed, or send a correction if I got it wrong.',
    ]
    return "\n".join(lines)


def _build_plan_preview(plan: BatchPlan) -> str:
    lines = [
        f"*Plan:* {plan.summary}",
        "",
        f"*Operations ({len(plan.operations)}):*",
    ]
    for op in plan.operations:
        record_ref = op.record_id or "(new record)"
        lines.append(
            f"  {op.order}. {op.method.upper()} {op.object_api_name} {record_ref}"
        )
        for field, val in op.payload.items():
            lines.append(f"       {field} = {val}")
        lines.append(f"       _{op.reason}_")

    if plan.assumptions:
        lines += ["", "*Assumptions:*"] + [f"  - {a}" for a in plan.assumptions]
    if plan.risks:
        lines += ["", "*Risks:*"] + [f"  - {r}" for r in plan.risks]
    if plan.pre_flight_issues:
        lines += [
            "",
            "*Pre-flight issues (must resolve before execution):*",
        ] + [f"  - {i}" for i in plan.pre_flight_issues]
        lines += ["", ":warning: This plan has blockers. Fix them before approving."]

    lines += ["", 'Reply *yes* to forward this plan to your manager for approval.']
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Internal: helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_confirmation(message: str) -> bool:
    msg = message.strip().lower().rstrip("!. ")
    return msg in _CONFIRM_WORDS


def _lookup_records(objects: list[str], hints: list[str]) -> str:
    """
    Search Salesforce for records matching name hints from intent extraction.

    Field selection:
      - Always includes Id
      - Always includes Name (or Subject) immediately after Id — prevents them
        being cut by the field cap when alphabetical ordering pushes them out
      - Fills remaining slots with other non-deprecated fields up to _MAX_LOOKUP_FIELDS
    """
    if not hints or not objects:
        return ""

    lines = ["=== Records Found ===\n"]
    found_any = False

    for obj in objects:
        all_fields = get_object_fields(obj)

        # Priority fields: always at the front regardless of sort order
        priority: list[str] = []
        if field_exists(obj, "Name"):
            priority.append("Name")
        elif field_exists(obj, "Subject"):
            priority.append("Subject")

        priority_set = {"Id", "Name", "Subject"}
        remaining_cap = _MAX_LOOKUP_FIELDS - len(priority)
        other = [
            f.api_name for f in all_fields
            if f.api_name not in priority_set and not f.is_deprecated
        ][:remaining_cap]

        fields = ["Id"] + priority + other

        # Text search on the primary label field
        name_field = priority[0] if priority else None

        for hint in hints:
            safe_hint = hint.replace("'", "\\'")
            where = f"{name_field} LIKE '%{safe_hint}%'" if name_field else None

            try:
                records = find_records(obj, fields=fields, where=where, limit=5)
                for r in records:
                    found_any = True
                    lines.append(f"{obj}:")
                    for k, v in r.items():
                        if v is not None:
                            lines.append(f"  {k}: {v}")
                    lines.append("")
            except Exception as exc:
                logger.warning(
                    "Record lookup failed | obj=%s | hint=%r | error=%s",
                    obj, hint, exc,
                )

    if not found_any:
        return ""

    return "\n".join(lines).strip()
