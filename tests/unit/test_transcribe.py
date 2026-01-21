"""Unit tests for transcription utilities."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDetectInputType:
    """Tests for detectInputType function."""

    def test_detect_youtube_url(self):
        """Should detect YouTube URLs."""
        from notebooklm._transcribe import detectInputType

        assert detectInputType("https://youtube.com/watch?v=abc123") == "youtube"
        assert detectInputType("https://www.youtube.com/watch?v=abc123") == "youtube"
        assert detectInputType("https://youtu.be/abc123") == "youtube"

    def test_detect_file_path(self):
        """Should detect file paths."""
        from notebooklm._transcribe import detectInputType

        assert detectInputType("./audio.mp3") == "file"
        assert detectInputType("/path/to/audio.wav") == "file"
        assert detectInputType("podcast.m4a") == "file"

    def test_detect_non_youtube_url_as_file(self):
        """Non-YouTube URLs should be treated as file paths."""
        from notebooklm._transcribe import detectInputType

        # Non-YouTube URLs are not detected as YouTube
        assert detectInputType("https://example.com/video.mp4") == "file"


class TestGetWhisperBackend:
    """Tests for getWhisperBackend function."""

    def test_returns_valid_backend_or_none(self):
        """Should return 'mlx', 'openai', or None."""
        from notebooklm._transcribe import getWhisperBackend

        backend = getWhisperBackend()
        assert backend in ("mlx", "openai", None)


class TestCheckDependencies:
    """Tests for checkTranscribeDependencies function."""

    def test_dependencies_check_returns_tuple(self):
        """Should return a tuple of (bool, str|None)."""
        from notebooklm._transcribe import checkTranscribeDependencies

        # Test the actual function - it should return a tuple
        is_available, error_msg = checkTranscribeDependencies()
        assert isinstance(is_available, bool)
        assert error_msg is None or isinstance(error_msg, str)

    def test_yt_dlp_missing(self):
        """Should report yt-dlp missing when yt-dlp import fails."""
        import notebooklm._transcribe as transcribe_module

        # Reset the whisper backend cache to ensure clean state
        original_backend = transcribe_module._WHISPER_BACKEND
        transcribe_module._WHISPER_BACKEND = None

        try:
            # Mock yt_dlp import to raise ImportError
            # Mock whisper to be available so only yt-dlp is missing
            with (
                patch.dict("sys.modules", {"yt_dlp": None}),
                patch.object(
                    transcribe_module,
                    "_detectWhisperBackend",
                    return_value="openai",
                ),
            ):
                is_available, error_msg = transcribe_module.checkTranscribeDependencies()

                assert is_available is False
                assert error_msg is not None
                assert "yt-dlp" in error_msg
        finally:
            # Restore the cache
            transcribe_module._WHISPER_BACKEND = original_backend


class TestTranscriptionResult:
    """Tests for TranscriptionResult namedtuple."""

    def test_create_result(self):
        """Should create TranscriptionResult with correct fields."""
        from notebooklm._transcribe import TranscriptionResult

        result = TranscriptionResult(
            text="Hello world",
            language="en",
            source_title="Test Audio",
        )

        assert result.text == "Hello world"
        assert result.language == "en"
        assert result.source_title == "Test Audio"

    def test_result_is_tuple(self):
        """TranscriptionResult should be a NamedTuple."""
        from notebooklm._transcribe import TranscriptionResult

        result = TranscriptionResult("text", "en", "title")
        assert isinstance(result, tuple)
        assert len(result) == 3


class TestExceptions:
    """Tests for custom exception classes."""

    def test_dependency_missing_error(self):
        """Should create DependencyMissingError."""
        from notebooklm._transcribe import DependencyMissingError

        error = DependencyMissingError("yt-dlp not installed")
        assert str(error) == "yt-dlp not installed"
        assert isinstance(error, Exception)

    def test_youtube_download_error(self):
        """Should create YouTubeDownloadError."""
        from notebooklm._transcribe import YouTubeDownloadError

        error = YouTubeDownloadError("Download failed")
        assert str(error) == "Download failed"
        assert isinstance(error, Exception)

    def test_transcription_failed_error(self):
        """Should create TranscriptionFailedError."""
        from notebooklm._transcribe import TranscriptionFailedError

        error = TranscriptionFailedError("Model failed")
        assert str(error) == "Model failed"
        assert isinstance(error, Exception)

    def test_audio_file_not_found_error(self):
        """Should create AudioFileNotFoundError."""
        from notebooklm._transcribe import AudioFileNotFoundError

        error = AudioFileNotFoundError("File not found: test.mp3")
        assert str(error) == "File not found: test.mp3"
        assert isinstance(error, Exception)


class TestDownloadYoutubeAudio:
    """Tests for downloadYoutubeAudio function."""

    def test_downloads_audio_and_returns_path_and_title(self, tmp_path):
        """Should download audio using yt-dlp and return file path and title."""
        import importlib
        import sys

        # Create a fake downloaded mp3 file
        fake_audio = tmp_path / "Test Video.mp3"
        fake_audio.write_bytes(b"fake audio content")

        mock_ydl_instance = MagicMock()
        mock_ydl_instance.extract_info.return_value = {"title": "Test Video"}

        # Create mock yt_dlp module with YoutubeDL as context manager
        mock_yt_dlp = MagicMock()
        mock_yt_dlp.YoutubeDL.return_value.__enter__.return_value = mock_ydl_instance
        mock_yt_dlp.YoutubeDL.return_value.__exit__.return_value = False
        mock_yt_dlp.utils.DownloadError = Exception  # Mock the exception class

        # Save original module if exists
        original_yt_dlp = sys.modules.get("yt_dlp")

        try:
            # Replace yt_dlp in sys.modules
            sys.modules["yt_dlp"] = mock_yt_dlp

            # Reload the module to pick up our mock
            import notebooklm._transcribe as transcribe_module

            importlib.reload(transcribe_module)

            audio_path, title = transcribe_module.downloadYoutubeAudio(
                "https://youtube.com/watch?v=test123",
                output_dir=tmp_path,
            )

            assert audio_path == fake_audio
            assert title == "Test Video"
            mock_ydl_instance.extract_info.assert_called_once_with(
                "https://youtube.com/watch?v=test123", download=True
            )
        finally:
            # Restore original module
            if original_yt_dlp is not None:
                sys.modules["yt_dlp"] = original_yt_dlp
            elif "yt_dlp" in sys.modules:
                del sys.modules["yt_dlp"]

            # Reload to restore original state
            importlib.reload(transcribe_module)

    def test_raises_dependency_error_when_yt_dlp_missing(self):
        """Should raise DependencyMissingError when yt-dlp is not installed."""
        from notebooklm._transcribe import DependencyMissingError, downloadYoutubeAudio

        with patch.dict("sys.modules", {"yt_dlp": None}):
            with pytest.raises(DependencyMissingError) as exc_info:
                downloadYoutubeAudio("https://youtube.com/watch?v=test123")

            assert "yt-dlp" in str(exc_info.value)


class TestTranscribeAudio:
    """Tests for transcribeAudio function."""

    def test_raises_error_for_missing_file(self):
        """Should raise AudioFileNotFoundError for non-existent file."""
        from notebooklm._transcribe import AudioFileNotFoundError, transcribeAudio

        with pytest.raises(AudioFileNotFoundError) as exc_info:
            transcribeAudio(Path("/non/existent/audio.mp3"))

        assert "Audio file not found" in str(exc_info.value)

    def test_function_exists(self):
        """Should have transcribeAudio function available."""
        from notebooklm._transcribe import transcribeAudio

        assert callable(transcribeAudio)


class TestTranscribeFromYoutube:
    """Tests for transcribeFromYoutube function."""

    def test_calls_download_and_transcribe(self):
        """Should download audio and transcribe it."""
        from notebooklm._transcribe import TranscriptionResult, transcribeFromYoutube

        mock_audio_path = Path("/tmp/test_audio.mp3")
        mock_result = TranscriptionResult(
            text="Test transcription",
            language="en",
            source_title="test_audio",
        )

        with (
            patch("notebooklm._transcribe.downloadYoutubeAudio") as mock_download,
            patch("notebooklm._transcribe.transcribeAudio") as mock_transcribe,
            patch.object(Path, "unlink"),
            patch.object(Path, "parent", new_callable=lambda: MagicMock()),
        ):
            mock_download.return_value = (mock_audio_path, "Test Video Title")
            mock_transcribe.return_value = mock_result

            result, audio_path = transcribeFromYoutube(
                "https://youtube.com/watch?v=test",
                model="base",
                language="en",
                keep_audio=False,
            )

            assert result.text == "Test transcription"
            assert result.source_title == "Test Video Title"
            mock_download.assert_called_once()
            mock_transcribe.assert_called_once()
