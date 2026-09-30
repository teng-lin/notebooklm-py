"""Web multi-profile MCP serving through the actual FastMCP protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from notebooklm import paths  # noqa: E402
from notebooklm._app import web_profiles  # noqa: E402
from notebooklm._app.web_profiles import web_session_keys  # noqa: E402
from notebooklm.exceptions import NotebookLMError  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402

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
        "NOTEBOOKLM_MCP_PROFILE_STARTUP_TIMEOUT",
        "NOTEBOOKLM_MCP_UPLOAD_WIDGET",
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


def write_session(name: str, psid: str) -> Path:
    path = paths.get_storage_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = [
        _cookie("SID", f"sid-{psid}"),
        _cookie("__Secure-1PSID", psid),
        _cookie("__Secure-1PSIDTS", f"ts-{psid}"),
    ]
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


def _client(name: str) -> MagicMock:
    client = MagicMock()
    client.notebooks.list = AsyncMock(return_value=[])
    client.get_account_email = AsyncMock(return_value=f"{name}@example.com")
    client.get_account_authuser = MagicMock(return_value=0)
    client.settings.get_user_settings = AsyncMock(side_effect=NotebookLMError("offline"))
    client.name = name
    return client


class OpenSeam:
    """Stand-in for ``WebProfileSet``'s network-touching open."""

    def __init__(self, stall: set[str] | None = None) -> None:
        self.opened: list[str] = []
        self.clients: dict[str, MagicMock] = {}
        self.stall = stall or set()
        self.active = 0
        self.max_active = 0

    def __call__(self, path: Path, profile: str, keepalive: float | None) -> Any:
        @asynccontextmanager
        async def open_client() -> Any:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if profile in self.stall:
                    await asyncio.Event().wait()
                await asyncio.sleep(0.01)
                self.opened.append(profile)
                client = self.clients.setdefault(profile, _client(profile))
                client.auth = SimpleNamespace(storage_path=path)
            finally:
                self.active -= 1
            yield client

        return open_client()


@pytest.fixture
def open_seam(monkeypatch: pytest.MonkeyPatch) -> OpenSeam:
    seam = OpenSeam()
    monkeypatch.setattr(web_profiles, "_open_web_client", seam)

    async def no_bootstrap(path: Path) -> bool:
        pytest.fail("bootstrap must not run for these profiles")

    monkeypatch.setattr(web_profiles, "bootstrap_missing_storage_from_master_token", no_bootstrap)
    return seam


async def test_web_multi_profile_is_accepted_and_requires_profile(open_seam: OpenSeam) -> None:
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    server = create_server(profiles=["work", "personal"], backend="web")
    tools = await server.list_tools()
    assert all("profile" in tool.parameters["required"] for tool in tools)
    async with Client(server) as session:
        with pytest.raises(ToolError):
            await session.call_tool("notebook_list", {})
        for name in ("work", "personal"):
            await session.call_tool("notebook_list", {"profile": name})
            open_seam.clients[name].notebooks.list.assert_awaited_once()
        registry = server._lifespan_result
        assert {state.backend for state in registry.profiles.values()} == {"web"}
    assert open_seam.max_active == 1


async def test_backend_from_environment_selects_web(
    open_seam: OpenSeam, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    server = create_server(profiles=["work", "personal"])
    async with Client(server) as session:
        await session.call_tool("notebook_list", {"profile": "work"})
    assert "work" in open_seam.opened


@pytest.mark.parametrize(
    ("env", "value", "match"),
    [
        ("NOTEBOOKLM_AUTH_JSON", "", "refuses NOTEBOOKLM_AUTH_JSON"),
        (
            "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL",
            "http://127.0.0.1:9222",
            "refuses NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL",
        ),
    ],
)
def test_web_refusals_do_not_affect_android_or_single_profile(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str, match: str
) -> None:
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError, match=match):
        create_server(profiles=["work", "personal"], backend="web")
    create_server(profiles=["work", "personal"], backend="android")
    create_server(profiles=["work"], backend="web")


def test_entrypoint_reports_web_refusal_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    from notebooklm.mcp import __main__ as entry

    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    monkeypatch.setattr(entry, "_configure_logging", lambda level: None)
    with pytest.raises(SystemExit, match="refuses NOTEBOOKLM_AUTH_JSON"):
        entry.main(["--profiles", "work,personal", "--backend", "web"])


async def test_copied_session_fails_only_that_profile(
    open_seam: OpenSeam, caplog: pytest.LogCaptureFixture
) -> None:
    write_session("work", "copied-psid-secret")
    write_session("personal", "copied-psid-secret")
    write_session("third", "distinct-psid-secret")
    keys = web_session_keys(paths.get_storage_path("work"))
    assert keys
    server = create_server(profiles=["work", "personal", "third"], backend="web")
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        async with Client(server) as session:
            for name in ("work", "personal"):
                with pytest.raises(
                    ToolError, match="SERVER: Selected Web profile is unavailable"
                ) as caught:
                    await session.call_tool("notebook_list", {"profile": name})
                texts.append(str(caught.value))
                info = await session.call_tool("server_info", {"profile": name})
                texts.append(json.dumps(info.structured_content))
                auth = info.structured_content["auth"]
                assert auth["session_conflict"] is True
                assert auth["ready"] is False
                assert auth["authenticated"] is False
            await session.call_tool("notebook_list", {"profile": "third"})
            open_seam.clients["third"].notebooks.list.assert_awaited_once()
    assert open_seam.opened == ["third"]
    assert "shares a Web session with configured profile(s)" in caplog.text
    for text in [*texts, caplog.text]:
        assert "copied-psid-secret" not in text
        assert "distinct-psid-secret" not in text
        assert all(key not in text for key in keys)


async def test_web_server_info_block(open_seam: OpenSeam, monkeypatch: pytest.MonkeyPatch) -> None:
    from notebooklm.mcp.tools import meta

    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    server = create_server(profiles=["work", "personal"], backend="web")
    async with Client(server) as session:
        await session.call_tool("notebook_list", {"profile": "work"})
        result = await session.call_tool("server_info", {"profile": "work"})
        auth = result.structured_content["auth"]
        assert set(auth) == WEB_AUTH_KEYS
        assert auth["backend"] == "web"
        assert auth["profile"] == "work"
        assert auth["storage_exists"] is True
        assert auth["json_valid"] is True
        assert auth["session_conflict"] is False
        assert auth["master_token_present"] is False
        assert auth["ready"] is True
        assert "chat_tasks" in result.structured_content
        # The Web block never falls through to the single-profile probe path.
        probe = AsyncMock(side_effect=AssertionError("single-profile probe must not run"))
        monkeypatch.setattr(meta, "resolve_profile", probe)
        await session.call_tool("server_info", {"profile": "personal"})
        probe.assert_not_called()


async def test_copy_made_after_serving_is_advisory_for_the_serving_profile(
    open_seam: OpenSeam,
) -> None:
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    server = create_server(profiles=["work", "personal"], backend="web")
    async with Client(server) as session:
        await session.call_tool("notebook_list", {"profile": "work"})
        write_session("personal", "psid-work")  # a copy appears after work serves
        result = await session.call_tool("server_info", {"profile": "work"})
        auth = result.structured_content["auth"]
        assert auth["session_conflict"] is True  # what a reopen would find
        assert auth["ready"] is True
        assert auth["authenticated"] is True  # still serving its own session


async def test_initialize_is_not_blocked_by_stalled_web_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_MCP_PROFILE_STARTUP_TIMEOUT", "0.5")
    seam = OpenSeam(stall={"work"})
    monkeypatch.setattr(web_profiles, "_open_web_client", seam)
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    server = create_server(profiles=["work", "personal"], backend="web")

    async def exercise() -> None:
        async with Client(server) as session:
            assert await session.list_tools()
            # The stalled profile fails within its deadline; its sibling then
            # takes its turn and serves.
            await session.call_tool("notebook_list", {"profile": "personal"})
            with pytest.raises(ToolError, match="Selected Web profile is unavailable"):
                await session.call_tool("notebook_list", {"profile": "work"})

    await asyncio.wait_for(exercise(), 5)
    assert seam.opened == ["personal"]


async def test_android_messages_are_unchanged() -> None:
    from notebooklm.exceptions import ConfigurationError, ServerError
    from notebooklm.mcp._profiles import ProfileClientProvider

    @asynccontextmanager
    async def failing() -> Any:
        raise ConfigurationError("detail")
        yield  # pragma: no cover

    for kwargs, message in (
        ({}, "Selected Android profile is unavailable"),
        ({"backend_label": "Web"}, "Selected Web profile is unavailable"),
    ):
        provider = ProfileClientProvider(failing, 1, **kwargs)
        try:
            with pytest.raises(ServerError) as caught:
                await provider.get()
            assert str(caught.value) == message
        finally:
            await provider.aclose()


async def test_shutdown_cancels_warm_ups_waiting_for_their_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "work" holds the turn indefinitely (long deadline); "personal" queues.
    monkeypatch.setenv("NOTEBOOKLM_MCP_PROFILE_STARTUP_TIMEOUT", "30")
    seam = OpenSeam(stall={"work"})
    monkeypatch.setattr(web_profiles, "_open_web_client", seam)
    write_session("work", "psid-work")
    write_session("personal", "psid-personal")
    server = create_server(profiles=["work", "personal"], backend="web")

    async def exercise() -> None:
        async with Client(server) as session:
            assert await session.list_tools()
            await asyncio.sleep(0.05)
            assert seam.active == 1  # Only the stalled open; its sibling waits.

    await asyncio.wait_for(exercise(), 5)
    assert seam.opened == []
    assert seam.active == 0


async def test_master_token_only_profile_info_reflects_minted_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam = OpenSeam()
    monkeypatch.setattr(web_profiles, "_open_web_client", seam)

    async def bootstrap(path: Path) -> bool:
        # Keep the background warm-up in flight while server_info is called.
        await asyncio.sleep(0.2)
        write_session(path.parent.name, f"minted-{path.parent.name}")
        return True

    monkeypatch.setattr(web_profiles, "bootstrap_missing_storage_from_master_token", bootstrap)
    for name in ("work", "personal"):
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
    server = create_server(profiles=["work", "personal"], backend="web")
    async with Client(server) as session:
        result = await session.call_tool(
            "server_info", {"profile": "work", "include_account": True}
        )
        auth = result.structured_content["auth"]
        assert auth["ready"] is True
        assert auth["storage_exists"] is True
        assert auth["master_token_present"] is True
        assert auth["session_conflict"] is False
        assert result.structured_content["account"]["email"] == "work@example.com"


async def test_android_opens_still_overlap() -> None:
    entered = {name: asyncio.Event() for name in ("work", "personal")}

    @asynccontextmanager
    async def factory(name: str) -> Any:
        entered[name].set()
        # Serialized opens would never see both inside at once.
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered.values())), 2)
        yield _client(name)

    server = create_server(
        profiles=["work", "personal"], backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        for name in ("work", "personal"):
            await session.call_tool("notebook_list", {"profile": name})
