"""Tests for :class:`UploadRuntimeAdapter` (ADR-014 Rule 2).

Pins the adapter's three contracts:

1. Structural — the frozen dataclass satisfies the
   :class:`UploadRuntime` Protocol it adapts (also enforced statically
   by the ``TYPE_CHECKING`` assertion at the bottom of
   ``_source_upload.py``).
2. Delegation — each of the three delegate methods forwards to the
   right held collaborator with the right positional/keyword shape.
3. Immutability — the adapter is a ``@dataclass(frozen=True)`` so its
   field set is fixed at construction; no post-construction mutation
   can re-bind ``rpc`` / ``drain`` / ``lifecycle``.

Wave 9 of the session-decoupling plan introduced this adapter so
:class:`SourceUploadPipeline` stops receiving a whole ``Session`` and
instead receives a narrow composite built from
``session.rpc_executor`` + ``coll.drain_tracker`` + ``coll.lifecycle``
at the composition root. :class:`SourceUploadPipeline` still takes
:class:`Kernel` and :class:`AuthMetadata` as separate parameters, so
this adapter covers only the composite ``UploadRuntime`` part.
"""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._source_upload import UploadRuntime, UploadRuntimeAdapter
from notebooklm.rpc import RPCMethod


def _make_adapter(
    *,
    rpc: Any = None,
    drain: Any = None,
    lifecycle: Any = None,
) -> UploadRuntimeAdapter:
    return UploadRuntimeAdapter(
        rpc=rpc if rpc is not None else MagicMock(),
        drain=drain if drain is not None else MagicMock(),
        lifecycle=lifecycle if lifecycle is not None else MagicMock(),
    )


def test_adapter_is_a_frozen_dataclass() -> None:
    """ADR-014 Rule 2 mandates frozen-dataclass adapter shape."""
    assert dataclasses.is_dataclass(UploadRuntimeAdapter)
    # ``frozen=True`` forbids re-binding fields after construction.
    adapter = _make_adapter()
    with pytest.raises(dataclasses.FrozenInstanceError):
        adapter.rpc = MagicMock()  # type: ignore[misc]


def test_adapter_structurally_satisfies_upload_runtime() -> None:
    """Static-analysis pin: the adapter is assignable to the Protocol.

    Pins the same contract the ``TYPE_CHECKING`` mypy guard at the
    bottom of ``_source_upload.py`` pins at static-analysis time.
    Python does not enforce type annotations at runtime, so the
    assignment itself is a no-op — the contract bites at mypy time on
    the annotation, not at runtime. This test exists alongside the
    delegate-behaviour tests below to keep the Protocol-satisfaction
    intent visible in the suite even on a CI without mypy enabled
    (the annotation would still surface as a syntax/import error if
    the Protocol moved or was renamed).

    Runtime structural verification would require
    ``@runtime_checkable`` plus ``isinstance``, which the Protocol
    intentionally is not — per the project's "prefer mypy + signature
    pins" rule of thumb (gemini-code-assist guidance, Wave 9).
    """
    adapter = _make_adapter()
    runtime: UploadRuntime = adapter
    assert runtime is adapter


@pytest.mark.asyncio
async def test_rpc_call_forwards_to_rpc_collaborator() -> None:
    """``adapter.rpc_call(...)`` proxies the full signature to ``rpc``.

    The mock for the held collaborator is constructed with its
    ``rpc_call`` attribute set at construction time (via the
    ``MagicMock`` kwarg form), not assigned post-construction; the
    ADR-007 lint rejects ``<chain>.rpc_call = AsyncMock(...)`` because
    that pattern signals mutating an instance under test, which is not
    what this adapter test is doing — but we use the constructor form
    so the lint doesn't even need to reason about intent.
    """
    rpc_call = AsyncMock(return_value="sentinel-result")
    rpc = MagicMock(rpc_call=rpc_call)
    adapter = _make_adapter(rpc=rpc)

    result = await adapter.rpc_call(
        RPCMethod.ADD_SOURCE_FILE,
        ["params"],
        source_path="/notebook/abc",
        allow_null=True,
        _is_retry=True,
        disable_internal_retries=True,
        operation_variant="variant-X",
    )

    assert result == "sentinel-result"
    rpc_call.assert_awaited_once_with(
        RPCMethod.ADD_SOURCE_FILE,
        ["params"],
        "/notebook/abc",
        True,
        True,
        disable_internal_retries=True,
        operation_variant="variant-X",
    )


@pytest.mark.asyncio
async def test_rpc_call_defaults_match_protocol_signature() -> None:
    """Default values match :meth:`RpcCaller.rpc_call` exactly."""
    rpc_call = AsyncMock(return_value=None)
    rpc = MagicMock(rpc_call=rpc_call)
    adapter = _make_adapter(rpc=rpc)

    await adapter.rpc_call(RPCMethod.ADD_SOURCE_FILE, [])

    rpc_call.assert_awaited_once_with(
        RPCMethod.ADD_SOURCE_FILE,
        [],
        "/",
        False,
        False,
        disable_internal_retries=False,
        operation_variant=None,
    )


def test_operation_scope_forwards_to_drain_collaborator() -> None:
    """``operation_scope(label)`` returns the drain's async ctx manager.

    Identity matters: the adapter must not wrap the returned context
    manager, or callers that ``async with`` it would see a different
    object than the underlying drain produced.
    """

    @asynccontextmanager
    async def _scope():
        yield None

    sentinel_cm = _scope()
    drain = MagicMock()
    drain.operation_scope = MagicMock(return_value=sentinel_cm)
    adapter = _make_adapter(drain=drain)

    result = adapter.operation_scope("upload source xyz")

    assert result is sentinel_cm
    drain.operation_scope.assert_called_once_with("upload source xyz")


def test_assert_bound_loop_forwards_to_lifecycle_collaborator() -> None:
    """``assert_bound_loop()`` proxies to lifecycle (no args, no return)."""
    lifecycle = MagicMock()
    lifecycle.assert_bound_loop = MagicMock(return_value=None)
    adapter = _make_adapter(lifecycle=lifecycle)

    result = adapter.assert_bound_loop()

    assert result is None
    lifecycle.assert_bound_loop.assert_called_once_with()


def test_adapter_does_not_expose_register_drain_hook() -> None:
    """``UploadRuntime`` does NOT include ``DrainHookRegistration``.

    The artifacts feature registers a close-time drain hook (artifact
    polling cancellation); the upload pipeline does not. Pinning the
    absence here documents the divergence between the two adapter
    shapes and catches any future drift that silently expands the
    upload contract.
    """
    adapter = _make_adapter()
    assert not hasattr(adapter, "register_drain_hook")
