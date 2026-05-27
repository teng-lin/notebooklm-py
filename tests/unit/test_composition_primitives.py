"""Tests for Stage B1 composition primitives (PR 2 — composition root live).

Covers the helpers introduced by Stage B1 PR 1 and made live by Stage B1
PR 2 of the post-refactoring plan
(``docs/post-refactoring-plan-2026-05-27.md``):

- :class:`notebooklm._session.ComposedSession` dataclass
- :func:`notebooklm._session.resolve_seam_defaults`
- :func:`notebooklm._session.compose_session_internals` — the live
  composition root after PR 2
- ``Session._bind_transport`` / ``_bind_chain_metadata`` / ``_bind_executor``
  write-once setters (now load-bearing — :meth:`Session.__init__` no
  longer inline-sets the slots; :func:`compose_session_internals`
  drives the binders)
- ``Session._require_constructed`` fail-fast guard

PR 2 inverted the composition root: :meth:`Session.__init__` now takes
``(*, collaborators, config, auth)`` and leaves the transport / chain /
executor slots at ``None``. :func:`compose_session_internals` is the
only path that produces a fully-bound :class:`Session`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from _helpers.session_factory import build_session_for_tests
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
# compose_session_internals — live composition root after Stage B1 PR 2
# ---------------------------------------------------------------------------


def test_compose_session_internals_returns_composed_session() -> None:
    """The helper returns a fully-bundled :class:`ComposedSession`.

    PR 2 made the helper load-bearing: :meth:`Session.__init__` no
    longer constructs collaborators / transport / chain inline, so this
    bundle is the only path to a usable :class:`Session`.
    """
    composed = compose_session_internals(auth=_make_auth())

    assert isinstance(composed, ComposedSession)
    assert isinstance(composed.session, Session)
    # The transport in the bundle was constructed by the helper and
    # passed into ``Session._bind_transport`` — both reads point at the
    # same instance.
    assert composed.transport is composed.session._transport
    # Same shape for the executor — bound via :meth:`_bind_executor`.
    assert composed.executor is composed.session._rpc_executor
    # The collaborators bundle is the same instance the helper threaded
    # into the Session constructor (stored on the Session as
    # ``_collaborators`` so :class:`NotebookLMClient` can hoist metrics
    # off the bundle without a fresh build).
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
    """Post-construction ``chain_host._refresh_retry_delay = X`` MUST be seen
    by the executor's ``refresh_retry_delay_provider`` lambda on the next
    call.

    The integration-test contract is that
    ``client._session._chain_host._refresh_retry_delay = 0`` continues
    to steer the live chain after construction. The lambda
    ``refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay``
    re-reads the attribute on every invocation, so this is a live binding,
    not a frozen snapshot.
    """
    composed = compose_session_internals(auth=_make_auth())

    chain_host = composed.session._chain_host
    # The provider lambda must dereference the *current* attribute on
    # each call — not the value captured at construction time.
    initial = chain_host._refresh_retry_delay
    assert composed.executor._refresh_retry_delay_provider() == initial

    chain_host._refresh_retry_delay = 0.99
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
# write-once binders — load-bearing after PR 2
# ---------------------------------------------------------------------------


def test_bind_executor_raises_on_double_bind() -> None:
    """:meth:`_bind_executor` accepts exactly one bind.

    :func:`compose_session_internals` invokes the binder once during
    composition; calling it a second time on the returned Session must
    raise.
    """
    composed = compose_session_internals(auth=_make_auth())

    with pytest.raises(RuntimeError, match="_rpc_executor already bound"):
        composed.session._bind_executor(composed.executor)


def test_bind_transport_raises_on_double_bind() -> None:
    """:meth:`_bind_transport` accepts exactly one bind.

    PR 2 of Stage B1 inverted :meth:`Session.__init__` (the transport
    slot is now left at ``None`` and the binder is the only assignment
    site). A second bind attempt after :func:`compose_session_internals`
    has driven the binder must raise.
    """
    composed = compose_session_internals(auth=_make_auth())

    with pytest.raises(RuntimeError, match="_transport already bound"):
        composed.session._bind_transport(composed.transport)


def test_bind_chain_metadata_raises_on_double_bind() -> None:
    """:meth:`_bind_chain_metadata` accepts exactly one bind.

    Re-driving the binder after composition raises. The binder stores
    only the auxiliary chain artifacts (``_chain_builder`` /
    ``_middlewares``); the chain slot itself lives on
    :class:`MiddlewareChainHost` and is assigned exactly once by the
    composition root (``chain_host._authed_post_chain =
    wired.authed_post_chain``).
    """
    composed = compose_session_internals(auth=_make_auth())

    # Build a sentinel ``WiredMiddleware`` carrying the existing values so
    # the rejection comes from the write-once guard, not a missing field.
    from notebooklm._session_init import WiredMiddleware

    wired = WiredMiddleware(
        chain_builder=composed.session._chain_builder,
        middlewares=composed.session._middlewares,
        authed_post_chain=composed.session._chain_host._authed_post_chain,
    )
    with pytest.raises(RuntimeError, match="_chain_metadata already bound"):
        composed.session._bind_chain_metadata(wired)


# ---------------------------------------------------------------------------
# fail-fast guards
# ---------------------------------------------------------------------------


def test_require_constructed_raises_when_attr_is_none() -> None:
    """The guard raises ``RuntimeError`` with a self-describing message.

    Constructs a bare ``Session`` via ``__new__`` to bypass the
    composition root — the resulting instance has all the late-bound
    slots unset so the guard fires actionably.
    """
    session = Session.__new__(Session)
    session._transport = None  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        session._require_constructed("_transport")


def test_require_constructed_is_inert_when_attr_is_set() -> None:
    """The guard returns silently when the binding is set."""
    session = build_session_for_tests(_make_auth())
    # ``_transport`` is set by :func:`compose_session_internals` via
    # :meth:`_bind_transport`.
    assert session._transport is not None
    # Should not raise.
    session._require_constructed("_transport")


def test_require_constructed_raises_on_missing_attribute() -> None:
    """The guard also raises for attributes that have never been assigned.

    Uses :func:`getattr` with a ``None`` default so the same actionable
    message surfaces during ``__init__`` itself, before the attribute
    has been assigned for the first time.
    """
    session = build_session_for_tests(_make_auth())

    with pytest.raises(RuntimeError, match="Session not fully constructed: _nonexistent is None"):
        session._require_constructed("_nonexistent")


def test_entry_point_guards_fire_on_uninitialised_session() -> None:
    """The fail-fast guards on ``rpc_call`` / ``_get_rpc_semaphore`` /
    ``open`` / ``close`` raise when the relevant write-once binding is
    ``None``.

    Bypasses :func:`compose_session_internals` (the canonical composition
    root) via ``Session.__new__`` so the guards see a pre-binding
    state — the same state ``__init__`` exits in if the composition
    root is short-circuited.

    Stage B1 PR 2 of the post-refactoring plan deleted the lazy
    ``Session._get_rpc_executor`` factory; :meth:`Session.rpc_call` now
    requires ``_rpc_executor`` (the slot the composition root binds
    last) instead of ``_transport``. The other three entry points
    continue to probe ``_transport`` because that's the slot the
    composition root binds first — a pre-transport call indicates
    a fundamentally unconstructed Session regardless of whether the
    chain or executor finished wiring.
    """
    session = Session.__new__(Session)
    # No attributes set — guards must treat this as "not constructed".

    # rpc_call probes the executor (Stage B1 PR 2 change).
    with pytest.raises(RuntimeError, match="Session not fully constructed: _rpc_executor is None"):
        asyncio.run(session.rpc_call(None, []))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        session._get_rpc_semaphore()

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        asyncio.run(session.open())

    with pytest.raises(RuntimeError, match="Session not fully constructed: _transport is None"):
        asyncio.run(session.close())
