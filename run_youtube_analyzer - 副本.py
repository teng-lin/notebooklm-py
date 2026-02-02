#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube 视频分析器 - 运行脚本
Run this script from the repository root directory.

Usage:
    python run_youtube_analyzer.py
"""
import sys
import asyncio

# Windows + Python 3.12 兼容性修复
if sys.platform == 'win32' and sys.version_info >= (3, 12):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from pathlib import Path

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm.extensions import YouTubeAnalyzer
from notebooklm.extensions.config import PROGRESS_CSV, OUTPUT_DIR


async def main():
    """主函数"""
    print("=" * 60)
    print("🚀 YouTube 视频分析器")
    print("=" * 60)
    print(f"📊 进度文件: {PROGRESS_CSV}")
    print(f"📁 输出目录: {OUTPUT_DIR}")
    print("=" * 60)
    
    async with YouTubeAnalyzer() as analyzer:
        # 显示待处理视频
        pending = analyzer.progress_manager.get_pending_videos()
        print(f"\n📋 待处理视频: {len(pending)} 个")
        
        if pending:
            print("\n前 5 个待处理视频:")
            for i, v in enumerate(pending[:5], 1):
                title = v.get('youtube_title', 'Unknown')[:40]
                print(f"  {i}. [{v.get('channel_name', 'Unknown')}] {title}...")
        
        print("\n" + "=" * 60)
        print("开始分析...")
        print("=" * 60 + "\n")
        
        await analyzer.run()
    
    print("\n" + "=" * 60)
    print("✅ 所有任务完成！")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断 (Ctrl+C)")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
