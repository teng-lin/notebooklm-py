"""Unit tests for :mod:`notebooklm._session_lifecycle`.

Covers the load-bearing behaviors of :class:`ClientLifecycle` directly, in
addition to the existing ``Session``-shaped tests in
``test_session_close.py`` / ``test_client_keepalive.py`` / ``test_vcr_config.py``
which exercise the same helper through the compat facade.

Specifically pinned here:

* :meth:`ClientLifecycle.open` is **idempotent** — a second call while the
  client is already open is a no-op (the first ``httpx.AsyncClient`` instance
  is preserved).
* :meth:`ClientLifecycle.close` **cancels and awaits the keepalive task
  cleanly** — the task exits and is set to ``None``; the call doesn't leak a
  ``CancelledError``.
* ``_bound_loop`` **mismatch raises ``RuntimeError``** — the cross-loop guard
  in :meth:`SessionTransport.perform_authed_post` reads ``_bound_loop`` through
  the lifecycle and raises actionably when the loops differ.
* :meth:`ClientLifecycle.save_cookies` **invokes** the
  :class:`CookiePersistence` collaborator's ``save`` method with the right
  ``jar`` and ``path`` arguments AND with the ``save_cookies_to_storage``
  value resolved from ``notebooklm._core`` at call time (so the monkeypatch
  surface keeps working).
* The httpx ``AsyncClient`` **always uses httpx's default transport** —
  Tier-12 PR 12.6 lifted synthetic-error injection into the chain
  (:class:`notebooklm._middleware_error_injection.ErrorInjectionMiddleware`)
  and PR 12.9 deleted the legacy ``_SyntheticErrorTransport`` class.
  The lifecycle constructs a plain transport regardless of
  ``NOTEBOOKLM_VCR_RECORD_ERRORS``.
* :meth:`ClientLifecycle._keepalive_loop` **respects the min-interval
  clamp** — ``_resolve_keepalive_interval`` floors the configured interval
  at ``keepalive_min_interval`` so a sub-floor user value gets bumped up.

Tests are intentionally helper-shaped (instantiate :class:`ClientLifecycle`
directly with a stub collaborator bundle) so they cover the lifecycle
without taking on a ``Session`` dependency. Wave 2 of plan
``host-protocol-removal`` narrowed the lifecycle method signatures from
the legacy Session-shaped ``host`` Protocol to explicit keyword-only
collaborators; the :class:`_StubHost` fixture now serves purely as a
convenience bundle, paired with module-level :func:`_open` / :func:`_close`
adapters that unpack the bundle into the new kwarg shape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from _helpers.session_factory import build_session_for_tests
from notebooklm._session_helpers import _resolve_keepalive_interval
from notebooklm._session_lifecycle import (
    ClientLifecycle,
    _default_cookie_rotator,
    _default_cookie_saver,
)
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits


class _StubHost:
    """Test-side collaborator bundle for :class:`ClientLifecycle` unit tests.

    Wave 2 of plan ``host-protocol-removal`` narrowed the four
    :class:`ClientLifecycle` methods to take explicit keyword-only
    collaborators rather than a Session-shaped ``host`` Protocol. This
    fixture survives as a convenience bundle — it holds the same set of
    stub collaborators every lifecycle test needs (auth, drain tracker,
    auth coordinator, reqid counter, cookie persistence) in one place,
    so each test can do ``lifecycle.open(auth=host.auth,
    drain_tracker=host._drain_tracker, ...)`` without re-building five
    mocks at every call site.

    Mirrors the live ``Session`` attribute names for grep continuity:

    * ``auth`` — a real :class:`AuthTokens` so :meth:`ClientLifecycle.open`
      can read ``cookies`` / ``cookie_jar`` / ``storage_path``.
    * ``_drain_tracker`` / ``_auth_coord`` / ``_reqid`` — ``MagicMock``s;
      the lifecycle calls ``_drain_tracker.reset_after_open()`` (Wave 1
      of host-protocol-removal replaced the legacy direct
      ``_drain_tracker._draining = False`` write) and ``set_bound_loop``
      on each of the three helpers (drain / reqid / auth_coord) from the
      open() path so cross-loop misuse can be caught.
    * ``cookie_persistence`` — a ``MagicMock`` with an async ``save``
      coroutine; assertions check it was called with the right args.
    * ``_drain_tracker.run_drain_hooks`` — called by close(); set to an
      ``AsyncMock`` so tests can assert it ran and inspect call order.

    Stage B1 PR 2 of the post-refactoring plan dropped the close-time
    ``host._rpc_executor = None`` line from
    :meth:`ClientLifecycle.close` — the executor now persists across
    ``close()`` → ``open()`` cycles. The corresponding sentinel and the
    ``test_close_nulls_rpc_executor`` regression test were removed in
    that PR; see :mod:`tests.unit.test_lifecycle_executor_reuse` for
    the replacement contract.
    """

    def __init__(self) -> None:
        self.auth = AuthTokens(
            csrf_token="CSRF",
            session_id="SID",
            cookies={"SID": "v1"},
            storage_path=None,
        )
        self._drain_tracker = MagicMock()
        # ``open()`` calls ``drain_tracker.reset_after_open()`` (Wave 1 of
        # host-protocol-removal — the encapsulated form of the legacy
        # ``_drain_tracker._draining = False`` write). The ``MagicMock``
        # default lets the call land without configuring a side effect; the
        # invocation is asserted by ``test_open_captures_bound_loop_and_resets_drain``.
        # Seed ``_draining = True`` so a future regression that re-introduces
        # a direct field read in the lifecycle would still see "drained".
        self._drain_tracker._draining = True
        # Wave 2 of session-decoupling: drain hooks live on the tracker.
        # ``close()`` calls ``drain_tracker.run_drain_hooks()`` so the mock
        # needs an async implementation.
        self._drain_tracker.run_drain_hooks = AsyncMock()
        self._auth_coord = MagicMock()
        # Wave 1 of host-protocol-removal: ``close()`` no longer reads the
        # private ``_refresh_task`` slot directly — it calls the awaitable
        # ``cancel_inflight_refresh`` method on the coordinator. Default
        # ``MagicMock()`` would return a non-awaitable, so the stub needs an
        # ``AsyncMock`` for that method. The real coordinator handles the
        # no-op / already-done / in-flight branches internally (covered by
        # the focused unit tests in ``tests/unit/test_session_auth.py``).
        # ``_refresh_task`` is kept as ``None`` on the stub for
        # forward-compatibility with any future test that probes the slot
        # directly (the lifecycle itself no longer reads it).
        self._auth_coord._refresh_task = None
        self._auth_coord.cancel_inflight_refresh = AsyncMock()
        # ``_reqid`` is targeted by ``set_bound_loop`` from open() (P0-2).
        self._reqid = MagicMock()
        self.cookie_persistence = MagicMock()
        self.cookie_persistence.save = AsyncMock()
        self.cookie_persistence.capture_open_snapshot = MagicMock()
        # Stage B1 PR 2 dropped the close-time null on ``_rpc_executor``;
        # the slot is left as-set by the composition root. Set a stable
        # sentinel here in case future regression tests want to assert
        # the value is untouched across an open/close cycle. The lifecycle
        # itself no longer reads this slot.
        self._rpc_executor: Any = "RPC_EXECUTOR_SENTINEL"


def _make_lifecycle(
    *,
    keepalive_interval: float | None = None,
    keepalive_storage_path: Path | None = None,
) -> ClientLifecycle:
    """Construct a :class:`ClientLifecycle` with defaults safe for unit tests.

    Default ``keepalive_interval=None`` means no background keepalive task is
    spawned on :meth:`open` — tests that want the task pass an interval
    explicitly.
    """
    return ClientLifecycle(
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=keepalive_interval,
        keepalive_storage_path=keepalive_storage_path,
    )


async def _open(lifecycle: ClientLifecycle, host: _StubHost) -> None:
    """Adapter that forwards a :class:`_StubHost` bundle into the new
    explicit-kwargs :meth:`ClientLifecycle.open` signature.

    Wave 2 of plan ``host-protocol-removal`` narrowed the lifecycle to
    take collaborators by keyword instead of a Session-shaped host. The
    test fixtures still bundle the collaborators into a stub for
    convenience; this helper bridges the two shapes so each test stays
    a single readable line.
    """
    await lifecycle.open(
        auth=host.auth,
        drain_tracker=host._drain_tracker,
        auth_coord=host._auth_coord,
        reqid=host._reqid,
        cookie_persistence=host.cookie_persistence,
    )


async def _close(lifecycle: ClientLifecycle, host: _StubHost) -> None:
    """Adapter for :meth:`ClientLifecycle.close` — see :func:`_open`."""
    await lifecycle.close(
        auth_coord=host._auth_coord,
        drain_tracker=host._drain_tracker,
        cookie_persistence=host.cookie_persistence,
    )


# ---------------------------------------------------------------------------
# open() — idempotency, bound-loop capture, AsyncClient construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_idempotent_preserves_existing_client() -> None:
    """Second ``open()`` while already open is a no-op — same ``httpx.AsyncClient``."""
    lifecycle = _make_lifecycle()
    host = _StubHost()

    await _open(lifecycle, host)
    first_client = lifecycle._http_client
    assert first_client is not None
    assert lifecycle.is_open()

    await _open(lifecycle, host)
    second_client = lifecycle._http_client

    assert second_client is first_client, (
        "open() must be idempotent — re-opening on an already-open lifecycle "
        "should preserve the existing AsyncClient instance, not build a fresh one."
    )

    await _close(lifecycle, host)


@pytest.mark.asyncio
async def test_open_captures_bound_loop_and_resets_drain() -> None:
    """``open()`` binds the running loop and calls ``reset_after_open`` on the tracker.

    Wave 1 of plan ``host-protocol-removal`` encapsulated the legacy
    direct write ``host._drain_tracker._draining = False`` behind
    :meth:`TransportDrainTracker.reset_after_open`. The lifecycle's
    obligation is now to CALL that method on every ``open()``; the
    method's own behavior (clearing ``_draining`` while leaving
    in-flight counters intact) is pinned by the focused unit tests
    further down in this file.

    Stubs ``host._drain_tracker`` as a ``MagicMock`` so this test
    captures the call without depending on a real
    :class:`TransportDrainTracker`. The companion full-stack
    open-then-close test that exercises a real tracker lives in
    ``tests/integration/`` (and the AST-guarded lint forbids any
    lifecycle code from writing to ``_draining`` directly outside the
    tracker itself, see the acceptance-criteria ``rg`` check in the
    plan).
    """
    lifecycle = _make_lifecycle()
    host = _StubHost()
    assert lifecycle._bound_loop is None

    await _open(lifecycle, host)

    assert lifecycle._bound_loop is asyncio.get_running_loop()
    assert lifecycle.get_bound_loop() is asyncio.get_running_loop()
    host._drain_tracker.reset_after_open.assert_called_once_with()

    await _close(lifecycle, host)


@pytest.mark.asyncio
async def test_open_close_open_rebinds_loop() -> None:
    """``close()`` does not unbind, but a subsequent ``open()`` re-captures
    the current loop (used by clients that close + re-open within one loop)."""
    lifecycle = _make_lifecycle()
    host = _StubHost()

    await _open(lifecycle, host)
    bound_after_first_open = lifecycle._bound_loop
    await _close(lifecycle, host)

    # close() does NOT clear _bound_loop — the cross-loop guard fires on the
    # next call against a different loop if the user mistakenly hands the
    # client off after close.
    assert lifecycle._bound_loop is bound_after_first_open
    assert lifecycle.is_open() is False

    # Re-open on the same loop. New AsyncClient instance; same bound loop.
    await _open(lifecycle, host)
    assert lifecycle._bound_loop is asyncio.get_running_loop()
    assert lifecycle.is_open() is True
    await _close(lifecycle, host)


@pytest.mark.asyncio
async def test_open_captures_cookie_snapshot() -> None:
    """``open()`` calls ``cookie_persistence.capture_open_snapshot`` with the
    live ``httpx.Cookies`` jar AFTER the AsyncClient is built — preserving
    the contract that the open-time baseline reflects httpx-normalized
    domains.
    """
    lifecycle = _make_lifecycle()
    host = _StubHost()

    await _open(lifecycle, host)
    try:
        host.cookie_persistence.capture_open_snapshot.assert_called_once()
        passed_jar = host.cookie_persistence.capture_open_snapshot.call_args.args[0]
        # The jar passed to capture is the AsyncClient's live jar.
        assert passed_jar is lifecycle._http_client.cookies  # type: ignore[union-attr]
    finally:
        await _close(lifecycle, host)


# ---------------------------------------------------------------------------
# Synthetic-error injection — lifted to the chain in PR 12.6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_uses_default_httpx_transport_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path: httpx's default ``AsyncHTTPTransport`` is in place
    (no custom transport wrapping). Post-Tier-12 the synthetic-error
    substitution lives in ``ErrorInjectionMiddleware``; the lifecycle
    constructs a plain transport regardless of any env var, so the test
    asserts the lifecycle's transport construction directly without
    monkeypatching the now-middleware-only error-injection seam.
    """
    from notebooklm import _error_injection

    monkeypatch.setattr(_error_injection, "_get_error_injection_mode", lambda: None)
    lifecycle = _make_lifecycle()
    host = _StubHost()

    await _open(lifecycle, host)
    try:
        client = lifecycle._http_client
        assert client is not None
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)
    finally:
        await _close(lifecycle, host)


@pytest.mark.asyncio
async def test_open_uses_default_httpx_transport_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AsyncClient`` uses httpx's default transport even with env var set.

    Pre-Tier-12 the lifecycle wrapped the inner transport in a synthetic
    httpx transport (deleted in PR 12.9). After Tier-12 the substitution
    lives in the chain (``ErrorInjectionMiddleware``); the lifecycle
    constructs a plain transport regardless of the env var.
    """
    from notebooklm import _error_injection

    monkeypatch.setattr(_error_injection, "_get_error_injection_mode", lambda: "429")
    lifecycle = _make_lifecycle()
    host = _StubHost()

    await _open(lifecycle, host)
    try:
        client = lifecycle._http_client
        assert client is not None
        assert isinstance(client._transport, httpx.AsyncHTTPTransport)
    finally:
        await _close(lifecycle, host)


# ---------------------------------------------------------------------------
# close() — keepalive cancellation, sentinel null-out, idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_cancels_keepalive_cleanly() -> None:
    """``close()`` cancels and awaits the keepalive task; no leaked exception.

    Uses a very short interval (the lifecycle does not re-clamp; the caller
    is expected to have passed the pre-clamped value) so the task has had a
    chance to park on its ``asyncio.sleep`` before close() cancels it.
    """
    lifecycle = _make_lifecycle(keepalive_interval=0.01)
    host = _StubHost()

    await _open(lifecycle, host)
    task = lifecycle._keepalive_task
    assert task is not None
    assert not task.done()

    # Yield once so the keepalive task actually parks on its sleep.
    await asyncio.sleep(0)

    await _close(lifecycle, host)
    assert lifecycle._keepalive_task is None, (
        "close() must null out _keepalive_task after the cancel+gather."
    )
    assert task.cancelled() or task.done(), (
        "keepalive task should be finished (cancelled) after close()."
    )


@pytest.mark.asyncio
async def test_close_when_never_opened_is_noop() -> None:
    """Closing a never-opened lifecycle is safe and does nothing harmful."""
    lifecycle = _make_lifecycle()
    host = _StubHost()

    # No exception, no state churn beyond what's already None/sentinel.
    await _close(lifecycle, host)
    assert lifecycle._http_client is None
    assert lifecycle._keepalive_task is None


@pytest.mark.asyncio
async def test_close_runs_drain_hooks_before_transport_teardown() -> None:
    """``close()`` invokes ``run_drain_hooks`` on the tracker before tearing down the HTTP client.

    Wave 2 of session-decoupling: drain hooks live on ``TransportDrainTracker``;
    the lifecycle just calls ``host._drain_tracker.run_drain_hooks()`` and the
    tracker handles the firing + exception suppression.
    """
    lifecycle = _make_lifecycle()
    host = _StubHost()

    # Record ordering: drain hooks must run *before* the HTTP client teardown
    # (so a hook that needs the live client — e.g. an in-flight cookie save —
    # can still see it).
    events: list[str] = []

    async def fake_run_drain_hooks() -> None:
        assert lifecycle._http_client is not None, (
            "drain hooks must run while the HTTP client is still open"
        )
        events.append("run_drain_hooks")

    host._drain_tracker.run_drain_hooks = fake_run_drain_hooks

    original_aclose = lifecycle._kernel.aclose

    async def recording_aclose() -> None:
        events.append("kernel_aclose")
        await original_aclose()

    lifecycle._kernel.aclose = recording_aclose  # type: ignore[method-assign]

    await _open(lifecycle, host)
    await _close(lifecycle, host)

    assert events == ["run_drain_hooks", "kernel_aclose"], (
        f"close() must run drain hooks before kernel.aclose(); got {events}"
    )
    assert lifecycle._http_client is None


# ---------------------------------------------------------------------------
# save_cookies — invokes cookie_persistence with right args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_cookies_invokes_cookie_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``save_cookies(host, jar, path)`` delegates to
    ``host.cookie_persistence.save(...)``, forwarding the lifecycle's
    ``_cookie_saver`` wrapper as the storage writer.

    Phase 2 PR 3 introduced an injectable ``cookie_saver`` seam; the
    default ``_default_cookie_saver`` wrapper still late-binds at call
    time so a swap of the canonical ``_auth.storage.save_cookies_to_storage``
    attribute fires through. (Phase 4 retargeted the wrapper's late-bind
    from ``notebooklm._core`` to ``notebooklm._auth.storage`` when the
    ``_core`` compatibility shim was deleted.) This assertion is BEHAVIORAL
    (invoke the wrapper, observe the sentinel was called) rather than
    identity-based, because the wrapper indirection is the whole point of
    the seam.
    """
    from notebooklm._auth import storage as storage_module

    sentinel = MagicMock()
    monkeypatch.setattr(storage_module, "save_cookies_to_storage", sentinel)

    lifecycle = _make_lifecycle()
    host = _StubHost()
    jar = httpx.Cookies()
    jar.set("SID", "v2", domain=".google.com")
    target_path = tmp_path / "storage_state.json"

    await lifecycle.save_cookies(host.cookie_persistence, jar, target_path)

    host.cookie_persistence.save.assert_awaited_once()
    call = host.cookie_persistence.save.call_args
    assert call.args[0] is jar
    assert call.args[1] == target_path
    # The kwarg is the lifecycle's wrapper (not the raw sentinel), so the
    # ``CookiePersistence._save`` worker-thread invocation goes through
    # ``_default_cookie_saver``'s late-bound ``_auth.storage`` lookup.
    forwarded_saver = call.kwargs["save_cookies_to_storage"]
    assert forwarded_saver is lifecycle._cookie_saver, (
        "lifecycle.save_cookies must forward self._cookie_saver as the "
        "storage writer (the wrapper indirection is what preserves the "
        "canonical monkeypatch surface)."
    )
    # Behavioral check: invoking the captured wrapper hits the monkeypatched
    # sentinel via late-bound canonical-module resolution.
    forwarded_saver(jar, target_path)
    sentinel.assert_called_once_with(jar, target_path)
    assert call.kwargs["to_thread"] is asyncio.to_thread


# ---------------------------------------------------------------------------
# _bound_loop accessor + cross-loop guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_loop_get_returns_running_loop_after_open() -> None:
    """``get_bound_loop()`` returns the captured loop after open().

    The cross-loop affinity ``RuntimeError`` is raised by
    ``SessionTransport.perform_authed_post`` on actual cross-loop reuse —
    see ``tests/integration/concurrency/test_cross_loop_affinity.py`` for
    the end-to-end exercise. Here we only assert the lifecycle exposes the
    captured loop via :meth:`get_bound_loop`.
    """
    lifecycle = _make_lifecycle()
    host = _StubHost()

    assert lifecycle.get_bound_loop() is None
    await _open(lifecycle, host)
    try:
        assert lifecycle.get_bound_loop() is asyncio.get_running_loop()
    finally:
        await _close(lifecycle, host)


def test_bound_loop_mismatch_via_session_raises_runtime_error() -> None:
    """Cross-loop reuse of a single :class:`Session` raises a clean
    ``RuntimeError`` on the second loop's first authed POST.

    Reaches through the ``Session`` facade (rather than ``ClientLifecycle``
    in isolation) because the guard lives in ``Session`` and only fires from
    inside an authed POST. The test runs two separate
    ``asyncio.run`` invocations to materialise two distinct loops.
    """

    auth = AuthTokens(csrf_token="CSRF", session_id="SID", cookies={"SID": "v1"})
    core = build_session_for_tests(auth=auth)

    async def _open_on_loop_a() -> None:
        await core.open()
        # We deliberately do NOT call core.close() because close() resets
        # _http_client (which would let loop B's open() re-bind the loop
        # and skip the guard). The whole point is that the guard fires when
        # _bound_loop is set from a different loop and a request is attempted
        # without an intervening close().

    def _build_request_stub(snapshot: Any) -> tuple[httpx.Request, Any]:
        return (
            httpx.Request(
                "POST",
                "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
            ),
            None,
        )

    async def _attempt_post_on_loop_b() -> Exception | None:
        # ``open()`` is idempotent — since loop A left ``_http_client``
        # populated, this is a no-op and ``_bound_loop`` stays bound to loop A.
        await core.open()
        try:
            await core._transport.perform_authed_post(
                build_request=_build_request_stub,
                log_label="test.cross_loop",
            )
        except RuntimeError as exc:
            return exc
        return None

    asyncio.run(_open_on_loop_a())
    exc = asyncio.run(_attempt_post_on_loop_b())
    assert isinstance(exc, RuntimeError), (
        f"Cross-loop authed POST must raise RuntimeError; got {exc!r}"
    )
    # The guard's message mentions the loop affinity invariant — match a
    # stable substring rather than the exact phrasing.
    assert "loop" in str(exc).lower(), f"Unexpected RuntimeError text: {exc!r}"


# ---------------------------------------------------------------------------
# _resolve_keepalive_interval clamping (stays in _core.py preamble)
# ---------------------------------------------------------------------------


def test_resolve_keepalive_interval_clamps_to_min_floor() -> None:
    """``_resolve_keepalive_interval`` floors a too-small user value at
    ``min_interval`` — preserving the "accidentally rate-limiting Google's
    identity surface" guard the lifecycle inherits from the resolver.

    The resolver stays in ``_core.py``'s module preamble per the master
    plan; this test belongs alongside the lifecycle suite because the
    clamped value is what the lifecycle stores in ``_keepalive_interval``.
    """
    # User asks for 1s — much lower than the 60s default floor.
    resolved = _resolve_keepalive_interval(keepalive=1.0, min_interval=60.0)
    assert resolved == 60.0


def test_resolve_keepalive_interval_passes_through_above_floor() -> None:
    """A user value above the floor passes through unchanged."""
    resolved = _resolve_keepalive_interval(keepalive=120.0, min_interval=60.0)
    assert resolved == 120.0


def test_resolve_keepalive_interval_none_disables() -> None:
    """``None`` disables the keepalive (no background task spawned)."""
    resolved = _resolve_keepalive_interval(keepalive=None, min_interval=60.0)
    assert resolved is None


def test_resolve_keepalive_interval_rejects_non_positive() -> None:
    """Zero / negative / NaN values raise ``ValueError`` instead of silently
    disabling — surface misconfiguration loudly at construction time."""
    with pytest.raises(ValueError):
        _resolve_keepalive_interval(keepalive=0, min_interval=60.0)
    with pytest.raises(ValueError):
        _resolve_keepalive_interval(keepalive=-1.0, min_interval=60.0)
    with pytest.raises(ValueError):
        _resolve_keepalive_interval(keepalive=1.0, min_interval=0)


# ---------------------------------------------------------------------------
# Construction-time invariants
# ---------------------------------------------------------------------------


def test_init_is_event_loop_agnostic() -> None:
    """Constructing a ``ClientLifecycle`` outside a running loop must not
    raise. The helper stores only plain values and ``None`` placeholders;
    the ``httpx.AsyncClient`` and keepalive task are deferred to ``open()``.
    """
    # Outside ``asyncio.run`` — no running loop available.
    lifecycle = ClientLifecycle(
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=60.0,
        keepalive_storage_path=Path("/tmp/storage.json"),
    )
    assert lifecycle._http_client is None
    assert lifecycle._bound_loop is None
    assert lifecycle._keepalive_task is None
    assert lifecycle._keepalive_interval == 60.0
    assert lifecycle._keepalive_storage_path == Path("/tmp/storage.json")
    assert lifecycle._timeout == 30.0
    assert lifecycle._connect_timeout == 10.0
    assert lifecycle.is_open() is False
    assert lifecycle.get_bound_loop() is None


# ---------------------------------------------------------------------------
# Injectable seams (Phase 2 PR 3 of .sisyphus/plans/refactor-completion-plan.md)
#
# Three load-bearing properties pinned here:
#
# 1. ``_default_cookie_saver`` performs a LATE-BOUND ``_core`` lookup inside
#    its function body. Monkeypatching ``notebooklm._core.save_cookies_to_storage``
#    AFTER the wrapper exists must still affect the wrapper's behavior.
#    Without late-binding, the 8+ existing tests that patch ``_core.save_*``
#    silently lose their effect under the seam refactor.
#
# 2. ``_default_cookie_rotator`` performs the same late-bound lookup for
#    ``_core._rotate_cookies``. The keepalive-loop equivalent of (1).
#
# 3. ``ClientLifecycle.__init__`` wires the defaults when ``cookie_saver`` /
#    ``cookie_rotator`` are ``None`` (or omitted), and accepts custom
#    callables when supplied. The ``or _default_*`` resolution pattern is
#    what lets ``_ensure_lifecycle`` (which does NOT pass the new kwargs)
#    keep working unchanged.
# ---------------------------------------------------------------------------


def test_default_cookie_saver_late_binds_to_canonical_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_default_cookie_saver`` resolves
    ``_auth.storage.save_cookies_to_storage`` at CALL time, not at
    module-import time.

    Establish a sentinel AFTER ``_default_cookie_saver`` already exists,
    then invoke the wrapper and prove the sentinel was called. A non-late-
    bound wrapper would have captured the original ``save_cookies_to_storage``
    reference at module load and silently ignored the monkeypatch.
    (Phase 4 retargeted the late-bind from ``notebooklm._core`` to
    ``notebooklm._auth.storage`` when the ``_core`` compatibility shim
    was deleted.)
    """
    from notebooklm._auth import storage as storage_module

    sentinel = MagicMock(return_value=True)
    monkeypatch.setattr(storage_module, "save_cookies_to_storage", sentinel)

    jar = httpx.Cookies()
    path = Path("/tmp/storage.json")
    result = _default_cookie_saver(jar, path)

    sentinel.assert_called_once_with(jar, path)
    assert result is True


@pytest.mark.asyncio
async def test_default_cookie_rotator_late_binds_to_canonical_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_default_cookie_rotator`` resolves
    ``_auth.keepalive._rotate_cookies`` at CALL time and awaits it.
    Async-shape counterpart to the saver test. (Phase 4 retargeted the
    late-bind from ``notebooklm._core`` to ``notebooklm._auth.keepalive``
    when the ``_core`` compatibility shim was deleted.)
    """
    from notebooklm._auth import keepalive as keepalive_module

    sentinel = AsyncMock(return_value=None)
    monkeypatch.setattr(keepalive_module, "_rotate_cookies", sentinel)

    client = MagicMock(spec=httpx.AsyncClient)
    path = Path("/tmp/storage.json")
    await _default_cookie_rotator(client, path)

    sentinel.assert_awaited_once_with(client, path)


def test_init_wires_default_seams_when_none_supplied() -> None:
    """When ``cookie_saver`` / ``cookie_rotator`` are omitted (or ``None``),
    ``ClientLifecycle.__init__`` wires the module-level late-binding
    defaults; supplying custom callables overrides them.

    This is what lets :meth:`Session._ensure_lifecycle` keep its existing
    no-arg signature — it constructs ``ClientLifecycle(...)`` without the
    new kwargs, and the ``or _default_*`` resolution preserves the legacy
    ``_core`` monkeypatch surface.
    """
    # Defaults: omit the kwargs entirely.
    default_lifecycle = ClientLifecycle(
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=None,
        keepalive_storage_path=None,
    )
    assert default_lifecycle._cookie_saver is _default_cookie_saver
    assert default_lifecycle._cookie_rotator is _default_cookie_rotator

    # Explicit ``None`` resolves the same way as omission.
    explicit_none_lifecycle = ClientLifecycle(
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=None,
        keepalive_storage_path=None,
        cookie_saver=None,
        cookie_rotator=None,
    )
    assert explicit_none_lifecycle._cookie_saver is _default_cookie_saver
    assert explicit_none_lifecycle._cookie_rotator is _default_cookie_rotator

    # Custom callables override the defaults — pure pass-through, no
    # ``_core`` indirection.
    custom_saver = MagicMock(return_value=True)
    custom_rotator = AsyncMock(return_value=None)
    custom_lifecycle = ClientLifecycle(
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=None,
        keepalive_storage_path=None,
        cookie_saver=custom_saver,
        cookie_rotator=custom_rotator,
    )
    assert custom_lifecycle._cookie_saver is custom_saver
    assert custom_lifecycle._cookie_rotator is custom_rotator


# ---------------------------------------------------------------------------
# TransportDrainTracker.reset_after_open — Wave 1 of host-protocol-removal
# encapsulated ``host._drain_tracker._draining = False`` (previously written
# directly by ``ClientLifecycle.open``) behind a method on the tracker. The
# method is intentionally narrow: it clears ONLY the ``_draining`` flag and
# leaves in-flight counters / depth maps untouched. These two tests pin
# both halves of that contract on the collaborator directly so a regression
# in the tracker is caught without driving the full lifecycle open() path.
# ---------------------------------------------------------------------------


def test_drain_tracker_reset_after_open_clears_draining_flag() -> None:
    """``reset_after_open`` flips ``_draining`` from ``True`` back to ``False``.

    Pins the encapsulation of the legacy
    ``host._drain_tracker._draining = False`` write performed inside
    ``ClientLifecycle.open``. The behavior is the same — a tracker that was
    previously drained, then re-opened, admits new top-level work again —
    just routed through a named method instead of a direct field write.
    """
    tracker = TransportDrainTracker()
    tracker._draining = True

    tracker.reset_after_open()

    assert tracker._draining is False


@pytest.mark.asyncio
async def test_drain_tracker_reset_after_open_does_not_touch_inflight_counts() -> None:
    """``reset_after_open`` ONLY clears ``_draining`` — every other piece
    of bookkeeping state is left intact.

    Pins the "intentionally narrow" half of the encapsulation. If a
    well-meaning maintainer "helpfully" expanded this method to also zero
    ``_in_flight_posts`` or reset / clear ``_operation_depths``, the
    load-bearing in-flight invariants asserted by
    ``tests/unit/test_observability.py::test_drain_allows_nested_work_inside_accepted_operation``
    and ``tests/unit/concurrency/test_close_cancellation_leak.py`` would
    break. This regression test catches that expansion at the tracker
    level so the failure points at the right code rather than surfacing
    as a confusing in-flight count mismatch elsewhere.

    Seeds an actual operation-depth entry (not just an identity-stable
    empty map) so a regression that calls
    ``self._operation_depths.clear()`` — which would preserve map identity
    but wipe the contents — also fails this test. Async-marked so the
    seeded task ``asyncio.current_task()`` returns a real task to key the
    WeakKeyDictionary on.
    """
    tracker = TransportDrainTracker()
    tracker._draining = True
    # Seed non-default values so the assertions below would fail loudly
    # if ``reset_after_open`` overwrote them.
    tracker._in_flight_posts = 3
    seed_task = asyncio.current_task()
    assert seed_task is not None, "test runs under @pytest.mark.asyncio"
    tracker._operation_depths[seed_task] = 2
    pre_depths_id = id(tracker._operation_depths)

    async def _drain_hook() -> None:
        return None

    tracker.register_drain_hook("seed_hook", _drain_hook)

    tracker.reset_after_open()

    assert tracker._draining is False, "the one flag this method *does* clear"
    assert tracker._in_flight_posts == 3, (
        "reset_after_open must not touch _in_flight_posts — clearing it "
        "would lose track of in-flight operations and let drain() return "
        "prematurely on the next close()."
    )
    assert id(tracker._operation_depths) == pre_depths_id, (
        "reset_after_open must preserve the _operation_depths "
        "WeakKeyDictionary identity — replacing it would orphan per-task "
        "depth bookkeeping for already-admitted operations."
    )
    assert tracker._operation_depths.get(seed_task) == 2, (
        "reset_after_open must not clear() _operation_depths contents — "
        "a regression that wiped per-task depths would reject already-"
        "admitted nested operations after the next open()."
    )
    assert "seed_hook" in tracker._drain_hooks, (
        "reset_after_open must not touch registered drain hooks; feature "
        "code registers them at construction-time on the tracker."
    )
