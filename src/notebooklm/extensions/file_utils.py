#!/usr/bin/env python3
"""
文件处理工具
包括文件名清理和 Markdown 格式化
"""

import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def sanitize_filename(title: str, max_length: int = 100) -> str:
    """
    清理文件名，移除非法字符

    Args:
        title: 原始标题
        max_length: 最大长度

    Returns:
        清理后的文件名
    """
    # 移除或替换 Windows 文件名非法字符
    illegal_chars = r'[<>:"/\\|?*]'
    cleaned = re.sub(illegal_chars, '_', title)

    # 移除前后空格
    cleaned = cleaned.strip()

    # 限制长度
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].strip()

    # 如果为空，使用默认名称
    if not cleaned:
        cleaned = "untitled"

    logger.debug(f"文件名清理: '{title}' -> '{cleaned}'")
    return cleaned


def format_markdown(video_info: dict, content: str) -> str:
    """
    格式化 Markdown 文档，添加元数据头部

    Args:
        video_info: 视频信息字典
        content: 正文内容

    Returns:
        格式化后的 Markdown 文本
    """
    # 构建 YouTube URL
    youtube_id = video_info.get('youtube_id', '')
    youtube_url = f"https://www.youtube.com/watch?v={youtube_id}"

    # 获取频道名称
    channel_name = video_info.get('channel_name', 'Unknown')

    # 获取标题
    title = video_info.get('youtube_title', 'Untitled')

    # 获取上传日期
    uptime = video_info.get('uptime', '')

    # 生成时间
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 构建 Markdown 文档
    markdown = f"""---
title: {title}
channel: {channel_name}
youtube_id: {youtube_id}
url: {youtube_url}
upload_date: {uptime}
generated_at: {generated_at}
---

# {title}

**频道**: {channel_name}
**上传日期**: {uptime}
**视频链接**: [{youtube_url}]({youtube_url})

---

{content}

---

*本文档由 NotebookLM 自动生成于 {generated_at}*
"""

    return markdown


def save_markdown(output_path: Path, video_info: dict, content: str):
    """
    保存 Markdown 文件

    Args:
        output_path: 输出文件路径
        video_info: 视频信息
        content: 内容
    """
    try:
        formatted_content = format_markdown(video_info, content)

        # 确保目录存在
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 写入文件
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(formatted_content)

        logger.info(f"保存 Markdown 文件: {output_path}")

    except Exception as e:
        logger.error(f"保存 Markdown 文件失败: {e}")
        raise


def generate_output_filename(channel_name: str, youtube_title: str) -> str:
    """
    生成输出文件名

    Args:
        channel_name: 频道名称
        youtube_title: 视频标题

    Returns:
        输出文件名（不含路径）
    """
    # 清理频道名和标题
    clean_channel = sanitize_filename(channel_name, max_length=30)
    clean_title = sanitize_filename(youtube_title, max_length=60)

    # 组合文件名
    filename = f"{clean_channel}_{clean_title}.md"

    return filename


def read_markdown(file_path: Path) -> str:
    """
    读取 Markdown 文件

    Args:
        file_path: 文件路径

    Returns:
        文件内容
    """
    try:
        with open(file_path, encoding='utf-8') as f:
            content = f.read()
        logger.debug(f"读取文件: {file_path}")
        return content
    except Exception as e:
        logger.error(f"读取文件失败: {e}")
        raise

