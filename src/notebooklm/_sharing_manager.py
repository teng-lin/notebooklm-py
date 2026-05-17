"""Legacy notebook share-link composition."""

from collections.abc import Callable
from typing import Any, Protocol

from ._env import get_base_url
from .rpc import RPCMethod


class ShareRpc(Protocol):
    """RPC surface needed by the legacy notebook share manager."""

    async def __call__(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any: ...


def build_share_url(base_url: str, notebook_id: str, artifact_id: str | None = None) -> str:
    """Build the legacy NotebookLM notebook or artifact share URL."""
    notebook_url = f"{base_url}/notebook/{notebook_id}"
    if artifact_id:
        return f"{notebook_url}?artifactId={artifact_id}"
    return notebook_url


class ShareManager:
    """Legacy ``SHARE_ARTIFACT`` manager used by ``NotebooksAPI.share``."""

    def __init__(
        self,
        rpc: ShareRpc,
        base_url_provider: Callable[[], str] = get_base_url,
    ) -> None:
        self._rpc = rpc
        self._base_url_provider = base_url_provider

    async def share(
        self, notebook_id: str, public: bool = True, artifact_id: str | None = None
    ) -> dict[str, Any]:
        """Toggle legacy notebook sharing through ``SHARE_ARTIFACT``."""
        share_options = [1] if public else [0]
        params: list[Any] = [share_options, notebook_id]
        if artifact_id:
            params.append(artifact_id)

        await self._rpc(
            RPCMethod.SHARE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        return {
            "public": public,
            "url": self.get_share_url(notebook_id, artifact_id) if public else None,
            "artifact_id": artifact_id,
        }

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Return the legacy share URL without toggling server-side sharing."""
        return build_share_url(self._base_url_provider(), notebook_id, artifact_id)


__all__ = ["ShareManager", "ShareRpc", "build_share_url"]
