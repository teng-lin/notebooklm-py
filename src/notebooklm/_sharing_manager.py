"""Backend-neutral notebook share-link composition."""

from urllib.parse import quote


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


__all__ = ["build_share_url"]
