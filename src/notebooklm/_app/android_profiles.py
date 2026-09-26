"""Cookie-free Android construction for profile-based adapters."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .. import AuthTokens
from ..client import NotebookLMClient
from ..exceptions import ConfigurationError
from ..options import AndroidBackendConfig, ClientConfig
from .master_token import inspect_master_token_status


@asynccontextmanager
async def android_profile_client(storage_path: Path) -> AsyncIterator[NotebookLMClient]:
    """Open only Android auth; no Web storage, homepage, rotation, or persistence.

    The SDK's legacy ``from_storage`` still bootstraps its compatibility Web
    sidecar. REST and MCP multi-profile clients never use that sidecar, so construct an
    explicit empty Web seed and bind the Android durable-token reader by path.
    Distinct paths may contain identical master tokens: each client owns its
    bearer cache and retries. No account/token/session fingerprint is retained.
    """
    status = await asyncio.to_thread(inspect_master_token_status, storage_path, has_env_auth=False)
    if not status.present or status.unreadable_error_type is not None:
        raise ConfigurationError("Android profile requires a valid master_token.json")
    auth = AuthTokens(
        cookies={},
        csrf_token="",
        session_id="",
        storage_path=storage_path,
        cookie_jar=httpx.Cookies(),
        account_email=status.account if isinstance(status.account, str) else None,
    )
    async with NotebookLMClient(
        auth, config=ClientConfig(backend=AndroidBackendConfig())
    ) as client:
        # Opening validates the local record; a read also validates minting and
        # upstream access before this profile is advertised as ready.
        await client.notebooks.list()
        yield client
