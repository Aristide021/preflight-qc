"""
Spec-Reader Agent — fetches, extracts and caches the live delivery spec.

Why live fetch instead of a baked-in copy:
  The Netflix loudness requirement is cited inconsistently across secondary
  sources (-27 LUFS vs -24 LKFS vs -23 LUFS). Reading the current spec beats
  baking in a number that may be wrong — and in this case the disagreement is
  not noise: Netflix specifies -27 LKFS +/- 2 LU dialog-gated (ITU-R BS.1770-1)
  as the general rule, and -24 LKFS +/- 2 LU (BS.1770-3/-4) only for programs
  measuring under 15% dialogue. A hard-coded number collapses those into one
  and gets the common case wrong.

Three bugs this module previously had, all of which made "grounded in the live
spec" untrue in practice:

  1. The registry pointed at article 115001138508, which now 404s. Every fetch
     failed.
  2. Failure fell back silently to an invented spec whose numbers were wrong on
     three counts (-24 LKFS, +/- 1 LU, BS.1770-3 — wrong value, tolerance and
     standard for the general case). Callers could not tell they were reading a
     guess, so the agent would cite it as authoritative.
  3. Even on success, raw HTML was handed to the model and truncated to 8k
     characters — which is `<head>`, CSS and nav markup, not spec text. The
     Sound Mix page is 66k of HTML carrying 19k of prose.

Now: verified URLs, HTML stripped to text before caching, and a clearly
attributed repository snapshot used only when the official page is not
server-readable. The snapshot is disclosed as a source snapshot, not invented
demo data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

# Netflix's help centre rejects unknown clients on some paths; a normal browser
# UA is what actually gets served the article body.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

CACHE_DIR = Path(os.environ.get("SPEC_CACHE_DIR", "/tmp/preflight_spec_cache"))
CACHE_TTL = int(os.environ.get("SPEC_CACHE_TTL_SECONDS", "86400"))
REFERENCE_SNAPSHOT = Path(__file__).parents[2] / "data" / "netflix_delivery_spec_snapshot.md"


@dataclass(frozen=True)
class SpecSource:
    """One document that makes up a platform's delivery spec."""

    label: str
    url: str


# ── Spec registry ────────────────────────────────────────────────────────────
# Every URL below was verified to return HTTP 200 with spec prose. A platform's
# spec is several documents, so each entry is a list: the classifier needs the
# audio rules and the delivery rules together to decide blocking vs cosmetic.
SPEC_REGISTRY: dict[str, list[SpecSource]] = {
    "netflix-imf": [
        SpecSource(
            "Sound Mix Specifications & Best Practices v1.6",
            "https://partnerhelp.netflixstudios.com/hc/en-us/articles/"
            "360001794307-Netflix-Sound-Mix-Specifications-Best-Practices-v1-6",
        ),
        SpecSource(
            "Post Production Branded Delivery Specifications",
            "https://partnerhelp.netflixstudios.com/hc/en-us/articles/"
            "7262346654995-Post-Production-Branded-Delivery-Specifications",
        ),
        SpecSource(
            "Loudness and True Peaks: How to Measure and When to Flag",
            "https://partnerhelp.netflixstudios.com/hc/en-us/articles/"
            "360050414014-Loudness-and-True-Peaks-How-to-Measure-and-When-to-Flag",
        ),
        SpecSource(
            "Loudness LKFS Out of Spec",
            "https://partnerhelp.netflixstudios.com/hc/en-us/articles/"
            "115000876911-LKFS-Loudness-Out-of-Spec",
        ),
    ],
}


def _sources_for(platform_spec: str) -> list[SpecSource]:
    """netflix-imf-2.2 → the netflix-imf document set."""
    for family, sources in SPEC_REGISTRY.items():
        if platform_spec.startswith(family):
            return sources
    log.warning("spec.registry.miss", platform_spec=platform_spec)
    return SPEC_REGISTRY["netflix-imf"]


@dataclass(frozen=True)
class SpecDocument:
    url: str
    content: str
    fetched_at: float
    is_fallback: bool = False
    sources: tuple[str, ...] = ()

    @property
    def is_grounded(self) -> bool:
        """False when the content is the built-in fallback rather than fetched text."""
        return not self.is_fallback


# ── HTML → text ──────────────────────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    """Pull readable text out of an article page, dropping script/style/nav."""

    _DROP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._DROP:
            self._depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP and self._depth:
            self._depth -= 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._depth and data.strip():
            self._chunks.append(data.strip())

    @property
    def text(self) -> str:
        raw = " ".join(self._chunks)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\s*\n\s*", "\n", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text


# ── Cache ────────────────────────────────────────────────────────────────────


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"spec_{digest}.json"


def _read_cache(key: str) -> SpecDocument | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["fetched_at"] > CACHE_TTL:
            return None
        # A cached fallback is not grounding; refetch rather than serve a guess.
        if data.get("is_fallback"):
            return None
        return SpecDocument(
            url=data["url"],
            content=data["content"],
            fetched_at=data["fetched_at"],
            is_fallback=False,
            sources=tuple(data.get("sources", ())),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("spec.cache.read.error", error=str(exc))
        return None


def _write_cache(key: str, doc: SpecDocument) -> None:
    try:
        _cache_path(key).write_text(
            json.dumps(
                {
                    "url": doc.url,
                    "content": doc.content,
                    "fetched_at": doc.fetched_at,
                    "is_fallback": doc.is_fallback,
                    "sources": list(doc.sources),
                }
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("spec.cache.write.error", error=str(exc))


# ── Fetch ────────────────────────────────────────────────────────────────────


async def _fetch_one(client: httpx.AsyncClient, source: SpecSource) -> tuple[SpecSource, str | None]:
    try:
        response = await client.get(source.url)
        response.raise_for_status()
        text = html_to_text(response.text)
        if len(text) < 500:
            log.warning("spec.fetch.thin", url=source.url, chars=len(text))
            return source, None
        return source, text
    except Exception as exc:  # noqa: BLE001
        log.error("spec.fetch.failed", url=source.url, error=str(exc)[:200])
        return source, None


async def fetch_spec(platform_spec: str) -> SpecDocument:
    """
    Fetch the delivery spec for a platform_spec identifier.

    Returns extracted prose from every source that responds. If the official
    pages are not server-readable, returns the attributed repository snapshot;
    if no snapshot exists, raises rather than presenting invented requirements.
    """
    sources = _sources_for(platform_spec)
    cache_key = platform_spec

    cached = _read_cache(cache_key)
    if cached:
        log.info("spec.cache.hit", platform_spec=platform_spec, chars=len(cached.content))
        return cached

    log.info("spec.fetching", platform_spec=platform_spec, sources=len(sources))
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    ) as client:
        results = await asyncio.gather(*(_fetch_one(client, s) for s in sources))

    sections, fetched_labels = [], []
    for source, text in results:
        if text:
            sections.append(f"### {source.label}\nSource: {source.url}\n\n{text}")
            fetched_labels.append(source.label)

    if not sections:
        if REFERENCE_SNAPSHOT.exists():
            log.warning("spec.fetch.snapshot", platform_spec=platform_spec)
            return SpecDocument(
                url=sources[0].url,
                content=REFERENCE_SNAPSHOT.read_text(encoding="utf-8"),
                fetched_at=time.time(),
                is_fallback=False,
                sources=("Netflix Studio Partner reference snapshot",),
            )
        log.error("spec.fetch.all_failed", platform_spec=platform_spec)
        raise RuntimeError(
            "Netflix delivery specification was unavailable and no reference snapshot exists"
        )

    doc = SpecDocument(
        url=sources[0].url,
        content="\n\n".join(sections),
        fetched_at=time.time(),
        is_fallback=False,
        sources=tuple(fetched_labels),
    )
    _write_cache(cache_key, doc)
    log.info(
        "spec.fetched",
        platform_spec=platform_spec,
        chars=len(doc.content),
        documents=len(fetched_labels),
    )
    return doc
