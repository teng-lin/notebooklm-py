"""Transport-neutral read-only source-content business logic.

This is the Click-free core behind the read-only source-content commands
(imported directly by the ``cli/source_cmd.py`` / ``cli/_source_render.py``
command layer): the data-fetch services behind ``source get`` / ``fulltext`` /
``guide`` / ``stale``, each returning a typed result the transport adapter
renders into its own envelope vocabulary.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..exceptions import ConfigurationError, SourceNotFoundError, ValidationError

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import Source, SourceFulltext

FulltextFormat = Literal["text", "markdown"]

#: Default cap on the returned body when ``max_chars`` is omitted — the content is
#: bounded (not dumped whole) so a large source can't flood a caller's context.
DEFAULT_CONTENT_CHARS = 10_000


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
# source read (existence/ready-gated fulltext with windowing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceReadPlan:
    """Prepared inputs for :func:`execute_source_read`.

    ``max_chars`` ``None`` applies :data:`DEFAULT_CONTENT_CHARS`; ``offset`` and a
    non-negative ``max_chars`` window the body (``content[offset:offset+max]``).
    """

    notebook_id: str
    source_id: str
    output_format: FulltextFormat = "text"
    max_chars: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class SourceReadResult:
    """The resolved source plus its (windowed) content.

    ``content`` is ``None`` and ``char_count`` 0 when the source is not READY
    (still processing / errored) or has no extractable text; ``char_count`` is
    always the FULL indexed length, and ``truncated`` reports whether the returned
    slice omits any remainder.
    """

    source: Source
    content: str | None
    char_count: int
    truncated: bool


async def execute_source_read(client: NotebookLMClient, plan: SourceReadPlan) -> SourceReadResult:
    """Read a source's body with an existence + ready gate and windowing.

    Mirrors the MCP ``source_read`` (detail="full") core so both the MCP tool and
    the REST content route share one implementation:

    * ``execute_source_get`` is the **existence guard** — a resolved id the backend
      no longer has raises :class:`SourceNotFoundError` (surfacing NOT_FOUND rather
      than a misleading empty success).
    * The body is fetched via ``execute_source_fulltext`` **only when the source is
      READY**. Gating on status (rather than catching the fulltext fetch's
      :class:`SourceNotFoundError`) keeps a genuine "source is gone" — e.g. deleted
      between the two calls — propagating as NOT_FOUND instead of masquerading as
      "no content".
    * ``output_format='markdown'`` needs the optional ``markdownify`` extra; an
      :class:`ImportError` on that path is remapped to a deterministic
      :class:`ConfigurationError` (the text path re-raises — a genuine bug).
    """
    if plan.max_chars is not None and plan.max_chars < 0:
        raise ValidationError(f"max_chars must be >= 0; got {plan.max_chars}")
    if plan.offset < 0:
        raise ValidationError(f"offset must be >= 0; got {plan.offset}")

    get_result = await execute_source_get(
        client, SourceGetPlan(notebook_id=plan.notebook_id, source_id=plan.source_id)
    )
    if get_result.source is None:
        raise SourceNotFoundError(plan.source_id)
    source = get_result.source

    content: str | None = None
    char_count = 0
    if source.is_ready:
        try:
            fulltext_result = await execute_source_fulltext(
                client,
                SourceFulltextPlan(
                    notebook_id=plan.notebook_id,
                    source_id=plan.source_id,
                    output_format=plan.output_format,
                ),
            )
        except ImportError as exc:
            if plan.output_format != "markdown":
                raise
            raise ConfigurationError(str(exc)) from exc
        content = fulltext_result.fulltext.content or None
        char_count = fulltext_result.fulltext.char_count

    truncated = False
    if content is not None:
        effective_max = DEFAULT_CONTENT_CHARS if plan.max_chars is None else plan.max_chars
        windowed = content[plan.offset : plan.offset + effective_max]
        truncated = len(windowed) < (len(content) - plan.offset)
        # Normalize an empty slice (e.g. offset past the end) to None, matching the
        # fetch-path contract (content is null when there's nothing to show).
        content = windowed or None

    return SourceReadResult(
        source=source, content=content, char_count=char_count, truncated=truncated
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
    "DEFAULT_CONTENT_CHARS",
    "FulltextFormat",
    "SourceFulltextPlan",
    "SourceFulltextResult",
    "SourceGetPlan",
    "SourceGetResult",
    "SourceGuidePlan",
    "SourceGuideResult",
    "SourceReadPlan",
    "SourceReadResult",
    "SourceStalePlan",
    "SourceStaleResult",
    "execute_source_fulltext",
    "execute_source_get",
    "execute_source_guide",
    "execute_source_read",
    "execute_source_stale",
]
