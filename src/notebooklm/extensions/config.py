#!/usr/bin/env python3
"""
YouTube Analyzer Configuration
改进版配置 - 减少 AI 幻觉，更忠实原文
"""

import os
from pathlib import Path

# ===== Path Configuration =====
# These can be overridden by environment variables

# Output directory (default: ./output relative to where script is run)
OUTPUT_DIR = Path(os.getenv("YOUTUBE_OUTPUT_DIR", "./output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# CSV video list file path (serves as both input and output)
# User adds videos here, script updates status
VIDEO_LIST_CSV = Path(os.getenv("YOUTUBE_VIDEO_LIST_CSV", str(OUTPUT_DIR / "video_list.csv")))

# Backward compatibility alias
PROGRESS_CSV = VIDEO_LIST_CSV

# ===== Logging Configuration =====
LOG_LEVEL = os.getenv("YOUTUBE_LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

# ===== Retry Configuration =====
MAX_RETRIES = int(os.getenv("YOUTUBE_MAX_RETRIES", "3"))
RETRY_DELAY = int(os.getenv("YOUTUBE_RETRY_DELAY", "5"))

# ===== API Configuration =====
WAIT_FOR_SOURCE_PROCESSING = os.getenv("WAIT_FOR_SOURCE_PROCESSING", "true").lower() == "true"

# ===== Batch Processing Configuration =====
# Delay between video processing (seconds)
# Give NotebookLM enough time to complete transcription, avoid API limits
VIDEO_PROCESSING_DELAY = int(
    os.getenv("VIDEO_PROCESSING_DELAY", "300")
)  # 5 minutes, suitable for free accounts

# ===== Analysis Prompts =====

# YouTube 内容结构化整理提示词 v2.1 - 中文版
ANALYSIS_PROMPT_CN = """你将把一段YouTube视频转录内容重写成深度阅读版本，按内容主题分成若干小节。目标是让读者通过阅读就能完整理解视频内容，如同阅读一篇专业的分析文章。

## 核心原则（严格遵守）
- **忠实原文**：仅基于提供的转录内容，绝不添加、推测或引用外部信息
- **保持原意**：遇到模糊表述时，保持原始含义并标注"（原文表述模糊）"
- **避免包装**：不要将简单信息包装成"研究发现"或"权威分析"
- **拒绝编造**：不创造原文中不存在的框架、数据或引用关系

## 输出结构

### 1. Overview
用150-200字概述视频的核心议题与主要结论。使用转录中的实际表述，避免过度概括。

### 2. 主题详解
根据转录内容的自然段落划分主题，每个主题需要：
- **标题**：反映该段落的核心内容
- **详细展开**：基于转录内容进行合理扩展，但不超出原意范围
- **关键信息保留**：
  - 具体数字、时间、人名保持原样
  - 专业术语保留日文原文，括号内注明中文含义（如确定）
  - 重要原话用「」标注
- **本土化表达**：
  - 使用自然的中文表达习惯
  - 适当保留日语语境中的表达方式
  - 避免生硬的翻译腔

### 4. 实务启示（仅当转录中明确提及时）
如果视频中确实讨论了具体的方法或建议，整理为：
- **背景说明**：基于转录内容的上下文
- **具体做法**：仅提取转录中明确提到的操作建议
- **注意事项**：如有相关讨论

## 写作规范
- **段落控制**：每段不超过200字，使用bullet points增强可读性
- **语言风格**：
  - 使用"业内人士表示"而非"专家认为"
  - 使用"据视频介绍"而非"根据研究"
  - 保持客观描述，避免过度解读
- **不确定性处理**：
  - 对于转录中的模糊表述，使用"似乎"、"可能"等词汇
  - 明确标注"（转录中表述不够清晰）"
- **避免AI味**：
  - 不使用"深度解析"、"全面剖析"等夸张词汇
  - 避免过度结构化的小标题
  - 保持朴实的叙述风格

## 严禁事项
❌ 创造转录中不存在的人名、数据、案例
❌ 添加引用标记[1][2]等学术格式
❌ 编造"框架"、"模型"等理论体系
❌ 使用"研究表明"、"数据显示"等权威包装
❌ 补充转录外的背景信息或行业知识
❌ 创造过于精确的技术描述

## NotebookLM适配
考虑到NotebookLM的上下文窗口限制，请：
- 优先处理转录中的核心内容
- 如内容过长，按重要性排序处理
- 保持输出的连贯性和完整性"""

# YouTube 内容结构化整理提示词 v2.1 - 日文版
ANALYSIS_PROMPT_JP = """このYouTube動画の書き起こし内容を、テーマごとに分けて詳細な読み物バージョンに書き直してください。読者が動画を見なくても内容を完全に理解できることを目標とします。

## 核心原則（厳守）
- **原文に忠実**：提供された書き起こし内容のみに基づき、推測や外部情報の追加は絶対にしない
- **原意を保持**：曖昧な表現は原文のまま保ち、「（原文の表現が不明瞭）」と注記する
- **装飾しない**：シンプルな情報を「研究によると」や「専門家の分析」などで装飾しない
- **捏造禁止**：原文に存在しないフレームワーク、データ、引用関係を創作しない

## 出力構造

### 1. 概要（Overview）
150-200文字で動画の核心的な論点と主要な結論を概説してください。書き起こしの実際の表現を使用し、過度な要約は避けてください。

### 2. テーマ別詳解
書き起こし内容の自然な段落に従ってテーマを分け、各テーマについて：
- **タイトル**：その段落の核心的な内容を反映
- **詳細展開**：書き起こし内容に基づいて合理的に展開するが、原意の範囲を超えない
- **重要情報の保持**：
  - 具体的な数字、時間、人名はそのまま保持
  - 専門用語は原文のまま使用
  - 重要な原文は「」で引用
- **自然な表現**：
  - 自然な日本語表現を使用
  - 原文のニュアンスを適切に保持
  - 直訳調を避ける

### 3. 実務的示唆（書き起こしで明確に言及されている場合のみ）
動画で具体的な方法や提案が議論されている場合：
- **背景説明**：書き起こし内容の文脈に基づく
- **具体的な方法**：書き起こしで明確に言及された操作的提案のみ抽出
- **注意事項**：関連する議論がある場合

## 執筆規範
- **段落コントロール**：各段落は200文字以内、箇条書きで可読性を向上
- **言語スタイル**：
  - 客観的な記述を保ち、過度な解釈を避ける
  - 書き起こしの実際の表現を尊重
- **不確実性の処理**：
  - 書き起こしの曖昧な表現には「ようだ」「可能性がある」などを使用
  - 「（書き起こしでの表現が不明瞭）」と明示
- **AI臭さを避ける**：
  - 「徹底解説」「完全分析」などの誇張表現を使用しない
  - 過度に構造化された小見出しを避ける
  - 素朴な叙述スタイルを保持

## 厳禁事項
❌ 書き起こしに存在しない人名、データ、事例を創作
❌ [1][2]などの引用マークや学術的フォーマットを追加
❌ 「フレームワーク」「モデル」などの理論体系を編造
❌ 「研究によると」「データが示す」などの権威的な装飾
❌ 書き起こし外の背景情報や業界知識を補足
❌ 過度に精密な技術的記述を創造

## NotebookLM適応
NotebookLMのコンテキストウィンドウ制限を考慮して：
- 書き起こしの核心的な内容を優先的に処理
- 内容が長い場合は重要度順に処理
- 出力の一貫性と完整性を保持"""

# YouTube Content Structuring Prompt v2.1 - English Version
ANALYSIS_PROMPT_EN = """You will rewrite a YouTube video transcript into a deep reading version, organized by content themes into several sections. The goal is to allow readers to fully understand the video content through reading, as if reading a professional analysis article.

## Core Principles (Strictly Follow)
- **Faithful to Original**: Only based on the provided transcript, never add, speculate, or cite external information
- **Preserve Original Meaning**: When encountering ambiguous expressions, maintain original meaning and mark "(original expression unclear)"
- **Avoid Packaging**: Do not package simple information as "research findings" or "expert analysis"
- **No Fabrication**: Do not create frameworks, data, or citation relationships that don't exist in the original

## Output Structure

### 1. Overview
Summarize the core topics and main conclusions of the video in 150-200 words. Use actual expressions from the transcript, avoid over-generalization.

### 2. Theme Details
Divide themes according to natural paragraphs in the transcript, each theme needs:
- **Title**: Reflect the core content of that section
- **Detailed Expansion**: Reasonably expand based on transcript content, but don't exceed original meaning
- **Key Information Retention**:
  - Keep specific numbers, times, names as they are
  - Retain professional terms with explanations if needed
  - Mark important original quotes with quotation marks
- **Natural Expression**:
  - Use natural English expression habits
  - Appropriately retain context-specific expressions
  - Avoid stiff translation tone

### 3. Practical Insights (Only if explicitly mentioned in transcript)
If the video discusses specific methods or suggestions:
- **Background**: Based on transcript context
- **Specific Approaches**: Only extract operational suggestions explicitly mentioned
- **Notes**: If related discussions exist

## Writing Standards
- **Paragraph Control**: Each paragraph no more than 200 words, use bullet points to enhance readability
- **Language Style**:
  - Use "according to the video" rather than "research shows"
  - Maintain objective description, avoid over-interpretation
- **Uncertainty Handling**:
  - Use "seems", "possibly" for ambiguous expressions
  - Clearly mark "(transcript expression unclear)"
- **Avoid AI-like Writing**:
  - Don't use exaggerated words like "deep analysis", "comprehensive breakdown"
  - Avoid overly structured subheadings
  - Maintain plain narrative style

## Prohibited Items
❌ Create names, data, cases that don't exist in transcript
❌ Add citation markers [1][2] or academic formats
❌ Fabricate "frameworks", "models" or theoretical systems
❌ Use authoritative packaging like "research shows", "data indicates"
❌ Supplement background information or industry knowledge outside transcript
❌ Create overly precise technical descriptions

## NotebookLM Adaptation
Considering NotebookLM's context window limitations:
- Prioritize core content in transcript
- Process by importance if content is too long
- Maintain output coherence and completeness"""

# Backward compatibility: default to Chinese version
ANALYSIS_PROMPT = ANALYSIS_PROMPT_CN
