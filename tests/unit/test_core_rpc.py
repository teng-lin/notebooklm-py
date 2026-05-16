from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm._core_rpc import RpcExecutor
from notebooklm._core_transport import _AuthSnapshot
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
        self.rpc_retry_calls: list[dict[str, Any]] = []
        self.rpc_retry_result: Any = {"retried": True}
        self.response = _ok_response()
        self.snapshot = _AuthSnapshot(
            csrf_token="CSRF_SNAPSHOT",
            session_id="SID_SNAPSHOT",
            authuser=1,
            account_email="user@example.test",
        )

    async def _perform_authed_post(
        self,
        *,
        build_request,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        url, body, headers = build_request(self.snapshot)
        self.perform_calls.append(
            {
                "log_label": log_label,
                "disable_internal_retries": disable_internal_retries,
                "url": url,
                "body": body,
                "headers": headers,
            }
        )
        return self.response

    async def _await_refresh(self) -> None:
        self.refresh_calls += 1

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        self.rpc_retry_calls.append(
            {
                "method": method,
                "params": params,
                "source_path": source_path,
                "allow_null": allow_null,
                "_is_retry": _is_retry,
                "disable_internal_retries": disable_internal_retries,
            }
        )
        return self.rpc_retry_result


def _executor(
    owner: _Owner,
    *,
    decode_response_late_bound: Callable[..., Any] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> RpcExecutor:
    async def _no_sleep(_: float) -> None:
        return None

    def _decode(_: str, rpc_id: str, *, allow_null: bool = False) -> dict[str, Any]:
        return {"rpc_id": rpc_id, "allow_null": allow_null}

    return RpcExecutor(
        owner,
        decode_response_late_bound=decode_response_late_bound or _decode,
        is_auth_error=is_auth_error or (lambda exc: False),
        sleep=sleep or _no_sleep,
    )


def test_core_rpc_has_no_runtime_core_imports() -> None:
    path = Path(__file__).parents[2] / "src/notebooklm/_core_rpc.py"
    tree = ast.parse(path.read_text())
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def inside_type_checking(node: ast.AST) -> bool:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
                return True
        return False

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if inside_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm._core" or alias.name.endswith("._core"):
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in {"notebooklm._core", "_core"} or module.endswith("._core"):
                violations.append((node.lineno, module))

    assert not violations


@pytest.mark.asyncio
async def test_client_core_rpc_wrappers_delegate_to_rpc_executor(monkeypatch) -> None:
    core = ClientCore(_auth_tokens())
    snapshot = _AuthSnapshot(
        csrf_token="csrf",
        session_id="session",
        authuser=0,
        account_email=None,
    )
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class FakeExecutor:
        async def execute(self, *args: Any, **kwargs: Any) -> str:
            calls.append(("execute", args, kwargs))
            return "executed"

        def build_url(self, *args: Any, **kwargs: Any) -> str:
            calls.append(("build_url", args, kwargs))
            return "url"

        def raise_rpc_error_from_http_status(self, *args: Any, **kwargs: Any) -> None:
            calls.append(("http_status", args, kwargs))
            raise RuntimeError("http status")

        def raise_rpc_error_from_request_error(self, *args: Any, **kwargs: Any) -> None:
            calls.append(("request_error", args, kwargs))
            raise RuntimeError("request error")

        async def try_refresh_and_retry(self, *args: Any, **kwargs: Any) -> str:
            calls.append(("try_refresh", args, kwargs))
            return "retried"

    executor = FakeExecutor()
    monkeypatch.setattr(core, "_get_rpc_executor", lambda: executor)

    assert (
        await core._rpc_call_impl(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            False,
            disable_internal_retries=True,
        )
        == "executed"
    )
    assert core._build_url(RPCMethod.LIST_NOTEBOOKS, snapshot, "/source") == "url"
    with pytest.raises(RuntimeError, match="http status"):
        core._raise_rpc_error_from_http_status(_status_error(500), RPCMethod.LIST_NOTEBOOKS)
    with pytest.raises(RuntimeError, match="request error"):
        core._raise_rpc_error_from_request_error(
            httpx.ConnectError("boom"),
            RPCMethod.LIST_NOTEBOOKS,
        )
    assert (
        await core._try_refresh_and_retry(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/",
            False,
            RPCError("auth"),
            disable_internal_retries=True,
        )
        == "retried"
    )

    assert [name for name, _, _ in calls] == [
        "execute",
        "build_url",
        "http_status",
        "request_error",
        "try_refresh",
    ]
    assert calls[0][2] == {"disable_internal_retries": True}
    assert calls[1][2] == {"rpc_id_override": None}
    assert calls[-1][2] == {"disable_internal_retries": True}


@pytest.mark.asyncio
async def test_core_decode_response_monkeypatch_after_executor_construction(monkeypatch) -> None:
    core = ClientCore(_auth_tokens())
    executor = core._get_rpc_executor()

    async def fake_perform_authed_post(
        *,
        build_request,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        return _ok_response("wire")

    decode_calls: list[dict[str, Any]] = []

    def fake_decode(raw: str, rpc_id: str, *, allow_null: bool = False) -> dict[str, Any]:
        decode_calls.append({"raw": raw, "rpc_id": rpc_id, "allow_null": allow_null})
        return {"decoded": rpc_id}

    monkeypatch.setattr(core, "_perform_authed_post", fake_perform_authed_post)
    monkeypatch.setattr("notebooklm._core.decode_response", fake_decode)

    result = await core._rpc_call_impl(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/notebook/abc",
        True,
        False,
    )

    assert core._get_rpc_executor() is executor
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

    result = await _executor(owner, decode_response_late_bound=decode).execute(
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

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise RPCError("not matched by the built-in auth detector")

    def is_auth_error(exc: Exception) -> bool:
        is_auth_error_calls.append(exc)
        return True

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    result = await _executor(
        owner,
        decode_response_late_bound=decode,
        is_auth_error=is_auth_error,
        sleep=sleep,
    ).execute(
        RPCMethod.LIST_NOTEBOOKS,
        ["param"],
        "/notebook/abc",
        True,
        False,
        disable_internal_retries=True,
    )

    assert result == {"retried": True}
    assert owner.refresh_calls == 1
    assert sleep_calls == [0.25]
    assert len(is_auth_error_calls) == 1
    assert owner.rpc_retry_calls == [
        {
            "method": RPCMethod.LIST_NOTEBOOKS,
            "params": ["param"],
            "source_path": "/notebook/abc",
            "allow_null": True,
            "_is_retry": True,
            "disable_internal_retries": True,
        }
    ]


@pytest.mark.asyncio
async def test_decode_time_auth_retry_preserves_none_result() -> None:
    async def refresh_callback() -> object:
        return object()

    owner = _Owner(refresh_callback=refresh_callback)
    owner.rpc_retry_result = None

    def decode(_: str, __: str, *, allow_null: bool = False) -> Any:
        raise RPCError("authentication expired")

    result = await _executor(
        owner,
        decode_response_late_bound=decode,
        is_auth_error=lambda exc: True,
    ).execute(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        True,
        False,
    )

    assert result is None
    assert owner.refresh_calls == 1
    assert owner.rpc_retry_calls[0]["allow_null"] is True
    assert owner.rpc_retry_calls[0]["_is_retry"] is True


@pytest.mark.asyncio
async def test_core_sleep_monkeypatch_after_executor_construction(monkeypatch) -> None:
    async def refresh_callback() -> AuthTokens:
        return _auth_tokens()

    core = ClientCore(
        _auth_tokens(),
        refresh_callback=refresh_callback,
        refresh_retry_delay=0.5,
    )
    executor = core._get_rpc_executor()
    refresh_calls = 0
    sleep_calls: list[float] = []

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
    ) -> dict[str, bool]:
        assert method is RPCMethod.LIST_NOTEBOOKS
        assert params == ["param"]
        assert source_path == "/notebook/abc"
        assert allow_null is True
        assert _is_retry is True
        assert disable_internal_retries is True
        return {"ok": True}

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(core, "_await_refresh", fake_await_refresh)
    monkeypatch.setattr(core, "rpc_call", fake_rpc_call)
    monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

    result = await core._try_refresh_and_retry(
        RPCMethod.LIST_NOTEBOOKS,
        ["param"],
        "/notebook/abc",
        True,
        RPCError("auth"),
        disable_internal_retries=True,
    )

    assert core._get_rpc_executor() is executor
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
