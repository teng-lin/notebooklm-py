"""Android backend implementation of the B1 notebook read surface."""

from __future__ import annotations

import builtins
import logging
from typing import Any, NoReturn, cast

from .._notebooks import NotebooksAPI
from ..exceptions import RPCError
from ..types import Notebook, NotebookDescription, PromptSuggestion
from .codecs.notebooks import decode_project, map_get_project_error, message_to_known_dict
from .codecs.sources import decode_sources
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import b1_read_pb2
from .session import AndroidSession
from .sources import AndroidSourcesAPI

logger = logging.getLogger(__name__)
_PROTO = cast(Any, b1_read_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
LIST_RECENT_PROJECTS_METHOD = f"/{_SERVICE}/ListRecentlyViewedProjects"


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class AndroidNotebooksAPI(NotebooksAPI):
    """Read-only Android notebook adapter for the directly tested B1 graph."""

    _create_method_id = f"/{_SERVICE}/CreateProject"

    def __init__(self, session: AndroidSession, sources_api: AndroidSourcesAPI) -> None:
        """Bind the Android session and its exact non-null source collaborator."""
        self._transport = session
        super().__init__(sources_api)

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
        """Reject B1 create before the base class can perform its list probe."""
        _reject("notebooks.create")

    async def _send_create(self, title: str) -> Notebook:
        _reject("notebooks.create")

    async def copy(self, notebook_id: str, title: str) -> Notebook:
        _reject("notebooks.copy")

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
        _reject("notebooks.delete")

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        _reject("notebooks.update")

    async def get_summary(self, notebook_id: str) -> str:
        _reject("notebooks.get_summary")

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        _reject("notebooks.get_description")

    async def remove_from_recent(self, notebook_id: str) -> None:
        _reject("notebooks.remove_from_recent")


__all__ = [
    "AndroidNotebooksAPI",
    "GET_PROJECT_METHOD",
    "LIST_RECENT_PROJECTS_METHOD",
]
