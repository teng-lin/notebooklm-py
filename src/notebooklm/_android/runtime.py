"""Owned collaborator bundle for an Android-selected client."""

from __future__ import annotations

from dataclasses import dataclass

from .assets import AndroidAssetDownloadService
from .auth import BearerProvider
from .phenotype import PhenotypeTokenProvider
from .session import AndroidSession
from .upload import AndroidUploadPipeline


@dataclass(frozen=True)
class AndroidRuntime:
    """All collaborators owned exclusively by the Android backend."""

    bearer_provider: BearerProvider
    session: AndroidSession
    upload_pipeline: AndroidUploadPipeline
    asset_downloads: AndroidAssetDownloadService
    phenotype: PhenotypeTokenProvider


__all__ = ["AndroidRuntime"]
