#!/usr/bin/env python3
"""
YouTube Video Analyzer - Entry Point Script
Run this script from the repository root directory.

Usage:
    python run_youtube_analyzer.py
"""

import asyncio
import io
import sys
from pathlib import Path

# Windows terminal UTF-8 encoding fix (for Japanese characters)
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Windows + Python 3.12 compatibility fix
if sys.platform == "win32" and sys.version_info >= (3, 12):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm.extensions import YouTubeAnalyzer
from notebooklm.extensions.config import OUTPUT_DIR, VIDEO_LIST_CSV
from notebooklm.extensions.messages import ENTRY_MESSAGES
from notebooklm.extensions.messages import get_entry_message as msg


def choose_language() -> str:
    """Let user choose UI language"""
    print("\n" + "=" * 60)
    print(ENTRY_MESSAGES["en"]["choose_language"])
    print(ENTRY_MESSAGES["en"]["language_options"])
    print("=" * 60)

    choice_map = {"1": "en", "2": "ja", "3": "zh"}
    try:
        choice = input(ENTRY_MESSAGES["en"]["enter_choice"]).strip()
        return choice_map.get(choice, "en")
    except (EOFError, KeyboardInterrupt):
        return "en"


def choose_output_languages(ui_lang: str) -> list:
    """
    Let user choose output languages.
    Default output language corresponds to UI language:
    - ja (Japanese UI) -> jp (Japanese output)
    - zh (Chinese UI) -> cn (Chinese output)
    - en (English UI) -> en (English output)
    Then ask about adding other languages one by one.
    """
    # UI language to output language mapping
    lang_map = {"ja": "jp", "zh": "cn", "en": "en"}

    # Output language names
    lang_names = {
        "en": msg(ui_lang, "english"),
        "jp": msg(ui_lang, "japanese"),
        "cn": msg(ui_lang, "chinese"),
    }

    # Prompt mappings
    add_prompts = {"en": "add_english", "jp": "add_japanese", "cn": "add_chinese"}

    print("\n" + "=" * 60)
    print(f"📝 {msg(ui_lang, 'output_lang_prompt')}")
    print("=" * 60)

    output_langs = []

    # Default language
    default_lang = lang_map[ui_lang]
    output_langs.append(default_lang)
    print(f"   {msg(ui_lang, 'default_output')}: {lang_names[default_lang]}")

    # Other optional languages (excluding default)
    other_langs = [lang for lang in ["en", "jp", "cn"] if lang != default_lang]

    # Ask about each other language
    for other_lang in other_langs:
        try:
            choice = input(f"   {msg(ui_lang, add_prompts[other_lang])}: ").strip().lower()
            if choice in ["y", "yes", "はい", "是", "好"]:
                output_langs.append(other_lang)
        except (EOFError, KeyboardInterrupt):
            pass

    # Show final selection
    selected_names = [lang_names[out_lang] for out_lang in output_langs]
    print(f"✅ {msg(ui_lang, 'output_langs_selected')}: {', '.join(selected_names)}")
    print("=" * 60)

    return output_langs


async def main():
    """Main function"""
    # Choose UI language
    lang = choose_language()

    print("\n" + "=" * 60)
    print(f"🚀 {msg(lang, 'title')}")
    print("=" * 60)
    print(f"📊 {msg(lang, 'progress_file')}: {VIDEO_LIST_CSV}")
    print(f"📁 {msg(lang, 'output_dir')}: {OUTPUT_DIR}")
    print("=" * 60)

    # Choose output languages
    output_langs = choose_output_languages(lang)

    # Show connection message
    print("\n" + "=" * 60)
    print(f"🔗 {msg(lang, 'connecting')}")
    print("=" * 60)

    async with YouTubeAnalyzer() as analyzer:
        # Show pending videos
        pending = analyzer.progress_manager.get_pending_videos()
        print(f"\n📋 {msg(lang, 'pending_videos')}: {len(pending)}")

        if pending:
            print(f"\n{msg(lang, 'first_n_videos', n=min(5, len(pending)))}")
            for i, v in enumerate(pending[:5], 1):
                title = v.get("youtube_title", "Unknown")[:40]
                channel = v.get("channel_name", "Unknown")
                print(f"  {i}. [{channel}] {title}...")

        print("\n" + "=" * 60)
        print(f"🚀 {msg(lang, 'starting')}")
        print("=" * 60 + "\n")

        await analyzer.run(ui_lang=lang, output_langs=output_langs)

    print("\n" + "=" * 60)
    print(f"✅ {msg(lang, 'completed')}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\n⚠️  {ENTRY_MESSAGES['en']['interrupted']}")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ {ENTRY_MESSAGES['en']['error']}: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
