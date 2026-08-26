"""Legacy notebook share-link composition."""

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from ._env import get_base_url
from ._semantic.backend import BackendAdapter, BackendError
from ._semantic.compat import project_backend_error
from ._semantic.records import LEGACY_SHARE_ARTIFACT_DEF, LegacyShareArtifactInput


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
        backend: BackendAdapter | None,
        base_url_provider: Callable[[], str] = get_base_url,
    ) -> None:
        self._backend = backend
        self._base_url_provider = base_url_provider

    async def share(
        self, notebook_id: str, public: bool = True, artifact_id: str | None = None
    ) -> dict[str, Any]:
        """Set/update legacy public share-link state through ``SHARE_ARTIFACT``."""
        if self._backend is None:
            raise RuntimeError("ShareManager semantic backend was not configured")

        public_error: Exception | None = None
        try:
            result = await self._backend.invoke(
                LEGACY_SHARE_ARTIFACT_DEF,
                LegacyShareArtifactInput(notebook_id, public, artifact_id),
                deadline=None,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return {
                "public": result.public,
                "url": (
                    self.get_share_url(notebook_id, result.artifact_id) if result.public else None
                ),
                "artifact_id": result.artifact_id,
            }

        # Raise outside the private BackendError frame so the compatibility
        # projector's reviewed public cause/context graph remains observable.
        assert public_error is not None
        raise public_error

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Return the legacy share URL without toggling server-side sharing."""
        return build_share_url(self._base_url_provider(), notebook_id, artifact_id)


__all__ = ["ShareManager", "build_share_url"]
