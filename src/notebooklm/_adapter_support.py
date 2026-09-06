"""Small import leaf for infrastructure shared by the MCP and REST adapters.

This module is intentionally outside :mod:`notebooklm._app`: the exported
values support transport hosting rather than transport-neutral business logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ._loop_bound import LoopBoundPrimitive
from ._redact import redact
from ._runtime.config import DEFAULT_SERVER_KEEPALIVE_INTERVAL
from ._runtime.operation_context import detached_operation_context
from ._serving import (
    LOOPBACK_HOSTNAMES,
    addr_is_loopback,
    check_bind_allowed,
    host_header_is_loopback,
    is_loopback,
)


def client_generation_epoch(client: Any) -> int:
    """Read the active client epoch through the adapter-support boundary."""

    epoch = client._collaborators.call_supervisor.active_epoch()
    if epoch is None:
        raise RuntimeError("Client not initialized. Use 'async with' context.")
    return epoch


@asynccontextmanager
async def _client_operation(
    client: Any,
    timeout: float | None,
    *,
    expected_epoch: int,
) -> AsyncIterator[Any]:
    """Create a fresh server-owned client operation for detached adapter work."""

    supervisor = client._collaborators.call_supervisor
    operation_scope = getattr(supervisor, "operation_scope", None)
    if operation_scope is None:
        async with client.operation(timeout=timeout) as lease:
            yield lease
        return
    async with operation_scope(
        "detached adapter operation", timeout=timeout, expected_epoch=expected_epoch
    ) as lease:
        yield lease


def _detached_adapter_context():
    """Clear request-owned operation/replay state in a detached adapter task."""

    return detached_operation_context()


__all__ = [
    "DEFAULT_SERVER_KEEPALIVE_INTERVAL",
    "LOOPBACK_HOSTNAMES",
    "LoopBoundPrimitive",
    "addr_is_loopback",
    "check_bind_allowed",
    "client_generation_epoch",
    "host_header_is_loopback",
    "is_loopback",
    "redact",
]
