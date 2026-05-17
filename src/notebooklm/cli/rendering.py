"""CLI rendering helpers.

This module owns stdout/stderr formatting, Rich display helpers, and CLI-facing
source/artifact display labels. ``cli.helpers`` remains a compatibility facade
for older imports and patch targets.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

import click
from rich.console import Console
from rich.table import Table

from ..types import ArtifactType

if TYPE_CHECKING:
    from ..types import Artifact

console = Console()
# Diagnostic / status output in --json mode must go to stderr so stdout stays
# parseable JSON for automation.
stderr_console = Console(stderr=True)


def _emit_status(
    msg: str,
    *,
    json_output: bool,
    style: str | None = None,
    stdout_console: Console = console,
    stderr_output_console: Console = stderr_console,
) -> None:
    # ``cli.helpers`` calls this private variant to preserve helper-level
    # Console patch seams while the public rendering helper uses local defaults.
    target = stderr_output_console if json_output else stdout_console
    if style is not None:
        target.print(msg, style=style)
    else:
        target.print(msg)


def emit_status(msg: str, *, json_output: bool, style: str | None = None) -> None:
    """Emit a status / diagnostic line."""
    _emit_status(
        msg,
        json_output=json_output,
        style=style,
        stdout_console=console,
        stderr_output_console=stderr_console,
    )


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
    response: dict[str, Any] = {"error": True, "code": code, "message": message}
    if extra:
        response.update(extra)
    click.echo(json.dumps(response, indent=2, default=str, ensure_ascii=False))
    raise SystemExit(1)


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
