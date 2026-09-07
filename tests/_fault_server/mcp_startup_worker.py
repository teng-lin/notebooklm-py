"""Real stdio server with a socket-gated client factory for startup regression tests.

The opening HTTP request models a slow authentication hop; it does not exercise
Google's authentication protocol. After release, notebook tools use the production
Web client with synthetic credentials and all traffic routed to the parent's server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from notebooklm.mcp.server import create_server

from .http import HttpFaultServer
from .web import build_fault_client


class _ParentFaultServer(HttpFaultServer):
    """Reuse the logical-host router against a listener owned by the parent."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self._parent_address = ("127.0.0.1", port)

    @property
    def address(self) -> tuple[str, int]:
        return self._parent_address


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    upstream = _ParentFaultServer(args.port)
    state = {"opens": 0, "cancelled": False, "client_closed": False, "http_closed": False}

    @asynccontextmanager
    async def factory():
        state["opens"] += 1
        opening = upstream.client_factory(timeout=60)
        client = None
        try:
            async with opening:
                response = await opening.get("https://notebook.google.com/")
                response.raise_for_status()
            client = build_fault_client(upstream, timeout=2, server_error_max_retries=0)
            async with client:
                yield client
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        finally:
            state["http_closed"] = opening.is_closed
            state["client_closed"] = client is None or not client._lifecycle.is_open()
            args.report.write_text(json.dumps(state), encoding="utf-8")

    create_server(client_factory=factory).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
