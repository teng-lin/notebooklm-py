"""Manual live conformance for the complete Android ``NotesAPI`` manifest.

This test is intentionally opt-in.  It mutates a disposable notebook through
both front doors and requires an explicitly selected profile containing both
Web cookies and the durable master token used by the Android bearer provider.

Run it twice with an isolated profile to validate the private adapter::

    NOTEBOOKLM_PROFILE=agent-b8p-notes \
    NOTEBOOKLM_ANDROID_NOTES_CONFORMANCE=1 \
      uv run pytest tests/e2e/test_android_notes_conformance.py -m e2e -vv

The eight-method direct Notes manifest covered here is ``list``, ``get``, ``get_or_none``,
``create``, ``update``, ``delete``, ``list_mind_maps``, and
``delete_mind_map``. The Web backend seeds the cross-backend note-backed map
because generation belongs to the separate ``MindMapsAPI`` namespace; the
selected Android ``client.mind_maps`` surface also supports native generation.
This focused probe validates the direct Notes adapter's persisted id/content
projection and retained cross-backend tombstone differences; it is not the
only coverage supporting the already-public Android Notes namespace.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from notebooklm import NotebookLMClient, NoteNotFoundError, RateLimitError
from notebooklm._android.auth import BearerProvider
from notebooklm._android.notes import AndroidNotesAPI
from notebooklm._android.session import AndroidSession
from notebooklm._auth.mint_service import MintService
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._client_metrics import ClientMetrics
from notebooklm._notes import NotesAPI
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.paths import get_storage_path

from .conftest import requires_auth

_OPT_IN_ENV = "NOTEBOOKLM_ANDROID_NOTES_CONFORMANCE"
_PROFILE_ENV = "NOTEBOOKLM_PROFILE"
_TITLE_PREFIX = "e2e-android-notes"
_NOTES_MANIFEST = {
    "create",
    "delete",
    "delete_mind_map",
    "get",
    "get_or_none",
    "list",
    "list_mind_maps",
    "update",
}

pytestmark = [
    requires_auth,
    pytest.mark.skipif(
        os.environ.get(_OPT_IN_ENV) != "1",
        reason=f"manual Android Notes conformance requires {_OPT_IN_ENV}=1",
    ),
]


@dataclass
class _CreatedResources:
    """IDs returned by successful creates, retained through final cleanup."""

    run_prefix: str
    notebook_ids: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    note_ids: set[str] = field(default_factory=set)
    mind_map_ids: set[str] = field(default_factory=set)


def _selected_storage_path(auth_tokens: object) -> Path:
    """Resolve one explicit profile and reject an ambiguous auth route."""

    profile = os.environ.get(_PROFILE_ENV)
    if not profile:
        pytest.skip(
            f"set {_PROFILE_ENV} to an isolated profile; the implicit default is not allowed"
        )

    selected = get_storage_path(profile=profile)
    token_storage = getattr(auth_tokens, "storage_path", None)
    if token_storage is None:
        pytest.skip("Android Notes conformance requires a file-backed selected profile")
    if Path(token_storage).resolve() != selected.resolve():
        pytest.fail(
            "Web auth did not resolve to the explicitly selected Android conformance profile"
        )
    if ProfileStore(selected).read_master_token() is None:
        pytest.skip(
            "selected profile has no durable master token; run "
            "`notebooklm login --master-token` for that profile"
        )
    return selected


@contextlib.asynccontextmanager
async def _open_android_notes(storage_path: Path) -> AsyncIterator[AndroidNotesAPI]:
    """Open a production-shaped Android transport graph for this one namespace."""

    bearer = BearerProvider(ProfileStore(storage_path), MintService())
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=4,
    )
    session = AndroidSession(bearer, supervisor, timeout=30.0)
    lifecycle = ClientLifecycle(
        supervisor=supervisor,
        transports=(session,),
        loop_participants=(supervisor, bearer, session),
    )
    notes = AndroidNotesAPI(
        session,
        deletion_poll_delays=(0.0, 0.1, 0.25, 0.5, 1.0, 2.0),
    )

    await lifecycle.open()
    try:
        yield notes
    finally:
        await lifecycle.close(drain=True, drain_timeout=30.0)


async def _cleanup_registered(web: NotebookLMClient, created: _CreatedResources) -> None:
    """Best-effort ID cleanup followed by a mandatory exact-prefix sweep."""

    for notebook_id in tuple(created.notebook_ids):
        for mind_map_id in tuple(created.mind_map_ids):
            with contextlib.suppress(Exception):
                await web.notes.delete_mind_map(notebook_id, mind_map_id)
        for note_id in tuple(created.note_ids):
            with contextlib.suppress(Exception):
                await web.notes.delete(notebook_id, note_id)
        with contextlib.suppress(Exception):
            await web.notebooks.delete(notebook_id)

    # A create can persist remotely before its response is decoded.  The run's
    # unique title is therefore the final authority for finding an unregistered
    # notebook whose child IDs could never be returned to this test.
    for notebook in await web.notebooks.list():
        if notebook.title.startswith(created.run_prefix):
            await web.notebooks.delete(notebook.id)

    leaked = [
        notebook.id
        for notebook in await web.notebooks.list()
        if notebook.title.startswith(created.run_prefix)
    ]
    assert leaked == [], f"disposable Android Notes notebooks leaked: {leaked!r}"


@pytest.mark.asyncio
async def test_complete_android_notes_manifest_records_cross_backend_boundaries(
    auth_tokens,
) -> None:
    """Exercise all eight methods and retain every known parity boundary."""

    public_methods = {
        name
        for name, member in vars(NotesAPI).items()
        if not name.startswith("_") and callable(member)
    }
    assert public_methods == _NOTES_MANIFEST

    storage_path = _selected_storage_path(auth_tokens)
    run_prefix = f"{_TITLE_PREFIX}-{uuid4().hex[:12]}"
    created = _CreatedResources(run_prefix=run_prefix)

    async with NotebookLMClient(
        auth_tokens,
        storage_path=storage_path,
        backend="web",
    ) as web:
        try:
            notebook = await web.notebooks.create(f"{run_prefix}-notebook")
            created.notebook_ids.add(notebook.id)

            source = await web.sources.add_text(
                notebook.id,
                title=f"{run_prefix}-source",
                content=(
                    f"Disposable conformance material for {run_prefix}. "
                    "Ada Lovelace described an analytical engine and software instructions."
                ),
                wait=False,
            )
            created.source_ids.add(source.id)
            await web.sources.wait_until_ready(notebook.id, source.id)

            async with _open_android_notes(storage_path) as android:
                note = await android.create(
                    notebook.id,
                    title=f"{run_prefix}-note",
                    content=f"original content {run_prefix}",
                )
                created.note_ids.add(note.id)

                # list / get / get_or_none: compare only public Note semantics.
                android_list = {item.id: item for item in await android.list(notebook.id)}
                web_list = {item.id: item for item in await web.notes.list(notebook.id)}
                assert note.id in android_list
                assert note.id in web_list
                assert (
                    android_list[note.id].title,
                    android_list[note.id].content,
                ) == (
                    web_list[note.id].title,
                    web_list[note.id].content,
                )

                android_get = await android.get(notebook.id, note.id)
                web_get = await web.notes.get(notebook.id, note.id)
                assert (android_get.id, android_get.title, android_get.content) == (
                    web_get.id,
                    web_get.title,
                    web_get.content,
                )
                assert await android.get_or_none(notebook.id, note.id) == android_get

                missing_id = f"{run_prefix}-missing"
                assert await android.get_or_none(notebook.id, missing_id) is None
                assert await web.notes.get_or_none(notebook.id, missing_id) is None
                with pytest.raises(NoteNotFoundError):
                    await android.get(notebook.id, missing_id)
                with pytest.raises(NoteNotFoundError):
                    await web.notes.get(notebook.id, missing_id)

                # update: Android's response/read-back must be visible unchanged on Web.
                updated_title = f"{run_prefix}-updated"
                updated_content = f"updated content {run_prefix}"
                assert (
                    await android.update(
                        notebook.id,
                        note.id,
                        content=updated_content,
                        title=updated_title,
                    )
                    is None
                )
                android_updated = await android.get(notebook.id, note.id)
                web_updated = await web.notes.get(notebook.id, note.id)
                assert (android_updated.title, android_updated.content) == (
                    updated_title,
                    updated_content,
                )
                assert (web_updated.title, web_updated.content) == (
                    updated_title,
                    updated_content,
                )

                # Generate through the Web namespace; the Android Notes surface
                # has no generation method and must not guess one.
                try:
                    generated = await web.artifacts.generate_mind_map(
                        notebook.id,
                        source_ids=[source.id],
                        instructions=f"Use {run_prefix}-map as the root name.",
                    )
                except RateLimitError as exc:
                    pytest.skip(f"mind-map generation was rate limited: {exc}")
                assert generated.note_id
                map_id = generated.note_id
                created.mind_map_ids.add(map_id)

                # list_mind_maps: Android admits the exact legacy [id, content]
                # shape. Current Web rows nest content inside their raw
                # metadata envelope, so compare the decoded content rather
                # than claiming byte-for-byte raw-row parity.
                android_maps = await android.list_mind_maps(notebook.id)
                web_maps = await web.notes.list_mind_maps(notebook.id)
                android_map = next(row for row in android_maps if row[0] == map_id)
                web_map = next(row for row in web_maps if row[0] == map_id)
                assert android_map[0] == web_map[0]
                assert android_map[1] == web.notes._extract_content(web_map)
                assert len(android_map) == 2
                assert map_id not in {item.id for item in await android.list(notebook.id)}
                assert map_id not in {item.id for item in await web.notes.list(notebook.id)}

                # delete_mind_map is kind-safe: an ordinary note ID is already
                # absent from the map projection and must leave the note intact.
                assert await android.delete_mind_map(notebook.id, note.id) is None
                assert await android.get(notebook.id, note.id) == android_updated
                assert await web.notes.get(notebook.id, note.id) == web_updated

                assert await android.delete_mind_map(notebook.id, map_id) is None
                assert map_id not in {row[0] for row in await android.list_mind_maps(notebook.id)}
                assert map_id not in {row[0] for row in await web.notes.list_mind_maps(notebook.id)}
                assert await android.delete_mind_map(notebook.id, map_id) is None

                # delete: Android reports absence and a second delete preserves
                # its idempotent-None contract. Web exposes the same persisted
                # soft-delete tombstone through exact-id get_or_none even
                # though both list projections exclude it. This is a retained
                # substitution blocker, not a value Android may synthesize.
                assert await android.delete(notebook.id, note.id) is None
                assert await android.get_or_none(notebook.id, note.id) is None
                web_tombstone = await web.notes.get_or_none(notebook.id, note.id)
                assert web_tombstone is not None
                assert (
                    web_tombstone.id,
                    web_tombstone.title,
                    web_tombstone.content,
                ) == (note.id, "", "")
                assert note.id not in {item.id for item in await android.list(notebook.id)}
                assert note.id not in {item.id for item in await web.notes.list(notebook.id)}
                assert await android.delete(notebook.id, note.id) is None
        finally:
            await _cleanup_registered(web, created)
