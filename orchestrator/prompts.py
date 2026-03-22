"""
orchestrator/prompts.py

Responsibilities:
  - Define the system prompt used in every Claude call
  - Build user-turn prompt strings for intent classification, read answering,
    and write plan generation

No Claude API calls happen here. This file is pure string construction.
Keeping all prompt text in one place means wording, constraints, and output
format can be tuned without touching the call logic in intent/reader/planner.

Input:  runtime values (user message, context block, records block)
Output: formatted strings passed as the user turn in messages.create()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.session import ConversationContext


# ─────────────────────────────────────────────────────────────────────────────
# System prompt — injected into every Claude call via the system= parameter
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are Asoka, an internal Salesforce CRM assistant for a B2B SaaS company.
You help operations and sales teams read data from and make controlled changes to Salesforce.

Core principles:
- You are precise, factual, and concise. You never invent record IDs or field values.
- You always follow the business rules and schema constraints provided to you.
- You never write to deprecated fields (marked DEPRECATED in the schema).
- You never propose deletion of Account records.
- You always state your assumptions explicitly.
- If you are uncertain or missing information, you say so rather than guessing.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Intent classification prompt
# ─────────────────────────────────────────────────────────────────────────────

def build_intent_prompt(
    message: str,
    schema_snapshot: str,
    context: "ConversationContext | None" = None,
) -> str:
    """
    User-turn prompt for intent classification.

    Claude reads the user's raw message and returns structured JSON identifying:
      - intent_type: READ (questions/lookups) or WRITE (creates/updates/changes)
      - objects: which CRM objects are directly involved
      - record_hints: company/person names to search for in the CRM
      - field_hints: field API names mentioned or implied in the message
      - summary: what the user wants in one sentence

    schema_snapshot: formatted string showing all objects with their key fields,
        built at runtime from SQLite — shows labels, types, picklist values,
        and custom field markers so Claude can link colloquial terms to API names.

    context: optional ConversationContext from earlier turns in the conversation —
        injected as a hint block so corrections don't lose accumulated signal.

    Output: JSON only, no preamble.
    """
    context_section = ""
    if context and not context.is_empty():
        lines = ["Previously in this conversation:"]
        if context.objects:
            lines.append(f"  Objects referenced: {', '.join(context.objects)}")
        if context.record_hints:
            lines.append(f"  Records referenced: {', '.join(context.record_hints)}")
        if context.field_hints:
            for obj, flds in context.field_hints.items():
                lines.append(f"  Fields on {obj}: {', '.join(flds)}")
        lines.append("Use this as additional context when classifying the new message.")
        context_section = "\n".join(lines) + "\n\n"

    return f"""\
Classify the following user message for a Salesforce CRM assistant.

--- CRM schema (objects and their key fields) ---
{schema_snapshot}

{context_section}--- User message ---
\"\"\"{message}\"\"\"

Respond with JSON only — no explanation, no preamble, no code fences:
{{
  "intent_type": "READ" or "WRITE" or "MIXED" or "UNKNOWN",
  "objects": ["<object API names from the schema above that are directly involved>"],
  "record_hints": ["<company names, person names, record identifiers mentioned>"],
  "field_hints": ["<field API names mentioned or implied — use the schema above to resolve colloquial terms>"],
  "summary": "<one sentence: exactly what the user wants to do>"
}}

Guidelines:
- READ:    questions, lookups, requests to show or explain data
- WRITE:   create, update, change, set, deactivate, transfer, modify
- MIXED:   message asks for BOTH a read AND a write in the same request
- UNKNOWN: the request has nothing to do with CRM data, or is too ambiguous to classify
- objects: only names that appear in the schema above
- record_hints: proper nouns that identify specific records (e.g. "TechGlobal", "John Smith")
- field_hints: if the user says "discount", resolve to "Discount_Percent__c"; if "stage", to "StageName"
"""


# ─────────────────────────────────────────────────────────────────────────────
# Reader prompt
# ─────────────────────────────────────────────────────────────────────────────

def build_reader_prompt(
    message: str,
    context_block: str,
    records_block: str,
) -> str:
    """
    User-turn prompt for answering a READ request.

    Claude receives the schema context (fields, relationships, policies) and
    live Salesforce records fetched ahead of time. It answers directly from
    what is provided. If the answer requires data not present, it says what
    is missing rather than guessing.
    """
    records_section = records_block.strip() if records_block else "No records fetched."

    return f"""\
Answer the user's Salesforce question using the context and records provided below.
Be concise and factual. Reference field names and values explicitly when relevant.
If the records section does not contain what you need to fully answer, say what is missing.

--- User question ---
{message}

--- Schema context & business rules ---
{context_block}

--- Live Salesforce records ---
{records_section}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Planner prompt
# ─────────────────────────────────────────────────────────────────────────────

def build_planner_prompt(
    message: str,
    context_block: str,
    records_block: str,
    knowledge_block: str = "",
) -> str:
    """
    User-turn prompt for generating a structured write plan.

    Claude receives schema context (including validation rules and business
    policies), live Salesforce records with their real IDs, and the user's
    write request. It returns a JSON plan listing exact Salesforce operations.

    Critical constraints encoded in the prompt:
    - Only use record IDs from the records block — never invent IDs
    - Flag missing records in pre_flight_issues rather than guessing IDs
    - Every operation must have a reason
    - Respect all validation rules and policy rules from the context block

    knowledge_block: optional session knowledge injected before the records
    section — field corrections prevent wrong API names in payloads, and
    validation rules already observed surface as known constraints.

    Output: JSON only.
    """
    records_section = records_block.strip() if records_block else "No records found."

    knowledge_section = ""
    if knowledge_block:
        knowledge_section = f"\n{knowledge_block}\n"

    return f"""\
Generate a Salesforce write plan for the request below.

--- User request ---
{message}

--- Schema context, validation rules & business rules ---
{context_block}
{knowledge_section}
--- Live Salesforce records (use these IDs in your plan — do not invent IDs) ---
{records_section}

Rules you MUST follow:
1. Only write to editable fields. Never write to read-only or system-managed fields.
2. Never write to deprecated fields (marked DEPRECATED in the schema).
3. If an operation would trigger a validation rule, list it in risks.
4. Never set Discount_Percent__c above 0.30.
5. Only use record IDs from the records section above. Never invent record IDs.
6. If a needed record is not in the records section, set record_id to null and add to pre_flight_issues.
7. For User deactivation: follow the 4-step procedure from the business rules exactly.
8. For Account Type changes: only allow forward transitions (Prospect > Customer > Churned).

Respond with JSON only — no explanation, no preamble, no code fences:
{{
  "summary": "<one sentence describing what this plan does>",
  "operations": [
    {{
      "order": 1,
      "object": "<SF object API name>",
      "method": "create" or "update",
      "record_id": "<18-char SF ID, or null for creates>",
      "payload": {{"<field_api_name>": "<value>"}},
      "reason": "<why this operation is needed>"
    }}
  ],
  "assumptions": ["<things assumed that are not explicit in the request>"],
  "risks": ["<validation rules or policy constraints that could affect this plan>"],
  "pre_flight_issues": ["<blockers: missing records, permission gaps, rule violations that prevent execution>"]
}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Parallel read — query plan prompt
# ─────────────────────────────────────────────────────────────────────────────

def build_read_query_plan_prompt(
    message: str,
    context_block: str,
    records_block: str,
    field_hints: list[str] | None = None,
    knowledge_block: str = "",
) -> str:
    """
    Ask Claude whether the current records are sufficient to answer the question,
    or whether additional parallel Salesforce queries are needed.

    Claude returns JSON with one of two shapes:

    Sufficient (can answer now):
      { "status": "sufficient" }

    Need more data — three query types are supported:
      {
        "status": "need_more",
        "queries": [
          // Type 1: simple SELECT (most common)
          {
            "type": "simple",
            "object": "<SF object API name>",
            "fields": ["<field_api_name>", ...],
            "where": "<SOQL WHERE body or null>",
            "order_by": [{"field": "<field>", "direction": "ASC"|"DESC"}],  // optional
            "limit": <integer, max 20>
          },
          // Type 2: aggregate (SUM/COUNT/AVG/MAX/MIN/COUNT_DISTINCT)
          {
            "type": "aggregate",
            "object": "<SF object API name>",
            "aggregate": "SUM"|"COUNT"|"AVG"|"MAX"|"MIN"|"COUNT_DISTINCT",
            "field": "<field_api_name to aggregate>",
            "group_by": ["<field_api_name>", ...],  // optional
            "where": "<SOQL WHERE body or null>",
            "limit": <integer, max 20>
          },
          // Type 3: raw SOQL (cross-object, subqueries, date literals, relationship traversal)
          {
            "type": "soql",
            "soql": "<complete valid SOQL statement>"
          }
        ]
      }

    All queries in the list are run in parallel. The results block includes
    both successful records and any errors (validation or Salesforce API) —
    if a query failed, read the error message and fix the query in the next response.
    """
    records_section = records_block.strip() if records_block else ""

    focus_section = ""
    if field_hints:
        focus_section = (
            f"\n--- Focus fields (user specifically mentioned these — prioritize them) ---\n"
            f"{', '.join(field_hints)}\n"
        )

    knowledge_section = ""
    if knowledge_block:
        knowledge_section = f"\n{knowledge_block}\n"

    no_data_yet = not records_section

    records_display = records_section if records_section else "(no Salesforce data fetched yet)"

    return f"""\
You are a Salesforce query planner. Your job is to fetch live data from Salesforce so that
the user's question can be answered from real records — not from your training knowledge.

--- User question ---
{message}
{focus_section}{knowledge_section}
--- Schema context ---
{context_block}

--- Records / query results so far ---
{records_display}

Decide:
{"IMPORTANT: No Salesforce data has been fetched yet. You MUST respond with need_more and request queries. Do NOT return sufficient — you cannot answer a question about live CRM data without first fetching it." if no_data_yet else ""}
- Respond with {{"status": "sufficient"}} ONLY if the records above already contain the specific data needed to fully answer the question.
- Otherwise respond with need_more and the queries required.

Query types:
  - "simple": standard filtered SELECT with optional ORDER BY
  - "aggregate": SUM/COUNT/AVG/MAX/MIN grouped by a field
  - "soql": raw SOQL for relationship traversal, subqueries, date literals (TODAY, THIS_YEAR, LAST_N_DAYS:30), or anything the other types can't express

Rules:
- For "simple" and "aggregate": only use field names from the schema context above.
- For "soql": write valid SOQL; errors will be returned so you can fix them next iteration.
- Check results for VALIDATION_ERROR or EXECUTION_ERROR entries — fix those queries.
- Do not repeat a query that already returned data.
- Maximum 5 queries per response.
- Respond with JSON only — no explanation, no preamble, no code fences.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Parallel read — synthesis prompt
# ─────────────────────────────────────────────────────────────────────────────

def build_read_synthesis_prompt(
    message: str,
    context_block: str,
    records_block: str,
    field_hints: list[str] | None = None,
) -> str:
    """
    Ask Claude to synthesize a final plain-text answer from all gathered records.

    Called after the query plan loop completes (either Claude signalled
    "sufficient" or MAX_READ_ITERATIONS was reached).
    """
    records_section = records_block.strip() if records_block else "No records found."

    focus_section = ""
    if field_hints:
        focus_section = (
            f"\n--- Focus fields (user specifically asked about these) ---\n"
            f"{', '.join(field_hints)}\n"
        )

    return f"""\
Answer the user's Salesforce question using ALL records provided below.
Be concise and factual. Reference field names and values explicitly.
If a piece of information is genuinely missing from the records, say so — do not guess.
Ignore any VALIDATION_ERROR or EXECUTION_ERROR entries in the records — those were failed queries.

--- User question ---
{message}
{focus_section}
--- Schema context & business rules ---
{context_block}

--- All Salesforce records gathered ---
{records_section}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Conversation follow-up prompt (idle state with history)
# ─────────────────────────────────────────────────────────────────────────────

def build_conversation_followup_prompt(message: str, history_block: str) -> str:
    """
    Answer a conversational follow-up using accumulated history and SF data.

    Called when the user sends a message in idle state that cannot be
    classified as a new READ or WRITE intent — i.e. they are asking about,
    clarifying, or reasoning over results already fetched.

    Claude is given the full conversation history and all previously
    fetched Salesforce records to work from.
    """
    return f"""\
You are continuing an ongoing Salesforce CRM conversation.
Use the conversation history and previously fetched data below to answer the user's message.
Be concise and factual. If the question genuinely requires fresh Salesforce data that is not
in the history, say so explicitly — do not guess.

{history_block}

--- New message ---
{message}
"""
