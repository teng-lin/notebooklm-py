"""Shared artifact selectors for live E2E tests and their unit coverage."""

from __future__ import annotations

from notebooklm import Artifact


def completed_interactive_mind_maps(artifacts: list[Artifact]) -> list[Artifact]:
    """Return only downloadable interactive mind-map artifacts."""
    return [
        artifact
        for artifact in artifacts
        if artifact.is_interactive_mind_map and artifact.is_completed
    ]
