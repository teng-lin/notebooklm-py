"""Typing checks for the capability-Protocol contracts in
``notebooklm._runtime_contracts``.

Phase 7 (refactor-history.md §Migration Plan step 10) replaced the broad
``Session`` Protocol with shared capability Protocols. The surviving
shared Protocols are ``RpcCaller`` (~17 consumers), ``LoopGuard`` (2
consumers), and the pure-transport ``Kernel``. The single-consumer
``AuthMetadata`` / ``OperationScopeProvider`` Protocols and the unused
``AsyncWorkRuntime`` composite were inlined into their owning feature
modules / deleted in issue #1327 — ``AuthMetadata`` now lives in
``_source_upload`` (used by ``SourceUploadPipeline``) and
``OperationScopeProvider`` in ``_artifact_polling`` (used by
``ArtifactPollingService``); mypy enforces their structural conformance
at the consuming call sites. The standalone
``DrainHookRegistration`` Protocol previously kept here was deleted in
Phase 7 — the canonical ``DrainHookRegistration`` is now local to
``_artifacts.py`` since artifact polling is its only consumer.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import httpx

from notebooklm._runtime_contracts import (
    Kernel,
    LoopGuard,
    RpcCaller,
)
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


def test_kernel_protocol_has_exactly_three_members() -> None:
    assert _public_contract_members(Kernel) == {"post", "cookies", "aclose"}


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
    ]
    assert sig.parameters["source_path"].default == "/"
    assert sig.parameters["allow_null"].default is False
    assert sig.parameters["_is_retry"].default is False
    assert sig.parameters["disable_internal_retries"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["disable_internal_retries"].default is False
    assert sig.parameters["operation_variant"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["operation_variant"].default is None


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
