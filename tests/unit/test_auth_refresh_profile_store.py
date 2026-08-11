"""Behavior oracles for refresh's typed profile-store persistence boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from notebooklm._auth import profile_store, refresh
from notebooklm._auth.cookie_types import Cookie, CookieIdentity, CookieJar
from notebooklm._auth.profile_store import (
    CookieMergeDisposition,
    CookieMergeResult,
    ProfileStore,
)


def _cookie(
    value: str,
    *,
    name: str = "SID",
    domain: str = ".google.com",
    path: str = "/",
    same_site: str | None = None,
) -> Cookie:
    return Cookie(
        name=name,
        domain=domain,
        path=path,
        value=value,
        same_site=same_site,
    )


def _row(
    name: str,
    value: str,
    *,
    domain: str = ".google.com",
    path: str = "/",
    expires: int | float = -1,
    same_site: str = "Lax",
) -> dict[str, object]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": expires,
        "httpOnly": True,
        "secure": True,
        "sameSite": same_site,
    }


def _payload(*rows: dict[str, object], account: dict[str, object] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cookies": list(rows),
        "origins": [{"origin": "https://example.invalid", "localStorage": []}],
        "unknown": {"preserve": [1, 2, 3]},
    }
    if account is not None:
        payload["notebooklm"] = {"version": 1, "account": account}
    return payload


def _write(path: Path, payload: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path.read_bytes()


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _value(path: Path, name: str) -> str | None:
    for row in _read(path)["cookies"]:
        if row.get("name") == name and row.get("domain") == ".google.com":
            return row.get("value")
    return None


def _set_live(live: httpx.Cookies, name: str, value: str) -> None:
    for cookie in live.jar:
        if cookie.name == name and cookie.domain == ".google.com":
            cookie.value = value
            return
    raise AssertionError(f"missing live cookie {name}")


class _StoreStub:
    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[CookieJar, CookieJar]] = []

    def merge_cookie_observation(
        self,
        observation: CookieJar,
        *,
        baseline: CookieJar,
    ) -> object:
        self.calls.append((observation, baseline))
        if self.error is not None:
            raise self.error
        return self.result


def _completed_result(
    disposition: CookieMergeDisposition,
    *,
    committed: bool,
) -> CookieMergeResult:
    rejected = (
        frozenset({CookieIdentity("SID", ".google.com", "/")})
        if disposition is CookieMergeDisposition.CONFLICT
        else frozenset()
    )
    return CookieMergeResult(
        disposition,
        advances_ordering=True,
        committed=committed,
        next_baseline=CookieJar((_cookie(disposition.value),)),
        rejected=rejected,
    )


@pytest.mark.parametrize(
    ("result", "case"),
    [
        (_completed_result(CookieMergeDisposition.APPLIED, committed=True), "applied"),
        (_completed_result(CookieMergeDisposition.NO_CHANGE, committed=False), "no-change"),
        (_completed_result(CookieMergeDisposition.CONFLICT, committed=True), "partial-conflict"),
        (_completed_result(CookieMergeDisposition.CONFLICT, committed=False), "all-conflict"),
    ],
)
def test_helper_returns_exact_advancing_baseline(
    result: CookieMergeResult,
    case: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    observation = CookieJar((_cookie(f"observation-{case}"),))
    baseline = CookieJar((_cookie(f"baseline-{case}"),))
    store = _StoreStub(result)

    returned = refresh._merge_domain_fetch_observation(store, observation, baseline)  # type: ignore[arg-type]

    assert returned is result.next_baseline
    assert store.calls == [(observation, baseline)]
    assert not caplog.records
    assert "observation-" not in repr(returned)


@pytest.mark.parametrize("committed", [False, None])
def test_helper_hard_failure_returns_exact_input_baseline(committed: bool | None) -> None:
    result = CookieMergeResult(
        CookieMergeDisposition.HARD_FAILURE,
        advances_ordering=False,
        committed=committed,
    )
    observation = CookieJar((_cookie("observation"),))
    baseline = CookieJar((_cookie("baseline"),))
    store = _StoreStub(result)

    returned = refresh._merge_domain_fetch_observation(store, observation, baseline)  # type: ignore[arg-type]

    assert returned is baseline
    assert store.calls == [(observation, baseline)]


def test_helper_asserts_impossible_advancing_result_and_propagates_errors() -> None:
    observation = CookieJar()
    baseline = CookieJar()
    impossible = _StoreStub(SimpleNamespace(advances_ordering=True, next_baseline=None))
    with pytest.raises(AssertionError, match="advancing refresh merge must provide a baseline"):
        refresh._merge_domain_fetch_observation(impossible, observation, baseline)  # type: ignore[arg-type]

    marker = LookupError("typed merge marker")
    failing = _StoreStub(error=marker)
    with pytest.raises(LookupError) as raised:
        refresh._merge_domain_fetch_observation(failing, observation, baseline)  # type: ignore[arg-type]
    assert raised.value is marker
    assert failing.calls == [(observation, baseline)]


ExactHandler = Callable[[dict[str, Any]], Awaitable[tuple[str, str, CookieJar]]]


class _ExactFetchHarness:
    def __init__(self) -> None:
        self.handler: ExactHandler | None = None
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        live: httpx.Cookies,
        storage_path: Path | None,
        profile: str | None,
        *,
        initial_baseline: CookieJar,
        resolve_route: Callable[[Path | None], Awaitable[dict[str, Any]]],
        allow_headless: bool,
        env_auth: bool,
    ) -> tuple[str, str, CookieJar]:
        call = {
            "live": live,
            "storage_path": storage_path,
            "profile": profile,
            "initial_baseline": initial_baseline,
            "resolve_route": resolve_route,
            "allow_headless": allow_headless,
            "env_auth": env_auth,
        }
        self.calls.append(call)
        if self.handler is None:
            return "csrf", "session", initial_baseline
        return await self.handler(call)


@pytest.fixture
def exact_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> _ExactFetchHarness:
    harness = _ExactFetchHarness()
    monkeypatch.setattr(refresh, "_fetch_tokens_with_exact_baseline", harness)
    return harness


def _auth_payload(**extra: Any) -> dict[str, Any]:
    return _payload(
        _row("SID", "sid", same_site="Strict"),
        _row("__Secure-1PSIDTS", "psidts", expires=-1.0, same_site="Lax"),
        **extra,
    )


@pytest.mark.asyncio
async def test_paired_sample_preserves_order_duplicates_samesite_and_expiry_types(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
) -> None:
    storage = tmp_path / "storage.json"
    _write(
        storage,
        _payload(
            _row("SID", "first", expires=-1, same_site="Strict"),
            _row("SID", "duplicate", expires=99, same_site="None"),
            _row("__Secure-1PSIDTS", "psidts", expires=-1.0, same_site="Lax"),
            _row("SID", "path-sibling", path="/u/1", expires=7),
        ),
    )

    assert await refresh.fetch_tokens_with_domains(storage) == ("csrf", "session")

    baseline = exact_fetch.calls[0]["initial_baseline"]
    assert [(cookie.name, cookie.path, cookie.value) for cookie in baseline] == [
        ("SID", "/", "first"),
        ("__Secure-1PSIDTS", "/", "psidts"),
        ("SID", "/u/1", "path-sibling"),
    ]
    assert [cookie.same_site for cookie in baseline] == ["Strict", "Lax", "Lax"]
    assert tuple(baseline)[0].expires is None
    assert tuple(baseline)[1].expires == -1
    assert type(tuple(baseline)[1].expires) is int


@pytest.mark.asyncio
async def test_applied_merge_preserves_samesite_and_unrelated_raw_state(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
) -> None:
    storage = tmp_path / "storage.json"
    original = _auth_payload()
    original["cookies"].append(_row("SID", "unrelated", domain="example.com"))
    _write(storage, original)

    async def mutate(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        _set_live(call["live"], "SID", "rotated")
        return "csrf-applied", "session-applied", call["initial_baseline"]

    exact_fetch.handler = mutate
    assert await refresh.fetch_tokens_with_domains(storage) == (
        "csrf-applied",
        "session-applied",
    )

    after = _read(storage)
    sid = next(
        row for row in after["cookies"] if row["domain"] == ".google.com" and row["name"] == "SID"
    )
    assert sid["value"] == "rotated"
    assert sid["sameSite"] == "Strict"
    assert next(row for row in after["cookies"] if row["domain"] == "example.com")["value"] == (
        "unrelated"
    )
    assert after["origins"] == original["origins"]
    assert after["unknown"] == original["unknown"]


@pytest.mark.asyncio
async def test_no_change_merge_preserves_exact_bytes(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
) -> None:
    storage = tmp_path / "storage.json"
    before = _write(storage, _auth_payload())
    assert await refresh.fetch_tokens_with_domains(storage) == ("csrf", "session")
    assert storage.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [True, False])
async def test_conflicts_preserve_sibling_and_apply_only_uncontested_delta(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
    partial: bool,
) -> None:
    storage = tmp_path / "storage.json"
    _write(storage, _auth_payload())

    async def race(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        _set_live(call["live"], "__Secure-1PSIDTS", "ours")
        if partial:
            _set_live(call["live"], "SID", "accepted")
        disk = _read(storage)
        next(row for row in disk["cookies"] if row["name"] == "__Secure-1PSIDTS")["value"] = (
            "sibling"
        )
        _write(storage, disk)
        return "csrf-conflict", "session-conflict", call["initial_baseline"]

    exact_fetch.handler = race
    assert await refresh.fetch_tokens_with_domains(storage) == (
        "csrf-conflict",
        "session-conflict",
    )
    assert _value(storage, "__Secure-1PSIDTS") == "sibling"
    assert _value(storage, "SID") == ("accepted" if partial else "sid")


@pytest.mark.asyncio
async def test_missing_read_and_failed_commit_are_non_raising_and_preserve_bytes(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.json"
    _write(missing, _auth_payload())

    async def remove(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        missing.unlink()
        return "csrf-missing", "session-missing", call["initial_baseline"]

    exact_fetch.handler = remove
    assert await refresh.fetch_tokens_with_domains(missing) == (
        "csrf-missing",
        "session-missing",
    )
    assert not missing.exists()

    storage = tmp_path / "commit.json"
    before = _write(storage, _auth_payload())

    async def mutate(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        _set_live(call["live"], "SID", "rotated")
        return "csrf-commit", "session-commit", call["initial_baseline"]

    exact_fetch.handler = mutate

    def fail_commit(path: Path, payload: object) -> None:
        raise OSError("commit marker")

    monkeypatch.setattr(profile_store, "_commit_profile_json", fail_commit)
    assert await refresh.fetch_tokens_with_domains(storage) == (
        "csrf-commit",
        "session-commit",
    )
    assert storage.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["initial", "l2.5", "l3", "l4"])
async def test_public_merge_receives_exact_selected_baseline(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
    monkeypatch: pytest.MonkeyPatch,
    selected: str,
) -> None:
    storage = tmp_path / "storage.json"
    _write(storage, _auth_payload())
    marker = CookieJar((_cookie(selected),))
    seen: list[CookieJar] = []

    async def select(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        return "csrf", "session", marker

    def capture(self, observation, *, baseline, recovery_observation=None):
        seen.append(baseline)
        return CookieMergeResult(
            CookieMergeDisposition.NO_CHANGE,
            advances_ordering=True,
            committed=False,
            next_baseline=baseline,
        )

    exact_fetch.handler = select
    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", capture)

    assert await refresh.fetch_tokens_with_domains(storage) == ("csrf", "session")
    assert seen == [marker]
    assert seen[0] is marker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authuser", "account_email", "expected"),
    [
        (0, None, {"authuser": 0, "force_authuser_query": True}),
        (None, "explicit@example.com", {"authuser": 3, "account_email": "explicit@example.com"}),
    ],
)
async def test_route_callback_preserves_raw_refresh_raw_timing_and_explicit_precedence(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
    authuser: int | None,
    account_email: str | None,
    expected: dict[str, Any],
) -> None:
    raw = tmp_path / "raw.json"
    refresh_path = tmp_path / "refresh.json"
    _write(raw, _auth_payload(account={"authuser": 3, "email": "raw@example.com"}))
    _write(refresh_path, _auth_payload(account={"authuser": 7, "email": "refresh@example.com"}))
    routes: list[dict[str, Any]] = []

    async def inspect_routes(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        resolver = call["resolve_route"]
        routes.extend(
            [
                await resolver(raw),
                await resolver(refresh_path),
                await resolver(raw),
            ]
        )
        return "csrf", "session", call["initial_baseline"]

    exact_fetch.handler = inspect_routes
    assert await refresh.fetch_tokens_with_domains(
        raw,
        profile="work",
        authuser=authuser,
        account_email=account_email,
    ) == ("csrf", "session")
    assert exact_fetch.calls[0]["storage_path"] == raw
    assert exact_fetch.calls[0]["profile"] == "work"
    assert routes[0] == expected
    assert routes[2] == expected
    if authuser is None:
        assert routes[1] == {"authuser": 7, "account_email": "explicit@example.com"}
    else:
        assert routes[1] == expected


class _CountingStore(ProfileStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.cookie_reads = 0
        self.document_reads = 0

    def _read_cookie_document(self):
        self.cookie_reads += 1
        return super()._read_cookie_document()

    def read_document(self):
        self.document_reads += 1
        return super().read_document()


@pytest.mark.parametrize("exists", [True, False])
def test_merge_invokes_one_cookie_read_and_at_most_one_document_decode(
    tmp_path: Path,
    exists: bool,
) -> None:
    storage = tmp_path / "storage.json"
    if exists:
        _write(storage, _auth_payload())
    store = _CountingStore(storage)
    baseline = CookieJar((_cookie("sid"), _cookie("psidts", name="__Secure-1PSIDTS")))

    returned = refresh._merge_domain_fetch_observation(store, baseline, baseline)

    assert store.cookie_reads == 1
    assert store.document_reads == (1 if exists else 0)
    assert returned is baseline if not exists else returned is not None


@pytest.mark.asyncio
async def test_cancellation_propagates_before_worker_release_and_observation_is_immutable(
    tmp_path: Path,
    exact_fetch: _ExactFetchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage.json"
    _write(storage, _auth_payload())
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    captured_live: list[httpx.Cookies] = []
    captured_observation: list[CookieJar] = []

    async def select(call: dict[str, Any]) -> tuple[str, str, CookieJar]:
        captured_live.append(call["live"])
        return "csrf", "session", call["initial_baseline"]

    def paused_merge(self, observation, *, baseline, recovery_observation=None):
        captured_observation.append(observation)
        entered.set()
        try:
            assert release.wait(timeout=5)
            return CookieMergeResult(
                CookieMergeDisposition.NO_CHANGE,
                advances_ordering=True,
                committed=False,
                next_baseline=baseline,
            )
        finally:
            finished.set()

    exact_fetch.handler = select
    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", paused_merge)
    task = asyncio.create_task(refresh.fetch_tokens_with_domains(storage))
    assert await asyncio.to_thread(entered.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _set_live(captured_live[0], "SID", "mutated-after-dispatch")
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert next(cookie.value for cookie in captured_observation[0] if cookie.name == "SID") == "sid"


@pytest.mark.asyncio
async def test_inline_env_logs_exact_skip_and_performs_no_merge(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exact_fetch: _ExactFetchHarness,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(_auth_payload()))

    def forbidden(self, observation, *, baseline, recovery_observation=None):
        raise AssertionError("inline env auth must not construct or call a store")

    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", forbidden)
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert await refresh.fetch_tokens_with_domains(None, profile="work") == (
            "csrf",
            "session",
        )

    messages = [record.getMessage() for record in caplog.records]
    assert (
        messages.count("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var") == 1
    )
    assert exact_fetch.calls[0]["storage_path"] is None
    assert exact_fetch.calls[0]["env_auth"] is True
