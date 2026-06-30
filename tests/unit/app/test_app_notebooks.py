"""Unit tests for the transport-neutral ``notebooklm._app.notebooks`` core.

These pin the Click-free notebook workflows at the ``_app`` boundary with a
``MagicMock`` client + an injected partial-id resolver (the CLI normally
injects ``cli.resolve.resolve_notebook_id``):

* ``create`` / ``delete`` / ``rename`` / ``describe`` (summary) / ``metadata``
  executors delegate to the right ``client.notebooks`` RPC and project the typed
  result dataclasses,
* the resolver is threaded through ``rename`` / ``describe`` / ``metadata`` and
  the resolved id flows into the downstream RPC,
* ``describe`` tolerates a ``None`` description (the CLI renders both views from
  the typed field).

The CLI tests keep ownership of the ``--use`` context side effect, the
serializers, the ``--json`` envelopes, and the error-category exit codes (the
generic error classification is covered by ``app/test_app_errors.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.notebooks import (
    NotebookCreateResult,
    NotebookDescribeResult,
    NotebookMetadataResult,
    NotebookRenameResult,
    execute_notebook_create,
    execute_notebook_delete,
    execute_notebook_describe,
    execute_notebook_metadata,
    execute_notebook_rename,
)
from notebooklm.exceptions import NotebookNotFoundError
from notebooklm.types import (
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    SuggestedTopic,
)


def _client() -> MagicMock:
    client = MagicMock()
    client.notebooks = MagicMock()
    return client


async def _resolve_nb(_client, nb_id, *, json_output=False):
    """Identity resolver that prefixes to verify the *resolved* id flows downstream."""
    return f"full_{nb_id}"


_CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_MODIFIED_AT = datetime(2026, 1, 3, 4, 5, 6, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_notebook_create_projects_notebook() -> None:
    client = _client()
    # Both timestamps already populated → the best-effort backfill is a no-op
    # (no wasted GET re-read), so this stays a pure projection assertion.
    notebook = Notebook(
        id="nb_new",
        title="My notebook",
        created_at=_CREATED_AT,
        modified_at=_MODIFIED_AT,
    )
    client.notebooks.create = AsyncMock(return_value=notebook)

    result = await execute_notebook_create(client, "My notebook")

    assert isinstance(result, NotebookCreateResult)
    assert result.notebook is notebook
    client.notebooks.create.assert_awaited_once_with("My notebook")
    client.notebooks.get.assert_not_called()


# The create RPC (CREATE_NOTEBOOK / CCqFvf) returns null created_at/modified_at;
# the core does ONE best-effort GET re-read to backfill just those two slots so
# every adapter (CLI/REST/MCP) surfaces populated timestamps on create (#1705,
# lifting the MCP-only #1699 fix into the transport-neutral home).


@pytest.mark.asyncio
async def test_execute_notebook_create_backfills_null_timestamps() -> None:
    client = _client()
    created = Notebook(id="nb_new", title="New", sources_count=0, is_owner=True)
    client.notebooks.create = AsyncMock(return_value=created)
    # The GET diverges on every non-timestamp field to prove ONLY the two
    # timestamp slots are taken from it; the create stays authoritative.
    client.notebooks.get = AsyncMock(
        return_value=Notebook(
            id="nb_new",
            title="Stale",
            created_at=_CREATED_AT,
            sources_count=9,
            is_owner=False,
            modified_at=_MODIFIED_AT,
        )
    )

    nb = (await execute_notebook_create(client, "New")).notebook

    assert nb.created_at == _CREATED_AT  # backfilled from GET
    assert nb.modified_at == _MODIFIED_AT  # backfilled from GET
    assert nb.title == "New"  # from create, not the divergent GET
    assert nb.sources_count == 0  # from create
    assert nb.is_owner is True  # from create
    client.notebooks.get.assert_awaited_once_with("nb_new")


@pytest.mark.asyncio
async def test_execute_notebook_create_fills_only_the_null_slot() -> None:
    """Per-key + additive: a populated create timestamp is never overwritten."""
    client = _client()
    created = Notebook(id="nb_new", title="New", created_at=_CREATED_AT)  # modified_at None
    client.notebooks.create = AsyncMock(return_value=created)
    client.notebooks.get = AsyncMock(
        return_value=Notebook(
            id="nb_new",
            title="New",
            created_at=datetime(2099, 1, 1, tzinfo=timezone.utc),  # must NOT win
            modified_at=_MODIFIED_AT,
        )
    )

    nb = (await execute_notebook_create(client, "New")).notebook

    assert nb.created_at == _CREATED_AT  # create's value preserved, GET ignored
    assert nb.modified_at == _MODIFIED_AT  # only the null slot filled from GET
    client.notebooks.get.assert_awaited_once_with("nb_new")


@pytest.mark.asyncio
async def test_execute_notebook_create_reread_failure_falls_back() -> None:
    """A failed GET re-read must not fail a successful create (best-effort)."""
    client = _client()
    client.notebooks.create = AsyncMock(return_value=Notebook(id="nb_new", title="New"))
    client.notebooks.get = AsyncMock(side_effect=NotebookNotFoundError("nb_new"))

    nb = (await execute_notebook_create(client, "New")).notebook  # must not raise

    assert nb.created_at is None
    assert nb.modified_at is None
    client.notebooks.get.assert_awaited_once_with("nb_new")


@pytest.mark.asyncio
async def test_execute_notebook_create_reread_still_null_stays_null() -> None:
    """If the GET itself still has null timestamps (propagation lag), stay null."""
    client = _client()
    client.notebooks.create = AsyncMock(return_value=Notebook(id="nb_new", title="New"))
    client.notebooks.get = AsyncMock(return_value=Notebook(id="nb_new", title="New"))

    nb = (await execute_notebook_create(client, "New")).notebook

    assert nb.created_at is None
    assert nb.modified_at is None
    client.notebooks.get.assert_awaited_once_with("nb_new")


@pytest.mark.asyncio
async def test_execute_notebook_create_empty_id_skips_reread() -> None:
    """No id → no ``get("")``; the create result is returned untouched."""
    client = _client()
    client.notebooks.create = AsyncMock(return_value=Notebook(id="", title="New"))
    client.notebooks.get = AsyncMock()

    nb = (await execute_notebook_create(client, "New")).notebook

    assert nb.created_at is None
    client.notebooks.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_notebook_delete_delegates_to_client() -> None:
    client = _client()
    client.notebooks.delete = AsyncMock(return_value=None)

    # ``delete()`` returns None and raises on failure (issue #1211); reaching
    # here without an exception is the success contract.
    await execute_notebook_delete(client, "nb_1")

    client.notebooks.delete.assert_awaited_once_with("nb_1")


@pytest.mark.asyncio
async def test_execute_notebook_delete_propagates_failure() -> None:
    """``delete()`` raises on real failure (issue #1211); the core does not swallow it."""
    client = _client()
    client.notebooks.delete = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await execute_notebook_delete(client, "nb_1")


# ---------------------------------------------------------------------------
# rename — resolver threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_notebook_rename_resolves_then_renames() -> None:
    client = _client()
    client.notebooks.rename = AsyncMock(return_value=None)

    result = await execute_notebook_rename(
        client, "nb_part", "New title", resolve_notebook_id=_resolve_nb
    )

    assert isinstance(result, NotebookRenameResult)
    assert result.notebook_id == "full_nb_part"
    assert result.new_title == "New title"
    # The *resolved* id flows into the rename RPC, not the partial input.
    client.notebooks.rename.assert_awaited_once_with("full_nb_part", "New title")


# ---------------------------------------------------------------------------
# describe (summary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_notebook_describe_returns_description() -> None:
    client = _client()
    description = NotebookDescription(
        summary="A summary",
        suggested_topics=[SuggestedTopic(question="Q?", prompt="Ask Q")],
    )
    client.notebooks.get_description = AsyncMock(return_value=description)

    result = await execute_notebook_describe(client, "nb_part", resolve_notebook_id=_resolve_nb)

    assert isinstance(result, NotebookDescribeResult)
    assert result.notebook_id == "full_nb_part"
    assert result.description is description
    client.notebooks.get_description.assert_awaited_once_with("full_nb_part")


@pytest.mark.asyncio
async def test_execute_notebook_describe_tolerates_none_description() -> None:
    client = _client()
    client.notebooks.get_description = AsyncMock(return_value=None)

    result = await execute_notebook_describe(client, "nb_1", resolve_notebook_id=_resolve_nb)

    assert result.description is None


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_notebook_metadata_returns_metadata() -> None:
    client = _client()
    notebook = Notebook(id="full_nb_part", title="My notebook")
    metadata = NotebookMetadata(notebook=notebook, sources=[])
    client.notebooks.get_metadata = AsyncMock(return_value=metadata)

    result = await execute_notebook_metadata(client, "nb_part", resolve_notebook_id=_resolve_nb)

    assert isinstance(result, NotebookMetadataResult)
    assert result.notebook_id == "full_nb_part"
    assert result.metadata is metadata
    client.notebooks.get_metadata.assert_awaited_once_with("full_nb_part")
