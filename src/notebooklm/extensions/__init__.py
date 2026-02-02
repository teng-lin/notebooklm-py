"""
NotebookLM Extensions

Additional functionality for notebooklm-py.
"""

# Export configuration (if needed)
from .config import (
    ANALYSIS_PROMPT,
    ANALYSIS_PROMPT_CN,
    ANALYSIS_PROMPT_EN,
    ANALYSIS_PROMPT_JP,
    OUTPUT_DIR,
    PROGRESS_CSV,  # Backward compatibility alias
    VIDEO_LIST_CSV,
)
from .csv_utils import ProgressManager
from .error_handler import ErrorHandler, ErrorLogger, retry_on_error
from .youtube_analyzer import YouTubeAnalyzer

__all__ = [
    "YouTubeAnalyzer",
    "ProgressManager",
    "ErrorHandler",
    "ErrorLogger",
    "retry_on_error",
    "ANALYSIS_PROMPT_CN",
    "ANALYSIS_PROMPT_JP",
    "ANALYSIS_PROMPT_EN",
    "ANALYSIS_PROMPT",
    "OUTPUT_DIR",
    "VIDEO_LIST_CSV",
    "PROGRESS_CSV",
]

__version__ = "1.0.0"
