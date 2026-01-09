"""Tests for download helper functions."""

import pytest

from notebooklm.cli.download_helpers import artifact_title_to_filename, select_artifact


class TestSelectArtifact:
    def test_select_single_artifact(self):
        """Should return the only artifact without applying filters."""
        artifacts = [{"id": "a1", "title": "Meeting", "created_at": 1000}]

        result, reason = select_artifact(artifacts)

        assert result == artifacts[0]
        assert "only artifact" in reason.lower()

    def test_filter_with_name_no_matches(self):
        """Should error when --name filter matches nothing."""
        artifacts = [{"id": "a1", "title": "Meeting", "created_at": 1000}]

        with pytest.raises(ValueError) as exc_info:
            select_artifact(artifacts, name="music")

        error_msg = str(exc_info.value)
        assert "No artifacts matching 'music'" in error_msg
        assert "Available:" in error_msg  # Verify it shows available options
        assert "Meeting" in error_msg

    def test_filter_with_name_single_match(self):
        """Should return artifact when --name filter matches one."""
        artifacts = [
            {"id": "a1", "title": "Meeting Notes", "created_at": 1000},
            {"id": "a2", "title": "Debate Session", "created_at": 2000},
        ]

        result, reason = select_artifact(artifacts, name="debate")

        assert result["id"] == "a2"
        assert "matched by name" in reason.lower()

    def test_filter_then_select_latest(self):
        """Should apply filter THEN select latest from matches."""
        artifacts = [
            {"id": "a1", "title": "Debate Round 1", "created_at": 1000},
            {"id": "a2", "title": "Meeting", "created_at": 2000},
            {"id": "a3", "title": "Debate Round 2", "created_at": 3000},
            {"id": "a4", "title": "Debate Round 3", "created_at": 2500},
        ]

        # Should find 3 "Debate" artifacts, return latest (a3)
        result, reason = select_artifact(artifacts, name="debate", latest=True)

        assert result["id"] == "a3"
        assert result["created_at"] == 3000

    def test_select_latest_from_multiple(self):
        """Should select latest when multiple artifacts exist."""
        artifacts = [
            {"id": "a1", "title": "Overview 1", "created_at": 1000},
            {"id": "a2", "title": "Overview 2", "created_at": 3000},
            {"id": "a3", "title": "Overview 3", "created_at": 2000},
        ]

        result, reason = select_artifact(artifacts, latest=True)

        assert result["id"] == "a2"
        assert "latest" in reason.lower()

    def test_select_earliest_from_multiple(self):
        """Should select earliest when requested."""
        artifacts = [
            {"id": "a1", "title": "Overview 1", "created_at": 1000},
            {"id": "a2", "title": "Overview 2", "created_at": 3000},
        ]

        # Must set latest=False when using earliest=True
        result, reason = select_artifact(artifacts, latest=False, earliest=True)

        assert result["id"] == "a1"
        assert "earliest" in reason.lower()

    def test_select_by_artifact_id(self):
        """Should select exact artifact by ID."""
        artifacts = [
            {"id": "a1", "title": "First", "created_at": 1000},
            {"id": "a2", "title": "Second", "created_at": 2000},
        ]

        result, reason = select_artifact(artifacts, artifact_id="a2")

        assert result["id"] == "a2"

    def test_artifact_id_not_found(self):
        """Should error when artifact ID doesn't exist."""
        artifacts = [{"id": "a1", "title": "Test", "created_at": 1000}]

        with pytest.raises(ValueError, match="Artifact.*not found"):
            select_artifact(artifacts, artifact_id="a99")

    def test_empty_artifacts_list(self):
        """Should error with helpful message when no artifacts."""
        with pytest.raises(ValueError, match="No artifacts found"):
            select_artifact([])

    def test_default_selects_latest(self):
        """Should select latest by default when no flags provided."""
        artifacts = [
            {"id": "a1", "title": "Overview 1", "created_at": 1000},
            {"id": "a2", "title": "Overview 2", "created_at": 3000},
            {"id": "a3", "title": "Overview 3", "created_at": 2000},
        ]

        # Don't pass latest=True explicitly - test the default
        result, reason = select_artifact(artifacts)

        assert result["id"] == "a2"  # Should be latest (highest created_at)
        assert "latest" in reason.lower()

    def test_both_latest_and_earliest_raises_error(self):
        """Should error when both --latest and --earliest are specified."""
        artifacts = [
            {"id": "a1", "title": "First", "created_at": 1000},
            {"id": "a2", "title": "Second", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="Cannot specify both"):
            select_artifact(artifacts, latest=True, earliest=True)


class TestArtifactTitleToFilename:
    def test_simple_title(self):
        """Should handle simple ASCII title."""
        result = artifact_title_to_filename("Deep Dive Overview", ".mp3", set())
        assert result == "Deep Dive Overview.mp3"

    def test_sanitize_special_characters(self):
        """Should remove invalid filename characters."""
        result = artifact_title_to_filename("My/Awesome\\Talk: Part 1?", ".mp3", set())
        assert result == "My_Awesome_Talk_ Part 1_.mp3"

    def test_handle_duplicate_titles(self):
        """Should append (2), (3) for duplicate titles."""
        existing = {"Overview.mp3"}

        result = artifact_title_to_filename("Overview", ".mp3", existing)
        assert result == "Overview (2).mp3"

        existing.add("Overview (2).mp3")
        result = artifact_title_to_filename("Overview", ".mp3", existing)
        assert result == "Overview (3).mp3"

    def test_handle_existing_with_number(self):
        """Should handle titles that already have (N) pattern."""
        existing = {"Report (1).pdf"}

        result = artifact_title_to_filename("Report (1)", ".pdf", existing)
        assert result == "Report (1) (2).pdf"

    def test_long_filename_truncation(self):
        """Should truncate very long filenames."""
        long_title = "A" * 300
        result = artifact_title_to_filename(long_title, ".mp3", set())

        # Most filesystems support 255 bytes max
        assert len(result) <= 255
        assert result.endswith(".mp3")

    def test_empty_title_after_sanitization(self):
        """Should handle titles that become empty after sanitization."""
        result = artifact_title_to_filename("...", ".mp3", set())
        assert result == "untitled.mp3"

        result = artifact_title_to_filename("   ", ".pdf", set())
        assert result == "untitled.pdf"

        result = artifact_title_to_filename(".", ".txt", set())
        assert result == "untitled.txt"

    def test_duplicate_with_long_truncated_title(self):
        """Should handle duplicates even when base is at max length."""
        long_title = "A" * 240
        existing = {f"{'A' * 233}.mp3"}

        result = artifact_title_to_filename(long_title, ".mp3", existing)

        # Should not exceed filesystem limits
        assert len(result) <= 255
        assert result.endswith(" (2).mp3")
