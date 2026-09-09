# PreFlight QC — Build Checklist

## Build Preferences

- **Build mode:** Autonomous
- **Comprehension checks:** No
- **Git:** Keep changes isolated on `demo-ui-fallback`; commit at stable checkpoints.
- **Verification:** Yes — browser check after each user-visible milestone.
- **Check-in cadence:** Speed-run with explicit verification pauses.

## Checklist

- [x] **1. Define the judge-facing workflow**
  Spec ref: `README.md > What It Does`
  What to build: Make the user journey explicit: submit QC result, inspect failures, review history, approve remediation.
  Acceptance: A first-time user understands the product purpose and next action without reading the repository.
  Verify: Review the landing screen manually.

- [x] **2. Build the final web interface shell**
  Spec ref: `README.md > Architecture > Web Front-End`
  What to build: Create the responsive dashboard layout, navigation, status banner, and main delivery workspace.
  Acceptance: The interface loads without dependencies beyond the local server and works at desktop/mobile widths.
  Verify: Open the local URL in the browser and inspect the layout.

- [x] **3. Add QC result intake**
  Spec ref: `agents/shared/models.py > QCResult`
  What to build: Support the sample payload, pasted JSON, and file upload with clear validation errors.
  Acceptance: A valid QC result renders title, vendor, platform, attempt, and error count.
  Verify: Submit `data/sample_qc_result.json` and an invalid payload.

- [x] **4. Add spec-backed failure presentation**
  Spec ref: `agents/shared/models.py > SpecClassification`
  What to build: Display blocking/cosmetic/advisory counts, cited requirement text, and remediation hints.
  Acceptance: Every classified failure is readable and severity is visually distinct.
  Verify: Inspect a result containing at least two severities.

- [x] **5. Add historical risk presentation**
  Spec ref: `agents/shared/models.py > RiskAssessment`
  What to build: Display risk score, label, vendor history, predicted codes, and cost estimate.
  Acceptance: The risk card explains the score’s evidence and identifies the analytics source.
  Verify: Confirm demo mode is labeled and no fixture result is presented as live ClickHouse data.

- [x] **6. Add orchestrator decision and remediation**
  Spec ref: `agents/shared/models.py > OrchestratorDecision`
  What to build: Present the recommended action, rationale, spec citation, and vendor-ready remediation.
  Acceptance: A user can understand what to do next in under one minute.
  Verify: Run the sample workflow and review the decision panel.

- [x] **7. Add ClickHouse MCP adapter modes**
  Spec ref: `agents/shared/clickhouse_mcp.py > ClickHouseMCP`
  What to build: Keep live MCP as the production path and add a clearly labeled development analytics fallback with matching output shapes.
  Acceptance: The UI works without Cloud credentials in demo mode; production mode fails visibly if MCP is unavailable.
  Verify: Exercise both modes and inspect the source indicator.

- [x] **8. Connect the real agent workflow**
  Spec ref: `agents/orchestrator/agent.py > run_orchestrator`
  What to build: Connect the web endpoint to Spec-Reader, QC-Analyst, Orchestrator, and Action Agent.
  Acceptance: A live request produces typed outputs and a tracking record when ClickHouse is configured.
  Verify: Run the full loop against ClickHouse MCP.

- [x] **9. Package and host the application**
  Spec ref: `README.md > Setup`
  What to build: Add deployment instructions, environment validation, and a public judge-facing URL.
  Acceptance: A judge can open the project and understand how to test it with minimal setup.
  Verify: Deploy, open in a clean browser session, and run the sample flow.

- [x] **10. Prepare Devpost handoff**
  Spec ref: `README.md > Data Provenance`
  What to build: Finalize the write-up, screenshots, demo script, runtime proof, repository instructions, and ClickHouse track explanation.
  Acceptance: Submission materials accurately describe live versus demo behavior and include the hosted URL and video.
  Verify: Conduct a final submission checklist review.
