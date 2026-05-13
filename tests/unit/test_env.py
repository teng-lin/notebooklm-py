"""Unit tests for notebooklm._env (NOTEBOOKLM_HL handling)."""

from notebooklm._env import get_default_language


def test_get_default_language_defaults_to_en(monkeypatch):
    """When NOTEBOOKLM_HL is unset, get_default_language() returns 'en'."""
    monkeypatch.delenv("NOTEBOOKLM_HL", raising=False)
    assert get_default_language() == "en"


def test_get_default_language_reads_env(monkeypatch):
    """When NOTEBOOKLM_HL is set, get_default_language() returns its value."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
    assert get_default_language() == "ja"


def test_get_default_language_empty_env_falls_back(monkeypatch):
    """An empty NOTEBOOKLM_HL value falls back to 'en' rather than ''."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "")
    assert get_default_language() == "en"


def test_get_default_language_strips_whitespace(monkeypatch):
    """Surrounding whitespace in NOTEBOOKLM_HL is stripped."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "  ja  ")
    assert get_default_language() == "ja"


def test_get_default_language_whitespace_only_falls_back(monkeypatch):
    """A whitespace-only NOTEBOOKLM_HL value falls back to 'en'."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "   ")
    assert get_default_language() == "en"
