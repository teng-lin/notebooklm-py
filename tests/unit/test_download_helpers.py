"""Tests for download helper functions."""

import pytest

from notebooklm.cli.download_helpers import (
    artifact_title_to_filename,
    resolve_partial_artifact_id,
    select_artifact,
)


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


class TestResolvePartialArtifactId:
    def test_full_uuid_returned_unchanged(self):
        """36-char UUID-shaped IDs should bypass prefix search and return as-is."""
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        assert len(full_uuid) == 36
        artifacts = [{"id": full_uuid, "title": "A", "created_at": 1000}]

        result = resolve_partial_artifact_id(artifacts, full_uuid)

        assert result == full_uuid

    def test_full_uuid_mixed_case_returned_unchanged(self):
        """Mixed-case 36-char UUID-shaped IDs should also fast-path."""
        full_uuid = "ABC12345-6789-4ABC-Def0-1234567890aB"
        assert len(full_uuid) == 36
        artifacts = [{"id": full_uuid, "title": "A", "created_at": 1000}]

        result = resolve_partial_artifact_id(artifacts, full_uuid)

        assert result == full_uuid

    def test_25_char_prefix_of_uuid_resolves_via_local_matching(self):
        """A 25-char prefix of a 36-char UUID must resolve via local prefix matching.

        Regression for P1.T9: the previous length-based fast-path (>= 20 chars)
        treated any 20–35 char prefix of a UUID as a full ID and returned it
        verbatim, which downstream would 404 against the backend.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        partial_25 = full_uuid[:25]
        assert len(partial_25) == 25
        artifacts = [{"id": full_uuid, "title": "A", "created_at": 1000}]

        result = resolve_partial_artifact_id(artifacts, partial_25)

        assert result == full_uuid

    def test_36_char_non_hex_string_is_not_fast_pathed(self):
        """A 36-char string containing non-hex characters does NOT fast-path.

        It must go through local prefix matching, where it will either find a
        unique match or raise an error — not be returned verbatim to the caller.
        """
        non_hex_36 = "zzz12345-6789-4zzz-zzz0-1234567890ab"
        assert len(non_hex_36) == 36
        # No artifact with this exact ID exists, so local matching should fail
        # with a "not found" error — proving the input was NOT fast-pathed.
        artifacts = [{"id": "abc12345-6789-4abc-def0-1234567890ab", "title": "A", "created_at": 1}]

        with pytest.raises(ValueError, match="not found"):
            resolve_partial_artifact_id(artifacts, non_hex_36)

    def test_36_char_all_dashes_is_not_fast_pathed(self):
        """Degenerate 36-char input (all dashes) does NOT fast-path."""
        all_dashes = "-" * 36
        assert len(all_dashes) == 36
        artifacts = [{"id": "abc12345-6789-4abc-def0-1234567890ab", "title": "A", "created_at": 1}]

        with pytest.raises(ValueError, match="not found"):
            resolve_partial_artifact_id(artifacts, all_dashes)

    def test_35_char_uuid_shaped_is_not_fast_pathed(self):
        """A 35-char string (one short of a UUID) is NOT fast-pathed."""
        short_uuid = "abc12345-6789-4abc-def0-1234567890a"
        assert len(short_uuid) == 35
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        artifacts = [{"id": full_uuid, "title": "A", "created_at": 1}]

        result = resolve_partial_artifact_id(artifacts, short_uuid)

        assert result == full_uuid

    def test_37_char_uuid_shaped_is_not_fast_pathed(self):
        """A 37-char string (one over a UUID) is NOT fast-pathed."""
        long_uuid = "abc12345-6789-4abc-def0-1234567890abc"
        assert len(long_uuid) == 37
        artifacts = [{"id": "abc12345-6789-4abc-def0-1234567890ab", "title": "A", "created_at": 1}]

        with pytest.raises(ValueError, match="not found"):
            resolve_partial_artifact_id(artifacts, long_uuid)

    def test_partial_id_resolves_to_full(self):
        """Partial prefix should resolve to the matching full ID."""
        artifacts = [
            {"id": "abc123def456", "title": "First", "created_at": 1000},
            {"id": "xyz789ghi012", "title": "Second", "created_at": 2000},
        ]

        result = resolve_partial_artifact_id(artifacts, "abc")

        assert result == "abc123def456"

    def test_partial_id_case_insensitive(self):
        """Prefix match should be case-insensitive."""
        artifacts = [{"id": "ABC123def456", "title": "First", "created_at": 1000}]

        result = resolve_partial_artifact_id(artifacts, "abc")

        assert result == "ABC123def456"

    def test_ambiguous_partial_id_raises(self):
        """Should raise ValueError when prefix matches multiple artifacts."""
        artifacts = [
            {"id": "abc111", "title": "First", "created_at": 1000},
            {"id": "abc222", "title": "Second", "created_at": 2000},
        ]

        with pytest.raises(ValueError, match="[Aa]mbiguous"):
            resolve_partial_artifact_id(artifacts, "abc")

    def test_no_match_raises(self):
        """Should raise ValueError when prefix matches nothing."""
        artifacts = [{"id": "xyz999", "title": "Only", "created_at": 1000}]

        with pytest.raises(ValueError, match="not found"):
            resolve_partial_artifact_id(artifacts, "abc")

    def test_empty_list_raises(self):
        """Should raise ValueError for any input when artifact list is empty."""
        with pytest.raises(ValueError, match="not found"):
            resolve_partial_artifact_id([], "abc")

    def test_ambiguous_error_includes_titles(self):
        """Ambiguous error message should include artifact titles to help the user."""
        artifacts = [
            {"id": "abc111", "title": "Meeting Notes", "created_at": 1000},
            {"id": "abc222", "title": "Debate Session", "created_at": 2000},
        ]

        with pytest.raises(ValueError) as exc_info:
            resolve_partial_artifact_id(artifacts, "abc")

        assert "Meeting Notes" in str(exc_info.value)
        assert "Debate Session" in str(exc_info.value)


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
