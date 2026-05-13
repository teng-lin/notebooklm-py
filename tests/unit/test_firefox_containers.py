"""Unit tests for container-aware Firefox cookie extraction.

These tests build a synthetic ``cookies.sqlite`` mirroring Firefox's
``moz_cookies`` schema and prove the three-branch SELECT in
:mod:`notebooklm._firefox_containers` returns the right rows per mode
(``firefox::<name>`` / ``firefox::none`` / unscoped fall-through).

Reference for the Firefox schema:
https://searchfox.org/mozilla-central/source/netwerk/cookie/CookieService.cpp
"""

from __future__ import annotations

import configparser
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from notebooklm._firefox_containers import (
    extract_firefox_container_cookies,
    find_firefox_profile_path,
    has_container_cookies_in_use,
    resolve_container_id,
)

# Firefox's actual moz_cookies schema as of FF142 (schema version 16+).
# We include every column extract_firefox_container_cookies reads plus the
# ``originAttributes`` filter column. ``UNIQUE`` matches Firefox's real
# constraint, which is why duplicate ``(host, name, path)`` rows across
# containers exist in the wild.
_MOZ_COOKIES_SCHEMA = """
CREATE TABLE moz_cookies (
    id INTEGER PRIMARY KEY,
    originAttributes TEXT NOT NULL DEFAULT '',
    name TEXT,
    value TEXT,
    host TEXT,
    path TEXT,
    expiry INTEGER,
    lastAccessed INTEGER,
    creationTime INTEGER,
    isSecure INTEGER,
    isHttpOnly INTEGER,
    inBrowserElement INTEGER DEFAULT 0,
    sameSite INTEGER DEFAULT 0,
    rawSameSite INTEGER DEFAULT 0,
    schemeMap INTEGER DEFAULT 0,
    isPartitionedAttributeSet INTEGER DEFAULT 0,
    CONSTRAINT moz_uniqueid UNIQUE (name, host, path, originAttributes)
);
"""


def _make_cookies_db(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    schema_version: int = 15,
) -> None:
    """Create a synthetic Firefox ``cookies.sqlite`` at ``path``.

    Args:
        path: Where to write the DB file.
        rows: Each dict provides the columns we care about; missing values
            default to sensible Firefox-ish placeholders.
        schema_version: ``PRAGMA user_version``. Set to ``>=16`` to emulate
            FF142's millisecond-expiry behavior.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_MOZ_COOKIES_SCHEMA)
        conn.execute(f"PRAGMA user_version = {schema_version}")
        for row in rows:
            conn.execute(
                "INSERT INTO moz_cookies "
                "(originAttributes, name, value, host, path, expiry, "
                " isSecure, isHttpOnly, sameSite) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("originAttributes", ""),
                    row.get("name", "X"),
                    row.get("value", "v"),
                    row.get("host", ".google.com"),
                    row.get("path", "/"),
                    row.get("expiry"),
                    int(row.get("isSecure", 1)),
                    int(row.get("isHttpOnly", 0)),
                    int(row.get("sameSite", 0)),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_containers_json(path: Path, identities: list[dict[str, Any]] | None = None) -> None:
    """Write a ``containers.json`` mimicking Firefox's real file shape."""
    data = {
        "version": 5,
        "lastUserContextId": 4,
        "identities": identities
        if identities is not None
        else [
            # Stock built-ins always present on a fresh Firefox profile.
            {
                "userContextId": 1,
                "public": True,
                "icon": "fingerprint",
                "color": "blue",
                "l10nID": "userContextPersonal.label",
                "accessKey": "userContextPersonal.accesskey",
            },
            {
                "userContextId": 2,
                "public": True,
                "icon": "briefcase",
                "color": "orange",
                "l10nID": "userContextWork.label",
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# resolve_container_id
# ---------------------------------------------------------------------------


class TestResolveContainerId:
    def test_none_returns_literal_none(self, tmp_path):
        _make_containers_json(tmp_path / "containers.json")
        assert resolve_container_id(tmp_path, "none") == "none"

    def test_none_is_case_insensitive(self, tmp_path):
        _make_containers_json(tmp_path / "containers.json")
        assert resolve_container_id(tmp_path, "NONE") == "none"
        assert resolve_container_id(tmp_path, "None") == "none"

    def test_empty_spec_returns_python_none(self, tmp_path):
        _make_containers_json(tmp_path / "containers.json")
        assert resolve_container_id(tmp_path, "") is None
        assert resolve_container_id(tmp_path, "   ") is None

    def test_match_by_user_name(self, tmp_path):
        _make_containers_json(
            tmp_path / "containers.json",
            identities=[
                {"userContextId": 3, "name": "google-work"},
                {"userContextId": 4, "name": "google-personal"},
            ],
        )
        assert resolve_container_id(tmp_path, "google-work") == 3
        assert resolve_container_id(tmp_path, "google-personal") == 4

    def test_match_by_name_is_case_insensitive(self, tmp_path):
        _make_containers_json(
            tmp_path / "containers.json",
            identities=[{"userContextId": 7, "name": "Work"}],
        )
        assert resolve_container_id(tmp_path, "work") == 7
        assert resolve_container_id(tmp_path, "WORK") == 7

    def test_match_by_l10n_label(self, tmp_path):
        # Built-in containers have l10nID, no name.
        _make_containers_json(tmp_path / "containers.json")
        assert resolve_container_id(tmp_path, "Personal") == 1
        assert resolve_container_id(tmp_path, "Work") == 2

    def test_name_match_preferred_over_l10n(self, tmp_path):
        # If a user happens to name a container "Personal", we should match
        # that one, not the built-in Personal l10nID.
        _make_containers_json(
            tmp_path / "containers.json",
            identities=[
                {"userContextId": 1, "l10nID": "userContextPersonal.label"},
                {"userContextId": 99, "name": "Personal"},
            ],
        )
        assert resolve_container_id(tmp_path, "Personal") == 99

    def test_unknown_name_raises_with_listing(self, tmp_path):
        _make_containers_json(
            tmp_path / "containers.json",
            identities=[
                {"userContextId": 3, "name": "Work"},
                {"userContextId": 4, "name": "Banking"},
            ],
        )
        with pytest.raises(ValueError) as exc_info:
            resolve_container_id(tmp_path, "Nope")
        msg = str(exc_info.value)
        assert "Nope" in msg
        assert "Work" in msg
        assert "Banking" in msg
        assert "firefox::none" in msg

    def test_unknown_name_with_no_containers_json(self, tmp_path):
        # No containers.json present at all → still error, with a different
        # message that points the user at the simpler escape hatch.
        with pytest.raises(ValueError) as exc_info:
            resolve_container_id(tmp_path, "Anything")
        msg = str(exc_info.value)
        assert "Anything" in msg
        assert "containers.json" in msg

    def test_malformed_containers_json_treated_as_empty(self, tmp_path):
        (tmp_path / "containers.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            resolve_container_id(tmp_path, "Work")


# ---------------------------------------------------------------------------
# extract_firefox_container_cookies — the three-branch SELECT
# ---------------------------------------------------------------------------


def _make_two_container_profile(tmp_path: Path) -> Path:
    """Build a Firefox-like profile with two containers + no-container cookies.

    Layout:
        userContextId=1 (Personal) → SID=personal_sid
        userContextId=2 (Work)     → SID=work_sid, plus a YouTube cookie
        no container               → SID=default_sid
    """
    profile = tmp_path / "Profile1"
    profile.mkdir()
    _make_containers_json(
        profile / "containers.json",
        identities=[
            {
                "userContextId": 1,
                "public": True,
                "l10nID": "userContextPersonal.label",
            },
            {
                "userContextId": 2,
                "public": True,
                "l10nID": "userContextWork.label",
            },
        ],
    )
    _make_cookies_db(
        profile / "cookies.sqlite",
        rows=[
            # No-container default. originAttributes='' (Firefox writes this
            # as empty, not 'userContextId=0').
            {
                "originAttributes": "",
                "name": "SID",
                "value": "default_sid",
                "host": ".google.com",
                "path": "/",
                "expiry": 9999999999,
                "isSecure": 1,
            },
            # Personal container. userContextId=1 at end of string.
            {
                "originAttributes": "^userContextId=1",
                "name": "SID",
                "value": "personal_sid",
                "host": ".google.com",
                "path": "/",
                "expiry": 9999999999,
                "isSecure": 1,
            },
            # Work container, userContextId=2 in middle of string (alongside
            # firstPartyDomain, which is what triggers yt-dlp's second LIKE).
            {
                "originAttributes": "^userContextId=2&firstPartyDomain=example.com",
                "name": "SID",
                "value": "work_sid",
                "host": ".google.com",
                "path": "/",
                "expiry": 9999999999,
                "isSecure": 1,
            },
            # Second Work cookie to prove we pull every matching row.
            {
                "originAttributes": "^userContextId=2",
                "name": "PREF",
                "value": "work_pref",
                "host": ".youtube.com",
                "path": "/",
                "expiry": 9999999999,
                "isSecure": 1,
            },
            # Edge case: another container ID we don't request, to confirm
            # the LIKE patterns aren't over-matching (e.g. ``userContextId=12``
            # is not the same as ``userContextId=1``).
            {
                "originAttributes": "^userContextId=12",
                "name": "SID",
                "value": "other_sid",
                "host": ".google.com",
                "path": "/",
                "expiry": 9999999999,
                "isSecure": 1,
            },
        ],
    )
    return profile


class TestExtractFirefoxContainerCookies:
    def test_specific_container_returns_only_its_rows(self, tmp_path):
        profile = _make_two_container_profile(tmp_path)

        rows = extract_firefox_container_cookies(profile, container_id=2)
        # Work container has SID + PREF; everything else must be excluded.
        names = {(r["domain"], r["name"], r["value"]) for r in rows}
        assert names == {
            (".google.com", "SID", "work_sid"),
            (".youtube.com", "PREF", "work_pref"),
        }

    def test_specific_container_does_not_match_prefix_collisions(self, tmp_path):
        # Cookie with userContextId=12 must NOT come back when we ask for 1.
        profile = _make_two_container_profile(tmp_path)
        rows = extract_firefox_container_cookies(profile, container_id=1)
        assert all(r["value"] != "other_sid" for r in rows)
        assert any(r["value"] == "personal_sid" for r in rows)

    def test_none_returns_only_default_rows(self, tmp_path):
        profile = _make_two_container_profile(tmp_path)
        rows = extract_firefox_container_cookies(profile, container_id="none")
        values = {r["value"] for r in rows}
        assert values == {"default_sid"}

    def test_unfiltered_returns_everything(self, tmp_path):
        profile = _make_two_container_profile(tmp_path)
        rows = extract_firefox_container_cookies(profile, container_id=None)
        values = {r["value"] for r in rows}
        assert values == {
            "default_sid",
            "personal_sid",
            "work_sid",
            "work_pref",
            "other_sid",
        }

    def test_domain_filter_drops_non_matches(self, tmp_path):
        profile = _make_two_container_profile(tmp_path)
        rows = extract_firefox_container_cookies(
            profile, container_id=None, domains=[".google.com"]
        )
        domains = {r["domain"] for r in rows}
        # .youtube.com cookie must be excluded.
        assert domains == {".google.com"}

    def test_rookiepy_shape_keys_present(self, tmp_path):
        profile = _make_two_container_profile(tmp_path)
        rows = extract_firefox_container_cookies(profile, container_id=2)
        assert rows
        expected_keys = {
            "domain",
            "name",
            "value",
            "path",
            "expires",
            "secure",
            "http_only",
            "same_site",
        }
        for row in rows:
            assert expected_keys <= row.keys()

    def test_ff142_millisecond_expiry_normalized_to_seconds(self, tmp_path):
        # Schema version 16+ → expiry is in milliseconds. We divide back to
        # seconds so the downstream Playwright storage_state stays correct.
        profile = tmp_path / "Profile1"
        profile.mkdir()
        _make_cookies_db(
            profile / "cookies.sqlite",
            rows=[
                {
                    "originAttributes": "",
                    "name": "SID",
                    "value": "x",
                    "host": ".google.com",
                    "expiry": 9_999_999_999_000,
                }
            ],
            schema_version=16,
        )
        rows = extract_firefox_container_cookies(profile, container_id="none")
        assert rows[0]["expires"] == 9_999_999_999

    def test_missing_cookies_sqlite_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            extract_firefox_container_cookies(tmp_path, container_id="none")


# ---------------------------------------------------------------------------
# has_container_cookies_in_use — the back-compat warning gate
# ---------------------------------------------------------------------------


class TestHasContainerCookiesInUse:
    def test_true_when_user_container_cookies_present(self, tmp_path):
        profile = tmp_path / "Profile"
        profile.mkdir()
        _make_containers_json(
            profile / "containers.json",
            identities=[{"userContextId": 1, "name": "google-work"}],
        )
        _make_cookies_db(
            profile / "cookies.sqlite",
            rows=[
                {"originAttributes": "^userContextId=1", "name": "SID", "value": "x"},
            ],
        )
        assert has_container_cookies_in_use(profile) is True

    def test_true_when_only_builtin_container_cookies(self, tmp_path):
        # A user can sign Google into a stock built-in container (e.g. the
        # bundled "Work") without ever editing containers.json. The cookie
        # still gets ``^userContextId=N``; the warning must fire. Regression
        # guard for the issue raised in the polish review (Codex HIGH).
        profile = tmp_path / "Profile"
        profile.mkdir()
        _make_containers_json(profile / "containers.json")  # built-ins only
        _make_cookies_db(
            profile / "cookies.sqlite",
            rows=[
                {"originAttributes": "^userContextId=2", "name": "SID", "value": "x"},
            ],
        )
        assert has_container_cookies_in_use(profile) is True

    def test_true_without_containers_json(self, tmp_path):
        # If cookies carry ``userContextId=`` we warn even when
        # containers.json is missing — the cookies themselves are the
        # ground truth for "is the user using containers."
        profile = tmp_path / "Profile"
        profile.mkdir()
        _make_cookies_db(
            profile / "cookies.sqlite",
            rows=[
                {"originAttributes": "^userContextId=1", "name": "SID", "value": "x"},
            ],
        )
        assert has_container_cookies_in_use(profile) is True

    def test_false_when_only_first_party_isolation(self, tmp_path):
        # First-Party Isolation gives every cookie a non-empty
        # ``originAttributes`` containing ``firstPartyDomain=…`` but no
        # ``userContextId=``. That's not container usage; don't warn.
        # Regression guard for the polish review (Gemini MEDIUM).
        profile = tmp_path / "Profile"
        profile.mkdir()
        _make_cookies_db(
            profile / "cookies.sqlite",
            rows=[
                {
                    "originAttributes": "^firstPartyDomain=google.com",
                    "name": "SID",
                    "value": "x",
                },
            ],
        )
        assert has_container_cookies_in_use(profile) is False

    def test_false_when_no_container_cookies(self, tmp_path):
        profile = tmp_path / "Profile"
        profile.mkdir()
        _make_containers_json(
            profile / "containers.json",
            identities=[{"userContextId": 9, "name": "unused"}],
        )
        _make_cookies_db(
            profile / "cookies.sqlite",
            rows=[
                {"originAttributes": "", "name": "SID", "value": "x"},
            ],
        )
        assert has_container_cookies_in_use(profile) is False

    def test_false_when_no_cookies_sqlite(self, tmp_path):
        profile = tmp_path / "Profile"
        profile.mkdir()
        _make_containers_json(
            profile / "containers.json",
            identities=[{"userContextId": 9, "name": "user"}],
        )
        assert has_container_cookies_in_use(profile) is False


# ---------------------------------------------------------------------------
# find_firefox_profile_path
# ---------------------------------------------------------------------------


class TestFindFirefoxProfilePath:
    def _write_profiles_ini(
        self, root: Path, content: str
    ) -> None:  # tiny helper; not enough churn to share globally
        root.mkdir(parents=True, exist_ok=True)
        (root / "profiles.ini").write_text(content, encoding="utf-8")

    def test_install_section_wins(self, tmp_path, monkeypatch):
        root = tmp_path / "Firefox"
        profile = root / "Profiles" / "abc.default-release"
        profile.mkdir(parents=True)
        (profile / "cookies.sqlite").touch()
        self._write_profiles_ini(
            root,
            "[Install1234ABCD]\n"
            "Default=Profiles/abc.default-release\n"
            "Locked=1\n"
            "\n"
            "[Profile0]\n"
            "Name=other\n"
            "IsRelative=1\n"
            "Path=Profiles/other.default\n"
            "\n"
            "[General]\n"
            "StartWithLastProfile=1\n",
        )
        monkeypatch.setattr(
            "notebooklm._firefox_containers._firefox_root_dirs",
            lambda: [root],
        )
        assert find_firefox_profile_path() == profile

    def test_falls_back_to_default_equals_1(self, tmp_path, monkeypatch):
        root = tmp_path / "Firefox"
        profile = root / "Profiles" / "xyz.default"
        profile.mkdir(parents=True)
        self._write_profiles_ini(
            root,
            "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/xyz.default\nDefault=1\n",
        )
        monkeypatch.setattr(
            "notebooklm._firefox_containers._firefox_root_dirs",
            lambda: [root],
        )
        assert find_firefox_profile_path() == profile.resolve()

    def test_falls_back_to_first_profile_with_cookies_sqlite(self, tmp_path, monkeypatch):
        root = tmp_path / "Firefox"
        profile = root / "Profiles" / "only.default"
        profile.mkdir(parents=True)
        (profile / "cookies.sqlite").touch()
        # No Install section, no Default=1; we still find it.
        self._write_profiles_ini(
            root,
            "[Profile0]\nName=only\nIsRelative=1\nPath=Profiles/only.default\n",
        )
        monkeypatch.setattr(
            "notebooklm._firefox_containers._firefox_root_dirs",
            lambda: [root],
        )
        assert find_firefox_profile_path() == profile.resolve()

    def test_returns_none_when_no_firefox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "notebooklm._firefox_containers._firefox_root_dirs",
            lambda: [tmp_path / "DoesNotExist"],
        )
        assert find_firefox_profile_path() is None

    def test_malformed_profiles_ini_returns_none(self, tmp_path, monkeypatch):
        root = tmp_path / "Firefox"
        root.mkdir()
        (root / "profiles.ini").write_text("\x00not\x00ini", encoding="utf-8")
        monkeypatch.setattr(
            "notebooklm._firefox_containers._firefox_root_dirs",
            lambda: [root],
        )
        # We deliberately swallow parser errors and try the next root; with
        # only one root that fails, we get None.
        try:
            result = find_firefox_profile_path()
        except configparser.Error:  # pragma: no cover — defensive
            pytest.fail("malformed profiles.ini should not raise to caller")
        assert result is None
