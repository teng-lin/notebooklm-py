"""Tests for Stage B1 PR 1 composition primitives.

Covers the additive helpers introduced by Stage B1 PR 1 of the
post-refactoring plan (``docs/post-refactoring-plan-2026-05-27.md``):

- :class:`notebooklm._session.ComposedSession` dataclass
- :func:`notebooklm._session.resolve_seam_defaults`
- :func:`notebooklm._session.compose_session_internals`
- ``Session._bind_transport`` / ``_bind_chain`` / ``_bind_executor``
  write-once setters
- ``Session._require_constructed`` fail-fast guard

All primitives are **DORMANT** in PR 1 — ``Session.__init__`` still
performs the legacy inline construction sequence. These tests exercise
the primitives directly so they don't bit-rot before PR 2 starts using
them. They also pin the write-once contract (raise on double-bind) and
the synthetic-error guard ordering (``_refuse_synthetic_error_outside_test_context``
runs first inside ``compose_session_internals``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from notebooklm._session import (
    ComposedSession,
    Session,
    compose_session_internals,
    resolve_seam_defaults,
)
from notebooklm.auth import AuthTokens


def _make_auth() -> AuthTokens:
    """Build a minimal :class:`AuthTokens` for composition tests.

    Cookies / CSRF / session id are sentinel values — these tests never
    hit the network; they only need a token shape that passes
    :func:`_validate_required_cookies`.
    """
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


# ---------------------------------------------------------------------------
# resolve_seam_defaults
# ---------------------------------------------------------------------------


def test_resolve_seam_defaults_returns_module_bindings_when_none() -> None:
    """All four seams default to the canonical module bindings."""
    resolved = resolve_seam_defaults(
        sleep=None,
        async_client_factory=None,
        is_auth_error=None,
        decode_response=None,
    )

    # ``sleep`` resolves to ``asyncio.sleep`` via the module-level
    # ``asyncio`` binding inside :mod:`notebooklm._session`.
    assert resolved["sleep"] is asyncio.sleep

    # ``async_client_factory`` resolves to :class:`httpx.AsyncClient`.
    assert resolved["async_client_factory"] is httpx.AsyncClient

    # ``is_auth_error`` resolves to :func:`notebooklm._session_helpers.is_auth_error`
    # via the lazy import inside :func:`_default_is_auth_error`.
    from notebooklm._session_helpers import is_auth_error as canonical_is_auth_error

    assert resolved["is_auth_error"] is canonical_is_auth_error

    # ``decode_response`` resolves to :func:`notebooklm.rpc.decode_response`
    # via the lazy import inside :func:`_default_decode_response`.
    from notebooklm.rpc import decode_response as canonical_decode_response

    assert resolved["decode_response"] is canonical_decode_response


def test_resolve_seam_defaults_passes_through_explicit_callables() -> None:
    """Explicit callables override the module-binding defaults."""

    async def fake_sleep(_d: float) -> None:
        """Sentinel callable — identity-checked, never invoked."""
        return None

    def fake_factory(*_a: Any, **_kw: Any) -> Any:  # pragma: no cover - identity check
        """Sentinel callable — identity-checked, never invoked."""
        raise AssertionError

    def fake_is_auth_error(_exc: Exception) -> bool:  # pragma: no cover
        """Sentinel callable — identity-checked, never invoked."""
        return False

    def fake_decode(*_a: Any, **_kw: Any) -> Any:  # pragma: no cover
        """Sentinel callable — identity-checked, never invoked."""
        return None

    resolved = resolve_seam_defaults(
        sleep=fake_sleep,
        async_client_factory=fake_factory,
        is_auth_error=fake_is_auth_error,
        decode_response=fake_decode,
    )

    assert resolved["sleep"] is fake_sleep
    assert resolved["async_client_factory"] is fake_factory
    assert resolved["is_auth_error"] is fake_is_auth_error
    assert resolved["decode_response"] is fake_decode


# ---------------------------------------------------------------------------
# compose_session_internals
# ---------------------------------------------------------------------------


def test_compose_session_internals_returns_composed_session() -> None:
    """The helper returns a fully-bundled :class:`ComposedSession`."""
    composed = compose_session_internals(auth=_make_auth())

    assert isinstance(composed, ComposedSession)
    assert isinstance(composed.session, Session)
    # The transport in the bundle is the one wired by the legacy inline
    # ``Session.__init__`` and read back inside the helper.
    assert composed.transport is composed.session._transport
    # The executor is the one bound by the helper via :meth:`_bind_executor`.
    assert composed.executor is composed.session._rpc_executor
    # The collaborators are accessible via the Stage-A accessor for
    # cross-checking (both reads should point at the same bundle).
    assert composed.collaborators is composed.session._collaborators


def test_compose_session_internals_refuses_synthetic_error_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_refuse_synthetic_error_outside_test_context`` MUST run before any
    other work in :func:`compose_session_internals`.

    Pins the same contract as
    :mod:`tests.unit.concurrency.test_synthetic_error_transport_guard` —
    the guard fires at the *earliest* opportunity. Setting the env var
    without ``PYTEST_CURRENT_TEST`` must raise from the helper before the
    seam resolution, validation, or collaborator construction can run.
    """
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD_ERRORS", "5xx")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm._core"),
        pytest.raises(RuntimeError, match="NOTEBOOKLM_VCR_RECORD_ERRORS"),
    ):
        compose_session_internals(auth=_make_auth())


def test_compose_session_internals_preserves_late_binding_for_decode_response() -> None:
    """Post-construction ``session._decode_response = rebound`` MUST still
    steer the executor's decode path.

    Pins the lambda-closure contract documented in the plan: the executor
    is wired with ``decode_response=lambda *a, **kw: session._decode_response(*a, **kw)``
    so that test reassignments after construction continue to take effect.
    """
    composed = compose_session_internals(auth=_make_auth())

    sentinel: list[Any] = []

    def rebound(*args: Any, **kwargs: Any) -> str:
        """Recording stand-in for ``session._decode_response``."""
        sentinel.append(("decoded", args, kwargs))
        return "rebound-result"

    composed.session._decode_response = rebound

    # The executor closure should dispatch through the live attribute,
    # not the value frozen at construction time.
    result = composed.executor._decode_response("payload", "method-id", allow_null=False)
    assert result == "rebound-result"
    assert sentinel and sentinel[-1][0] == "decoded"


def test_compose_session_internals_preserves_late_binding_for_is_auth_error() -> None:
    """Post-construction ``session._is_auth_error = rebound`` MUST still
    steer the executor's classifier.

    Mirror of the ``_decode_response`` test for the auth-error seam.
    """
    composed = compose_session_internals(auth=_make_auth())

    def rebound(exc: Exception) -> bool:
        """Stand-in classifier — treats KeyError as auth-related."""
        return isinstance(exc, KeyError)

    composed.session._is_auth_error = rebound

    assert composed.executor._is_auth_error(KeyError("auth")) is True
    assert composed.executor._is_auth_error(RuntimeError("nope")) is False


def test_compose_session_internals_preserves_late_binding_for_sleep() -> None:
    """Post-construction ``session._sleep = rebound`` MUST still steer the
    executor's backoff path.
    """
    composed = compose_session_internals(auth=_make_auth())

    calls: list[float] = []

    async def rebound(delay: float) -> None:
        """Recording stand-in for ``session._sleep`` (captures delays)."""
        calls.append(delay)

    composed.session._sleep = rebound

    asyncio.run(composed.executor._sleep(0.25))
    assert calls == [0.25]


def test_compose_session_internals_preserves_late_binding_for_refresh_retry_delay() -> None:
    """Post-construction ``session._refresh_retry_delay = X`` MUST be seen
    by the executor's ``refresh_retry_delay_provider`` lambda on the next
    call.

    The plan's "Design Invariants" section explicitly calls out this
    contract: ``client._session._refresh_retry_delay = 0`` continues to
    steer the live chain after construction. The lambda
    ``refresh_retry_delay_provider=lambda: session._refresh_retry_delay``
    re-reads the attribute on every invocation, so this is a live binding,
    not a frozen snapshot.
    """
    composed = compose_session_internals(auth=_make_auth())

    # The provider lambda must dereference the *current* attribute on
    # each call — not the value captured at construction time.
    initial = composed.session._refresh_retry_delay
    assert composed.executor._refresh_retry_delay_provider() == initial

    composed.session._refresh_retry_delay = 0.99
    assert composed.executor._refresh_retry_delay_provider() == 0.99


def test_compose_session_internals_executor_timeout_provider_reads_lifecycle() -> None:
    """The executor's ``timeout_provider`` reads from the live
    ``ClientLifecycle._timeout`` collaborator attribute.

    Pins the documented closure shape
    ``timeout_provider=lambda: collaborators.lifecycle._timeout`` (plan
    line 253). A lifecycle-side mutation must surface on the next executor
    call without re-binding.
    """
    composed = compose_session_internals(auth=_make_auth())

    initial = composed.collaborators.lifecycle._timeout
    assert composed.executor._timeout_provider() == initial

    composed.collaborators.lifecycle._timeout = 99.0
    assert composed.executor._timeout_provider() == 99.0


# ---------------------------------------------------------------------------
# write-once binders
# ---------------------------------------------------------------------------


def test_bind_executor_succeeds_when_slot_is_none() -> None:
    """The legacy ``Session.__init__`` leaves ``_rpc_executor`` at ``None``
    (lazy via :meth:`_get_rpc_executor`), so :meth:`_bind_executor` is the
    one binder that fires cleanly in PR 1.
    """
    session = Session(_make_auth())
    assert session._rpc_executor is None

    # Build an executor the same way the helper does and bind it.
    from notebooklm._rpc_executor import RpcExecutor

    executor = RpcExecutor(
        kernel=session._kernel,
        transport=session._transport,
        auth_refresh=session._auth_coord,
        metrics=session._metrics_obj,
        decode_response=session._decode_response,
        is_auth_error=session._is_auth_error,
        sleep=session._sleep,
        timeout_provider=lambda: session._lifecycle._timeout,
        refresh_callback_enabled_provider=lambda: session._auth_coord.has_refresh_callback,
        refresh_retry_delay_provider=lambda: session._refresh_retry_delay,
    )

    session._bind_executor(executor)
    assert session._rpc_executor is executor


def test_bind_executor_raises_on_double_bind() -> None:
    """:meth:`_bind_executor` accepts exactly one bind."""
    composed = compose_session_internals(auth=_make_auth())

    with pytest.raises(RuntimeError, match="_rpc_executor already bound"):
        composed.session._bind_executor(composed.executor)


def test_bind_transport_raises_after_legacy_init_sets_slot() -> None:
    """In PR 1, the legacy ``Session.__init__`` inline-sets ``_transport``,
    so :meth:`_bind_transport` raises if called after construction.

    PR 2 of Stage B1 inverts this — ``Session.__init__`` will leave
    ``_transport`` at ``None`` and :func:`compose_session_internals` will
    drive the binder. Until then, the write-once contract is exercised
    by this test against the inline-set slot.
    """
    session = Session(_make_auth())

    with pytest.raises(RuntimeError, match="_transport already bound"):
        session._bind_transport(session._transport)


def test_bind_chain_raises_after_legacy_init_sets_slot() -> None:
    """Same shape as :func:`test_bind_transport_raises_after_legacy_init_sets_slot`:
    legacy ``Session.__init__`` sets ``_chain_builder`` inline, so
    :meth:`_bind_chain` raises on the first call after construction.
    """
    session = Session(_make_auth())

    # Build a sentinel ``WiredMiddleware`` carrying the existing values so
    # the rejection comes from the write-once guard, not a missing field.
    from notebooklm._session_init import WiredMiddleware

    wired = WiredMiddleware(
        chain_builder=session._chain_builder,
        middlewares=session._middlewares,
        authed_post_chain=session._authed_post_chain,
    )
    with pytest.raises(RuntimeError, match="_chain already bound"):
        session._bind_chain(wired)


# ---------------------------------------------------------------------------
# fail-fast guards
# ---------------------------------------------------------------------------


def test_require_constructed_raises_when_attr_is_none() -> None:
    """The guard raises ``RuntimeError`` with a self-describing message."""
    session = Session(_make_auth())

    # ``_rpc_executor`` is None until the lazy factory fires; use it as
    # the canonical ``is None`` slot for this assertion.
    assert session._rpc_executor is None
    with pytest.raises(RuntimeError, match="Session not fully constructed: _rpc_executor is None"):
        session._require_constructed("_rpc_executor")


def test_require_constructed_is_inert_when_attr_is_set() -> None:
    """The guard returns silently when the binding is set."""
    session = Session(_make_auth())
    # ``_transport`` is set inline by ``Session.__init__`` in PR 1.
    assert session._transport is not None
    # Should not raise.
    session._require_constructed("_transport")


def test_require_constructed_raises_on_missing_attribute() -> None:
    """The guard also raises for attributes that have never been assigned.

    Uses :func:`getattr` with a ``None`` default so the same actionable
    message surfaces during ``__init__`` itself, before the attribute
    has been assigned for the first time.
    """
    session = Session(_make_auth())

    with pytest.raises(RuntimeError, match="Session not fully constructed: _nonexistent is None"):
        session._require_constructed("_nonexistent")


def test_entry_point_guards_fire_on_uninitialised_session() -> None:
    """The fail-fast guards on ``rpc_call`` / ``_get_rpc_semaphore`` /
    ``open`` / ``close`` raise when ``_transport`` is ``None``.

    Bypasses ``Session.__init__`` (which sets ``_transport`` inline in
    PR 1) by using ``Session.__new__`` directly so the guards see a
    pre-binding state. This is the contract that PR 2 of Stage B1 will
    rely on once :func:`compose_session_internals` becomes the only
    composition path and ``Session.__init__`` leaves the slot at
    ``None``.
    """
    session = Session.__new__(Session)
    # No attributes set — guards must treat this as "not constructed".

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        asyncio.run(session.rpc_call(None, []))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        session._get_rpc_semaphore()

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        asyncio.run(session.open())

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        asyncio.run(session.close())
