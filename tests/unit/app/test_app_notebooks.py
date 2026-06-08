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


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_notebook_create_projects_notebook() -> None:
    client = _client()
    notebook = Notebook(id="nb_new", title="My notebook")
    client.notebooks.create = AsyncMock(return_value=notebook)

    result = await execute_notebook_create(client, "My notebook")

    assert isinstance(result, NotebookCreateResult)
    assert result.notebook is notebook
    client.notebooks.create.assert_awaited_once_with("My notebook")


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
