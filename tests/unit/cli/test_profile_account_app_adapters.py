"""Direct adapter tests for the Phase 13B coarse profile/account owner."""

from __future__ import annotations

import asyncio
import gc
import inspect
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import notebooklm.auth as auth_module
import notebooklm.cli.playwright_login_io as playwright_login_io
import notebooklm.cli.profile_cmd as profile_cmd
import notebooklm.cli.services.login.profile_targets as profile_targets
import notebooklm.cli.services.playwright_login as playwright_login
from notebooklm._app.profile import ProfileAccountObservation


class _LoginIO:
    def __init__(self) -> None:
        self.emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emitted.append((args, kwargs))

    def fail(self, code: int) -> None:
        raise SystemExit(code)

    def run_async(self, operation):
        return asyncio.run(operation)


def test_profile_cmd_passes_current_cli_path_collaborators(monkeypatch) -> None:
    list_profiles = MagicMock(name="list_profiles")
    resolve_profile = MagicMock(name="resolve_profile")
    get_storage_path = MagicMock(name="get_storage_path")
    observed: dict[str, object] = {}

    def _gather(**kwargs):
        observed.update(kwargs)
        return [], "default"

    response = MagicMock()
    monkeypatch.setattr(profile_cmd, "list_profiles", list_profiles)
    monkeypatch.setattr(profile_cmd, "resolve_profile", resolve_profile)
    monkeypatch.setattr(profile_cmd, "get_storage_path", get_storage_path)
    monkeypatch.setattr(profile_cmd, "gather_profile_list", _gather)
    monkeypatch.setattr(profile_cmd, "json_output_response", response)

    profile_cmd._run_list_cmd(json_output=True)

    assert observed == {
        "list_profiles": list_profiles,
        "resolve_profile": resolve_profile,
        "get_storage_path": get_storage_path,
    }
    response.assert_called_once_with({"profiles": [], "active": "default"})
    assert not hasattr(profile_cmd, "read_account_metadata")


def test_profile_target_wrappers_use_app_observations_and_preserve_signatures(
    monkeypatch,
) -> None:
    metadata = {
        "one": {"email": "Alice@Example.com"},
        "two": {"email": "alice@example.COM"},
        "base": {"email": "target@example.com"},
    }
    paths: list[str | None] = []

    def _path(profile: str | None = None) -> Path:
        paths.append(profile)
        return Path(str(profile))

    monkeypatch.setattr(profile_targets, "get_storage_path", _path)
    monkeypatch.setattr(
        auth_module,
        "read_account_metadata",
        lambda path: metadata.get(str(path), {}),
    )

    assert profile_targets._profiles_by_account_email(["one", "two"]) == {
        "alice@example.com": "one"
    }
    assert profile_targets._profile_account_email("base") == "target@example.com"
    assert (
        profile_targets._resolve_all_accounts_target(
            base_name="base",
            account_email="TARGET@example.com",
            existing_profiles={"base"},
            unavailable={"base"},
            claimed=set(),
            update=True,
        )
        == "base"
    )
    assert profile_targets._next_available_profile_name("base", {"base", "base-2"}) == "base-3"
    assert paths == ["one", "two", "base", "base"]
    assert str(inspect.signature(profile_targets._profile_account_email)) == (
        "(profile: 'str') -> 'str | None'"
    )


@pytest.mark.parametrize(
    ("public_result", "expected", "fragment"),
    [
        (
            SimpleNamespace(written=True, email="alice@example.com", error=None),
            True,
            "Account:[/green] alice@example.com",
        ),
        (
            SimpleNamespace(
                written=False,
                email=None,
                error=None,
                ambiguity_reason="multiple accounts",
            ),
            False,
            "multiple accounts",
        ),
        (
            SimpleNamespace(
                written=False,
                email=None,
                error="network down",
                ambiguity_reason=None,
            ),
            False,
            "Details: network down",
        ),
    ],
)
def test_repair_adapter_projects_public_result_and_rendering(
    monkeypatch, public_result: object, expected: bool, fragment: str
) -> None:
    calls: list[tuple[Path, str | None]] = []

    async def _repair(storage_path: Path, *, page_html: str | None = None):
        calls.append((storage_path, page_html))
        return public_result

    monkeypatch.setattr(auth_module, "repair_account_metadata_from_playwright_storage", _repair)
    io = _LoginIO()
    storage = Path("storage.json")

    assert (
        playwright_login.repair_playwright_account_metadata(
            storage,
            io,
            page_html="<html>active</html>",
            quiet=False,
        )
        is expected
    )
    assert calls == [(storage, "<html>active</html>")]
    assert "Identifying Google account" in str(io.emitted[0])
    assert fragment in str(io.emitted[1])


def test_repair_adapter_closes_created_coroutine_when_runner_rejects(monkeypatch) -> None:
    public_repair = MagicMock(side_effect=AssertionError("operation must not start"))
    monkeypatch.setattr(
        auth_module,
        "repair_account_metadata_from_playwright_storage",
        public_repair,
    )

    class _RejectingIO(_LoginIO):
        operation = None

        def run_async(self, operation):
            self.operation = operation
            raise RuntimeError("runner rejected")

    io = _RejectingIO()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert (
            playwright_login.repair_playwright_account_metadata(
                Path("storage.json"), io, page_html="<html>active</html>"
            )
            is False
        )
        gc.collect()

    assert inspect.getcoroutinestate(io.operation) == inspect.CORO_CLOSED
    assert not [warning for warning in caught if issubclass(warning.category, RuntimeWarning)]
    public_repair.assert_not_called()
    assert "runner rejected" in str(io.emitted[-1])


def test_repair_adapter_preserves_unlisted_error_and_scrubs_frame(monkeypatch) -> None:
    class StopRepair(BaseException):
        pass

    error = StopRepair("stop")

    async def _repair(storage_path: Path, *, page_html: str | None = None):
        raise error

    monkeypatch.setattr(auth_module, "repair_account_metadata_from_playwright_storage", _repair)
    io = _LoginIO()

    with pytest.raises(StopRepair) as raised:
        playwright_login.repair_playwright_account_metadata(
            Path("storage.json"), io, page_html="<html>active</html>"
        )

    assert raised.value is error
    frames = []
    traceback = raised.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "repair_playwright_account_metadata":
            frames.append(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert len(frames) == 2
    sensitive_locals = {
        "storage_path",
        "io",
        "page_html",
        "request",
        "operation",
        "result",
    }
    assert all(not sensitive_locals & set(frame) for frame in frames)


def test_repair_adapter_preserves_cancellation_identity(monkeypatch) -> None:
    error = asyncio.CancelledError("cancel")

    async def _repair(storage_path: Path, *, page_html: str | None = None):
        raise error

    monkeypatch.setattr(auth_module, "repair_account_metadata_from_playwright_storage", _repair)

    with pytest.raises(asyncio.CancelledError) as raised:
        playwright_login.repair_playwright_account_metadata(
            Path("storage.json"), _LoginIO(), page_html="<html>active</html>"
        )

    if sys.version_info >= (3, 11):
        assert raised.value is error
    else:
        # Python 3.10's task machinery reconstructs cancellation at the
        # synchronous runner boundary and drops the original arguments.
        assert raised.value.args == ()


@pytest.mark.parametrize(
    ("exists", "valid", "repair_calls"), [(False, False, 0), (True, True, 0), (True, False, 1)]
)
def test_refresh_stored_session_observes_only_existing_storage(
    monkeypatch, exists: bool, valid: bool, repair_calls: int
) -> None:
    storage = MagicMock(spec=Path)
    storage.exists.return_value = exists
    observation = MagicMock(
        return_value=ProfileAccountObservation(None, None, metadata_valid=valid)
    )
    repair = MagicMock(return_value=False)

    async def _bootstrap(_storage_path: Path) -> bool:
        return False

    async def _fetch(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        playwright_login_io,
        "bootstrap_missing_storage_from_master_token",
        _bootstrap,
    )
    monkeypatch.setattr(playwright_login_io, "observe_profile_account", observation)
    monkeypatch.setattr(playwright_login_io, "repair_after_refresh", repair)

    playwright_login_io.refresh_stored_session(
        storage,
        "work",
        allow_headless=False,
        quiet=True,
        verify=False,
        json_output=False,
        fetch_tokens=_fetch,
    )

    assert observation.call_count == int(exists)
    if exists:
        observation.assert_called_once_with(storage)
    assert repair.call_count == repair_calls
    if repair_calls:
        repair.assert_called_once_with(storage, quiet=True)
