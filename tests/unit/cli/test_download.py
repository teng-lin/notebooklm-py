"""Tests for download CLI commands."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Artifact

from .conftest import create_mock_client, get_cli_module, patch_client_for_module

# Get the actual download module (not the click group that shadows it)
download_module = get_cli_module("download")


def make_artifact(
    id: str, title: str, _artifact_type: int, status: int = 3, created_at: datetime = None
) -> Artifact:
    """Create an Artifact for testing."""
    return Artifact(
        id=id,
        title=title,
        _artifact_type=_artifact_type,
        status=status,
        created_at=created_at or datetime.fromtimestamp(1234567890),
    )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


@pytest.fixture
def mock_fetch_tokens():
    """Mock fetch_tokens and load_auth_from_storage at download module level.

    Download.py imports these functions directly, so we must patch at the module
    level where they're imported (not at helpers where they're defined).
    """
    with (
        patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
        patch.object(download_module, "load_auth_from_storage") as mock_load,
    ):
        mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
        mock_fetch.return_value = ("csrf", "session")
        yield mock_fetch


# =============================================================================
# DOWNLOAD AUDIO TESTS
# =============================================================================


class TestDownloadAudio:
    def test_download_audio(self, runner, mock_auth, tmp_path):
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "audio.mp3"

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake audio content")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "My Audio", 1)]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["download", "audio", str(output_file), "-n", "nb_123"])

            assert result.exit_code == 0
            assert output_file.exists()

    def test_download_audio_dry_run(self, runner, mock_auth, tmp_path):
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "My Audio", 1)]
            )
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["download", "audio", "--dry-run", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "DRY RUN" in result.output

    def test_download_audio_no_artifacts(self, runner, mock_auth):
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

            assert "No completed audio artifacts found" in result.output or result.exit_code != 0


# =============================================================================
# DOWNLOAD VIDEO TESTS
# =============================================================================


class TestDownloadVideo:
    def test_download_video(self, runner, mock_auth, tmp_path):
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "video.mp4"

            async def mock_download_video(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake video content")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("vid_1", "My Video", 3)]
            )
            mock_client.artifacts.download_video = mock_download_video
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["download", "video", str(output_file), "-n", "nb_123"])

            assert result.exit_code == 0
            assert output_file.exists()


# =============================================================================
# DOWNLOAD INFOGRAPHIC TESTS
# =============================================================================


class TestDownloadInfographic:
    def test_download_infographic(self, runner, mock_auth, tmp_path):
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "infographic.png"

            async def mock_download_infographic(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake image content")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("info_1", "My Infographic", 7)]
            )
            mock_client.artifacts.download_infographic = mock_download_infographic
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "infographic", str(output_file), "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert output_file.exists()


# =============================================================================
# DOWNLOAD SLIDE DECK TESTS
# =============================================================================


class TestDownloadSlideDeck:
    def test_download_slide_deck(self, runner, mock_auth, tmp_path):
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_dir = tmp_path / "slides"

            async def mock_download_slide_deck(notebook_id, output_path, artifact_id=None):
                Path(output_path).mkdir(parents=True, exist_ok=True)
                (Path(output_path) / "slide_1.png").write_bytes(b"fake slide")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("slide_1", "My Slides", 8)]
            )
            mock_client.artifacts.download_slide_deck = mock_download_slide_deck
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "slide-deck", str(output_dir), "-n", "nb_123"]
                )

            assert result.exit_code == 0


# =============================================================================
# DOWNLOAD FLAGS TESTS
# =============================================================================


class TestDownloadFlags:
    def test_download_audio_latest(self, runner, mock_auth, tmp_path):
        """Test --latest flag selects most recent artifact"""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "audio.mp3"

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake audio")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact(
                        "audio_old", "Old Audio", 1, created_at=datetime.fromtimestamp(1000000000)
                    ),
                    make_artifact(
                        "audio_new", "New Audio", 1, created_at=datetime.fromtimestamp(2000000000)
                    ),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "audio", str(output_file), "--latest", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_download_audio_earliest(self, runner, mock_auth, tmp_path):
        """Test --earliest flag selects oldest artifact"""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "audio.mp3"

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake audio")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact(
                        "audio_old", "Old Audio", 1, created_at=datetime.fromtimestamp(1000000000)
                    ),
                    make_artifact(
                        "audio_new", "New Audio", 1, created_at=datetime.fromtimestamp(2000000000)
                    ),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "audio", str(output_file), "--earliest", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_download_force_overwrites(self, runner, mock_auth, tmp_path):
        """Test --force flag overwrites existing file"""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "audio.mp3"
            output_file.write_bytes(b"existing content")

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"new content")
                return output_path

            # Set up artifacts namespace (pre-created by create_mock_client)
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "audio", str(output_file), "--force", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert output_file.read_bytes() == b"new content"

    def test_download_no_clobber_skips(self, runner, mock_auth, tmp_path):
        """Test --no-clobber flag skips existing file"""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )

            output_file = tmp_path / "audio.mp3"
            output_file.write_bytes(b"existing content")

            mock_client_cls.return_value = mock_client

            with (
                patch.object(download_module, "fetch_tokens", new_callable=AsyncMock) as mock_fetch,
                patch.object(download_module, "load_auth_from_storage") as mock_load,
            ):
                mock_load.return_value = {"SID": "test"}
                mock_fetch.return_value = ("csrf", "session")
                runner.invoke(
                    cli, ["download", "audio", str(output_file), "--no-clobber", "-n", "nb_123"]
                )

            # File should remain unchanged
            assert output_file.read_bytes() == b"existing content"


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestDownloadCommandsExist:
    def test_download_group_exists(self, runner):
        result = runner.invoke(cli, ["download", "--help"])
        assert result.exit_code == 0
        assert "audio" in result.output
        assert "video" in result.output

    def test_download_audio_command_exists(self, runner):
        result = runner.invoke(cli, ["download", "audio", "--help"])
        assert result.exit_code == 0
        assert "OUTPUT_PATH" in result.output
        assert "--notebook" in result.output or "-n" in result.output


# =============================================================================
# FLAG CONFLICT VALIDATION TESTS
# =============================================================================


class TestDownloadFlagConflicts:
    """Test that conflicting flag combinations raise appropriate errors."""

    def test_force_and_no_clobber_conflict(self, runner, mock_auth, mock_fetch_tokens):
        """Test --force and --no-clobber cannot be used together."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "audio", "--force", "--no-clobber", "-n", "nb_123"]
            )

        assert result.exit_code != 0
        assert "Cannot specify both --force and --no-clobber" in result.output

    def test_latest_and_earliest_conflict(self, runner, mock_auth, mock_fetch_tokens):
        """Test --latest and --earliest cannot be used together."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "audio", "--latest", "--earliest", "-n", "nb_123"]
            )

        assert result.exit_code != 0
        assert "Cannot specify both --latest and --earliest" in result.output

    def test_all_and_artifact_conflict(self, runner, mock_auth, mock_fetch_tokens):
        """Test --all and --artifact cannot be used together."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", "--all", "--artifact", "art_123", "-n", "nb_123"],
            )

        assert result.exit_code != 0
        assert "Cannot specify both --all and --artifact" in result.output


# =============================================================================
# AUTO-RENAME TESTS
# =============================================================================


class TestDownloadAutoRename:
    """Test auto-rename functionality when file exists and --force not specified."""

    def test_auto_renames_on_conflict(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """When file exists without --force or --no-clobber, should auto-rename."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "audio.mp3"
            output_file.write_bytes(b"existing content")

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"new content")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["download", "audio", str(output_file), "-n", "nb_123"])

        assert result.exit_code == 0
        # Original file unchanged
        assert output_file.read_bytes() == b"existing content"
        # New file created with (2) suffix
        renamed_file = tmp_path / "audio (2).mp3"
        assert renamed_file.exists()
        assert renamed_file.read_bytes() == b"new content"


# =============================================================================
# DOWNLOAD ALL TESTS
# =============================================================================


class TestDownloadAll:
    """Test --all flag for batch downloading."""

    def test_download_all_basic(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Test basic --all download to a directory."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_dir = tmp_path / "downloads"

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"audio content")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_1", "First Audio", 1),
                    make_artifact("audio_2", "Second Audio", 1),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "audio", "--all", str(output_dir), "-n", "nb_123"]
            )

        assert result.exit_code == 0
        assert output_dir.exists()
        # Check that files were downloaded
        downloaded_files = list(output_dir.glob("*.mp3"))
        assert len(downloaded_files) == 2

    def test_download_all_dry_run(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Test --all --dry-run shows preview without downloading."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_dir = tmp_path / "downloads"

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_1", "First Audio", 1),
                    make_artifact("audio_2", "Second Audio", 1),
                ]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", "--all", "--dry-run", str(output_dir), "-n", "nb_123"],
            )

        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert "2" in result.output  # Count of artifacts
        # Directory should NOT be created
        assert not output_dir.exists()

    def test_download_all_with_failures(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Test --all continues on individual artifact failures."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_dir = tmp_path / "downloads"
            call_count = 0

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("Network error")
                Path(output_path).write_bytes(b"audio content")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_1", "First Audio", 1),
                    make_artifact("audio_2", "Second Audio", 1),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "audio", "--all", str(output_dir), "-n", "nb_123"]
            )

        # Should still succeed overall (partial download)
        assert result.exit_code == 0
        # One file should be downloaded
        downloaded_files = list(output_dir.glob("*.mp3"))
        assert len(downloaded_files) == 1
        # Output should mention failure
        assert "failed" in result.output.lower() or "1" in result.output

    def test_download_all_with_no_clobber(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Test --all --no-clobber skips existing files."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_dir = tmp_path / "downloads"
            output_dir.mkdir(parents=True)
            # Create existing file
            (output_dir / "First Audio.mp3").write_bytes(b"existing")

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"new content")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_1", "First Audio", 1),
                    make_artifact("audio_2", "Second Audio", 1),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", "--all", "--no-clobber", str(output_dir), "-n", "nb_123"],
            )

        assert result.exit_code == 0
        # First file should remain unchanged
        assert (output_dir / "First Audio.mp3").read_bytes() == b"existing"
        # Second file should be downloaded
        assert (output_dir / "Second Audio.mp3").exists()


# =============================================================================
# DOWNLOAD ERROR HANDLING TESTS
# =============================================================================


class TestDownloadErrorHandling:
    """Test error handling during downloads."""

    def test_download_single_failure(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """When download fails, should return error gracefully."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "audio.mp3"

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                raise Exception("Connection refused")

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "Audio", 1)]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["download", "audio", str(output_file), "-n", "nb_123"])

        assert result.exit_code != 0
        assert "Connection refused" in result.output or "error" in result.output.lower()

    def test_download_name_not_found(self, runner, mock_auth, mock_fetch_tokens):
        """When --name matches no artifacts, should show helpful error."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_123", "My Audio", 1)]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "audio", "--name", "nonexistent", "-n", "nb_123"]
            )

        assert result.exit_code != 0
        # Should mention no match found or available artifacts
        assert "No artifact" in result.output or "nonexistent" in result.output.lower()
