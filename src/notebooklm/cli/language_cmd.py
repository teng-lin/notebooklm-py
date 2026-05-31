"""Language configuration CLI commands.

Commands:
    list    List all supported language codes
    get     Get current language setting
    set     Set default language for artifact generation
"""

import json
import logging

import click
from rich.table import Table

from ..auth import AuthTokens
from ..client import NotebookLMClient
from ..io import atomic_update_json
from ..paths import get_config_path, get_home_dir
from .auth_runtime import with_auth_and_errors
from .error_handler import exit_with_code
from .options import json_option
from .rendering import console, json_error_response, json_output_response

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


def get_config() -> dict:
    """Read config from config.json."""
    config_path = get_config_path()
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.warning("Config file corrupted, using defaults: %s", e)
            return {}
        except OSError as e:
            logger.warning("Could not read config file: %s", e)
            return {}
    return {}


def _save_config(config: dict) -> None:
    """Internal: write ``config.json`` via a single non-locked overwrite.

    .. deprecated::
        Prefer :func:`set_language` (or any other lock-protected helper built
        on :func:`notebooklm.io.atomic_update_json`) for read-modify-write
        flows. This raw overwrite has no cross-process locking and is kept
        only as the low-level write primitive for callers that already hold
        no shared state to merge.
    """
    config_path = get_config_path()
    get_home_dir(create=True)  # Ensure directory exists
    # ``json.dump`` streams directly to the file handle and avoids
    # materializing the full serialized string in memory.
    with config_path.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)


def get_language() -> str | None:
    """Get the configured language, or None if not set."""
    return get_config().get("language")


def set_language(code: str) -> None:
    """Set the language in config.

    Uses ``atomic_update_json`` so concurrent CLI invocations cannot lose
    other keys (e.g., ``default_profile``) via interleaved read-modify-write.
    ``recover_from_corrupt=True`` keeps the empty-dict fallback **inside**
    the file lock so a peer's valid concurrent write is never clobbered by
    an out-of-lock unlink-and-retry.
    """
    config_path = get_config_path()
    get_home_dir(create=True)  # Ensure directory exists

    def _set_lang(current: dict) -> dict:
        current["language"] = code
        return current

    atomic_update_json(config_path, _set_lang, recover_from_corrupt=True)


@click.group()
def language():
    """Manage output language for artifact generation.

    \b
    ⚠️  Language is a GLOBAL setting that affects all notebooks in your account.

    \b
    Examples:
      notebooklm language list           # Show all supported languages
      notebooklm language get            # Show current language
      notebooklm language set zh_Hans    # Set to Simplified Chinese
    """
    pass


@language.command("list")
@json_option
def language_list(json_output):
    """List all supported language codes.

    Shows language codes with their native names for easy identification.
    """
    if json_output:
        json_output_response({"languages": SUPPORTED_LANGUAGES})
        return

    table = Table(title="Supported Languages")
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Language", style="green")

    for code, name in SUPPORTED_LANGUAGES.items():
        table.add_row(code, name)

    console.print(table)
    console.print(f"\n[dim]Total: {len(SUPPORTED_LANGUAGES)} languages[/dim]")


def _render_get(current: str | None, synced: bool, json_output: bool) -> None:
    """Render the resolved ``language get`` state in JSON or text mode."""
    if json_output:
        json_output_response(
            {
                "language": current,
                "name": SUPPORTED_LANGUAGES.get(current) if current else None,
                "is_default": current is None,
                "synced_from_server": synced,
            }
        )
        return

    if current:
        name = SUPPORTED_LANGUAGES.get(current, "Unknown")
        console.print(f"Language: [cyan]{current}[/cyan] ({name})")
        console.print("[dim]This is a global setting that applies to all notebooks.[/dim]")
        if synced:
            console.print("[dim](synced from server)[/dim]")
    else:
        console.print("Language: [dim]not set[/dim] (defaults to 'en')")
        console.print("\n[dim]Use 'notebooklm language set <code>' to set a default.[/dim]")


@language.command("get")
@click.option("--local", is_flag=True, help="Show local config only (skip server sync)")
@json_option
@click.pass_context
def language_get(ctx, local, json_output):
    """Get current language setting.

    Shows the currently configured output language for artifact generation.
    By default, fetches from server (the source of truth) and updates local
    config if different. Use --local to read the local config offline without
    contacting the server (no auth required).

    Without --local an auth/network/RPC failure surfaces as the structured
    error envelope with a non-zero exit code -- it is not silently degraded to
    the stale local value.
    """
    # --local is the offline escape hatch: short-circuit BEFORE any auth or
    # client construction so it works with no credentials available.
    if local:
        _render_get(get_language(), synced=False, json_output=json_output)
        return

    # Server path: route the RPC through the standard error envelope so auth /
    # network / RPC failures hard-fail (structured envelope + non-zero exit)
    # instead of being swallowed. The body stays pure RPC I/O -- the local
    # config write happens outside the envelope so a (rare) disk-write error
    # is never misreported as an RPC failure for an otherwise-successful fetch.
    async def body(auth: AuthTokens) -> str | None:
        async with NotebookLMClient(auth) as client:
            return await client.settings.get_output_language()

    server_lang = with_auth_and_errors(
        ctx,
        command_name="language get",
        json_output=json_output,
        body=body,
    )

    # Server is authoritative: persist its value locally on a change.
    synced = False
    if server_lang is not None and server_lang != get_language():
        set_language(server_lang)
        synced = True

    # Server may have no value set; fall back to the local config for display.
    current = server_lang if server_lang is not None else get_language()
    _render_get(current, synced=synced, json_output=json_output)


@language.command("set")
@click.argument("code")
@click.option("--local", is_flag=True, help="Set local config only (skip server sync)")
@json_option
@click.pass_context
def language_set(ctx, code, local, json_output):
    """Set default language for artifact generation.

    \b
    ⚠️  This is a GLOBAL setting that affects all notebooks in your account.

    Saves to local config and syncs to server (use --local to skip server sync).

    \b
    Example:
      notebooklm language set zh_Hans    # Simplified Chinese
      notebooklm language set ja         # Japanese
      notebooklm language set en         # English
    """
    # Validate the language code BEFORE any auth/client/local write so a bad
    # code never touches storage or the network.
    if code not in SUPPORTED_LANGUAGES:
        if json_output:
            # Match the shared JSON error schema from ``cli/rendering.py``:
            # ``{"error": True, "code": ..., "message": ..., **extra}``.
            # ``json_error_response`` is ``NoReturn``; execution never reaches
            # the text-mode ``console.print`` lines below when this branch fires.
            json_error_response(
                "INVALID_LANGUAGE",
                f"Unknown language code: {code}",
                extra={"hint": "Run 'notebooklm language list' to see supported codes"},
            )
        console.print(f"[red]Unknown language code: {code}[/red]")
        console.print("\nRun [cyan]notebooklm language list[/cyan] to see supported codes.")
        exit_with_code(1)

    name = SUPPORTED_LANGUAGES[code]

    # --local is the offline escape hatch: persist to local config only,
    # short-circuiting BEFORE any auth or client construction so it works with
    # no credentials available.
    if local:
        set_language(code)
        synced = False
    else:
        # Server-authoritative ordering: sync to the server FIRST (inside the
        # error envelope), and only persist locally once the server confirms.
        # This way a failed sync hard-fails (structured envelope + non-zero
        # exit) instead of silently leaving a misleading local value behind.
        async def body(auth: AuthTokens) -> None:
            async with NotebookLMClient(auth) as client:
                await client.settings.set_output_language(code)

        with_auth_and_errors(
            ctx,
            command_name="language set",
            json_output=json_output,
            body=body,
        )
        set_language(code)
        synced = True

    if json_output:
        json_output_response(
            {
                "language": code,
                "name": name,
                "message": "Language set successfully",
                "synced_to_server": synced,
            }
        )
        return

    console.print("\n[yellow]⚠️  This is a GLOBAL setting that affects all notebooks.[/yellow]")
    console.print(f"\nLanguage set to: [cyan]{code}[/cyan] ({name})")
    if synced:
        console.print("[dim](synced to server)[/dim]")
    elif local:
        console.print("[dim](saved locally, server sync skipped)[/dim]")
