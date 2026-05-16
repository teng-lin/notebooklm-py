"""Cookie conversion and jar helpers for authentication.

This private module is safe to import directly, but ``notebooklm.auth`` owns the
compatibility facade for monkeypatched validation policy.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from ..paths import get_storage_path
from . import cookie_policy as _cookie_policy

logger = logging.getLogger("notebooklm.auth")

CookieKey: TypeAlias = tuple[str, str, str]
DomainCookieMap: TypeAlias = dict[CookieKey, str]
FlatCookieMap: TypeAlias = dict[str, str]
# ``CookieInput`` also accepts the legacy ``(name, domain) -> value`` shape that
# pre-#369 callers constructed by hand; :func:`normalize_cookie_map` widens
# those entries to ``(name, domain, "/")`` so the rest of the pipeline sees a
# uniform path-aware shape.
LegacyDomainCookieMap: TypeAlias = dict[tuple[str, str], str]
CookieInput: TypeAlias = DomainCookieMap | LegacyDomainCookieMap | FlatCookieMap

MINIMUM_REQUIRED_COOKIES = _cookie_policy.MINIMUM_REQUIRED_COOKIES
_EXTRACTION_HINT = _cookie_policy._EXTRACTION_HINT
_auth_domain_priority = _cookie_policy._auth_domain_priority
_is_allowed_auth_domain = _cookie_policy._is_allowed_auth_domain
_is_allowed_cookie_domain = _cookie_policy._is_allowed_cookie_domain
# Rebound by notebooklm.auth at import time so moved helpers still honor the
# public facade's monkeypatch-compatible policy state.
_validate_required_cookies = _cookie_policy._validate_required_cookies


def normalize_cookie_map(cookies: CookieInput | None) -> DomainCookieMap:
    """Normalize flat or domain-aware cookie maps into ``(name, domain, path)`` keys.

    Accepts three input shapes for back-compat:

    - Path-aware ``(name, domain, path) -> value`` (the canonical post-#369 shape).
    - Legacy ``(name, domain) -> value`` — kept so external callers that built a
      ``DomainCookieMap`` against the pre-#369 type alias keep working. The
      missing path component defaults to ``/``.
    - Flat ``name -> value`` — assigned to ``.google.com`` / ``/`` for backward
      compatibility with very old callers.
    """
    normalized: DomainCookieMap = {}
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
                logger.warning(
                    "Dropping malformed cookie key %r (expected (name, domain[, path]))",
                    key,
                )
                continue
        else:
            name, domain, path = key, ".google.com", "/"
        if name:
            normalized[(name, domain or ".google.com", path or "/")] = value
    return normalized


def flatten_cookie_map(cookies: CookieInput | None) -> FlatCookieMap:
    """Flatten domain-aware cookies for legacy raw Cookie header callers.

    Duplicate-name resolution mirrors :func:`extract_cookies_from_storage`:
    domains are ranked by :func:`_auth_domain_priority` (``.google.com`` >
    ``.notebooklm.google.com`` > ``notebooklm.google.com`` > regional > other).
    Named tiers are strictly distinct, so the cross-tier case from #375 (e.g.
    ``OSID`` on ``myaccount.google.com`` (tier 0) vs ``notebooklm.google.com``
    (tier 2)) resolves the same way regardless of input order. Within a single
    tier, first occurrence in iteration order wins — matching
    :func:`extract_cookies_from_storage`'s within-tier semantics.

    Path is intentionally collapsed here (#369): the legacy ``Cookie:`` header
    that consumes the flat shape carries only ``name=value`` pairs, with no slot
    for path. When two cookies share ``(name, domain)`` at different paths, the
    first one observed during iteration of the normalized map wins. This is
    deterministic but **not** RFC 6265 §5.4 path-specificity ordering — callers
    that need accurate path-aware behavior must use ``cookie_jar`` or the
    ``DomainCookieMap`` directly.
    """
    flat: FlatCookieMap = {}
    priorities: dict[str, int] = {}

    for (name, domain, _path), value in normalize_cookie_map(cookies).items():
        priority = _auth_domain_priority(domain)
        if name not in flat or priority > priorities[name]:
            flat[name] = value
            priorities[name] = priority

    return flat


def convert_rookiepy_cookies_to_storage_state(
    rookiepy_cookies: list[dict],
) -> dict[str, Any]:
    """Convert rookiepy cookie dicts to Playwright storage_state.json format.

    Key mappings:
    - ``http_only`` → ``httpOnly`` (snake_case to camelCase)
    - ``expires=None`` → ``expires=-1`` (Playwright convention for session cookies)
    - ``sameSite`` always ``"None"`` for cross-site Google cookies

    Args:
        rookiepy_cookies: List of cookie dicts from any ``rookiepy.*()`` call.
            Required keys: ``domain``, ``name``, ``value``.

    Returns:
        Dict matching storage_state.json schema: ``{"cookies": [...], "origins": []}``.
        Cookies missing required fields or from non-Google domains are silently skipped.
    """
    converted = []
    for cookie in rookiepy_cookies:
        domain = cookie.get("domain", "")
        name = cookie.get("name", "")
        value = cookie.get("value", "")

        if not name or not value or not domain:
            continue

        if not _is_allowed_auth_domain(domain):
            continue

        path = cookie.get("path", "/")
        http_only = cookie.get("http_only", False)
        secure = cookie.get("secure", False)
        expires = cookie.get("expires")

        converted.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "expires": expires if expires is not None else -1,
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": "None",
            }
        )
    return {"cookies": converted, "origins": []}


def extract_cookies_from_storage(storage_state: dict[str, Any]) -> dict[str, str]:
    """Extract Google cookies from Playwright storage state for NotebookLM auth.

    Filters cookies to include those from .google.com, notebooklm.google.com,
    .googleusercontent.com domains, and regional Google domains
    (e.g., .google.com.sg, .google.com.au). The regional domains are needed
    because Google sets SID cookies on country-specific domains for users
    in those regions.

    Cookie Priority Rules:
        When the same cookie name exists on multiple domains (e.g., SID on both
        .google.com and .google.com.sg), we use this priority order:

        1. .google.com (base domain) - ALWAYS preferred when present
        2. .notebooklm.google.com (Playwright canonical NotebookLM subdomain)
        3. notebooklm.google.com (no-dot NotebookLM subdomain)
        4. Regional domains (e.g. .google.de, .google.com.sg, .google.co.uk)
        5. Other allowlisted domains (e.g. .googleusercontent.com)

        Within a single priority tier, the first occurrence in the list wins;
        later duplicates at the same tier are ignored. Tiers are distinct so the
        outcome is deterministic regardless of storage_state ordering. See PR #34
        for the bug this fixes.

    Args:
        storage_state: Parsed JSON from Playwright's storage state file.

    Returns:
        Dict mapping cookie names to values.

    Raises:
        ValueError: If required cookies (SID) are missing from storage state.

    Example:
        >>> storage = {"cookies": [
        ...     {"name": "SID", "value": "regional", "domain": ".google.com.sg"},
        ...     {"name": "SID", "value": "base", "domain": ".google.com"},
        ... ]}
        >>> cookies = extract_cookies_from_storage(storage)
        >>> cookies["SID"]
        'base'  # .google.com wins regardless of list order
    """
    cookies = {}
    cookie_domains: dict[str, str] = {}
    cookie_priorities: dict[str, int] = {}

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        name = cookie.get("name")
        if not _is_allowed_auth_domain(domain) or not name:
            continue

        priority = _auth_domain_priority(domain)
        if name not in cookies or priority > cookie_priorities[name]:
            if name in cookies:
                logger.debug(
                    "Cookie %s: using %s value (overriding %s)",
                    name,
                    domain,
                    cookie_domains[name],
                )
            cookies[name] = cookie.get("value", "")
            cookie_domains[name] = domain
            cookie_priorities[name] = priority
        else:
            logger.debug(
                "Cookie %s: ignoring duplicate from %s (keeping %s)",
                name,
                domain,
                cookie_domains[name],
            )

    if cookie_domains:
        unique_domains = sorted(set(cookie_domains.values()))
        logger.debug(
            "Extracted %d cookies from domains: %s", len(cookies), ", ".join(unique_domains)
        )
        if "SID" in cookie_domains:
            logger.debug("SID cookie from domain: %s", cookie_domains["SID"])

    cookie_names = set(cookies.keys())
    extras: list[str] = []
    if not MINIMUM_REQUIRED_COOKIES.issubset(cookie_names):
        all_domains = {c.get("domain", "") for c in storage_state.get("cookies", [])}
        google_domains = sorted(d for d in all_domains if "google" in d.lower())
        found_names = list(cookies.keys())[:5]
        if found_names:
            extras.append(f"Found cookies: {found_names}{'...' if len(cookies) > 5 else ''}")
        if google_domains:
            extras.append(f"Google domains in storage: {google_domains}")
    _validate_required_cookies(cookie_names, extra_diagnostics=extras)

    return cookies


def _load_storage_state(path: Path | None = None) -> dict[str, Any]:
    """Load Playwright storage state from file or environment variable.

    This is a shared helper used by load_auth_from_storage() and load_httpx_cookies()
    to avoid code duplication.

    Precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. File at $NOTEBOOKLM_HOME/storage_state.json (or ~/.notebooklm/storage_state.json)

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Parsed storage state dict.

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If JSON is malformed or empty.
    """
    if path:
        if not path.exists():
            raise FileNotFoundError(
                f"Storage file not found: {path}\nRun 'notebooklm login' to authenticate first."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    if "NOTEBOOKLM_AUTH_JSON" in os.environ:
        auth_json = os.environ["NOTEBOOKLM_AUTH_JSON"].strip()
        if not auth_json:
            raise ValueError(
                "NOTEBOOKLM_AUTH_JSON environment variable is set but empty.\n"
                "Provide valid Playwright storage state JSON or unset the variable."
            )
        try:
            storage_state = json.loads(auth_json)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in NOTEBOOKLM_AUTH_JSON environment variable: {e}\n"
                f"Ensure the value is valid Playwright storage state JSON."
            ) from e
        if not isinstance(storage_state, dict) or "cookies" not in storage_state:
            raise ValueError(
                "NOTEBOOKLM_AUTH_JSON must contain valid Playwright storage state "
                "with a 'cookies' key.\n"
                'Expected format: {"cookies": [{"name": "SID", "value": "...", ...}]}'
            )
        return storage_state

    storage_path = get_storage_path()
    if not storage_path.exists():
        raise FileNotFoundError(
            f"Storage file not found: {storage_path}\nRun 'notebooklm login' to authenticate first."
        )

    return json.loads(storage_path.read_text(encoding="utf-8"))


def load_httpx_cookies(path: Path | None = None) -> httpx.Cookies:
    """Load cookies as an httpx.Cookies object for authenticated downloads.

    Unlike load_auth_from_storage() which returns a simple dict, this function
    returns a proper httpx.Cookies object with domain information preserved.
    This is required for downloads that follow redirects across Google domains.

    Supports the same precedence as load_auth_from_storage():
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable
    3. File at $NOTEBOOKLM_HOME/storage_state.json

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        httpx.Cookies object with all Google cookies.

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If required cookies are missing or JSON is malformed.
    """
    storage_state = _load_storage_state(path)

    cookies = httpx.Cookies()
    cookie_names: set[str] = set()

    for entry in storage_state.get("cookies", []):
        domain = entry.get("domain", "")
        name = entry.get("name", "")
        value = entry.get("value", "")

        if _is_allowed_cookie_domain(domain) and name and value:
            cookies.jar.set_cookie(_storage_entry_to_cookie(entry))
            cookie_names.add(name)

    _validate_required_cookies(cookie_names, context=" for downloads")

    return cookies


def extract_cookies_with_domains(
    storage_state: dict[str, Any],
) -> DomainCookieMap:
    """Extract Google cookies from storage state preserving original identity.

    Returns a path-aware ``(name, domain, path) -> value`` map per RFC 6265 §5.3.
    Two cookies sharing ``(name, domain)`` at distinct paths survive as
    independent entries instead of one silently shadowing the other (issue #369).

    Args:
        storage_state: Parsed JSON from Playwright's storage state file.

    Returns:
        Dict mapping ``(cookie_name, domain, path)`` tuples to values.
        Example: ``{("SID", ".google.com", "/"): "abc123"}``.

    Raises:
        ValueError: If required cookies (SID) are missing from storage state.
    """
    cookie_map: DomainCookieMap = {}

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        name = cookie.get("name")
        value = cookie.get("value", "")

        if not _is_allowed_auth_domain(domain) or not name or not value:
            continue

        key = (name, domain, cookie.get("path") or "/")
        if key not in cookie_map:
            cookie_map[key] = value

    _validate_required_cookies({name for name, _, _ in cookie_map})
    return cookie_map


def build_httpx_cookies_from_storage(path: Path | None = None) -> httpx.Cookies:
    """Build an httpx.Cookies jar with original domains preserved.

    This function loads cookies from storage and creates a proper httpx.Cookies
    jar with the original domains intact. This is critical for cross-domain
    redirects (e.g., to accounts.google.com for token refresh) to work correctly.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        httpx.Cookies jar with all cookies set to their original domains.

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        ValueError: If required cookies are missing or JSON is malformed.
    """
    storage_state = _load_storage_state(path)

    cookies = httpx.Cookies()
    seen_names: set[str] = set()
    seen_keys: set[CookieKey] = set()
    for entry in storage_state.get("cookies", []):
        domain = entry.get("domain", "")
        name = entry.get("name")
        value = entry.get("value", "")
        if not _is_allowed_auth_domain(domain) or not name or not value:
            continue
        key = (name, domain, entry.get("path") or "/")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        seen_names.add(name)
        cookies.jar.set_cookie(_storage_entry_to_cookie(entry))

    _validate_required_cookies(seen_names)
    return cookies


def build_cookie_jar(
    cookies: CookieInput | None = None,
    storage_path: Path | None = None,
) -> httpx.Cookies:
    """Build an httpx.Cookies jar with original domains preserved.

    This is the SINGLE authoritative place to construct cookie jars.

    Priority:
    1. If storage_path exists, load from storage with original domains
    2. Otherwise, use provided cookies while preserving domain keys. Legacy
       flat mappings are assigned to .google.com for backward compatibility.

    Args:
        cookies: Path-aware ``(name, domain, path)`` cookie dict (the
            canonical post-#369 shape), legacy ``(name, domain)`` cookie
            dict, or legacy flat ``name -> value`` dict. The latter two are
            widened via :func:`normalize_cookie_map` — missing path defaults
            to ``/``, missing domain to ``.google.com``.
        storage_path: Path to storage_state.json with domain metadata.

    Returns:
        httpx.Cookies jar populated with auth cookies.
    """
    if storage_path and storage_path.exists():
        return build_httpx_cookies_from_storage(storage_path)

    jar = httpx.Cookies()
    for (name, domain, path), value in normalize_cookie_map(cookies).items():
        jar.set(name, value, domain=domain, path=path)
    return jar


def _cookie_is_http_only(cookie: Any) -> bool:
    """Return whether an http.cookiejar.Cookie has the HttpOnly marker."""
    try:
        return bool(
            cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly")
        )
    except AttributeError:
        return False


def _cookie_to_storage_state(cookie: Any) -> dict[str, Any]:
    """Convert an http.cookiejar.Cookie to a Playwright storage_state cookie."""
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "expires": cookie.expires if cookie.expires is not None else -1,
        "httpOnly": _cookie_is_http_only(cookie),
        "secure": cookie.secure,
        "sameSite": "None",
    }


def _storage_entry_to_cookie(entry: dict[str, Any]) -> http.cookiejar.Cookie:
    """Construct a faithful ``http.cookiejar.Cookie`` from a storage_state entry.

    ``httpx.Cookies.set(name, value, domain=...)`` accepts only those three
    fields, so cookies loaded that way drop ``path``, ``secure``, and
    ``httpOnly``. Each load+save round-trip would erode attributes until disk
    stabilized at ``Path=/``, ``secure=false``, ``httpOnly=false`` — silently
    breaking ``__Host-`` prefix invariants and any future server-enforced
    attribute. This helper is the load-side mirror of
    :func:`_cookie_to_storage_state` so the round-trip is lossless. See #365.
    """
    domain = entry.get("domain", "") or ""
    expires = entry.get("expires")
    expires_value = None if expires in (None, -1) else expires
    rest: dict[str, str] = {"HttpOnly": ""} if entry.get("httpOnly") else {}
    return http.cookiejar.Cookie(
        version=0,
        name=entry.get("name", "") or "",
        value=entry.get("value", "") or "",
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=entry.get("path") or "/",
        path_specified=True,
        secure=bool(entry.get("secure", False)),
        expires=expires_value,
        discard=expires_value is None,
        comment=None,
        comment_url=None,
        rest=rest,
    )


def _cookie_key_variants(key: CookieKey) -> set[CookieKey]:
    """Return equivalent host/domain cookie keys for leading-dot domains.

    The path component is preserved verbatim (issue #369): RFC 6265 §5.3 treats
    ``path`` as part of cookie identity, so variants only span the leading-dot
    domain normalization that ``http.cookiejar`` applies.
    """
    name, domain, path = key
    variants = {key}
    if domain.startswith("."):
        variants.add((name, domain[1:], path))
    else:
        variants.add((name, f".{domain}", path))
    return variants


def _find_cookie_for_storage(
    cookies_by_key: dict[CookieKey, Any], key: CookieKey, stored_value: str | None
) -> Any | None:
    """Find the best refreshed cookie for a stored cookie key.

    http.cookiejar normalizes ``Domain=accounts.google.com`` to
    ``.accounts.google.com``. If both the original host-only key and the
    normalized domain key exist, prefer the value that differs from storage
    because that is the refreshed Set-Cookie value. Path is held fixed across
    variants so a same-name sibling on a different path can't be returned by
    accident (issue #369).
    """
    candidates = [
        cookie
        for variant in _cookie_key_variants(key)
        if (cookie := cookies_by_key.get(variant)) is not None
    ]
    if not candidates:
        return None

    for cookie in candidates:
        if cookie.value != stored_value:
            return cookie
    return candidates[0]


def _replace_cookie_jar(target: httpx.Cookies, source: httpx.Cookies) -> None:
    """Replace target jar contents with source jar contents."""
    if target is source:
        return
    target.jar.clear()
    for cookie in source.jar:
        target.jar.set_cookie(cookie)


def _cookie_map_from_jar(cookie_jar: httpx.Cookies) -> DomainCookieMap:
    """Extract a path-aware auth cookie map from an httpx cookie jar.

    Path-aware identity (issue #369) keeps two cookies that share ``(name,
    domain)`` but differ on ``path`` from collapsing into a single map entry
    on the way into ``AuthTokens.cookies``.
    """
    return {
        (cookie.name, cookie.domain, cookie.path or "/"): cookie.value
        for cookie in cookie_jar.jar
        if cookie.name
        and cookie.domain
        and cookie.value is not None
        and _is_allowed_auth_domain(cookie.domain)
    }
