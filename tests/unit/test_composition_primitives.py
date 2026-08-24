"""Tests for the single production client composition path."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext

import pytest

from notebooklm._rpc_semaphore import RpcSemaphore
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


def test_public_construction_publishes_one_complete_runtime() -> None:
    client = NotebookLMClient(_make_auth(), max_concurrent_rpcs=3)
    backend = client._backend

    assert backend.runtime_ready
    assert backend._runtime is client.notebooks._legacy_rpc
    assert backend._chat_transport is backend._runtime._transport
    assert backend._pipeline is backend._runtime._transport.pipeline
    assert client._provider._rpc_semaphore.max_concurrent_rpcs == 3
    assert not hasattr(client, "_seams")
    assert not hasattr(backend, "_chain_host")
    assert not hasattr(client, "_composed")
    assert not hasattr(client, "_collaborators")
    assert not hasattr(client, "_rpc_executor")


def test_invalid_max_concurrent_rpcs_rejected_before_publication() -> None:
    with pytest.raises(ValueError, match="max_concurrent_rpcs must be >= 1, got 0"):
        NotebookLMClient(_make_auth(), max_concurrent_rpcs=0)


def test_public_composition_refuses_synthetic_error_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD_ERRORS", "5xx")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm._core"),
        pytest.raises(RuntimeError, match="NOTEBOOKLM_VCR_RECORD_ERRORS"),
    ):
        NotebookLMClient(_make_auth())


def test_runtime_rpc_semaphore_unbounded_path() -> None:
    owner = RpcSemaphore(None)
    assert isinstance(owner.get(), type(nullcontext()))


def test_runtime_rpc_semaphore_rebind_discards_stale_primitive() -> None:
    owner = RpcSemaphore(2)

    async def bind_and_build() -> None:
        owner.set_bound_loop(asyncio.get_running_loop())
        async with owner.get():
            pass

    asyncio.run(bind_and_build())
    first = owner._semaphore
    assert first is not None

    async def rebind() -> None:
        owner.set_bound_loop(asyncio.get_running_loop())
        assert owner._semaphore is None
        async with owner.get():
            pass

    asyncio.run(rebind())
    assert owner._semaphore is not first
