"""
knowledge/loader.py

Responsibilities:
  - Read rules.md and split it into chunks at section (##) boundaries
  - Embed each chunk using sentence-transformers (all-MiniLM-L6-v2)
  - Store chunks in ChromaDB with category metadata
  - Skip re-embedding if the collection already has documents (idempotent)

Why chunk at section headers?
  Each ## section in rules.md is a self-contained policy topic (Account Rules,
  Case Rules, User Deactivation Procedure, etc.). Chunking at these boundaries
  means a similarity search for "how do I deactivate a user?" retrieves exactly
  the User Deactivation Procedure section — not a merged blob of every rule.
  Smaller, topic-coherent chunks give better retrieval precision than large ones.

Why all-MiniLM-L6-v2?
  Fast (runs on CPU), small (80MB), good enough for sentence-level similarity
  on domain text. We don't need a large model here — the chunks are short
  policy paragraphs, not long documents.

Input:  knowledge/rules.md path from config, chroma_path from config
Output: a populated ChromaDB collection named "asoka_knowledge"

Usage:
    from knowledge.loader import init_knowledge

    init_knowledge()   # call once at startup in main.py
"""

import json
import logging
import re
import uuid
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

from config import cfg

logger = logging.getLogger(__name__)

COLLECTION_NAME = "asoka_knowledge"
ORG_KNOWLEDGE_COLLECTION = "org_knowledge"

# The embedding model — downloaded once and cached by sentence-transformers
# all-MiniLM-L6-v2: 384-dimensional embeddings, fast on CPU, ~80MB
_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Module-level ChromaDB client singleton
# chromadb.PersistentClient is a factory function in 1.x, not a class —
# annotate with Any to avoid runtime TypeError on the | operator
_chroma_client = None
_collection = None
_org_collection = None


def init_knowledge() -> None:
    """
    Initialize ChromaDB and load rules.md if the collection is empty.

    Idempotent: if the collection already contains documents (i.e., a
    previous startup already embedded rules.md), this function returns
    immediately without re-embedding. Re-embedding is only triggered when
    the collection is empty — e.g., first boot or after wiping chroma_store/.

    To force a reload (e.g., after editing rules.md), delete the
    chroma_store/ directory and restart the bot.
    """
    global _chroma_client, _collection, _org_collection

    logger.info("Initializing knowledge store at: %s", cfg.chroma_path)

    # PersistentClient stores data on disk at chroma_path.
    # Survives restarts — no re-embedding on every boot.
    _chroma_client = chromadb.PersistentClient(path=cfg.chroma_path)

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=_EMBEDDING_MODEL
    )

    _collection = _chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
        # cosine similarity: better than L2 for semantic text comparison
        # because it measures angle between vectors, not magnitude
    )

    # Persistent org_knowledge collection — written by post-session synthesis,
    # queried at prompt-build time to inject learned corrections and rules.
    _org_collection = _chroma_client.get_or_create_collection(
        name=ORG_KNOWLEDGE_COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info(
        "Org knowledge collection ready: %d chunks", _org_collection.count()
    )

    existing_count = _collection.count()
    if existing_count > 0:
        logger.info(
            "Knowledge store already populated (%d chunks). Skipping re-embed.",
            existing_count
        )
        return

    # Collection is empty — load and embed rules.md
    logger.info("Embedding rules.md into knowledge store...")
    chunks = _parse_rules_md(cfg.knowledge_path)
    _embed_chunks(chunks)
    logger.info("Knowledge store ready: %d chunks embedded", len(chunks))


def query(text: str, n_results: int = 5) -> list[dict]:
    """
    Similarity search against the knowledge store.

    Args:
        text:      the query string (typically the user's message or a
                   key phrase extracted from it)
        n_results: number of top chunks to return (default 5)

    Returns:
        List of dicts, each with:
          - "text":     the chunk content
          - "category": the section header it came from (e.g. "Account Rules")
          - "distance": cosine distance (lower = more similar)

    Raises:
        RuntimeError if init_knowledge() was never called.
    """
    if _collection is None:
        raise RuntimeError(
            "Knowledge store not initialized. "
            "Call knowledge.loader.init_knowledge() before querying."
        )

    results = _collection.query(
        query_texts=[text],
        n_results=min(n_results, _collection.count()),
        include=["documents", "metadatas", "distances"],
    )

    output = []
    # results["documents"] is a list-of-lists (one list per query text)
    # We send one query at a time, so index [0] is always the right list
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        output.append({
            "text": doc,
            "category": meta.get("category", "General"),
            "distance": round(dist, 4),
        })

    logger.debug("Knowledge query returned %d chunks for: %r", len(output), text[:60])
    return output


def add_org_knowledge(chunks) -> int:
    """
    Add synthesized OrgKnowledgeChunk objects to the org_knowledge collection.

    Accepts any objects with attributes:
        content, chunk_type, objects (list), confidence, source_session

    ChromaDB metadata values must be scalar — the objects list is JSON-encoded.
    Returns number of chunks added.
    """
    if _org_collection is None:
        raise RuntimeError(
            "Knowledge store not initialized. Call init_knowledge() first."
        )

    if not chunks:
        return 0

    ids = []
    documents = []
    metadatas = []

    for chunk in chunks:
        chunk_id = f"ok_{chunk.source_session}_{uuid.uuid4().hex[:8]}"
        ids.append(chunk_id)
        documents.append(chunk.content)
        metadatas.append({
            "chunk_type": chunk.chunk_type,
            "objects": json.dumps(chunk.objects),
            "confidence": float(chunk.confidence),
            "source_session": chunk.source_session,
        })

    _org_collection.add(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("Added %d chunks to org_knowledge collection", len(chunks))
    return len(chunks)


def query_org_knowledge(
    text: str,
    n_results: int = 5,
    chunk_types: list[str] | None = None,
) -> list[dict]:
    """
    Similarity search against the org_knowledge collection.

    Args:
        text:        query string
        n_results:   number of top chunks to return
        chunk_types: optional filter — only return chunks of these types
                     e.g. ["field_correction", "schema_note"]

    Returns list of dicts: text, chunk_type, objects, confidence, distance, collection
    """
    if _org_collection is None:
        raise RuntimeError(
            "Knowledge store not initialized. Call init_knowledge() first."
        )

    count = _org_collection.count()
    if count == 0:
        return []

    where = None
    if chunk_types:
        where = (
            {"chunk_type": chunk_types[0]}
            if len(chunk_types) == 1
            else {"chunk_type": {"$in": chunk_types}}
        )

    results = _org_collection.query(
        query_texts=[text],
        n_results=min(n_results, count),
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    output = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        output.append({
            "text": doc,
            "chunk_type": meta.get("chunk_type", "misc"),
            "objects": json.loads(meta.get("objects", "[]")),
            "confidence": meta.get("confidence", 0.7),
            "distance": round(dist, 4),
            "collection": "org_knowledge",
        })

    logger.debug(
        "Org knowledge query returned %d chunks for: %r", len(output), text[:60]
    )
    return output


def query_all_knowledge(text: str, n_results: int = 5) -> list[dict]:
    """
    Search both rules (asoka_knowledge) and org_knowledge collections,
    merge results, and sort by distance (most relevant first).

    Used by knowledge/synthesizer.py for conflict detection — needs to check
    both static rules and previously learned org knowledge.

    Returns dicts with: text, distance, collection (plus category or chunk_type).
    """
    rules_results = []
    try:
        for r in query(text, n_results=n_results):
            r["collection"] = "rules"
            rules_results.append(r)
    except Exception as exc:
        logger.warning("query_all_knowledge: rules query failed: %s", exc)

    org_results = []
    try:
        org_results = query_org_knowledge(text, n_results=n_results)
    except Exception as exc:
        logger.warning("query_all_knowledge: org query failed: %s", exc)

    merged = rules_results + org_results
    merged.sort(key=lambda r: r.get("distance", 1.0))
    return merged[:n_results]


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_rules_md(md_path: str) -> list[dict]:
    """
    Split rules.md into chunks at top-level section headers (# Header).

    Each chunk becomes one document in ChromaDB. The section header
    is stored as the "category" metadata field.

    Strategy:
      - Split on lines starting with "# " (top-level headers only)
      - The header line becomes the category name
      - Everything between two headers is one chunk
      - Empty chunks (headers with no content) are skipped

    Returns:
        List of dicts: {"id": str, "text": str, "category": str}
    """
    path = Path(md_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Knowledge file not found: {path.resolve()}. "
            f"Ensure knowledge/rules.md exists."
        )

    content = path.read_text(encoding="utf-8")

    # Split on top-level headers — lines that start with exactly "# "
    # (not "## " which would be sub-headers)
    sections = re.split(r"(?m)^(?=# )", content)

    chunks = []
    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue

        lines = section.splitlines()
        # First line is the header — strip the "# " prefix for the category name
        header_line = lines[0]
        category = header_line.lstrip("#").strip()

        # Body is everything after the header line
        body = "\n".join(lines[1:]).strip()
        if not body:
            continue  # skip empty sections

        # Full text includes the header so the chunk is self-contained
        # (the embedding captures both the topic and the content)
        full_text = f"{category}\n\n{body}"

        chunks.append({
            "id": f"chunk_{i:03d}_{category.lower().replace(' ', '_')}",
            "text": full_text,
            "category": category,
        })

    logger.debug("Parsed %d chunks from %s", len(chunks), md_path)
    return chunks


def _embed_chunks(chunks: list[dict]) -> None:
    """
    Add all chunks to the ChromaDB collection in one batch call.

    ChromaDB's add() accepts lists of ids, documents, and metadatas.
    The embedding function set on the collection automatically converts
    each document string into a vector — we don't call the model directly.
    """
    _collection.add(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[{"category": c["category"]} for c in chunks],
    )
