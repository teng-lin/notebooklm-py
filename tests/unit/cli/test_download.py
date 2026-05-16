"""Tests for download CLI commands."""

import json
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
    from notebooklm.auth import AuthTokens

    auth = AuthTokens(
        cookies={
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        },
        csrf_token="csrf",
        session_id="session",
    )

    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
        patch("notebooklm.auth.AuthTokens.from_storage", new_callable=AsyncMock) as mock_from,
    ):
        mock_load.return_value = auth.flat_cookies
        mock_from.return_value = auth
        yield mock_load


@pytest.fixture
def mock_fetch_tokens():
    """Mock fetch_tokens and load_auth_from_storage at download module level.

    Download.py imports these functions directly, so we must patch at the module
    level where they're imported (not at helpers where they're defined).
    """
    with (
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as mock_fetch,
    ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                runner.invoke(
                    cli, ["download", "audio", str(output_file), "--no-clobber", "-n", "nb_123"]
                )

            # File should remain unchanged
            assert output_file.read_bytes() == b"existing content"


# =============================================================================
# JSON OUTPUT UNICODE TESTS
# =============================================================================


class TestDownloadJsonOutputUnicode:
    def test_download_json_output_preserves_unicode(self, runner, mock_auth):
        """`download <type> --json` should emit CJK / emoji as real UTF-8, not \\uXXXX."""
        fake_result = {
            "artifact_id": "audio_123",
            "title": "中文音频 🎧",
            "output_path": "音频.mp3",
        }

        async def fake_download_generic(*args, **kwargs):
            return fake_result

        with (
            patch.object(download_module, "_download_artifacts_generic", fake_download_generic),
            patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["title"] == "中文音频 🎧"
        assert data["output_path"] == "音频.mp3"
        # Raw output must contain real CJK/emoji, not escaped sequences.
        assert "中文音频" in result.output
        assert "🎧" in result.output
        assert "\\u" not in result.output


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

    def test_download_cinematic_video_alias_exists(self, runner):
        """Verify 'download cinematic-video' alias is registered and shows help."""
        result = runner.invoke(cli, ["download", "cinematic-video", "--help"])
        assert result.exit_code == 0
        assert "cinematic" in result.output.lower()

    def test_download_cinematic_video_alias_callable(self, runner, mock_auth, tmp_path):
        """Verify 'download cinematic-video' alias invokes download video logic."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            output_file = tmp_path / "cinematic.mp4"

            async def mock_download_video(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake cinematic content")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("cin_1", "My Cinematic Video", 3)]
            )
            mock_client.artifacts.download_video = mock_download_video
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["download", "cinematic-video", str(output_file), "-n", "nb_123"],
                )

            assert result.exit_code == 0, result.output
            assert output_file.exists()


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


class TestDownloadArtifactFlag:
    """Test -a / --artifact flag for selecting by artifact ID."""

    def test_download_by_full_artifact_id(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Full artifact ID (20+ chars) bypasses prefix search and selects correctly."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "audio.mp3"
            downloaded_ids = []

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                downloaded_ids.append(artifact_id)
                Path(output_path).write_bytes(b"audio")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_aaa111bbb222ccc333", "First", 1),
                    make_artifact("audio_bbb222ccc333ddd444", "Second", 1),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "download",
                    "audio",
                    str(output_file),
                    "-a",
                    "audio_bbb222ccc333ddd444",
                    "-n",
                    "nb_123",
                ],
            )

        assert result.exit_code == 0
        assert downloaded_ids == ["audio_bbb222ccc333ddd444"]

    def test_download_full_id_not_in_list_errors(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """Full-length ID (20+ chars) that doesn't exist in the artifact list should error."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_aaa111bbb222ccc333", "Only", 1)]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", "-a", "nonexistentidtwentyplus1", "-n", "nb_123"],
            )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_download_by_partial_artifact_id(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Partial artifact ID prefix resolves and selects the correct artifact."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "audio.mp3"
            downloaded_ids = []

            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                downloaded_ids.append(artifact_id)
                Path(output_path).write_bytes(b"audio")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_aaa111", "First", 1),
                    make_artifact("audio_bbb222", "Second", 1),
                ]
            )
            mock_client.artifacts.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", str(output_file), "-a", "audio_bbb", "-n", "nb_123"],
            )

        assert result.exit_code == 0
        assert downloaded_ids == ["audio_bbb222"]

    def test_download_ambiguous_partial_id_errors(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """Partial ID matching multiple artifacts produces an error."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_artifact("audio_aaa111", "First", 1),
                    make_artifact("audio_aaa222", "Second", 1),
                ]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", "-a", "audio_aaa", "-n", "nb_123"],
            )

        assert result.exit_code != 0
        assert "Ambiguous" in result.output

    def test_download_partial_id_not_found_errors(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """Partial ID matching nothing produces an error."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_aaa111", "First", 1)]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "audio", "-a", "zzz", "-n", "nb_123"],
            )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()


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


# =============================================================================
# DOWNLOAD QUIZ + FLASHCARDS STANDARD-FLAG TESTS (P2.T4)
# =============================================================================


def make_quiz_artifact(
    id: str, title: str, status: int = 3, created_at: datetime | None = None
) -> Artifact:
    """Quiz artifact factory: type=4 + variant=2 → ArtifactType.QUIZ."""
    return Artifact(
        id=id,
        title=title,
        _artifact_type=4,
        status=status,
        created_at=created_at or datetime.fromtimestamp(1234567890),
        _variant=2,
    )


def make_flashcard_artifact(
    id: str, title: str, status: int = 3, created_at: datetime | None = None
) -> Artifact:
    """Flashcard artifact factory: type=4 + variant=1 → ArtifactType.FLASHCARDS."""
    return Artifact(
        id=id,
        title=title,
        _artifact_type=4,
        status=status,
        created_at=created_at or datetime.fromtimestamp(1234567890),
        _variant=1,
    )


class TestDownloadQuizStandardFlags:
    """Smoke tests for the standard flag set on `download quiz` (P2.T4)."""

    def test_quiz_help_lists_full_flag_set(self, runner):
        """Each flag from the standard download flag set must appear in --help."""
        result = runner.invoke(cli, ["download", "quiz", "--help"])
        assert result.exit_code == 0
        for flag in (
            "--latest",
            "--earliest",
            "--all",
            "--name",
            "--artifact",
            "--json",
            "--dry-run",
            "--force",
            "--no-clobber",
            "--format",
        ):
            assert flag in result.output, f"missing flag in `download quiz --help`: {flag}"

    def test_quiz_basic_download_writes_file(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """Single-artifact download writes the file via the dispatched API call."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text('{"questions": []}')
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Chapter 1 Quiz")]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["download", "quiz", str(output_file), "-n", "nb_123"])

        assert result.exit_code == 0, result.output
        assert output_file.exists()

    def test_quiz_latest_flag_selects_newest(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """--latest picks the newest completed quiz artifact."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"
            chosen_ids: list[str | None] = []

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                chosen_ids.append(artifact_id)
                Path(output_path).write_text("{}")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_quiz_artifact(
                        "quiz_old", "Old", created_at=datetime.fromtimestamp(1000000000)
                    ),
                    make_quiz_artifact(
                        "quiz_new", "New", created_at=datetime.fromtimestamp(2000000000)
                    ),
                ]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--latest", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert chosen_ids == ["quiz_new"]

    def test_quiz_earliest_flag_selects_oldest(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--earliest picks the oldest completed quiz artifact."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"
            chosen_ids: list[str | None] = []

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                chosen_ids.append(artifact_id)
                Path(output_path).write_text("{}")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_quiz_artifact(
                        "quiz_old", "Old", created_at=datetime.fromtimestamp(1000000000)
                    ),
                    make_quiz_artifact(
                        "quiz_new", "New", created_at=datetime.fromtimestamp(2000000000)
                    ),
                ]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--earliest", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert chosen_ids == ["quiz_old"]

    def test_quiz_name_filter(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """--name picks the artifact whose title fuzzy-matches."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"
            chosen_ids: list[str | None] = []

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                chosen_ids.append(artifact_id)
                Path(output_path).write_text("{}")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_quiz_artifact("quiz_a", "Chapter 1 Basics"),
                    make_quiz_artifact("quiz_b", "Final Exam Review"),
                ]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--name", "Final", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert chosen_ids == ["quiz_b"]

    def test_quiz_dry_run_does_not_download(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """--dry-run shows preview without invoking the API."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"
            api_calls: list[str] = []

            async def fake_download_quiz(*args, **kwargs):
                api_calls.append("called")
                return ""

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Quiz One")]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--dry-run", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert api_calls == []  # No actual download
        assert not output_file.exists()

    def test_quiz_all_flag_downloads_each(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """--all batch-downloads every completed quiz to the target directory."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_dir = tmp_path / "quizzes"
            downloaded: list[str | None] = []

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                downloaded.append(artifact_id)
                Path(output_path).write_text('{"q": []}')
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_quiz_artifact("quiz_1", "First Quiz"),
                    make_quiz_artifact("quiz_2", "Second Quiz"),
                ]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", "--all", str(output_dir), "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert sorted(downloaded) == ["quiz_1", "quiz_2"]
        assert len(list(output_dir.glob("*.json"))) == 2

    def test_quiz_force_overwrites_existing_file(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--force overwrites a file that already exists at output_path."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"
            output_file.write_text("OLD")

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text("NEW")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Quiz One")]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--force", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert output_file.read_text() == "NEW"

    def test_quiz_no_clobber_skips_existing_file(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--no-clobber leaves an existing file untouched."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"
            output_file.write_text("EXISTING")

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text("OVERWROTE")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Quiz One")]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--no-clobber", "-n", "nb_123"],
            )

        # File untouched
        assert output_file.read_text() == "EXISTING"

    def test_quiz_force_and_no_clobber_conflict(self, runner, mock_auth, mock_fetch_tokens):
        """--force + --no-clobber must fail with a clear message."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Quiz")]
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", "--force", "--no-clobber", "-n", "nb_123"],
            )

        assert result.exit_code != 0
        assert "Cannot specify both --force and --no-clobber" in result.output

    def test_quiz_json_output_emits_json(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """--json emits a parseable JSON document on success."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.json"

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text("{}")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Quiz One")]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "quiz", str(output_file), "--json", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["operation"] == "download_single"
        assert data["status"] == "downloaded"
        assert data["artifact"]["id"] == "quiz_1"

    def test_quiz_format_markdown_passes_through_to_api(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--format markdown propagates output_format='markdown' to the API."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "quiz.md"
            captured: dict[str, str] = {}

            async def fake_download_quiz(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                captured["output_format"] = output_format
                Path(output_path).write_text("# Quiz")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_quiz_artifact("quiz_1", "Quiz One")]
            )
            mock_client.artifacts.download_quiz = fake_download_quiz
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "download",
                    "quiz",
                    str(output_file),
                    "--format",
                    "markdown",
                    "-n",
                    "nb_123",
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["output_format"] == "markdown"


class TestDownloadFlashcardsStandardFlags:
    """Smoke tests for the standard flag set on `download flashcards` (P2.T4)."""

    def test_flashcards_help_lists_full_flag_set(self, runner):
        """Each flag from the standard download flag set must appear in --help."""
        result = runner.invoke(cli, ["download", "flashcards", "--help"])
        assert result.exit_code == 0
        for flag in (
            "--latest",
            "--earliest",
            "--all",
            "--name",
            "--artifact",
            "--json",
            "--dry-run",
            "--force",
            "--no-clobber",
            "--format",
        ):
            assert flag in result.output, f"missing flag in `download flashcards --help`: {flag}"

    def test_flashcards_basic_download_writes_file(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """Single-artifact download writes the file via the dispatched API call."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.json"

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text('{"cards": []}')
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_flashcard_artifact("fc_1", "Vocabulary Deck")]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "flashcards", str(output_file), "-n", "nb_123"]
            )

        assert result.exit_code == 0, result.output
        assert output_file.exists()

    def test_flashcards_all_flag_downloads_each(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--all batch-downloads every completed deck to the target directory."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_dir = tmp_path / "flashcards"
            downloaded: list[str | None] = []

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                downloaded.append(artifact_id)
                Path(output_path).write_text('{"cards": []}')
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_flashcard_artifact("fc_1", "Deck A"),
                    make_flashcard_artifact("fc_2", "Deck B"),
                ]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "flashcards", "--all", str(output_dir), "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert sorted(downloaded) == ["fc_1", "fc_2"]
        assert len(list(output_dir.glob("*.json"))) == 2

    def test_flashcards_dry_run_does_not_download(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--dry-run shows preview without invoking the API."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.json"
            api_calls: list[str] = []

            async def fake_download_flashcards(*args, **kwargs):
                api_calls.append("called")
                return ""

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_flashcard_artifact("fc_1", "Deck One")]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "flashcards", str(output_file), "--dry-run", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert api_calls == []
        assert not output_file.exists()

    def test_flashcards_force_overwrites_existing_file(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--force overwrites a file that already exists at output_path."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.json"
            output_file.write_text("OLD")

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text("NEW")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_flashcard_artifact("fc_1", "Deck One")]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "flashcards", str(output_file), "--force", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        assert output_file.read_text() == "NEW"

    def test_flashcards_no_clobber_skips_existing_file(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--no-clobber leaves an existing file untouched."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.json"
            output_file.write_text("EXISTING")

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text("OVERWROTE")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_flashcard_artifact("fc_1", "Deck One")]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            runner.invoke(
                cli,
                ["download", "flashcards", str(output_file), "--no-clobber", "-n", "nb_123"],
            )

        assert output_file.read_text() == "EXISTING"

    def test_flashcards_json_output_emits_json(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--json emits a parseable JSON document on success."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.json"

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                Path(output_path).write_text("{}")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_flashcard_artifact("fc_1", "Deck One")]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["download", "flashcards", str(output_file), "--json", "-n", "nb_123"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["operation"] == "download_single"
        assert data["status"] == "downloaded"
        assert data["artifact"]["id"] == "fc_1"

    def test_flashcards_format_html_passes_through_to_api(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """--format html propagates output_format='html' to the API."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.html"
            captured: dict[str, str] = {}

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                captured["output_format"] = output_format
                Path(output_path).write_text("<html></html>")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_flashcard_artifact("fc_1", "Deck One")]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "download",
                    "flashcards",
                    str(output_file),
                    "--format",
                    "html",
                    "-n",
                    "nb_123",
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["output_format"] == "html"

    def test_flashcards_artifact_id_selects_specific(
        self, runner, mock_auth, mock_fetch_tokens, tmp_path
    ):
        """-a/--artifact selects a specific deck by ID (partial-match resolution)."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "cards.json"
            chosen_ids: list[str | None] = []

            async def fake_download_flashcards(
                notebook_id, output_path, artifact_id=None, output_format="json"
            ):
                chosen_ids.append(artifact_id)
                Path(output_path).write_text("{}")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    make_flashcard_artifact("fc_aaa111", "Deck A"),
                    make_flashcard_artifact("fc_bbb222", "Deck B"),
                ]
            )
            mock_client.artifacts.download_flashcards = fake_download_flashcards
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "download",
                    "flashcards",
                    str(output_file),
                    "-a",
                    "fc_bbb",
                    "-n",
                    "nb_123",
                ],
            )

        assert result.exit_code == 0, result.output
        assert chosen_ids == ["fc_bbb222"]


# =============================================================================
# DOWNLOAD TYPED ERROR PATH TESTS (P3.T2 / I14)
# =============================================================================
#
# These tests pin the contract that `download <type>` exception paths route
# through `cli.error_handler` (the typed handler) rather than the legacy
# `helpers.handle_error()` shim that always exits 1 with a text-only message
# regardless of `--json`. The handler is invoked from
# `_run_artifact_download` and applies to every `download` subcommand
# (audio/video/slide-deck/...); we exercise `download audio` as a
# representative because the dispatch is shared.
#
# Contract under test:
#   - --json honored on the exception path: emits a JSON envelope of shape
#     {"error": true, "code": "<TYPED_CODE>", "message": "..."} on stdout.
#   - RateLimitError surfaces `retry_after` in the JSON body and "Retry after
#     <N>s" in text mode.
#   - AuthError surfaces a re-authentication hint
#     ("Run 'notebooklm login' to re-authenticate.") in text mode.
#   - Typed exit codes from error_handler.py:64-67:
#       1 = library/user error (RateLimit, Auth, Validation, Network, ...)
#       2 = unexpected/system error (anything else)
# =============================================================================


class TestDownloadTypedErrorPath:
    """`download <type>` exception paths route through the typed handler."""

    def _list_raises(self, exc: Exception):
        """Build a mock client whose `artifacts.list` raises ``exc``.

        The exception fires at the first awaited call inside
        ``_download_artifacts_generic``, which surfaces directly to the outer
        ``_run_artifact_download`` exception handler — the exact site under
        test for I14. The single-download / --all per-artifact try/except
        blocks deliberately swallow API errors into ``{"error": ...}`` rows;
        forcing the failure on ``list`` exercises the *typed* handler path.
        """
        client = create_mock_client()
        client.artifacts.list = AsyncMock(side_effect=exc)
        return client

    # ----- RateLimitError --------------------------------------------------

    def test_rate_limit_error_text_exit_code_1(self, runner, mock_auth, mock_fetch_tokens):
        """RateLimitError surfaces as exit 1 with retry hint in text mode."""
        from notebooklm.exceptions import RateLimitError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(
                RateLimitError("Quota exceeded", retry_after=42)
            )
            result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        # Text-mode message routes through error_handler._output_error → safe_echo
        # to stderr, which CliRunner mixes into result.output.
        assert "Rate limited" in result.output
        assert "42" in result.output  # retry_after surfaced

    def test_rate_limit_error_json_includes_retry_after(self, runner, mock_auth, mock_fetch_tokens):
        """`--json` emits a typed JSON error envelope with retry_after."""
        from notebooklm.exceptions import RateLimitError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(
                RateLimitError("Quota exceeded", retry_after=42)
            )
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "RATE_LIMITED"
        assert data["retry_after"] == 42
        assert "Rate limited" in data["message"]

    def test_rate_limit_error_json_no_retry_after_omits_field(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """No retry_after on the exception → field absent from JSON."""
        from notebooklm.exceptions import RateLimitError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(RateLimitError("Quota exceeded"))
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "RATE_LIMITED"
        assert "retry_after" not in data

    # ----- AuthError -------------------------------------------------------

    def test_auth_error_text_includes_login_hint(self, runner, mock_auth, mock_fetch_tokens):
        """AuthError on the download path shows the typed re-auth hint."""
        from notebooklm.exceptions import AuthError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(AuthError("Token expired"))
            result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        assert "Authentication error" in result.output
        # error_handler.py ships this exact hint for AuthError.
        assert "notebooklm login" in result.output

    def test_auth_error_json_emits_typed_code(self, runner, mock_auth, mock_fetch_tokens):
        """`--json` emits {"error": true, "code": "AUTH_ERROR", ...} for AuthError."""
        from notebooklm.exceptions import AuthError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(AuthError("Token expired"))
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "AUTH_ERROR"
        assert "Token expired" in data["message"]

    # ----- Unexpected exceptions (typed exit code 2) ------------------------

    def test_unexpected_exception_exits_with_code_2(self, runner, mock_auth, mock_fetch_tokens):
        """Unknown exceptions exit 2 per error_handler.py:64-67 policy."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(RuntimeError("kaboom"))
            result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

        # Legacy helpers.handle_error always exited 1; the typed handler must
        # distinguish user errors (1) from system bugs (2).
        assert result.exit_code == 2, result.output
        assert "Unexpected error" in result.output

    def test_unexpected_exception_json_envelope(self, runner, mock_auth, mock_fetch_tokens):
        """`--json` emits {"code": "UNEXPECTED_ERROR"} with exit 2."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(RuntimeError("kaboom"))
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 2, result.output
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "UNEXPECTED_ERROR"
        assert "kaboom" in data["message"]

    # ----- ValidationError / NetworkError sanity (typed dispatch) ----------

    def test_validation_error_typed_envelope(self, runner, mock_auth, mock_fetch_tokens):
        """ValidationError reaches the typed handler with its own code."""
        from notebooklm.exceptions import ValidationError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(ValidationError("bad input"))
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION_ERROR"

    def test_network_error_typed_envelope(self, runner, mock_auth, mock_fetch_tokens):
        """NetworkError reaches the typed handler and surfaces its hint in text."""
        from notebooklm.exceptions import NetworkError

        with patch_client_for_module("download") as mock_client_cls:
            mock_client_cls.return_value = self._list_raises(NetworkError("DNS down"))
            text_result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

        assert text_result.exit_code == 1, text_result.output
        assert "Network error" in text_result.output
        assert "internet connection" in text_result.output  # error_handler hint

    # ----- JSON happy-path preservation (must not regress P2.T4 shape) -----

    def test_json_happy_path_shape_unchanged(self, runner, mock_auth, mock_fetch_tokens, tmp_path):
        """The JSON happy-path envelope is preserved (operation/status/...)."""
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            output_file = tmp_path / "audio.mp3"

            async def fake_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"hello")
                return output_path

            mock_client.artifacts.list = AsyncMock(
                return_value=[make_artifact("audio_happy", "Happy Audio", 1)]
            )
            mock_client.artifacts.download_audio = fake_download_audio
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["download", "audio", str(output_file), "--json", "-n", "nb_123"]
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["operation"] == "download_single"
        assert data["status"] == "downloaded"
        assert data["artifact"]["id"] == "audio_happy"
        # Make sure we did NOT add an "error" envelope on the happy path.
        assert "error" not in data
        assert "code" not in data

    def test_json_returned_error_envelope_unchanged_exit_1(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """The pre-existing "no completed artifacts" → JSON {"error": "..."} + exit 1
        path (download.py:709-710) is preserved by the typed-handler refactor.

        That branch surfaces a *returned* dict-shaped error from
        ``_download_artifacts_generic`` (not a raised exception), so it must
        NOT be re-routed through the typed handler — exit 1 with the legacy
        ``error: "<msg>"`` JSON shape is the documented behavior.
        """
        with patch_client_for_module("download") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        # Legacy returned-dict shape: free-form "error" string, no "code" envelope.
        assert "error" in data
        assert "No completed audio artifacts" in data["error"]

    # ----- Missing-storage auth bootstrap (regression guard) ---------------

    def test_missing_storage_routes_to_auth_error_exit_1(self, runner):
        """A missing ``storage_state.json`` exits 1 via ``handle_auth_error``.

        Regression guard for the integration-test failure that surfaced after
        the typed-handler swap: ``AuthTokens.from_storage`` raises
        ``FileNotFoundError`` when no auth file exists, and the typed handler
        would otherwise classify it as ``UNEXPECTED_ERROR`` (exit 2). The
        canonical ``with_client`` decorator catches this exact case and routes
        it through ``handle_auth_error`` — ``download`` must do the same so
        ``tests/integration/cli_vcr/test_downloads.py`` (which asserts
        ``exit_code in (0, 1)`` for unauth invocations) keeps passing.
        """
        from notebooklm.auth import AuthTokens

        with patch.object(AuthTokens, "from_storage", new_callable=AsyncMock) as mock_from_storage:
            mock_from_storage.side_effect = FileNotFoundError(
                "Storage file not found: /tmp/missing/storage_state.json"
            )
            result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        # Rich auth UX (from handle_auth_error) — the literal "Not logged in"
        # header plus the "notebooklm login" remediation hint.
        assert "Not logged in" in result.output
        assert "notebooklm login" in result.output

    def test_missing_storage_json_emits_auth_required_envelope(self, runner):
        """Missing storage in --json mode emits AUTH_REQUIRED envelope, exit 1."""
        from notebooklm.auth import AuthTokens

        with patch.object(AuthTokens, "from_storage", new_callable=AsyncMock) as mock_from_storage:
            mock_from_storage.side_effect = FileNotFoundError("Storage file not found")
            result = runner.invoke(cli, ["download", "audio", "--json", "-n", "nb_123"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        # `helpers.json_error_response` ships shape:
        # {"error": True, "code": "AUTH_REQUIRED", "message": "...", ...}
        assert data["error"] is True
        assert data["code"] == "AUTH_REQUIRED"
        assert "notebooklm login" in data["message"]
