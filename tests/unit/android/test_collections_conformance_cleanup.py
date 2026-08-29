"""Deterministic coverage for the live Android Collections cleanup guard."""

from __future__ import annotations

import pytest
from tests.e2e._android_collections_cleanup import cleanup_disposable_resources

from notebooklm.types import Collection, Notebook


class _Collections:
    def __init__(self, *items: Collection, undeletable: set[str] | None = None) -> None:
        self.items = {item.id: item for item in items}
        self.undeletable = undeletable or set()
        self.delete_attempts: list[str] = []

    async def list(self) -> list[Collection]:
        return list(self.items.values())

    async def delete(self, collection_id: str) -> None:
        self.delete_attempts.append(collection_id)
        if collection_id in self.undeletable:
            raise RuntimeError("collection delete failed")
        self.items.pop(collection_id, None)


class _Notebooks:
    def __init__(self, *items: Notebook) -> None:
        self.items = {item.id: item for item in items}
        self.delete_attempts: list[str] = []

    async def list(self) -> list[Notebook]:
        return list(self.items.values())

    async def delete(self, notebook_id: str) -> None:
        self.delete_attempts.append(notebook_id)
        self.items.pop(notebook_id, None)


class _Client:
    def __init__(self, collections: _Collections, notebooks: _Notebooks) -> None:
        self.collections = collections
        self.notebooks = notebooks


async def test_cleanup_sweeps_prefix_matches_when_create_response_was_not_recorded() -> None:
    prefix = "nbpy-android-collections-run"
    collection = Collection(id="collection-id", name=f"{prefix}-collection")
    notebook = Notebook(id="notebook-id", title=f"{prefix}-notebook")
    client = _Client(_Collections(collection), _Notebooks(notebook))

    await cleanup_disposable_resources(
        client,  # type: ignore[arg-type]
        run_prefix=prefix,
        collection_ids=set(),
        notebook_ids=set(),
    )

    assert client.collections.items == {}
    assert client.notebooks.items == {}
    assert client.collections.delete_attempts == [collection.id]
    assert client.notebooks.delete_attempts == [notebook.id]


async def test_cleanup_verifies_both_namespaces_when_one_delete_keeps_failing() -> None:
    prefix = "nbpy-android-collections-run"
    collection = Collection(id="collection-id", name=f"{prefix}-collection")
    notebook = Notebook(id="notebook-id", title=f"{prefix}-notebook")
    client = _Client(
        _Collections(collection, undeletable={collection.id}),
        _Notebooks(notebook),
    )

    with pytest.raises(AssertionError) as caught:
        await cleanup_disposable_resources(
            client,  # type: ignore[arg-type]
            run_prefix=prefix,
            collection_ids={collection.id},
            notebook_ids={notebook.id},
        )

    assert notebook.id not in client.notebooks.items
    assert client.notebooks.delete_attempts == [notebook.id]
    assert collection.id in str(caught.value)
    assert "notebook_ids=[]" in str(caught.value)
