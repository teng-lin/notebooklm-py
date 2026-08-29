"""Failure-safe cleanup support for the live Android Collections gate."""

from __future__ import annotations

import contextlib

from notebooklm import NotebookLMClient


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


async def cleanup_disposable_resources(
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
