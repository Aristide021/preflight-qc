-- ─────────────────────────────────────────────────────────────────────────────
-- PreFlight QC — Redelivery Tracking DDL
-- Table: redelivery_tracking
-- Written by the Action agent when a redelivery is filed;
-- Read by the Orchestrator and QC-Analyst to maintain a closed audit loop.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS redelivery_tracking
(
    tracking_id          UUID            COMMENT 'Unique tracking record ID',
    title_id             String          COMMENT 'Title being redelivered',
    original_inspection  UUID            COMMENT 'FK: qc_inspections.inspection_id that triggered this',
    package_id           UUID            COMMENT 'New package ID for the redelivery',
    vendor_id            LowCardinality(String) COMMENT 'Delivering vendor ID',
    platform_spec        LowCardinality(String) COMMENT 'e.g. netflix-imf-2.1',

    -- Agent decision
    filed_at             DateTime        COMMENT 'When the Action agent filed the redelivery',
    filed_by_agent       LowCardinality(String) COMMENT 'Agent version that filed: orchestrator-v1, etc.',
    decision             LowCardinality(String) COMMENT 'Decision: redeliver | waive | escalate | investigate',
    decision_rationale   String          COMMENT 'Agent reasoning for the decision (spec citation included)',

    -- Status
    status               LowCardinality(String) COMMENT 'Status: filed | in_progress | resubmitted | resolved | cancelled',
    attempt_number       UInt8           COMMENT 'Redelivery attempt number (0 = initial)',
    estimated_cost_usd   Float32         COMMENT 'Estimated remediation cost in USD',
    resolved_at          Nullable(DateTime) COMMENT 'Timestamp when resolved',

    -- Spec citations (auditable compliance evidence)
    spec_section         String          COMMENT 'Spec section cited (e.g. "Section 4.2.3 Loudness")',
    spec_requirement     String          COMMENT 'Exact spec language quoted',

    -- Risk context from QC-Analyst
    risk_score           Float32         COMMENT 'QC-Analyst risk score at time of filing (0.0–1.0)',
    predicted_fail_codes String          COMMENT 'JSON array: top predicted failure codes for redelivery'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(filed_at)
ORDER BY (vendor_id, title_id, filed_at)
COMMENT 'Redelivery tracking — written by Action agent, closed by Orchestrator';
