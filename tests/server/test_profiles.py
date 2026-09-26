"""Multi-profile Android routing, recovery, and credential isolation."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from notebooklm import paths
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.profile_store import ProfileStore
from notebooklm.exceptions import AuthError
from notebooklm.server._context import ProfileRegistry
from notebooklm.server.app import create_app
from notebooklm.types import Notebook

from .conftest import TEST_TOKEN
from .fakes import FakeClient

HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_BACKEND", raising=False)


def profile_app(**kwargs: Any) -> Any:
    return create_app(profiles=["work", "personal"], backend="android", **kwargs)


@asynccontextmanager
async def fake_factory(name: str) -> Any:
    client = FakeClient()
    client.notebooks_store["same-id"] = Notebook(id="same-id", title=name)
    yield client


def test_profile_routing_and_response_cache_policy() -> None:
    app = profile_app(profile_client_factory=fake_factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal", "work"):
            response = client.get("/v1/notebooks/same-id", headers={"X-NotebookLM-Profile": name})
            assert response.status_code == 200
            assert response.json()["title"] == name
            assert response.headers["X-NotebookLM-Profile"] == name
            assert response.headers["Cache-Control"] == "no-store"
            assert "X-NotebookLM-Profile" in response.headers["Vary"]
        # A mutation of an identical resource ID cannot affect the other client.
        client.patch(
            "/v1/notebooks/same-id",
            json={"title": "changed"},
            headers={"X-NotebookLM-Profile": "work"},
        )

        assert (
            client.get(
                "/v1/notebooks/same-id", headers={"X-NotebookLM-Profile": "personal"}
            ).json()["title"]
            == "personal"
        )


def test_mounted_routes_keep_profile_headers_and_body_limits() -> None:
    app = profile_app(profile_client_factory=fake_factory)
    with TestClient(app, root_path="/proxy", headers=HEADERS, client=("127.0.0.1", 1)) as client:
        headers = {"X-NotebookLM-Profile": "work"}
        response = client.get("/proxy/v1/notebooks", headers=headers)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Vary"] == "X-NotebookLM-Profile"
        assert response.headers["X-NotebookLM-Profile"] == "work"
        response = client.post(
            "/proxy/v1/notebooks",
            content=b"{}",
            headers={**headers, "Content-Type": "application/json", "Content-Length": "999999"},
        )
        assert response.status_code == 413


def test_unexpected_error_keeps_profile_cache_headers() -> None:
    @asynccontextmanager
    async def factory(name: str) -> Any:
        client = FakeClient()

        async def broken() -> Any:
            raise RuntimeError("secret internal detail")

        client.notebooks.list = broken  # type: ignore[method-assign]
        yield client

    app = profile_app(profile_client_factory=factory)
    with TestClient(
        app, headers=HEADERS, client=("127.0.0.1", 1), raise_server_exceptions=False
    ) as client:
        response = client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "work"})
        assert response.status_code == 500
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Vary"] == "X-NotebookLM-Profile"
        assert response.headers["X-NotebookLM-Profile"] == "work"
        assert "secret internal detail" not in response.text


@pytest.mark.parametrize(
    ("selection", "status", "code"),
    [
        ([], 400, "profile_required"),
        ([("X-NotebookLM-Profile", "")], 400, "profile_required"),
        ([("X-NotebookLM-Profile", "missing")], 404, "unknown_profile"),
        ([("X-NotebookLM-Profile", "work,personal")], 400, "invalid_profile"),
        (
            [("X-NotebookLM-Profile", "work"), ("X-NotebookLM-Profile", "personal")],
            400,
            "invalid_profile",
        ),
    ],
)
def test_selection_is_required_for_all_authenticated_routes(
    selection: Any, status: int, code: str
) -> None:
    app = profile_app(profile_client_factory=fake_factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for route in ("/v1/notebooks", "/v1/server/info"):
            response = client.get(route, headers=selection)
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Vary"] == "X-NotebookLM-Profile"
        assert client.get("/healthz").json() == {"ok": True}
        # Authentication always wins over selection errors.
        response = client.get("/v1/notebooks", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401


def test_pending_state_is_per_profile_but_route_capacity_is_process_wide() -> None:
    app = profile_app(profile_client_factory=fake_factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        registry = app.state.notebooklm
        assert isinstance(registry, ProfileRegistry)
        a, b = registry.profiles.values()
        assert a.limiters is b.limiters
        a.pending.record("same-id", "source")
        assert not b.pending.knows("same-id", "source")
        for name, status in (("work", 200), ("personal", 404)):
            response = client.get(
                "/v1/notebooks/same-id/sources/source", headers={"X-NotebookLM-Profile": name}
            )
            assert response.status_code == status


def test_missing_profile_degrades_independently_then_recovers() -> None:
    attempts: Counter[str] = Counter()
    closed: list[str] = []
    repaired = False

    @asynccontextmanager
    async def factory(name: str) -> Any:
        attempts[name] += 1
        if name == "work" and not repaired:
            raise AuthError("unavailable")
        try:
            yield FakeClient()
        finally:
            closed.append(name)

    app = profile_app(profile_client_factory=factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        response = client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "work"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "profile_unavailable"
        assert (
            client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "personal"}).status_code
            == 200
        )
        info = client.get("/v1/server/info", headers={"X-NotebookLM-Profile": "work"}).json()
        assert info["auth"]["profile"] == "work"
        assert info["auth"]["ready"] is False
        assert "startup_error" in info["auth"]
        assert attempts == {"work": 2, "personal": 1}
        # Force the retry clock forward without sleeping or changing the limiter.
        repaired = True
        with pytest.MonkeyPatch.context() as patch:
            import notebooklm.server.app as app_module

            original = app_module.time.monotonic
            patch.setattr(
                app_module,
                "time",
                type("Clock", (), {"monotonic": staticmethod(lambda: original() + 10)}),
            )
            assert (
                client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "work"}).status_code
                == 200
            )
    assert Counter(closed) == {"work": 1, "personal": 1}


async def test_concurrent_requests_coalesce_only_their_profile_recovery() -> None:
    attempts: Counter[str] = Counter()

    @asynccontextmanager
    async def factory(name: str) -> Any:
        attempts[name] += 1
        if name == "work":
            await asyncio.sleep(0.03)
            raise AuthError("unavailable")
        yield FakeClient()

    app = profile_app(profile_client_factory=factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.1", 1)),
            base_url="http://127.0.0.1",
            headers=HEADERS,
        ) as client,
    ):
        responses = await asyncio.gather(
            *(
                client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": name})
                for name in ["work"] * 5 + ["personal"] * 5
            )
        )
    assert [r.status_code for r in responses] == [503] * 5 + [200] * 5
    assert attempts == {"work": 2, "personal": 1}


async def test_profile_startup_overlaps_and_closes_all_clients() -> None:
    entered = {name: asyncio.Event() for name in ("work", "personal")}
    closed: list[str] = []

    @asynccontextmanager
    async def factory(name: str) -> Any:
        entered[name].set()
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        try:
            yield FakeClient()
        finally:
            closed.append(name)

    app = profile_app(profile_client_factory=factory)
    async with app.router.lifespan_context(app):
        assert all(state.client is not None for state in app.state.notebooklm.profiles.values())
    assert sorted(closed) == ["personal", "work"]


async def test_cancelled_profile_startup_settles_before_closing_clients() -> None:
    opened = asyncio.Event()
    waiting = asyncio.Event()
    closed: list[str] = []

    @asynccontextmanager
    async def factory(name: str) -> Any:
        try:
            if name == "work":
                opened.set()
            else:
                await opened.wait()
                waiting.set()
                await asyncio.Event().wait()
            yield FakeClient()
        finally:
            closed.append(name)

    app = profile_app(profile_client_factory=factory)

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pytest.fail("startup should be cancelled")

    task = asyncio.create_task(start())
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(closed) == ["personal", "work"]


def test_transient_startup_diagnostics_are_retriable_and_sanitized() -> None:
    @asynccontextmanager
    async def factory(name: str) -> Any:
        if name == "work":
            raise RuntimeError("sensitive upstream body")
        yield FakeClient()

    app = profile_app(profile_client_factory=factory)
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        response = client.get("/v1/server/info", headers={"X-NotebookLM-Profile": "work"})
        error = response.json()["auth"]["startup_error"]
        assert error["code"] == "profile_unavailable"
        assert error["retriable"] is True
        assert "sensitive" not in response.text


def test_real_profiles_with_missing_or_malformed_tokens_degrade(tmp_path: Path) -> None:
    malformed = tmp_path / "profiles" / "personal" / "master_token.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not JSON")
    app = profile_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal"):
            headers = {"X-NotebookLM-Profile": name}
            info = client.get("/v1/server/info", headers=headers).json()["auth"]
            assert info["ready"] is False
            assert info["master_token_valid"] is False
            assert client.get("/v1/notebooks", headers=headers).status_code == 503


async def test_chat_capacity_is_shared_across_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    from notebooklm.types import AskResult

    from ._concurrency import ActiveHold

    monkeypatch.setenv("NOTEBOOKLM_SERVER_CHAT_CONCURRENCY", "1")
    hold = ActiveHold()

    async def slow_ask(notebook_id: str, question: str, **kwargs: Any) -> AskResult:
        await hold.enter()
        try:
            await hold.release.wait()
            return AskResult(
                answer="answer", conversation_id="conversation", turn_number=1, is_follow_up=False
            )
        finally:
            await hold.leave()

    @asynccontextmanager
    async def factory(name: str) -> Any:
        client = FakeClient()
        monkeypatch.setattr(client.chat, "ask", slow_ask)
        yield client

    app = profile_app(profile_client_factory=factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.1", 1)),
            base_url="http://127.0.0.1",
            headers=HEADERS,
        ) as client,
    ):
        first = asyncio.create_task(
            client.post(
                "/v1/notebooks/nb/chat",
                json={"question": "one"},
                headers={"X-NotebookLM-Profile": "work"},
            )
        )
        await asyncio.wait_for(hold.entered.wait(), 1)
        second = asyncio.create_task(
            client.post(
                "/v1/notebooks/nb/chat",
                json={"question": "two"},
                headers={"X-NotebookLM-Profile": "personal"},
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert not second.done()
            assert (await asyncio.wait_for(client.get("/healthz"), 1)).status_code == 200
        finally:
            hold.release.set()
        results = await asyncio.gather(first, second)
    assert [r.status_code for r in results] == [200, 200]
    assert hold.max_active == 1


@pytest.mark.parametrize(
    "profiles",
    [
        [],
        ["work", "work"],
        ["work", "../escape"],
        ["work", ""],
        ["work", " personal"],
        "work,personal",
    ],
)
def test_invalid_profile_configuration_fails_before_startup(profiles: Any) -> None:
    with pytest.raises(ValueError):
        create_app(profiles=profiles, backend="android")


def test_canonical_path_aliases_are_rejected(tmp_path: Path) -> None:
    real = tmp_path / "profiles" / "work"
    real.mkdir(parents=True)
    (real.parent / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical storage path"):
        create_app(profiles=["work", "alias"], backend="android")


def test_web_multi_profile_is_refused_and_single_entry_retains_behavior() -> None:
    with pytest.raises(ValueError, match="requires backend"):
        create_app(profiles=["work", "personal"])
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_app(profile="work", profiles=["personal"], backend="android")
    app = create_app(profiles=["work"], client_factory=lambda: fake_factory("work"))
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        assert client.get("/v1/notebooks").status_code == 200
        assert (
            client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "ignored"}).status_code
            == 200
        )


def test_multi_profile_never_changes_process_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paths, "_active_profile", "unrelated")
    app = profile_app(profile_client_factory=fake_factory)
    with TestClient(app) as client:
        assert paths.get_active_profile() == "unrelated"
        assert client.get("/healthz").json() == {"ok": True}
    assert paths.get_active_profile() == "unrelated"


def test_real_clients_allow_copied_tokens_without_web_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grpc = pytest.importorskip("grpc")
    pytest.importorskip("gpsoauth")
    from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2
    from notebooklm._android.session import AndroidSession
    from notebooklm._auth import tokens
    from notebooklm._auth.mint_service import MintedOAuthToken, MintService

    record = MasterToken(
        email="same@example.com", android_id="1234567890123456", secret="copied-secret"
    )
    for name in ("work", "personal"):
        path = paths.get_storage_path(name)
        ProfileStore(path).write_master_token(record)
        path.write_text("not valid Web auth JSON")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "also not valid Web auth")
    mints: list[MasterToken] = []
    wire_bearers: list[str] = []
    initial_bearers: dict[str, str] = {}
    reject_work = False

    async def no_web(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Android multi-profile must not bootstrap Web auth")

    async def mint(self: Any, token: MasterToken, spec: Any) -> MintedOAuthToken:
        mints.append(token)
        return MintedOAuthToken(
            token=f"fake-bearer-{len(mints)}", expires_at=int(time.time()) + 3600
        )

    async def callable_(self: Any, *args: Any, **kwargs: Any) -> Any:
        async def send(request: Any, *, metadata: Any, timeout: Any) -> Any:
            bearer = dict(metadata)["authorization"]
            wire_bearers.append(bearer)
            if reject_work and bearer == initial_bearers["work"]:
                raise grpc.aio.AioRpcError(grpc.StatusCode.UNAUTHENTICATED, (), ())
            return read_pb2.ListRecentlyViewedProjectsResponse()

        return send

    monkeypatch.setattr(tokens, "_load_stored_auth", no_web)
    monkeypatch.setattr(MintService, "mint_oauth", mint)
    monkeypatch.setattr(AndroidSession, "_unary_callable", callable_)
    app = profile_app()
    with TestClient(app, headers=HEADERS, client=("127.0.0.1", 1)) as client:
        for name in ("work", "personal"):
            assert (
                client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": name}).status_code
                == 200
            )
            initial_bearers[name] = wire_bearers[-1]
            info = client.get("/v1/server/info", headers={"X-NotebookLM-Profile": name}).json()
            assert info["auth"] == {
                "backend": "android",
                "profile": name,
                "master_token_present": True,
                "master_token_valid": True,
                "authenticated": True,
                "ready": True,
            }
        assert len(mints) == 2
        reject_work = True
        assert (
            client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "work"}).status_code == 200
        )
        assert len(mints) == 3
        assert (
            client.get("/v1/notebooks", headers={"X-NotebookLM-Profile": "personal"}).status_code
            == 200
        )
        assert wire_bearers[-1] == initial_bearers["personal"]
        assert len(mints) == 3
    assert all(token == record for token in mints)
    assert set(wire_bearers) == {
        "Bearer fake-bearer-1",
        "Bearer fake-bearer-2",
        "Bearer fake-bearer-3",
    }
    for name in ("work", "personal"):
        assert paths.get_storage_path(name).read_text() == "not valid Web auth JSON"
