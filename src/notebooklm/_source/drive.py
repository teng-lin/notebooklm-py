"""Backend-neutral parsing for Google Drive file references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from ..exceptions import ValidationError

_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_DRIVE_URL_PATH_ID_RE = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{20,})")


@dataclass(frozen=True)
class DriveRef:
    """A Drive file id plus the optional key required by link-shared files."""

    file_id: str
    resource_key: str | None = None


# Preserve the exact host families accepted by the web Drive parser before it
# moved here. The old parser deliberately reused the artifact-download policy:
# ordinary Drive/Docs links live under google.com, while download/share links
# can be served from googleusercontent.com or googleapis.com.
_TRUSTED_GOOGLE_DOMAINS = (".google.com", ".googleusercontent.com", ".googleapis.com")


def _trusted_google_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.lower()
    # Match the exact hostname the transport parsed. Percent-decoding could
    # turn an attacker-controlled raw host into a trusted-looking suffix, and
    # neither slash form is valid in a real Google hostname.
    if "%" in normalized or "\\" in normalized or "/" in normalized:
        return False
    return any(
        normalized == domain.lstrip(".") or normalized.endswith(domain)
        for domain in _TRUSTED_GOOGLE_DOMAINS
    )


def parse_drive_ref(id_or_url: str) -> DriveRef:
    """Parse a raw Drive id or a Google-hosted share URL without doing I/O."""

    candidate = (id_or_url or "").strip()
    if not candidate:
        raise ValidationError("A Google Drive file id or share URL is required.")
    if _DRIVE_ID_RE.fullmatch(candidate):
        return DriveRef(file_id=candidate)

    parsed = urlparse(candidate)
    if parsed.scheme == "https" and _trusted_google_host(parsed.hostname):
        query = parse_qs(parsed.query)
        resource_key = next((value for value in query.get("resourcekey", []) if value), None)
        for value in query.get("id", []):
            if _DRIVE_ID_RE.fullmatch(value):
                return DriveRef(file_id=value, resource_key=resource_key)
        path_match = _DRIVE_URL_PATH_ID_RE.search(parsed.path)
        if path_match is not None:
            return DriveRef(file_id=path_match.group(1), resource_key=resource_key)

    raise ValidationError(
        "Could not parse a Google Drive file id. Pass a raw file id or a Drive URL like "
        "https://drive.google.com/file/d/<id>/view."
    )


__all__ = ["DriveRef", "parse_drive_ref"]
