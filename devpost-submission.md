# Title

PreFlight QC

## One-line Summary

An agentic IMF delivery control tower that turns validator findings into grounded redelivery decisions, ClickHouse history, and an auditable action.

## Problem

IMF delivery validators report technical failures, but delivery teams still need to determine what matters, whether the package is blocked, what to fix, and how the decision should be recorded. That judgment is often manual and slows delivery.

## Solution

PreFlight QC receives a QC result and coordinates four agents: Spec-Reader, QC-Analyst, Orchestrator, and Action Agent. Gemini on Google Cloud explains and synthesizes the evidence. ClickHouse history supplies vendor, codec, error, and remediation-cost context. The result is a human-readable decision with specific next steps.

## Why This Matters

IMF reduces duplicated media by describing versions through playlists and shared assets, but those references make package integrity important. PreFlight QC gives delivery managers an operational answer after a validator reports a failure.

## How We Used AI

- Google ADK coordinates the multi-agent workflow.
- Gemini 2.5 Flash on Vertex AI classifies findings against the fetched delivery material and synthesizes the final decision.
- ClickHouse is queried at runtime through the official `mcp-clickhouse` MCP server.
- Risk scoring is deterministic code based on returned history; Gemini provides the explanation rather than inventing the score.

## How We Used Codex

Codex was used to design and implement the typed agent contracts, browser-testable dashboard, ClickHouse MCP integration, adapter architecture, Photon evidence fixture, Vertex AI authentication path, tests, and deployment configuration.

## Key Features

- Human-readable failure labels with engineering details available secondarily.
- Blocking, cosmetic, and advisory classification.
- Historical vendor/error/codec analysis from ClickHouse.
- Deterministic risk score and remediation-cost context.
- Redelivery decision and ClickHouse audit record.
- Extensible domain event adapters for future webhooks, Jira, and Slack integrations.
- Public Photon-derived evidence fixture with provenance notes.

## Architecture

The web dashboard sends a QCResult to the Orchestrator. Spec-Reader and QC-Analyst run concurrently. The Orchestrator combines their typed outputs and hands a decision to the Action Agent. The Action Agent writes redelivery tracking to ClickHouse through the official MCP server. Gemini runs through Vertex AI using Application Default Credentials.

## Testing Instructions

```bash
uv sync
uv run pytest tests/ -m 'not integration'
uv run python mcp_config/gate2_smoke_test.py
uv run python agents/orchestrator/agent.py --input data/sample_qc_result.json
```

The Gate 2 smoke test has been run successfully: Gemini on Vertex AI invoked `mcp-clickhouse`, queried ClickHouse, and returned the sentinel result. The end-to-end sample also filed a tracking record.

## Public Demo Link

https://preflight-qc-575561187011.us-central1.run.app

## Public Repository Link

https://github.com/Aristide021/preflight-qc/tree/demo-ui-fallback

## Demo Video

TODO: Record and upload a public English video to YouTube or Vimeo.

Suggested three-minute flow: explain IMF and the operational problem; load the public Photon evidence; show the human-readable blocked decision; show Gemini/ClickHouse MCP runtime evidence; show the tracking record and close with the concrete questions PreFlight answers.

## Screenshot Shot List

1. Dashboard landing state with the product purpose visible.
2. Photon evidence loaded with human-readable findings.
3. Decision view showing blocked status, impact, and remediation.
4. History/risk view showing ClickHouse-backed evidence.
5. Tracking/audit result showing the filed redelivery record.

## Submission Readiness Notes

- Official requirements require a hosted project URL, public repository, open-source license, selected partner track, completed form, and a three-minute functional demo video.
- ClickHouse track is the intended track.
- The live ClickHouse MCP and Vertex AI gate is green.
- The judge-facing application is live-only; fixture buttons load public JSON for analysis and do not provide fake analysis results.

## Known Limitations

- The Netflix Studio Partner page currently serves an HTML shell to automated fetches, so the repository includes a clearly disclosed source snapshot used for grounding.
- Jira, Slack, and generic webhook adapters are extension points unless configured.
- The historical corpus is synthetic and must be described as illustrative; ClickHouse runtime querying is real.
- Cloud Run is publicly deployed and has passed a live end-to-end analysis with Gemini on Vertex AI, ClickHouse MCP, and a ClickHouse audit write.

## TODO Official Form Fields

- Submitter type: TODO
- Organization name: TODO
- Government employee: TODO
- Country of residence: TODO
- Canada province or `N/A`: TODO
- New or existing before July 27, 2026: TODO
- Team size: TODO
- ClickHouse tools first time: TODO
- Google Cloud products: Vertex AI / Gemini Enterprise Agent Platform, Google ADK, Cloud Run, Secret Manager
- Other tools: ClickHouse Cloud, official `mcp-clickhouse`, Photon, Python, Pydantic
- Codex session ID: only if the official form asks for it; TODO
