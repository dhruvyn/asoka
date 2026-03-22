# Orchestrator Flows

Open in VS Code with a monospace font. All diagrams use plain ASCII.

---

## Diagram 1 — Full Session State Machine + Context Build

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  SESSION STATE  (per user_id, in-memory, thread-safe)                               ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  ┌───────────────────────────────────────────────────────────────────────┐
  │  SessionState                                                         │
  │                                                                       │
  │  state:         "idle" | "proposing" | "plan_shown"                   │
  │  intent:        IntentResult | None           ← cleared on reset      │
  │  plan:          BatchPlan | None              ← cleared on reset      │
  │  records_block: str                           ← cleared on reset      │
  │                                                                       │
  │  ┌───────────────────────────────────────────────────────────────┐   │
  │  │ context: ConversationContext          survives reset_to_idle   │   │
  │  │   objects:      list[str]  max 5, MRU order                   │   │
  │  │   record_hints: list[str]  max 5, MRU order                   │   │
  │  │   field_hints:  dict[obj → list[str]]  max 5 per obj          │   │
  │  └───────────────────────────────────────────────────────────────┘   │
  │                                                                       │
  │  ┌───────────────────────────────────────────────────────────────┐   │
  │  │ history: ConversationHistory          survives reset_to_idle   │   │
  │  │   turns:            list[ConversationTurn]  all messages       │   │
  │  │   accumulated_data: str  filtered OK records from all turns    │   │
  │  │   queries_run:      list[dict]                                 │   │
  │  └───────────────────────────────────────────────────────────────┘   │
  │                                                                       │
  │  ┌───────────────────────────────────────────────────────────────┐   │
  │  │ knowledge: SessionKnowledge           survives reset_to_idle   │   │
  │  │   field_corrections:       list[FieldCorrectionEntry]          │   │
  │  │   relationship_corrections: list[RelationshipCorrectionEntry]  │   │
  │  │   validation_rules:        list[str]                           │   │
  │  │   metadata:                list[str]  named record facts        │   │
  │  │   error_queries:           list[ErrorRecord]  synthesis input   │   │
  │  └───────────────────────────────────────────────────────────────┘   │
  └───────────────────────────────────────────────────────────────────────┘

  reset_to_idle()  →  clears state / intent / plan / records_block
                      KEEPS context + history + knowledge

  clear_session()  →  full wipe  (only /reset command and test teardown)
  end_session()    →  synthesize_session() then clear_session()


═══════════════════════════════════════════════════════════════════════════════════════
  STATE: idle
═══════════════════════════════════════════════════════════════════════════════════════

  User message arrives
         │
         ▼
  classify_intent(message, user_id, context=session.context)
    ├─ builds schema snapshot from SQLite
    │    custom fields first, alphabetical, cap 12/object
    │    noise fields excluded (_SNAPSHOT_NOISE)
    │    picklist values, reference targets, required flags shown
    ├─ injects ConversationContext as hint block if non-empty
    └─ returns IntentResult { intent_type, objects, record_hints,
                               field_hints, summary, raw_message, user_id }
         │
         ▼
  session.context.merge(objects, record_hints, field_hints)   ← every turn
         │
         ├─── UNKNOWN  +  history is empty
         │         └─→ HandleResult("UNKNOWN", "I'm not sure…")
         │
         ├─── UNKNOWN  +  history non-empty
         │         │
         │         └─→ _answer_from_history(message, history)
         │               1 Claude call · no SF queries
         │               history.add_turn(user) + add_turn(assistant)
         │               state stays idle
         │               └─→ HandleResult("CONVERSATION", answer)
         │
         ├─── MIXED
         │         └─→ HandleResult("MIXED", "please split into two messages")
         │
         └─── READ or WRITE
                   │
                   ▼
             history.add_turn("user", message, intent_type)
             update_session(state="proposing", intent=intent)
             └─→ HandleResult("PROPOSAL", proposal_text)


═══════════════════════════════════════════════════════════════════════════════════════
  STATE: proposing  (awaiting confirmation)
═══════════════════════════════════════════════════════════════════════════════════════

  ├─── confirmation
  │         │
  │         ▼
  │    _lookup_records(intent.objects, intent.record_hints)
  │      SELECT Id + Name/Subject + up to 13 other fields
  │      Name/Subject always prepended before field cap
  │         │
  │    combined = history.accumulated_data + "\n\n" + fresh_records
  │    knowledge_block = session.knowledge.to_prompt_block("inject")
  │         │
  │    ─────┤ READ
  │         ▼
  │    handle_read(intent, combined, knowledge_block)
  │         └─→ ReadResult(answer, full_records, session_knowledge)
  │    filtered = _filter_ok_records(full_records)
  │    history.add_data(filtered)
  │    history.add_turn("assistant", answer, "READ_ANSWER")
  │    session.knowledge.merge(session_knowledge)
  │    reset_to_idle()
  │    └─→ HandleResult("READ_ANSWER", answer)
  │
  │    ─────┤ WRITE
  │         ▼
  │    generate_plan(intent, combined, knowledge_block)
  │         └─→ BatchPlan
  │    extract plan.risks + pre_flight_issues
  │         → session.knowledge.validation_rules
  │    update_session(state="plan_shown", plan=plan)
  │    └─→ HandleResult("PLAN_PREVIEW", preview, plan)
  │
  └─── non-confirmation (correction)
            │
            ▼
      reset_to_idle()   ← history + knowledge preserved
      _classify_and_propose(message, user_id)


═══════════════════════════════════════════════════════════════════════════════════════
  STATE: plan_shown  (awaiting forward confirmation)
═══════════════════════════════════════════════════════════════════════════════════════

  ├─── confirmation
  │         │
  │         ▼
  │    history.add_turn("user", "confirm", "CONFIRM")
  │    history.add_turn("assistant", "Forwarded…", "SEND_FOR_APPROVAL")
  │    reset_to_idle()
  │    └─→ HandleResult("SEND_FOR_APPROVAL", text, plan)
  │
  └─── non-confirmation
            ▼
      reset_to_idle()
      _classify_and_propose(message, user_id)


═══════════════════════════════════════════════════════════════════════════════════════
  HandleResult types
═══════════════════════════════════════════════════════════════════════════════════════

  PROPOSAL          proposal shown · must confirm before anything executes
  READ_ANSWER       read complete · answer in text
  PLAN_PREVIEW      write plan built · must confirm to forward
  SEND_FOR_APPROVAL plan forwarded to manager
  CONVERSATION      follow-up answered from history · no new SF fetch
  MIXED             mixed read+write · user asked to split
  UNKNOWN           can't classify and no history to draw from
```

---

## Diagram 2 — READ Flow Detail

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  handle_read(intent, combined_records, knowledge_block)                             ║
║  intent: { objects, raw_message, field_hints, user_id }                            ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  ┌───────────────────────────────────────────────────────────────────┐
  │  build_context(objects, query, include_validation_rules=False)    │
  │                                                                   │
  │  SQLite  →  per object:                                           │
  │    fields (editable, deprecated, read-only)                       │
  │    relationships (lookup / master-detail)                         │
  │    role hierarchy (always included)                               │
  │                                                                   │
  │  ChromaDB  →  top-5 semantic chunks                               │
  │    query = raw user message                                       │
  │    cosine similarity · distance filter <= 1.0                     │
  │                                                                   │
  │  → context_block (str, reused across all iterations)             │
  └───────────────────────────────────────────────────────────────────┘
         │
         ▼
  accumulated_records = combined_records   (history data + fresh lookup)
  session_knowledge   = SessionKnowledge()
         │
  ┌──────┴────────────────────────────────────────────────────────────────────┐
  │  QUERY-PLAN LOOP  (max 4 iterations)                                      │
  │                                                                            │
  │  ┌──────────────────────────────────────────────────────────────────┐     │
  │  │  Claude · build_read_query_plan_prompt(                           │     │
  │  │      message, context_block, accumulated_records,                │     │
  │  │      field_hints, knowledge_block)                               │     │
  │  │                                                                  │     │
  │  │  Sections injected:                                              │     │
  │  │    Focus fields  (if field_hints non-empty)                      │     │
  │  │    Session knowledge  (field/relationship corrections)           │     │
  │  │    Schema context                                                │     │
  │  │    Records / query results so far                                │     │
  │  │                                                                  │     │
  │  │  Response:                                                       │     │
  │  │    { "status": "sufficient" }                                    │     │
  │  │    { "status": "need_more", "queries": [...] }                   │     │
  │  └─────────────────────────────┬────────────────────────────────────┘     │
  │                                │                                           │
  │            ┌───────────────────┴──────────────────┐                       │
  │            │                                      │                       │
  │       "sufficient"                           "need_more"                  │
  │       or no queries                  up to 5 queries dispatched           │
  │            │                                      │                       │
  │            │          ┌───────────────────────────▼─────────────────┐    │
  │            │          │  ThreadPoolExecutor (parallel)               │    │
  │            │          │                                              │    │
  │            │          │  type: "simple"                              │    │
  │            │          │    _validate_simple_query(obj, fields,       │    │
  │            │          │                            order_by_specs)   │    │
  │            │          │    → errors: list[str]  (SQLite check)       │    │
  │            │          │    if errors → VALIDATION_ERROR (no SF call) │    │
  │            │          │    else → find_records(obj, fields, where,   │    │
  │            │          │                        limit, order_by)      │    │
  │            │          │         → OK or EXECUTION_ERROR              │    │
  │            │          │                                              │    │
  │            │          │  type: "aggregate"                           │    │
  │            │          │    _validate_aggregate_query(obj, agg,       │    │
  │            │          │                               field, grp)    │    │
  │            │          │    validates: agg in VALID_AGGREGATES        │    │
  │            │          │              field type numeric for SUM/AVG  │    │
  │            │          │              group_by fields exist           │    │
  │            │          │    if errors → VALIDATION_ERROR              │    │
  │            │          │    else → soql(SELECT AGG(f) alias, grp      │    │
  │            │          │                FROM obj GROUP BY grp         │    │
  │            │          │                WHERE ... LIMIT n)            │    │
  │            │          │         → OK or EXECUTION_ERROR              │    │
  │            │          │                                              │    │
  │            │          │  type: "soql"                                │    │
  │            │          │    raw SOQL passthrough · no pre-validation  │    │
  │            │          │    → soql(query_str)                         │    │
  │            │          │    SF errors captured verbatim               │    │
  │            │          │    _format_soql_record() handles nested      │    │
  │            │          │    relationship subquery results             │    │
  │            │          │         → OK or EXECUTION_ERROR              │    │
  │            │          └───────────────────────────┬─────────────────┘    │
  │            │                                      │                       │
  │            │          results_block = "=== Query Results ===\n\n          │
  │            │                          [type] label — OK\n  records...\n   │
  │            │                          [type] label — VALIDATION_ERROR\n   │
  │            │                            Field 'X' not on Obj. Custom: Y   │
  │            │                          [type] label — EXECUTION_ERROR\n    │
  │            │                            No such relation 'X'. Try 'X__r'" │
  │            │                                      │                       │
  │            │          if errors present in results_block:                 │
  │            │            _extract_from_errors(results_block, raw_message)  │
  │            │            → session_knowledge.merge(extracted)              │
  │            │                                      │                       │
  │            │          accumulated_records += results_block                │
  │            │          loop back ─────────────────────────────────────────►│
  │            │                                                              │
  └────────────┴──────────────────────────────────────────────────────────────┘
         │
         ▼  (loop exited: sufficient or cap hit)
  if len(accumulated_records) > 500:
    _extract_metadata_from_ok(accumulated_records)
    → session_knowledge.metadata  (named records with Id + key fields)
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Claude · build_read_synthesis_prompt(                               │
  │      message, context_block, accumulated_records, field_hints)       │
  │                                                                      │
  │  "Ignore VALIDATION_ERROR/EXECUTION_ERROR entries"                   │
  │  "Answer from ALL records · say what is missing"                     │
  └───────────────────────────────────────────────────────────────────────┘
         │
         ▼
  return ReadResult(
    answer          = synthesis response text
    full_records    = accumulated_records  (with errors, unfiltered)
    session_knowledge = all corrections + metadata discovered
  )

  ─────────────────────────────────────────────────────────────────
  Claude API calls per READ:
    1   query-plan call per iteration  (typical: 1, max: 4)
    1   synthesis call
    ─────────────────────────────────────────────────────────────────
    Total: 2 typical · up to 5 with error-correction iterations
  ─────────────────────────────────────────────────────────────────
```

---

## Diagram 3 — WRITE Flow Detail

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  WRITE path through the state machine                                               ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  User: "Set discount to 15% on TechGlobal renewal"
         │
         ▼  [state: idle]
  classify_intent()  →  IntentResult(WRITE, ["Opportunity"], ["TechGlobal"],
                                     ["Discount_Percent__c"], summary)
  context.merge(objects, hints, field_hints)
  history.add_turn("user", message, "WRITE")
  update_session(state="proposing", intent=intent)
  └─→ HandleResult("PROPOSAL",
        "I understand you want to update Opportunity (TechGlobal).
         Summary: _Set Discount_Percent__c to 15% on TechGlobal's renewal._
         Reply yes to proceed.")


  User: "yes"
         │
         ▼  [state: proposing + confirmation detected]
  _execute_confirmed(session, user_id)
         │
  ┌──────┴───────────────────────────────────────────────────────────────────┐
  │  _lookup_records(["Opportunity"], ["TechGlobal"])                        │
  │                                                                          │
  │    for each object × hint:                                               │
  │      fields = [Id] + [Name or Subject] + [up to 13 others]              │
  │      find_records(obj, fields, WHERE Name LIKE '%TechGlobal%', limit=5)  │
  │      → SF SOQL SELECT                                                    │
  │                                                                          │
  │  fresh_records = "=== Records Found ===\n                                │
  │    Opportunity:\n      Id: 006xyz...\n      Name: TechGlobal Renewal\n   │
  │    ..."                                                                  │
  └──────┬───────────────────────────────────────────────────────────────────┘
         │
  combined_records = history.accumulated_data + "\n\n" + fresh_records
         │
  knowledge_block = session.knowledge.to_prompt_block("inject")
  ┌──────────────────────────────────────────────────────────────────────┐
  │  === Learned from this session ===                                   │
  │  Field corrections:                                                  │
  │    Discount__c → Discount_Percent__c, ARR__c, Tier__c (Opportunity)  │
  │  Validation rules observed:                                          │
  │    - Discount_Percent__c cap is 0.30 on Opportunity (VR_Cap)         │
  └──────────────────────────────────────────────────────────────────────┘
         │
         ▼
  generate_plan(intent, combined_records, knowledge_block)
         │
  ┌──────┴───────────────────────────────────────────────────────────────────┐
  │  build_context(objects, query, include_validation_rules=True)            │
  │                                                                          │
  │  SQLite  →  fields, relationships, VALIDATION RULES (write needs these)  │
  │  ChromaDB  →  top-5 policy chunks matching the request                   │
  │  → context_block                                                         │
  │                                                                          │
  │  build_planner_prompt(message, context_block, records, knowledge_block)  │
  │                                                                          │
  │  Hard constraints encoded in prompt:                                     │
  │    1. Only editable fields · never deprecated fields                     │
  │    2. Never invent record IDs · use only IDs from records section        │
  │    3. Missing record → record_id=null + add to pre_flight_issues         │
  │    4. Discount_Percent__c cap 0.30                                       │
  │    5. Account Type transitions forward-only                              │
  │    6. User deactivation: exact 4-step procedure                          │
  │                                                                          │
  │  Claude → JSON plan                                                      │
  │  _parse_plan() → BatchPlan                                               │
  └──────┬───────────────────────────────────────────────────────────────────┘
         │
  BatchPlan = {
    summary:           "Set Discount_Percent__c to 0.15 on TechGlobal Renewal"
    operations: [
      Operation(order=1, object="Opportunity", method="update",
                record_id="006xyz...", payload={"Discount_Percent__c": 0.15},
                reason="User requested 15% discount…")
    ]
    assumptions:       ["TechGlobal has one open opportunity"]
    risks:             ["VR_Cap fires if value exceeds 0.30"]
    pre_flight_issues: []   ← empty means plan is not blocked
  }
         │
  Extract into session.knowledge:
    plan.risks             → session.knowledge.validation_rules
    plan.pre_flight_issues → session.knowledge.validation_rules
         │
  update_session(state="plan_shown", plan=plan)
  └─→ HandleResult("PLAN_PREVIEW",
        "*Plan:* Set Discount_Percent__c to 0.15…
         *Operations (1):*
           1. UPDATE Opportunity 006xyz...
              Discount_Percent__c = 0.15
         *Risks:* VR_Cap fires if > 0.30
         Reply yes to forward to manager.")


  User: "yes"
         │
         ▼  [state: plan_shown + confirmation detected]
  history.add_turn("user", "yes", "CONFIRM")
  history.add_turn("assistant", "Forwarded…", "SEND_FOR_APPROVAL")
  reset_to_idle()   ← knowledge preserved for follow-up questions
  └─→ HandleResult("SEND_FOR_APPROVAL", text, plan=plan)


  ─────────────────────────────────────────────────────────────────
  Claude API calls per WRITE:
    1   classify_intent
    1   generate_plan
    ─────────────────────────────────────────────────────────────────
    Total: 2 Claude calls · 1 SF lookup query
  ─────────────────────────────────────────────────────────────────
```

---

## Diagram 4 — Error Accumulation Across Flows

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  How errors become knowledge — from raw results to persistent OrgKnowledge          ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  ┌───────────────────────────────────────────────────────────────────────────────┐
  │  SOURCE 1: VALIDATION_ERROR in reader iteration                               │
  │                                                                               │
  │  Raw results block line:                                                      │
  │    "[simple] Opportunity WHERE Discount__c > 0.15 — VALIDATION_ERROR"         │
  │    "  Field 'Discount__c' does not exist on Opportunity."                     │
  │    "  Custom fields: Discount_Percent__c, ARR__c, Tier__c"                    │
  │                                                                               │
  │  _extract_from_errors(results_block, raw_message):                            │
  │    regex: Field '(\w+)' does not exist on (\w+)                               │
  │    regex: Custom fields: (.+)                                                 │
  │                                                                               │
  │    → FieldCorrectionEntry(                                                    │
  │        wrong        = "Discount__c"                                           │
  │        correct      = ["Discount_Percent__c", "ARR__c", "Tier__c"]  ← list   │
  │        object_name  = "Opportunity"                                           │
  │        error_context = "Field 'Discount__c'...\n  Custom fields: ..."         │
  │      )                                                                        │
  │    → ErrorRecord(                                                             │
  │        query_label         = "[simple] Opportunity WHERE Discount__c > 0.15"  │
  │        error_type          = "VALIDATION_ERROR"                               │
  │        error_message       = "Field 'Discount__c' does not exist..."          │
  │        correction_applied  = None  (set later if next iter uses correct name) │
  │        user_intent         = "What opportunities have a discount over 15%?"   │
  │      )                                                                        │
  └───────────────────────────────────────────────────────────────────────────────┘

  ┌───────────────────────────────────────────────────────────────────────────────┐
  │  SOURCE 2: EXECUTION_ERROR in reader iteration                                │
  │                                                                               │
  │  Raw results block line:                                                      │
  │    "[soql] SELECT Name, (SELECT Id FROM Opportunities)... — EXECUTION_ERROR"  │
  │    "  No such relation 'Opportunities'. Did you mean 'Opportunities__r'?"     │
  │                                                                               │
  │  _extract_from_errors():                                                      │
  │    regex: No such relation '(\w+)'.*Did you mean '(\w+)'                      │
  │    parent object inferred from query label (FROM Account)                     │
  │                                                                               │
  │    → RelationshipCorrectionEntry(                                             │
  │        wrong         = "Opportunities"                                        │
  │        correct       = "Opportunities__r"                                     │
  │        parent_object = "Account"                                              │
  │        error_context = "No such relation 'Opportunities'..."                  │
  │      )                                                                        │
  │    → ErrorRecord( query_label, EXECUTION_ERROR,                               │
  │                   correction_applied="Opportunities__r", user_intent )        │
  └───────────────────────────────────────────────────────────────────────────────┘

  ┌───────────────────────────────────────────────────────────────────────────────┐
  │  SOURCE 3: WRITE plan risks + pre_flight_issues                               │
  │                                                                               │
  │  After generate_plan() returns BatchPlan:                                     │
  │    plan.risks = ["Discount_Percent__c cap is 0.30 (VR_Discount_Cap)"]         │
  │    plan.pre_flight_issues = ["No Opportunity record found for TechGlobal"]    │
  │                                                                               │
  │  core.py extracts directly — no Claude call:                                  │
  │    for rule in plan.risks + plan.pre_flight_issues:                           │
  │        session.knowledge.validation_rules.append(rule)                        │
  └───────────────────────────────────────────────────────────────────────────────┘

  ┌───────────────────────────────────────────────────────────────────────────────┐
  │  SOURCE 4: OK query results (metadata)                                        │
  │                                                                               │
  │  After READ loop completes, if accumulated_records > 500 chars:               │
  │    _extract_metadata_from_ok(accumulated_records)                             │
  │    looks for object blocks with both Id: and Name: present                    │
  │                                                                               │
  │    → metadata: ["TechGlobal Solutions (Account): Id=001abc, ARR__c=240000"]   │
  └───────────────────────────────────────────────────────────────────────────────┘

  All sources → session.knowledge.merge(extracted)
         │
         │   Gate: merge() deduplicates by:
         │     field_corrections    → (wrong, object_name)
         │     relationship_corrections → (wrong, parent_object)
         │     validation_rules / metadata → exact string
         │     error_queries        → query_label
         │
  ┌──────▼──────────────────────────────────────────────────────────────────────┐
  │  SessionKnowledge (lives in session, survives reset_to_idle)                │
  │                                                                             │
  │  field_corrections:          [FieldCorrectionEntry, ...]                   │
  │  relationship_corrections:   [RelationshipCorrectionEntry, ...]             │
  │  validation_rules:           ["Discount cap 0.30", ...]                     │
  │  metadata:                   ["TechGlobal: Id=001abc, ARR=240k", ...]       │
  │  error_queries:              [ErrorRecord, ...]  ← full context for synth   │
  └──────┬──────────────────────────────────────────────────────────────────────┘
         │
         ├─── to_prompt_block("inject")   used at each prompt stage THIS session
         │      compact · no error context
         │      ┌──────────────────────────────────────────────────────────┐
         │      │ === Learned from this session ===                        │
         │      │ Field corrections:                                       │
         │      │   Discount__c → Discount_Percent__c, ARR__c (Opportunity)│
         │      │ Relationship corrections:                                │
         │      │   Opportunities → Opportunities__r (Account)             │
         │      │ Validation rules observed:                               │
         │      │   - Discount_Percent__c cap is 0.30 on Opportunity       │
         │      └──────────────────────────────────────────────────────────┘
         │      injected into:
         │        build_read_query_plan_prompt  ← prevents field name errors
         │        build_planner_prompt          ← correct API names in payload
         │        (synthesis prompt uses separate mode, not this)
         │
         └─── to_prompt_block("synthesis")  used by post-session synthesizer
                verbose · includes full error context + error_queries
                ┌──────────────────────────────────────────────────────────┐
                │ === Learned from this session ===                        │
                │ Field corrections:                                       │
                │   Discount__c → Discount_Percent__c (Opportunity)        │
                │   Context: Field 'Discount__c' does not exist...         │
                │             Custom fields: Discount_Percent__c, ARR__c   │
                │ === Raw error queries ===                                 │
                │ Query:      [simple] Opportunity WHERE Discount__c > 0.15 │
                │ Type:       VALIDATION_ERROR                              │
                │ Error:      Field 'Discount__c' does not exist...         │
                │ Correction: Discount_Percent__c                           │
                │ Intent:     "What opps have a discount over 15%?"         │
                └──────────────────────────────────────────────────────────┘
                → input to synthesize_session() Claude call
```

---

## Diagram 5 — SessionKnowledge and OrgKnowledge Interaction

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  Two knowledge layers: in-session (programmatic) and persistent (Claude-synthesized)║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  ┌────────────────────────────────────────────┐
  │  ON SESSION START                          │
  │                                            │
  │  1. SessionKnowledge()  ← empty            │
  │  2. Load OrgKnowledge from ChromaDB        │
  │       collection: "org_knowledge"          │
  │       query: topics from session init      │
  │  3. OrgKnowledge injected into prompts     │
  │     alongside rules.md chunks             │
  └────────────────────────────────────────────┘
         │
         ▼

  ┌─────────────────────────────────────────────────────┐
  │  LAYER 1: SessionKnowledge  (in-session)            │
  │                                                     │
  │  Populated by:                                      │
  │    READ errors   → field_corrections                │
  │                    relationship_corrections         │
  │                    error_queries                    │
  │    READ OK data  → metadata (gated: len > 500)      │
  │    WRITE plan    → validation_rules                 │
  │                                                     │
  │  All programmatic — zero Claude calls               │
  │                                                     │
  │  Lifecycle:                                         │
  │    Created:  first error or rule observed           │
  │    Grows:    merge() called after each READ/WRITE   │
  │    Survives: reset_to_idle()                        │
  │    Wiped:    clear_session() / end_session()        │
  └────────────────────┬────────────────────────────────┘
                       │
         ┌─────────────┤
         │             │
         ▼             ▼
  Injected NOW         Exported at session end
  (this session)       (post-session synthesis)
         │             │
         │             ▼
         │    ┌──────────────────────────────────────────────────────┐
         │    │  synthesize_session(session_id, history, knowledge)  │
         │    │                                                      │
         │    │  Input:                                              │
         │    │    history.to_prompt_block()                         │
         │    │    knowledge.to_prompt_block("synthesis")            │
         │    │      (includes full error context + error_queries)   │
         │    │    existing_snapshot (top-8 chunks from ChromaDB)    │
         │    │                                                      │
         │    │  Claude writes chunks as:                            │
         │    │    "intent + what failed + correct approach"         │
         │    │    confidence: 1.0 confirmed / 0.7 inferred / 0.4 ? │
         │    │                                                      │
         │    │  Per chunk → conflict check (top-k, no raw threshold)│
         │    │    _chroma_query(chunk.content, n=3)                 │
         │    │    if top-1 distance > 0.75 → no conflict possible   │
         │    │    else → Claude verdict: contradiction/overlap/fine  │
         │    │      fine/extension → clean chunk                    │
         │    │      contradiction/overlap → KnowledgeConflict       │
         │    │                                                      │
         │    │  Returns (OrgKnowledge, list[KnowledgeConflict])     │
         │    └──────────────────┬───────────────────────────────────┘
         │                       │
         │          ┌────────────┴──────────────┐
         │          │                           │
         │     clean chunks               conflicts
         │          │                           │
         │          ▼                           ▼
         │    ChromaDB write             SQLite: knowledge_conflicts
         │    collection:                  new_content / existing_content
         │    "org_knowledge"              conflict_type / resolution=None
         │    with metadata:              surfaced to coworker via Slack:
         │    { chunk_type,               "Existing rule says X, session
         │      objects,                   observed Y — which is correct?"
         │      confidence,               [A] existing  [B] new  [C] both
         │      source_session }
         │
         ▼
  ┌─────────────────────────────────────────────────────┐
  │  LAYER 2: OrgKnowledge  (persistent)               │
  │                                                     │
  │  ChromaDB collection: "org_knowledge"               │
  │                                                     │
  │  Chunk types:                                       │
  │    field_correction   "When querying Opportunity    │
  │                        discounts, Discount__c is   │
  │                        invalid — use               │
  │                        Discount_Percent__c"        │
  │    schema_note        "Account→Opportunity SOQL    │
  │                        subqueries use              │
  │                        Opportunities__r"           │
  │    validation_rule    "Discount_Percent__c cannot  │
  │                        exceed 0.30 on Opportunity  │
  │                        (VR_Discount_Cap)"          │
  │    record_context     "TechGlobal Solutions is an  │
  │                        Enterprise customer,        │
  │                        Id=001abc, ARR=240k"        │
  │    misc               anything else worth keeping  │
  │                                                     │
  │  Lifecycle:                                         │
  │    Created:  end_session() → synthesize_session()  │
  │    Grows:    union merge across sessions            │
  │    Compacts: when collection > N chunks (batch job) │
  │    Loaded:   at session start                       │
  │                                                     │
  │  Metadata per chunk:                               │
  │    chunk_type, objects[], confidence,               │
  │    source_session, created_at, disputed             │
  └────────────────────┬────────────────────────────────┘
                       │
                       ▼
  Prompt-time retrieval (stage-filtered):
  ┌───────────────────────────────────────────────────────────────────┐
  │  Stage             Query text               chunk_type filter     │
  │  ─────────────     ──────────────────────   ──────────────────── │
  │  Query plan        "{objects} field names"  field_correction      │
  │                                             schema_note           │
  │  Planner           raw user request         field_correction      │
  │                                             validation_rule       │
  │  Synthesis         raw user question        record_context        │
  │                                             (all types)           │
  └───────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  What the two layers know at each point in a session                         │
  │                                                                              │
  │  Turn 1  User asks about TechGlobal discounts                                │
  │    OrgKnowledge:  "Discount__c → Discount_Percent__c"  ← from past sessions  │
  │    SessionKnowledge:  empty                                                  │
  │    → Claude uses correct field immediately, zero error iterations            │
  │                                                                              │
  │  Turn 2  User asks a new question, Claude tries a bad relationship name      │
  │    OrgKnowledge:  same as above                                              │
  │    SessionKnowledge:  RelationshipCorrectionEntry added after iter 1 error   │
  │    → iter 2 uses correct relationship, does not repeat error                 │
  │                                                                              │
  │  Turn 3  User writes a discount update                                       │
  │    OrgKnowledge:  validation rule chunk retrieved by planner                 │
  │    SessionKnowledge:  validation_rules from WRITE plan added                 │
  │    → planner pre-aware of cap, lists it in risks not as a surprise           │
  │                                                                              │
  │  End of session  /reset                                                      │
  │    synthesize_session()  runs                                                │
  │    → "When querying Opportunity discounts, Discount__c is invalid..."        │
  │       written to org_knowledge collection                                    │
  │    → next user in a fresh session gets this as context from turn 1           │
  └──────────────────────────────────────────────────────────────────────────────┘
```

---

## Diagram 6 — Batch Queue DB Layer

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  BATCH QUEUE  (SQLite)  —  persists write plans from approval through execution     ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  ┌─────────────────────────────────────────────────────────────────────────────────┐
  │  batches                                                                        │
  │                                                                                 │
  │  batch_id          UUID · primary key                                           │
  │  user_id           Slack user who requested the plan                            │
  │  conversation_id   Slack DM channel (for result notifications)                  │
  │  status            PENDING_APPROVAL → APPROVED → EXECUTING                      │
  │                                    → COMPLETED / FAILED / DENIED / EXPIRED      │
  │  summary           plan summary text                                            │
  │  assumptions       JSON list of strings                                         │
  │  approved_by       Slack user ID of coworker who approved (NULL until approved) │
  │  approved_at       timestamp set when status → APPROVED                         │
  │  denial_reason     free text (NULL unless DENIED)                               │
  │  approver_message_ts  Slack message ts of the approval card                     │
  │  created_at / updated_at                                                        │
  └────────────────────────────┬────────────────────────────────────────────────────┘
                               │  1
                               │
                               │  N
  ┌────────────────────────────▼────────────────────────────────────────────────────┐
  │  operations                                                                     │
  │                                                                                 │
  │  operation_id      UUID · primary key                                           │
  │  batch_id          FK → batches                                                 │
  │  sequence_order    1, 2, 3 … execution order                                   │
  │  operation_type    "CREATE" | "UPDATE"                                          │
  │  target_object     Salesforce API name (e.g. "Opportunity")                     │
  │  target_record_id  SF record ID (NULL for CREATE, may be {{op_N.result.id}})    │
  │  payload           JSON dict of field → value pairs                             │
  │  status            PENDING → COMPLETED / FAILED / SKIPPED                       │
  │  result            JSON dict returned by SF (e.g. {"id":"006..","success":true})│
  │  error             error message string (NULL unless FAILED)                    │
  │  created_at / updated_at                                                        │
  └────────────────────────────┬────────────────────────────────────────────────────┘
                               │
                               │  (UPDATE ops with known record_id only)
                               │
  ┌────────────────────────────▼────────────────────────────────────────────────────┐
  │  locks                                                                          │
  │                                                                                 │
  │  lock_id           UUID · primary key                                           │
  │  batch_id          FK → batches (owner of this lock)                            │
  │  lock_type         "RECORD" (only type currently)                               │
  │  object_api_name   Salesforce object being locked                               │
  │  record_id         specific record being locked                                 │
  │  created_at                                                                     │
  │                                                                                 │
  │  Uniqueness: (object_api_name, record_id) across all active batches             │
  │  Active = batch.status IN (PENDING_APPROVAL, APPROVED, EXECUTING)               │
  └─────────────────────────────────────────────────────────────────────────────────┘


  STATUS LIFECYCLE
  ─────────────────────────────────────────────────────────────────────────────────

  create_batch()
    │
    ▼
  PENDING_APPROVAL ──────────────────────────────────────────────────── EXPIRED
    │  coworker clicks Approve                     (TTL elapsed, not yet built)
    │
    ▼
  APPROVED  ←── acquire_locks() runs HERE
    │  execute_batch() called
    │
    ▼
  EXECUTING  ←── operations run one-by-one in sequence_order
    │
    ├── all ops COMPLETED  →  release_locks()  →  COMPLETED
    │
    └── any op raises  →  remaining ops → SKIPPED
                       →  release_locks()  →  FAILED (re-raises)

  PENDING_APPROVAL ── coworker clicks Deny  →  DENIED  (release_locks defensive)


  RECORD LOCKING DETAIL
  ─────────────────────────────────────────────────────────────────────────────────

  acquire_locks(batch_id, operations):
    │
    ├── filter: only UPDATE ops with a known (non-template) record_id
    │
    ├── _find_conflicts(targets, exclude_batch_id)
    │     JOIN locks ON (object_api_name, record_id)
    │     JOIN batches ON batch_id WHERE status IN active statuses
    │     returns conflicting rows
    │
    ├── if conflicts found  →  return False  (batch cannot proceed)
    │
    └── insert one RECORD lock row per target  →  return True

  release_locks(batch_id):
    DELETE FROM locks WHERE batch_id = ?
    always called in executor try/finally — even on failure


  TEMPLATE RESOLUTION  (executor, before each operation)
  ─────────────────────────────────────────────────────────────────────────────────

  {{op_N.result.FIELD}}  in  target_record_id  or  payload values

  Example chained plan:
    op 1: CREATE Opportunity  →  SF returns {"id": "006abc...", "success": true}
    op 2: UPDATE Opportunity  record_id = "{{op_1.result.id}}"

  Resolution:
    results = { 1: {"id": "006abc...", "success": true} }
    _resolve_templates({"_rid": "{{op_1.result.id}}"}, results)
      regex: \{\{op_(\d+)\.result\.(\w+)\}\}
      → "006abc..."

  Unresolvable ref → left as-is → SF rejects → operation FAILED
```

---

## Diagram 7 — Approval Layer

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  APPROVAL FLOW  —  from SEND_FOR_APPROVAL through execution result notification     ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  [slack/listener.py]  on_message()
    HandleResult.intent_type == "SEND_FOR_APPROVAL"
         │
         ▼
  create_batch(user_id, channel, plan)
    INSERT batches  status=PENDING_APPROVAL
    INSERT operations (one row per op)
    returns batch_id (UUID)
         │
         ▼
  format_approval_request(batch_id, plan, requester_id)
    returns (fallback_text, blocks)
         │
  ┌──────▼───────────────────────────────────────────────────────────────────────────┐
  │  Slack Block Kit message sent to coworker DM                                     │
  │                                                                                  │
  │  ┌─ header ─────────────────────────────────────────────────────────────────┐   │
  │  │  Write Plan — Approval Required                                          │   │
  │  └──────────────────────────────────────────────────────────────────────────┘   │
  │  ┌─ section ─────────────────────────────────────────────────────────────────┐  │
  │  │  Requested by: @user          Batch: abc123...                           │   │
  │  │  Plan: Set Discount_Percent__c to 0.15 on TechGlobal Renewal             │   │
  │  └──────────────────────────────────────────────────────────────────────────┘   │
  │  ┌─ section ─────────────────────────────────────────────────────────────────┐  │
  │  │  Operations (1):                                                          │   │
  │  │    1. UPDATE Opportunity 006xyz...                                        │   │
  │  │       Discount_Percent__c = 0.15                                          │   │
  │  │       _User requested 15% discount on renewal_                            │   │
  │  └──────────────────────────────────────────────────────────────────────────┘   │
  │  ┌─ actions ──────────────────────────────────────────────────────────────────┐  │
  │  │  [ Approve ✓ ]  (primary · confirm dialog)      [ Deny ✗ ]  (danger)     │   │
  │  │  value = batch_id                               value = batch_id          │   │
  │  │  action_id = approve_batch                      action_id = deny_batch    │   │
  │  └──────────────────────────────────────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────────────────────────────────────┘
         │
         ▼  (waiting…  batch status = PENDING_APPROVAL)


  ═══════ PATH A: COWORKER CLICKS APPROVE ════════════════════════════════════════════

  [slack/listener.py]  on_approve_batch()
    ack()  ← must be < 3s
         │
         ▼
  handle_approve(batch_id, approver_id, message_ts, channel_id, notify_fn)
    │
    ├── get_batch(batch_id)
    │     guard: status must be PENDING_APPROVAL
    │
    ├── update_batch_status(batch_id, "APPROVED", approved_by=approver_id)
    │     COALESCE ensures approved_by survives later COMPLETED update
    │
    ├── execute_batch(batch_id)  ────────────────────────────────────────────────────┐
    │     acquire_locks()                                                             │
    │     status → EXECUTING                                                          │
    │     for each op in sequence_order:                                              │
    │       resolve {{op_N.result.id}} templates                                      │
    │       CREATE → salesforce.writer.execute_create(obj, payload)                   │
    │       UPDATE → salesforce.writer.execute_update(obj, record_id, payload)        │
    │       op status → COMPLETED  (or FAILED + remaining → SKIPPED)                 │
    │     status → COMPLETED  (or FAILED)                                             │
    │     release_locks()  ← always, in finally                                       │
    │                                                                                 │
    │     execute_update(obj, record_id, payload)                                     │
    │       sf.<Object>.update(record_id, payload)  →  HTTP 204                       │
    │       returns {"id": record_id, "status": 204}                                  │
    │                                                                                 │
    │     execute_create(obj, payload)                                                 │
    │       sf.<Object>.create(payload)                                                │
    │       returns {"id": "006...", "success": True, "errors": []}                   │
    │                                                                                 │◄┘
    ├── (on success) notify_fn(requester, fallback, format_execution_result blocks)
    │     DM to requester:
    │       ✓ Executed successfully
    │       Plan: Set Discount_Percent__c to 0.15…
    │       Operations (1):
    │         ✓ 1. UPDATE Opportunity 006xyz...  Result ID: 006xyz...
    │
    └── returns status_msg  →  update_message(channel, ts, status_msg)
          edits the approval card in-place:
          "✓ Approved and executed by @coworker — 1/1 operation(s) completed."


  ═══════ PATH B: COWORKER CLICKS DENY ══════════════════════════════════════════════

  [slack/listener.py]  on_deny_batch()
    ack()
         │
         ▼
  handle_deny(batch_id, denier_id, reason, notify_fn)
    │
    ├── get_batch(batch_id)
    │     guard: status must be PENDING_APPROVAL
    │
    ├── update_batch_status(batch_id, "DENIED", denial_reason=reason)
    │
    ├── release_locks(batch_id)  ← defensive (locks shouldn't be held here)
    │
    ├── notify_fn(requester, fallback, format_denial_notice blocks)
    │     DM to requester:
    │       ⛔ Plan denied by @coworker
    │       Plan: Set Discount_Percent__c to 0.15…
    │       Reason: "Discount already negotiated above policy cap"
    │
    └── returns status_msg  →  update_message(channel, ts, status_msg)
          "⛔ Denied by @coworker. Reason: …"


  ═══════ NOTIFICATION INJECTION PATTERN ════════════════════════════════════════════

  notify_fn is injected as a callable(user_id, text, blocks):

    Production:  notify_fn = messenger.send_dm   (real Slack DM)
    Tests:       notify_fn = lambda u,t,b: notified.append({...})  (mock)

  This keeps handle_approve / handle_deny free of any Slack SDK imports —
  testable without a live Slack workspace.


  ─────────────────────────────────────────────────────────────────
  Batch DB writes per approval cycle:
    create_batch:           1 batch INSERT + N operation INSERTs
    handle_approve path:    4 batch UPDATEs  (APPROVED, EXECUTING,
                                              COMPLETED/FAILED)
                            N operation UPDATEs (COMPLETED/FAILED/SKIPPED)
                            lock INSERTs + DELETE
    handle_deny path:       1 batch UPDATE  (DENIED)
  ─────────────────────────────────────────────────────────────────
```

---

## Diagram 8 — Full Architecture

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  ASOKA — FULL ARCHITECTURE                                                          ║
║  All components, data stores, and which DB each stage reads/writes                 ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  EXTERNAL                        ASOKA BOT                           DATA STORES
  ────────────────────────────────────────────────────────────────────────────────────

  ┌──────────┐   DM / action    ┌──────────────────────┐
  │  Slack   │ ────────────────►│  slack/listener.py   │
  │  User    │                  │  (Bolt + SocketMode)  │
  │ (DM bot) │ ◄──────────────── │                      │
  └──────────┘   text / blocks  └──────────┬───────────┘
                                           │
                                           │ handle(message, user_id)
                                           ▼
                                ┌──────────────────────┐             ┌─────────────────┐
                                │  orchestrator/        │  R: schema  │                 │
                                │  core.py              │ ──────────► │   SQLite        │
                                │                       │  R: history │                 │
                                │  session state        │  W: session │  • objects      │
                                │  machine              │  (in-memory)│  • fields       │
                                └──────┬───────┬────────┘             │  • relationships│
                                       │       │                      │  • val rules    │
                              READ     │       │ WRITE                │  • roles        │
                              path     │       │ path                 │  • batches      │
                                       │       │                      │  • operations   │
                   ┌───────────────────┘       └────────────┐         │  • locks        │
                   │                                        │         │  • conflicts    │
                   ▼                                        ▼         └─────────────────┘
        ┌──────────────────────┐              ┌──────────────────────┐
        │  orchestrator/       │              │  orchestrator/       │
        │  reader.py           │              │  planner.py          │
        │                      │              │                      │
        │  query-plan loop     │              │  generate_plan()     │
        │  (max 4 iterations)  │              │  1 Claude call       │
        └──────┬───────────────┘              └──────┬───────────────┘
               │                                     │
        ┌──────▼───────────────────────────────────┐ │
        │  context/retriever.py build_context()    │ │
        │                                          │◄┘
        │  R: SQLite  → fields, rels, val rules    │         ┌──────────────────┐
        │  R: ChromaDB → policy chunks (rules.md)  │────────►│   ChromaDB       │
        │  R: ChromaDB → org_knowledge chunks      │         │                  │
        │  stage filter: "read"  or "write"        │         │  asoka_knowledge  │
        └──────────────────────────────────────────┘         │  (rules.md)      │
               │                                             │                  │
               │ context_block                               │  org_knowledge   │
               ▼                                             │  (learned facts) │
        ┌──────────────────────┐                             └──────────────────┘
        │  Anthropic API       │  ◄── all Claude calls flow through here
        │  claude-sonnet-4-6   │
        │                      │      classify_intent    (1 call)
        │  max_tokens: 1024    │      query_plan_loop    (1–4 calls)
        │  JSON output mode    │      synthesis          (1 call)
        │  for structured      │      generate_plan      (1 call)
        │  responses           │      answer_from_history (1 call, follow-ups)
        └──────────────────────┘      synthesize_session  (1 call, end-of-session)
               │
               │ structured JSON responses
               ▼
        ┌──────────────────────┐
        │  salesforce/         │  ◄── all SF API calls from reader + writer
        │  query.py            │
        │  writer.py           │      find_records(obj, fields, where, order_by)
        │                      │      soql(raw_query_string)
        │  simple_salesforce   │      execute_update(obj, record_id, payload)
        │  WebClient           │      execute_create(obj, payload)
        └──────────┬───────────┘
                   │
                   ▼
        ┌──────────────────────┐
        │  Salesforce Org      │
        │                      │
        │  Account             │
        │  Opportunity         │
        │  Case                │
        │  User                │
        └──────────────────────┘


  ═══ WRITE APPROVAL PIPELINE  (detailed) ══════════════════════════════════════════

  SEND_FOR_APPROVAL result
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  batchqueue/batch.py         W: SQLite batches + operations (INSERT)         │
  │  create_batch(user,chan,plan) ─────────────────────────────────────────────► │
  └──────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  approval/formatter.py       (pure: no DB, no SF, no Slack SDK)              │
  │  format_approval_request()   builds Block Kit dicts in memory                │
  └──────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  slack/messenger.py          Slack API: chat.postMessage → coworker DM       │
  │  send_to_coworker()                                                          │
  └──────────────────────────────────────────────────────────────────────────────┘

         ↓  coworker clicks button

  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  approval/handler.py                                                         │
  │  handle_approve()  or  handle_deny()                                         │
  │                                                                              │
  │  R/W: SQLite batches   (status updates, COALESCE preserves approved_by)      │
  │  R/W: SQLite operations (status → COMPLETED / FAILED / SKIPPED)              │
  │  R/W: SQLite locks      (INSERT on acquire, DELETE on release)               │
  └─────────────────────────────────┬────────────────────────────────────────────┘
                                    │ (approve path only)
                                    ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  batchqueue/executor.py                                                      │
  │  execute_batch()                                                             │
  │                                                                              │
  │  R: SQLite operations  (load ops in sequence_order)                          │
  │  R: SQLite locks       (acquire_locks conflict check)                        │
  │  W: SQLite locks       (INSERT lock rows)                                    │
  │  W: Salesforce         (execute_update / execute_create)                     │
  │  W: SQLite operations  (UPDATE status + result JSON)                         │
  │  W: SQLite batches     (EXECUTING → COMPLETED / FAILED)                      │
  │  W: SQLite locks       (DELETE on release)                                   │
  └──────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  slack/messenger.py          Slack API: chat.postMessage → requester DM      │
  │  send_dm(requester, result)  + chat.update → edit approval card in-place     │
  └──────────────────────────────────────────────────────────────────────────────┘


  ═══ END-OF-SESSION SYNTHESIS ═════════════════════════════════════════════════════

  /reset command  →  end_session(user_id)
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  knowledge/synthesizer.py                                                    │
  │  synthesize_session(session_id, history, knowledge)                          │
  │                                                                              │
  │  R: ChromaDB org_knowledge  (existing snapshot for conflict detection)       │
  │  R: ChromaDB asoka_knowledge (rules.md for conflict detection)               │
  │  W: Anthropic API           (1 Claude call)                                  │
  └──────────────────────────────────────────────────────────────────────────────┘
         │
         ├── clean chunks ──────────────────────────────────────────────────────────►
         │                                                                          │
         │                            ┌──────────────────────────────────────────┐ │
         │                            │  ChromaDB  org_knowledge collection       │◄┘
         │                            │  W: add_org_knowledge(chunks)             │
         │                            └──────────────────────────────────────────┘
         │
         └── conflicts ───────────────────────────────────────────────────────────►
                                      ┌──────────────────────────────────────────┐
                                      │  SQLite  knowledge_conflicts table        │
                                      │  W: write_conflicts(conflicts)            │
                                      │  resolution = NULL (awaits coworker)      │
                                      └──────────────────────────────────────────┘


  ═══ STARTUP SEQUENCE ═════════════════════════════════════════════════════════════

  python main.py
    │
    ├─ 1. db.connection.init()        CREATE TABLEs (idempotent)       → SQLite
    ├─ 2. salesforce.client.init()    auth.test + session token         → SF org
    ├─ 3. startup.sync.full_sync()    describe objects + rules + roles  → SF org
    │                                 upsert schema rows                → SQLite
    ├─ 4. knowledge.loader.init()     load / create ChromaDB collections → ChromaDB
    └─ 5. SocketModeHandler.start()   WebSocket connect                 → Slack


  ═══ DATA STORE LEGEND ════════════════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  Store          Tech          Contents                     Lifetime          │
  │  ──────────     ──────────    ──────────────────────────   ───────────────── │
  │  SQLite         sqlite3       schema, batches, ops,        persistent        │
  │                               locks, conflicts                               │
  │                                                                              │
  │  ChromaDB       chromadb      rules.md chunks (static),   persistent        │
  │  (local disk)                 org_knowledge (learned)                        │
  │                                                                              │
  │  In-memory      Python dict   session state, history,      process lifetime  │
  │  sessions                     knowledge (per user_id)      (lost on restart) │
  │                                                                              │
  │  Salesforce     REST API      live CRM data                source of truth   │
  │                 (SOQL/DML)                                                   │
  └──────────────────────────────────────────────────────────────────────────────┘

  ─────────────────────────────────────────────────────────────────
  DB access per message type (typical):
    READ   query:    SQLite×2 (schema, context)  ChromaDB×2  SF×1–5  Anthropic×2–5
    WRITE  request:  SQLite×2 (schema, context)  ChromaDB×2  SF×1    Anthropic×2
    Approval cycle:  SQLite×8 (batch lifecycle)  SF×1 per op
    /reset:          ChromaDB×2 (read+write)     SQLite×1    Anthropic×1
  ─────────────────────────────────────────────────────────────────
```
