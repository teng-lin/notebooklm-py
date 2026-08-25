"""Private artifact-feature service package.

Cohesive cluster promoted from the former flat ``_artifact_*.py`` modules (issue #1328).
Re-exports the cluster's public service classes/builders; importers may also reach
submodules directly (``from .._artifact.polling import ArtifactPollingService``).
"""

from . import downloads, formatters, listing, polling
from .downloads import DownloadResult
from .listing import ArtifactListingService, find_artifact_row_by_id, iter_artifact_rows
from .polling import ArtifactPollingService

__all__ = [
    "downloads",
    "formatters",
    "listing",
    "polling",
    "DownloadResult",
    "ArtifactListingService",
    "find_artifact_row_by_id",
    "iter_artifact_rows",
    "ArtifactPollingService",
]
