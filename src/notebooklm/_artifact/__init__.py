"""Private artifact-feature service package.

Cohesive cluster promoted from the former flat ``_artifact_*.py`` modules (issue #1328).
Re-exports the cluster's public service classes/builders; importers may also reach
submodules directly (``from .._artifact.polling import ArtifactPollingService``).
"""

from . import downloads, formatters, payloads, polling
from .downloads import ArtifactDownloadService, DownloadResult
from .polling import ArtifactPollingService

__all__ = [
    "downloads",
    "formatters",
    "payloads",
    "polling",
    "ArtifactDownloadService",
    "DownloadResult",
    "ArtifactPollingService",
]
