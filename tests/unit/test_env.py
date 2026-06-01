"""Unit tests for notebooklm._env (NOTEBOOKLM_HL / NOTEBOOKLM_BL handling)."""

from notebooklm._env import DEFAULT_BL, get_default_bl, get_default_language


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


# ---------------------------------------------------------------------------
# NOTEBOOKLM_BL — chat-endpoint build label (consumed by ChatAPI.ask)
# ---------------------------------------------------------------------------


def test_get_default_bl_defaults(monkeypatch):
    """Unset NOTEBOOKLM_BL falls back to the pinned DEFAULT_BL constant."""
    monkeypatch.delenv("NOTEBOOKLM_BL", raising=False)
    assert get_default_bl() == DEFAULT_BL


def test_get_default_bl_reads_env(monkeypatch):
    """A custom NOTEBOOKLM_BL value flows through unchanged."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "boq_labs-custom_20990101.00_p0")
    assert get_default_bl() == "boq_labs-custom_20990101.00_p0"


def test_get_default_bl_empty_env_falls_back(monkeypatch):
    """An empty NOTEBOOKLM_BL falls back to DEFAULT_BL, not ''."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "")
    assert get_default_bl() == DEFAULT_BL


def test_get_default_bl_strips_whitespace(monkeypatch):
    """Surrounding whitespace in NOTEBOOKLM_BL is stripped."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "  custom_build  ")
    assert get_default_bl() == "custom_build"


def test_get_default_bl_whitespace_only_falls_back(monkeypatch):
    """A whitespace-only NOTEBOOKLM_BL value falls back to DEFAULT_BL."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "   ")
    assert get_default_bl() == DEFAULT_BL
