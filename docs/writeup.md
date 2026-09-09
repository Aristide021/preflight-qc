# PreFlight QC — Architectural Write-Up

> **Agentic Cinema: The Blockbuster Hackathon — ClickHouse Track**

---

## 1. Executive Summary

Every year, film studios and post houses spend millions of dollars and thousands of engineer-hours dealing with rejected media package deliveries at streaming platform ingest (Netflix, Disney+, Apple TV+). A single rejected Interoperable Master Format (IMF) package costs between \$5,000 and \$50,000 in triage, remediation, and missed delivery windows.

Existing automated Quality Control (QC) tools (such as Netflix Photon, Interra Baton, and Telestream Aurora) tell engineers **what** failed inside a package, but they cannot:
1. Ground the findings in the platform's live contractual delivery specification.
2. Distinguish between hard blocking failures vs waivable cosmetic issues.
3. Query millions of historical delivery events to determine **why** packages keep failing, score vendor risk, and predict the next failure before submission.
4. Close the compliance loop by generating actionable remediation and tracking redelivery attempts in an auditable database.

**PreFlight QC** solves this by uniting a multi-agent system (powered by Google ADK and Gemini Flash) with a high-performance **ClickHouse Cloud** analytical backend queried through the Model Context Protocol (MCP).

---

## 2. Multi-Agent Architecture

```
                    ┌─────────────────────────────────┐
                    │     Delivery Control Tower      │
                    │   (Web Dashboard & Visualizer)  │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │       Orchestrator Agent        │
                    │   (Google ADK / Gemini Flash)   │
                    └────────┬───────────────┬────────┘
                             │               │
            ┌────────────────▼───┐       ┌───▼────────────────┐
            │  Spec-Reader Agent │       │  QC-Analyst Agent  │
            │  (Live Spec Fetch  │       │ (History Reasoner  │
            │    + Classifier)   │       │  via ClickHouse)   │
            └────────────────────┘       └───┬────────────────┘
                                             │
                                 ┌───────────▼────────────┐
                                 │    ClickHouse Cloud    │
                                 │   (50M+ inspections    │
                                 │     via MCP server)    │
                                 └───────────┬────────────┘
                                             │
                             ┌───────────────▼────────┐
                             │      Action Agent      │
                             │  (Files Redelivery &   │
                             │   Writes Audit Trail)  │
                             └────────────────────────┘
```

### Agent Roles and Contracts

Every handoff across the system uses strictly typed **Pydantic models** (`QCResult`, `SpecClassification`, `RiskAssessment`, `OrchestratorDecision`, `RedeliveryRecord`):

* **Spec-Reader Agent:**
  - Fetches the platform's delivery specification at runtime.
  - Classifies every QC error into `blocking`, `cosmetic`, or `advisory`.
  - Quotes the exact contractual requirement and section to eliminate hallucinations.
* **QC-Analyst Agent (The ClickHouse History Reasoner):**
  - Connects to ClickHouse Cloud via `mcp-clickhouse`.
  - Computes historical failure baselines, vendor risk profiles, error clusters by codec, and expected remediation costs.
  - Generates a deterministic mathematical risk score (`_score_from_history`) so that critical compliance scoring is reproducible and auditable.
* **Orchestrator Agent:**
  - Concurrently invokes Spec-Reader and QC-Analyst using `asyncio.gather()`.
  - Synthesizes the spec classifications with historical risk context to make a concrete decision: `REDELIVER`, `WAIVE`, `ESCALATE`, or `INVESTIGATE`.
* **Action Agent:**
  - When `REDELIVER` is mandated, writes an auditable record to ClickHouse (`redelivery_tracking`) through a dedicated write-enabled client.
  - Non-blocking outcomes (`WAIVE`, `INVESTIGATE`) do not generate false redelivery records.

---

## 3. Why ClickHouse?

ClickHouse is not an auxiliary store in PreFlight QC—it is the analytical core that makes the agent intelligent:

1. **High-Cardinality Aggregations:**
   - Calculating historical failure rates across combinations of `(platform_spec, vendor_id, codec, error_code)` over tens of millions of rows takes single-digit milliseconds.
2. **Deterministic Risk Grounding:**
   - Instead of asking an LLM to guess a risk percentage, ClickHouse computes the exact ratio of vendor failure rates against the corpus baseline.
3. **Closed-Loop Audit Trail:**
   - Every redelivery filed by the Action Agent is immediately inserted into `redelivery_tracking`, becoming part of the historical dataset that informs future risk assessments.

---

## 4. Ground Truth Data & Provenance

* **Real Error Taxonomy:** Harvested from Netflix's open-source Photon validator (`v5.0.1`) by inspecting `IMFErrorLogger.IMFErrors.ErrorCodes` and running test vectors through `IMPAnalyzer`.
* **Explicit Synthetic Boundaries:** 50M+ row delivery volumes and cost models are generated and explicitly tagged as assumptions in `data/generator/error_weights.json`.
* **Non-Photon Boundaries:** Audio loudness and HDR checks are separate platform specification requirements and are treated as such.

---

## 5. Deployment & Runtime Verification

* **Web UI / API:** Serves the interactive Delivery Control Tower with three explicit execution modes:
  - `auto`: Live execution when configured; graceful demo fallback when unconfigured.
  - `live`: Strict end-to-end execution through Gemini Flash and ClickHouse MCP.
  - `demo`: Deterministic offline evaluation mode.
* **Container Packaging:** Production `Dockerfile` and Google Cloud Run service definitions (`infra/cloudrun/service.yaml`) with Google Secret Manager bindings for secure credential handling.
