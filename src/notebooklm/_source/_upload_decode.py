"""Pure decode/validation helpers for the source upload pipeline.

Extracted from :mod:`notebooklm._source.upload` to keep that module under the
size budget. These are side-effect-free helpers over the resumable-upload URL,
the ``ADD_SOURCE_FILE`` register response (source-id extraction), and upload
content-type policy. ``upload.py`` re-exports every name so the historical
``notebooklm._source.upload.<helper>`` import/patch surface keeps resolving.
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import SplitResult, parse_qsl, urlsplit

import httpx

from .._transport_errors import parse_retry_after
from ..exceptions import AuthError, RateLimitError, ServerError, ValidationError
from ..rpc import get_upload_url

_SOURCE_ID_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SOURCE_ID_FIELD_NAMES = frozenset({"SOURCE_ID", "source_id", "sourceId"})
_CONTEXTUAL_SOURCE_ID_FIELD_NAMES = frozenset({"id"})
_SOURCE_NAME_FIELD_NAMES = frozenset(
    {"SOURCE_NAME", "source_name", "sourceName", "filename", "fileName", "name", "title"}
)
_SOURCE_ID_ENVELOPE_MAX_DEPTH = 8

_MEDIA_CONTENT_TYPE_PREFIXES = ("audio/", "video/")
_MEDIA_APPLICATION_CONTENT_TYPES = frozenset(
    {
        "application/mp4",
        "application/ogg",
        "application/x-matroska",
    }
)
_MEDIA_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = (10, 0, None)
_STRICT_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = ()
_HTML_UPLOAD_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".xht"})
_HTML_UPLOAD_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# ``mimetypes.guess_type`` returns ``None`` for some text suffixes on Python 3.10
# and on hosts without a populated ``/etc/mime.types`` (notably ``.md``). Without
# an override the upload falls back to ``application/octet-stream`` (see
# ``_resolve_upload_content_type``), NotebookLM cannot infer how to parse the
# file, and processing fails with status=ERROR. Pin the types we know NotebookLM
# accepts so upload works regardless of the host's MIME table.
_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


def _normalize_upload_path(path: str) -> str:
    return (path or "/").rstrip("/") + "/"


def _default_port_for_scheme(scheme: str) -> int | None:
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _redacted_upload_authority(parsed: SplitResult) -> str | None:
    host = parsed.hostname
    if host is None:
        return None

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port = parsed.port
    port_suffix = f":{port}" if port is not None else ""
    return f"{host}{port_suffix}"


def _redact_upload_url(upload_url: str) -> str:
    """Return a log-safe representation of a resumable upload URL."""
    try:
        parsed = urlsplit(upload_url)
        authority = _redacted_upload_authority(parsed)
    except ValueError:
        return "[REDACTED_UPLOAD_URL]"
    if not parsed.scheme or authority is None:
        return "[REDACTED_UPLOAD_URL]"
    suffix = "?..." if parsed.query else ""
    return f"{parsed.scheme}://{authority}{parsed.path}{suffix}"


def _validate_resumable_upload_url(upload_url: str) -> str:
    """Validate that a resumable upload URL targets the configured upload endpoint."""
    try:
        parsed = urlsplit(upload_url)
        actual_port = parsed.port or _default_port_for_scheme(parsed.scheme)
        expected = urlsplit(get_upload_url())
        expected_port = expected.port or _default_port_for_scheme(expected.scheme)
    except ValueError as exc:
        raise ValidationError("Upload URL is not valid") from exc

    if parsed.scheme != "https":
        raise ValidationError("Upload URL must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("Upload URL must not contain credentials")
    if parsed.hostname is None:
        raise ValidationError("Upload URL must include a host")
    if parsed.hostname != expected.hostname or actual_port != expected_port:
        raise ValidationError("Upload URL host is not trusted")
    if _normalize_upload_path(parsed.path) != _normalize_upload_path(expected.path):
        raise ValidationError("Upload URL path is not trusted")
    upload_ids = [
        value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() == "upload_id"
    ]
    if len(upload_ids) != 1:
        raise ValidationError("Upload URL must include exactly one non-empty upload_id")
    (upload_id,) = upload_ids  # exactly one (guarded); unpack avoids next(iter()): ratchet
    if not upload_id:
        raise ValidationError("Upload URL must include exactly one non-empty upload_id")

    return upload_url


def _extract_register_file_source_id(result: Any, filename: str) -> str | None:
    """Locate the SOURCE_ID string in an ADD_SOURCE_FILE response.

    Only trusted ADD_SOURCE_FILE shapes are accepted: explicit source-id fields
    and the legacy singleton list envelope (``[[id]]`` / ``[[[[id]]]]``).
    Arbitrary nested ids are intentionally ignored so ambiguous responses fall
    through to the post-register source-list probe.
    """
    field_candidates = _extract_source_id_field_candidates(result, filename)
    if len(field_candidates) == 1:
        (candidate,) = field_candidates  # exactly one (guarded); unpack avoids name[int]
        return candidate
    if len(field_candidates) > 1:
        return None

    row_candidates = _extract_contextual_source_id_row_candidates(result, filename)
    if len(row_candidates) == 1:
        (candidate,) = row_candidates  # exactly one (guarded); unpack avoids name[int]
        return candidate
    if len(row_candidates) > 1:
        return None

    prefixed_candidate = _extract_prefixed_singleton_source_id_envelope(result, filename)
    if prefixed_candidate is not None:
        return prefixed_candidate

    return _extract_singleton_source_id_envelope(result, filename)


def _extract_source_id_field_candidates(result: Any, filename: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: Any) -> None:
        candidate = _coerce_source_id_candidate(value, filename)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    def walk(node: Any, depth: int) -> None:
        if depth > _SOURCE_ID_ENVELOPE_MAX_DEPTH:
            return
        if isinstance(node, dict):
            names = _source_context_names(node)
            matched_context = bool(names) and any(
                _coerce_filename_candidate(name) == filename for name in names
            )
            mismatched_context = bool(names) and not matched_context
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                if (
                    key in _SOURCE_ID_FIELD_NAMES
                    and not mismatched_context
                    and (depth == 0 or matched_context)
                ) or (key in _CONTEXTUAL_SOURCE_ID_FIELD_NAMES and matched_context):
                    add_candidate(value)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    walk(result, 0)
    return candidates


def _extract_singleton_source_id_envelope(result: Any, filename: str) -> str | None:
    node, depth = _unwrap_singleton_envelope(result)
    if depth == 0:
        return None

    return _coerce_source_id_candidate(node, filename)


def _extract_prefixed_singleton_source_id_envelope(result: Any, filename: str) -> str | None:
    if not isinstance(result, list) or len(result) != 2:
        return None
    prefix, inner = result  # unpack ``[None, inner]``, not index it (ratchet)
    if prefix is not None:
        return None
    return _extract_singleton_source_id_envelope(inner, filename)


def _extract_contextual_source_id_row_candidates(result: Any, filename: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: Any) -> None:
        candidate = _coerce_source_id_candidate(value, filename)
        if candidate is not None and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    def walk(node: Any, depth: int) -> None:
        if depth > _SOURCE_ID_ENVELOPE_MAX_DEPTH:
            return
        if isinstance(node, list):
            if len(node) >= 2:
                first, second, *_rest = node  # unpack pair, not index (ratchet)
                if _coerce_filename_candidate(second) == filename:
                    add_candidate(first)
                if _coerce_filename_candidate(first) == filename:
                    add_candidate(second)
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)

    walk(result, 0)
    return candidates


def _coerce_filename_candidate(value: Any) -> str | None:
    value, _depth = _unwrap_singleton_envelope(value)
    if not isinstance(value, str):
        return None
    return value.strip()


def _coerce_source_id_candidate(value: Any, filename: str) -> str | None:
    value, _depth = _unwrap_singleton_envelope(value)
    if not isinstance(value, str):
        return None
    if len(value) > 1000:
        return None
    candidate = value.strip()
    if not candidate or candidate == filename:
        return None
    if _SOURCE_ID_UUID_PATTERN.match(candidate) or _looks_like_id_string(candidate):
        return candidate
    return None


def _source_context_names(node: dict[Any, Any]) -> list[Any]:
    return [
        value
        for key, value in node.items()
        if isinstance(key, str) and key in _SOURCE_NAME_FIELD_NAMES
    ]


def _unwrap_singleton_envelope(value: Any) -> tuple[Any, int]:
    depth = 0
    while isinstance(value, list) and len(value) == 1 and depth < _SOURCE_ID_ENVELOPE_MAX_DEPTH:
        (value,) = value  # not ``value[0]`` (guard pins len 1): ratchet
        depth += 1
    return value, depth


def _register_response_shape_label(result: Any) -> str:
    if isinstance(result, dict):
        return "object"
    if isinstance(result, list):
        return "array"
    if isinstance(result, str):
        return "string"
    if result is None:
        return "null"
    return type(result).__name__


def _looks_like_id_string(candidate: str) -> bool:
    """Heuristic for the non-UUID fallback in file-source id extraction."""
    if len(candidate) < 4:
        return False
    if any(c in candidate for c in " \t/"):
        return False
    return any(c.isdigit() or c in "-_" for c in candidate)


def _resolve_upload_content_type(file_path: Path, mime_type: str | None) -> str:
    """Return the content type for the Scotty resumable-upload start request."""
    if mime_type is not None:
        content_type = mime_type.strip()
        if not content_type:
            raise ValidationError("mime_type cannot be empty or whitespace-only")
        return content_type

    guessed, _encoding = mimetypes.guess_type(file_path.name)
    if guessed:
        return guessed
    # Fall back to the pinned overrides before the opaque octet-stream default
    # (see ``_EXTENSION_CONTENT_TYPES`` for why; #1627).
    return _EXTENSION_CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")


def _normalize_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _transient_error_types_for_upload(content_type: str) -> tuple[int | None, ...]:
    """Return source status=ERROR transient policy for this upload."""
    normalized = _normalize_content_type(content_type)
    if (
        normalized.startswith(_MEDIA_CONTENT_TYPE_PREFIXES)
        or normalized in _MEDIA_APPLICATION_CONTENT_TYPES
    ):
        return _MEDIA_TRANSIENT_ERROR_TYPES
    return _STRICT_TRANSIENT_ERROR_TYPES


def _validate_upload_file_supported(file_path: Path, content_type: str) -> None:
    """Reject local file types known to fail NotebookLM's upload endpoint."""
    normalized = _normalize_content_type(content_type)
    if (
        file_path.suffix.lower() in _HTML_UPLOAD_SUFFIXES
        or normalized in _HTML_UPLOAD_CONTENT_TYPES
    ):
        raise ValidationError(
            "HTML file uploads are not supported by NotebookLM's upload endpoint: "
            f"{file_path.name}. Convert the page to .txt, .md, or .pdf first, then retry."
        )


def _raise_from_upload_http_status(exc: httpx.HTTPStatusError, filename: str) -> NoReturn:
    """Convert a raw ``httpx.HTTPStatusError`` from the file-upload endpoint into a
    classified :class:`~notebooklm.exceptions.NotebookLMError` (issue #1892).

    NotebookLM's resumable-upload endpoint (``/upload/_/``) answers with an HTTP
    status, not a batchexecute envelope, so it never passes through the RPC
    executor's status mapper — the client layer must map it here or a raw
    ``httpx.HTTPStatusError`` leaks to every caller (the MCP ``/files/ul`` route
    turned it into an opaque 500; the CLI/REST ``add_file`` path leaked it too).

    The status classes mirror the RPC executor's contract (429 →
    :class:`RateLimitError`, 401/403 and **3xx** → :class:`AuthError`, 5xx →
    :class:`ServerError`) with one deliberate deviation: a generic **4xx** is
    raised as :class:`ValidationError`, not ``ClientError``. The endpoint returns
    **400** when it rejects the file *type/content* (e.g. a ``.pub`` that slips
    past the local :func:`_validate_upload_file_supported` allowlist), which is the
    same bad-input outcome as that local check — so it surfaces as a clean,
    redacted 4xx through the MCP/REST adapters instead of a gateway 5xx/500.

    ``httpx.raise_for_status`` raises for **any** non-2xx (the upload clients do
    not follow redirects), so an unfollowed **3xx** reaches here too. A redirect
    during upload means the request was bounced (typically an expired/invalid
    Google session redirected to a login page) rather than accepted, so it is
    classified as auth — NOT swept into the ``ValidationError`` "unsupported file"
    catch-all, which would mislabel a dead session as a bad file.
    """
    status = exc.response.status_code
    reason = exc.response.reason_phrase
    if status == 429:
        retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
        msg = f"Upload of {filename!r} was rate limited"
        if retry_after:
            msg += f"; retry after {retry_after} seconds"
        raise RateLimitError(msg, retry_after=retry_after) from exc
    if status in (401, 403):
        raise AuthError(f"Authentication failed uploading {filename!r} (HTTP {status})") from exc
    if 300 <= status < 400:
        raise AuthError(
            f"Upload of {filename!r} was redirected (HTTP {status}); the Google session "
            "may have expired — re-run `notebooklm login`."
        ) from exc
    if status >= 500:
        raise ServerError(
            f"NotebookLM upload endpoint returned {status} for {filename!r}: {reason}",
            status_code=status,
        ) from exc
    # Remaining case: a 4xx client rejection (raise_for_status fired, and 3xx/5xx
    # are handled above). The request/file was rejected — treat it as invalid
    # input, same category as the local unsupported-type check, so it becomes a
    # clean 4xx rather than an opaque 500.
    raise ValidationError(
        f"NotebookLM rejected the upload of {filename!r} (HTTP {status}: {reason}). "
        "The file type or content may be unsupported."
    ) from exc


def raise_for_upload_status(response: httpx.Response, filename: str) -> None:
    """``response.raise_for_status()`` that classifies a failure via
    :func:`_raise_from_upload_http_status` instead of leaking the raw
    ``httpx.HTTPStatusError`` to callers (issue #1892)."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_from_upload_http_status(exc, filename)


GetSourceLimit = Callable[[], Awaitable[int | None]]

_SOURCE_LIMIT_HINT_FLOOR = 50
_TIER_SOURCE_LIMITS_SUMMARY = "50/100/300/600"


async def _build_invalid_argument_source_limit_hint(
    *,
    source_count: int | None,
    get_source_limit: GetSourceLimit | None,
    logger: Any,
) -> str:
    """Build a best-effort hint for ADD_SOURCE_FILE status code 3 failures."""
    source_limit: int | None = None
    if get_source_limit is not None:
        try:
            source_limit = await get_source_limit()
        except Exception:  # noqa: BLE001 - hint lookup must not mask the upload error.
            logger.debug(
                "register_file_source: source-limit lookup failed; continuing without limit hint",
                exc_info=True,
            )

    if source_limit is not None and source_limit <= 0:
        source_limit = None

    if source_count is not None and source_limit is not None:
        if source_count >= source_limit:
            return (
                f" Notebook currently has {source_count}/{source_limit} sources, "
                "so this likely means the notebook has reached its tier-specific "
                "per-notebook source limit. Delete sources or try a fresh notebook, "
                "then retry."
            )
        return (
            f" Notebook currently has {source_count}/{source_limit} sources, below "
            "the advertised account limit. If the file is valid, try the same add "
            "in a fresh notebook to distinguish file rejection from notebook state."
        )

    if source_count is not None and source_count >= _SOURCE_LIMIT_HINT_FLOOR:
        return (
            f" Notebook currently has {source_count} sources; status code 3 can "
            "indicate the notebook is at or near the tier-specific per-notebook "
            f"source limit ({_TIER_SOURCE_LIMITS_SUMMARY}). Delete sources or "
            "try a fresh notebook, then retry."
        )

    if source_limit is not None:
        return (
            f" Advertised source limit for this tier is {source_limit}; compare "
            "it with this notebook's source count. Status code 3 can indicate a "
            "per-notebook source-limit rejection."
        )

    return ""
