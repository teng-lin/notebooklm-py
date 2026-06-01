"""Coverage gap tests for ``notebooklm.cli._chromium_profiles``.

These exercise the per-platform user-data-dir tables, the platform dispatch
helper, the ``Local State`` ``info_cache`` non-dict guard, and the
profile-choice formatting helpers — branches not covered by the existing
``tests/unit/test_chromium_profiles.py`` suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebooklm.cli import _chromium_profiles
from notebooklm.cli._chromium_profiles import (
    ChromiumProfile,
    _format_chromium_profile_choices,
    _linux_user_data_dirs,
    _load_local_state_names,
    _macos_user_data_dirs,
    _platform_user_data_dirs,
    _windows_user_data_dirs,
)


class TestMacosUserDataDirs:
    def test_returns_expected_browser_keys(self, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))
        dirs = _macos_user_data_dirs()
        assert set(dirs) == {
            "chrome",
            "chromium",
            "brave",
            "edge",
            "arc",
            "vivaldi",
            "opera",
            "opera-gx",
        }
        assert dirs["chrome"] == Path("/Users/test/Library/Application Support/Google/Chrome")
        assert dirs["brave"] == Path(
            "/Users/test/Library/Application Support/BraveSoftware/Brave-Browser"
        )


class TestWindowsUserDataDirs:
    def test_localappdata_and_appdata_present(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
        monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
        dirs = _windows_user_data_dirs()
        # LOCALAPPDATA-backed browsers
        assert "chrome" in dirs
        assert "edge" in dirs
        assert (
            dirs["chrome"]
            == Path(r"C:\Users\test\AppData\Local") / "Google" / "Chrome" / "User Data"
        )
        # APPDATA (Roaming)-backed Opera
        assert (
            dirs["opera"]
            == Path(r"C:\Users\test\AppData\Roaming") / "Opera Software" / "Opera Stable"
        )
        assert dirs["opera-gx"] == (
            Path(r"C:\Users\test\AppData\Roaming") / "Opera Software" / "Opera GX Stable"
        )

    def test_missing_env_vars_yields_empty(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        assert _windows_user_data_dirs() == {}


class TestPlatformUserDataDirs:
    def test_darwin_dispatches_to_macos(self, monkeypatch):
        monkeypatch.setattr(_chromium_profiles.sys, "platform", "darwin")
        sentinel = {"chrome": Path("/macos")}
        monkeypatch.setattr(_chromium_profiles, "_macos_user_data_dirs", lambda: sentinel)
        assert _platform_user_data_dirs() is sentinel

    @pytest.mark.parametrize("platform", ["win32", "cygwin"])
    def test_windows_dispatches_to_windows(self, monkeypatch, platform):
        monkeypatch.setattr(_chromium_profiles.sys, "platform", platform)
        sentinel = {"chrome": Path("/windows")}
        monkeypatch.setattr(_chromium_profiles, "_windows_user_data_dirs", lambda: sentinel)
        assert _platform_user_data_dirs() is sentinel

    def test_other_dispatches_to_linux(self, monkeypatch):
        monkeypatch.setattr(_chromium_profiles.sys, "platform", "linux")
        sentinel = {"chrome": Path("/linux")}
        monkeypatch.setattr(_chromium_profiles, "_linux_user_data_dirs", lambda: sentinel)
        assert _platform_user_data_dirs() is sentinel

    def test_linux_uses_xdg_config_home(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
        dirs = _linux_user_data_dirs()
        assert dirs["chrome"] == Path("/custom/config/google-chrome")


class TestLoadLocalStateInfoCacheGuard:
    def test_info_cache_not_a_dict_returns_empty(self, tmp_path):
        local_state = tmp_path / "Local State"
        local_state.write_text(
            json.dumps({"profile": {"info_cache": ["not", "a", "dict"]}}),
            encoding="utf-8",
        )
        assert _load_local_state_names(tmp_path) == {}


class TestFormatChromiumProfileChoices:
    def test_empty_returns_none_sentinel(self):
        assert _format_chromium_profile_choices([]) == "none"

    def test_non_empty_joins_choices(self):
        profiles = [
            ChromiumProfile(
                browser="chrome",
                directory_name="Default",
                human_name="Personal",
                cookies_db=Path("/x/Default/Cookies"),
            ),
            ChromiumProfile(
                browser="chrome",
                directory_name="Profile 1",
                human_name="Work",
                cookies_db=Path("/x/Profile 1/Cookies"),
            ),
        ]
        out = _format_chromium_profile_choices(profiles)
        assert "Personal (directory: Default)" in out
        assert "Work (directory: Profile 1)" in out
        assert ", " in out
