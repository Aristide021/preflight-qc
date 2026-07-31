#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Make a deliberately malformed IMF package so Photon has something to reject.
#
# An IMF package is a directory with:
#   - ASSETMAP.xml   (asset inventory)
#   - PKL_*.xml      (packing list)
#   - CPL_*.xml      (composition playlist)
#   - One or more MXF essence files
#
# We create structurally valid XML envelopes with deliberately wrong values
# (missing required elements, bad UUIDs, incorrect track counts) to trigger
# a broad range of Photon error codes.
#
# This is the "Option C" fallback when the imf-plugfest S3 bucket is unreachable.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

OUTPUT_DIR="${1:-/tmp/malformed_imf}"
mkdir -p "$OUTPUT_DIR"

echo "▶ Creating malformed IMF package at: $OUTPUT_DIR"

# ── ASSETMAP.xml — missing required xmlns and Creator ────────────────────────
cat > "$OUTPUT_DIR/ASSETMAP.xml" << 'ASSETMAP'
<?xml version="1.0" encoding="UTF-8"?>
<!-- DELIBERATELY MALFORMED: missing xmlns, invalid Creator, bad asset refs -->
<AssetMap>
    <Id>urn:uuid:XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX</Id>
    <VolumeCount>1</VolumeCount>
    <IssueDate>2026-01-01T00:00:00+00:00</IssueDate>
    <Issuer>malformed-test</Issuer>
    <AssetList>
        <Asset>
            <Id>urn:uuid:NOT-A-VALID-UUID</Id>
            <PackingList/>
            <ChunkList>
                <Chunk>
                    <Path>PKL_INVALID.xml</Path>
                </Chunk>
            </ChunkList>
        </Asset>
    </AssetList>
</AssetMap>
ASSETMAP

# ── PKL_INVALID.xml — missing hash, wrong size ───────────────────────────────
cat > "$OUTPUT_DIR/PKL_INVALID.xml" << 'PKL'
<?xml version="1.0" encoding="UTF-8"?>
<!-- DELIBERATELY MALFORMED: missing Hash, wrong type URIs, no IconId -->
<PackingList xmlns="http://www.smpte-ra.org/schemas/2067-2/2016/PKL">
    <Id>urn:uuid:NOT-A-VALID-UUID-PKL</Id>
    <AnnotationText>Malformed Test PKL</AnnotationText>
    <IssueDate>2026-01-01T00:00:00+00:00</IssueDate>
    <Issuer>malformed-test</Issuer>
    <Creator>MalformedGenerator/1.0</Creator>
    <AssetList>
        <Asset>
            <Id>urn:uuid:NOT-A-VALID-UUID-CPL</Id>
            <AnnotationText>Malformed CPL</AnnotationText>
            <Hash></Hash>
            <Size>0</Size>
            <Type>application/mxf</Type>
            <OriginalFileName>CPL_INVALID.xml</OriginalFileName>
        </Asset>
    </AssetList>
</PackingList>
PKL

# ── CPL_INVALID.xml — multiple structural violations ────────────────────────
cat > "$OUTPUT_DIR/CPL_INVALID.xml" << 'CPL'
<?xml version="1.0" encoding="UTF-8"?>
<!-- DELIBERATELY MALFORMED:
     - No ApplicationIdentification (triggers IMF_CPL_ERROR)
     - Mismatched track counts (triggers IMF_AM_ERROR)
     - Invalid EditRate (triggers IMF_ESSENCE_EXCEPTION)
     - Missing AudioChannelSchemeLabel (triggers audio validation error)
     - No SubtitleTrack when spec requires one
-->
<CompositionPlaylist xmlns="http://www.smpte-ra.org/ns/2067-3/2016">
    <Id>urn:uuid:NOT-A-VALID-UUID-CPL</Id>
    <AnnotationText>Malformed CPL — triggers broad error set</AnnotationText>
    <EditRate>INVALID RATE</EditRate>
    <TimecodeRate>24</TimecodeRate>
    <SegmentList>
        <Segment>
            <Id>urn:uuid:NOT-A-VALID-UUID-SEG</Id>
            <SequenceList>
                <MarkerSequence>
                    <Id>urn:uuid:NOT-A-VALID-UUID-MS</Id>
                    <TrackId>urn:uuid:NOT-A-VALID-UUID-TRK</TrackId>
                    <ResourceList>
                    </ResourceList>
                </MarkerSequence>
            </SequenceList>
        </Segment>
    </SegmentList>
</CompositionPlaylist>
CPL

# ── Dummy MXF file (not a real MXF — will trigger essence errors) ───────────
echo "NOT_A_REAL_MXF_FILE" > "$OUTPUT_DIR/essence_dummy.mxf"

echo "✓ Malformed IMF package created with 4 files:"
ls -la "$OUTPUT_DIR"
echo ""
echo "  Expected Photon error codes (non-exhaustive):"
echo "    IMF_CPL_ERROR          — missing ApplicationIdentification"
echo "    IMF_AM_ERROR           — malformed ASSETMAP UUIDs"
echo "    IMF_ESSENCE_EXCEPTION  — invalid MXF essence file"
echo "    IMF_PKL_ERROR          — empty Hash field in PKL"
