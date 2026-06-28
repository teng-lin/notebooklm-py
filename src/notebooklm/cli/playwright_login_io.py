"""Command-layer IO seam + wrappers for the Playwright login flow (#1391).

:mod:`notebooklm.cli.services.playwright_login` is a pure service: it owns the
browser-automation logic but no longer imports the command layer's
presentation (``..rendering``), exit-policy (``..error_handler``), or
async-runner (``..runtime``) modules. This module sits on the *command* side of
the ADR-0008 boundary and supplies the concrete sink the service's
``LoginIO`` Protocol describes, plus the thin orchestration wrappers that drive
the service from ``session_cmd``:

* :class:`PlaywrightLoginIO` (+ :func:`make_login_io`) — the concrete
  ``console.print`` / ``exit_with_code`` / ``run_async`` sink.
* :func:`validate_flags_or_exit` — render-and-exit wrapper over
  ``validate_login_flag_conflicts`` (which now returns a typed ``Conflict``).
* :func:`prepare_paths_or_exit` — render-and-exit wrapper over
  ``prepare_login_paths`` (which now returns a typed
  ``PreparedPaths | PathError``); preserves the legacy
  ``(storage_path, browser_profile)`` 2-tuple contract.
* :func:`run_login` — drives ``run_playwright_login`` with the concrete sink.
* :func:`repair_after_refresh` — drives ``repair_playwright_account_metadata``
  with the concrete sink (used by the file-backed ``auth refresh`` keepalive).

Keeping the sink + wrappers here (not in ``session_cmd``) lets the command
module collapse its five Playwright import blocks into one and keeps the
orchestration that the service shed from re-inflating the handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click

from .error_handler import exit_with_code
from .rendering import console, json_error_response
from .runtime import run_async
from .services.login.io_seam import set_default_login_io_factory
from .services.playwright_login import (
    PathError,
    PlaywrightLoginPlan,
    prepare_login_paths,
    repair_playwright_account_metadata,
    run_playwright_login,
    validate_login_flag_conflicts,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from .services.playwright_login import LoginIO


@dataclass(frozen=True)
class PlaywrightLoginIO:
    """Concrete command-layer sink satisfying the service's ``LoginIO`` Protocol.

    Each method forwards to the canonical command-layer collaborator:

    * :meth:`emit` → ``console.print`` (``*args, **kwargs`` pass through
      verbatim, so ``markup=False`` reaches Rich unchanged).
    * :meth:`fail` → ``exit_with_code`` (raises ``SystemExit``).
    * :meth:`run_async` → ``run_async``.

    Frozen + stateless: a single shared instance is reused per login flow.
    """

    def emit(self, *args: Any, **kwargs: Any) -> None:
        console.print(*args, **kwargs)

    def fail(self, code: int) -> NoReturn:
        exit_with_code(code)

    def run_async(self, coro: Awaitable[Any]) -> Any:
        return run_async(coro)


def make_login_io() -> LoginIO:
    """Build the concrete :class:`PlaywrightLoginIO` sink for the login flow."""
    return PlaywrightLoginIO()


# Register this concrete sink as the browser-cookie login service's default at
# import time (#1393). The login DAG (``services/login/*``) inverts its
# presentation / exit / async reach-ins behind a ``LoginIO`` Protocol and
# resolves an explicit injected sink first; when none is injected (direct
# callers, and tests that exercise the service through the command layer) it
# falls back to this factory so behavior is byte-for-byte identical. Importing
# this command-layer module is what wires the default — the service never
# imports across the ADR-0008 boundary itself.
set_default_login_io_factory(make_login_io)


def validate_flags_or_exit(
    *,
    browser_cookies: str | None,
    account_email: str | None,
    all_accounts: bool,
    update: bool,
    profile_name: str | None,
    storage: str | None,
) -> None:
    """Validate ``login`` flag mutual-exclusion, emitting + exiting 1 on conflict.

    Thin command-layer wrapper: the service returns a typed ``Conflict`` (or
    ``None``); here we render its message and exit, preserving the historical
    rendered contract.
    """
    conflict = validate_login_flag_conflicts(
        browser_cookies=browser_cookies,
        account_email=account_email,
        all_accounts=all_accounts,
        update=update,
        profile_name=profile_name,
        storage=storage,
    )
    if conflict is not None:
        console.print(conflict.message)
        exit_with_code(1)


def prepare_paths_or_exit(
    profile: str | None, storage: str | None, fresh: bool
) -> tuple[Path, Path]:
    """Resolve login paths, emitting the ``--fresh`` notice / exiting 1 on failure.

    Wraps the service's ``prepare_login_paths`` (now returning a typed
    ``PreparedPaths | PathError``): on a ``--fresh`` wipe it emits the
    cleared-session line; on an OSError it emits the error block and exits 1.
    Returns the legacy ``(storage_path, browser_profile)`` 2-tuple so the
    command handler is unchanged.
    """
    outcome = prepare_login_paths(profile, storage, fresh)
    if isinstance(outcome, PathError):
        console.print(outcome.message)
        exit_with_code(1)
    else:
        # ``outcome`` is narrowed to ``PreparedPaths`` here. The ``else`` is
        # explicit so the narrowing does not rely on every tool recognising
        # ``exit_with_code`` as ``NoReturn``.
        if outcome.fresh_cleared:
            console.print("[yellow]Cleared cached browser session (--fresh)[/yellow]")
        return outcome.storage_path, outcome.browser_profile


def run_login(plan: PlaywrightLoginPlan) -> None:
    """Drive ``run_playwright_login`` with the concrete command-layer sink."""
    run_playwright_login(plan, make_login_io())


def repair_after_refresh(
    storage_path: Path,
    *,
    page_html: str | None = None,
    quiet: bool = False,
) -> bool:
    """Drive ``repair_playwright_account_metadata`` with the concrete sink.

    Used by the file-backed ``auth refresh`` keepalive path. ``quiet`` stays a
    parameter (the ``LoginIO`` Protocol has no silencing concept); it is
    forwarded to the service unchanged.
    """
    return repair_playwright_account_metadata(
        storage_path, make_login_io(), page_html=page_html, quiet=quiet
    )


def _verify_token_fetch_after_refresh(
    storage_path: Path, profile: str | None, *, quiet: bool, json_output: bool = False
) -> None:
    """Confirm a token fetch actually succeeds after ``auth refresh``.

    Runs the strictly read-only passive probe (no NOTEBOOKLM_REFRESH_CMD, no
    cookie rotation, no write). A successful ``auth refresh`` — especially the
    ``--browser-cookies`` rewrite — does not by itself prove the resulting
    cookies authenticate; ``--verify`` makes that an explicit, fail-loud gate
    so unattended schedulers can rely on the exit code (issue #1569).

    With ``json_output`` a verify failure is emitted as the error envelope on
    stdout (exit 1); otherwise the human ``Error: …`` line goes to stderr. The
    ``fetch_tokens_passive`` import is deferred so the ``notebooklm.auth`` patch
    seam used by the auth-subcommand tests stays effective.
    """
    from ..auth import fetch_tokens_passive

    try:
        run_async(fetch_tokens_passive(storage_path, profile))
    except Exception as exc:  # noqa: BLE001 — surface any failure as a clean exit 1
        message = f"refresh completed but the post-refresh token fetch failed: {exc}"
        if json_output:
            json_error_response("post_refresh_token_fetch_failed", message)  # NoReturn
        # Non-json path only (the json branch above exits): human error on stderr.
        click.echo(f"Error: {message}", err=True)
        exit_with_code(1)
    # Suppress the human success line in --json mode too (not just --quiet), so the
    # caller's single JSON document is the only thing on stdout.
    if not quiet and not json_output:
        console.print("[green]ok[/green] verified: token fetch succeeds after refresh")


__all__ = [
    "PlaywrightLoginIO",
    "_verify_token_fetch_after_refresh",
    "make_login_io",
    "prepare_paths_or_exit",
    "repair_after_refresh",
    "run_login",
    "validate_flags_or_exit",
]
