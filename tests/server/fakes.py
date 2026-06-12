"""In-memory fake :class:`NotebookLMClient` for the REST server tests.

Mirrors the public client namespaces the REST routes touch
(``notebooks`` / ``sources`` / ``chat`` / ``artifacts``) with simple in-memory
state — no auth, no network. Tests inject it via ``create_app(client_factory=…)``
and drive the app through a FastAPI ``TestClient``.

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
from notebooklm._types.sources import Source
from notebooklm.exceptions import NotebookNotFoundError
from notebooklm.rpc.types import SourceStatus

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
        )


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


class FakeClient:
    """Scriptable in-memory client mirroring the namespaces the routes use."""

    def __init__(self) -> None:
        self.notebooks_store: dict[str, Notebook] = {}
        self.sources_store: dict[str, dict[str, Source]] = {}
        self.artifacts_store: dict[str, dict[str, Artifact]] = {}
        self.poll_states: dict[tuple[str, str], GenerationState] = {}

        self.new_source_status: SourceStatus = SourceStatus.PROCESSING
        self.hide_new_sources: bool = False
        self.download_bytes: bytes = b"FAKE-ARTIFACT-BYTES"
        self.download_return_path: str | None = None
        self.chat_error: Exception | None = None

        self.next_task = 1
        self.next_source = 1
        self.uploaded_paths: list[str] = []
        self.last_ask: dict[str, Any] | None = None

        self.notebooks = FakeNotebooks(self)
        self.sources = FakeSources(self)
        self.chat = FakeChat(self)
        self.artifacts = FakeArtifacts(self)
