"""P4.2 aggregate-deadline seeding and fail-closed authority audit."""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._binding import CodecBinding
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._operations import Operation
from notebooklm._records import (
    NOTEBOOK_UPDATE_DEF,
    SOURCE_WAIT_DEF,
    NotebookUpdateInput,
    SourceWaitSnapshotInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.deadlines import (
    CLIENT_TIMEOUT_DEADLINE_OPERATIONS,
    SEMANTIC_DEADLINE_AUTHORITIES,
    SemanticDeadlineAuthority,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod

_EXPECTED_CLIENT_TIMEOUT_OPERATIONS = frozenset(
    {
        Operation.NOTEBOOK_CREATE,
        Operation.NOTEBOOK_UPDATE,
        Operation.NOTEBOOK_SUGGEST_PROMPTS,
        Operation.SOURCE_ADD_URL,
        Operation.SOURCE_ADD_URL_BATCH,
        Operation.SOURCE_ADD_DRIVE,
        Operation.SOURCE_UPDATE,
        Operation.ARTIFACT_LIST,
        Operation.ARTIFACT_GET,
        Operation.ARTIFACT_GENERATE_AUDIO,
        Operation.ARTIFACT_GENERATE_VIDEO,
        Operation.ARTIFACT_GENERATE_REPORT,
        Operation.ARTIFACT_GENERATE_QUIZ,
        Operation.ARTIFACT_GENERATE_FLASHCARDS,
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK,
        Operation.ARTIFACT_GENERATE_DATA_TABLE,
        Operation.ARTIFACT_GENERATE_MIND_MAP,
        Operation.ARTIFACT_RENAME,
        Operation.MIND_MAP_GENERATE_NOTE,
        Operation.MIND_MAP_GENERATE_INTERACTIVE,
        Operation.LABEL_CREATE,
        Operation.LABEL_UPDATE,
        Operation.COLLECTION_CREATE,
        Operation.COLLECTION_UPDATE,
        Operation.SHARING_SET_PUBLIC,
        Operation.SHARING_SET_VIEW_LEVEL,
        Operation.SHARING_UPDATE_USERS,
    }
)
_EXPECTED_WORKFLOW_OWNED_OPERATIONS = frozenset(
    {
        Operation.SOURCE_ADD_FILE,
        Operation.SOURCE_WAIT,
        Operation.ARTIFACT_WAIT,
        Operation.CHAT_ASK,
        Operation.RESEARCH_IMPORT,
    }
)
_EXPECTED_BRANCH_EXCLUSIVE_OPERATIONS = frozenset(
    {Operation.ARTIFACT_DOWNLOAD, Operation.CHAT_CONFIGURE}
)


def _reachable_native_sites(method_name: str, seen: tuple[str, ...] = ()) -> set[tuple[str, int]]:
    """Find syntactically reachable ``self._rpc_call`` sites on the concrete backend."""

    if method_name in seen:
        return set()
    method = getattr(WebRpcBackend, method_name, None)
    if method is None:
        return set()
    source = textwrap.dedent(inspect.getsource(method))
    node = ast.parse(source).body[0]
    sites: set[tuple[str, int]] = set()
    callees: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        owner = call.func.value
        if not isinstance(owner, ast.Name) or owner.id != "self":
            continue
        if call.func.attr == "_rpc_call":
            sites.add((method.__qualname__, call.lineno))
        elif call.func.attr.startswith("_"):
            callees.add(call.func.attr)
    for callee in callees:
        sites.update(_reachable_native_sites(callee, (*seen, method_name)))
    return sites


def test_multi_native_deadline_authority_ledger_is_closed_and_active() -> None:
    """New syntactic composites require an explicit reviewed deadline authority."""

    assert CLIENT_TIMEOUT_DEADLINE_OPERATIONS == _EXPECTED_CLIENT_TIMEOUT_OPERATIONS
    assert {
        operation
        for operation, authority in SEMANTIC_DEADLINE_AUTHORITIES.items()
        if authority is SemanticDeadlineAuthority.WORKFLOW_OWNED
    } == _EXPECTED_WORKFLOW_OWNED_OPERATIONS
    assert {
        operation
        for operation, authority in SEMANTIC_DEADLINE_AUTHORITIES.items()
        if authority is SemanticDeadlineAuthority.BRANCH_EXCLUSIVE
    } == _EXPECTED_BRANCH_EXCLUSIVE_OPERATIONS
    assert all(
        WEB_OPERATION_REGISTRY[operation].is_supported
        for operation in SEMANTIC_DEADLINE_AUTHORITIES
    )

    syntactic_composites = {
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported
        and binding.handler_name is not None
        and len(_reachable_native_sites(binding.handler_name)) > 1
    }
    # P9.3: a row-backed operation whose ``NativeCallSpec`` is input-keyed declares
    # more than one native without any ``self`` walk; those rows are the
    # branch-exclusive members by construction (one call per input).
    keyed_row_composites = {
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported
        and isinstance(binding.row, CodecBinding)
        and not binding.row.native.is_constant
    }
    assert keyed_row_composites <= _EXPECTED_BRANCH_EXCLUSIVE_OPERATIONS | {
        Operation.RESEARCH_START
    }
    # ARTIFACT_LIST/GET also call the note-backed mind-map collaborator;
    # their second native call is intentionally invisible to a ``self`` AST walk.
    hidden_collaborator_composites = {Operation.ARTIFACT_LIST, Operation.ARTIFACT_GET}
    assert (
        syntactic_composites
        | hidden_collaborator_composites
        | (keyed_row_composites & _EXPECTED_BRANCH_EXCLUSIVE_OPERATIONS)
    ) == (_EXPECTED_CLIENT_TIMEOUT_OPERATIONS | _EXPECTED_BRANCH_EXCLUSIVE_OPERATIONS)


@pytest.mark.parametrize("timeout", [None, float("inf")])
def test_deadline_factory_preserves_unbounded_client_timeout(timeout: float | None) -> None:
    assert RuntimeDeadlineFactory.fixed(timeout).start() is None


@pytest.mark.asyncio
async def test_seeded_composite_shares_identity_and_consumes_remaining_budget() -> None:
    clock = [100.0]

    async def rpc_call(method: RPCMethod, _params: list[Any], **kwargs: Any) -> Any:
        calls.append((method, kwargs))
        if method is RPCMethod.RENAME_NOTEBOOK:
            clock[0] = 104.0
            return None
        return [["Renamed", [], "nb-1"]]

    calls: list[tuple[RPCMethod, dict[str, Any]]] = []
    executor = MagicMock(rpc_call=AsyncMock(side_effect=rpc_call))
    factory = RuntimeDeadlineFactory.fixed(10.0, monotonic=lambda: clock[0])
    backend = WebRpcBackend(
        executor,
        transport_factory=lambda **_kwargs: object(),
        deadline_factory=factory,
    )

    result = await backend.invoke(
        NOTEBOOK_UPDATE_DEF,
        NotebookUpdateInput("nb-1", title="Renamed"),
        deadline=None,
    )

    assert result.notebook.title == "Renamed"
    first_deadline = calls[0][1]["_retry_deadline"]
    assert isinstance(first_deadline, RuntimeDeadline)
    assert calls[1][1]["_retry_deadline"] is first_deadline
    assert calls[0][1]["read_timeout"] == 10.0
    assert calls[1][1]["read_timeout"] == 6.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_never_replaced_by_composition_factory() -> None:
    deadline = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)
    executor = MagicMock(rpc_call=AsyncMock(side_effect=[None, [["Renamed", [], "nb-1"]]]))
    backend = WebRpcBackend(
        executor,
        transport_factory=lambda **_kwargs: object(),
        deadline_factory=RuntimeDeadlineFactory(lambda: pytest.fail("factory was called")),
    )

    await backend.invoke(
        NOTEBOOK_UPDATE_DEF,
        NotebookUpdateInput("nb-1", title="Renamed"),
        deadline=deadline,
    )

    assert all(
        invocation.kwargs["_retry_deadline"] is deadline
        for invocation in executor.rpc_call.await_args_list
    )


@pytest.mark.asyncio
async def test_source_poll_snapshot_keeps_legacy_in_flight_timeout_semantics() -> None:
    executor = MagicMock(rpc_call=AsyncMock(return_value=[["Notebook", [], "nb-1"]]))
    backend = WebRpcBackend(
        executor,
        transport_factory=lambda **_kwargs: object(),
        deadline_factory=RuntimeDeadlineFactory(lambda: pytest.fail("factory was called")),
    )

    await backend.invoke(
        SOURCE_WAIT_DEF,
        SourceWaitSnapshotInput("nb-1"),
        deadline=None,
    )

    call = executor.rpc_call.await_args
    assert call.kwargs["_retry_deadline"] is None
    assert call.kwargs["read_timeout"] is None


def test_production_assembly_reads_live_timeout_without_mutating_started_deadline() -> None:
    client = NotebookLMClient(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
        timeout=17.0,
    )

    first = client._backend._deadline_factory.start()
    client._provider._lifecycle._timeout = 41.0
    second = client._backend._deadline_factory.start()

    assert first is not None and second is not None
    assert first.timeout == 17.0
    assert second.timeout == 41.0
    assert first.timeout == 17.0
