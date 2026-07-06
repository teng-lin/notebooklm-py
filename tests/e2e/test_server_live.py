"""Layer-B e2e: the REST ``/v1`` server against the **real** NotebookLM API,
driven entirely in-process over ``httpx.ASGITransport`` (no socket, no
subprocess).

Mirrors ``tests/e2e/test_mcp_http.py``: ``create_app``'s ``client_factory`` seam
yields the already-open LIVE e2e ``client`` fixture, so the routes hit real
Google while still exercising the REST-specific surface:

* the **bearer gate** (no token → 401) and the **loopback-Host guard**
  (non-loopback ``Host`` → 403, the DNS-rebinding defense),
* the read routes (``/v1/server/info``, ``/v1/notebooks``,
  ``/v1/notebooks/{id}``, ``/v1/notebooks/{id}/sources``) against live data,
* one live ``POST /v1/notebooks/{id}/chat`` (a real answer).

Requires auth and the ``server`` extra (``importorskip`` skips the module when
FastAPI is absent). Auto-marked ``e2e`` by ``conftest.pytest_itemcollected``.
"""

from __future__ import annotations

import contextlib
import traceback
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

# Require the `server` extra; skip the whole module cleanly when FastAPI is absent.
pytest.importorskip("fastapi")

from notebooklm.server._auth import (  # noqa: E402 - after importorskip
    ALLOW_EXTERNAL_BIND_ENV,
    SERVER_TOKEN_ENV,
)
from notebooklm.server.app import create_app  # noqa: E402 - after importorskip

from .conftest import requires_auth  # noqa: E402 - after importorskip

pytestmark = pytest.mark.e2e

#: Arbitrary bearer the in-process app validates against (set via the env below).
_TOKEN = "e2e-rest-bearer-token"
#: Bearer + a loopback ``Host`` literal — satisfies both the token gate and the
#: DNS-rebinding (loopback-Host) guard on every ``/v1`` request.
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "Host": "127.0.0.1"}

#: Phrases that mark a live chat rate-limit (mirrors the conftest set) — a
#: server-side throttle, not a client defect, so the chat e2e skips on these.
_RATE_LIMIT_PHRASES = (
    "rate limit",
    "rate limited",
    "rate-limited",
    "rejected by the api",
    "429",
    "too many requests",
)


@pytest.fixture(autouse=True)
def _server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the bearer the REST app checks against, for every test here."""
    monkeypatch.setenv(SERVER_TOKEN_ENV, _TOKEN)
    # Keep the default loopback-Host guard active: a leaked
    # NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1 would disable it and let the
    # non-loopback-Host gate test pass a request it should reject.
    monkeypatch.delenv(ALLOW_EXTERNAL_BIND_ENV, raising=False)


@contextlib.asynccontextmanager
async def _live_rest_app(real_client: Any) -> AsyncIterator[httpx.AsyncClient]:
    """A REST app bound to the LIVE e2e client, driven in-process over ASGI.

    The ``client_factory`` yields the already-open fixture client and must **not**
    close it on lifespan exit (the fixture owns its lifecycle). ``ASGITransport``
    does not run the ASGI lifespan, so we enter it explicitly to populate
    ``app.state`` the way a real ``uvicorn`` boot would.
    """

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        yield real_client  # fixture-owned; do not aclose here

    app = create_app(client_factory=factory)
    async with app.router.lifespan_context(app):
        # Pin the ASGI peer to a loopback literal so the auth-gate tests exercise
        # the intended peer condition (require_auth reads request.client.host)
        # instead of relying on httpx's default ASGITransport peer.
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            yield http


@requires_auth
class TestRestServerLiveAuthGate:
    """The bearer + loopback-Host gates on the in-process REST transport."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_unauthenticated_is_rejected(self, client: Any) -> None:
        """A ``/v1`` request with no bearer is rejected (401) before any handler."""
        async with _live_rest_app(client) as http:
            resp = await http.get("/v1/notebooks", headers={"Host": "127.0.0.1"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_non_loopback_host_is_rejected(self, client: Any) -> None:
        """A spoofed non-loopback ``Host`` is rejected (403) even with the bearer."""
        async with _live_rest_app(client) as http:
            resp = await http.get(
                "/v1/notebooks",
                headers={"Authorization": f"Bearer {_TOKEN}", "Host": "evil.example.com"},
            )
        assert resp.status_code == 403


@requires_auth
class TestRestServerLiveReads:
    """Read routes against live account data."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_server_info(self, client: Any) -> None:
        """``GET /v1/server/info`` reports the version and the auth-probe block."""
        async with _live_rest_app(client) as http:
            resp = await http.get("/v1/server/info", headers=_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["server"] == "notebooklm-server"
        assert body["version"]
        # server_info deliberately probes ON-DISK storage (has_env_auth=False), so
        # authenticated/sid_cookie are True only when a storage_state.json exists —
        # not under the nightly's inline NOTEBOOKLM_AUTH_JSON auth. Assert the block
        # shape, not the storage-dependent values (the data routes below prove the
        # injected client is genuinely authenticated).
        auth = body["auth"]
        assert isinstance(auth["authenticated"], bool)
        assert isinstance(auth["sid_cookie"], bool)

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_notebooks_contains_readonly(
        self, client: Any, read_only_notebook_id: str
    ) -> None:
        """``GET /v1/notebooks`` lists the live set including the read-only notebook."""
        async with _live_rest_app(client) as http:
            resp = await http.get("/v1/notebooks", headers=_HEADERS)
        assert resp.status_code == 200
        notebooks = resp.json()["notebooks"]
        assert isinstance(notebooks, list) and notebooks
        assert any(nb.get("id") == read_only_notebook_id for nb in notebooks)

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_notebook(self, client: Any, read_only_notebook_id: str) -> None:
        """``GET /v1/notebooks/{id}`` resolves the read-only notebook by id."""
        async with _live_rest_app(client) as http:
            resp = await http.get(f"/v1/notebooks/{read_only_notebook_id}", headers=_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["id"] == read_only_notebook_id

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_sources_nonempty(self, client: Any, read_only_notebook_id: str) -> None:
        """``GET /v1/notebooks/{id}/sources`` returns the notebook's sources."""
        async with _live_rest_app(client) as http:
            resp = await http.get(
                f"/v1/notebooks/{read_only_notebook_id}/sources", headers=_HEADERS
            )
        assert resp.status_code == 200
        sources = resp.json()["sources"]
        assert isinstance(sources, list) and sources
        assert all(s.get("id") for s in sources)


@requires_auth
@pytest.mark.live_chat_ask
class TestRestServerLiveChat:
    """A live chat answer over the REST POST route."""

    # @pytest.mark.readonly: chat.ask only appends ephemeral conversation history
    # — no source, artifact, or note is mutated — so it is safe against the shared
    # read-only notebook, matching the MCP TestMcpChat.test_configure_then_ask
    # convention (also @readonly with the same fixture).
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_chat_ask_returns_answer(self, client: Any, read_only_notebook_id: str) -> None:
        """``POST /v1/notebooks/{id}/chat`` returns a non-empty answer string."""
        async with _live_rest_app(client) as http:
            try:
                resp = await http.post(
                    f"/v1/notebooks/{read_only_notebook_id}/chat",
                    headers=_HEADERS,
                    json={"question": "In one sentence, what are these sources about?"},
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised unless rate-limited
                # The conftest wraps client.chat.ask to pytest.skip() on a
                # rate-limit ChatError. That works when a test calls ask()
                # directly (the MCP chat e2e does), but here ask() runs *inside*
                # the ASGI route, so the skip surfaces as a BaseExceptionGroup at
                # the ASGITransport boundary instead of a clean skip. Honor the
                # rate-limit skip here at the test frame; re-raise anything else.
                rendered = "".join(traceback.format_exception(exc)).lower()
                if any(p in rendered for p in _RATE_LIMIT_PHRASES):
                    pytest.skip("chat rate-limited (surfaced through the REST route)")
                raise
        assert resp.status_code == 200, resp.text
        answer = resp.json()["answer"]
        assert isinstance(answer, str) and answer.strip()
