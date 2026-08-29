"""Opt-in live qualification for the publicly selectable Android Collections API.

Run this test twice against an isolated master-token profile before changing the
substitution status. It creates one disposable notebook and collection, exercises
all nine public collection methods, and cleans both namespaces by exact ID and a
unique-prefix sweep before asserting that neither resource leaked.
"""

from __future__ import annotations

import os
import uuid

import pytest

from notebooklm import Collection, NotebookLMClient

from ._android_collections_cleanup import cleanup_disposable_resources

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("NOTEBOOKLM_ANDROID_COLLECTIONS_CONFORMANCE") != "1",
        reason="set NOTEBOOKLM_ANDROID_COLLECTIONS_CONFORMANCE=1 for the live Android gate",
    ),
]


async def test_android_collections_complete_lifecycle_and_cleanup() -> None:
    run_prefix = f"nbpy-android-collections-{uuid.uuid4().hex[:12]}"
    notebook_ids: set[str] = set()
    collection_ids: set[str] = set()

    async with NotebookLMClient.from_storage(backend="android") as client:
        assert client.backends["collections"] == "android"
        assert set(client.backends.values()) == {"android"}

        try:
            notebook = await client.notebooks.create(f"{run_prefix}-notebook")
            notebook_ids.add(notebook.id)
            collection = await client.collections.create(f"{run_prefix}-collection")
            collection_ids.add(collection.id)
            assert isinstance(collection, Collection)
            assert collection.id in {item.id for item in await client.collections.list()}
            assert (await client.collections.get(collection.id)).id == collection.id
            assert await client.collections.get_or_none(str(uuid.uuid4())) is None

            renamed = await client.collections.rename(
                collection.id,
                f"{run_prefix}-renamed-collection",
            )
            assert renamed is not None
            assert renamed.name == f"{run_prefix}-renamed-collection"

            added = await client.collections.add_notebooks(collection.id, [notebook.id])
            assert added is not None and notebook.id in added.notebook_ids
            assert notebook.id in {
                member.id for member in await client.collections.notebooks(collection.id)
            }

            removed = await client.collections.remove_notebooks(collection.id, [notebook.id])
            assert removed is not None and notebook.id not in removed.notebook_ids

            await client.collections.delete(collection.id)
            assert await client.collections.get_or_none(collection.id) is None
        finally:
            await cleanup_disposable_resources(
                client,
                run_prefix=run_prefix,
                collection_ids=collection_ids,
                notebook_ids=notebook_ids,
            )
