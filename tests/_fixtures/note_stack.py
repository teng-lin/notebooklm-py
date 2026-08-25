"""Production-shaped semantic/legacy note wiring for focused tests."""

from __future__ import annotations

from notebooklm._mind_map import LegacyNoteBackedService, NoteBackedMindMapService
from notebooklm._note_service import NoteService
from notebooklm._notes import NotesAPI
from notebooklm._web.backend import WebRpcBackend

from .fake_core import FakeSession


def make_note_stack(
    core: FakeSession,
) -> tuple[NoteService, LegacyNoteBackedService, NoteBackedMindMapService, NotesAPI]:
    """Mirror the production split while sharing one recording executor."""

    backend = WebRpcBackend(core.rpc_executor)
    notes = NoteService(backend)
    legacy = LegacyNoteBackedService(core)
    mind_maps = NoteBackedMindMapService(legacy)
    return notes, legacy, mind_maps, NotesAPI(notes=notes, mind_maps=mind_maps)


__all__ = ["make_note_stack"]
