"""Raw Web ``batchexecute`` adapter."""

from __future__ import annotations

from typing import Any

from ..rpc import RPCMethod


class WebRawAPI:
    """Raw Web calls through the client's normal executor."""

    def __init__(self, rpc: Any) -> None:
        self._rpc = rpc

    async def call(
        self,
        method: RPCMethod,
        params: list[Any],
        *,
        allow_null: bool = False,
        read_timeout: float | None = None,
    ) -> Any:
        """Dispatch one Web method without changing executor behavior."""

        return await self._rpc.rpc_call(
            method=method,
            params=params,
            allow_null=allow_null,
            read_timeout=read_timeout,
        )


__all__ = ["WebRawAPI"]
