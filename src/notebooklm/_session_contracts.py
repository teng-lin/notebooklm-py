"""Type-only contracts for the Tier-13 Session/Kernel split.

This module defines the narrow structural Protocols that later Tier-13 PRs
will wire into concrete classes. It intentionally contains no runtime
implementation and no import of the concrete ``Session``.

``Session.rpc_call`` deliberately mirrors the legacy feature RPC signature,
including the transitional ``_is_retry`` parameter, so feature retyping can
happen without changing call semantics.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import httpx

from ._request_types import BuildRequest
from .rpc.types import RPCMethod


class AuthMetadata(Protocol):
    """Selected-account routing metadata required by upload flows."""

    @property
    def authuser(self) -> int: ...

    @property
    def account_email(self) -> str | None: ...


class Kernel(Protocol):
    """Pure transport surface owned by the concrete Kernel in PR 13.2."""

    async def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> httpx.Response: ...

    @property
    def cookies(self) -> httpx.Cookies: ...

    async def aclose(self) -> None: ...


class Session(Protocol):
    """Orchestration surface consumed by feature APIs after Tier 13."""

    @property
    def auth(self) -> AuthMetadata: ...

    @property
    def kernel(self) -> Kernel: ...

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
    ) -> Any: ...

    async def transport_post(
        self,
        build_request: BuildRequest,
        parse_label: str,
        *,
        disable_internal_retries: bool = False,
    ) -> httpx.Response: ...

    async def next_reqid(self, step: int = 100000) -> int: ...

    def assert_bound_loop(self) -> None: ...

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]: ...

    def register_drain_hook(
        self,
        name: str,
        hook: Callable[[], Awaitable[None]],
    ) -> None: ...


class DrainHookRegistration(Protocol):
    """Narrow close-time hook registration surface for Artifacts."""

    def register_drain_hook(
        self,
        name: str,
        hook: Callable[[], Awaitable[None]],
    ) -> None: ...


__all__ = [
    "AuthMetadata",
    "DrainHookRegistration",
    "Kernel",
    "Session",
]
