"""Stage B2 PR 2 — :class:`MiddlewareChainHost` live-binding contract.

The host owns the chain leaf, the chain slot, the three retry-budget
tunables, and the dynamic ``await_refresh`` delegate. The chain's
provider lambdas and the transport's ``chain_provider`` lambda both
capture the host directly (Stage B2 PR 2 signature split), so the
long-standing post-construction mutation patterns are now load-bearing
on the host itself rather than on :class:`Session`. These tests pin
that contract end-to-end:

* ``chain_host._rate_limit_max_retries = 0`` mid-flight steers the live
  retry budget (the :class:`RetryMiddleware` provider lambda reads the
  host slot on every attempt).
* ``chain_host._auth_refresh.await_refresh = fake`` rebind steers the
  live refresh path (dynamic delegation via
  :meth:`MiddlewareChainHost.await_refresh`).
* ``core._authed_post_chain = fake_chain`` writes through to
  ``chain_host._authed_post_chain``; the transport's ``chain_provider``
  lambda returns ``fake_chain`` on the next call.
* ``core._authed_post_chain_terminal = fake_terminal`` writes through to
  the host (mirrors the ``test_observability.py:77`` pattern).

The first two tests drive a real chain through
:meth:`SessionTransport.perform_authed_post`; the last two assert the
write-through descriptor contract without a live chain.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from _helpers.session_factory import build_session_for_tests
from conftest import install_post_as_stream
from notebooklm._middleware import RpcRequest, RpcResponse
from notebooklm._request_types import AuthSnapshot
from notebooklm._session import Session
from notebooklm.auth import AuthTokens


@pytest.fixture(autouse=True)
def _no_backoff_jitter(monkeypatch):
    """Pin retry backoff jitter to 0 for deterministic sleep assertions.

    Mirrors the ``_no_backoff_jitter`` fixture in
    ``test_authed_post_pipeline.py`` semantically — pin the ±20%
    exponential-backoff jitter to 0 so these chain-level tests can
    assert exact sleep schedules. Uses ADR-007 object-target
    monkeypatching: ``random`` is a singleton module, so patching
    ``random.uniform`` directly is functionally identical to patching
    ``notebooklm._session.random.uniform`` (the string-target form),
    but the object form is the ADR-007-preferred shape and keeps this
    file out of the forbidden-monkeypatch allowlist.
    """
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)


def _make_core(
    *,
    refresh_callback: Callable[[], Any] | None = None,
    rate_limit_max_retries: int = 0,
    server_error_max_retries: int = 0,
) -> Session:
    """Build a Session with a real chain wired against the host."""
    auth = AuthTokens(
        csrf_token="CSRF",
        session_id="SID",
        cookies={"SID": "sid_cookie"},
    )
    return build_session_for_tests(
        auth=auth,
        refresh_callback=refresh_callback,
        refresh_retry_delay=0.0,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
    )


def _ok_response(text: str = "OK") -> httpx.Response:
    return httpx.Response(
        200,
        text=text,
        request=httpx.Request("POST", "https://example.test/x"),
    )


def _status_error(code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("POST", "https://example.test/x")
    response = httpx.Response(code, request=request, headers=headers)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


# ---------------------------------------------------------------------------
# Test 1 — chain_host._rate_limit_max_retries mid-flight steers the live chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_host_rate_limit_max_retries_steers_live_chain(monkeypatch) -> None:
    """Mid-flight ``chain_host._rate_limit_max_retries = N`` steers the retry budget.

    Pins the Stage B2 PR 2 contract: the :class:`RetryMiddleware`'s
    ``rate_limit_max_retries`` provider lambda (built by
    :func:`wire_middleware_chain`) captures the host directly and reads
    ``chain_host._rate_limit_max_retries`` LIVE on every attempt. A test
    that bumps the budget AFTER ``open()`` still takes effect on the
    next chain call — preserving the pre-Stage-B2 contract where the
    provider lambda read ``session._rate_limit_max_retries``.

    Drives the chain via :meth:`SessionTransport.perform_authed_post`
    so the assertion exercises the production seam used by
    :meth:`RpcExecutor._execute_once`.
    """
    core = _make_core(rate_limit_max_retries=0)
    chain_host = core._chain_host
    await core.open()
    try:
        # Mutate the host slot directly (Stage B2 PR 2: the provider
        # lambda captures chain_host, NOT session). The bump from
        # 0 -> 1 grants a single retry on the next chain call.
        chain_host._rate_limit_max_retries = 1
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # ADR-007 object-target form. ``asyncio`` is a singleton module
        # so patching ``asyncio.sleep`` directly is functionally
        # identical to the string-target form
        # ``notebooklm._session.asyncio.sleep`` — both resolve to the
        # same callable on the same module object — while staying out
        # of the forbidden-monkeypatch allowlist.
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(429, retry_after="1")
            return _ok_response()

        install_post_as_stream(monkeypatch, core._kernel.get_http_client(), fake_post)

        response = await core._transport.perform_authed_post(
            build_request=build,
            log_label="test-rate-limit-host-steers",
        )

        assert response.status_code == 200
        # Exactly one retry attempt was made — the budget bump from
        # 0 -> 1 on chain_host took effect.
        assert call_count["n"] == 2
        assert sleeps == [1]
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# Test 2 — chain_host._auth_refresh.await_refresh rebind steers the live refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_host_auth_refresh_rebind_steers_live_refresh() -> None:
    """Rebinding ``chain_host._auth_refresh.await_refresh`` steers the live refresh.

    Pins the dynamic-delegation contract for
    :meth:`MiddlewareChainHost.await_refresh` (Stage B2 PR 1 + 2).
    :func:`wire_middleware_chain` passes ``chain_host.await_refresh`` as
    the chain's ``refresh_callable``. That method looks up
    ``self._auth_refresh.await_refresh`` on every call, so a
    fixture-time rebind of the coordinator's method keeps steering the
    live refresh path — preserving the long-standing test pattern that
    swaps the refresh implementation without rebuilding the chain.
    """
    core = _make_core()
    chain_host = core._chain_host

    fake_calls: list[None] = []

    async def fake_refresh() -> None:
        fake_calls.append(None)

    # Stage B2 PR 1's MiddlewareChainHost.await_refresh re-reads
    # self._auth_refresh.await_refresh on every call. Rebind the
    # coordinator's method and assert the host sees the new
    # implementation.
    chain_host._auth_refresh.await_refresh = fake_refresh  # type: ignore[method-assign]

    await chain_host.await_refresh()
    await chain_host.await_refresh()

    assert len(fake_calls) == 2


# ---------------------------------------------------------------------------
# Test 3 — core._authed_post_chain writes through to host slot;
#          transport's chain_provider returns the new chain on next call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authed_post_chain_write_through_to_host_steers_transport() -> None:
    """``core._authed_post_chain = fake_chain`` writes through to the host slot.

    The :class:`Session` descriptor setter writes through to
    ``chain_host._authed_post_chain``. The transport's
    ``chain_provider`` lambda (built in :func:`build_session_transport`
    after Stage B2 PR 2) captures the host directly and reads
    ``chain_host._authed_post_chain`` on every authed POST, so a
    post-construction fake-chain install reaches the next dispatch
    without any further mutation.

    Mirrors the ``test_authed_post_pipeline.py:113`` pattern but exists
    at this level to pin the host-side write-through contract
    independently of the larger pipeline test.
    """
    core = _make_core()
    chain_host = core._chain_host

    captured: list[RpcRequest] = []

    async def fake_chain(request: RpcRequest) -> RpcResponse:
        captured.append(request)
        return RpcResponse(response=_ok_response("fake-chain"), context=request.context)

    # Assignment goes through the :class:`Session` descriptor setter,
    # which writes through to ``chain_host._authed_post_chain``.
    core._authed_post_chain = fake_chain  # type: ignore[method-assign]

    # The Session descriptor read MUST resolve to the host slot.
    assert core._authed_post_chain is fake_chain
    # The host slot itself MUST hold the fake chain.
    assert chain_host._authed_post_chain is fake_chain

    # The transport's chain_provider lambda must return the fake on
    # the next dispatch. We invoke the lambda directly to assert the
    # live-binding contract without a full perform_authed_post run.
    assert core._transport._chain_provider() is fake_chain

    await core.open()
    try:

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {"X-Test": "yes"}

        response = await core._transport.perform_authed_post(
            build_request=build,
            log_label="test-chain-write-through",
        )

        # The fake chain produced the response — proves the transport's
        # chain_provider picked up the host slot value, not the original
        # wired chain.
        assert response.status_code == 200
        assert response.text == "fake-chain"
        assert len(captured) == 1
        assert captured[0].url == "https://example.test/x"
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# Test 4 — core._authed_post_chain_terminal writes through to host
#          (mirrors test_observability.py:77 pattern)
# ---------------------------------------------------------------------------


def test_authed_post_chain_terminal_write_through_to_host() -> None:
    """``core._authed_post_chain_terminal = fake`` writes through to the host.

    Mirrors the ``test_observability.py:77`` pattern: a test reassigns
    the chain leaf via the :class:`Session` descriptor setter to install
    a fake terminal; the setter writes the value through to
    ``chain_host._authed_post_chain_terminal``. Subsequent reads via
    either the descriptor (``core._authed_post_chain_terminal``) or the
    host slot directly (``chain_host._authed_post_chain_terminal``)
    resolve to the fake.

    The setter intentionally does NOT re-route an already-built chain —
    ``test_observability.py:82`` follows the assignment with
    ``core._authed_post_chain = build_chain(core._middlewares,
    fake_terminal)`` to rebuild the chain around the new terminal. The
    setter's only job is to accept the write; chain rebuild is the
    test's responsibility. This test only asserts the write-through
    contract; chain rebuild integration is covered by
    ``test_observability.py``.
    """
    auth = MagicMock()
    auth.storage_path = None
    auth.authuser = 0
    auth.account_email = None
    auth.csrf_token = "csrf-token"
    auth.session_id = "session-id"
    core = build_session_for_tests(auth=auth)
    chain_host = core._chain_host

    async def fake_terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=_ok_response("fake-terminal"), context=request.context)

    # Assignment goes through the :class:`Session` descriptor setter,
    # which writes through to ``chain_host._authed_post_chain_terminal``.
    core._authed_post_chain_terminal = fake_terminal  # type: ignore[method-assign]

    # Both surfaces must resolve to the fake.
    assert core._authed_post_chain_terminal is fake_terminal
    assert chain_host._authed_post_chain_terminal is fake_terminal


def test_chain_host_tunable_descriptor_read_after_write() -> None:
    """Session-side descriptor reads reflect host-side mutations.

    Stage B2 PR 2 makes the chain's provider lambdas capture the host
    directly, so a write to ``chain_host._refresh_retry_delay`` (or
    siblings) is visible through both surfaces — the host slot
    directly, and the :class:`Session` descriptor that forwards to it.
    Pins both directions so future refactors cannot break the read /
    write symmetry between the two surfaces.
    """
    auth = MagicMock()
    auth.storage_path = None
    auth.authuser = 0
    auth.account_email = None
    auth.csrf_token = "csrf-token"
    auth.session_id = "session-id"
    core = build_session_for_tests(auth=auth)
    chain_host = core._chain_host

    # Write through host -> read through Session descriptor.
    chain_host._refresh_retry_delay = 0.5
    chain_host._rate_limit_max_retries = 7
    chain_host._server_error_max_retries = 11
    assert core._refresh_retry_delay == 0.5
    assert core._rate_limit_max_retries == 7
    assert core._server_error_max_retries == 11

    # Write through Session descriptor -> read through host slot.
    core._refresh_retry_delay = 1.25
    core._rate_limit_max_retries = 2
    core._server_error_max_retries = 3
    assert chain_host._refresh_retry_delay == 1.25
    assert chain_host._rate_limit_max_retries == 2
    assert chain_host._server_error_max_retries == 3
