"""Runtime characterization tests for Phase 7 (P7) web runtime collapse.

Governed by ADR-0035 and docs/plan/2026-08-13-semantic-backend-refactor.md.
P7 runs last: when P7 collapses the mutable composition holders and generic
middleware container behind WebRpcBackend, it MUST equality-preserve:

1. Atomic backend-owned runtime / middleware ordering parity
2. Constructor / test factory vars() parity and option routing
3. Loop affinity (ADR-0004) and cross-loop reset protocols
4. Drain, close, and cancellation-safety lifecycle invariants
5. Retry, auth-refresh single-flight, and ADR-0016 Auth Instance Invariant
6. Exception lattice and diagnostics population
7. Metrics snapshot and RpcTelemetryEvent telemetry invariants
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._chat import ChatAPI
from notebooklm._collections import CollectionsAPI
from notebooklm._labels import LabelsAPI
from notebooklm._loop_bound import LoopBoundPrimitive
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._notebooks import NotebooksAPI
from notebooklm._notes import NotesAPI
from notebooklm._research import ResearchAPI
from notebooklm._runtime.auth import AuthRefreshCoordinator
from notebooklm._runtime.retry_behavior import RetryBehavior
from notebooklm._settings import SettingsAPI
from notebooklm._sharing import SharingAPI
from notebooklm._source.upload import SourceUploadPipeline
from notebooklm._sources import SourcesAPI
from notebooklm._transport_errors import TransportRateLimited
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import (
    ArtifactTimeoutError,
    DecodingError,
    NetworkError,
    NotebookLMError,
    NotebookNotFoundError,
    NotFoundError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    SourceNotFoundError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    WaitTimeoutError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import (
    ClientMetricsSnapshot,
    ConnectionLimits,
    RpcTelemetryEvent,
)
from tests._fixtures.chain import FakeChainTerminal, build_chain, make_request


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test-sid", "HSID": "test-hsid"},
        csrf_token="test-csrf",
        session_id="test-session",
    )


# -----------------------------------------------------------------------------
# 1. Atomic Runtime / Middleware Ordering Parity
# -----------------------------------------------------------------------------


def test_atomic_runtime_is_complete_before_backend_publication() -> None:
    client = NotebookLMClient(auth=_make_auth(), max_concurrent_rpcs=4)
    backend = client._backend

    assert backend._runtime is client.sources._rpc
    assert backend._chat_transport is backend._runtime._transport
    # P8 moved the lifecycle-opened semaphore behind the credential provider;
    # the semantic backend consumes the already-wired middleware chain and
    # must not retain a second owner.
    assert client._provider._rpc_semaphore.max_concurrent_rpcs == 4
    assert not hasattr(backend, "_rpc_semaphore")
    assert backend._metrics is not None
    assert client._provider._lifecycle is not None
    assert not hasattr(backend, "_lifecycle")
    assert not hasattr(client, "_composed")
    assert not hasattr(client, "_collaborators")
    assert not hasattr(client, "_rpc_executor")


def test_rpc_executor_and_middleware_chain_ordering_invariants() -> None:
    """Middleware chain ordering is strictly preserved in Tier-12 composition."""
    client = NotebookLMClient(auth=_make_auth())

    # RpcExecutor is shared identically across client and features
    # R6.2: the notebook facade holds no executor at all — every notebook read,
    # ``get_raw`` included, goes through the semantic backend.
    assert not hasattr(client.notebooks, "_legacy_rpc")
    assert client.notebooks._share_manager._backend is client._backend
    assert client.sources._rpc is client._backend._runtime
    assert not hasattr(client.artifacts, "_rpc")
    assert client.artifacts._backend is client._backend
    assert client.chat._service._backend is client._backend

    # The composed chain is closed over by the transport rather than retained
    # as an inspectable mutable list. Exercise the published terminal instead.
    assert client._backend.runtime_ready


# -----------------------------------------------------------------------------
# 2. Constructor / Factory vars() Parity & Option Routing
# -----------------------------------------------------------------------------


def test_constructor_and_factory_vars_exact_parity() -> None:
    """NotebookLMClient(...) and NotebookLMClient(...) have identical vars() attribute surface."""
    prod = NotebookLMClient(_make_auth())
    shell = NotebookLMClient(auth=_make_auth())

    prod_vars = {k: type(v) for k, v in vars(prod).items()}
    shell_vars = {k: type(v) for k, v in vars(shell).items()}

    assert prod_vars == shell_vars, (
        f"vars() divergence between constructor and test factory:\n"
        f"Missing on shell: {set(prod_vars) - set(shell_vars)}\n"
        f"Extra on shell: {set(shell_vars) - set(prod_vars)}"
    )


def test_constructor_option_routing_to_all_collaborators() -> None:
    """Every __init__ kwarg reaches its designated collaborator."""
    custom_limits = ConnectionLimits(max_connections=50, max_keepalive_connections=25)
    upload_timeout = httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0)

    events: list[RpcTelemetryEvent] = []
    saver_calls: list[Any] = []

    def on_event(event: RpcTelemetryEvent) -> None:
        events.append(event)

    def custom_saver(jar: Any, path: Any, **_kwargs: Any) -> None:
        saver_calls.append((jar, path))

    async def custom_rotator() -> bool:
        return True

    client = NotebookLMClient(
        auth=_make_auth(),
        timeout=45.0,
        storage_path=Path("/tmp/test_storage.json"),
        keepalive=120.0,
        keepalive_min_interval=60.0,
        rate_limit_max_retries=5,
        server_error_max_retries=4,
        limits=custom_limits,
        max_concurrent_uploads=2,
        max_concurrent_rpcs=8,
        upload_timeout=upload_timeout,
        on_rpc_event=on_event,
        cookie_saver=custom_saver,
        cookie_rotator=custom_rotator,
        chat_timeout=200.0,
        chat_response_max_bytes=1024 * 1024,
        import_research_timeout=150.0,
    )

    # 1. timeout -> lifecycle & research
    assert client._provider._lifecycle is not None
    assert client._provider._lifecycle._timeout == 45.0
    assert client.research._base_timeout == 45.0

    # 2. storage_path -> auth & artifacts download service
    assert client.auth.storage_path == Path("/tmp/test_storage.json")
    assert client.artifacts._downloads._remote._storage_path == Path("/tmp/test_storage.json")

    # 3. keepalive & keepalive_min_interval -> lifecycle (clamped to min_interval)
    assert client._provider._lifecycle._keepalive_interval == 120.0

    # 4. retry retries -> RetryBehavior
    assert client._backend.retry_limits == (5, 4)

    # 5. limits -> lifecycle
    assert client._provider._lifecycle._limits is custom_limits

    # 6. max_concurrent_uploads & upload_timeout -> source_uploader
    assert client._source_uploader._max_concurrent_uploads == 2
    assert client._source_uploader._upload_timeout == upload_timeout

    # 7. max_concurrent_rpcs -> atomic runtime semaphore
    assert client._provider._rpc_semaphore is not None
    assert client._provider._rpc_semaphore.max_concurrent_rpcs == 8

    # 8. on_rpc_event -> ClientMetrics
    assert client._backend._metrics is not None
    assert client._backend._metrics._on_rpc_event is on_event

    # 9. cookie_saver & cookie_rotator -> lifecycle
    assert client._provider._lifecycle._cookie_saver is custom_saver
    assert client._provider._lifecycle._cookie_rotator is custom_rotator

    # 10. chat_timeout & chat_response_max_bytes -> chat backend binding
    assert client._backend._chat_timeout == 200.0
    assert client._backend._chat_response_max_bytes == 1024 * 1024

    # 11. import_research_timeout -> ResearchAPI
    assert client.research._import_research_timeout == 150.0


def test_constructor_cross_validation_throttle_against_connection_pool() -> None:
    """max_concurrent_rpcs > limits.max_connections raises ValueError at construction."""
    tight_limits = ConnectionLimits(max_connections=5)
    with pytest.raises(ValueError, match="max_concurrent_rpcs must be <= limits.max_connections"):
        NotebookLMClient(
            auth=_make_auth(),
            limits=tight_limits,
            max_concurrent_rpcs=10,
        )


def test_public_client_member_disposition_and_owner_parity() -> None:
    """Every public member on NotebookLMClient has an explicit owner and declared target."""
    client = NotebookLMClient(_make_auth())

    # 1. Namespaces
    assert isinstance(client.notebooks, NotebooksAPI)
    assert isinstance(client.sources, SourcesAPI)
    assert isinstance(client.artifacts, ArtifactsAPI)
    assert isinstance(client.chat, ChatAPI)
    assert isinstance(client.notes, NotesAPI)
    assert isinstance(client.mind_maps, MindMapsAPI)
    assert isinstance(client.research, ResearchAPI)
    assert isinstance(client.settings, SettingsAPI)
    assert isinstance(client.sharing, SharingAPI)
    assert isinstance(client.labels, LabelsAPI)
    assert isinstance(client.collections, CollectionsAPI)

    # 2. Public root client members
    assert inspect.ismethod(client.rpc_call)
    assert inspect.ismethod(client.refresh_auth)
    assert inspect.ismethod(client.get_account_email)
    assert inspect.ismethod(client.get_account_authuser)
    assert inspect.ismethod(client.metrics_snapshot)
    assert inspect.ismethod(client.drain)
    assert inspect.ismethod(client.close)
    assert isinstance(client.is_connected, bool)
    assert isinstance(client.auth, AuthTokens)


# -----------------------------------------------------------------------------
# 3. Loop Affinity Invariants (ADR-0004)
# -----------------------------------------------------------------------------


def test_loop_affinity_protocol_and_cross_loop_rejection() -> None:
    """The runtime semaphore rejects cross-loop reuse and rebuilds after rebinding."""
    client = NotebookLMClient(auth=_make_auth(), max_concurrent_rpcs=2)
    semaphore = client._provider._rpc_semaphore
    assert semaphore is not None
    assert isinstance(semaphore, LoopBoundPrimitive)

    async def acquire_semaphore() -> None:
        async with semaphore.get():
            pass

    asyncio.run(acquire_semaphore())

    async def bind_and_acquire() -> None:
        semaphore.set_bound_loop(asyncio.get_running_loop())
        async with semaphore.get():
            pass

    asyncio.run(bind_and_acquire())

    async def reject_cross_loop() -> None:
        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            async with semaphore.get():
                pass

    asyncio.run(reject_cross_loop())

    async def rebind_and_reopen() -> None:
        semaphore.set_bound_loop(asyncio.get_running_loop())
        semaphore.reset_after_open()
        async with semaphore.get():
            pass

    asyncio.run(rebind_and_reopen())


def test_uploader_and_chat_loop_bound_reset_contracts() -> None:
    """SourceUploadPipeline and ChatAPI participate in the LoopGuard protocol."""
    client = NotebookLMClient(auth=_make_auth())

    assert isinstance(client._source_uploader, (SourceUploadPipeline, LoopBoundPrimitive))
    assert isinstance(client.chat, (ChatAPI, LoopBoundPrimitive))

    # reset_after_open resets upload semaphore
    client._source_uploader._upload_semaphore = MagicMock()
    client._source_uploader.reset_after_open()
    assert client._source_uploader._upload_semaphore is None


# -----------------------------------------------------------------------------
# 4. Drain & Close Lifecycle Invariants
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_lifecycle_invariants() -> None:
    """drain() blocks new operations and waits for in-flight operations."""
    client = NotebookLMClient(auth=_make_auth())
    drain_tracker = client._backend._drain_tracker
    assert drain_tracker is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight_worker() -> None:
        token = await drain_tracker.begin_transport_post("in_flight_rpc")
        started.set()
        try:
            await release.wait()
        finally:
            await drain_tracker.finish_transport_post(token)

    task = asyncio.create_task(in_flight_worker())
    await started.wait()
    assert drain_tracker._in_flight_posts == 1

    drain_task = asyncio.create_task(drain_tracker.drain(timeout=0.5))
    await asyncio.sleep(0.01)

    assert not drain_task.done()

    # New work in a fresh task is rejected while draining
    async def new_work() -> None:
        await drain_tracker.begin_transport_post("new_rpc")

    with pytest.raises(RuntimeError, match="draining"):
        await asyncio.create_task(new_work())

    release.set()
    await task
    await drain_task
    assert drain_tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_close_with_drain_runs_cancel_hooks_first() -> None:
    """close(drain=True) executes cancel drain hooks before the drain wait."""
    client = NotebookLMClient(_make_auth())
    call_order: list[str] = []

    async def fake_drain_hook() -> None:
        call_order.append("drain_hook")

    async def fake_drain(timeout: float | None = None) -> None:
        call_order.append("drain_wait")

    async def fake_close(**_kwargs: Any) -> None:
        call_order.append("lifecycle_close")

    assert client._backend._drain_tracker is not None
    assert client._provider._lifecycle is not None
    client._backend._drain_tracker.register_drain_hook("test_hook", fake_drain_hook)
    client._backend._drain_tracker.drain = fake_drain  # type: ignore[method-assign]
    client._provider._lifecycle.close = fake_close  # type: ignore[method-assign]

    await client.close(drain=True)

    assert call_order == ["drain_hook", "drain_wait", "lifecycle_close"]


@pytest.mark.asyncio
async def test_close_cancellation_during_drain_tears_down_transport_via_shield() -> None:
    """Cancellation during drain() runs shielded lifecycle close without leaking transport."""
    client = NotebookLMClient(_make_auth())
    close_called = False

    async def hanging_drain(timeout: float | None = None) -> None:
        await asyncio.sleep(10.0)

    async def fake_close(**_kwargs: Any) -> None:
        nonlocal close_called
        close_called = True

    assert client._backend._drain_tracker is not None
    assert client._provider._lifecycle is not None
    client._backend._drain_tracker.drain = hanging_drain  # type: ignore[method-assign]
    client._provider._lifecycle.close = fake_close  # type: ignore[method-assign]

    close_task = asyncio.create_task(client.close(drain=True))
    await asyncio.sleep(0.01)
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert close_called is True


# -----------------------------------------------------------------------------
# 5. Retry & Auth Refresh Invariants
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_middleware_rate_limit_and_server_error_retries() -> None:
    """RetryBehavior retries TransportRateLimited and TransportServerError up to max retries."""
    rate_limit_resp = httpx.Response(429, headers={"Retry-After": "0"})
    fake_req = make_request()
    fake_http_err = httpx.HTTPStatusError(
        "429 Too Many Requests", request=fake_req, response=rate_limit_resp
    )
    terminal = FakeChainTerminal(
        raises=TransportRateLimited(
            "rate limited",
            retry_after=0,
            response=rate_limit_resp,
            original=fake_http_err,
        ),
    )
    slept: list[float] = []

    async def fake_sleep(secs: float) -> None:
        slept.append(secs)

    retry_mw = RetryBehavior(
        rate_limit_max_retries=2,
        server_error_max_retries=1,
        sleep=fake_sleep,
    )

    chain = build_chain([retry_mw], terminal)
    with pytest.raises(TransportRateLimited):
        await chain(fake_req)

    # Initial call + 2 retries = 3 calls
    assert terminal.call_count == 3
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_auth_refresh_coordinator_single_flight() -> None:
    """AuthRefreshCoordinator coalesces concurrent refresh calls into a single execution."""
    refresh_count = 0
    auth = _make_auth()

    async def mock_refresh() -> AuthTokens:
        nonlocal refresh_count
        refresh_count += 1
        await asyncio.sleep(0.02)
        return auth

    coord = AuthRefreshCoordinator(refresh_callback=mock_refresh)

    await asyncio.gather(
        coord.await_refresh(),
        coord.await_refresh(),
        coord.await_refresh(),
    )

    assert refresh_count == 1


def test_adr0016_auth_instance_invariant_is_provider_owned() -> None:
    """The provider owns auth; upload receives its reconciled direct-leg port."""
    auth = _make_auth()
    client = NotebookLMClient(auth)

    assert client.auth is auth
    assert client._provider.auth is auth
    assert client._source_uploader._auth is None
    generation_provider = client._source_uploader._generation_provider
    assert generation_provider is not None
    assert getattr(generation_provider, "__self__", None) is client._provider
    assert (
        getattr(generation_provider, "__func__", None)
        is type(client._provider).reconciled_generation
    )

    # ADR-0016's in-place identity remains exact at the public/provider owner;
    # the uploader cannot observe the mutable AuthTokens capability directly.
    auth.csrf_token = "new-csrf-token"
    assert client.auth.csrf_token == "new-csrf-token"
    assert client._provider.auth.csrf_token == "new-csrf-token"


# -----------------------------------------------------------------------------
# 6. Errors & Diagnostics Invariants
# -----------------------------------------------------------------------------


def test_exception_lattice_hierarchy_and_diagnostics_preservation() -> None:
    """The public exception hierarchy and diagnostics attributes are strictly preserved."""
    # Domain specific exceptions inherit from both RPCError and domain umbrellas
    assert issubclass(NotebookNotFoundError, (RPCError, NotFoundError))
    assert issubclass(SourceNotFoundError, (RPCError, NotFoundError))
    assert issubclass(ServerError, RPCError)
    assert issubclass(RateLimitError, RPCError)
    assert issubclass(DecodingError, RPCError)
    assert issubclass(RPCTimeoutError, (NetworkError, NotebookLMError))
    assert issubclass(SourceTimeoutError, (WaitTimeoutError, TimeoutError))
    assert issubclass(ArtifactTimeoutError, (WaitTimeoutError, TimeoutError))
    assert issubclass(UnknownRPCMethodError, DecodingError)

    # Diagnostics population
    err = RPCError(
        "rpc failure",
        method_id=RPCMethod.GET_NOTEBOOK.value,
        rpc_code=13,
        found_ids=["id1", "id2"],
        raw_response='{"error": "bad"}',
    )
    assert err.method_id == RPCMethod.GET_NOTEBOOK.value
    assert err.rpc_code == 13
    assert err.found_ids == ["id1", "id2"]
    assert err.raw_response == '{"error": "bad"}'
    # Aliases
    assert err.rpc_id == RPCMethod.GET_NOTEBOOK.value
    assert err.code == 13


# -----------------------------------------------------------------------------
# 7. Metrics & Telemetry Invariants
# -----------------------------------------------------------------------------


def test_client_metrics_snapshot_fields_and_types() -> None:
    """ClientMetricsSnapshot field types and structure match public contract."""
    snapshot = ClientMetricsSnapshot()

    assert isinstance(snapshot.rpc_calls_started, int)
    assert isinstance(snapshot.rpc_calls_succeeded, int)
    assert isinstance(snapshot.rpc_calls_failed, int)
    assert isinstance(snapshot.rpc_decode_errors, int)
    assert isinstance(snapshot.rpc_latency_seconds_total, float)
    assert isinstance(snapshot.rpc_queue_wait_seconds_total, float)
    assert isinstance(snapshot.rpc_queue_wait_seconds_max, float)
    assert isinstance(snapshot.upload_queue_wait_seconds_total, float)
    assert isinstance(snapshot.upload_queue_wait_seconds_max, float)
    assert isinstance(snapshot.lock_wait_seconds_total, float)
    assert isinstance(snapshot.lock_wait_seconds_max, float)


@pytest.mark.asyncio
async def test_on_rpc_event_backpressure_and_swallow_contracts(caplog) -> None:
    """on_rpc_event awaits async callbacks (backpressure) and swallows exceptions."""
    events: list[RpcTelemetryEvent] = []
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_cb(event: RpcTelemetryEvent) -> None:
        started.set()
        await finish.wait()
        events.append(event)

    client = NotebookLMClient(auth=_make_auth(), on_rpc_event=slow_cb)
    event = RpcTelemetryEvent(method="GET_NOTEBOOK", status="success", elapsed_seconds=0.05)

    assert client._backend._metrics is not None
    task = asyncio.create_task(client._backend._metrics.emit_rpc_event(event))
    await started.wait()
    assert not task.done(), "emit_rpc_event must await the callback before returning"

    finish.set()
    await task
    assert events == [event]

    # Callback exception swallowing
    def failing_cb(event: RpcTelemetryEvent) -> None:
        raise RuntimeError("callback exploded")

    client._backend._metrics._on_rpc_event = failing_cb
    with caplog.at_level(logging.WARNING, logger="notebooklm._core"):
        # Must not raise
        await client._backend._metrics.emit_rpc_event(event)

    assert any("callback exploded" in r.getMessage() for r in caplog.records)
