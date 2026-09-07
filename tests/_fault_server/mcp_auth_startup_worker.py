"""Isolated MCP process running the real stored-auth startup against local sockets."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from notebooklm._atomic_io import atomic_write_json
from notebooklm.client import NotebookLMClient
from notebooklm.mcp._clientprovider import ClientProvider
from notebooklm.mcp.server import create_server

from .http import LogicalHostTransport


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--transport", choices=("stdio", "http"), required=True)
    args = parser.parse_args()
    state: dict[str, Any] = {
        "opens": 0,
        "waiters": 0,
        "cancelled_waiters": 0,
        "errors": [],
        "report_errors": [],
    }
    clients: list[httpx.AsyncClient] = []
    library_clients: list[NotebookLMClient] = []
    report = args.directory / "report.json"

    def observe() -> None:
        try:
            atomic_write_json(report, state)
        except OSError as error:
            # Evidence I/O must not replace an authentication or shutdown error.
            state["report_errors"].append(type(error).__name__)

    # This process owns one cohort. Preserve the real client class while changing
    # only socket routing, including the pre-client auth transport constructor.
    original_client = httpx.AsyncClient

    class RoutedClient(original_client):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = LogicalHostTransport(
                dict.fromkeys(
                    ("notebook.google.com", "accounts.google.com"),
                    ("127.0.0.1", args.port),
                )
            )
            kwargs["trust_env"] = False
            super().__init__(**kwargs)
            clients.append(self)

    httpx.AsyncClient = RoutedClient
    original_get = ClientProvider.get

    async def observed_get(self: ClientProvider) -> NotebookLMClient:
        state["waiters"] += 1
        observe()
        try:
            return await original_get(self)
        except asyncio.CancelledError:
            state["cancelled_waiters"] += 1
            observe()
            raise

    ClientProvider.get = observed_get

    @asynccontextmanager
    async def factory():
        state["opens"] += 1
        observe()
        try:
            async with NotebookLMClient.from_storage(
                str(args.directory / "storage_state.json"),
                backend="web",
                timeout=3,
                keepalive=None,
                server_error_max_retries=0,
                rate_limit_max_retries=0,
            ) as client:
                library_clients.append(client)
                yield client
        except Exception as error:
            state["errors"].append(type(error).__name__)
            raise
        finally:
            observe()

    server = create_server(client_factory=factory)

    async def serve_http() -> None:
        from .adapter_listener import live_listener

        async with live_listener(server.http_app()) as (url, _listener):
            atomic_write_json(args.directory / "ready", {"url": url + "/mcp"})

            async def stopped() -> None:
                while not (args.directory / "stop").exists():
                    await asyncio.sleep(0.025)

            await asyncio.wait_for(stopped(), 25)

    try:
        if args.transport == "stdio":
            server.run(transport="stdio", show_banner=False)
        else:
            asyncio.run(serve_http())
    finally:
        state["http_closed"] = bool(clients) and all(client.is_closed for client in clients)
        state["client_closed"] = all(not client._lifecycle.is_open() for client in library_clients)
        state["settled"] = True
        observe()


if __name__ == "__main__":
    main()
