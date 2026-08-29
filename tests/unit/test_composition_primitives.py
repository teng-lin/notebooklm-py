"""Tests for client-owned composition primitives.

Covers the client-owned composition helpers in :mod:`notebooklm._runtime.init`:

- :class:`notebooklm._runtime.init.ClientInternals` dataclass
- :func:`notebooklm._runtime.init.compose_client_internals`
- ``ClientComposed.bind_*`` write-once setters
- ``ClientComposed`` required-property guards

The redundant ``resolve_seam_defaults`` resolver was removed in issue
#1327 — ``compose_client_internals`` resolves seams directly via
``resolve_client_seams`` + ``_resolve_async_client_factory``, so the
parallel dict-shaped resolver had no production caller.

Session-elimination Phase 3 leaves ``NotebookLMClient`` as both composition
root and public surface; all composition runtime state belongs to
``ClientComposed`` or the client-owned collaborator bundle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from notebooklm._client_composed import ClientComposed
from notebooklm._runtime.init import (
    ClientInternals,
    compose_client_internals,
)
from notebooklm._web.transport.seams import ClientSeams
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._helpers.client_factory import build_client_shell_for_tests


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
# compose_client_internals — client-owned composition root
# ---------------------------------------------------------------------------


def test_compose_client_internals_returns_client_internals() -> None:
    """The helper returns collaborators + executor while binding ``ClientComposed``."""
    holder = ClientComposed()
    internals = compose_client_internals(auth=_make_auth(), composed=holder)

    assert isinstance(internals, ClientInternals)
    assert holder.executor is internals.executor
    with pytest.raises(RuntimeError, match="_runtime_collaborators is None"):
        _ = holder.runtime_collaborators
    assert internals.collaborators._lifecycle is None
    assert holder.transport is internals.executor._transport
    assert holder.chain_host._transport is holder.transport
    assert holder.chain_builder is not None
    assert len(holder.middlewares) == 4


def test_shell_helpers_carry_client_holders() -> None:
    """Client shell helpers mirror production holder attributes."""
    client = build_client_shell_for_tests(auth=_make_auth(), max_concurrent_rpcs=3)

    assert isinstance(client._seams, ClientSeams)
    assert isinstance(client._composed, ClientComposed)
    assert client._collaborators.call_supervisor._max_concurrent_rpcs == 3
    assert client._composed.runtime_collaborators is client._collaborators
    assert client._composed.executor is client._rpc_executor


def test_notebooklm_client_initializes_client_holders() -> None:
    """Production clients own the same holder shape returned by composition."""
    client = NotebookLMClient(_make_auth(), max_concurrent_rpcs=2)

    assert isinstance(client._seams, ClientSeams)
    assert isinstance(client._composed, ClientComposed)
    assert client._composed.runtime_collaborators is client._collaborators
    assert client._collaborators.call_supervisor._max_concurrent_rpcs == 2
    assert client._composed.executor is client._rpc_executor
    assert client._composed.transport is client._rpc_executor._transport


def test_invalid_max_concurrent_rpcs_rejected_before_zero_cap_semaphore() -> None:
    """Production and test construction reject invalid caps before composition use."""
    auth = _make_auth()

    with pytest.raises(ValueError, match="max_concurrent_rpcs must be >= 1, got 0"):
        NotebookLMClient(auth, max_concurrent_rpcs=0)

    with pytest.raises(ValueError, match="max_concurrent_rpcs must be >= 1, got 0"):
        build_client_shell_for_tests(auth, max_concurrent_rpcs=0)


@pytest.mark.parametrize(
    "other_invalid",
    [
        {"rate_limit_max_retries": -1},
        {"max_concurrent_uploads": 0},
    ],
)
def test_max_concurrent_rpcs_keeps_phase_a_validation_precedence(
    other_invalid: dict[str, int],
) -> None:
    with pytest.raises(ValueError) as raised:
        NotebookLMClient(
            _make_auth(),
            max_concurrent_rpcs=0,
            **other_invalid,  # type: ignore[arg-type]
        )

    assert str(raised.value) == "max_concurrent_rpcs must be >= 1, got 0"


def test_prebuilt_client_composed_has_no_runtime_policy_configuration() -> None:
    """A supplied composition holder does not own the RPC cap."""
    holder = ClientComposed()
    internals = compose_client_internals(
        auth=_make_auth(),
        max_concurrent_rpcs=10,
        composed=holder,
    )
    assert internals.collaborators.call_supervisor._max_concurrent_rpcs == 10


def test_compose_client_internals_refuses_synthetic_error_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_refuse_synthetic_error_outside_test_context`` MUST run before any
    other work in :func:`compose_client_internals`.

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
        compose_client_internals(auth=_make_auth())


def test_compose_client_internals_preserves_late_binding_for_decode_response() -> None:
    """Post-construction ``seams.decode_response = rebound`` MUST still
    steer the executor's decode path.

    Pins the lambda-closure contract documented in the plan: the executor
    is wired with ``decode_response=lambda *a, **kw: seams.decode_response(*a, **kw)``
    so that test reassignments after construction continue to take effect.
    """
    seams = ClientSeams(
        decode_response=lambda *_a, **_kw: None,
        sleep=asyncio.sleep,
        is_auth_error=lambda _exc: False,
    )
    internals = compose_client_internals(auth=_make_auth(), seams=seams)

    sentinel: list[Any] = []

    def rebound(*args: Any, **kwargs: Any) -> str:
        """Recording stand-in for ``seams.decode_response``."""
        sentinel.append(("decoded", args, kwargs))
        return "rebound-result"

    seams.decode_response = rebound

    # The executor closure should dispatch through the live attribute,
    # not the value frozen at construction time.
    result = internals.executor._decode_response("payload", "method-id", allow_null=False)
    assert result == "rebound-result"
    assert sentinel and sentinel[-1][0] == "decoded"


def test_compose_client_internals_preserves_late_binding_for_is_auth_error() -> None:
    """Post-construction ``seams.is_auth_error = rebound`` MUST still
    steer the executor's classifier.

    Mirror of the ``decode_response`` test for the auth-error seam.
    """
    seams = ClientSeams(
        decode_response=lambda *_a, **_kw: None,
        sleep=asyncio.sleep,
        is_auth_error=lambda _exc: False,
    )
    internals = compose_client_internals(auth=_make_auth(), seams=seams)

    def rebound(exc: Exception) -> bool:
        """Stand-in classifier — treats KeyError as auth-related."""
        return isinstance(exc, KeyError)

    seams.is_auth_error = rebound

    assert internals.executor._is_auth_error(KeyError("auth")) is True
    assert internals.executor._is_auth_error(RuntimeError("nope")) is False


def test_compose_client_internals_preserves_late_binding_for_sleep() -> None:
    """Post-construction ``seams.sleep = rebound`` MUST still steer the
    executor's backoff path.
    """
    seams = ClientSeams(
        decode_response=lambda *_a, **_kw: None,
        sleep=asyncio.sleep,
        is_auth_error=lambda _exc: False,
    )
    internals = compose_client_internals(auth=_make_auth(), seams=seams)

    calls: list[float] = []

    async def rebound(delay: float) -> None:
        """Recording stand-in for ``seams.sleep`` (captures delays)."""
        calls.append(delay)

    seams.sleep = rebound

    asyncio.run(internals.executor._sleep(0.25))
    assert calls == [0.25]


def test_compose_client_internals_preserves_late_binding_for_refresh_retry_delay() -> None:
    """Post-construction ``chain_host._refresh_retry_delay = X`` MUST be seen
    by the executor's ``refresh_retry_delay_provider`` lambda on the next
    call.

    The integration-test contract is that
    ``client._composed.chain_host._refresh_retry_delay = 0`` continues
    to steer the live chain after construction. The lambda
    ``refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay``
    re-reads the attribute on every invocation, so this is a live binding,
    not a frozen snapshot.
    """
    holder = ClientComposed()
    internals = compose_client_internals(auth=_make_auth(), composed=holder)

    chain_host = holder.chain_host
    # The provider lambda must dereference the *current* attribute on
    # each call — not the value captured at construction time.
    initial = chain_host._refresh_retry_delay
    assert internals.executor._refresh_retry_delay_provider() == initial

    chain_host._refresh_retry_delay = 0.99
    assert internals.executor._refresh_retry_delay_provider() == 0.99


def test_compose_client_internals_executor_timeout_provider_reads_config() -> None:
    """The executor captures validated timeout without depending on root lifecycle."""
    internals = compose_client_internals(auth=_make_auth(), timeout=99.0)

    assert internals.executor._timeout_provider() == 99.0


# ---------------------------------------------------------------------------
# ClientComposed write-once binders
# ---------------------------------------------------------------------------


def test_client_composed_executor_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_executor already bound"):
        holder.bind_executor(holder.executor)


def test_client_composed_transport_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_transport already bound"):
        holder.bind_transport(holder.transport)


def test_client_composed_chain_metadata_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    # Build a sentinel ``WiredMiddleware`` carrying the existing values so
    # the rejection comes from the write-once guard, not a missing field.
    from notebooklm._runtime.init import WiredMiddleware

    wired = WiredMiddleware(
        chain_builder=holder.chain_builder,
        middlewares=holder.middlewares,
        authed_post_chain=holder.chain_host._authed_post_chain,
    )
    with pytest.raises(RuntimeError, match="_chain_builder already bound"):
        holder.bind_chain_metadata(wired)


def test_client_composed_chain_host_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_chain_host already bound"):
        holder.bind_chain_host(holder.chain_host)


def test_client_composed_runtime_collaborators_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    internals = compose_client_internals(auth=_make_auth(), composed=holder)
    holder.bind_runtime_collaborators(internals.collaborators)

    with pytest.raises(RuntimeError, match="_runtime_collaborators already bound"):
        holder.bind_runtime_collaborators(internals.collaborators)


# ---------------------------------------------------------------------------
# ClientComposed required-property guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr_name", "message"),
    [
        ("transport", "_transport"),
        ("executor", "_executor"),
        ("chain_host", "_chain_host"),
        ("chain_builder", "_chain_builder"),
        ("middlewares", "_middlewares"),
        ("runtime_collaborators", "_runtime_collaborators"),
    ],
)
def test_client_composed_properties_raise_before_binding(attr_name: str, message: str) -> None:
    holder = ClientComposed()

    with pytest.raises(
        RuntimeError,
        match=rf"ClientComposed not fully constructed: {message} is None",
    ):
        getattr(holder, attr_name)


def test_client_shell_reads_composition_from_client_composed() -> None:
    client = build_client_shell_for_tests(_make_auth())

    assert client._rpc_executor is client._composed.executor
    assert client._rpc_executor._transport is client._composed.transport
    assert client._composed.chain_host._transport is client._composed.transport
    assert not hasattr(client._collaborators, "drain_tracker")
    assert not hasattr(client._collaborators.call_supervisor, "drain_tracker")
    assert not hasattr(client._collaborators.call_supervisor, "max_concurrent_rpcs")
    assert not hasattr(client._collaborators.call_supervisor, "drain")
    assert client._composed.middlewares[0]._metrics is client._collaborators.metrics
