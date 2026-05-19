"""Typing checks for the Tier-13 Session/Kernel protocol contracts."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any

import httpx

from notebooklm._request_types import BuildRequest
from notebooklm._session_contracts import AuthMetadata, DrainHookRegistration, Kernel, Session
from notebooklm.rpc.types import RPCMethod


class _NoopOperationScope:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return False


class _SessionImpl:
    @property
    def auth(self) -> AuthMetadata:
        return _AuthMetadataImpl()

    @property
    def kernel(self) -> Kernel:
        return _KernelImpl()

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        return None

    async def transport_post(
        self,
        build_request: BuildRequest,
        parse_label: str,
        *,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        return httpx.Response(200, content=b"")

    async def next_reqid(self, step: int = 100000) -> int:
        return step

    def assert_bound_loop(self) -> None:
        return None

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        return _NoopOperationScope()

    def register_drain_hook(
        self,
        name: str,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        return None


class _AuthMetadataImpl:
    @property
    def authuser(self) -> int:
        return 0

    @property
    def account_email(self) -> str | None:
        return None


class _KernelImpl:
    async def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> httpx.Response:
        return httpx.Response(200, content=body)

    @property
    def cookies(self) -> httpx.Cookies:
        return httpx.Cookies()

    async def aclose(self) -> None:
        return None


class _DrainHookRegistrationImpl:
    def register_drain_hook(
        self,
        name: str,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        return None


def _public_contract_members(protocol: type[Any]) -> set[str]:
    return {name for name in protocol.__dict__ if not name.startswith("_")}


def test_auth_metadata_protocol_has_exactly_two_members() -> None:
    assert _public_contract_members(AuthMetadata) == {"authuser", "account_email"}


def test_session_protocol_has_exactly_eight_members() -> None:
    assert _public_contract_members(Session) == {
        "auth",
        "kernel",
        "rpc_call",
        "transport_post",
        "next_reqid",
        "assert_bound_loop",
        "operation_scope",
        "register_drain_hook",
    }


def test_kernel_protocol_has_exactly_three_members() -> None:
    assert _public_contract_members(Kernel) == {"post", "cookies", "aclose"}


def test_drain_hook_registration_protocol_has_exactly_one_member() -> None:
    assert _public_contract_members(DrainHookRegistration) == {"register_drain_hook"}


def test_session_protocol_signatures_are_pinned() -> None:
    auth = inspect.signature(Session.auth.fget)
    assert auth.return_annotation == "AuthMetadata"

    kernel = inspect.signature(Session.kernel.fget)
    assert kernel.return_annotation == "Kernel"

    rpc_call = inspect.signature(Session.rpc_call)
    assert list(rpc_call.parameters) == [
        "self",
        "method",
        "params",
        "source_path",
        "allow_null",
        "_is_retry",
        "disable_internal_retries",
        "operation_variant",
    ]
    assert rpc_call.parameters["source_path"].default == "/"
    assert rpc_call.parameters["allow_null"].default is False
    assert rpc_call.parameters["_is_retry"].default is False
    assert rpc_call.parameters["disable_internal_retries"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rpc_call.parameters["disable_internal_retries"].default is False
    assert rpc_call.parameters["operation_variant"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rpc_call.parameters["operation_variant"].default is None

    transport_post = inspect.signature(Session.transport_post)
    assert transport_post.parameters["build_request"].annotation == "BuildRequest"
    assert transport_post.parameters["parse_label"].annotation == "str"
    assert transport_post.parameters["disable_internal_retries"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert "_BuildRequest" not in str(transport_post)

    next_reqid = inspect.signature(Session.next_reqid)
    assert next_reqid.parameters["step"].default == 100000
    assert next_reqid.return_annotation == "int"

    operation_scope = inspect.signature(Session.operation_scope)
    assert operation_scope.return_annotation == "AbstractAsyncContextManager[None]"

    register_drain_hook = inspect.signature(Session.register_drain_hook)
    assert list(register_drain_hook.parameters) == ["self", "name", "hook"]
    assert register_drain_hook.parameters["hook"].annotation == "Callable[[], Awaitable[None]]"
    assert register_drain_hook.return_annotation == "None"


def test_auth_metadata_protocol_signatures_are_pinned() -> None:
    authuser = inspect.signature(AuthMetadata.authuser.fget)
    assert authuser.return_annotation == "int"

    account_email = inspect.signature(AuthMetadata.account_email.fget)
    assert account_email.return_annotation == "str | None"


def test_kernel_and_drain_hook_signatures_are_pinned() -> None:
    post = inspect.signature(Kernel.post)
    assert list(post.parameters) == ["self", "url", "headers", "body"]
    assert post.parameters["headers"].annotation == "Mapping[str, str]"
    assert post.parameters["body"].annotation == "bytes"
    assert post.return_annotation == "httpx.Response"

    # Protocol properties expose the getter signature through ``fget``.
    cookies = inspect.signature(Kernel.cookies.fget)
    assert cookies.return_annotation == "httpx.Cookies"

    aclose = inspect.signature(Kernel.aclose)
    assert list(aclose.parameters) == ["self"]
    assert aclose.return_annotation == "None"

    register_drain_hook = inspect.signature(DrainHookRegistration.register_drain_hook)
    assert list(register_drain_hook.parameters) == ["self", "name", "hook"]
    assert register_drain_hook.parameters["hook"].annotation == "Callable[[], Awaitable[None]]"
    assert register_drain_hook.return_annotation == "None"


def test_structural_implementations_satisfy_protocols() -> None:
    # These assignments are the contract check: mypy verifies that each
    # implementation structurally satisfies its Protocol. Runtime
    # ``isinstance`` checks would only prove the concrete class identity
    # unless the Protocols became ``@runtime_checkable``, which is weaker than
    # the signature-level static check and not needed for this type-only PR.
    session: Session = _SessionImpl()
    kernel: Kernel = _KernelImpl()
    drain_hooks: DrainHookRegistration = _DrainHookRegistrationImpl()

    assert session is not None
    assert kernel is not None
    assert drain_hooks is not None
