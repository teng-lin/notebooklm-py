"""Click-free orchestration helpers for ``notebooklm auth refresh``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from filelock import FileLock, Timeout

from .login import master_token


def _bootstrap_lock_path(storage_path: Path) -> Path:
    """Return the canonical lock that serializes first-time session minting."""
    canonical_path = storage_path.expanduser().resolve()
    return canonical_path.with_name(f".{canonical_path.name}.lock.bootstrap")


async def _acquire_bootstrap_lock(lock: FileLock) -> None:
    """Acquire without blocking the event loop, including on filelock 3.13."""
    while True:
        try:
            lock.acquire(blocking=False)
        except Timeout:
            await asyncio.sleep(0.05)
        else:
            return


async def _run_refresh_to_settlement(*, storage_path: Path, master_token_path: Path) -> None:
    """Do not release the bootstrap lock while refresh persistence is still running."""
    task = asyncio.create_task(
        master_token.refresh(
            storage_path=storage_path,
            master_token_path=master_token_path,
        )
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # ``refresh`` offloads its final write to a thread. Propagating caller
        # cancellation immediately would release the bootstrap lock while that
        # write can still be running, allowing a waiting process to mint again.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - settle before cancellation propagates
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def bootstrap_missing_storage_from_master_token(storage_path: Path) -> bool:
    """Mint initial storage when only the sibling master token exists."""
    # Preserve the healthy-storage fast path: profiles that already have a jar
    # should not pay for a lock merely because they also retain a master token.
    if storage_path.exists():
        return False
    master_token_path = storage_path.parent / "master_token.json"
    if not master_token_path.exists():
        return False

    # A dedicated bootstrap lock is intentionally distinct from the shared
    # storage-write lock: master_token.refresh() acquires that write lock while
    # persisting, so holding it here would self-deadlock. Recheck both inputs
    # after waiting because another process may have completed the bootstrap or
    # removed the durable token in the meantime.
    lock = FileLock(str(_bootstrap_lock_path(storage_path)))
    await _acquire_bootstrap_lock(lock)
    try:
        # A leader may have created storage while this caller waited. Report
        # bootstrap-ready in that case so the waiter takes the same mandatory
        # passive-validation path instead of entering ordinary recovery.
        if storage_path.exists():
            return True
        if not master_token_path.exists():
            return False
        await _run_refresh_to_settlement(
            storage_path=storage_path,
            master_token_path=master_token_path,
        )
        return True
    finally:
        # Release synchronously so cancellation cannot strand the process-wide
        # lock between a completed mint and an await scheduled for cleanup.
        lock.release()


__all__ = ["bootstrap_missing_storage_from_master_token"]
