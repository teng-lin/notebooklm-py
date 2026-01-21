"""Transcription utilities for audio/video content.

This module provides functionality to:
1. Download audio from YouTube URLs using yt-dlp
2. Transcribe audio files using Whisper (mlx-whisper on Mac, openai-whisper elsewhere)

Requires the [transcribe] or [transcribe-mlx] optional dependency:
    pip install notebooklm-py[transcribe]      # Cross-platform (openai-whisper)
    pip install notebooklm-py[transcribe-mlx]  # Mac Apple Silicon (mlx-whisper, faster)
"""

import tempfile
from pathlib import Path
from typing import NamedTuple

from ._url_utils import is_youtube_url

# Whisper backend detection
_WHISPER_BACKEND: str | None = None


def _detectWhisperBackend() -> str | None:
    """Detect which whisper backend is available.

    Returns:
        "mlx" if mlx-whisper is available (preferred on Mac),
        "openai" if openai-whisper is available,
        None if neither is installed.
    """
    global _WHISPER_BACKEND
    if _WHISPER_BACKEND is not None:
        return _WHISPER_BACKEND

    # Try mlx-whisper first (faster on Mac Apple Silicon)
    try:
        import mlx_whisper  # noqa: F401

        _WHISPER_BACKEND = "mlx"
        return _WHISPER_BACKEND
    except ImportError:
        pass

    # Fallback to openai-whisper
    try:
        import whisper  # noqa: F401

        _WHISPER_BACKEND = "openai"
        return _WHISPER_BACKEND
    except ImportError:
        pass

    return None


def getWhisperBackend() -> str | None:
    """Get the name of the available whisper backend.

    Returns:
        "mlx" for mlx-whisper, "openai" for openai-whisper, None if unavailable.
    """
    return _detectWhisperBackend()


class TranscriptionResult(NamedTuple):
    """Result of a transcription operation."""

    text: str
    language: str
    source_title: str


class DependencyMissingError(Exception):
    """Raised when required transcription dependencies are not installed."""


class YouTubeDownloadError(Exception):
    """Raised when YouTube audio download fails."""


class TranscriptionFailedError(Exception):
    """Raised when audio transcription fails."""


class AudioFileNotFoundError(Exception):
    """Raised when the specified audio file does not exist."""


def checkTranscribeDependencies() -> tuple[bool, str | None]:
    """Check if transcription dependencies are installed.

    Returns:
        Tuple of (is_available, error_message).
        If available, returns (True, None).
        If not available, returns (False, error_description).
    """
    missing = []

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append("yt-dlp")

    # Check for either whisper backend
    whisper_backend = _detectWhisperBackend()
    if whisper_backend is None:
        missing.append("whisper (openai-whisper or mlx-whisper)")

    if missing:
        deps = ", ".join(missing)
        return (
            False,
            f"Missing dependencies: {deps}. "
            "Run: pip install notebooklm-py[transcribe] (cross-platform) "
            "or pip install notebooklm-py[transcribe-mlx] (Mac Apple Silicon)",
        )

    return (True, None)


def detectInputType(content: str) -> str:
    """Detect whether input is a YouTube URL or local file path.

    Args:
        content: URL or file path string

    Returns:
        "youtube" for YouTube URLs, "file" for local paths
    """
    if is_youtube_url(content):
        return "youtube"
    return "file"


def downloadYoutubeAudio(url: str, output_dir: Path | None = None) -> tuple[Path, str]:
    """Download audio from a YouTube video.

    Args:
        url: YouTube video URL
        output_dir: Directory to save the audio file. Uses temp dir if None.

    Returns:
        Tuple of (audio_file_path, video_title)

    Raises:
        DependencyMissingError: If yt-dlp is not installed
        YouTubeDownloadError: If download fails
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise DependencyMissingError(
            "yt-dlp not installed. Run: pip install notebooklm-py[transcribe]"
        ) from e

    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="notebooklm_transcribe_"))

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Configure yt-dlp to extract audio
    output_template = str(output_dir / "%(title)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "Unknown")

            # Find the downloaded file
            # yt-dlp converts to mp3, so look for that
            for file in output_dir.iterdir():
                if file.suffix == ".mp3":
                    return (file, title)

            # Fallback: look for any audio file
            for file in output_dir.iterdir():
                if file.suffix in (".mp3", ".m4a", ".wav", ".webm", ".opus"):
                    return (file, title)

            raise YouTubeDownloadError(f"Downloaded file not found in {output_dir}")

    except yt_dlp.utils.DownloadError as e:
        raise YouTubeDownloadError(f"Failed to download audio: {e}") from e
    except Exception as e:
        raise YouTubeDownloadError(f"Unexpected error during download: {e}") from e


def transcribeAudio(
    audio_path: Path | str,
    model: str = "base",
    language: str | None = None,
) -> TranscriptionResult:
    """Transcribe an audio file using Whisper.

    Automatically uses mlx-whisper on Mac Apple Silicon if available,
    otherwise falls back to openai-whisper.

    Args:
        audio_path: Path to the audio file
        model: Whisper model name (tiny, base, small, medium, large).
               For mlx-whisper, uses mlx-community models automatically.
        language: Language code (e.g., "en", "zh"). Auto-detected if None.

    Returns:
        TranscriptionResult with text, detected language, and source title

    Raises:
        DependencyMissingError: If no whisper backend is installed
        AudioFileNotFoundError: If the audio file doesn't exist
        TranscriptionFailedError: If transcription fails
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise AudioFileNotFoundError(f"Audio file not found: {audio_path}")

    backend = _detectWhisperBackend()
    if backend is None:
        raise DependencyMissingError(
            "No whisper backend installed. "
            "Run: pip install notebooklm-py[transcribe] (cross-platform) "
            "or pip install notebooklm-py[transcribe-mlx] (Mac Apple Silicon)"
        )

    try:
        if backend == "mlx":
            return _transcribeWithMlxWhisper(audio_path, model, language)
        else:
            return _transcribeWithOpenaiWhisper(audio_path, model, language)
    except Exception as e:
        raise TranscriptionFailedError(f"Transcription failed: {e}") from e


def _transcribeWithMlxWhisper(
    audio_path: Path,
    model: str,
    language: str | None,
) -> TranscriptionResult:
    """Transcribe using mlx-whisper (Mac Apple Silicon)."""
    import mlx_whisper

    # Map model names to mlx-community model IDs
    model_map = {
        "tiny": "mlx-community/whisper-tiny",
        "base": "mlx-community/whisper-base",
        "small": "mlx-community/whisper-small",
        "medium": "mlx-community/whisper-medium",
        "large": "mlx-community/whisper-large-v3",
        "large-v3": "mlx-community/whisper-large-v3",
        "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    }

    # Use mapped model or assume it's already a full model path
    mlx_model = model_map.get(model, model)

    # Prepare transcription options
    options = {"path_or_hf_repo": mlx_model}
    if language:
        options["language"] = language

    result = mlx_whisper.transcribe(str(audio_path), **options)

    detected_language = result.get("language", language or "unknown")
    text = result.get("text", "").strip()

    return TranscriptionResult(
        text=text,
        language=detected_language,
        source_title=audio_path.stem,
    )


def _transcribeWithOpenaiWhisper(
    audio_path: Path,
    model: str,
    language: str | None,
) -> TranscriptionResult:
    """Transcribe using openai-whisper (cross-platform)."""
    import whisper

    whisper_model = whisper.load_model(model)

    # Prepare transcription options
    options = {}
    if language:
        options["language"] = language

    result = whisper_model.transcribe(str(audio_path), **options)

    detected_language = result.get("language", language or "unknown")
    text = result.get("text", "").strip()

    return TranscriptionResult(
        text=text,
        language=detected_language,
        source_title=audio_path.stem,
    )


def _cleanupAudioFile(audio_path: Path) -> None:
    """Remove audio file and parent directory if empty."""
    audio_path.unlink(missing_ok=True)
    try:
        audio_path.parent.rmdir()
    except OSError:
        pass  # Directory not empty or other error


def transcribeFromYoutube(
    url: str,
    model: str = "base",
    language: str | None = None,
    keep_audio: bool = False,
    output_dir: Path | None = None,
) -> tuple[TranscriptionResult, Path | None]:
    """Download and transcribe audio from a YouTube video.

    Args:
        url: YouTube video URL
        model: Whisper model name (tiny, base, small, medium, large)
        language: Language code for transcription. Auto-detected if None.
        keep_audio: If True, keep the downloaded audio file
        output_dir: Directory for audio file. Uses temp dir if None.

    Returns:
        Tuple of (TranscriptionResult, audio_path or None if not kept)

    Raises:
        DependencyMissingError: If dependencies are missing
        YouTubeDownloadError: If download fails
        TranscriptionFailedError: If transcription fails
    """
    audio_path, title = downloadYoutubeAudio(url, output_dir)

    try:
        result = transcribeAudio(audio_path, model=model, language=language)

        # Update title from YouTube metadata
        result = TranscriptionResult(
            text=result.text,
            language=result.language,
            source_title=title,
        )

        if keep_audio:
            return (result, audio_path)

        _cleanupAudioFile(audio_path)
        return (result, None)

    except Exception:
        if not keep_audio:
            _cleanupAudioFile(audio_path)
        raise
