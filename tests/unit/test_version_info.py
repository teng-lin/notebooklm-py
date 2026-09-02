"""Unit tests for ``version_string()`` — the version + short-commit helper.

The two commit sources (build-embedded ``_commit.py`` vs. live ``git``) are
stubbed so the three resolution outcomes are covered deterministically,
independent of whether the test runs from a checkout or an installed wheel.
``version_string`` is ``lru_cache``d, so each test clears the cache first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from notebooklm import _version_info as vi


def _reset() -> None:
    vi.version_string.cache_clear()


def test_embedded_commit_wins_and_skips_git(monkeypatch) -> None:
    """The baked-in commit is used; live git is never consulted."""
    _reset()
    monkeypatch.setattr(vi, "_embedded_commit", lambda: "abc12345")
    monkeypatch.setattr(vi, "_live_commit", lambda: _must_not_be_called())
    assert vi.version_string() == f"{vi.__version__} (abc12345)"
    _reset()


def test_falls_back_to_live_git(monkeypatch) -> None:
    """No embedded commit → the live-git commit is used."""
    _reset()
    monkeypatch.setattr(vi, "_embedded_commit", lambda: None)
    monkeypatch.setattr(vi, "_live_commit", lambda: "def67890")
    assert vi.version_string() == f"{vi.__version__} (def67890)"
    _reset()


def test_bare_version_when_no_commit(monkeypatch) -> None:
    """Neither source knows the commit → bare version, no parens."""
    _reset()
    monkeypatch.setattr(vi, "_embedded_commit", lambda: None)
    monkeypatch.setattr(vi, "_live_commit", lambda: None)
    assert vi.version_string() == vi.__version__
    _reset()


def _must_not_be_called():  # pragma: no cover - only runs if the guard fails
    raise AssertionError("_live_commit must not be called when a commit is embedded")


# ---------------------------------------------------------------------------
# The two commit sources themselves
# ---------------------------------------------------------------------------


def test_embedded_commit_is_none_without_a_build_stamped_module(monkeypatch) -> None:
    """A source checkout has no ``_commit.py``; the import failure is expected."""
    monkeypatch.setitem(sys.modules, "notebooklm._commit", None)

    assert vi._embedded_commit() is None


def test_embedded_commit_treats_an_empty_stamp_as_absent(monkeypatch) -> None:
    """A build that saw no git writes an empty ``COMMIT`` rather than omitting it."""
    module = type(sys)("notebooklm._commit")
    module.COMMIT = ""
    monkeypatch.setitem(sys.modules, "notebooklm._commit", module)

    assert vi._embedded_commit() is None


def test_embedded_commit_returns_the_stamped_value(monkeypatch) -> None:
    module = type(sys)("notebooklm._commit")
    module.COMMIT = "abc12345"
    monkeypatch.setitem(sys.modules, "notebooklm._commit", module)

    assert vi._embedded_commit() == "abc12345"


@pytest.mark.parametrize(
    "repo_root",
    [pytest.param(None, id="install-too-shallow"), pytest.param("missing", id="no-dot-git")],
)
def test_live_commit_is_none_without_a_checkout(monkeypatch, tmp_path, repo_root) -> None:
    root = None if repo_root is None else tmp_path / "no-git"
    if root is not None:
        root.mkdir()
    monkeypatch.setattr(vi, "_REPO_ROOT", root)

    assert vi._live_commit() is None


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    return root


def test_live_commit_returns_the_short_hash(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vi, "_REPO_ROOT", _checkout(tmp_path))
    monkeypatch.setattr(
        vi.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="5d748a26\n", stderr=""
        ),
    )

    assert vi._live_commit() == "5d748a26"


def test_live_commit_treats_empty_git_output_as_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vi, "_REPO_ROOT", _checkout(tmp_path))
    monkeypatch.setattr(
        vi.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="  \n", stderr=""),
    )

    assert vi._live_commit() is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(FileNotFoundError("git"), id="git-not-installed"),
        pytest.param(subprocess.TimeoutExpired("git", 2), id="timed-out"),
        pytest.param(subprocess.CalledProcessError(128, "git"), id="not-a-repository"),
    ],
)
def test_live_commit_swallows_every_git_failure(monkeypatch, tmp_path, error) -> None:
    """Version reporting must never fail because git did."""
    monkeypatch.setattr(vi, "_REPO_ROOT", _checkout(tmp_path))

    def _raise(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(vi.subprocess, "run", _raise)

    assert vi._live_commit() is None
