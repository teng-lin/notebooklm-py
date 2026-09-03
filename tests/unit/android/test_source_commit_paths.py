"""Evidence-isolation and error-propagation paths of the Android source adapter.

These cover the seams where a write's outcome is *unknown* rather than known:
the row-level proof collector, the commit reconciler, and the transport-error
edges of the read and mutate surfaces. The recurring contract is the three-way
outcome distinction -- confirmed, refused, unconfirmed -- and that an
unconfirmed write is never dressed up as a safely retryable one.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest

import notebooklm._android.sources as sources_module
from notebooklm._android.phenotype import PhenotypeTokenProvider
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import source_content_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import (
    ADD_SOURCES_METHOD,
    ADD_TENTATIVE_SOURCES_METHOD,
    CHECK_SOURCE_FRESHNESS_METHOD,
    DELETE_SOURCES_METHOD,
    GENERATE_DOCUMENT_GUIDES_METHOD,
    GET_PROJECT_METHOD,
    LOAD_SOURCE_METHOD,
    MUTATE_SOURCE_METHOD,
    AndroidSourcesAPI,
    _canonical_source_id,
    _collect_commit_proofs,
    _ProofKind,
)
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm.exceptions import (
    AuthError,
    ConfigurationError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceTimeoutError,
)
from notebooklm.types import SourceStatus

NOTEBOOK_ID = "00000000-0000-4000-8000-000000000100"
SOURCE_A = "00000000-0000-4000-8000-000000000101"
SOURCE_B = "00000000-0000-4000-8000-000000000102"
URL_A = "https://example.invalid/first"

_SETTINGS = source_settings_pb2


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


Handler = Callable[[Any, dict[str, Any]], Any]


class FakeTransport:
    """Recording direct-test transport with one epoch-bearing workflow scope."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler | deque[Any] | Any] = {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        if method == GET_PROJECT_METHOD and method not in self.handlers:
            return _project(_source(SOURCE_A), _source(SOURCE_B))
        result = self.handlers[method]
        if isinstance(result, deque):
            result = result.popleft()
        if callable(result):
            result = result(request, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    async def prepare_metadata(
        self,
        metadata_augmentor: Any,
        *,
        expected_epoch: int,
    ) -> tuple[tuple[str, str | bytes], ...]:
        assert expected_epoch == 7
        return tuple(await metadata_augmentor("fake-bearer"))


class FakePhenotype:
    async def experiment_metadata(
        self,
        bearer: str,
        *,
        force: bool = False,
    ) -> tuple[tuple[str, bytes], ...]:
        del bearer, force
        return (("x-phenotype-bin", b"metadata"),)


def _api(transport: FakeTransport, **kwargs: Any) -> AndroidSourcesAPI:
    return AndroidSourcesAPI(
        cast(AndroidSession, transport),
        cast(AndroidUploadPipeline, object()),
        phenotype=cast(PhenotypeTokenProvider, FakePhenotype()),
        **kwargs,
    )


def _source(
    source_id: str,
    *,
    title: str = "Example",
    url: str = URL_A,
    status: int = _SETTINGS.SOURCE_STATUS_PENDING,
) -> read_pb2.Source:
    return read_pb2.Source(
        source_id=read_pb2.SourceId(id=source_id),
        title=title,
        metadata=read_pb2.SourceMetadata(
            original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
            webpage_metadata=read_pb2.WebpageMetadata(url=url),
        ),
        settings=source_settings_pb2.SourceSettings(status=status),
    )


def _project(*sources: read_pb2.Source) -> read_pb2.GetProjectResponse:
    return read_pb2.GetProjectResponse(
        project=read_pb2.Project(id=NOTEBOOK_ID, title="Notebook", sources=sources)
    )


def _registration_handler(ids: list[str]) -> Handler:
    def _handle(request: Any, kwargs: dict[str, Any]) -> Any:
        del kwargs
        return sources_pb2.AddTentativeSourcesResponse(
            tentative_sources=[
                _source(source_id, title=metadata.name, status=0)
                for metadata, source_id in zip(
                    request.tentative_sources_metadata,
                    ids,
                    strict=True,
                )
            ]
        )

    return _handle


class _UnreadableRow:
    """A row whose structure cannot be probed at all."""

    def HasField(self, name: str) -> bool:
        raise AttributeError(name)


class _UndecodableRow:
    """A row that answers the status probe but has no decodable projection."""

    def __init__(self, source_id: str, status: int) -> None:
        self.source_id = SimpleNamespace(id=source_id)
        self.settings = SimpleNamespace(status=status)

    def HasField(self, name: str) -> bool:
        return name in {"source_id", "settings"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(SOURCE_A, SOURCE_A, id="canonical"),
        pytest.param(SOURCE_A.upper(), SOURCE_A, id="uppercase-normalizes"),
        pytest.param("0" * 32 + "zzzz", None, id="right-length-not-a-uuid"),
        pytest.param("urn:uuid:" + SOURCE_A, None, id="urn-alias"),
        pytest.param("{" + SOURCE_A + "}", None, id="braced-alias"),
        pytest.param(SOURCE_A.replace("-", ""), None, id="undashed-alias"),
        pytest.param("", None, id="empty"),
    ],
)
def test_canonical_source_id_accepts_only_exact_dashed_uuids(
    value: str,
    expected: str | None,
) -> None:
    """Only an exact 36-char dashed UUID may become a commit-proof key.

    Aliases (``urn:``, braces, undashed) name the same UUID but are not the
    string the backend echoes, so admitting them would let a proof be filed
    against an id the caller never registered.
    """
    assert _canonical_source_id(value) == expected


def test_canonical_source_id_swallows_a_non_string_of_canonical_length() -> None:
    """The guard is a decoder, so a hostile value returns ``None``, not a crash.

    Its callers treat ``None`` as "this row proves nothing"; an ``AttributeError``
    escaping instead would surface as an adapter bug rather than as drift.
    """

    class _LengthOnly:
        def __len__(self) -> int:
            return 36

    assert _canonical_source_id(cast(Any, _LengthOnly())) is None


def test_unreadable_rows_are_isolated_without_poisoning_their_neighbours() -> None:
    """One structurally unreadable row proves nothing and taints nothing.

    It cannot be assigned to an exact id, so it is neither a proof nor an
    ``unresolved`` marker -- the readable row beside it still commits.
    """
    proofs, unresolved = _collect_commit_proofs(
        [_UnreadableRow(), _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE)],
        [SOURCE_A],
        method_id=ADD_SOURCES_METHOD,
    )

    assert unresolved == set()
    assert set(proofs) == {SOURCE_A}
    assert proofs[SOURCE_A].kind is _ProofKind.COMPLETE


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_source(SOURCE_B, status=_SETTINGS.SOURCE_STATUS_COMPLETE), id="foreign-id"),
        pytest.param(_source("not-a-uuid", status=_SETTINGS.SOURCE_STATUS_COMPLETE), id="alias-id"),
        pytest.param(_source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_TENTATIVE), id="tentative"),
        pytest.param(_source(SOURCE_A, status=999), id="unknown-status"),
    ],
)
def test_rows_outside_the_exact_id_and_status_evidence_set_prove_nothing(
    row: read_pb2.Source,
) -> None:
    """A proof needs both an exact candidate id and an affirmative status.

    Neither a row for another source nor a still-tentative row is evidence the
    commit landed, and neither may be recorded as unresolved either -- doing so
    would strand a candidate that other rows could still prove.
    """
    proofs, unresolved = _collect_commit_proofs(
        [row],
        [SOURCE_A],
        method_id=ADD_SOURCES_METHOD,
    )

    assert proofs == {}
    assert unresolved == set()


def test_an_undecodable_candidate_row_retracts_an_earlier_proof_for_that_id() -> None:
    """A row that cannot be decoded makes its id unresolved, not merely absent.

    Absent would read as "no evidence yet"; unresolved records that the backend
    said something about this exact id that we could not read, so the commit
    outcome is unknown rather than pending.
    """
    proofs, unresolved = _collect_commit_proofs(
        [
            _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE),
            _UndecodableRow(SOURCE_A, _SETTINGS.SOURCE_STATUS_COMPLETE),
        ],
        [SOURCE_A],
        method_id=ADD_SOURCES_METHOD,
    )

    assert proofs == {}
    assert unresolved == {SOURCE_A}


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(
            _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_ERROR),
            id="conflicting-status",
        ),
        pytest.param(
            _source(SOURCE_A, title="Other", status=_SETTINGS.SOURCE_STATUS_COMPLETE),
            id="conflicting-payload",
        ),
    ],
)
def test_two_disagreeing_rows_for_one_id_leave_it_unresolved(
    second: read_pb2.Source,
) -> None:
    """Disagreement between rows for one id is unknown, never first-wins.

    Picking either row would report a definite outcome the backend did not
    actually state.
    """
    proofs, unresolved = _collect_commit_proofs(
        [_source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE), second],
        [SOURCE_A],
        method_id=ADD_SOURCES_METHOD,
    )

    assert proofs == {}
    assert unresolved == {SOURCE_A}


def test_an_unresolved_id_is_never_resurrected_by_a_later_agreeing_row() -> None:
    """Once contradicted, an id stays unresolved for the rest of the envelope.

    A third row echoing the first would otherwise let a contradicted commit be
    reported as confirmed simply because the disagreeing row came in the middle.
    """
    complete = _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE)
    proofs, unresolved = _collect_commit_proofs(
        [complete, _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_ERROR), complete],
        [SOURCE_A],
        method_id=ADD_SOURCES_METHOD,
    )

    assert proofs == {}
    assert unresolved == {SOURCE_A}


class _ProjectProxy:
    """A real ``Project`` whose ``sources`` are replaced by arbitrary rows."""

    def __init__(self, project: Any, rows: list[Any]) -> None:
        self._project = project
        self._rows = rows

    @property
    def sources(self) -> list[Any]:
        return self._rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._project, name)


def _project_with_rows(*rows: Any) -> Any:
    """A ``GetProject`` response carrying rows a protobuf could not hold."""
    return SimpleNamespace(project=_ProjectProxy(_project().project, list(rows)))


def _stepping_clock(*readings: float) -> Callable[[], float]:
    """A monotonic clock that returns each reading once, then the last forever."""
    queue = deque(readings)

    def read() -> float:
        if len(queue) > 1:
            return queue.popleft()
        return queue[0]

    return read


@pytest.mark.asyncio
async def test_uncorrelatable_file_registration_is_unconfirmed_not_a_clean_failure() -> None:
    """A registration echo we cannot pin to one id leaves an upload in doubt.

    The backend answered, so a tentative source may exist; reporting a clean
    failure would invite a retry that silently doubles the source.
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler(["not-a-uuid"])

    with pytest.raises(SourceAddError) as raised:
        await _api(transport)._register_file_tentative(NOTEBOOK_ID, "report.pdf", 7, 30.0)

    assert getattr(raised.value, "unconfirmed", False) is True
    assert getattr(raised.value, "stage", None) == "register"
    assert "unconfirmed" in str(raised.value)


@pytest.mark.asyncio
async def test_upload_polling_skips_an_unreadable_row_and_still_accepts_the_match() -> None:
    """One unreadable neighbour must not blind the poll to the real source."""
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = _project_with_rows(
        _UnreadableRow(),
        _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE),
    )

    source = await _api(transport)._wait_uploaded_source(NOTEBOOK_ID, SOURCE_A, 30.0, 7, ready=True)

    assert source.id == SOURCE_A
    assert source.status is SourceStatus.READY
    assert [call[0] for call in transport.calls] == [GET_PROJECT_METHOD]


@pytest.mark.asyncio
async def test_upload_polling_never_reads_a_status_off_a_different_source() -> None:
    """A sibling row is not evidence about the uploaded id.

    ``last_status is None`` is the assertion that bites: if the loop matched
    any row it would report the sibling's PENDING as the upload's own status.
    """
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_B))
    api = _api(transport, monotonic=_stepping_clock(0.0, 1.0))

    with pytest.raises(SourceTimeoutError) as raised:
        await api._wait_uploaded_source(NOTEBOOK_ID, SOURCE_A, 0.01, 7, ready=False)

    assert raised.value.last_status is None
    assert [call[0] for call in transport.calls] == [GET_PROJECT_METHOD]


@pytest.mark.asyncio
async def test_duplicate_rows_for_the_uploaded_id_are_drift_not_a_coin_flip() -> None:
    """Two rows for one id make the upload's state ambiguous, so decoding fails."""
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE),
        _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_ERROR),
    )

    with pytest.raises(DecodingError) as raised:
        await _api(transport)._wait_uploaded_source(NOTEBOOK_ID, SOURCE_A, 30.0, 7, ready=True)

    assert raised.value.method_id == GET_PROJECT_METHOD
    assert "duplicate source ids" in str(raised.value)


@pytest.mark.asyncio
async def test_a_pending_source_times_out_under_ready_but_settles_under_registered() -> None:
    """``ready`` is the whole difference between PENDING accepted and rejected.

    The timeout still carries the status it observed, so the caller can tell a
    source that is processing from one that never appeared.
    """
    pending = _project(_source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_PENDING))

    strict = FakeTransport()
    strict.handlers[GET_PROJECT_METHOD] = pending
    with pytest.raises(SourceTimeoutError) as raised:
        await _api(strict, monotonic=_stepping_clock(0.0, 1.0))._wait_uploaded_source(
            NOTEBOOK_ID, SOURCE_A, 0.01, 7, ready=True
        )
    assert raised.value.last_status == _SETTINGS.SOURCE_STATUS_PENDING

    lenient = FakeTransport()
    lenient.handlers[GET_PROJECT_METHOD] = pending
    source = await _api(lenient)._wait_uploaded_source(NOTEBOOK_ID, SOURCE_A, 30.0, 7, ready=False)
    assert source.id == SOURCE_A


@pytest.mark.asyncio
async def test_a_rename_echo_for_another_source_is_rejected_after_upload() -> None:
    """A mutation echoing a different id means the title landed somewhere else.

    Returning that title would report a rename of the uploaded source that
    never happened.
    """
    transport = FakeTransport()
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse(
        source=_source(SOURCE_B, title="Renamed")
    )

    with pytest.raises(DecodingError) as raised:
        await _api(transport)._rename_uploaded(NOTEBOOK_ID, SOURCE_A, "Renamed", 7)

    assert raised.value.method_id == MUTATE_SOURCE_METHOD
    assert "unexpected source id" in str(raised.value)


@pytest.mark.asyncio
async def test_cancelling_the_reconciliation_read_is_not_read_as_missing_evidence() -> None:
    """Cancellation is not an RPC failure, so it must not be absorbed.

    ``_read_commit_proofs`` swallows transport errors into "no evidence", which
    for a cancelled caller would silently downgrade the outcome instead of
    unwinding.
    """
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _api(transport)._read_commit_proofs(NOTEBOOK_ID, [SOURCE_A], expected_epoch=7)


@pytest.mark.asyncio
async def test_cancelling_the_commit_call_unwinds_before_any_reconciliation() -> None:
    """A cancelled commit stops the workflow; it does not go on to read back.

    The uncertain-commit handlers below it catch ``NetworkError`` and friends;
    cancellation must reach the caller instead of being reshaped into one.
    """
    transport = FakeTransport()
    transport.handlers[ADD_SOURCES_METHOD] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _api(transport)._commit_user_contents(
            NOTEBOOK_ID,
            [(sources_pb2.UserContent(), SOURCE_A)],
            expected_epoch=7,
        )

    assert [call[0] for call in transport.calls] == [ADD_SOURCES_METHOD]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("add_text", id="add_text"),
        pytest.param("add_url", id="add_url"),
        pytest.param("add_urls_batch", id="add_urls_batch"),
    ],
)
async def test_cancelling_registration_never_becomes_a_source_add_error(
    operation: str,
) -> None:
    """Cancellation must not be laundered into an ``unconfirmed`` add failure.

    The batch path is the sharpest case: it converts registration failures into
    per-URL error items and returns normally, which would swallow the cancel.
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = asyncio.CancelledError()
    api = _api(transport)

    with pytest.raises(asyncio.CancelledError):
        if operation == "add_text":
            await api.add_text(NOTEBOOK_ID, "Title", "Body")
        elif operation == "add_url":
            await api.add_url(NOTEBOOK_ID, URL_A)
        else:
            await api._add_urls_batch(NOTEBOOK_ID, [URL_A])


@pytest.mark.asyncio
async def test_an_unreadable_commit_envelope_still_lets_the_read_back_prove_the_add() -> None:
    """A malformed ``AddSources`` reply is an uncertain commit, not an adapter bug.

    The wire call completed, so the one exact-id read below it must still get
    its chance to prove acceptance rather than the decode crash escaping.
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = object()
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE)
    )

    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert result.id == SOURCE_A
    assert result.status is SourceStatus.READY
    assert [call[0] for call in transport.calls] == [
        ADD_TENTATIVE_SOURCES_METHOD,
        ADD_SOURCES_METHOD,
        GET_PROJECT_METHOD,
    ]


@pytest.mark.asyncio
async def test_a_contradicted_commit_stays_unconfirmed_even_when_the_read_back_is_clean() -> None:
    """A clean later read must not overwrite an earlier self-contradiction.

    The commit reply disagreed with itself about this exact id, so what the
    backend did is unknown; merging the tidy read-back on top would hand back a
    confirmed source and invite a retry that duplicates it.
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse(
        sources=[
            _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE),
            _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_ERROR),
        ]
    )
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, status=_SETTINGS.SOURCE_STATUS_COMPLETE)
    )

    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "UNRESOLVED" in str(raised.value)


@pytest.mark.asyncio
async def test_registered_content_reports_an_omitted_registration_as_a_clean_failure() -> None:
    """Nothing was registered, so nothing was written: this one is safe to retry.

    Its ``unconfirmed`` flag must stay false -- that flag is what tells callers
    a retry could duplicate.
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = sources_pb2.AddTentativeSourcesResponse()

    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_text(NOTEBOOK_ID, "Title", "Body")

    assert getattr(raised.value, "unconfirmed", False) is False
    assert "omitted its registration" in str(raised.value)
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.asyncio
async def test_registered_content_reports_an_uncorrelatable_registration_as_unconfirmed() -> None:
    """A registration we cannot pin to an id may still have created a source."""
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler(["not-a-uuid"])

    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_text(NOTEBOOK_ID, "Title", "Body")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "tentative registration correlation" in str(raised.value)
    assert ADD_SOURCES_METHOD not in [call[0] for call in transport.calls]


@pytest.mark.asyncio
async def test_registered_content_without_commit_acceptance_is_unconfirmed() -> None:
    """Registration landed but no row ever proved the commit.

    The tentative source may exist upstream, so the failure names the commit
    stage and stays unconfirmed rather than reading as "nothing happened".
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse()
    transport.handlers[GET_PROJECT_METHOD] = _project()

    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_text(NOTEBOOK_ID, "Title", "Body")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "source commit acceptance" in str(raised.value)
    assert transport.scopes == ["source.add_text"]


@pytest.mark.asyncio
async def test_a_blank_drive_title_never_dispatches_a_finalizing_rename() -> None:
    """``add_drive`` treats a blank title as "no title asked for".

    The upstream title stands and no mutation is sent, so a whitespace-only
    argument cannot blank out the source's real name.
    """
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse(
        sources=[_source(SOURCE_A, title="Upstream")]
    )
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A, title="Upstream"))

    result = await _api(transport).add_drive(NOTEBOOK_ID, "drive-file-id", "   ")

    assert result.title == "Upstream"
    assert MUTATE_SOURCE_METHOD not in [call[0] for call in transport.calls]


@pytest.mark.asyncio
async def test_drive_file_import_without_a_download_pipeline_is_a_configuration_error() -> None:
    """Without the native download scope there is no way to fetch the bytes.

    Failing here keeps the caller from seeing a generic attribute error later,
    and no notebook write is attempted.
    """
    transport = FakeTransport()

    with pytest.raises(ConfigurationError, match="native download pipeline"):
        await _api(transport).add_drive_file(NOTEBOOK_ID, "drive-file-id")

    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(AuthError("expired"), id="auth"),
        pytest.param(RateLimitError("slow down", rpc_code=8), id="rate-limit"),
        pytest.param(ServerError("boom", rpc_code=14), id="server"),
        pytest.param(NetworkError("down"), id="network"),
        pytest.param(RPCError("bad argument", rpc_code=3), id="rpc-not-not-found"),
    ],
)
async def test_delete_only_absorbs_not_found_and_propagates_every_other_failure(
    failure: Exception,
) -> None:
    """Delete's idempotence covers absence, not "the call did not get through".

    Absorbing a transport failure would report a source as deleted while it is
    still in the notebook.
    """
    transport = FakeTransport()
    transport.handlers[DELETE_SOURCES_METHOD] = failure

    with pytest.raises(type(failure)):
        await _api(transport).delete(NOTEBOOK_ID, SOURCE_A)


@pytest.mark.asyncio
async def test_delete_absorbs_a_not_found_from_the_delete_call_itself() -> None:
    """The row vanished between the ownership check and the delete: still done."""
    transport = FakeTransport()
    transport.handlers[DELETE_SOURCES_METHOD] = RPCError("gone", rpc_code=5)

    assert await _api(transport).delete(NOTEBOOK_ID, SOURCE_A) is None

    assert DELETE_SOURCES_METHOD in [call[0] for call in transport.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(AuthError("expired"), id="auth"),
        pytest.param(RateLimitError("slow down", rpc_code=8), id="rate-limit"),
        pytest.param(ServerError("boom", rpc_code=14), id="server"),
        pytest.param(NetworkError("down"), id="network"),
        pytest.param(RPCError("bad argument", rpc_code=3), id="rpc-not-not-found"),
    ],
)
async def test_rename_propagates_transport_failures_instead_of_claiming_absence(
    failure: Exception,
) -> None:
    """Only NOT_FOUND may become ``SourceNotFoundError``.

    Mapping a rate limit or an auth expiry onto absence would tell the caller
    the source is gone when it is merely unreachable.
    """
    transport = FakeTransport()
    transport.handlers[MUTATE_SOURCE_METHOD] = failure

    with pytest.raises(type(failure)) as raised:
        await _api(transport).rename(NOTEBOOK_ID, SOURCE_A, "Renamed")

    assert not isinstance(raised.value, SourceNotFoundError)


@pytest.mark.asyncio
async def test_rename_rejects_a_mutation_echo_naming_a_different_source() -> None:
    """An echo for another id means the title was applied somewhere else."""
    transport = FakeTransport()
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse(
        source=_source(SOURCE_B, title="Renamed")
    )

    with pytest.raises(DecodingError) as raised:
        await _api(transport).rename(NOTEBOOK_ID, SOURCE_A, "Renamed")

    assert raised.value.method_id == MUTATE_SOURCE_METHOD


@pytest.mark.asyncio
async def test_rename_with_return_object_false_skips_decoding_the_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``return_object=False`` still validates identity but hands back nothing.

    The id check must happen first: a caller that ignores the return value
    still needs a wrong-source rename to raise. The echo here is fully
    decodable, so a ``None`` result alone cannot tell "never decoded" apart
    from "decoded and discarded" — hence the spy.
    """
    transport = FakeTransport()
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse(
        source=_source(SOURCE_A, title="Renamed")
    )

    decodes: list[str] = []
    real_decode_source = sources_module.decode_source

    def _spy(row: Any, **kwargs: Any) -> Any:
        decodes.append(kwargs.get("method_id", ""))
        return real_decode_source(row, **kwargs)

    monkeypatch.setattr(sources_module, "decode_source", _spy)

    assert (
        await _api(transport).rename(NOTEBOOK_ID, SOURCE_A, "Renamed", return_object=False) is None
    )

    assert decodes == [], "the echo was decoded despite return_object=False"
    assert GET_PROJECT_METHOD in [call[0] for call in transport.calls]
    assert [call[0] for call in transport.calls].count(GET_PROJECT_METHOD) == 1


@pytest.mark.asyncio
async def test_a_silent_rename_whose_readback_lost_the_source_raises_not_found() -> None:
    """The source was owned before the mutation and absent after it.

    With no echo to trust, the read-back is the only evidence; an empty one
    must raise rather than fabricate a renamed source.
    """
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = deque([_project(_source(SOURCE_A)), _project()])
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse()

    with pytest.raises(SourceNotFoundError) as raised:
        await _api(transport).rename(NOTEBOOK_ID, SOURCE_A, "Renamed")

    assert raised.value.method_id == MUTATE_SOURCE_METHOD
    assert [call[0] for call in transport.calls].count(GET_PROJECT_METHOD) == 2


@pytest.mark.asyncio
async def test_internal_freshness_probe_omits_the_epoch_when_it_has_none() -> None:
    """An epoch-free probe must not send ``expected_epoch=None`` to the session.

    The session treats the keyword as a real epoch assertion, so it is left off
    entirely rather than passed as a null.
    """
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = sources_pb2.CheckSourceFreshnessResponse()

    assert await _api(transport)._check_freshness(SOURCE_A) is True

    [(_method, _request, kwargs)] = transport.calls
    assert "expected_epoch" not in kwargs
    assert kwargs["replay_safe"] is True


@pytest.mark.asyncio
async def test_a_freshness_row_without_a_verdict_reads_as_fresh() -> None:
    """An unset ``is_fresh`` is no verdict, and no verdict means no refresh.

    Defaulting the other way would send a mutation the native handler rejects.
    """
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = sources_pb2.CheckSourceFreshnessResponse(
        source_freshness=sources_pb2.SourceFreshness(source_id=read_pb2.SourceId(id=SOURCE_A))
    )

    assert await _api(transport).check_freshness(NOTEBOOK_ID, SOURCE_A) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(AuthError("expired"), id="auth"),
        pytest.param(RateLimitError("slow down", rpc_code=8), id="rate-limit"),
        pytest.param(ServerError("boom", rpc_code=14), id="server"),
        pytest.param(NetworkError("down"), id="network"),
        pytest.param(RPCError("bad argument", rpc_code=3), id="rpc-not-not-found"),
    ],
)
async def test_get_guide_returns_an_empty_guide_only_for_not_found(
    failure: Exception,
) -> None:
    """Every other failure propagates; only NOT_FOUND means "no guide".

    An empty guide returned on a rate limit would look like a summarised
    source with nothing to say.
    """
    transport = FakeTransport()
    transport.handlers[GENERATE_DOCUMENT_GUIDES_METHOD] = failure

    with pytest.raises(type(failure)):
        await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_format",
    [
        pytest.param("html", id="unsupported-format"),
        pytest.param("Markdown", id="wrong-case"),
        pytest.param("", id="empty"),
    ],
)
async def test_get_fulltext_rejects_an_unknown_format_before_touching_the_wire(
    output_format: str,
) -> None:
    """The format is validated first so a typo costs no round-trip.

    It also cannot fall through to the plain-text branch, which would quietly
    hand back text to a caller who asked for something else.
    """
    transport = FakeTransport()

    with pytest.raises(ValueError, match="Must be 'text' or 'markdown'"):
        await _api(transport).get_fulltext(
            NOTEBOOK_ID,
            SOURCE_A,
            output_format=cast(Any, output_format),
        )

    assert transport.calls == []


@pytest.mark.asyncio
async def test_markdown_without_markdownify_names_the_extra_before_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The missing optional dependency is reported as an install hint, not a crash.

    Checking before the request means a caller without the extra never pays for
    a ``LoadSource`` whose result they cannot render.
    """
    monkeypatch.setitem(__import__("sys").modules, "markdownify", None)
    transport = FakeTransport()

    with pytest.raises(ImportError, match=r"notebooklm-py\[markdown\]"):
        await _api(transport).get_fulltext(NOTEBOOK_ID, SOURCE_A, output_format="markdown")

    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(AuthError("expired"), id="auth"),
        pytest.param(RateLimitError("slow down", rpc_code=8), id="rate-limit"),
        pytest.param(ServerError("boom", rpc_code=14), id="server"),
        pytest.param(NetworkError("down"), id="network"),
        pytest.param(RPCError("bad argument", rpc_code=3), id="rpc-not-not-found"),
    ],
)
async def test_get_fulltext_propagates_transport_failures_instead_of_claiming_absence(
    failure: Exception,
) -> None:
    """Only NOT_FOUND may become ``SourceNotFoundError`` on the content read."""
    transport = FakeTransport()
    transport.handlers[LOAD_SOURCE_METHOD] = failure

    with pytest.raises(type(failure)) as raised:
        await _api(transport).get_fulltext(NOTEBOOK_ID, SOURCE_A)

    assert not isinstance(raised.value, SourceNotFoundError)


@pytest.mark.asyncio
async def test_get_fulltext_maps_not_found_and_a_sourceless_reply_to_the_same_absence() -> None:
    """A NOT_FOUND status and an empty envelope both mean "no such source".

    The chained NOT_FOUND is suppressed so callers see the domain error rather
    than a raw RPC code.
    """
    refused = FakeTransport()
    refused.handlers[LOAD_SOURCE_METHOD] = RPCError("gone", rpc_code=5)
    with pytest.raises(SourceNotFoundError) as from_status:
        await _api(refused).get_fulltext(NOTEBOOK_ID, SOURCE_A)
    assert from_status.value.method_id == LOAD_SOURCE_METHOD
    assert from_status.value.__cause__ is None

    empty = FakeTransport()
    empty.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse()
    with pytest.raises(SourceNotFoundError) as from_envelope:
        await _api(empty).get_fulltext(NOTEBOOK_ID, SOURCE_A)
    assert from_envelope.value.method_id == LOAD_SOURCE_METHOD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_format",
    [pytest.param("text", id="text"), pytest.param("markdown", id="markdown")],
)
async def test_a_source_with_no_renderable_body_warns_and_returns_an_empty_fulltext(
    output_format: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty body is a real answer, but a loud one.

    The source exists and decoded cleanly, so raising would be wrong; staying
    silent would hide a backend that stopped shipping content.
    """
    transport = FakeTransport()
    transport.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse(
        source=_source(SOURCE_A, title="Document")
    )

    with caplog.at_level(logging.WARNING, logger="notebooklm._android.sources"):
        fulltext = await _api(transport).get_fulltext(
            NOTEBOOK_ID,
            SOURCE_A,
            output_format=cast(Any, output_format),
        )

    assert fulltext.content == ""
    assert fulltext.char_count == 0
    assert fulltext.title == "Document"
    assert fulltext.source_id == SOURCE_A
    assert [record.getMessage() for record in caplog.records] == [
        f"Android source {SOURCE_A} returned empty {output_format} content"
    ]


def _run_off_loop(coro: Any) -> Any:
    """Drive a coroutine to completion without an event loop.

    Every await on these paths resolves synchronously -- the fake transport
    raises before anything yields -- so the coroutine can simply be stepped.
    That is what makes the interrupt cases below testable at all: a
    ``KeyboardInterrupt`` raised inside an ``asyncio.Task`` is re-raised into
    the event loop by ``Task.__step`` and would tear the session down rather
    than reach ``pytest.raises``.
    """
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("coroutine suspended; it cannot be driven off the loop")


def _interrupt_transport(method: str, interrupt: BaseException) -> FakeTransport:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse(sources=[])
    transport.handlers[method] = interrupt
    return transport


@pytest.mark.parametrize(
    "interrupt_type",
    [pytest.param(KeyboardInterrupt, id="keyboard-interrupt"), pytest.param(SystemExit, id="exit")],
)
@pytest.mark.parametrize(
    ("entry_point", "method"),
    [
        pytest.param("register_file", ADD_TENTATIVE_SOURCES_METHOD, id="register-file"),
        pytest.param("read_proofs", GET_PROJECT_METHOD, id="read-commit-proofs"),
        pytest.param("commit", ADD_SOURCES_METHOD, id="commit-user-contents"),
        pytest.param("add_text", ADD_TENTATIVE_SOURCES_METHOD, id="add-text"),
        pytest.param("add_url", ADD_TENTATIVE_SOURCES_METHOD, id="add-url"),
        pytest.param("add_urls_batch", ADD_TENTATIVE_SOURCES_METHOD, id="add-urls-batch"),
    ],
)
def test_process_interrupts_escape_every_uncertain_outcome_handler(
    entry_point: str,
    method: str,
    interrupt_type: type[BaseException],
) -> None:
    """A shutdown signal must never be reshaped into a source-write outcome.

    Each of these paths sits behind a broad handler that turns transport
    trouble into "unconfirmed", "no evidence", or a per-URL error item. Any of
    those would swallow a Ctrl-C and let the interpreter keep going with a
    half-finished write instead of unwinding.
    """
    transport = _interrupt_transport(method, interrupt_type())
    api = _api(transport)
    coroutines = {
        "register_file": lambda: api._register_file_tentative(NOTEBOOK_ID, "report.pdf", 7, 30.0),
        "read_proofs": lambda: api._read_commit_proofs(NOTEBOOK_ID, [SOURCE_A], expected_epoch=7),
        "commit": lambda: api._commit_user_contents(
            NOTEBOOK_ID,
            [(sources_pb2.UserContent(), SOURCE_A)],
            expected_epoch=7,
        ),
        "add_text": lambda: api.add_text(NOTEBOOK_ID, "Title", "Body"),
        "add_url": lambda: api.add_url(NOTEBOOK_ID, URL_A),
        "add_urls_batch": lambda: api._add_urls_batch(NOTEBOOK_ID, [URL_A]),
    }

    with pytest.raises(interrupt_type):
        _run_off_loop(coroutines[entry_point]())
