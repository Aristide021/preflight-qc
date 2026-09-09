# PreFlight QC — Data Provenance & Grounding

This document details the data sources, harvesting methodology, synthetic generation assumptions, and domain boundaries underpinning PreFlight QC.

---

## 1. What Is Ground Truth (Harvested from Netflix Photon)

The core error vocabulary is **harvested directly from Netflix's open-source Photon validator (`v5.0.1`)**, rather than invented:

1. **Declared Error Codes:**
   - All 15 constants of `IMFErrorLogger.IMFErrors.ErrorCodes` dumped directly from Photon's loaded bytecode via `infra/photon/harvester/PhotonHarvester.java`.
2. **Error Levels and Severities:**
   - Derived directly from Photon's `WARNING`, `NON_FATAL`, and `FATAL` error levels.
3. **Verbatim Message Strings:**
   - 188 real error instances extracted by running `IMPAnalyzer.analyzeDelivery(Path)` across 37 IMF test packages bundled with Photon (`src/test/resources/TestIMP`).
4. **Empirical Failure Frequencies:**
   - Measured failure distributions from real test vectors are used to weight the analytical corpus.

The full reproducible harvest script lives in [`infra/photon/harvest_all.sh`](file:///Users/sheldon/Documents/GitHub/preflight-qc/infra/photon/harvest_all.sh), and the structured output is committed at [`data/photon_harvest/error_taxonomy.json`](file:///Users/sheldon/Documents/GitHub/preflight-qc/data/photon_harvest/error_taxonomy.json).

---

## 2. What Is Synthetic (And Labeled as Such)

Because commercial post-production QC delivery records are proprietary enterprise data, the scale of the dataset is generated synthetically:

* **Corpus Volume (50M+ rows):**
  - Generated using the frequency weights derived from Gate 1's Photon harvest.
* **Delivery Metadata:**
  - Titles, vendor identifiers, submission dates, codecs (JPEG2000, ProRes, H.264, H.265), resolutions, and audio configs.
* **Vendor Behavior & Redelivery Economics:**
  - Redelivery chain lengths, cost escalation metrics (\$5,000–\$50,000 remediation brackets), and per-vendor weakness biases.
  - These assumptions are explicitly codified and tagged `"_status": "assumption"` in [`data/generator/error_weights.json`](file:///Users/sheldon/Documents/GitHub/preflight-qc/data/generator/error_weights.json).

---

## 3. Clear Boundaries: Non-Photon Checks

Netflix Photon is strictly an IMF **structure and essence** validator (verifying XML schemas, AssetMaps, CPL/PKL hashes, and track files).

Photon does **not** check:
- Audio integrated loudness (e.g. -24 LKFS / -27 LKFS limits)
- Dolby Vision dynamic metadata (e.g. RPU presence)
- Subtitle synchronization / Timed Text presence

In PreFlight QC:
- Structure and packaging errors cite Photon-grounded requirements.
- External checks (loudness, Dolby Vision) are grounded in the **published Netflix delivery specifications** via the Spec-Reader agent, and are explicitly documented as specification compliance checks outside of Photon's structural scope.
