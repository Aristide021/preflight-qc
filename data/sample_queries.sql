-- ─────────────────────────────────────────────────────────────────────────────
-- Top-10 Analytical Queries — validated against local ClickHouse before
-- touching the paid ClickHouse Cloud cluster.
--
-- These are the exact queries the QC-Analyst agent tools will issue.
-- Validate: each should run in <2 seconds on the 10M+ row local corpus.
--
-- Run with timing:
--   clickhouse-client --time --query "$(cat data/sample_queries.sql)"
-- ─────────────────────────────────────────────────────────────────────────────

USE preflight;

-- ── Q1: Vendor failure rate by error code (last 90 days) ─────────────────────
-- Used by: QC-Analyst → risk scoring → "this vendor has 62% loudness fail rate"
SELECT
    vendor_id,
    error_code,
    error_category,
    count()                                        AS total_inspections,
    countIf(result = 'fail')                       AS failures,
    round(countIf(result = 'fail') / count(), 3)   AS fail_rate,
    round(avg(remediation_cost_usd), 2)            AS avg_remediation_cost_usd,
    round(sum(remediation_cost_usd), 0)            AS total_cost_usd
FROM qc_inspections
WHERE
    inspected_at >= now() - INTERVAL 90 DAY
    AND error_code != ''
GROUP BY vendor_id, error_code, error_category
ORDER BY fail_rate DESC
LIMIT 50;

-- ── Q2: Redelivery attempt distribution by error category ────────────────────
-- Used by: QC-Analyst → "structural errors take 3.2 attempts to resolve on avg"
SELECT
    error_category,
    error_severity,
    count()                       AS total_failures,
    round(avg(redelivery_attempt), 1) AS avg_attempts_to_resolve,
    max(redelivery_attempt)       AS max_attempts,
    countIf(resolved)             AS resolved_count,
    round(countIf(resolved) / count(), 3) AS resolution_rate
FROM qc_inspections
WHERE result = 'fail'
GROUP BY error_category, error_severity
ORDER BY avg_attempts_to_resolve DESC;

-- ── Q3: Risk score for an incoming delivery ───────────────────────────────────
-- Used by: QC-Analyst → pre-flight risk scoring for a new delivery
-- Parameters: :vendor_id, :platform_spec, :codec, :hdr_format
SELECT
    vendor_id,
    platform_spec,
    codec,
    hdr_format,
    count()                                                    AS historical_submissions,
    countIf(result = 'fail')                                   AS historical_failures,
    round(countIf(result = 'fail') / count(), 3)               AS historical_fail_rate,
    round(avg(redelivery_attempt), 1)                          AS avg_redelivery_attempts,
    round(sum(remediation_cost_usd), 0)                        AS total_cost_to_date_usd,
    -- Top 3 most common error codes (packed into a string for agent consumption)
    arrayStringConcat(
        arraySlice(
            arraySort(
                x -> -x.2,
                groupArray((error_code, countIf(result = 'fail')))
            ),
            1, 3
        ),
        ', '
    ) AS top_3_failure_codes
FROM qc_inspections
WHERE
    vendor_id = {vendor_id:String}
    AND platform_spec = {platform_spec:String}
    AND codec = {codec:String}
    AND inspected_at >= now() - INTERVAL 365 DAY
GROUP BY vendor_id, platform_spec, codec, hdr_format
ORDER BY historical_fail_rate DESC
LIMIT 1;

-- ── Q4: Error code clustering by codec and HDR format ────────────────────────
-- Used by: QC-Analyst → "Dolby Vision + H.265 clusters with these 4 codes"
SELECT
    codec,
    hdr_format,
    error_code,
    count()                                     AS occurrences,
    round(count() / sum(count()) OVER (
        PARTITION BY codec, hdr_format
    ), 3)                                       AS share_within_codec_hdr
FROM qc_inspections
WHERE result = 'fail' AND error_code != ''
GROUP BY codec, hdr_format, error_code
HAVING occurrences > 100
ORDER BY codec, hdr_format, occurrences DESC;

-- ── Q5: Failure trend by vendor over time ────────────────────────────────────
-- Used by: Dashboard → "failure trend by vendor" chart
SELECT
    vendor_id,
    toStartOfWeek(inspected_at)                AS week,
    count()                                    AS total_inspections,
    countIf(result = 'fail')                   AS failures,
    round(countIf(result = 'fail') / count(), 3) AS fail_rate
FROM qc_inspections
WHERE inspected_at >= now() - INTERVAL 52 WEEK
GROUP BY vendor_id, week
ORDER BY vendor_id, week;

-- ── Q6: Cost-per-failure by error category and platform ──────────────────────
-- Used by: Dashboard → "cost-per-failure business view"
SELECT
    platform_spec,
    error_category,
    error_severity,
    count()                                    AS failure_count,
    round(sum(remediation_cost_usd), 0)        AS total_cost_usd,
    round(avg(remediation_cost_usd), 2)        AS avg_cost_per_failure_usd,
    round(median(remediation_cost_usd), 2)     AS median_cost_usd,
    round(quantile(0.95)(remediation_cost_usd), 2) AS p95_cost_usd
FROM qc_inspections
WHERE result = 'fail' AND remediation_cost_usd > 0
GROUP BY platform_spec, error_category, error_severity
ORDER BY total_cost_usd DESC;

-- ── Q7: Title history — full redelivery chain ─────────────────────────────────
-- Used by: Agent → "show me all attempts for NFLX_12345"
SELECT
    title_id,
    title_name,
    vendor_id,
    redelivery_attempt,
    inspected_at,
    result,
    error_code,
    error_severity,
    error_message,
    resolved
FROM qc_inspections
WHERE title_id = {title_id:String}
ORDER BY redelivery_attempt ASC, inspected_at ASC;

-- ── Q8: Predictive — which error codes appear first in a redelivery chain ─────
-- Used by: Pre-flight predictor → "first failure → predicts these follow-on failures"
SELECT
    error_code AS first_error,
    nextError,
    count()    AS co_occurrence_count
FROM (
    SELECT
        title_id,
        error_code,
        leadInFrame(error_code) OVER (
            PARTITION BY title_id
            ORDER BY redelivery_attempt
        ) AS nextError
    FROM qc_inspections
    WHERE result = 'fail' AND error_code != ''
)
WHERE nextError != '' AND first_error != nextError
GROUP BY first_error, nextError
ORDER BY co_occurrence_count DESC
LIMIT 30;

-- ── Q9: Spec version adoption + failure rate shift ────────────────────────────
-- Used by: QC-Analyst → "failure rate dropped 18% after spec v2.3 rollout"
SELECT
    spec_version,
    min(inspected_at)                          AS first_seen,
    count()                                    AS total_inspections,
    countIf(result = 'fail')                   AS failures,
    round(countIf(result = 'fail') / count(), 3) AS fail_rate,
    round(avg(remediation_cost_usd), 2)        AS avg_cost_usd
FROM qc_inspections
GROUP BY spec_version
ORDER BY first_seen;

-- ── Q10: Fast aggregate for dashboard KPI tiles (sub-second expected) ─────────
-- Used by: Dashboard header → "today's numbers at a glance"
SELECT
    countIf(result = 'pass')                                  AS passes_today,
    countIf(result = 'fail')                                  AS failures_today,
    countIf(result = 'fail' AND error_severity = 'blocking')  AS blocking_failures_today,
    round(sum(if(result='fail', remediation_cost_usd, 0)), 0) AS cost_at_risk_usd,
    count(DISTINCT title_id)                                  AS titles_in_flight,
    count(DISTINCT vendor_id)                                 AS active_vendors
FROM qc_inspections
WHERE toDate(inspected_at) = today();
