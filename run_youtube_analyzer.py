#!/usr/bin/env python3
"""
YouTube 视频分析器 - 运行脚本
Run this script from the repository root directory.

Usage:
    python run_youtube_analyzer.py
"""
import asyncio
import io
import sys

# Windows 终端 UTF-8 编码修复（解决日文乱码）
if sys.platform == 'win32':
    # 设置标准输出为 UTF-8
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Windows + Python 3.12 兼容性修复
if sys.platform == 'win32' and sys.version_info >= (3, 12):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 保留日志显示，让用户知道程序在运行

from pathlib import Path

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm.extensions import YouTubeAnalyzer
from notebooklm.extensions.config import OUTPUT_DIR, VIDEO_LIST_CSV

# =============================================================================
# 多语言界面支持 / Multi-language UI / 多言語UIサポート
# =============================================================================

MESSAGES = {
    'en': {
        'choose_language': 'Choose language / 言語を選択 / 选择语言:',
        'language_options': '  1. English\n  2. 日本語\n  3. 中文',
        'enter_choice': 'Enter 1/2/3 (default: 1): ',
        'title': 'YouTube Video Analyzer',
        'progress_file': 'Progress file',
        'output_dir': 'Output directory',
        'pending_videos': 'Pending videos',
        'first_n_videos': 'First {n} pending videos:',
        'starting': 'Starting analysis...',
        'completed': 'All tasks completed!',
        'interrupted': 'User interrupted (Ctrl+C)',
        'error': 'Error',
        'channel': 'Channel',
        'processing': 'Processing channel',
        'video_count': 'Videos to process',
        'start_time': 'Start time',
        'output_lang_prompt': 'Output language settings:',
        'default_output': 'Default output',
        'add_english': 'Add English version? (y/n)',
        'add_japanese': 'Add Japanese version? (y/n)',
        'add_chinese': 'Add Chinese version? (y/n)',
        'output_langs_selected': 'Output languages',
        'english': 'English',
        'japanese': 'Japanese',
        'chinese': 'Chinese',
        'connecting': 'Connecting to NotebookLM server...',
    },
    'ja': {
        'choose_language': 'Choose language / 言語を選択 / 选择语言:',
        'language_options': '  1. English\n  2. 日本語\n  3. 中文',
        'enter_choice': '1/2/3 を入力 (デフォルト: 2): ',
        'title': 'YouTube 動画アナライザー',
        'progress_file': '進捗ファイル',
        'output_dir': '出力ディレクトリ',
        'pending_videos': '処理待ち動画',
        'first_n_videos': '最初の {n} 件の処理待ち動画:',
        'starting': '分析を開始します...',
        'completed': 'すべてのタスクが完了しました！',
        'interrupted': 'ユーザーによる中断 (Ctrl+C)',
        'error': 'エラー',
        'channel': 'チャンネル',
        'processing': 'チャンネルを処理中',
        'video_count': '処理する動画数',
        'start_time': '開始時刻',
        'output_lang_prompt': '出力言語の設定:',
        'default_output': 'デフォルト出力',
        'add_english': '英語版も出力しますか？(y/n)',
        'add_japanese': '日本語版も出力しますか？(y/n)',
        'add_chinese': '中国語版も出力しますか？(y/n)',
        'output_langs_selected': '出力言語',
        'english': 'English',
        'japanese': '日本語',
        'chinese': '中国語',
        'connecting': 'NotebookLMサーバーに接続中...',
    },
    'zh': {
        'choose_language': 'Choose language / 言語を選択 / 选择语言:',
        'language_options': '  1. English\n  2. 日本語\n  3. 中文',
        'enter_choice': '输入 1/2/3 (默认: 3): ',
        'title': 'YouTube 视频分析器',
        'progress_file': '进度文件',
        'output_dir': '输出目录',
        'pending_videos': '待处理视频',
        'first_n_videos': '前 {n} 个待处理视频:',
        'starting': '开始分析...',
        'completed': '所有任务完成！',
        'interrupted': '用户中断 (Ctrl+C)',
        'error': '错误',
        'channel': '频道',
        'processing': '正在处理频道',
        'video_count': '待处理视频数',
        'start_time': '开始时间',
        'output_lang_prompt': '输出语言设置:',
        'default_output': '默认输出',
        'add_english': '是否添加英文版？(y/n)',
        'add_japanese': '是否添加日语版？(y/n)',
        'add_chinese': '是否添加中文版？(y/n)',
        'output_langs_selected': '输出语言',
        'english': 'English',
        'japanese': '日语',
        'chinese': '中文',
        'connecting': '正在连接 NotebookLM 服务器...',
    }
}


def choose_language() -> str:
    """让用户选择界面语言"""
    print("\n" + "=" * 60)
    print(MESSAGES['en']['choose_language'])
    print(MESSAGES['en']['language_options'])
    print("=" * 60)

    choice_map = {'1': 'en', '2': 'ja', '3': 'zh'}
    try:
        choice = input(MESSAGES['en']['enter_choice']).strip()
        return choice_map.get(choice, 'en')
    except (EOFError, KeyboardInterrupt):
        return 'en'


def msg(lang: str, key: str, **kwargs) -> str:
    """获取对应语言的消息"""
    text = MESSAGES.get(lang, MESSAGES['en']).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def choose_output_languages(ui_lang: str) -> list:
    """
    让用户选择输出语言
    默认输出语言与界面语言对应：
    - ja (日语界面) -> jp (日语输出)
    - zh (中文界面) -> cn (中文输出)
    - en (英语界面) -> en (英文输出)
    然后逐个询问是否添加其他语言
    """
    # 界面语言到输出语言的映射
    lang_map = {
        'ja': 'jp',  # 日语界面 -> 日语输出
        'zh': 'cn',  # 中文界面 -> 中文输出
        'en': 'en'   # 英语界面 -> 英文输出
    }

    # 输出语言名称
    lang_names = {
        'en': msg(ui_lang, 'english'),
        'jp': msg(ui_lang, 'japanese'),
        'cn': msg(ui_lang, 'chinese'),
    }

    # 询问提示映射
    add_prompts = {
        'en': 'add_english',
        'jp': 'add_japanese',
        'cn': 'add_chinese',
    }

    print("\n" + "=" * 60)
    print(f"📝 {msg(ui_lang, 'output_lang_prompt')}")
    print("=" * 60)

    output_langs = []

    # 默认语言
    default_lang = lang_map[ui_lang]
    output_langs.append(default_lang)
    print(f"   {msg(ui_lang, 'default_output')}: {lang_names[default_lang]}")

    # 其他可选语言（排除默认语言）
    other_langs = [lang for lang in ['en', 'jp', 'cn'] if lang != default_lang]

    # 逐个询问是否添加其他语言
    for other_lang in other_langs:
        try:
            choice = input(f"   {msg(ui_lang, add_prompts[other_lang])}: ").strip().lower()
            if choice in ['y', 'yes', 'はい', '是', '好']:
                output_langs.append(other_lang)
        except (EOFError, KeyboardInterrupt):
            pass

    # 显示最终选择
    selected_names = [lang_names[lang] for lang in output_langs]
    print(f"✅ {msg(ui_lang, 'output_langs_selected')}: {', '.join(selected_names)}")
    print("=" * 60)

    return output_langs


async def main():
    """主函数"""
    # 选择界面语言
    lang = choose_language()

    print("\n" + "=" * 60)
    print(f"🚀 {msg(lang, 'title')}")
    print("=" * 60)
    print(f"📊 {msg(lang, 'progress_file')}: {VIDEO_LIST_CSV}")
    print(f"📁 {msg(lang, 'output_dir')}: {OUTPUT_DIR}")
    print("=" * 60)

    # 选择输出语言
    output_langs = choose_output_languages(lang)

    # 显示连接提示
    print("\n" + "=" * 60)
    print(f"🔗 {msg(lang, 'connecting')}")
    print("=" * 60)

    async with YouTubeAnalyzer() as analyzer:
        # 显示待处理视频
        pending = analyzer.progress_manager.get_pending_videos()
        print(f"\n📋 {msg(lang, 'pending_videos')}: {len(pending)}")

        if pending:
            print(f"\n{msg(lang, 'first_n_videos', n=min(5, len(pending)))}")
            for i, v in enumerate(pending[:5], 1):
                title = v.get('youtube_title', 'Unknown')[:40]
                channel = v.get('channel_name', 'Unknown')
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
        print(f"\n\n⚠️  {MESSAGES['en']['interrupted']}")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ {MESSAGES['en']['error']}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
