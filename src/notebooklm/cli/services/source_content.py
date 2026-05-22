"""Services for read-only source-content commands: get, fulltext, guide, stale.

Each command has its own plan + executor pair so the ``cli/source_cmd.py``
Click handler collapses to a one-call wrapper. Output rendering stays in
the executors (rather than returning a result object the handler renders)
because the pre-extraction handlers thread text-mode console.print calls
inline with the fetch — preserving that order is what keeps the
characterization-test snapshots byte-for-byte stable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ...types import source_status_to_str
from ..error_handler import exit_with_code, output_error
from ..rendering import console, get_source_type_display, json_output_response

if TYPE_CHECKING:
    from ...client import NotebookLMClient

FulltextFormat = Literal["text", "markdown"]


# ---------------------------------------------------------------------------
# source get
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceGetPlan:
    """Prepared inputs for ``execute_source_get``."""

    notebook_id: str
    source_id: str
    json_output: bool


async def execute_source_get(client: NotebookLMClient, plan: SourceGetPlan) -> None:
    """Fetch + render a single source.

    Exit-code contract: 0 on success, 1 if the source is not found.
    """
    src = await client.sources.get(plan.notebook_id, plan.source_id)

    # BREAKING (P1.T2): not-found exits 1 with a typed error instead of
    # the previous exit-0 ``found: false`` placeholder. See
    # ``docs/cli-exit-codes.md`` and the BREAKING entry in CHANGELOG.md
    # (Unreleased → Changed). The trailing ``raise AssertionError``
    # unreachable narrows the ``Source | None`` for mypy.
    if src is None:
        output_error(
            "Source not found",
            code="NOT_FOUND",
            json_output=plan.json_output,
            exit_code=1,
            extra={"source_id": plan.source_id, "notebook_id": plan.notebook_id},
        )
        raise AssertionError("unreachable")  # pragma: no cover

    if plan.json_output:
        json_output_response(
            {
                "source": {
                    "id": src.id,
                    "title": src.title,
                    "type": str(src.kind),
                    "url": src.url,
                    "status": source_status_to_str(src.status),
                    "status_id": src.status,
                    "created_at": (src.created_at.isoformat() if src.created_at else None),
                },
                "found": True,
            }
        )
        return

    console.print(f"[bold cyan]Source:[/bold cyan] {src.id}")
    console.print(f"[bold]Title:[/bold] {src.title}")
    console.print(f"[bold]Type:[/bold] {get_source_type_display(src.kind)}")
    if src.url:
        console.print(f"[bold]URL:[/bold] {src.url}")
    if src.created_at:
        console.print(f"[bold]Created:[/bold] {src.created_at.strftime('%Y-%m-%d %H:%M')}")


# ---------------------------------------------------------------------------
# source fulltext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFulltextPlan:
    """Prepared inputs for ``execute_source_fulltext``."""

    notebook_id: str
    source_id: str
    json_output: bool
    output: str | None
    output_format: FulltextFormat


async def execute_source_fulltext(client: NotebookLMClient, plan: SourceFulltextPlan) -> None:
    """Fetch + render (or save) a source's full indexed text content."""

    async def _fetch():
        return await client.sources.get_fulltext(
            plan.notebook_id, plan.source_id, output_format=plan.output_format
        )

    if plan.json_output:
        fulltext = await _fetch()
    else:
        with console.status("Fetching fulltext content..."):
            fulltext = await _fetch()

    if plan.json_output:
        # P1.T2 bug 4: when both --json and -o are given, write the
        # (potentially multi-MB) content to disk and emit a small
        # metadata envelope on stdout — not the full content twice.
        if plan.output:
            content_bytes = fulltext.content.encode("utf-8")
            Path(plan.output).write_bytes(content_bytes)
            json_output_response(
                {
                    "path": str(plan.output),
                    "bytes": len(content_bytes),
                    "source_id": fulltext.source_id,
                    "title": fulltext.title,
                }
            )
            return

        json_output_response(asdict(fulltext))
        return

    if plan.output:
        Path(plan.output).write_text(fulltext.content, encoding="utf-8")
        console.print(f"[green]Saved {fulltext.char_count} chars to {plan.output}[/green]")
        return

    console.print(f"[bold cyan]Source:[/bold cyan] {fulltext.source_id}")
    console.print(f"[bold]Title:[/bold] {fulltext.title}")
    console.print(f"[bold]Characters:[/bold] {fulltext.char_count:,}")
    if fulltext.url:
        console.print(f"[bold]URL:[/bold] {fulltext.url}")
    console.print()
    console.print("[bold cyan]Content:[/bold cyan]")
    # ``markup=False`` so markdown links like ``[text](url)`` are not
    # eaten by Rich's tag parser.
    if len(fulltext.content) > 2000:
        console.print(fulltext.content[:2000], markup=False, highlight=False)
        console.print(
            f"\n[dim]... ({fulltext.char_count - 2000:,} more chars, "
            "use -o to save full content)[/dim]"
        )
    else:
        console.print(fulltext.content, markup=False, highlight=False)


# ---------------------------------------------------------------------------
# source guide
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceGuidePlan:
    """Prepared inputs for ``execute_source_guide``."""

    notebook_id: str
    source_id: str
    json_output: bool


async def execute_source_guide(client: NotebookLMClient, plan: SourceGuidePlan) -> None:
    """Fetch + render an AI-generated source summary and keywords."""

    async def _fetch_guide():
        return await client.sources.get_guide(plan.notebook_id, plan.source_id)

    if plan.json_output:
        guide = await _fetch_guide()
    else:
        with console.status("Generating source guide..."):
            guide = await _fetch_guide()

    if plan.json_output:
        json_output_response(
            {
                "source_id": plan.source_id,
                "summary": guide.get("summary", ""),
                "keywords": guide.get("keywords", []),
            }
        )
        return

    summary = guide.get("summary", "").strip()
    keywords = guide.get("keywords", [])

    if not summary and not keywords:
        console.print("[yellow]No guide available for this source[/yellow]")
        return

    if summary:
        console.print("[bold cyan]Summary:[/bold cyan]")
        console.print(summary)
        console.print()

    if keywords:
        console.print("[bold cyan]Keywords:[/bold cyan]")
        console.print(", ".join(keywords))


# ---------------------------------------------------------------------------
# source stale
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceStalePlan:
    """Prepared inputs for ``execute_source_stale``."""

    notebook_id: str
    source_id: str
    json_output: bool


async def execute_source_stale(client: NotebookLMClient, plan: SourceStalePlan) -> None:
    """Check if a URL/Drive source needs refresh; exit 0 if stale, 1 if fresh.

    Inverted exit-code semantics are intentional — see
    ``docs/cli-exit-codes.md``. The JSON body carries an explicit
    ``stale`` boolean so callers who prefer to branch on a field rather
    than the exit code can do so.
    """
    is_fresh = await client.sources.check_freshness(plan.notebook_id, plan.source_id)
    stale = not is_fresh

    if plan.json_output:
        json_output_response(
            {
                "source_id": plan.source_id,
                "notebook_id": plan.notebook_id,
                "stale": stale,
                "fresh": is_fresh,
            }
        )
        # Inverted exit codes preserved by design.
        exit_with_code(0 if stale else 1)

    if is_fresh:
        console.print("[green]✓ Source is fresh[/green]")
        exit_with_code(1)
    else:
        console.print("[yellow]⚠ Source is stale[/yellow]")
        console.print("[dim]Run 'source refresh' to update[/dim]")
        exit_with_code(0)


__all__ = [
    "SourceFulltextPlan",
    "SourceGetPlan",
    "SourceGuidePlan",
    "SourceStalePlan",
    "execute_source_fulltext",
    "execute_source_get",
    "execute_source_guide",
    "execute_source_stale",
]
