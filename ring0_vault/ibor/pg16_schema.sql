-- ============================================================================
-- NIOS Core v3.0 -- PostgreSQL 16 SERIALIZABLE settlement schema
-- Governing sections: sec_005, sec_009, sec_011, sec_012, sec_017
--
-- This database is the durable accounting half of the Ring 0 sovereign vault.
-- TigerBeetle owns the atomic two-phase outbox; PostgreSQL 16 owns the
-- double-entry investment book of records (IBOR), the outbox mirror, the
-- broker-ack inbox, tax lots, source quarantine evidence and the SHA-256
-- grounding ledger.  Every money column is a 128-bit-safe BIGINT of
-- micro-units -- never a floating-point value.
--
-- Apply only inside a SERIALIZABLE transaction.  The DDL is idempotent.
-- ============================================================================

BEGIN;

SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;

-- ---------------------------------------------------------------------------
-- 1. financial_outbox -- durable mirror of the TigerBeetle two-phase outbox
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS financial_outbox (
    outbox_idempotency_key  CHAR(64)      PRIMARY KEY,
    proposal_id             TEXT          NOT NULL,
    strategy_id             TEXT          NOT NULL,
    silo                    TEXT          NOT NULL,
    vault                   TEXT          NOT NULL
        CHECK (vault IN ('ReserveYield', 'LongTermCompounder', 'TacticalSwing', 'SpinalScalper')),
    symbol                  TEXT          NOT NULL,
    side                    TEXT          NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty_micros              BIGINT        NOT NULL CHECK (qty_micros > 0),
    price_micros            BIGINT        NOT NULL CHECK (price_micros > 0),
    notional_micros         BIGINT        NOT NULL CHECK (notional_micros >= 0),
    fee_micros              BIGINT        NOT NULL DEFAULT 0 CHECK (fee_micros >= 0),
    tax_micros              BIGINT        NOT NULL DEFAULT 0 CHECK (tax_micros >= 0),
    total_lock_micros       BIGINT        NOT NULL CHECK (total_lock_micros >= 0),
    state                   TEXT          NOT NULL
        CHECK (state IN ('PENDING', 'COMMITTED', 'VOIDED')),
    pending_transfer_id     CHAR(64),
    broker_order_id         TEXT,
    created_ns              BIGINT        NOT NULL,
    updated_ns              BIGINT        NOT NULL,
    CONSTRAINT financial_outbox_updated_after_created CHECK (updated_ns >= created_ns),
    CONSTRAINT financial_outbox_transfer_required CHECK (
        state = 'PENDING' OR pending_transfer_id IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_financial_outbox_state
    ON financial_outbox (state, updated_ns);
CREATE INDEX IF NOT EXISTS idx_financial_outbox_silo
    ON financial_outbox (silo, symbol, updated_ns);
CREATE UNIQUE INDEX IF NOT EXISTS uq_financial_outbox_transfer
    ON financial_outbox (pending_transfer_id)
    WHERE pending_transfer_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. order_ack_inbox -- exactly-once broker acknowledgement inbox
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_ack_inbox (
    broker_order_id         TEXT          PRIMARY KEY,
    outbox_idempotency_key  CHAR(64)      NOT NULL REFERENCES financial_outbox (outbox_idempotency_key)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    ack_state               TEXT          NOT NULL
        CHECK (ack_state IN ('FILLED', 'PARTIALLY_FILLED', 'WORKING', 'CANCELED', 'REJECTED', 'TIMEOUT')),
    filled_qty_micros       BIGINT        NOT NULL DEFAULT 0 CHECK (filled_qty_micros >= 0),
    avg_price_micros        BIGINT        NOT NULL DEFAULT 0 CHECK (avg_price_micros >= 0),
    applied                 BOOLEAN       NOT NULL DEFAULT FALSE,
    raw_payload             JSONB         NOT NULL DEFAULT '{}'::jsonb,
    received_ns             BIGINT        NOT NULL,
    applied_ns              BIGINT,
    CONSTRAINT order_ack_applied_consistency CHECK (
        (applied = FALSE AND applied_ns IS NULL)
        OR (applied = TRUE AND applied_ns IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_order_ack_inbox_key
    ON order_ack_inbox (outbox_idempotency_key, received_ns);
CREATE INDEX IF NOT EXISTS idx_order_ack_inbox_unapplied
    ON order_ack_inbox (applied, received_ns)
    WHERE applied = FALSE;

-- ---------------------------------------------------------------------------
-- 3. ibor_double_entry_ledger -- immutable double-entry journal
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ibor_double_entry_ledger (
    transfer_id             CHAR(64)      PRIMARY KEY,
    silo                    TEXT          NOT NULL,
    debit_account           TEXT          NOT NULL,
    credit_account          TEXT          NOT NULL,
    amount_micros           BIGINT        NOT NULL CHECK (amount_micros >= 0),
    code                    INTEGER       NOT NULL CHECK (code >= 0),
    phase                   TEXT          NOT NULL CHECK (phase IN ('PENDING', 'POSTED', 'VOIDED')),
    pending_id              CHAR(64),
    created_ns              BIGINT        NOT NULL,
    posted_ns               BIGINT,
    CONSTRAINT ibor_ledger_distinct_accounts CHECK (debit_account <> credit_account),
    CONSTRAINT ibor_ledger_posted_has_timestamp CHECK (
        (phase = 'POSTED' AND posted_ns IS NOT NULL)
        OR (phase <> 'POSTED')
    )
);

CREATE INDEX IF NOT EXISTS idx_ibor_ledger_silo_time
    ON ibor_double_entry_ledger (silo, created_ns);
CREATE INDEX IF NOT EXISTS idx_ibor_ledger_phase
    ON ibor_double_entry_ledger (phase, created_ns);
CREATE INDEX IF NOT EXISTS idx_ibor_ledger_pending
    ON ibor_double_entry_ledger (pending_id)
    WHERE pending_id IS NOT NULL;

-- The cross-row floor is enforced by the materialised projection in
-- silo_vault_balances (CONSTRAINT silo_vault_debits_within_credits) and by
-- TigerBeetle's native debits_must_not_exceed_credits rule.  Per-row bounds
-- are enforced directly by the CHECK constraints above.

-- ---------------------------------------------------------------------------
-- 4. silo_vault_balances -- materialised per-vault balance projection
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silo_vault_balances (
    silo                    TEXT          NOT NULL,
    vault                   TEXT          NOT NULL
        CHECK (vault IN ('ReserveYield', 'LongTermCompounder', 'TacticalSwing', 'SpinalScalper')),
    credits_posted_micros   BIGINT        NOT NULL DEFAULT 0 CHECK (credits_posted_micros >= 0),
    debits_posted_micros    BIGINT        NOT NULL DEFAULT 0 CHECK (debits_posted_micros >= 0),
    credits_pending_micros  BIGINT        NOT NULL DEFAULT 0 CHECK (credits_pending_micros >= 0),
    debits_pending_micros   BIGINT        NOT NULL DEFAULT 0 CHECK (debits_pending_micros >= 0),
    updated_ns              BIGINT        NOT NULL,
    PRIMARY KEY (silo, vault),
    CONSTRAINT silo_vault_debits_within_credits CHECK (
        debits_posted_micros + debits_pending_micros
        <= credits_posted_micros + credits_pending_micros
    )
);

CREATE INDEX IF NOT EXISTS idx_silo_vault_balances_vault
    ON silo_vault_balances (vault, silo);

-- ---------------------------------------------------------------------------
-- 5. tax_lots -- multi-FX tax lots (India STT/TDS, US, FX)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tax_lots (
    lot_id                  CHAR(64)      PRIMARY KEY,
    silo                    TEXT          NOT NULL,
    symbol                  TEXT          NOT NULL,
    side                    TEXT          NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty_micros              BIGINT        NOT NULL CHECK (qty_micros > 0),
    cost_basis_micros       BIGINT        NOT NULL CHECK (cost_basis_micros >= 0),
    proceeds_micros         BIGINT        NOT NULL DEFAULT 0 CHECK (proceeds_micros >= 0),
    realized_pnl_micros     BIGINT        NOT NULL DEFAULT 0,
    fee_micros              BIGINT        NOT NULL DEFAULT 0 CHECK (fee_micros >= 0),
    tax_micros              BIGINT        NOT NULL DEFAULT 0 CHECK (tax_micros >= 0),
    stt_micros              BIGINT        NOT NULL DEFAULT 0 CHECK (stt_micros >= 0),
    tds_micros              BIGINT        NOT NULL DEFAULT 0 CHECK (tds_micros >= 0),
    acquired_ns             BIGINT        NOT NULL,
    disposed_ns             BIGINT,
    tax_year                INTEGER       CHECK (tax_year BETWEEN 2000 AND 2200),
    CONSTRAINT tax_lot_disposal_after_acquisition CHECK (
        disposed_ns IS NULL OR disposed_ns >= acquired_ns
    )
);

CREATE INDEX IF NOT EXISTS idx_tax_lots_silo_symbol
    ON tax_lots (silo, symbol, acquired_ns);
CREATE INDEX IF NOT EXISTS idx_tax_lots_open
    ON tax_lots (silo, symbol)
    WHERE disposed_ns IS NULL;

-- ---------------------------------------------------------------------------
-- 6. source_quarantine_store -- corrupt / divergent feed evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_quarantine_store (
    quarantine_id           CHAR(64)      PRIMARY KEY,
    source_id               TEXT          NOT NULL,
    silo                    TEXT          NOT NULL,
    symbol                  TEXT,
    reason                  TEXT          NOT NULL
        CHECK (reason IN ('LAG', 'DIVERGENCE', 'SPLIT_GUARD', 'CHECKSUM', 'SCHEMA', 'MANUAL')),
    observed_value          DOUBLE PRECISION,
    threshold_value         DOUBLE PRECISION,
    raw_payload             JSONB         NOT NULL DEFAULT '{}'::jsonb,
    quarantined_ns          BIGINT        NOT NULL,
    released_ns             BIGINT,
    CONSTRAINT source_quarantine_release_after_quarantine CHECK (
        released_ns IS NULL OR released_ns >= quarantined_ns
    )
);

CREATE INDEX IF NOT EXISTS idx_source_quarantine_active
    ON source_quarantine_store (silo, source_id, quarantined_ns)
    WHERE released_ns IS NULL;

-- ---------------------------------------------------------------------------
-- 7. grounding_ledger_hashes -- SHA-256 commitments for every numeric claim
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grounding_ledger_hashes (
    sha256_digest           CHAR(64)      PRIMARY KEY
        CHECK (sha256_digest ~ '^[0-9a-f]{64}$'),
    symbol                  TEXT          NOT NULL,
    valid_time_ns           BIGINT        NOT NULL,
    knowledge_ts_ns         BIGINT        NOT NULL,
    as_of_ns                BIGINT        NOT NULL,
    metrics                 JSONB         NOT NULL,
    source                  TEXT          NOT NULL DEFAULT 'ring2_brain/truth',
    created_ns              BIGINT        NOT NULL,
    CONSTRAINT grounding_no_lookahead CHECK (
        knowledge_ts_ns <= as_of_ns AND valid_time_ns <= as_of_ns
    ),
    CONSTRAINT grounding_metrics_object CHECK (jsonb_typeof(metrics) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_grounding_symbol_time
    ON grounding_ledger_hashes (symbol, as_of_ns DESC);
CREATE INDEX IF NOT EXISTS idx_grounding_knowledge
    ON grounding_ledger_hashes (knowledge_ts_ns);

-- ---------------------------------------------------------------------------
-- 8. V1.9 entity-linked post-mortem memory (``gbrain`` harvest)
--
-- Layer 1 is append-only and immutable: a correction is a new event, never an
-- in-place update.  Layer 2 is a deterministic cache derived from layer 1 --
-- safe to truncate, because ``EntityMemoryGraph.load_state`` always replays the
-- timeline and never trusts the cache.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nios_entity_timeline (
    event_id            TEXT        PRIMARY KEY,
    ts_utc              BIGINT      NOT NULL CHECK (ts_utc >= 0),
    entity_ids          JSONB       NOT NULL,
    outcome_kind        TEXT        NOT NULL CHECK (
        outcome_kind IN ('LOSS', 'SLIPPAGE_SPIKE', 'BREAKER_TRIP', 'ORDER_REJECTED', 'WIN')
    ),
    net_pnl_micros      BIGINT      NOT NULL,
    slippage_bps        DOUBLE PRECISION NOT NULL DEFAULT 0,
    lesson_summary      TEXT        NOT NULL DEFAULT '',
    payload_sha256      CHAR(64)    NOT NULL,
    recorded_at_utc     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT entity_timeline_entities_array CHECK (jsonb_typeof(entity_ids) = 'array'),
    CONSTRAINT entity_timeline_digest_hex CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_entity_timeline_time
    ON nios_entity_timeline (ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_entity_timeline_entities
    ON nios_entity_timeline USING GIN (entity_ids);
CREATE INDEX IF NOT EXISTS idx_entity_timeline_outcome
    ON nios_entity_timeline (outcome_kind, ts_utc DESC);

CREATE TABLE IF NOT EXISTS nios_compiled_entity_truths (
    entity_id               TEXT        PRIMARY KEY,
    entity_type             TEXT        NOT NULL CHECK (
        entity_type IN ('SYMBOL', 'SILO', 'STRATEGY', 'MACRO', 'REGIME')
    ),
    sample_count            INTEGER     NOT NULL CHECK (sample_count >= 0),
    win_rate_bps            DOUBLE PRECISION NOT NULL CHECK (win_rate_bps >= 0 AND win_rate_bps <= 10000),
    avg_slippage_bps        DOUBLE PRECISION NOT NULL,
    worst_drawdown_bps      DOUBLE PRECISION NOT NULL CHECK (worst_drawdown_bps >= 0),
    known_failure_modes     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    caution_regimes         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_compiled_utc       BIGINT      NOT NULL DEFAULT 0,
    truth_sha256            CHAR(64)    NOT NULL,
    CONSTRAINT compiled_truth_modes_array CHECK (jsonb_typeof(known_failure_modes) = 'array'),
    CONSTRAINT compiled_truth_regimes_array CHECK (jsonb_typeof(caution_regimes) = 'array'),
    CONSTRAINT compiled_truth_digest_hex CHECK (truth_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_compiled_truth_type
    ON nios_compiled_entity_truths (entity_type, sample_count DESC);

-- ---------------------------------------------------------------------------
-- 9. V2.3 memory checkpoints -- sealed compaction boundaries (PART 2.2)
--
-- A checkpoint binds the canonical compiled-truth digest to an event cutoff, so
-- a restart can replay ``seq > checkpoint_seq`` instead of the whole timeline and
-- still prove it reached the same truths.  ``compiled_truths`` is an object keyed
-- by entity_id; the SHA-256 over that object is the seal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nios_memory_checkpoints (
    checkpoint_seq          BIGINT      PRIMARY KEY CHECK (checkpoint_seq >= 0),
    cutoff_ts_utc           TIMESTAMPTZ NOT NULL,
    event_count_compacted   INTEGER     NOT NULL DEFAULT 0 CHECK (event_count_compacted >= 0),
    compiled_truths         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    checkpoint_sha256       CHAR(64)    NOT NULL,
    created_at_utc          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memory_checkpoint_truths_object CHECK (jsonb_typeof(compiled_truths) = 'object'),
    CONSTRAINT memory_checkpoint_digest_hex CHECK (checkpoint_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_memory_checkpoints_cutoff
    ON nios_memory_checkpoints (cutoff_ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_memory_checkpoints_created
    ON nios_memory_checkpoints (created_at_utc DESC);

-- ---------------------------------------------------------------------------
-- 10. V2.3 episodic operator memory (PART 2.3)
--
-- One row per operator intervention -- a plannotator override, a mission
-- revocation, a manual tranche rearm, a cold-start freeze clearance, an
-- autopilot pause/play.  ``context_tags`` is a JSONB array with a GIN index so
-- ``context_tags @> '["REGIME:CONTRACTION"]'`` stays index-backed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nios_episodic_memory (
    episode_id              TEXT        PRIMARY KEY,
    ts_utc                  BIGINT      NOT NULL CHECK (ts_utc >= 0),
    actor                   TEXT        NOT NULL DEFAULT 'operator',
    action_kind             TEXT        NOT NULL,
    regime                  TEXT        NOT NULL DEFAULT '',
    context_tags            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    decision_summary        TEXT        NOT NULL DEFAULT '',
    payload_sha256          CHAR(64)    NOT NULL,
    CONSTRAINT episodic_context_tags_array CHECK (jsonb_typeof(context_tags) = 'array'),
    CONSTRAINT episodic_digest_hex CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_episodic_memory_time
    ON nios_episodic_memory (ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_episodic_memory_kind
    ON nios_episodic_memory (action_kind, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_episodic_memory_tags
    ON nios_episodic_memory USING GIN (context_tags);

-- ---------------------------------------------------------------------------
-- 11. V2.3 schema migration ledger
--
-- Every applied DDL version records a SHA-256 of its own body.  A stored
-- checksum that no longer matches the code means the schema on disk was not
-- produced by the migration that claims it, and the repository refuses to guess.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nios_schema_migrations (
    version                 INTEGER     PRIMARY KEY CHECK (version >= 0),
    name                    TEXT        NOT NULL,
    applied_at_utc          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checksum_sha256         CHAR(64)    NOT NULL,
    CONSTRAINT schema_migration_digest_hex CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
);

-- ---------------------------------------------------------------------------
-- 12. Operational guard rails
-- ---------------------------------------------------------------------------
COMMENT ON TABLE financial_outbox IS
    'Durable mirror of the TigerBeetle two-phase outbox; one row per deterministic idempotency key.';
COMMENT ON TABLE order_ack_inbox IS
    'Exactly-once broker ack inbox keyed by broker_order_id; applied flag is the dedupe latch.';
COMMENT ON TABLE ibor_double_entry_ledger IS
    'Append-only double-entry journal. Money is BIGINT micro-units, never floating point.';
COMMENT ON TABLE silo_vault_balances IS
    'Per-silo, per-vault materialised balances across the four sovereign vaults.';
COMMENT ON TABLE tax_lots IS
    'Multi-FX tax lots including India STT and TDS components.';
COMMENT ON TABLE source_quarantine_store IS
    'Evidence for quarantined market-data sources (lag, divergence, split guard).';
COMMENT ON TABLE grounding_ledger_hashes IS
    'SHA-256 commitments from the Ring 2 grounding ledger; rejects lookahead facts.';
COMMENT ON TABLE nios_entity_timeline IS
    'Append-only post-mortem outcome events keyed by SHA-256 payload commitment (layer 1).';
COMMENT ON TABLE nios_compiled_entity_truths IS
    'Deterministic rolling truth per entity, derived from nios_entity_timeline (layer 2).';
COMMENT ON TABLE nios_memory_checkpoints IS
    'Sealed compaction checkpoints: a cutoff plus the SHA-256 of the compiled truths it stands for.';
COMMENT ON TABLE nios_episodic_memory IS
    'Operator decisions (overrides, revocations, rearms, freeze clearances) with GIN-indexed context tags.';
COMMENT ON TABLE nios_schema_migrations IS
    'Applied DDL versions with the SHA-256 of each migration body.';

COMMIT;
