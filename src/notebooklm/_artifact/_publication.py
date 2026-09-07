"""Neutral staging and cancellation settlement for artifact publication."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .._hop_credentials import CredentialPolicy, HopCredentials

logger = logging.getLogger(__name__)


async def write_staging(path: Path, writer: Callable[[Path], object]) -> None:
    """Settle the actual thread before callers unlink staging or release admission."""
    task = asyncio.create_task(asyncio.to_thread(writer, path))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except BaseException:
            if not task.done():
                raise
            break
    # Observe writer errors even when the caller was also cancelled.
    try:
        task.result()
    finally:
        if cancellation is not None:
            raise cancellation


class AssetPublication:
    """Transfer hooks; assembled backend owners supply lifecycle enforcement."""

    def _assert_active(self) -> None:
        """Standalone helpers have no client generation; owners override this hook."""

    @asynccontextmanager
    async def _client_scope(self, client: Any) -> AsyncIterator[Any]:
        async with client:
            yield client

    def _guard_credentials(self, policy: CredentialPolicy) -> CredentialPolicy:
        async def credential_for(url: str) -> HopCredentials | None:
            self._assert_active()
            credentials = await policy(url)
            self._assert_active()
            return credentials

        return credential_for

    def _publish_download(self, staging: Path, destination: Path) -> None:
        os.replace(staging, destination)

    def _remove_download_staging(self, staging: Path) -> None:
        staging.unlink(missing_ok=True)

    def _cleanup_download_staging(self, staging: Path) -> None:
        try:
            self._remove_download_staging(staging)
        except OSError as cleanup_error:
            # Retention must not replace the primary error or cancellation.
            logger.warning(
                "Could not remove download staging file (%s)",
                type(cleanup_error).__name__,
            )

    async def write_file(self, output_path: str, writer: Callable[[Path], object]) -> str:
        """Stage bytes off-loop, settle the writer, then publish without an await gap."""
        self._assert_active()
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            dir=destination.parent, prefix=destination.name + ".", suffix=".tmp"
        )
        os.close(fd)
        staging = Path(name)
        try:
            await write_staging(staging, writer)
            self._assert_active()
            self._publish_download(staging, destination)
            return str(destination)
        except BaseException:
            self._cleanup_download_staging(staging)
            raise
