"""Direct behavior oracles for the operation-scoped account repair service."""

from __future__ import annotations

import asyncio
import inspect
import pickle
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm.auth as auth_facade
from notebooklm._auth import account, account_repair, account_types
from notebooklm._auth.profile_account import ProfileAccount

_CALLBACK_FIELDS = (
    "_writer_factory",
    "_load_cookie_jar",
    "_enumerate_accounts",
    "_select_account",
    "_extract_active_email",
    "_poke_session",
)


class _Writer:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events
        self.write_error: BaseException | None = None
        self.clear_error: BaseException | None = None

    def write(self, record: ProfileAccount) -> None:
        self.events.append(("write", record, threading.get_ident()))
        if self.write_error is not None:
            raise self.write_error

    def clear(self) -> None:
        self.events.append(("clear", threading.get_ident()))
        if self.clear_error is not None:
            raise self.clear_error


class _Scenario:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.writer = _Writer(self.events)
        self.account = account.Account(2, "alice@example.com", False)
        self.selected: account.Account | None = self.account
        self.reason: str | None = None
        self.load_error: BaseException | None = None
        self.enumerate_error: BaseException | None = None
        self.select_error: BaseException | None = None
        self.extract_error: BaseException | None = None
        self.pause = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.jar = httpx.Cookies({"SID": "secret"})

    def service(self) -> account_repair.AccountRepairService:
        return account_repair.AccountRepairService(
            writer_factory=self.writer_factory,
            load_cookie_jar=self.load_cookie_jar,
            enumerate_accounts=self.enumerate_accounts,
            select_account=self.select_account,
            extract_active_email=self.extract_active_email,
            poke_session=self.poke_session,
        )

    def writer_factory(self, path: Path) -> _Writer:
        self.events.append(("writer", path, threading.get_ident()))
        return self.writer

    def load_cookie_jar(self, path: Path) -> httpx.Cookies:
        self.events.append(("load", path, threading.get_ident()))
        if self.load_error is not None:
            raise self.load_error
        return self.jar

    async def enumerate_accounts(
        self,
        jar: httpx.Cookies,
        poke_session: account_repair.PokeSession,
    ) -> list[account.Account]:
        self.events.append(("enumerate", jar, poke_session, threading.get_ident()))
        if self.pause:
            self.started.set()
            await self.release.wait()
        if self.enumerate_error is not None:
            raise self.enumerate_error
        return [self.account]

    def select_account(
        self,
        accounts: list[account.Account],
        active_email: str | None,
    ) -> tuple[account.Account | None, str | None]:
        self.events.append(("select", accounts, active_email, threading.get_ident()))
        if self.select_error is not None:
            raise self.select_error
        return self.selected, self.reason

    def extract_active_email(self, html: str) -> str | None:
        self.events.append(("extract", html, threading.get_ident()))
        if self.extract_error is not None:
            raise self.extract_error
        return "alice@example.com"

    async def poke_session(self, _client: httpx.AsyncClient, _path: Path | None) -> None:
        raise AssertionError("the enumerator owns whether to invoke the poke callback")


def _assert_scrubbed(service: account_repair.AccountRepairService) -> None:
    assert service._claimed is True
    assert all(not hasattr(service, field) for field in _CALLBACK_FIELDS)
    assert set(vars(service)) == {"_claim_lock", "_claimed"}


@pytest.mark.asyncio
async def test_success_orders_callbacks_offloads_only_load_and_writes_typed_record() -> None:
    scenario = _Scenario()
    service = scenario.service()
    path = Path("/tmp/profile.json")
    loop_thread = threading.get_ident()

    result = await service.repair(path, page_html="<html>alice@example.com</html>")

    assert result == account.PlaywrightAccountRepairResult(
        written=True,
        email="alice@example.com",
    )
    assert [event[0] for event in scenario.events] == [
        "extract",
        "load",
        "enumerate",
        "select",
        "writer",
        "write",
    ]
    assert scenario.events[1][2] != loop_thread
    assert all(event[-1] == loop_thread for event in scenario.events if event[0] != "load")
    assert scenario.events[2][1] is scenario.jar
    assert scenario.events[2][2] == scenario.poke_session
    assert scenario.events[4][1] == path
    assert scenario.events[5][1] == ProfileAccount(2, "alice@example.com")
    _assert_scrubbed(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "no Google accounts were discovered",
        "multiple Google accounts were discovered",
        "active page email was not discovered",
    ],
)
async def test_each_ambiguity_clears_and_projects_the_reason(reason: str) -> None:
    scenario = _Scenario()
    scenario.selected = None
    scenario.reason = reason
    service = scenario.service()

    result = await service.repair(Path("/tmp/profile.json"))

    assert result == account.PlaywrightAccountRepairResult(
        written=False,
        ambiguity_reason=reason,
    )
    assert [event[0] for event in scenario.events] == [
        "load",
        "enumerate",
        "select",
        "writer",
        "clear",
    ]
    _assert_scrubbed(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("load", OSError("read failed")),
        ("enumerate", httpx.ConnectError("network failed")),
        ("select", ValueError("selection failed")),
        ("write", RuntimeError("write failed")),
    ],
)
async def test_handled_errors_clear_and_project_value(stage: str, error: BaseException) -> None:
    scenario = _Scenario()
    if stage == "load":
        scenario.load_error = error
    elif stage == "enumerate":
        scenario.enumerate_error = error
    elif stage == "select":
        scenario.select_error = error
    else:
        scenario.writer.write_error = error
    service = scenario.service()

    result = await service.repair(Path("/tmp/profile.json"))

    assert result == account.PlaywrightAccountRepairResult(written=False, error=str(error))
    assert [event[0] for event in scenario.events][-2:] == ["writer", "clear"]
    _assert_scrubbed(service)


@pytest.mark.asyncio
async def test_clear_error_is_logged_without_replacing_handled_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario = _Scenario()
    scenario.load_error = ValueError("bad cookies")
    scenario.writer.clear_error = OSError("clear failed")
    service = scenario.service()
    path = Path("/tmp/profile.json")

    with caplog.at_level("WARNING", logger="notebooklm.auth"):
        result = await service.repair(path)

    assert result.error == "bad cookies"
    assert [record.getMessage() for record in caplog.records] == [
        f"Failed to clear stale account metadata for {path}: clear failed"
    ]
    _assert_scrubbed(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [KeyError("unexpected"), asyncio.CancelledError()])
async def test_unexpected_error_and_cancellation_escape_without_clear(
    error: BaseException,
) -> None:
    scenario = _Scenario()
    scenario.enumerate_error = error
    service = scenario.service()

    with pytest.raises(type(error)) as raised:
        await service.repair(Path("/tmp/profile.json"))

    assert raised.value is error
    assert "clear" not in [event[0] for event in scenario.events]
    _assert_scrubbed(service)


@pytest.mark.asyncio
async def test_extractor_runs_before_handled_region_and_still_scrubs() -> None:
    scenario = _Scenario()
    error = ValueError("extractor failed")
    scenario.extract_error = error
    service = scenario.service()

    with pytest.raises(ValueError) as raised:
        await service.repair(Path("/tmp/profile.json"), page_html="html")

    assert raised.value is error
    assert [event[0] for event in scenario.events] == ["extract"]
    _assert_scrubbed(service)


@pytest.mark.asyncio
async def test_concurrent_and_post_completion_second_calls_fail_before_capability_lookup() -> None:
    scenario = _Scenario()
    scenario.pause = True
    service = scenario.service()
    first = asyncio.create_task(service.repair(Path("/tmp/profile.json")))
    await scenario.started.wait()

    with pytest.raises(RuntimeError, match="may only be called once"):
        await service.repair(Path("/tmp/other.json"))
    assert all(hasattr(service, field) for field in _CALLBACK_FIELDS)

    scenario.release.set()
    assert (await first).written is True
    _assert_scrubbed(service)
    with pytest.raises(RuntimeError, match="may only be called once"):
        await service.repair(Path("/tmp/after.json"))


def test_value_facade_pickle_and_runtime_signatures_are_historical() -> None:
    assert account.Account is account_types.Account is auth_facade.Account
    assert account.PlaywrightAccountRepairResult is account_types.PlaywrightAccountRepairResult
    assert not hasattr(auth_facade, "PlaywrightAccountRepairResult")
    assert account.Account.__module__ == "notebooklm._auth.account"
    assert account.PlaywrightAccountRepairResult.__module__ == "notebooklm._auth.account"
    value = account.PlaywrightAccountRepairResult(written=True, email="alice@example.com")
    assert pickle.loads(pickle.dumps(value)) == value
    assert list(inspect.signature(account_repair.AccountRepairService).parameters) == [
        "writer_factory",
        "load_cookie_jar",
        "enumerate_accounts",
        "select_account",
        "extract_active_email",
        "poke_session",
    ]
    assert list(inspect.signature(account_repair.AccountRepairService.repair).parameters) == [
        "self",
        "storage_path",
        "page_html",
    ]
