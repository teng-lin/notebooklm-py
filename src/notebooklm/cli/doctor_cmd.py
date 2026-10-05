"""Diagnostic and migration CLI command.

Commands:
    doctor   Check profile setup, auth, and migration status

The doctor checks + automatic fixes + health aggregation live in the
transport-neutral :mod:`notebooklm._app.doctor`. This module owns the Rich
rendering, the ``--json`` envelope, and the exit codes, and forwards the path
helpers (read off this module at call time so the
``patch("...doctor_cmd.get_storage_path")`` seam keeps landing) into the
neutral ``run_checks``.
"""

import re
import shlex
import sys
from typing import Any

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .._app.doctor import DoctorPaths, DoctorReport, read_doctor_auth_state, run_checks
from ..auth import check_headless_reauth_readiness
from ..paths import (
    get_browser_profile_dir,
    get_config_path,
    get_home_dir,
    get_path_info,
    get_profile_dir,
    get_storage_path,
)
from .error_handler import exit_with_code, handle_errors
from .rendering import console, json_output_response
from .services.auth_source import AUTH_JSON_ENV_NAME, AuthSource, read_env_auth_json


def _doctor_paths(auth: AuthSource | None = None) -> DoctorPaths:
    """Bundle this module's path helpers for the neutral ``run_checks``.

    Each callable is resolved off the module global at call time so a
    ``patch("notebooklm.cli.doctor_cmd.<helper>", ...)`` test seam lands.
    """
    if auth is None:
        auth = AuthSource.from_click_context(click.get_current_context(silent=True))

    # Capture inline auth through the consolidated accessor. Explicit storage
    # suppresses it, just as it does for runtime and the passive auth check.
    inline_json: str | None = None
    if auth.has_env_auth:
        inline_json = read_env_auth_json()
        storage_path = auth.storage_path_for_diagnostics()
        auth_source = AUTH_JSON_ENV_NAME
    else:
        storage_path = (
            auth.storage_override
            if auth.storage_override is not None
            else get_storage_path(profile=auth.profile)
        )
        auth_source = f"file ({storage_path})"

    def read_auth_state() -> dict[str, Any]:
        return read_doctor_auth_state(storage_path, inline_auth_json=inline_json)

    resolved_auth = auth
    return DoctorPaths(
        get_path_info=lambda: get_path_info(
            profile=resolved_auth.profile,
            storage_path=resolved_auth.storage_override,
        ),
        get_home_dir=get_home_dir,
        get_profile_dir=get_profile_dir,
        get_storage_path=lambda: (
            resolved_auth.storage_override
            if resolved_auth.storage_override is not None
            else get_storage_path(profile=resolved_auth.profile)
        ),
        get_config_path=get_config_path,
        headless_reauth_check=_headless_reauth_check,
        read_auth_state=read_auth_state,
        auth_source=auth_source,
        has_inline_auth=auth.has_env_auth,
    )


def _headless_reauth_check() -> dict[str, str]:
    """Map the L3 readiness probe to the standard ``{status, detail}`` check shape.

    The transport-neutral ``_app.doctor`` core receives this credential-free,
    browser-free probe as a ready-made check row from the CLI adapter.

    ``warn`` (never ``fail``) when L3 is unavailable: it is an optional, opt-in
    fallback, so a missing persistent profile or an absent ``browser`` extra is
    not a broken install — only an unavailable enhancement. The coarse auth
    facade imports the browser implementation only when this
    check runs and never imports Playwright merely to resolve the function.

    ``doctor`` is a read-only diagnostic, so resolving the browser-profile dir
    is wrapped: path resolution can raise ``ValueError`` / ``OSError``, and the
    readiness probe can also raise ``RuntimeError`` while checking browser
    availability. Each is degraded to a ``warn`` row rather than crashing the
    whole command — consistent with the other doctor checks, which all map
    malformed inputs to a status instead of raising.

    This L3 row reads the live :class:`AuthSource` independently of the other
    doctor paths so root ``--storage`` and ``--profile`` select the same browser
    directory as runtime re-auth.
    """
    try:
        auth = AuthSource.from_click_context(click.get_current_context(silent=True))
        if auth.has_env_auth:
            return {
                "status": "pass",
                "detail": "not applicable to inline authentication (no writable auth profile)",
            }
        browser_profile = get_browser_profile_dir(
            profile=auth.profile,
            storage_path=auth.storage_override,
        )
        available, detail = check_headless_reauth_readiness(browser_profile=browser_profile)
    except (ValueError, OSError, RuntimeError) as exc:
        return {
            "status": "warn",
            "detail": f"unavailable: could not resolve the browser profile ({type(exc).__name__})",
        }
    return {
        "status": "pass" if available else "warn",
        "detail": detail,
    }


def register_doctor_command(cli):
    """Register the doctor command on the main CLI group."""

    @cli.command("doctor")
    @click.option("--fix", "fix_issues", is_flag=True, help="Attempt to fix detected issues")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def doctor(fix_issues, json_output):
        """Check profile setup, local auth material, and migration.

        Diagnoses common issues with profiles, authentication, and directory
        structure without testing online authentication. Use --fix to
        automatically repair detected filesystem problems.

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
    # The doctor checks + automatic fixes + health aggregation are
    # transport-neutral and live in ``_app.doctor``. Path helpers are forwarded
    # via ``_doctor_paths`` (read off this module at call time so the
    # ``patch("...doctor_cmd.get_storage_path")`` seam lands); an unexpected
    # ``OSError`` from one of them propagates here for ``handle_errors`` to wrap.
    auth = AuthSource.from_click_context(click.get_current_context(silent=True))
    report = run_checks(fix=fix_issues, paths=_doctor_paths(auth))

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

    _display_results(report, auth=auth)
    if report.has_failures:
        exit_with_code(1)


def _source_command(
    report: DoctorReport, auth: AuthSource, *args: str, platform: str | None = None
) -> str:
    """Preserve the auth selector in copyable diagnostic/remediation commands."""
    command = ["notebooklm"]
    if auth.storage_override is not None:
        command.extend(("--storage", str(auth.storage_override)))
    else:
        command.extend(("--profile", auth.profile or report.profile))
    command.extend(args)
    if (sys.platform if platform is None else platform) == "win32":
        return " ".join(_powershell_command_arg(arg) for arg in command)
    return shlex.join(command)


def _powershell_command_arg(arg: str) -> str:
    """Use literal PowerShell arguments so selectors cannot expand variables."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+", arg):
        return arg
    # PowerShell also treats these typographic single quotes as delimiters.
    escaped = "".join(char * 2 if char in "'\u2018\u2019\u201a\u201b" else char for char in arg)
    return "'" + escaped + "'"


def _display_results(report: DoctorReport, *, auth: AuthSource, platform: str | None = None):
    """Display doctor results using Rich."""
    checks = report.checks
    fixes_applied = report.fixes_applied
    shell_label = (
        " in PowerShell" if (sys.platform if platform is None else platform) == "win32" else ""
    )

    def source_command(*args: str) -> str:
        return _source_command(report, auth, *args, platform=platform)

    def command_hint(prose: str, command: str, *, style: str | None = None) -> None:
        console.print(prose, markup=False, style=style)
        console.print(command, markup=False, soft_wrap=True, highlight=False, emoji=False)

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

    table.add_row(
        "Profile",
        Text(report.profile, style="bold"),
        Text(f"source: {report.profile_source}"),
    )

    labels = {name: name.replace("_", " ").title() for name in checks}
    for name, check in checks.items():
        table.add_row(labels[name], status_icon(check["status"]), Text(check["detail"]))

    console.print(table)
    auth_source = checks.get("auth", {}).get("source")
    if auth_source is not None:
        console.print(
            f"Authentication source: {auth_source} (local checks only)", markup=False, emoji=False
        )

    guidance = checks.get("auth", {}).get("guidance")
    online_command = source_command("auth", "check", "--test", "--passive")
    if guidance == "test_authentication":
        command_hint(
            f"To test this auth source online without refreshing it, run{shell_label}:",
            online_command,
        )
        if auth.has_env_auth:
            console.print(
                f"[yellow]If the passive check fails, replace {AUTH_JSON_ENV_NAME} with "
                "valid exported authentication, or unset it to use stored profile "
                "authentication.[/yellow]"
            )
        else:
            login_command = source_command("login")
            command_hint(
                f"If the passive check fails, re-run{shell_label}:",
                login_command,
                style="yellow",
            )
    elif guidance == "configure_notebooklm_url":
        console.print(
            "[yellow]Fix or unset NOTEBOOKLM_BASE_URL to use a supported NotebookLM URL.[/yellow]"
        )
    elif guidance == "recover_file_authentication":
        recovery_command = source_command("auth", "check", "--test")
        command_hint(
            f"To attempt best-effort recovery, run{shell_label}:",
            recovery_command,
        )
        console.print("This check may refresh, rotate, or update stored cookies.", markup=False)
        login_command = source_command("login")
        command_hint(f"If recovery fails, re-run{shell_label}:", login_command, style="yellow")
    elif guidance == "replace_incomplete_inline_auth":
        console.print(
            f"[yellow]{AUTH_JSON_ENV_NAME} is an incomplete export: __Secure-1PSIDTS is "
            "missing or malformed. The passive check stops locally, and inline auth "
            "has no writable backing file for recovery. Replace it with a complete "
            "export, or unset it to use stored profile authentication and recovery.[/yellow]"
        )
    elif guidance == "replace_inline_auth":
        console.print(
            f"[yellow]Replace {AUTH_JSON_ENV_NAME} with valid exported authentication, "
            "or unset it to use stored profile authentication.[/yellow]"
        )
    elif guidance == "refresh_authentication":
        login_command = source_command("login")
        command_hint(f"Re-run{shell_label}:", login_command, style="yellow")
        console.print(
            "[yellow]On Windows (Chrome 127+ App-Bound "
            "Encryption) add '--browser-cookies firefox' or '--master-token' "
            "to that login command.[/yellow]"
        )

    if fixes_applied:
        console.print()
        for fix in fixes_applied:
            console.print(f"  [green]\u2713[/green] {escape(fix)}", emoji=False)

    has_failures = report.has_failures
    if has_failures and not fixes_applied:
        console.print()
        fix_command = source_command("doctor", "--fix")
        if checks.get("migration", {}).get("status") == "fail":
            command_hint(
                f"Run{shell_label} to migrate and set up profiles:",
                fix_command,
                style="yellow",
            )
        if checks.get("profile_dir", {}).get("status") == "fail":
            command_hint(
                f"Run{shell_label} to create the profile directory:",
                fix_command,
                style="yellow",
            )
    elif not has_failures:
        warned_labels = [
            labels[name] for name, check in checks.items() if check["status"] == "warn"
        ]
        if warned_labels:
            console.print(
                f"\n[yellow]Local checks raised a warning ({', '.join(warned_labels)}). "
                "Online authentication was not tested.[/yellow]"
            )
        else:
            console.print("\nNo local failures detected. Online authentication was not tested.")

    if checks.get("auth", {}).get("status") != "fail" and guidance not in (
        "test_authentication",
        "recover_file_authentication",
        "replace_incomplete_inline_auth",
    ):
        command_hint(
            f"To test this auth source online without refreshing it, run{shell_label}:",
            online_command,
        )
