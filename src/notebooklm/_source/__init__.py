"""Transport-neutral source services and lazy compatibility exports.

Polling, batch types, and Markdown rendering remain neutral. Historical package-level names
for concrete services resolve lazily to their web owners so importing
``notebooklm._source`` itself never pulls in the web backend.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_MODULE_EXPORTS = {
    "markdown": "notebooklm._source.markdown",
    "polling": "notebooklm._source.polling",
    "add": "notebooklm._web.sources.add",
    "batch": "notebooklm._source.batch",
    "content": "notebooklm._web.sources.content",
    "drive_import": "notebooklm._web.sources.drive_import",
    "listing": "notebooklm._web.sources.listing",
    "upload": "notebooklm._web.sources.upload",
    "upload_payloads": "notebooklm._web.params.sources",
}

_SYMBOL_EXPORTS = {
    "SourcePoller": ("notebooklm._source.polling", "SourcePoller"),
    "DriveFetcher": ("notebooklm._web.sources.drive_import", "DriveFetcher"),
    "DriveImportService": ("notebooklm._web.sources.drive_import", "DriveImportService"),
    "SourceAddService": ("notebooklm._web.sources.add", "SourceAddService"),
    "SourceContentRenderer": ("notebooklm._web.sources.content", "SourceContentRenderer"),
    "SourceLister": ("notebooklm._web.sources.listing", "SourceLister"),
    "SourceUploadPipeline": ("notebooklm._web.sources.upload", "SourceUploadPipeline"),
    "ResumableUploadStartRequest": (
        "notebooklm._web.params.sources",
        "ResumableUploadStartRequest",
    ),
    "build_register_file_source_params": (
        "notebooklm._web.params.sources",
        "build_register_file_source_params",
    ),
    "build_rename_source_params": (
        "notebooklm._web.params.sources",
        "build_rename_source_params",
    ),
    "build_resumable_upload_start_request": (
        "notebooklm._web.params.sources",
        "build_resumable_upload_start_request",
    ),
}


def __getattr__(name: str) -> Any:
    """Resolve a neutral or moved package export on first access."""
    if module_name := _MODULE_EXPORTS.get(name):
        value = import_module(module_name)
    elif target := _SYMBOL_EXPORTS.get(name):
        module_name, attribute = target
        value = getattr(import_module(module_name), attribute)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy exports in interactive discovery."""
    return sorted({*globals(), *_MODULE_EXPORTS, *_SYMBOL_EXPORTS})


__all__ = [
    "add",
    "batch",
    "content",
    "drive_import",
    "listing",
    "markdown",
    "polling",
    "upload",
    "upload_payloads",
    "DriveFetcher",
    "DriveImportService",
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
