"""Closed, transport-neutral Studio family classification."""

from __future__ import annotations

from .._semantic.records import ArtifactRecord

ARTIFACT_FAMILIES = frozenset(
    {
        "audio",
        "video",
        "report",
        "quiz",
        "flashcards",
        "mind_map",
        "infographic",
        "slide_deck",
        "data_table",
        "fantasy_map",
        "file",
        "unknown",
    }
)


def matches_artifact_family(record: ArtifactRecord, family: str | None) -> bool:
    """Return whether one neutral catalog record belongs to ``family``."""

    if family is None:
        return True
    if family not in ARTIFACT_FAMILIES:
        return False
    return record.family == family


__all__ = ["ARTIFACT_FAMILIES", "matches_artifact_family"]
