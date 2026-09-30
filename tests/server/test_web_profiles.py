"""Multi-profile Web routing, session-conflict refusal, and serialized opens."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from notebooklm import paths
from notebooklm._app import web_profiles
from notebooklm._app.web_profiles import web_session_keys
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import AuthError
from notebooklm.server import app as app_module
from notebooklm.server._context import AppState, ProfileRegistry
from notebooklm.server.app import create_app
from notebooklm.types import Notebook

from .conftest import TEST_TOKEN
from .fakes import FakeClient

HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
WEB_AUTH_KEYS = {
    "backend",
    "profile",
    "storage_exists",
    "json_valid",
    "cookies_present",
    "sid_cookie",
    "master_token_present",
    "session_conflict",
    "authenticated",
    "ready",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    for name in (
        "NOTEBOOKLM_BACKEND",
        "NOTEBOOKLM_AUTH_JSON",
        "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL",
        "NOTEBOOKLM_SERVER_PROFILE_STARTUP_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


def _cookie(name: str, value: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": ".google.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def write_session(name: str, psid: str, *, sid: str | None = None) -> Path:
    # Distinct sessions carry distinct SIDs; SID identifies a session, not an account.
    sid = sid if sid is not None else f"sid-{psid}"
    path = paths.get_storage_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = [
        _cookie("SID", sid),
        _cookie("__Secure-1PSID", psid),
        _cookie("__Secure-1PSIDTS", f"ts-{psid}"),
    ]
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


def write_master_token(name: str) -> None:
    token = paths.get_storage_path(name).with_name("master_token.json")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(
        json.dumps(
            {
                "version": 1,
                "email": "same@example.com",
                "android_id": "1234567890123456",
                "master_token": "aas_et/copied-master-token",
            }
        ),
        encoding="utf-8",
    )
    token.chmod(0o600)


def web_app(names: tuple[str, ...] = ("work", "personal"), **kwargs: Any) -> Any:
    return create_app(profiles=list(names), backend="web", **kwargs)


def select(name: str) -> dict[str, str]:
    return {"X-NotebookLM-Profile": name}


@asynccontextmanager
async def fake_factory(name: str) -> Any:
    client = FakeClient()
    client.notebooks_store["same-id"] = Notebook(id="same-id", title=name)
    yield client


class OpenSeam:
    """Stand-in for the one network-touching open inside ``WebProfileSet``."""

    def __init__(self, delay: float = 0.0) -> None:
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.active = 0
        self.max_active = 0
        self.delay = delay

    def __call__(self, path: Path, profile: str, keepalive: float | None) -> Any:
        @asynccontextmanager
        async def open_client() -> Any:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                self.opened.append(profile)
                client = FakeClient()
                client.notebooks_store["same-id"] = Notebook(id="same-id", title=profile)
                client.auth = SimpleNamespace(storage_path=path)  # type: ignore[attr-defined]
            finally:
                self.active -= 1
            try:
                yield client
            finally:
                self.closed.append(profile)

        return open_client()


@pytest.fixture
def open_seam(monkeypatch: pytest.MonkeyPatch) -> OpenSeam:
    seam = OpenSeam()
    monkeypatch.setattr(web_profiles, "_open_web_client", seam)
    return seam


@pytest.fixture
def no_bootstrap(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    async def bootstrap(path: Path) -> bool:
        calls.append(path)
        pytest.fail("bootstrap must not run for this profile")

    monkeypatch.setattr(web_profiles, "bootstrap_missing_storage_from_master_token", bootstrap)
    return calls


# --- routing, selection, and recovery carry over unchanged -------------------


def test_web_multi_profile_routes_by_header() -> None:
    app = web_app(profile_client_factory=fake_factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal", "work"):
            response = client.get("/v1/notebooks/same-id", headers=select(name))
            assert response.status_code == 200
            assert response.json()["title"] == name
            assert response.headers["X-NotebookLM-Profile"] == name
            assert response.headers["Cache-Control"] == "no-store"
            assert "X-NotebookLM-Profile" in response.headers["Vary"]
        registry = app.state.notebooklm
        assert isinstance(registry, ProfileRegistry)
        assert all(state.backend == "web" for state in registry.profiles.values())


@pytest.mark.parametrize(
    ("selection", "status", "code"),
    [
        ([], 400, "profile_required"),
        ([("X-NotebookLM-Profile", "missing")], 404, "unknown_profile"),
        ([("X-NotebookLM-Profile", "work,personal")], 400, "invalid_profile"),
    ],
)
def test_web_selection_errors(selection: Any, status: int, code: str) -> None:
    app = web_app(profile_client_factory=fake_factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for route in ("/v1/notebooks", "/v1/server/info"):
            response = client.get(route, headers=selection)
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
        assert client.get("/healthz").json() == {"ok": True}


def test_web_unavailable_profile_503_then_recovers_after_cooldown() -> None:
    attempts: Counter[str] = Counter()
    repaired = False

    @asynccontextmanager
    async def factory(name: str) -> Any:
        attempts[name] += 1
        if name == "work" and not repaired:
            raise AuthError("unavailable")
        yield FakeClient()

    app = web_app(profile_client_factory=factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        response = client.get("/v1/notebooks", headers=select("work"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "profile_unavailable"
        assert response.json()["error"]["message"] == "Selected Web profile is unavailable"
        assert client.get("/v1/notebooks", headers=select("personal")).status_code == 200
        # Cooldown: an immediate retry shares the failure without a new attempt.
        assert client.get("/v1/notebooks", headers=select("work")).status_code == 503
        assert attempts == {"work": 2, "personal": 1}
        repaired = True
        with pytest.MonkeyPatch.context() as patch:
            original = app_module.time.monotonic
            patch.setattr(
                app_module,
                "time",
                type("Clock", (), {"monotonic": staticmethod(lambda: original() + 10)}),
            )
            assert client.get("/v1/notebooks", headers=select("work")).status_code == 200
    assert attempts == {"work": 3, "personal": 1}


# --- copied cookie sessions are refused per profile --------------------------


def test_copied_sessions_are_refused_while_sibling_serves(
    open_seam: OpenSeam, no_bootstrap: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    write_session("work", "copied-psid-secret")
    write_session("personal", "copied-psid-secret")
    write_session("third", "distinct-psid-secret")
    keys = web_session_keys(paths.get_storage_path("work"))
    assert keys
    app = web_app(("work", "personal", "third"))
    bodies: list[str] = []
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client,
    ):
        for name in ("work", "personal"):
            response = client.get("/v1/notebooks", headers=select(name))
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "profile_unavailable"
            bodies.append(response.text)
            info = client.get("/v1/server/info", headers=select(name))
            bodies.append(info.text)
            auth = info.json()["auth"]
            assert set(auth) >= WEB_AUTH_KEYS
            assert auth["backend"] == "web"
            assert auth["profile"] == name
            assert auth["session_conflict"] is True
            assert auth["ready"] is False
            assert auth["authenticated"] is False
            assert auth["startup_error"]["code"] == "session_conflict"
            account = client.get(
                "/v1/server/info", params={"include_account": "true"}, headers=select(name)
            )
            bodies.append(account.text)
            assert account.json()["account"]["available"] is False
        third = client.get("/v1/notebooks", headers=select("third"))
        assert third.status_code == 200
        info = client.get("/v1/server/info", headers=select("third")).json()["auth"]
        assert info["session_conflict"] is False
        assert info["ready"] is True
        assert "startup_error" not in info
        healthz = client.get("/healthz")
        assert healthz.json() == {"ok": True}
        bodies.append(healthz.text)
    # The open seam never ran for the refused profiles.
    assert open_seam.opened == ["third"]
    assert no_bootstrap == []
    assert "shares a Web session with configured profile(s) 'work'" in caplog.text
    for text in [*bodies, caplog.text]:
        assert "copied-psid-secret" not in text
        assert "distinct-psid-secret" not in text
        assert all(key not in text for key in keys)


def test_copy_made_after_serving_is_advisory_for_the_serving_profile(
    open_seam: OpenSeam, no_bootstrap: list[Path]
) -> None:
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    with TestClient(web_app(), headers=HEADERS, client=("127.0.0.1", 1)) as client:
        assert client.get("/v1/notebooks", headers=select("work")).status_code == 200
        write_session("personal", "psid-work")  # a copy appears after work serves
        auth = client.get("/v1/server/info", headers=select("work")).json()["auth"]
        assert auth["session_conflict"] is True  # current files: reopening would refuse
        assert auth["ready"] is True
        assert auth["authenticated"] is True  # still serving its own session
        assert "startup_error" not in auth


def test_startup_error_code_reflects_the_recorded_failure(
    open_seam: OpenSeam, no_bootstrap: list[Path]
) -> None:
    paths.get_storage_path("work").parent.mkdir(parents=True, exist_ok=True)
    paths.get_storage_path("work").write_text("not JSON", encoding="utf-8")
    write_session("personal", "psid-personal")
    with TestClient(web_app(), headers=HEADERS, client=("127.0.0.1", 1)) as client:
        write_session("work", "psid-personal")  # a conflicting copy appears later
        auth = client.get("/v1/server/info", headers=select("work")).json()["auth"]
        assert auth["session_conflict"] is True
        assert auth["ready"] is False
        # The open failed on unreadable storage, not on a shared session.
        assert auth["startup_error"]["code"] == "profile_unavailable"


def test_same_account_with_distinct_sessions_both_serve(
    open_seam: OpenSeam, no_bootstrap: list[Path]
) -> None:
    # Same account (one master token), separately minted sessions.
    for name, psid in (("work", "psid-a"), ("personal", "psid-b")):
        write_session(name, psid)
        write_master_token(name)
    app = web_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal"):
            response = client.get("/v1/notebooks/same-id", headers=select(name))
            assert response.status_code == 200
            assert response.json()["title"] == name
            auth = client.get("/v1/server/info", headers=select(name)).json()["auth"]
            assert auth["session_conflict"] is False
            assert auth["master_token_present"] is True
    assert sorted(open_seam.opened) == ["personal", "work"]


def test_bootstrap_runs_once_per_profile_one_at_a_time(
    open_seam: OpenSeam, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: Counter[str] = Counter()
    active = 0
    peak = 0

    async def bootstrap(path: Path) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            name = path.parent.name
            calls[name] += 1
            write_session(name, f"minted-{name}")
            return True
        finally:
            active -= 1

    monkeypatch.setattr(web_profiles, "bootstrap_missing_storage_from_master_token", bootstrap)
    names = ("work", "personal", "third")
    for name in names:
        write_master_token(name)  # Copies of one master token are allowed.
    app = web_app(names)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in names:
            assert client.get("/v1/notebooks", headers=select(name)).status_code == 200
    assert calls == dict.fromkeys(names, 1)
    assert peak == 1
    assert sorted(open_seam.opened) == sorted(names)


@pytest.mark.parametrize(
    "breakage",
    ["malformed_json", "not_object", "no_session_cookie", "nothing", "bad_master_token"],
)
def test_unusable_files_make_only_that_profile_unavailable(
    open_seam: OpenSeam, no_bootstrap: list[Path], breakage: str
) -> None:
    storage = paths.get_storage_path("work")
    storage.parent.mkdir(parents=True, exist_ok=True)
    if breakage == "malformed_json":
        storage.write_text("not JSON", encoding="utf-8")
    elif breakage == "not_object":
        storage.write_text("[]", encoding="utf-8")
    elif breakage == "no_session_cookie":
        storage.write_text(json.dumps({"cookies": [_cookie("HSID", "hsid")]}), encoding="utf-8")
    elif breakage == "bad_master_token":
        storage.with_name("master_token.json").write_text("not JSON", encoding="utf-8")
    write_session("personal", "psid-personal")
    app = web_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        response = client.get("/v1/notebooks", headers=select("work"))
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "Selected Web profile is unavailable"
        auth = client.get("/v1/server/info", headers=select("work")).json()["auth"]
        assert auth["ready"] is False
        assert auth["session_conflict"] is False
        assert auth["startup_error"]["code"] == "profile_unavailable"
        assert client.get("/v1/notebooks", headers=select("personal")).status_code == 200
    assert open_seam.opened == ["personal"]


async def test_opens_never_overlap_and_waiting_is_not_startup_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each open takes 100 ms against a 250 ms deadline: serialized, the last of
    # four profiles waits ~300 ms for its turn yet still succeeds.
    monkeypatch.setenv("NOTEBOOKLM_SERVER_PROFILE_STARTUP_TIMEOUT", "0.25")
    seam = OpenSeam(delay=0.1)
    monkeypatch.setattr(web_profiles, "_open_web_client", seam)
    names = ("one", "two", "three", "four")
    for name in names:
        write_session(name, f"psid-{name}")
    app = web_app(names)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.1", 1)),
            base_url="http://127.0.0.1",
            headers=HEADERS,
        ) as client,
    ):
        states = app.state.notebooklm.profiles
        assert all(state.client is not None for state in states.values())
        responses = await asyncio.gather(
            *(client.get("/v1/notebooks", headers=select(name)) for name in names)
        )
        assert [response.status_code for response in responses] == [200] * len(names)
    assert seam.max_active == 1
    assert sorted(seam.opened) == sorted(names)
    assert sorted(seam.closed) == sorted(names)


async def test_injected_web_factories_also_take_turns() -> None:
    active = 0
    peak = 0

    @asynccontextmanager
    async def factory(name: str) -> Any:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        yield FakeClient()

    app = web_app(("one", "two", "three"), profile_client_factory=factory)
    async with app.router.lifespan_context(app):
        assert all(s.client is not None for s in app.state.notebooklm.profiles.values())
    assert peak == 1


def test_from_storage_receives_explicit_path_and_profile(
    monkeypatch: pytest.MonkeyPatch, no_bootstrap: list[Path]
) -> None:
    calls: list[dict[str, Any]] = []

    def from_storage(**kwargs: Any) -> Any:
        calls.append(kwargs)

        @asynccontextmanager
        async def opened() -> Any:
            client = FakeClient()
            client.auth = SimpleNamespace(storage_path=Path(kwargs["path"]))  # type: ignore[attr-defined]
            yield client

        return opened()

    monkeypatch.setattr(NotebookLMClient, "from_storage", from_storage)
    for name, psid in (("work", "psid-work"), ("personal", "psid-personal")):
        write_session(name, psid)
    app = web_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal"):
            assert client.get("/v1/notebooks", headers=select(name)).status_code == 200
    by_profile = {call["profile"]: call for call in calls}
    assert set(by_profile) == {"work", "personal"}
    for name, call in by_profile.items():
        assert call["path"] == str(paths.get_storage_path(name).resolve())
        backend = call["config"].backend
        assert type(backend).__name__ == "WebBackendConfig"
        assert backend.session.keepalive_interval == app_module.DEFAULT_SERVER_KEEPALIVE_INTERVAL


def test_client_bound_to_other_storage_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_bootstrap: list[Path]
) -> None:
    closed: list[str] = []

    def open_client(path: Path, profile: str, keepalive: float | None) -> Any:
        @asynccontextmanager
        async def opened() -> Any:
            client = FakeClient()
            foreign = tmp_path / "foreign.json" if profile == "work" else path
            client.auth = SimpleNamespace(storage_path=foreign)  # type: ignore[attr-defined]
            try:
                yield client
            finally:
                closed.append(profile)

        return opened()

    monkeypatch.setattr(web_profiles, "_open_web_client", open_client)
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    app = web_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        assert client.get("/v1/notebooks", headers=select("work")).status_code == 503
        assert client.get("/v1/notebooks", headers=select("personal")).status_code == 200
        # Startup and the first request's retry each closed the mismatched
        # client immediately instead of publishing it.
        assert closed == ["work", "work"]
    assert Counter(closed) == {"work": 2, "personal": 1}


def test_healthy_profile_diagnostics_and_account(
    open_seam: OpenSeam, no_bootstrap: list[Path]
) -> None:
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    app = web_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        info = client.get(
            "/v1/server/info", params={"include_account": "true"}, headers=select("work")
        ).json()
        assert set(info["auth"]) == WEB_AUTH_KEYS
        assert info["auth"]["profile"] == "work"
        assert info["auth"]["storage_exists"] is True
        assert info["auth"]["ready"] is True
        assert info["auth"]["session_conflict"] is False
        assert info["auth"]["master_token_present"] is False
        assert "available" in info["account"]


# --- environment refusals are Web-only ---------------------------------------


@pytest.mark.parametrize(
    ("env", "value", "match"),
    [
        ("NOTEBOOKLM_AUTH_JSON", "", "refuses NOTEBOOKLM_AUTH_JSON"),
        ("NOTEBOOKLM_AUTH_JSON", '{"cookies": []}', "refuses NOTEBOOKLM_AUTH_JSON"),
        (
            "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL",
            "http://127.0.0.1:9222",
            "refuses NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL",
        ),
    ],
)
def test_web_multi_profile_refuses_process_wide_auth(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str, match: str
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError, match=match):
        web_app()
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
    with pytest.raises(ValueError, match=match):
        create_app(profiles=["work", "personal"])
    # Android multi-profile and single-profile Web are unaffected.
    create_app(profiles=["work", "personal"], backend="android")
    create_app(profiles=["work"], backend="web")


def test_blank_cdp_url_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL", "  ")
    web_app()


def test_refusal_is_repeated_at_open(
    monkeypatch: pytest.MonkeyPatch, open_seam: OpenSeam, no_bootstrap: list[Path]
) -> None:
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    app = web_app()
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal"):
            assert client.get("/v1/notebooks", headers=select(name)).status_code == 503
    assert open_seam.opened == []


def test_unknown_backend_is_refused_for_multi_profile() -> None:
    with pytest.raises(ValueError, match="backend='web' or 'android'"):
        create_app(profiles=["work", "personal"], backend="auto")  # type: ignore[arg-type]


def test_single_entry_profile_list_uses_default_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str | None, str | None]] = []

    def default_factory(profile: str | None = None, backend: str | None = None) -> Any:
        seen.append((profile, backend))
        return fake_factory(profile or "")

    def no_web_profiles(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("single-profile mode must not build a WebProfileSet")

    monkeypatch.setattr(app_module, "_default_factory", default_factory)
    monkeypatch.setattr(app_module, "WebProfileSet", no_web_profiles)
    # Single-profile behavior is unchanged, including inline auth acceptance.
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    app = create_app(profiles=["work"], backend="web")
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        assert client.get("/v1/notebooks").status_code == 200
        assert client.get("/v1/notebooks", headers=select("ignored")).status_code == 200
        state = app.state.notebooklm
        assert isinstance(state, AppState)
        assert state.isolated is False
        assert state.web_profiles is None
    assert seen == [("work", "web")]


def test_startup_log_names_the_web_backend(caplog: pytest.LogCaptureFixture) -> None:
    @asynccontextmanager
    async def factory(name: str) -> Any:
        if name == "work":
            raise RuntimeError("boom")
        yield FakeClient()

    app = web_app(profile_client_factory=factory)
    with caplog.at_level(logging.WARNING), TestClient(app, headers=HEADERS):
        pass
    assert "Web profile work is unavailable at startup" in caplog.text
    assert "Android" not in caplog.text


def test_web_recovery_cooldown_uses_monotonic_clock(
    open_seam: OpenSeam, no_bootstrap: list[Path]
) -> None:
    write_session("work", "copied")
    write_session("personal", "copied")
    app = web_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        assert client.get("/v1/notebooks", headers=select("work")).status_code == 503
        # Operator fixes the copy by logging "personal" in separately.
        write_session("personal", "fresh")
        now = time.monotonic()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: now + 6)
            for name in ("work", "personal"):
                assert client.get("/v1/notebooks", headers=select(name)).status_code == 200
    assert sorted(open_seam.opened) == ["personal", "work"]


async def test_cancelled_startup_settles_profiles_waiting_for_their_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_PROFILE_STARTUP_TIMEOUT", "30")
    holding = asyncio.Event()
    entered: list[str] = []
    closed: list[str] = []

    @asynccontextmanager
    async def factory(name: str) -> Any:
        entered.append(name)
        try:
            holding.set()
            await asyncio.Event().wait()  # Hold the turn until cancelled.
            yield FakeClient()
        finally:
            closed.append(name)

    app = web_app(("one", "two", "three"), profile_client_factory=factory)

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pytest.fail("startup should be cancelled")

    task = asyncio.create_task(start())
    await asyncio.wait_for(holding.wait(), 2)
    await asyncio.sleep(0.02)
    assert len(entered) == 1  # The others are queued for their turn.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert closed == entered
