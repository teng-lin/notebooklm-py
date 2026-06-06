"""Render helpers for the session CLI commands.

These presentation helpers live here rather than in the service layer so
``cli/services/`` stays free of ``..rendering`` / exit imports (ADR-0008).
``register_session_commands`` in :mod:`notebooklm.cli.session_cmd` imports
them back so the command bodies keep calling them through their own module
namespace.
"""

from __future__ import annotations

from typing import Any, NoReturn

from rich.markup import render as render_markup
from rich.table import Table

from .error_handler import _output_error, exit_with_code
from .rendering import console, json_output_response
from .services.auth_diagnostics import AuthCheckResult
from .services.auth_source import AUTH_JSON_ENV_NAME
from .services.login.outcomes import BrowserCookieOutcome
from .services.session_context import LogoutOutcome, StatusReport


def _use_notebook_table() -> Table:
    t = Table()
    t.add_column("ID", style="cyan")
    t.add_column("Title", style="green")
    t.add_column("Owner")
    t.add_column("Created", style="dim")
    return t


def _render_status(report: StatusReport, *, json_output: bool) -> None:
    """Render a :class:`StatusReport` to the configured console.

    Lives here rather than in
    :mod:`notebooklm.cli.services.session_context` so the service layer
    does not reach into ``..rendering`` (ADR-0008). Supports ``--paths``
    (resolved configuration paths) and ``--json`` (machine-readable
    envelope).
    """
    if report.paths is not None:
        # --paths flag was set; render the paths view and stop.
        if json_output:
            json_output_response({"paths": report.paths})
            return

        table = Table(title="Configuration Paths")
        table.add_column("File", style="dim")
        table.add_column("Path", style="cyan")
        table.add_column("Source", style="green")

        path_info = report.paths
        table.add_row(
            "Profile",
            path_info.get("profile", "default"),
            path_info.get("profile_source", ""),
        )
        table.add_row("Home Directory", path_info["home_dir"], path_info["home_source"])
        table.add_row("Profile Directory", path_info.get("profile_dir", ""), "")
        table.add_row("Storage State", path_info["storage_path"], "")
        table.add_row("Context", path_info["context_path"], "")
        table.add_row("Browser Profile", path_info["browser_profile_dir"], "")

        if report.has_env_auth:
            console.print(
                f"[yellow]Note: {AUTH_JSON_ENV_NAME} is set (inline auth active)[/yellow]\n"
            )

        console.print(table)
        return

    ctx_view = report.context

    if not ctx_view.has_context:
        if json_output:
            json_output_response({"has_context": False, "notebook": None, "conversation_id": None})
            return
        console.print(
            "[yellow]No notebook selected. Use 'notebooklm use <id>' to set one.[/yellow]"
        )
        return

    if not ctx_view.payload_readable:
        # Context file existed but couldn't be parsed; surface minimal info.
        if json_output:
            json_output_response(
                {
                    "has_context": True,
                    "notebook": {
                        "id": ctx_view.notebook_id,
                        "title": None,
                        "is_owner": None,
                    },
                    "conversation_id": None,
                }
            )
            return

        table = Table(title="Current Context")
        table.add_column("Property", style="dim")
        table.add_column("Value", style="cyan")
        table.add_row("Notebook ID", ctx_view.notebook_id or "")
        table.add_row("Title", "-")
        table.add_row("Ownership", "-")
        table.add_row("Created", "-")
        table.add_row("Conversation", "[dim]None[/dim]")
        console.print(table)
        return

    if json_output:
        json_output_response(
            {
                "has_context": True,
                "notebook": {
                    "id": ctx_view.notebook_id,
                    "title": ctx_view.title if ctx_view.title and ctx_view.title != "-" else None,
                    "is_owner": ctx_view.is_owner if ctx_view.is_owner is not None else True,
                },
                "conversation_id": ctx_view.conversation_id,
            }
        )
        return

    table = Table(title="Current Context")
    table.add_column("Property", style="dim")
    table.add_column("Value", style="cyan")

    table.add_row("Notebook ID", ctx_view.notebook_id or "")
    table.add_row("Title", str(ctx_view.title or "-"))
    is_owner = ctx_view.is_owner if ctx_view.is_owner is not None else True
    owner_status = "Owner" if is_owner else "Shared"
    table.add_row("Ownership", owner_status)
    table.add_row("Created", ctx_view.created_at or "-")
    if ctx_view.conversation_id:
        table.add_row("Conversation", ctx_view.conversation_id)
    else:
        table.add_row("Conversation", "[dim]None (will auto-select on next ask)[/dim]")
    console.print(table)


def _render_logout_outcome(outcome: LogoutOutcome) -> None:
    """Render a :class:`LogoutOutcome` and apply its exit policy.

    Owns the presentation + exit policy for the ``run_logout`` flow,
    keeping the service function Click-free. On per-step
    :class:`OSError` failures, prints the diagnostic and then exits 1; on
    success prints either the green "Logged out." line or the yellow
    "No active session found." no-op line and returns normally.
    """
    if outcome.env_auth_remains:
        console.print(
            f"[yellow]Note: {AUTH_JSON_ENV_NAME} is set — env-based auth will "
            "remain active after logout. Unset it to fully log out.[/yellow]"
        )

    failure = outcome.failure
    if failure is not None:
        if failure.kind == "storage":
            console.print(
                f"[red]Cannot remove auth file: {failure.error_message}[/red]\n"
                "Close any running notebooklm commands and try again.\n"
                f"If the problem persists, manually delete: {failure.path}"
            )
        elif failure.kind == "browser_profile":
            partial = (
                "[yellow]Note: Auth file was removed, but browser profile "
                "could not be deleted.[/yellow]\n"
                if failure.partial_storage_removed
                else ""
            )
            console.print(
                f"{partial}"
                f"[red]Cannot remove browser profile: {failure.error_message}[/red]\n"
                "Close any open browser windows and try again.\n"
                f"If the problem persists, manually delete: {failure.path}"
            )
        else:  # failure.kind == "context"
            console.print(
                f"[red]Cannot remove context file: {failure.error_message}[/red]\n"
                "Close any running notebooklm commands and try again.\n"
                f"If the problem persists, manually delete: {failure.path}"
            )
        exit_with_code(1)

    if outcome.removed_any:
        console.print("[green]Logged out.[/green] Run 'notebooklm login' to sign in again.")
    else:
        console.print("[yellow]No active session found.[/yellow] Already logged out.")


def _render_auth_check_result(result: AuthCheckResult) -> None:
    """Render an :class:`AuthCheckResult` (table or JSON) and exit on failure.

    The presentation + exit-code policy lives here in the command layer
    so ``services/auth_diagnostics.py`` can stay free of rendering and
    exit imports (ADR-0008 boundary).
    """
    plan = result.plan
    all_passed = result.all_passed
    checks = result.checks
    details = result.details

    if plan.json_output:
        json_output_response(
            {
                "status": "ok" if all_passed else "error",
                "checks": checks,
                "details": details,
            }
        )
        if not all_passed:
            exit_with_code(1)
        return

    # Rich-table render.
    table = Table(title="Authentication Check")
    table.add_column("Check", style="dim")
    table.add_column("Status")
    table.add_column("Details", style="cyan")

    def status_icon(val: bool | None) -> str:
        if val is None:
            return "[dim]⊘ skipped[/dim]"
        return "[green]✓ pass[/green]" if val else "[red]✗ fail[/red]"

    table.add_row(
        "Storage exists",
        status_icon(checks["storage_exists"]),
        details["auth_source"],
    )
    table.add_row("JSON valid", status_icon(checks["json_valid"]), "")
    table.add_row(
        "Cookies present",
        status_icon(checks["cookies_present"]),
        f"{len(details.get('cookies_found', []))} cookies" if checks["cookies_present"] else "",
    )
    table.add_row(
        "SID cookie",
        status_icon(checks["sid_cookie"]),
        ", ".join(details.get("cookie_domains", [])[:3]) or "",
    )
    table.add_row(
        "Token fetch",
        status_icon(checks["token_fetch"]),
        "use --test to check" if checks["token_fetch"] is None else "",
    )

    console.print(table)

    cookies_by_domain = details.get("cookies_by_domain", {})
    if cookies_by_domain:
        console.print()
        cookie_table = Table(title="Cookies by Domain")
        cookie_table.add_column("Domain", style="cyan")
        cookie_table.add_column("Cookies")

        key_cookies = {"SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC"}

        def format_cookie_name(name: str) -> str:
            if name in key_cookies:
                return f"[green]{name}[/green]"
            if name.startswith("__Secure-"):
                return f"[blue]{name}[/blue]"
            return f"[dim]{name}[/dim]"

        for domain in sorted(cookies_by_domain.keys()):
            cookie_names = cookies_by_domain[domain]
            formatted = [format_cookie_name(name) for name in sorted(cookie_names)]
            cookie_table.add_row(domain, ", ".join(formatted))

        console.print(cookie_table)

    if details.get("error"):
        console.print(f"\n[red]Error:[/red] {details['error']}")

    if all_passed:
        console.print("\n[green]Authentication is valid.[/green]")
    elif not checks["storage_exists"]:
        console.print("\n[yellow]Run 'notebooklm login' to authenticate.[/yellow]")
    elif checks["token_fetch"] is False:
        console.print(
            "\n[yellow]Cookies may be expired. Run 'notebooklm login' to refresh.[/yellow]"
        )


def _render_auth_inspect(
    browser_name: str,
    accounts: list[Any],
    *,
    json_output: bool,
    verbose: bool,
) -> None:
    """Render ``auth inspect`` results (text table or JSON envelope).

    Moved here from ``services/auth_diagnostics.py`` so the service module
    stays free of rendering imports (ADR-0008 boundary).
    """
    if json_output:
        json_output_response(
            {
                "browser": browser_name,
                "accounts": [
                    {
                        "email": a.email,
                        "is_default": a.is_default,
                        "browser_profile": a.browser_profile,
                    }
                    for a in accounts
                ],
            }
        )
        return

    console.print(f"\n[bold]Browser:[/bold] {browser_name}")
    console.print(f"[bold]Found {len(accounts)} signed-in Google account(s):[/bold]\n")
    show_browser_profile = verbose and any(a.browser_profile for a in accounts)
    table = Table(show_header=True, header_style="bold")
    table.add_column("email")
    if show_browser_profile:
        table.add_column(f"{browser_name} user")
    table.add_column("default", justify="center")
    for a in accounts:
        row = [a.email]
        if show_browser_profile:
            row.append(a.browser_profile or "")
        row.append("[green]✓[/green]" if a.is_default else "")
        table.add_row(*row)
    console.print(table)
    hint = (
        f"Pick one with: [cyan]notebooklm login --browser-cookies "
        f"{browser_name} --account EMAIL[/cyan]\n"
        f"Or extract them all: [cyan]notebooklm login --browser-cookies "
        f"{browser_name} --all-accounts[/cyan]"
    )
    if not verbose and any(a.browser_profile for a in accounts):
        hint = (
            "[dim]Pass -v to see which browser user-profile each account came from.[/dim]\n" + hint
        )
    console.print("\n" + hint)


def _render_auth_inspect_error(outcome: BrowserCookieOutcome, *, json_output: bool) -> NoReturn:
    """Render a browser-cookie discovery failure for ``auth inspect``."""
    if json_output:
        extra: dict[str, Any] = {}
        name = getattr(outcome, "name", None)
        if isinstance(name, str):
            extra["browser"] = name
        supported = getattr(outcome, "supported", None)
        if supported is not None:
            extra["supported"] = list(supported)
        _output_error(render_markup(outcome.message).plain, outcome.code, True, 1, extra=extra)

    console.print(outcome.message)
    exit_with_code(1)
