"""Direct behavior oracles for the operation-scoped cold recovery coordinator."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth import recovery, refresh
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.cookies import _clone_cookie_jar, _LoadedCookiePair
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._auth.storage import CookieSnapshot
from notebooklm._env import get_base_url

_CALLBACK_FIELDS = (
    "_should_try_refresh",
    "_resolve_refresh_path",
    "_run_refresh_attempt",
    "_load_cookie_pair",
    "_run_headless_attempt",
    "_run_master_token_attempt",
    "_validate_recovered",
    "_fetch_recovered",
    "_replace_cookie_jar",
    "_snapshot_cookie_jar",
    "_clone_cookie_jar",
)
_TRACE_NAMES = {
    "_fetch_tokens_with_refresh",
    "_fetch_tokens_with_exact_baseline",
    "_fetch_tokens_with_refresh_core",
    "_cold_fallbacks",
    "recover",
    "run_refresh_attempt",
}
_LOGIN_MESSAGE = "Authentication expired or invalid. Redirected to Google login."


def _jar(sid: str) -> httpx.Cookies:
    jar = httpx.Cookies()
    jar.set("SID", sid, domain=".google.com")
    return jar


def _sid(jar: httpx.Cookies) -> str | None:
    return jar.get("SID", domain=".google.com")


def _write_storage(path: Path, *, sid: str = "stale") -> None:
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": sid, "domain": ".google.com"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": f"{sid}-ts",
                        "domain": ".google.com",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def _result(sid: str = "recovered") -> recovery.ColdRecoveryResult:
    return recovery.ColdRecoveryResult(_jar(sid), {}, CookieJar())


def _assert_spent(coordinator: recovery.ColdRecoveryCoordinator) -> None:
    assert coordinator._used is True
    assert all(not hasattr(coordinator, field) for field in _CALLBACK_FIELDS)


def _trace_projection(error: BaseException) -> list[str]:
    names: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        name = traceback.tb_frame.f_code.co_name
        if name in _TRACE_NAMES:
            names.append(name)
        traceback = traceback.tb_next
    return names


async def _recover(
    coordinator: recovery.ColdRecoveryCoordinator,
    initial_error: ValueError,
    *,
    cookie_jar: httpx.Cookies | None = None,
    storage_path: Path | None = None,
    env_auth: bool = False,
    allow_headless: bool = False,
    baseline: CookieJar | None = None,
) -> tuple[str, str, bool, CookieSnapshot | None, CookieJar | None]:
    try:
        raise initial_error
    except ValueError as active_error:
        return await coordinator.recover(
            initial_error=active_error,
            cookie_jar=cookie_jar if cookie_jar is not None else httpx.Cookies(),
            storage_path=storage_path,
            env_auth=env_auth,
            allow_headless=allow_headless,
            baseline=baseline,
        )


class _Scenario:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.eligible = False
        self.refresh_path = Path("/refresh.json")
        self.refresh_result: tuple[str, str, CookieSnapshot, CookieJar | None] = (
            "refresh-csrf",
            "refresh-session",
            {},
            CookieJar(),
        )
        self.refresh_error: BaseException | None = None
        self.resolve_error: BaseException | None = None
        self.cold_result = _result()
        self.cold_error: BaseException | None = None
        self.validate_error: BaseException | None = None
        self.fetch_result = ("cold-csrf", "cold-session")
        self.fetch_error: BaseException | None = None
        self.replace_error: BaseException | None = None
        self.pause_stage: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.caller_jar: httpx.Cookies | None = None
        self.initial_error: Exception | None = None

    def coordinator(self) -> recovery.ColdRecoveryCoordinator:
        return recovery.ColdRecoveryCoordinator(
            state=recovery.ColdRecoveryState(),
            single_flight=recovery._single_flight.SingleFlight(),
            should_try_refresh=self.should_try_refresh,
            resolve_refresh_path=self.resolve_refresh_path,
            run_refresh_attempt=self.run_refresh_attempt,
            load_cookie_pair=self.load_cookie_pair,
            run_headless_attempt=self.run_headless_attempt,
            run_master_token_attempt=self.run_master_token_attempt,
            validate_recovered=self.validate_recovered,
            fetch_recovered=self.fetch_recovered,
            replace_cookie_jar=self.replace_cookie_jar,
            snapshot_cookie_jar=self.snapshot_cookie_jar,
            clone_cookie_jar=_clone_cookie_jar,
        )

    async def _pause(self, stage: str) -> None:
        if self.pause_stage == stage:
            self.started.set()
            await self.release.wait()

    def should_try_refresh(self, error: Exception, env_auth: bool) -> bool:
        self.initial_error = error
        self.events.append(("should", error, env_auth))
        return self.eligible and not env_auth

    def resolve_refresh_path(self, error: ValueError) -> Path:
        self.events.append(("resolve", error))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.refresh_path

    async def run_refresh_attempt(
        self, path: Path
    ) -> tuple[str, str, CookieSnapshot, CookieJar | None]:
        self.events.append(("refresh", path))
        await self._pause("refresh-before-replace")
        if self.pause_stage == "refresh-after-replace":
            assert self.caller_jar is not None
            self.caller_jar.set("SID", "refresh-mutated", domain=".google.com")
            self.started.set()
            await self.release.wait()
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refresh_result

    def load_cookie_pair(self, _path: Path) -> _LoadedCookiePair:
        return _LoadedCookiePair(_jar("disk"), CookieJar())

    async def run_headless_attempt(
        self,
        path: Path | None,
        allow_headless: bool,
    ) -> _LoadedCookiePair | None:
        assert isinstance(self.initial_error, _LoginRedirectError)
        self.events.append(
            ("cold", path, allow_headless, self.initial_error, self.validate_recovered)
        )
        await self._pause("cold-before-replace")
        if self.cold_error is not None:
            raise self.cold_error
        return _LoadedCookiePair(self.cold_result.cookie_jar, self.cold_result.baseline)

    async def run_master_token_attempt(self, _path: Path | None) -> _LoadedCookiePair | None:
        return None

    def snapshot_cookie_jar(self, jar: httpx.Cookies) -> CookieSnapshot:
        if jar is self.cold_result.cookie_jar:
            return self.cold_result.snapshot
        return {}

    async def validate_recovered(self, jar: httpx.Cookies) -> None:
        self.events.append(("validate-route", jar))
        if self.validate_error is not None:
            raise self.validate_error

    async def fetch_recovered(self, jar: httpx.Cookies) -> tuple[str, str]:
        self.events.append(("final-route", jar))
        await self._pause("cold-after-replace")
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_result

    def replace_cookie_jar(self, target: httpx.Cookies, source: httpx.Cookies) -> None:
        self.events.append(("replace", target, source))
        if self.replace_error is not None:
            raise self.replace_error
        target.jar.clear()
        for cookie in source.jar:
            target.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)


@pytest.mark.asyncio
async def test_refresh_success_is_ordered_returns_same_sample_and_scrubs() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    snapshot: CookieSnapshot = {}
    baseline = CookieJar()
    scenario.refresh_result = ("csrf", "session", snapshot, baseline)
    coordinator = scenario.coordinator()
    initial = ValueError("Authentication expired")

    result = await _recover(coordinator, initial, env_auth=False)

    assert result == ("csrf", "session", True, snapshot, baseline)
    assert result[3] is snapshot
    assert result[4] is baseline
    assert scenario.events == [
        ("should", initial, False),
        ("resolve", initial),
        ("refresh", scenario.refresh_path),
    ]
    _assert_spent(coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, OSError, ValueError])
async def test_resolver_allowed_errors_escape_without_refresh_or_cold_and_scrub(
    error_type: type[Exception],
) -> None:
    scenario = _Scenario()
    scenario.eligible = True
    resolver_error = error_type("resolver failed")
    scenario.resolve_error = resolver_error
    coordinator = scenario.coordinator()

    with pytest.raises(error_type) as raised:
        await _recover(coordinator, ValueError("Authentication expired"))

    assert raised.value is resolver_error
    assert [event[0] for event in scenario.events] == ["should", "resolve"]
    _assert_spent(coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, OSError, ValueError])
async def test_refresh_errors_are_retained_by_identity(error_type: type[Exception]) -> None:
    scenario = _Scenario()
    scenario.eligible = True
    refresh_error = error_type("refresh failed")
    scenario.refresh_error = refresh_error
    coordinator = scenario.coordinator()

    with pytest.raises(error_type) as raised:
        await _recover(coordinator, ValueError("Authentication expired"))

    assert raised.value is refresh_error
    assert [event[0] for event in scenario.events] == ["should", "resolve", "refresh"]
    _assert_spent(coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected", [KeyError("unexpected"), asyncio.CancelledError()])
async def test_refresh_unexpected_and_cancellation_escape_without_cold(
    unexpected: BaseException,
) -> None:
    scenario = _Scenario()
    scenario.eligible = True
    scenario.refresh_error = unexpected
    coordinator = scenario.coordinator()

    with pytest.raises(type(unexpected)) as raised:
        await _recover(coordinator, ValueError("Authentication expired"))

    assert raised.value is unexpected
    assert all(event[0] != "cold" for event in scenario.events)
    _assert_spent(coordinator)


@pytest.mark.asyncio
async def test_typed_cold_success_wins_over_retained_refresh_error() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    scenario.refresh_error = RuntimeError("refresh failed")
    snapshot: CookieSnapshot = {}
    baseline = CookieJar()
    scenario.cold_result = recovery.ColdRecoveryResult(_jar("fresh"), snapshot, baseline)
    coordinator = scenario.coordinator()
    caller = _jar("stale")
    initial = _LoginRedirectError(_LOGIN_MESSAGE)
    storage = Path("/original-storage.json")

    result = await _recover(
        coordinator,
        initial,
        cookie_jar=caller,
        storage_path=storage,
        allow_headless=True,
    )

    assert result == ("cold-csrf", "cold-session", True, snapshot, baseline)
    assert _sid(caller) == "fresh"
    assert [event[0] for event in scenario.events] == [
        "should",
        "resolve",
        "refresh",
        "cold",
        "validate-route",
        "replace",
        "final-route",
    ]
    cold_event = scenario.events[3]
    assert cold_event[1:4] == (storage.resolve(), True, initial)
    assert cold_event[4] == scenario.validate_recovered
    assert scenario.events[4][1] is scenario.cold_result.cookie_jar
    assert scenario.events[6][1] is caller
    _assert_spent(coordinator)


@pytest.mark.asyncio
async def test_plain_error_and_derived_refresh_path_never_enter_cold() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    refresh_error = RuntimeError("refresh failed")
    scenario.refresh_error = refresh_error
    coordinator = scenario.coordinator()

    with pytest.raises(RuntimeError) as raised:
        await _recover(coordinator, ValueError("Authentication expired"), storage_path=None)

    assert raised.value is refresh_error
    assert [event[0] for event in scenario.events] == ["should", "resolve", "refresh"]
    _assert_spent(coordinator)


@pytest.mark.asyncio
async def test_environment_auth_skips_refresh_but_keeps_typed_cold_gate() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    coordinator = scenario.coordinator()
    storage = Path("/storage.json")

    result = await _recover(
        coordinator,
        _LoginRedirectError(_LOGIN_MESSAGE),
        storage_path=storage,
        env_auth=True,
    )

    assert result[:3] == ("cold-csrf", "cold-session", True)
    assert [event[0] for event in scenario.events] == [
        "should",
        "cold",
        "validate-route",
        "replace",
        "final-route",
    ]
    _assert_spent(coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_refresh", [False, True])
async def test_typed_exhaustion_selects_original_or_saved_error(saved_refresh: bool) -> None:
    scenario = _Scenario()
    scenario.eligible = saved_refresh
    refresh_error = RuntimeError("refresh failed")
    scenario.refresh_error = refresh_error
    scenario.cold_error = _LoginRedirectError("cold exhausted")
    coordinator = scenario.coordinator()
    initial = _LoginRedirectError(_LOGIN_MESSAGE)

    expected: BaseException = refresh_error if saved_refresh else initial
    with pytest.raises(type(expected)) as raised:
        await _recover(coordinator, initial, storage_path=Path("/storage.json"))

    assert raised.value is expected
    _assert_spent(coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["cold", "final"])
async def test_non_typed_cold_or_final_value_error_propagates_itself(stage: str) -> None:
    scenario = _Scenario()
    propagated = ValueError(f"{stage} non-login failure")
    if stage == "cold":
        scenario.cold_error = propagated
    else:
        scenario.fetch_error = propagated
    coordinator = scenario.coordinator()
    caller = _jar("stale")

    with pytest.raises(ValueError) as raised:
        await _recover(
            coordinator,
            _LoginRedirectError(_LOGIN_MESSAGE),
            cookie_jar=caller,
            storage_path=Path("/storage.json"),
        )

    assert raised.value is propagated
    assert _sid(caller) == ("stale" if stage == "cold" else "recovered")
    _assert_spent(coordinator)


@pytest.mark.asyncio
async def test_error_identity_cause_context_and_suppression_are_preserved() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    cause = LookupError("refresh cause")
    refresh_error = RuntimeError("refresh failed")
    try:
        raise refresh_error from cause
    except RuntimeError:
        pass
    scenario.refresh_error = refresh_error
    coordinator = scenario.coordinator()

    with pytest.raises(RuntimeError) as raised:
        await _recover(coordinator, ValueError("Authentication expired"))

    assert raised.value is refresh_error
    assert raised.value.__cause__ is cause
    assert raised.value.__suppress_context__ is True
    assert raised.value.__context__ is not None
    _assert_spent(coordinator)


@pytest.mark.asyncio
async def test_paused_winner_rejects_concurrent_and_post_completion_calls() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    scenario.pause_stage = "refresh-before-replace"
    coordinator = scenario.coordinator()
    first = asyncio.create_task(_recover(coordinator, ValueError("Authentication expired")))
    await scenario.started.wait()

    with pytest.raises(RuntimeError, match=r"recover\(\) is one-shot"):
        await _recover(coordinator, ValueError("Authentication expired again"))

    assert all(hasattr(coordinator, field) for field in _CALLBACK_FIELDS)
    assert [event[0] for event in scenario.events].count("refresh") == 1
    scenario.release.set()
    assert (await first)[:3] == ("refresh-csrf", "refresh-session", True)
    _assert_spent(coordinator)

    with pytest.raises(RuntimeError, match=r"recover\(\) is one-shot"):
        await _recover(coordinator, ValueError("Authentication expired after completion"))
    assert [event[0] for event in scenario.events].count("refresh") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_sid"),
    [
        ("refresh-before-replace", "stale"),
        ("refresh-after-replace", "refresh-mutated"),
        ("cold-before-replace", "stale"),
        ("cold-after-replace", "recovered"),
    ],
)
async def test_cancellation_preserves_exact_before_after_replacement_mutation(
    stage: str,
    expected_sid: str,
) -> None:
    scenario = _Scenario()
    scenario.pause_stage = stage
    scenario.eligible = stage.startswith("refresh")
    caller = _jar("stale")
    scenario.caller_jar = caller
    coordinator = scenario.coordinator()
    initial: ValueError = (
        ValueError("Authentication expired")
        if stage.startswith("refresh")
        else _LoginRedirectError(_LOGIN_MESSAGE)
    )
    storage = None if stage.startswith("refresh") else Path("/storage.json")
    task = asyncio.create_task(
        _recover(coordinator, initial, cookie_jar=caller, storage_path=storage)
    )
    await scenario.started.wait()

    task.cancel()
    scenario.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _sid(caller) == expected_sid
    _assert_spent(coordinator)


@pytest.mark.asyncio
async def test_cancelled_owner_scrubs_all_callbacks() -> None:
    scenario = _Scenario()
    scenario.eligible = True
    scenario.pause_stage = "refresh-before-replace"
    coordinator = scenario.coordinator()
    task = asyncio.create_task(_recover(coordinator, ValueError("Authentication expired")))
    await scenario.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_spent(coordinator)


def _stub_login_redirect(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        status_code=302,
        url=f"{get_base_url()}/",
        headers={"Location": "https://accounts.google.com/signin"},
    )
    httpx_mock.add_response(
        status_code=200,
        url="https://accounts.google.com/signin",
        content=b"<html>Login</html>",
    )


async def _empty_route(_path: Path | None) -> dict[str, Any]:
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["public", "exact-baseline"])
async def test_original_error_traceback_projection_is_exact(
    wrapper: str,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, raising=False)
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    _stub_login_redirect(httpx_mock)

    with pytest.raises(_LoginRedirectError) as raised:
        if wrapper == "public":
            await refresh._fetch_tokens_with_refresh(httpx.Cookies())
        else:
            await refresh._fetch_tokens_with_exact_baseline(
                httpx.Cookies(),
                None,
                None,
                initial_baseline=CookieJar(),
                resolve_route=_empty_route,
                allow_headless=False,
                env_auth=False,
            )

    outer = (
        "_fetch_tokens_with_refresh" if wrapper == "public" else "_fetch_tokens_with_exact_baseline"
    )
    assert _trace_projection(raised.value) == [
        outer,
        "_fetch_tokens_with_refresh_core",
        "_cold_fallbacks",
        "_fetch_tokens_with_refresh_core",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["public", "exact-baseline"])
async def test_retained_refresh_error_traceback_projection_is_exact(
    wrapper: str,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    _write_storage(storage)
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    _stub_login_redirect(httpx_mock)

    if wrapper == "public":

        async def fail_refresh(_path: Path, _profile: str | None) -> None:
            raise RuntimeError("refresh failed")

        deps = refresh.RefreshCmdDeps(run_refresh_cmd=fail_refresh)
        monkeypatch.setenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "injected-refresh")
    else:
        deps = None
        monkeypatch.setenv(
            refresh.NOTEBOOKLM_REFRESH_CMD_ENV,
            "__notebooklm_missing_refresh_command_phase12a__",
        )

    with pytest.raises(RuntimeError) as raised:
        if wrapper == "public":
            await refresh._fetch_tokens_with_refresh(httpx.Cookies(), storage, deps=deps)
        else:
            await refresh._fetch_tokens_with_exact_baseline(
                httpx.Cookies(),
                storage,
                None,
                initial_baseline=CookieJar(),
                resolve_route=_empty_route,
                allow_headless=False,
                env_auth=False,
            )

    outer = (
        "_fetch_tokens_with_refresh" if wrapper == "public" else "_fetch_tokens_with_exact_baseline"
    )
    assert _trace_projection(raised.value) == [
        outer,
        "_fetch_tokens_with_refresh_core",
        "_cold_fallbacks",
        "recover",
        "recover",
        "run_refresh_attempt",
    ]
    assert "No active exception to reraise" not in str(raised.value)


async def _call_cold_fallbacks(
    error: ValueError,
    *,
    storage_path: Path | None,
    env_auth: bool,
    deps: refresh.RefreshCmdDeps,
) -> tuple[str, str, bool, CookieSnapshot | None, CookieJar | None]:
    async def load_replacement(
        _path: Path,
    ) -> tuple[httpx.Cookies, CookieSnapshot, CookieJar | None]:
        raise AssertionError("replacement load should not run")

    try:
        raise error
    except ValueError as active_error:
        return await refresh._cold_fallbacks(
            active_error,
            httpx.Cookies(),
            storage_path,
            None,
            env_auth=env_auth,
            allow_headless=False,
            resolve_route=_empty_route,
            load_replacement=load_replacement,
            baseline=None,
            deps=deps,
        )


@pytest.mark.asyncio
async def test_environment_skip_log_is_exact(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    initial = ValueError("Authentication expired")
    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth"),
        pytest.raises(ValueError) as raised,
    ):
        await _call_cold_fallbacks(
            initial,
            storage_path=None,
            env_auth=True,
            deps=refresh.RefreshCmdDeps(),
        )

    assert raised.value is initial
    records = [record for record in caplog.records if "env auth has no file" in record.getMessage()]
    assert [(record.name, record.levelno, record.getMessage()) for record in records] == [
        (
            "notebooklm.auth",
            logging.DEBUG,
            "Skipping NOTEBOOKLM_REFRESH_CMD: env auth has no file",
        )
    ]


@pytest.mark.asyncio
async def test_start_and_failure_logs_are_exact_and_ordered(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    initial = ValueError("Authentication expired")
    refresh_error = RuntimeError("refresh failed")

    async def fail_refresh(_path: Path, _profile: str | None) -> None:
        raise refresh_error

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm.auth"),
        pytest.raises(RuntimeError) as raised,
    ):
        await _call_cold_fallbacks(
            initial,
            storage_path=tmp_path / "storage.json",
            env_auth=False,
            deps=refresh.RefreshCmdDeps(run_refresh_cmd=fail_refresh),
        )

    assert raised.value is refresh_error
    records = [
        record
        for record in caplog.records
        if record.getMessage()
        in {
            "NotebookLM auth failed (Authentication expired). Running "
            "NOTEBOOKLM_REFRESH_CMD to refresh cookies.",
            "NOTEBOOKLM_REFRESH_CMD rung failed (refresh failed); falling through to the "
            "re-mint rungs.",
        }
    ]
    assert [(record.name, record.levelno, record.getMessage()) for record in records] == [
        (
            "notebooklm.auth",
            logging.WARNING,
            "NotebookLM auth failed (Authentication expired). Running "
            "NOTEBOOKLM_REFRESH_CMD to refresh cookies.",
        ),
        (
            "notebooklm.auth",
            logging.WARNING,
            "NOTEBOOKLM_REFRESH_CMD rung failed (refresh failed); falling through to the "
            "re-mint rungs.",
        ),
    ]


@pytest.mark.asyncio
async def test_resolver_error_logs_only_start_and_never_runs_refresh(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    initial = ValueError("Authentication expired")
    resolver_error = OSError("cannot resolve")
    runs = 0

    class BrokenPath(type(Path())):
        def expanduser(self) -> Path:
            raise resolver_error

    async def count_refresh(_path: Path, _profile: str | None) -> None:
        nonlocal runs
        runs += 1

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm.auth"),
        pytest.raises(OSError) as raised,
    ):
        await _call_cold_fallbacks(
            initial,
            storage_path=BrokenPath("broken.json"),
            env_auth=False,
            deps=refresh.RefreshCmdDeps(run_refresh_cmd=count_refresh),
        )

    assert raised.value is resolver_error
    assert runs == 0
    records = [record for record in caplog.records if record.name == "notebooklm.auth"]
    assert [(record.levelno, record.getMessage()) for record in records] == [
        (
            logging.WARNING,
            "NotebookLM auth failed (Authentication expired). Running "
            "NOTEBOOKLM_REFRESH_CMD to refresh cookies.",
        )
    ]
