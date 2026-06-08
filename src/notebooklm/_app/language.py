"""Transport-neutral language-configuration data + store.

This is the Click-free core of ``cli/language_cmd.py``: it owns the
:data:`SUPPORTED_LANGUAGES` catalog (code → native name), the
:func:`is_supported_language` predicate, and a :class:`LanguageConfigStore`
that reads / writes the persisted ``config.json`` ``"language"`` key. Every
transport adapter (the Click CLI today, the FastMCP server / future HTTP
surface tomorrow) shares the same catalog and the same on-disk config contract.

The store does **not** import ``notebooklm.paths`` or ``notebooklm.io``
directly: the config-path resolver, the home-directory ensurer, and the
locked ``atomic_update_json`` writer are **injected** as callables. This keeps
the neutral core decoupled from the path/profile machinery *and* preserves the
CLI's historical ``patch.object(language_cmd, "get_config_path", ...)`` /
``patch.object(language_cmd, "get_home_dir", ...)`` test seams — the CLI
wrapper reads those off its own module at call time and forwards them here.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Language codes with native names
# Based on BCP 47 / IETF language tags from WIZ_global_data
SUPPORTED_LANGUAGES: dict[str, str] = {
    # Major languages (sorted by usage)
    "en": "English",
    "zh_Hans": "中文（简体）",
    "zh_Hant": "中文（繁體）",
    "es": "Español",
    "es_419": "Español (Latinoamérica)",
    "es_MX": "Español (México)",
    "hi": "हिन्दी",
    "ar_001": "العربية",
    "ar_eg": "العربية (مصر)",
    "pt_BR": "Português (Brasil)",
    "pt_PT": "Português (Portugal)",
    "bn": "বাংলা",
    "ru": "Русский",
    "ja": "日本語",
    "pa": "ਪੰਜਾਬੀ",
    "de": "Deutsch",
    "jv": "Basa Jawa",
    "ko": "한국어",
    "fr": "Français",
    "fr_CA": "Français (Canada)",
    "te": "తెలుగు",
    "vi": "Tiếng Việt",
    "mr": "मराठी",
    "ta": "தமிழ்",
    "tr": "Türkçe",
    "ur": "اردو",
    "it": "Italiano",
    "th": "ไทย",
    "gu": "ગુજરાતી",
    "fa": "فارسی",
    "pl": "Polski",
    "uk": "Українська",
    "ml": "മലയാളം",
    "kn": "ಕನ್ನಡ",
    "or": "ଓଡ଼ିଆ",
    "my": "မြန်မာဘာသာ",
    "sw": "Kiswahili",
    "nl_NL": "Nederlands",
    "ro": "Română",
    "hu": "Magyar",
    "el": "Ελληνικά",
    "cs": "Čeština",
    "sv": "Svenska",
    "be": "Беларуская",
    "bg": "Български",
    "hr": "Hrvatski",
    "sk": "Slovenčina",
    "da": "Dansk",
    "fi": "Suomi",
    "nb_NO": "Norsk Bokmål",
    "nn_NO": "Norsk Nynorsk",
    "he": "עברית",
    "iw": "עברית",  # Legacy code
    "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu",
    "fil": "Filipino",
    "ceb": "Cebuano",
    "sr": "Српски",
    "sl": "Slovenščina",
    "sq": "Shqip",
    "mk": "Македонски",
    "lt": "Lietuvių",
    "lv": "Latviešu",
    "et": "Eesti",
    "hy": "Հայերեն",
    "ka": "ქართული",
    "az": "Azərbaycanca",
    "af": "Afrikaans",
    "am": "አማርኛ",
    "eu": "Euskara",
    "ca": "Català",
    "gl": "Galego",
    "is": "Íslenska",
    "la": "Latina",
    "ne": "नेपाली",
    "ps": "پښتو",
    "sd": "سنڌي",
    "si": "සිංහල",
    "ht": "Kreyòl Ayisyen",
    "kok": "कोंकणी",
    "mai": "मैथिली",
}

#: Resolves the path of the persisted ``config.json``.
ConfigPathResolver = Callable[[], Path]
#: Ensures the config home directory exists (``get_home_dir(create=True)``).
HomeDirEnsurer = Callable[..., Any]
#: Locked read-modify-write writer (``notebooklm.io.atomic_update_json``).
AtomicUpdate = Callable[..., Any]


def is_supported_language(code: str) -> bool:
    """Return ``True`` when ``code`` is a known output-language code."""
    return code in SUPPORTED_LANGUAGES


def language_name(code: str) -> str | None:
    """Return the native name for ``code``, or ``None`` if unknown."""
    return SUPPORTED_LANGUAGES.get(code)


class LanguageConfigStore:
    """Read / write the persisted ``config.json`` output-language setting.

    The path/home/writer collaborators are injected (never imported) so this
    neutral core stays decoupled from ``notebooklm.paths`` / ``notebooklm.io``
    and the CLI's ``patch.object`` seams keep landing. The CLI wrapper reads its
    own ``get_config_path`` / ``get_home_dir`` / ``atomic_update_json`` at call
    time and constructs the store with them.
    """

    def __init__(
        self,
        *,
        config_path: ConfigPathResolver,
        ensure_home: HomeDirEnsurer,
        atomic_update: AtomicUpdate,
    ) -> None:
        self._config_path = config_path
        self._ensure_home = ensure_home
        self._atomic_update = atomic_update

    def get_config(self) -> dict:
        """Read config from ``config.json`` (empty dict on missing/corrupt)."""
        config_path = self._config_path()
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                logger.warning("Config file corrupted, using defaults: %s", e)
                return {}
            except OSError as e:
                logger.warning("Could not read config file: %s", e)
                return {}
            # A non-dict root (valid JSON that is a list/scalar) is treated as
            # corrupt per the "empty dict on missing/corrupt" contract, so
            # downstream ``get_language()`` never does ``.get()`` on a non-dict.
            if isinstance(data, dict):
                return data
            logger.warning("Config root is %s, not an object; using defaults", type(data).__name__)
        return {}

    def save_config(self, config: dict) -> None:
        """Write ``config.json`` via a single non-locked overwrite.

        .. deprecated::
            Prefer :meth:`set_language` (lock-protected via the injected
            ``atomic_update``) for read-modify-write flows. This raw overwrite
            has no cross-process locking and is kept only as the low-level
            write primitive for callers with no shared state to merge.
        """
        config_path = self._config_path()
        self._ensure_home(create=True)  # Ensure directory exists
        # ``json.dump`` streams directly to the file handle and avoids
        # materializing the full serialized string in memory.
        with config_path.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, ensure_ascii=False)

    def get_language(self) -> str | None:
        """Get the configured language, or ``None`` if not set."""
        return self.get_config().get("language")

    def set_language(self, code: str) -> None:
        """Set the language in config via the locked read-modify-write writer.

        ``recover_from_corrupt=True`` keeps the empty-dict fallback **inside**
        the file lock so a peer's valid concurrent write is never clobbered by
        an out-of-lock unlink-and-retry.
        """
        config_path = self._config_path()
        self._ensure_home(create=True)  # Ensure directory exists

        def _set_lang(current: dict) -> dict:
            current["language"] = code
            return current

        self._atomic_update(config_path, _set_lang, recover_from_corrupt=True)


__all__ = [
    "SUPPORTED_LANGUAGES",
    "LanguageConfigStore",
    "is_supported_language",
    "language_name",
]
