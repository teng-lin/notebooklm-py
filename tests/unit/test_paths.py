"""Tests for path resolution module."""

import os
from pathlib import Path
from unittest.mock import patch

from notebooklm.paths import (
    get_browser_profile_dir,
    get_context_path,
    get_home_dir,
    get_path_info,
    get_storage_path,
)


class TestGetHomeDir:
    def test_default_path(self):
        """Without NOTEBOOKLM_HOME, returns ~/.notebooklm."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove NOTEBOOKLM_HOME if it exists
            os.environ.pop("NOTEBOOKLM_HOME", None)
            result = get_home_dir()
            assert result == Path.home() / ".notebooklm"

    def test_respects_env_var(self, tmp_path):
        """NOTEBOOKLM_HOME env var overrides default."""
        custom_path = tmp_path / "custom_home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            result = get_home_dir()
            assert result == custom_path.resolve()

    def test_expands_tilde(self):
        """Tilde in NOTEBOOKLM_HOME is expanded."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": "~/custom_notebooklm"}):
            result = get_home_dir()
            assert result == (Path.home() / "custom_notebooklm").resolve()

    def test_create_flag_creates_directory(self, tmp_path):
        """create=True creates the directory if it doesn't exist."""
        custom_path = tmp_path / "new_home"
        assert not custom_path.exists()

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            result = get_home_dir(create=True)
            assert result.exists()
            assert result.is_dir()

    def test_create_flag_sets_permissions(self, tmp_path):
        """create=True sets directory permissions to 0o700."""
        custom_path = tmp_path / "secure_home"

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            get_home_dir(create=True)
            # Check permissions (on Unix systems)
            mode = custom_path.stat().st_mode & 0o777
            assert mode == 0o700


class TestGetStoragePath:
    def test_default_path(self):
        """Returns storage_state.json in home dir."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("NOTEBOOKLM_HOME", None)
            result = get_storage_path()
            assert result == Path.home() / ".notebooklm" / "storage_state.json"

    def test_respects_home_env_var(self, tmp_path):
        """Storage path follows NOTEBOOKLM_HOME."""
        custom_path = tmp_path / "custom_home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            result = get_storage_path()
            assert result == custom_path.resolve() / "storage_state.json"


class TestGetContextPath:
    def test_default_path(self):
        """Returns context.json in home dir."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("NOTEBOOKLM_HOME", None)
            result = get_context_path()
            assert result == Path.home() / ".notebooklm" / "context.json"

    def test_respects_home_env_var(self, tmp_path):
        """Context path follows NOTEBOOKLM_HOME."""
        custom_path = tmp_path / "custom_home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            result = get_context_path()
            assert result == custom_path.resolve() / "context.json"


class TestGetBrowserProfileDir:
    def test_default_path(self):
        """Returns browser_profile in home dir."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("NOTEBOOKLM_HOME", None)
            result = get_browser_profile_dir()
            assert result == Path.home() / ".notebooklm" / "browser_profile"

    def test_respects_home_env_var(self, tmp_path):
        """Browser profile follows NOTEBOOKLM_HOME."""
        custom_path = tmp_path / "custom_home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            result = get_browser_profile_dir()
            assert result == custom_path.resolve() / "browser_profile"


class TestGetPathInfo:
    def test_default_paths(self):
        """Returns correct info with default paths."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("NOTEBOOKLM_HOME", None)
            info = get_path_info()

            assert info["home_source"] == "default (~/.notebooklm)"
            assert ".notebooklm" in info["home_dir"]
            assert "storage_state.json" in info["storage_path"]
            assert "context.json" in info["context_path"]
            assert "browser_profile" in info["browser_profile_dir"]

    def test_custom_home(self, tmp_path):
        """Returns correct info with NOTEBOOKLM_HOME set."""
        custom_path = tmp_path / "custom_home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            info = get_path_info()

            assert info["home_source"] == "NOTEBOOKLM_HOME"
            assert str(custom_path.resolve()) in info["home_dir"]
