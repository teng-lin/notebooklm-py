"""Click-free orchestration helpers for ``notebooklm auth refresh``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from filelock import FileLock, Timeout

from .login import master_token


def _bootstrap_lock_path(storage_path: Path) -> Path:
    """Return the sibling lock that serializes first-time session minting."""
    return storage_path.with_name(f".{storage_path.name}.bootstrap.lock")


async def _acquire_bootstrap_lock(lock: FileLock) -> None:
    """Acquire without parking the event loop or leaking a lock on cancellation."""
    while True:
        try:
            # The zero timeout makes this a single non-blocking filesystem
            # attempt. Keeping it synchronous leaves no worker that could
            # acquire the lock after this coroutine has been cancelled.
            lock.acquire(timeout=0)
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
    storage_path = storage_path.expanduser().resolve()
    master_token_path = storage_path.parent / "master_token.json"
    if not master_token_path.exists():
        return False

    # This must be distinct from the canonical storage-writer lock: refresh()
    # acquires that writer lock while persisting the minted jar. Holding the
    # same sentinel around the whole mint would self-deadlock. The dedicated
    # bootstrap lock instead serializes the check-mint sequence across CLI
    # processes, while refresh() continues to serialize its final write with
    # every other storage-state writer.
    lock = FileLock(str(_bootstrap_lock_path(storage_path)), thread_local=False)
    await _acquire_bootstrap_lock(lock)
    try:
        # A process that waited for another bootstrap must observe its completed
        # write and continue through the ordinary validation path without
        # minting a second session.
        if storage_path.exists():
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
