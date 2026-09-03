"""Transport-neutral orchestration for interactive browser login.

The optional browser implementation stays behind coarse operations on
``notebooklm.auth``. This module owns validation, path preparation, call order,
and account repair; the CLI adapter owns Chromium installation and rendering.
"""

from __future__ import annotations

import inspect
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeAlias

import httpx

from .. import auth
from ..paths import (
    BROWSER_PROFILE_OWNERSHIP_MARKER,
    browser_profile_is_owned,
    get_browser_profile_dir,
    get_storage_path,
    resolve_profile,
)
from .profile import ProfileRepairRequest, repair_playwright_account

logger = logging.getLogger(__name__)

BrowserEmit: TypeAlias = Callable[..., None]
Fail: TypeAlias = Callable[[int], NoReturn]
RunAsync: TypeAlias = Callable[[Awaitable[Any]], Any]
BrowserLoginEventSink: TypeAlias = Callable[["BrowserLoginEvent"], None]
ChromiumPreflight: TypeAlias = Callable[[], None]

LoginFlagConflictCode: TypeAlias = Literal[
    "COOKIE_OPTIONS_REQUIRE_BROWSER_COOKIES",
    "ALL_ACCOUNTS_SELECTION_CONFLICT",
    "ALL_ACCOUNTS_STORAGE_CONFLICT",
    "UPDATE_REQUIRES_ALL_ACCOUNTS",
]
PathErrorCode: TypeAlias = Literal["UNOWNED_BROWSER_PROFILE", "CLEAR_BROWSER_PROFILE_FAILED"]
BrowserLoginEventKind: TypeAlias = Literal[
    "PROFILE",
    "OPENING_BROWSER",
    "BROWSER_PROFILE",
    "ACCOUNT_IDENTIFYING",
    "ACCOUNT_WRITTEN",
    "ACCOUNT_ERROR",
    "ACCOUNT_UNRESOLVED",
]


@dataclass(frozen=True)
class LoginFlagConflict:
    """A transport-neutral login-option conflict."""

    code: LoginFlagConflictCode


@dataclass(frozen=True)
class PreparedLoginPaths:
    """Resolved storage and persistent-browser paths for one login."""

    storage_path: Path
    browser_profile: Path
    fresh_cleared: bool


@dataclass(frozen=True)
class LoginPathError:
    """A safe path-preparation failure for adapter rendering."""

    code: PathErrorCode
    browser_profile: Path
    detail: str | None = None


@dataclass(frozen=True)
class BrowserLoginEvent:
    """One adapter-rendered event emitted by browser-login orchestration."""

    kind: BrowserLoginEventKind
    value: str | Path | None = None
    detail: str | None = None


@dataclass(frozen=True)
class BrowserLoginPlan:
    """Inputs for one interactive browser login."""

    browser: str
    browser_profile: Path
    storage_path: Path
    include_domains: set[str] | None = None
    login_timeout_s: int = 300


def browser_login_channels() -> tuple[tuple[str, str], ...]:
    """Return the facade-owned immutable browser-channel choices."""
    return auth.browser_login_channels()


def validate_login_flag_conflicts(
    *,
    browser_cookies: str | None,
    account_email: str | None,
    all_accounts: bool,
    update: bool,
    profile_name: str | None,
    storage: str | None,
) -> LoginFlagConflict | None:
    """Return the first invalid login-option combination, if any."""
    if browser_cookies is None and (
        account_email is not None or all_accounts or profile_name is not None
    ):
        return LoginFlagConflict("COOKIE_OPTIONS_REQUIRE_BROWSER_COOKIES")
    if all_accounts and (account_email is not None or profile_name is not None):
        return LoginFlagConflict("ALL_ACCOUNTS_SELECTION_CONFLICT")
    if all_accounts and storage:
        return LoginFlagConflict("ALL_ACCOUNTS_STORAGE_CONFLICT")
    if update and not all_accounts:
        return LoginFlagConflict("UPDATE_REQUIRES_ALL_ACCOUNTS")
    return None


def prepare_login_paths(
    profile: str | None,
    storage: str | None,
    fresh: bool,
) -> PreparedLoginPaths | LoginPathError:
    """Resolve, optionally clear, and securely create login storage paths."""
    if storage:
        storage_path = Path(storage)
        browser_profile = get_browser_profile_dir(storage_path=storage_path)
    elif profile:
        storage_path = get_storage_path(profile=profile)
        browser_profile = get_browser_profile_dir(profile=profile)
    else:
        storage_path = get_storage_path()
        browser_profile = get_browser_profile_dir()

    fresh_cleared = False
    if fresh and browser_profile.exists():
        if storage and not browser_profile_is_owned(storage_path, browser_profile):
            return LoginPathError("UNOWNED_BROWSER_PROFILE", browser_profile)
        try:
            shutil.rmtree(browser_profile)
            fresh_cleared = True
        except OSError as exc:
            logger.error("Failed to clear browser profile %s: %s", browser_profile, exc)
            return LoginPathError(
                "CLEAR_BROWSER_PROFILE_FAILED",
                browser_profile,
                detail=str(exc),
            )

    browser_profile_existed = browser_profile.exists()
    if sys.platform == "win32":
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        browser_profile.mkdir(parents=True, exist_ok=True)
    else:
        storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        storage_path.parent.chmod(0o700)
        browser_profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        browser_profile.chmod(0o700)

    if storage and not browser_profile_existed:
        (browser_profile / BROWSER_PROFILE_OWNERSHIP_MARKER).touch()

    return PreparedLoginPaths(
        storage_path=storage_path,
        browser_profile=browser_profile,
        fresh_cleared=fresh_cleared,
    )


def repair_playwright_account_metadata(
    storage_path: Path,
    *,
    emit_event: BrowserLoginEventSink,
    run_async: RunAsync,
    page_html: str | None = None,
    quiet: bool = False,
) -> bool:
    """Populate persisted account metadata when the active account is unambiguous."""
    operation = None
    result = None
    try:
        if not quiet:
            emit_event(BrowserLoginEvent("ACCOUNT_IDENTIFYING"))
        operation = repair_playwright_account(
            ProfileRepairRequest(storage_path=storage_path, page_html=page_html)
        )
        try:
            try:
                result = run_async(operation)
            except BaseException:
                if inspect.getcoroutinestate(operation) == inspect.CORO_CREATED:
                    operation.close()
                raise
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            if not quiet:
                emit_event(BrowserLoginEvent("ACCOUNT_ERROR", detail=str(exc)))
            return False
        if result.status == "WRITTEN":
            if not quiet:
                emit_event(BrowserLoginEvent("ACCOUNT_WRITTEN", value=result.email))
            return True
        if not quiet:
            kind: BrowserLoginEventKind = (
                "ACCOUNT_ERROR" if result.status == "ERROR" else "ACCOUNT_UNRESOLVED"
            )
            emit_event(BrowserLoginEvent(kind, detail=result.detail))
        return False
    finally:
        del storage_path, emit_event, run_async, page_html, operation, result


def run_browser_login(
    plan: BrowserLoginPlan,
    *,
    emit_event: BrowserLoginEventSink,
    browser_emit: BrowserEmit,
    fail: Fail,
    run_async: RunAsync,
    chromium_preflight: ChromiumPreflight,
) -> None:
    """Run availability, preflight, capture, and account repair in stable order."""
    auth.ensure_browser_login_available(plan.browser, emit=browser_emit, fail=fail)
    if plan.browser == "chromium":
        chromium_preflight()

    channel_labels = dict(browser_login_channels())
    browser_label = channel_labels.get(plan.browser, "Chromium")
    emit_event(BrowserLoginEvent("PROFILE", value=resolve_profile()))
    emit_event(BrowserLoginEvent("OPENING_BROWSER", value=browser_label))
    emit_event(BrowserLoginEvent("BROWSER_PROFILE", value=plan.browser_profile))

    page_html = None
    try:
        page_html = auth.run_browser_login_capture(
            browser=plan.browser,
            browser_profile=plan.browser_profile,
            storage_path=plan.storage_path,
            include_domains=plan.include_domains,
            login_timeout_s=plan.login_timeout_s,
            emit=browser_emit,
            fail=fail,
        )
        repair_playwright_account_metadata(
            plan.storage_path,
            emit_event=emit_event,
            run_async=run_async,
            page_html=page_html,
        )
    finally:
        del page_html


__all__ = [
    "BrowserLoginEvent",
    "BrowserLoginPlan",
    "LoginFlagConflict",
    "LoginPathError",
    "PreparedLoginPaths",
    "browser_login_channels",
    "prepare_login_paths",
    "repair_playwright_account_metadata",
    "run_browser_login",
    "validate_login_flag_conflicts",
]
