#!/usr/bin/env python3
"""
YouTube 视频分析器 - 主程序入口
"""

import asyncio
import sys

# 修复 Windows + Python 3.12 兼容性
if sys.platform == "win32" and sys.version_info >= (3, 12):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ⚠️ 使用相对导入
from .config import OUTPUT_DIR, PROGRESS_CSV
from .youtube_analyzer import YouTubeAnalyzer


async def main():
    """主函数"""
    print("=" * 60)
    print("🚀 YouTube 视频分析器")
    print("=" * 60)
    print(f"📊 进度文件: {PROGRESS_CSV}")
    print(f"📁 输出目录: {OUTPUT_DIR}")
    print("=" * 60)
    print("\n")

    async with YouTubeAnalyzer() as analyzer:
        await analyzer.run()

    print("\n✅ 所有任务完成！")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 错误: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
