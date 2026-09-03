"""Android backend implementation of the read notebook read surface."""

from __future__ import annotations

import builtins
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from .._idempotency import mark_unconfirmed
from .._notebook_metadata import NotebookSourceLister
from .._notebooks import NotebooksAPI
from .._runtime.call_supervisor import OperationLease
from ..exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from ..types import NextStepSuggestion, Notebook, NotebookDescription, PromptSuggestion
from .epoch import bind_workflow_epoch, reset_workflow_epoch
from .session import AndroidSession

logger = logging.getLogger(__name__)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
LIST_RECENT_PROJECTS_METHOD = f"/{_SERVICE}/ListRecentlyViewedProjects"
CREATE_PROJECT_METHOD = f"/{_SERVICE}/CreateProject"
COPY_PROJECT_METHOD = f"/{_SERVICE}/CopyProject"
DELETE_PROJECTS_METHOD = f"/{_SERVICE}/DeleteProjects"
MUTATE_PROJECT_METHOD = f"/{_SERVICE}/MutateProject"
GENERATE_NOTEBOOK_GUIDE_METHOD = f"/{_SERVICE}/GenerateNotebookGuide"
GENERATE_PROMPT_SUGGESTIONS_METHOD = f"/{_SERVICE}/GeneratePromptSuggestions"
REMOVE_RECENTLY_VIEWED_PROJECT_METHOD = f"/{_SERVICE}/RemoveRecentlyViewedProject"
NEXT_STEP_SUGGESTIONS_METHOD = f"/{_SERVICE}/NextStepSuggestions"

_LEADING_LIST_MARKER = re.compile(r"[-*+]\s+")
_MAX_CREATED_CHAT_SESSION_HINTS = 256
# gRPC INTERNAL. The route refuses a project the caller owns; see
# ``remove_from_recent``.
_INTERNAL_RPC_CODE = 13


def _read_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _notebook_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import notebooks_pb2

    return cast(Any, notebooks_pb2)


def _write_proto_sources() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _chat_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import chat_pb2

    return cast(Any, chat_pb2)


def _wire_proto() -> Any:
    from .proto.notebooklm.internal.android.wire.v1 import notebooks_pb2

    return cast(Any, notebooks_pb2)


def _notebook_codec() -> Any:
    from .codecs import notebooks

    return notebooks


def _android_request_context() -> Any:
    from .upload import android_request_context

    return android_request_context()


def _empty_message_type() -> type[Any]:
    """Import the optional protobuf response only when Android is selected."""
    from google.protobuf.empty_pb2 import Empty

    return Empty


def _strip_leading_list_marker(text: str) -> str:
    lstripped = text.lstrip()
    marker = _LEADING_LIST_MARKER.match(lstripped)
    if marker:
        return lstripped[marker.end() :].strip()
    return lstripped.rstrip()


class AndroidNotebooksAPI(NotebooksAPI):
    """Android notebook adapter for the directly tested read/notebook graph."""

    _create_method_id = f"/{_SERVICE}/CreateProject"
    _copy_method_id = COPY_PROJECT_METHOD
    _copy_failure_chain = "suppress"

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
        sources_api: NotebookSourceLister,
    ) -> None:
        self._transport = session
        super().__init__(sources_api)

    def _remember_created_chat_session(self, notebook: Notebook) -> None:
        if notebook.id and notebook.chat_sessions:
            self._created_chat_session_ids.pop(notebook.id, None)
            self._created_chat_session_ids[notebook.id] = notebook.chat_sessions[0].id
            while len(self._created_chat_session_ids) > _MAX_CREATED_CHAT_SESSION_HINTS:
                self._created_chat_session_ids.pop(next(iter(self._created_chat_session_ids)))

    async def _get_project_response(
        self,
        notebook_id: str,
    ) -> Any:
        # evidence: docs/android/proto-evidence-ledger.md#field-ledger
        proto = _read_proto()
        wire = _wire_proto()
        request = proto.GetProjectRequest(
            project_id=notebook_id,
            include_audio_overview_ids=True,
        )
        try:
            response = await self._transport.unary(
                GET_PROJECT_METHOD,
                request,
                replay_safe=True,
                response_type=wire.WireGetProjectResponse,
            )
            _notebook_codec().validate_project_identity(
                response.project,
                notebook_id,
                method_id=GET_PROJECT_METHOD,
            )
            return response
        except RPCError as exc:
            mapped = _notebook_codec().map_get_project_error(
                notebook_id,
                exc,
                method_id=GET_PROJECT_METHOD,
            )
            if mapped is exc:
                raise
            raise mapped from exc

    async def list(self) -> builtins.list[Notebook]:
        """List Android projects in the server's recent-first order."""
        # evidence: docs/android/proto-evidence-ledger.md#field-ledger
        proto = _read_proto()
        request = proto.ListRecentlyViewedProjectsRequest(
            include_own_projects=True,
            include_audio_overview_ids=True,
        )
        response = await self._transport.unary(
            LIST_RECENT_PROJECTS_METHOD,
            request,
            replay_safe=True,
            response_type=proto.ListRecentlyViewedProjectsResponse,
        )
        return [
            _notebook_codec().decode_project(project, method_id=LIST_RECENT_PROJECTS_METHOD)
            for project in response.projects
        ]

    async def get(self, notebook_id: str) -> Notebook:
        """Get one Android project, translating only status-5 misses."""
        response = await self._get_project_response(notebook_id)
        return _notebook_codec().decode_project(
            response.project,
            method_id=GET_PROJECT_METHOD,
            include_chat_settings=True,
        )

    async def get_raw(self, notebook_id: str) -> dict[str, Any]:
        """Return the known-field protobuf response as a backend-shaped dict."""
        response = await self._get_project_response(notebook_id)
        # The raw contract is the full response envelope, matching the web
        # method's transport-shaped return rather than silently unwrapping the
        # project only for Android.
        return _notebook_codec().message_to_known_dict(response, method_id=GET_PROJECT_METHOD)

    async def get_source_ids(self, notebook_id: str) -> builtins.list[str]:
        """Return ordered, first-occurrence source IDs from one project read."""
        response = await self._get_project_response(notebook_id)
        # Validate the containing entity before accepting an apparently empty
        # repeated field from a malformed/default response.
        _notebook_codec().decode_project(response.project, method_id=GET_PROJECT_METHOD)
        from .codecs.sources import decode_sources

        return [
            source.id
            for source in decode_sources(
                response.project.sources,
                method_id=GET_PROJECT_METHOD,
                strict=False,
                logger=logger,
            )
        ]

    async def _send_create(self, title: str) -> Notebook:
        # evidence: docs/android/proto-evidence-ledger.md#notebook-method-ledger
        notebook_proto = _notebook_proto()
        read_proto = _read_proto()
        response = await self._transport.unary(
            CREATE_PROJECT_METHOD,
            notebook_proto.CreateProjectRequest(name=title),
            replay_safe=False,
            response_type=read_proto.Project,
        )
        try:
            notebook = _notebook_codec().decode_project(response, method_id=CREATE_PROJECT_METHOD)
        except DecodingError as error:
            raise mark_unconfirmed(error) from None
        self._remember_created_chat_session(notebook)
        return notebook

    async def _send_copy(self, notebook_id: str, title: str) -> Notebook:
        """Send one Android ``CopyProject`` request and decode the new notebook."""
        # evidence: docs/android/proto-evidence-ledger.md#notebook-exact-and-web-derived-field-ledger
        notebook_proto = _notebook_proto()
        read_proto = _read_proto()
        response = await self._transport.unary(
            COPY_PROJECT_METHOD,
            notebook_proto.CopyProjectRequest(
                request_context=_android_request_context(),
                source_project_id=notebook_id,
                title=title,
            ),
            replay_safe=False,
            response_type=read_proto.Project,
        )
        try:
            notebook = _notebook_codec().decode_project(response, method_id=COPY_PROJECT_METHOD)
            if notebook.id == notebook_id:
                raise DecodingError(
                    "CopyProject response reused the source notebook id",
                    method_id=COPY_PROJECT_METHOD,
                )
            if notebook.title != title:
                raise DecodingError(
                    "CopyProject response returned an unexpected notebook title",
                    method_id=COPY_PROJECT_METHOD,
                )
        except DecodingError as error:
            raise mark_unconfirmed(error) from None
        self._remember_created_chat_session(notebook)
        return notebook

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
        mode: int = 4,
        query: str | None = None,
    ) -> builtins.list[PromptSuggestion]:
        if not 1 <= mode <= 10:
            raise ValidationError(f"mode must be in the inclusive range 1..10, got {mode!r}")
        if source_ids is None:
            source_ids = await self.get_source_ids(notebook_id)
        resolved_query = query if query and query.strip() else ""
        notebook_proto = _notebook_proto()
        read_proto = _read_proto()
        response = await self._transport.unary(
            GENERATE_PROMPT_SUGGESTIONS_METHOD,
            notebook_proto.GeneratePromptSuggestionsRequest(
                request_context=_android_request_context(),
                project_id=notebook_id,
                source_ids=[read_proto.SourceId(id=source_id) for source_id in source_ids],
                config_id=mode,
                query=resolved_query,
            ),
            replay_safe=True,
            response_type=notebook_proto.GeneratePromptSuggestionsResponse,
        )
        return [
            PromptSuggestion(
                title=_strip_leading_list_marker(item.title),
                prompt=_strip_leading_list_marker(item.prompt),
            )
            for item in response.suggestions
        ]

    async def suggest_next_steps(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
    ) -> builtins.list[NextStepSuggestion]:
        """Grounded follow-up questions over ``NextStepSuggestions`` (#2283).

        Request: ``project_id`` #2 plus optional ``repeated InputSource`` #3 (a
        bare ``SourceId`` at #3 draws ``INVALID_ARGUMENT``); the reply is the
        exact ``NextStepSuggestions`` message. A bogus notebook draws
        ``NOT_FOUND``, mapped to ``NotebookNotFoundError`` here like ``get``.
        """
        if not notebook_id:
            raise ValidationError("notebook_id must not be empty")
        chat_proto = _chat_proto()
        read_proto = _read_proto()
        request = chat_proto.NextStepSuggestionsRequest(project_id=notebook_id)
        if source_ids:
            request.sources.extend(
                _write_proto_sources().InputSource(source_id=read_proto.SourceId(id=source_id))
                for source_id in source_ids
            )
        try:
            response = await self._transport.unary(
                NEXT_STEP_SUGGESTIONS_METHOD,
                request,
                replay_safe=True,
                response_type=_notebook_proto().NextStepSuggestions,
            )
        except (AuthError, RateLimitError, ServerError, NetworkError):
            # ADR-0019: typed transport signals propagate unwrapped.
            raise
        except RPCError as error:
            # The route answers NOT_FOUND for an unknown notebook (live-verified);
            # surface it as the public miss exception like ``get`` does.
            raise _notebook_codec().map_get_project_error(
                notebook_id, error, method_id=NEXT_STEP_SUGGESTIONS_METHOD
            ) from None
        return [
            NextStepSuggestion(question=step.suggestion, type_code=int(step.suggestion_type))
            for step in response.next_steps
            if step.suggestion
        ]

    async def delete(self, notebook_id: str) -> None:
        # The official generated client proves both the exact request FQN and
        # the google.protobuf.Empty response binding.
        try:
            empty_type = _empty_message_type()
            notebook_proto = _notebook_proto()
            await self._transport.unary(
                DELETE_PROJECTS_METHOD,
                notebook_proto.DeleteProjectsRequest(project_ids=[notebook_id]),
                replay_safe=False,
                response_type=empty_type,
            )
        except RPCError as exc:
            # Public notebook deletion is idempotent: an already-absent row is
            # the requested final state. This is status projection, not replay.
            if exc.rpc_code == 5:
                self._created_chat_session_ids.pop(notebook_id, None)
                return
            raise
        self._created_chat_session_ids.pop(notebook_id, None)

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        if title is None and emoji is None:
            raise ValidationError("At least one of title or emoji must be provided")

        # evidence: docs/android/proto-evidence-ledger.md#notebook-exact-and-web-derived-field-ledger
        # ``new_emoji`` is proto3-optional because an explicitly present empty
        # string clears the emoji; omitting it preserves the current value.
        wire_proto = _wire_proto()
        request = wire_proto.WireMutateProjectRequest(project_id=notebook_id)
        change = request.mutations.add().change_property
        if title is not None:
            change.new_title = title
        if emoji is not None:
            change.new_emoji = emoji
        response = await self._transport.unary(
            MUTATE_PROJECT_METHOD,
            request,
            replay_safe=False,
            response_type=_read_proto().Project,
        )
        _notebook_codec().validate_project_identity(
            response,
            notebook_id,
            method_id=MUTATE_PROJECT_METHOD,
        )
        if title is not None and response.title != title:
            raise DecodingError(
                "MutateProject response returned an unexpected notebook title",
                method_id=MUTATE_PROJECT_METHOD,
            )
        if emoji is not None and response.emoji != emoji:
            raise DecodingError(
                "MutateProject response returned an unexpected notebook emoji",
                method_id=MUTATE_PROJECT_METHOD,
            )
        return _notebook_codec().decode_project(response, method_id=MUTATE_PROJECT_METHOD)

    async def get_summary(self, notebook_id: str) -> str:
        response = await self._generate_notebook_guide(notebook_id)
        return (
            _notebook_codec()
            .decode_notebook_guide(
                response,
                method_id=GENERATE_NOTEBOOK_GUIDE_METHOD,
            )
            .summary
        )

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        response = await self._generate_notebook_guide(notebook_id)
        return _notebook_codec().decode_notebook_guide(
            response,
            method_id=GENERATE_NOTEBOOK_GUIDE_METHOD,
        )

    async def _generate_notebook_guide(self, notebook_id: str) -> Any:
        # The exact response subset does not expose captured topic field #2,
        # so only response parsing uses the explicit local wire override.
        notebook_proto = _notebook_proto()
        wire_proto = _wire_proto()
        return await self._transport.unary(
            GENERATE_NOTEBOOK_GUIDE_METHOD,
            notebook_proto.GenerateNotebookGuideRequest(project_id=notebook_id),
            replay_safe=False,
            response_type=wire_proto.WireGenerateNotebookGuideResponse,
        )

    async def remove_from_recent(self, notebook_id: str) -> None:
        """Drop one project from the recently-viewed list.

        The list this mutates holds projects *shared with* the caller;
        ``ListRecentlyViewedProjects`` only surfaces owned projects under the
        separate ``include_own_projects`` flag. Live probing showed the route
        succeeding on a genuinely shared project and returning ``INTERNAL`` for
        an owned one -- the earlier "known-broken route" reading came from
        exercising it against an owned notebook, which it legitimately refuses.

        Web returns success for an owned notebook while leaving it in place, so
        ``INTERNAL`` here is folded into the same no-op rather than surfaced as
        a backend-visible parity break: the postcondition ("not in the
        recently-viewed list") already holds in exactly that case.
        """
        empty_type = _empty_message_type()
        notebook_proto = _notebook_proto()
        try:
            await self._transport.unary(
                REMOVE_RECENTLY_VIEWED_PROJECT_METHOD,
                notebook_proto.RemoveRecentlyViewedProjectRequest(
                    project_id=notebook_id,
                    request_context=_android_request_context(),
                ),
                replay_safe=False,
                response_type=empty_type,
            )
        except RPCError as exc:
            if exc.rpc_code != _INTERNAL_RPC_CODE:
                raise
            logger.debug(
                "RemoveRecentlyViewedProject returned INTERNAL for %s; "
                "treating as the Web no-op for a non-shared project",
                notebook_id,
            )


__all__ = [
    "AndroidNotebooksAPI",
    "COPY_PROJECT_METHOD",
    "CREATE_PROJECT_METHOD",
    "DELETE_PROJECTS_METHOD",
    "GENERATE_NOTEBOOK_GUIDE_METHOD",
    "GENERATE_PROMPT_SUGGESTIONS_METHOD",
    "GET_PROJECT_METHOD",
    "LIST_RECENT_PROJECTS_METHOD",
    "MUTATE_PROJECT_METHOD",
    "REMOVE_RECENTLY_VIEWED_PROJECT_METHOD",
]
