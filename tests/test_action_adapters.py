"""
Unit tests for Action Agent Extensible Downstream Adapters & Domain Events.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agents.action.adapters import (
    BaseAdapter,
    DownstreamDispatcher,
    JiraAdapter,
    SlackAdapter,
    WebhookAdapter,
)
from agents.action.agent import file_redelivery
from agents.shared.models import (
    AdapterResult,
    AdapterStatus,
    OrchestratorDecision,
    QCError,
    QCResult,
    RedeliveryDecision,
    RedeliveryFiledEvent,
    RiskAssessment,
)


# Note: The fixtures below are synthetic test doubles used exclusively for testing
# adapter serialization and dispatch lifecycle. They are not part of the Photon-harvested
# error taxonomy in data/photon_harvest/.

@pytest.fixture
def sample_event() -> RedeliveryFiledEvent:
    return RedeliveryFiledEvent(
        tracking_id=uuid4(),
        title_id="NFLX_TEST_001",
        title_name="The Test Film",
        vendor_id="VND_ROUNDABOUT",
        platform_spec="netflix-imf-2.1",
        decision="redeliver",
        decision_rationale="Blocking audio loudness violation detected.",
        blocking_findings=[
            {
                "error_code": "IMF_AUDIO_LOUDNESS_ERROR",
                "error_message": "Integrated loudness -31.2 LKFS exceeds -24.0 LKFS",
            }
        ],
        remediation_instructions="Normalize dialogue to -27 LKFS target band.",
        estimated_cost_usd=7500.0,
        risk_score=0.82,
        release_window_impact="Remediation cost est. $7,500.00",
    )


@pytest.fixture
def sample_qc_result() -> QCResult:
    return QCResult(
        title_id="NFLX_TEST_001",
        title_name="The Test Film",
        vendor_id="VND_ROUNDABOUT",
        platform_spec="netflix-imf-2.1",
        codec="ProRes",
        hdr_format="SDR",
        audio_config="5.1",
        errors=[
            QCError(
                error_code="IMF_AUDIO_LOUDNESS_ERROR",
                error_category="audio",
                error_message="Integrated loudness -31.2 LKFS exceeds maximum -24.0 LKFS",
                stage="auto-qc",
            )
        ],
    )


@pytest.fixture
def sample_decision() -> OrchestratorDecision:
    return OrchestratorDecision(
        inspection_id=uuid4(),
        title_id="NFLX_TEST_001",
        vendor_id="VND_ROUNDABOUT",
        decision=RedeliveryDecision.REDELIVER,
        decision_rationale="Blocking audio loudness violation detected.",
        spec_section="Section 4.1",
        spec_requirement="Audio integrated loudness must be -24 LKFS (+/- 1 LU)",
        risk_score=0.82,
        estimated_cost_usd=7500.0,
        remediation_instructions="Normalize dialogue to -27 LKFS target band.",
    )


@pytest.fixture
def sample_assessment() -> RiskAssessment:
    return RiskAssessment(
        inspection_id=uuid4(),
        title_id="NFLX_TEST_001",
        vendor_id="VND_ROUNDABOUT",
        risk_score=0.82,
        vendor_historical_fail_rate=0.34,
        predicted_failure_codes=["IMF_AUDIO_LOUDNESS_ERROR"],
        estimated_remediation_cost_usd=7500.0,
    )


# ── Webhook Adapter Tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_adapter_dry_run_when_unset(sample_event: RedeliveryFiledEvent):
    adapter = WebhookAdapter(url="")
    result = await adapter.handle(sample_event)
    assert result.status == AdapterStatus.DRY_RUN
    assert result.adapter_name == "webhook"
    assert "No HTTP call made" in result.message


@pytest.mark.asyncio
async def test_webhook_adapter_dispatches_when_url_set(sample_event: RedeliveryFiledEvent):
    adapter = WebhookAdapter(url="https://hooks.example.com/test", enabled=True)

    with patch("httpx.AsyncClient.post") as mock_post:
        from unittest.mock import MagicMock
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = await adapter.handle(sample_event)

        assert result.status == AdapterStatus.SUCCESS
        assert result.adapter_name == "webhook"
        assert mock_post.called
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"]["title_name"] == "The Test Film"


@pytest.mark.asyncio
async def test_webhook_adapter_handles_timeout_safely(sample_event: RedeliveryFiledEvent):
    adapter = WebhookAdapter(url="https://hooks.example.com/slow", enabled=True, timeout_seconds=0.1)

    with patch("httpx.AsyncClient.post", side_effect=asyncio.TimeoutError("Request timed out")):
        result = await adapter.handle(sample_event)
        assert result.status == AdapterStatus.FAILED
        assert "Request timed out" in result.message


# ── Jira Adapter Tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jira_adapter_dry_run_payload_format(sample_event: RedeliveryFiledEvent):
    adapter = JiraAdapter(api_url="", api_token="")
    result = await adapter.handle(sample_event)

    assert result.status == AdapterStatus.DRY_RUN
    assert result.adapter_name == "jira"
    fields = result.payload["fields"]
    assert fields["issuetype"]["name"] == "Bug"
    assert fields["priority"]["name"] == "Blocker"
    assert "The Test Film" in fields["summary"]
    assert "imf-qc" in fields["labels"]


# ── Slack Adapter Tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slack_adapter_dry_run_payload_format(sample_event: RedeliveryFiledEvent):
    adapter = SlackAdapter(webhook_url="")
    result = await adapter.handle(sample_event)

    assert result.status == AdapterStatus.DRY_RUN
    assert result.adapter_name == "slack"
    assert "blocks" in result.payload
    header_text = result.payload["blocks"][0]["text"]["text"]
    assert "QC Verdict: REDELIVER" in header_text


# ── Downstream Dispatcher Tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatcher_executes_all_adapters(sample_event: RedeliveryFiledEvent):
    dispatcher = DownstreamDispatcher()
    results = await dispatcher.dispatch(sample_event)

    assert len(results) == 3
    names = {r.adapter_name for r in results}
    assert names == {"webhook", "jira", "slack"}
    for r in results:
        assert r.status in (AdapterStatus.DRY_RUN, AdapterStatus.SUCCESS)


@pytest.mark.asyncio
async def test_dispatcher_error_isolation(sample_event: RedeliveryFiledEvent):
    class CrashingAdapter(BaseAdapter):
        @property
        def name(self) -> str:
            return "crasher"

        async def handle(self, event: RedeliveryFiledEvent) -> AdapterResult:
            raise RuntimeError("Catastrophic downstream failure")

    dispatcher = DownstreamDispatcher(adapters=[WebhookAdapter(url=""), CrashingAdapter()])
    results = await dispatcher.dispatch(sample_event)

    assert len(results) == 2
    crasher_res = next(r for r in results if r.adapter_name == "crasher")
    assert crasher_res.status == AdapterStatus.FAILED
    assert "Catastrophic downstream failure" in crasher_res.message


# ── Audit-First Guarantee Test ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_failure_blocks_downstream_dispatch(
    sample_qc_result: QCResult,
    sample_decision: OrchestratorDecision,
    sample_assessment: RiskAssessment,
):
    with patch("agents.action.agent.write_client") as mock_write:
        mock_client = AsyncMock()
        mock_client.execute.side_effect = ConnectionError("ClickHouse unavailable")
        mock_write.return_value = mock_client

        with patch("agents.action.agent.DownstreamDispatcher.dispatch") as mock_dispatch:
            with pytest.raises(ConnectionError, match="ClickHouse unavailable"):
                await file_redelivery(sample_qc_result, sample_decision, sample_assessment)

            # Downstream dispatch MUST NEVER be called if audit write failed
            assert not mock_dispatch.called


# ── Single-Dispatch Guarantee Tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_file_redelivery_dispatches_exactly_once(
    sample_qc_result: QCResult,
    sample_decision: OrchestratorDecision,
    sample_assessment: RiskAssessment,
):
    with patch("agents.action.agent.write_client") as mock_write:
        mock_client = AsyncMock()
        mock_write.return_value = mock_client

        with patch("agents.action.agent.DownstreamDispatcher.dispatch") as mock_dispatch:
            mock_dispatch.return_value = [
                AdapterResult(adapter_name="webhook", status=AdapterStatus.DRY_RUN, message="ok")
            ]

            record = await file_redelivery(sample_qc_result, sample_decision, sample_assessment)

            assert record is not None
            assert mock_dispatch.call_count == 1


@pytest.mark.asyncio
async def test_file_redelivery_with_events_dispatches_exactly_once(
    sample_qc_result: QCResult,
    sample_decision: OrchestratorDecision,
    sample_assessment: RiskAssessment,
):
    from agents.action.agent import file_redelivery_with_events

    with patch("agents.action.agent.write_client") as mock_write:
        mock_client = AsyncMock()
        mock_write.return_value = mock_client

        with patch("agents.action.agent.DownstreamDispatcher.dispatch") as mock_dispatch:
            expected_results = [
                AdapterResult(adapter_name="webhook", status=AdapterStatus.DRY_RUN, message="ok"),
                AdapterResult(adapter_name="jira", status=AdapterStatus.DRY_RUN, message="ok"),
            ]
            mock_dispatch.return_value = expected_results

            record, adapter_results = await file_redelivery_with_events(
                sample_qc_result, sample_decision, sample_assessment
            )

            assert record is not None
            assert adapter_results == expected_results
            # MUST dispatch exactly once — not twice!
            assert mock_dispatch.call_count == 1

