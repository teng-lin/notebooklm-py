"""Integration tests for multi-artifact download functionality.

Note: These tests focus on the core download logic rather than the CLI interface
to avoid asyncio event loop conflicts with pytest-asyncio.
"""

import pytest

from notebooklm.cli.download_helpers import artifact_title_to_filename, select_artifact


class TestArtifactSelection:
    """Tests for artifact selection logic (Filter → Count → Select)."""

    def test_filter_then_select_latest(self):
        """Should apply name filter BEFORE selecting latest."""
        artifacts = [
            {"id": "a1", "title": "Debate Round 1", "created_at": 1000},
            {"id": "a2", "title": "Meeting Notes", "created_at": 2000},
            {"id": "a3", "title": "Debate Round 3", "created_at": 3000},  # Latest "debate"
            {"id": "a4", "title": "Debate Round 2", "created_at": 2500},
            {"id": "a5", "title": "Overview", "created_at": 4000},  # Latest overall
        ]

        selected, reason = select_artifact(artifacts, latest=True, name="debate")

        # Should select a3 (latest of the 3 "debate" matches, NOT a5 which is latest overall)
        assert selected["id"] == "a3"
        assert selected["title"] == "Debate Round 3"
        assert reason == "latest of 3 artifacts"

    def test_filter_then_select_earliest(self):
        """Should apply name filter BEFORE selecting earliest."""
        artifacts = [
            {"id": "a1", "title": "Introduction", "created_at": 1000},  # Earliest overall
            {"id": "a2", "title": "Chapter 2", "created_at": 3000},
            {"id": "a3", "title": "Chapter 1", "created_at": 2000},  # Earliest "chapter"
            {"id": "a4", "title": "Chapter 3", "created_at": 4000},
            {"id": "a5", "title": "Conclusion", "created_at": 5000},
        ]

        selected, reason = select_artifact(artifacts, latest=False, earliest=True, name="chapter")

        # Should select a3 (earliest of the 3 "chapter" matches, NOT a1)
        assert selected["id"] == "a3"
        assert selected["title"] == "Chapter 1"
        assert reason == "earliest of 3 artifacts"

    def test_select_latest_without_filter(self):
        """Should select latest when no filter applied."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 3000},  # Latest
            {"id": "a3", "title": "Audio 3", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, latest=True)

        assert selected["id"] == "a2"
        assert selected["title"] == "Audio 2"
        assert reason == "latest of 3 artifacts"

    def test_select_earliest_without_filter(self):
        """Should select earliest when no filter applied."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 3000},
            {"id": "a2", "title": "Audio 2", "created_at": 1000},  # Earliest
            {"id": "a3", "title": "Audio 3", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, latest=False, earliest=True)

        assert selected["id"] == "a2"
        assert selected["title"] == "Audio 2"
        assert reason == "earliest of 3 artifacts"

    def test_select_by_artifact_id(self):
        """Should select by exact artifact ID."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 2000},
            {"id": "a3", "title": "Audio 3", "created_at": 3000},
        ]

        selected, reason = select_artifact(artifacts, artifact_id="a2")

        assert selected["id"] == "a2"
        assert selected["title"] == "Audio 2"
        assert reason == "matched by ID: a2"

    def test_artifact_id_not_found(self):
        """Should raise ValueError if artifact ID not found."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="Artifact nonexistent not found"):
            select_artifact(artifacts, artifact_id="nonexistent")

    def test_name_filter_no_matches(self):
        """Should raise ValueError if name filter matches nothing."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
            {"id": "a2", "title": "Audio 2", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="No artifacts matching 'nonexistent'"):
            select_artifact(artifacts, name="nonexistent")

    def test_name_filter_case_insensitive(self):
        """Should perform case-insensitive name matching."""
        artifacts = [
            {"id": "a1", "title": "CHAPTER ONE", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, name="chapter")

        assert selected["id"] == "a1"
        assert reason == "matched by name"

    def test_single_artifact_without_filter(self):
        """Should select single artifact with appropriate reason."""
        artifacts = [
            {"id": "a1", "title": "Only One", "created_at": 1000},
        ]

        selected, reason = select_artifact(artifacts)

        assert selected["id"] == "a1"
        assert reason == "only artifact"

    def test_single_artifact_with_name_match(self):
        """Should select single artifact matching name filter."""
        artifacts = [
            {"id": "a1", "title": "Chapter 1", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
        ]

        selected, reason = select_artifact(artifacts, name="chapter")

        assert selected["id"] == "a1"
        assert reason == "matched by name"

    def test_no_artifacts_error(self):
        """Should raise ValueError if no artifacts provided."""
        with pytest.raises(ValueError, match="No artifacts found"):
            select_artifact([])

    def test_latest_and_earliest_conflict(self):
        """Should raise ValueError if both latest and earliest specified."""
        artifacts = [
            {"id": "a1", "title": "Audio 1", "created_at": 1000},
        ]

        with pytest.raises(ValueError, match="Cannot specify both"):
            select_artifact(artifacts, latest=True, earliest=True)


class TestFilenameGeneration:
    """Tests for artifact filename generation."""

    def test_basic_filename_generation(self):
        """Should generate safe filename from title."""
        filename = artifact_title_to_filename("My Audio", ".mp3", set())

        assert filename == "My Audio.mp3"

    def test_sanitize_invalid_characters(self):
        """Should replace invalid filesystem characters."""
        filename = artifact_title_to_filename('Audio: Part 1 / "Main"', ".mp3", set())

        # Invalid chars (: / ") should be replaced with _
        assert "/" not in filename
        assert ":" not in filename
        assert '"' not in filename
        assert filename == "Audio_ Part 1 _ _Main_.mp3"

    def test_handle_duplicates(self):
        """Should add (2), (3) suffixes for duplicates."""
        existing = {"Overview.mp3"}

        filename1 = artifact_title_to_filename("Overview", ".mp3", existing)
        assert filename1 == "Overview (2).mp3"

        existing.add(filename1)
        filename2 = artifact_title_to_filename("Overview", ".mp3", existing)
        assert filename2 == "Overview (3).mp3"

    def test_empty_title_fallback(self):
        """Should use 'untitled' for empty/whitespace titles."""
        filename1 = artifact_title_to_filename("", ".mp3", set())
        assert filename1 == "untitled.mp3"

        filename2 = artifact_title_to_filename("   ", ".mp3", set())
        assert filename2 == "untitled.mp3"

    def test_strip_leading_trailing_whitespace(self):
        """Should strip leading/trailing whitespace and dots."""
        filename = artifact_title_to_filename("  Audio Title  ", ".mp3", set())
        assert filename == "Audio Title.mp3"

        filename = artifact_title_to_filename("...Title...", ".mp3", set())
        assert filename == "Title.mp3"

    def test_truncate_long_titles(self):
        """Should truncate very long titles."""
        long_title = "A" * 300
        filename = artifact_title_to_filename(long_title, ".mp3", set())

        # Should be truncated (max 240 - 7 for duplicate suffix reserve = 233)
        assert len(filename) < 250
        assert filename.endswith(".mp3")

    def test_directory_no_extension(self):
        """Should handle directory-type artifacts (no extension)."""
        filename = artifact_title_to_filename("My Presentation", "", set())
        assert filename == "My Presentation"

    def test_duplicate_tracking_across_calls(self):
        """Should track duplicates correctly across multiple calls."""
        existing = set()

        f1 = artifact_title_to_filename("Overview", ".mp3", existing)
        existing.add(f1)
        assert f1 == "Overview.mp3"

        f2 = artifact_title_to_filename("Overview", ".mp3", existing)
        existing.add(f2)
        assert f2 == "Overview (2).mp3"

        f3 = artifact_title_to_filename("Overview", ".mp3", existing)
        existing.add(f3)
        assert f3 == "Overview (3).mp3"

        # Verify all are unique
        assert len(existing) == 3


class TestIntegrationScenarios:
    """Integration scenarios combining selection and filename generation."""

    def test_download_all_with_duplicates_scenario(self):
        """Simulate downloading all artifacts with duplicate names."""
        artifacts = [
            {"id": "a1", "title": "Overview", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
            {"id": "a3", "title": "Overview", "created_at": 3000},
        ]

        existing_names = set()
        filenames = []

        for artifact in artifacts:
            filename = artifact_title_to_filename(artifact["title"], ".mp3", existing_names)
            existing_names.add(filename)
            filenames.append(filename)

        assert sorted(filenames) == ["Overview (2).mp3", "Overview (3).mp3", "Overview.mp3"]

    def test_download_all_with_name_filter_scenario(self):
        """Simulate downloading all artifacts matching a name filter."""
        artifacts = [
            {"id": "a1", "title": "Chapter 1", "created_at": 1000},
            {"id": "a2", "title": "Overview", "created_at": 2000},
            {"id": "a3", "title": "Chapter 2", "created_at": 3000},
            {"id": "a4", "title": "Summary", "created_at": 4000},
        ]

        # Filter for "chapter" artifacts
        filtered = [a for a in artifacts if "chapter" in a["title"].lower()]

        assert len(filtered) == 2
        assert filtered[0]["title"] == "Chapter 1"
        assert filtered[1]["title"] == "Chapter 2"

    def test_latest_of_filtered_artifacts_scenario(self):
        """Simulate selecting latest of filtered artifacts."""
        artifacts = [
            {"id": "a1", "title": "Debate Round 1", "created_at": 1000},
            {"id": "a2", "title": "Meeting Notes", "created_at": 2000},
            {"id": "a3", "title": "Debate Round 3", "created_at": 3000},
            {"id": "a4", "title": "Debate Round 2", "created_at": 2500},
            {"id": "a5", "title": "Overview", "created_at": 4000},
        ]

        # This is the key test: Filter → Count → Select
        selected, reason = select_artifact(artifacts, latest=True, name="debate")

        # Should get the latest of the "debate" matches (created_at=3000)
        # NOT the latest overall (created_at=4000)
        assert selected["id"] == "a3"
        assert selected["created_at"] == 3000
        assert "latest of 3 artifacts" in reason
