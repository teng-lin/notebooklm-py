"""Unit tests for the transport-neutral ``notebooklm._app.notes`` core.

These pin the Click-free note workflows at the ``_app`` boundary with a
``MagicMock`` client + injected partial-id resolvers (the CLI normally injects
``cli.resolve.resolve_notebook_id`` / ``resolve_note_id``):

* ``create`` consuming the typed facade (``notes.create`` returns a ``Note``;
  facade failures propagate as exceptions — no raw-shape extraction),
* the ``created`` / ``found`` discriminator flags on the result dataclasses,
* the concurrent-delete race (``found=False``) on ``get`` and ``rename`` — the
  **neutral** result shape; the CLI keeps ownership of the #1247 NOT_FOUND /
  exit-1 envelope mapping,
* the content-preserving ``rename`` (carries the fetched note content through
  the update),
* ``save`` passing ``None`` through for "leave unchanged".

The CLI tests keep the NOT_FOUND exit-code contract + the ``--json`` envelopes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.notes import (
    NoteCreateResult,
    NoteGetResult,
    NoteRenameResult,
    NoteSaveResult,
    execute_note_create,
    execute_note_delete,
    execute_note_get,
    execute_note_rename,
    execute_note_save,
    resolve_note_for_delete,
)
from notebooklm.exceptions import RPCError
from notebooklm.types import Note


def _client() -> MagicMock:
    client = MagicMock()
    client.notes = MagicMock()
    return client


async def _resolve_nb(_client, nb_id, *, json_output=False):
    return nb_id


async def _resolve_note(_client, _nb_id, note_id, *, json_output=False):
    return note_id


# ---------------------------------------------------------------------------
# note create — typed facade contract (notes.create returns a Note)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_note_create_returns_typed_note_result() -> None:
    """``create`` trusts the typed facade: ``note_id`` is the Note's id.

    Regression for the dead-decoder bug: a leftover raw-shape extractor
    returned ``None`` for every typed ``Note``, so ``note create`` reported
    failure on every success.
    """
    client = _client()
    note = Note(id="note_new", notebook_id="nb_1", title="Title", content="Body")
    client.notes.create = AsyncMock(return_value=note)

    result = await execute_note_create(
        client, "nb_1", "Title", "Body", resolve_notebook_id=_resolve_nb
    )

    assert isinstance(result, NoteCreateResult)
    assert result.notebook_id == "nb_1"
    assert result.title == "Title"
    assert result.note_id == "note_new"
    assert result.raw is note
    client.notes.create.assert_awaited_once_with("nb_1", "Title", "Body")


@pytest.mark.asyncio
async def test_execute_note_create_facade_exception_propagates() -> None:
    """A facade failure raises out of ``execute_note_create`` — no soft-failure result.

    The facade raises on degenerate shapes (it never returns ``None``), so the
    ``_app`` core must let that exception propagate to the adapter's standard
    error handler instead of swallowing it into a ``created=False`` envelope.
    """
    client = _client()
    client.notes.create = AsyncMock(side_effect=RPCError("boom"))

    with pytest.raises(RPCError, match="boom"):
        await execute_note_create(client, "nb_1", "Title", "Body", resolve_notebook_id=_resolve_nb)


# ---------------------------------------------------------------------------
# note get — found flag + concurrent-delete race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_note_get_found() -> None:
    client = _client()
    note = Note(id="note_1", notebook_id="nb_1", title="T", content="C")
    client.notes.get_or_none = AsyncMock(return_value=note)

    result = await execute_note_get(
        client,
        "nb_1",
        "note_1",
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )

    assert isinstance(result, NoteGetResult)
    assert result.found is True
    assert result.note is note
    client.notes.get_or_none.assert_awaited_once_with("nb_1", "note_1")


@pytest.mark.asyncio
async def test_execute_note_get_race_reports_not_found_without_raising() -> None:
    """A row that vanished between resolve and get → ``found=False`` (no raise)."""
    client = _client()
    client.notes.get_or_none = AsyncMock(return_value=None)

    result = await execute_note_get(
        client,
        "nb_1",
        "note_1",
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )

    assert result.found is False
    assert result.note is None


# ---------------------------------------------------------------------------
# note save — passes None through for "leave unchanged"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_note_save_passes_fields_through() -> None:
    client = _client()
    client.notes.update = AsyncMock(return_value=None)

    result = await execute_note_save(
        client,
        "nb_1",
        "note_1",
        title="New title",
        content=None,
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )

    assert isinstance(result, NoteSaveResult)
    assert result == NoteSaveResult(notebook_id="nb_1", note_id="note_1")
    client.notes.update.assert_awaited_once_with("nb_1", "note_1", content=None, title="New title")


# ---------------------------------------------------------------------------
# note rename — content-preserving + race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_note_rename_preserves_content() -> None:
    client = _client()
    note = Note(id="note_1", notebook_id="nb_1", title="Old", content="KEEP ME")
    client.notes.get_or_none = AsyncMock(return_value=note)
    client.notes.update = AsyncMock(return_value=None)

    result = await execute_note_rename(
        client,
        "nb_1",
        "note_1",
        "New title",
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )

    assert isinstance(result, NoteRenameResult)
    assert result.found is True
    assert result.new_title == "New title"
    # The fetched content is carried through verbatim.
    client.notes.update.assert_awaited_once_with(
        "nb_1", "note_1", content="KEEP ME", title="New title"
    )


@pytest.mark.asyncio
async def test_execute_note_rename_empty_content_normalized() -> None:
    """A note with ``content=None`` renames with an empty-string body, not None.

    ``Note.content`` is typed ``str`` but the RPC/facade can yield ``None`` at
    runtime (the ``note.content or ""`` guard in ``_app`` exists for exactly
    this), so the field is set to ``None`` post-construction to pin that path.
    """
    client = _client()
    note = Note(id="note_1", notebook_id="nb_1", title="Old", content="")
    note.content = None  # type: ignore[assignment]
    client.notes.get_or_none = AsyncMock(return_value=note)
    client.notes.update = AsyncMock(return_value=None)

    result = await execute_note_rename(
        client,
        "nb_1",
        "note_1",
        "New title",
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )

    assert result.found is True
    client.notes.update.assert_awaited_once_with("nb_1", "note_1", content="", title="New title")


@pytest.mark.asyncio
async def test_execute_note_rename_race_reports_not_found_and_skips_update() -> None:
    client = _client()
    client.notes.get_or_none = AsyncMock(return_value=None)
    client.notes.update = AsyncMock(return_value=None)

    result = await execute_note_rename(
        client,
        "nb_1",
        "note_1",
        "New title",
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )

    assert result.found is False
    assert result.new_title == "New title"
    client.notes.update.assert_not_called()


# ---------------------------------------------------------------------------
# note delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_note_for_delete_returns_pair() -> None:
    client = _client()
    nb_id, note_id = await resolve_note_for_delete(
        client,
        "nb_part",
        "note_part",
        resolve_notebook_id=_resolve_nb,
        resolve_note_id=_resolve_note,
    )
    assert (nb_id, note_id) == ("nb_part", "note_part")


@pytest.mark.asyncio
async def test_execute_note_delete_delegates_to_client() -> None:
    client = _client()
    client.notes.delete = AsyncMock(return_value=None)
    await execute_note_delete(client, "nb_1", "note_1")
    client.notes.delete.assert_awaited_once_with("nb_1", "note_1")
