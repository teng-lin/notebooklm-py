"""Container-aware Firefox cookie extraction.

Ports yt-dlp's Firefox Multi-Account Container handling
(https://github.com/yt-dlp/yt-dlp/blob/c8695f52a91f0d2aabbba7b7200c1099bfa9a3e5/yt_dlp/cookies.py#L149-L177)
so callers can target a single container instead of getting every container's
cookies merged into one jar.

``rookiepy.firefox`` selects ``moz_cookies`` without filtering on
``originAttributes``, which silently merges cookies from every Firefox
Multi-Account Container plus the no-container default into a single return.
For users who isolate their Google session in a container (a common privacy
pattern), that produces an inconsistent / wrong session and no warning.

This module bypasses ``rookiepy`` for Firefox when the user requests a
specific container via ``--browser-cookies 'firefox::<name>'`` (or
``firefox::none`` for the explicit no-container default).

Notes on Firefox internals (verified against mozilla-central):

* ``originAttributes`` is a caret-prefixed querystring like
  ``^userContextId=2&firstPartyDomain=…``, NOT JSON.
* ``userContextId=0`` is omitted (Firefox writes an empty string for the
  default container), so "none" means ``originAttributes = ''``, not
  ``userContextId=0``.
* ``cookies.sqlite`` may be open by a running Firefox, so we copy it (and
  any WAL/SHM siblings) to a temp dir before opening.
* Firefox cookies are stored in plaintext — no decryption needed.

Public surface:

* :func:`find_firefox_profile_path`
* :func:`resolve_container_id`
* :func:`extract_firefox_container_cookies`
* :func:`has_container_cookies_in_use`
"""

from __future__ import annotations

import configparser
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

# ``"none"`` is a sentinel for the no-container default. An ``int`` selects a
# specific container by ``userContextId``. ``None`` means "no filter at all"
# (every container plus the default), matching yt-dlp's fall-through branch.
ContainerSelector = int | Literal["none"] | None


def _firefox_root_dirs() -> list[Path]:
    """Return candidate Firefox root directories for the current platform.

    The returned list is platform-dependent; only the first existing entry
    is typically used. Order matches yt-dlp's preference (newer install
    layouts first).
    """
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Firefox"]
    if sys.platform in ("win32", "cygwin"):
        import os

        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        candidates: list[Path] = []
        if appdata:
            candidates.append(Path(appdata) / "Mozilla" / "Firefox")
        if localappdata:
            candidates.append(
                Path(localappdata)
                / "Packages"
                / "Mozilla.Firefox_n80bbvh6b1yt2"
                / "LocalCache"
                / "Roaming"
                / "Mozilla"
                / "Firefox"
            )
        return candidates
    # Linux + everything else.
    import os

    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    return [
        xdg_config / "mozilla" / "firefox",
        home / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / "config" / "mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
    ]


def _resolve_profile_path(root: Path, raw_path: str, is_relative: bool) -> Path:
    """Resolve a ``profiles.ini`` ``Path=`` value to an absolute path."""
    if is_relative:
        return (root / raw_path).resolve()
    return Path(raw_path)


def find_firefox_profile_path() -> Path | None:
    """Locate the default Firefox profile directory.

    Reads ``profiles.ini`` from the first existing Firefox root and returns
    the directory holding ``cookies.sqlite``.

    Resolution order, matching modern Firefox behavior:

    1. Any ``[Install...]`` section's ``Default=`` field (most reliable on
       Firefox 67+; this is what Firefox itself uses to find the profile).
    2. Any profile section whose ``Default=1`` is set (legacy fallback).
    3. The first profile that contains a ``cookies.sqlite`` (best-effort).

    Returns ``None`` if no Firefox installation or profile is found. We
    deliberately don't raise — callers want a clean "no Firefox installed"
    message, not a stack trace.
    """
    for root in _firefox_root_dirs():
        if not root.exists():
            continue
        profiles_ini = root / "profiles.ini"
        if not profiles_ini.is_file():
            continue

        parser = configparser.ConfigParser()
        try:
            parser.read(profiles_ini, encoding="utf-8")
        except (OSError, configparser.Error):
            # best-effort: profiles.ini unreadable for this candidate; try next.
            continue

        # 1. Modern: any [Install...] section names a default profile path.
        for section in parser.sections():
            if not section.startswith("Install"):
                continue
            default = parser.get(section, "Default", fallback=None)
            if default:
                # ``Default=`` here is always relative to profiles.ini's parent.
                candidate = (root / default).resolve()
                if candidate.exists():
                    return candidate

        # 2. Legacy: per-profile ``Default=1``.
        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            if parser.get(section, "Default", fallback="") == "1":
                raw = parser.get(section, "Path", fallback=None)
                if raw:
                    is_rel = parser.get(section, "IsRelative", fallback="0") == "1"
                    candidate = _resolve_profile_path(root, raw, is_rel)
                    if candidate.exists():
                        return candidate

        # 3. Best-effort: first profile with a cookies.sqlite.
        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            raw = parser.get(section, "Path", fallback=None)
            if not raw:
                continue
            is_rel = parser.get(section, "IsRelative", fallback="0") == "1"
            candidate = _resolve_profile_path(root, raw, is_rel)
            if (candidate / "cookies.sqlite").is_file():
                return candidate

    return None


def _normalize_for_match(text: str) -> str:
    """Lower-case and strip whitespace for case-insensitive name matching."""
    return text.strip().lower()


# Built-in containers reference their display name via Fluent l10n IDs of the
# form ``userContext<Name>.label`` (e.g. ``userContextPersonal.label``). We
# extract ``<Name>`` for matching against the user-supplied container name.
_L10N_RE = re.compile(r"userContext(?P<name>[^.]+)\.label")


def _l10n_label_name(l10n_id: str | None) -> str | None:
    if not l10n_id:
        return None
    match = _L10N_RE.fullmatch(l10n_id)
    if match is None:
        return None
    return match.group("name")


def _load_identities(profile_path: Path) -> list[dict[str, Any]]:
    """Load the ``identities`` array from ``containers.json``.

    Returns ``[]`` if ``containers.json`` is missing or unreadable — callers
    interpret that as "this profile has no user-defined containers".
    """
    containers_path = profile_path / "containers.json"
    if not containers_path.is_file():
        return []
    try:
        data = json.loads(containers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    identities = data.get("identities")
    if not isinstance(identities, list):
        return []
    return [item for item in identities if isinstance(item, dict)]


def _identity_display_name(identity: dict[str, Any]) -> str | None:
    """Best-effort display name for a Firefox container identity.

    Built-in containers expose ``l10nID`` (``userContextPersonal.label``);
    user-created ones expose ``name``. Either may be present.
    """
    name = identity.get("name")
    if isinstance(name, str) and name:
        return name
    label = _l10n_label_name(identity.get("l10nID"))
    return label


def resolve_container_id(profile_path: Path, container_spec: str) -> ContainerSelector:
    """Resolve a ``--browser-cookies 'firefox::<spec>'`` to a selector.

    Args:
        profile_path: Firefox profile directory holding ``containers.json``.
        container_spec: The part after ``firefox::``. ``"none"``
            (case-insensitive) selects cookies with no container. Anything
            else is matched against ``containers.json``. The CLI rejects
            empty specs upstream, but a programmatic caller passing one
            still gets ``None`` (interpreted as "no filter" downstream).

    Returns:
        * ``int`` — the matched container's ``userContextId``.
        * ``"none"`` — explicit no-container selection.
        * ``None`` — empty spec (not produced by the CLI).

    Raises:
        ValueError: When ``container_spec`` doesn't match any identity.
            The message lists available container names so the user can fix
            the typo without having to open ``containers.json``.
    """
    spec = container_spec.strip()
    if not spec:
        return None
    if spec.lower() == "none":
        return "none"

    identities = _load_identities(profile_path)
    target = _normalize_for_match(spec)

    # First pass: exact case-insensitive match on the ``name`` field. This is
    # what the user normally types ("Work", "Personal", their own
    # "google-work").
    for identity in identities:
        name = identity.get("name")
        if isinstance(name, str) and _normalize_for_match(name) == target:
            ctx_id = identity.get("userContextId")
            if isinstance(ctx_id, int):
                return ctx_id

    # Second pass: l10nID-derived label, also case-insensitive. Built-in
    # containers don't carry a ``name`` field; their display name comes from
    # ``userContextPersonal.label`` → "Personal", etc.
    for identity in identities:
        label = _l10n_label_name(identity.get("l10nID"))
        if label is not None and _normalize_for_match(label) == target:
            ctx_id = identity.get("userContextId")
            if isinstance(ctx_id, int):
                return ctx_id

    # No match → build a helpful error listing what we did find.
    available = sorted(
        {name for name in (_identity_display_name(i) for i in identities) if name},
        key=str.lower,
    )
    if available:
        joined = ", ".join(repr(name) for name in available)
        raise ValueError(
            f"Firefox container {container_spec!r} not found. "
            f"Available containers: {joined}. "
            "Use 'firefox::none' to select cookies with no container."
        )
    raise ValueError(
        f"Firefox container {container_spec!r} not found and no containers "
        f"are defined in {profile_path / 'containers.json'}. "
        "Use 'firefox::none' or drop the '::<name>' suffix."
    )


def _copy_sqlite_for_read(source: Path, tmpdir: Path) -> Path:
    """Copy ``cookies.sqlite`` (and any WAL/SHM siblings) to ``tmpdir``.

    Firefox keeps ``cookies.sqlite`` open while running. Opening it directly
    risks lock errors and, for WAL-mode DBs, can return stale rows. Copying
    the WAL/SHM sidecars preserves any committed-but-unflushed writes.
    """
    dest = tmpdir / "cookies.sqlite"
    shutil.copy2(source, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, tmpdir / (source.name + suffix))
    return dest


@contextmanager
def _open_cookies_db(cookies_path: Path) -> Iterator[sqlite3.Cursor]:
    """Yield a cursor over a temp copy of ``cookies.sqlite``.

    Centralises the copy-to-tempdir + connect + close dance shared by
    :func:`extract_firefox_container_cookies` and
    :func:`has_container_cookies_in_use`.
    """
    with tempfile.TemporaryDirectory(prefix="notebooklm_ff_") as tmp:
        copy_path = _copy_sqlite_for_read(cookies_path, Path(tmp))
        conn = sqlite3.connect(str(copy_path))
        try:
            yield conn.cursor()
        finally:
            conn.close()


def _firefox_db_uses_millisecond_expiry(cursor: sqlite3.Cursor) -> bool:
    """Return True if this Firefox DB stores expiry in milliseconds.

    Firefox 142+ (schema version >= 16) switched ``expiry`` from seconds to
    milliseconds. Earlier versions still write seconds.

    Ref: https://github.com/mozilla-firefox/firefox/commit/5869af852cd20425165837f6c2d9971f3efba83d
    """
    try:
        row = cursor.execute("PRAGMA user_version;").fetchone()
    except sqlite3.DatabaseError:
        return False
    if not row:
        return False
    try:
        return int(row[0]) >= 16
    except (TypeError, ValueError):
        return False


def _row_to_rookiepy_dict(row: tuple[Any, ...], *, expiry_in_ms: bool) -> dict[str, Any]:
    """Reshape a ``moz_cookies`` row into a rookiepy-compatible dict.

    Columns match the SELECT in :func:`_select_for_container` exactly:
    ``(host, name, value, path, expiry, isSecure, isHttpOnly, sameSite)``.

    rookiepy's Firefox path emits the same shape (verified against rookiepy
    0.5.6), so the resulting list can flow into
    :func:`notebooklm.auth.convert_rookiepy_cookies_to_storage_state`
    unchanged.
    """
    host, name, value, path, expiry, is_secure, is_http_only, same_site = row
    if expiry_in_ms and expiry is not None:
        try:
            expiry = expiry // 1000
        except TypeError:
            # best-effort: cookie expiry is non-numeric; leave as-is.
            pass
    return {
        "domain": host or "",
        "name": name or "",
        "value": value if value is not None else "",
        "path": path or "/",
        "expires": expiry,
        "secure": bool(is_secure),
        "http_only": bool(is_http_only),
        "same_site": same_site,
    }


def _select_for_container(
    cursor: sqlite3.Cursor, container_id: ContainerSelector
) -> list[tuple[Any, ...]]:
    """Run the three-branch ``moz_cookies`` SELECT and return raw rows.

    The two LIKE clauses match ``userContextId=N`` both at end-of-string
    and as ``userContextId=N&…`` in the middle. The ``None`` branch is
    unreachable from the CLI (which routes unscoped ``firefox`` to
    rookiepy) but kept for direct API callers.
    """
    base_select = (
        "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies"
    )
    if isinstance(container_id, int):
        cursor.execute(
            base_select + " WHERE originAttributes LIKE ? OR originAttributes LIKE ?",
            (f"%userContextId={container_id}", f"%userContextId={container_id}&%"),
        )
    elif container_id == "none":
        cursor.execute(base_select + " WHERE NOT INSTR(originAttributes, 'userContextId=')")
    else:
        cursor.execute(base_select)
    return cursor.fetchall()


def extract_firefox_container_cookies(
    profile_path: Path,
    container_id: ContainerSelector,
    domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read ``cookies.sqlite`` filtered to a single container.

    Args:
        profile_path: Firefox profile directory containing ``cookies.sqlite``.
        container_id: Output of :func:`resolve_container_id`.
        domains: Optional list of cookie ``host`` values (including any
            leading dots, e.g. ``".google.com"``) to keep. If ``None``,
            every cookie matching the container filter is returned. Matching
            is suffix-based for entries that start with a dot, exact-match
            otherwise — same convention rookiepy uses.

    Returns:
        Rookiepy-shape dicts with keys ``domain``, ``name``, ``value``,
        ``path``, ``expires``, ``secure``, ``http_only``, ``same_site``.
        Designed to flow directly into
        :func:`notebooklm.auth.convert_rookiepy_cookies_to_storage_state`.

    Raises:
        FileNotFoundError: If ``cookies.sqlite`` is missing.
        sqlite3.DatabaseError: On a malformed DB (re-raised for the caller
            to surface a friendly message).
    """
    cookies_path = profile_path / "cookies.sqlite"
    if not cookies_path.is_file():
        raise FileNotFoundError(f"Firefox cookies database not found at {cookies_path}")

    with _open_cookies_db(cookies_path) as cursor:
        expiry_in_ms = _firefox_db_uses_millisecond_expiry(cursor)
        rows = _select_for_container(cursor, container_id)

    cookies = [_row_to_rookiepy_dict(row, expiry_in_ms=expiry_in_ms) for row in rows]

    if domains is None:
        return cookies

    return [cookie for cookie in cookies if _domain_matches_any(cookie["domain"], domains)]


def _domain_matches_any(host: str, domains: list[str]) -> bool:
    """Replicate rookiepy's domain filter: dot-prefixed = suffix; else exact."""
    if not host:
        return False
    host_l = host.lower()
    for needle in domains:
        if not needle:
            continue
        needle_l = needle.lower()
        if needle_l.startswith("."):
            base = needle_l[1:]
            if host_l == base or host_l.endswith(needle_l):
                return True
        else:
            if host_l == needle_l:
                return True
    return False


def has_container_cookies_in_use(profile_path: Path) -> bool:
    """Return True when this profile has cookies stored inside a container.

    Used by the back-compat warning path for unscoped ``--browser-cookies
    firefox``. The check is cookie-driven: any row whose ``originAttributes``
    contains ``userContextId=`` counts, regardless of whether the matching
    identity in ``containers.json`` is a stock built-in (only carries
    ``l10nID``) or user-created. First-Party-Isolation cookies, which only
    carry ``^firstPartyDomain=…`` without ``userContextId``, do not trigger.

    Failures (missing files, locked DB, parse errors) return ``False`` —
    this is a heuristic for a warning, not a correctness gate.
    """
    cookies_path = profile_path / "cookies.sqlite"
    if not cookies_path.is_file():
        return False

    try:
        with _open_cookies_db(cookies_path) as cursor:
            row = cursor.execute(
                "SELECT 1 FROM moz_cookies "
                "WHERE INSTR(COALESCE(originAttributes, ''), 'userContextId=') > 0 "
                "LIMIT 1"
            ).fetchone()
    except (OSError, sqlite3.DatabaseError):
        return False

    return row is not None
