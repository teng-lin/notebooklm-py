"""Public research utilities (free functions; no client instance required).

Note: this is the *library* research helper module. The CLI command group lives
at ``notebooklm.cli.research`` — different file, different concern.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .types import CitedSourceSelection

logger = logging.getLogger(__name__)

_RESEARCH_RESULT_TYPE_ALIASES = {
    "web": 1,
    "drive": 2,
    "report": 5,
}

_URL_RE = r"https?://(?:[^\s<>\]\(\)\"']+|\([^\s<>\]\(\)\"']*\))+"
_URL_PATTERN = re.compile(_URL_RE)
_MARKDOWN_IMAGE_PATTERN = re.compile(rf"!\[[^\]]*\]\(({_URL_RE})(?:\s+[^\)]*)?\)")
_MARKDOWN_LINK_PATTERN = re.compile(rf"(?<!!)\[[^\]]+\]\(({_URL_RE})\)")
_TRAILING_URL_PUNCTUATION = ".,;:!?"


def normalize_url(url: str) -> str:
    """Normalize source/report URLs for citation matching."""
    parsed = urlsplit(url.rstrip(_TRAILING_URL_PUNCTUATION))
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
    )


def extract_report_urls(report: str) -> set[str]:
    """Extract normalized URLs from research report markdown/text."""
    if not report:
        return set()

    # Collect URL-like references from both markdown links and bare text,
    # then subtract markdown images because embedded assets are not citations.
    urls = {normalize_url(match.group(1)) for match in _MARKDOWN_LINK_PATTERN.finditer(report)}
    urls.update(normalize_url(match.group(0)) for match in _URL_PATTERN.finditer(report))
    image_urls = {
        normalize_url(match.group(1)) for match in _MARKDOWN_IMAGE_PATTERN.finditer(report)
    }
    urls.difference_update(image_urls)
    return {url for url in urls if url.startswith(("http://", "https://"))}


def select_cited_sources(
    sources: list[dict[str, Any]],
    report: str,
) -> CitedSourceSelection:
    """Return research sources cited by the completed report.

    Report entries are preserved so deep-research import still brings in the
    generated report itself. If no cited URL subset can be resolved, falls
    back to the original source list to avoid an empty import surprise.
    """
    cited_urls = extract_report_urls(report)
    if not cited_urls:
        logger.warning(
            "Cited-only research import requested, but no cited URLs were found; "
            "falling back to all importable research sources"
        )
        return CitedSourceSelection(
            sources=sources,
            cited_url_count=0,
            matched_url_source_count=0,
            used_fallback=True,
        )

    report_sources = [
        source
        for source in sources
        if source.get("result_type") == _RESEARCH_RESULT_TYPE_ALIASES["report"]
        and source.get("report_markdown")
    ]
    report_source_ids = {id(source) for source in report_sources}
    matched_url_sources = [
        source
        for source in sources
        if id(source) not in report_source_ids
        and isinstance(source.get("url"), str)
        and normalize_url(source["url"]) in cited_urls
    ]

    if not matched_url_sources:
        logger.warning(
            "Cited-only research import requested, but none of the report URLs "
            "matched research sources; falling back to all importable research sources"
        )
        return CitedSourceSelection(
            sources=sources,
            cited_url_count=len(cited_urls),
            matched_url_source_count=0,
            used_fallback=True,
        )

    return CitedSourceSelection(
        sources=[*report_sources, *matched_url_sources],
        cited_url_count=len(cited_urls),
        matched_url_source_count=len(matched_url_sources),
    )


__all__ = ["extract_report_urls", "normalize_url", "select_cited_sources"]
