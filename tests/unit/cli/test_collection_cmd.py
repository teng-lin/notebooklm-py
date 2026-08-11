"""Tests for the ``collection`` CLI command group.

Drives the thin Click handlers in ``cli/collection_cmd.py`` via ``CliRunner``,
injecting a mock client through ``ctx.obj`` (``inject_client``). Collections are
account-level, so — unlike ``label`` — the commands take no ``-n/--notebook``
option; membership args are notebooks (resolved against the conftest ``nb_*``
stub).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Collection

from .conftest import create_mock_client, inject_client


class MockNotebook:
    def __init__(self, id: str, title: str) -> None:
        self.id = id
        self.title = title


def _client_with_collections(*, collections=None):
    client = create_mock_client()
    client.collections = AsyncMock()
    client.collections.list = AsyncMock(return_value=collections or [])
    return client


def _run(runner, argv, client):
    return runner.invoke(cli, argv, obj=inject_client(client), catch_exceptions=False)


# ---------------------------------------------------------------------------
# collection list
# ---------------------------------------------------------------------------


def test_collection_list_json(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [
        Collection(id="colaaa111", name="Research", emoji="📁", notebook_ids=["n1", "n2"])
    ]
    client = _client_with_collections(collections=collections)

    result = _run(runner, ["collection", "list", "--json"], client)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    item = payload["collections"][0]
    assert item["id"] == "colaaa111"
    assert item["notebook_count"] == 2


def test_collection_list_human(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [Collection(id="colaaa111", name="Research", emoji="📁", notebook_ids=["n1"])]
    client = _client_with_collections(collections=collections)

    result = _run(runner, ["collection", "list"], client)

    assert result.exit_code == 0, result.output
    assert "Research" in result.output


# ---------------------------------------------------------------------------
# collection notebooks
# ---------------------------------------------------------------------------


def test_collection_notebooks_delegates(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [Collection(id="colaaa111", name="Research", notebook_ids=["nb_123"])]
    client = _client_with_collections(collections=collections)
    client.collections.notebooks = AsyncMock(return_value=[MockNotebook("nb_123", "First")])

    result = _run(runner, ["collection", "notebooks", "colaaa111", "--json"], client)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["collection_id"] == "colaaa111"
    assert payload["notebooks"] == [{"id": "nb_123", "title": "First"}]


# ---------------------------------------------------------------------------
# collection create / rename
# ---------------------------------------------------------------------------


def test_collection_create_human(runner, mock_auth, mock_fetch_tokens) -> None:
    client = _client_with_collections()
    client.collections.create = AsyncMock(
        return_value=Collection(id="colnew111", name="Research Q3")
    )

    result = _run(runner, ["collection", "create", "Research Q3"], client)

    assert result.exit_code == 0, result.output
    assert "Created collection" in result.output
    client.collections.create.assert_awaited_once_with("Research Q3")


def test_collection_rename_resolves_then_renames(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [Collection(id="colaaa111", name="Old", emoji="📁")]
    client = _client_with_collections(collections=collections)
    client.collections.rename = AsyncMock(
        return_value=Collection(id="colaaa111", name="New", emoji="📁")
    )

    result = _run(runner, ["collection", "rename", "colaaa111", "New", "--json"], client)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "New"
    client.collections.rename.assert_awaited_once_with("colaaa111", "New")


def test_collection_rename_by_name(runner, mock_auth, mock_fetch_tokens) -> None:
    # Exact-name resolution to the id, then rename.
    collections = [Collection(id="colaaa111", name="Old")]
    client = _client_with_collections(collections=collections)
    client.collections.rename = AsyncMock(return_value=Collection(id="colaaa111", name="New"))

    result = _run(runner, ["collection", "rename", "Old", "New"], client)

    assert result.exit_code == 0, result.output
    client.collections.rename.assert_awaited_once_with("colaaa111", "New")


# ---------------------------------------------------------------------------
# collection add / remove
# ---------------------------------------------------------------------------


def test_collection_add_resolves_notebooks(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [Collection(id="colaaa111", name="Research")]
    client = _client_with_collections(collections=collections)
    client.collections.add_notebooks = AsyncMock(
        return_value=Collection(id="colaaa111", name="Research", notebook_ids=["nb_123"])
    )

    result = _run(runner, ["collection", "add", "colaaa111", "nb_123", "--json"], client)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["added_notebook_ids"] == ["nb_123"]
    client.collections.add_notebooks.assert_awaited_once_with("colaaa111", ["nb_123"])


def test_collection_add_multi_notebook_resolves_with_one_list(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    # The whole point of _resolve_notebook_ids: N refs → ONE notebooks.list().
    collections = [Collection(id="colaaa111", name="Research")]
    client = _client_with_collections(collections=collections)
    client.collections.add_notebooks = AsyncMock(
        return_value=Collection(id="colaaa111", name="Research", notebook_ids=["nb_123", "nb_456"])
    )

    result = _run(runner, ["collection", "add", "colaaa111", "nb_123", "nb_456"], client)

    assert result.exit_code == 0, result.output
    client.notebooks.list.assert_awaited_once()  # not once-per-ref
    client.collections.add_notebooks.assert_awaited_once_with("colaaa111", ["nb_123", "nb_456"])


def test_collection_remove_human(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [Collection(id="colaaa111", name="Research", notebook_ids=["nb_123"])]
    client = _client_with_collections(collections=collections)
    client.collections.remove_notebooks = AsyncMock(
        return_value=Collection(id="colaaa111", name="Research")
    )

    result = _run(runner, ["collection", "remove", "colaaa111", "nb_123"], client)

    assert result.exit_code == 0, result.output
    assert "Removed 1 notebook(s)" in result.output
    client.collections.remove_notebooks.assert_awaited_once_with("colaaa111", ["nb_123"])


# ---------------------------------------------------------------------------
# collection delete
# ---------------------------------------------------------------------------


def test_collection_delete_yes_json(runner, mock_auth, mock_fetch_tokens) -> None:
    collections = [Collection(id="colaaa111", name="Research")]
    client = _client_with_collections(collections=collections)
    client.collections.delete = AsyncMock(return_value=None)

    result = _run(runner, ["collection", "delete", "colaaa111", "--yes", "--json"], client)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["deleted"] is True
    assert payload["collection_ids"] == ["colaaa111"]
    client.collections.delete.assert_awaited_once_with(["colaaa111"])


def test_collection_delete_multi_ref_resolves_with_one_list(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    # Mirrors test_collection_add_multi_notebook_resolves_with_one_list: N refs
    # -> ONE collections.list(), not one resolve_collection_id() call each.
    collections = [
        Collection(id="colaaa111", name="Research"),
        Collection(id="colbbb222", name="Archive"),
    ]
    client = _client_with_collections(collections=collections)
    client.collections.delete = AsyncMock(return_value=None)

    result = _run(
        runner, ["collection", "delete", "colaaa111", "colbbb222", "--yes", "--json"], client
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["collection_ids"] == ["colaaa111", "colbbb222"]
    client.collections.list.assert_awaited_once()  # not once-per-ref
    client.collections.delete.assert_awaited_once_with(["colaaa111", "colbbb222"])


def test_collection_delete_json_without_yes_is_confirm_required(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    collections = [Collection(id="colaaa111", name="Research")]
    client = _client_with_collections(collections=collections)
    client.collections.delete = AsyncMock(return_value=None)

    result = _run(runner, ["collection", "delete", "colaaa111", "--json"], client)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "CONFIRM_REQUIRED"
    client.collections.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# resolution errors
# ---------------------------------------------------------------------------


def test_collection_rename_not_found_json_envelope(runner, mock_auth, mock_fetch_tokens) -> None:
    client = _client_with_collections(collections=[])  # nothing to resolve against

    result = _run(runner, ["collection", "rename", "nope", "New", "--json"], client)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "NOT_FOUND"
