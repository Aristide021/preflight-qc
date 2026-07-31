# Gate 1 — Photon Error Harvest

**Run:** `./infra/photon/harvest_all.sh`
**Output:** `data/photon_harvest/error_taxonomy.json`

Gate 1 is green when the taxonomy is Photon-authentic: the full declared
vocabulary is dumped from Photon's own classes, live `analyzeDelivery` runs
exercise a meaningful share of it, and enough real error instances are captured
to weight a corpus. The gate is checked at the end of `harvest_all.sh`.

## Current status

| | |
|---|---|
| Photon version | v5.0.1 (pinned via `PHOTON_REF`) |
| Declared error codes | 15 |
| Codes exercised in live runs | 8 |
| Real error instances | 188 |
| Packages analyzed | 37 (35 produced errors) |
| Gate 1 | **GREEN** |

## How the harvest works

Photon exposes its error vocabulary as two enums:

- `IMFErrorLogger.IMFErrors.ErrorCodes` — 15 constants
- `IMFErrorLogger.IMFErrors.ErrorLevels` — `WARNING`, `NON_FATAL`, `FATAL`

**The `IMPAnalyzer` CLI never prints those constants.** It prints prose:

```
ERROR-UUID 0eb3d1b9-...ca84 in the CPL is not same as UUID 0eb3d1b9-...ca85 ...
```

Photon overrides `toString()` on the enums to return a human label ("IMF CPL
Error"), so even the structured values do not surface the constant names
without calling `name()`. Any approach that regex-scans CLI text for
`IMF_*_ERROR` tokens recovers **zero** codes.

So `infra/photon/harvester/PhotonHarvester.java` calls the same entry point the
CLI does — `IMPAnalyzer.analyzeDelivery(Path)` — and reads the returned
`ErrorObject` list directly, emitting the real
`(error_code, error_level, error_description)` triple as JSONL.

## Test vectors

Photon bundles ~50 real IMF packages under `src/test/resources/TestIMP`:
plugfest deliveries (`Netflix_Sony_Plugfest_2015`), deliberately broken
variants (`MERIDIAN_..._ID_MISMATCH`, `MissingFilesAndAssetMapEntries`,
`BadXML`, `WrongXmlMimeTypes`), and Application 2E / 5, IAB, PHDR and HT
profile packages. The Docker image copies these to `/photon/test_vectors`, so
the harvest is reproducible with no network access.

**The `imf-plugfest` S3 bucket referenced in Photon's docs is not usable.**
Anonymous listing returns `403 AccessDenied`, so `aws s3 cp --no-sign-request`
cannot enumerate it. The bundled vectors replace it entirely.

`infra/photon/make_malformed_imf.sh` is kept as a last-resort fallback, but it
is a poor harvest source: its ASSETMAP omits a supported namespace, so Photon
rejects the package immediately and never reaches CPL, PKL or essence
validation. It yields exactly one error.

## Reading `error_taxonomy.json`

Every entry is a real Photon enum constant. Nothing in this file is invented.

| Field | Meaning |
|---|---|
| `error_code` | Enum constant name, e.g. `IMF_CPL_ERROR` |
| `error_label` | Photon's own human label |
| `observed` | Whether live runs actually produced it |
| `observed_count` / `observed_share` | Measured frequency, used to weight the corpus |
| `levels_observed` | Photon error levels seen for this code |
| `severity` | Derived from the dominant level (`FATAL`/`NON_FATAL` → blocking, `WARNING` → advisory) |
| `example_messages` | Verbatim Photon message text |
| `category` | **Our** grouping, not Photon's — see below |

`observed: false` means Photon declares the code but the bundled packages did
not trigger it. Those codes are real and stay in the file; the generator gives
them a floor weight so they appear rarely rather than never.

### What is ours, not Photon's

Two things in this pipeline are modeling decisions, marked as such in the code:

- **`category`** (structural / essence / integrity / metadata / internal).
  Photon has no category axis. The mapping lives in `CATEGORY_BY_CODE` in
  `infra/photon/build_taxonomy.py`.
- **No `cosmetic` severity.** Photon's levels only support blocking vs
  advisory. Whether a given failure is *cosmetic* for a particular platform is
  a delivery-spec judgment, and it is the Spec-Reader agent's job to make it.

## Known gap: spec-derived checks

Photon validates IMF **structure and essence**. It does not check integrated
loudness, subtitle presence, or Dolby Vision metadata — a real delivery
pipeline runs those as separate checks against the platform delivery spec.

The earlier seed vocabulary papered over this with invented codes
(`IMF_AUDIO_LOUDNESS_ERROR`, `IMF_SUBTITLE_ERROR`, `IMF_DOLBY_VISION_ERROR`,
`IMF_HDR_ERROR`, `IMF_CODEC_ERROR`, `IMF_HASH_ERROR`, …). Those are gone. The
corpus currently covers only what Photon genuinely validates.

Adding spec-derived checks is a separate, clearly-labelled second source,
grounded in the published Netflix delivery spec rather than in Photon. Until
that exists, the corpus is narrower but everything in it is real.

## Notes

- `raw/` holds the harvest artifacts (`declared_codes.jsonl`,
  `observed_errors.jsonl`). They are the Gate 1 evidence and are committed.
- `error_taxonomy.json` is the artifact the generator and agents consume.
- Re-running `harvest_all.sh` regenerates all three deterministically.
