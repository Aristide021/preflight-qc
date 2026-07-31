"""
Shared Pydantic models for the QC agent system.

These are the typed contracts between agents. Every inter-agent handoff
uses one of these models — no raw dicts crossing agent boundaries.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class FailureSeverity(str, Enum):
    BLOCKING = "blocking"
    COSMETIC  = "cosmetic"
    ADVISORY  = "advisory"


class InspectionResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


class RedeliveryDecision(str, Enum):
    REDELIVER   = "redeliver"
    WAIVE       = "waive"
    ESCALATE    = "escalate"
    INVESTIGATE = "investigate"


class RedeliveryStatus(str, Enum):
    FILED       = "filed"
    IN_PROGRESS = "in_progress"
    RESUBMITTED = "resubmitted"
    RESOLVED    = "resolved"
    CANCELLED   = "cancelled"


# ─────────────────────────────────────────────────────────────────────────────
# Input: QC Result (what comes in from the platform or Photon)
# ─────────────────────────────────────────────────────────────────────────────

class QCError(BaseModel):
    """A single error/warning from a QC inspection."""
    error_code: str                  = Field(..., description="e.g. IMF_CPL_ERROR")
    error_category: str              = Field(..., description="structural | audio | essence | ...")
    error_message: str               = Field(..., description="Human-readable error description")
    error_context: dict[str, Any]    = Field(default_factory=dict, description="Extra context (timecode, track ID, etc.)")
    stage: str                       = Field("auto-qc", description="QC stage that produced this error")


class QCResult(BaseModel):
    """
    The input to the agent system — a QC inspection result for a single
    package delivery. Produced by Photon or the platform's QC pipeline.
    """
    inspection_id: UUID              = Field(default_factory=uuid4)
    title_id: str                    = Field(..., description="Platform title ID (e.g. NFLX_12345)")
    title_name: str                  = Field(default="")
    vendor_id: str                   = Field(..., description="Delivering vendor ID")
    platform_spec: str               = Field(..., description="e.g. netflix-imf-2.1")
    spec_version: str                = Field(default="")
    package_type: str                = Field(default="IMF")
    codec: str                       = Field(default="JPEG2000")
    hdr_format: str                  = Field(default="SDR")
    audio_config: str                = Field(default="5.1")
    redelivery_attempt: int          = Field(default=0, ge=0)
    submitted_at: datetime           = Field(default_factory=datetime.utcnow)
    errors: list[QCError]            = Field(default_factory=list)
    raw_output: str                  = Field(default="", description="Raw Photon or QC tool output")


# ─────────────────────────────────────────────────────────────────────────────
# Spec-Reader output: classified failures
# ─────────────────────────────────────────────────────────────────────────────

class ClassifiedFailure(BaseModel):
    """A QC error classified against the delivery spec."""
    error_code: str
    error_category: str
    error_message: str
    severity: FailureSeverity
    spec_section: str                = Field(..., description="Spec section that defines this requirement")
    spec_requirement: str            = Field(..., description="Exact spec language (quoted)")
    remediation_hint: str            = Field(default="", description="What needs to change to pass")
    is_waivable: bool                = Field(default=False, description="Can this be waived for cosmetic issues?")


class SpecClassification(BaseModel):
    """
    Output of the Spec-Reader agent.
    Maps every QC error to a spec citation and severity classification.
    """
    inspection_id: UUID
    title_id: str
    vendor_id: str
    platform_spec: str
    classified_at: datetime          = Field(default_factory=datetime.utcnow)
    failures: list[ClassifiedFailure]= Field(default_factory=list)
    blocking_count: int              = Field(default=0)
    cosmetic_count: int              = Field(default=0)
    advisory_count: int              = Field(default=0)
    has_blocking_failures: bool      = Field(default=False)
    spec_version_used: str           = Field(default="")
    spec_url: str                    = Field(default="")
    agent_reasoning: str             = Field(default="", description="Agent's reasoning trace")

    def model_post_init(self, __context: Any) -> None:
        self.blocking_count  = sum(1 for f in self.failures if f.severity == FailureSeverity.BLOCKING)
        self.cosmetic_count  = sum(1 for f in self.failures if f.severity == FailureSeverity.COSMETIC)
        self.advisory_count  = sum(1 for f in self.failures if f.severity == FailureSeverity.ADVISORY)
        self.has_blocking_failures = self.blocking_count > 0


# ─────────────────────────────────────────────────────────────────────────────
# QC-Analyst output: historical risk assessment
# ─────────────────────────────────────────────────────────────────────────────

class HistoricalInsight(BaseModel):
    """A single insight from the historical ClickHouse query."""
    query_type: str                  = Field(..., description="vendor_fail_rate | codec_cluster | cost_trend | ...")
    finding: str                     = Field(..., description="Human-readable finding (agent writes this)")
    data: dict[str, Any]             = Field(default_factory=dict, description="Raw query result data")
    clickhouse_query: str            = Field(default="", description="The SQL that produced this insight")


class RiskAssessment(BaseModel):
    """
    Output of the QC-Analyst agent.
    Historical context + risk score for an incoming delivery.
    This is the differentiating output — it's what 'checker' tools can't produce.
    """
    inspection_id: UUID
    title_id: str
    vendor_id: str
    assessed_at: datetime            = Field(default_factory=datetime.utcnow)

    # The risk score: 0.0 (very safe) → 1.0 (very likely to fail)
    risk_score: float                = Field(..., ge=0.0, le=1.0)
    risk_label: str                  = Field(default="")  # low | medium | high | critical

    # Historical context
    vendor_historical_fail_rate: float = Field(default=0.0)
    vendor_total_submissions: int      = Field(default=0)
    predicted_failure_codes: list[str] = Field(default_factory=list)
    avg_redelivery_attempts: float     = Field(default=0.0)
    estimated_remediation_cost_usd: float = Field(default=0.0)

    # The ClickHouse insights that drove the score
    insights: list[HistoricalInsight] = Field(default_factory=list)
    agent_reasoning: str              = Field(default="")

    def model_post_init(self, __context: Any) -> None:
        if self.risk_score < 0.25:
            self.risk_label = "low"
        elif self.risk_score < 0.50:
            self.risk_label = "medium"
        elif self.risk_score < 0.75:
            self.risk_label = "high"
        else:
            self.risk_label = "critical"


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator output: filing decision
# ─────────────────────────────────────────────────────────────────────────────

class OrchestratorDecision(BaseModel):
    """
    The Orchestrator's decision, synthesizing Spec-Reader + QC-Analyst outputs.
    This drives what the Action agent does next.
    """
    inspection_id: UUID
    title_id: str
    vendor_id: str
    decided_at: datetime             = Field(default_factory=datetime.utcnow)
    decision: RedeliveryDecision
    decision_rationale: str          = Field(..., description="Full reasoning with spec citations")
    spec_section: str                = Field(default="")
    spec_requirement: str            = Field(default="")
    risk_score: float                = Field(default=0.0)
    estimated_cost_usd: float        = Field(default=0.0)
    remediation_instructions: str    = Field(default="", description="Specific fix instructions for vendor")


# ─────────────────────────────────────────────────────────────────────────────
# Action agent output: tracking record (written to ClickHouse)
# ─────────────────────────────────────────────────────────────────────────────

class RedeliveryRecord(BaseModel):
    """
    Written to ClickHouse redelivery_tracking table by the Action agent.
    This is the audit trail that closes the loop.
    """
    tracking_id: UUID                = Field(default_factory=uuid4)
    title_id: str
    original_inspection: UUID
    package_id: UUID                 = Field(default_factory=uuid4)
    vendor_id: str
    platform_spec: str
    filed_at: datetime               = Field(default_factory=datetime.utcnow)
    filed_by_agent: str              = Field(default="orchestrator-v1")
    decision: str
    decision_rationale: str
    status: RedeliveryStatus         = Field(default=RedeliveryStatus.FILED)
    attempt_number: int              = Field(default=0, ge=0)
    estimated_cost_usd: float        = Field(default=0.0)
    resolved_at: datetime | None     = Field(default=None)
    spec_section: str                = Field(default="")
    spec_requirement: str            = Field(default="")
    risk_score: float                = Field(default=0.0)
    predicted_fail_codes: list[str]  = Field(default_factory=list)
