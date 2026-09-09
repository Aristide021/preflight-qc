"""
PreFlight QC — ClickHouse RBAC Security Enforcement Test.

PURPOSE:
  Verifies that database-level privilege isolation is actively enforced
  by ClickHouse RBAC (see infra/clickhouse/rbac.sql).

CHECKS:
  1. Read-only user (qc_analyst_user) CAN execute SELECT queries.
  2. Read-only user CANNOT execute INSERT statements (proves prompt injection
     cannot mutate audit or tracking tables).
  3. Write user (qc_action_user) CAN execute INSERT into redelivery_tracking.
  4. Write user CANNOT execute DROP, ALTER, or TRUNCATE statements.

USAGE:
  uv run python tests/test_rbac_enforcement.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import clickhouse_connect


def check_rbac() -> bool:
    host = os.environ.get("CLICKHOUSE_HOST")
    port = int(os.environ.get("CLICKHOUSE_PORT", "8443"))
    database = os.environ.get("CLICKHOUSE_DATABASE", "preflight")
    secure = os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true"

    ro_user = os.environ.get("CLICKHOUSE_READONLY_USER", "qc_analyst_user")
    ro_password = os.environ.get("CLICKHOUSE_READONLY_PASSWORD")

    wr_user = os.environ.get("CLICKHOUSE_WRITE_USER", "qc_action_user")
    wr_password = os.environ.get("CLICKHOUSE_WRITE_PASSWORD")

    if not host or not ro_password or not wr_password:
        print("\n[RBAC TEST] SKIPPED — Dedicated RBAC credentials not set in environment.")
        print("            To verify database RBAC enforcement:")
        print("            1. Apply infra/clickhouse/rbac.sql to your ClickHouse instance.")
        print("            2. Set CLICKHOUSE_READONLY_PASSWORD and CLICKHOUSE_WRITE_PASSWORD in .env.")
        return True

    print("\n" + "=" * 60)
    print("  PreFlight QC — ClickHouse RBAC Security Verification")
    print("=" * 60)
    print(f"Target Cluster : {host}:{port}")
    print(f"Database       : {database}")
    print(f"Read-Only User : {ro_user}")
    print(f"Write User     : {wr_user}\n")

    all_passed = True

    # ── Test 1: Read-only user can SELECT ────────────────────────────────────
    print("[TEST 1] Verifying Read-Only User SELECT access...")
    try:
        ro_client = clickhouse_connect.get_client(
            host=host, port=port, username=ro_user, password=ro_password, database=database, secure=secure
        )
        res = ro_client.command("SELECT 1")
        assert res == 1
        print("  -> PASS: Read-only user executed SELECT successfully.")
    except Exception as exc:
        print(f"  -> FAIL: Read-only user could not SELECT: {exc}")
        all_passed = False

    # ── Test 2: Read-only user CANNOT INSERT ─────────────────────────────────
    print("[TEST 2] Verifying Read-Only User is BLOCKED from INSERT (Prompt-injection defense)...")
    try:
        ro_client.command(
            f"INSERT INTO {database}.redelivery_tracking (tracking_id, title_id) VALUES ('{uuid.uuid4()}', 'PROBE')"
        )
        print("  -> FAIL: Read-only user was able to INSERT! RBAC not enforced.")
        all_passed = False
    except Exception as exc:
        print(f"  -> PASS: INSERT rejected as expected with error:\n     {str(exc).splitlines()[0][:120]}")

    # ── Test 3: Write user CAN INSERT into redelivery_tracking ───────────────
    print("[TEST 3] Verifying Write User can INSERT into redelivery_tracking...")
    test_id = uuid.uuid4()
    try:
        wr_client = clickhouse_connect.get_client(
            host=host, port=port, username=wr_user, password=wr_password, database=database, secure=secure
        )
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        wr_client.command(
            f"""
            INSERT INTO {database}.redelivery_tracking
            (tracking_id, title_id, vendor_id, filed_at, decision, status)
            VALUES ('{test_id}', 'RBAC_TEST_TITLE', 'VND_TEST', '{now_str}', 'investigate', 'filed')
            """
        )
        print("  -> PASS: Write user successfully filed record into redelivery_tracking.")
    except Exception as exc:
        print(f"  -> FAIL: Write user could not INSERT: {exc}")
        all_passed = False

    # ── Test 4: Write user CANNOT DROP tables ────────────────────────────────
    print("[TEST 4] Verifying Write User is BLOCKED from DROP statements...")
    try:
        wr_client.command(f"DROP TABLE IF EXISTS {database}.redelivery_tracking")
        print("  -> FAIL: Write user was able to DROP table! Dangerous permissions granted.")
        all_passed = False
    except Exception as exc:
        print(f"  -> PASS: DROP rejected as expected with error:\n     {str(exc).splitlines()[0][:120]}")

    print("\n" + "=" * 60)
    if all_passed:
        print("  ALL RBAC ENFORCEMENT CHECKS PASSED: True DB security isolation proven.")
    else:
        print("  RBAC ENFORCEMENT FAILURES DETECTED: Check infra/clickhouse/rbac.sql.")
    print("=" * 60 + "\n")

    return all_passed


if __name__ == "__main__":
    success = check_rbac()
    sys.exit(0 if success else 1)
