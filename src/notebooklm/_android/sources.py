"""Android backend implementation of the B1 source read surface."""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar, cast

from .._sources import SourcesAPI
from .._types.research import SourceGuide
from ..exceptions import RPCError
from ..types import Source, SourceFulltext, SourceStatus, SourceType
from .codecs.notebooks import decode_project, map_get_project_error
from .codecs.sources import decode_sources
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import b1_read_pb2
from .session import AndroidSession

logger = logging.getLogger(__name__)
_PROTO = cast(Any, b1_read_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"

_FilterValue = TypeVar("_FilterValue")


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


class AndroidSourcesAPI(SourcesAPI):
    """Read-only Android source adapter for the directly tested B1 graph."""

    def __init__(self, session: AndroidSession) -> None:
        self._session = session
        super().__init__()

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> builtins.list[Source]:
        """List normalized Android sources, preserving server order."""
        status_filter = _snapshot_enum_filter(
            statuses,
            enum_type=SourceStatus,
            parameter="statuses",
        )
        type_filter = _snapshot_enum_filter(
            types,
            enum_type=SourceType,
            parameter="types",
        )

        # evidence: docs/android/proto-evidence-ledger.md#field-ledger
        request = _PROTO.GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        try:
            response = await self._session.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=_PROTO.GetProjectResponse,
            )
        except RPCError as exc:
            mapped = map_get_project_error(notebook_id, exc, method_id=GET_PROJECT_METHOD)
            if mapped is exc:
                raise
            raise mapped from exc
        # Project identity is required even when its source list happens to be
        # empty; otherwise a default/malformed response could masquerade as an
        # honestly empty notebook.
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

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        title: str | None = None,
    ) -> Source:
        _reject("sources.add_url")

    async def _add_urls_batch(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[Any]:
        _reject("sources._add_urls_batch")

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
        _reject("sources.delete")

    async def rename(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Source | None:
        _reject("sources.rename")

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        _reject("sources.refresh")

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        _reject("sources.check_freshness")

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        _reject("sources.get_guide")

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: Literal["text", "markdown"] = "text",
    ) -> SourceFulltext:
        _reject("sources.get_fulltext")


__all__ = ["AndroidSourcesAPI", "GET_PROJECT_METHOD"]
