# PreFlight QC

> **Agentic Cinema: The Blockbuster Hackathon — ClickHouse Track**

A multi-agent system that takes a media delivery's QC results, interprets them
against the platform's delivery spec, decides what's **blocking vs cosmetic**,
files and tracks the redelivery, and reasons over the *history* of past failures
to **predict and prevent the next one**.

---

## The Problem

Every year, post houses and studios lose millions of dollars and thousands of
man-days to media package failures at platform ingest. A single rejected delivery
can cost $5,000–$50,000 in remediation and delay a title's release window. Most
QC tooling tells you *what* failed — none of it tells you *why it keeps failing*
or *what the next package will fail on* before it's ever submitted.

**This agent closes that loop.**

---

## What It Does

| Verb | What the agent actually does |
|---|---|
| **Classifies** | Reads the live Netflix IMF delivery spec; classifies each QC error as `blocking`, `cosmetic`, or `advisory` with a spec citation |
| **Predicts** | Queries millions of historical inspection records in ClickHouse to risk-score an incoming delivery before submission |
| **Files** | Generates a specific, actionable remediation and files a tracked redelivery record |
| **Resolves** | Closes the audit loop — every attempt, decision, and cost estimate is recorded and queryable |

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │         Web Front-End            │
                    │  (delivery dashboard + analytics)│
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │         Orchestrator Agent       │
                    │    (Google ADK / Gemini Flash)     │
                    └──┬───────────────────┬──────────┘
                       │                   │
          ┌────────────▼──────┐   ┌────────▼──────────┐
          │  Spec-Reader Agent│   │  QC-Analyst Agent  │
          │  (live spec fetch │   │  (history reasoner │
          │   + classifier)   │   │   via ClickHouse)  │
          └────────────┬──────┘   └────────┬──────────┘
                       │                   │
                       │         ┌─────────▼──────────┐
                       │         │  ClickHouse Cloud   │
                       │         │  (50M+ inspection   │
                       │         │   records via MCP)  │
                       │         └────────────────────-┘
                       │
          ┌────────────▼──────────┐
          │  Action Agent         │
          │  (files redelivery,   │
          │   writes audit trail) │
          └───────────────────────┘
                       │
          ┌────────────▼──────────┐
          │  Photon (offline)     │
          │  Netflix IMF validator│
          │  (error taxonomy      │
          │   ground truth)       │
          └───────────────────────┘
```

**Tech stack:**
- **Agents:** Google ADK + Gemini Flash (`google-adk`, `google-genai`)
- **Analytical DB:** ClickHouse Cloud via `mcp-clickhouse` MCP server
- **Error taxonomy ground truth:** Netflix Photon (open-source IMF validator)
- **Hosting:** Google Cloud Run + Secret Manager + IAM
- **Front-end:** Next.js / React

---

## Runtime Proof (Hard Requirements)

### Google Cloud SDK — actually called at runtime

```python
# agents/shared/clickhouse_mcp.py
import google.adk  # google-adk imported and called in agent runner
from google import genai  # google-genai: Gemini inference

client = genai.Client()
runner = adk.Runner(agent=orchestrator_agent, ...)
```

### ClickHouse via mcp-clickhouse — actually called at runtime

```python
# agents/qc_analyst/tools.py
from mcp import ClientSession
# MCP server: mcp-clickhouse, connected to ClickHouse Cloud
# Every QC-Analyst tool call routes through the MCP connection
```

---

## Data Provenance

**What's real — harvested from Photon, not hand-written:**
- **Error codes:** all 15 constants of `IMFErrorLogger.IMFErrors.ErrorCodes`,
  dumped from Photon v5.0.1's own loaded classes
- **Error severities:** derived from Photon's `WARNING` / `NON_FATAL` / `FATAL`
  levels, not from an assumed mapping
- **Message text:** verbatim strings from 188 real error instances, produced by
  running `IMPAnalyzer.analyzeDelivery` over 37 of the IMF packages Photon ships
  in `src/test/resources/TestIMP`
- **Failure frequencies:** measured from those runs, and used directly to weight
  the corpus

Reproduce it end to end with `./infra/photon/harvest_all.sh`. The evidence is
committed under `data/photon_harvest/`.

**What's synthetic — and labelled as such:**
- Volume (50M+ rows): synthesized because production QC data is proprietary
- Delivery metadata (titles, vendors, dates, codecs): synthesized
- Vendor behaviour and remediation economics — redelivery chain lengths, cost
  escalation, per-vendor weakness bias. These are explicit assumptions in
  `data/generator/error_weights.json`, each tagged `"_status": "assumption"`

**What's deliberately absent:**
Photon validates IMF structure and essence. It does **not** check integrated
loudness, subtitle presence, or Dolby Vision metadata, so no such codes appear
in the corpus. Adding them means grounding a second source in the published
Netflix delivery spec — see `data/photon_harvest/README.md`.

The generator refuses to run without the harvested taxonomy rather than falling
back to invented codes.

---

## Setup

### Prerequisites

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Copy environment template
cp .env.example .env
# Fill in: CLICKHOUSE_HOST, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD,
#          GOOGLE_API_KEY, GOOGLE_PROJECT_ID, GOOGLE_REGION
```

### Install dependencies

```bash
uv sync
```

### Run Gate 2 smoke test (ClickHouse MCP connectivity)

```bash
uv run python mcp_config/gate2_smoke_test.py
```

### Run full agent loop (local)

```bash
uv run python agents/orchestrator/agent.py --input data/sample_qc_result.json
```

### Run integration test

```bash
uv run pytest tests/integration/test_full_loop.py -v
```

---

## Project Structure

```
preflight-qc/
├── LICENSE                        # Apache-2.0
├── README.md
├── pyproject.toml                 # uv-managed; google-adk, google-genai runtime deps
├── .env.example
├── agents/
│   ├── __init__.py
│   ├── shared/
│   │   ├── clickhouse_mcp.py      # MCP client, connection pool
│   │   └── models.py              # Pydantic models
│   ├── spec_reader/               # Spec-Reader agent
│   ├── qc_analyst/                # QC-Analyst agent (the differentiator)
│   ├── orchestrator/              # Orchestrator agent
│   └── action/                    # Action agent
├── data/
│   ├── photon_harvest/            # Real Photon error taxonomy
│   ├── generator/                 # Synthetic corpus generator
│   ├── schema.sql                 # ClickHouse DDL (qc_inspections)
│   ├── schema_tracking.sql        # ClickHouse DDL (redelivery_tracking)
│   └── sample_queries.sql         # Top-10 analytical queries
├── mcp_config/
│   ├── clickhouse_config.yaml     # MCP server config
│   └── gate2_smoke_test.py        # Gate 2 proof artifact
├── infra/
│   ├── photon/                    # Photon Docker wrapper + harvester
│   ├── cloudrun/                  # Cloud Run service definition
│   ├── secretmanager/             # Secret Manager bootstrap
│   └── iam/                       # IAM policy
├── web/                           # Delivery dashboard front-end
├── tests/
│   └── integration/
│       └── test_full_loop.py
└── docs/
    ├── writeup.md
    └── data_provenance.md
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).
