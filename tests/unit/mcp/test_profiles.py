"""Profile routing contracts through the actual FastMCP protocol."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from notebooklm.exceptions import ConfigurationError, ServerError  # noqa: E402
from notebooklm.mcp._context import get_client_from_app, selected_profile  # noqa: E402
from notebooklm.mcp._filelink import FileLinkSigner, FileTransferConfig  # noqa: E402
from notebooklm.mcp._profiles import ProfileClientProvider  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402


@pytest.fixture
def profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_MCP_UPLOAD_WIDGET", raising=False)
    clients = {}
    for name in ("work", "personal"):
        client = MagicMock()
        client.notebooks.list = AsyncMock(return_value=[])
        clients[name] = client

    @asynccontextmanager
    async def factory(name):
        yield clients[name]

    return clients, factory


async def test_every_tool_requires_profile_and_preserves_manifest(profiles):
    _, factory = profiles
    single = await create_server().list_tools()
    multi = await create_server(
        profiles=["work", "personal"], backend="android", profile_client_factory=factory
    ).list_tools()
    assert {t.name for t in single} == {t.name for t in multi}
    original = {t.name: t for t in single}
    for tool in multi:
        assert "profile" in tool.parameters["required"]
        assert tool.parameters["properties"]["profile"] == {"type": "string"}
        assert tool.annotations == original[tool.name].annotations
        assert tool.output_schema == original[tool.name].output_schema
        assert "profile" not in original[tool.name].parameters.get("properties", {})


async def test_concurrent_calls_route_without_global_state(profiles):
    clients, factory = profiles
    entered = {name: asyncio.Event() for name in clients}

    def listing(name):
        async def run():
            entered[name].set()
            await entered["personal" if name == "work" else "work"].wait()
            assert selected_profile.get() == name
            return []

        return run

    for name, client in clients.items():
        client.notebooks.list.side_effect = listing(name)
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        await asyncio.wait_for(
            asyncio.gather(
                *[session.call_tool("notebook_list", {"profile": name}) for name in clients]
            ),
            3,
        )
        for client in clients.values():
            client.notebooks.list.assert_awaited_once()
    assert selected_profile.get() is None


async def test_invalid_selection_fails_before_client_use(profiles):
    clients, factory = profiles
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        for args in ({}, {"profile": ""}, {"profile": "missing"}, {"profile": "work,personal"}):
            with pytest.raises(ToolError):
                await session.call_tool("notebook_list", args)
        for client in clients.values():
            client.notebooks.list.assert_not_awaited()
        await session.call_tool("notebook_list", {"profile": "work"})
        clients["work"].notebooks.list.assert_awaited_once()


async def test_tool_error_keeps_category(profiles):
    clients, factory = profiles
    clients["work"].notebooks.list.side_effect = ConfigurationError("setup required")
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        with pytest.raises(ToolError, match="CONFIG: setup required"):
            await session.call_tool("notebook_list", {"profile": "work"})


async def test_handshake_and_healthy_profile_ignore_stalled_profile(profiles, monkeypatch):
    clients, _ = profiles
    stalled = asyncio.Event()
    cleaned = asyncio.Event()

    @asynccontextmanager
    async def factory(name):
        if name == "work":
            try:
                await stalled.wait()
            finally:
                cleaned.set()
        yield clients[name]

    monkeypatch.setenv("NOTEBOOKLM_MCP_PROFILE_STARTUP_TIMEOUT", "0.05")
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        await session.call_tool("notebook_list", {"profile": "personal"})
        with pytest.raises(ToolError, match="SERVER: Selected Android profile is unavailable"):
            await session.call_tool("notebook_list", {"profile": "work"})
        await asyncio.wait_for(cleaned.wait(), 1)
        await session.call_tool("notebook_list", {"profile": "personal"})


async def test_failure_cooldown_then_single_flight_recovery(monkeypatch):
    attempts = 0
    now = 10.0
    monkeypatch.setattr("notebooklm.mcp._profiles.time", SimpleNamespace(monotonic=lambda: now))
    client = MagicMock()

    @asynccontextmanager
    async def factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConfigurationError("secret credential detail")
        yield client

    provider = ProfileClientProvider(factory, 1)
    try:
        for _ in range(2):
            with pytest.raises(ServerError, match="Selected Android profile is unavailable"):
                await provider.get()
        assert attempts == 1
        now += 6
        assert await asyncio.gather(*[provider.get() for _ in range(10)]) == [client] * 10
        assert attempts == 2
    finally:
        await provider.aclose()


async def test_profile_state_isolates_jobs_and_research(profiles):
    clients, factory = profiles
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        registry = server._lifespan_result
        work = registry.profiles["work"]
        personal = registry.profiles["personal"]
        work.cancelled_research.record(("same-notebook", "same-task"))
        assert ("same-notebook", "same-task") not in personal.cancelled_research

        async def job():
            return {"answer": "work private answer"}

        entry, _ = work.chat_tasks.start("same-key", job)
        await entry.task
        own = await session.call_tool("chat_status", {"profile": "work", "task_id": entry.task_id})
        assert "work private answer" in str(own)
        other = await session.call_tool(
            "chat_status", {"profile": "personal", "task_id": entry.task_id}
        )
        assert "work private answer" not in str(other)
        assert other.structured_content["status"] == "unknown"


async def test_android_diagnostics_never_probe_web(profiles, monkeypatch):
    clients, factory = profiles
    probe = AsyncMock(side_effect=AssertionError("Web auth probe must not run"))
    monkeypatch.setattr("notebooklm.mcp.tools.meta.run_auth_check", probe)
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory
    )
    async with Client(server) as session:
        for name in clients:
            result = await session.call_tool("server_info", {"profile": name})
            assert result.structured_content["auth"]["profile"] == name
            assert result.structured_content["auth"]["backend"] == "android"
            assert result.structured_content["auth"]["master_token_present"] is False
        probe.assert_not_awaited()


async def test_signed_links_bind_profile_and_route_client(profiles):
    clients, factory = profiles
    cfg = FileTransferConfig(FileLinkSigner(b"a" * 32), "https://example.com")
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory, file_transfer=cfg
    )
    async with Client(server):
        work = server._lifespan_result.profiles["work"].file_transfer
        personal = server._lifespan_result.profiles["personal"].file_transfer
        for method, op in (("upload_url", "ul"), ("download_url", "dl")):
            url = getattr(work, method)({"nb": "same-id", "profile": "personal"})
            payload = cfg.signer.verify(url.rsplit("/", 1)[-1], op=op)
            assert payload["profile"] == "work"
            assert work.matches_profile(payload)
            assert not personal.matches_profile(payload)
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(fastmcp_server=server))
            )
            assert await get_client_from_app(request, profile=payload["profile"]) is clients["work"]
            with pytest.raises(Exception, match="profile is required"):
                await get_client_from_app(request)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"profile": "work", "profiles": ["work", "personal"]}, "mutually exclusive"),
        ({"profiles": ["work", "personal"], "backend": "web"}, "requires backend"),
        ({"profiles": ["work", "WORK"], "backend": "android"}, "Duplicate profile"),
        ({"profiles": [], "backend": "android"}, "non-empty"),
        ({"profiles": ["../escape", "work"], "backend": "android"}, "profile"),
    ],
)
def test_configuration_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        create_server(**kwargs)


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize(
    "cli, env, expected",
    [
        (["--profiles", "work,personal"], "ignored,profiles", {"profiles": ["work", "personal"]}),
        (["--profile", "solo"], "work,personal", {"profile": "solo"}),
        ([], "work,personal", {"profiles": ["work", "personal"]}),
    ],
)
def test_cli_profile_precedence(monkeypatch, transport, cli, env, expected):
    from notebooklm.mcp import __main__ as entry

    monkeypatch.setenv("NOTEBOOKLM_MCP_PROFILES", env)
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "ambient")
    monkeypatch.delenv("NOTEBOOKLM_MCP_OAUTH_PASSWORD", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_OAUTH_BASE_URL", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_PUBLIC_URL", raising=False)
    build = MagicMock()
    monkeypatch.setattr(entry, "create_server", build)
    entry.main(["--transport", transport, "--backend", "android", *cli])
    kwargs = build.call_args.kwargs
    assert kwargs["backend"] == "android"
    assert {k: v for k, v in kwargs.items() if k in ("profile", "profiles")} == expected


async def test_single_profile_rejects_other_signed_account(profiles):
    clients, _ = profiles

    @asynccontextmanager
    async def factory():
        yield clients["personal"]

    server = create_server(profile="personal", client_factory=factory)
    async with Client(server):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fastmcp_server=server)))
        with pytest.raises(Exception, match="Signed profile"):
            await get_client_from_app(request, profile="work")
        assert await get_client_from_app(request) is clients["personal"]


def test_http_file_download_uses_signed_profile(profiles, monkeypatch, tmp_path):
    from dataclasses import replace
    from pathlib import Path

    from starlette.testclient import TestClient

    from notebooklm.mcp import _fileroutes

    clients, factory = profiles
    cfg = FileTransferConfig(FileLinkSigner(b"a" * 32), "https://example.com")
    seen = []

    async def download(plan, client, **kwargs):
        seen.append(client)
        Path(plan.output_path).write_bytes(b"audio")
        return _fileroutes.download_core.DownloadResult(
            outcome=_fileroutes.download_core.DownloadOutcome.SINGLE_DOWNLOADED,
            artifact={"id": "a1", "title": "Recording", "selection_reason": "latest"},
            output_path=plan.output_path,
        )

    monkeypatch.setattr(_fileroutes.download_core, "execute_download", download)
    server = create_server(
        profiles=list(clients), backend="android", profile_client_factory=factory, file_transfer=cfg
    )
    with TestClient(server.http_app()) as http:
        for name in clients:
            url = replace(cfg, profile=name).download_url({"nb": "same-id", "atype": "audio"})
            response = http.get(url.replace(cfg.base_url, ""))
            assert response.status_code == 200
        # A valid MAC without profile must never fall back to either account.
        url = cfg.download_url({"nb": "same-id", "atype": "audio"})
        assert http.get(url.replace(cfg.base_url, "")).status_code != 200
    assert seen == [clients["work"], clients["personal"]]
