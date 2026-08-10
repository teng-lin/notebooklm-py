"""Cookie conversion and jar helpers for authentication.

This private module is safe to import directly. Runtime cookie policy lives in
:mod:`notebooklm._auth.cookie_policy`; ``notebooklm.auth`` passively re-exports
the compatibility names.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias, cast

import httpx

from ..paths import get_storage_path
from . import cookie_policy as _cookie_policy
from . import cookie_semantics as _cookie_semantics
from .cookie_types import Cookie, CookieIdentity, CookieJar
from .paths import resolve_auth_json_env

logger = logging.getLogger("notebooklm.auth")


class StorageStateValidationError(ValueError):
    """Storage JSON has no usable Playwright ``cookies`` list."""


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
# Local alias to the canonical validator. The validator reads policy constants
# from ``_auth.cookie_policy`` at call time; tests that rebind policy state
# should patch that owning module directly.
_validate_required_cookies = _cookie_policy._validate_required_cookies
RequiredCookieValidationError = _cookie_policy.RequiredCookieValidationError
_CookieRowError = _cookie_semantics.CookieRowError


class _SanitizedCookieEntry(dict[str, Any]):
    """Marker for a row already sanitized within the current load operation."""


@dataclass(frozen=True, slots=True, repr=False)
class _LoadedCookiePair:
    """One raw-state sample projected to live and persistence-safe forms."""

    live: httpx.Cookies = field(repr=False)
    baseline: CookieJar = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.live, httpx.Cookies) or not isinstance(self.baseline, CookieJar):
            raise TypeError("loaded cookie pair fields are invalid")
        object.__setattr__(self, "baseline", CookieJar(tuple(self.baseline)))


def _bounded_row_field(entry: Any, field: str) -> str:
    """Return a value-free, bounded diagnostic for one row field."""
    if not isinstance(entry, dict):
        return type(entry).__name__
    value = entry.get(field)
    if isinstance(value, str):
        return value[:80]
    return type(value).__name__


def _sanitize_cookie_entry(entry: Any) -> dict[str, Any] | None:
    """Sanitize one storage/rookiepy row and emit only redacted diagnostics."""
    if isinstance(entry, _SanitizedCookieEntry):
        return entry
    try:
        return _cookie_semantics.sanitize_cookie_entry(entry)
    except _CookieRowError as exc:
        logger.debug(
            "Skipping malformed cookie row name=%s domain=%s row_type=%s error=%s",
            _bounded_row_field(entry, "name"),
            _bounded_row_field(entry, "domain"),
            type(entry).__name__,
            type(exc).__name__,
        )
        return None


def _validate_cookie_shape(entry: Any, *, require_nonempty_value: bool = True) -> dict[str, Any]:
    """Validate identity/value fields without normalizing expiry."""
    return _cookie_semantics.validate_cookie_shape(
        entry, require_nonempty_value=require_nonempty_value
    )


def _sanitized_auth_entries(storage_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structurally safe, allowlisted rows from a storage state."""
    raw_entries = storage_state.get("cookies", [])
    if not isinstance(raw_entries, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw_entry in raw_entries:
        entry = _sanitize_cookie_entry(raw_entry)
        if entry is not None and _is_allowed_auth_domain(entry["domain"]):
            entries.append(entry)
    return entries


def _validate_routable_entries(
    entries: list[dict[str, Any]],
    *,
    to_cookie: Callable[[dict[str, Any]], http.cookiejar.Cookie],
    context: str = "",
    require_routable: bool = True,
) -> None:
    """Run required-cookie validation and, optionally, RFC 6265 preflight."""
    _validate_required_cookies({entry["name"] for entry in entries}, context=context)
    if not require_routable:
        return

    # Keep the cookies -> recovery dependency acyclic.  Recovery owns the
    # actual request-jar projection and the #2057 duplicate/routing predicate.
    # Breaks the cookies <-> psidts_recovery cycle: ``psidts_recovery`` imports
    # THIS module at module scope (it reuses the loaders/converters here), so
    # the reverse edge has to stay function-local.
    from . import psidts_recovery  # noqa: PLC0415 (cycle: psidts_recovery -> cookies)

    if not psidts_recovery._psidts_routes_to_rotate(entries, to_cookie=to_cookie):
        raise RequiredCookieValidationError(
            f"Required cookie __Secure-1PSIDTS is not routable{context}.\n{_EXTRACTION_HINT}",
            reason="psidts_unroutable",
        )


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

    def warn_invalid_key(key: Any) -> None:
        logger.warning(
            "Dropping malformed cookie key %r (expected (name, domain[, path]))",
            key,
        )

    return _cookie_semantics.normalize_legacy_cookie_map(
        cookies,
        on_invalid_key=warn_invalid_key,
    )


def flatten_cookie_map(cookies: CookieInput | None) -> FlatCookieMap:
    """Flatten domain-aware cookies for legacy raw Cookie header callers.

    Duplicate-name resolution mirrors :func:`extract_cookies_from_storage`:
    domains are ranked by :func:`_auth_domain_priority` (``.google.com`` >
    dotted app hosts > their no-dot variants > regional > other; both personal
    hosts share a tier at each level, so neither outranks the other).
    The cross-tier case from #375 (e.g. ``OSID`` on ``myaccount.google.com``
    (tier 0) vs ``notebooklm.google.com`` (tier 2)) resolves the same way
    regardless of input order. But tiers are **not** all distinct — several
    domains share tier 3, tier 2, and tier 0 — so for a name duplicated *within*
    one tier the winner is the first occurrence in iteration order and depends
    on input ordering (issue #2057). Within-tier semantics match
    :func:`extract_cookies_from_storage`.

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
    - ``sameSite`` is preserved when a browser-shaped row already carries it;
      rookiepy rows without that attribute continue to default to ``"None"``

    Args:
        rookiepy_cookies: List of cookie dicts from any ``rookiepy.*()`` call.
            Required keys: ``domain``, ``name``, ``value``.

    Returns:
        Dict matching storage_state.json schema: ``{"cookies": [...], "origins": []}``.
        Cookies missing required fields or from non-allowlisted domains are silently skipped.
    """
    converted = []
    for cookie in rookiepy_cookies:
        normalized = _sanitize_cookie_entry(cookie)
        if normalized is None or not _is_allowed_auth_domain(normalized["domain"]):
            continue

        converted.append(_cookie_semantics.rookiepy_row_to_storage_row(normalized))
    return {"cookies": converted, "origins": []}


def extract_cookies_from_storage(storage_state: dict[str, Any]) -> dict[str, str]:
    """Extract Google cookies from Playwright storage state for NotebookLM auth.

    Filters through the canonical auth-domain allowlist: the NotebookLM hosts,
    Google auth hosts (``.google.com`` / ``accounts.google.com`` plus regional
    ccTLDs), Googleusercontent media domains, Drive-ingest domains, and any
    optional sibling-product domains already present because the user opted in
    at extraction time.

    Cookie Priority Rules:
        When the same cookie name exists on multiple domains (e.g., SID on both
        .google.com and .google.com.sg), we use this priority order:

        1. .google.com (base domain) - ALWAYS preferred when present
        2. Dotted app-host domains (``.notebook.google.com``,
           ``.notebooklm.google.com``) — the Playwright canonical form
        3. Their no-dot variants (``notebook.google.com``,
           ``notebooklm.google.com``)
        4. Regional domains (e.g. .google.de, .google.com.sg, .google.co.uk)
        5. Other allowlisted domains (e.g. .googleusercontent.com)

        Within a single priority tier, the first occurrence in the list wins;
        later duplicates at the same tier are ignored. Tiers are **not** all
        distinct — several domains share tier 3, tier 2, and tier 0 (see
        :func:`notebooklm._auth.cookie_policy._auth_domain_priority`) — so for a
        name duplicated *within* one tier the outcome depends on storage_state
        ordering. Across distinct tiers it is deterministic. See PR #34 for the
        bug this fixes, and issue #2057 for why this ranking must not be used to
        decide whether a cookie would be sent to a particular URL.

    Args:
        storage_state: Parsed JSON from Playwright's storage state file.

    Returns:
        Dict mapping cookie names to values.

    Raises:
        ValueError: If required cookies (SID + ``__Secure-1PSIDTS``) are missing
            from storage state.

    Example:
        >>> storage = {"cookies": [
        ...     {"name": "SID", "value": "regional", "domain": ".google.com.sg"},
        ...     {"name": "SID", "value": "base", "domain": ".google.com"},
        ...     {"name": "__Secure-1PSIDTS", "value": "tts", "domain": ".google.com"},
        ...     {"name": "APISID", "value": "apisid", "domain": ".google.com"},
        ...     {"name": "SAPISID", "value": "sapisid", "domain": ".google.com"},
        ... ]}
        >>> cookies = extract_cookies_from_storage(storage)
        >>> cookies["SID"]
        'base'  # .google.com wins regardless of list order
    """
    cookies = {}
    cookie_domains: dict[str, str] = {}
    cookie_priorities: dict[str, int] = {}

    entries = _sanitized_auth_entries(storage_state)
    for cookie in entries:
        domain = cookie["domain"]
        name = cookie["name"]

        priority = _auth_domain_priority(domain)
        if name not in cookies or priority > cookie_priorities[name]:
            if name in cookies:
                logger.debug(
                    "Cookie %s: using %s value (overriding %s)",
                    name,
                    domain,
                    cookie_domains[name],
                )
            cookies[name] = cookie["value"]
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
        all_domains = {entry["domain"] for entry in entries}
        google_domains = sorted(d for d in all_domains if "google" in d.lower())
        found_names = list(cookies.keys())[:5]
        if found_names:
            extras.append(f"Found cookies: {found_names}{'...' if len(cookies) > 5 else ''}")
        if google_domains:
            extras.append(f"Google domains in storage: {google_domains}")
    _validate_required_cookies(cookie_names, extra_diagnostics=extras)

    return cookies


def resolve_auth_storage_path(path: Path | None, profile: str | None) -> Path | None:
    """Resolve which storage file auth should read, or ``None`` for env auth.

    The single source of the library-side precedence, mirroring
    ``cli.services.auth_source.AuthSource``:

    1. an explicit ``path`` (the ``--storage`` override) wins outright;
    2. inline ``NOTEBOOKLM_AUTH_JSON`` → ``None``, so the loaders read the env
       var and nothing writes to disk;
    3. otherwise the profile's ``storage_state.json``.

    A profile does **not** re-resolve a file when env auth is present. Three
    call sites used to spell this predicate themselves and two of them ranked
    profile above the env var, so ``--profile x`` with ``NOTEBOOKLM_AUTH_JSON``
    set produced a client whose CSRF/session tokens were minted from the
    profile file while its cookie jar came from the env var — two accounts in
    one client (#2083). Keep the rule here, not at the call sites.

    Presence, not truthiness: an empty ``NOTEBOOKLM_AUTH_JSON`` is a
    configuration error :func:`_load_storage_state` reports, not a silent
    fall-through to a file. This matches ``_resolve_recovery_path``. The env-var
    read is centralised in :func:`notebooklm._auth.paths.resolve_auth_json_env`.
    """
    if path is not None:
        return path
    if resolve_auth_json_env() is not None:
        return None
    return get_storage_path(profile=profile)


def _load_storage_state(path: Path | None = None) -> dict[str, Any]:
    """Load Playwright storage state from file or environment variable.

    This is a shared helper used by load_auth_from_storage() and load_httpx_cookies()
    to avoid code duplication.

    Precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. Profile storage path from :func:`notebooklm.paths.get_storage_path`
       (``$NOTEBOOKLM_HOME/profiles/<profile>/storage_state.json`` with legacy
       home-root fallback for the default profile)

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Parsed storage state dict.

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If JSON is malformed or empty.
    """
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(
                f"Storage file not found: {path}\nRun 'notebooklm login' to authenticate first."
            )
        storage_state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(storage_state, dict) or not isinstance(
            storage_state.get("cookies"), list
        ):
            raise StorageStateValidationError(
                "Storage state must contain a 'cookies' list.\n"
                'Expected format: {"cookies": [{"name": "SID", "value": "...", ...}]}'
            )
        return storage_state

    env_auth_json = resolve_auth_json_env()
    if env_auth_json is not None:
        return _load_storage_state_from_env_value(env_auth_json)

    storage_path = get_storage_path()
    if not storage_path.exists():
        raise FileNotFoundError(
            f"Storage file not found: {storage_path}\nRun 'notebooklm login' to authenticate first."
        )

    storage_state = json.loads(storage_path.read_text(encoding="utf-8"))
    if not isinstance(storage_state, dict) or not isinstance(storage_state.get("cookies"), list):
        raise StorageStateValidationError(
            "Storage state must contain a 'cookies' list.\n"
            'Expected format: {"cookies": [{"name": "SID", "value": "...", ...}]}'
        )
    return storage_state


def _load_storage_state_from_env_value(env_auth_json: str) -> dict[str, Any]:
    """Parse one already-captured inline-auth value without rereading ambient state."""
    auth_json = env_auth_json.strip()
    if not auth_json:
        raise StorageStateValidationError(
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
    if not isinstance(storage_state, dict) or not isinstance(storage_state.get("cookies"), list):
        raise StorageStateValidationError(
            "NOTEBOOKLM_AUTH_JSON must contain valid Playwright storage state "
            "with a 'cookies' key.\n"
            'Expected format: {"cookies": [{"name": "SID", "value": "...", ...}]}'
        )
    return storage_state


def load_httpx_cookies(path: Path | None = None) -> httpx.Cookies:
    """Load cookies as an httpx.Cookies object for authenticated downloads.

    Unlike load_auth_from_storage() which returns a simple dict, this function
    returns a proper httpx.Cookies object with domain information preserved.
    This is required for downloads that follow redirects across Google domains.

    Supports the same precedence as load_auth_from_storage():
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable
    3. Profile storage path from :func:`notebooklm.paths.get_storage_path`
       (with legacy home-root fallback for the default profile)

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        httpx.Cookies object with all Google cookies.

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If required cookies are missing or JSON is malformed.
    """
    # Name-only, deliberately. No recovery follows this loader, and the routing
    # preflight exists to *trigger* a heal — see the rule on
    # ``_build_httpx_cookies_from_storage_state``. Asking it here would reject a
    # ``__Secure-1PSIDTS`` scoped to the app host, which is a cookie the download
    # host receives and the rotate URL does not: unrotatable, but not unusable.
    storage_state = _load_storage_state(path)
    return _build_httpx_cookies_from_storage_state(
        storage_state,
        context=" for downloads",
        require_routable=False,
    )


def extract_cookies_with_domains(
    storage_state: dict[str, Any],
    *,
    validate_required: bool = True,
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
        ValueError: If ``validate_required`` is true and required cookies (SID +
            ``__Secure-1PSIDTS``) are missing from storage state.
    """
    cookie_map: DomainCookieMap = {}

    for cookie in _sanitized_auth_entries(storage_state):
        name = cookie["name"]
        domain = cookie["domain"]
        value = cookie["value"]
        key = (name, domain, cookie["path"])
        if key not in cookie_map:
            cookie_map[key] = value

    if validate_required:
        _validate_required_cookies({name for name, _, _ in cookie_map})
    return cookie_map


def _load_cookies_pure(path: Path | None = None, *, require_routable: bool = True) -> httpx.Cookies:
    """PURE inner loader: file I/O + validation ONLY — never any network.

    This is the network-free half of :func:`build_httpx_cookies_from_storage`.
    It reads the storage state (file, inline ``NOTEBOOKLM_AUTH_JSON``, or the
    resolved profile), builds the domain-preserving jar, and runs the
    required-cookie + RFC 6265 routing preflight. On a validation failure it
    raises :class:`RequiredCookieValidationError` carrying a closed-enum
    ``reason`` (:data:`~notebooklm._auth.cookie_policy.RequiredCookieReason` —
    ``"missing_cookie"`` or ``"psidts_unroutable"``) and STOPS. It does not fire
    the inline ``RotateCookies`` recovery POST, and does not touch the network
    under any input; composing recovery on top of the typed reason is the job of
    the public wrapper below (issue #2061 / event-loop-blocking fix). Callers on
    an event loop must run the public wrapper via ``asyncio.to_thread`` so the
    (blocking) file read and any recovery POST stay off the loop.

    ``require_routable`` toggles the RFC 6265 routing preflight; pass it ``True``
    only where a recovery attempt follows in the wrapper. See
    :func:`_build_httpx_cookies_from_storage_state` for why a loader with no
    recovery arm must stay name-only.
    """
    storage_state = _load_storage_state(path)
    return _build_httpx_cookies_from_storage_state(storage_state, require_routable=require_routable)


def _load_cookie_pair_pure(
    path: Path | None = None, *, require_routable: bool = True
) -> _LoadedCookiePair:
    """Load one raw state sample into its paired live and typed projections."""
    storage_state = _load_storage_state(path)
    return _build_cookie_pair_from_storage_state(
        storage_state,
        require_routable=require_routable,
    )


def _default_heal_policy() -> object:
    from .psidts_recovery import HealPolicy

    return HealPolicy.HEAL_THEN_NAME_ONLY


def load_session_jar(
    path: Path | None = None,
    policy: object = _default_heal_policy(),
    *,
    heal: Callable[[Path | None], bool] | None = None,
) -> httpx.Cookies:
    """Load the session jar through the shared heal/retry composition."""
    from . import psidts_recovery

    return psidts_recovery.load_with_recovery(
        path,
        cast(psidts_recovery.HealPolicy, policy),
        load=_load_cookies_pure,
        heal=heal,
    )


def build_httpx_cookies_from_storage(path: Path | None = None) -> httpx.Cookies:
    """Build an httpx.Cookies jar with original domains preserved.

    This function loads cookies from storage and creates a proper httpx.Cookies
    jar with the original domains intact. This is critical for cross-domain
    redirects (e.g., to accounts.google.com for token refresh) to work correctly.

    It is the PUBLIC WRAPPER over :func:`_load_cookies_pure`: it composes the
    pure (network-free) load with the explicit inline ``__Secure-1PSIDTS``
    recovery POST. Synchronous callers (CLI login, ``playwright_login``) invoke
    it directly and see unchanged behavior; async callers must offload it with
    ``asyncio.to_thread`` so the blocking recovery POST + disk write never freeze
    the event loop.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        httpx.Cookies jar with all cookies set to their original domains.

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        ValueError: If required cookies are missing or JSON is malformed.
    """
    # The load -> heal -> retry sequence has ONE owner (``psidts_recovery``),
    # which also owns the heal and the routing predicate; this name, its
    # signature and its patch seam are unchanged (ADR-0017). Inline
    # ``__Secure-1PSIDTS`` recovery (issue #865) must hang off this loader and
    # not only off ``load_auth_from_storage``, because ``AuthTokens.from_storage``
    # and ``NotebookLMClient.from_storage`` come through here.
    return load_session_jar(path)


def _build_cookie_pair_from_storage(path: Path | None = None) -> _LoadedCookiePair:
    """Load/heal/retry once and retain the exact typed baseline from that sample."""
    from . import psidts_recovery  # noqa: PLC0415 (cycle: psidts_recovery -> cookies)

    return psidts_recovery.load_with_recovery(
        path,
        psidts_recovery.HealPolicy.HEAL_THEN_NAME_ONLY,
        load=_load_cookie_pair_pure,
    )


def _build_cookie_pair_from_storage_state(
    storage_state: dict[str, Any],
    *,
    context: str = "",
    require_routable: bool,
) -> _LoadedCookiePair:
    """Project one already-loaded state without losing typed cookie provenance."""
    entries: list[dict[str, Any]] = [
        _SanitizedCookieEntry(entry) for entry in _sanitized_auth_entries(storage_state)
    ]
    converted_rows: list[tuple[dict[str, Any], http.cookiejar.Cookie | None]] = []
    for entry in entries:
        try:
            converted = _cookie_from_normalized_entry(entry, http_only_key="httpOnly")
        except (ValueError, TypeError, OverflowError) as exc:
            logger.debug(
                "Skipping unusable cookie row name=%s domain=%s error=%s",
                _bounded_row_field(entry, "name"),
                _bounded_row_field(entry, "domain"),
                type(exc).__name__,
            )
            converted_rows.append((entry, None))
            continue
        converted_rows.append((entry, converted))

    live = httpx.Cookies()
    baseline: list[Cookie] = []
    seen_keys: set[CookieIdentity] = set()
    converted_by_row = {id(entry): converted for entry, converted in converted_rows}
    for normalized, selected_cookie in converted_rows:
        if selected_cookie is None:
            continue
        key = CookieIdentity(normalized["name"], normalized["domain"], normalized["path"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        live.jar.set_cookie(selected_cookie)
        raw_same_site = normalized.get("sameSite", normalized.get("same_site"))
        baseline.append(
            Cookie(
                name=selected_cookie.name,
                domain=selected_cookie.domain,
                path=selected_cookie.path or "/",
                value=cast(str, selected_cookie.value),
                expires=selected_cookie.expires,
                secure=bool(selected_cookie.secure),
                http_only=_cookie_is_http_only(selected_cookie),
                same_site=raw_same_site if isinstance(raw_same_site, str) else None,
            )
        )

    def reuse_converted(entry: dict[str, Any]) -> http.cookiejar.Cookie:
        converted = converted_by_row.get(id(entry))
        if converted is None:
            raise ValueError("cookie row was unusable")
        return converted

    _validate_routable_entries(
        entries,
        to_cookie=reuse_converted,
        context=context,
        require_routable=require_routable,
    )
    return _LoadedCookiePair(live=live, baseline=CookieJar(baseline))


def _build_httpx_cookies_from_storage_state(
    storage_state: dict[str, Any],
    *,
    context: str = "",
    require_routable: bool,
) -> httpx.Cookies:
    """Build a jar from an already-loaded state without recovery side effects.

    ``require_routable`` adds the RFC 6265 routing preflight on top of the
    required-name check. **Pass it only where a recovery attempt follows.**

    The routing condition asks whether ``__Secure-1PSIDTS`` would be sent to
    the ``RotateCookies`` URL on ``accounts.google.com``. That is a question
    about whether the cookie can be *refreshed*, not about whether it can be
    *used*: one scoped to the app host is delivered on every app request while
    never reaching the rotate URL — unrotatable, but not unusable. Raising on it
    is only justified when the raise is what triggers the heal that fixes it.
    A loader with no recovery arm must stay name-only, or it converts a working
    session into a hard failure it cannot repair (#2061).
    """
    return _build_cookie_pair_from_storage_state(
        storage_state,
        context=context,
        require_routable=require_routable,
    ).live


def _build_httpx_cookies_from_storage_strict(path: Path | None) -> httpx.Cookies:
    """Inner load-and-validate body. No recovery — raises ``ValueError`` directly.

    Name-only for the reason given on :func:`_build_httpx_cookies_from_storage_state`:
    ``fetch_tokens_passive`` uses this loader precisely because it must not fire a
    heal, so it is the wrong place to raise a condition only a heal can clear.

    The sibling of :func:`build_httpx_cookies_from_storage` over the same
    composition, differing only in the policy it selects — which is the point of
    naming the policy: "does this load fire a heal?" is now answered at the call
    site instead of by which of two near-identical private loaders was reached
    for. ``NAME_ONLY`` performs no network I/O.
    """
    from .psidts_recovery import HealPolicy

    return load_session_jar(path, HealPolicy.NAME_ONLY)


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
    if storage_path is not None and storage_path.exists():
        return build_httpx_cookies_from_storage(storage_path)

    jar = httpx.Cookies()
    for (name, domain, path), value in normalize_cookie_map(cookies).items():
        jar.set(name, value, domain=domain, path=path)
    return jar


def _cookie_is_http_only(cookie: Any) -> bool:
    """Return whether an http.cookiejar.Cookie has the HttpOnly marker."""
    return _cookie_semantics.cookie_is_http_only(cookie)


def _cookie_to_storage_state(cookie: Any) -> dict[str, Any]:
    """Convert an http.cookiejar.Cookie to a Playwright storage_state cookie."""
    return _cookie_semantics.cookie_to_storage_row(
        cookie,
        http_only=_cookie_is_http_only(cookie),
        same_site="None",
        include_same_site=True,
    )


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
    normalized = _cookie_semantics.sanitize_cookie_entry(entry)
    return _cookie_from_normalized_entry(normalized, http_only_key="httpOnly")


def _cookie_from_normalized_entry(
    normalized: dict[str, Any], *, http_only_key: str
) -> http.cookiejar.Cookie:
    """Build a ``Cookie`` from a row normalized by ``cookie_semantics``."""
    return _cookie_semantics.cookie_from_normalized_entry(
        normalized,
        http_only_key=http_only_key,
    )


def _safe_to_cookie(
    entry: Any,
    to_cookie: Callable[[dict[str, Any]], http.cookiejar.Cookie] | None = None,
) -> http.cookiejar.Cookie | None:
    """Convert one storage/rookiepy row, returning ``None`` instead of raising.

    ``http.cookiejar.Cookie.__init__`` coerces the expiry eagerly with
    ``int(float(expires))``, so a row whose ``expires`` is ``""``, ``"never"``,
    ``nan`` (``ValueError``), ``inf`` (``OverflowError``), or a list/dict
    (``TypeError``) blows up at *construction*. Cookie rows reach us from Chrome
    via rookiepy or from a hand-editable JSON file, so their shape is not ours to
    guarantee, and one unusable row must not take a whole profile down.

    A row we cannot convert is a row we could never have sent, so dropping it
    leaves the loaders' own validation to speak: the caller gets the actionable
    "Missing required cookies … Run 'notebooklm login'" when the dropped row
    mattered, instead of a raw ``could not convert string to float`` surfacing
    from deep inside the cookie machinery — which, on the recovery paths, was
    raised from *inside* an ``except ValueError:`` handler and replaced the
    diagnostic entirely (issue #2057).

    ``to_cookie`` defaults to :func:`_storage_entry_to_cookie`; the recovery
    module passes :func:`~notebooklm._auth.psidts_recovery._rookiepy_entry_to_cookie`
    for snake_case rookiepy rows.
    """
    normalized = _sanitize_cookie_entry(entry)
    if normalized is None:
        return None

    converter = to_cookie or _storage_entry_to_cookie
    try:
        return converter(normalized)
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug(
            "Skipping unusable cookie row name=%s domain=%s error=%s",
            _bounded_row_field(normalized, "name"),
            _bounded_row_field(normalized, "domain"),
            type(exc).__name__,
        )
        return None


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


def _clone_cookie_jar(source: httpx.Cookies) -> httpx.Cookies:
    """Return a fresh ``httpx.Cookies`` with its own jar container from ``source``.

    A distinct jar CONTAINER (the individual ``http.cookiejar.Cookie`` values are
    shared by reference — they are effectively immutable), so a caller that later
    clears / repopulates / rotates its jar cannot disturb a sibling holding the
    same single-flight recovery result on another loop (see recovery.py's
    coalesced cold / master-token paths — CodeRabbit copy-on-return).
    """
    clone = httpx.Cookies()
    for cookie in source.jar:
        clone.jar.set_cookie(cookie)
    return clone


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


def _update_cookie_input(target: CookieInput, fresh: DomainCookieMap) -> None:
    """Update caller-provided cookies in place while preserving key style.

    The caller's ``target`` may use any of the three accepted shapes (flat
    ``name -> value``, legacy ``(name, domain) -> value``, or path-aware
    ``(name, domain, path) -> value``). The freshly-fetched delta is always the
    path-aware shape; we collapse it back to the caller's original shape so
    they don't observe an in-place type change.
    """
    if any(isinstance(key, tuple) and len(key) == 2 for key in target):
        # Legacy 2-tuple caller. Collapse the path dimension by keeping the
        # first occurrence per (name, domain); for cookies that share name and
        # domain at distinct paths this is lossy, but legacy callers had no
        # way to express path either, so this matches their original contract.
        legacy: dict[tuple[str, str], str] = {}
        for (name, domain, _path), value in fresh.items():
            legacy.setdefault((name, domain), value)
        target.clear()
        target.update(legacy)  # type: ignore[arg-type]
        return

    use_domain_keys = any(isinstance(key, tuple) for key in target)
    target.clear()
    if use_domain_keys:
        target.update(fresh)  # type: ignore[arg-type]
    else:
        target.update(flatten_cookie_map(fresh))  # type: ignore[arg-type]
