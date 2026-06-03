from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from _helpers.client_factory import build_client_shell_for_tests
from notebooklm._logging import get_request_id, reset_request_id, set_request_id
from notebooklm._request_types import AuthSnapshot
from notebooklm._rpc_executor import RpcExecutor
from notebooklm.auth import AuthTokens
from notebooklm.rpc import (
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCMethod,
    RPCTimeoutError,
    ServerError,
)


def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "sid_cookie"},
        csrf_token="CSRF",
        session_id="SID",
    )


def _ok_response(text: str = "raw") -> httpx.Response:
    return httpx.Response(
        200,
        text=text,
        request=httpx.Request("POST", "https://example.test/rpc"),
    )


def _status_error(status_code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/rpc")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


class _Owner:
    """Test stub satisfying RpcExecutor's four collaborator dependencies.

    Wave 4 of session-decoupling (ADR-014 Rule 5): RpcExecutor takes
    Kernel + RuntimeTransport + AuthRefreshCoordinator + ClientMetrics
    directly via keyword arguments. This stub plays all four roles in
    one object — see :func:`_executor` for the wiring.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        refresh_callback: Callable[[], Awaitable[Any]] | None = None,
        refresh_retry_delay: float = 0.0,
    ):
        self._timeout = timeout
        self._refresh_callback = refresh_callback
        self._refresh_retry_delay = refresh_retry_delay
        self.perform_calls: list[dict[str, Any]] = []
        self.refresh_calls = 0
        self.metric_increments: list[dict[str, int | float]] = []
        self.response = _ok_response()
        self.snapshot = AuthSnapshot(
            csrf_token="CSRF_SNAPSHOT",
            session_id="SID_SNAPSHOT",
            authuser=1,
            account_email="user@example.test",
        )
        # Self-reference so the same stub can play both ``kernel`` and the
        # other three roles when passed to ``RpcExecutor(...)`` below.
        self._kernel = self

    # --- Kernel role ----------------------------------------------------
    def get_http_client(self) -> object:
        return object()

    # --- ClientMetrics role ---------------------------------------------
    def increment(self, **increments: int | float) -> None:
        self.metric_increments.append(increments)

    # --- RuntimeTransport role ------------------------------------------
    async def perform_authed_post(
        self,
        *,
        build_request,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
        refresh_budget: Any = None,
    ) -> httpx.Response:
        url, body, headers = build_request(self.snapshot)
        self.perform_calls.append(
            {
                "log_label": log_label,
                "disable_internal_retries": disable_internal_retries,
                "url": url,
                "body": body,
                "headers": headers,
                "refresh_budget": refresh_budget,
            }
        )
        return self.response

    # --- AuthRefreshCoordinator role ------------------------------------
    async def await_refresh(self) -> None:
        self.refresh_calls += 1


def _executor(
    owner: _Owner,
    *,
    decode_response: Callable[..., Any] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> RpcExecutor:
    async def _no_sleep(_: float) -> None:
        return None

    def _decode(_: str, rpc_id: str, *, allow_null: bool = False) -> dict[str, Any]:
        return {"rpc_id": rpc_id, "allow_null": allow_null}

    # ADR-014 Rule 5 (Wave 4 of session-decoupling): the executor takes
    # its four collaborators as keyword-only args. The ``_Owner`` stub
    # plays all four roles; pass it under each keyword so the executor's
    # ``self._kernel`` / ``self._metrics`` / ``self._transport`` /
    # ``self._auth_refresh`` references all land on the same stub.
    return RpcExecutor(
        kernel=owner,  # type: ignore[arg-type]
        transport=owner,  # type: ignore[arg-type]
        auth_refresh=owner,  # type: ignore[arg-type]
        metrics=owner,  # type: ignore[arg-type]
        decode_response=decode_response or _decode,
        is_auth_error=is_auth_error or (lambda exc: False),
        sleep=sleep or _no_sleep,
        timeout_provider=lambda: owner._timeout,
        refresh_callback_enabled_provider=lambda: owner._refresh_callback is not None,
        refresh_retry_delay_provider=lambda: owner._refresh_retry_delay,
    )


@pytest.mark.asyncio
async def test_rpc_executor_attribute_is_dispatched_through(monkeypatch) -> None:
    """``core._rpc_executor`` is the canonical RPC dispatch seam."""
    core = build_client_shell_for_tests(_auth_tokens())
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class FakeExecutor:
        async def rpc_call(self, *args: Any, **kwargs: Any) -> str:
            calls.append(("rpc_call", args, kwargs))
            return "retried"

    executor = FakeExecutor()
    # Stage B1 PR 2 deleted ``Session._get_rpc_executor`` (the lazy
    # factory) — the executor now lives directly on ``core._rpc_executor``
    # post-composition. Override the attribute so every caller that
    # dispatches through ``core._rpc_executor.rpc_call(...)`` sees the
    # fake.
    monkeypatch.setattr(core, "_rpc_executor", executor)

    assert (
        await core._rpc_executor.rpc_call(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
            disable_internal_retries=True,
        )
        == "retried"
    )

    assert [name for name, _, _ in calls] == ["rpc_call"]
    # Only ``disable_internal_retries`` is passed by the test; the
    # ``operation_variant`` kwarg defaults to ``None`` at the executor
    # level and is not bound by the dispatch site here.
    assert calls[0][2] == {
        "disable_internal_retries": True,
    }


@pytest.mark.asyncio
async def test_rpc_call_wraps_execute_once_with_metrics_and_request_id(monkeypatch) -> None:
    owner = _Owner()
    executor = _executor(owner)
    captured_ids: list[str | None] = []

    async def fake_execute_once(*args: Any, **kwargs: Any) -> str:
        captured_ids.append(get_request_id())
        return "ok"

    monkeypatch.setattr(executor, "_execute_once", fake_execute_once)

    result = await executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    assert result == "ok"
    assert owner.metric_increments == [{"rpc_calls_started": 1}]
    assert captured_ids[0] is not None
    assert get_request_id() is None

    owner.metric_increments.clear()
    token = set_request_id("parent-req")
    try:
        retry_result = await executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [], _is_retry=True)
        assert retry_result == "ok"
        assert captured_ids[-1] == "parent-req"
    finally:
        reset_request_id(token)

    assert owner.metric_increments == []


@pytest.mark.asyncio
async def test_constructor_injected_decode_response_drives_executor(monkeypatch) -> None:
    """Pin that the constructor-injected ``decode_response`` reaches the executor.

    The legacy module-level ``_decode_response_late_bound`` wrapper used to
    re-import ``notebooklm.rpc.decode_response`` on every call, so a late
    string-target monkeypatch of that module attribute (after the executor
    was already constructed) still affected the live decode path.
    The constructor-DI seam (``Session(..., decode_response=…)``) intentionally
    captures the callable at construction time — see
    ``docs/improvement.md`` §4.1. This test asserts the new contract: the
    injected callable reaches :class:`RpcExecutor` end-to-end.
    """
    decode_calls: list[dict[str, Any]] = []

    def fake_decode(raw: str, rpc_id: str, *, allow_null: bool = False) -> dict[str, Any]:
        decode_calls.append({"raw": raw, "rpc_id": rpc_id, "allow_null": allow_null})
        return {"decoded": rpc_id}

    core = build_client_shell_for_tests(_auth_tokens(), decode_response=fake_decode)
    executor = core._rpc_executor

    async def fake_perform_authed_post(
        *,
        build_request,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
        refresh_budget: Any = None,
    ) -> httpx.Response:
        return _ok_response("wire")

    # ADR-014 Rule 5 (Wave 4 of session-decoupling): the executor calls
    # ``self._transport.perform_authed_post(...)`` directly instead of
    # routing through ``Session._perform_authed_post``. Patch the
    # collaborator the executor actually reaches.
    monkeypatch.setattr(core._composed.transport, "perform_authed_post", fake_perform_authed_post)

    result = await executor._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/notebook/abc",
        True,
        False,
    )

    assert core._rpc_executor is executor
    assert result == {"decoded": RPCMethod.LIST_NOTEBOOKS.value}
    assert decode_calls == [
        {
            "raw": "wire",
            "rpc_id": RPCMethod.LIST_NOTEBOOKS.value,
            "allow_null": True,
        }
    ]


@pytest.mark.asyncio
async def test_execute_threads_override_source_allow_null_and_retry_flag(monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "OverrideRpc"}')
    owner = _Owner()
    decode_calls: list[dict[str, Any]] = []

    def decode(raw: str, rpc_id: str, *, allow_null: bool = False) -> dict[str, Any]:
        decode_calls.append({"raw": raw, "rpc_id": rpc_id, "allow_null": allow_null})
        return {"ok": True}

    result = await _executor(owner, decode_response=decode)._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        [["param"]],
        "/notebook/abc",
        True,
        False,
        disable_internal_retries=True,
    )

    assert result == {"ok": True}
    assert owner.perform_calls[0]["log_label"] == "RPC LIST_NOTEBOOKS"
    assert owner.perform_calls[0]["disable_internal_retries"] is True
    url = httpx.URL(owner.perform_calls[0]["url"])
    assert url.params["rpcids"] == "OverrideRpc"
    assert url.params["source-path"] == "/notebook/abc"
    assert url.params["f.sid"] == "SID_SNAPSHOT"
    assert url.params["authuser"] == "user@example.test"
    body = httpx.QueryParams(owner.perform_calls[0]["body"])
    assert body["at"] == "CSRF_SNAPSHOT"
    assert '"OverrideRpc"' in body["f.req"]
    assert decode_calls == [{"raw": "raw", "rpc_id": "OverrideRpc", "allow_null": True}]


@pytest.mark.asyncio
async def test_decode_time_auth_retry_uses_injected_collaborators() -> None:
    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback, refresh_retry_delay=0.25)
    sleep_calls: list[float] = []
    is_auth_error_calls: list[Exception] = []
    decode_allow_nulls: list[bool] = []

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        decode_allow_nulls.append(allow_null)
        if len(decode_allow_nulls) == 1:
            raise RPCError("not matched by the built-in auth detector")
        return {"retried": True}

    def is_auth_error(exc: Exception) -> bool:
        is_auth_error_calls.append(exc)
        return True

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    # ``LIST_NOTEBOOKS`` is IDEMPOTENT_SET_OP and the caller passes
    # ``disable_internal_retries=False``, so the effective disable flag is
    # False and the decode-time auth retry is permitted to fire. The
    # non-idempotent skip path is covered separately by
    # ``test_decode_time_auth_retry_skipped_for_non_idempotent_method``.
    result = await _executor(
        owner,
        decode_response=decode,
        is_auth_error=is_auth_error,
        sleep=sleep,
    )._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        ["param"],
        "/notebook/abc",
        True,
        False,
        disable_internal_retries=False,
    )

    assert result == {"retried": True}
    assert owner.refresh_calls == 1
    assert sleep_calls == [0.25]
    assert len(is_auth_error_calls) == 1
    assert decode_allow_nulls == [True, True]
    assert len(owner.perform_calls) == 2
    assert [call["disable_internal_retries"] for call in owner.perform_calls] == [False, False]


@pytest.mark.asyncio
async def test_decode_time_auth_retry_gives_up_when_aggregate_deadline_exhausted() -> None:
    """Issue #1271: an exhausted aggregate deadline gives up after the refresh.

    The executor mints a ``RuntimeDeadline`` from ``timeout_provider`` for the
    logical call. With a zero aggregate timeout the deadline is already
    exhausted, so after the (productive) refresh the executor must NOT sleep the
    large ``refresh_retry_delay`` and must NOT issue a retry POST that would run
    past the budget — it re-raises the original decoded auth error, symmetric
    with ``RetryMiddleware`` re-raising instead of re-invoking the chain.
    """

    async def refresh_callback() -> object:
        return object()

    owner = _Owner(
        refresh_callback=refresh_callback,
        refresh_retry_delay=100.0,
        timeout=0.0,
    )
    sleep_calls: list[float] = []
    auth_rpc_error = RPCError("authentication expired")
    decode_calls = 0

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        nonlocal decode_calls
        decode_calls += 1
        raise auth_rpc_error

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with pytest.raises(RPCError) as raised:
        await _executor(
            owner,
            decode_response=decode,
            is_auth_error=lambda exc: True,
            sleep=sleep,
        )._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            ["param"],
            "/notebook/abc",
            True,
            False,
            disable_internal_retries=False,
        )

    # The refresh still ran (productive: the next call benefits from the fresh
    # token), but the exhausted budget suppressed both the post-refresh sleep
    # and the retry POST. The original decoded auth error propagates.
    assert raised.value is auth_rpc_error
    assert owner.refresh_calls == 1
    assert sleep_calls == []
    # Exactly one transport POST — the retry was NOT issued past the deadline.
    assert len(owner.perform_calls) == 1
    assert decode_calls == 1


@pytest.mark.asyncio
async def test_decode_time_auth_retry_preserves_none_result() -> None:
    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    decode_calls = 0

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        nonlocal decode_calls
        decode_calls += 1
        if decode_calls == 1:
            raise RPCError("authentication expired")
        return None

    result = await _executor(
        owner,
        decode_response=decode,
        is_auth_error=lambda exc: True,
    )._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        True,
        False,
    )

    assert result is None
    assert owner.refresh_calls == 1
    assert decode_calls == 2


@pytest.mark.asyncio
async def test_decode_time_auth_retry_skipped_for_non_idempotent_method() -> None:
    """A non-idempotent create is NOT replayed on a decode-time auth error.

    Regression for issue #1157: ``CREATE_NOTEBOOK`` is PROBE_THEN_CREATE, so
    ``resolve_effective_disable_internal_retries`` forces the effective
    disable flag True even though the caller passed False. The server may
    have already committed the notebook before the auth-shaped ``RPCError``
    surfaced; re-POSTing would duplicate it. The original error must
    propagate so the caller's probe-then-create wrapper can disambiguate.
    """

    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    auth_rpc_error = RPCError("authentication expired")

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise auth_rpc_error

    with pytest.raises(RPCError) as raised:
        await _executor(
            owner,
            decode_response=decode,
            is_auth_error=lambda exc: True,
        )._execute_once(
            RPCMethod.CREATE_NOTEBOOK,
            ["param"],
            "/",
            False,
            False,
            disable_internal_retries=False,
        )

    assert raised.value is auth_rpc_error
    assert owner.refresh_calls == 0
    # Exactly one POST — the create is never replayed.
    assert len(owner.perform_calls) == 1


@pytest.mark.asyncio
async def test_decode_time_auth_retry_skipped_when_caller_disables_retries() -> None:
    """A caller-set ``disable_internal_retries`` also suppresses the replay.

    Even for an otherwise retry-safe method (``LIST_NOTEBOOKS`` is
    IDEMPOTENT_SET_OP), an explicit ``disable_internal_retries=True`` means
    the caller has opted out of any internal re-issue. The decode-time auth
    leg must honor that effective flag rather than blindly re-POST.
    """

    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    auth_rpc_error = RPCError("authentication expired")

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise auth_rpc_error

    with pytest.raises(RPCError) as raised:
        await _executor(
            owner,
            decode_response=decode,
            is_auth_error=lambda exc: True,
        )._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
            disable_internal_retries=True,
        )

    assert raised.value is auth_rpc_error
    assert owner.refresh_calls == 0
    assert len(owner.perform_calls) == 1


@pytest.mark.asyncio
async def test_decode_time_auth_retry_threads_refresh_budget_to_transport() -> None:
    """Issue #1205: the executor seeds the chain with the shared RefreshBudget.

    The same budget instance reaches ``perform_authed_post`` (so the
    HTTP-status layer can consume it) on BOTH the initial attempt and the
    decode-time retry. The budget is consumed by the decode-time refresh, so
    the retry leg's transport call carries a spent budget.
    """
    from notebooklm._auth_refresh_retry import RefreshBudget

    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    decode_calls = 0

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        nonlocal decode_calls
        decode_calls += 1
        if decode_calls == 1:
            raise RPCError("authentication expired")
        return {"ok": True}

    result = await _executor(
        owner,
        decode_response=decode,
        is_auth_error=lambda exc: True,
    )._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        False,
        False,
    )

    assert result == {"ok": True}
    # Two transport calls (initial + decode-time retry); both carry the SAME
    # budget instance, which is spent after the decode-time refresh.
    budgets = [call["refresh_budget"] for call in owner.perform_calls]
    assert len(budgets) == 2
    assert all(isinstance(b, RefreshBudget) for b in budgets)
    assert budgets[0] is budgets[1]
    assert budgets[0].available is False


@pytest.mark.asyncio
async def test_decode_time_auth_retry_skips_when_shared_budget_already_spent() -> None:
    """Issue #1205: a budget already consumed (e.g. by the HTTP-status layer)
    suppresses the decode-time refresh.

    Mirrors the production sequence where ``AuthRefreshMiddleware`` refreshed
    on a wire-401, consumed the shared budget, and the post-refresh retry
    returned a decoded auth error: the executor must NOT refresh a second time.
    """
    from notebooklm._auth_refresh_retry import RefreshBudget

    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    auth_rpc_error = RPCError("authentication expired")

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise auth_rpc_error

    spent_budget = RefreshBudget()
    assert spent_budget.consume() is True  # pre-spend it

    with pytest.raises(RPCError) as raised:
        await _executor(
            owner,
            decode_response=decode,
            is_auth_error=lambda exc: True,
        )._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
            _refresh_budget=spent_budget,
        )

    assert raised.value is auth_rpc_error
    assert owner.refresh_calls == 0
    # Exactly one POST — no decode-time refresh-and-retry replay.
    assert len(owner.perform_calls) == 1


@pytest.mark.asyncio
async def test_decode_time_auth_retry_increments_auth_retry_metric() -> None:
    """Issue #1205: the decode-time refresh leg counts ``rpc_auth_retries``.

    Before consolidation only the HTTP-status layer incremented this metric;
    the shared refresh body now counts on both layers.
    """

    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    decode_calls = 0

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        nonlocal decode_calls
        decode_calls += 1
        if decode_calls == 1:
            raise RPCError("authentication expired")
        return {"ok": True}

    await _executor(
        owner,
        decode_response=decode,
        is_auth_error=lambda exc: True,
    )._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        False,
        False,
    )

    assert {"rpc_auth_retries": 1} in owner.metric_increments


@pytest.mark.asyncio
async def test_constructor_injected_sleep_drives_executor(monkeypatch) -> None:
    """Pin that the constructor-injected ``sleep`` reaches the executor.

    The legacy module-level ``_sleep_late_bound`` wrapper used to re-import
    ``asyncio.sleep`` on every call, so a late string-target monkeypatch of
    the ``notebooklm._runtime.helpers`` ``asyncio.sleep`` attribute (after the
    executor was already constructed) still affected the live sleep path.
    The constructor-DI seam (``Session(..., sleep=…)``) intentionally captures
    the callable at construction time — see ``docs/improvement.md`` §4.1.
    This test asserts the new contract: the injected callable reaches
    :class:`RpcExecutor`'s refresh-and-retry delay.
    """

    async def refresh_callback() -> AuthTokens:
        return _auth_tokens()

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    core = build_client_shell_for_tests(
        _auth_tokens(),
        refresh_callback=refresh_callback,
        refresh_retry_delay=0.5,
        sleep=fake_sleep,
    )
    executor = core._rpc_executor
    refresh_calls = 0

    async def fake_await_refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    async def fake_rpc_call(
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        _refresh_budget: Any = None,
        _retry_deadline: Any = None,
    ) -> dict[str, bool]:
        assert method is RPCMethod.LIST_NOTEBOOKS
        assert params == ["param"]
        assert source_path == "/notebook/abc"
        assert allow_null is True
        assert _is_retry is True
        assert disable_internal_retries is True
        assert operation_variant is None
        return {"ok": True}

    # ADR-014 Rule 5 (Wave 4): executor calls ``self._auth_refresh.await_refresh()``
    # directly. Patch the collaborator the executor actually reaches.
    monkeypatch.setattr(core._collaborators.auth_coord, "await_refresh", fake_await_refresh)
    monkeypatch.setattr(executor, "rpc_call", fake_rpc_call)

    from notebooklm._auth_refresh_retry import RefreshBudget

    result = await executor.try_refresh_and_retry(
        RPCMethod.LIST_NOTEBOOKS,
        ["param"],
        "/notebook/abc",
        True,
        RPCError("auth"),
        disable_internal_retries=True,
        _refresh_budget=RefreshBudget(),
    )

    assert core._rpc_executor is executor
    assert result == {"ok": True}
    assert refresh_calls == 1
    assert sleep_calls == [0.5]


@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_attr"),
    [
        (_status_error(429, retry_after="7"), RateLimitError, ("retry_after", 7)),
        (_status_error(404), ClientError, ("status_code", 404)),
        (_status_error(502), ServerError, ("status_code", 502)),
        (_status_error(401), RPCError, ("method_id", RPCMethod.LIST_NOTEBOOKS.value)),
    ],
)
def test_http_status_error_mapper_parity(
    exc: httpx.HTTPStatusError,
    expected_type: type[Exception],
    expected_attr: tuple[str, Any],
) -> None:
    executor = _executor(_Owner())

    with pytest.raises(expected_type) as raised:
        executor.raise_rpc_error_from_http_status(exc, RPCMethod.LIST_NOTEBOOKS)

    attr, value = expected_attr
    assert getattr(raised.value, attr) == value


def test_request_error_mapper_uses_owner_timeout_seconds() -> None:
    executor = _executor(_Owner(timeout=12.5))

    with pytest.raises(RPCTimeoutError) as raised:
        executor.raise_rpc_error_from_request_error(
            httpx.ReadTimeout("slow"),
            RPCMethod.LIST_NOTEBOOKS,
        )

    assert raised.value.timeout_seconds == 12.5


@pytest.mark.parametrize(
    ("exc", "expected_type"),
    [
        (httpx.ConnectTimeout("connect slow"), NetworkError),
        (httpx.ConnectError("connect failed"), NetworkError),
        (httpx.ReadError("read failed"), NetworkError),
    ],
)
def test_request_error_mapper_parity(
    exc: httpx.RequestError, expected_type: type[Exception]
) -> None:
    executor = _executor(_Owner())

    with pytest.raises(expected_type):
        executor.raise_rpc_error_from_request_error(exc, RPCMethod.LIST_NOTEBOOKS)


# =============================================================================
# decode-time exception surface contract
#
# The ``except`` at ``_rpc_executor.py::RpcExecutor._execute_once`` only wraps genuine
# shape-drift exceptions (``json.JSONDecodeError``, ``KeyError``, ``IndexError``,
# ``TypeError``) as ``RPCError``. Code bugs (``AttributeError`` and friends)
# must propagate unmasked. These tests pin that contract.
# =============================================================================


@pytest.mark.parametrize(
    ("decoder_exc_factory", "_label"),
    [
        (lambda: KeyError("missing"), "KeyError"),
        (lambda: IndexError("oob"), "IndexError"),
        (lambda: TypeError("bad type"), "TypeError"),
    ],
)
@pytest.mark.asyncio
async def test_decode_shape_error_wrapped(
    decoder_exc_factory: Callable[[], Exception], _label: str
) -> None:
    """Genuine shape-drift exceptions get wrapped as ``RPCError`` with the
    ``Failed to decode response`` message and the original cause chained
    via ``__cause__``.
    """
    decoder_exc = decoder_exc_factory()
    owner = _Owner()

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise decoder_exc

    with pytest.raises(RPCError) as raised:
        await _executor(owner, decode_response=decode)._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
        )

    assert "Failed to decode response for LIST_NOTEBOOKS" in str(raised.value)
    assert raised.value.method_id == RPCMethod.LIST_NOTEBOOKS.value
    assert raised.value.__cause__ is decoder_exc


@pytest.mark.asyncio
async def test_decode_shape_error_json_decode_wrapped() -> None:
    """``json.JSONDecodeError`` (a ``ValueError`` subclass) is wrapped too —
    it's explicitly named in the narrow tuple at the catch site so callers
    don't have to depend on the ``ValueError`` base-class relationship.
    """
    import json as _json

    owner = _Owner()
    decoder_exc = _json.JSONDecodeError("expecting value", "doc", 0)

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise decoder_exc

    with pytest.raises(RPCError) as raised:
        await _executor(owner, decode_response=decode)._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
        )

    assert "Failed to decode response for LIST_NOTEBOOKS" in str(raised.value)
    assert raised.value.__cause__ is decoder_exc


@pytest.mark.asyncio
async def test_rpc_error_log_includes_class_code_and_retry_after(caplog) -> None:
    """Decode-time RPCError logs carry enough non-sensitive CI diagnostics."""
    owner = _Owner()

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise RateLimitError(
            "quota",
            method_id=RPCMethod.START_DEEP_RESEARCH.value,
            rpc_code="USER_DISPLAYABLE_ERROR",
            retry_after=30,
        )

    with (
        caplog.at_level(logging.ERROR, logger="notebooklm._rpc_executor"),
        pytest.raises(RateLimitError),
    ):
        await _executor(owner, decode_response=decode)._execute_once(
            RPCMethod.START_DEEP_RESEARCH,
            [],
            "/",
            False,
            False,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "RPC START_DEEP_RESEARCH failed" in message
        and "RateLimitError" in message
        and "rpc_code=USER_DISPLAYABLE_ERROR" in message
        and "retry_after=30" in message
        for message in messages
    )


@pytest.mark.parametrize(
    "decoder_exc_factory",
    [
        lambda: AttributeError("typo: response.gotcha"),
        lambda: NameError("undefined name"),
        lambda: RuntimeError("invariant broken"),
        lambda: ZeroDivisionError("oops"),
        # Bare ``ValueError`` (not a ``JSONDecodeError``) — e.g. ``int("bad")``
        # or a ``uuid.UUID("...")`` failure inside a decoder. Only the
        # ``JSONDecodeError`` subclass is in the narrow tuple, so a bare
        # ``ValueError`` MUST propagate unmasked. The new test guards
        # against accidental future widening of the catch tuple.
        lambda: ValueError("non-json value error"),
    ],
)
@pytest.mark.asyncio
async def test_decode_code_bug_propagates(
    decoder_exc_factory: Callable[[], Exception],
) -> None:
    """Code-bug exceptions (``AttributeError``, ``NameError``, generic
    ``RuntimeError``, bare ``ValueError`` that isn't a ``JSONDecodeError``,
    etc.) propagate as their native type — they are NOT wrapped as
    ``RPCError``. This is what surfaces decoder typos and broken
    invariants instead of masking them as "API drift."
    """
    decoder_exc = decoder_exc_factory()
    owner = _Owner()

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise decoder_exc

    with pytest.raises(type(decoder_exc)) as raised:
        await _executor(owner, decode_response=decode)._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
        )

    assert raised.value is decoder_exc
