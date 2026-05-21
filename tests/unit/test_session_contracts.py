"""Typing checks for the capability-Protocol contracts in
``notebooklm._session_contracts``.

Phase 7 (refactor-history.md §Migration Plan step 10) replaced the broad
``Session`` Protocol with four shared capability Protocols
(``RpcCaller``, ``LoopGuard``, ``OperationScopeProvider``,
``AsyncWorkRuntime``). ``AuthMetadata`` and ``Kernel`` are preserved
as standalone Protocols for the upload pipeline. The standalone
``DrainHookRegistration`` Protocol previously kept here was deleted in
the same step — the canonical ``DrainHookRegistration`` is now local
to ``_artifacts.py`` since artifact polling is its only consumer.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any

import httpx

from notebooklm._session_contracts import (
    AsyncWorkRuntime,
    AuthMetadata,
    Kernel,
    LoopGuard,
    OperationScopeProvider,
    RpcCaller,
)
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


class _RpcCallerImpl:
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


class _LoopGuardImpl:
    def assert_bound_loop(self) -> None:
        return None


class _OperationScopeProviderImpl:
    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        return _NoopOperationScope()


class _AsyncWorkRuntimeImpl:
    def assert_bound_loop(self) -> None:
        return None

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        return _NoopOperationScope()


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


def _public_contract_members(protocol: type[Any]) -> set[str]:
    return {name for name in protocol.__dict__ if not name.startswith("_")}


# ----------------------------------------------------------------------
# Membership pins — one test per Protocol
# ----------------------------------------------------------------------


def test_auth_metadata_protocol_has_exactly_two_members() -> None:
    assert _public_contract_members(AuthMetadata) == {"authuser", "account_email"}


def test_kernel_protocol_has_exactly_three_members() -> None:
    assert _public_contract_members(Kernel) == {"post", "cookies", "aclose"}


def test_rpc_caller_protocol_has_exactly_one_member() -> None:
    assert _public_contract_members(RpcCaller) == {"rpc_call"}


def test_loop_guard_protocol_has_exactly_one_member() -> None:
    assert _public_contract_members(LoopGuard) == {"assert_bound_loop"}


def test_operation_scope_provider_protocol_has_exactly_one_member() -> None:
    assert _public_contract_members(OperationScopeProvider) == {"operation_scope"}


def test_async_work_runtime_protocol_extends_loop_guard_and_operation_scope_provider() -> None:
    # ``AsyncWorkRuntime`` inherits both members through Protocol composition.
    # The union of inherited public members across its MRO must match.
    mro_members: set[str] = set()
    for parent in AsyncWorkRuntime.__mro__:
        mro_members.update(name for name in parent.__dict__ if not name.startswith("_"))
    assert {"assert_bound_loop", "operation_scope"} <= mro_members
    assert AsyncWorkRuntime.__mro__[1:3] in (
        (LoopGuard, OperationScopeProvider),
        (OperationScopeProvider, LoopGuard),
    )


# ----------------------------------------------------------------------
# Signature pins — load-bearing for feature retypes
# ----------------------------------------------------------------------


def test_rpc_caller_signature_matches_legacy_session_rpc_call() -> None:
    sig = inspect.signature(RpcCaller.rpc_call)
    assert list(sig.parameters) == [
        "self",
        "method",
        "params",
        "source_path",
        "allow_null",
        "_is_retry",
        "disable_internal_retries",
        "operation_variant",
    ]
    assert sig.parameters["source_path"].default == "/"
    assert sig.parameters["allow_null"].default is False
    assert sig.parameters["_is_retry"].default is False
    assert sig.parameters["disable_internal_retries"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["disable_internal_retries"].default is False
    assert sig.parameters["operation_variant"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["operation_variant"].default is None


def test_auth_metadata_protocol_signatures_are_pinned() -> None:
    authuser = inspect.signature(AuthMetadata.authuser.fget)
    assert authuser.return_annotation == "int"

    account_email = inspect.signature(AuthMetadata.account_email.fget)
    assert account_email.return_annotation == "str | None"


def test_kernel_protocol_signatures_are_pinned() -> None:
    post = inspect.signature(Kernel.post)
    assert list(post.parameters) == ["self", "url", "headers", "body"]
    assert post.parameters["headers"].annotation == "Mapping[str, str]"
    assert post.parameters["body"].annotation == "bytes"
    assert post.return_annotation == "httpx.Response"

    cookies = inspect.signature(Kernel.cookies.fget)
    assert cookies.return_annotation == "httpx.Cookies"

    aclose = inspect.signature(Kernel.aclose)
    assert list(aclose.parameters) == ["self"]
    assert aclose.return_annotation == "None"


def test_loop_guard_signature_is_pinned() -> None:
    sig = inspect.signature(LoopGuard.assert_bound_loop)
    assert list(sig.parameters) == ["self"]
    assert sig.return_annotation == "None"


def test_operation_scope_provider_signature_is_pinned() -> None:
    sig = inspect.signature(OperationScopeProvider.operation_scope)
    assert list(sig.parameters) == ["self", "label"]
    assert sig.parameters["label"].annotation == "str"
    assert sig.return_annotation == "AbstractAsyncContextManager[None]"


# ----------------------------------------------------------------------
# Structural conformance — mypy verifies these assignments
# ----------------------------------------------------------------------


def test_structural_implementations_satisfy_protocols() -> None:
    auth: AuthMetadata = _AuthMetadataImpl()
    kernel: Kernel = _KernelImpl()
    rpc: RpcCaller = _RpcCallerImpl()
    loop_guard: LoopGuard = _LoopGuardImpl()
    op_scope: OperationScopeProvider = _OperationScopeProviderImpl()
    async_work: AsyncWorkRuntime = _AsyncWorkRuntimeImpl()

    assert auth is not None
    assert kernel is not None
    assert rpc is not None
    assert loop_guard is not None
    assert op_scope is not None
    assert async_work is not None
