"""Dependency-bottom scalar and row codecs for cookies.

Cookie rows come from browser stores, rookiepy, and hand-editable JSON.  Keep
schema handling and mechanical conversion in this leaf so every loader, value,
and writer agrees about cookie identity, flags, and expiry representation.  The
module makes no policy or logging decisions.
"""

from __future__ import annotations

import http.cookiejar
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


class CookieRowError(ValueError):
    """A cookie row is structurally or semantically unusable.

    The message is deliberately structural only.  Callers may use the
    ``field`` attribute for bounded diagnostics without ever including a
    credential, raw expiry, or arbitrary converter exception text.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid cookie row field {field}: {reason}")


@dataclass(frozen=True)
class NormalizedExpiry:
    """Normalized expiry plus whether it represents a session cookie."""

    value: int | float | None
    session: bool


def normalize_cookie_expiry(raw: Any) -> NormalizedExpiry:
    """Normalize a storage expiry without allowing ``Cookie`` to guess.

    ``None`` and the exact non-boolean integer ``-1`` are Playwright's
    session-cookie forms.  Every other accepted numeric representation is a
    dated value, including ``-1.0`` and ``"-1"``.  Dated values use the same
    ``int(float(value))`` conversion as ``http.cookiejar``.
    """
    if raw is None or (type(raw) is int and raw == -1):
        return NormalizedExpiry(None, True)

    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise CookieRowError("expires", "unsupported type")

    try:
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise CookieRowError("expires", "non-finite number")
        value = int(numeric)
    except CookieRowError:
        raise
    except (ValueError, TypeError, OverflowError) as exc:
        raise CookieRowError("expires", "not numeric") from exc

    # Preserve the distinction between a dated -1 and Playwright's session
    # sentinel after the row is handed to ``http.cookiejar.Cookie``.
    if value == -1:
        return NormalizedExpiry(-1.0, False)
    return NormalizedExpiry(value, False)


def validate_cookie_shape(
    entry: Any, *, check_value: bool = True, require_nonempty_value: bool = True
) -> dict[str, Any]:
    """Validate identity/value fields and return a shallow normalized copy.

    The canonical "is this row usable?" predicate.  The load- and write-path
    spellings are adapters over this one, differing only in how they report a
    defect: :func:`notebooklm._auth.cookies._sanitize_cookie_entry` returns
    ``None`` and logs a redacted diagnostic, the write-time domain filter
    (:func:`notebooklm._auth.storage.filter_storage_state_cookies_by_domain_policy`) maps
    :attr:`CookieRowError.field` to a bounded per-field warning, and callers that
    want the raise take it straight.  Keeping the *checks* here and the *failure
    mode* at the boundary is what stops "is this row usable" from being
    re-derived per call site.

    ``check_value=False`` skips the value field entirely, for the callers that
    gate on identity alone: the write-time filter keeps rows whose value it never
    reads, and the recovery observation deliberately records identities for
    value-less rows so a rotation can replace them.  ``require_nonempty_value``
    narrows the check when it *is* performed (a present-but-empty value is a
    usable identity for the CLI import preview, but not for a request jar).

    Expiry is intentionally not touched here.  Recovery's persisted-liveness
    predicate needs to recognize a structurally valid row with a malformed
    expiry as present on disk, while routing and conversion must reject it.

    Not yet universal: ``_auth/storage.py``'s snapshot helper still re-derives
    the same four checks with a third failure mode (silent ``None``). Routing it
    is a follow-up — ``cookie_semantics`` is a leaf with no first-party imports,
    so the edge would be cycle-free.
    """
    if not isinstance(entry, dict):
        raise CookieRowError("row", "not a dict")

    for field in ("name", "domain"):
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            raise CookieRowError(field, "missing or non-string")

    if check_value:
        value = entry.get("value")
        if not isinstance(value, str) or (require_nonempty_value and not value):
            raise CookieRowError("value", "missing or non-string")

    path = entry.get("path")
    if path is not None and not isinstance(path, str):
        raise CookieRowError("path", "non-string")

    normalized = dict(entry)
    normalized["path"] = path or "/"
    return normalized


def sanitize_cookie_entry(entry: Any, *, check_value: bool = True) -> dict[str, Any]:
    """Validate a row and replace its expiry with a safe canonical value.

    :func:`validate_cookie_shape` plus expiry normalization — the full row
    contract for anything that will be rebuilt into an ``http.cookiejar.Cookie``
    or persisted, since every such path goes through ``int(float(expires))``.
    ``check_value`` is forwarded verbatim.
    """
    normalized = validate_cookie_shape(entry, check_value=check_value)
    normalized["expires"] = normalize_cookie_expiry(entry.get("expires")).value
    return normalized


def normalize_legacy_cookie_map(
    cookies: Mapping[Any, str] | None,
    *,
    on_invalid_key: Callable[[Any], None] | None = None,
) -> dict[tuple[str, str, str], str]:
    """Widen path-aware, legacy two-tuple, and flat cookie maps.

    Invalid tuple arity is skipped.  The optional callback lets a compatibility
    boundary retain its warning without putting a logging decision in this leaf.
    """
    normalized: dict[tuple[str, str, str], str] = {}
    if not cookies:
        return normalized

    for key, value in cookies.items():
        if isinstance(key, tuple):
            if len(key) == 3:
                name, domain, path = key
            elif len(key) == 2:
                name, domain = key
                path = "/"
            else:
                if on_invalid_key is not None:
                    on_invalid_key(key)
                continue
        else:
            name, domain, path = key, ".google.com", "/"
        if name:
            normalized[(name, domain or ".google.com", path or "/")] = value
    return normalized


def rookiepy_row_to_storage_row(normalized: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt one already-normalized rookiepy row to Playwright spelling."""
    same_site = normalized.get("sameSite", normalized.get("same_site"))
    if same_site not in {"Strict", "Lax", "None"}:
        same_site = "None"
    return {
        "name": normalized["name"],
        "value": normalized["value"],
        "domain": normalized["domain"],
        "path": normalized["path"],
        "expires": -1 if normalized["expires"] is None else normalized["expires"],
        "httpOnly": bool(normalized.get("http_only", False)),
        "secure": bool(normalized.get("secure", False)),
        "sameSite": same_site,
    }


def cookie_is_http_only(cookie: Any) -> bool:
    """Observe the two stdlib spellings of the HttpOnly nonstandard flag."""
    try:
        return bool(
            cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly")
        )
    except AttributeError:
        return False


def cookie_from_normalized_entry(
    normalized: Mapping[str, Any], *, http_only_key: str
) -> http.cookiejar.Cookie:
    """Construct a faithful stdlib cookie from a normalized row."""
    expires_value = normalized["expires"]
    domain = normalized["domain"]
    rest: dict[str, str] = {"HttpOnly": ""} if normalized.get(http_only_key) else {}
    return http.cookiejar.Cookie(
        version=0,
        name=normalized["name"],
        value=normalized["value"],
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=normalized["path"],
        path_specified=True,
        secure=bool(normalized.get("secure", False)),
        expires=expires_value,
        discard=expires_value is None,
        comment=None,
        comment_url=None,
        rest=rest,
    )


def cookie_to_storage_row(
    cookie: Any,
    *,
    http_only: bool,
    same_site: str | None = None,
    include_same_site: bool = False,
) -> dict[str, Any]:
    """Serialize cookie-shaped attributes to one Playwright storage row.

    ``http.cookiejar`` coerces a dated ``-1.0`` expiry to integer ``-1``.
    Emitting float ``-1.0`` for every non-session ``-1`` preserves its
    distinction from Playwright's exact integer session sentinel.
    """
    expires = cookie.expires
    if expires is None:
        stored_expires: int | float = -1
    elif expires == -1:
        stored_expires = -1.0
    else:
        stored_expires = expires
    row = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "expires": stored_expires,
        "httpOnly": http_only,
        "secure": cookie.secure,
    }
    if include_same_site:
        row["sameSite"] = same_site
    return row
