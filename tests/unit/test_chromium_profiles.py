"""Unit tests for Chromium-family multi-user-profile cookie discovery.

These tests build a synthetic ``<user-data-dir>/{Default,Profile N}/Cookies``
layout (plus ``Local State``) on disk and prove
:func:`notebooklm.cli._chromium_profiles.discover_chromium_profiles` returns
the right :class:`ChromiumProfile` records for issue #571's fan-out path.

We don't exercise rookiepy decryption here — that's covered against the real
Chrome cookie DB during local validation, and at the
:mod:`tests.unit.cli.test_session` level via mocked ``rookiepy.any_browser``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from notebooklm.cli import _chromium_profiles
from notebooklm.cli._chromium_profiles import (
    discover_chromium_profiles,
    is_chromium_browser,
    resolve_chromium_profile,
)

# ---------------------------------------------------------------------------
# is_chromium_browser
# ---------------------------------------------------------------------------


class TestIsChromiumBrowser:
    @pytest.mark.parametrize(
        "name",
        [
            "chrome",
            "Chrome",
            "CHROMIUM",
            "brave",
            "edge",
            "arc",
            "vivaldi",
            "opera",
            "opera-gx",
            "opera_gx",
            " chrome ",
        ],
    )
    def test_recognized(self, name):
        assert is_chromium_browser(name) is True

    @pytest.mark.parametrize("name", ["firefox", "safari", "auto", "", "librewolf"])
    def test_unrecognized(self, name):
        assert is_chromium_browser(name) is False


# ---------------------------------------------------------------------------
# discover_chromium_profiles
# ---------------------------------------------------------------------------


def _make_chromium_user_data(
    root: Path,
    *,
    profiles: dict[str, str],
    local_state_names: dict[str, str] | None = None,
) -> Path:
    """Build a synthetic Chrome user-data directory at ``root``.

    Args:
        root: Directory to populate (created if missing).
        profiles: Map of ``directory-name -> "populated" | "empty" | "no-dir"``.
            ``"populated"`` writes a ``Cookies`` file; ``"empty"`` creates the
            directory but no Cookies file; ``"no-dir"`` is a no-op (used to
            simulate a never-created profile).
        local_state_names: Optional map of ``directory-name -> human name``
            written into ``Local State``'s ``profile.info_cache``.

    Returns:
        The ``root`` path for convenience.
    """
    root.mkdir(parents=True, exist_ok=True)
    for dir_name, state in profiles.items():
        if state == "no-dir":
            continue
        prof_dir = root / dir_name
        prof_dir.mkdir(parents=True, exist_ok=True)
        if state == "populated":
            (prof_dir / "Cookies").write_bytes(b"SQLite format 3\x00")
    if local_state_names is not None:
        local_state = {
            "profile": {
                "info_cache": {
                    dir_name: {"name": human} for dir_name, human in local_state_names.items()
                }
            }
        }
        (root / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
    return root


@pytest.fixture
def patched_user_data_dir(tmp_path, monkeypatch):
    """Redirect chromium user-data dir lookups at the test's tmp_path.

    Returns the tmp_path the test should populate under
    ``{browser-key}/...``. Discovery is wired so that asking about
    ``"chrome"`` will resolve to ``tmp_path / "chrome"``.
    """

    def _fake_user_data_dir(browser_name: str) -> Path | None:
        return tmp_path / browser_name.lower()

    monkeypatch.setattr(_chromium_profiles, "_user_data_dir", _fake_user_data_dir)
    return tmp_path


class TestDiscoverChromiumProfiles:
    def test_non_chromium_browser_returns_empty(self):
        assert discover_chromium_profiles("firefox") == []
        assert discover_chromium_profiles("safari") == []

    def test_missing_user_data_dir_returns_empty(self, patched_user_data_dir):
        # No directory created for chrome at all.
        assert discover_chromium_profiles("chrome") == []

    def test_finds_default_only_install(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(chrome_root, profiles={"Default": "populated"})

        profiles = discover_chromium_profiles("chrome")
        assert len(profiles) == 1
        assert profiles[0].directory_name == "Default"
        assert profiles[0].browser == "chrome"
        assert profiles[0].cookies_db == chrome_root / "Default" / "Cookies"
        # No Local State → human_name falls back to directory_name.
        assert profiles[0].human_name == "Default"

    def test_finds_multiple_profiles_in_default_then_numeric_order(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={
                "Profile 2": "populated",
                "Profile 10": "populated",
                "Default": "populated",
                "Profile 1": "populated",
            },
            local_state_names={
                "Default": "Personal",
                "Profile 1": "Work",
                "Profile 2": "Side Project",
                "Profile 10": "Tenth",
            },
        )
        profiles = discover_chromium_profiles("chrome")
        # Default first, then Profile 1, 2, 10 in numeric order.
        assert [p.directory_name for p in profiles] == [
            "Default",
            "Profile 1",
            "Profile 2",
            "Profile 10",
        ]
        assert [p.human_name for p in profiles] == [
            "Personal",
            "Work",
            "Side Project",
            "Tenth",
        ]

    def test_skips_empty_profile_dirs_without_cookies_file(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={
                "Default": "populated",
                "Profile 1": "empty",  # dir exists but no Cookies
                "Profile 2": "populated",
            },
        )
        profiles = discover_chromium_profiles("chrome")
        assert [p.directory_name for p in profiles] == ["Default", "Profile 2"]

    def test_skips_unrecognized_directory_names(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        chrome_root.mkdir(parents=True)
        # System / extension / cache dirs that aren't user-data profiles.
        for junk in ("System Profile", "Crashpad", "ShaderCache"):
            (chrome_root / junk).mkdir()
            (chrome_root / junk / "Cookies").write_bytes(b"x")
        # And one real profile.
        _make_chromium_user_data(chrome_root, profiles={"Default": "populated"})

        profiles = discover_chromium_profiles("chrome")
        assert [p.directory_name for p in profiles] == ["Default"]

    def test_human_name_falls_back_when_local_state_missing(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Default": "populated", "Profile 1": "populated"},
            # No local_state_names → no Local State file.
        )
        profiles = discover_chromium_profiles("chrome")
        assert [p.human_name for p in profiles] == ["Default", "Profile 1"]

    def test_malformed_local_state_treated_as_empty(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Default": "populated", "Profile 1": "populated"},
        )
        (chrome_root / "Local State").write_text("not json", encoding="utf-8")
        profiles = discover_chromium_profiles("chrome")
        # Falls back to directory_name; doesn't crash.
        assert [p.human_name for p in profiles] == ["Default", "Profile 1"]

    def test_browser_name_is_case_insensitive(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(chrome_root, profiles={"Default": "populated"})
        assert len(discover_chromium_profiles("Chrome")) == 1
        assert len(discover_chromium_profiles("CHROME")) == 1


# ---------------------------------------------------------------------------
# resolve_chromium_profile
# ---------------------------------------------------------------------------


class TestResolveChromiumProfile:
    def test_unsupported_browser_is_rejected(self):
        with pytest.raises(ValueError, match="not a Chromium-family browser"):
            resolve_chromium_profile("firefox", "Default")

    def test_no_populated_profiles_is_rejected(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Default": "empty", "Profile 1": "empty"},
            local_state_names={"Default": "Personal", "Profile 1": "Work"},
        )

        with pytest.raises(ValueError, match="No populated chrome profiles were found"):
            resolve_chromium_profile("chrome", "Default")

    def test_resolves_by_stable_directory_name(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Default": "populated", "Profile 1": "populated"},
            local_state_names={"Default": "Personal", "Profile 1": "Work"},
        )

        profile = resolve_chromium_profile("chrome", "Profile 1")

        assert profile.directory_name == "Profile 1"
        assert profile.human_name == "Work"

    def test_resolves_by_human_profile_name(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Default": "populated", "Profile 1": "populated"},
            local_state_names={"Default": "Personal", "Profile 1": "Work"},
        )

        profile = resolve_chromium_profile("chrome", "work")

        assert profile.directory_name == "Profile 1"

    def test_directory_name_wins_when_human_names_collide(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Profile 1": "populated", "Profile 2": "populated"},
            local_state_names={"Profile 1": "Work", "Profile 2": "Work"},
        )

        profile = resolve_chromium_profile("chrome", "Profile 2")

        assert profile.directory_name == "Profile 2"

    def test_ambiguous_human_profile_name_lists_directory_names(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Profile 1": "populated", "Profile 2": "populated"},
            local_state_names={"Profile 1": "Work", "Profile 2": "Work"},
        )

        with pytest.raises(ValueError, match="ambiguous") as exc_info:
            resolve_chromium_profile("chrome", "Work")

        message = str(exc_info.value)
        assert "Profile 1" in message
        assert "Profile 2" in message

    def test_unknown_profile_lists_available_profiles(self, patched_user_data_dir):
        chrome_root = patched_user_data_dir / "chrome"
        _make_chromium_user_data(
            chrome_root,
            profiles={"Default": "populated", "Profile 1": "populated"},
            local_state_names={"Default": "Personal", "Profile 1": "Work"},
        )

        with pytest.raises(ValueError, match="was not found") as exc_info:
            resolve_chromium_profile("chrome", "Missing")

        message = str(exc_info.value)
        assert "Personal (directory: Default)" in message
        assert "Work (directory: Profile 1)" in message

    def test_empty_profile_selector_is_rejected(self, patched_user_data_dir):
        with pytest.raises(ValueError, match="Empty Chromium profile selector"):
            resolve_chromium_profile("chrome", "")


# ---------------------------------------------------------------------------
# read_chromium_profile_cookies
# ---------------------------------------------------------------------------


class TestReadChromiumProfileCookies:
    def test_dispatches_to_rookiepy_any_browser(self, tmp_path, monkeypatch):
        """Confirms we use any_browser (which decrypts non-Default profiles
        on macOS) rather than the chromium_based path that errors with
        ``missing osx_key_service`` per #511.
        """
        from notebooklm.cli._chromium_profiles import (
            ChromiumProfile,
            read_chromium_profile_cookies,
        )

        captured: dict[str, object] = {}

        class FakeRookiepy:
            @staticmethod
            def any_browser(db_path, domains=None):
                captured["db_path"] = db_path
                captured["domains"] = domains
                return [{"name": "SID", "value": "x"}]

            @staticmethod
            def chromium_based(*a, **kw):  # pragma: no cover
                raise AssertionError("must not be called — see #511")

        monkeypatch.setitem(sys.modules, "rookiepy", FakeRookiepy)

        db = tmp_path / "Profile 1" / "Cookies"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"x")
        profile = ChromiumProfile(
            browser="chrome",
            directory_name="Profile 1",
            human_name="Work",
            cookies_db=db,
        )
        result = read_chromium_profile_cookies(profile, domains=["google.com"])
        assert result == [{"name": "SID", "value": "x"}]
        assert captured["db_path"] == str(db)
        assert captured["domains"] == ["google.com"]


# ---------------------------------------------------------------------------
# Per-platform paths (sanity check the table)
# ---------------------------------------------------------------------------


class TestPlatformUserDataDirs:
    def test_chrome_path_present_on_current_platform(self):
        path = _chromium_profiles._user_data_dir("chrome")
        # Every supported platform documents a Chrome path. ``None`` would
        # indicate the platform table dropped a row.
        assert path is not None

    def test_unknown_browser_returns_none(self):
        assert _chromium_profiles._user_data_dir("not-a-browser") is None
