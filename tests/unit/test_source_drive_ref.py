"""Parsing of Google Drive references.

``parse_drive_ref`` is an input boundary: it decides which strings become a
Drive file id the client will later fetch. The host check is the interesting
part — a share URL is only trusted on ``google.com`` and its subdomains, so a
look-alike host must fall through to the rejection rather than yield an id.
"""

from __future__ import annotations

import pytest

from notebooklm._source.drive import DriveRef, _trusted_google_host, parse_drive_ref
from notebooklm.exceptions import ValidationError

FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz01"
KEY = "0-AbCdEf"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(FILE_ID, DriveRef(FILE_ID), id="raw-id"),
        pytest.param(f"  {FILE_ID}  ", DriveRef(FILE_ID), id="surrounding-whitespace"),
        pytest.param(
            f"https://drive.google.com/file/d/{FILE_ID}/view",
            DriveRef(FILE_ID),
            id="file-share-url",
        ),
        pytest.param(
            f"https://drive.google.com/d/{FILE_ID}",
            DriveRef(FILE_ID),
            id="short-d-path",
        ),
        pytest.param(
            f"https://docs.google.com/document/d/{FILE_ID}/edit",
            DriveRef(FILE_ID),
            id="docs-subdomain",
        ),
        pytest.param(
            f"https://drive.google.com/open?id={FILE_ID}",
            DriveRef(FILE_ID),
            id="query-id",
        ),
        pytest.param(
            f"https://drive.google.com/open?id=short&id={FILE_ID}",
            DriveRef(FILE_ID),
            id="first-usable-query-id-wins",
        ),
        pytest.param(
            f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey={KEY}",
            DriveRef(FILE_ID, KEY),
            id="link-shared-resource-key",
        ),
        pytest.param(
            f"https://drive.google.com/open?id={FILE_ID}&resourcekey={KEY}",
            DriveRef(FILE_ID, KEY),
            id="query-id-with-resource-key",
        ),
        pytest.param(
            f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey=&resourcekey={KEY}",
            DriveRef(FILE_ID, KEY),
            id="blank-resource-key-is-skipped",
        ),
        pytest.param(
            f"https://GOOGLE.COM/file/d/{FILE_ID}/view",
            DriveRef(FILE_ID),
            id="uppercase-host",
        ),
        pytest.param(
            f"https://drive.google.com./file/d/{FILE_ID}/view",
            DriveRef(FILE_ID),
            id="fully-qualified-trailing-dot",
        ),
    ],
)
def test_admitted_drive_references(value: str, expected: DriveRef) -> None:
    assert parse_drive_ref(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("short-id", id="too-short-to-be-an-id"),
        pytest.param("has spaces in it and is long enough", id="illegal-characters"),
        pytest.param(f"http://drive.google.com/file/d/{FILE_ID}/view", id="not-https"),
        pytest.param(f"https://drive.google.com.evil.test/file/d/{FILE_ID}", id="suffix-attack"),
        pytest.param(f"https://notgoogle.com/file/d/{FILE_ID}", id="unrelated-host"),
        pytest.param(f"https://evil.test/?id={FILE_ID}", id="untrusted-host-with-query-id"),
        pytest.param("https://drive.google.com/file/d/short/view", id="path-id-too-short"),
        pytest.param("https://drive.google.com/open?id=short", id="query-id-too-short"),
        pytest.param("https://drive.google.com/", id="no-id-anywhere"),
    ],
)
def test_rejected_drive_references(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_drive_ref(value)

    assert "Drive" in str(caught.value)


def test_a_null_reference_is_rejected_without_raising_a_type_error() -> None:
    with pytest.raises(ValidationError, match="required"):
        parse_drive_ref(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("host", "trusted"),
    [
        pytest.param("google.com", True, id="apex"),
        pytest.param("drive.google.com", True, id="subdomain"),
        pytest.param("GOOGLE.COM", True, id="uppercase"),
        pytest.param("google.com.", True, id="trailing-dot"),
        pytest.param(None, False, id="absent"),
        pytest.param("", False, id="empty"),
        pytest.param("notgoogle.com", False, id="suffix-without-the-dot"),
        pytest.param("google.com.evil.test", False, id="apex-in-the-middle"),
        pytest.param("evil.test", False, id="unrelated"),
    ],
)
def test_the_trusted_host_rule(host: str | None, trusted: bool) -> None:
    assert _trusted_google_host(host) is trusted
