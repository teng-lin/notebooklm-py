"""Android source reads plus evidence-qualified B3 source operations."""

from __future__ import annotations

import asyncio
import builtins
import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar, cast

from google.protobuf import empty_pb2

from .._idempotency import mark_unconfirmed
from .._source.batch import SourceUrlBatchItem
from .._sources import SourcesAPI
from .._types.documents import StructuredDocument
from .._types.research import SourceGuide
from .._url_utils import is_youtube_url
from ..exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
)
from ..types import Source, SourceFulltext, SourceStatus, SourceType
from .codecs.notebooks import decode_project, map_get_project_error
from .codecs.sources import decode_source, decode_sources
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import (
    b1_read_pb2,
    b3_sources_pb2,
)
from .proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from .proto.notebooklm.internal.android.wire import source_mutation_wire_pb2
from .session import AndroidSession

logger = logging.getLogger(__name__)
_READ_PROTO = cast(Any, b1_read_pb2)
_WRITE_PROTO = cast(Any, b3_sources_pb2)
_MUTATION_WIRE = cast(Any, source_mutation_wire_pb2)
_SETTINGS_PROTO = cast(Any, source_settings_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
ADD_TENTATIVE_SOURCES_METHOD = f"/{_SERVICE}/AddTentativeSources"
ADD_SOURCES_METHOD = f"/{_SERVICE}/AddSources"
DELETE_SOURCES_METHOD = f"/{_SERVICE}/DeleteSources"
MUTATE_SOURCE_METHOD = f"/{_SERVICE}/MutateSource"
GENERATE_DOCUMENT_GUIDES_METHOD = f"/{_SERVICE}/GenerateDocumentGuides"
LOAD_SOURCE_METHOD = f"/{_SERVICE}/LoadSource"

_FilterValue = TypeVar("_FilterValue")
_CORRELATION_PREFIX = "nblm-"
_CANONICAL_ID_LENGTH = 36


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


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


def _known_registration_error(url: str) -> SourceAddError:
    return SourceAddError(
        url,
        message=f"Failed to register URL source {url!r}: the backend omitted its registration.",
    )


def _unresolved_add_error(
    url: str,
    *,
    stage: str,
    cause: Exception | None = None,
) -> SourceAddError:
    return mark_unconfirmed(
        SourceAddError(
            url,
            cause=cause,
            message=(
                "UNRESOLVED — check the notebook source list before retrying. "
                f"The Android URL add could not prove {stage} for {url!r}; neither write "
                "was replayed and no cleanup delete was sent."
            ),
        )
    )


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
    if raw_status == _SETTINGS_PROTO.SOURCE_STATUS_PENDING:
        return _ProofKind.PENDING
    if raw_status == _SETTINGS_PROTO.SOURCE_STATUS_COMPLETE:
        return _ProofKind.COMPLETE
    if raw_status == _SETTINGS_PROTO.SOURCE_STATUS_ERROR:
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
    """Direct-test Android source adapter through the B3 evidence slice."""

    def __init__(self, session: AndroidSession) -> None:
        self._transport = session
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
        request = _READ_PROTO.GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        try:
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=_READ_PROTO.GetProjectResponse,
            )
        except RPCError as exc:
            mapped = map_get_project_error(notebook_id, exc, method_id=GET_PROJECT_METHOD)
            if mapped is exc:
                raise
            raise mapped from exc
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
        request = _WRITE_PROTO.AddTentativeSourcesRequest(
            tentative_sources_metadata=[
                _WRITE_PROTO.TentativeSourceMetadata(name=name) for name in names
            ],
            project_id=notebook_id,
        )
        response = await self._transport.unary(
            ADD_TENTATIVE_SOURCES_METHOD,
            request,
            replay_safe=False,
            response_type=_WRITE_PROTO.AddTentativeSourcesResponse,
            expected_epoch=expected_epoch,
        )
        return _correlate_registrations(names, response)

    async def _read_commit_proofs(
        self,
        notebook_id: str,
        source_ids: Collection[str],
        *,
        expected_epoch: int,
    ) -> tuple[dict[str, _CommitProof], set[str]]:
        request = _READ_PROTO.GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        try:
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=_READ_PROTO.GetProjectResponse,
                expected_epoch=expected_epoch,
            )
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
        except RPCError:
            logger.warning(
                "Android source commit reconciliation failed; preserving only prior "
                "affirmative response evidence"
            )
            return {}, set()

    async def _commit_urls(
        self,
        notebook_id: str,
        entries: Sequence[tuple[str, str]],
        *,
        expected_epoch: int,
    ) -> tuple[dict[str, _CommitProof], set[str]]:
        candidate_ids = [source_id for _, source_id in entries]
        request = _WRITE_PROTO.AddSourcesRequest(
            user_content=[
                _WRITE_PROTO.UserContent(
                    web_content=_WRITE_PROTO.WebContent(url=url),
                    tentative_source_id=_READ_PROTO.SourceId(id=source_id),
                )
                for url, source_id in entries
            ],
            project_id=notebook_id,
        )
        response_proofs: dict[str, _CommitProof] = {}
        response_unresolved: set[str] = set()
        try:
            response = await self._transport.unary(
                ADD_SOURCES_METHOD,
                request,
                replay_safe=False,
                response_type=_WRITE_PROTO.AddSourcesResponse,
                expected_epoch=expected_epoch,
            )
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except RPCError:
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

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
    ) -> Source:
        if is_youtube_url(url):
            _reject("sources.add_url for YouTube URLs")
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
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as exc:
                raise _unresolved_add_error(
                    url,
                    stage="tentative registration",
                    cause=exc,
                ) from None

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
                raise _unresolved_add_error(url, stage="phase-two acceptance")
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
        for url in snapshot:
            if is_youtube_url(url):
                _reject("sources._add_urls_batch for YouTube URLs")
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
            except RPCError as exc:
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
                        else "phase-two acceptance"
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
        _reject("sources.add_text")

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
        _reject("sources.add_file")

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
        _reject("sources.add_drive")

    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None = None,
        wait: bool = False,
        wait_timeout: float = 120.0,
    ) -> Source:
        _reject("sources.add_drive_file")

    async def delete(self, notebook_id: str, source_id: str) -> None:
        del notebook_id
        request = _WRITE_PROTO.DeleteSourcesRequest(source_ids=[_READ_PROTO.SourceId(id=source_id)])
        try:
            await self._transport.unary(
                DELETE_SOURCES_METHOD,
                request,
                replay_safe=False,
                response_type=empty_pb2.Empty,
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
        request = _MUTATION_WIRE.MutateSourceWireRequest(
            source_id=_READ_PROTO.SourceId(id=source_id),
            mutations=[
                _MUTATION_WIRE.SourceMutation(
                    change_title=_MUTATION_WIRE.ChangeTitle(title=new_title)
                )
            ],
        )
        try:
            response = await self._transport.unary(
                MUTATE_SOURCE_METHOD,
                request,
                replay_safe=False,
                response_type=_READ_PROTO.Source,
            )
        except (AuthError, RateLimitError, ServerError, NetworkError):
            raise
        except RPCError as exc:
            if exc.rpc_code != 5:
                raise
            raise SourceNotFoundError(source_id, method_id=MUTATE_SOURCE_METHOD) from None

        echoed_id = response.source_id.id if response.HasField("source_id") else ""
        if echoed_id:
            if echoed_id != source_id:
                raise DecodingError(
                    "Android source mutation returned an unexpected source id",
                    method_id=MUTATE_SOURCE_METHOD,
                )
            if not return_object:
                return None
            return decode_source(response, method_id=MUTATE_SOURCE_METHOD)
        source = await self.get_or_none(notebook_id, source_id)
        if source is None:
            raise SourceNotFoundError(source_id, method_id=MUTATE_SOURCE_METHOD)
        return source if return_object else None

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        _reject("sources.refresh")

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        _reject("sources.check_freshness")

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        del notebook_id
        request = _WRITE_PROTO.GenerateDocumentGuidesRequest(
            sources=[_WRITE_PROTO.InputSource(source_id=_READ_PROTO.SourceId(id=source_id))]
        )
        try:
            response = await self._transport.unary(
                GENERATE_DOCUMENT_GUIDES_METHOD,
                request,
                replay_safe=True,
                response_type=_WRITE_PROTO.GenerateDocumentGuidesResponse,
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
        del notebook_id
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

        request = _WRITE_PROTO.LoadSourceRequest(source_id=_READ_PROTO.SourceId(id=source_id))
        try:
            response = await self._transport.unary(
                LOAD_SOURCE_METHOD,
                request,
                replay_safe=True,
                response_type=_WRITE_PROTO.LoadSourceResponse,
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
        content = (
            response.markdown_string
            if output_format == "markdown"
            else (response.plain_text.body if response.HasField("plain_text") else "")
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
            document=StructuredDocument(),
        )


__all__ = [
    "ADD_SOURCES_METHOD",
    "ADD_TENTATIVE_SOURCES_METHOD",
    "DELETE_SOURCES_METHOD",
    "GENERATE_DOCUMENT_GUIDES_METHOD",
    "GET_PROJECT_METHOD",
    "LOAD_SOURCE_METHOD",
    "MUTATE_SOURCE_METHOD",
    "AndroidSourcesAPI",
]
