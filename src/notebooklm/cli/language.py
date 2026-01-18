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

from ..client import NotebookLMClient
from ..paths import get_config_path, get_home_dir
from .helpers import console, get_auth_tokens, json_output_response, run_async
from .options import json_option

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


def save_config(config: dict) -> None:
    """Save config to config.json."""
    config_path = get_config_path()
    get_home_dir(create=True)  # Ensure directory exists
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def get_language() -> str | None:
    """Get the configured language, or None if not set."""
    return get_config().get("language")


def set_language(code: str) -> None:
    """Set the language in config."""
    config = get_config()
    config["language"] = code
    save_config(config)


def _sync_language_to_server(code: str, ctx: click.Context) -> str | None:
    """Sync language setting to server via RPC.

    Args:
        code: Language code to set.
        ctx: Click context for auth.

    Returns:
        Server's response language, or None on failure.
    """
    try:
        auth = get_auth_tokens(ctx)

        async def _set():
            async with NotebookLMClient(auth) as client:
                return await client.settings.set_output_language(code)

        return run_async(_set())
    except Exception as e:
        logger.debug("Failed to sync language to server: %s", e)
        return None


def _get_language_from_server(ctx: click.Context) -> str | None:
    """Get current language from server via RPC.

    Args:
        ctx: Click context for auth.

    Returns:
        Server's language setting, or None on failure.
    """
    try:
        auth = get_auth_tokens(ctx)

        async def _get():
            async with NotebookLMClient(auth) as client:
                return await client.settings.get_output_language()

        return run_async(_get())
    except Exception as e:
        logger.debug("Failed to get language from server: %s", e)
        return None


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


@language.command("get")
@click.option("--local", is_flag=True, help="Show local config only (skip server sync)")
@json_option
@click.pass_context
def language_get(ctx, local, json_output):
    """Get current language setting.

    Shows the currently configured output language for artifact generation.
    By default, fetches from server and updates local config if different.
    Use --local to skip server sync.
    """
    local_lang = get_language()
    server_lang = None
    synced = False

    # Try to get from server unless --local is set
    if not local:
        server_lang = _get_language_from_server(ctx)
        if server_lang is not None:
            # Update local config if server has different value
            if server_lang != local_lang:
                set_language(server_lang)
                synced = True
            local_lang = server_lang

    current = local_lang

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
    # Validate the language code
    if code not in SUPPORTED_LANGUAGES:
        if json_output:
            json_output_response(
                {
                    "error": "INVALID_LANGUAGE",
                    "message": f"Unknown language code: {code}",
                    "hint": "Run 'notebooklm language list' to see supported codes",
                }
            )
        else:
            console.print(f"[red]Unknown language code: {code}[/red]")
            console.print("\nRun [cyan]notebooklm language list[/cyan] to see supported codes.")
        raise SystemExit(1)

    # Save locally first
    set_language(code)
    name = SUPPORTED_LANGUAGES[code]

    # Sync to server unless --local is set
    synced = False
    if not local:
        server_result = _sync_language_to_server(code, ctx)
        synced = server_result is not None

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
    elif not local:
        console.print(
            "[dim](saved locally, server sync failed - server may still use previous value)[/dim]"
        )
