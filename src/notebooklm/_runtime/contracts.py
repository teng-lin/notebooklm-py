"""Type-only contracts shared across feature APIs.

This module defines the narrow structural Protocols feature APIs depend
on. Per ADR-0013, a Protocol lives here only when **shared by ≥2
features**; single-consumer capabilities stay local to their owning
feature module (e.g. ``AuthMetadata`` lives in ``_source/upload.py`` and
``OperationScopeProvider`` lives in ``_artifact/polling.py``, each with a
single consumer).

Contents:

* :class:`Kernel` — pure transport surface consumed by the upload
  pipeline (and structurally satisfied by the concrete ``Kernel``).
* :class:`RpcCaller` (~17 consumers) and :class:`LoopGuard` (2
  consumers) — the surviving shared capability Protocols that meet the
  ADR-0013 ≥2-feature bar.

Feature APIs that need more than one capability take their direct
collaborators by keyword-only constructor argument (``ChatAPI`` in
``_chat/api.py``, ``ArtifactsAPI`` in ``_artifacts.py``, and
``SourceUploadPipeline`` in ``_source/upload.py``). The feature-local
composite Protocols ``ArtifactsRuntime`` and ``UploadRuntime`` (and
their corresponding adapter dataclasses) that previously bundled three
capability Protocols apiece were retired once it was clear they only
hid three stable collaborators with exactly one production satisfier.
The single-consumer ``AuthMetadata`` / ``OperationScopeProvider`` and
the unused ``AsyncWorkRuntime`` composite were inlined / deleted in
issue #1327 for the same reason — a Protocol with fewer than two
production consumers is indirection that no production code varies.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from ..rpc.types import RPCMethod


class Kernel(Protocol):
    """Pure transport surface owned by the concrete Kernel in PR 13.2."""

    async def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        read_timeout: float | None = None,
    ) -> httpx.Response: ...

    @property
    def cookies(self) -> httpx.Cookies: ...

    async def aclose(self) -> None: ...


class RpcCaller(Protocol):
    """Narrow RPC dispatch surface consumed by pure-RPC feature APIs.

    Mirrors the ``NotebookLMClient.rpc_call`` signature exactly so feature
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


__all__ = [
    "Kernel",
    "LoopGuard",
    "RpcCaller",
]
