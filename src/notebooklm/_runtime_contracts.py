"""Type-only contracts shared across feature APIs.

This module defines the narrow structural Protocols feature APIs depend
on. Per ADR-013, a Protocol lives here only when **shared by ≥2
features**; single-consumer capabilities (e.g. artifact polling's
``register_drain_hook``) stay local to their owning feature module.

Contents:

* :class:`AuthMetadata` and :class:`Kernel` — selected-account routing
  metadata + pure transport surface consumed by the upload pipeline.
* :class:`RpcCaller`, :class:`LoopGuard`, :class:`OperationScopeProvider`,
  :class:`AsyncWorkRuntime` — the four shared capability Protocols
  promoted in Phase 1 of the capability refactor.

The broad ``Session`` Protocol that previously bundled all of these
together was deleted in Phase 7 (refactor-history.md §Migration Plan step 10).
Feature APIs that need more than one capability take their direct
collaborators by keyword-only constructor argument (``ChatAPI`` in
``_chat.py``, ``ArtifactsAPI`` in ``_artifacts.py``, and
``SourceUploadPipeline`` in ``_source_upload.py``). The feature-local
composite Protocols ``ArtifactsRuntime`` and ``UploadRuntime`` (and
their corresponding adapter dataclasses) that previously bundled three
capability Protocols apiece were retired once it was clear they only
hid three stable collaborators with exactly one production satisfier;
the surviving narrow Protocols (``RpcCaller``, ``LoopGuard``,
``OperationScopeProvider``, ``AsyncWorkRuntime``) here continue to
describe the per-capability shapes feature constructors consume.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import httpx

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


class RpcCaller(Protocol):
    """Narrow RPC dispatch surface consumed by pure-RPC feature APIs.

    Mirrors the legacy ``Session.rpc_call`` signature exactly so feature
    retypes do not change call semantics. The transitional
    ``_is_retry`` parameter and the keyword-only
    ``disable_internal_retries`` / ``operation_variant`` parameters are
    preserved as-is.

    ``NotebookLMClient`` and ``RpcExecutor`` structurally satisfy this
    Protocol; features that only need to issue RPC calls depend on this
    narrow surface so they are not coupled to
    transport, loop affinity, or close-time-hook concerns.
    """

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


class LoopGuard(Protocol):
    """Loop-affinity assertion surface for features that own async work."""

    def assert_bound_loop(self) -> None: ...


class OperationScopeProvider(Protocol):
    """``operation_scope`` async-context-manager surface for feature APIs."""

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]: ...


class AsyncWorkRuntime(LoopGuard, OperationScopeProvider, Protocol):
    """Runtime support for feature-owned async work."""


__all__ = [
    "AsyncWorkRuntime",
    "AuthMetadata",
    "Kernel",
    "LoopGuard",
    "OperationScopeProvider",
    "RpcCaller",
]
