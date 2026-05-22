"""``auth check`` diagnostic service.

Extracted from :mod:`notebooklm.cli.session_cmd` in P3.T3. Owns the
"validate the cookies on disk" probe plus the renderer for both text
(rich table) and JSON envelope modes.

Public surface
==============

* :class:`AuthCheckPlan` — frozen description of one ``auth check`` run.
* :class:`AuthCheckResult` — the structured outcome.
* :func:`run_auth_check` — sync executor. Reads the auth source, runs
  every check up to the user's opt-in level (``--test`` for token fetch),
  renders the result, and exits with the appropriate code.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.table import Table

from ..error_handler import exit_with_code
from ..rendering import console, json_output_response
from .auth_source import AUTH_JSON_ENV_NAME, AuthSource, read_env_auth_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthCheckPlan:
    """One ``auth check`` invocation.

    Attributes:
        storage_path: Resolved storage_state.json path (the file the
            check will read when no env-var auth is active).
        profile: Active profile name (forwarded to the token-fetch path
            so SID/SAPISID extraction targets the right account).
        has_env_auth: ``True`` when env-supplied auth is active;
            short-circuits the file-read in favor of parsing the env var.
        has_home_env: ``True`` when ``NOTEBOOKLM_HOME`` is set; used in
            the ``auth_source`` display string.
        test_fetch: When ``True``, also exercise the token-fetch path
            (network round-trip). Off by default.
        json_output: When ``True``, render the result as a JSON envelope
            and propagate non-zero exit on failure.
    """

    storage_path: Path
    profile: str | None
    has_env_auth: bool
    has_home_env: bool
    test_fetch: bool
    json_output: bool


@dataclass
class AuthCheckResult:
    """Outcome of a single ``auth check`` run.

    The ``checks`` dict mirrors the legacy contract from
    ``cli/session_cmd.py``: each value is ``True`` (passed), ``False``
    (failed), or ``None`` (not tested — only valid for ``token_fetch``).

    ``details`` carries human-readable context that the renderer joins
    into the table / JSON envelope.
    """

    checks: dict[str, bool | None]
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return all(v is True for v in self.checks.values() if v is not None)


def _make_initial_checks() -> dict[str, bool | None]:
    return {
        "storage_exists": False,
        "json_valid": False,
        "cookies_present": False,
        "sid_cookie": False,
        "token_fetch": None,
    }


def plan_from_click_context(ctx, *, test_fetch: bool, json_output: bool) -> AuthCheckPlan:
    """Build an :class:`AuthCheckPlan` from a Click context + flags.

    The profile + storage path come from the same :class:`AuthSource`
    resolver every other auth-aware command uses, so the diagnostic
    reports the same file the runtime would actually try to load.
    """
    auth = AuthSource.from_click_context(ctx)
    storage_path = auth.storage_path_for_diagnostics()
    has_env_auth = auth.has_env_auth
    has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))
    return AuthCheckPlan(
        storage_path=storage_path,
        profile=auth.profile,
        has_env_auth=has_env_auth,
        has_home_env=has_home_env,
        test_fetch=test_fetch,
        json_output=json_output,
    )


def _format_auth_source(plan: AuthCheckPlan) -> str:
    if plan.has_env_auth:
        return AUTH_JSON_ENV_NAME
    if plan.has_home_env:
        return f"$NOTEBOOKLM_HOME ({plan.storage_path})"
    return f"file ({plan.storage_path})"


def _read_storage_state(plan: AuthCheckPlan) -> tuple[dict[str, Any] | None, str | None]:
    """Read the storage_state dict from disk or env var.

    Returns ``(state, error_message)``. On success ``error_message`` is
    ``None``; on failure ``state`` is ``None`` and ``error_message``
    carries the user-facing description.
    """
    if plan.has_env_auth:
        # Env-var auth: read the inline JSON via the consolidated
        # :func:`read_env_auth_json` accessor so this module stays out
        # of the auth-source consolidation gate's grep.
        try:
            return json.loads(read_env_auth_json()), None
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON: {exc}"
    try:
        return json.loads(plan.storage_path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc}"
    except (OSError, UnicodeDecodeError) as exc:
        # P1.T3 contract: ``OSError`` on read (e.g. PermissionError) or
        # ``UnicodeDecodeError`` on a corrupt file must route through the
        # structured renderer so --json callers see a parseable
        # ``status: "error"`` envelope.
        return None, f"Storage unreadable: {exc}"


def run_auth_check(plan: AuthCheckPlan) -> AuthCheckResult:
    """Execute an ``auth check`` plan, render the result, and exit on failure.

    The function performs side effects (console output + ``exit_with_code``
    when ``--json`` and a check fails) so the handler can be a one-liner.
    Returns the :class:`AuthCheckResult` for tests / callers that want to
    inspect the structured outcome before the renderer runs.
    """
    from ...auth import extract_cookies_from_storage

    checks = _make_initial_checks()
    details: dict[str, Any] = {
        "storage_path": str(plan.storage_path),
        "auth_source": _format_auth_source(plan),
        "cookies_found": [],
        "cookie_domains": [],
        "error": None,
    }

    # Check 1: storage exists.
    if plan.has_env_auth:
        checks["storage_exists"] = True
    else:
        checks["storage_exists"] = plan.storage_path.exists()

    if not checks["storage_exists"]:
        details["error"] = f"Storage file not found: {plan.storage_path}"
        return _finalize(plan, checks, details)

    # Check 2: JSON valid.
    storage_state, read_error = _read_storage_state(plan)
    if storage_state is None:
        details["error"] = read_error
        return _finalize(plan, checks, details)
    checks["json_valid"] = True

    # Check 3: cookies present + SID lookup.
    try:
        cookies = extract_cookies_from_storage(storage_state)
        checks["cookies_present"] = True
        checks["sid_cookie"] = "SID" in cookies
        details["cookies_found"] = list(cookies.keys())

        cookies_by_domain: dict[str, list[str]] = {}
        for cookie in storage_state.get("cookies", []):
            domain = cookie.get("domain", "")
            name = cookie.get("name", "")
            if domain and name and "google" in domain.lower():
                cookies_by_domain.setdefault(domain, []).append(name)
        details["cookies_by_domain"] = cookies_by_domain
        details["cookie_domains"] = sorted(cookies_by_domain.keys())
    except ValueError as exc:
        details["error"] = str(exc)
        return _finalize(plan, checks, details)

    # Check 4: optional token-fetch round-trip.
    if plan.test_fetch:
        try:
            from ...auth import fetch_tokens_with_domains
            from ..runtime import run_async

            token_path = None if plan.has_env_auth else plan.storage_path
            csrf, session_id = run_async(fetch_tokens_with_domains(token_path, plan.profile))
            checks["token_fetch"] = True
            details["csrf_length"] = len(csrf)
            details["session_id_length"] = len(session_id)
        except Exception as exc:
            checks["token_fetch"] = False
            details["error"] = f"Token fetch failed: {exc}"

    return _finalize(plan, checks, details)


def _finalize(
    plan: AuthCheckPlan, checks: dict[str, bool | None], details: dict[str, Any]
) -> AuthCheckResult:
    """Render the result, exit on JSON failure, return the structured outcome."""
    result = AuthCheckResult(checks=checks, details=details)
    render_auth_check(result, json_output=plan.json_output)
    return result


def render_auth_check(result: AuthCheckResult, *, json_output: bool) -> None:
    """Render an :class:`AuthCheckResult` to the console.

    Exits non-zero via :func:`exit_with_code` when ``--json`` and any
    check failed, matching the legacy contract.
    """
    all_passed = result.all_passed
    checks = result.checks
    details = result.details

    if json_output:
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


def render_auth_inspect(
    browser_name: str,
    accounts: list,
    *,
    json_output: bool,
    verbose: bool,
) -> None:
    """Render ``auth inspect`` results (text table or JSON envelope).

    The handler stays a thin Click wrapper around :func:`_enumerate_browser_accounts`
    + this renderer.
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


__all__ = [
    "AuthCheckPlan",
    "AuthCheckResult",
    "plan_from_click_context",
    "render_auth_check",
    "render_auth_inspect",
    "run_auth_check",
]
