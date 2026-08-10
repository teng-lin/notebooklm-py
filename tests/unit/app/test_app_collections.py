"""Unit tests for the transport-neutral ``notebooklm._app.collections`` core.

Covers the composite ``<id|name>`` :func:`resolve_collection_id` resolver
(id / unambiguous-prefix / exact-name / ambiguity / near-miss) — which, unlike
the label resolver, has no CLI-listing-service re-export, so it is pinned
directly here — plus the collection executors and the
:class:`CollectionResolutionError` attribute contract, at the ``_app`` boundary
with a ``MagicMock`` client (no Click / ``CliRunner``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.collections import (
    CollectionMembershipResult,
    CollectionResolutionError,
    execute_collection_add_notebooks,
    execute_collection_create,
    execute_collection_delete,
    execute_collection_notebooks,
    execute_collection_remove_notebooks,
    execute_collection_rename,
    resolve_collection_id,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import Collection


def _client(collections=None) -> MagicMock:
    client = MagicMock()
    client.collections = MagicMock()
    client.collections.list = AsyncMock(return_value=collections or [])
    return client


# ---------------------------------------------------------------------------
# CollectionResolutionError — typed attribute contract
# ---------------------------------------------------------------------------


def test_resolution_error_carries_code_and_extra() -> None:
    err = CollectionResolutionError("No match", "NOT_FOUND", {"id": "x"})
    assert err.message == "No match"
    assert err.code == "NOT_FOUND"
    assert err.extra == {"id": "x"}
    assert isinstance(err, ValidationError)
    assert "code=NOT_FOUND" in str(err)


# ---------------------------------------------------------------------------
# resolve_collection_id
# ---------------------------------------------------------------------------


async def test_resolve_exact_id() -> None:
    client = _client([Collection(id="colaaa111", name="A")])
    assert await resolve_collection_id(client, "colaaa111") == "colaaa111"


async def test_resolve_unique_prefix() -> None:
    client = _client([Collection(id="colaaa111", name="A")])
    assert await resolve_collection_id(client, "colaaa") == "colaaa111"


async def test_resolve_exact_name() -> None:
    client = _client([Collection(id="colaaa111", name="Research Q3")])
    assert await resolve_collection_id(client, "Research Q3") == "colaaa111"


async def test_resolve_exact_id_wins_over_longer_prefix() -> None:
    client = _client([Collection(id="col1", name="A"), Collection(id="col1234", name="B")])
    # "col1" is a complete id AND a prefix of "col1234" — exact match wins.
    assert await resolve_collection_id(client, "col1") == "col1"


async def test_resolve_ambiguous_prefix_raises_before_name_fallback() -> None:
    client = _client([Collection(id="colaaa111", name="A"), Collection(id="colaaa222", name="B")])
    with pytest.raises(CollectionResolutionError) as exc:
        await resolve_collection_id(client, "colaaa")
    assert exc.value.code == "AMBIGUOUS_ID"
    assert len(exc.value.extra["candidates"]) == 2


async def test_resolve_ambiguous_name_raises() -> None:
    client = _client(
        [Collection(id="colaaa111", name="Dup"), Collection(id="colbbb222", name="Dup")]
    )
    with pytest.raises(CollectionResolutionError) as exc:
        await resolve_collection_id(client, "Dup")
    assert exc.value.code == "AMBIGUOUS_NAME"


async def test_resolve_not_found() -> None:
    client = _client([Collection(id="colaaa111", name="A")])
    with pytest.raises(CollectionResolutionError) as exc:
        await resolve_collection_id(client, "zzz")
    assert exc.value.code == "NOT_FOUND"


async def test_resolve_empty_token_raises_validation_error() -> None:
    client = _client([])
    with pytest.raises(ValidationError):
        await resolve_collection_id(client, "   ")


# ---------------------------------------------------------------------------
# executors
# ---------------------------------------------------------------------------


async def test_execute_create_delegates() -> None:
    client = _client()
    client.collections.create = AsyncMock(return_value=Collection(id="c1", name="New"))
    result = await execute_collection_create(client, "New")
    assert result.id == "c1"
    client.collections.create.assert_awaited_once_with("New")


async def test_execute_rename_delegates() -> None:
    client = _client()
    client.collections.rename = AsyncMock(return_value=Collection(id="c1", name="New"))
    result = await execute_collection_rename(client, "c1", "New")
    assert result.name == "New"
    client.collections.rename.assert_awaited_once_with("c1", "New")


async def test_execute_add_notebooks_projects_membership() -> None:
    client = _client()
    client.collections.add_notebooks = AsyncMock(
        return_value=Collection(id="c1", name="A", notebook_ids=["n1"])
    )
    result = await execute_collection_add_notebooks(client, "c1", ["n1"])
    assert isinstance(result, CollectionMembershipResult)
    assert result.notebook_ids == ["n1"]
    client.collections.add_notebooks.assert_awaited_once_with("c1", ["n1"])


async def test_execute_remove_notebooks_projects_membership() -> None:
    client = _client()
    client.collections.remove_notebooks = AsyncMock(return_value=Collection(id="c1", name="A"))
    result = await execute_collection_remove_notebooks(client, "c1", ["n1"])
    assert result.notebook_ids == ["n1"]
    client.collections.remove_notebooks.assert_awaited_once_with("c1", ["n1"])


async def test_execute_notebooks_delegates() -> None:
    client = _client()
    client.collections.notebooks = AsyncMock(return_value=[])
    await execute_collection_notebooks(client, "c1")
    client.collections.notebooks.assert_awaited_once_with("c1")


async def test_execute_delete_delegates() -> None:
    client = _client()
    client.collections.delete = AsyncMock(return_value=None)
    await execute_collection_delete(client, ["c1", "c2"])
    client.collections.delete.assert_awaited_once_with(["c1", "c2"])
