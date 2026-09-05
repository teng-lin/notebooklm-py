"""Real Web/Android producer and adapter agreement for P2 commit evidence."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import ADD_TENTATIVE_SOURCES_METHOD, AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._web.sources.batch import SourceBatchAddService
from notebooklm.cli.error_handler import handle_errors
from notebooklm.exceptions import NetworkError, RPCError
from notebooklm.mcp._errors import to_tool_error, tool_error_payload
from notebooklm.outcomes import CommitState, RecoveryAction
from notebooklm.rpc import RPCMethod
from notebooklm.server._errors import error_response

_URLS = ("https://a.example.test", "https://b.example.test")


@dataclass(frozen=True)
class _Expected:
    state: CommitState
    recovery: RecoveryAction
    category: ErrorCategory
    retriable: bool
    unconfirmed: bool
    cli_code: str
    mcp_code: str
    rest_category: str


_UNKNOWN = _Expected(
    CommitState.UNKNOWN,
    RecoveryAction.INSPECT_AND_RECONCILE,
    ErrorCategory.RPC,
    False,
    True,
    "UNCONFIRMED_WRITE",
    "RPC",
    "rpc",
)
_REJECTED = _Expected(
    CommitState.REJECTED,
    RecoveryAction.NONE,
    ErrorCategory.SOURCE_ADD,
    False,
    False,
    "NOTEBOOKLM_ERROR",
    "SOURCE_ADD",
    "source_add",
)
_NOT_SENT = _Expected(
    CommitState.NOT_SENT,
    RecoveryAction.NONE,
    ErrorCategory.NETWORK,
    True,
    False,
    "NETWORK_ERROR",
    "NETWORK",
    "network",
)
_NOT_SENT_SOURCE = _Expected(
    CommitState.NOT_SENT,
    RecoveryAction.NONE,
    ErrorCategory.SOURCE_ADD,
    False,
    False,
    "NOTEBOOKLM_ERROR",
    "SOURCE_ADD",
    "source_add",
)


class _WebTerminal:
    """Minimal wire terminal used through the real Web batch workflow."""

    def __init__(self, case: str) -> None:
        self.case = case

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        **kwargs: Any,
    ) -> Any:
        del method, params
        entries = kwargs["journal_entries"]
        if self.case != "pre_dispatch":
            for entry in entries:
                entry.mark_dispatched()
        if self.case == "rejected":
            raise RPCError("decoded refusal", method_id=RPCMethod.ADD_SOURCE.value, rpc_code=9)
        raise NetworkError("transport failed")


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


class _AndroidTerminal:
    """Terminal fake that opens attempts while the real Android owner settles them."""

    def __init__(self, case: str) -> None:
        self.case = case

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        del label, kwargs
        yield _Lease()

    async def spawn_child(self, label: str, factory: Any) -> Any:
        del label
        return factory()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        del request
        entries: Sequence[Any] = kwargs.get("journal_entries") or ()
        if self.case != "pre_dispatch":
            for entry in entries:
                entry.mark_dispatched()
        if self.case in {"pre_dispatch", "unknown"}:
            raise NetworkError("transport failed")
        assert method == ADD_TENTATIVE_SOURCES_METHOD
        return sources_pb2.AddTentativeSourcesResponse()


async def _produce(backend: str, case: str) -> tuple[BaseException, _Expected]:
    if backend == "web":
        terminal = _WebTerminal(case)

        async def no_error_rows(*args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        try:
            outcomes = await SourceBatchAddService().add_urls(
                "notebook-1",
                _URLS,
                rpc=terminal,
                list_sources=no_error_rows,
                extract_youtube_video_id=lambda _url: None,
                logger=logging.getLogger(__name__),
            )
        except BaseException as error:
            return error, _NOT_SENT if case == "pre_dispatch" else _UNKNOWN
    else:
        terminal = _AndroidTerminal(case)
        api = AndroidSourcesAPI(
            cast(AndroidSession, terminal),
            cast(AndroidUploadPipeline, object()),
        )
        outcomes = await api._add_urls_batch("notebook-1", list(_URLS))

    error = outcomes[0].error
    assert error is not None
    expected = (
        _REJECTED
        if case == "rejected"
        else _NOT_SENT_SOURCE
        if case == "pre_dispatch"
        else _UNKNOWN
    )
    return error, expected


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize("case", ["rejected", "unknown", "pre_dispatch"])
async def test_commit_evidence_producer_consumer_matrix(
    backend: str,
    case: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error, expected = await _produce(backend, case)

    metadata = getattr(error, "operation_metadata", None)
    assert metadata is not None
    assert metadata.commit_state is expected.state
    assert metadata.recovery_action is expected.recovery
    assert getattr(error, "unconfirmed", False) is expected.unconfirmed
    classified = classify(error)
    assert (classified.category, classified.retriable) == (
        expected.category,
        expected.retriable,
    )

    batch = metadata.batch_outcome
    assert batch is not None
    assert [item.member for item in batch.items] == [0, 1]
    assert [item.commit_state for item in batch.items] == [expected.state, expected.state]
    if expected.state is CommitState.UNKNOWN:
        assert all(item.reconciliation is not None for item in batch.items)

    with pytest.raises(SystemExit) as cli_exit, handle_errors(json_output=True):
        raise error
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_exit.value.code == 1
    assert cli_payload["code"] == expected.cli_code
    assert cli_payload["commit_state"] == expected.state.value
    assert cli_payload["batch_outcome"]["items"][1]["member"] == 1

    mcp_payload = tool_error_payload(error)
    assert mcp_payload["code"] == expected.mcp_code
    assert mcp_payload["retriable"] is expected.retriable
    assert mcp_payload["batch_outcome"] == cli_payload["batch_outcome"]
    mcp_wire = str(to_tool_error(error))
    assert mcp_wire.startswith(f"{expected.mcp_code}:")
    assert "batch_outcome" in mcp_wire

    rest_payload = json.loads(error_response(error).body)["error"]
    assert rest_payload["category"] == expected.rest_category
    assert rest_payload["retriable"] is expected.retriable
    assert rest_payload["batch_outcome"] == cli_payload["batch_outcome"]
