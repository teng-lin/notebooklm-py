"""Transport-neutral orchestration for master-token CLI workflows.

The public :mod:`notebooklm.auth` facade remains the transaction owner.  This
module only groups those transactions into coarse app operations so command
adapters do not assemble authentication policy themselves.
"""

from __future__ import annotations

import inspect
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .. import auth
from ..auth import MasterTokenError as MasterTokenError
from ..paths import master_token_path_for

T = TypeVar("T")


class CoroutineRunner(Protocol):
    """Synchronous capability that settles one native coroutine."""

    def __call__(self, awaitable: Coroutine[Any, Any, T], /) -> T: ...


class OAuthTokenCapture(Protocol):
    """Interactive browser capability supplied by the command adapter."""

    def __call__(
        self,
        *,
        browser: str,
        cdp_url: str | None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class MasterTokenLoginPlan:
    """Non-secret inputs for one master-token bootstrap."""

    email: str
    storage_path: Path
    android_id: str | None
    force: bool


@dataclass(frozen=True, slots=True)
class MasterTokenLoginSuccess:
    """Secret-free projection of a completed bootstrap."""

    notebook_count: int
    storage_path: Path


@dataclass(frozen=True, slots=True)
class MasterTokenRemintSuccess:
    """Secret-free projection of a completed forced re-mint."""

    storage_path: Path


@dataclass(frozen=True, slots=True)
class MasterTokenStatus:
    """Read-only sibling-token status staged for the shared auth-check app."""

    present: bool
    path: Path | None
    account: object | None
    unreadable_error_type: str | None


def bootstrap_login(
    plan: MasterTokenLoginPlan,
    *,
    oauth_token: str | None,
    browser: str,
    cdp_url: str | None,
    capture_oauth_token: OAuthTokenCapture,
    run_async: CoroutineRunner,
) -> MasterTokenLoginSuccess:
    """Run the advisory preflight, optional capture, and bootstrap transaction."""
    preflight: dict[str, Any] | None = None
    token: str | None = None
    operation: Coroutine[Any, Any, int] | None = None
    count: int | None = None
    result: MasterTokenLoginSuccess | None = None
    try:
        auth.assert_account_writable(
            email=plan.email,
            storage_path=plan.storage_path,
            force=plan.force,
        )
        preflight = auth.read_master_token(master_token_path_for(plan.storage_path))
        del preflight
        preflight = None

        token = oauth_token or capture_oauth_token(browser=browser, cdp_url=cdp_url)
        operation = auth.master_token_bootstrap(
            email=plan.email,
            oauth_token=token,
            storage_path=plan.storage_path,
            android_id=plan.android_id,
            force=plan.force,
        )
        try:
            count = run_async(operation)
        except BaseException:
            if inspect.getcoroutinestate(operation) is inspect.CORO_CREATED:
                operation.close()
            raise
        result = MasterTokenLoginSuccess(
            notebook_count=count,
            storage_path=plan.storage_path,
        )
        return result
    finally:
        del preflight
        del token
        del operation
        del count
        del result
        del oauth_token
        del cdp_url
        del capture_oauth_token
        del run_async
        del browser
        del plan


def remint_login(
    storage_path: Path,
    *,
    run_async: CoroutineRunner,
) -> MasterTokenRemintSuccess:
    """Run one forced re-mint and discard the returned credential jar."""
    operation: Coroutine[Any, Any, Any] | None = None
    jar: object | None = None
    result: MasterTokenRemintSuccess | None = None
    try:
        operation = auth.master_token_remint(storage_path)
        try:
            jar = run_async(operation)
        except BaseException:
            if inspect.getcoroutinestate(operation) is inspect.CORO_CREATED:
                operation.close()
            raise
        result = MasterTokenRemintSuccess(storage_path=storage_path)
        return result
    finally:
        del operation
        del jar
        del result
        del run_async
        del storage_path


async def bootstrap_missing_storage_from_master_token(storage_path: Path) -> bool:
    """Preserve the public cold-bootstrap awaitable and boolean projection."""
    return await auth.bootstrap_missing_storage_from_master_token(storage_path)


def inspect_master_token_status(
    storage_path: Path,
    *,
    has_env_auth: bool,
) -> MasterTokenStatus:
    """Project sibling-token presence without exposing its credential record."""
    path: Path | None = None
    record: dict[str, Any] | None = None
    account: object | None = None
    unreadable_error_type: str | None = None
    result: MasterTokenStatus | None = None
    try:
        if has_env_auth:
            result = MasterTokenStatus(False, None, None, None)
            return result

        path = master_token_path_for(storage_path)
        if not path.exists():
            result = MasterTokenStatus(False, path, None, None)
            return result

        try:
            record = auth.read_master_token(path)
        except Exception as exc:
            unreadable_error_type = type(exc).__name__
            result = MasterTokenStatus(True, path, None, unreadable_error_type)
            return result

        if record:
            account = record.get("email")
        result = MasterTokenStatus(True, path, account, None)
        return result
    finally:
        del record
        del account
        del unreadable_error_type
        del result
        del path
        del storage_path


__all__ = [
    "MasterTokenError",
    "MasterTokenLoginPlan",
    "MasterTokenLoginSuccess",
    "MasterTokenRemintSuccess",
    "MasterTokenStatus",
    "bootstrap_login",
    "bootstrap_missing_storage_from_master_token",
    "inspect_master_token_status",
    "remint_login",
]
