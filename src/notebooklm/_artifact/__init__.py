"""Private artifact-feature service package.

Cohesive cluster promoted from the former flat ``_artifact_*.py`` modules (issue #1328).
Neutral services remain eager. Historical package-level Web service names are
resolved lazily so importing the neutral package never pulls in ``_web``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import downloads, formatters, polling
from .downloads import AssetDownloadService, DownloadResult
from .polling import ArtifactPollingService

if TYPE_CHECKING:
    from .._web.artifact import generation, listing
    from .._web.artifact.downloads import ArtifactDownloadService
    from .._web.artifact.listing import (
        ArtifactListingService,
        find_artifact_row_by_id,
        iter_artifact_rows,
    )
    from .._web.params import artifacts as payloads


def __getattr__(name: str) -> Any:
    """Lazily preserve the package-level names that moved to the Web backend."""
    if name == "ArtifactDownloadService":
        from .._web.artifact.downloads import ArtifactDownloadService

        value: Any = ArtifactDownloadService
    elif name in {"ArtifactListingService", "find_artifact_row_by_id", "iter_artifact_rows"}:
        from .._web.artifact import listing

        value = getattr(listing, name)
    elif name == "generation":
        from .._web.artifact import generation

        value = generation
    elif name == "listing":
        from .._web.artifact import listing

        value = listing
    elif name == "payloads":
        from .._web.params import artifacts

        value = artifacts
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value


__all__ = [
    "downloads",
    "formatters",
    "polling",
    "AssetDownloadService",
    "ArtifactDownloadService",
    "ArtifactListingService",
    "DownloadResult",
    "ArtifactPollingService",
    "find_artifact_row_by_id",
    "generation",
    "iter_artifact_rows",
    "listing",
    "payloads",
]
