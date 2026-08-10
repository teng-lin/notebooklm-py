"""Dependency-bottom storage-state cookie domain filtering."""

from __future__ import annotations

import logging
from typing import Any

from . import cookie_policy as _cookie_policy
from . import cookie_semantics as _cookie_semantics

logger = logging.getLogger("notebooklm.auth")


def _safe_cookie_shape(cookie: dict[str, Any]) -> str:
    """Return a value-free structural summary of a cookie dictionary."""
    sorted_items = sorted(cookie.items(), key=lambda item: str(item[0]))
    keys = [str(k) for k, _ in sorted_items]
    types = ", ".join(f"{k}: {type(v).__name__}" for k, v in sorted_items)
    return f"keys={keys} types={{{types}}}"


_MALFORMED_ROW_WARNINGS: dict[str, str] = {
    "name": "Skipping storage_state cookie with missing/empty/non-str name (%s)",
    "domain": "Skipping storage_state cookie with non-str domain (%s)",
    "path": "Skipping storage_state cookie with non-str path (%s)",
    "expires": "Skipping storage_state cookie with unusable expires (%s)",
}


def _report_malformed_row(cookie: Any, exc: _cookie_semantics.CookieRowError) -> None:
    """Log one bounded, value-free warning for a rejected row."""
    if exc.field == "row":
        logger.warning(
            "Skipping malformed storage_state cookie entry (not a dict): type=%s",
            type(cookie).__name__,
        )
        return
    if exc.field == "domain" and isinstance(cookie.get("domain", ""), str):
        return
    message = _MALFORMED_ROW_WARNINGS.get(exc.field, "Skipping malformed storage_state cookie (%s)")
    logger.warning(message, _safe_cookie_shape(cookie))


def filter_storage_state_cookies_by_domain_policy(
    state: dict[str, Any],
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> dict[str, Any]:
    """Return a fresh, domain-filtered storage-state cookie projection."""
    allowed_list = _cookie_policy.build_cookie_domain_allowlist(
        include_optional=include_optional, include_domains=include_domains
    )
    allowed: frozenset[str] = frozenset(allowed_list)
    allowed_stripped: frozenset[str] = frozenset(d.lstrip(".").lower() for d in allowed_list)

    def _is_allowed(domain: str) -> bool:
        normalized = domain[1:] if domain.startswith(".") else domain
        return (
            domain in allowed
            or normalized.lower() in allowed_stripped
            or _cookie_policy._is_trusted_google_cookie_domain(domain)
        )

    filtered_cookies: list[dict[str, Any]] = []
    index_by_identity: dict[tuple[str, str, Any], int] = {}

    for cookie in state.get("cookies", []):
        try:
            normalized = _cookie_semantics.sanitize_cookie_entry(cookie, check_value=False)
        except _cookie_semantics.CookieRowError as exc:
            _report_malformed_row(cookie, exc)
            continue
        name = normalized["name"]
        domain = normalized["domain"]
        if not _is_allowed(domain):
            continue

        identity = (name, domain, normalized["path"])
        existing = index_by_identity.get(identity)
        if existing is None:
            index_by_identity[identity] = len(filtered_cookies)
            filtered_cookies.append(cookie)
        else:
            logger.debug(
                "Cookie %s: exact-identity duplicate on (%s, %s); keeping later observation",
                name,
                domain,
                identity[2],
            )
            filtered_cookies[existing] = cookie

    return {
        "cookies": filtered_cookies,
        "origins": [],
    }
