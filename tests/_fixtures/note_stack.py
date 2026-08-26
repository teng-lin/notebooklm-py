"""Production-shaped semantic note wiring for focused tests."""

from __future__ import annotations

from notebooklm._notes import NotesAPI
from notebooklm._semantic.services.note import NoteService
from notebooklm._web.backend import WebRpcBackend

from .fake_core import FakeSession


def make_note_stack(core: FakeSession) -> tuple[NoteService, NotesAPI]:
    """Mirror the production wiring while sharing one recording executor.

    P10 R4.2 collapsed the split this helper used to mirror: the deferred raw
    note-row service and its mind-map adapter are gone, so the facade and every
    note-backed mind-map path run on the one semantic service.
    """

    notes = NoteService(WebRpcBackend(core.rpc_executor))
    return notes, NotesAPI(notes=notes)


__all__ = ["make_note_stack"]
