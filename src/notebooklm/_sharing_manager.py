"""Legacy notebook share-link composition."""

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from ._env import get_base_url
from ._runtime.contracts import RpcCaller
from .rpc import RPCMethod


def build_share_url(base_url: str, notebook_id: str, artifact_id: str | None = None) -> str:
    """Build the legacy NotebookLM notebook or artifact share URL.

    Both IDs are percent-encoded with ``safe=""`` so reserved characters
    (``/``, ``?``, ``&``, ``#``) and whitespace cannot escape the path /
    query position and rewrite the URL into another endpoint.
    """
    notebook_url = f"{base_url}/notebook/{quote(notebook_id, safe='')}"
    if artifact_id:
        return f"{notebook_url}?artifactId={quote(artifact_id, safe='')}"
    return notebook_url


class ShareManager:
    """Legacy ``SHARE_ARTIFACT`` manager kept behind share URL internals."""

    def __init__(
        self,
        rpc: RpcCaller,
        base_url_provider: Callable[[], str] = get_base_url,
    ) -> None:
        self._rpc = rpc
        self._base_url_provider = base_url_provider

    async def share(
        self, notebook_id: str, public: bool = True, artifact_id: str | None = None
    ) -> dict[str, Any]:
        """Set/update legacy public share-link state through ``SHARE_ARTIFACT``."""
        share_options = [1] if public else [0]
        params: list[Any] = [share_options, notebook_id]
        if artifact_id:
            params.append(artifact_id)

        await self._rpc.rpc_call(
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


__all__ = ["ShareManager", "build_share_url"]
