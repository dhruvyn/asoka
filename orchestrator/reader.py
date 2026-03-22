"""
orchestrator/reader.py

Multi-step parallel READ handler with typed query dispatch and error feedback.

Flow:
  1. Build full schema context once (structural + semantic).
  2. Run the query-plan loop (up to MAX_READ_ITERATIONS):
       a. Ask Claude: "sufficient?" or "need_more + queries"
       b. If need_more: dispatch each query by type in parallel, collect results AND errors
       c. Feed the unified results block (records + error messages) back to Claude
       d. Claude can fix bad queries on the next iteration
  3. When Claude says "sufficient" (or cap is hit): synthesis call → final answer.

Three query types Claude can request:
  simple    — SELECT with optional ORDER BY; fields validated against SQLite
  aggregate — SUM/COUNT/AVG/MAX/MIN/COUNT_DISTINCT with optional GROUP BY; validated
  soql      — raw SOQL passthrough; executed directly, SF errors captured verbatim

Error feedback:
  Each query result is tagged [OK], [VALIDATION_ERROR], or [EXECUTION_ERROR].
  Failed queries include the error message so Claude can self-correct next iteration.

Limits:
  MAX_READ_ITERATIONS  = 4  — allows 1 correction cycle per typical 3-query flow
  MAX_PARALLEL_QUERIES = 5  — queries per iteration, run concurrently
  MAX_QUERY_LIMIT      = 20 — records per individual query
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import anthropic

from config import cfg
from context.retriever import build_context
from context.structural import get_object_fields
from orchestrator.intent import IntentResult
from orchestrator.prompts import (
    SYSTEM_PROMPT,
    build_read_query_plan_prompt,
    build_read_synthesis_prompt,
)
from orchestrator.session import (
    SessionKnowledge,
    FieldCorrectionEntry,
    RelationshipCorrectionEntry,
    ErrorRecord,
)
from salesforce.query import find_records, soql as run_soql

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
_MODEL = "claude-sonnet-4-6"

_MAX_READ_ITERATIONS = 4
_MAX_PARALLEL_QUERIES = 5
_MAX_QUERY_LIMIT = 20

# Aggregate functions supported for the "aggregate" query type.
_VALID_AGGREGATES = {"COUNT", "SUM", "AVG", "MAX", "MIN", "COUNT_DISTINCT"}

# Numeric data types that support SUM/AVG/MAX/MIN.
_NUMERIC_TYPES = {"currency", "double", "integer", "percent"}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass as _dataclass

@_dataclass
class ReadResult:
    """
    Return value of handle_read().

    answer:           plain-text answer ready for Slack
    full_records:     complete accumulated_records including all query results
                      and error lines — used by core.py for:
                        1. Filtered storage into history (OK sections only)
                        2. SessionKnowledge extraction
    session_knowledge: corrections and metadata extracted from this READ
    """
    answer: str
    full_records: str
    session_knowledge: "SessionKnowledge"


def handle_read(
    intent: IntentResult,
    records_block: str,
    knowledge_block: str = "",
) -> ReadResult:
    """
    Answer a READ request, launching parallel Salesforce queries if needed.

    Args:
        intent:          classified READ intent from classify_intent()
        records_block:   pre-fetched SF records string from core._lookup_records()
        knowledge_block: session knowledge rendered for prompt injection —
                         field corrections and relationship names prevent
                         wasted error iterations on known bad field names

    Returns:
        ReadResult with answer, full accumulated_records, and extracted
        SessionKnowledge (field corrections, relationship corrections,
        error records, metadata facts).
    """
    logger.info(
        "Handling READ | objects=%s | user=%s",
        intent.objects, intent.user_id,
    )

    # Build schema context once — reused across all iterations and the synthesis
    bundle = build_context(
        objects=intent.objects,
        query=intent.raw_message,
        include_validation_rules=False,
        stage="read",
    )
    context_block = bundle.to_prompt_block()

    accumulated_records = records_block  # grows with each iteration
    session_knowledge = SessionKnowledge()

    # ── Query-plan loop ───────────────────────────────────────────────────────
    for iteration in range(_MAX_READ_ITERATIONS):
        logger.info(
            "READ query-plan iteration %d/%d | accumulated_len=%d",
            iteration + 1, _MAX_READ_ITERATIONS, len(accumulated_records),
        )

        plan_prompt = build_read_query_plan_prompt(
            message=intent.raw_message,
            context_block=context_block,
            records_block=accumulated_records,
            field_hints=intent.field_hints,
            knowledge_block=knowledge_block,
        )
        plan_response = _client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": plan_prompt}],
        )
        plan_text = plan_response.content[0].text.strip()
        logger.debug("Query plan response (iter %d): %s", iteration + 1, plan_text[:400])

        plan_data = _extract_json_safe(plan_text)
        status = plan_data.get("status", "sufficient")

        # Safety guard: never skip querying on the first iteration when we have
        # no accumulated data. Claude sometimes says "sufficient" from training
        # knowledge when the records block is empty — that is always wrong.
        if status != "need_more" and not accumulated_records and iteration == 0:
            logger.warning(
                "Claude returned 'sufficient' on iter 1 with no data — forcing need_more"
            )
            # Build a minimal query: fetch up to 10 records for each object
            plan_data = {
                "status": "need_more",
                "queries": [
                    {
                        "type": "simple",
                        "object": obj,
                        "fields": ["Id", "Name"],
                        "where": None,
                        "limit": 10,
                    }
                    for obj in intent.objects
                ],
            }
            status = "need_more"

        if status != "need_more" or not plan_data.get("queries"):
            logger.info("Claude signals sufficient data — proceeding to synthesis")
            break

        # Dispatch all requested queries in parallel with full feedback
        queries = plan_data["queries"][:_MAX_PARALLEL_QUERIES]
        logger.info("Launching %d parallel queries (typed)", len(queries))
        results_block = _run_parallel_queries_with_feedback(queries)

        if results_block:
            accumulated_records = (accumulated_records + "\n\n" + results_block).strip()
            logger.info("Query batch produced %d chars", len(results_block))

            # Extract knowledge from this iteration's errors — gated on errors present
            if "VALIDATION_ERROR" in results_block or "EXECUTION_ERROR" in results_block:
                extracted = _extract_from_errors(results_block, intent.raw_message)
                session_knowledge.merge(extracted)
                logger.info(
                    "Extracted knowledge | fc=%d rc=%d eq=%d",
                    len(extracted.field_corrections),
                    len(extracted.relationship_corrections),
                    len(extracted.error_queries),
                )
        else:
            logger.info("Query batch returned nothing — stopping early")
            break

    # ── Extract metadata from OK results (gated on non-trivial data) ──────────
    if len(accumulated_records) > 500:
        meta_facts = _extract_metadata_from_ok(accumulated_records)
        for fact in meta_facts:
            if fact not in session_knowledge.metadata:
                session_knowledge.metadata.append(fact)

    # ── Synthesis call ────────────────────────────────────────────────────────
    synthesis_prompt = build_read_synthesis_prompt(
        message=intent.raw_message,
        context_block=context_block,
        records_block=accumulated_records,
        field_hints=intent.field_hints,
    )
    synthesis_response = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": synthesis_prompt}],
    )
    answer = synthesis_response.content[0].text.strip()

    logger.info(
        "READ answered | user=%s | answer_len=%d chars | knowledge_empty=%s",
        intent.user_id, len(answer), session_knowledge.is_empty(),
    )
    return ReadResult(
        answer=answer,
        full_records=accumulated_records,
        session_knowledge=session_knowledge,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Typed query dispatch
# ─────────────────────────────────────────────────────────────────────────────

def _run_parallel_queries_with_feedback(query_specs: list[dict]) -> str:
    """
    Execute all query specs concurrently, returning a unified results block
    that includes both successful records and error messages.

    Each result is labelled [OK], [VALIDATION_ERROR], or [EXECUTION_ERROR]
    so Claude can diagnose and fix failures on the next iteration.
    """
    result_sections: list[str] = []

    def _dispatch(spec: dict) -> str:
        q_type = spec.get("type", "simple").lower()
        if q_type == "aggregate":
            return _run_aggregate(spec)
        elif q_type == "soql":
            return _run_soql(spec)
        else:
            return _run_simple(spec)

    with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_QUERIES) as executor:
        futures = {executor.submit(_dispatch, spec): spec for spec in query_specs}
        for future in as_completed(futures):
            section = future.result()
            if section:
                result_sections.append(section)

    if not result_sections:
        return ""

    return "=== Query Results ===\n\n" + "\n\n".join(result_sections)


def _run_simple(spec: dict) -> str:
    """
    Execute a structured SELECT query with optional ORDER BY.
    Validates field names against SQLite before touching Salesforce.
    """
    obj = spec.get("object", "")
    raw_fields = spec.get("fields") or ["Id", "Name"]
    fields = raw_fields if "Id" in raw_fields else ["Id"] + raw_fields
    where: Optional[str] = spec.get("where") or None
    limit = min(int(spec.get("limit", 10)), _MAX_QUERY_LIMIT)
    raw_order_by = spec.get("order_by") or []
    # Normalize: Claude sometimes returns order_by as strings ("Name ASC") instead
    # of dicts ({"field": "Name", "direction": "ASC"}).
    order_by_specs: list[dict] = []
    for ob in raw_order_by:
        if isinstance(ob, dict):
            order_by_specs.append(ob)
        elif isinstance(ob, str):
            parts = ob.strip().split()
            direction = parts[1].upper() if len(parts) > 1 else "ASC"
            order_by_specs.append({"field": parts[0], "direction": direction})

    label = f"[simple] {obj}" + (f" WHERE {where}" if where else "")
    if order_by_specs:
        label += f" ORDER BY {order_by_specs[0].get('field', '?')}"

    # Validate fields against SQLite before hitting SF
    errors = _validate_simple_query(obj, fields, order_by_specs)
    if errors:
        error_text = "\n  ".join(errors)
        logger.warning("simple query validation failed | obj=%s | errors=%s", obj, errors)
        return f"{label} — VALIDATION_ERROR\n  {error_text}"

    # Build ORDER BY string
    order_by_str: Optional[str] = None
    if order_by_specs:
        parts = []
        for ob in order_by_specs:
            direction = ob.get("direction", "ASC").upper()
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            parts.append(f"{ob['field']} {direction}")
        order_by_str = ", ".join(parts)

    try:
        records = find_records(
            obj, fields=fields, where=where, limit=limit, order_by=order_by_str
        )
        if not records:
            return f"{label} — OK (0 records)"

        lines = [f"{label} — OK"]
        for r in records:
            lines.append(f"  {obj}:")
            for k, v in r.items():
                if v is not None:
                    lines.append(f"    {k}: {v}")
        return "\n".join(lines)

    except Exception as exc:
        logger.warning("simple query execution failed | obj=%s | error=%s", obj, exc)
        return f"{label} — EXECUTION_ERROR\n  {exc}"


def _run_aggregate(spec: dict) -> str:
    """
    Execute an aggregate query (SUM/COUNT/AVG/MAX/MIN/COUNT_DISTINCT).
    Validates the aggregate function and field type before hitting SF.
    """
    obj = spec.get("object", "")
    agg_func = spec.get("aggregate", "COUNT").upper()
    agg_field = spec.get("field", "Id")
    group_by: list[str] = spec.get("group_by") or []
    where: Optional[str] = spec.get("where") or None
    limit = min(int(spec.get("limit", 10)), _MAX_QUERY_LIMIT)

    label = f"[aggregate] {agg_func}({agg_field}) FROM {obj}"
    if group_by:
        label += f" GROUP BY {', '.join(group_by)}"
    if where:
        label += f" WHERE {where}"

    errors = _validate_aggregate_query(obj, agg_func, agg_field, group_by)
    if errors:
        error_text = "\n  ".join(errors)
        logger.warning("aggregate query validation failed | obj=%s | errors=%s", obj, errors)
        return f"{label} — VALIDATION_ERROR\n  {error_text}"

    # COUNT_DISTINCT uses SOQL COUNT_DISTINCT() syntax
    soql_func = "COUNT_DISTINCT" if agg_func == "COUNT_DISTINCT" else agg_func
    alias = f"{agg_func.lower()}_{agg_field.lower().replace('__c', '')}"

    select_parts = [f"{soql_func}({agg_field}) {alias}"] + group_by
    parts = [f"SELECT {', '.join(select_parts)}", f"FROM {obj}"]
    if where:
        parts.append(f"WHERE {where}")
    if group_by:
        parts.append(f"GROUP BY {', '.join(group_by)}")
    parts.append(f"LIMIT {limit}")

    query_str = " ".join(parts)
    logger.debug("aggregate SOQL: %s", query_str)

    try:
        records = run_soql(query_str)
        if not records:
            return f"{label} — OK (0 results)"

        lines = [f"{label} — OK"]
        for r in records:
            row_parts = []
            for gb in group_by:
                if gb in r:
                    row_parts.append(f"{gb}: {r[gb]}")
            agg_val = r.get(alias, "N/A")
            row_parts.append(f"{agg_func}({agg_field}): {agg_val}")
            lines.append("  " + ", ".join(row_parts))
        return "\n".join(lines)

    except Exception as exc:
        logger.warning("aggregate query execution failed | obj=%s | error=%s", obj, exc)
        return f"{label} — EXECUTION_ERROR\n  {exc}"


def _run_soql(spec: dict) -> str:
    """
    Execute a raw SOQL string. No pre-validation — SF errors are captured
    verbatim and returned so Claude can self-correct on the next iteration.
    """
    query_str = (spec.get("soql") or "").strip()
    if not query_str:
        return "[soql] — VALIDATION_ERROR\n  Missing 'soql' field in query spec"

    # Truncate label for readability
    label = f"[soql] {query_str[:80]}{'...' if len(query_str) > 80 else ''}"

    try:
        records = run_soql(query_str)
        if not records:
            return f"{label} — OK (0 records)"

        lines = [f"{label} — OK"]
        for r in records:
            lines.append("  Record:")
            _format_soql_record(r, lines, indent=4)
        return "\n".join(lines)

    except Exception as exc:
        logger.warning("soql query execution failed | error=%s | soql=%s", exc, query_str[:100])
        return f"{label} — EXECUTION_ERROR\n  {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _validate_simple_query(
    obj: str,
    fields: list[str],
    order_by_specs: list[dict],
) -> list[str]:
    """
    Check field names against SQLite. Returns a list of error strings (empty = valid).
    """
    all_field_info = get_object_fields(obj)
    known = {f.api_name for f in all_field_info}
    custom = [f.api_name for f in all_field_info if f.is_custom and not f.is_deprecated]
    errors = []

    for f in fields:
        if f not in known:
            hint = f"  Custom fields available: {', '.join(custom[:6])}" if custom else ""
            errors.append(f"Field '{f}' does not exist on {obj}.{hint}")

    for ob in order_by_specs:
        ob_field = ob.get("field", "")
        if ob_field and ob_field not in known:
            errors.append(f"ORDER BY field '{ob_field}' does not exist on {obj}")

    return errors


def _validate_aggregate_query(
    obj: str,
    agg_func: str,
    field: str,
    group_by: list[str],
) -> list[str]:
    """
    Check aggregate function, field, and group_by names. Returns error strings.
    """
    errors = []

    if agg_func not in _VALID_AGGREGATES:
        errors.append(
            f"Unsupported aggregate '{agg_func}'. "
            f"Valid: {', '.join(sorted(_VALID_AGGREGATES))}"
        )

    all_field_info = {f.api_name: f for f in get_object_fields(obj)}
    custom = [n for n, fi in all_field_info.items() if fi.is_custom and not fi.is_deprecated]

    if field not in all_field_info:
        errors.append(
            f"Field '{field}' does not exist on {obj}. "
            f"Custom fields: {', '.join(custom[:6])}"
        )
    elif agg_func in ("SUM", "AVG", "MAX", "MIN"):
        fi = all_field_info[field]
        if fi.data_type not in _NUMERIC_TYPES:
            errors.append(
                f"Field '{field}' is type '{fi.data_type}' — "
                f"{agg_func} requires a numeric field ({', '.join(_NUMERIC_TYPES)})"
            )

    for f in group_by:
        if f not in all_field_info:
            errors.append(f"GROUP BY field '{f}' does not exist on {obj}")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# SessionKnowledge extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_from_errors(results_block: str, raw_message: str) -> "SessionKnowledge":
    """
    Parse VALIDATION_ERROR and EXECUTION_ERROR lines from a query results block
    and return a SessionKnowledge populated with corrections and error records.

    Only called when errors are confirmed present — no wasted iteration otherwise.

    Patterns handled:
      VALIDATION_ERROR — "Field 'X' does not exist on ObjName."
                         followed by "  Custom fields: A, B, C"
      EXECUTION_ERROR  — "No such relation 'X'. Did you mean 'Y'?"
    """
    field_corrections: list[FieldCorrectionEntry] = []
    relationship_corrections: list[RelationshipCorrectionEntry] = []
    error_queries: list[ErrorRecord] = []

    lines = results_block.split("\n")

    current_label: str | None = None
    current_error_type: str | None = None
    current_error_lines: list[str] = []

    def _flush_error_record(correction_applied: str | None) -> None:
        if current_label and current_error_type:
            error_queries.append(ErrorRecord(
                query_label=current_label,
                error_type=current_error_type,
                error_message=" ".join(current_error_lines).strip(),
                correction_applied=correction_applied,
                user_intent=raw_message,
            ))

    for i, line in enumerate(lines):

        # Detect query label lines — e.g. "[simple] Opportunity WHERE ... — VALIDATION_ERROR"
        val_match = re.search(r"^(.+) — (VALIDATION_ERROR|EXECUTION_ERROR)$", line.strip())
        if val_match:
            # Flush previous error record before starting a new one
            _flush_error_record(correction_applied=None)
            current_label = val_match.group(1).strip()
            current_error_type = val_match.group(2)
            current_error_lines = []
            continue

        # Accumulate error message lines (indented lines after the label)
        if current_error_type and line.startswith("  ") and line.strip():
            current_error_lines.append(line.strip())

            # ── VALIDATION_ERROR: field name correction ────────────────────
            # Line: "Field 'Discount__c' does not exist on Opportunity."
            field_match = re.search(
                r"Field '(\w+)' does not exist on (\w+)", line
            )
            if field_match:
                wrong_field = field_match.group(1)
                obj_name = field_match.group(2)
                # Look ahead for the custom fields suggestion on the next line
                candidates: list[str] = []
                if i + 1 < len(lines):
                    sugg_match = re.search(r"Custom fields:\s*(.+)", lines[i + 1])
                    if sugg_match:
                        candidates = [
                            f.strip() for f in sugg_match.group(1).split(",")
                            if f.strip()
                        ]
                error_ctx = line.strip()
                if candidates:
                    error_ctx += "\n  " + lines[i + 1].strip()
                field_corrections.append(FieldCorrectionEntry(
                    wrong=wrong_field,
                    correct=candidates,
                    object_name=obj_name,
                    error_context=error_ctx,
                ))

            # ── EXECUTION_ERROR: relationship name correction ──────────────
            # Line: "No such relation 'Opportunities'. Did you mean 'Opportunities__r'?"
            rel_match = re.search(
                r"No such relation '(\w+)'.*Did you mean '(\w+)'", line
            )
            if rel_match:
                wrong_rel = rel_match.group(1)
                correct_rel = rel_match.group(2)
                # Infer parent object from the current query label
                parent_obj = ""
                if current_label:
                    obj_m = re.search(r"FROM\s+(\w+)", current_label, re.IGNORECASE)
                    if obj_m:
                        parent_obj = obj_m.group(1)
                relationship_corrections.append(RelationshipCorrectionEntry(
                    wrong=wrong_rel,
                    correct=correct_rel,
                    parent_object=parent_obj,
                    error_context=line.strip(),
                ))
                _flush_error_record(correction_applied=correct_rel)
                current_label = None
                current_error_type = None
                current_error_lines = []
                continue

        # Blank line or new section — flush pending error record
        elif current_error_type and not line.strip():
            _flush_error_record(correction_applied=None)
            current_label = None
            current_error_type = None
            current_error_lines = []

    # Flush any trailing error record
    _flush_error_record(correction_applied=None)

    return SessionKnowledge(
        field_corrections=field_corrections,
        relationship_corrections=relationship_corrections,
        validation_rules=[],
        metadata=[],
        error_queries=error_queries,
    )


def _filter_ok_records(full_records: str) -> str:
    """
    Strip VALIDATION_ERROR and EXECUTION_ERROR sections from accumulated_records,
    keeping only OK query results and the initial lookup block.

    Used before storing records into ConversationHistory so future turns
    don't inherit noise from error lines.
    """
    output_lines: list[str] = []
    skip_section = False

    for line in full_records.split("\n"):
        if re.search(r"— (VALIDATION_ERROR|EXECUTION_ERROR)$", line.strip()):
            skip_section = True
            continue
        # A new query label starting with "— OK" ends any skip
        if re.search(r"— OK", line):
            skip_section = False
        if not skip_section:
            output_lines.append(line)

    return "\n".join(output_lines).strip()


def _extract_metadata_from_ok(full_records: str) -> list[str]:
    """
    Pull named record facts from OK query results.

    Looks for blocks like:
        Account:
          Id: 001abc123def456GHI
          Name: TechGlobal Solutions
          ARR__c: 240000

    Returns facts as compact strings:
        "TechGlobal Solutions (Account): Id=001abc123def456GHI, ARR__c=240000"

    Only emits a fact when both Id and Name are present — avoids storing
    anonymous or partial records.
    """
    facts: list[str] = []
    current_obj: str | None = None
    current_fields: dict[str, str] = {}

    def _flush_record() -> None:
        if current_obj and "Id" in current_fields and "Name" in current_fields:
            name = current_fields.pop("Name")
            record_id = current_fields["Id"]
            extra = ", ".join(
                f"{k}={v}" for k, v in current_fields.items()
                if k != "Id" and v
            )
            fact = f"{name} ({current_obj}): Id={record_id}"
            if extra:
                fact += f", {extra}"
            facts.append(fact)

    for line in full_records.split("\n"):
        # Object header: "  Account:" or "Account:"
        obj_match = re.match(r"^\s{0,4}(\w+):$", line)
        if obj_match:
            _flush_record()
            current_obj = obj_match.group(1)
            current_fields = {}
            continue

        # Field line: "    Id: 001abc..." or "  Name: TechGlobal"
        field_match = re.match(r"^\s{2,6}(\w+):\s+(.+)$", line)
        if field_match and current_obj:
            current_fields[field_match.group(1)] = field_match.group(2).strip()

    _flush_record()
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# SOQL record formatter (handles nested relationship results)
# ─────────────────────────────────────────────────────────────────────────────

def _format_soql_record(record: dict, lines: list[str], indent: int = 2) -> None:
    """
    Recursively format a SOQL record dict into lines, handling nested
    relationship results (e.g. `Opportunities` subquery results).
    """
    prefix = " " * indent
    for k, v in record.items():
        if isinstance(v, dict) and "records" in v:
            # Nested subquery result
            lines.append(f"{prefix}{k}:")
            for child in v.get("records", []):
                child_clean = {ck: cv for ck, cv in child.items() if ck != "attributes"}
                lines.append(f"{prefix}  -")
                _format_soql_record(child_clean, lines, indent + 4)
        elif v is not None:
            lines.append(f"{prefix}{k}: {v}")


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json_safe(text: str) -> dict:
    """
    Parse JSON from Claude's query-plan response.
    Returns {"status": "sufficient"} on any parse failure so the loop
    gracefully falls through to the synthesis step rather than crashing.
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

    logger.warning("Could not parse query-plan JSON — defaulting to sufficient")
    return {"status": "sufficient"}
