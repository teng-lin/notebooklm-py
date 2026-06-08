"""Unit tests for the transport-neutral ``notebooklm._app.language`` core.

These pin the relocated language-configuration business logic at the ``_app``
boundary (independent of the Click adapter):

* the :data:`SUPPORTED_LANGUAGES` catalog + :func:`is_supported_language` /
  :func:`language_name` predicates;
* :class:`LanguageConfigStore` get/save/get_language/set_language against an
  injected config-path resolver, home-dir ensurer, and atomic-update writer —
  including the corrupt-JSON / OSError fallbacks (moved from the former
  ``tests/unit/cli/test_language.py`` ``TestGetConfigErrorPaths``, retargeted
  off the CLI ``get_config()`` wrapper onto the store's ``get_config``).

No Click / ``CliRunner`` — every test drives the store directly. The CLI
``--json`` / exit-code / server-sync assertions stay in
``tests/unit/cli/test_language.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from notebooklm._app.language import (
    SUPPORTED_LANGUAGES,
    LanguageConfigStore,
    is_supported_language,
    language_name,
)


def _make_store(
    config_path: Path,
    *,
    atomic_update=None,
    ensure_home=None,
) -> LanguageConfigStore:
    """Build a store with simple injected collaborators for unit testing."""
    return LanguageConfigStore(
        config_path=lambda: config_path,
        ensure_home=ensure_home or MagicMock(),
        atomic_update=atomic_update or MagicMock(),
    )


# ---------------------------------------------------------------------------
# Catalog + predicates.
# ---------------------------------------------------------------------------


class TestLanguageCatalog:
    def test_known_codes_are_supported(self):
        assert is_supported_language("en")
        assert is_supported_language("zh_Hans")
        assert is_supported_language("ja")

    def test_unknown_code_is_not_supported(self):
        assert not is_supported_language("xyz")
        assert not is_supported_language("")

    def test_language_name_returns_native_name(self):
        assert language_name("en") == "English"
        assert language_name("ja") == "日本語"

    def test_language_name_unknown_returns_none(self):
        assert language_name("xyz") is None

    def test_catalog_native_names_are_present(self):
        # Anchor a couple of native names that the CLI surfaces in `language list`.
        assert SUPPORTED_LANGUAGES["en"] == "English"
        assert "中文" in SUPPORTED_LANGUAGES["zh_Hans"]


# ---------------------------------------------------------------------------
# LanguageConfigStore.get_config — read + error fallbacks.
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_reads_existing_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"language": "ja"}', encoding="utf-8")
        store = _make_store(config_file)
        assert store.get_config() == {"language": "ja"}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        store = _make_store(tmp_path / "absent.json")
        assert store.get_config() == {}

    def test_json_decode_error_returns_empty_dict(self, tmp_path):
        """Moved from CLI ``TestGetConfigErrorPaths::test_get_config_json_decode_error``."""
        config_file = tmp_path / "config.json"
        config_file.write_text("this is not valid json{{{", encoding="utf-8")
        store = _make_store(config_file)
        assert store.get_config() == {}

    def test_non_dict_root_returns_empty_dict(self, tmp_path):
        """Valid JSON whose root is a list/scalar (not an object) is treated as
        corrupt → ``{}``, so ``get_language()`` never does ``.get()`` on a
        non-dict (PR #1479 review)."""
        config_file = tmp_path / "config.json"
        config_file.write_text("[1, 2, 3]", encoding="utf-8")
        store = _make_store(config_file)
        assert store.get_config() == {}
        # the downstream accessor must not raise AttributeError on a list root
        assert store.get_language() is None

    def test_oserror_returns_empty_dict(self, tmp_path):
        """Moved from CLI ``TestGetConfigErrorPaths::test_get_config_oserror``.

        The file exists (``exists()`` is True) but reading it raises OSError;
        the store swallows it and falls back to an empty dict.
        """
        config_file = tmp_path / "config.json"
        config_file.write_text('{"language": "en"}', encoding="utf-8")

        class _Boom:
            def exists(self) -> bool:
                return True

            def read_text(self, *args, **kwargs):
                raise OSError("permission denied")

        store = _make_store(config_file)
        store._config_path = lambda: _Boom()
        assert store.get_config() == {}


# ---------------------------------------------------------------------------
# LanguageConfigStore.get_language.
# ---------------------------------------------------------------------------


class TestGetLanguage:
    def test_returns_configured_language(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"language": "de"}', encoding="utf-8")
        store = _make_store(config_file)
        assert store.get_language() == "de"

    def test_returns_none_when_unset(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("{}", encoding="utf-8")
        store = _make_store(config_file)
        assert store.get_language() is None

    def test_returns_none_when_file_missing(self, tmp_path):
        store = _make_store(tmp_path / "absent.json")
        assert store.get_language() is None


# ---------------------------------------------------------------------------
# LanguageConfigStore.save_config — raw overwrite.
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_save_config_writes_and_ensures_home(self, tmp_path):
        config_file = tmp_path / "config.json"
        ensure_home = MagicMock()
        store = _make_store(config_file, ensure_home=ensure_home)

        store.save_config({"language": "ko", "other": 1})

        ensure_home.assert_called_once_with(create=True)
        assert json.loads(config_file.read_text(encoding="utf-8")) == {
            "language": "ko",
            "other": 1,
        }

    def test_save_config_preserves_unicode(self, tmp_path):
        config_file = tmp_path / "config.json"
        store = _make_store(config_file)
        store.save_config({"note": "中文"})
        # ensure_ascii=False keeps the native characters readable on disk.
        assert "中文" in config_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LanguageConfigStore.set_language — locked read-modify-write via injected writer.
# ---------------------------------------------------------------------------


class TestSetLanguage:
    def test_set_language_uses_atomic_update(self, tmp_path):
        config_file = tmp_path / "config.json"
        ensure_home = MagicMock()
        atomic_update = MagicMock()
        store = _make_store(config_file, atomic_update=atomic_update, ensure_home=ensure_home)

        store.set_language("fr")

        ensure_home.assert_called_once_with(create=True)
        atomic_update.assert_called_once()
        args, kwargs = atomic_update.call_args
        # First arg is the config path; second is the mutator callable.
        assert args[0] == config_file
        assert kwargs.get("recover_from_corrupt") is True

    def test_set_language_mutator_sets_the_key(self, tmp_path):
        config_file = tmp_path / "config.json"
        captured = {}

        def fake_atomic_update(path, mutator, *, recover_from_corrupt):
            # Simulate the locked read-modify-write on an empty current state.
            captured["result"] = mutator({})

        store = _make_store(config_file, atomic_update=fake_atomic_update)
        store.set_language("es")
        assert captured["result"] == {"language": "es"}

    def test_set_language_mutator_preserves_other_keys(self, tmp_path):
        config_file = tmp_path / "config.json"
        captured = {}

        def fake_atomic_update(path, mutator, *, recover_from_corrupt):
            captured["result"] = mutator({"existing": "value"})

        store = _make_store(config_file, atomic_update=fake_atomic_update)
        store.set_language("it")
        assert captured["result"] == {"existing": "value", "language": "it"}


def test_round_trip_via_store(tmp_path):
    """save_config then get_language reads back the persisted value."""
    config_file = tmp_path / "config.json"
    store = _make_store(config_file)
    store.save_config({"language": "pt_BR"})
    assert store.get_language() == "pt_BR"
