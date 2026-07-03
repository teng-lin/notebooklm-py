"""In-memory fake :class:`NotebookLMClient` for the REST server tests.

Mirrors the public client namespaces the REST routes touch
(``notebooks`` / ``sources`` / ``chat`` / ``artifacts`` / ``sharing``) with
simple in-memory state — no auth, no network. Tests inject it via
``create_app(client_factory=…)`` and drive the app through a FastAPI
``TestClient``.

State is scriptable per test: pre-seed notebooks/sources/artifacts, override the
poll/get behavior (e.g. return ``None`` for the not-yet-listable window), and
record the calls each namespace received.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from notebooklm._types.artifacts import Artifact, GenerationState, GenerationStatus
from notebooklm._types.chat import AskResult
from notebooklm._types.notebooks import Notebook
from notebooklm._types.notes import Note
from notebooklm._types.research import (
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    SourceGuide,
)
from notebooklm._types.sharing import SharedUser, ShareStatus
from notebooklm._types.sources import Source, SourceFulltext
from notebooklm.exceptions import NotebookNotFoundError, NoteNotFoundError
from notebooklm.rpc.types import ShareAccess, SharePermission, ShareViewLevel, SourceStatus

#: download-spec kind -> internal artifact type-code.
_KIND_CODE = {
    "audio": 1,
    "report": 2,
    "video": 3,
    "mind-map": 5,
    "infographic": 7,
    "slide-deck": 8,
    "data-table": 9,
}


def make_artifact(artifact_id: str, kind: str, *, title: str = "Artifact") -> Artifact:
    """Build a completed :class:`Artifact` of the named download-spec kind."""
    return Artifact(
        id=artifact_id,
        title=title,
        _artifact_type=_KIND_CODE[kind],
        status=3,  # completed
        created_at=datetime.now(timezone.utc),
    )


class FakeNotebooks:
    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def list(self) -> list[Notebook]:
        return list(self._s.notebooks_store.values())

    async def get(self, notebook_id: str) -> Notebook:
        nb = self._s.notebooks_store.get(notebook_id)
        if nb is None:
            raise NotebookNotFoundError(notebook_id)
        return nb

    async def create(self, title: str) -> Notebook:
        nb = Notebook(id=f"nb-{len(self._s.notebooks_store) + 1}", title=title)
        self._s.notebooks_store[nb.id] = nb
        return nb

    async def delete(self, notebook_id: str) -> None:
        # Idempotent-on-missing (the public delete contract).
        self._s.notebooks_store.pop(notebook_id, None)


class FakeSources:
    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        return list(self._s.sources_store.get(notebook_id, {}).values())

    async def get_or_none(self, notebook_id: str, source_id: str) -> Source | None:
        return self._s.sources_store.get(notebook_id, {}).get(source_id)

    async def get_fulltext(
        self, notebook_id: str, source_id: str, *, output_format: str = "text"
    ) -> SourceFulltext:
        content = self._s.fulltext_store.get((notebook_id, source_id), "")
        return SourceFulltext(
            source_id=source_id, title="", content=content, char_count=len(content)
        )

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuide:
        return self._s.guide_store.get((notebook_id, source_id), SourceGuide())

    async def add_url(self, notebook_id: str, url: str) -> Source:
        return self._add(notebook_id, title=url, url=url)

    async def add_text(self, notebook_id: str, title: str, content: str) -> Source:
        return self._add(notebook_id, title=title)

    async def add_file(
        self,
        notebook_id: str,
        path: str,
        mime_type: str | None = None,
        *,
        title: str | None = None,
    ) -> Source:
        self._s.uploaded_paths.append(path)
        return self._add(notebook_id, title=title or "file")

    async def delete(self, notebook_id: str, source_id: str) -> None:
        self._s.sources_store.get(notebook_id, {}).pop(source_id, None)

    def _add(self, notebook_id: str, *, title: str | None, url: str | None = None) -> Source:
        bucket = self._s.sources_store.setdefault(notebook_id, {})
        src = Source(
            id=f"src-{self._s.next_source}",
            title=title,
            url=url,
            status=self._s.new_source_status,
        )
        self._s.next_source += 1
        # The not-yet-listable window: a created source need not appear in
        # get_or_none until the test marks it listable.
        if not self._s.hide_new_sources:
            bucket[src.id] = src
        return src


class FakeNotes:
    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def list(self, notebook_id: str) -> list[Note]:
        return list(self._s.notes_store.get(notebook_id, {}).values())

    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        return self._s.notes_store.get(notebook_id, {}).get(note_id)

    async def get(self, notebook_id: str, note_id: str) -> Note:
        note = await self.get_or_none(notebook_id, note_id)
        if note is None:
            raise NoteNotFoundError(note_id)
        return note

    async def create(self, notebook_id: str, title: str, content: str) -> Note:
        bucket = self._s.notes_store.setdefault(notebook_id, {})
        note = Note(
            id=f"note-{self._s.next_note}",
            notebook_id=notebook_id,
            title=title,
            content=content,
        )
        self._s.next_note += 1
        bucket[note.id] = note
        return note

    async def update(self, notebook_id: str, note_id: str, content: str, title: str) -> None:
        # Mirror the facade: a missing note fails loud (drives the 404 path).
        existing = await self.get_or_none(notebook_id, note_id)
        if existing is None:
            raise NoteNotFoundError(note_id)
        self._s.notes_store[notebook_id][note_id] = Note(
            id=note_id,
            notebook_id=notebook_id,
            title=title,
            content=content,
            created_at=existing.created_at,
        )

    async def delete(self, notebook_id: str, note_id: str) -> None:
        # Idempotent-on-missing (the public delete contract).
        self._s.notes_store.get(notebook_id, {}).pop(note_id, None)


class FakeChat:
    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def ask(
        self, notebook_id: str, question: str, *, conversation_id: str | None = None
    ) -> AskResult:
        if self._s.chat_error is not None:
            raise self._s.chat_error
        self._s.last_ask = {"notebook_id": notebook_id, "conversation_id": conversation_id}
        return AskResult(
            answer=f"answer to: {question}",
            conversation_id=conversation_id or "conv-1",
            turn_number=1,
            is_follow_up=conversation_id is not None,
            raw_response='[["wrb.fr", ... internal wire blob ...]]',
        )

    async def set_mode(self, notebook_id: str, mode: Any) -> None:
        self._s.last_configure = {"notebook_id": notebook_id, "mode": mode}

    async def configure(
        self,
        notebook_id: str,
        *,
        goal: Any = None,
        response_length: Any = None,
        custom_prompt: str | None = None,
    ) -> None:
        self._s.last_configure = {
            "notebook_id": notebook_id,
            "goal": goal,
            "response_length": response_length,
            "custom_prompt": custom_prompt,
        }


class FakeArtifacts:
    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def list(self, notebook_id: str, *args: Any, **kwargs: Any) -> list[Artifact]:
        return list(self._s.artifacts_store.get(notebook_id, {}).values())

    async def poll_status(self, notebook_id: str, task_id: str) -> GenerationStatus:
        state = self._s.poll_states.get((notebook_id, task_id), GenerationState.NOT_FOUND)
        return GenerationStatus(
            task_id=task_id,
            status=state,
            error="boom" if state == GenerationState.FAILED else None,
        )

    async def generate_audio(self, notebook_id: str, **kwargs: Any) -> GenerationStatus:
        task_id = f"task-{self._s.next_task}"
        self._s.next_task += 1
        self._s.poll_states[(notebook_id, task_id)] = GenerationState.PENDING
        return GenerationStatus(task_id=task_id, status=GenerationState.PENDING)

    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: Any = None,
    ) -> str:
        return await self._do_download(output_path)

    async def download_slide_deck(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None, **kwargs: Any
    ) -> str:
        # A format-bearing download kind (output_format → pdf/pptx).
        return await self._do_download(output_path)

    async def _do_download(self, output_path: str) -> str:
        with open(output_path, "wb") as fh:
            fh.write(self._s.download_bytes)
        # download_return_path lets a test force a path OUTSIDE the server's temp
        # dir, exercising the route's served-path safety guard.
        return self._s.download_return_path or output_path


class FakeSharing:
    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def get_status(self, notebook_id: str) -> ShareStatus:
        return self._s.share_status(notebook_id)

    async def set_public(self, notebook_id: str, enable: bool) -> ShareStatus:
        self._s.public_shares[notebook_id] = enable
        return self._s.share_status(notebook_id)

    async def set_view_level(self, notebook_id: str, level: ShareViewLevel) -> ShareStatus:
        self._s.share_view_levels[notebook_id] = level
        return self._s.share_status(notebook_id)

    async def add_user(
        self,
        notebook_id: str,
        email: str,
        *,
        permission: SharePermission,
        notify: bool,
        welcome_message: str,
    ) -> ShareStatus:
        self._s.shared_users.setdefault(notebook_id, {})[email] = SharedUser(
            email=email,
            permission=permission,
        )
        self._s.last_share_notify = notify
        return self._s.share_status(notebook_id)

    async def update_user(
        self, notebook_id: str, email: str, permission: SharePermission
    ) -> ShareStatus:
        return await self.add_user(
            notebook_id,
            email,
            permission=permission,
            notify=False,
            welcome_message="",
        )

    async def remove_user(self, notebook_id: str, email: str) -> ShareStatus:
        self._s.shared_users.get(notebook_id, {}).pop(email, None)
        return self._s.share_status(notebook_id)


class FakeResearch:
    """Scriptable in-memory research surface (start / poll / cancel / import).

    ``start`` records an ``in_progress`` task keyed by the route's ``poll_id``
    (``report_id`` for deep, ``task_id`` for fast); a test drives it to
    ``completed`` via :meth:`FakeClient.set_research_completed`. ``poll`` returns
    the stored task or a typed ``not_found`` sentinel for an unknown id.
    """

    def __init__(self, state: FakeClient) -> None:
        self._s = state

    async def start(
        self, notebook_id: str, query: str, source: str = "web", mode: str = "fast"
    ) -> ResearchStart:
        n = self._s.next_research
        self._s.next_research += 1
        task_id = f"rtask-{n}"
        # ``deep_missing_report_id`` simulates the backend returning a deep start
        # WITHOUT a report_id — the route must fail loud rather than emit the
        # sessionId task_id as a pollable id.
        report_id = (
            f"rreport-{n}" if mode == "deep" and not self._s.deep_missing_report_id else None
        )
        poll_id = report_id or task_id
        self._s.last_research_start = {
            "notebook_id": notebook_id,
            "query": query,
            "source": source,
            "mode": mode,
        }
        # Record the started run as in-progress under its poll id.
        self._s.research_tasks[(notebook_id, poll_id)] = ResearchTask(
            task_id=poll_id, status=ResearchStatus.IN_PROGRESS, query=query
        )
        return ResearchStart(
            task_id=task_id,
            report_id=report_id,
            notebook_id=notebook_id,
            query=query,
            mode=mode,
        )

    async def poll(self, notebook_id: str, task_id: str | None = None) -> ResearchTask:
        if task_id is None:
            return ResearchTask.empty()
        task = self._s.research_tasks.get((notebook_id, task_id))
        if task is None:
            return ResearchTask.not_found(task_id)
        return task

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        # Fire-and-forget: record the call, never raise on an unknown id.
        self._s.cancelled_research.append((notebook_id, run_id))

    async def import_sources(
        self, notebook_id: str, task_id: str, sources: Any
    ) -> list[dict[str, str]]:
        rows = list(sources)
        self._s.imported_research.append((notebook_id, task_id, rows))
        return [
            {"id": f"src-imported-{i}", "title": str(row.get("title", ""))}
            for i, row in enumerate(rows)
        ]


class FakeClient:
    """Scriptable in-memory client mirroring the namespaces the routes use."""

    def __init__(self) -> None:
        self.notebooks_store: dict[str, Notebook] = {}
        self.sources_store: dict[str, dict[str, Source]] = {}
        self.notes_store: dict[str, dict[str, Note]] = {}
        self.artifacts_store: dict[str, dict[str, Artifact]] = {}
        self.poll_states: dict[tuple[str, str], GenerationState] = {}
        self.public_shares: dict[str, bool] = {}
        self.share_view_levels: dict[str, ShareViewLevel] = {}
        self.shared_users: dict[str, dict[str, SharedUser]] = {}
        self.fulltext_store: dict[tuple[str, str], str] = {}
        self.guide_store: dict[tuple[str, str], SourceGuide] = {}
        self.research_tasks: dict[tuple[str, str], ResearchTask] = {}
        self.cancelled_research: list[tuple[str, str]] = []
        self.imported_research: list[tuple[str, str, list[Any]]] = []

        self.new_source_status: SourceStatus = SourceStatus.PROCESSING
        self.hide_new_sources: bool = False
        self.download_bytes: bytes = b"FAKE-ARTIFACT-BYTES"
        self.download_return_path: str | None = None
        self.chat_error: Exception | None = None
        self.last_share_notify: bool | None = None
        self.last_configure: dict[str, Any] | None = None
        self.last_research_start: dict[str, Any] | None = None
        self.deep_missing_report_id: bool = False

        self.next_task = 1
        self.next_source = 1
        self.next_note = 1
        self.next_research = 1
        self.uploaded_paths: list[str] = []
        self.last_ask: dict[str, Any] | None = None

        self.notebooks = FakeNotebooks(self)
        self.sources = FakeSources(self)
        self.notes = FakeNotes(self)
        self.chat = FakeChat(self)
        self.artifacts = FakeArtifacts(self)
        self.sharing = FakeSharing(self)
        self.research = FakeResearch(self)

    def set_research_completed(
        self,
        notebook_id: str,
        poll_id: str,
        *,
        query: str = "q",
        sources: list[dict[str, str]] | None = None,
        report: str = "report body",
    ) -> None:
        """Drive a started research run to ``completed`` with the given sources."""
        src_models = tuple(
            ResearchSource(url=s.get("url", ""), title=s.get("title", "")) for s in (sources or [])
        )
        self.research_tasks[(notebook_id, poll_id)] = ResearchTask(
            task_id=poll_id,
            status=ResearchStatus.COMPLETED,
            query=query,
            sources=src_models,
            report=report,
        )

    def set_research_failed(self, notebook_id: str, poll_id: str, *, query: str = "q") -> None:
        """Drive a started research run to the terminal ``failed`` state."""
        self.research_tasks[(notebook_id, poll_id)] = ResearchTask(
            task_id=poll_id,
            status=ResearchStatus.FAILED,
            query=query,
        )

    def share_status(self, notebook_id: str) -> ShareStatus:
        is_public = self.public_shares.get(notebook_id, False)
        return ShareStatus(
            notebook_id=notebook_id,
            is_public=is_public,
            access=ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED,
            view_level=self.share_view_levels.get(notebook_id, ShareViewLevel.FULL_NOTEBOOK),
            shared_users=list(self.shared_users.get(notebook_id, {}).values()),
            share_url=f"https://notebooklm.google.com/notebook/{notebook_id}"
            if is_public
            else None,
        )
