"""Android source reads plus evidence-qualified source operations."""

from __future__ import annotations

import asyncio
import builtins
import logging
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast

from .._deadline import RuntimeDeadline
from .._idempotency import mark_unconfirmed, unresolved_commit_error
from .._runtime.call_supervisor import OperationLease
from .._source.batch import SourceUrlBatchItem
from .._source.polling import SourcePoller
from .._sources import SourcesAPI, _validate_add_text_idempotency, validate_search
from .._types.documents import StructuredDocument
from .._types.research import SourceGuide
from .._url_utils import is_youtube_url
from ..exceptions import (
    AuthError,
    ConfigurationError,
    DecodingError,
    NetworkError,
    PlayBookNotExportableError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    ValidationError,
)
from ..types import PlayBook, RelevantChunk, Source, SourceFulltext, SourceStatus, SourceType
from .codecs.documents import decode_document, tailwind_doc_markdown, tailwind_doc_plain_text
from .codecs.notebooks import decode_project, map_get_project_error, validate_project_identity
from .codecs.sources import decode_source, decode_sources, select_document_guide
from .drive_staging import _DRIVE_STAGED_UPLOAD_EXTENSIONS
from .epoch import bind_workflow_epoch, reset_workflow_epoch
from .phenotype import PhenotypeTokenProvider
from .play_books import (
    build_expert_intelligence_content,
    decode_play_book_item,
    static_metadata_augmentor,
    tentative_source_ids,
)
from .session import AndroidSession
from .source_search import AndroidSourceSearchService
from .source_transfers import (
    ADD_SOURCES_ASYNC_METHOD,
    APPEND_SOURCE_METHOD,
    COPY_SOURCES_ASYNC_METHOD,
    AndroidSourceTransferMixin,
)
from .upload import (
    AndroidUploadPipeline,
    android_provenance,
    android_request_context,
)

logger = logging.getLogger(__name__)


def _read_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _write_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _source_content_proto() -> Any:
    from .proto.notebooklm.internal.android.wire.v1 import source_content_pb2

    return cast(Any, source_content_pb2)


def _settings_proto() -> Any:
    from .proto.google.internal.labs.tailwind.v1 import source_settings_pb2

    return cast(Any, source_settings_pb2)


def _empty_type() -> Any:
    from google.protobuf.empty_pb2 import Empty

    return Empty


_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
ADD_TENTATIVE_SOURCES_METHOD = f"/{_SERVICE}/AddTentativeSources"
ADD_SOURCES_METHOD = f"/{_SERVICE}/AddSources"
LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD = f"/{_SERVICE}/ListExpertIntelligenceContent"
DELETE_SOURCES_METHOD = f"/{_SERVICE}/DeleteSources"
MUTATE_SOURCE_METHOD = f"/{_SERVICE}/MutateSource"
GENERATE_DOCUMENT_GUIDES_METHOD = f"/{_SERVICE}/GenerateDocumentGuides"
LOAD_SOURCE_METHOD = f"/{_SERVICE}/LoadSource"
CHECK_SOURCE_FRESHNESS_METHOD = f"/{_SERVICE}/CheckSourceFreshness"
REFRESH_SOURCE_METHOD = f"/{_SERVICE}/RefreshSource"

_FilterValue = TypeVar("_FilterValue")
_CORRELATION_PREFIX = "nblm-"
_CANONICAL_ID_LENGTH = 36
# Post-upload readiness polling sleeps between GetProject looks. The smallest wire budget
# a single look may be handed is capped by ``wait_timeout`` so a deadline that reads as spent
# on its first tick still gets a real request out.
_POLL_INTERVAL = 0.5
_POLL_WIRE_FLOOR = 1.0


class DriveDownload(Protocol):
    """Narrow authenticated download context used by ``add_drive_file``."""

    def __call__(
        self,
        document_id: str,
    ) -> AbstractAsyncContextManager[tuple[Path, str, str | None]]: ...


class AddFileCompat(Protocol):
    """Narrow Web upload capability for formats rejected by the mobile plane."""

    async def __call__(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None = None,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
    ) -> Source: ...


def _snapshot_enum_filter(
    values: Collection[_FilterValue] | None,
    *,
    enum_type: type[_FilterValue],
    parameter: str,
) -> frozenset[_FilterValue] | None:
    """Validate and snapshot one source filter before session entry."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Collection):
        raise TypeError(f"{parameter} must be a collection of {enum_type.__name__} values")
    snapshot = tuple(values)
    for value in snapshot:
        if not isinstance(value, enum_type):
            raise TypeError(f"{parameter} must contain only {enum_type.__name__} values")
    return frozenset(snapshot)


def _canonical_source_id(value: str) -> str | None:
    """Return a canonical UUID source id, rejecting aliases and loose strings."""
    if len(value) != _CANONICAL_ID_LENGTH:
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if value.lower() == canonical else None


def _correlation_name() -> str:
    """Allocate one bounded, opaque, per-occurrence registration key."""
    return f"{_CORRELATION_PREFIX}{uuid.uuid4().hex}"


def _known_registration_error(subject: str, *, kind: str = "URL") -> SourceAddError:
    return SourceAddError(
        subject,
        message=(
            f"Failed to register {kind} source {subject!r}: the backend omitted its registration."
        ),
    )


def _unresolved_add_error(
    subject: str,
    *,
    stage: str,
    cause: Exception | None = None,
    kind: str = "URL",
) -> SourceAddError:
    return cast(
        SourceAddError,
        unresolved_commit_error(
            ADD_SOURCES_METHOD,
            f"the Android {kind} add",
            SourceAddError(
                subject,
                cause=cause,
                message=(
                    "UNRESOLVED — check the notebook source list before retrying. "
                    f"The Android {kind} add could not prove {stage} for {subject!r}; neither write "
                    "was replayed and no cleanup delete was sent."
                ),
            ),
            preserve_exception=True,
        ),
    )


def _validate_drive_file_id(file_id: str) -> None:
    if not file_id or not file_id.strip():
        raise ValidationError("Drive file_id cannot be empty or whitespace-only")


def _unresolved_file_registration_error(filename: str) -> SourceAddError:
    error = SourceAddError(
        filename,
        message=(
            f"Android file upload tentative registration outcome is unconfirmed for {filename!r}."
        ),
    )
    cast(Any, error).stage = "register"
    return mark_unconfirmed(error)


@dataclass(frozen=True)
class _Registration:
    name: str
    source_id: str | None
    omitted: bool
    ambiguous: bool


def _correlate_registrations(names: Sequence[str], response: Any) -> list[_Registration]:
    """Correlate registration rows by a strict name-to-canonical-id bijection."""
    requested = Counter(names)
    rows_by_name: dict[str, list[str | None]] = defaultdict(list)
    try:
        rows = tuple(response.tentative_sources)
        for row in rows:
            echoed_name = row.title
            if echoed_name not in requested:
                continue
            raw_id = row.source_id.id if row.HasField("source_id") else ""
            rows_by_name[echoed_name].append(_canonical_source_id(raw_id))
    except Exception:
        raise DecodingError(
            "Could not decode Android tentative-source registration response",
            method_id=ADD_TENTATIVE_SOURCES_METHOD,
        ) from None

    provisional: list[_Registration] = []
    for name in names:
        candidates = rows_by_name[name]
        if not candidates:
            provisional.append(_Registration(name, None, omitted=True, ambiguous=False))
        elif len(candidates) != 1:
            provisional.append(_Registration(name, None, omitted=False, ambiguous=True))
        else:
            candidate = next(iter(candidates))
            provisional.append(
                _Registration(
                    name,
                    candidate,
                    omitted=False,
                    ambiguous=candidate is None,
                )
            )

    id_counts = Counter(
        item.source_id for item in provisional if item.source_id is not None and not item.ambiguous
    )
    return [
        replace(item, source_id=None, ambiguous=True)
        if item.source_id is not None and id_counts[item.source_id] != 1
        else item
        for item in provisional
    ]


class _ProofKind(Enum):
    PENDING = auto()
    COMPLETE = auto()
    ERROR = auto()


@dataclass(frozen=True)
class _CommitProof:
    kind: _ProofKind
    source: Source


def _proof_kind(raw_status: int) -> _ProofKind | None:
    if raw_status == _settings_proto().SOURCE_STATUS_PENDING:
        return _ProofKind.PENDING
    if raw_status == _settings_proto().SOURCE_STATUS_COMPLETE:
        return _ProofKind.COMPLETE
    if raw_status == _settings_proto().SOURCE_STATUS_ERROR:
        return _ProofKind.ERROR
    return None


def _collect_commit_proofs(
    rows: Sequence[Any],
    candidate_ids: Collection[str],
    *,
    method_id: str,
) -> tuple[dict[str, _CommitProof], set[str]]:
    """Extract exact-ID affirmative status evidence without public coercion."""
    candidates = frozenset(candidate_ids)
    proofs: dict[str, _CommitProof] = {}
    unresolved: set[str] = set()
    for row in rows:
        try:
            raw_id = row.source_id.id if row.HasField("source_id") else ""
            source_id = _canonical_source_id(raw_id)
        except Exception:
            # A structurally unreadable row cannot be assigned to one exact id,
            # so it proves nothing. Other individually isolated rows remain usable.
            continue
        if source_id is None or source_id not in candidates:
            continue
        raw_status = row.settings.status if row.HasField("settings") else 0
        kind = _proof_kind(raw_status)
        if kind is None:
            continue
        try:
            source = decode_source(row, method_id=method_id)
        except DecodingError:
            unresolved.add(source_id)
            proofs.pop(source_id, None)
            continue
        current = proofs.get(source_id)
        if current is not None and (current.kind is not kind or current.source != source):
            unresolved.add(source_id)
            proofs.pop(source_id, None)
            continue
        if source_id not in unresolved:
            proofs[source_id] = _CommitProof(kind, source)
    return proofs, unresolved


def _merge_commit_proof(
    first: _CommitProof | None,
    later: _CommitProof | None,
) -> _CommitProof | None:
    if first is None:
        return later
    if later is None:
        return first
    if first.kind is _ProofKind.PENDING or first.kind is later.kind:
        return later
    return None


class AndroidSourcesAPI(AndroidSourceTransferMixin, SourcesAPI):
    """Android source adapter installed by public Android backend selection."""

    @asynccontextmanager
    async def _operation_scope(self, label: str) -> AsyncIterator[OperationLease]:
        async with self._transport.operation_scope(label) as lease:
            token = bind_workflow_epoch(self._transport, lease.epoch)
            try:
                yield lease
            finally:
                reset_workflow_epoch(token)

    def __init__(
        self,
        session: AndroidSession,
        upload_pipeline: AndroidUploadPipeline,
        *,
        drive_download: DriveDownload | None = None,
        add_file_compat: AddFileCompat | None = None,
        phenotype: PhenotypeTokenProvider | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the fully native source surface.

        ``add_file_compat`` optionally overrides Drive staging for direct adapter
        callers. Public client assembly supplies nothing: the adapter holds no
        Web collaborator and uses the native staging round-trip.

        ``monotonic`` makes the post-upload readiness deadline testable with a
        stepping clock instead of racing ``time.monotonic()``.
        """
        self._transport = session
        self._searcher = AndroidSourceSearchService(session)
        self._upload_pipeline = upload_pipeline
        self._add_file_compat = add_file_compat
        self._phenotype = phenotype or PhenotypeTokenProvider()
        self._monotonic = monotonic
        self._poller = SourcePoller()
        native_drive_download = getattr(upload_pipeline, "drive_download_scope", None)
        self._drive_download = drive_download or (
            native_drive_download if callable(native_drive_download) else None
        )
        super().__init__()

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> builtins.list[Source]:
        status_filter = _snapshot_enum_filter(
            statuses, enum_type=SourceStatus, parameter="statuses"
        )
        type_filter = _snapshot_enum_filter(types, enum_type=SourceType, parameter="types")
        return await self._list_project_sources(
            notebook_id,
            strict=strict,
            status_filter=status_filter,
            type_filter=type_filter,
        )

    async def search(
        self,
        notebook_id: str,
        query: str,
        *,
        source_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> builtins.list[RelevantChunk]:
        """Search indexed source passages through native Android gRPC."""
        query, normalized_ids, limit = validate_search(query, source_ids, limit)
        return await self._searcher.search(
            notebook_id,
            query,
            source_ids=normalized_ids,
            limit=limit,
        )

    async def _list_project_sources(
        self,
        notebook_id: str,
        *,
        strict: bool,
        status_filter: frozenset[SourceStatus] | None,
        type_filter: frozenset[SourceType] | None,
        expected_epoch: int | None = None,
    ) -> builtins.list[Source]:
        request = _read_proto().GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        epoch_kwargs: dict[str, Any] = (
            {} if expected_epoch is None else {"expected_epoch": expected_epoch}
        )
        try:
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=_read_proto().GetProjectResponse,
                **epoch_kwargs,
            )
        except RPCError as exc:
            mapped = map_get_project_error(notebook_id, exc, method_id=GET_PROJECT_METHOD)
            if mapped is exc:
                raise
            raise mapped from exc
        validate_project_identity(response.project, notebook_id, method_id=GET_PROJECT_METHOD)
        decode_project(response.project, method_id=GET_PROJECT_METHOD)
        sources = decode_sources(
            response.project.sources,
            method_id=GET_PROJECT_METHOD,
            strict=strict,
            logger=logger,
        )
        return [
            source
            for source in sources
            if (status_filter is None or source.status in status_filter)
            and (type_filter is None or source.kind in type_filter)
        ]

    async def _register_tentative_sources(
        self,
        notebook_id: str,
        names: Sequence[str],
        *,
        expected_epoch: int,
    ) -> builtins.list[_Registration]:
        request = _write_proto().AddTentativeSourcesRequest(
            tentative_sources_metadata=[
                _write_proto().TentativeSourceMetadata(name=name) for name in names
            ],
            project_id=notebook_id,
        )
        response = await self._transport.unary(
            ADD_TENTATIVE_SOURCES_METHOD,
            request,
            replay_safe=False,
            response_type=_write_proto().AddTentativeSourcesResponse,
            expected_epoch=expected_epoch,
        )
        return _correlate_registrations(names, response)

    async def _register_file_tentative(
        self,
        notebook_id: str,
        filename: str,
        expected_epoch: int,
        timeout: float,
    ) -> str:
        request = _write_proto().AddTentativeSourcesRequest(
            tentative_sources_metadata=[_write_proto().TentativeSourceMetadata(name=filename)],
            project_id=notebook_id,
            request_context=android_request_context(),
            provenance=android_provenance(),
        )
        try:
            response = await self._transport.unary(
                ADD_TENTATIVE_SOURCES_METHOD,
                request,
                replay_safe=False,
                response_type=_write_proto().AddTentativeSourcesResponse,
                expected_epoch=expected_epoch,
                timeout=timeout,
            )
            (registration,) = _correlate_registrations([filename], response)
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except (NetworkError, RateLimitError, ServerError, DecodingError):
            failure = _unresolved_file_registration_error(filename)
            raise failure from None
        if registration.omitted:
            raise SourceAddError(
                filename,
                message=(
                    f"Failed to register file source {filename!r}: "
                    "the backend omitted its registration."
                ),
            )
        if registration.ambiguous or registration.source_id is None:
            raise _unresolved_file_registration_error(filename)
        return registration.source_id

    async def _wait_uploaded_source(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float,
        expected_epoch: int,
        *,
        ready: bool,
    ) -> Source:
        deadline = RuntimeDeadline.start(timeout, monotonic=self._monotonic)

        async def get_source(project_id: str, expected_source_id: str) -> Source | None:
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                _read_proto().GetProjectRequest(
                    project_id=project_id,
                    include_audio_overview_ids=True,
                ),
                replay_safe=True,
                response_type=_read_proto().GetProjectResponse,
                expected_epoch=expected_epoch,
                timeout=max(deadline.remaining(), min(deadline.timeout, _POLL_WIRE_FLOOR)),
            )
            validate_project_identity(response.project, project_id, method_id=GET_PROJECT_METHOD)
            decode_project(response.project, method_id=GET_PROJECT_METHOD)
            matches = []
            for row in response.project.sources:
                try:
                    raw_id = row.source_id.id if row.HasField("source_id") else ""
                except Exception:
                    continue
                if raw_id == expected_source_id:
                    matches.append(row)
            if len(matches) > 1:
                raise DecodingError(
                    "Android upload polling returned duplicate source ids",
                    method_id=GET_PROJECT_METHOD,
                )
            return (
                None
                if not matches
                else decode_source(next(iter(matches)), method_id=GET_PROJECT_METHOD)
            )

        common: dict[str, Any] = {
            "timeout": timeout,
            "initial_interval": _POLL_INTERVAL,
            "max_interval": _POLL_INTERVAL,
            "backoff_factor": 1.0,
            "transient_error_types": (),
            "look_first": True,
            "deadline": deadline,
            "get_source": get_source,
            "sleep": asyncio.sleep,
            "monotonic": self._monotonic,
            "logger": logger,
        }
        if ready:
            return await self._poller.wait_until_ready(
                notebook_id,
                source_id,
                missing_is_pending=True,
                **common,
            )
        return await self._poller.wait_until_registered(
            notebook_id,
            source_id,
            accept=lambda source: source.status in {SourceStatus.PROCESSING, SourceStatus.READY},
            **common,
        )

    async def _wait_uploaded_registered(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float,
        expected_epoch: int,
    ) -> Source:
        return await self._wait_uploaded_source(
            notebook_id,
            source_id,
            timeout,
            expected_epoch,
            ready=False,
        )

    async def _wait_uploaded_ready(
        self,
        notebook_id: str,
        source_id: str,
        timeout: float,
        expected_epoch: int,
    ) -> Source:
        return await self._wait_uploaded_source(
            notebook_id,
            source_id,
            timeout,
            expected_epoch,
            ready=True,
        )

    async def _rename_uploaded(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        expected_epoch: int,
    ) -> str | None:
        del notebook_id
        response = await self._transport.unary(
            MUTATE_SOURCE_METHOD,
            _write_proto().MutateSourceRequest(
                source_id=_read_proto().SourceId(id=source_id),
                mutations=[
                    _write_proto().SourceMutation(
                        change_title=_write_proto().ChangeTitle(title=new_title)
                    )
                ],
                request_context=android_request_context(),
            ),
            replay_safe=False,
            response_type=_write_proto().MutateSourceResponse,
            expected_epoch=expected_epoch,
        )
        source = response.source if response.HasField("source") else None
        echoed_id = (
            source.source_id.id if source is not None and source.HasField("source_id") else ""
        )
        if echoed_id and echoed_id != source_id:
            raise DecodingError(
                "Android source mutation returned an unexpected source id",
                method_id=MUTATE_SOURCE_METHOD,
            )
        return (source.title or None) if source is not None else None

    async def _read_commit_proofs(
        self,
        notebook_id: str,
        source_ids: Collection[str],
        *,
        expected_epoch: int,
    ) -> tuple[dict[str, _CommitProof], set[str], set[str]]:
        request = _read_proto().GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        try:
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=_read_proto().GetProjectResponse,
                expected_epoch=expected_epoch,
            )
            validate_project_identity(response.project, notebook_id, method_id=GET_PROJECT_METHOD)
            decode_project(response.project, method_id=GET_PROJECT_METHOD)
            proofs, unresolved = _collect_commit_proofs(
                response.project.sources,
                source_ids,
                method_id=GET_PROJECT_METHOD,
            )
            tentative = tentative_source_ids(response.project.sources, source_ids)
            return proofs, unresolved, tentative
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except (RPCError, NetworkError):
            logger.warning(
                "Android source commit reconciliation failed; preserving only prior "
                "affirmative response evidence"
            )
            return {}, set(), set()

    async def _commit_user_contents(
        self,
        notebook_id: str,
        entries: Sequence[tuple[Any, str]],
        *,
        expected_epoch: int,
        metadata_augmentor: Any = None,
        metadata_refresher: Any = None,
    ) -> tuple[dict[str, _CommitProof], set[str]]:
        candidate_ids = [source_id for _, source_id in entries]
        request = _write_proto().AddSourcesRequest(
            user_content=[content for content, _ in entries],
            project_id=notebook_id,
            request_context=android_request_context(),
        )
        response_proofs: dict[str, _CommitProof] = {}
        response_unresolved: set[str] = set()
        commit_failure: ServerError | None = None
        try:
            response = await self._transport.unary(
                ADD_SOURCES_METHOD,
                request,
                replay_safe=False,
                response_type=_write_proto().AddSourcesResponse,
                expected_epoch=expected_epoch,
                metadata_augmentor=metadata_augmentor,
            )
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except (NetworkError, RateLimitError, ServerError) as exc:
            if isinstance(exc, ServerError):
                commit_failure = exc
        else:
            try:
                response_proofs, response_unresolved = _collect_commit_proofs(
                    response.sources,
                    candidate_ids,
                    method_id=ADD_SOURCES_METHOD,
                )
            except Exception:
                # The wire call completed, so a malformed envelope is an
                # uncertain commit, not an adapter exception leak. The one safe
                # exact-ID read below still gets its chance to prove acceptance.
                response_proofs = {}
                response_unresolved = set()

        read_proofs, read_unresolved, read_tentative = await self._read_commit_proofs(
            notebook_id,
            candidate_ids,
            expected_epoch=expected_epoch,
        )
        unresolved = response_unresolved | read_unresolved
        merged: dict[str, _CommitProof] = {}
        for source_id in candidate_ids:
            if source_id in unresolved:
                continue
            first = response_proofs.get(source_id)
            later = read_proofs.get(source_id)
            proof = _merge_commit_proof(first, later)
            if proof is not None:
                merged[source_id] = proof
            elif first is not None and later is not None:
                unresolved.add(source_id)

        if (
            metadata_refresher is not None
            and commit_failure is not None
            and commit_failure.rpc_code == 13
            and not merged
            and not unresolved
            and set(candidate_ids).issubset(read_tentative)
        ):
            refreshed = await self._transport.prepare_metadata(
                metadata_refresher,
                expected_epoch=expected_epoch,
            )
            return await self._commit_user_contents(
                notebook_id,
                entries,
                expected_epoch=expected_epoch,
                metadata_augmentor=static_metadata_augmentor(refreshed),
            )
        return merged, unresolved

    async def _commit_urls(
        self,
        notebook_id: str,
        entries: Sequence[tuple[str, str]],
        *,
        expected_epoch: int,
    ) -> tuple[dict[str, _CommitProof], set[str]]:
        user_contents = []
        for url, source_id in entries:
            content_kwargs = (
                {"video_content": _write_proto().VideoContent(youtube_url=url)}
                if is_youtube_url(url)
                else {"web_content": _write_proto().WebContent(url=url)}
            )
            user_contents.append(
                (
                    _write_proto().UserContent(
                        **content_kwargs,
                        tentative_source_id=_read_proto().SourceId(id=source_id),
                    ),
                    source_id,
                )
            )
        return await self._commit_user_contents(
            notebook_id,
            user_contents,
            expected_epoch=expected_epoch,
        )

    async def _add_registered_content(
        self,
        notebook_id: str,
        *,
        subject: str,
        kind: str,
        operation_label: str,
        build_content: Callable[[str], Any],
        wait: bool,
        wait_timeout: float,
        metadata_augmentor: Any = None,
        metadata_refresher: Any = None,
    ) -> Source:
        correlation = _correlation_name()
        async with self._transport.operation_scope(operation_label) as lease:
            if metadata_augmentor is not None:
                prepared = await self._transport.prepare_metadata(
                    metadata_augmentor,
                    expected_epoch=lease.epoch,
                )
                metadata_augmentor = static_metadata_augmentor(prepared)
            try:
                (registration,) = await self._register_tentative_sources(
                    notebook_id,
                    [correlation],
                    expected_epoch=lease.epoch,
                )
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except AuthError:
                raise
            except (RateLimitError, ServerError, NetworkError, DecodingError) as exc:
                raise _unresolved_add_error(
                    subject,
                    stage="tentative registration",
                    cause=exc,
                    kind=kind,
                ) from None

            if registration.omitted:
                raise _known_registration_error(subject, kind=kind)
            if registration.ambiguous or registration.source_id is None:
                raise _unresolved_add_error(
                    subject,
                    stage="tentative registration correlation",
                    kind=kind,
                )

            source_id = registration.source_id
            proofs, _ = await self._commit_user_contents(
                notebook_id,
                [(build_content(source_id), source_id)],
                expected_epoch=lease.epoch,
                metadata_augmentor=metadata_augmentor,
                metadata_refresher=metadata_refresher,
            )
            proof = proofs.get(source_id)
            if proof is None:
                raise _unresolved_add_error(
                    subject,
                    stage="source commit acceptance",
                    kind=kind,
                )
            source = proof.source
            if wait:
                source = await self.wait_until_ready(
                    notebook_id,
                    source.id,
                    timeout=wait_timeout,
                )
            return source

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
    ) -> Source:
        requested_title = title.strip() if title is not None else None
        if not requested_title:
            requested_title = None
        correlation = _correlation_name()

        async with self._transport.operation_scope("source.add_url") as lease:
            try:
                (registration,) = await self._register_tentative_sources(
                    notebook_id,
                    [correlation],
                    expected_epoch=lease.epoch,
                )
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except (RateLimitError, ServerError, NetworkError, DecodingError) as exc:
                failure = _unresolved_add_error(
                    url,
                    stage="tentative registration",
                    cause=exc,
                )
                raise failure from None

            if registration.omitted:
                raise _known_registration_error(url)
            if registration.ambiguous or registration.source_id is None:
                raise _unresolved_add_error(url, stage="tentative registration correlation")

            proofs, _ = await self._commit_urls(
                notebook_id,
                [(url, registration.source_id)],
                expected_epoch=lease.epoch,
            )
            proof = proofs.get(registration.source_id)
            if proof is None:
                raise _unresolved_add_error(url, stage="source commit acceptance")
            source = proof.source
            if wait:
                source = await self.wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            if requested_title is not None and source.title != requested_title:
                source = await self._best_effort_title(notebook_id, source, requested_title)
            return source

    async def _add_urls_batch(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[SourceUrlBatchItem]:
        snapshot = tuple(urls)
        if not snapshot:
            return []
        correlations = [_correlation_name() for _ in snapshot]

        async with self._transport.operation_scope("source.add_urls_batch") as lease:
            try:
                registrations = await self._register_tentative_sources(
                    notebook_id,
                    correlations,
                    expected_epoch=lease.epoch,
                )
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except (RateLimitError, ServerError, NetworkError, DecodingError) as exc:
                return [
                    SourceUrlBatchItem(
                        url=url,
                        error=_unresolved_add_error(
                            url,
                            stage="tentative registration",
                            cause=exc,
                        ),
                    )
                    for url in snapshot
                ]

            entries = [
                (url, registration.source_id)
                for url, registration in zip(snapshot, registrations, strict=True)
                if registration.source_id is not None and not registration.ambiguous
            ]
            proofs: dict[str, _CommitProof] = {}
            if entries:
                proofs, _ = await self._commit_urls(
                    notebook_id,
                    cast(Sequence[tuple[str, str]], entries),
                    expected_epoch=lease.epoch,
                )

            outcomes: builtins.list[SourceUrlBatchItem] = []
            for url, registration in zip(snapshot, registrations, strict=True):
                if registration.omitted:
                    outcomes.append(
                        SourceUrlBatchItem(url=url, error=_known_registration_error(url))
                    )
                    continue
                source_id = registration.source_id
                proof = proofs.get(source_id or "")
                if registration.ambiguous or source_id is None or proof is None:
                    stage = (
                        "tentative registration correlation"
                        if registration.ambiguous or source_id is None
                        else "source commit acceptance"
                    )
                    outcomes.append(
                        SourceUrlBatchItem(
                            url=url,
                            error=_unresolved_add_error(url, stage=stage),
                        )
                    )
                else:
                    outcomes.append(SourceUrlBatchItem(url=url, source=proof.source))
            return outcomes

    async def _best_effort_title(
        self,
        notebook_id: str,
        source: Source,
        requested_title: str,
    ) -> Source:
        try:
            renamed = await self.rename(notebook_id, source.id, requested_title)
        except (RPCError, NetworkError):
            logger.warning(
                "Source %s added but Android title finalization failed; keeping upstream title",
                source.id,
            )
            return source
        return replace(source, title=(renamed.title if renamed else None) or requested_title)

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        idempotent: bool = False,
    ) -> Source:
        _validate_add_text_idempotency(idempotent)
        return await self._add_registered_content(
            notebook_id,
            subject=title,
            kind="text",
            operation_label="source.add_text",
            build_content=lambda source_id: _write_proto().UserContent(
                text_content=_write_proto().TextContent(
                    source_name=title,
                    content=content,
                ),
                text_content_type=_write_proto().UserContent.CONTENT_TYPE_TEXT,
                tentative_source_id=_read_proto().SourceId(id=source_id),
            ),
            wait=wait,
            wait_timeout=wait_timeout,
        )

    async def _send_upload(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: Callable[[int, int], object] | None,
    ) -> Source:
        adapter = self
        pipeline = adapter._upload_pipeline
        compat: AddFileCompat | None = None
        result: Source | None = None
        failure: BaseException | None = None
        try:
            canonical_path = await asyncio.to_thread(Path(file_path).resolve)
            if canonical_path.suffix.lower() in _DRIVE_STAGED_UPLOAD_EXTENSIONS:
                compat = adapter._add_file_compat
                if compat is not None:
                    result = await compat(
                        notebook_id,
                        canonical_path,
                        mime_type,
                        wait=wait,
                        wait_timeout=wait_timeout,
                        title=title,
                        on_progress=on_progress,
                    )
                else:
                    result = await pipeline.add_file_via_drive_staging(
                        notebook_id,
                        canonical_path,
                        mime_type,
                        wait_timeout=wait_timeout,
                        title=title,
                        import_drive_file=adapter.add_drive,
                    )
            else:
                result = await pipeline.upload_file(
                    notebook_id,
                    canonical_path,
                    mime_type,
                    wait=wait,
                    wait_timeout=wait_timeout,
                    title=title,
                    on_progress=on_progress,
                    register_tentative=adapter._register_file_tentative,
                    wait_until_registered=adapter._wait_uploaded_registered,
                    wait_until_ready=adapter._wait_uploaded_ready,
                    rename_uploaded=adapter._rename_uploaded,
                    finalize_uploaded=SourcesAPI._finalize_uploaded_file,
                )
        except BaseException as error:
            from .errors import sanitize_escaping_exception

            failure = sanitize_escaping_exception(error)
        finally:
            del self, adapter, pipeline, compat, file_path, title, on_progress
        if failure is not None:
            raise failure from None
        return cast(Source, result)

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        mime_type: str = "application/vnd.google-apps.document",
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        _validate_drive_file_id(file_id)
        requested_title = title.strip() or None
        source = await self._add_registered_content(
            notebook_id,
            subject=file_id,
            kind="Drive",
            operation_label="source.add_drive",
            build_content=lambda source_id: _write_proto().UserContent(
                google_drive_content=_write_proto().GoogleDriveContent(
                    document_id=file_id,
                    mime_type=mime_type,
                    can_download=True,
                    source_name=title,
                ),
                tentative_source_id=_read_proto().SourceId(id=source_id),
            ),
            wait=wait,
            wait_timeout=wait_timeout,
        )
        if requested_title is not None and source.title != requested_title:
            source = await self._best_effort_title(notebook_id, source, requested_title)
        return source

    async def list_play_books(self) -> builtins.list[PlayBook]:
        """List the account's Google Play Books library (#2292, #2302).

        Calls the Android ``ListExpertIntelligenceContent`` RPC — the same
        library the web backend serves — and returns **every** title, addable or
        not. Inspect :attr:`~notebooklm.types.PlayBook.export_disabled` before
        passing a ``content_id`` to :meth:`add_play_book`.
        """
        request = _write_proto().ListExpertIntelligenceContentRequest(
            request_context=android_request_context(),
            source_class=1,
        )
        response = await self._transport.unary(
            LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD,
            request,
            replay_safe=True,
            response_type=_write_proto().ListExpertIntelligenceContentResponse,
        )
        return [
            decode_play_book_item(item, method_id=LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD)
            for item in response.items
        ]

    async def add_play_book(
        self,
        notebook_id: str,
        content_id: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        """Add a Google Play Book as a source over the Android backend (#2302).

        Looks ``content_id`` up in :meth:`list_play_books` (which supplies the
        title, description, cover, authors, and opaque ``field_type`` echoed back
        in the add), refuses a non-exportable title with
        :class:`~notebooklm.exceptions.PlayBookNotExportableError`, then commits
        it via ``AddSources``. The commit carries the GMS Phenotype experiment
        header (``x-goog-ext-202964622-bin``) minted headlessly by
        :class:`~notebooklm._android.phenotype.PhenotypeTokenProvider`; without
        it the server refuses the Expert-Intelligence content with ``INTERNAL``.
        The created source ingests as :attr:`~notebooklm.types.SourceType.EXPERT_INTELLIGENCE`.

        Args:
            notebook_id: The notebook ID.
            content_id: Play Books volume id (from a :class:`PlayBook`).
            wait: If True, wait for the source to be READY before returning.
            wait_timeout: Maximum seconds to wait if ``wait=True`` (default 120).

        Raises:
            SourceNotFoundError: ``content_id`` is not in the library.
            PlayBookNotExportableError: the title cannot be exported.
        """
        books = await self.list_play_books()
        book = next((b for b in books if b.content_id == content_id), None)
        if book is None:
            raise SourceNotFoundError(
                content_id,
                method_id=LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD,
            )
        if book.export_disabled:
            raise PlayBookNotExportableError(book.content_id, book.reason)

        async def _augment(bearer: str) -> tuple[tuple[str, bytes], ...]:
            return await self._phenotype.experiment_metadata(bearer)

        async def _refresh(bearer: str) -> tuple[tuple[str, bytes], ...]:
            return await self._phenotype.experiment_metadata(bearer, force=True)

        return await self._add_registered_content(
            notebook_id,
            subject=book.title or content_id,
            kind="Play Books",
            operation_label="source.add_play_book",
            build_content=lambda source_id: _write_proto().UserContent(
                expert_intelligence_content=build_expert_intelligence_content(book),
                tentative_source_id=_read_proto().SourceId(id=source_id),
            ),
            wait=wait,
            wait_timeout=wait_timeout,
            metadata_augmentor=_augment,
            metadata_refresher=_refresh,
        )

    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        drive_download = self._drive_download
        if drive_download is None:
            raise ConfigurationError(
                "Android Drive-file import requires the native download pipeline."
            )
        async with drive_download(document_id) as (path, filename, content_type):
            return await self.add_file(
                notebook_id,
                path,
                mime_type=content_type,
                title=title if title else (filename or None),
                wait=wait,
                wait_timeout=wait_timeout,
            )

    async def _require_owned_source(
        self,
        notebook_id: str,
        source_id: str,
        *,
        expected_epoch: int,
        method_id: str,
    ) -> Source:
        source = next(
            (
                item
                for item in await self._list_project_sources(
                    notebook_id,
                    strict=False,
                    status_filter=None,
                    type_filter=None,
                    expected_epoch=expected_epoch,
                )
                if item.id == source_id
            ),
            None,
        )
        if source is None:
            raise SourceNotFoundError(source_id, method_id=method_id)
        return source

    async def delete(self, notebook_id: str, source_id: str) -> None:
        request = _write_proto().DeleteSourcesRequest(
            source_ids=[_read_proto().SourceId(id=source_id)]
        )
        async with self._transport.operation_scope("sources.delete") as lease:
            try:
                await self._require_owned_source(
                    notebook_id,
                    source_id,
                    expected_epoch=lease.epoch,
                    method_id=DELETE_SOURCES_METHOD,
                )
            except SourceNotFoundError:
                # Delete is idempotent. Absence from this notebook is already
                # the requested final state and must also prevent a global-id
                # delete from reaching a resource owned by another notebook.
                return
            try:
                await self._transport.unary(
                    DELETE_SOURCES_METHOD,
                    request,
                    replay_safe=False,
                    response_type=_empty_type(),
                    expected_epoch=lease.epoch,
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise

    async def rename(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Source | None:
        request = _write_proto().MutateSourceRequest(
            source_id=_read_proto().SourceId(id=source_id),
            mutations=[
                _write_proto().SourceMutation(
                    change_title=_write_proto().ChangeTitle(title=new_title)
                )
            ],
            request_context=android_request_context(),
        )
        async with self._transport.operation_scope("source.rename") as lease:
            await self._require_owned_source(
                notebook_id,
                source_id,
                expected_epoch=lease.epoch,
                method_id=MUTATE_SOURCE_METHOD,
            )
            try:
                response = await self._transport.unary(
                    MUTATE_SOURCE_METHOD,
                    request,
                    replay_safe=False,
                    response_type=_write_proto().MutateSourceResponse,
                    expected_epoch=lease.epoch,
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
                raise SourceNotFoundError(source_id, method_id=MUTATE_SOURCE_METHOD) from None

            source = response.source if response.HasField("source") else None
            echoed_id = (
                source.source_id.id if source is not None and source.HasField("source_id") else ""
            )
            if echoed_id:
                if echoed_id != source_id:
                    raise DecodingError(
                        "Android source mutation returned an unexpected source id",
                        method_id=MUTATE_SOURCE_METHOD,
                    )
                if not return_object:
                    return None
                return decode_source(source, method_id=MUTATE_SOURCE_METHOD)
            source = next(
                (
                    item
                    for item in await self._list_project_sources(
                        notebook_id,
                        strict=False,
                        status_filter=None,
                        type_filter=None,
                        expected_epoch=lease.epoch,
                    )
                    if item.id == source_id
                ),
                None,
            )
            if source is None:
                raise SourceNotFoundError(source_id, method_id=MUTATE_SOURCE_METHOD)
            return source if return_object else None

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        async with self._transport.operation_scope("sources.refresh") as lease:
            await self._require_owned_source(
                notebook_id,
                source_id,
                expected_epoch=lease.epoch,
                method_id=REFRESH_SOURCE_METHOD,
            )
            # Live evidence bounds the native mutation to content that is
            # actually stale: a stale native Drive source succeeds, while the
            # freshly-added URL exercised by the shared E2E is rejected.  An
            # already-fresh source needs no mutation, so preserve refresh's
            # public ``None`` success contract without sending a no-effect call
            # the native handler rejects. Keep both calls in one operation epoch
            # so closing/reopening the client cannot split the decision from the
            # write.
            if await self._check_freshness(source_id, expected_epoch=lease.epoch):
                return
            response = await self._transport.unary(
                REFRESH_SOURCE_METHOD,
                _write_proto().RefreshSourceRequest(
                    source_id=_read_proto().SourceId(id=source_id),
                    request_context=android_request_context(),
                ),
                replay_safe=False,
                response_type=_write_proto().RefreshSourceResponse,
                expected_epoch=lease.epoch,
            )
            echoed_id = (
                response.source.source_id.id
                if response.HasField("source") and response.source.HasField("source_id")
                else ""
            )
            if echoed_id and echoed_id != source_id:
                raise DecodingError(
                    "Android source refresh returned an unexpected source id",
                    method_id=REFRESH_SOURCE_METHOD,
                )

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        """Report whether a source is fresh, without policing its existence.

        The other ADR-0019 derived read on this adapter (see ``get_guide``).
        No pre-flight is needed to stay web-compatible: a live probe of a
        nonexistent id returned an *empty* ``CheckSourceFreshness`` response
        rather than an error, which ``_check_freshness`` already reads as
        fresh -- the same ``True`` the web backend returns for that input
        (issue #2278). ``refresh`` keeps its ownership check, because mutating
        a missing source must still raise.
        """
        async with self._transport.operation_scope("sources.check_freshness") as lease:
            return await self._check_freshness(source_id, expected_epoch=lease.epoch)

    async def _check_freshness(
        self,
        source_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        request = _write_proto().CheckSourceFreshnessRequest(
            source_id=_read_proto().SourceId(id=source_id),
            request_context=android_request_context(),
        )
        if expected_epoch is None:
            response = await self._transport.unary(
                CHECK_SOURCE_FRESHNESS_METHOD,
                request,
                replay_safe=True,
                response_type=_write_proto().CheckSourceFreshnessResponse,
            )
        else:
            response = await self._transport.unary(
                CHECK_SOURCE_FRESHNESS_METHOD,
                request,
                replay_safe=True,
                response_type=_write_proto().CheckSourceFreshnessResponse,
                expected_epoch=expected_epoch,
            )
        if not response.HasField("source_freshness"):
            return True
        freshness = response.source_freshness
        echoed_id = freshness.source_id.id if freshness.HasField("source_id") else ""
        if echoed_id and echoed_id != source_id:
            raise DecodingError(
                "Android source freshness returned an unexpected source id",
                method_id=CHECK_SOURCE_FRESHNESS_METHOD,
            )
        if not freshness.HasField("is_fresh"):
            return True
        return bool(freshness.is_fresh)

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        """Return the AI summary and keywords for a source, or an empty guide.

        A derived read under ADR-0019: it does not police parent existence.
        A source that is absent, or present but not yet summarised, yields
        ``SourceGuide("", ())`` rather than an error -- identical to the web
        backend, which was live-verified returning exactly that for a
        nonexistent source id (issue #2278). Existence is ``get()``'s job, and
        every surface that needs a 404 already asks for one: the MCP tool and
        the REST route both run ``execute_source_get`` first, and the CLI
        resolves the id against ``sources.list()``.

        Shape drift still raises ``DecodingError`` via ``select_document_guide``.
        """
        request = _write_proto().GenerateDocumentGuidesRequest(
            sources=[_write_proto().InputSource(source_id=_read_proto().SourceId(id=source_id))]
        )
        async with self._transport.operation_scope("sources.get_guide") as lease:
            try:
                response = await self._transport.unary(
                    GENERATE_DOCUMENT_GUIDES_METHOD,
                    request,
                    replay_safe=True,
                    response_type=_write_proto().GenerateDocumentGuidesResponse,
                    expected_epoch=lease.epoch,
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
                # NOT_FOUND is how the backend reports "no guide for this id",
                # whether the source is gone or was never summarised; a live
                # probe confirmed a nonexistent id lands here. Web returns an
                # empty guide for the same input.
                return SourceGuide(summary="", keywords=())

        if not response.guides:
            return SourceGuide(summary="", keywords=())
        guide = select_document_guide(
            response,
            source_id=source_id,
            method_id=GENERATE_DOCUMENT_GUIDES_METHOD,
        )
        summary = guide.snippet.text_snippet if guide.HasField("snippet") else ""
        keywords = tuple(guide.main_ideas.text_ideas) if guide.HasField("main_ideas") else ()
        return SourceGuide(summary=summary, keywords=keywords)

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: Literal["text", "markdown"] = "text",
    ) -> SourceFulltext:
        if output_format not in ("text", "markdown"):
            raise ValueError(f"Invalid format: {output_format!r}. Must be 'text' or 'markdown'.")
        if output_format == "markdown":
            try:
                import markdownify  # noqa: F401
            except ImportError:
                raise ImportError(
                    "The 'markdown' format requires the 'markdownify' package. "
                    "Install it with: pip install 'notebooklm-py[markdown]'"
                ) from None

        request = _write_proto().LoadSourceRequest(source_id=_read_proto().SourceId(id=source_id))
        async with self._transport.operation_scope("sources.get_fulltext") as lease:
            await self._require_owned_source(
                notebook_id,
                source_id,
                expected_epoch=lease.epoch,
                method_id=LOAD_SOURCE_METHOD,
            )
            try:
                response = await self._transport.unary(
                    LOAD_SOURCE_METHOD,
                    request,
                    replay_safe=True,
                    response_type=_source_content_proto().WireLoadSourceResponse,
                    expected_epoch=lease.epoch,
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
                raise SourceNotFoundError(source_id, method_id=LOAD_SOURCE_METHOD) from None

        if not response.HasField("source"):
            raise SourceNotFoundError(source_id, method_id=LOAD_SOURCE_METHOD)
        raw_id = response.source.source_id.id if response.source.HasField("source_id") else ""
        # An absent echo is not an absent source: ``LoadSource`` was observed
        # echoing every probed source type (issue #2276), but turning a
        # hypothetically unlabelled response into ``SourceNotFoundError`` would
        # misreport a source the server did return. Only a populated and
        # different id is a decoding failure, as in ``get_guide`` and
        # ``refresh``.
        if raw_id and raw_id != source_id:
            raise DecodingError(
                "Android full-text response did not match the requested source id "
                f"(requested={source_id}, observed={raw_id})",
                method_id=LOAD_SOURCE_METHOD,
                found_ids=[raw_id],
            )
        source = decode_source(response.source, method_id=LOAD_SOURCE_METHOD)
        document = (
            decode_document(response.tailwind_doc)
            if response.HasField("tailwind_doc")
            else StructuredDocument()
        )
        if output_format == "markdown":
            content = response.markdown_string or (
                tailwind_doc_markdown(response.tailwind_doc)
                if response.HasField("tailwind_doc")
                else ""
            )
        elif response.HasField("plain_text"):
            content = response.plain_text.body
        else:
            # Current Android LoadSource responses carry the indexed source in
            # TailwindDoc #4 and omit the older flat #2/#3 renditions. The
            # shared renderer covers every admitted text-bearing structural
            # variant; StructuredDocument separately retains the backend's
            # UTF-16 coordinate space for paragraphs and tables.
            content = (
                tailwind_doc_plain_text(response.tailwind_doc)
                if response.HasField("tailwind_doc")
                else ""
            )
        if not content:
            logger.warning(
                "Android source %s returned empty %s content",
                source_id,
                output_format,
            )
        return SourceFulltext(
            source_id=source_id,
            title=source.title or "",
            content=content,
            _type_code=source._type_code,
            url=source.url,
            char_count=len(content),
            document=document,
        )


__all__ = [
    "ADD_SOURCES_ASYNC_METHOD",
    "ADD_SOURCES_METHOD",
    "APPEND_SOURCE_METHOD",
    "COPY_SOURCES_ASYNC_METHOD",
    "ADD_TENTATIVE_SOURCES_METHOD",
    "CHECK_SOURCE_FRESHNESS_METHOD",
    "DELETE_SOURCES_METHOD",
    "GENERATE_DOCUMENT_GUIDES_METHOD",
    "GET_PROJECT_METHOD",
    "LOAD_SOURCE_METHOD",
    "MUTATE_SOURCE_METHOD",
    "AndroidSourcesAPI",
]
