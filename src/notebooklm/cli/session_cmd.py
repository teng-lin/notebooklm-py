"""Session and context management CLI commands.

Commands:
    login   Log in to NotebookLM via browser
    use     Set the current notebook context
    status  Show current context
    clear   Clear current notebook context
    auth    Authentication management (logout / inspect / check / refresh)

This module is split into thin Click handlers over four service
modules:

* :mod:`notebooklm.cli.services.playwright_login` — Playwright login flow
* :mod:`notebooklm.cli.services.session_context` — ``use`` / ``status``
* :mod:`notebooklm.cli.services.auth_diagnostics` — ``auth check``
* :mod:`notebooklm.cli.services.auth_source` — auth-source precedence

Body-used names that *moved* into those services are re-imported here as
the command layer's own bindings. A handful are also bound on the
``notebooklm.cli.session_cmd`` namespace by tests that pre-date ADR-0008's
services-side patching convention (e.g. ``_sync_server_language_to_config``,
``_login_browser_cookies_single``); those names stay because they are
referenced from this module's body.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any, NoReturn

import click
import httpx
from rich.markup import render as render_markup
from rich.table import Table

from ..client import NotebookLMClient
from ..exceptions import AuthError, NotebookNotFoundError
from ..paths import get_storage_path
from .auth_runtime import handle_auth_error, run_client_workflow
from .context import clear_context, set_current_notebook
from .error_handler import _output_error, exit_with_code, handle_errors
from .playwright_login_io import (
    prepare_paths_or_exit,
    repair_after_refresh,
    run_login,
    validate_flags_or_exit,
)
from .rendering import console, json_output_response
from .resolve import resolve_notebook_id
from .runtime import run_async
from .services.auth_diagnostics import (
    AuthCheckResult,
    plan_from_click_context,
    run_auth_check,
)
from .services.auth_source import AUTH_JSON_ENV_NAME, has_env_auth_json

# Direct imports replace the D1-PR-3-retired forwarding wrappers; see ADR-0008.
# These names are all called from this module's body. Several also serve as
# ``notebooklm.cli.session_cmd.*`` monkeypatch surfaces for tests that pre-date
# ADR-0008's services-side patching convention (e.g.
# ``_sync_server_language_to_config``, ``_login_browser_cookies_single``,
# ``_refresh_from_browser_cookies``, ``_enumerate_browser_accounts``); those
# patches keep working because the body-used name stays bound here.
from .services.login import (
    _enumerate_browser_accounts,
    _login_all_accounts_from_browser,
    _login_browser_cookies_single,
    _refresh_from_browser_cookies,
    _sync_server_language_to_config,
)
from .services.login import (
    cookie_domains as _cookie_domains,
)
from .services.login.exceptions import LoginConfigurationError
from .services.login.outcomes import BrowserCookieOutcome, NetworkFailure
from .services.playwright_login import (
    CHANNEL_BROWSERS as _CHANNEL_BROWSERS,
)
from .services.playwright_login import (
    PlaywrightLoginPlan,
)
from .services.session_context import (
    LogoutOutcome,
    StatusReport,
    UseNotebookResult,
    execute_logout,
    read_status,
    verify_and_set_notebook,
)

logger = logging.getLogger(__name__)


async def fetch_tokens_with_domains(*args: Any, **kwargs: Any) -> Any:
    """Patch-compatible forwarding wrapper for auth token refresh helpers."""
    from ..auth import fetch_tokens_with_domains as auth_fetch_tokens_with_domains

    return await auth_fetch_tokens_with_domains(*args, **kwargs)


def _click_exception_from(exc: LoginConfigurationError) -> click.ClickException:
    """Translate a login-service ``LoginConfigurationError`` into a Click error.

    The login services raise plain Python exceptions (ADR-0015 Pattern B
    decoupling) so the command layer owns the Click translation here.
    ``hint`` is appended to the user-facing message when present so the
    final ``Error: ...`` line carries the remediation advice.
    """
    if exc.hint:
        return click.ClickException(
            f"{exc.message} {exc.hint}"
        )  # cli-input-validation: login profile-name validation translation
    return click.ClickException(
        exc.message
    )  # cli-input-validation: login profile-name validation translation


def _is_valid_account_metadata(metadata: dict[str, Any]) -> bool:
    raw_authuser = metadata.get("authuser")
    if type(raw_authuser) is not int or raw_authuser < 0:
        return False
    raw_email = metadata.get("email")
    if raw_email is None:
        return True
    return isinstance(raw_email, str) and bool(raw_email.strip())


# Legacy thin alias kept for the small set of session-cmd-internal helpers
# below. The Playwright login flow now lives in
# :mod:`notebooklm.cli.services.playwright_login`; this thunk preserves the
# historical ``patch("notebooklm.cli.session_cmd._run_playwright_login")``
# surface used by the unit tests.
def _run_playwright_login(
    *,
    browser: str,
    browser_profile: Path,
    storage_path: Path,
    include_domains: set[str] | None = None,
) -> None:
    """Backward-compat wrapper around :func:`run_login`."""
    plan = PlaywrightLoginPlan(
        browser=browser,
        browser_profile=browser_profile,
        storage_path=storage_path,
        include_domains=include_domains,
    )
    run_login(plan)


def _parse_include_domains(values: tuple[str, ...]) -> set[str]:
    """Command-layer Click wrapper for the service ``--include-domains`` parser."""
    try:
        return _cookie_domains._parse_include_domains(values)
    except _cookie_domains.IncludeDomainsParseError as exc:
        raise click.BadParameter(  # cli-input-validation: --include-domains value parse failure
            str(exc)
        ) from None


def _warn_missing_optional_domains(include_domains: set[str]) -> None:
    """Render the cookie-domain migration warning from the command layer."""
    _cookie_domains._warn_missing_optional_domains(include_domains, warn=console.print)


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


def register_session_commands(cli):
    """Register session commands on the main CLI group."""

    @cli.command("login")
    @click.option(
        "--storage",
        type=click.Path(),
        default=None,
        help="Where to save storage_state.json (default: profile-specific location)",
    )
    @click.option(
        "--browser",
        type=click.Choice(["chromium", *_CHANNEL_BROWSERS], case_sensitive=False),
        default="chromium",
        help=(
            "Browser to use for login (default: chromium). "
            "Use 'chrome' for system Google Chrome (workaround when bundled "
            "Chromium crashes, e.g. macOS 15+), 'msedge' for Microsoft Edge."
        ),
    )
    @click.option(
        "--browser-cookies",
        "browser_cookies",
        default=None,
        is_flag=False,
        flag_value="auto",
        help=(
            "Read cookies from an installed browser instead of launching Playwright. "
            "Optionally specify browser: chrome, firefox, brave, edge, safari, arc, ... "
            "For Chromium-family profiles, target one with 'chrome::<profile>' "
            "(e.g. 'chrome::Profile 1' or 'brave::Work'). "
            "For Firefox Multi-Account Containers, target a specific container with "
            "'firefox::<container-name>' (or 'firefox::none' for the default). "
            "Requires: pip install 'notebooklm-py[cookies]'"
        ),
    )
    @click.option(
        "--account",
        "account_email",
        default=None,
        help=(
            "Pick a signed-in Google account by email when several are present "
            "in the browser. Only valid with --browser-cookies."
        ),
    )
    @click.option(
        "--all-accounts",
        "all_accounts",
        is_flag=True,
        default=False,
        help=(
            "Extract every Google account signed in to the browser into its own "
            "profile (auto-named from each account's email). Only valid with "
            "--browser-cookies."
        ),
    )
    @click.option(
        "--update",
        "update",
        is_flag=True,
        default=False,
        help=(
            "With --all-accounts: when an account's natural profile name "
            "(e.g. 'alice' for alice@gmail.com) already exists but has no "
            "account metadata, update that profile in place instead of "
            "creating a suffixed 'alice-2'. Profiles that already bind a "
            "different email are still given a suffix to avoid clobbering. "
            "Only valid with --all-accounts."
        ),
    )
    @click.option(
        "--profile-name",
        "profile_name",
        default=None,
        help=(
            "Write a targeted --account browser-cookie login to this named profile "
            "instead of the active profile. Only valid with --browser-cookies."
        ),
    )
    @click.option(
        "--fresh",
        is_flag=True,
        default=False,
        help="Start with a clean browser session (deletes cached browser profile). Use to switch Google accounts.",
    )
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Opt in to extracting sibling-product cookies (default: required "
            "Google auth/Drive cookies only). Pass labels comma-separated or "
            "repeat the flag: --include-domains=youtube,docs OR "
            "--include-domains=youtube --include-domains=docs. Supported "
            "labels: youtube, docs, myaccount, mail, all."
        ),
    )
    @click.pass_context
    def login(
        ctx,
        storage,
        browser,
        browser_cookies,
        account_email,
        all_accounts,
        update,
        profile_name,
        fresh,
        include_domains_raw,
    ):
        """Log in to NotebookLM via browser.

        Opens a browser window for Google login. Authentication is saved
        automatically once login is detected (no terminal interaction needed).

        Use --browser chrome if the bundled Chromium crashes (e.g. macOS 15+).
        Use --browser msedge if your organization requires Microsoft Edge for SSO.

        Note: Cannot be used when the env-var auth fast path is active
        (use file-based auth or unset the env var first).
        """
        # Wrap entire body in handle_errors so unexpected failures (e.g.
        # Playwright internal crashes) emit a friendly 'Unexpected error:
        # <msg>' line + exit 2 instead of a Python traceback. Existing
        # ``exit_with_code(N)`` calls inside the body propagate unchanged.
        with handle_errors():
            if has_env_auth_json():
                console.print(
                    f"[red]Error: Cannot run 'login' when {AUTH_JSON_ENV_NAME} is set.[/red]\n"
                    f"The {AUTH_JSON_ENV_NAME} environment variable provides inline authentication,\n"
                    "which conflicts with browser-based login that saves to a file.\n\n"
                    "Either:\n"
                    f"  1. Unset {AUTH_JSON_ENV_NAME} and run 'login' again\n"
                    f"  2. Continue using {AUTH_JSON_ENV_NAME} for authentication"
                )
                exit_with_code(1)

            validate_flags_or_exit(
                browser_cookies=browser_cookies,
                account_email=account_email,
                all_accounts=all_accounts,
                update=update,
                profile_name=profile_name,
                storage=storage,
            )

            include_domains = _parse_include_domains(include_domains_raw)

            # rookiepy fast-path: skip Playwright entirely
            if browser_cookies is not None:
                if fresh:
                    console.print(
                        "[yellow]Warning: --fresh has no effect with --browser-cookies "
                        "(no browser profile is used).[/yellow]"
                    )
                _warn_missing_optional_domains(include_domains)
                if all_accounts:
                    _login_all_accounts_from_browser(
                        browser_cookies,
                        update=update,
                        include_domains=include_domains,
                    )
                    return
                active_profile = ctx.obj.get("profile") if ctx.obj else None
                # Inject ``click.confirm`` as the overwrite confirmer so the
                # login service stays Click-free (ADR-0015 Pattern B). The
                # service defaults ``confirm=None`` to "auto-accept" for
                # non-interactive callers; production CLI runs always inject
                # an actual prompt here.
                confirm_overwrite = functools.partial(click.confirm, default=False)
                try:
                    _login_browser_cookies_single(
                        browser_cookies,
                        storage=storage,
                        account_email=account_email,
                        profile_name=profile_name,
                        active_profile=active_profile,
                        include_domains=include_domains,
                        confirm=confirm_overwrite,
                    )
                except LoginConfigurationError as exc:
                    raise _click_exception_from(exc) from None
                return

            profile = ctx.obj.get("profile") if ctx.obj else None
            storage_path, browser_profile = prepare_paths_or_exit(profile, storage, fresh)
            _run_playwright_login(
                browser=browser,
                browser_profile=browser_profile,
                storage_path=storage_path,
                include_domains=include_domains,
            )
            console.print(f"\n[green]Authentication saved to:[/green] {storage_path}")

            # Sync server language setting to local config so generate commands
            # respect the user's global language preference (fixes #121).
            _sync_server_language_to_config(storage_path=storage_path, profile=profile)

    @cli.command("use")
    @click.argument("notebook_id")
    @click.option(
        "--force",
        is_flag=True,
        default=False,
        help=(
            "Skip the existence check and persist the notebook ID even if "
            "verification fails. Use for offline work or debugging."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def use_notebook(ctx, notebook_id, force, json_output):
        """Set the current notebook context.

        Once set, all commands will use this notebook by default.
        You can still override by passing --notebook explicitly.

        Supports partial IDs - 'notebooklm use abc' matches 'abc123...'

        By default, the notebook must exist on the server; a typo or
        unreachable backend results in a non-zero exit and the saved
        context is left untouched. Pass --force to bypass verification.

        \b
        Example:
          notebooklm use nb123
          notebooklm ask "what is this about?"   # Uses nb123
          notebooklm generate video "a fun explainer"  # Uses nb123
        """
        if force:
            # --force path: persist immediately without any RPC verification.
            set_current_notebook(notebook_id)
            if json_output:
                json_output_response(
                    {
                        "active_notebook_id": notebook_id,
                        "success": True,
                        "verified": False,
                    }
                )
                return
            table = _use_notebook_table()
            table.add_row(notebook_id, "(not verified — --force)", "-", "-")
            console.print(table)
            return

        async def _get(client: NotebookLMClient) -> UseNotebookResult:
            # Pass the locally-bound ``resolve_notebook_id`` so legacy tests
            # patching ``notebooklm.cli.session_cmd.resolve_notebook_id`` still
            # intercept the call. The service module would otherwise import
            # the symbol from ``cli.resolve`` directly and bypass the patch.
            return await verify_and_set_notebook(
                client,
                notebook_id,
                json_output=json_output,
                resolver=resolve_notebook_id,
            )

        def _handle_use_verification_error(exc: Exception):
            if isinstance(exc, click.ClickException):
                raise exc
            if isinstance(exc, NotebookNotFoundError):
                _output_error(
                    f"Error: Notebook {notebook_id!r} not found. "
                    "Run 'notebooklm list' to see available notebooks, "
                    "or pass --force to bypass verification.",
                    "NOT_FOUND",
                    json_output,
                    1,
                )
                raise AssertionError("unreachable")
            if isinstance(exc, AuthError):
                handle_auth_error(json_output)
                raise AssertionError("unreachable")
            _output_error(
                f"Error: Could not verify notebook {notebook_id!r}: {exc}. "
                "Pass --force to persist without verification.",
                "VERIFICATION_FAILED",
                json_output,
                1,
            )
            raise AssertionError("unreachable")

        result = run_client_workflow(
            ctx,
            command_name="session_use",
            json_output=json_output,
            body=_get,
            client_factory=NotebookLMClient,
            body_error_handler=_handle_use_verification_error,
        )

        nb = result.notebook
        resolved_id = result.resolved_id
        created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
        set_current_notebook(resolved_id, nb.title, nb.is_owner, created_str)

        if json_output:
            json_output_response(
                {
                    "active_notebook_id": resolved_id,
                    "success": True,
                    "verified": True,
                    "notebook": {
                        "id": resolved_id,
                        "title": nb.title,
                        "is_owner": nb.is_owner,
                        "created_at": nb.created_at.isoformat() if nb.created_at else None,
                    },
                }
            )
            return

        table = _use_notebook_table()
        created = created_str or "-"
        owner_status = "Owner" if nb.is_owner else "Shared"
        table.add_row(nb.id, nb.title, owner_status, created)
        console.print(table)

    @cli.command("status")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option("--paths", "show_paths", is_flag=True, help="Show resolved file paths")
    @click.pass_context
    def status(ctx, json_output, show_paths):
        """Show current context (active notebook and conversation).

        Use --paths to see where configuration files are located
        (useful for debugging NOTEBOOKLM_HOME).
        """
        report = read_status(ctx, show_paths=show_paths)
        _render_status(report, json_output=json_output)

    @cli.command("clear")
    def clear_cmd():
        """Clear current notebook context."""
        clear_context()
        console.print("[green]Context cleared[/green]")

    @cli.group("auth")
    def auth_group():
        """Authentication management commands."""
        pass

    @auth_group.command("logout")
    @click.pass_context
    def auth_logout(ctx):
        """Log out by clearing saved authentication.

        Removes both the saved cookie file (storage_state.json) and the
        cached browser profile. After logout, run 'notebooklm login' to
        authenticate with a different Google account.

        \b
        Examples:
          notebooklm auth logout                       # Clear auth for active profile
          notebooklm -p work auth logout               # Clear auth for 'work' profile
          notebooklm --storage A.json auth logout      # Clear the override auth file
        """
        outcome = execute_logout(ctx)
        _render_logout_outcome(outcome)

    @auth_group.command("inspect")
    @click.option(
        "--browser",
        "browser_name",
        default="auto",
        help=(
            "Browser to read cookies from (chrome, firefox, brave, edge, "
            "safari, arc, ...). 'auto' picks the first one rookiepy can read. "
            "Use 'chrome::<profile>' for one Chromium profile or "
            "'firefox::<container>' for one Firefox container. "
            "Requires: pip install 'notebooklm-py[cookies]'"
        ),
    )
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Opt in to enumerating accounts via sibling-product cookies. "
            "Same syntax as 'notebooklm login --include-domains'. By "
            "default this command only consults required Google auth "
            "cookies, which is sufficient for account discovery on every "
            "tested path."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option(
        "-v",
        "--verbose",
        "verbose",
        is_flag=True,
        default=False,
        help=(
            "Also show which browser user-profile each account's cookies came "
            "from. Useful for Chromium-family browsers with multiple "
            "user-profiles."
        ),
    )
    def auth_inspect(browser_name, include_domains_raw, json_output, verbose):
        """List Google accounts visible to a browser's cookie store.

        Read-only — never writes to disk. Use this before
        ``notebooklm login --browser-cookies <browser> --account <email>`` to
        see which account emails are available.

        For Chromium-family browsers (chrome, brave, edge, …) with multiple
        user-profiles, accounts from every populated profile are surfaced and
        deduped by email. Pass ``-v`` to see the originating user-profile per
        account, or ``--json`` for a structured ``browser_profile`` field.
        Use ``chrome::<profile-name-or-directory>`` to inspect only one
        Chromium user-profile.

        \b
        Examples:
          notebooklm auth inspect --browser chrome
          notebooklm auth inspect --browser 'chrome::Profile 1'
          notebooklm auth inspect --browser chrome -v
          notebooklm auth inspect --browser firefox --json
        """
        include_domains = _parse_include_domains(include_domains_raw)
        try:
            enum_result = _enumerate_browser_accounts(
                browser_name, verbose=not json_output, include_domains=include_domains
            )
        except httpx.RequestError as e:
            enum_result = NetworkFailure(
                code="NETWORK_ERROR",
                message=(
                    f"[red]Account discovery failed (network error):[/red] {e}\n"
                    "Check your internet connection and try again."
                ),
            )
        if isinstance(enum_result, BrowserCookieOutcome):
            _render_auth_inspect_error(enum_result, json_output=json_output)
        _, accounts = enum_result
        _render_auth_inspect(browser_name, list(accounts), json_output=json_output, verbose=verbose)

    @auth_group.command("check")
    @click.option(
        "--test", "test_fetch", is_flag=True, help="Test token fetch (makes network request)"
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def auth_check(ctx, test_fetch, json_output):
        """Check authentication status and diagnose issues.

        Validates that authentication is properly configured by checking:
        - Storage file exists and is readable
        - JSON structure is valid
        - Required cookies (SID + ``__Secure-1PSIDTS``) are present
        - Cookie domains are correct

        Use --test to also verify tokens can be fetched from NotebookLM
        (requires network access).

        \b
        Examples:
          notebooklm auth check           # Quick local validation
          notebooklm auth check --test    # Full validation with network test
          notebooklm auth check --json    # Machine-readable output
        """
        plan = plan_from_click_context(ctx, test_fetch=test_fetch, json_output=json_output)
        result = run_async(run_auth_check(plan))
        _render_auth_check_result(result)

    @auth_group.command("refresh")
    @click.option(
        "--browser-cookies",
        "--browser-cookie",
        "browser_cookies",
        default=None,
        is_flag=False,
        flag_value="auto",
        help=(
            "Re-extract cookies from an installed browser and match the profile "
            "account from context.json. Optionally specify browser: chrome, "
            "firefox, brave, edge, safari, arc, ... Use 'chrome::<profile>' "
            "for one Chromium profile or 'firefox::<container>' for one "
            "Firefox container."
        ),
    )
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Forward to the browser-cookie reader (only meaningful with "
            "--browser-cookies). Same syntax as 'notebooklm login "
            "--include-domains'."
        ),
    )
    @click.option(
        "--quiet", "-q", is_flag=True, help="Suppress success output (only print on error)"
    )
    @click.pass_context
    def auth_refresh(ctx, browser_cookies, include_domains_raw, quiet):
        """Refresh stored cookies by exercising the auth path once.

        One-shot keepalive: opens a session, runs the layer-1 poke against
        ``accounts.google.com`` to elicit ``__Secure-1PSIDTS`` rotation,
        fetches CSRF + session ID from ``notebooklm.google.com`` (discarded;
        their side effect is the cookie jar), and persists the rotated jar
        to ``storage_state.json`` on close. Designed to be scheduled by the
        OS (launchd / systemd / cron) so that an otherwise-idle profile
        does not stale out between user-driven calls.

        Cadence: 15-20 minutes is the recommended interval. Tighter is
        wasteful; significantly looser may cross the SIDTS server-side
        validity window for your account/region.

        Transient errors (e.g. ``httpx.RequestError`` from a flaky network)
        are surfaced as exit 1 rather than retried in-process; the OS
        scheduler's next firing is the retry mechanism.

        \b
        Examples:
          notebooklm auth refresh                 # one-shot, exit 0/1
          notebooklm auth refresh --browser-cookies chrome
          notebooklm --profile work auth refresh  # against a named profile
          watch -n 1200 notebooklm auth refresh   # quick in-terminal loop

        See docs/troubleshooting.md ("Cookie freshness for long-running /
        unattended use") for launchd / systemd / cron recipes.
        """
        with handle_errors():
            if has_env_auth_json():
                click.echo(
                    f"Error: 'auth refresh' is incompatible with {AUTH_JSON_ENV_NAME}. "
                    "The keepalive needs a writable storage_state.json to persist "
                    "rotated cookies. Either unset the env var for this "
                    "process and use a profile-backed storage file, or arrange for "
                    "the env var to be refreshed externally.",
                    err=True,
                )
                exit_with_code(1)

            include_domains = _parse_include_domains(include_domains_raw)
            if include_domains and browser_cookies is None:
                click.echo(
                    "Error: --include-domains only applies when --browser-cookies "
                    "is also set (the keepalive-only path does not re-extract cookies).",
                    err=True,
                )
                exit_with_code(1)

            profile = ctx.obj.get("profile") if ctx.obj else None
            storage_path = get_storage_path(profile=profile)

            if browser_cookies is not None:
                _refresh_from_browser_cookies(
                    browser_cookies,
                    storage_path=storage_path,
                    profile=profile,
                    quiet=quiet,
                    include_domains=include_domains,
                )
                return

            run_async(fetch_tokens_with_domains(storage_path, profile))

            from ..auth import read_account_metadata

            if storage_path.exists():
                metadata = read_account_metadata(storage_path)
                if not _is_valid_account_metadata(metadata):
                    repair_after_refresh(storage_path, quiet=quiet)

            if not quiet:
                console.print(f"[green]ok[/green] refreshed: {storage_path}")


# Backward-compat constant kept at module scope for tests that import it
# directly. The Playwright service owns the canonical definition.
GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"
