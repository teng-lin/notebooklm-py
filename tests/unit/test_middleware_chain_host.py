""":class:`MiddlewareChainHost` live-binding contract.

The host owns the chain leaf, the chain slot, the three retry-budget
tunables, and the dynamic ``await_refresh`` delegate. The chain's
provider lambdas and the transport's ``chain_provider`` lambda both
capture the host directly, so post-construction mutation patterns
are load-bearing on the host itself. These tests pin that contract
end-to-end:

* ``chain_host._rate_limit_max_retries = 0`` mid-flight steers the live
  retry budget (the :class:`RetryMiddleware` provider lambda reads the
  host slot on every attempt).
* ``chain_host._auth_refresh.await_refresh = fake`` rebind steers the
  live refresh path (dynamic delegation via
  :meth:`MiddlewareChainHost.await_refresh`).
* ``chain_host._authed_post_chain = fake_chain`` installs a fake chain
  that the transport's ``chain_provider`` lambda returns on the next
  call.
* ``chain_host._authed_post_chain_terminal = fake_terminal`` installs
  a fake chain leaf (mirrors the ``test_observability.py`` rebind
  pattern).

The first two tests drive a real chain through
:meth:`RuntimeTransport.perform_authed_post`; the last two assert the
host-side rebind contract without a live chain.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from notebooklm._middleware.core import RpcRequest, RpcResponse
from notebooklm._request_types import AuthSnapshot
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._helpers.client_factory import build_client_shell_for_tests
from tests.unit.conftest import install_post_as_stream


@pytest.fixture(autouse=True)
def _no_backoff_jitter(monkeypatch):
    """Pin retry backoff jitter to 0 for deterministic sleep assertions.

    Mirrors the ``_no_backoff_jitter`` fixture in
    ``test_authed_post_pipeline.py`` semantically — pin the ±20%
    exponential-backoff jitter to 0 so these chain-level tests can
    assert exact sleep schedules. Uses ADR-0007 object-target
    monkeypatching: ``random`` is a singleton module, so patching
    ``random.uniform`` directly is functionally identical to patching
    ``notebooklm._backoff._random.uniform`` (the string-target form),
    but the object form is the ADR-0007-preferred shape and keeps this
    file out of the forbidden-monkeypatch allowlist.
    """
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)


def _make_core(
    *,
    refresh_callback: Callable[[], Any] | None = None,
    rate_limit_max_retries: int = 0,
    server_error_max_retries: int = 0,
) -> NotebookLMClient:
    """Build a NotebookLMClient with a real chain wired against the host."""
    auth = AuthTokens(
        csrf_token="CSRF",
        session_id="SID",
        cookies={"SID": "sid_cookie"},
    )
    return build_client_shell_for_tests(
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

    Pins the contract: the :class:`RetryMiddleware`'s
    ``rate_limit_max_retries`` provider lambda (built by
    :func:`wire_middleware_chain`) captures the host directly and reads
    ``chain_host._rate_limit_max_retries`` LIVE on every attempt. A test
    that bumps the budget AFTER ``open()`` still takes effect on the
    next chain call.

    Drives the chain via :meth:`RuntimeTransport.perform_authed_post`
    so the assertion exercises the production seam used by
    :meth:`RpcExecutor._execute_once`.
    """
    core = _make_core(rate_limit_max_retries=0)
    chain_host = core._composed.chain_host
    await core.__aenter__()
    try:
        # Mutate the host slot directly — the provider lambda captures
        # chain_host. The bump from 0 -> 1 grants a single retry on
        # the next chain call.
        chain_host._rate_limit_max_retries = 1
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # ADR-0007 object-target form. ``asyncio`` is a singleton module
        # so patching ``asyncio.sleep`` directly is functionally
        # identical to the string-target form
        # ``notebooklm._runtime.helpers.asyncio.sleep`` — both resolve to the
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

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
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
    chain_host = core._composed.chain_host

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
# Test 3 — chain_host._authed_post_chain steers the transport's chain_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authed_post_chain_on_host_steers_transport() -> None:
    """``chain_host._authed_post_chain = fake_chain`` steers the live transport.

    The transport's ``chain_provider`` lambda (built in
    :func:`build_runtime_transport`) captures the host directly and
    reads ``chain_host._authed_post_chain`` on every authed POST, so a
    post-construction fake-chain install reaches the next dispatch
    without any further mutation.

    Mirrors the ``test_authed_post_pipeline.py`` rebind pattern but
    exists at this level to pin the host-side contract independently
    of the larger pipeline test.
    """
    core = _make_core()
    chain_host = core._composed.chain_host

    captured: list[RpcRequest] = []

    async def fake_chain(request: RpcRequest) -> RpcResponse:
        captured.append(request)
        return RpcResponse(response=_ok_response("fake-chain"), context=request.context)

    # Install the fake chain directly on the host — there is no
    # NotebookLMClient-side alias.
    chain_host._authed_post_chain = fake_chain

    # The host slot holds the fake chain.
    assert chain_host._authed_post_chain is fake_chain

    # The transport's chain_provider lambda must return the fake on
    # the next dispatch. We invoke the lambda directly to assert the
    # live-binding contract without a full perform_authed_post run.
    assert core._composed.transport._chain_provider() is fake_chain

    await core.__aenter__()
    try:

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {"X-Test": "yes"}

        response = await core._composed.transport.perform_authed_post(
            build_request=build,
            log_label="test-chain-host-steers-transport",
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
# Test 4 — chain_host._authed_post_chain_terminal can be rebound directly
# ---------------------------------------------------------------------------


def test_authed_post_chain_terminal_on_host_is_rebindable() -> None:
    """``chain_host._authed_post_chain_terminal = fake`` installs a fake terminal.

    Mirrors the ``test_observability.py`` rebind pattern: a test
    swaps the chain leaf on the host and rebuilds the chain around
    the new terminal (``chain_host._authed_post_chain =
    build_chain(core._composed.middlewares, fake_terminal)``). This test only
    asserts the host-side rebind contract; chain-rebuild integration
    is covered by ``test_observability.py``.
    """
    auth = MagicMock()
    auth.storage_path = None
    auth.authuser = 0
    auth.account_email = None
    auth.csrf_token = "csrf-token"
    auth.session_id = "session-id"
    core = build_client_shell_for_tests(auth=auth)
    chain_host = core._composed.chain_host

    async def fake_terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=_ok_response("fake-terminal"), context=request.context)

    # Rebind directly on the host.
    chain_host._authed_post_chain_terminal = fake_terminal  # type: ignore[method-assign]
    assert chain_host._authed_post_chain_terminal is fake_terminal


def test_chain_host_tunable_attributes_are_writable() -> None:
    """The chain-host retry-budget attributes accept post-construction writes.

    The chain's provider lambdas capture the host directly, so a
    write to ``chain_host._refresh_retry_delay`` (or siblings) is
    visible to the live chain on the next attempt. Pins the
    plain-attribute contract on :class:`MiddlewareChainHost`.
    """
    auth = MagicMock()
    auth.storage_path = None
    auth.authuser = 0
    auth.account_email = None
    auth.csrf_token = "csrf-token"
    auth.session_id = "session-id"
    core = build_client_shell_for_tests(auth=auth)
    chain_host = core._composed.chain_host

    chain_host._refresh_retry_delay = 0.5
    chain_host._rate_limit_max_retries = 7
    chain_host._server_error_max_retries = 11
    assert chain_host._refresh_retry_delay == 0.5
    assert chain_host._rate_limit_max_retries == 7
    assert chain_host._server_error_max_retries == 11

    chain_host._refresh_retry_delay = 1.25
    chain_host._rate_limit_max_retries = 2
    chain_host._server_error_max_retries = 3
    assert chain_host._refresh_retry_delay == 1.25
    assert chain_host._rate_limit_max_retries == 2
    assert chain_host._server_error_max_retries == 3
