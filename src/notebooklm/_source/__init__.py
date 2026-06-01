"""Private source-feature service package.

Cohesive cluster promoted from the former flat ``_source_*.py`` modules (issue #1328).
Re-exports the cluster's public service classes; importers may also reach submodules
directly (``from .._source.upload import SourceUploadPipeline``).
"""

from . import add, content, listing, polling, upload, upload_payloads
from .add import SourceAddService
from .content import SourceContentRenderer
from .listing import SourceLister
from .polling import SourcePoller
from .upload import SourceUploadPipeline
from .upload_payloads import (
    ResumableUploadStartRequest,
    build_register_file_source_params,
    build_rename_source_params,
    build_resumable_upload_start_request,
)

__all__ = [
    "add",
    "content",
    "listing",
    "polling",
    "upload",
    "upload_payloads",
    "SourceAddService",
    "SourceContentRenderer",
    "SourceLister",
    "SourcePoller",
    "SourceUploadPipeline",
    "ResumableUploadStartRequest",
    "build_register_file_source_params",
    "build_rename_source_params",
    "build_resumable_upload_start_request",
]
