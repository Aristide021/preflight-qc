-- ─────────────────────────────────────────────────────────────────────────────
-- ClickHouse Schema: qc_inspections
--
-- The primary analytical table. Stores every QC inspection event across all
-- titles, vendors, platforms, and redelivery attempts.
--
-- Design decisions:
--   MergeTree ORDER BY (platform_spec, vendor_id, title_id, inspected_at)
--     → the primary query pattern is "for this platform+vendor, show history"
--     → ClickHouse will co-locate these rows for fast range scans
--
--   LowCardinality(String) for enumerable fields
--     → ClickHouse stores these as dictionary-encoded; ~10x compression on
--       fields like vendor_id (20 distinct values in 50M rows)
--
--   Enum8 for result and severity
--     → stored as a single byte; fast filtering; self-documenting
--
--   PARTITION BY toYYYYMM(inspected_at)
--     → time-range queries skip partitions entirely; essential for
--       "show last 90 days" type queries the QC-Analyst issues
-- ─────────────────────────────────────────────────────────────────────────────

CREATE DATABASE IF NOT EXISTS preflight
    COMMENT 'Delivery-QC Compliance Agent — analytical database';

USE preflight;

-- ── Primary inspection table ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS qc_inspections
(
    -- Identity
    inspection_id        UUID            COMMENT 'Unique inspection event ID',
    title_id             String          COMMENT 'Platform title identifier (e.g. NFLX_12345)',
    title_name           String          COMMENT 'Human-readable title name',
    version_label        LowCardinality(String)
                                         COMMENT 'Delivery version (OV, v2, redelivery-3, ...)',

    -- Package metadata
    package_type         LowCardinality(String)
                                         COMMENT 'Package format: IMF | ProRes | MXF | DCP',
    package_id           UUID            COMMENT 'Unique package identifier (from ASSETMAP)',
    vendor_id            LowCardinality(String)
                                         COMMENT 'Delivering vendor/facility ID',
    vendor_name          LowCardinality(String)
                                         COMMENT 'Human-readable vendor name',

    -- Spec / platform context
    platform_spec        LowCardinality(String)
                                         COMMENT 'Platform spec identifier: netflix-imf-2.x | amazon-vcs-3.x',
    spec_version         LowCardinality(String)
                                         COMMENT 'Specific spec version (e.g. netflix-imf-2.0-20240101)',
    app_profile          LowCardinality(String)
                                         COMMENT 'IMF Application Profile: App2E | App2 | App5',

    -- Timing
    submitted_at         DateTime        COMMENT 'When the package was submitted for QC',
    inspected_at         DateTime        COMMENT 'When the inspection completed',

    -- QC stage
    stage                LowCardinality(String)
                                         COMMENT 'QC stage: photon | backlot | iaas | auto-qc | manual-qc',

    -- Result
    result               Enum8('pass'=1, 'fail'=2, 'warn'=3)
                                         COMMENT 'Inspection outcome',

    -- Error detail (NULL when result=pass)
    error_code           LowCardinality(String)
                                         COMMENT 'IMF error code (e.g. IMF_CPL_ERROR)',
    error_category       LowCardinality(String)
                                         COMMENT 'Error category: structural | essence | audio | subtitle | metadata | integrity',
    error_severity       Enum8('blocking'=1, 'cosmetic'=2, 'advisory'=3)
                                         COMMENT 'Impact level per platform spec',
    error_message        String          COMMENT 'Human-readable error description',
    error_context        String          COMMENT 'JSON: additional context (file path, timecode, track ID, etc.)',

    -- Redelivery tracking
    redelivery_attempt   UInt8           COMMENT 'Attempt number (0=first submission, 1=first redelivery, ...)',
    remediation_cost_usd Float32         COMMENT 'Estimated cost to fix and redeliver (USD)',
    resolved             Bool            COMMENT 'Whether this error was ultimately resolved',
    resolved_at          Nullable(DateTime)
                                         COMMENT 'When the error was resolved (NULL if still open)',

    -- Codec / technical parameters (for pattern analysis)
    codec                LowCardinality(String)
                                         COMMENT 'Video codec: JPEG2000 | H.264 | H.265 | ProRes',
    frame_rate           LowCardinality(String)
                                         COMMENT 'Frame rate: 23.976 | 24 | 25 | 29.97 | 30 | ...',
    resolution           LowCardinality(String)
                                         COMMENT 'Nominal resolution: 1920x1080 | 3840x2160 | ...',
    hdr_format           LowCardinality(String)
                                         COMMENT 'HDR: SDR | HDR10 | HDR10+ | DolbyVision | HLG',
    audio_config         LowCardinality(String)
                                         COMMENT 'Audio: 5.1 | 7.1 | Atmos | Stereo | Binaural'
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(inspected_at)
ORDER BY (platform_spec, vendor_id, title_id, inspected_at)
SETTINGS index_granularity = 8192
COMMENT 'QC inspection events — primary analytical table';

-- ── Redelivery tracking table ─────────────────────────────────────────────────
-- Written by the Action agent when it files a redelivery.
-- Read by the Orchestrator to check loop status.

CREATE TABLE IF NOT EXISTS redelivery_tracking
(
    tracking_id          UUID            COMMENT 'Unique tracking record ID',
    title_id             String          COMMENT 'Title being redelivered',
    original_inspection  UUID            COMMENT 'FK: qc_inspections.inspection_id that triggered this',
    package_id           UUID            COMMENT 'New package ID for the redelivery',
    vendor_id            LowCardinality(String),
    platform_spec        LowCardinality(String),

    -- Agent decision
    filed_at             DateTime        COMMENT 'When the Action agent filed the redelivery',
    filed_by_agent       LowCardinality(String)
                                         COMMENT 'Agent version that filed: orchestrator-v1, etc.',
    decision             LowCardinality(String)
                                         COMMENT 'Decision: redeliver | waive | escalate | investigate',
    decision_rationale   String          COMMENT 'Agent reasoning for the decision (spec citation included)',

    -- Status
    status               LowCardinality(String)
                                         COMMENT 'Status: filed | in_progress | resubmitted | resolved | cancelled',
    attempt_number       UInt8,
    estimated_cost_usd   Float32,
    resolved_at          Nullable(DateTime),

    -- Spec citations (the agent must cite the spec, not hallucinate rules)
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

-- ── Materialized view: vendor failure rates ───────────────────────────────────
-- Bonus item #3 — ClickHouse-native incremental aggregation.
-- Updated automatically on every INSERT into qc_inspections.
-- Enables sub-millisecond vendor failure rate queries.

CREATE TABLE IF NOT EXISTS vendor_failure_rates_mv_state
(
    platform_spec  LowCardinality(String),
    vendor_id      LowCardinality(String),
    error_category LowCardinality(String),
    error_code     LowCardinality(String),
    month          Date,
    total_count    AggregateFunction(count),
    fail_count     AggregateFunction(countIf),
    avg_cost       AggregateFunction(avg, Float32),
    total_cost     AggregateFunction(sum, Float32)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(month)
ORDER BY (platform_spec, vendor_id, error_category, error_code, month);

CREATE MATERIALIZED VIEW IF NOT EXISTS vendor_failure_rates_mv
TO vendor_failure_rates_mv_state
AS
SELECT
    platform_spec,
    vendor_id,
    error_category,
    error_code,
    toStartOfMonth(inspected_at) AS month,
    countState()                  AS total_count,
    countIfState(result = 'fail') AS fail_count,
    avgState(remediation_cost_usd) AS avg_cost,
    sumState(remediation_cost_usd) AS total_cost
FROM qc_inspections
GROUP BY platform_spec, vendor_id, error_category, error_code, month;

-- Query the materialized view:
-- SELECT
--     vendor_id,
--     error_code,
--     countMerge(total_count) AS inspections,
--     countIfMerge(fail_count) AS failures,
--     round(countIfMerge(fail_count) / countMerge(total_count), 3) AS fail_rate,
--     avgMerge(avg_cost) AS avg_remediation_cost
-- FROM vendor_failure_rates_mv_state
-- WHERE month >= toStartOfMonth(today() - INTERVAL 90 DAY)
-- GROUP BY vendor_id, error_code
-- ORDER BY fail_rate DESC;
