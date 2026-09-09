"""PreFlight QC Web Server & Dashboard API.

The judge-facing application runs only the live Gemini + ClickHouse MCP path.
There is no fallback response that could be mistaken for a real analysis.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# Resilient agent imports
AGENTS_AVAILABLE = False
_import_error_message = ""
try:
    from agents.orchestrator.agent import run_full_pipeline
    from agents.shared.models import QCResult, RedeliveryDecision

    AGENTS_AVAILABLE = True
except Exception as _exc:
    _import_error_message = str(_exc)


ROOT = Path(__file__).resolve().parent
SAMPLE = PROJECT_ROOT / "data" / "sample_qc_result.json"
SAMPLE_PHOTON = PROJECT_ROOT / "data" / "sample_photon_vector.json"
SAMPLE_SUPPLEMENTAL = PROJECT_ROOT / "data" / "sample_supplemental_integrity.json"
PORT = int(os.environ.get("PORT", "8080"))

ISSUE_GUIDANCE = {
    "IMF_MASTER_PACKAGE_ERROR": {
        "label": "Referenced Track Availability",
        "summary": "A track file referenced by the Composition Playlist is unavailable for validation.",
        "fix": "Verify that the referenced track exists on the ingest volume or that the parent Base IMP asset is mounted and correctly linked via the AssetMap.",
        "impact": "The composition cannot be validated or played because a referenced essence track cannot be resolved.",
        "spec_requirement": "All track files referenced by a Composition Playlist must be available and resolvable via the package AssetMap (SMPTE ST 2067-2).",
    },
    "IMF_PKL_ERROR": {
        "label": "Package manifest & assets",
        "summary": "The Packing List (PKL) references essential files that are missing from the package or omitted from the AssetMap.",
        "fix": "Restore or regenerate the missing video and OPL asset files, update the AssetMap/PKL references with matching UUIDs, then rerun Photon validation.",
        "impact": "Ingest validation fails because essential essence or metadata files referenced in the manifest cannot be located.",
        "spec_requirement": "Photon detected missing assets referenced by the package manifest (SMPTE ST 2067-2 Packing List requirements).",
    },
    "APPLICATION_COMPOSITION_ERROR": {
        "label": "IMF Composition Profile",
        "summary": "Essence descriptor violates SMPTE ST 2067 / Application profile constraints.",
        "fix": "Correct the video line map and ensure ACES subdescriptors are homogeneous in the CPL, then rerun Photon validation.",
        "impact": "The IMF package cannot be played back or processed by ingest rendering nodes.",
        "spec_requirement": "Photon detected essence descriptor mismatch against SMPTE ST 2067-50 IMF Application #5 specification.",
    },
    "IMF_CPL_ERROR": {
        "label": "Package structure",
        "summary": "The package is missing a required title-structure identifier.",
        "fix": "Regenerate the Composition Playlist with a valid ApplicationIdentification element, then rerun the IMF validator.",
        "impact": "The platform cannot reliably determine which IMF application profile the package uses.",
        "spec_requirement": "Composition Playlist must declare a supported IMF Application Identification (SMPTE ST 2067-2).",
    },
    "IMF_CORE_CONSTRAINTS_ERROR": {
        "label": "Package structure",
        "summary": "The package points to an older Core Constraints namespace.",
        "fix": "Regenerate the CPL and Core Constraints files against the required 2016 namespace, then rerun package validation.",
        "impact": "The package structure may not match the platform profile expected at ingest.",
        "spec_requirement": "Core Constraints namespace must be http://www.smpte-ra.org/schemas/2067-2/2016.",
    },
    "AUDIO_LOUDNESS_OUT_OF_SPEC": {
        "label": "Audio loudness",
        "summary": "The program audio is outside the delivery loudness and peak limits.",
        "fix": "Remix or normalize the audio to the delivery target, verify true peak, and rerun the audio QC pass.",
        "impact": "Viewers may experience inconsistent playback volume or clipping, and the delivery may be rejected.",
        "spec_requirement": "Audio loudness must measure -27 LKFS +/- 2 LKFS dialog-gated per ITU-R BS.1770-1.",
    },
    "DOLBY_VISION_RPU_MISSING": {
        "label": "HDR metadata",
        "summary": "Dolby Vision is declared, but the required dynamic metadata is missing from the video essence.",
        "fix": "Attach the correct Dolby Vision RPU metadata to the video essence, then rerun the HDR validation.",
        "impact": "The platform may be unable to reproduce the intended Dolby Vision presentation.",
        "spec_requirement": "When Dolby Vision is declared, dynamic RPU metadata must accompany the essence track.",
    },
}


def check_live_prerequisites() -> tuple[bool, str]:
    """Check if prerequisites for running live Gemini + ClickHouse exist."""
    if not AGENTS_AVAILABLE:
        return False, f"Agent dependencies not available ({_import_error_message})"
    vertex_configured = bool(
        os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    has_gemini = bool(
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or vertex_configured
    )
    if not has_gemini:
        return False, "Missing Vertex AI/ADC or Gemini API credentials"
    has_clickhouse = bool(os.environ.get("CLICKHOUSE_HOST"))
    if not has_clickhouse:
        return False, "Missing CLICKHOUSE_HOST configuration"
    return True, "Prerequisites satisfied"


def demo_analysis(qc: dict, reason: str = "ClickHouse MCP not connected") -> dict:
    """Deterministic offline fallback with clear data provenance disclosures."""
    errors = qc.get("errors", [])
    failures = []
    is_photon_vector = (
        qc.get("title_id") == "PHOTON_TEST_MISSING_FILES"
        or "Photon" in qc.get("title_name", "")
        or any(e.get("stage") == "photon" for e in errors)
    )

    for index, error in enumerate(errors):
        code = error.get("error_code", "UNKNOWN_ERROR")
        guidance = ISSUE_GUIDANCE.get(
            code,
            {
                "label": error.get("error_category", "Package check").replace("_", " ").title(),
                "summary": "The delivery contains a quality-control finding that needs attention.",
                "fix": "Resolve the finding described in the inspection details, then rerun the relevant QC check.",
                "impact": "This may prevent the package from being accepted at platform ingest.",
                "spec_requirement": "Requirement review pending live spec verification.",
            },
        )
        photon_level = error.get("error_context", {}).get("error_level", "")
        severity = (
            "blocking" if photon_level in {"FATAL", "NON_FATAL"}
            else "advisory" if photon_level == "WARNING"
            else "blocking" if index < 2 else "cosmetic"
        )
        failures.append(
            {
                "error_code": code,
                "error_category": error.get("error_category", "general"),
                "error_message": error.get("error_message", "No message provided"),
                "error_context": error.get("error_context", {}),
                "user_label": guidance["label"],
                "user_summary": guidance["summary"],
                "impact": guidance["impact"],
                "severity": severity,
                "spec_section": "SMPTE ST 2067-2 / Netflix IMF Delivery Requirements",
                "spec_requirement": guidance.get(
                    "spec_requirement",
                    "Photon detected missing assets referenced by the package manifest.",
                ),
                "remediation_hint": guidance["fix"],
                "is_waivable": severity not in {"blocking"},
            }
        )
    blocking = sum(item["severity"] == "blocking" for item in failures)

    source_badge = (
        "Real Photon validation result + illustrative demo history"
        if is_photon_vector
        else f"Demo analytics — {reason}"
    )

    if is_photon_vector:
        decision_rationale = "Fatal and non-fatal packaging manifest errors detected by Netflix Photon. Missing package assets must be restored before ingest can proceed."
        remediation = "Restore or regenerate the missing video and OPL assets, update AssetMap and PKL references with matching UUIDs, then rerun Photon validation."
    else:
        decision_rationale = "Blocking quality control findings require remediation prior to platform acceptance."
        remediation = "Correct the blocking findings, rerun verification, and submit a revised package."

    return {
        "mode": "demo",
        "source_label": source_badge,
        "qc_result": qc,
        "classification": {
            "failures": failures,
            "blocking_count": blocking,
            "cosmetic_count": len(failures) - blocking,
            "advisory_count": 0,
            "has_blocking_failures": blocking > 0,
            "spec_version_used": "SMPTE ST 2067-2 / Netflix IMF Spec Grounding",
        },
        "assessment": {
            "risk_score": 0.68 if failures else 0.12,
            "risk_label": "high" if failures else "low",
            "vendor_historical_fail_rate": 0.183,
            "vendor_total_submissions": 1240,
            "predicted_failure_codes": [item["error_code"] for item in failures[:3]],
            "avg_redelivery_attempts": 1.4,
            "estimated_remediation_cost_usd": 7800.0 if failures else 0.0,
            "agent_reasoning": (
                "Illustrative demo statistics — ClickHouse not connected. "
                "Demonstrates historical risk scoring pattern for IMF manifest errors."
            ),
            "analytics_label": "Illustrative vendor history — ClickHouse not connected",
        },
        "decision": {
            "decision": "redeliver" if blocking else "waive",
            "decision_rationale": decision_rationale,
            "spec_section": "SMPTE ST 2067-2 Packaging & Core Constraints",
            "spec_requirement": "All assets declared in the Packing List must resolve to valid files via the AssetMap.",
            "remediation_instructions": remediation,
        },
        "tracking_record": None,
    }


def execute_live(qc_data: dict) -> dict:
    """Execute live multi-agent pipeline."""
    qc_result = QCResult.model_validate(qc_data)
    result = asyncio.run(run_full_pipeline(qc_result))

    # Format classification failures with human-friendly UX labels
    failures = []
    for f in result["classification"].failures:
        code = f.error_code
        guidance = ISSUE_GUIDANCE.get(
            code,
            {
                "label": f.error_category.replace("_", " ").title(),
                "summary": f.error_message,
                "fix": f.remediation_hint or "Review spec requirements and rerun QC.",
                "impact": "This may affect acceptance at platform ingest.",
            },
        )
        f_dict = f.model_dump(mode="json")
        f_dict["user_label"] = guidance["label"]
        f_dict["user_summary"] = guidance["summary"]
        f_dict["impact"] = guidance["impact"]
        if not f_dict.get("remediation_hint"):
            f_dict["remediation_hint"] = guidance["fix"]
        failures.append(f_dict)

    class_dict = result["classification"].model_dump(mode="json")
    class_dict["failures"] = failures

    tracking = (
        result["tracking_record"].model_dump(mode="json")
        if result.get("tracking_record")
        else None
    )

    return {
        "mode": "live",
        "source_label": "Live: Gemini Flash + ClickHouse Cloud via MCP",
        "qc_result": result["qc_result"].model_dump(mode="json"),
        "classification": class_dict,
        "assessment": result["assessment"].model_dump(mode="json"),
        "decision": result["decision"].model_dump(mode="json"),
        "tracking_record": tracking,
    }


def process_qc(payload: dict, mode: str = "auto") -> dict:
    """
    Process a QC payload according to the requested mode.

    The public application accepts only the live path. The mode argument is
    retained for backwards-compatible callers but cannot select demo data.
    """
    mode = mode.lower().strip()
    if mode not in ("auto", "live"):
        raise ValueError("Only live execution is available in the judge-facing application")

    prereqs_ok, prereq_reason = check_live_prerequisites()

    if not prereqs_ok:
        raise RuntimeError(f"Cannot execute live mode: {prereq_reason}")
    return execute_live(payload)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/status":
            prereqs_ok, reason = check_live_prerequisites()
            res = {
                "live_available": prereqs_ok,
                "reason": reason,
                "display_status": "Live Ready (Gemini + ClickHouse MCP)" if prereqs_ok else "Live services unavailable",
                "default_mode": "live",
                "agents_available": AGENTS_AVAILABLE,
            }
            self._send(200, "application/json", json.dumps(res).encode())
            return

        if path == "/api/sample":
            mode = query.get("mode", ["auto"])[0]
            sample_type = query.get("type", ["multistage"])[0]
            if sample_type == "photon":
                sample_file = SAMPLE_PHOTON
            elif sample_type == "supplemental":
                sample_file = SAMPLE_SUPPLEMENTAL
            else:
                sample_file = SAMPLE
            try:
                sample_payload = json.loads(sample_file.read_text(encoding="utf-8"))
                result = process_qc(sample_payload, mode=mode)
                self._send(200, "application/json", json.dumps(result).encode())
            except Exception as exc:
                err_body = {
                    "error": str(exc),
                    "details": traceback.format_exc(),
                    "mode": mode,
                }
                self._send(500, "application/json", json.dumps(err_body).encode())
            return

        requested = ROOT / ("index.html" if path == "/" else path.lstrip("/"))
        if requested.is_file() and ROOT in requested.parents:
            content_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
            self._send(200, content_type, requested.read_bytes())
            return

        self._send(404, "text/plain", b"Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/analyze":
            self._send(404, "text/plain", b"Not found")
            return

        query = parse_qs(parsed.query)
        mode = query.get("mode", ["auto"])[0]

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("errors", []), list):
                raise ValueError("QC result must be a JSON object with an 'errors' array")

            # Allow payload to override mode if provided
            if "mode" in payload and isinstance(payload["mode"], str):
                mode = payload["mode"]

            result = process_qc(payload, mode=mode)
            self._send(200, "application/json", json.dumps(result).encode())
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, "application/json", json.dumps({"error": str(exc)}).encode())
        except Exception as exc:
            log_msg = f"Analysis error ({mode} mode): {exc}"
            print(f"[preflight-ui] {log_msg}")
            err_body = {
                "error": str(exc),
                "details": traceback.format_exc(),
                "mode": mode,
            }
            self._send(500, "application/json", json.dumps(err_body).encode())

    def log_message(self, format: str, *args: object) -> None:
        print(f"[preflight-ui] {format % args}")


if __name__ == "__main__":
    prereqs, reason = check_live_prerequisites()
    status_str = "LIVE READY" if prereqs else f"LIVE UNAVAILABLE ({reason})"
    print(f"PreFlight QC UI: http://localhost:{PORT} [{status_str}]")
    # Cloud Run routes traffic to the container network interface.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
