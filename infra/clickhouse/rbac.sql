-- ─────────────────────────────────────────────────────────────────────────────
-- PreFlight QC — ClickHouse Role-Based Access Control (RBAC) Setup
-- Enforces true infrastructure-level security isolation between agents.
-- Prevents prompt injection or rogue agents from mutating historical data.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Create Roles
CREATE ROLE IF NOT EXISTS qc_analytics_role;
CREATE ROLE IF NOT EXISTS qc_action_role;

-- 2. Grant Permissions to Analytics Role (Used by Spec-Reader and QC-Analyst)
-- Strictly read-only: SELECT on all tables in preflight database.
GRANT SELECT ON preflight.* TO qc_analytics_role;

-- 3. Grant Permissions to Action Role (Used exclusively by Action Agent)
-- Can read all tables, and INSERT only into redelivery_tracking.
-- Explicitly NO DROP, NO ALTER, NO TRUNCATE, NO DELETE.
GRANT SELECT ON preflight.* TO qc_action_role;
GRANT INSERT ON preflight.redelivery_tracking TO qc_action_role;

-- 4. Create Users for Each Agent Role
-- Note: Replace with strong, rotated passwords in production Secret Manager.
CREATE USER IF NOT EXISTS qc_analyst_user
    IDENTIFIED WITH sha256_password BY '${CLICKHOUSE_READONLY_PASSWORD}'
    DEFAULT ROLE qc_analytics_role;

CREATE USER IF NOT EXISTS qc_action_user
    IDENTIFIED WITH sha256_password BY '${CLICKHOUSE_WRITE_PASSWORD}'
    DEFAULT ROLE qc_action_role;

-- 5. Verification Queries (Run as qc_analyst_user to verify enforcement)
-- SELECT count() FROM preflight.qc_inspections;                      -- MUST SUCCEED
-- INSERT INTO preflight.redelivery_tracking (title_id) VALUES ('x'); -- MUST FAIL WITH ACCESS_DENIED
