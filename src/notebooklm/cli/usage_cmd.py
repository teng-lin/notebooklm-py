"""Account-scoped live compute usage CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import click
from rich.table import Table

from ..types import UsageAction, UsageActionKind, UsageSummary, UsageSummaryStatus, UsageWindowKind
from .auth_runtime import run_client_workflow
from .options import json_option
from .rendering import cli_print, json_output_response

if TYPE_CHECKING:
    from ..client import NotebookLMClient


# Presentation labels are separate from the exact protocol enum names. In the
# recorded Web translator, video format 3 maps to action 3 (Cinematic video).
# See docs/android/usage-quota-evidence.md; NOS has no verified public mapping.
_CATEGORY_LABELS = {
    UsageActionKind.AUDIO_OVERVIEW: "Audio overview",
    UsageActionKind.VIDEO_OVERVIEW: "Video overview",
    UsageActionKind.BREAKDOWNS_VIDEO: "Cinematic video",
    UsageActionKind.SHORTS_VIDEO: "Short video",
    UsageActionKind.INFOGRAPHIC: "Infographic",
    UsageActionKind.SLIDES: "Slide deck",
    UsageActionKind.REPORTS: "Reports",
    UsageActionKind.TABLES: "Data table",
    UsageActionKind.FLASHCARDS: "Flashcards",
    UsageActionKind.QUIZ: "Quiz",
    UsageActionKind.MINDMAP: "Mind map",
    UsageActionKind.CANVAS: "Canvas",
    UsageActionKind.SLIDES_EDITING: "Slide editing",
    UsageActionKind.FLASHCARD_EDITING: "Flashcard editing",
    UsageActionKind.DEEP_RESEARCH: "Deep research",
    UsageActionKind.NOS: "Unmapped category (NOS)",
    UsageActionKind.FAST_RESEARCH: "Fast research",
    UsageActionKind.QNA: "Chat Q&A",
    UsageActionKind.NOS_IMAGE_GENERATION: "Image generation (NOS)",
    UsageActionKind.GUIDED_VIEW: "Guided view",
    UsageActionKind.DOCUMENT_GUIDE: "Source guide",
    UsageActionKind.SUGGESTION_CHIPS: "Suggested questions",
}


def _category_label(action: UsageAction) -> str:
    """Name known features while keeping unmapped category codes identifiable."""
    if action.kind is None:
        return f"Unknown category ({action.code})"
    return _CATEGORY_LABELS[action.kind]


def _usage_json(summary: UsageSummary) -> dict[str, Any]:
    """Project the snapshot with named enums and explicit ISO 8601 timestamps."""
    active = summary.active_window
    return {
        "status": summary.status.value,
        "enabled": summary.enabled,
        "available": summary.available,
        "is_exhausted": summary.is_exhausted,
        "active_window": active.kind.name.lower() if active is not None else None,
        "windows": [
            {
                "kind": window.kind.name.lower(),
                "used_percent": window.used_percent,
                "remaining_percent": window.remaining_percent,
                "resets_at": window.resets_at.isoformat(),
            }
            for window in summary.windows
        ],
        "actions": [
            {
                "code": action.code,
                "kind": action.kind.name.lower() if action.kind is not None else None,
                "has_sufficient_quota": action.has_sufficient_quota,
                "cost_tier": action.cost_tier.name.lower()
                if action.cost_tier is not None
                else None,
                "remaining_deferred_artifact_generations": (
                    action.remaining_deferred_artifact_generations
                ),
                "estimated_cost_percent": action.estimated_cost_percent,
            }
            for action in summary.actions
        ],
    }


def _render_usage(summary: UsageSummary, *, categories: bool) -> None:
    """Display meter availability, reset windows, and optional category details."""
    if summary.status is UsageSummaryStatus.DISABLED:
        cli_print("Live compute metering is not enabled for this account (disabled).")
        return
    if summary.status is UsageSummaryStatus.SKIPPED:
        cli_print("Live compute usage is temporarily unavailable (skipped). Try again later.")
        return

    table = Table(title="Live compute usage")
    table.add_column("Window", style="cyan")
    table.add_column("Used", justify="right")
    table.add_column("Remaining", justify="right")
    table.add_column("Resets at (UTC)")
    for window in summary.windows:
        label = "Five-hour" if window.kind is UsageWindowKind.FIVE_HOUR else "Weekly"
        table.add_row(
            label,
            f"{window.used_percent:.2f}%",
            f"{window.remaining_percent:.2f}%",
            window.resets_at.isoformat(),
        )
    cli_print(table)
    if summary.is_exhausted:
        cli_print("[yellow]The active compute usage window is exhausted.[/yellow]")

    if not categories:
        cli_print("[dim]Use --categories for availability and estimated costs by category.[/dim]")
        return
    if not summary.actions:
        cli_print("No usage category details were supplied by the server.")
        return

    table = Table(title="Usage categories")
    table.add_column("Code", justify="right")
    table.add_column("Category", style="cyan")
    table.add_column("Quota")
    table.add_column("Cost tier")
    table.add_column("Est. cost*", justify="right")
    table.add_column("Deferred left", justify="right")
    for action in summary.actions:
        table.add_row(
            str(action.code),
            _category_label(action),
            "Sufficient" if action.has_sufficient_quota else "Insufficient",
            action.cost_tier.name.lower().replace("_", " ")
            if action.cost_tier is not None
            else "Unknown",
            f"{action.estimated_cost_percent:.2f}%"
            if action.estimated_cost_percent is not None
            else "Unknown",
            str(action.remaining_deferred_artifact_generations)
            if action.remaining_deferred_artifact_generations is not None
            else "Unknown",
        )
    cli_print(table)
    cli_print(
        "[dim]* Estimates matched the five-hour budget in recorded tests. The server does not "
        "name the window; final usage may differ.[/dim]"
    )
    if any(
        action.kind in (UsageActionKind.NOS, UsageActionKind.NOS_IMAGE_GENERATION)
        for action in summary.actions
    ):
        cli_print(
            "[dim]NOS is an internal server name; its public feature mapping is unverified.[/dim]"
        )


@click.command("usage")
@click.option(
    "--categories",
    "--actions",
    "show_categories",
    is_flag=True,
    help="Include usage categories, availability, and estimated costs.",
)
@json_option
@click.pass_context
def usage(ctx: click.Context, show_categories: bool, json_output: bool) -> None:
    """Show live compute usage for the current account.

    Displays five-hour and weekly percentages and server-provided reset times.

    No active notebook is required. Supports both Web and Android backends.
    JSON always includes category details in its actions array. Use --categories
    (alias --actions) to add them to text output.

    \b
    Examples:
      notebooklm usage
      notebooklm usage --categories
      notebooklm -p work usage --json
      notebooklm --backend android usage --json
    """

    async def body(client: NotebookLMClient) -> UsageSummary:
        return await client.settings.get_usage()

    summary = run_client_workflow(ctx, command_name="usage", json_output=json_output, body=body)
    if json_output:
        json_output_response(_usage_json(summary))
    else:
        _render_usage(summary, categories=show_categories)
