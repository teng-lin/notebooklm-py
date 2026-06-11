"""Source upload request payload builders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResumableUploadStartRequest:
    """HTTP request fields for starting a Scotty resumable upload."""

    url: str
    headers: dict[str, str]
    body: str


def build_template_block() -> list[Any]:
    """Return the nested request-options wrapper ``[2, None, None, [1, ..., [1]]]``.

    Shared by ``CREATE_NOTEBOOK`` and every ``ADD_SOURCE`` / ``ADD_SOURCE_FILE``
    variant. This is the same wrapper the label RPCs already send
    (``_label.params._opts``; its inner ``[1, ..., [1]]`` context block also
    appears in ``_settings``). Google's Gemini-3.5 rollout made create/source
    require the full wrapper too — they previously sent a degenerate
    ``[2], [1]`` (create) / ``[2], None, None`` (source) tail, which migrated
    backends now reject (``status=3``/``5``/``9``). Verified live against an
    un-migrated account. Returns a fresh list each call so callers never share a
    mutable nested structure. See https://github.com/teng-lin/notebooklm-py/issues/1546.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def build_register_file_source_params(filename: str, notebook_id: str) -> list[Any]:
    """Build ``ADD_SOURCE_FILE`` params for file source registration.

    Uses the shared nested template block (#1546); the old flat
    ``[2], [1,...,[1]]`` tail no longer validates on migrated cohorts.
    """
    return [
        [[filename]],
        notebook_id,
        build_template_block(),
    ]


def build_rename_source_params(source_id: str, new_title: str) -> list[Any]:
    """Build ``UPDATE_SOURCE`` params for source title updates."""
    return [None, [source_id], [[[new_title]]]]


def build_resumable_upload_start_request(
    *,
    notebook_id: str,
    filename: str,
    file_size: int,
    source_id: str,
    content_type: str,
    base_url: str,
    upload_url: str,
    authuser_query: str,
    authuser_header: str,
) -> ResumableUploadStartRequest:
    """Build the HTTP request that starts a resumable upload session."""
    return ResumableUploadStartRequest(
        url=f"{upload_url}?{authuser_query}",
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": base_url,
            "Referer": f"{base_url}/",
            "x-goog-authuser": authuser_header,
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-header-content-type": content_type,
            "x-goog-upload-protocol": "resumable",
        },
        body=json.dumps(
            {
                "PROJECT_ID": notebook_id,
                "SOURCE_NAME": filename,
                "SOURCE_ID": source_id,
            }
        ),
    )
