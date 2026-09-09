# Netflix Delivery Requirements Reference Snapshot

Source: https://studiopartner.netflix.net/studio/delivery-specifications-and-requirements-external

Additional authoritative source used for audio requirements:
https://partnerhelp.netflixstudios.com/hc/en-us/articles/7262346654995-Post-Production-Branded-Delivery-Specifications

This snapshot was refreshed on 2026-09-09 from the official Netflix Partner Help
Center article above. It records only the requirements needed by this demo; the
linked source remains the authority for a production delivery.

This committed reference is a source snapshot used only when the Studio Partner
page is not server-readable. It is not generated demo data. The live source URL
is always reported to the operator, and classifications should be reviewed when
the source is available again.

## IMF and Dolby Vision

Netflix Originals delivering in HDR must be mastered in Dolby Vision and
delivered according to the IMF delivery specifications for Dolby Vision packages.
Dolby Vision 4.0 deliveries require CMVersion [4 1] and Dolby Vision Metadata
embedded in the IMF Picture Trackfile. Deliveries must include Dolby Vision IMF
with embedded metadata.

Metadata must be accurate, including Color Encoding, Mastering Display, and
Aspect Ratio. Dolby's metafier validation should be used to check the metadata
before delivery.

## Audio requirements

For original-language near-field 5.1 and 2.0 mixes, Netflix specifies dialogue
loudness of -27 LKFS +/- 2 LU, measured over the entire program using
ITU-R BS.1770-1. The 5.1 and 2.0 mix must not exceed -2 dBFS maximum true peak.
Netflix requires 5.1 audio for all titles; 2.0 is optional. Audio masters use
48 kHz sample rate and 24-bit depth.

For a near-field Atmos mix, Netflix specifies -27 LKFS +/- 2 LU dialogue-gated
loudness and recommends true-peak limiters on beds and objects at -2.3 dBFS or
lower; loudness should be checked using a 5.1 rerender.

The Netflix Partner Help Center also documents the analyzer finding
`ANALYZER-Audio-LoudnessNotCompliant`, whose general solution is to normalize
program loudness to the applicable Netflix target and remeasure with an
ITU-R BS.1770 meter. Audio thresholds are scope-dependent, so the delivery
type and measurement method must be identified before labeling a finding
blocking.

## Delivery reference

The authoritative delivery requirements are maintained in the Netflix Studio
Partner delivery specifications. The reference page describes the package and
metadata requirements; it does not replace Photon or another IMF validator.
