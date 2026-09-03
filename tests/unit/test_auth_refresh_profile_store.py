"""Behavior oracles for refresh's typed profile-store persistence boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from notebooklm._auth import cookies as auth_cookies
from notebooklm._auth import profile_store, refresh
from notebooklm._auth import recovery as recovery_mod
from notebooklm._auth import single_flight as single_flight_mod
from notebooklm._auth.cookie_types import Cookie, CookieIdentity, CookieJar
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._auth.profile_store import (
    CookieMergeDisposition,
    CookieMergeResult,
    ProfileStore,
)
from notebooklm._auth.storage import snapshot_cookie_jar
from tests._fixtures import platform_command


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


def _auth_payload(**extra: Any) -> dict[str, Any]:
    return _payload(
        _row("SID", "sid", same_site="Strict"),
        _row("__Secure-1PSIDTS", "psidts", expires=-1.0, same_site="Lax"),
        **extra,
    )


@pytest.mark.asyncio
async def test_paired_sample_preserves_order_duplicates_samesite_and_expiry_types(
    tmp_path: Path,
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

    pair = await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)
    baseline = pair.baseline
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
) -> None:
    storage = tmp_path / "storage.json"
    original = _auth_payload()
    original["cookies"].append(_row("SID", "unrelated", domain="example.com"))
    _write(storage, original)

    pair = await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)
    _set_live(pair.live, "SID", "rotated")
    result = await asyncio.to_thread(
        ProfileStore(storage).merge_cookie_observation,
        CookieJar.from_httpx(pair.live),
        baseline=pair.baseline,
    )
    assert result.disposition is CookieMergeDisposition.APPLIED

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
) -> None:
    storage = tmp_path / "storage.json"
    before = _write(storage, _auth_payload())
    pair = await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)
    result = await asyncio.to_thread(
        ProfileStore(storage).merge_cookie_observation,
        CookieJar.from_httpx(pair.live),
        baseline=pair.baseline,
    )
    assert result.disposition is CookieMergeDisposition.NO_CHANGE
    assert storage.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [True, False])
async def test_conflicts_preserve_sibling_and_apply_only_uncontested_delta(
    tmp_path: Path,
    partial: bool,
) -> None:
    storage = tmp_path / "storage.json"
    _write(storage, _auth_payload())

    pair = await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)
    _set_live(pair.live, "__Secure-1PSIDTS", "ours")
    if partial:
        _set_live(pair.live, "SID", "accepted")
    disk = _read(storage)
    next(row for row in disk["cookies"] if row["name"] == "__Secure-1PSIDTS")["value"] = "sibling"
    _write(storage, disk)
    result = await asyncio.to_thread(
        ProfileStore(storage).merge_cookie_observation,
        CookieJar.from_httpx(pair.live),
        baseline=pair.baseline,
    )
    assert result.disposition is CookieMergeDisposition.CONFLICT
    assert _value(storage, "__Secure-1PSIDTS") == "sibling"
    assert _value(storage, "SID") == ("accepted" if partial else "sid")


@pytest.mark.asyncio
async def test_missing_read_and_failed_commit_are_non_raising_and_preserve_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.json"
    _write(missing, _auth_payload())
    missing_pair = await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, missing)
    missing.unlink()
    missing_result = await asyncio.to_thread(
        ProfileStore(missing).merge_cookie_observation,
        CookieJar.from_httpx(missing_pair.live),
        baseline=missing_pair.baseline,
    )
    assert missing_result.disposition is CookieMergeDisposition.HARD_FAILURE
    assert not missing.exists()

    storage = tmp_path / "commit.json"
    before = _write(storage, _auth_payload())

    pair = await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)
    _set_live(pair.live, "SID", "rotated")

    def fail_commit(path: Path, payload: object) -> None:
        raise OSError("commit marker")

    monkeypatch.setattr(profile_store, "_commit_profile_json", fail_commit)
    commit_result = await asyncio.to_thread(
        ProfileStore(storage).merge_cookie_observation,
        CookieJar.from_httpx(pair.live),
        baseline=pair.baseline,
    )
    assert commit_result.disposition is CookieMergeDisposition.HARD_FAILURE
    assert storage.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["initial", "l2.5", "l3", "l4"])
async def test_public_merge_receives_exact_selected_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock,
    selected: str,
) -> None:
    """Each ladder stage's selected baseline reaches the real profile-store merge.

    Initial and L2.5 exercise the production public wrapper. L3 and L4 use a
    fresh coordinator with injected leaf owners so they do not mutate the
    process-global browser or mint adapters.
    """
    storage = tmp_path / "storage_state.json"

    def write_stage(value: str) -> None:
        _write(
            storage,
            _payload(
                _row("SID", value, same_site="Strict"),
                _row("__Secure-1PSIDTS", f"{value}-ts"),
            ),
        )

    write_stage("initial")
    seen_sids: list[str] = []
    if selected in {"initial", "l2.5"}:
        monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
        monkeypatch.delenv(refresh.NOTEBOOKLM_REFRESH_CMD_ENV, raising=False)

        def homepage(request: httpx.Request) -> httpx.Response:
            cookies = {
                part.strip().partition("=")[0]: part.strip().partition("=")[2]
                for part in request.headers.get("cookie", "").split(";")
                if "=" in part
            }
            sid = cookies.get("SID", "<missing>")
            seen_sids.append(sid)
            if selected == "l2.5" and sid == "initial":
                return httpx.Response(
                    302,
                    headers={"Location": "https://accounts.google.com/signin"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"Set-Cookie": "SID=observed; Domain=.google.com; Path=/"},
                content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
                request=request,
            )

        httpx_mock.add_callback(
            homepage,
            url=re.compile(r"^https://notebook\.google\.com/(?:\?.*)?$"),
            is_reusable=True,
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
            is_optional=True,
            is_reusable=True,
        )

        if selected == "l2.5":
            script = tmp_path / "refresh_profile.py"
            payload = json.dumps(
                _payload(_row("SID", selected), _row("__Secure-1PSIDTS", f"{selected}-ts")),
                indent=2,
            )
            script.write_text(
                "import os\n"
                "from pathlib import Path\n"
                f"Path(os.environ['NOTEBOOKLM_REFRESH_STORAGE_PATH']).write_text({payload!r}, "
                "encoding='utf-8')\n",
                encoding="utf-8",
            )
            monkeypatch.setenv(
                refresh.NOTEBOOKLM_REFRESH_CMD_ENV,
                platform_command([sys.executable, str(script)]),
            )

        result = await refresh.fetch_tokens_with_domains(storage)
    else:
        initial_pair = await asyncio.to_thread(
            auth_cookies._build_cookie_pair_from_storage, storage
        )
        stage_calls: list[str] = []

        async def forbidden_refresh(_path: Path):
            raise AssertionError("L2.5 is outside the injected L3/L4 owner scenario")

        async def run_headless(path: Path | None, allow_headless: bool):
            stage_calls.append("l3")
            assert path == storage
            if selected != "l3":
                return None
            assert allow_headless is True
            write_stage("l3")
            return await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)

        async def run_master(path: Path | None):
            stage_calls.append("l4")
            assert selected == "l4"
            assert path == storage
            write_stage("l4")
            return await asyncio.to_thread(auth_cookies._build_cookie_pair_from_storage, storage)

        async def validate_recovered(jar: httpx.Cookies) -> None:
            stage_calls.append("validate")
            seen_sids.append(jar.get("SID", domain=".google.com"))
            _set_live(jar, "SID", "observed")

        async def fetch_recovered(jar: httpx.Cookies) -> tuple[str, str]:
            stage_calls.append("fetch")
            seen_sids.append(jar.get("SID", domain=".google.com"))
            return "csrf", "session"

        coordinator = recovery_mod.ColdRecoveryCoordinator(
            state=recovery_mod.ColdRecoveryState(),
            single_flight=single_flight_mod.SingleFlight(),
            should_try_refresh=lambda _error, _env_auth: False,
            resolve_refresh_path=lambda _error: storage,
            run_refresh_attempt=forbidden_refresh,
            load_cookie_pair=auth_cookies._build_cookie_pair_from_storage,
            run_headless_attempt=run_headless,
            run_master_token_attempt=run_master,
            validate_recovered=validate_recovered,
            fetch_recovered=fetch_recovered,
            replace_cookie_jar=auth_cookies._replace_cookie_jar,
            snapshot_cookie_jar=snapshot_cookie_jar,
            clone_cookie_jar=auth_cookies._clone_cookie_jar,
        )
        recovered = await coordinator.recover(
            initial_error=_LoginRedirectError("Authentication expired or invalid."),
            cookie_jar=initial_pair.live,
            storage_path=storage,
            env_auth=False,
            allow_headless=selected == "l3",
            baseline=initial_pair.baseline,
        )
        selected_baseline = recovered[4]
        assert selected_baseline is not None
        assert (
            next(cookie.value for cookie in selected_baseline if cookie.name == "SID") == selected
        )
        await asyncio.to_thread(
            refresh._merge_domain_fetch_observation,
            ProfileStore(storage),
            CookieJar.from_httpx(initial_pair.live),
            selected_baseline,
        )
        result = recovered[:2]
        seen_sids.insert(0, "initial")
        assert stage_calls == (
            ["l3", "validate", "fetch"] if selected == "l3" else ["l3", "l4", "validate", "fetch"]
        )

    assert result == ("csrf", "session")
    assert seen_sids[0] == "initial"
    assert selected == "initial" or selected in seen_sids
    assert _value(storage, "SID") == "observed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authuser", "account_email", "expected"),
    [
        (0, None, {"url": "https://notebook.google.com/?authuser=0"}),
        (
            None,
            "explicit@example.com",
            {"url": "https://notebook.google.com/?authuser=explicit%40example.com"},
        ),
    ],
)
async def test_route_callback_preserves_raw_refresh_raw_timing_and_explicit_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock,
    authuser: int | None,
    account_email: str | None,
    expected: dict[str, str],
) -> None:
    raw = tmp_path / "raw.json"
    _write(raw, _auth_payload(account={"authuser": 3, "email": "raw@example.com"}))
    route_threads: list[int] = []
    event_loop_thread = threading.get_ident()
    real_to_thread = asyncio.to_thread

    async def record_route_thread(func, /, *args, **kwargs):
        if func is refresh._resolve_token_route_kwargs:

            def observed_call():
                route_threads.append(threading.get_ident())
                return func(*args, **kwargs)

            return await real_to_thread(observed_call)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", record_route_thread)
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    httpx_mock.add_response(
        url=expected["url"],
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
    )

    assert await refresh.fetch_tokens_with_domains(
        raw,
        profile="work",
        authuser=authuser,
        account_email=account_email,
    ) == ("csrf", "session")
    assert route_threads and all(thread_id != event_loop_thread for thread_id in route_threads)


@pytest.mark.asyncio
async def test_compatibility_route_callback_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock
) -> None:
    """The mapping-compatible refresh path also owns blocking profile reads."""
    storage = tmp_path / "storage_state.json"
    _write(storage, _auth_payload(account={"authuser": 4}))
    route_threads: list[int] = []
    event_loop_thread = threading.get_ident()
    real_to_thread = asyncio.to_thread

    async def record_route_dispatch(func, /, *args, **kwargs):
        if func is refresh._resolve_token_route_kwargs:

            def observed_call():
                route_threads.append(threading.get_ident())
                return func(*args, **kwargs)

            return await real_to_thread(observed_call)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", record_route_dispatch)
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    httpx_mock.add_response(
        url="https://notebook.google.com/?authuser=4",
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
    )

    assert await refresh._fetch_tokens_with_refresh(httpx.Cookies(), storage) == (
        "csrf",
        "session",
        False,
        None,
    )
    assert route_threads and all(thread_id != event_loop_thread for thread_id in route_threads)


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
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock,
) -> None:
    storage = tmp_path / "storage.json"
    _write(storage, _auth_payload())
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    captured_observation: list[CookieJar] = []

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

    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", paused_merge)
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    httpx_mock.add_response(
        url="https://notebook.google.com/",
        headers={"Set-Cookie": "SID=observed-before-dispatch; Domain=.google.com; Path=/"},
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
    )
    task = asyncio.create_task(refresh.fetch_tokens_with_domains(storage))
    assert await asyncio.to_thread(entered.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _write(storage, _auth_payload())
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert (
        next(cookie.value for cookie in captured_observation[0] if cookie.name == "SID")
        == "observed-before-dispatch"
    )


@pytest.mark.asyncio
async def test_inline_env_logs_exact_skip_and_performs_no_merge(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    httpx_mock,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(_auth_payload()))
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    httpx_mock.add_response(
        url="https://notebook.google.com/",
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
    )

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
    request = httpx_mock.get_request()
    assert request is not None
    assert "SID=sid" in request.headers.get("cookie", "")
