"""CLI rendering helpers.

This module owns stdout/stderr formatting, Rich display helpers, and CLI-facing
source/artifact display labels. ``cli.helpers`` remains a compatibility facade
for older imports and patch targets.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import click
from rich.console import Console
from rich.table import Table

from ..types import ArtifactType

if TYPE_CHECKING:
    from ..types import Artifact
    from .services.listing import ListRender


def _resolve_quiet(ctx: click.Context | None) -> bool:
    """Resolve root ``--quiet`` without importing ``cli.runtime``."""
    if ctx is None:
        ctx = click.get_current_context(silent=True)
        if ctx is None:
            return False
    try:
        value = ctx.find_root().params.get("quiet", False)
    except (AttributeError, RuntimeError):
        return False
    return value if isinstance(value, bool) else False


console = Console()
# Diagnostic / status output in --json mode must go to stderr so stdout stays
# parseable JSON for automation.
stderr_console = Console(stderr=True)


def _emit_status(
    msg: str,
    *,
    json_output: bool,
    style: str | None = None,
    quiet: bool = False,
    stdout_console: Console = console,
    stderr_output_console: Console = stderr_console,
) -> None:
    # Shared implementation for callers that need explicit stdout/stderr
    # console injection while preserving the public ``emit_status`` wrapper.
    # Quiet suppresses status prose only. Errors and JSON payloads use their
    # own output paths and are intentionally unaffected.
    if quiet or _resolve_quiet(None):
        return
    target = stderr_output_console if json_output else stdout_console
    if style is not None:
        target.print(msg, style=style)
    else:
        target.print(msg)


def emit_status(
    msg: str,
    *,
    json_output: bool,
    style: str | None = None,
    quiet: bool = False,
    stdout_console: Console = console,
    stderr_output_console: Console = stderr_console,
) -> None:
    """Emit a status / diagnostic line.

    Args:
        msg: The status message to print (Rich markup allowed).
        json_output: When True, route the line to stderr so stdout stays
            parseable JSON for automation. When False, route to stdout.
        style: Optional Rich style string (e.g. ``"bold red"``).
        quiet: When True, suppress entirely. The active root ``--quiet`` flag
            is also honored automatically inside a Click invocation.
        stdout_console: Override target for non-JSON mode (defaults to the
            module-level ``console`` — patchable by callers).
        stderr_output_console: Override target for JSON mode (defaults to
            the module-level ``stderr_console``).
    """
    _emit_status(
        msg,
        json_output=json_output,
        style=style,
        quiet=quiet,
        stdout_console=stdout_console,
        stderr_output_console=stderr_output_console,
    )


def cli_print(
    *args: Any,
    ctx: click.Context | None = None,
    output_console: Console | None = None,
    **kwargs: Any,
) -> None:
    """Quiet-aware drop-in replacement for ``console.print(...)``.

    Use this for CLI status prose that should disappear under root
    ``--quiet``. Outside a Click invocation, it forwards normally.

    Args:
        *args: Positional args forwarded to ``console.print`` (e.g. a
            Rich-markup string, a ``Table``, a ``Panel``).
        ctx: Optional Click context. When omitted, the helper consults
            ``click.get_current_context(silent=True)``; outside any Click
            context (library importers, direct unit tests) the helper
            forwards unconditionally so non-CLI callers are unaffected.
        output_console: Optional Console override. Defaults to the module-
            level ``console`` so tests that patch ``rendering.console``
            (or ``helpers.console``) still see the print routed through
            their patched target.
        **kwargs: Forwarded to ``console.print`` (``style``, ``markup``,
            ``highlight``, etc.).

    Note:
        Errors must NOT route through this helper — ``--quiet`` silences
        *status*, not *errors*. Error paths use ``_output_error`` (which
        writes to stderr unconditionally) or, for JSON envelopes,
        ``json_error_response``.
    """
    if _resolve_quiet(ctx):
        return
    target = output_console if output_console is not None else console
    target.print(*args, **kwargs)


def cli_status(
    status_message: str,
    *,
    ctx: click.Context | None = None,
    output_console: Console | None = None,
    **kwargs: Any,
):
    """Quiet-aware replacement for ``console.status(...)``.

    Returns a context manager that wraps a Rich spinner. Under ``--quiet``
    (resolved the same way as :func:`cli_print`), the spinner is replaced
    with a no-op context manager so no animation reaches the terminal.

    Args:
        status_message: The status text shown next to the spinner.
        ctx: Optional Click context (see :func:`cli_print`).
        output_console: Optional Console override.
        **kwargs: Forwarded to ``console.status`` (e.g. ``spinner=`` kwarg).
    """
    if _resolve_quiet(ctx):
        return contextlib.nullcontext()
    target = output_console if output_console is not None else console
    return target.status(status_message, **kwargs)


_CLI_ARTIFACT_ALIASES = {
    "flashcard": "flashcards",  # CLI uses singular, enum uses plural
}


def cli_name_to_artifact_type(name: str) -> ArtifactType | None:
    """Convert CLI artifact type name to ArtifactType enum."""
    if name == "all":
        return None

    name = _CLI_ARTIFACT_ALIASES.get(name, name)
    enum_name = name.upper().replace("-", "_")
    return ArtifactType.__members__.get(enum_name)


def json_output_response(data: dict | list) -> None:
    """Print JSON response (no colors for machine parsing)."""
    click.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def json_error_response(code: str, message: str, extra: dict | None = None) -> NoReturn:
    """Print JSON error and exit (no colors for machine parsing)."""
    from .error_handler import output_error

    output_error(message, code, json_output=True, exit_code=1, extra=extra)
    raise AssertionError("unreachable")  # pragma: no cover


_RESULT_TYPE_LABELS = {
    1: "Web",
    2: "Drive",
    5: "Report",
    "web": "Web",
    "drive": "Drive",
    "report": "Report",
}


def _display_research_sources(
    sources: list[dict], max_display: int = 10, *, output_console: Console = console
) -> None:
    # ``cli.helpers`` calls this private variant to inject its compatibility
    # ``console`` patch target instead of binding to this module's Console.
    output_console.print(f"[bold]Found {len(sources)} sources[/bold]")

    if sources:
        has_types = any("result_type" in s for s in sources)

        table = Table(show_header=True, header_style="bold")
        table.add_column("Title", style="cyan")
        if has_types:
            table.add_column("Type", style="yellow")
        table.add_column("URL", style="dim")
        for src in sources[:max_display]:
            row = [src.get("title", "Untitled")[:50]]
            if has_types:
                rt: int | None = src.get("result_type")
                label = (
                    _RESULT_TYPE_LABELS.get(rt, str(rt) if rt is not None else "")
                    if rt is not None
                    else ""
                )
                row.append(label)
            row.append(src.get("url", "")[:60])
            table.add_row(*row)
        if len(sources) > max_display:
            extra_row = [f"... and {len(sources) - max_display} more"]
            if has_types:
                extra_row.append("")
            extra_row.append("")
            table.add_row(*extra_row)
        output_console.print(table)


def display_research_sources(sources: list[dict], max_display: int = 10) -> None:
    """Display research sources in a formatted table."""
    _display_research_sources(sources, max_display, output_console=console)


def _display_report(
    report: str,
    max_chars: int = 1000,
    json_hint: bool = True,
    *,
    output_console: Console = console,
) -> None:
    # ``cli.helpers`` calls this private variant to inject its compatibility
    # ``console`` patch target instead of binding to this module's Console.
    if not report:
        return
    output_console.print("\n[bold]Report:[/bold]")
    output_console.print(report[:max_chars], markup=False)
    if len(report) > max_chars:
        hint = " use --json for full report" if json_hint else ""
        output_console.print(
            f"[dim]... (truncated,{hint})[/dim]" if hint else "[dim]... (truncated)[/dim]"
        )


def display_report(report: str, max_chars: int = 1000, json_hint: bool = True) -> None:
    """Display a research report, truncated for terminal output."""
    _display_report(report, max_chars, json_hint, output_console=console)


def get_artifact_type_display(artifact: Artifact) -> str:
    """Get display string for artifact type."""
    kind = artifact.kind

    display_map = {
        ArtifactType.AUDIO: "🎧 Audio",
        ArtifactType.VIDEO: "🎬 Video",
        ArtifactType.QUIZ: "📝 Quiz",
        ArtifactType.FLASHCARDS: "🃏 Flashcards",
        ArtifactType.MIND_MAP: "🧠 Mind Map",
        ArtifactType.INFOGRAPHIC: "🖼️ Infographic",
        ArtifactType.SLIDE_DECK: "📊 Slide Deck",
        ArtifactType.DATA_TABLE: "📈 Data Table",
    }

    if kind == ArtifactType.REPORT:
        report_displays = {
            "briefing_doc": "📋 Briefing Doc",
            "study_guide": "📚 Study Guide",
            "blog_post": "✍️ Blog Post",
            "report": "📄 Report",
        }
        return report_displays.get(artifact.report_subtype or "report", "📄 Report")

    fallback_label = kind.name if hasattr(kind, "name") else kind
    return display_map.get(kind, f"Unknown ({fallback_label})")


def get_source_type_display(source_type: str) -> str:
    """Get display string for source type."""
    type_str = source_type.value if hasattr(source_type, "value") else str(source_type)
    type_map = {
        "google_docs": "📄 Google Docs",
        "google_slides": "📊 Google Slides",
        "google_spreadsheet": "📊 Google Sheets",
        "pdf": "📄 PDF",
        "pasted_text": "📝 Pasted Text",
        "docx": "📝 DOCX",
        "web_page": "🌐 Web Page",
        "markdown": "📝 Markdown",
        "youtube": "🎬 YouTube",
        "media": "🎵 Media",
        "google_drive_audio": "🎧 Drive Audio",
        "google_drive_video": "🎬 Drive Video",
        "image": "🖼️ Image",
        "csv": "📊 CSV",
        "epub": "📕 EPUB",
        "unknown": "❓ Unknown",
    }
    return type_map.get(type_str, f"❓ {type_str}")


def _list_column_options(header: str, *, no_truncate: bool) -> dict[str, Any]:
    """Return the default Rich column options for common list-table headers.

    Layering note: this lives in ``cli.rendering`` (presentation layer) so
    ``cli.services.listing`` stays free of Rich/Click imports per ADR-0008.
    """
    title_overflow: Literal["fold", "ellipsis"] = "fold" if no_truncate else "ellipsis"
    if header == "ID":
        return {"style": "cyan"}
    if header == "Title":
        return {"style": "green", "overflow": title_overflow}
    if header == "Created":
        return {"style": "dim"}
    if header == "Status":
        return {"style": "yellow"}
    if header == "Preview":
        return {"style": "dim", "max_width": 50}
    return {}


def render_list(render: ListRender) -> None:
    """Render a :class:`~notebooklm.cli.services.listing.ListRender` payload.

    Handles all three modes the service can emit:

    - JSON mode (``render.json_envelope`` set) → write the envelope to stdout
      via :func:`json_output_response`.
    - Empty-state text mode (``render.empty_message`` set) → print the
      command-supplied placeholder via the Rich console.
    - Standard table mode → build a Rich :class:`~rich.table.Table` from the
      render's columns / rows / column-option overrides and print it.

    This is the only place that touches ``rich.table.Table`` and the global
    ``console`` for list commands; ``cli.services.listing`` returns pure data.
    """
    if render.json_envelope is not None:
        json_output_response(render.json_envelope)
        return

    if render.empty_message is not None:
        console.print(render.empty_message)
        return

    table = Table(title=render.title)
    for header in render.columns:
        options = _list_column_options(header, no_truncate=render.no_truncate)
        override = render.column_options.get(header)
        if override:
            options.update(override)
        table.add_column(header, **options)
    for row in render.rows:
        table.add_row(*row)
    console.print(table)
