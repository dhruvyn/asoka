"""
knowledge/synthesizer.py

Post-session OrgKnowledge synthesis.

Called once per session when the user explicitly resets (/reset) or
when clear_session() is triggered. Takes the full conversation history
and session knowledge accumulated during the session, runs a single
Claude call, and returns a structured OrgKnowledge object ready to be
written to the persistent ChromaDB org_knowledge collection.

Flow:
  1. Render session knowledge in synthesis mode (verbose, with error context)
  2. Load top relevant chunks from existing rules + org_knowledge collections
     to give Claude awareness of what is already known
  3. Claude extracts generalizable knowledge into typed chunks
  4. Each chunk is conflict-checked against existing collections (top-k)
  5. Clean chunks are returned for writing; conflicting chunks are flagged
     as KnowledgeConflict for human coworker review

Two data objects:
  OrgKnowledge        — synthesized output, ready for ChromaDB write
  KnowledgeConflict   — chunk that contradicts or closely overlaps existing
                        knowledge; held pending coworker resolution
"""

import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from config import cfg
from knowledge.loader import query as _chroma_query, query_all_knowledge as _chroma_query_all
from orchestrator.session import SessionKnowledge, ConversationHistory
from orchestrator.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
_MODEL = "claude-sonnet-4-6"

# Top-k existing chunks fetched per new chunk for conflict detection.
# No raw distance threshold — always check top-k and let Claude judge.
_CONFLICT_CHECK_TOP_K = 3

# Minimum conflict check similarity — chunks with top-1 distance above this
# are too dissimilar to conflict (saves Claude calls on clearly unrelated chunks).
_CONFLICT_SKIP_DISTANCE = 0.75


# ─────────────────────────────────────────────────────────────────────────────
# Output types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OrgKnowledgeChunk:
    """
    One unit of persistent org knowledge, stored as a ChromaDB document.

    content:         the knowledge statement — written by Claude as
                     "intended action + error/observation + correct approach"
                     so it has rich semantic surface for future similarity search
    chunk_type:      category for stage-filtered retrieval
    objects:         SF object API names this chunk relates to
    confidence:      1.0 = confirmed by SF (OK result / fired rule)
                     0.7 = inferred from context
                     0.4 = uncertain
    source_session:  session_id for traceability
    """
    content: str
    chunk_type: str         # "field_correction" | "schema_note" |
                            # "validation_rule" | "record_context" | "misc"
    objects: list[str]
    confidence: float
    source_session: str


@dataclass
class OrgKnowledge:
    """Batch of synthesized chunks ready to write to persistent storage."""
    chunks: list[OrgKnowledgeChunk] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.chunks


@dataclass
class KnowledgeConflict:
    """
    A new chunk that contradicts or closely overlaps an existing chunk.
    Held pending coworker resolution — not written to DB until resolved.

    resolution options (set by coworker):
      "new_wins"      — replace existing with new chunk
      "existing_wins" — discard new chunk
      "both_valid"    — write new chunk alongside existing (different context)
    """
    new_chunk: OrgKnowledgeChunk
    existing_content: str       # the conflicting existing chunk content
    existing_collection: str    # "rules" | "org_knowledge"
    conflict_type: str          # "contradiction" | "overlap"
    resolution: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def synthesize_session(
    session_id: str,
    history: ConversationHistory,
    knowledge: SessionKnowledge,
) -> tuple[OrgKnowledge, list[KnowledgeConflict]]:
    """
    Synthesize OrgKnowledge from a completed session.

    Args:
        session_id: identifier for this session (used for traceability)
        history:    full ConversationHistory from the session
        knowledge:  SessionKnowledge accumulated during the session

    Returns:
        (org_knowledge, conflicts) where:
          org_knowledge — chunks ready to write to ChromaDB
          conflicts     — chunks that need coworker resolution before writing
    """
    if knowledge.is_empty() and history.is_empty():
        logger.info("synthesize_session: nothing to synthesize | session=%s", session_id)
        return OrgKnowledge(), []

    logger.info("Synthesizing session knowledge | session=%s", session_id)

    # Load a snapshot of existing knowledge to give Claude awareness
    existing_snapshot = _load_existing_snapshot(history)

    # Build synthesis prompt
    prompt = _build_synthesis_prompt(history, knowledge, existing_snapshot)

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=2048,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    logger.debug("Synthesis response: %s", raw[:400])

    chunks_data = _parse_chunks(raw)
    if not chunks_data:
        logger.info("Synthesis produced no chunks | session=%s", session_id)
        return OrgKnowledge(), []

    # Build OrgKnowledgeChunk objects
    raw_chunks = [
        OrgKnowledgeChunk(
            content=c.get("content", ""),
            chunk_type=c.get("chunk_type", "misc"),
            objects=c.get("objects") or [],
            confidence=float(c.get("confidence", 0.7)),
            source_session=session_id,
        )
        for c in chunks_data
        if c.get("content", "").strip()
    ]

    # Conflict-check each chunk against existing knowledge (top-k, no raw distance gate)
    clean: list[OrgKnowledgeChunk] = []
    conflicts: list[KnowledgeConflict] = []

    for chunk in raw_chunks:
        conflict = _check_conflict(chunk)
        if conflict:
            conflicts.append(conflict)
            logger.info(
                "Conflict detected | type=%s | existing=%r | new=%r",
                conflict.conflict_type,
                conflict.existing_content[:80],
                chunk.content[:80],
            )
        else:
            clean.append(chunk)

    logger.info(
        "Synthesis complete | session=%s | clean=%d | conflicts=%d",
        session_id, len(clean), len(conflicts),
    )
    return OrgKnowledge(chunks=clean), conflicts


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis prompt
# ─────────────────────────────────────────────────────────────────────────────

def _build_synthesis_prompt(
    history: ConversationHistory,
    knowledge: SessionKnowledge,
    existing_snapshot: str,
) -> str:
    history_block = history.to_prompt_block()
    knowledge_block = knowledge.to_prompt_block(mode="synthesis")

    return f"""\
You are extracting persistent knowledge from a completed Salesforce CRM session.
This knowledge will be stored and used in all future sessions.

Write each chunk as: what was being attempted + what was observed/failed + \
the correct approach.
This gives future sessions rich context, not just bare corrections.

--- Existing knowledge (do not duplicate — flag contradictions with low confidence) ---
{existing_snapshot}

--- Session conversation history ---
{history_block}

--- Session knowledge with full error context ---
{knowledge_block}

Extract only things that generalize beyond this specific conversation.
Confidence: 1.0 = confirmed by Salesforce (OK result or fired validation rule)
            0.7 = inferred from context
            0.4 = uncertain or possibly specific to this account/user

Return JSON only — no explanation, no preamble, no code fences:
{{
  "chunks": [
    {{
      "content": "<rich statement: intent + error/observation + correct approach>",
      "chunk_type": "field_correction" | "schema_note" | "validation_rule" | "record_context" | "misc",
      "objects": ["<SF object API names>"],
      "confidence": 0.0-1.0
    }}
  ]
}}

Rules:
- field_correction: confirmed API name corrections with object context and usage guidance
- schema_note: relationship names, object quirks, deprecated field observations
- validation_rule: rules that actually fired or were confirmed (include the constraint value)
- record_context: named accounts/records significant enough to remember long-term
- misc: anything else worth noting for future sessions
- If a chunk would duplicate existing knowledge above, omit it
- If a chunk contradicts existing knowledge, still include it with confidence <= 0.4
- If nothing worth persisting, return {{"chunks": []}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Conflict detection — top-k, no raw distance threshold
# ─────────────────────────────────────────────────────────────────────────────

def _check_conflict(chunk: OrgKnowledgeChunk) -> KnowledgeConflict | None:
    """
    Query existing knowledge (rules + org_knowledge) for top-k similar chunks.
    Pass the top match to Claude to determine if there is a conflict.

    Returns a KnowledgeConflict if a contradiction or significant overlap is
    found, otherwise None (chunk is safe to write).
    """
    try:
        existing = _chroma_query_all(text=chunk.content, n_results=_CONFLICT_CHECK_TOP_K)
    except Exception as exc:
        logger.warning("Conflict check query failed: %s — writing chunk without check", exc)
        return None

    if not existing:
        return None

    top = existing[0]

    # Skip the Claude call if the top result is clearly unrelated
    if top.get("distance", 1.0) > _CONFLICT_SKIP_DISTANCE:
        return None

    # Ask Claude: contradiction, overlap, or fine?
    verdict = _claude_conflict_verdict(chunk.content, top["text"])

    if verdict in ("contradiction", "overlap"):
        return KnowledgeConflict(
            new_chunk=chunk,
            existing_content=top["text"],
            existing_collection=top.get("collection", "rules"),
            conflict_type=verdict,
        )
    return None


def _claude_conflict_verdict(new_content: str, existing_content: str) -> str:
    """
    Ask Claude whether two knowledge chunks conflict.

    Returns: "contradiction" | "overlap" | "fine"
    """
    prompt = f"""\
Compare these two knowledge statements about a Salesforce CRM system.

Existing:
{existing_content}

New:
{new_content}

Classify their relationship as one of:
- "contradiction": they make incompatible claims (e.g. different field names for the same thing)
- "overlap": they say essentially the same thing (new adds no new information)
- "fine": they are about different topics or the new one genuinely extends the existing one

Respond with one word only: contradiction, overlap, or fine.
"""
    try:
        response = _client.messages.create(
            model=_MODEL,
            max_tokens=10,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip().lower()
    except Exception as exc:
        logger.warning("Conflict verdict Claude call failed: %s — treating as fine", exc)
        return "fine"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_existing_snapshot(history: ConversationHistory) -> str:
    """
    Load a compact snapshot of existing knowledge relevant to this session's
    topics — injected into the synthesis prompt so Claude avoids duplicating
    what is already known and flags genuine contradictions.
    """
    if not history.accumulated_data:
        return "(no existing knowledge loaded)"

    # Use the accumulated data summary as the query — captures the topics discussed
    query_text = history.accumulated_data[:500]
    try:
        chunks = _chroma_query_all(text=query_text, n_results=8)
        if not chunks:
            return "(no relevant existing knowledge found)"
        lines = []
        for c in chunks:
            label = c.get("chunk_type") or c.get("category", "general")
            lines.append(f"[{label}] {c['text'][:200]}")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Failed to load existing snapshot: %s", exc)
        return "(existing knowledge unavailable)"


def _parse_chunks(raw: str) -> list[dict]:
    """Parse Claude's JSON response, returning the chunks list."""
    try:
        data = json.loads(raw)
        return data.get("chunks") or []
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if match:
        try:
            data = json.loads(match.group(1).strip())
            return data.get("chunks") or []
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse synthesis JSON — no chunks extracted")
    return []
