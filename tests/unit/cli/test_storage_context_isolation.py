"""Tests for ``--storage`` context isolation.

The ``--storage <path>`` flag pins both auth state AND notebook context to
sibling files (``<path>`` and ``<path>.context.json``). Without isolation,
``--storage A use nb1`` would write to the DEFAULT profile context, then
``--storage B ask`` would silently reuse A's notebook ID — see synthesis K1.

These tests pin the contract:

- ``--storage A use nb1`` writes to ``A.context.json`` (NOT the default profile).
- ``--storage B status`` shows no context (B has its own sibling file).
- ``--storage A status --paths`` reports the sibling path so users can
  discover where context lives.
- Default (no ``--storage``) behavior is unchanged.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.paths import get_context_path, get_path_info


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Pin NOTEBOOKLM_HOME to tmp so default-profile context lives in tmp.

    Also resets the module-level active profile and config cache so test order
    is irrelevant.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(home))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)

    from notebooklm.paths import _reset_config_cache, set_active_profile

    set_active_profile(None)
    _reset_config_cache()
    yield home
    set_active_profile(None)
    _reset_config_cache()


# =============================================================================
# Pure get_context_path() unit tests
# =============================================================================


class TestGetContextPathStorageOverride:
    def test_sibling_path_when_storage_set(self, tmp_path):
        """``storage_path`` returns sibling ``<path>.context.json``."""
        storage = tmp_path / "accountA.json"
        result = get_context_path(storage_path=storage)
        assert result == tmp_path / "accountA.json.context.json"

    def test_sibling_preserves_directory(self, tmp_path):
        """Sibling path lives next to the storage file, not in HOME."""
        nested = tmp_path / "deep" / "nest" / "creds.json"
        result = get_context_path(storage_path=nested)
        assert result.parent == nested.parent
        assert result.name == "creds.json.context.json"

    def test_storage_takes_precedence_over_profile(self, tmp_path, isolated_home):
        """Explicit storage beats both profile + legacy fallback."""
        storage = tmp_path / "explicit.json"
        result = get_context_path(profile="work", storage_path=storage)
        assert result == tmp_path / "explicit.json.context.json"

    def test_none_storage_falls_back_to_profile(self, isolated_home):
        """When ``storage_path`` is None, falls back to profile-based path."""
        result = get_context_path(storage_path=None)
        # Default profile, legacy fallback to home root (profile dir not created).
        assert "context.json" in str(result)
        assert str(isolated_home) in str(result.resolve())


class TestGetPathInfoStorageOverride:
    def test_storage_override_in_info(self, tmp_path, isolated_home):
        """``get_path_info(storage_path=...)`` reflects sibling layout."""
        storage = tmp_path / "explicit.json"
        info = get_path_info(storage_path=storage)
        assert info["storage_path"] == str(storage)
        assert info["context_path"] == str(tmp_path / "explicit.json.context.json")
        assert info["profile_source"] == "CLI flag (--storage)"

    def test_no_override_unchanged(self, isolated_home):
        """Default invocation (no override) keeps prior behavior."""
        info = get_path_info()
        assert "context.json" in info["context_path"]
        assert "storage_state.json" in info["storage_path"]
        # No --storage means we don't claim "CLI flag (--storage)" as source.
        assert info["profile_source"] != "CLI flag (--storage)"

    def test_storage_with_profile_marks_profile_ignored(self, tmp_path, isolated_home):
        """When --storage and --profile both set, label is explicit."""
        storage = tmp_path / "explicit.json"
        info = get_path_info(profile="work", storage_path=storage)
        # ``--profile work`` is shadowed by ``--storage`` for auth/context, so
        # the source label must communicate that to avoid confusion in
        # ``status --paths``.
        assert info["profile_source"] == "CLI flag (--storage, profile ignored)"


# =============================================================================
# CLI-level isolation tests
# =============================================================================


class TestUseWritesSiblingContext:
    """``--storage A use nb1`` must write to ``A.context.json``, not default."""

    def test_use_writes_sibling_not_default(self, runner, tmp_path, isolated_home):
        storage_a = tmp_path / "A.json"
        # Pre-populate a fake storage file so existence checks pass; the CLI's
        # `use --force` path persists the ID locally without an RPC round-trip.
        storage_a.write_text(json.dumps({"cookies": [], "origins": []}))

        # Place a default-profile context file so we can detect accidental writes.
        default_context = isolated_home / "context.json"
        default_context.write_text(json.dumps({}))

        sibling_context = tmp_path / "A.json.context.json"
        assert not sibling_context.exists()

        # ``use --force`` skips the existence-check RPC and writes context
        # immediately — that's the right primitive for an isolation test that
        # doesn't care whether the notebook exists, only WHERE the write lands.
        # After the fix, the unverified-but-saved fallback no longer exists; we
        # use ``--force`` to express the same intent explicitly.
        result = runner.invoke(
            cli, ["--storage", str(storage_a), "use", "--force", "nb_abc123def456"]
        )

        assert sibling_context.exists(), (
            f"sibling context not written: stdout={result.output} exit={result.exit_code}"
        )
        data = json.loads(sibling_context.read_text())
        assert data["notebook_id"] == "nb_abc123def456"

        # Default profile context must be untouched.
        assert json.loads(default_context.read_text()) == {}


class TestStorageIsolationBetweenFiles:
    """``--storage A`` and ``--storage B`` must not see each other's context."""

    def test_b_status_does_not_see_a_notebook(self, runner, tmp_path, isolated_home):
        storage_b = tmp_path / "B.json"
        # A's context says nb_aaa is selected; B has no context file at all.
        (tmp_path / "A.json.context.json").write_text(
            json.dumps({"notebook_id": "nb_aaa", "title": "A"})
        )
        # B's sibling context intentionally absent.

        # `status` reads context only; doesn't need auth.
        result_b = runner.invoke(cli, ["--storage", str(storage_b), "status"])
        assert result_b.exit_code == 0
        assert "nb_aaa" not in result_b.output
        # The "No notebook selected" hint confirms B saw an empty context.
        assert "No notebook selected" in result_b.output

    def test_a_status_sees_only_a_notebook(self, runner, tmp_path, isolated_home):
        storage_a = tmp_path / "A.json"
        (tmp_path / "A.json.context.json").write_text(
            json.dumps({"notebook_id": "nb_aaa", "title": "Notebook A"})
        )

        # Also put a different ID in the default profile context; A must NOT
        # see it.
        (isolated_home / "context.json").write_text(
            json.dumps({"notebook_id": "nb_default", "title": "Default"})
        )

        result_a = runner.invoke(cli, ["--storage", str(storage_a), "status"])
        assert result_a.exit_code == 0
        assert "nb_aaa" in result_a.output
        assert "nb_default" not in result_a.output


class TestStatusPathsReflectsStorageOverride:
    def test_paths_json_shows_sibling(self, runner, tmp_path, isolated_home):
        storage_a = tmp_path / "A.json"
        result = runner.invoke(cli, ["--storage", str(storage_a), "status", "--paths", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        paths = data["paths"]
        assert paths["storage_path"] == str(storage_a)
        assert paths["context_path"] == str(tmp_path / "A.json.context.json")
        assert paths["profile_source"] == "CLI flag (--storage)"


class TestDefaultBehaviorUnchanged:
    """Without ``--storage``, context resolution must match prior behavior."""

    def test_use_writes_default_profile_context(self, runner, isolated_home):
        # Pre-create the default *profile* context (migration on first CLI
        # invocation creates ``profiles/default/`` and moves legacy files
        # there; we mirror that layout up front so the helpers' exists() gate
        # opens).
        profile_context = isolated_home / "profiles" / "default" / "context.json"
        profile_context.parent.mkdir(parents=True, exist_ok=True)
        profile_context.write_text(json.dumps({}))

        # ``--force`` keeps this test focused on *where* the write lands
        # (default profile vs. sibling) rather than dragging in a full
        # auth/RPC mock. After the fix, ``use`` without ``--force`` fails closed
        # on RPC errors, so the prior "best-effort fallback" wording no
        # longer applies.
        result = runner.invoke(cli, ["use", "--force", "nb_default_path"])
        assert result.exit_code == 0, result.output

        data = json.loads(profile_context.read_text())
        assert data.get("notebook_id") == "nb_default_path"

    def test_status_paths_no_storage_flag(self, runner, isolated_home):
        result = runner.invoke(cli, ["status", "--paths", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # profile_source should NOT be the --storage label when flag absent.
        assert data["paths"]["profile_source"] != "CLI flag (--storage)"
