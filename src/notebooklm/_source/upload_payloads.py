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
    import os
    import base64
    from urllib.parse import urlparse
    from .._env import ENTERPRISE_BASE_HOST, get_notebooklm_region

    parsed_host = urlparse(base_url).hostname
    if parsed_host == ENTERPRISE_BASE_HOST:
        parts = notebook_id.split("/")
        if len(parts) >= 6:
            p_0, p_1, p_2, p_3, p_4, p_5 = parts[:6]
            if (
                p_0 == "projects"
                and p_2 == "locations"
                and p_4 == "notebooks"
            ):
                project = p_1
                region = p_3
                notebook = p_5
        else:
            project = os.environ.get("NOTEBOOKLM_PROJECT", "").strip()
            region = get_notebooklm_region()
            notebook = notebook_id

        if not project:
            from ..exceptions import ValidationError
            raise ValidationError(
                "NOTEBOOKLM_PROJECT environment variable must be set when notebook_id "
                "is not fully-qualified (projects/.../locations/.../notebooks/...)"
            )

        url = (
            "https://discoveryengine.clients6.google.com/upload/v1alpha/"
            f"projects/{project}/locations/{region}/notebooks/{notebook}/sources:uploadFile"
        )
        b64_filename = base64.b64encode(filename.encode("utf-8")).decode("utf-8")

        return ResumableUploadStartRequest(
            url=url,
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
                "x-goog-upload-file-name": b64_filename,
            },
            body="",
        )

    url = f"{upload_url}?{authuser_query}"
    project_id_payload = notebook_id
    return ResumableUploadStartRequest(
        url=url,
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
                "PROJECT_ID": project_id_payload,
                "SOURCE_NAME": filename,
                "SOURCE_ID": source_id,
            }
        ),
    )

