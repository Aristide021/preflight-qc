# Build Notes

## 2026-09-04 — UI and fallback build

- The participant clarified that the web UI is part of the final product; only the ClickHouse analytics path may use a development fallback.
- Work is isolated on branch `demo-ui-fallback`.
- The first milestone is a browser-testable dashboard around the existing QCResult, SpecClassification, RiskAssessment, and OrchestratorDecision contracts.
- Live `mcp-clickhouse` remains the production/submission path. Demo analytics must be visibly labeled.
- The first UX review found that raw error codes and repetitive generic remediation were too technical. The dashboard now leads with human-readable issue labels, package impact, and specific fixes; engineering codes are secondary details.

## 2026-09-05 — Photon evidence demo

- Added `data/sample_photon_vector.json`, built from the committed Photon harvest records for the public `MissingFilesAndAssetMapEntries` fixture.
- The dashboard's Photon evidence button now loads those four real Photon findings rather than the synthetic multi-stage sample.
- Fixture envelope metadata is explicitly marked as a demo wrapper; Photon error codes, levels, assets, and messages remain traceable to `data/photon_harvest/raw/observed_errors.jsonl`.
- Verified the JSON fixture, demo classification, and live HTTP route at `/api/sample?mode=demo&type=photon`.
