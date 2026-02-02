#!/usr/bin/env python3
"""
YouTube Video Content Analyzer
Uses NotebookLM API to analyze YouTube videos and generate structured documents
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Windows + Python 3.12 compatibility fix
if sys.platform == 'win32' and sys.version_info >= (3, 12):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from notebooklm import NotebookLMClient

from .config import (
    ANALYSIS_PROMPT_CN,
    ANALYSIS_PROMPT_EN,
    ANALYSIS_PROMPT_JP,
    LOG_FORMAT,
    LOG_LEVEL,
    OUTPUT_DIR,
    PROGRESS_CSV,
    VIDEO_PROCESSING_DELAY,
    WAIT_FOR_SOURCE_PROCESSING,
)
from .csv_utils import ProgressManager
from .file_utils import generate_output_filename, save_markdown

# Configure logging (keep logs in English for technical purposes)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT
)
logger = logging.getLogger(__name__)

# =============================================================================
# Multi-language UI Messages
# =============================================================================
UI_MESSAGES = {
    'en': {
        'system_start': 'YouTube Video Analysis System Started',
        'start_time': 'Start time',
        'task_overview': 'Task Overview',
        'total_videos': 'Total videos',
        'channels': 'Channels',
        'channel_details': 'Channel Details',
        'videos': 'videos',
        'estimated_time': 'Estimated time',
        'minutes': 'minutes',
        'estimated_end': 'Estimated end time',
        'tips': 'Tips: This is a long-running task. You can:',
        'tip1': 'Grab a coffee',
        'tip2': 'Check the progress log for real-time status',
        'tip3': 'Check output/video_list.csv for progress',
        'processing_channel': 'Processing Channel',
        'videos_to_process': 'Videos to process',
        'video': 'Video',
        'title': 'Title',
        'id': 'ID',
        'upload_date': 'Upload date',
        'skip_completed': 'Skipping completed video',
        'step_add': 'Step 1/3: Adding video to Notebook',
        'video_added': 'Video added (NotebookLM is transcribing...)',
        'add_failed': 'Failed to add video, skipping',
        'step_analyze': 'Step 2/3: Analyzing video',
        'analyzing_wait': 'This may take 1-6 minutes per version...',
        'generating_en': 'Generating English version...',
        'generating_cn': 'Generating Chinese version...',
        'generating_jp': 'Generating Japanese version...',
        'en_saved': 'English version saved',
        'cn_saved': 'Chinese version saved',
        'jp_saved': 'Japanese version saved',
        'en_failed': 'English version failed',
        'cn_failed': 'Chinese version failed',
        'jp_failed': 'Japanese version failed',
        'video_complete': 'Video processing complete!',
        'files': 'Files',
        'progress': 'Progress',
        'completed': 'completed',
        'step_wait': 'Step 3/3: Waiting interval',
        'wait_next': 'Waiting before processing next video',
        'all_done': 'All videos completed!',
        'partial_complete': 'Video partially complete',
        'video_failed': 'Video analysis failed',
        'channel_complete': 'Channel processing complete!',
        'processing_complete': 'Processing Complete!',
        'end_time': 'End time',
        'total_duration': 'Total duration',
        'min': 'min',
        'sec': 'sec',
        'processing_stats': 'Processing Statistics',
        'output_location': 'Output file location',
        'next_steps': 'Next steps',
        'next1': 'Review generated MD documents',
        'next2': 'Visit NotebookLM: https://notebooklm.google.com',
        'next3': 'Check video content in Notebooks',
        'no_pending': 'No pending videos',
        'wait_time': 'Wait time',
        'seconds': 'seconds',
        'finish_time': 'Finish time',
        'remaining': 'Remaining',
        'wait_complete': 'Wait complete!',
    },
    'ja': {
        'system_start': 'YouTube動画分析システム起動',
        'start_time': '開始時刻',
        'task_overview': 'タスク概要',
        'total_videos': '総動画数',
        'channels': 'チャンネル数',
        'channel_details': 'チャンネル詳細',
        'videos': '本',
        'estimated_time': '予想所要時間',
        'minutes': '分',
        'estimated_end': '予想終了時刻',
        'tips': 'ヒント: これは長時間実行されるタスクです:',
        'tip1': 'コーヒーでも飲みながら',
        'tip2': '進捗ログでリアルタイム状況を確認',
        'tip3': 'output/video_list.csv で進捗を確認',
        'processing_channel': 'チャンネル処理中',
        'videos_to_process': '処理する動画数',
        'video': '動画',
        'title': 'タイトル',
        'id': 'ID',
        'upload_date': 'アップロード日',
        'skip_completed': '完了済み動画をスキップ',
        'step_add': 'ステップ 1/3: 動画をNotebookに追加',
        'video_added': '動画を追加しました（NotebookLMが文字起こし中...）',
        'add_failed': '動画の追加に失敗、スキップします',
        'step_analyze': 'ステップ 2/3: 動画を分析中',
        'analyzing_wait': '各バージョンに1〜6分かかる場合があります...',
        'generating_en': 'English版を生成中...',
        'generating_cn': '中国語版を生成中...',
        'generating_jp': '日本語版を生成中...',
        'en_saved': 'English版を保存しました',
        'cn_saved': '中国語版を保存しました',
        'jp_saved': '日本語版を保存しました',
        'en_failed': 'English版の生成に失敗',
        'cn_failed': '中国語版の生成に失敗',
        'jp_failed': '日本語版の生成に失敗',
        'video_complete': '動画処理完了！',
        'files': 'ファイル',
        'progress': '進捗',
        'completed': '完了',
        'step_wait': 'ステップ 3/3: 待機中',
        'wait_next': '次の動画を処理する前に待機中',
        'all_done': 'すべての動画が完了しました！',
        'partial_complete': '動画が部分的に完了',
        'video_failed': '動画分析に失敗',
        'channel_complete': 'チャンネル処理完了！',
        'processing_complete': '処理完了！',
        'end_time': '終了時刻',
        'total_duration': '合計所要時間',
        'min': '分',
        'sec': '秒',
        'processing_stats': '処理統計',
        'output_location': '出力ファイルの場所',
        'next_steps': '次のステップ',
        'next1': '生成されたMDドキュメントを確認',
        'next2': 'NotebookLMにアクセス: https://notebooklm.google.com',
        'next3': 'Notebookの動画コンテンツを確認',
        'no_pending': '処理待ちの動画がありません',
        'wait_time': '待機時間',
        'seconds': '秒',
        'finish_time': '完了時刻',
        'remaining': '残り',
        'wait_complete': '待機完了！',
    },
    'zh': {
        'system_start': 'YouTube 视频内容分析系统启动',
        'start_time': '开始时间',
        'task_overview': '任务概览',
        'total_videos': '总视频数',
        'channels': '频道数',
        'channel_details': '频道详情',
        'videos': '个视频',
        'estimated_time': '预计总时间',
        'minutes': '分钟',
        'estimated_end': '预计完成时间',
        'tips': '提示: 这是一个长时间运行的任务，您可以:',
        'tip1': '泡杯咖啡',
        'tip2': '查看进度日志了解实时状态',
        'tip3': '查看 output/video_list.csv 了解进度',
        'processing_channel': '开始处理频道',
        'videos_to_process': '待处理视频数',
        'video': '视频',
        'title': '标题',
        'id': 'ID',
        'upload_date': '上传日期',
        'skip_completed': '跳过已完成视频',
        'step_add': '步骤 1/3: 添加视频到 Notebook',
        'video_added': '视频已添加（NotebookLM 正在自动转录...）',
        'add_failed': '添加视频失败，跳过此视频',
        'step_analyze': '步骤 2/3: 分析视频并生成内容',
        'analyzing_wait': '每个版本可能需要 1-6分钟...',
        'generating_en': '生成英文版...',
        'generating_cn': '生成中文版...',
        'generating_jp': '生成日文版...',
        'en_saved': '英文版已保存',
        'cn_saved': '中文版已保存',
        'jp_saved': '日文版已保存',
        'en_failed': '英文版生成失败',
        'cn_failed': '中文版生成失败',
        'jp_failed': '日文版生成失败',
        'video_complete': '视频处理完成！',
        'files': '文件',
        'progress': '进度',
        'completed': '已完成',
        'step_wait': '步骤 3/3: 等待间隔',
        'wait_next': '等待后处理下一个视频',
        'all_done': '所有视频已完成！',
        'partial_complete': '视频部分完成',
        'video_failed': '视频分析失败',
        'channel_complete': '频道处理完成！',
        'processing_complete': '处理完成！',
        'end_time': '结束时间',
        'total_duration': '总耗时',
        'min': '分',
        'sec': '秒',
        'processing_stats': '处理统计',
        'output_location': '输出文件位置',
        'next_steps': '下一步',
        'next1': '查看生成的 MD 文档',
        'next2': '访问 NotebookLM: https://notebooklm.google.com',
        'next3': '检查 Notebook 中的视频内容',
        'no_pending': '没有待处理的视频',
        'wait_time': '等待时间',
        'seconds': '秒',
        'finish_time': '完成时间',
        'remaining': '剩余时间',
        'wait_complete': '等待完成！',
    }
}


def ui_msg(lang: str, key: str) -> str:
    """Get UI message in specified language"""
    return UI_MESSAGES.get(lang, UI_MESSAGES['en']).get(key, key)


async def wait_with_progress(seconds: int, reason: str, lang: str = 'en'):
    """
    Wait function with progress display

    Args:
        seconds: Seconds to wait
        reason: Reason for waiting
        lang: UI language
    """
    end_time = datetime.now() + timedelta(seconds=seconds)

    print(f"\n{'='*60}")
    print(f"⏳ {reason}")
    print(f"⏱️  {ui_msg(lang, 'wait_time')}: {seconds} {ui_msg(lang, 'seconds')} ({seconds//60} {ui_msg(lang, 'minutes')})")
    print(f"🕐 {ui_msg(lang, 'finish_time')}: {end_time.strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    remaining = seconds
    interval = 30

    while remaining > 0:
        if remaining <= interval:
            await asyncio.sleep(remaining)
            remaining = 0
        else:
            await asyncio.sleep(interval)
            remaining -= interval
            mins = remaining // 60
            secs = remaining % 60
            print(f"⏳ {ui_msg(lang, 'remaining')}: {mins} {ui_msg(lang, 'min')} {secs} {ui_msg(lang, 'sec')} ({remaining} {ui_msg(lang, 'seconds')})")

    print(f"✅ {ui_msg(lang, 'wait_complete')}\n")


class YouTubeAnalyzer:
    """YouTube Video Analyzer"""

    def __init__(self, progress_csv: Path | None = None, output_dir: Path | None = None):
        """
        Initialize analyzer

        Args:
            progress_csv: CSV progress file path (optional, uses config default)
            output_dir: Output directory (optional, uses config default)
        """
        self.progress_csv = Path(progress_csv) if progress_csv else PROGRESS_CSV
        self.output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.progress_manager = ProgressManager(self.progress_csv)
        self.client: NotebookLMClient | None = None
        self.notebooks: dict[str, str] = {}  # channel_name -> notebook_id
        self.ui_lang: str = 'en'  # UI language
        self.output_langs: list = ['cn', 'jp']  # Output languages

    async def __aenter__(self):
        """Async context manager entry"""
        # Connect to NotebookLM
        logger.info("Connecting to NotebookLM...")
        self.client = await NotebookLMClient.from_storage()
        await self.client.__aenter__()
        logger.info("Connected to NotebookLM")

        # Load existing Notebooks for cross-run reuse
        await self._load_existing_notebooks()

        return self

    async def _load_existing_notebooks(self):
        """
        Load existing Notebooks for cross-run reuse
        Looking for Notebooks with title format "YouTube 分析: {channel_name}"
        """
        try:
            logger.info("Loading existing Notebooks...")
            existing_notebooks = await self.client.notebooks.list()

            prefix = "YouTube 分析: "
            for nb in existing_notebooks:
                if nb.title and nb.title.startswith(prefix):
                    channel_name = nb.title[len(prefix):]
                    self.notebooks[channel_name] = nb.id
                    logger.info(f"  Found existing Notebook: {channel_name} -> {nb.id}")

            if self.notebooks:
                logger.info(f"Loaded {len(self.notebooks)} existing Notebooks (will reuse)")
            else:
                logger.info("No reusable Notebooks found, will create new ones")

        except Exception as e:
            logger.warning(f"Failed to load existing Notebooks: {e}")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.client:
            await self.client.__aexit__(exc_type, exc_val, exc_tb)
        logger.info("Disconnected from NotebookLM")

    async def create_or_get_notebook(self, channel_name: str) -> str:
        """
        Create or get Notebook for channel (supports cross-run reuse)

        If a Notebook named "YouTube 分析: {channel_name}" exists,
        it will be reused instead of creating a new one.

        Args:
            channel_name: Channel name

        Returns:
            Notebook ID
        """
        if channel_name in self.notebooks:
            notebook_id = self.notebooks[channel_name]
            logger.info(f"Reusing existing Notebook: {channel_name}")
            logger.info(f"   Notebook ID: {notebook_id}")
            return notebook_id

        # Create new Notebook
        notebook_title = f"YouTube 分析: {channel_name}"
        logger.info(f"Creating new Notebook: {notebook_title}")

        nb = await self.client.notebooks.create(notebook_title)
        self.notebooks[channel_name] = nb.id

        logger.info(f"Notebook created: {nb.id}")
        return nb.id

    async def add_video_to_notebook(self, notebook_id: str, video: dict) -> bool:
        """
        Add video to Notebook

        Args:
            notebook_id: Notebook ID
            video: Video info dictionary

        Returns:
            Success status
        """
        youtube_id = video['youtube_id']
        youtube_url = f"https://www.youtube.com/watch?v={youtube_id}"

        try:
            logger.info(f"Adding video: {video['youtube_title']}")
            logger.info(f"  URL: {youtube_url}")

            await self.client.sources.add_url(
                notebook_id,
                youtube_url,
                wait=WAIT_FOR_SOURCE_PROCESSING
            )

            logger.info(f"Video added: {youtube_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to add video: {e}")
            return False

    async def analyze_video(self, notebook_id: str, video: dict, language: str = 'cn') -> str | None:
        """
        Analyze a single video and generate content

        Args:
            notebook_id: Notebook ID
            video: Video info dictionary
            language: Language version ('en', 'cn', or 'jp')

        Returns:
            Generated content, or None if failed
        """
        youtube_title = video['youtube_title']
        # Use first 30 chars for source matching
        title_prefix = youtube_title[:30]

        # Select prompt and language name
        if language == 'en':
            prompt = ANALYSIS_PROMPT_EN
            lang_name = "English"
        elif language == 'jp':
            prompt = ANALYSIS_PROMPT_JP
            lang_name = "Japanese"
        else:  # cn
            prompt = ANALYSIS_PROMPT_CN
            lang_name = "Chinese"

        try:
            # Build question - explicitly specify the video source to analyze
            if language == 'en':
                question = f"""**Important: Please analyze ONLY based on the transcript content of the source named "{youtube_title}".**
**If there are multiple video sources in this Notebook, ignore others and only analyze the video whose title starts with "{title_prefix}".**

Please generate a reading version of this video according to the following requirements:

{prompt}
"""
            elif language == 'jp':
                question = f"""**重要：source 名が「{youtube_title}」の動画の書き起こし内容のみに基づいて分析してください。**
**Notebook に複数の動画 source がある場合は、他の source を無視し、タイトルの最初の30文字が「{title_prefix}」の動画のみを分析してください。**

以下の要件に従って、この動画の読み物バージョンを生成してください：

{prompt}
"""
            else:  # cn
                question = f"""**重要提示：请仅基于 source 名称为「{youtube_title}」的视频转录内容进行分析。**
**如果 Notebook 中有多个视频源，请忽略其他源，只分析标题前30个字符为「{title_prefix}」的视频。**

请按照以下要求生成这个视频的阅读版本：

{prompt}
"""

            logger.info(f"Analyzing video ({lang_name}): {youtube_title}")
            logger.info(f"Source identifier: {title_prefix}...")
            logger.info(f"Sending {lang_name} prompt to NotebookLM...")

            # Get analysis result
            result = await self.client.chat.ask(notebook_id, question)

            logger.info(f"{lang_name} analysis complete, content length: {len(result.answer)} chars")
            return result.answer

        except Exception as e:
            logger.error(f"{lang_name} analysis failed: {e}")
            return None

    async def process_channel(self, channel_name: str, videos: list[dict]):
        """
        Process all videos for a single channel

        Args:
            channel_name: Channel name
            videos: List of videos
        """
        lang = self.ui_lang

        print("\n")
        print("="*60)
        print(f"📺 {ui_msg(lang, 'processing_channel')}: {channel_name}")
        print("="*60)
        print(f"📹 {ui_msg(lang, 'videos_to_process')}: {len(videos)}")
        print(f"🕐 {ui_msg(lang, 'start_time')}: {datetime.now().strftime('%H:%M:%S')}")
        print("="*60)
        print("\n")

        # Create/get Notebook
        logger.info("Creating/getting Notebook...")
        notebook_id = await self.create_or_get_notebook(channel_name)
        logger.info(f"Notebook ID: {notebook_id}\n")

        # Process videos: add → analyze → wait
        videos_to_process = [v for v in videos if v.get('status') != 'completed']
        pending_count = len(videos_to_process)
        processed = 0

        for _i, video in enumerate(videos, 1):
            if video.get('status') == 'completed':
                print(f"⏭️  {ui_msg(lang, 'skip_completed')}: {video['youtube_title']}\n")
                continue

            processed += 1

            print("="*60)
            print(f"🎬 {ui_msg(lang, 'video')} [{processed}/{pending_count}]")
            print("="*60)
            print(f"📝 {ui_msg(lang, 'title')}: {video['youtube_title']}")
            print(f"🆔 {ui_msg(lang, 'id')}: {video['youtube_id']}")
            print(f"📅 {ui_msg(lang, 'upload_date')}: {video['uptime']}")
            print("="*60)
            print()

            # Step 1: Add video to Notebook
            print(f"📥 {ui_msg(lang, 'step_add')}")
            success = await self.add_video_to_notebook(notebook_id, video)

            if not success:
                print(f"❌ {ui_msg(lang, 'add_failed')}\n")
                self.progress_manager.update_status(video['youtube_id'], 'failed')
                continue

            print(f"✅ {ui_msg(lang, 'video_added')}\n")

            # Step 2: Analyze video and generate bilingual content
            print(f"🤖 {ui_msg(lang, 'step_analyze')}")
            print(f"⏳ {ui_msg(lang, 'analyzing_wait')}")

            # Update status to processing
            self.progress_manager.update_status(video['youtube_id'], 'processing')

            # Generate base filename
            base_filename = generate_output_filename(
                channel_name,
                video['youtube_title']
            ).replace('.md', '')

            success_count = 0
            output_files = []
            expected_count = len(self.output_langs)

            # Generate output based on selected languages
            if 'en' in self.output_langs:
                print(f"  📝 {ui_msg(lang, 'generating_en')}")
                content_en = await self.analyze_video(notebook_id, video, 'en')
                if content_en:
                    output_filename_en = f"{base_filename}_en.md"
                    output_path_en = self.output_dir / output_filename_en
                    try:
                        save_markdown(output_path_en, video, content_en)
                        print(f"  ✅ {ui_msg(lang, 'en_saved')}: {output_filename_en}")
                        success_count += 1
                        output_files.append(output_filename_en)
                    except Exception as e:
                        logger.error(f"Failed to save English version: {e}")
                else:
                    print(f"  ❌ {ui_msg(lang, 'en_failed')}")

            if 'jp' in self.output_langs:
                print(f"  📝 {ui_msg(lang, 'generating_jp')}")
                content_jp = await self.analyze_video(notebook_id, video, 'jp')
                if content_jp:
                    output_filename_jp = f"{base_filename}_jp.md"
                    output_path_jp = self.output_dir / output_filename_jp
                    try:
                        save_markdown(output_path_jp, video, content_jp)
                        print(f"  ✅ {ui_msg(lang, 'jp_saved')}: {output_filename_jp}")
                        success_count += 1
                        output_files.append(output_filename_jp)
                    except Exception as e:
                        logger.error(f"Failed to save Japanese version: {e}")
                else:
                    print(f"  ❌ {ui_msg(lang, 'jp_failed')}")

            if 'cn' in self.output_langs:
                print(f"  📝 {ui_msg(lang, 'generating_cn')}")
                content_cn = await self.analyze_video(notebook_id, video, 'cn')
                if content_cn:
                    output_filename_cn = f"{base_filename}_cn.md"
                    output_path_cn = self.output_dir / output_filename_cn
                    try:
                        save_markdown(output_path_cn, video, content_cn)
                        print(f"  ✅ {ui_msg(lang, 'cn_saved')}: {output_filename_cn}")
                        success_count += 1
                        output_files.append(output_filename_cn)
                    except Exception as e:
                        logger.error(f"Failed to save Chinese version: {e}")
                else:
                    print(f"  ❌ {ui_msg(lang, 'cn_failed')}")

            # Update status based on success count
            if success_count == expected_count:
                self.progress_manager.update_status(
                    video['youtube_id'],
                    'completed',
                    ', '.join(output_files)
                )
                print(f"\n✅ {ui_msg(lang, 'video_complete')}")
                print(f"📁 {ui_msg(lang, 'files')}: {', '.join(output_files)}")
                print(f"📊 {ui_msg(lang, 'progress')}: {processed}/{pending_count} {ui_msg(lang, 'completed')}\n")

                # Step 3: Wait before next video
                if processed < pending_count:
                    print(f"⏳ {ui_msg(lang, 'step_wait')}")
                    await wait_with_progress(
                        VIDEO_PROCESSING_DELAY,
                        f"{ui_msg(lang, 'wait_next')} ({processed}/{pending_count} {ui_msg(lang, 'completed')})",
                        lang
                    )
                else:
                    print(f"🎉 {ui_msg(lang, 'all_done')}\n")
            elif success_count > 0:
                self.progress_manager.update_status(
                    video['youtube_id'],
                    'partial' if expected_count > 1 else 'completed',
                    ', '.join(output_files)
                )
                if expected_count > 1:
                    print(f"\n⚠️ {ui_msg(lang, 'partial_complete')}")
                else:
                    print(f"\n✅ {ui_msg(lang, 'video_complete')}")
                print(f"📁 {ui_msg(lang, 'files')}: {', '.join(output_files)}")
            else:
                self.progress_manager.update_status(video['youtube_id'], 'failed')
                print(f"\n❌ {ui_msg(lang, 'video_failed')}")

        print("\n")
        print("="*60)
        print(f"✅ {ui_msg(lang, 'channel_complete')} '{channel_name}'")
        print("="*60)
        print("\n")

    async def run(self, ui_lang: str = 'en', output_langs: list = None):
        """
        Run the analyzer

        Args:
            ui_lang: UI language ('en', 'ja', 'zh')
            output_langs: List of output languages ('cn', 'jp'), defaults to both
        """
        self.ui_lang = ui_lang
        self.output_langs = output_langs if output_langs else ['cn', 'jp']
        lang = ui_lang
        start_time = datetime.now()

        print("\n")
        print("="*60)
        print(f"🚀 {ui_msg(lang, 'system_start')}")
        print("="*60)
        print(f"🕐 {ui_msg(lang, 'start_time')}: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*60)

        # Get pending videos
        pending_videos = self.progress_manager.get_pending_videos()

        if not pending_videos:
            print(f"\n✅ {ui_msg(lang, 'no_pending')}")
            return

        print(f"\n📊 {ui_msg(lang, 'task_overview')}:")
        print(f"   {ui_msg(lang, 'total_videos')}: {len(pending_videos)} {ui_msg(lang, 'videos')}")

        # Group by channel
        grouped_videos = self.progress_manager.group_by_channel(pending_videos)

        print(f"   {ui_msg(lang, 'channels')}: {len(grouped_videos)}")
        print(f"\n📋 {ui_msg(lang, 'channel_details')}:")
        for i, (channel, videos) in enumerate(grouped_videos.items(), 1):
            print(f"   {i}. {channel}: {len(videos)} {ui_msg(lang, 'videos')}")

        # Calculate estimated time
        total_videos = len(pending_videos)
        estimated_mins = (total_videos * (VIDEO_PROCESSING_DELAY + 120)) // 60
        if total_videos > 0:
            estimated_mins -= VIDEO_PROCESSING_DELAY // 60
        estimated_end = start_time + timedelta(minutes=estimated_mins)

        print(f"\n⏱️  {ui_msg(lang, 'estimated_time')}: ~{estimated_mins} {ui_msg(lang, 'minutes')}")
        print(f"🕐 {ui_msg(lang, 'estimated_end')}: {estimated_end.strftime('%H:%M:%S')}")
        print(f"\n💡 {ui_msg(lang, 'tips')}")
        print(f"   - {ui_msg(lang, 'tip1')} ☕")
        print(f"   - {ui_msg(lang, 'tip2')}")
        print(f"   - {ui_msg(lang, 'tip3')}")
        print("="*60)
        print("\n")

        # Process channels
        for channel_name, videos in grouped_videos.items():
            try:
                await self.process_channel(channel_name, videos)
            except Exception as e:
                logger.error(f"Error processing channel '{channel_name}': {e}")
                continue

        # Show statistics
        end_time = datetime.now()
        duration = end_time - start_time
        duration_mins = int(duration.total_seconds() // 60)
        duration_secs = int(duration.total_seconds() % 60)

        print("\n")
        print("="*60)
        print(f"🎉 {ui_msg(lang, 'processing_complete')}")
        print("="*60)
        print(f"🕐 {ui_msg(lang, 'start_time')}: {start_time.strftime('%H:%M:%S')}")
        print(f"🕐 {ui_msg(lang, 'end_time')}: {end_time.strftime('%H:%M:%S')}")
        print(f"⏱️  {ui_msg(lang, 'total_duration')}: {duration_mins} {ui_msg(lang, 'min')} {duration_secs} {ui_msg(lang, 'sec')}")
        print("="*60)

        print(f"\n📊 {ui_msg(lang, 'processing_stats')}:")
        stats = self.progress_manager.get_statistics()
        for status, count in stats.items():
            emoji = "✅" if status == "completed" else "❌" if status == "failed" else "⏸️"
            print(f"   {emoji} {status}: {count}")

        print(f"\n📁 {ui_msg(lang, 'output_location')}:")
        print(f"   {self.output_dir}")

        print(f"\n💡 {ui_msg(lang, 'next_steps')}:")
        print(f"   1. {ui_msg(lang, 'next1')}")
        print(f"   2. {ui_msg(lang, 'next2')}")
        print(f"   3. {ui_msg(lang, 'next3')}")
        print("="*60)
        print("\n")


async def main():
    """Main function"""
    try:
        async with YouTubeAnalyzer() as analyzer:
            await analyzer.run()
    except Exception as e:
        logger.error(f"Program error: {e}", exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
