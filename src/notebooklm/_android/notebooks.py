"""Android backend implementation of the B1 notebook read surface."""

from __future__ import annotations

import builtins
import logging
from contextvars import ContextVar
from typing import Any, NoReturn, cast

from google.protobuf.empty_pb2 import Empty

from .._idempotency import mark_unconfirmed
from .._notebook_metadata import NotebookSourceLister
from .._notebooks import NotebooksAPI
from ..exceptions import (
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from ..types import Notebook, NotebookDescription, PromptSuggestion
from .codecs.notebooks import (
    decode_notebook_guide,
    decode_project,
    map_get_project_error,
    message_to_known_dict,
)
from .codecs.sources import decode_sources
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2
from .proto.notebooklm.internal.android.wire.v1 import notebooks_pb2
from .session import AndroidSession

logger = logging.getLogger(__name__)
_PROTO = cast(Any, read_pb2)
_WIRE = cast(Any, notebooks_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
LIST_RECENT_PROJECTS_METHOD = f"/{_SERVICE}/ListRecentlyViewedProjects"
CREATE_PROJECT_METHOD = f"/{_SERVICE}/CreateProject"
COPY_PROJECT_METHOD = f"/{_SERVICE}/CopyProject"
DELETE_PROJECTS_METHOD = f"/{_SERVICE}/DeleteProjects"
MUTATE_PROJECT_METHOD = f"/{_SERVICE}/MutateProject"
GENERATE_NOTEBOOK_GUIDE_METHOD = f"/{_SERVICE}/GenerateNotebookGuide"


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class AndroidNotebooksAPI(NotebooksAPI):
    """Android notebook adapter for the directly tested B1/B2 graph."""

    _create_method_id = f"/{_SERVICE}/CreateProject"

    def __init__(self, session: AndroidSession, sources_api: NotebookSourceLister) -> None:
        """Bind the Android session and required structural source-listing collaborator."""
        self._transport = session
        self._workflow_epoch: ContextVar[int | None] = ContextVar(
            "android_notebook_workflow_epoch",
            default=None,
        )
        super().__init__(sources_api)

    def _epoch_kwargs(self) -> dict[str, Any]:
        epoch = self._workflow_epoch.get()
        return {} if epoch is None else {"expected_epoch": epoch}

    async def _get_project_response(
        self,
        notebook_id: str,
    ) -> Any:
        # evidence: docs/android/proto-evidence-ledger.md#field-ledger
        request = _PROTO.GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        try:
            return await self._transport.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=_PROTO.GetProjectResponse,
                **self._epoch_kwargs(),
            )
        except RPCError as exc:
            mapped = map_get_project_error(notebook_id, exc, method_id=GET_PROJECT_METHOD)
            if mapped is exc:
                raise
            raise mapped from exc

    async def list(self) -> builtins.list[Notebook]:
        """List Android projects in the server's recent-first order."""
        # evidence: docs/android/proto-evidence-ledger.md#field-ledger
        request = _PROTO.ListRecentlyViewedProjectsRequest(
            include_own_projects=True,
            include_audio_overview_ids=True,
        )
        response = await self._transport.unary(
            LIST_RECENT_PROJECTS_METHOD,
            request,
            replay_safe=True,
            response_type=_PROTO.ListRecentlyViewedProjectsResponse,
            **self._epoch_kwargs(),
        )
        return [
            decode_project(project, method_id=LIST_RECENT_PROJECTS_METHOD)
            for project in response.projects
        ]

    async def get(self, notebook_id: str) -> Notebook:
        """Get one Android project, translating only status-5 misses."""
        response = await self._get_project_response(notebook_id)
        return decode_project(response.project, method_id=GET_PROJECT_METHOD)

    async def get_raw(self, notebook_id: str) -> dict[str, Any]:
        """Return the known-field protobuf response as a backend-shaped dict."""
        response = await self._get_project_response(notebook_id)
        # The raw contract is the full response envelope, matching the web
        # method's transport-shaped return rather than silently unwrapping the
        # project only for Android.
        return message_to_known_dict(response, method_id=GET_PROJECT_METHOD)

    async def get_source_ids(self, notebook_id: str) -> builtins.list[str]:
        """Return ordered, first-occurrence source IDs from one project read."""
        response = await self._get_project_response(notebook_id)
        # Validate the containing entity before accepting an apparently empty
        # repeated field from a malformed/default response.
        decode_project(response.project, method_id=GET_PROJECT_METHOD)
        return [
            source.id
            for source in decode_sources(
                response.project.sources,
                method_id=GET_PROJECT_METHOD,
                strict=False,
                logger=logger,
            )
        ]

    async def create(self, title: str) -> Notebook:
        """Create through the base transport-neutral probe workflow."""
        async with self._transport.operation_scope("notebooks.create") as lease:
            token = self._workflow_epoch.set(lease.epoch)
            try:
                return await super().create(title)
            finally:
                self._workflow_epoch.reset(token)

    async def _send_create(self, title: str) -> Notebook:
        # evidence: docs/android/proto-evidence-ledger.md#b2-repository-local-wire-field-ledger
        response = await self._transport.unary(
            CREATE_PROJECT_METHOD,
            _WIRE.WireCreateProjectRequest(name=title),
            replay_safe=False,
            response_type=_PROTO.Project,
            **self._epoch_kwargs(),
        )
        return decode_project(response, method_id=CREATE_PROJECT_METHOD)

    async def copy(self, notebook_id: str, title: str) -> Notebook:
        """Copy once, surfacing transport loss as an ambiguous outcome."""
        if not notebook_id:
            raise ValidationError("notebook_id must not be empty")
        if not title or not title.strip():
            raise ValidationError("title must not be empty")

        # evidence: docs/android/proto-evidence-ledger.md#b2-repository-local-wire-field-ledger
        try:
            response = await self._transport.unary(
                COPY_PROJECT_METHOD,
                _WIRE.WireCopyProjectRequest(
                    source_project_id=notebook_id,
                    title=title,
                ),
                replay_safe=False,
                response_type=_PROTO.Project,
            )
        except (NetworkError, RateLimitError, ServerError) as exc:
            rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
            raise mark_unconfirmed(
                RPCError(
                    "UNRESOLVED — CopyProject may have committed before its response was "
                    "lost. Do not blindly retry; list notebooks and resolve copies "
                    "manually first.",
                    method_id=COPY_PROJECT_METHOD,
                    rpc_code=rpc_code,
                )
            ) from exc
        notebook = decode_project(response, method_id=COPY_PROJECT_METHOD)
        if notebook.id == notebook_id:
            raise DecodingError(
                "CopyProject response reused the source notebook id",
                method_id=COPY_PROJECT_METHOD,
            )
        return notebook

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
        mode: int = 4,
        query: str | None = None,
    ) -> builtins.list[PromptSuggestion]:
        _reject("notebooks.suggest_prompts")

    async def delete(self, notebook_id: str) -> None:
        # evidence: docs/android/proto-evidence-ledger.md#b2-repository-local-wire-field-ledger
        try:
            await self._transport.unary(
                DELETE_PROJECTS_METHOD,
                _WIRE.WireDeleteProjectsRequest(project_ids=[notebook_id]),
                replay_safe=False,
                response_type=Empty,
            )
        except RPCError as exc:
            # Public notebook deletion is idempotent: an already-absent row is
            # the requested final state. This is status projection, not replay.
            if exc.rpc_code == 5:
                return
            raise

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        if title is None and emoji is None:
            raise ValidationError("At least one of title or emoji must be provided")

        # evidence: docs/android/proto-evidence-ledger.md#b2-repository-local-wire-field-ledger
        # ``new_emoji`` is proto3-optional because an explicitly present empty
        # string clears the emoji; omitting it preserves the current value.
        request = _WIRE.WireMutateProjectRequest(project_id=notebook_id)
        change = request.mutations.add().change_property
        if title is not None:
            change.new_title = title
        if emoji is not None:
            change.new_emoji = emoji
        response = await self._transport.unary(
            MUTATE_PROJECT_METHOD,
            request,
            replay_safe=False,
            response_type=_PROTO.Project,
        )
        return decode_project(response, method_id=MUTATE_PROJECT_METHOD)

    async def get_summary(self, notebook_id: str) -> str:
        response = await self._generate_notebook_guide(notebook_id)
        return decode_notebook_guide(response, method_id=GENERATE_NOTEBOOK_GUIDE_METHOD).summary

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        response = await self._generate_notebook_guide(notebook_id)
        return decode_notebook_guide(response, method_id=GENERATE_NOTEBOOK_GUIDE_METHOD)

    async def _generate_notebook_guide(self, notebook_id: str) -> Any:
        # evidence: docs/android/proto-evidence-ledger.md#b2-repository-local-wire-field-ledger
        return await self._transport.unary(
            GENERATE_NOTEBOOK_GUIDE_METHOD,
            _WIRE.WireGenerateNotebookGuideRequest(project_id=notebook_id),
            replay_safe=False,
            response_type=_WIRE.WireGenerateNotebookGuideResponse,
        )

    async def remove_from_recent(self, notebook_id: str) -> None:
        _reject("notebooks.remove_from_recent")


__all__ = [
    "AndroidNotebooksAPI",
    "COPY_PROJECT_METHOD",
    "CREATE_PROJECT_METHOD",
    "DELETE_PROJECTS_METHOD",
    "GENERATE_NOTEBOOK_GUIDE_METHOD",
    "GET_PROJECT_METHOD",
    "LIST_RECENT_PROJECTS_METHOD",
    "MUTATE_PROJECT_METHOD",
]
