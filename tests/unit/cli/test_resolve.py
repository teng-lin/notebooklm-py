"""Tests for resolve_notebook_id and resolve_source_id partial ID matching."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import click
import pytest

from notebooklm.cli.resolve import resolve_notebook_id, resolve_source_id, resolve_source_ids
from notebooklm.types import Notebook, Source


@pytest.fixture
def mock_client():
    """Create a mock client with notebooks.list method."""
    client = MagicMock()
    client.notebooks = MagicMock()
    return client


@pytest.fixture
def sample_notebooks():
    """Sample notebooks for testing."""
    return [
        Notebook(
            id="abc123def456ghi789",
            title="First Notebook",
            created_at=datetime(2024, 1, 1),
            is_owner=True,
        ),
        Notebook(
            id="xyz789uvw456rst123",
            title="Second Notebook",
            created_at=datetime(2024, 1, 2),
            is_owner=False,
        ),
        Notebook(
            id="abc999zzz888yyy777",
            title="Third Notebook",
            created_at=datetime(2024, 1, 3),
            is_owner=True,
        ),
    ]


class TestResolveNotebookId:
    """Test partial notebook ID resolution."""

    @pytest.mark.asyncio
    async def test_exact_match_returns_unchanged(self, mock_client, sample_notebooks):
        """Exact full ID match returns the ID unchanged."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        result = await resolve_notebook_id(mock_client, "abc123def456ghi789")
        assert result == "abc123def456ghi789"

    @pytest.mark.asyncio
    async def test_unique_prefix_returns_full_id(self, mock_client, sample_notebooks):
        """Unique prefix returns the full matched ID."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        # "xyz" uniquely matches "xyz789uvw456rst123"
        mock_console = MagicMock()
        result = await resolve_notebook_id(mock_client, "xyz", stdout_console=mock_console)

        assert result == "xyz789uvw456rst123"
        # Should print a match message
        mock_console.print.assert_called()

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_raises_exception(self, mock_client, sample_notebooks):
        """Ambiguous prefix (matches multiple) raises ClickException."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        # "abc" matches both "abc123..." and "abc999..."
        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, "abc")

        assert "Ambiguous" in str(exc_info.value)
        assert "abc123" in str(exc_info.value)
        assert "abc999" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exact_match_wins_over_prefix_ambiguity(self, mock_client):
        """Exact short IDs win even when another item shares that prefix."""
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(
                    id="abc",
                    title="Exact Notebook",
                    created_at=datetime(2024, 1, 1),
                    is_owner=True,
                ),
                Notebook(
                    id="abc123def456ghi789",
                    title="Prefixed Notebook",
                    created_at=datetime(2024, 1, 2),
                    is_owner=True,
                ),
            ]
        )

        mock_console = MagicMock()
        result = await resolve_notebook_id(mock_client, "abc", stdout_console=mock_console)

        assert result == "abc"
        mock_console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_match_raises_exception(self, mock_client, sample_notebooks):
        """No matching prefix raises ClickException with helpful message."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, "zzz")

        assert "No notebook found" in str(exc_info.value)
        assert "notebooklm list" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_uuid_shaped_id_returns_without_listing(self, mock_client):
        """36-char UUID-shaped IDs fast-path without hitting the backend."""
        # Canonical 8-4-4-4-12 UUID layout — 36 chars, all hex + dashes.
        uuid_id = "abc12345-6789-4abc-def0-1234567890ab"
        assert len(uuid_id) == 36
        mock_client.notebooks.list = AsyncMock()

        result = await resolve_notebook_id(mock_client, uuid_id)

        assert result == uuid_id
        mock_client.notebooks.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_uuid_shaped_id_mixed_case_returns_without_listing(self, mock_client):
        """Mixed-case 36-char UUID-shaped IDs also fast-path."""
        uuid_id = "ABC12345-6789-4ABC-Def0-1234567890aB"
        assert len(uuid_id) == 36
        mock_client.notebooks.list = AsyncMock()

        result = await resolve_notebook_id(mock_client, uuid_id)

        assert result == uuid_id
        mock_client.notebooks.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_25_char_prefix_of_uuid_resolves_via_local_matching(self, mock_client):
        """A 25-char prefix of a 36-char UUID resolves locally, not via the backend.

        Regression for P1.T9: the previous length-based fast-path (>= 20 chars)
        bypassed local matching for any 20–35 char prefix of a UUID, sending the
        truncated string straight to the backend. Per the acceptance criteria,
        this path must also emit the ``Matched:`` diagnostic so users can see
        which full ID the prefix resolved to.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        partial_25 = full_uuid[:25]  # "abc12345-6789-4abc-def0-1"
        assert len(partial_25) == 25
        assert len(full_uuid) == 36
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(
                    id=full_uuid,
                    title="UUID Notebook",
                    created_at=datetime(2024, 1, 1),
                    is_owner=True,
                ),
            ]
        )
        mock_console = MagicMock()

        result = await resolve_notebook_id(mock_client, partial_25, stdout_console=mock_console)

        assert result == full_uuid
        # Local matching MUST have happened, i.e. the backend was listed.
        mock_client.notebooks.list.assert_awaited_once()
        # And the "Matched: ..." diagnostic from the acceptance criteria must fire.
        mock_console.print.assert_called_once()
        printed = mock_console.print.call_args.args[0]
        assert "Matched" in printed

    @pytest.mark.asyncio
    async def test_36_char_non_hex_string_is_not_fast_pathed(self, mock_client):
        """A 36-char string containing non-hex characters does NOT fast-path.

        Only UUID-shaped strings (hex digits + dashes, 36 chars, 8-4-4-4-12 layout)
        qualify; a 36-char string with letters outside ``[0-9a-fA-F]`` must go
        through the local prefix-matching path so a typo cannot reach the backend
        as a malformed ID.
        """
        # 36 chars, 8-4-4-4-12 dash layout, but includes 'z' (non-hex). The
        # matching notebook in the list confirms local resolution succeeded.
        non_hex_36 = "zzz12345-6789-4zzz-zzz0-1234567890ab"
        assert len(non_hex_36) == 36
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(
                    id=non_hex_36,
                    title="Non-hex 36-char ID",
                    created_at=datetime(2024, 1, 1),
                    is_owner=True,
                ),
            ]
        )

        result = await resolve_notebook_id(mock_client, non_hex_36, stdout_console=MagicMock())

        assert result == non_hex_36
        # Backend listing MUST have happened (no fast-path).
        mock_client.notebooks.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_36_char_all_dashes_is_not_fast_pathed(self, mock_client):
        """Degenerate 36-char input (all dashes) does NOT fast-path.

        The 8-4-4-4-12 layout requires hex digits in each block, so a pathological
        ``"-" * 36`` input cannot bypass local resolution — it gets routed through
        the local prefix-match path and surfaces a clear "no match" error.
        """
        all_dashes = "-" * 36
        assert len(all_dashes) == 36
        mock_client.notebooks.list = AsyncMock(return_value=[])

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, all_dashes)

        assert "No notebook found" in str(exc_info.value)
        # Backend listing MUST have happened (no fast-path).
        mock_client.notebooks.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_35_char_uuid_shaped_is_not_fast_pathed(self, mock_client):
        """A 35-char string (one short of a UUID) does NOT fast-path.

        Boundary check: the regex requires exactly 36 chars in the 8-4-4-4-12
        layout. A 35-char input must take the local list-and-match path.
        """
        # Drop the last char of a canonical UUID -> 35 chars, still hex+dash but
        # with a 11-digit final block instead of 12.
        short_uuid = "abc12345-6789-4abc-def0-1234567890a"
        assert len(short_uuid) == 35
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(
                    id=full_uuid,
                    title="UUID Notebook",
                    created_at=datetime(2024, 1, 1),
                    is_owner=True,
                ),
            ]
        )

        result = await resolve_notebook_id(mock_client, short_uuid, stdout_console=MagicMock())

        assert result == full_uuid
        mock_client.notebooks.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_37_char_uuid_shaped_is_not_fast_pathed(self, mock_client):
        """A 37-char string (one over a UUID) does NOT fast-path.

        Boundary check on the other side: any extra character past the canonical
        36 fails the regex and forces the local path. With no match in the list,
        the resolver raises a clear "no match" error.
        """
        long_uuid = "abc12345-6789-4abc-def0-1234567890abc"
        assert len(long_uuid) == 37
        mock_client.notebooks.list = AsyncMock(return_value=[])

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, long_uuid)

        assert "No notebook found" in str(exc_info.value)
        mock_client.notebooks.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_36_char_wrong_dash_placement_is_not_fast_pathed(self, mock_client):
        """36-char hex+dash with wrong dash placement does NOT fast-path.

        The tightened regex enforces the exact 8-4-4-4-12 layout, so a 36-char
        string with the right character classes but the wrong layout (e.g. dashes
        slipped one position over) must go through local resolution.
        """
        # Same 32 hex chars as a canonical UUID, but dashes shifted one position
        # (9-3-4-4-12 instead of 8-4-4-4-12). Total length still 36.
        wrong_layout = "abc123456-789-4abc-def0-1234567890ab"
        assert len(wrong_layout) == 36
        mock_client.notebooks.list = AsyncMock(return_value=[])

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, wrong_layout)

        assert "No notebook found" in str(exc_info.value)
        mock_client.notebooks.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_id_raises_exception(self, mock_client):
        """Empty string raises ClickException."""
        mock_client.notebooks.list = AsyncMock()

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, "")

        assert "cannot be empty" in str(exc_info.value)
        mock_client.notebooks.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_id_raises_exception(self, mock_client):
        """None raises ClickException."""
        mock_client.notebooks.list = AsyncMock()

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, None)

        assert "cannot be empty" in str(exc_info.value)
        mock_client.notebooks.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self, mock_client, sample_notebooks):
        """Prefix matching should be case-insensitive."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        # "XYZ" should match "xyz789..." (case-insensitive)
        result = await resolve_notebook_id(mock_client, "XYZ", stdout_console=MagicMock())

        assert result == "xyz789uvw456rst123"

    @pytest.mark.asyncio
    async def test_exact_short_id_no_message(self, mock_client, sample_notebooks):
        """Exact match with a non-UUID ID returns without printing a match message."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        # Create a notebook with a short ID that we'll match exactly
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(
                    id="shortid",
                    title="Short ID Notebook",
                    created_at=datetime(2024, 1, 1),
                    is_owner=True,
                ),
            ]
        )

        mock_console = MagicMock()
        result = await resolve_notebook_id(mock_client, "shortid", stdout_console=mock_console)

        assert result == "shortid"
        # Should NOT print match message since it's an exact match
        mock_console.print.assert_not_called()


class TestResolveNotebookIdAmbiguityDisplay:
    """Test the display format of ambiguous match errors."""

    @pytest.mark.asyncio
    async def test_shows_up_to_five_matches(self, mock_client):
        """Ambiguous error shows up to 5 matching notebooks."""
        notebooks = [
            Notebook(
                id=f"abc{i}00000000000000",
                title=f"Notebook {i}",
                created_at=datetime(2024, 1, i + 1),
                is_owner=True,
            )
            for i in range(7)
        ]
        mock_client.notebooks.list = AsyncMock(return_value=notebooks)

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, "abc")

        error_msg = str(exc_info.value)
        assert "matches 7 notebooks" in error_msg
        assert "... and 2 more" in error_msg

    @pytest.mark.asyncio
    async def test_shows_notebook_titles_in_ambiguous_error(self, mock_client, sample_notebooks):
        """Ambiguous error includes notebook titles."""
        mock_client.notebooks.list = AsyncMock(return_value=sample_notebooks)

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_notebook_id(mock_client, "abc")

        error_msg = str(exc_info.value)
        assert "First Notebook" in error_msg
        assert "Third Notebook" in error_msg


# =============================================================================
# Tests for resolve_source_id
# =============================================================================


@pytest.fixture
def mock_client_with_sources():
    """Create a mock client with sources.list method."""
    client = MagicMock()
    client.sources = MagicMock()
    return client


@pytest.fixture
def sample_sources():
    """Sample sources for testing."""
    return [
        Source(id="src123def456ghi789", title="First Source"),
        Source(id="xyz789uvw456rst123", title="Second Source"),
        Source(id="src999zzz888yyy777", title="Third Source"),
    ]


class TestResolveSourceId:
    """Test partial source ID resolution."""

    @pytest.mark.asyncio
    async def test_exact_match_returns_unchanged(self, mock_client_with_sources, sample_sources):
        """Exact full ID match returns the ID unchanged."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        result = await resolve_source_id(mock_client_with_sources, "nb_123", "src123def456ghi789")
        assert result == "src123def456ghi789"

    @pytest.mark.asyncio
    async def test_unique_prefix_returns_full_id(self, mock_client_with_sources, sample_sources):
        """Unique prefix returns the full matched ID."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        # "xyz" uniquely matches "xyz789uvw456rst123"
        mock_console = MagicMock()
        result = await resolve_source_id(
            mock_client_with_sources,
            "nb_123",
            "xyz",
            stdout_console=mock_console,
        )

        assert result == "xyz789uvw456rst123"
        # Should print a match message
        mock_console.print.assert_called()

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_raises_exception(
        self, mock_client_with_sources, sample_sources
    ):
        """Ambiguous prefix (matches multiple) raises ClickException."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        # "src" matches both "src123..." and "src999..."
        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_id(mock_client_with_sources, "nb_123", "src")

        assert "Ambiguous" in str(exc_info.value)
        assert "src123" in str(exc_info.value)
        assert "src999" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exact_match_wins_over_prefix_ambiguity(self, mock_client_with_sources):
        """Exact short source IDs win even when another source shares that prefix."""
        mock_client_with_sources.sources.list = AsyncMock(
            return_value=[
                Source(id="src", title="Exact Source"),
                Source(id="src123def456ghi789", title="Prefixed Source"),
            ]
        )

        mock_console = MagicMock()
        result = await resolve_source_id(
            mock_client_with_sources,
            "nb_123",
            "src",
            stdout_console=mock_console,
        )

        assert result == "src"
        mock_console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_match_raises_exception(self, mock_client_with_sources, sample_sources):
        """No matching prefix raises ClickException with helpful message."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_id(mock_client_with_sources, "nb_123", "zzz")

        assert "No source found" in str(exc_info.value)
        assert "notebooklm source list" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_uuid_shaped_id_returns_without_listing(self, mock_client_with_sources):
        """36-char UUID-shaped IDs fast-path without hitting the backend."""
        uuid_id = "abc12345-6789-4abc-def0-1234567890ab"
        assert len(uuid_id) == 36
        mock_client_with_sources.sources.list = AsyncMock()

        result = await resolve_source_id(mock_client_with_sources, "nb_123", uuid_id)

        assert result == uuid_id
        mock_client_with_sources.sources.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_25_char_prefix_of_uuid_resolves_via_local_matching(
        self, mock_client_with_sources
    ):
        """A 25-char prefix of a 36-char UUID resolves locally for sources too."""
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        partial_25 = full_uuid[:25]
        assert len(partial_25) == 25
        mock_client_with_sources.sources.list = AsyncMock(
            return_value=[Source(id=full_uuid, title="UUID Source")]
        )

        result = await resolve_source_id(
            mock_client_with_sources,
            "nb_123",
            partial_25,
            stdout_console=MagicMock(),
        )

        assert result == full_uuid
        mock_client_with_sources.sources.list.assert_awaited_once_with("nb_123")

    @pytest.mark.asyncio
    async def test_empty_id_raises_exception(self, mock_client_with_sources):
        """Empty string raises ClickException."""
        mock_client_with_sources.sources.list = AsyncMock()

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_id(mock_client_with_sources, "nb_123", "")

        assert "cannot be empty" in str(exc_info.value)
        mock_client_with_sources.sources.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_id_raises_exception(self, mock_client_with_sources):
        """None raises ClickException."""
        mock_client_with_sources.sources.list = AsyncMock()

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_id(mock_client_with_sources, "nb_123", None)

        assert "cannot be empty" in str(exc_info.value)
        mock_client_with_sources.sources.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self, mock_client_with_sources, sample_sources):
        """Prefix matching should be case-insensitive."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        # "XYZ" should match "xyz789..." (case-insensitive)
        result = await resolve_source_id(
            mock_client_with_sources,
            "nb_123",
            "XYZ",
            stdout_console=MagicMock(),
        )

        assert result == "xyz789uvw456rst123"

    @pytest.mark.asyncio
    async def test_passes_notebook_id_to_list(self, mock_client_with_sources, sample_sources):
        """Should pass the notebook ID to sources.list."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        await resolve_source_id(
            mock_client_with_sources,
            "my_notebook_id",
            "xyz",
            stdout_console=MagicMock(),
        )

        mock_client_with_sources.sources.list.assert_called_once_with("my_notebook_id")


class TestResolveSourceIdAmbiguityDisplay:
    """Test the display format of ambiguous match errors."""

    @pytest.mark.asyncio
    async def test_shows_up_to_five_matches(self, mock_client_with_sources):
        """Ambiguous error shows up to 5 matching sources."""
        sources = [Source(id=f"src{i}00000000000000", title=f"Source {i}") for i in range(7)]
        mock_client_with_sources.sources.list = AsyncMock(return_value=sources)

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_id(mock_client_with_sources, "nb_123", "src")

        error_msg = str(exc_info.value)
        assert "matches 7 sources" in error_msg
        assert "... and 2 more" in error_msg

    @pytest.mark.asyncio
    async def test_shows_source_titles_in_ambiguous_error(
        self, mock_client_with_sources, sample_sources
    ):
        """Ambiguous error includes source titles."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_id(mock_client_with_sources, "nb_123", "src")

        error_msg = str(exc_info.value)
        assert "First Source" in error_msg
        assert "Third Source" in error_msg


class TestResolveSourceIds:
    """Test multiple source ID resolution."""

    @pytest.mark.asyncio
    async def test_reuses_source_list_for_multiple_partial_ids(
        self, mock_client_with_sources, sample_sources
    ):
        """Multiple partial IDs share one sources.list call."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        result = await resolve_source_ids(
            mock_client_with_sources,
            "nb_123",
            ("xyz", "src999"),
            stdout_console=MagicMock(),
        )

        assert result == ["xyz789uvw456rst123", "src999zzz888yyy777"]
        mock_client_with_sources.sources.list.assert_awaited_once_with("nb_123")

    @pytest.mark.asyncio
    async def test_full_ids_skip_source_list(self, mock_client_with_sources):
        """Full UUID-shaped source IDs pass through without a source list call."""
        mock_client_with_sources.sources.list = AsyncMock()

        uuid_a = "abc12345-6789-4abc-def0-1234567890ab"
        uuid_b = "fedcba98-7654-4321-0fed-cba987654321"
        result = await resolve_source_ids(
            mock_client_with_sources,
            "nb_123",
            (uuid_a, uuid_b),
        )

        assert result == [uuid_a, uuid_b]
        mock_client_with_sources.sources.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_full_and_partial_ids_list_once(
        self, mock_client_with_sources, sample_sources
    ):
        """Full and partial IDs share one source list call."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)

        result = await resolve_source_ids(
            mock_client_with_sources,
            "nb_123",
            ("src123def456ghi789", "xyz"),
            stdout_console=MagicMock(),
        )

        assert result == ["src123def456ghi789", "xyz789uvw456rst123"]
        mock_client_with_sources.sources.list.assert_awaited_once_with("nb_123")

    @pytest.mark.asyncio
    async def test_duplicate_partial_ids_resolve_once_preserving_duplicates(
        self, mock_client_with_sources, sample_sources
    ):
        """Duplicate partial IDs produce one status message but preserve output shape."""
        mock_client_with_sources.sources.list = AsyncMock(return_value=sample_sources)
        mock_console = MagicMock()

        result = await resolve_source_ids(
            mock_client_with_sources,
            "nb_123",
            ("xyz", "xyz"),
            stdout_console=mock_console,
        )

        assert result == ["xyz789uvw456rst123", "xyz789uvw456rst123"]
        mock_client_with_sources.sources.list.assert_awaited_once_with("nb_123")
        mock_console.print.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_source_id_raises_before_listing(self, mock_client_with_sources):
        """Invalid multi-source input does not trigger a source-list RPC."""
        mock_client_with_sources.sources.list = AsyncMock()

        with pytest.raises(click.ClickException) as exc_info:
            await resolve_source_ids(mock_client_with_sources, "nb_123", ("xyz", ""))

        assert "cannot be empty" in str(exc_info.value)
        mock_client_with_sources.sources.list.assert_not_called()
