"""Diagnostic and migration CLI command.

Commands:
    doctor   Check profile setup, auth, and migration status

The four checks + automatic fixes + health aggregation live in the
transport-neutral :mod:`notebooklm._app.doctor`. This module owns the Rich
rendering, the ``--json`` envelope, and the exit codes, and forwards the path
helpers (read off this module at call time so the
``patch("...doctor_cmd.get_storage_path")`` seam keeps landing) into the
neutral ``run_checks``.
"""

import click
from rich.table import Table

from .._app.doctor import DoctorPaths, DoctorReport, run_checks
from ..paths import (
    get_config_path,
    get_home_dir,
    get_path_info,
    get_profile_dir,
    get_storage_path,
)
from .error_handler import exit_with_code, handle_errors
from .rendering import console, json_output_response


def _doctor_paths() -> DoctorPaths:
    """Bundle this module's path helpers for the neutral ``run_checks``.

    Each callable is resolved off the module global at call time so a
    ``patch("notebooklm.cli.doctor_cmd.<helper>", ...)`` test seam lands.
    """
    return DoctorPaths(
        get_path_info=get_path_info,
        get_home_dir=get_home_dir,
        get_profile_dir=get_profile_dir,
        get_storage_path=get_storage_path,
        get_config_path=get_config_path,
    )


def register_doctor_command(cli):
    """Register the doctor command on the main CLI group."""

    @cli.command("doctor")
    @click.option("--fix", "fix_issues", is_flag=True, help="Attempt to fix detected issues")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def doctor(fix_issues, json_output):
        """Check profile setup, auth status, and migration.

        Diagnoses common issues with profiles, authentication, and directory
        structure. Use --fix to automatically repair detected problems.

        \b
        Examples:
          notebooklm doctor           # Check for issues
          notebooklm doctor --fix     # Fix detected issues
          notebooklm doctor --json    # Machine-readable output
        """
        if json_output:
            with handle_errors(json_output=True):
                _run_doctor(fix_issues, json_output=True)
            return
        _run_doctor(fix_issues, json_output=False)


def _run_doctor(fix_issues: bool, *, json_output: bool) -> None:
    """Run doctor checks and emit either JSON or rich text output."""
    # The four checks + automatic fixes + health aggregation are
    # transport-neutral and live in ``_app.doctor``. Path helpers are forwarded
    # via ``_doctor_paths`` (read off this module at call time so the
    # ``patch("...doctor_cmd.get_storage_path")`` seam lands); an unexpected
    # ``OSError`` from one of them propagates here for ``handle_errors`` to wrap.
    report = run_checks(fix=fix_issues, paths=_doctor_paths())

    # Output
    if json_output:
        result: dict = {
            "profile": report.profile,
            "profile_source": report.profile_source,
            "checks": report.checks,
        }
        if report.fixes_applied:
            result["fixes_applied"] = report.fixes_applied
        json_output_response(result)
        # A lingering "fail" (after any fixes) means the install is broken, so
        # exit non-zero — consistent with ``auth check`` and the CLI exit-code
        # convention — instead of reading as green in CI / ``set -e`` scripts.
        if report.has_failures:
            exit_with_code(1)
        return

    _display_results(report)
    if report.has_failures:
        exit_with_code(1)


def _display_results(report: DoctorReport):
    """Display doctor results using Rich."""
    checks = report.checks
    fixes_applied = report.fixes_applied
    table = Table(title="NotebookLM Doctor")
    table.add_column("Check", style="dim")
    table.add_column("Status")
    table.add_column("Details", style="cyan")

    def status_icon(status: str) -> str:
        if status == "pass":
            return "[green]\u2713 pass[/green]"
        elif status == "warn":
            return "[yellow]! warn[/yellow]"
        return "[red]\u2717 fail[/red]"

    table.add_row("Profile", f"[bold]{report.profile}[/bold]", f"source: {report.profile_source}")

    for name, check in checks.items():
        label = name.replace("_", " ").title()
        table.add_row(label, status_icon(check["status"]), check["detail"])

    console.print(table)

    if fixes_applied:
        console.print()
        for fix in fixes_applied:
            console.print(f"  [green]\u2713[/green] {fix}")

    has_failures = report.has_failures
    if has_failures and not fixes_applied:
        console.print()
        if checks.get("migration", {}).get("status") == "fail":
            console.print(
                "[yellow]Run 'notebooklm doctor --fix' to migrate and set up profiles.[/yellow]"
            )
        if checks.get("auth", {}).get("status") == "fail":
            console.print("[yellow]Run 'notebooklm login' to authenticate.[/yellow]")
        if checks.get("profile_dir", {}).get("status") == "fail":
            console.print(
                "[yellow]Run 'notebooklm doctor --fix' to create the profile directory.[/yellow]"
            )
    elif not has_failures:
        console.print("\n[green]All checks passed.[/green]")
