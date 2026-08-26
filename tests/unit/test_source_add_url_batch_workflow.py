"""P10 R3.5: ``source.add_url_batch`` is a service-owned workflow over two leaves.

``SourceService.add_urls_batch`` owns what the P9.4b custom row owned: one
non-replayed ``source.register`` url write carrying every validated entry, the
positional attribution of its response, and — only when that response omits
entries — one ``source.list`` of ERROR rows to name the ghosts behind them.

These tests are the hoist's oracles, and the one the plan names as its
acceptance gate comes first:

* **exact-id-diff attribution** — a response row is assigned to a request only
  when it identifies that request (positionally *and* by canonical URL identity
  for a complete echo, by identity alone for a sparse one). Every way that match
  can fail to answer reports one unresolved failure for the whole write rather
  than guessing at a position.

The second oracle is the failure-graph replay. The pre-P10 service manufactured
an ``RPCError`` in-process, purely to hang off a ``SourceAddError`` as its
``cause``, on eight different branches; above the port there is no public
exception to build, so the workflow nests neutral records and ``_backend_compat``
rebuilds the graph. Every branch below asserts the *reconstructed* public
exception against the object the retired service would have raised, field for
field, through :func:`assert_replays`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._deadline import RuntimeDeadline
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy, mark_unconfirmed
from notebooklm._operations import Operation
from notebooklm._row_adapters.sources import unwrap_add_source_rows
from notebooklm._semantic.compat import project_backend_error, project_source_add_failure
from notebooklm._semantic.records import (
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REGISTER_DEF,
    SourceAddUrlBatchInput,
    SourceListResult,
    SourceRegisterKind,
    SourceRegisterResult,
)
from notebooklm._semantic.services.source import SourceService
from notebooklm._semantic.services.source_batch import (
    _ALL_REJECTED_RPC_CODE,
    _ERROR_SOURCE_STATUS,
    _normalized_rpc_code,
)
from notebooklm._url_utils import url_identity
from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import (
    GrpcStatusCode,
    SourceStatus,
    normalize_rpc_code,
    source_status_to_str,
)
from notebooklm.types import Source
from tests._fixtures.recording_backend import RecordingBackend
from tests._fixtures.source_add_replay import assert_replays
from tests._fixtures.web_backend import build_web_backend

_NB = "nb-1"
_ROUTE = f"/notebook/{_NB}"
_A = "https://a.example.com"
_B = "https://b.example.com"

_BASE_KWARGS: dict[str, Any] = {
    "_is_retry": False,
    "_retry_deadline": None,
    "allow_null": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "raise_on_null_status": False,
    "read_timeout": None,
}


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _url_row(source_id: str, url: str | None) -> list[Any]:
    """One ``ADD_SOURCE`` echo row, in the shape the batch write returns."""
    metadata: list[Any] = [None, None, None, None, 5, None, None, [url] if url else None]
    return [[source_id], url, metadata, [None, SourceStatus.PROCESSING.value]]


def _entry(source_id: str, *, title: str, url: str | None, status: int) -> list[Any]:
    metadata = [None, 11, [1704067200, 0], None, 5, None, None, [url] if url else None]
    return [[source_id], title, metadata, [None, status]]


def _snapshot(*entries: list[Any]) -> list[Any]:
    return [["Notebook", list(entries), _NB]]


def _service(*responses: object) -> tuple[SourceService, _RecordingExecutor]:
    executor = _RecordingExecutor(*responses)
    return SourceService(build_web_backend(executor)), executor


async def _fails(service: SourceService, urls: list[str]) -> BackendError:
    with pytest.raises(BackendError) as caught:
        await service.add_urls_batch(_NB, tuple(urls))
    return caught.value


def _identity(url: str) -> tuple[str, str]:
    return url_identity(url, logger=logging.getLogger(__name__))


def _unresolved(urls: list[str], detail: str, cause: Exception) -> SourceAddError:
    """Rebuild the exact object the retired below-port service raised."""
    preview = ", ".join(repr(url) for url in urls[:3])
    if len(urls) > 3:
        preview += f", … ({len(urls)} total)"
    return mark_unconfirmed(
        SourceAddError(
            preview,
            cause=cause,
            message=(
                "UNRESOLVED — do not blindly retry; check the notebook source list and "
                f"reconcile these URLs first: {preview}. {detail}"
            ),
        )
    )


# --- the wire vocabulary the workflow may not import ------------------------------


def test_the_neutral_all_rejected_status_is_the_grpc_one() -> None:
    """``rpc.types`` is wire vocabulary a semantic service may not import (I1).

    The workflow therefore spells the status as a literal; this is the pin that
    keeps the literal and ``GrpcStatusCode`` from drifting apart.
    """
    assert GrpcStatusCode.FAILED_PRECONDITION.value == _ALL_REJECTED_RPC_CODE


def test_the_neutral_error_status_is_what_the_adapter_renders() -> None:
    assert source_status_to_str(SourceStatus.ERROR) == _ERROR_SOURCE_STATUS


@pytest.mark.parametrize("code", [None, 9, "9", "USER_DISPLAYABLE_ERROR", "", True, False, 3.5])
def test_the_neutral_rpc_code_coercion_matches_the_adapter(code: Any) -> None:
    assert normalize_rpc_code(code) == _normalized_rpc_code(code)


# --- the operation's new shape ----------------------------------------------------


def test_add_url_batch_is_service_owned_over_its_two_leaves() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SOURCE_ADD_URL_BATCH]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.SOURCE_ADD_URL_BATCH in WEB_SERVICE_OWNED_OPERATIONS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_URL_BATCH]
    assert [leaf.operation for leaf in workflow.leaf_operations] == [
        Operation.SOURCE_REGISTER,
        Operation.SOURCE_LIST,
    ]
    # The registration leaf declares three variants; this workflow reaches
    # exactly one, so the derived native set is still the two the retired row
    # declared — no more, no fewer.
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.ADD_SOURCE, "url"),
        (RPCMethod.GET_NOTEBOOK, None),
    }


def test_the_hoist_preserves_the_batch_retry_classification() -> None:
    """``PROBE_THEN_CREATE`` forces the inner retry loop off; nothing replays it."""
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_URL_BATCH]
    (create,) = [
        native for native in workflow.native_bindings if native.method is RPCMethod.ADD_SOURCE
    ]

    assert create.variant == "url"
    assert create.expected_policy is IdempotencyPolicy.PROBE_THEN_CREATE
    assert (
        IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="url").policy
        is IdempotencyPolicy.PROBE_THEN_CREATE
    )


def test_the_workflow_left_the_client_timeout_deadline_ledger() -> None:
    """The ledger names supported operations only; the service owns the budget now."""
    assert Operation.SOURCE_ADD_URL_BATCH not in SEMANTIC_DEADLINE_AUTHORITIES


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))
    backend.set_result(SOURCE_LIST_DEF, SourceListResult(()))
    backend.set_workflows(Operation.SOURCE_ADD_URL_BATCH)

    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            SOURCE_ADD_URL_BATCH_DEF, SourceAddUrlBatchInput(_NB, (_A,)), deadline=None
        )


@pytest.mark.asyncio
async def test_the_reconciliation_leaf_is_checked_before_the_write() -> None:
    """A backend must never register the batch and only then find it cannot reconcile."""
    backend = RecordingBackend()
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))

    with pytest.raises(UnsupportedOperationError) as caught:
        await SourceService(backend).add_urls_batch(_NB, (_A,))

    assert caught.value.operation is Operation.SOURCE_LIST
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_an_empty_batch_writes_nothing() -> None:
    service, executor = _service()

    assert (await service.add_urls_batch(_NB, ())).items == ()
    assert executor.calls == []


# --- exact-id-diff attribution ----------------------------------------------------


def test_unwrap_add_source_rows_accepts_known_repeated_envelopes() -> None:
    """The decode the positional attribution is built on top of."""
    for payload in (
        [["src-a"], ["src-b"]],
        [[["src-a"], "A"], [["src-b"], "B"]],
        [[["src-a"], "A"], [[["src-b"], "B"]]],
        [[[["src-a"], "A"]], [[["src-b"], "B"]]],
        [[[["src-a"], "A"], [["src-b"], "B"]]],
    ):
        assert [Source.from_api_response(row).id for row in unwrap_add_source_rows(payload)] == [
            "src-a",
            "src-b",
        ]


@pytest.mark.asyncio
async def test_one_write_carries_every_url_and_preserves_order() -> None:
    service, executor = _service([_url_row("src-a", _A), _url_row("src-b", _B)])

    result = await service.add_urls_batch(_NB, (_A, _B))

    assert [item.url for item in result.items] == [_A, _B]
    assert [item.source.id if item.source else None for item in result.items] == [
        "src-a",
        "src-b",
    ]
    assert [item.error for item in result.items] == [None, None]
    # One write, and no reconciliation read at all: the echo was complete.
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
async def test_the_write_forwards_the_row_s_request_and_shared_deadline() -> None:
    service, executor = _service([_url_row("src-a", _A)])
    deadline = RuntimeDeadline(timeout=30.0, started_at=10.0, monotonic=lambda: 12.0)

    await service.add_urls_batch(_NB, (_A,), deadline=deadline)

    (create,) = executor.calls
    assert create.method is RPCMethod.ADD_SOURCE
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        # The retired row passed ``disable_internal_retries=True`` itself; the
        # leaf leaves the caller flag False and lets the reviewed
        # ``(ADD_SOURCE, "url")`` PROBE_THEN_CREATE row force the inner loop
        # off, which is the same effective dispatch.
        "operation_variant": "url",
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }


@pytest.mark.asyncio
async def test_mixed_web_page_and_youtube_intent_is_preserved_on_the_wire() -> None:
    youtube = "https://www.youtube.com/watch?v=abc123"
    service, executor = _service([_url_row("src-web", _A), _url_row("src-yt", youtube)])

    await service.add_urls_batch(_NB, (_A, youtube))

    (create,) = executor.calls
    specs, notebook_id, _template = create.params
    generic, video = specs
    assert notebook_id == _NB
    # The YouTube discriminator moves the URL from index 2 to index 7.
    assert generic[2] == [_A] and generic[7] is None
    assert video[7] == [youtube] and video[2] is None


@pytest.mark.asyncio
async def test_complete_legacy_short_rows_are_zipped_positionally() -> None:
    """Rows without URL metadata keep the documented positional fallback."""
    service, executor = _service([["src-a"], ["src-b"]])

    result = await service.add_urls_batch(_NB, (_A, _B))

    assert [item.source.id if item.source else None for item in result.items] == [
        "src-a",
        "src-b",
    ]
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
async def test_complete_canonicalized_urls_are_zipped_in_documented_response_order() -> None:
    urls = ["https://youtu.be/abc123", "https://example.com"]
    service, _executor = _service(
        [
            _url_row("src-yt", "https://www.youtube.com/watch?v=abc123"),
            _url_row("src-web", "https://example.com/"),
        ]
    )

    result = await service.add_urls_batch(_NB, tuple(urls))

    assert [item.url for item in result.items] == urls
    assert [item.source.id if item.source else None for item in result.items] == [
        "src-yt",
        "src-web",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_url", "response_url"),
    [
        (
            "http://[2001:db8::1]:80/article",
            "http://[2001:0DB8:0000:0000:0000:0000:0000:0001]/article",
        ),
        (
            "https://user%2fname:pa%3ass@example.com/item%2fpart?q=%ab",
            "https://user%2Fname:pa%3Ass@example.com/item%2Fpart?q=%AB",
        ),
    ],
)
async def test_sparse_equivalent_url_spellings_share_one_identity(
    requested_url: str,
    response_url: str,
) -> None:
    service, _executor = _service([_url_row("src-good", response_url)], _snapshot())

    result = await service.add_urls_batch(_NB, (requested_url, _B))

    assert result.items[0].source is not None
    assert result.items[0].source.id == "src-good"
    assert result.items[1].error is not None


@pytest.mark.asyncio
async def test_sparse_deep_row_preserves_bare_metadata_url_for_attribution() -> None:
    """A deeply wrapped row still identifies its request through bare metadata."""
    metadata: list[Any] = [_A, None, None, None, 5]
    service, _executor = _service(
        [[[["src-good"], _A, metadata, [None, SourceStatus.PROCESSING.value]]]],
        _snapshot(),
    )

    result = await service.add_urls_batch(_NB, (_A, _B))

    assert result.items[0].source is not None
    assert result.items[0].source.id == "src-good"
    assert result.items[0].source.url == _A
    assert result.items[1].error is not None


@pytest.mark.asyncio
async def test_sparse_canonical_youtube_url_matches_requested_video_identity() -> None:
    service, _executor = _service(
        [_url_row("src-yt", "https://www.youtube.com/watch?v=abc123")], _snapshot()
    )

    result = await service.add_urls_batch(_NB, ("https://youtu.be/abc123", _B))

    assert result.items[0].source is not None
    assert result.items[0].source.id == "src-yt"
    assert result.items[1].error is not None


@pytest.mark.asyncio
async def test_partial_batch_reconciles_omission_and_keeps_positional_outcomes() -> None:
    good_a, bad, good_b = _A, _B, "https://c.example.com"
    service, executor = _service(
        [_url_row("src-a", good_a), _url_row("src-b", good_b)],
        _snapshot(
            _entry("ghost-bad", title="Bad", url=bad, status=SourceStatus.ERROR.value),
            # A READY row with the same URL is not a ghost: the reconciliation
            # asks the leaf for ERROR rows only.
            _entry("ready-bad", title="Bad", url=bad, status=SourceStatus.READY.value),
        ),
    )

    result = await service.add_urls_batch(_NB, (good_a, bad, good_b))

    assert [item.source.id if item.source else None for item in result.items] == [
        "src-a",
        None,
        "src-b",
    ]
    failure = result.items[1].error
    assert failure is not None
    replayed = project_source_add_failure(failure)
    assert isinstance(replayed, SourceAddError)
    assert "ghost-bad" in str(replayed)
    assert "ready-bad" not in str(replayed)
    assert "Existing matching ERROR source row" in str(replayed)
    # The per-item failure still classifies as the non-fatal SOURCE_ADD, not as
    # a transport failure the adapter would report for the whole batch.
    assert classify(replayed).category is ErrorCategory.SOURCE_ADD
    create, reconcile = executor.calls
    assert create.method is RPCMethod.ADD_SOURCE
    assert reconcile.method is RPCMethod.GET_NOTEBOOK


@pytest.mark.asyncio
async def test_the_reconciliation_read_is_skipped_when_nothing_is_missing() -> None:
    service, executor = _service([_url_row("src-a", _A), _url_row("src-b", _B)])

    await service.add_urls_batch(_NB, (_A, _B))

    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
async def test_reconciliation_failure_preserves_known_positional_outcomes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Diagnostics only: its failure must not discard known positional outcomes."""
    service, _executor = _service(
        [_url_row("src-good", _A)],
        RPCError("listing drifted"),
    )

    with caplog.at_level("WARNING"):
        result = await service.add_urls_batch(_NB, (_A, _B))

    assert result.items[0].source is not None
    assert result.items[0].source.id == "src-good"
    failure = result.items[1].error
    assert failure is not None
    assert "ERROR source row" not in failure.message
    assert "failed to list ERROR rows" in caplog.text


# --- the eight synthesized failure graphs -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("urls", "responses", "detail", "rpc_message"),
    [
        pytest.param(
            [_A],
            [[[""], _A, [None, None, None, None, 5, None, None, [_A]], [None, 1]]],
            "The batch response did not identify its successful writes, so the "
            "committed subset is unknown; no automatic retry was attempted.",
            "ADD_SOURCE returned no decodable source rows with non-empty ids",
            id="id-less-row",
        ),
        pytest.param(
            [_A],
            [[_url_row("src-a", _A), _url_row("src-b", _B)]],
            "The batch response contained more sources than requested, so positional "
            "outcomes cannot be trusted; no automatic retry was attempted.",
            "ADD_SOURCE returned 2 rows for 1 requests",
            id="more-rows-than-requests",
        ),
        pytest.param(
            [_A, _B],
            [[_url_row("src-1", _A), _url_row("src-2", "https://other.example.com")]],
            "The complete batch response did not match request order, so positional "
            "outcomes cannot be trusted; no automatic retry was attempted.",
            "ADD_SOURCE returned unexpected positional URL 'https://other.example.com'",
            id="wrong-positional-identity",
        ),
        pytest.param(
            [_A, _B],
            [[_url_row("src-1", _A), _url_row("src-2", _A)]],
            "The complete batch response did not match request order, so positional "
            "outcomes cannot be trusted; no automatic retry was attempted.",
            f"ADD_SOURCE returned unexpected positional URL {_A!r}",
            id="duplicated-positional-identity",
        ),
        pytest.param(
            [_A, _B],
            [[_url_row("src-other", "https://other.example.com")]],
            "The batch response contained an unrequested source, so positional "
            "outcomes cannot be trusted; no automatic retry was attempted.",
            "ADD_SOURCE returned an unrequested URL 'https://other.example.com'",
            id="unrequested-url",
        ),
        pytest.param(
            [_A, _B],
            [[["src-only"]]],
            "The partial batch response omitted URL metadata needed to match rows "
            "back to inputs; no automatic retry was attempted.",
            "ADD_SOURCE returned sparse source rows without URL metadata",
            id="sparse-row-without-url",
        ),
        pytest.param(
            [_A, _B, "https://c.example.com"],
            [[_url_row("src-a1", _A), _url_row("src-a2", _A)]],
            "The batch response contained more sources than requested, so positional "
            "outcomes cannot be trusted; no automatic retry was attempted.",
            f"ADD_SOURCE returned 2 rows for 1 request(s) of {_A!r}",
            id="too-many-rows-for-one-identity",
        ),
    ],
)
async def test_an_unattributable_response_replays_its_manufactured_cause(
    urls: list[str],
    responses: list[Any],
    detail: str,
    rpc_message: str,
) -> None:
    """The eight ``RPCError``-as-cause branches, rebuilt from nested records.

    The pre-P10 service built the ``RPCError`` in-process purely to hang it off
    the ``SourceAddError``; there is no public exception to build above the
    port, so the workflow nests a record and the projector reconstructs the same
    two-node graph — including the bare ``raise``'s empty ``__cause__``.
    """
    service, _executor = _service(*responses)

    error = await _fails(service, urls)

    assert error.operation is Operation.SOURCE_ADD_URL_BATCH
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is True
    assert error.dispatched is False
    assert_replays(
        project_backend_error(error),
        _unresolved(urls, detail, RPCError(rpc_message)),
    )


@pytest.mark.asyncio
async def test_a_partially_admitted_duplicate_url_is_ambiguous() -> None:
    """The one branch whose report names the duplicate, not the whole batch."""
    service, _executor = _service([_url_row("src-one", _A)])

    error = await _fails(service, [_A, _A])

    assert_replays(
        project_backend_error(error),
        _unresolved(
            [_A, _A],
            "The backend partially admitted identical URLs without returning request "
            "positions, so the successful copies are ambiguous; no automatic retry "
            "was attempted.",
            RPCError(f"ADD_SOURCE partially admitted duplicate URL {_A!r}"),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, [], [[]], [123], [None], [""]])
async def test_a_malformed_success_payload_is_unconfirmed_not_per_item_rejection(
    payload: Any,
) -> None:
    service, executor = _service(payload)

    error = await _fails(service, [_A])

    assert error.outcome_unknown is True
    replayed = project_backend_error(error)
    assert isinstance(replayed, SourceAddError)
    assert "committed subset is unknown" in str(replayed)
    # One write, and no reconciliation read: nothing was attributable.
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
async def test_an_undecodable_create_response_carries_the_leaf_as_explicit_cause() -> None:
    """The one unresolved branch raised ``from`` a live exception."""
    native = DecodingError("garbled batch payload", method_id=RPCMethod.ADD_SOURCE.value)
    service, _executor = _service(native)

    error = await _fails(service, [_A, _B])

    expected = _unresolved(
        [_A, _B],
        "The create response could not be decoded, so the committed subset is unknown.",
        native,
    )
    expected.__cause__ = native
    expected.__context__ = native
    expected.__suppress_context__ = True
    assert_replays(project_backend_error(error), expected)


@pytest.mark.asyncio
async def test_an_undecodable_response_reaches_the_same_branch_from_a_subclass() -> None:
    """``UnknownRPCMethodError`` is a ``DecodingError``; it never gets rewritten."""
    native = UnknownRPCMethodError(
        "batch schema drift",
        method_id=RPCMethod.ADD_SOURCE.value,
        path=(0, 2),
        source="_sources.add_urls",
    )
    service, _executor = _service(native)

    error = await _fails(service, [_A])

    replayed = project_backend_error(error)
    assert isinstance(replayed, SourceAddError)
    assert "could not be decoded" in str(replayed)
    assert_replays(replayed.__cause__, native)


# --- the create's own failures ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_auth_rejection_keeps_its_re_authentication_contract() -> None:
    """It cannot have committed, so it is neither unconfirmed nor rewritten."""
    native = AuthError("csrf token expired")
    service, executor = _service(native)

    error = await _fails(service, [_A, _B])

    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is False
    replayed = project_backend_error(error)
    assert type(replayed) is AuthError
    assert_replays(replayed, native)
    assert getattr(replayed, "unconfirmed", False) is False
    # No reconciliation read: nothing was written to reconcile.
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native",
    [
        NetworkError("connection reset after write"),
        RateLimitError("rate limit response after write"),
        ServerError("upstream failed after write"),
    ],
    ids=["network", "rate-limit", "server"],
)
async def test_a_transport_failure_keeps_its_type_and_is_never_replayed(
    native: Exception,
) -> None:
    """ADR-0019: the type survives so callers can classify; only ``args`` change."""
    service, executor = _service(native)

    error = await _fails(service, [_A, _B])

    assert error.outcome_unknown is True
    replayed = project_backend_error(error)
    assert type(replayed) is type(native)
    assert getattr(replayed, "unconfirmed", False) is True
    assert classify(replayed).category is ErrorCategory.RPC
    assert str(replayed) == (
        "UNRESOLVED — do not blindly retry; check the notebook source list and "
        "reconcile the batch URLs first. The batch transport failed and an "
        "unknown subset may have committed; no automatic retry was attempted. "
        f"{native.args[0]}"
    )
    # One write, never repeated, and no reconciliation of an unknown subset.
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
async def test_a_generic_rpc_failure_is_not_misreported_as_all_rejected() -> None:
    native = RPCError("response decode drift", method_id=RPCMethod.ADD_SOURCE.value)
    service, executor = _service(native)

    error = await _fails(service, [_A, _B])

    assert error.outcome_unknown is True
    replayed = project_backend_error(error)
    assert type(replayed) is RPCError
    assert replayed.method_id == RPCMethod.ADD_SOURCE.value
    assert "without the documented all-rejected status" in str(replayed)
    assert getattr(replayed, "unconfirmed", False) is True
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


@pytest.mark.asyncio
async def test_the_documented_all_rejected_status_is_still_a_per_item_result_array() -> None:
    rejected = RPCError(
        "all sources rejected",
        method_id=RPCMethod.ADD_SOURCE.value,
        rpc_code=GrpcStatusCode.FAILED_PRECONDITION.value,
    )
    service, executor = _service(rejected, _snapshot())

    result = await service.add_urls_batch(_NB, (_A, _B))

    assert [item.url for item in result.items] == [_A, _B]
    assert all(item.source is None for item in result.items)
    for item, url in zip(result.items, (_A, _B), strict=True):
        assert item.error is not None
        replayed = project_source_add_failure(item.error)
        assert_replays(
            replayed,
            SourceAddError(
                url,
                cause=rejected,
                message=(
                    f"Failed to add URL source {url!r}: the backend omitted it from "
                    "the batch success response."
                ),
            ),
        )
    create, reconcile = executor.calls
    assert create.method is RPCMethod.ADD_SOURCE
    assert reconcile.method is RPCMethod.GET_NOTEBOOK


@pytest.mark.asyncio
async def test_a_string_all_rejected_status_is_read_the_same_way() -> None:
    """``rpc_code`` is ``str | int``; ``"9"`` has to answer like ``9``."""
    service, _executor = _service(
        RPCError("all sources rejected", rpc_code=str(GrpcStatusCode.FAILED_PRECONDITION.value)),
        _snapshot(),
    )

    result = await service.add_urls_batch(_NB, (_A,))

    assert result.items[0].error is not None


@pytest.mark.asyncio
async def test_the_all_rejected_items_carry_their_ghost_ids() -> None:
    rejected = RPCError("all sources rejected", rpc_code=GrpcStatusCode.FAILED_PRECONDITION.value)
    service, _executor = _service(
        rejected,
        _snapshot(_entry("ghost", title="Bad", url=_A, status=SourceStatus.ERROR.value)),
    )

    result = await service.add_urls_batch(_NB, (_A,))

    failure = result.items[0].error
    assert failure is not None
    replayed = project_source_add_failure(failure)
    assert "Existing matching ERROR source row(s): ghost." in str(replayed)
    assert isinstance(replayed, SourceAddError)
    assert_replays(replayed.cause, rejected)


@pytest.mark.asyncio
async def test_a_write_that_times_out_past_the_budget_stays_an_unconfirmed_timeout() -> None:
    """The aggregate budget moved with the workflow, so the head now owns expiry.

    The retired row absorbed the native ``RPCTimeoutError`` itself and the head
    never saw it, so a deadline that had already expired was invisible. The
    workflow mints the same ``CLIENT_TIMEOUT`` budget the head used to seed for
    this operation and threads it through the leaf, so an expiry is reported by
    the head — still as an ``RPCTimeoutError``, still unconfirmed (the write is
    a mutation and may have committed), and still attributed to the batch.
    """
    clock = [11.0]
    executor = _RecordingExecutor(RPCTimeoutError("slow", method_id=RPCMethod.ADD_SOURCE.value))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])
    inner = executor.rpc_call

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await inner(method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendError) as caught:
        await SourceService(backend).add_urls_batch(_NB, (_A,), deadline=deadline)

    error = caught.value
    assert isinstance(error, BackendDeadlineExceededError)
    assert error.operation is Operation.SOURCE_ADD_URL_BATCH
    assert error.outcome_unknown is True
    replayed = project_backend_error(error)
    assert isinstance(replayed, RPCTimeoutError)
    assert getattr(replayed, "unconfirmed", False) is True
    # One write, never repeated: an expiry is not a reason to re-issue a batch.
    assert [call.method for call in executor.calls] == [RPCMethod.ADD_SOURCE]


# --- the identity function the attribution rests on -------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://youtu.be/abc123", "https://www.youtube.com/watch?v=abc123"),
        ("https://Example.COM", "https://example.com/"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:80/x", "http://example.com/x"),
    ],
)
def test_equivalent_spellings_share_one_identity(left: str, right: str) -> None:
    assert _identity(left) == _identity(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://example.com/a", "https://example.com/b"),
        ("https://example.com/x?q=1", "https://example.com/x?q=2"),
        ("https://example.com:8443/x", "https://example.com/x"),
        ("not a url", "also not a url"),
        ("http://example.com:notaport/x", "http://example.com/x"),
    ],
)
def test_distinct_addresses_keep_distinct_identities(left: str, right: str) -> None:
    assert _identity(left) != _identity(right)


@pytest.mark.parametrize(
    "malformed",
    ["http://example.com:notaport/x", "http://example.com:99999/x", "//no-scheme/x", ""],
)
def test_a_malformed_url_stays_distinct_rather_than_raising(malformed: str) -> None:
    """A response URL is untrusted wire data; normalization must fail closed, not raise."""
    assert _identity(malformed) == ("raw", malformed.strip())


def test_the_register_leaf_dispatches_the_url_variant() -> None:
    assert SourceRegisterKind.URL.value == "url"
