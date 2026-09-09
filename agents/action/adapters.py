"""
Action Agent Downstream Adapters & Domain Event Dispatcher.

This module provides an extensible event-driven adapter architecture for
notifying downstream workflow systems after an audit record has been committed.

Execution guarantees:
  1. Downstream adapters ONLY run AFTER the ClickHouse audit record is successfully written.
  2. Optional adapters never raise exceptions into the main agent loop (error isolation).
  3. Active network calls have strict timeouts (5.0s).
  4. Unconfigured adapters run in safe, explicit DRY_RUN mode without making network requests.
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

from agents.shared.models import AdapterResult, AdapterStatus, RedeliveryFiledEvent

log = structlog.get_logger(__name__)


class BaseAdapter(ABC):
    """Abstract base class for all downstream workflow adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name identifying this adapter."""
        ...

    @abstractmethod
    async def handle(self, event: RedeliveryFiledEvent) -> AdapterResult:
        """Handle the domain event and return a structured AdapterResult."""
        ...


class WebhookAdapter(BaseAdapter):
    """
    Dispatches a standardized JSON payload to an external HTTP webhook.
    
    If PREFLIGHT_WEBHOOK_URL is not set or disabled, it returns a safe DRY_RUN result
    without executing any network requests.
    """

    def __init__(
        self,
        url: str | None = None,
        enabled: bool | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.url = url or os.environ.get("PREFLIGHT_WEBHOOK_URL", "")
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.environ.get("PREFLIGHT_WEBHOOK_ENABLED", "true").lower() in ("true", "1", "yes")
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "webhook"

    async def handle(self, event: RedeliveryFiledEvent) -> AdapterResult:
        payload = event.model_dump(mode="json")
        start_time = time.monotonic()

        if not self.url or not self.enabled:
            return AdapterResult(
                adapter_name=self.name,
                status=AdapterStatus.DRY_RUN,
                message="Dry-run: PREFLIGHT_WEBHOOK_URL is unset or disabled. No HTTP call made.",
                payload={"target_url": self.url, "event_type": event.event_type},
                duration_ms=round((time.monotonic() - start_time) * 1000, 2),
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.url,
                    json=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "PreFlight-QC/1.0"},
                )
                duration = round((time.monotonic() - start_time) * 1000, 2)
                response.raise_for_status()

                log.info(
                    "webhook_adapter.dispatched",
                    url=self.url,
                    status_code=response.status_code,
                    duration_ms=duration,
                )
                return AdapterResult(
                    adapter_name=self.name,
                    status=AdapterStatus.SUCCESS,
                    message=f"Webhook successfully delivered to {self.url} (HTTP {response.status_code})",
                    payload={"status_code": response.status_code},
                    duration_ms=duration,
                )
        except Exception as exc:
            duration = round((time.monotonic() - start_time) * 1000, 2)
            log.warning(
                "webhook_adapter.failed",
                url=self.url,
                error=str(exc),
                duration_ms=duration,
            )
            return AdapterResult(
                adapter_name=self.name,
                status=AdapterStatus.FAILED,
                message=f"Webhook delivery failed: {exc}",
                payload={"error": str(exc)},
                duration_ms=duration,
            )


class JiraAdapter(BaseAdapter):
    """
    Extension Point: Prepares a Jira REST API issue creation payload.
    
    Operates in explicit DRY_RUN mode unless JIRA_API_URL and JIRA_API_TOKEN are configured.
    """

    def __init__(
        self,
        api_url: str | None = None,
        api_token: str | None = None,
        project_key: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.api_url = api_url or os.environ.get("JIRA_API_URL", "")
        self.api_token = api_token or os.environ.get("JIRA_API_TOKEN", "")
        self.project_key = project_key or os.environ.get("JIRA_PROJECT_KEY", "QC")
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "jira"

    def format_issue_payload(self, event: RedeliveryFiledEvent) -> dict[str, Any]:
        """Generate standard Jira REST API issue schema."""
        priority = "Blocker" if event.decision.lower() == "redeliver" else "Major"
        return {
            "fields": {
                "project": {"key": self.project_key},
                "summary": f"[{event.decision.upper()}] QC Redelivery: {event.title_name} ({event.vendor_id})",
                "description": (
                    f"*Tracking ID:* {event.tracking_id}\n"
                    f"*Title:* {event.title_name} ({event.title_id})\n"
                    f"*Vendor:* {event.vendor_id}\n"
                    f"*Decision:* {event.decision.upper()}\n"
                    f"*Risk Score:* {event.risk_score:.2f}\n"
                    f"*Est. Remediation Cost:* ${event.estimated_cost_usd:,.2f}\n\n"
                    f"*Decision Rationale:*\n{event.decision_rationale}\n\n"
                    f"*Remediation Instructions:*\n{event.remediation_instructions}"
                ),
                "issuetype": {"name": "Bug"},
                "priority": {"name": priority},
                "labels": ["imf-qc", "preflight", f"vendor-{event.vendor_id.lower()}"],
            }
        }

    async def handle(self, event: RedeliveryFiledEvent) -> AdapterResult:
        start_time = time.monotonic()
        payload = self.format_issue_payload(event)

        if not self.api_url or not self.api_token:
            return AdapterResult(
                adapter_name=self.name,
                status=AdapterStatus.DRY_RUN,
                message="Extension point: Jira integration ready to connect. JIRA_API_URL/TOKEN not configured.",
                payload=payload,
                duration_ms=round((time.monotonic() - start_time) * 1000, 2),
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    f"{self.api_url.rstrip('/')}/rest/api/2/issue",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/json",
                    },
                )
                duration = round((time.monotonic() - start_time) * 1000, 2)
                resp.raise_for_status()
                return AdapterResult(
                    adapter_name=self.name,
                    status=AdapterStatus.SUCCESS,
                    message="Jira issue successfully created",
                    payload=resp.json() if resp.content else {},
                    duration_ms=duration,
                )
        except Exception as exc:
            duration = round((time.monotonic() - start_time) * 1000, 2)
            return AdapterResult(
                adapter_name=self.name,
                status=AdapterStatus.FAILED,
                message=f"Jira issue creation failed: {exc}",
                payload={"error": str(exc)},
                duration_ms=duration,
            )


class SlackAdapter(BaseAdapter):
    """
    Extension Point: Formats Slack Block Kit message with triage decision badges.
    
    Operates in explicit DRY_RUN mode unless SLACK_WEBHOOK_URL is configured.
    """

    def __init__(
        self,
        webhook_url: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "slack"

    def format_block_kit(self, event: RedeliveryFiledEvent) -> dict[str, Any]:
        """Generate formatted Slack Block Kit alert."""
        icon = "🛑" if event.decision.lower() == "redeliver" else "⚠️"
        return {
            "text": f"{icon} PreFlight QC: {event.decision.upper()} for {event.title_name}",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{icon} QC Verdict: {event.decision.upper()}",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Title:*\n{event.title_name}"},
                        {"type": "mrkdwn", "text": f"*Vendor:*\n{event.vendor_id}"},
                        {"type": "mrkdwn", "text": f"*Risk Score:*\n{event.risk_score:.2f}"},
                        {"type": "mrkdwn", "text": f"*Est. Cost:*\n${event.estimated_cost_usd:,.2f}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Remediation:*\n```{event.remediation_instructions[:500]}```",
                    },
                },
            ],
        }

    async def handle(self, event: RedeliveryFiledEvent) -> AdapterResult:
        start_time = time.monotonic()
        payload = self.format_block_kit(event)

        if not self.webhook_url:
            return AdapterResult(
                adapter_name=self.name,
                status=AdapterStatus.DRY_RUN,
                message="Extension point: Slack integration ready to connect. SLACK_WEBHOOK_URL not configured.",
                payload=payload,
                duration_ms=round((time.monotonic() - start_time) * 1000, 2),
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                duration = round((time.monotonic() - start_time) * 1000, 2)
                resp.raise_for_status()
                return AdapterResult(
                    adapter_name=self.name,
                    status=AdapterStatus.SUCCESS,
                    message="Slack notification delivered successfully",
                    payload={"status_code": resp.status_code},
                    duration_ms=duration,
                )
        except Exception as exc:
            duration = round((time.monotonic() - start_time) * 1000, 2)
            return AdapterResult(
                adapter_name=self.name,
                status=AdapterStatus.FAILED,
                message=f"Slack notification delivery failed: {exc}",
                payload={"error": str(exc)},
                duration_ms=duration,
            )


class DownstreamDispatcher:
    """
    Coordinates execution of all registered downstream workflow adapters.
    Guarantees that individual adapter failures do not impact other adapters or callers.
    """

    def __init__(self, adapters: list[BaseAdapter] | None = None) -> None:
        if adapters is not None:
            self.adapters = list(adapters)
        else:
            self.adapters = [
                WebhookAdapter(),
                JiraAdapter(),
                SlackAdapter(),
            ]

    def register(self, adapter: BaseAdapter) -> None:
        """Register a new downstream workflow adapter."""
        self.adapters.append(adapter)

    async def dispatch(self, event: RedeliveryFiledEvent) -> list[AdapterResult]:
        """
        Dispatch the event to all registered adapters concurrently.
        Captures any unhandled exceptions to enforce strict error isolation.
        """
        results: list[AdapterResult] = []
        if not self.adapters:
            return results

        async def _safe_handle(adapter: BaseAdapter) -> AdapterResult:
            try:
                return await adapter.handle(event)
            except Exception as exc:
                log.error("adapter.unhandled_exception", adapter=adapter.name, error=str(exc))
                return AdapterResult(
                    adapter_name=adapter.name,
                    status=AdapterStatus.FAILED,
                    message=f"Unhandled adapter exception: {exc}",
                    payload={"error": str(exc)},
                )

        tasks = [_safe_handle(ad) for ad in self.adapters]
        gathered = await asyncio.gather(*tasks)
        return list(gathered)
