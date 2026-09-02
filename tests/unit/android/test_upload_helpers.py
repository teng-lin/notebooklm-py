"""Unit coverage for the Android uploader's pure admission helpers.

These sit in front of every byte the adapter sends: the MIME resolution, the
HTML refusal, the Drive metadata gate, and the timeout projection. The adapter
suites exercise their accept paths; these cases pin the rejections, which is
where a regression would silently widen what the client is willing to upload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._android.upload import (
    _drive_filename,
    _drive_resource_key_headers,
    _resolve_upload_content_type,
    _resolve_upload_timeouts,
    _validate_drive_metadata,
    _validate_upload_file_supported,
)
from notebooklm._source.drive import DriveRef
from notebooklm.exceptions import ValidationError

FILE_ID = "1AbCdEf"


def _metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": "paper.pdf", "mimeType": "application/pdf"}
    metadata.update(overrides)
    return metadata


# ---------------------------------------------------------------------------
# _resolve_upload_content_type
# ---------------------------------------------------------------------------


def test_an_explicit_mime_type_wins_and_is_stripped() -> None:
    assert _resolve_upload_content_type(Path("notes.md"), "  text/plain  ") == "text/plain"


@pytest.mark.parametrize("mime_type", ["", "   "])
def test_a_blank_explicit_mime_type_is_rejected(mime_type: str) -> None:
    with pytest.raises(ValidationError, match="cannot be empty or whitespace-only"):
        _resolve_upload_content_type(Path("notes.md"), mime_type)


def test_the_pinned_table_beats_the_platform_mimetypes_database() -> None:
    """``.docx``/``.pptx`` return nothing from the Windows registry (#2034 note)."""
    assert _resolve_upload_content_type(Path("NOTES.MD"), None) == "text/markdown"
    assert _resolve_upload_content_type(Path("deck.pptx"), None) == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_an_unpinned_extension_falls_back_to_the_mimetypes_guess() -> None:
    assert _resolve_upload_content_type(Path("page.json"), None) == "application/json"


def test_an_unguessable_extension_falls_back_to_octet_stream() -> None:
    assert _resolve_upload_content_type(Path("blob.unknownext"), None) == "application/octet-stream"


# ---------------------------------------------------------------------------
# _validate_upload_file_supported
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        pytest.param("page.html", "text/plain", id="html-extension"),
        pytest.param("page.XHTML", "text/plain", id="uppercase-extension"),
        pytest.param("page.txt", "text/html; charset=utf-8", id="html-content-type"),
        pytest.param("page.txt", "APPLICATION/XHTML+XML", id="uppercase-content-type"),
    ],
)
def test_html_family_uploads_are_refused(name: str, content_type: str) -> None:
    with pytest.raises(ValidationError, match="HTML file uploads are not supported"):
        _validate_upload_file_supported(Path(name), content_type)


def test_a_supported_upload_passes_the_gate() -> None:
    _validate_upload_file_supported(Path("paper.pdf"), "application/pdf")


# ---------------------------------------------------------------------------
# _drive_resource_key_headers
# ---------------------------------------------------------------------------


def test_a_link_shared_file_echoes_its_resource_key() -> None:
    headers = _drive_resource_key_headers(DriveRef(file_id=FILE_ID, resource_key="rk-1"))

    assert headers == {"X-Goog-Drive-Resource-Keys": f"{FILE_ID}/rk-1"}


def test_a_file_without_a_resource_key_sends_no_extra_header() -> None:
    assert _drive_resource_key_headers(DriveRef(file_id=FILE_ID)) == {}


# ---------------------------------------------------------------------------
# _drive_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("paper.pdf", "paper.pdf", id="plain"),
        pytest.param("  paper.pdf  ", "paper.pdf", id="stripped"),
        pytest.param("a/b/paper.pdf", "paper.pdf", id="posix-path-stripped"),
        pytest.param("a\\b\\paper.pdf", "paper.pdf", id="windows-path-stripped"),
        pytest.param("pa\tper\x7f.pdf", "pa_per_.pdf", id="control-characters-replaced"),
    ],
)
def test_drive_filename_reduces_to_one_safe_leaf(value: str, expected: str) -> None:
    assert _drive_filename(value, FILE_ID) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="parent"),
        pytest.param("dir/", id="trailing-separator"),
        pytest.param(None, id="not-a-string"),
        pytest.param(7, id="numeric"),
    ],
)
def test_drive_filename_rejects_unusable_values(value: Any) -> None:
    with pytest.raises(ValidationError, match="usable filename"):
        _drive_filename(value, FILE_ID)


def test_drive_filename_rejects_an_over_long_name() -> None:
    with pytest.raises(ValidationError, match="too long to import safely"):
        _drive_filename("x" * 241 + ".pdf", FILE_ID)


def test_drive_filename_measures_length_in_utf8_bytes() -> None:
    """A multi-byte name under the character cap can still exceed the byte cap."""
    with pytest.raises(ValidationError, match="too long to import safely"):
        _drive_filename("é" * 121, FILE_ID)


# ---------------------------------------------------------------------------
# _validate_drive_metadata
# ---------------------------------------------------------------------------


def test_drive_metadata_returns_the_admitted_filename_and_mime() -> None:
    ref = DriveRef(file_id=FILE_ID)

    assert _validate_drive_metadata(_metadata(), ref) == ("paper.pdf", "application/pdf")


def test_drive_metadata_accepts_a_size_under_the_cap() -> None:
    ref = DriveRef(file_id=FILE_ID)

    assert _validate_drive_metadata(_metadata(size="1024"), ref)[0] == "paper.pdf"


def test_drive_metadata_accepts_explicitly_downloadable_files() -> None:
    ref = DriveRef(file_id=FILE_ID)

    assert _validate_drive_metadata(_metadata(capabilities={"canDownload": True}), ref)


@pytest.mark.parametrize(
    "metadata",
    [pytest.param(None, id="none"), pytest.param([], id="list"), pytest.param("{}", id="string")],
)
def test_drive_metadata_rejects_a_non_object_payload(metadata: Any) -> None:
    with pytest.raises(ValidationError, match="malformed metadata"):
        _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))


@pytest.mark.parametrize(
    "mime_type",
    [pytest.param(None, id="absent"), pytest.param("", id="empty"), pytest.param(7, id="numeric")],
)
def test_drive_metadata_requires_a_mime_type(mime_type: Any) -> None:
    with pytest.raises(ValidationError, match="did not return a MIME type"):
        _validate_drive_metadata(_metadata(mimeType=mime_type), DriveRef(file_id=FILE_ID))


def test_drive_metadata_routes_native_google_documents_elsewhere() -> None:
    metadata = _metadata(name="doc.pdf", mimeType="application/vnd.google-apps.document")

    with pytest.raises(ValidationError, match="sources.add_drive"):
        _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))


def test_drive_metadata_rejects_malformed_capabilities() -> None:
    with pytest.raises(ValidationError, match="malformed metadata"):
        _validate_drive_metadata(_metadata(capabilities="yes"), DriveRef(file_id=FILE_ID))


def test_drive_metadata_rejects_a_file_this_account_cannot_download() -> None:
    metadata = _metadata(capabilities={"canDownload": False})

    with pytest.raises(ValidationError, match="not downloadable by this account"):
        _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))


def test_drive_metadata_refuses_html_before_the_extension_allowlist() -> None:
    metadata = _metadata(name="page.html", mimeType="text/html")

    with pytest.raises(ValidationError, match="HTML isn't supported"):
        _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))


def test_drive_metadata_rejects_an_unsupported_extension_and_names_the_accepted_set() -> None:
    metadata = _metadata(name="archive.zip", mimeType="application/zip")

    with pytest.raises(ValidationError, match="unsupported type") as caught:
        _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))

    assert "pdf" in str(caught.value)


def test_drive_metadata_rejects_a_file_over_the_download_cap() -> None:
    metadata = _metadata(size=str(200 * 1024 * 1024 + 1))

    with pytest.raises(ValidationError, match="over the 200 MiB download cap"):
        _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))


@pytest.mark.parametrize(
    "size", [pytest.param("huge", id="non-numeric"), pytest.param(None, id="absent")]
)
def test_drive_metadata_tolerates_an_undeclared_or_unparseable_size(size: Any) -> None:
    """An unreadable size is not treated as over-cap; the stream cap still applies."""
    metadata = _metadata()
    if size is not None:
        metadata["size"] = size

    assert _validate_drive_metadata(metadata, DriveRef(file_id=FILE_ID))[0] == "paper.pdf"


# ---------------------------------------------------------------------------
# _resolve_upload_timeouts
# ---------------------------------------------------------------------------


def test_an_unset_timeout_uses_the_historical_lifecycle_fence() -> None:
    assert _resolve_upload_timeouts(None) == (300.0, None)


def test_a_numeric_timeout_becomes_the_aggregate_with_no_per_request_override() -> None:
    assert _resolve_upload_timeouts(45.0) == (45.0, None)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param(float("nan"), id="nan"),
    ],
)
def test_a_non_positive_or_non_finite_numeric_timeout_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="must be a finite positive number"):
        _resolve_upload_timeouts(value)


def test_an_httpx_timeout_is_preserved_and_widens_the_lifecycle_fence() -> None:
    configured = httpx.Timeout(connect=10.0, read=200.0, write=200.0, pool=10.0)

    aggregate, per_request = _resolve_upload_timeouts(configured)

    assert per_request is configured
    assert aggregate == 2.0 * (10.0 + 200.0 + 200.0 + 10.0)


def test_a_small_httpx_timeout_keeps_the_300_second_floor() -> None:
    aggregate, per_request = _resolve_upload_timeouts(httpx.Timeout(5.0))

    assert (aggregate, per_request.read) == (300.0, 5.0)


def test_a_fully_unbounded_httpx_timeout_keeps_the_historical_fence() -> None:
    configured = httpx.Timeout(None)

    aggregate, per_request = _resolve_upload_timeouts(configured)

    assert aggregate == 300.0
    assert per_request is configured


@pytest.mark.parametrize(
    "component",
    [
        pytest.param({"connect": 0.0}, id="zero-connect"),
        pytest.param({"read": -1.0}, id="negative-read"),
        pytest.param({"write": float("inf")}, id="infinite-write"),
        pytest.param({"pool": float("nan")}, id="nan-pool"),
    ],
)
def test_an_httpx_timeout_component_must_be_finite_and_positive(
    component: dict[str, float],
) -> None:
    kwargs: dict[str, float | None] = {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }
    kwargs.update(component)

    with pytest.raises(ValueError, match="components must be finite positive numbers"):
        _resolve_upload_timeouts(httpx.Timeout(**kwargs))
