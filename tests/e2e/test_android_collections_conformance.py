"""Opt-in live qualification for the publicly selectable Android Collections API.

Run this test twice against an isolated master-token profile before changing the
substitution status. It creates one disposable notebook and collection, exercises
all nine public collection methods, and cleans both namespaces by exact ID and a
unique-prefix sweep before asserting that neither resource leaked.
"""

from __future__ import annotations

import contextlib
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


async def _cleanup_collections(
    client: NotebookLMClient,
    *,
    run_prefix: str,
    known_ids: set[str],
) -> tuple[list[str], str | None]:
    """Best-effort exact/prefix cleanup followed by a final absence check."""

    for collection_id in tuple(known_ids):
        with contextlib.suppress(Exception):
            await client.collections.delete(collection_id)

    try:
        candidates = [
            collection
            for collection in await client.collections.list()
            if collection.name.startswith(run_prefix)
        ]
    except Exception:
        candidates = []
    for collection in candidates:
        with contextlib.suppress(Exception):
            await client.collections.delete(collection.id)

    try:
        remaining = [
            collection
            for collection in await client.collections.list()
            if collection.name.startswith(run_prefix)
        ]
    except Exception as exc:
        return [], type(exc).__name__

    # A failed exact delete or first prefix sweep gets one more best-effort
    # attempt before the final leak assertion.
    for collection in remaining:
        with contextlib.suppress(Exception):
            await client.collections.delete(collection.id)
    if remaining:
        try:
            remaining = [
                collection
                for collection in await client.collections.list()
                if collection.name.startswith(run_prefix)
            ]
        except Exception as exc:
            return [], type(exc).__name__
    return [collection.id for collection in remaining], None


async def _cleanup_notebooks(
    client: NotebookLMClient,
    *,
    run_prefix: str,
    known_ids: set[str],
) -> tuple[list[str], str | None]:
    """Clean notebooks independently even when collection cleanup failed."""

    for notebook_id in tuple(known_ids):
        with contextlib.suppress(Exception):
            await client.notebooks.delete(notebook_id)

    try:
        candidates = [
            notebook
            for notebook in await client.notebooks.list()
            if notebook.title.startswith(run_prefix)
        ]
    except Exception:
        candidates = []
    for notebook in candidates:
        with contextlib.suppress(Exception):
            await client.notebooks.delete(notebook.id)

    try:
        remaining = [
            notebook
            for notebook in await client.notebooks.list()
            if notebook.title.startswith(run_prefix)
        ]
    except Exception as exc:
        return [], type(exc).__name__

    for notebook in remaining:
        with contextlib.suppress(Exception):
            await client.notebooks.delete(notebook.id)
    if remaining:
        try:
            remaining = [
                notebook
                for notebook in await client.notebooks.list()
                if notebook.title.startswith(run_prefix)
            ]
        except Exception as exc:
            return [], type(exc).__name__
    return [notebook.id for notebook in remaining], None


async def _cleanup_disposable_resources(
    client: NotebookLMClient,
    *,
    run_prefix: str,
    collection_ids: set[str],
    notebook_ids: set[str],
) -> None:
    """Attempt and verify both cleanup families without short-circuiting."""

    leaked_collections, collection_error = await _cleanup_collections(
        client,
        run_prefix=run_prefix,
        known_ids=collection_ids,
    )
    leaked_notebooks, notebook_error = await _cleanup_notebooks(
        client,
        run_prefix=run_prefix,
        known_ids=notebook_ids,
    )
    verification_errors = [
        message
        for message in (
            None if collection_error is None else f"collections:{collection_error}",
            None if notebook_error is None else f"notebooks:{notebook_error}",
        )
        if message is not None
    ]
    assert verification_errors == [] and leaked_collections == [] and leaked_notebooks == [], (
        "disposable Android Collections resources leaked or could not be verified: "
        f"verification_errors={verification_errors!r}, "
        f"collection_ids={leaked_collections!r}, notebook_ids={leaked_notebooks!r}"
    )


async def test_android_collections_complete_lifecycle_and_cleanup() -> None:
    run_prefix = f"nbpy-android-collections-{uuid.uuid4().hex[:12]}"
    notebook_ids: set[str] = set()
    collection_ids: set[str] = set()

    async with NotebookLMClient.from_storage(backend="android") as client:
        assert client.backends["collections"] == "android"
        assert all(
            backend == ("android" if namespace == "collections" else "web")
            for namespace, backend in client.backends.items()
        )

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
            await _cleanup_disposable_resources(
                client,
                run_prefix=run_prefix,
                collection_ids=collection_ids,
                notebook_ids=notebook_ids,
            )
