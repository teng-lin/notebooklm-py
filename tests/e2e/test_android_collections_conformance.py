"""Opt-in live qualification for the publicly selectable Android Collections API.

Run this test twice against an isolated master-token profile before changing the
substitution status. It creates one disposable notebook and collection, exercises
all nine public collection methods, and deletes both resources in ``finally``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from notebooklm import Collection, NotebookLMClient

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("NOTEBOOKLM_ANDROID_COLLECTIONS_CONFORMANCE") != "1",
        reason="set NOTEBOOKLM_ANDROID_COLLECTIONS_CONFORMANCE=1 for the live Android gate",
    ),
]


async def test_android_collections_complete_lifecycle_and_cleanup() -> None:
    marker = uuid.uuid4().hex[:12]
    notebook = None
    collection = None

    async with NotebookLMClient.from_storage(backend="android") as client:
        assert client.backends["collections"] == "android"
        assert all(
            backend == ("android" if namespace == "collections" else "web")
            for namespace, backend in client.backends.items()
        )

        try:
            notebook = await client.notebooks.create(f"nbpy-android-collections-{marker}")
            collection = await client.collections.create(f"nbpy-android-collections-{marker}")
            assert isinstance(collection, Collection)
            assert collection.id in {item.id for item in await client.collections.list()}
            assert (await client.collections.get(collection.id)).id == collection.id
            assert await client.collections.get_or_none(str(uuid.uuid4())) is None

            renamed = await client.collections.rename(
                collection.id,
                f"nbpy-android-collections-renamed-{marker}",
            )
            assert renamed is not None
            assert renamed.name == f"nbpy-android-collections-renamed-{marker}"

            added = await client.collections.add_notebooks(collection.id, [notebook.id])
            assert added is not None and notebook.id in added.notebook_ids
            assert notebook.id in {
                member.id for member in await client.collections.notebooks(collection.id)
            }

            removed = await client.collections.remove_notebooks(collection.id, [notebook.id])
            assert removed is not None and notebook.id not in removed.notebook_ids

            await client.collections.delete(collection.id)
            assert await client.collections.get_or_none(collection.id) is None
            collection = None
        finally:
            if collection is not None:
                await client.collections.delete(collection.id)
            if notebook is not None:
                await client.notebooks.delete(notebook.id)
