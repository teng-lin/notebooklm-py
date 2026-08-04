"""E2E tests for CollectionsAPI (``client.collections``).

These exercise the real account-level collection lifecycle against a live
account. Collected only under ``-m e2e`` with valid auth (``requires_auth``
skips otherwise).

**Cleanup discipline is critical here:** unlike labels (torn down with their
notebook), collections are *account-level* and are NOT removed by the
``temp_notebook`` teardown. Every test that creates a collection deletes it in a
``finally`` block (``collections.delete`` is idempotent) so a failing test never
leaves orphan collections on the account.

This module is the live-verification harness for the collection wire shapes.
An earlier ``remove_notebooks`` shape (inferred, not captured) and an earlier
``create`` shape both turned out to be silent no-ops on the wire; both were
corrected and independently reverified on four separate accounts, with
credit to contributors tomihe0720 and erricklong85-tech for the captures that
made the fix possible (PR #2009). Run ``pytest tests/e2e/test_collections.py
-m e2e`` against an account with Collections enabled.
"""

import pytest

from notebooklm import Collection, CollectionNotFoundError

from .conftest import requires_auth


@requires_auth
class TestCollectionLifecycle:
    """Create / read / rename / delete a single collection end-to-end."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_create_list_get(self, client):
        """``create`` returns a Collection; ``list``/``get`` find it; misses behave."""
        collection = await client.collections.create("nbpy-e2e Research")
        try:
            assert isinstance(collection, Collection)
            assert collection.id
            assert collection.name == "nbpy-e2e Research"

            collections = await client.collections.list()
            assert all(isinstance(item, Collection) for item in collections)
            assert collection.id in {item.id for item in collections}

            fetched = await client.collections.get(collection.id)
            assert fetched.id == collection.id

            assert await client.collections.get_or_none("missing") is None
            with pytest.raises(CollectionNotFoundError):
                await client.collections.get("missing")
        finally:
            await client.collections.delete(collection.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_rename(self, client):
        """``rename`` changes the name."""
        collection = await client.collections.create("nbpy-e2e Old")
        try:
            renamed = await client.collections.rename(collection.id, "nbpy-e2e New")
            assert isinstance(renamed, Collection)
            assert renamed.name == "nbpy-e2e New"
        finally:
            await client.collections.delete(collection.id)


@requires_auth
class TestCollectionMembership:
    """Add a notebook to a collection, expand it back, then un-assign it."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_add_expand_remove(self, client, temp_notebook):
        """``add_notebooks`` → ``notebooks`` → ``remove_notebooks`` round-trip."""
        collection = await client.collections.create("nbpy-e2e Members")
        try:
            updated = await client.collections.add_notebooks(collection.id, [temp_notebook.id])
            assert isinstance(updated, Collection)
            assert temp_notebook.id in updated.notebook_ids

            # notebooks() expands membership back to Notebook objects.
            members = await client.collections.notebooks(collection.id)
            assert temp_notebook.id in {nb.id for nb in members}

            # Un-assign — the notebook must leave the collection but must NOT
            # be deleted from the account.
            await client.collections.remove_notebooks(collection.id, [temp_notebook.id])
            after = await client.collections.get(collection.id)
            assert temp_notebook.id not in after.notebook_ids
            assert temp_notebook.id in {nb.id for nb in await client.notebooks.list()}
        finally:
            await client.collections.delete(collection.id)


@requires_auth
class TestCollectionDelete:
    """Delete semantics: removal from the list + idempotent re-delete."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_delete_removes_collection(self, client):
        """``delete`` removes the collection from ``list``."""
        collection = await client.collections.create("nbpy-e2e Temporary")
        try:
            result = await client.collections.delete(collection.id)
            assert result is None

            collections = await client.collections.list()
            assert collection.id not in {item.id for item in collections}
        finally:
            # Idempotent: a no-op if the delete above already succeeded, but
            # ensures an early-failing assertion never orphans the collection.
            await client.collections.delete(collection.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_delete_absent_is_noop(self, client):
        """Deleting an already-absent collection id is a no-op (no raise)."""
        result = await client.collections.delete("nonexistent_collection_id")
        assert result is None


@requires_auth
class TestCollectionsAPIAttributes:
    """Tests for CollectionsAPI availability on the client."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_client_has_collections_api(self, client):
        """The client exposes ``collections`` with the full method surface."""
        assert hasattr(client, "collections")
        for method in (
            "list",
            "get",
            "get_or_none",
            "notebooks",
            "create",
            "rename",
            "add_notebooks",
            "remove_notebooks",
            "delete",
        ):
            assert hasattr(client.collections, method), f"collections.{method} missing"
