"""Unit tests for ``version_string()`` — the version + short-commit helper.

The two commit sources (build-embedded ``_commit.py`` vs. live ``git``) are
stubbed so the three resolution outcomes are covered deterministically,
independent of whether the test runs from a checkout or an installed wheel.
``version_string`` is ``lru_cache``d, so each test clears the cache first.
"""

from __future__ import annotations

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
