"""
knowledge/conflicts.py

SQLite persistence for KnowledgeConflict objects.

Called from orchestrator/core.py end_session() when synthesize_session()
returns conflicts that need coworker review before the new chunk can be
written to the org_knowledge ChromaDB collection.

Lifecycle:
  1. end_session() calls write_conflicts() — rows inserted with resolution=NULL
  2. Coworker reviews (Slack command / admin UI) and calls resolve_conflict()
  3. Caller reads resolution and actions the chunk:
       "new_wins"      → write new chunk to org_knowledge (replacing existing if any)
       "existing_wins" → discard new chunk
       "both_valid"    → write new chunk alongside existing
"""

import json
import logging
import uuid
from datetime import datetime

from db.connection import get_connection
from knowledge.synthesizer import KnowledgeConflict

logger = logging.getLogger(__name__)

_VALID_RESOLUTIONS = {"new_wins", "existing_wins", "both_valid"}


def write_conflicts(conflicts: list[KnowledgeConflict]) -> list[str]:
    """
    Persist a list of KnowledgeConflict objects to the knowledge_conflicts table.

    Returns list of conflict_ids written (UUIDs).
    """
    if not conflicts:
        return []

    conn = get_connection()
    ids = []

    for conflict in conflicts:
        conflict_id = str(uuid.uuid4())
        chunk = conflict.new_chunk

        conn.execute(
            """
            INSERT INTO knowledge_conflicts (
                conflict_id, new_chunk_content, new_chunk_type,
                new_chunk_objects, new_chunk_confidence, new_chunk_session,
                existing_content, existing_collection, conflict_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict_id,
                chunk.content,
                chunk.chunk_type,
                json.dumps(chunk.objects),
                float(chunk.confidence),
                chunk.source_session,
                conflict.existing_content,
                conflict.existing_collection,
                conflict.conflict_type,
            ),
        )
        ids.append(conflict_id)

    conn.commit()
    logger.info("Wrote %d knowledge conflicts to SQLite", len(ids))
    return ids


def get_pending_conflicts() -> list[dict]:
    """
    Return all unresolved knowledge conflicts (resolution IS NULL).

    Returns list of dicts with all conflict fields; new_chunk_objects is
    deserialized from JSON to a Python list.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM knowledge_conflicts WHERE resolution IS NULL ORDER BY created_at ASC"
    ).fetchall()

    result = []
    for row in rows:
        r = dict(row)
        r["new_chunk_objects"] = json.loads(r.get("new_chunk_objects") or "[]")
        result.append(r)

    return result


def resolve_conflict(conflict_id: str, resolution: str) -> bool:
    """
    Set the resolution for a pending knowledge conflict.

    Args:
        conflict_id: UUID of the conflict to resolve
        resolution:  "new_wins" | "existing_wins" | "both_valid"

    Returns True if the conflict was found and updated, False if not found.
    Raises ValueError for invalid resolution values.
    """
    if resolution not in _VALID_RESOLUTIONS:
        raise ValueError(
            f"Invalid resolution '{resolution}'. "
            f"Must be one of: {', '.join(sorted(_VALID_RESOLUTIONS))}"
        )

    conn = get_connection()
    cursor = conn.execute(
        "UPDATE knowledge_conflicts SET resolution=?, resolved_at=? WHERE conflict_id=?",
        (resolution, datetime.utcnow(), conflict_id),
    )
    conn.commit()

    found = cursor.rowcount > 0
    if found:
        logger.info("Resolved conflict %s → '%s'", conflict_id, resolution)
    else:
        logger.warning("Conflict %s not found", conflict_id)
    return found
