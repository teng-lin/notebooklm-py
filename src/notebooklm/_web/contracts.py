"""Type-only contracts shared by web feature and transport modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from ..rpc.types import RPCMethod


class Kernel(Protocol):
    """Pure transport surface implemented by the concrete web kernel."""

    async def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        read_timeout: float | None = None,
        max_response_bytes: int | None = None,
        expected_epoch: int | None = None,
    ) -> httpx.Response: ...

    def get_http_client(self, *, expected_epoch: int | None = None) -> httpx.AsyncClient: ...

    @property
    def cookies(self) -> httpx.Cookies: ...

    async def aclose(self) -> None: ...


class RpcCaller(Protocol):
    """Narrow RPC dispatch surface consumed by web feature APIs."""

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        read_timeout: float | None = None,
        raise_on_null_status: bool = False,
    ) -> Any: ...


__all__ = ["Kernel", "RpcCaller"]
