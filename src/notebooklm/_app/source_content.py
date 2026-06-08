"""Transport-neutral read-only source-content business logic.

This is the Click-free core of ``cli/services/source_content.py``: the
data-fetch services behind ``source get`` / ``fulltext`` / ``guide`` /
``stale``, each returning a typed result the transport adapter renders into
its own envelope vocabulary.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import Source, SourceFulltext

FulltextFormat = Literal["text", "markdown"]


# ---------------------------------------------------------------------------
# source get
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceGetPlan:
    """Prepared inputs for ``execute_source_get``."""

    notebook_id: str
    source_id: str


@dataclass(frozen=True)
class SourceGetResult:
    """Fetched source details, or ``None`` when the backend no longer has it."""

    notebook_id: str
    source_id: str
    source: Source | None


async def execute_source_get(client: NotebookLMClient, plan: SourceGetPlan) -> SourceGetResult:
    """Fetch a single source."""
    src = await client.sources.get_or_none(plan.notebook_id, plan.source_id)
    return SourceGetResult(notebook_id=plan.notebook_id, source_id=plan.source_id, source=src)


# ---------------------------------------------------------------------------
# source fulltext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFulltextPlan:
    """Prepared inputs for ``execute_source_fulltext``."""

    notebook_id: str
    source_id: str
    output_format: FulltextFormat


@dataclass(frozen=True)
class SourceFulltextResult:
    """Fetched fulltext content for a source."""

    fulltext: SourceFulltext


async def execute_source_fulltext(
    client: NotebookLMClient, plan: SourceFulltextPlan
) -> SourceFulltextResult:
    """Fetch a source's full indexed text content."""
    fulltext = await client.sources.get_fulltext(
        plan.notebook_id, plan.source_id, output_format=plan.output_format
    )
    return SourceFulltextResult(fulltext=fulltext)


# ---------------------------------------------------------------------------
# source guide
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceGuidePlan:
    """Prepared inputs for ``execute_source_guide``."""

    notebook_id: str
    source_id: str


@dataclass(frozen=True)
class SourceGuideResult:
    """Fetched source-guide content."""

    source_id: str
    summary: str
    keywords: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        """Whether the backend returned no guide content."""
        return not self.summary.strip() and not self.keywords


async def execute_source_guide(
    client: NotebookLMClient, plan: SourceGuidePlan
) -> SourceGuideResult:
    """Fetch an AI-generated source summary and keywords."""
    guide = await client.sources.get_guide(plan.notebook_id, plan.source_id)
    summary = guide.summary
    keywords = guide.keywords
    keyword_strings = (
        tuple(
            keyword.strip() for keyword in keywords if isinstance(keyword, str) and keyword.strip()
        )
        if isinstance(keywords, (list, tuple))
        else ()
    )
    return SourceGuideResult(
        source_id=plan.source_id,
        summary=summary if isinstance(summary, str) else "",
        keywords=keyword_strings,
    )


# ---------------------------------------------------------------------------
# source stale
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceStalePlan:
    """Prepared inputs for ``execute_source_stale``."""

    notebook_id: str
    source_id: str


@dataclass(frozen=True)
class SourceStaleResult:
    """Freshness predicate for a URL/Drive source."""

    notebook_id: str
    source_id: str
    is_fresh: bool

    @property
    def stale(self) -> bool:
        """Whether the source needs refresh."""
        return not self.is_fresh


async def execute_source_stale(
    client: NotebookLMClient, plan: SourceStalePlan
) -> SourceStaleResult:
    """Check if a URL/Drive source needs refresh."""
    is_fresh = await client.sources.check_freshness(plan.notebook_id, plan.source_id)
    return SourceStaleResult(
        notebook_id=plan.notebook_id,
        source_id=plan.source_id,
        is_fresh=is_fresh,
    )


__all__ = [
    "FulltextFormat",
    "SourceFulltextPlan",
    "SourceFulltextResult",
    "SourceGetPlan",
    "SourceGetResult",
    "SourceGuidePlan",
    "SourceGuideResult",
    "SourceStalePlan",
    "SourceStaleResult",
    "execute_source_fulltext",
    "execute_source_get",
    "execute_source_guide",
    "execute_source_stale",
]
