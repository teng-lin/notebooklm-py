"""CLI interaction adapter for transport-neutral browser-login orchestration.

The app layer owns validation, path preparation, capture order, and account
repair. This module retains the subprocess-bounded Chromium preflight and maps
typed app events to the historical Rich rendering contract.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Awaitable
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, Protocol

from ..._app.login_browser import (
    BrowserLoginEvent,
    BrowserLoginPlan,
    LoginFlagConflict,
    LoginPathError,
    run_browser_login,
)
from ..._app.login_browser import (
    repair_playwright_account_metadata as repair_account_metadata,
)
from .playwright_redaction import redact_subprocess_output

logger = logging.getLogger(__name__)


class LoginIO(Protocol):
    """Caller-injected command-side sink for browser login."""

    def emit(self, *args: Any, **kwargs: Any) -> None: ...

    def fail(self, code: int) -> NoReturn: ...

    def run_async(self, coro: Awaitable[Any]) -> Any: ...


ACCOUNT_METADATA_REMEDIATION = (
    "Run [cyan]notebooklm auth inspect --browser chrome -v[/cyan] "
    "or [cyan]notebooklm login --browser-cookies chrome --account EMAIL[/cyan]."
)

CHROMIUM_PRESENT_MARKER = "notebooklm-chromium-present"
CHROMIUM_MISSING_MARKER = "notebooklm-chromium-missing"

# This is executable source for a child interpreter, not an in-process import.
# The equality-pinned import-footprint guard owns this sole exception.
CHROMIUM_PROBE_SOURCE = f"""\
import os
import sys

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    path = playwright.chromium.executable_path
sys.stdout.write(
    "{CHROMIUM_PRESENT_MARKER}" if path and os.path.exists(path) else "{CHROMIUM_MISSING_MARKER}"
)
"""


def ensure_chromium_installed(io: LoginIO) -> None:
    """Probe for bundled Chromium in a child process and install when absent.

    Output is captured rather than streamed so it can be redacted before any
    diagnostic reaches the terminal. Probe and install are timeout-bounded.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", CHROMIUM_PROBE_SOURCE],
            capture_output=True,
            text=True,
            timeout=30,
        )
        saw_present = CHROMIUM_PRESENT_MARKER in result.stdout
        saw_missing = CHROMIUM_MISSING_MARKER in result.stdout
        chromium_missing = result.returncode == 0 and saw_missing and not saw_present
        if not chromium_missing:
            if result.stderr:
                logger.debug(
                    "chromium pre-flight probe stderr: %s",
                    redact_subprocess_output(result.stderr),
                )
            return

        io.emit("[yellow]Chromium browser not installed. Installing now...[/yellow]")
        install_result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if install_result.returncode != 0:
            sanitised_stderr = redact_subprocess_output(install_result.stderr or "").strip()
            sanitised_stdout = redact_subprocess_output(install_result.stdout or "").strip()
            diagnostic_tail = sanitised_stderr or sanitised_stdout
            io.emit(
                "[red]Failed to install Chromium browser.[/red]\n"
                f'Run manually: "{sys.executable}" -m playwright install chromium'
            )
            if diagnostic_tail:
                io.emit(
                    f"[dim]Subprocess output (sanitised):[/dim]\n{diagnostic_tail}",
                    markup=False,
                )
            io.fail(1)
        io.emit("[green]Chromium installed successfully.[/green]\n")
    except SystemExit:
        raise
    except subprocess.TimeoutExpired as exc:
        io.emit(
            f"[dim]Warning: Chromium pre-flight check timed out after "
            f"{exc.timeout}s. Proceeding anyway.[/dim]"
        )
    except Exception as exc:
        io.emit(f"[dim]Warning: Chromium pre-flight check failed: {exc}. Proceeding anyway.[/dim]")


def login_flag_conflict_message(conflict: LoginFlagConflict) -> str:
    """Render a typed app conflict using the stable CLI text."""
    messages = {
        "COOKIE_OPTIONS_REQUIRE_BROWSER_COOKIES": (
            "[red]Error: --account, --all-accounts, and --profile-name "
            "require --browser-cookies.[/red]"
        ),
        "ALL_ACCOUNTS_SELECTION_CONFLICT": (
            "[red]Error: --all-accounts cannot be combined with --account or --profile-name.[/red]"
        ),
        "ALL_ACCOUNTS_STORAGE_CONFLICT": (
            "[red]Error: --all-accounts writes one profile per account "
            "and cannot be combined with --storage.[/red]"
        ),
        "UPDATE_REQUIRES_ALL_ACCOUNTS": (
            "[red]Error: --update only applies to --all-accounts.[/red]"
        ),
    }
    return messages[conflict.code]


def login_path_error_message(error: LoginPathError) -> str:
    """Render a typed app path failure using the stable CLI text."""
    if error.code == "UNOWNED_BROWSER_PROFILE":
        return (
            "[red]Refusing to delete an unowned browser profile.[/red]\n"
            "The directory is not recognized as NotebookLM-managed:\n"
            f"{error.browser_profile}\n"
            "Move it aside, or remove it manually only if you know it is safe."
        )
    return (
        f"[red]Cannot clear browser profile: {error.detail}[/red]\n"
        "Close any open browser windows and try again.\n"
        f"If the problem persists, manually delete: {error.browser_profile}"
    )


def render_browser_login_event(event: BrowserLoginEvent, io: LoginIO) -> None:
    """Translate one typed app event to the historical Rich message."""
    if event.kind == "PROFILE":
        io.emit(f"[dim]Profile: {event.value}[/dim]")
    elif event.kind == "OPENING_BROWSER":
        io.emit(f"[yellow]Opening {event.value} for Google login...[/yellow]")
    elif event.kind == "BROWSER_PROFILE":
        io.emit(f"[dim]Using persistent profile: {event.value}[/dim]")
    elif event.kind == "ACCOUNT_IDENTIFYING":
        io.emit("[dim]Identifying Google account...[/dim]")
    elif event.kind == "ACCOUNT_WRITTEN":
        io.emit(f"[green]Account:[/green] {event.value}")
    elif event.kind == "ACCOUNT_ERROR":
        io.emit(
            "[yellow]Warning: account metadata was not written. "
            "NotebookLM auth still saved, but multi-account routing may "
            "fall back to authuser=0. "
            f"{ACCOUNT_METADATA_REMEDIATION} Details: {event.detail}[/yellow]"
        )
    else:
        io.emit(
            "[yellow]Warning: account metadata was not written; "
            f"{event.detail}. {ACCOUNT_METADATA_REMEDIATION}[/yellow]"
        )


def repair_playwright_account_metadata(
    storage_path: Path,
    io: LoginIO,
    *,
    page_html: str | None = None,
    quiet: bool = False,
) -> bool:
    """Run app-owned repair while retaining CLI rendering and async execution."""
    try:
        return repair_account_metadata(
            storage_path,
            emit_event=partial(render_browser_login_event, io=io),
            run_async=io.run_async,
            page_html=page_html,
            quiet=quiet,
        )
    finally:
        del storage_path, io, page_html


def run_playwright_login(plan: BrowserLoginPlan, io: LoginIO) -> None:
    """Drive app-owned login with the CLI preflight and rendering sinks."""
    run_browser_login(
        plan,
        emit_event=lambda event: render_browser_login_event(event, io),
        browser_emit=io.emit,
        fail=io.fail,
        run_async=io.run_async,
        chromium_preflight=lambda: ensure_chromium_installed(io),
    )


__all__ = [
    "CHROMIUM_MISSING_MARKER",
    "CHROMIUM_PRESENT_MARKER",
    "CHROMIUM_PROBE_SOURCE",
    "LoginIO",
    "ensure_chromium_installed",
    "login_flag_conflict_message",
    "login_path_error_message",
    "redact_subprocess_output",
    "render_browser_login_event",
    "repair_playwright_account_metadata",
    "run_playwright_login",
]
