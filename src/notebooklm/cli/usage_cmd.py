"""Account-scoped live compute usage CLI command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import click
from rich.table import Table

from ..types import UsageSummary, UsageSummaryStatus, UsageWindowKind
from .auth_runtime import run_client_workflow
from .options import json_option
from .rendering import cli_print, json_output_response

if TYPE_CHECKING:
    from ..client import NotebookLMClient


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


def _render_usage(summary: UsageSummary, *, actions: bool) -> None:
    """Display meter availability, reset windows, and optional action details."""
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

    if not actions:
        cli_print("[dim]Use --actions for action availability and advertised costs.[/dim]")
        return
    if not summary.actions:
        cli_print("No action usage details were supplied by the server.")
        return

    table = Table(title="Action availability and advertised costs")
    table.add_column("Code", justify="right")
    table.add_column("Action", style="cyan")
    table.add_column("Quota")
    table.add_column("Cost tier")
    table.add_column("Est. cost", justify="right")
    table.add_column("Deferred left", justify="right")
    for action in summary.actions:
        table.add_row(
            str(action.code),
            action.kind.name.lower().replace("_", " ") if action.kind is not None else "Unknown",
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


@click.command("usage")
@click.option("--actions", is_flag=True, help="Include action availability and advertised costs.")
@json_option
@click.pass_context
def usage(ctx: click.Context, actions: bool, json_output: bool) -> None:
    """Show live compute usage for the current account.

    Displays five-hour and weekly percentages and server-provided reset times.

    No active notebook is required. Supports both Web and Android backends.
    JSON always includes action details; --actions adds them to text output.

    \b
    Examples:
      notebooklm usage
      notebooklm usage --actions
      notebooklm -p work usage --json
      notebooklm --backend android usage --json
    """

    async def body(client: NotebookLMClient) -> UsageSummary:
        return await client.settings.get_usage()

    summary = run_client_workflow(ctx, command_name="usage", json_output=json_output, body=body)
    if json_output:
        json_output_response(_usage_json(summary))
    else:
        _render_usage(summary, actions=actions)
