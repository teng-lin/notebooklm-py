"""Session and context management CLI commands.

Commands:
    login   Log in to NotebookLM via browser
    use     Set the current notebook context
    status  Show current context
    clear   Clear current notebook context
    auth    Authentication management (logout / inspect / check / refresh)

This module is split into thin Click handlers over service modules for
Playwright login, browser-cookie login/refresh, session context,
auth diagnostics, and auth-source precedence. Command-side wrappers in
:mod:`notebooklm.cli.playwright_login_io` provide the concrete rendering,
exit, and async-runner seams for the Playwright and browser-cookie login
services.

Body-used names that moved into services are re-imported here as command-layer
bindings and legacy patch seams.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click
import httpx

from ..exceptions import AuthError, NotebookNotFoundError
from ..paths import get_storage_path

# Cookie-JSON import helpers (split out to keep this module under the size budget).
from ._cookie_import import _import_cookie_json, _read_auth_json_input

# Render helpers stay in a sibling module; command registration calls them here.
from ._session_render import (
    _render_auth_check_result,
    _render_auth_inspect,
    _render_auth_inspect_error,
    _render_logout_outcome,
    _render_status,
    _use_notebook_table,
)
from .auth_runtime import (
    auth_check_notebook_count,
    handle_auth_error,
    resolve_client_factory,
    run_client_workflow,
)
from .context import clear_context, set_current_notebook
from .error_handler import _output_error, exit_with_code, handle_errors
from .playwright_login_io import (
    _verify_token_fetch_after_refresh,
    prepare_paths_or_exit,
    repair_after_refresh,
    run_login,
    validate_flags_or_exit,
)
from .rendering import console, json_error_response, json_output_response
from .resolve import resolve_notebook_id
from .runtime import run_async
from .services.auth_diagnostics import (
    plan_from_click_context,
    run_auth_check,
)
from .services.auth_source import AUTH_JSON_ENV_NAME, auth_source_from_ctx, has_env_auth_json

# Direct imports replace the D1-PR-3-retired forwarding wrappers; see ADR-0008.
from .services.login import (
    _enumerate_browser_accounts,
    _login_all_accounts_from_browser,
    _login_browser_cookies_single,
    _refresh_from_browser_cookies,
    _sync_server_language_to_config,
)
from .services.login import cookie_domains as _cookie_domains
from .services.login.exceptions import LoginConfigurationError
from .services.login.outcomes import BrowserCookieOutcome, NetworkFailure
from .services.playwright_login import (
    CHANNEL_BROWSERS as _CHANNEL_BROWSERS,
)
from .services.playwright_login import (
    PlaywrightLoginPlan,
)
from .services.session_context import (
    UseNotebookResult,
    execute_logout,
    read_status,
    verify_and_set_notebook,
)

if TYPE_CHECKING:
    from ..client import NotebookLMClient

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
            "in the browser. Required with --master-token; otherwise only valid "
            "with --browser-cookies."
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
    @click.option(
        "--master-token",
        "master_token",
        is_flag=True,
        default=False,
        help=(
            "Headless auth: bootstrap a durable Google master token (one browser "
            "sign-in), then mint web cookies from it with no per-session browser. "
            "Requires --account EMAIL. Needs pip install 'notebooklm-py[headless]'; "
            "the browser oauth_token capture also needs [browser] — or skip it by "
            "passing --oauth-token. See docs/installation.md#headless."
        ),
    )
    @click.option(
        "--master-token-refresh",
        "master_token_refresh",
        is_flag=True,
        default=False,
        help="Re-mint web cookies from the stored master token (no prompt). For recovery / cron.",
    )
    @click.option(
        "--oauth-token",
        "oauth_token",
        default=None,
        help="Single-use EmbeddedSetup oauth_token for --master-token (else captured via browser).",
    )
    @click.option(
        "--android-id",
        "android_id",
        default=None,
        help="Override the per-install Android id for --master-token (default: generated/persisted).",
    )
    @click.option(
        "--cdp-url",
        "cdp_url",
        default=None,
        help="Attach oauth_token capture to a running Chrome via CDP (e.g. http://localhost:9222).",
    )
    @click.option(
        "--force",
        "force",
        is_flag=True,
        default=False,
        help="With --master-token: overwrite even if the profile belongs to a different account.",
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
        master_token,
        master_token_refresh,
        oauth_token,
        android_id,
        cdp_url,
        force,
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

            if master_token or master_token_refresh:
                from .master_token_login import run_master_token_login

                run_master_token_login(
                    ctx,
                    storage=storage,
                    browser=browser,
                    account_email=account_email,
                    oauth_token=oauth_token,
                    android_id=android_id,
                    cdp_url=cdp_url,
                    refresh=master_token_refresh,
                    force=force,
                )
                return

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
            client_factory=resolve_client_factory(ctx),
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
                        "modified_at": nb.modified_at.isoformat() if nb.modified_at else None,
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
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def clear_cmd(json_output):
        """Clear current notebook context."""
        cleared = clear_context()
        if json_output:
            # Preserve the actual outcome so automation can tell a real clear
            # from a no-op (the text path stays idempotent for humans).
            json_output_response(
                {"status": "cleared" if cleared else "already_clear", "cleared": cleared}
            )
            return
        console.print("[green]Context cleared[/green]")

    @cli.group("auth")
    def auth_group():
        """Authentication management commands."""
        pass

    @auth_group.command("logout")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def auth_logout(ctx, json_output):
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
        _render_logout_outcome(outcome, json_output=json_output)

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

    @auth_group.command("import-cookies")
    @click.argument("json_path", type=click.Path(exists=False))
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Opt in to persisting sibling-product cookies. Same syntax as "
            "'notebooklm login --include-domains': youtube, docs, myaccount, "
            "mail, all. By default, only required Google auth/Drive/NotebookLM "
            "cookie domains are kept."
        ),
    )
    @click.option(
        "--include-optional",
        is_flag=True,
        default=False,
        help="Persist all optional sibling-product cookie domains.",
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option("--quiet", "quiet", is_flag=True, help="Suppress success output")
    @click.pass_context
    def auth_import_cookies(
        ctx, json_path, include_domains_raw, include_optional, json_output, quiet
    ):
        """Import authentication cookies from JSON and save them persistently.

        Accepts either a Playwright ``storage_state`` object (``{"cookies": [...]}``)
        or a bare JSON list of cookie objects, including JSON exported from many
        browser-cookie tools. Use ``-`` to read JSON from stdin.

        The imported cookies are filtered through the same domain allowlist used
        by browser login, validated locally for NotebookLM-required cookies, and
        written atomically to the active profile's ``storage_state.json`` (or the
        root ``--storage`` override) with private file permissions.

        Examples:
          notebooklm auth import-cookies cookies.json
          notebooklm -p work auth import-cookies playwright-storage-state.json
          cat cookies.json | notebooklm auth import-cookies -
        """
        with handle_errors(json_output=json_output):
            auth_source = auth_source_from_ctx(ctx)
            if auth_source.has_env_auth:
                raise click.ClickException(  # cli-input-validation: import-cookies env-auth conflict
                    f"'auth import-cookies' is incompatible with {AUTH_JSON_ENV_NAME}. "
                    "Unset the env var first so the imported cookies can be used "
                    "from storage_state.json."
                )

            include_domains = _parse_include_domains(include_domains_raw)
            storage_path = auth_source.storage_path_for_diagnostics()

            imported, backup_path = _import_cookie_json(
                payload=_read_auth_json_input(json_path),
                storage_path=storage_path,
                include_domains=include_domains,
                include_optional=include_optional,
            )

            if json_output:
                json_output_response(
                    {
                        "success": True,
                        "storage_path": str(storage_path),
                        "cookie_count": len(imported.get("cookies", [])),
                        "backup_path": str(backup_path) if backup_path else None,
                    }
                )
            elif not quiet:
                console.print(
                    f"[green]ok[/green] imported {len(imported.get('cookies', []))} "
                    f"cookies to: {storage_path}"
                )
                if backup_path:
                    console.print(f"[dim]previous session backed up to: {backup_path}[/dim]")

    @auth_group.command("check")
    @click.option(
        "--test", "test_fetch", is_flag=True, help="Test token fetch (makes network request)"
    )
    @click.option(
        "--passive",
        is_flag=True,
        help=(
            "With --test, validate read-only: never run NOTEBOOKLM_REFRESH_CMD, "
            "rotate cookies, or write to disk. For unattended health checks."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def auth_check(ctx, test_fetch, passive, json_output):
        """Check authentication status and diagnose issues.

        Validates that authentication is properly configured by checking:
        - Storage file exists and is readable
        - JSON structure is valid
        - Required cookies (SID + ``__Secure-1PSIDTS``) are present
        - Cookie domains are correct

        Use --test to also verify tokens can be fetched from NotebookLM
        (requires network access). Add --passive so that token test is strictly
        read-only — it never triggers NOTEBOOKLM_REFRESH_CMD, rotates cookies,
        or writes storage, which is what a passive readiness probe wants.

        Exits 0 only when every executed check passes; non-zero otherwise, in
        both text and --json modes.

        \b
        Examples:
          notebooklm auth check                  # Quick local validation
          notebooklm auth check --test           # Full validation with network test
          notebooklm auth check --test --passive # Read-only probe (no refresh/no write)
          notebooklm auth check --json           # Machine-readable output
        """
        if passive and not test_fetch:
            # The local cookie checks are already side-effect-free, so --passive
            # only changes the optional --test token fetch. Warn (don't fail) so
            # a caller does not mistake a local-only run for a network probe.
            click.echo(
                "Note: --passive has no effect without --test "
                "(the local cookie checks never run a refresh command or write to disk).",
                err=True,
            )
        plan = plan_from_click_context(
            ctx, test_fetch=test_fetch, json_output=json_output, passive=passive
        )
        result = run_async(run_auth_check(plan))

        # Live notebook count: a "the API really accepts this session" signal
        # beyond the homepage token round-trip. Computed here (not in the neutral
        # core, which never opens a client) and only when the token fetch already
        # passed. Skipped for --passive: opening a client may rotate/persist
        # cookies, which the read-only contract (issue #1569) forbids.
        if test_fetch and not passive and result.checks["token_fetch"] is True:
            # cli-rpc-unenveloped: the notebook count is a best-effort liveness
            # probe whose failures degrade to null, so this RPC must NOT route
            # through the error envelope (a failed count must not fail the auth
            # check or hijack its exit code).
            result.details["notebook_count"] = auth_check_notebook_count(ctx)

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
    @click.option(
        "--verify",
        is_flag=True,
        help=(
            "After refreshing, confirm a token fetch actually succeeds (read-only "
            "passive probe). Exit non-zero if the post-refresh cookies still fail."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def auth_refresh(ctx, browser_cookies, include_domains_raw, quiet, verify, json_output):
        """Refresh stored cookies by exercising the auth path once or reading browser cookies.

        Default mode is a one-shot keepalive: opens a session, runs the
        layer-1 poke against ``accounts.google.com`` to elicit
        ``__Secure-1PSIDTS`` rotation, fetches CSRF + session ID from
        ``notebooklm.google.com`` (discarded; their side effect is the cookie
        jar), and persists the rotated jar to ``storage_state.json`` on close.

        With ``--browser-cookies``, re-extracts cookies from the selected
        installed browser, matches the stored profile account, rewrites the
        profile's ``storage_state.json``, and refreshes account metadata.

        Designed to be scheduled by the OS (launchd / systemd / cron) so
        that an otherwise-idle profile does not stale out between
        user-driven calls.

        Cadence: 15-20 minutes is the recommended interval for the default
        keepalive path. Tighter is wasteful; significantly looser may cross
        the SIDTS server-side validity window for your account/region.

        Transient errors (e.g. ``httpx.RequestError`` from a flaky network)
        are surfaced as exit 1 rather than retried in-process; the OS
        scheduler's next firing is the retry mechanism.

        With ``--verify``, after the refresh completes a read-only passive token
        fetch confirms the resulting cookies actually authenticate, exiting
        non-zero if not. A successful refresh command alone does not prove the
        post-refresh cookies work (they may still redirect to sign-in).

        \b
        Examples:
          notebooklm auth refresh                 # one-shot, exit 0/1
          notebooklm auth refresh --verify        # refresh, then confirm token fetch works
          notebooklm auth refresh --browser-cookies chrome --verify
          notebooklm --profile work auth refresh  # against a named profile
          watch -n 1200 notebooklm auth refresh   # quick in-terminal loop

        See docs/troubleshooting.md ("Cookie freshness for long-running /
        unattended use") for launchd / systemd / cron recipes.
        """

        def _fail(code: str, message: str) -> NoReturn:
            # --json -> envelope on stdout (NoReturn); else human stderr. Both exit 1.
            if json_output:
                json_error_response(code, message)
            click.echo(f"Error: {message}", err=True)
            exit_with_code(1)

        with handle_errors(json_output=json_output):
            if has_env_auth_json():
                _fail(
                    "auth_json_env_conflict",
                    f"'auth refresh' is incompatible with {AUTH_JSON_ENV_NAME}. "
                    "The keepalive needs a writable storage_state.json to persist "
                    "rotated cookies. Either unset the env var for this "
                    "process and use a profile-backed storage file, or arrange for "
                    "the env var to be refreshed externally.",
                )

            include_domains = _parse_include_domains(include_domains_raw)
            if include_domains and browser_cookies is None:
                _fail(
                    "include_domains_without_browser_cookies",
                    "--include-domains only applies when --browser-cookies "
                    "is also set (the keepalive-only path does not re-extract cookies).",
                )

            # --json is keepalive-only (--browser-cookies prints to stdout) — refuse.
            if json_output and browser_cookies is not None:
                _fail(
                    "json_unsupported_with_browser_cookies",
                    "--json is not supported with --browser-cookies; use the "
                    "default keepalive refresh with --json instead.",
                )

            # --json suppresses human status lines (like --quiet); a verify failure
            # emits the error envelope on stdout in --json mode, else on stderr.
            quiet = quiet or json_output

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
            else:
                run_async(fetch_tokens_with_domains(storage_path, profile))

                from ..auth import read_account_metadata

                if storage_path.exists():
                    metadata = read_account_metadata(storage_path)
                    if not _is_valid_account_metadata(metadata):
                        repair_after_refresh(storage_path, quiet=quiet)

                if not quiet:
                    console.print(f"[green]ok[/green] refreshed: {storage_path}")

            if verify:
                _verify_token_fetch_after_refresh(
                    storage_path, profile, quiet=quiet, json_output=json_output
                )

            if json_output:
                json_output_response(
                    {"status": "ok", "storage_path": str(storage_path), "verified": verify}
                )


# Backward-compat constant kept at module scope for tests that import it
# directly. The Playwright service owns the canonical definition.
GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"
