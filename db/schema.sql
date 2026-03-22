-- =============================================================
-- STRUCTURAL STORE: Salesforce schema cache
-- Populated once at startup by startup/sync.py
-- Re-synced per-object after any successful metadata write
-- =============================================================

CREATE TABLE IF NOT EXISTS objects (
    api_name       TEXT PRIMARY KEY,   -- e.g. "Account", "Opportunity__c"
    label          TEXT NOT NULL,      -- human label from SF describe
    is_custom      BOOLEAN NOT NULL DEFAULT 0,
    last_synced_at TIMESTAMP           -- set every time we re-describe this object
);

CREATE TABLE IF NOT EXISTS fields (
    object_api_name   TEXT NOT NULL,   -- FK → objects.api_name
    field_api_name    TEXT NOT NULL,   -- e.g. "Discount_Percent__c"
    label             TEXT NOT NULL,
    data_type         TEXT NOT NULL,   -- "string", "currency", "picklist", etc.
    is_required       BOOLEAN NOT NULL DEFAULT 0,
    is_editable       BOOLEAN NOT NULL DEFAULT 1,
    is_custom         BOOLEAN NOT NULL DEFAULT 0,
    is_deprecated     BOOLEAN NOT NULL DEFAULT 0,  -- flagged from field description keywords
    description       TEXT,                         -- raw SF field description (Phase B mines this)
    picklist_values   TEXT,                         -- JSON array: ["Prospecting","Proposal",...]
    reference_to      TEXT,                         -- target object for Lookup/MasterDetail
    relationship_name TEXT,                         -- SF relationship name (e.g. "Account")
    PRIMARY KEY (object_api_name, field_api_name),
    FOREIGN KEY (object_api_name) REFERENCES objects(api_name)
);

CREATE TABLE IF NOT EXISTS relationships (
    parent_object     TEXT NOT NULL,
    child_object      TEXT NOT NULL,
    field_api_name    TEXT NOT NULL,   -- the lookup field on the child object
    relationship_type TEXT NOT NULL,   -- "Lookup" or "MasterDetail"
    relationship_name TEXT,            -- e.g. "Opportunities" (the child relationship name)
    PRIMARY KEY (parent_object, child_object, field_api_name)
);

-- Salesforce validation rules pulled via Metadata API
-- Used for pre-flight checks before sending a batch for approval
CREATE TABLE IF NOT EXISTS validation_rules (
    rule_id         TEXT PRIMARY KEY,  -- SF metadata ID
    object_api_name TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT 1,
    description     TEXT,              -- optional description set by admin
    error_message   TEXT,              -- the message SF shows when the rule fires
    formula         TEXT               -- the actual validation formula
);

-- SF UserRole hierarchy, used to resolve "transfer to manager" chains
CREATE TABLE IF NOT EXISTS role_hierarchy (
    role_id        TEXT PRIMARY KEY,
    role_name      TEXT NOT NULL,
    parent_role_id TEXT,               -- NULL for top-level roles (e.g. VP Sales)
    FOREIGN KEY (parent_role_id) REFERENCES role_hierarchy(role_id)
);

-- =============================================================
-- BATCH / QUEUE SYSTEM
-- Manages the lifecycle of every write request
-- =============================================================

CREATE TABLE IF NOT EXISTS batches (
    batch_id            TEXT PRIMARY KEY,   -- UUID
    user_id             TEXT NOT NULL,      -- Slack user ID of the requester
    conversation_id     TEXT NOT NULL,      -- Slack DM channel ID
    status              TEXT NOT NULL DEFAULT 'DRAFT',
    -- DRAFT | PENDING_APPROVAL | APPROVED | EXECUTING | COMPLETED
    -- DENIED | EXPIRED | FAILED
    summary             TEXT,
    assumptions         TEXT,              -- JSON array of strings
    reasoning           TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at         TIMESTAMP,
    approved_by         TEXT,              -- Slack user ID of the approver
    denial_reason       TEXT,
    approver_message_ts TEXT               -- Slack message ts of the approval DM
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id     TEXT PRIMARY KEY,     -- UUID
    batch_id         TEXT NOT NULL,
    sequence_order   INTEGER NOT NULL,     -- execution order within the batch
    operation_type   TEXT NOT NULL,
    -- Record ops:   CREATE | UPDATE | DELETE
    -- Metadata ops: CREATE_FIELD | UPDATE_FIELD | DELETE_FIELD
    target_object    TEXT NOT NULL,        -- SF object API name
    target_record_id TEXT,                 -- NULL for creates and metadata ops
    target_field     TEXT,                 -- populated only for field-level metadata ops
    payload          TEXT NOT NULL,        -- JSON; may contain {{op_N.result.id}} refs
    status           TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING | EXECUTING | COMPLETED | FAILED | SKIPPED
    result           TEXT,                 -- JSON returned by SF on success
    error            TEXT,                 -- error string on failure
    executed_at      TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);

-- One lock row per "thing" a batch intends to touch
-- Cleared when batch reaches COMPLETED / FAILED / DENIED / EXPIRED
CREATE TABLE IF NOT EXISTS locks (
    lock_id         TEXT PRIMARY KEY,      -- UUID
    batch_id        TEXT NOT NULL,
    object_api_name TEXT NOT NULL,
    record_id       TEXT,                  -- NULL for creates and metadata ops
    lock_type       TEXT NOT NULL,         -- "RECORD" or "METADATA"
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);

-- =============================================================
-- KNOWLEDGE SYSTEM
-- Tracks synthesized org knowledge conflicts awaiting coworker resolution
-- =============================================================

CREATE TABLE IF NOT EXISTS knowledge_conflicts (
    conflict_id          TEXT PRIMARY KEY,  -- UUID
    new_chunk_content    TEXT NOT NULL,
    new_chunk_type       TEXT NOT NULL,
    new_chunk_objects    TEXT NOT NULL,     -- JSON array of SF object names
    new_chunk_confidence REAL NOT NULL,
    new_chunk_session    TEXT NOT NULL,     -- source_session of the new chunk
    existing_content     TEXT NOT NULL,     -- the conflicting existing chunk text
    existing_collection  TEXT NOT NULL,     -- "rules" | "org_knowledge"
    conflict_type        TEXT NOT NULL,     -- "contradiction" | "overlap"
    resolution           TEXT,             -- NULL=pending, "new_wins"|"existing_wins"|"both_valid"
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at          TIMESTAMP
);

-- =============================================================
-- INDEXES
-- =============================================================

-- Fast field lookup by object (used constantly by context/structural.py)
CREATE INDEX IF NOT EXISTS idx_fields_object
    ON fields(object_api_name);

-- Fast lock conflict checks
CREATE INDEX IF NOT EXISTS idx_locks_batch
    ON locks(batch_id);
CREATE INDEX IF NOT EXISTS idx_locks_object_record
    ON locks(object_api_name, record_id);

-- Fast operation lookup by batch and order
CREATE INDEX IF NOT EXISTS idx_ops_batch_order
    ON operations(batch_id, sequence_order);

-- Fast batch lookup by status (TTL checker, approval routing)
CREATE INDEX IF NOT EXISTS idx_batches_status
    ON batches(status);
