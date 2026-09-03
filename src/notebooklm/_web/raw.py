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
        disable_internal_retries: bool = False,
        read_timeout: float | None = None,
        raise_on_null_status: bool = False,
    ) -> Any:
        """Dispatch one Web method without narrowing executor behavior."""

        return await self._rpc.rpc_call(
            method=method,
            params=params,
            allow_null=allow_null,
            disable_internal_retries=disable_internal_retries,
            read_timeout=read_timeout,
            raise_on_null_status=raise_on_null_status,
        )


__all__ = ["WebRawAPI"]
