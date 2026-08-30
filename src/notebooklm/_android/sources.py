"""Android source reads plus evidence-qualified source operations."""

from __future__ import annotations

import asyncio
import builtins
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast

from .._deadline import RuntimeDeadline
from .._idempotency import mark_unconfirmed
from .._source.batch import SourceUrlBatchItem
from .._sources import SourcesAPI
from .._types.documents import StructuredDocument
from .._types.research import SourceGuide
from .._url_utils import is_youtube_url
from ..exceptions import (
    AuthError,
    ConfigurationError,
    DecodingError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    ValidationError,
)
from ..types import Source, SourceFulltext, SourceStatus, SourceType
from .codecs.documents import decode_document, tailwind_doc_markdown, tailwind_doc_plain_text
from .codecs.notebooks import decode_project, map_get_project_error, validate_project_identity
from .codecs.sources import decode_source, decode_sources
from .session import AndroidSession
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
DELETE_SOURCES_METHOD = f"/{_SERVICE}/DeleteSources"
MUTATE_SOURCE_METHOD = f"/{_SERVICE}/MutateSource"
GENERATE_DOCUMENT_GUIDES_METHOD = f"/{_SERVICE}/GenerateDocumentGuides"
LOAD_SOURCE_METHOD = f"/{_SERVICE}/LoadSource"
CHECK_SOURCE_FRESHNESS_METHOD = f"/{_SERVICE}/CheckSourceFreshness"
REFRESH_SOURCE_METHOD = f"/{_SERVICE}/RefreshSource"

_FilterValue = TypeVar("_FilterValue")
_CORRELATION_PREFIX = "nblm-"
_CANONICAL_ID_LENGTH = 36


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


_WEB_FILE_UPLOAD_COMPAT_EXTENSIONS = frozenset({".csv", ".docx"})


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
    return mark_unconfirmed(
        SourceAddError(
            subject,
            cause=cause,
            message=(
                "UNRESOLVED — check the notebook source list before retrying. "
                f"The Android {kind} add could not prove {stage} for {subject!r}; neither write "
                "was replayed and no cleanup delete was sent."
            ),
        )
    )


def _validate_add_text_idempotency(idempotent: bool) -> None:
    if idempotent:
        raise NonIdempotentRetryError(
            "add_text cannot be marked idempotent: text sources have no "
            "reliable server-side dedupe key (titles non-unique, content "
            "not exposed). For idempotent text imports, embed a UUID in "
            "the title and dedupe client-side. See "
            "docs/python-api.md#idempotency."
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


class AndroidSourcesAPI(SourcesAPI):
    """Android source adapter installed by public Android backend selection."""

    def __init__(
        self,
        session: AndroidSession,
        upload_pipeline: AndroidUploadPipeline,
        *,
        drive_download: DriveDownload | None = None,
        add_file_compat: AddFileCompat | None = None,
    ) -> None:
        """Bind native sources plus the two qualified Web file-upload seams.

        Live native PDF and Markdown controls reach ``SOURCE_STATUS_COMPLETE``,
        while CSV and DOCX finish in ``SOURCE_STATUS_ERROR`` even with the exact
        APK Scotty transaction. Public client assembly therefore supplies the
        already-authenticated Web uploader for only those two extensions. Direct
        adapter callers may omit the collaborator to exercise the native
        transaction for evidence work. Other extensions remain native unless
        separately qualified by evidence.
        """
        self._transport = session
        self._upload_pipeline = upload_pipeline
        self._add_file_compat = add_file_compat
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
        deadline = RuntimeDeadline.start(timeout)
        last_status: int | None = None
        while not deadline.expired():
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                _read_proto().GetProjectRequest(
                    project_id=notebook_id,
                    include_audio_overview_ids=True,
                ),
                replay_safe=True,
                response_type=_read_proto().GetProjectResponse,
                expected_epoch=expected_epoch,
                timeout=deadline.remaining(),
            )
            validate_project_identity(response.project, notebook_id, method_id=GET_PROJECT_METHOD)
            decode_project(response.project, method_id=GET_PROJECT_METHOD)
            matches = []
            for row in response.project.sources:
                try:
                    raw_id = row.source_id.id if row.HasField("source_id") else ""
                except Exception:
                    continue
                if raw_id == source_id:
                    matches.append(row)
            if len(matches) > 1:
                raise DecodingError(
                    "Android upload polling returned duplicate source ids",
                    method_id=GET_PROJECT_METHOD,
                )
            if matches:
                row = next(iter(matches))
                last_status = row.settings.status if row.HasField("settings") else 0
                if last_status == _settings_proto().SOURCE_STATUS_ERROR:
                    raise SourceProcessingError(source_id, status=last_status)
                accepted = (
                    last_status == _settings_proto().SOURCE_STATUS_COMPLETE
                    if ready
                    else last_status
                    in {
                        _settings_proto().SOURCE_STATUS_PENDING,
                        _settings_proto().SOURCE_STATUS_COMPLETE,
                    }
                )
                if accepted:
                    return decode_source(row, method_id=GET_PROJECT_METHOD)
            remaining = deadline.remaining()
            if remaining <= 0.0:
                break
            await asyncio.sleep(min(0.5, remaining))
        raise SourceTimeoutError(source_id, timeout, last_status)

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
    ) -> tuple[dict[str, _CommitProof], set[str]]:
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
            return _collect_commit_proofs(
                response.project.sources,
                source_ids,
                method_id=GET_PROJECT_METHOD,
            )
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except (RPCError, NetworkError):
            logger.warning(
                "Android source commit reconciliation failed; preserving only prior "
                "affirmative response evidence"
            )
            return {}, set()

    async def _commit_user_contents(
        self,
        notebook_id: str,
        entries: Sequence[tuple[Any, str]],
        *,
        expected_epoch: int,
    ) -> tuple[dict[str, _CommitProof], set[str]]:
        candidate_ids = [source_id for _, source_id in entries]
        request = _write_proto().AddSourcesRequest(
            user_content=[content for content, _ in entries],
            project_id=notebook_id,
            request_context=android_request_context(),
        )
        response_proofs: dict[str, _CommitProof] = {}
        response_unresolved: set[str] = set()
        try:
            response = await self._transport.unary(
                ADD_SOURCES_METHOD,
                request,
                replay_safe=False,
                response_type=_write_proto().AddSourcesResponse,
                expected_epoch=expected_epoch,
            )
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except (NetworkError, RateLimitError, ServerError):
            pass
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

        read_proofs, read_unresolved = await self._read_commit_proofs(
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
    ) -> Source:
        correlation = _correlation_name()
        async with self._transport.operation_scope(operation_label) as lease:
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

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None = None,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
        on_progress: Callable[[int, int], object] | None = None,
    ) -> Source:
        # Choose the qualified compatibility path from the same canonical
        # target whose filename drives MIME inference in either uploader. This
        # prevents a misleading symlink suffix from routing CSV/DOCX through
        # the native transaction that live evidence has shown will fail. Both
        # uploaders still resolve/check the supplied canonical path inside
        # their own admitted operation before opening it.
        canonical_path = await asyncio.to_thread(Path(file_path).resolve)
        if (
            canonical_path.suffix.lower() in _WEB_FILE_UPLOAD_COMPAT_EXTENSIONS
            and self._add_file_compat is not None
        ):
            return await self._add_file_compat(
                notebook_id,
                canonical_path,
                mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
                title=title,
                on_progress=on_progress,
            )

        adapter = self
        result: Source | None = None
        failure: BaseException | None = None
        try:
            result = await adapter._upload_pipeline.upload_file(
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
            )
        except BaseException as error:
            from .errors import sanitize_escaping_exception

            failure = sanitize_escaping_exception(error)
        finally:
            del self, adapter
        if failure is not None:
            raise failure
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
        async with self._transport.operation_scope("sources.check_freshness") as lease:
            await self._require_owned_source(
                notebook_id,
                source_id,
                expected_epoch=lease.epoch,
                method_id=CHECK_SOURCE_FRESHNESS_METHOD,
            )
            return await self._check_freshness(source_id, expected_epoch=lease.epoch)

    async def _check_freshness(
        self,
        source_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        unary_options: dict[str, Any] = {
            "replay_safe": True,
            "response_type": _write_proto().CheckSourceFreshnessResponse,
        }
        if expected_epoch is not None:
            unary_options["expected_epoch"] = expected_epoch
        response = await self._transport.unary(
            CHECK_SOURCE_FRESHNESS_METHOD,
            _write_proto().CheckSourceFreshnessRequest(
                source_id=_read_proto().SourceId(id=source_id),
                request_context=android_request_context(),
            ),
            **unary_options,
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
        request = _write_proto().GenerateDocumentGuidesRequest(
            sources=[_write_proto().InputSource(source_id=_read_proto().SourceId(id=source_id))]
        )
        async with self._transport.operation_scope("sources.get_guide") as lease:
            await self._require_owned_source(
                notebook_id,
                source_id,
                expected_epoch=lease.epoch,
                method_id=GENERATE_DOCUMENT_GUIDES_METHOD,
            )
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
                raise SourceNotFoundError(
                    source_id, method_id=GENERATE_DOCUMENT_GUIDES_METHOD
                ) from None

        matches = [
            guide
            for guide in response.guides
            if guide.HasField("source")
            and guide.source.HasField("source_id")
            and guide.source.source_id.id == source_id
        ]
        if not matches:
            if not response.guides:
                raise SourceNotFoundError(source_id, method_id=GENERATE_DOCUMENT_GUIDES_METHOD)
            raise DecodingError(
                "Android source guide response did not match the requested source id",
                method_id=GENERATE_DOCUMENT_GUIDES_METHOD,
            )
        if len(matches) != 1:
            raise DecodingError(
                "Android source guide response contained duplicate source ids",
                method_id=GENERATE_DOCUMENT_GUIDES_METHOD,
            )
        guide = next(iter(matches))
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
        if not raw_id:
            raise SourceNotFoundError(source_id, method_id=LOAD_SOURCE_METHOD)
        if raw_id != source_id:
            raise DecodingError(
                "Android full-text response did not match the requested source id",
                method_id=LOAD_SOURCE_METHOD,
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
    "ADD_SOURCES_METHOD",
    "ADD_TENTATIVE_SOURCES_METHOD",
    "CHECK_SOURCE_FRESHNESS_METHOD",
    "DELETE_SOURCES_METHOD",
    "GENERATE_DOCUMENT_GUIDES_METHOD",
    "GET_PROJECT_METHOD",
    "LOAD_SOURCE_METHOD",
    "MUTATE_SOURCE_METHOD",
    "AndroidSourcesAPI",
]
