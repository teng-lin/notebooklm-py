"""Tests for path resolution module."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from notebooklm.paths import (
    BROWSER_PROFILE_OWNERSHIP_MARKER,
    _read_default_profile,
    _reset_config_cache,
    browser_profile_is_owned,
    get_browser_profile_dir,
    get_context_path,
    get_home_dir,
    get_master_token_path,
    get_path_info,
    get_profile_dir,
    get_storage_path,
    list_profiles,
    master_token_path_for,
    profile_from_storage_path,
    resolve_profile,
    set_active_profile,
)


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """Reset module-level profile state between tests."""
    set_active_profile(None)
    _reset_config_cache()
    yield
    set_active_profile(None)
    _reset_config_cache()


def _clean_env():
    """Get env without any NOTEBOOKLM_* vars and NOTEBOOKLM_PROFILE."""
    env = os.environ.copy()
    env.pop("NOTEBOOKLM_HOME", None)
    env.pop("NOTEBOOKLM_PROFILE", None)
    return env


class TestGetHomeDir:
    def test_default_path(self):
        """Without NOTEBOOKLM_HOME, returns ~/.notebooklm."""
        with patch.dict(os.environ, _clean_env(), clear=True):
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

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Unix permissions not applicable on Windows"
    )
    def test_create_flag_sets_permissions(self, tmp_path):
        """create=True sets directory permissions to 0o700."""
        custom_path = tmp_path / "secure_home"

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}):
            get_home_dir(create=True)
            mode = custom_path.stat().st_mode & 0o777
            assert mode == 0o700

    def test_windows_create_skips_mode_and_chmod(self, tmp_path, monkeypatch):
        """On Windows, create=True calls mkdir without mode= and skips chmod."""
        import notebooklm.paths as paths_mod

        custom_path = tmp_path / "win_home"
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(custom_path))
        monkeypatch.setattr(paths_mod.sys, "platform", "win32")

        mkdir_calls = []
        chmod_calls = []
        _orig_mkdir = Path.mkdir

        def _track_mkdir(self, *args, **kwargs):
            mkdir_calls.append({"args": args, "kwargs": kwargs})
            return _orig_mkdir(self, *args, **kwargs)

        def _track_chmod(self, *args, **kwargs):
            chmod_calls.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(Path, "mkdir", _track_mkdir)
        monkeypatch.setattr(Path, "chmod", _track_chmod)

        get_home_dir(create=True)

        assert custom_path.exists()
        # mkdir should NOT receive mode= kwarg on Windows
        assert len(mkdir_calls) == 1
        assert "mode" not in mkdir_calls[0]["kwargs"]
        # chmod should NOT be called on Windows
        assert len(chmod_calls) == 0

    def test_unix_create_sets_mode_and_chmod(self, tmp_path, monkeypatch):
        """On Unix, create=True passes mode=0o700 to mkdir and calls chmod(0o700)."""
        import notebooklm.paths as paths_mod

        custom_path = tmp_path / "unix_home"
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(custom_path))
        monkeypatch.setattr(paths_mod.sys, "platform", "linux")

        mkdir_calls = []
        chmod_calls = []
        _orig_mkdir = Path.mkdir

        def _track_mkdir(self, *args, **kwargs):
            mkdir_calls.append({"args": args, "kwargs": kwargs})
            return _orig_mkdir(self, *args, **kwargs)

        def _track_chmod(self, *args, **kwargs):
            chmod_calls.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(Path, "mkdir", _track_mkdir)
        monkeypatch.setattr(Path, "chmod", _track_chmod)

        get_home_dir(create=True)

        assert custom_path.exists()
        # mkdir should receive mode=0o700 on Unix
        assert len(mkdir_calls) == 1
        assert mkdir_calls[0]["kwargs"].get("mode") == 0o700
        # chmod should be called with 0o700 on Unix
        assert len(chmod_calls) == 1
        assert chmod_calls[0]["args"] == (0o700,)


class TestResolveProfile:
    def test_explicit_argument(self):
        """Explicit profile argument takes precedence."""
        set_active_profile("other")
        assert resolve_profile("explicit") == "explicit"

    def test_active_profile(self):
        """Module-level _active_profile is used if no explicit arg."""
        with patch.dict(os.environ, _clean_env(), clear=True):
            set_active_profile("cli-profile")
            assert resolve_profile() == "cli-profile"

    def test_env_var(self):
        """NOTEBOOKLM_PROFILE env var is used if no active profile."""
        with patch.dict(
            os.environ, {**_clean_env(), "NOTEBOOKLM_PROFILE": "env-profile"}, clear=True
        ):
            assert resolve_profile() == "env-profile"

    def test_config_file(self, tmp_path):
        """default_profile from config.json is used as fallback."""
        home = tmp_path / "home"
        home.mkdir()
        config = home / "config.json"
        config.write_text(json.dumps({"default_profile": "config-profile"}))

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            assert resolve_profile() == "config-profile"

    def test_default_fallback(self, tmp_path):
        """Falls back to "default" if nothing configured."""
        home = tmp_path / "home"
        home.mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            assert resolve_profile() == "default"

    def test_precedence_order(self, tmp_path):
        """Explicit > active > env > config > default."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"default_profile": "from-config"}))

        with patch.dict(
            os.environ,
            {"NOTEBOOKLM_HOME": str(home), "NOTEBOOKLM_PROFILE": "from-env"},
            clear=True,
        ):
            set_active_profile("from-cli")
            # Explicit wins over everything
            assert resolve_profile("explicit") == "explicit"
            # Active wins over env and config
            assert resolve_profile() == "from-cli"

            # Remove active → env wins
            set_active_profile(None)
            assert resolve_profile() == "from-env"


class TestGetProfileDir:
    def test_returns_profile_subdir(self, tmp_path):
        """Profile dir is under home/profiles/<name>/."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            result = get_profile_dir("work")
            assert result == tmp_path / "profiles" / "work"

    def test_create_flag(self, tmp_path):
        """create=True creates the profile directory."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            result = get_profile_dir("new-profile", create=True)
            assert result.exists()
            assert result.is_dir()

    def test_rejects_dot_profile(self, tmp_path):
        """Profile name '.' resolves to profiles root — must be rejected."""
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True),
            pytest.raises(ValueError, match="Invalid profile name"),
        ):
            get_profile_dir(".")

    def test_rejects_path_traversal(self, tmp_path):
        """Profile names with path traversal are rejected."""
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True),
            pytest.raises(ValueError, match="Invalid profile name"),
        ):
            get_profile_dir("../../etc")

    def test_rejects_dotdot_in_name(self, tmp_path):
        """Even subtle traversal attempts are caught."""
        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True),
            pytest.raises(ValueError, match="Invalid profile name"),
        ):
            get_profile_dir("foo/../../bar")


class TestGetStoragePath:
    def test_profile_based_path(self, tmp_path):
        """Returns profile-based path when profile dir exists."""
        home = tmp_path / "home"
        profile_dir = home / "profiles" / "default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "storage_state.json").write_text("{}")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_storage_path()
            assert result == profile_dir / "storage_state.json"

    def test_legacy_fallback(self, tmp_path):
        """Falls back to legacy path for "default" profile when profile dir doesn't exist."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "storage_state.json").write_text("{}")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_storage_path()
            assert result == home / "storage_state.json"

    def test_legacy_fallback_warns_deprecated(self, tmp_path):
        """#2103: the home-root fallback is deprecated (scheduled removal v1.0),
        routed through ``_deprecation.warn_deprecated`` per ADR-0018 (never an
        inline ``warnings.warn``, which the guardrail
        ``test_no_inline_deprecation_warnings.py`` forbids outside that module)."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "storage_state.json").write_text("{}")

        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True),
            pytest.warns(DeprecationWarning, match="pre-profiles home-root layout"),
        ):
            get_storage_path()

    def test_legacy_fallback_quiet_env_suppresses_warning(self, tmp_path, recwarn):
        home = tmp_path / "home"
        home.mkdir()
        (home / "storage_state.json").write_text("{}")

        with patch.dict(
            os.environ,
            {"NOTEBOOKLM_HOME": str(home), "NOTEBOOKLM_QUIET_DEPRECATIONS": "1"},
            clear=True,
        ):
            get_storage_path()

        assert not any(issubclass(w.category, DeprecationWarning) for w in recwarn.list)

    def test_no_fallback_for_named_profile(self, tmp_path):
        """No legacy fallback for non-default profiles."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "storage_state.json").write_text("{}")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_storage_path(profile="work")
            # Should return profile-based path, NOT legacy
            assert result == home / "profiles" / "work" / "storage_state.json"

    def test_explicit_profile(self, tmp_path):
        """Explicit profile parameter is respected."""
        home = tmp_path / "home"
        home.mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_storage_path(profile="work")
            assert result == home / "profiles" / "work" / "storage_state.json"

    def test_respects_home_env_var(self, tmp_path):
        """Storage path follows NOTEBOOKLM_HOME."""
        custom_path = tmp_path / "custom_home"
        custom_path.mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}, clear=True):
            result = get_storage_path()
            assert "storage_state.json" in str(result)
            assert str(custom_path.resolve()) in str(result)


class TestMasterTokenPathFor:
    """Pinned BEFORE the #2103-structural-follow-up PR-1 convergence: the
    SOLE derivation site four call sites previously duplicated (and
    disagreed on canonicalization for). Every scenario below must resolve
    identically regardless of which of the four callers asks."""

    def test_ordinary_profile_path(self, tmp_path):
        storage = tmp_path / "profiles" / "default" / "storage_state.json"
        assert master_token_path_for(storage) == storage.with_name("master_token.json")

    def test_relative_path_resolves_to_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = master_token_path_for(Path("storage_state.json"))
        assert result == (tmp_path / "master_token.json").resolve()
        assert result.is_absolute()

    def test_tilde_path_expands(self, tmp_path, monkeypatch):
        """Hardcoding a literal ``/tmp/...`` prefix broke on macOS CI (#2108
        review round 1): ``/tmp`` is itself a symlink to ``/private/tmp``
        there, so the resolved result legitimately starts with a different
        prefix than the unresolved ``HOME`` value. Use ``tmp_path`` (already
        the platform's real temp root) and compare against ITS resolved
        form, matching exactly what ``master_token_path_for`` itself does.

        Round 2: setting ``HOME`` alone still failed on Windows CI —
        ``Path.expanduser()`` there reads ``USERPROFILE``, not ``HOME``
        (the established cross-platform pattern already used by
        ``test_mcp_desktop_extension.py``: set both so ``~`` resolves to the
        temp home on every OS)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = master_token_path_for(Path("~/profiles/default/storage_state.json"))
        assert "~" not in str(result)
        assert result == tmp_path.resolve() / "profiles" / "default" / "master_token.json"

    def test_symlinked_directory_resolves_to_target(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "storage_state.json").write_text("{}")
        alias_dir = tmp_path / "alias"
        try:
            alias_dir.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
            pytest.skip("platform cannot create directory symlinks")

        via_alias = master_token_path_for(alias_dir / "storage_state.json")
        via_real = master_token_path_for(real_dir / "storage_state.json")
        assert via_alias == via_real  # the whole point: alias and target agree

    def test_legacy_home_root_path(self, tmp_path):
        """The home-root legacy layout (pre-profiles) is just another concrete
        path to this function — no special-casing needed, since it derives
        from whatever ``storage_path`` the caller already resolved."""
        home = tmp_path / "home"
        storage = home / "storage_state.json"
        assert master_token_path_for(storage) == home / "master_token.json"

    def test_all_four_call_site_shapes_agree(self, tmp_path):
        """The four pre-PR-1 call sites used three different policies
        (resolved parent, raw parent, unresolved with_name). Prove they now
        all produce the SAME sibling for a relative/symlinked storage path —
        the actual invariant this PR restores."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        alias_dir = tmp_path / "alias"
        try:
            alias_dir.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
            pytest.skip("platform cannot create directory symlinks")
        storage = alias_dir / "storage_state.json"

        resolved_parent_style = storage.expanduser().resolve().parent / "master_token.json"
        raw_parent_style = storage.parent / "master_token.json"  # pre-fix auth_refresh.py
        with_name_style = storage.with_name("master_token.json")  # pre-fix auth_check.py

        result = master_token_path_for(storage)
        assert result == resolved_parent_style
        # The other two pre-existing policies did NOT agree with the resolved
        # one for a symlinked path — this is the divergence PR-1 closes.
        assert result != raw_parent_style
        assert result != with_name_style

    def test_resolve_runtime_error_falls_back_to_expanded_path(self, tmp_path, monkeypatch):
        """#2103 PR-1 review: verified empirically
        against real 3.10.20/3.11.15/3.12.13 interpreters (not just this
        suite's own, since CPython's pathlib changed ELOOP handling in 3.13)
        that a circular symlink makes ``Path.resolve()`` raise ``RuntimeError``
        on 3.10-3.12 -- and ``RuntimeError`` is NOT an ``OSError`` subclass, so
        the ``except OSError`` pattern used elsewhere in this module
        (``profile_from_storage_path``) would not have caught it. Pinned
        directly via monkeypatch so this holds on every interpreter this
        suite happens to run under, not only the ones where a real symlink
        loop reproduces it."""
        storage = tmp_path / "storage_state.json"

        def raise_runtime_error(self: Path) -> Path:
            raise RuntimeError("Symlink loop from '...'")

        monkeypatch.setattr(Path, "resolve", raise_runtime_error)

        result = master_token_path_for(storage)

        assert result == storage.with_name("master_token.json")

    def test_resolve_os_error_falls_back_to_expanded_path(self, tmp_path, monkeypatch):
        """Symmetric coverage for the ``OSError`` half of the except clause --
        matching the exception type this module already treats as
        resolve-can-fail elsewhere, even though no known real-world trigger
        raises plain ``OSError`` from this call site today."""
        storage = tmp_path / "storage_state.json"

        def raise_os_error(self: Path) -> Path:
            raise OSError("simulated resolve failure")

        monkeypatch.setattr(Path, "resolve", raise_os_error)

        result = master_token_path_for(storage)

        assert result == storage.with_name("master_token.json")

    def test_real_symlink_loop_does_not_crash(self, tmp_path):
        """End-to-end proof alongside the monkeypatched tests above: a
        genuinely circular symlink must never propagate out of this
        function, regardless of which of the two behaviors (3.10-3.12 raise,
        3.13+ silent degrade) the running interpreter has."""
        loop_dir = tmp_path / "loop"
        try:
            loop_dir.symlink_to(loop_dir)
        except (OSError, NotImplementedError):  # pragma: no cover - platform can't self-link
            pytest.skip("platform cannot create a self-referential symlink")
        storage = loop_dir / "sub" / "storage_state.json"

        result = master_token_path_for(storage)  # must not raise

        assert result.name == "master_token.json"


class TestGetMasterTokenPath:
    def test_default_profile(self, tmp_path):
        home = tmp_path / "home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_master_token_path()
            assert result == (home / "profiles" / "default" / "master_token.json").resolve()

    def test_named_profile(self, tmp_path):
        home = tmp_path / "home"
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_master_token_path(profile="work")
            assert result == (home / "profiles" / "work" / "master_token.json").resolve()

    def test_agrees_with_profile_dir_derivation_for_ordinary_profile(self, tmp_path):
        """The behavior change (deriving from ``get_storage_path`` instead of
        ``get_profile_dir``) is purely additive for the common, already-migrated
        case — explicit, not just incidentally implied by the two tests above
        happening to compute the same literal path."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert get_master_token_path("work") == get_profile_dir("work") / "master_token.json"

    def test_agrees_with_legacy_home_root_storage_fallback(self, tmp_path):
        """Behavior change (#2103 PR-1): previously derived from
        ``get_profile_dir`` directly, which has NO legacy home-root fallback —
        for a legacy "default" profile it would have returned
        ``profiles/default/master_token.json`` even when the real sibling
        (had one existed pre-profiles) sat at the home root beside the legacy
        ``storage_state.json``. Now derives from ``get_storage_path``, so it
        agrees with wherever THAT function says storage actually lives."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "storage_state.json").write_text("{}")  # triggers the legacy fallback

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            storage = get_storage_path()  # resolves to the legacy home-root file
            assert storage == home / "storage_state.json"
            result = get_master_token_path()

        assert result == home / "master_token.json"
        # The old (pre-PR-1) behavior would have said this instead:
        assert result != (home / "profiles" / "default" / "master_token.json").resolve()


class TestGetContextPath:
    def test_profile_based_path(self, tmp_path):
        """Returns profile-based context path."""
        home = tmp_path / "home"
        profile_dir = home / "profiles" / "default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "context.json").write_text("{}")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_context_path()
            assert result == profile_dir / "context.json"

    def test_legacy_fallback(self, tmp_path):
        """Falls back to legacy path for "default" profile."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "context.json").write_text("{}")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_context_path()
            assert result == home / "context.json"

    def test_legacy_fallback_warns_deprecated(self, tmp_path):
        """#2103: same deprecation contract as ``TestGetStoragePath`` — pinned
        per call site, since each ``get_*_path`` function reaches the shared
        ``_legacy_fallback`` helper independently."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "context.json").write_text("{}")

        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True),
            pytest.warns(DeprecationWarning, match="pre-profiles home-root layout"),
        ):
            get_context_path()

    def test_legacy_fallback_quiet_env_suppresses_warning(self, tmp_path, recwarn):
        home = tmp_path / "home"
        home.mkdir()
        (home / "context.json").write_text("{}")

        with patch.dict(
            os.environ,
            {"NOTEBOOKLM_HOME": str(home), "NOTEBOOKLM_QUIET_DEPRECATIONS": "1"},
            clear=True,
        ):
            get_context_path()

        assert not any(issubclass(w.category, DeprecationWarning) for w in recwarn.list)

    def test_respects_home_env_var(self, tmp_path):
        """Context path follows NOTEBOOKLM_HOME."""
        custom_path = tmp_path / "custom_home"
        custom_path.mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}, clear=True):
            result = get_context_path()
            assert "context.json" in str(result)
            assert str(custom_path.resolve()) in str(result)


class TestGetBrowserProfileDir:
    def test_custom_storage_files_get_distinct_sibling_profiles(self, tmp_path):
        """Custom storage filenames retain their full name for isolation."""
        storage_a = tmp_path / "A.json"
        storage_b = tmp_path / "B.json"

        assert get_browser_profile_dir(storage_path=storage_a) == (
            tmp_path / "A.json.browser_profile"
        )
        assert get_browser_profile_dir(storage_path=storage_b) == (
            tmp_path / "B.json.browser_profile"
        )

    def test_conventional_storage_filename_uses_browser_profile_sibling(self, tmp_path):
        """The standard storage filename preserves the established directory name."""
        storage = tmp_path / "storage_state.json"

        assert get_browser_profile_dir(storage_path=storage) == tmp_path / "browser_profile"

    def test_storage_path_takes_precedence_over_profile(self, tmp_path):
        """An explicit storage path wins over profile resolution."""
        storage = tmp_path / "account.json"

        assert get_browser_profile_dir(profile="work", storage_path=storage) == (
            tmp_path / "account.json.browser_profile"
        )

    def test_long_custom_storage_name_uses_stable_canonical_hash(self, tmp_path, monkeypatch):
        """Long relative and absolute aliases resolve to one safe short component."""
        accounts = tmp_path / "accounts"
        accounts.mkdir()
        monkeypatch.chdir(tmp_path)
        relative_storage = Path("accounts") / ("x" * 240 + ".json")
        canonical_storage = relative_storage.resolve()

        relative_result = get_browser_profile_dir(storage_path=relative_storage)
        canonical_result = get_browser_profile_dir(storage_path=canonical_storage)

        assert relative_result == canonical_result
        assert relative_result.parent == accounts.resolve()
        assert relative_result.name.endswith(".browser_profile")
        assert len(os.fsencode(relative_result.name)) <= 255
        assert relative_result.name != relative_storage.name + ".browser_profile"

    def test_profile_based_path(self, tmp_path):
        """Returns profile-based browser_profile path."""
        home = tmp_path / "home"
        profile_dir = home / "profiles" / "default"
        browser_dir = profile_dir / "browser_profile"
        browser_dir.mkdir(parents=True)

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_browser_profile_dir()
            assert result == browser_dir

    def test_legacy_fallback(self, tmp_path):
        """Falls back to legacy path for "default" profile."""
        home = tmp_path / "home"
        (home / "browser_profile").mkdir(parents=True)

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            result = get_browser_profile_dir()
            assert result == home / "browser_profile"

    def test_legacy_fallback_warns_deprecated(self, tmp_path):
        """#2103: same deprecation contract as ``TestGetStoragePath`` — pinned
        per call site, since each ``get_*_path`` function reaches the shared
        ``_legacy_fallback`` helper independently."""
        home = tmp_path / "home"
        (home / "browser_profile").mkdir(parents=True)

        with (
            patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True),
            pytest.warns(DeprecationWarning, match="pre-profiles home-root layout"),
        ):
            get_browser_profile_dir()

    def test_legacy_fallback_quiet_env_suppresses_warning(self, tmp_path, recwarn):
        home = tmp_path / "home"
        (home / "browser_profile").mkdir(parents=True)

        with patch.dict(
            os.environ,
            {"NOTEBOOKLM_HOME": str(home), "NOTEBOOKLM_QUIET_DEPRECATIONS": "1"},
            clear=True,
        ):
            get_browser_profile_dir()

        assert not any(issubclass(w.category, DeprecationWarning) for w in recwarn.list)

    def test_respects_home_env_var(self, tmp_path):
        """Browser profile follows NOTEBOOKLM_HOME."""
        custom_path = tmp_path / "custom_home"
        custom_path.mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}, clear=True):
            result = get_browser_profile_dir()
            assert "browser_profile" in str(result)
            assert str(custom_path.resolve()) in str(result)


class TestBrowserProfileIsOwned:
    def test_marker_owns_arbitrary_browser_profile(self, tmp_path):
        browser_profile = tmp_path / "external" / "browser_profile"
        browser_profile.mkdir(parents=True)
        (browser_profile / BROWSER_PROFILE_OWNERSHIP_MARKER).touch()

        assert browser_profile_is_owned(tmp_path / "external" / "storage.json", browser_profile)

    def test_recognizes_legacy_and_named_profile_layouts(self, tmp_path):
        home = tmp_path / "home"

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            assert browser_profile_is_owned(
                home / "storage_state.json",
                home / "browser_profile",
            )
            assert browser_profile_is_owned(
                home / "profiles" / "work" / "storage_state.json",
                home / "profiles" / "work" / "browser_profile",
            )

    def test_rejects_arbitrary_conventional_layout(self, tmp_path):
        home = tmp_path / "home"
        external = tmp_path / "external"

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            assert not browser_profile_is_owned(
                external / "storage_state.json",
                external / "browser_profile",
            )

    def test_rejects_named_profile_symlink_that_escapes_home(self, tmp_path):
        home = tmp_path / "home"
        profiles = home / "profiles"
        profiles.mkdir(parents=True)
        external = tmp_path / "external"
        external.mkdir()
        try:
            (profiles / "work").symlink_to(external, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(home)}, clear=True):
            assert not browser_profile_is_owned(
                profiles / "work" / "storage_state.json",
                profiles / "work" / "browser_profile",
            )


class TestListProfiles:
    def test_no_profiles_dir(self, tmp_path):
        """Returns empty list when profiles/ doesn't exist."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert list_profiles() == []

    def test_lists_profiles(self, tmp_path):
        """Returns sorted list of profile directories."""
        profiles = tmp_path / "profiles"
        (profiles / "work").mkdir(parents=True)
        (profiles / "default").mkdir()
        (profiles / "personal").mkdir()

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert list_profiles() == ["default", "personal", "work"]

    def test_ignores_files(self, tmp_path):
        """Only lists directories, not files."""
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "default").mkdir()
        (profiles / ".migration_complete").write_text("done")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert list_profiles() == ["default"]


class TestReadDefaultProfile:
    def test_no_config(self, tmp_path):
        """Returns None when config.json doesn't exist."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert _read_default_profile() is None

    def test_reads_default_profile(self, tmp_path):
        """Reads default_profile from config.json."""
        (tmp_path / "config.json").write_text(json.dumps({"default_profile": "work"}))
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert _read_default_profile() == "work"

    def test_handles_corrupt_json(self, tmp_path):
        """Returns None on corrupt config.json."""
        (tmp_path / "config.json").write_text("not json")
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert _read_default_profile() is None


class TestGetPathInfo:
    def test_includes_profile_info(self, tmp_path):
        """Path info includes profile fields."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            info = get_path_info()

            assert info["profile"] == "default"
            assert info["profile_source"] == "default"
            assert "profiles" in info["profile_dir"]
            assert "storage_state.json" in info["storage_path"]
            assert "context.json" in info["context_path"]
            assert "browser_profile" in info["browser_profile_dir"]

    def test_custom_home(self, tmp_path):
        """Returns correct info with NOTEBOOKLM_HOME set."""
        custom_path = tmp_path / "custom_home"
        custom_path.mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(custom_path)}, clear=True):
            info = get_path_info()

            assert info["home_source"] == "NOTEBOOKLM_HOME"
            assert str(custom_path.resolve()) in info["home_dir"]


class TestProfileFromStoragePath:
    """Reverse resolver: a storage path under profiles/<name>/ yields <name>."""

    def test_none_returns_none(self):
        assert profile_from_storage_path(None) is None

    def test_profile_path_yields_profile_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        storage = get_storage_path(profile="work")
        assert storage.parent.name == "work"  # profiles/work/storage_state.json
        assert profile_from_storage_path(storage) == "work"

    def test_default_profile_path_yields_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        storage = tmp_path / "profiles" / "default" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text("{}", encoding="utf-8")
        assert profile_from_storage_path(storage) == "default"

    def test_arbitrary_path_yields_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        # An explicit --storage path OUTSIDE the profiles root carries no profile.
        arbitrary = tmp_path / "elsewhere" / "storage_state.json"
        assert profile_from_storage_path(arbitrary) is None
