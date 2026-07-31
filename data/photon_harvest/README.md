# ─────────────────────────────────────────────────────────────────────────────
# Gate 1 — Photon Error Harvest
# ─────────────────────────────────────────────────────────────────────────────
# Run: ./infra/photon/run_photon.sh
# Output: data/photon_harvest/error_taxonomy.json
#
# Gate 1 is GREEN when error_taxonomy.json has ≥10 distinct IMF_* codes
# from a live Photon run (not seed-only).

## Test vectors used

### Primary: imf-plugfest S3 (verify reachability on D1)
```bash
aws s3 ls s3://imf-plugfest/ --no-sign-request
```
If accessible, download with:
```bash
aws s3 cp s3://imf-plugfest/imf-packages/ ./raw/plugfest_vectors/ \
    --recursive --no-sign-request
```

### Fallback A: Photon bundled test resources
The Photon JAR includes internal test resources. Running `docker run photon-validator`
with no arguments invokes the help/self-test mode and may emit error vocabulary.

### Fallback B: Deliberately malformed IMF package
`infra/photon/make_malformed_imf.sh` generates a syntactically invalid IMF
package guaranteed to trigger: `IMF_CPL_ERROR`, `IMF_AM_ERROR`,
`IMF_ESSENCE_EXCEPTION`, `IMF_PKL_ERROR`.

### Fallback C: Third-party IMF test packages
- EBU IMF samples: https://tech.ebu.ch/publications/imf
- SMPTE IMF interop test materials (members only)
- Construct a valid package from free content + deliberately violate one field

## Harvest log

| Date | Test vector | Codes found | Gate status |
|------|------------|-------------|-------------|
| TBD  | TBD        | TBD         | ⏳ Pending  |

## Notes
- The `raw/` directory is in `.gitignore` (large binary test vectors not committed)
- `error_taxonomy.json` IS committed — it's the harvested knowledge artifact
- `source: "seed_vocabulary"` entries in the taxonomy are pre-seeded from the
  Netflix IMF spec and Photon source code; they are not live-run outputs
