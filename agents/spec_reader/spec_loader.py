"""
Spec-Reader Agent — fetches and caches the live delivery spec.

Why live fetch instead of a baked-in copy:
  The Netflix IMF loudness spec is cited inconsistently across sources
  (−27 LUFS vs −24 LKFS vs −23 LUFS depending on publication date).
  Reading the current spec beats baking in a number that may be wrong.
  The agent cites what it reads, making its reasoning auditable.

Caching: the spec is cached for SPEC_CACHE_TTL_SECONDS (default 24h)
to avoid redundant fetches across agent runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import NamedTuple

import httpx
import structlog

log = structlog.get_logger(__name__)

# ── Spec registry ─────────────────────────────────────────────────────────────
# Maps platform_spec identifier → spec URL.
# The Spec-Reader fetches from here; agents never hardcode a spec version.

SPEC_REGISTRY: dict[str, str] = {
    "netflix-imf-2.0": "https://partnerhelp.netflixstudios.com/hc/en-us/articles/115001138508",
    "netflix-imf-2.1": "https://partnerhelp.netflixstudios.com/hc/en-us/articles/115001138508",
    "netflix-imf-2.2": "https://partnerhelp.netflixstudios.com/hc/en-us/articles/115001138508",
    # Phase 4 bonus — multi-platform spec support
    # "amazon-vcs-3.x": "https://videodirectsupport.amazon.com/vcs",
}

FALLBACK_SPEC_URL = os.environ.get(
    "NETFLIX_SPEC_URL",
    "https://partnerhelp.netflixstudios.com/hc/en-us/articles/115001138508",
)

CACHE_DIR = Path(os.environ.get("SPEC_CACHE_DIR", "/tmp/preflight_spec_cache"))
CACHE_TTL = int(os.environ.get("SPEC_CACHE_TTL_SECONDS", "86400"))


class SpecDocument(NamedTuple):
    url: str
    content: str          # Full text of the spec (HTML or plain text)
    fetched_at: float     # Unix timestamp


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _cache_path(url: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"spec_{_cache_key(url)}.json"


def _read_cache(url: str) -> SpecDocument | None:
    path = _cache_path(url)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        age = time.time() - data["fetched_at"]
        if age > CACHE_TTL:
            log.debug("spec.cache.expired", url=url, age_hours=age / 3600)
            return None
        return SpecDocument(url=data["url"], content=data["content"], fetched_at=data["fetched_at"])
    except Exception as e:
        log.warning("spec.cache.read.error", error=str(e))
        return None


def _write_cache(doc: SpecDocument) -> None:
    path = _cache_path(doc.url)
    try:
        path.write_text(json.dumps({"url": doc.url, "content": doc.content, "fetched_at": doc.fetched_at}))
    except Exception as e:
        log.warning("spec.cache.write.error", error=str(e))


async def fetch_spec(platform_spec: str) -> SpecDocument:
    """
    Fetch the delivery spec for a given platform_spec identifier.
    Returns the cached version if fresh; otherwise fetches and caches.

    Args:
        platform_spec: e.g. "netflix-imf-2.1"

    Returns:
        SpecDocument with the full spec text
    """
    url = SPEC_REGISTRY.get(platform_spec, FALLBACK_SPEC_URL)
    log.info("spec.fetch", platform_spec=platform_spec, url=url)

    # Check cache first
    cached = _read_cache(url)
    if cached:
        log.debug("spec.cache.hit", url=url)
        return cached

    # Fetch live
    log.info("spec.fetching.live", url=url)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "DeliveryQC-Agent/1.0 (hackathon compliance tool)",
                    "Accept": "text/html,application/xhtml+xml,text/plain",
                },
            )
            response.raise_for_status()
            content = response.text
    except httpx.HTTPError as e:
        log.error("spec.fetch.failed", url=url, error=str(e))
        # Return a minimal fallback spec so the agent can still reason
        content = _minimal_fallback_spec(platform_spec)

    doc = SpecDocument(url=url, content=content, fetched_at=time.time())
    _write_cache(doc)
    log.info("spec.fetched", url=url, content_length=len(content))
    return doc


def _minimal_fallback_spec(platform_spec: str) -> str:
    """
    Minimal spec content if the live fetch fails.
    Based on publicly documented Netflix IMF requirements.
    The agent must note this is a fallback, not the authoritative spec.
    """
    return f"""
    FALLBACK SPEC (live fetch failed for {platform_spec})
    Source: Netflix Partner Help Center — IMF Delivery Specification

    Audio Requirements:
    - Integrated loudness: -24 LKFS (±1 LU), measured per ITU-R BS.1770-3
    - True peak: maximum -2 dBTP
    - Audio must be present in a separate IMF audio track

    Video Requirements:
    - IMF Application Profile: App #2E (preferred) or App #2
    - Frame rate must match CPL EditRate declaration
    - Color space and HDR metadata must match essence encoding

    Structural Requirements:
    - Valid ASSETMAP.xml with correct UUIDs and asset references
    - Valid PKL with SHA-1 hash for all assets
    - Valid CPL with ApplicationIdentification

    Note: This is a fallback. Fetch the authoritative spec at:
    https://partnerhelp.netflixstudios.com/hc/en-us/articles/115001138508
    """
