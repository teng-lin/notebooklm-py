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


def build_register_file_source_params(filename: str, notebook_id: str) -> list[Any]:
    """Build ``ADD_SOURCE_FILE`` params for file source registration."""
    return [
        [[filename]],
        notebook_id,
        [2],
        [1, None, None, None, None, None, None, None, None, None, [1]],
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
