"""Typing checks for the capability-Protocol contracts in
``notebooklm._runtime.contracts`` and ``notebooklm._web.contracts``.

Phase 7 (docs/refactor-history.md §Migration Plan step 10) replaced the broad
``Session`` Protocol with shared capability Protocols. The surviving
shared Protocols are ``RpcCaller`` (~17 consumers), ``LoopGuard`` (2
consumers), and the pure-transport ``Kernel``. The single-consumer
``AuthMetadata`` Protocol and the unused
``AsyncWorkRuntime`` composite were inlined into their owning feature
modules / deleted in issue #1327 — ``AuthMetadata`` now lives in
``_web.sources.upload`` (used by ``SourceUploadPipeline``); mypy enforces its
structural conformance
at the consuming call sites. The standalone
``DrainHookRegistration`` Protocol previously kept here was deleted in
Phase 7; feature APIs now receive the concrete ``CallSupervisor`` when they
need operation admission plus drain-hook registration.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import httpx

from notebooklm._runtime.contracts import LoopGuard
from notebooklm._web.contracts import Kernel, RpcCaller
from notebooklm.rpc.types import RPCMethod


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


class _KernelImpl:
    async def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        read_timeout: float | None = None,
        max_response_bytes: int | None = None,
        expected_epoch: int | None = None,
    ) -> httpx.Response:
        del expected_epoch
        return httpx.Response(200, content=body)

    def get_http_client(self, *, expected_epoch: int | None = None) -> httpx.AsyncClient:
        del expected_epoch
        raise AssertionError("structural typing stub")

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


def test_kernel_protocol_has_exactly_four_members() -> None:
    assert _public_contract_members(Kernel) == {
        "post",
        "get_http_client",
        "cookies",
        "aclose",
    }


def test_rpc_caller_protocol_has_exactly_one_member() -> None:
    assert _public_contract_members(RpcCaller) == {"rpc_call"}


def test_loop_guard_protocol_has_exactly_one_member() -> None:
    assert _public_contract_members(LoopGuard) == {"assert_bound_loop"}


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
        "read_timeout",
        "raise_on_null_status",
    ]
    assert sig.parameters["source_path"].default == "/"
    assert sig.parameters["allow_null"].default is False
    assert sig.parameters["_is_retry"].default is False
    assert sig.parameters["disable_internal_retries"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["disable_internal_retries"].default is False
    assert sig.parameters["operation_variant"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["operation_variant"].default is None
    # #2187: per-call read-timeout override (mirrors the chat-transport
    # precedent) so IMPORT_RESEARCH can get a batch-scaled budget without
    # lowering the client-wide default for every other RPC.
    assert sig.parameters["read_timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["read_timeout"].default is None
    # #2188: opt-in strictness for ``allow_null`` callers — a null result the
    # server tagged with a non-OK google.rpc.Status raises instead of decoding
    # to None, so an artifact rejection reports the server's reason.
    assert sig.parameters["raise_on_null_status"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["raise_on_null_status"].default is False


def test_kernel_protocol_signatures_are_pinned() -> None:
    post = inspect.signature(Kernel.post)
    assert list(post.parameters) == [
        "self",
        "url",
        "headers",
        "body",
        "read_timeout",
        "max_response_bytes",
        "expected_epoch",
    ]
    assert post.parameters["headers"].annotation == "Mapping[str, str]"
    assert post.parameters["body"].annotation == "bytes"
    assert post.parameters["read_timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert post.parameters["read_timeout"].default is None
    assert post.parameters["max_response_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert post.parameters["max_response_bytes"].default is None
    assert post.parameters["expected_epoch"].kind is inspect.Parameter.KEYWORD_ONLY
    assert post.parameters["expected_epoch"].default is None
    assert post.return_annotation == "httpx.Response"

    get_http_client = inspect.signature(Kernel.get_http_client)
    assert list(get_http_client.parameters) == ["self", "expected_epoch"]
    assert get_http_client.parameters["expected_epoch"].kind is inspect.Parameter.KEYWORD_ONLY
    assert get_http_client.parameters["expected_epoch"].default is None
    assert get_http_client.return_annotation == "httpx.AsyncClient"

    cookies = inspect.signature(Kernel.cookies.fget)
    assert cookies.return_annotation == "httpx.Cookies"

    aclose = inspect.signature(Kernel.aclose)
    assert list(aclose.parameters) == ["self"]
    assert aclose.return_annotation == "None"


def test_loop_guard_signature_is_pinned() -> None:
    sig = inspect.signature(LoopGuard.assert_bound_loop)
    assert list(sig.parameters) == ["self"]
    assert sig.return_annotation == "None"


# ----------------------------------------------------------------------
# Structural conformance — mypy verifies these assignments
# ----------------------------------------------------------------------


def test_structural_implementations_satisfy_protocols() -> None:
    kernel: Kernel = _KernelImpl()
    rpc: RpcCaller = _RpcCallerImpl()
    loop_guard: LoopGuard = _LoopGuardImpl()

    assert kernel is not None
    assert rpc is not None
    assert loop_guard is not None
