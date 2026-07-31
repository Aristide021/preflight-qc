"""
Delivery-QC Compliance Agent — agents package.

Multi-agent system structure:
  spec_reader  — reads the live delivery spec, classifies failures
  qc_analyst   — queries ClickHouse history, risk-scores deliveries
  orchestrator — coordinates the full loop, makes filing decisions
  action       — files redeliveries, writes audit trail to ClickHouse

All agents use google-adk (google.adk) for orchestration
and google-genai (google.genai) for Gemini inference.
"""
