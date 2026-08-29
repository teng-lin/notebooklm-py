"""Android backend implementation of the B1 notebook read surface."""

from __future__ import annotations

import logging
from typing import Any

from .._notebooks import NotebooksAPI
from ..exceptions import NotebookNotFoundError, RPCError
from ..types import Notebook, NotebookDescription, PromptSuggestion
from .codecs.notebooks import decode_project, message_to_known_dict
from .codecs.sources import decode_sources
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import b1_read_pb2
from .session import AndroidSession
from .sources import AndroidSourcesAPI

logger = logging.getLogger(__name__)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
LIST_RECENT_PROJECTS_METHOD = f"/{_SERVICE}/ListRecentlyViewedProjects"


class AndroidNotebooksAPI(NotebooksAPI):
    """Read-only Android notebook adapter for the directly tested B1 graph."""

    _create_method_id = f"/{_SERVICE}/CreateProject"

    def __init__(self, session: AndroidSession, sources_api: AndroidSourcesAPI) -> None:
        """Bind the Android session and its exact non-null source collaborator."""
        self._session = session
        super().__init__(sources_api)

    async def _get_project_response(
        self,
        notebook_id: str,
    ) -> b1_read_pb2.GetProjectResponse:
        # evidence: docs/mobile/endpoints.md#GetProject
        request = b1_read_pb2.GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        return await self._session.unary(
            GET_PROJECT_METHOD,
            request,
            replay_safe=True,
            response_type=b1_read_pb2.GetProjectResponse,
        )

    async def list(self) -> list[Notebook]:
        """List Android projects in the server's recent-first order."""
        # evidence: docs/mobile/endpoints.md#Method-reference
        request = b1_read_pb2.ListRecentlyViewedProjectsRequest(
            include_own_projects=True,
            include_audio_overview_ids=True,
        )
        response = await self._session.unary(
            LIST_RECENT_PROJECTS_METHOD,
            request,
            replay_safe=True,
            response_type=b1_read_pb2.ListRecentlyViewedProjectsResponse,
        )
        return [
            decode_project(project, method_id=LIST_RECENT_PROJECTS_METHOD)
            for project in response.projects
        ]

    async def get(self, notebook_id: str) -> Notebook:
        """Get one Android project, translating only status-5 misses."""
        try:
            response = await self._get_project_response(notebook_id)
        except RPCError as exc:
            if exc.rpc_code != 5:
                raise
            raise NotebookNotFoundError(
                notebook_id,
                method_id=GET_PROJECT_METHOD,
                raw_response=exc.raw_response,
                rpc_code=exc.rpc_code,
                found_ids=exc.found_ids,
                detail=str(exc),
            ) from exc
        return decode_project(response.project, method_id=GET_PROJECT_METHOD)

    async def get_raw(self, notebook_id: str) -> dict[str, Any]:
        """Return the known-field protobuf response as a backend-shaped dict."""
        response = await self._get_project_response(notebook_id)
        return message_to_known_dict(response)

    async def get_source_ids(self, notebook_id: str) -> list[str]:
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
        unsupported_operation("notebooks.create")

    async def _send_create(self, title: str) -> Notebook:
        unsupported_operation("notebooks.create")

    async def copy(self, notebook_id: str, title: str) -> Notebook:
        unsupported_operation("notebooks.copy")

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: list[str] | None = None,
        mode: int = 4,
        query: str | None = None,
    ) -> list[PromptSuggestion]:
        unsupported_operation("notebooks.suggest_prompts")

    async def delete(self, notebook_id: str) -> None:
        unsupported_operation("notebooks.delete")

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        unsupported_operation("notebooks.update")

    async def get_summary(self, notebook_id: str) -> str:
        unsupported_operation("notebooks.get_summary")

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        unsupported_operation("notebooks.get_description")

    async def remove_from_recent(self, notebook_id: str) -> None:
        unsupported_operation("notebooks.remove_from_recent")


__all__ = [
    "AndroidNotebooksAPI",
    "GET_PROJECT_METHOD",
    "LIST_RECENT_PROJECTS_METHOD",
]
