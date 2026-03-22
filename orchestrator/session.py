"""
orchestrator/session.py

In-memory session state machine for per-user conversation tracking.

States:
  idle        — conversational mode; may have history from prior turns
  proposing   — intent classified, proposal shown to user, awaiting confirmation
  plan_shown  — WRITE plan generated and shown to user, awaiting user confirmation

Thread safety: a single Lock protects all dict mutations.
Sessions are never persisted — a restart clears all sessions.

History persists across the session lifetime:
  - Accumulated SF records from all fetches
  - All conversation turns (user + assistant)
  - Queries run so far

SessionKnowledge persists across the session lifetime:
  - Field corrections discovered through VALIDATION_ERRORs
  - Relationship name corrections from EXECUTION_ERRORs
  - Validation rules observed from WRITE plan risks/pre_flight_issues
  - Record metadata (IDs, key field values) from OK query results
  - Raw error records with full context for post-session synthesis

reset_to_idle() → keeps history, context, and knowledge; resets flow state
clear_session() → full wipe (only used by explicit /reset)
"""

import threading
from dataclasses import dataclass, field
from typing import Any

# Bounds for ConversationContext — keeps classification signal pool small.
_MAX_CTX_OBJECTS = 5
_MAX_CTX_HINTS   = 5
_MAX_CTX_FIELDS  = 5   # per object

# Bounds for ConversationHistory prompt injection.
_MAX_HISTORY_TURNS  = 10   # recent turns shown to Claude in follow-up answers
_MAX_HISTORY_DATA   = 4000 # chars of accumulated SF data passed to reader/prompts

# Bounds for SessionKnowledge.
_MAX_KNOWLEDGE_ERRORS    = 50  # raw error records kept per session
_MAX_KNOWLEDGE_METADATA  = 30  # metadata facts kept per session


# ─────────────────────────────────────────────────────────────────────────────
# SessionKnowledge — survives state resets, accumulates learnings from errors
# and query results throughout the session; exported for post-session synthesis
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FieldCorrectionEntry:
    """
    A field name correction discovered through a VALIDATION_ERROR.

    wrong:         the field name Claude tried (does not exist on the object)
    correct:       list of candidate field names from the error message
                   (all custom fields suggested — Claude picks the best one
                   during synthesis based on user intent)
    object_name:   the SF object the error occurred on
    error_context: the raw error line(s) from the results block, preserved
                   verbatim so the synthesis prompt has full context
    """
    wrong: str
    correct: list[str]
    object_name: str
    error_context: str


@dataclass
class RelationshipCorrectionEntry:
    """
    A relationship name correction discovered through an EXECUTION_ERROR.

    wrong:         the relationship name Claude tried (e.g. "Opportunities")
    correct:       the valid relationship API name (e.g. "Opportunities__r")
    parent_object: the object the subquery was on (e.g. "Account")
    error_context: the raw error line from the results block
    """
    wrong: str
    correct: str
    parent_object: str
    error_context: str


@dataclass
class ErrorRecord:
    """
    Full record of one failed query — used as context input during
    post-session OrgKnowledge synthesis so Claude can write rich,
    intent-aware knowledge chunks rather than bare corrections.

    query_label:         the query descriptor line, e.g.
                         "[simple] Opportunity WHERE Discount__c > 0.15"
    error_type:          "VALIDATION_ERROR" | "EXECUTION_ERROR"
    error_message:       full error text from the results block
    correction_applied:  what was substituted in the corrected query (if known)
    user_intent:         the raw user message that triggered this query chain
    """
    query_label: str
    error_type: str
    error_message: str
    correction_applied: str | None
    user_intent: str


@dataclass
class SessionKnowledge:
    """
    Accumulated learnings from the current session.

    Populated programmatically — no Claude call required during the session.
    Persists across reset_to_idle(); wiped only by clear_session().

    Two rendering modes via to_prompt_block(mode):
      "inject"    — compact, used at query-plan / planner prompt stages
      "synthesis" — verbose with full error context, used as input to the
                    post-session OrgKnowledge synthesis Claude call
    """
    field_corrections: list[FieldCorrectionEntry] = field(default_factory=list)
    relationship_corrections: list[RelationshipCorrectionEntry] = field(default_factory=list)
    validation_rules: list[str] = field(default_factory=list)
    metadata: list[str] = field(default_factory=list)
    error_queries: list[ErrorRecord] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.field_corrections,
            self.relationship_corrections,
            self.validation_rules,
            self.metadata,
            self.error_queries,
        ])

    def merge(self, other: "SessionKnowledge") -> None:
        """Union-merge another SessionKnowledge into this one (dedup by key)."""
        # Field corrections — dedup by (wrong, object_name)
        existing_fc = {(e.wrong, e.object_name) for e in self.field_corrections}
        for e in other.field_corrections:
            if (e.wrong, e.object_name) not in existing_fc:
                self.field_corrections.append(e)
                existing_fc.add((e.wrong, e.object_name))

        # Relationship corrections — dedup by (wrong, parent_object)
        existing_rc = {(e.wrong, e.parent_object) for e in self.relationship_corrections}
        for e in other.relationship_corrections:
            if (e.wrong, e.parent_object) not in existing_rc:
                self.relationship_corrections.append(e)
                existing_rc.add((e.wrong, e.parent_object))

        # Flat lists — dedup by value
        for item in other.validation_rules:
            if item not in self.validation_rules:
                self.validation_rules.append(item)

        for item in other.metadata:
            if item not in self.metadata:
                self.metadata.append(item)
        self.metadata = self.metadata[:_MAX_KNOWLEDGE_METADATA]

        # Error records — dedup by query_label
        existing_eq = {e.query_label for e in self.error_queries}
        for e in other.error_queries:
            if e.query_label not in existing_eq:
                self.error_queries.append(e)
                existing_eq.add(e.query_label)
        self.error_queries = self.error_queries[:_MAX_KNOWLEDGE_ERRORS]

    def to_prompt_block(self, mode: str = "inject") -> str:
        """
        Render session knowledge for prompt injection.

        mode="inject"    — compact summary; injected at query-plan and planner stages
        mode="synthesis" — verbose with error context; input to post-session synthesis
        """
        if self.is_empty():
            return ""

        lines = ["=== Learned from this session ==="]

        if self.field_corrections:
            lines.append("Field corrections:")
            for e in self.field_corrections:
                candidates = ", ".join(e.correct)
                lines.append(f"  {e.wrong} → {candidates} ({e.object_name})")
                if mode == "synthesis":
                    lines.append(f"    Context: {e.error_context}")

        if self.relationship_corrections:
            lines.append("Relationship corrections:")
            for e in self.relationship_corrections:
                lines.append(f"  {e.wrong} → {e.correct} ({e.parent_object})")
                if mode == "synthesis":
                    lines.append(f"    Context: {e.error_context}")

        if self.validation_rules:
            lines.append("Validation rules observed:")
            for r in self.validation_rules:
                lines.append(f"  - {r}")

        if self.metadata:
            lines.append("Record metadata:")
            for m in self.metadata:
                lines.append(f"  - {m}")

        if mode == "synthesis" and self.error_queries:
            lines.append("\n=== Raw error queries (for synthesis context) ===")
            for eq in self.error_queries:
                lines.append(f"\nQuery:      {eq.query_label}")
                lines.append(f"Type:       {eq.error_type}")
                lines.append(f"Error:      {eq.error_message}")
                if eq.correction_applied:
                    lines.append(f"Correction: {eq.correction_applied}")
                lines.append(f"Intent:     {eq.user_intent}")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ConversationContext — survives state resets, accumulates signal for intent
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConversationContext:
    """
    Bounded pool of CRM concepts discussed so far.
    Injected into classify_intent on every turn so corrections don't lose signal.
    """
    objects: list[str] = field(default_factory=list)
    record_hints: list[str] = field(default_factory=list)
    field_hints: dict[str, list[str]] = field(default_factory=dict)

    def merge(
        self,
        new_objects: list[str],
        new_hints: list[str],
        new_field_hints: dict[str, list[str]] | None = None,
    ) -> None:
        for o in reversed(new_objects):
            if o in self.objects:
                self.objects.remove(o)
            self.objects.insert(0, o)
        self.objects = self.objects[:_MAX_CTX_OBJECTS]

        for h in reversed(new_hints):
            if h in self.record_hints:
                self.record_hints.remove(h)
            self.record_hints.insert(0, h)
        self.record_hints = self.record_hints[:_MAX_CTX_HINTS]

        if new_field_hints:
            for obj, flds in new_field_hints.items():
                existing = self.field_hints.get(obj, [])
                for f in reversed(flds):
                    if f in existing:
                        existing.remove(f)
                    existing.insert(0, f)
                self.field_hints[obj] = existing[:_MAX_CTX_FIELDS]

    def is_empty(self) -> bool:
        return not self.objects and not self.record_hints and not self.field_hints


# ─────────────────────────────────────────────────────────────────────────────
# ConversationHistory — full memory of this conversation session
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConversationTurn:
    """One exchange in the conversation."""
    role: str           # "user" | "assistant"
    content: str
    intent_type: str    # "READ", "WRITE", "PROPOSAL", "READ_ANSWER", "CONVERSATION", etc.


@dataclass
class ConversationHistory:
    """
    Accumulated record of the current conversation session.

    turns:            all user/assistant messages in order
    accumulated_data: merged SF records from every fetch in this session
    queries_run:      log of every query attempted (type, spec, status)

    This data is:
      - Fed to the reader as a seed so it knows what was already fetched
      - Injected into follow-up answer prompts
      - Cleared only on explicit /reset
    """
    turns: list[ConversationTurn] = field(default_factory=list)
    accumulated_data: str = ""
    queries_run: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.turns and not self.accumulated_data

    def add_turn(self, role: str, content: str, intent_type: str = "") -> None:
        self.turns.append(
            ConversationTurn(role=role, content=content, intent_type=intent_type)
        )

    def add_data(self, records_block: str, queries: list[dict] | None = None) -> None:
        """Merge new SF records into the accumulated store."""
        if records_block and records_block.strip():
            if self.accumulated_data:
                self.accumulated_data = (
                    self.accumulated_data + "\n\n" + records_block
                ).strip()
            else:
                self.accumulated_data = records_block.strip()
        if queries:
            self.queries_run.extend(queries)

    def to_prompt_block(self) -> str:
        """
        Format recent history for injection into Claude prompts.
        Capped to avoid token overrun.
        """
        lines = []

        if self.turns:
            lines.append("=== Conversation History ===")
            recent = self.turns[-_MAX_HISTORY_TURNS:]
            for t in recent:
                prefix = "User" if t.role == "user" else "Asoka"
                # Cap individual turn length to keep total manageable
                content = t.content[:400] + ("..." if len(t.content) > 400 else "")
                lines.append(f"{prefix}: {content}")

        if self.accumulated_data:
            lines.append("\n=== Previously Fetched Salesforce Data ===")
            data = self.accumulated_data
            if len(data) > _MAX_HISTORY_DATA:
                data = data[:_MAX_HISTORY_DATA] + "\n... (truncated)"
            lines.append(data)

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SessionState
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    """
    Per-user conversation state.

    user_id:       Slack user ID
    state:         current state ("idle" | "proposing" | "plan_shown")
    intent:        last classified IntentResult (set when state → proposing)
    plan:          BatchPlan from planner (set when state → plan_shown)
    records_block: pre-fetched SF records string cached between proposing→plan_shown
    context:       accumulated CRM classification context (persists across corrections)
    history:       full conversation memory (persists until explicit /reset)
    """
    user_id: str
    state: str = "idle"
    intent: Any = None
    plan: Any = None
    records_block: str = ""
    context: ConversationContext = field(default_factory=ConversationContext)
    history: ConversationHistory = field(default_factory=ConversationHistory)
    knowledge: SessionKnowledge = field(default_factory=SessionKnowledge)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level store and lock
# ─────────────────────────────────────────────────────────────────────────────

_sessions: dict[str, SessionState] = {}
_lock = threading.Lock()


def get_session(user_id: str) -> SessionState:
    """Return the session for user_id. Creates a fresh idle session if needed."""
    with _lock:
        if user_id not in _sessions:
            _sessions[user_id] = SessionState(user_id=user_id)
        return _sessions[user_id]


def update_session(user_id: str, **kwargs) -> SessionState:
    """Update one or more fields on an existing session and return it."""
    with _lock:
        if user_id not in _sessions:
            _sessions[user_id] = SessionState(user_id=user_id)
        session = _sessions[user_id]
        for k, v in kwargs.items():
            setattr(session, k, v)
        return session


def reset_to_idle(user_id: str) -> None:
    """
    Reset flow state to idle, preserving context and history.

    Used after:
      - A READ answer is returned (history gains the answer + data)
      - A plan is forwarded (history gains the event)
      - A mid-flow correction (user sent a non-confirmation)

    History and context are intentionally kept so the user can immediately
    ask follow-up questions about what was just found.
    """
    with _lock:
        session = _sessions.get(user_id)
        if session:
            session.state = "idle"
            session.intent = None
            session.plan = None
            session.records_block = ""
        # If no session exists, nothing to reset


# Keep the old name as an alias so existing call sites still work
def reset_session_keep_context(user_id: str) -> None:
    """Alias for reset_to_idle — preserved for backwards compatibility."""
    reset_to_idle(user_id)


def clear_session(user_id: str) -> None:
    """
    Full wipe — discards history, context, and all state.
    Only called by explicit /reset in the chat interface and test teardown.
    NOT called on normal flow completion.
    """
    with _lock:
        _sessions[user_id] = SessionState(user_id=user_id)
