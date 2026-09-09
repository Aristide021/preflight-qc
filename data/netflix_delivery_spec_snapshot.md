# Netflix Delivery Requirements Reference Snapshot

Source: https://studiopartner.netflix.net/studio/delivery-specifications-and-requirements-external

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

## Delivery reference

The authoritative delivery requirements are maintained in the Netflix Studio
Partner delivery specifications. The reference page describes the package and
metadata requirements; it does not replace Photon or another IMF validator.
