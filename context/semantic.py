"""
context/semantic.py

Responsibilities:
  - Thin query interface over knowledge/loader.py
  - Given a query string, return the most relevant policy chunks
  - Filter by category when the orchestrator knows which topic it needs
  - Format chunks into a clean string block for Claude prompt injection

This file adds no new ChromaDB logic — it wraps knowledge/loader.query()
with orchestrator-facing helpers that format results cleanly.

Input:  a query string from the orchestrator
Output: list of relevant policy chunks, or a formatted string for prompt injection

Usage:
    from context.semantic import get_relevant_rules, format_rules_for_prompt

    chunks = get_relevant_rules("how do I deactivate a user?", n=3)
    prompt_block = format_rules_for_prompt(chunks)
"""

import logging
from knowledge.loader import query as _chroma_query

logger = logging.getLogger(__name__)


def get_relevant_rules(query_text: str, n: int = 5) -> list[dict]:
    """
    Return the top-n most semantically relevant policy chunks for a query.

    Each returned dict has:
      - "text":     full chunk content (category header + body)
      - "category": section name from rules.md
      - "distance": cosine distance (0 = identical, 2 = opposite)

    Args:
        query_text: the user's message or a distilled key phrase
        n:          number of chunks to retrieve (default 5, max = collection size)

    Lower distance = higher relevance. Chunks with distance > 1.0 are
    semantically dissimilar and filtered out — they add noise to the prompt.
    """
    results = _chroma_query(text=query_text, n_results=n)

    # Filter out low-relevance chunks (cosine distance > 1.0 means less than
    # 0% cosine similarity — the vectors point away from each other)
    relevant = [r for r in results if r["distance"] <= 1.0]

    logger.debug(
        "get_relevant_rules: %d/%d chunks passed distance filter for query: %r",
        len(relevant), len(results), query_text[:60]
    )
    return relevant


def get_rules_by_category(category: str) -> list[dict]:
    """
    Retrieve all chunks that belong to a specific category.

    Used when the orchestrator knows exactly which section it needs —
    e.g., intent classification identified "user deactivation," so we
    fetch the User Deactivation Procedure section directly rather than
    relying on similarity search.

    Note: this uses the category query text as the search string, which
    reliably surfaces that section at the top of results.
    """
    results = _chroma_query(text=category, n_results=10)
    # Filter to exact category match using stored metadata
    return [r for r in results if r["category"].lower() == category.lower()]


def format_rules_for_prompt(chunks: list[dict]) -> str:
    """
    Format a list of retrieved chunks into a clean block for injection
    into a Claude prompt.

    Output format:
        === Business Rules & Policies ===

        [Account Rules]
        Accounts are never deleted...

        [User Deactivation Procedure]
        When deactivating a user...

    Returns an empty string if chunks is empty (no rules section added to prompt).
    """
    if not chunks:
        return ""

    lines = ["=== Business Rules & Policies ===\n"]
    seen_categories = set()

    for chunk in chunks:
        category = chunk["category"]
        text = chunk["text"]

        # Deduplicate — if two chunks from the same category were retrieved,
        # show the category header only once
        if category not in seen_categories:
            seen_categories.add(category)
            lines.append(f"[{category}]")

        # The chunk text already includes the category as its first line
        # (set by _parse_rules_md). Strip it to avoid double-printing.
        body_lines = text.splitlines()
        body = "\n".join(
            line for line in body_lines
            if line.strip().lower() != category.lower()
        ).strip()

        lines.append(body)
        lines.append("")  # blank line between chunks

    return "\n".join(lines).strip()
