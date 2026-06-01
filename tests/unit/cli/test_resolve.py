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
        # Canonical 8-4-4-4-12 UUID layout - 36 chars, all hex + dashes.
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
        bypassed local matching for any 20-35 char prefix of a UUID, sending the
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
        ``"-" * 36`` input cannot bypass local resolution - it gets routed through
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


# ----------------------------------------------------------------------------
# Sync resolver core + entity-specific config (P2.T1 acceptance criteria)
# ----------------------------------------------------------------------------


class TestRequireNotebookEnvVarFallback:
    """Pin the env-var rung of ``require_notebook``'s precedence ladder.

    The full ladder is: ``-n`` flag > ``NOTEBOOKLM_NOTEBOOK`` env >
    persisted-context > error. ``test_helpers.py`` covers the same paths
    via the helpers facade; this class covers them via the canonical
    ``cli.resolve.require_notebook`` directly so any drift between the two
    surfaces is caught here.
    """

    def test_argument_wins_over_env_and_context(self, tmp_path, monkeypatch):
        from notebooklm.cli.resolve import require_notebook

        ctx_file = tmp_path / "context.json"
        ctx_file.write_text('{"notebook_id": "nb_from_context"}')
        monkeypatch.setenv("NOTEBOOKLM_NOTEBOOK", "nb_from_env")

        assert (
            require_notebook("nb_from_arg", context_path_fn=lambda **_: ctx_file) == "nb_from_arg"
        )

    def test_env_var_fills_in_when_no_arg(self, tmp_path, monkeypatch):
        """When ``-n`` is None and there is no context file, env wins."""
        from notebooklm.cli.resolve import require_notebook

        monkeypatch.setenv("NOTEBOOKLM_NOTEBOOK", "nb_from_env")
        assert (
            require_notebook(
                None,
                context_path_fn=lambda **_: tmp_path / "nonexistent.json",
            )
            == "nb_from_env"
        )

    def test_env_var_wins_over_context_file(self, tmp_path, monkeypatch):
        """Env-var takes precedence over the persisted active-notebook."""
        from notebooklm.cli.resolve import require_notebook

        ctx_file = tmp_path / "context.json"
        ctx_file.write_text('{"notebook_id": "nb_from_context"}')
        monkeypatch.setenv("NOTEBOOKLM_NOTEBOOK", "nb_from_env")
        assert require_notebook(None, context_path_fn=lambda **_: ctx_file) == "nb_from_env"

    def test_context_file_used_when_no_arg_and_no_env(self, tmp_path, monkeypatch):
        """No arg + no env-var -> fall through to the active-context file."""
        from notebooklm.cli.resolve import require_notebook

        monkeypatch.delenv("NOTEBOOKLM_NOTEBOOK", raising=False)
        ctx_file = tmp_path / "context.json"
        ctx_file.write_text('{"notebook_id": "nb_from_context"}')
        assert require_notebook(None, context_path_fn=lambda **_: ctx_file) == "nb_from_context"

    def test_blank_env_var_falls_through_to_context(self, tmp_path, monkeypatch):
        """Whitespace-only ``NOTEBOOKLM_NOTEBOOK`` is treated as unset."""
        from notebooklm.cli.resolve import require_notebook

        monkeypatch.setenv("NOTEBOOKLM_NOTEBOOK", "   ")
        ctx_file = tmp_path / "context.json"
        ctx_file.write_text('{"notebook_id": "nb_from_context"}')
        assert require_notebook(None, context_path_fn=lambda **_: ctx_file) == "nb_from_context"

    def test_env_var_is_stripped(self, tmp_path, monkeypatch):
        """``NOTEBOOKLM_NOTEBOOK`` value is trimmed before being returned."""
        from notebooklm.cli.resolve import require_notebook

        monkeypatch.setenv("NOTEBOOKLM_NOTEBOOK", "  nb_padded  ")
        assert (
            require_notebook(
                None,
                context_path_fn=lambda **_: tmp_path / "nonexistent.json",
            )
            == "nb_padded"
        )

    def test_no_source_anywhere_raises_with_discoverability_hint(self, tmp_path, monkeypatch):
        """All three resolution paths must be named in the error message."""
        from notebooklm.cli.resolve import require_notebook

        monkeypatch.delenv("NOTEBOOKLM_NOTEBOOK", raising=False)
        mock_console = MagicMock()
        with pytest.raises(SystemExit):
            require_notebook(
                None,
                context_path_fn=lambda **_: tmp_path / "nonexistent.json",
                output_console=mock_console,
            )

        mock_console.print.assert_called_once()
        printed = mock_console.print.call_args[0][0]
        assert "-n/--notebook" in printed
        assert "NOTEBOOKLM_NOTEBOOK" in printed
        assert "notebooklm use" in printed


class TestResolvePartialIdInItems:
    """Direct tests for the sync resolver core that powers both the async
    ``_resolve_partial_id`` and the download-pre-fetched-list path.

    These cover the consolidation contract: identical matching rules
    regardless of accessor shape and error factory.
    """

    def test_full_uuid_fast_path(self):
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        full = "abc12345-6789-4abc-def0-1234567890ab"
        # An empty items list MUST not be visited at all; the fast-path
        # returns before any iteration.
        result = resolve_partial_id_in_items(
            full,
            [],
            entity_name="artifact",
            list_command="artifact list",
        )
        assert result == full

    def test_full_uuid_no_passthrough_when_disabled(self):
        """``allow_full_id_passthrough=False`` forces membership validation.

        Callers that already hold the authoritative item list (download
        helpers) opt out of the fast-path so a valid-shape UUID that isn't
        in the list surfaces the canonical "not found" error instead of
        being passed through and yielding a silent backend 404.
        """
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        full = "abc12345-6789-4abc-def0-1234567890ab"
        with pytest.raises(click.ClickException) as exc_info:
            resolve_partial_id_in_items(
                full,
                [],
                entity_name="artifact",
                list_command="artifact list",
                allow_full_id_passthrough=False,
            )
        assert "No artifact found starting with" in str(exc_info.value.message)

    def test_partial_unique_match_with_attr_accessor(self):
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        class Item:
            def __init__(self, id_, title):
                self.id = id_
                self.title = title

        items = [Item("aaa111", "A"), Item("bbb222", "B")]
        assert (
            resolve_partial_id_in_items(
                "aaa",
                items,
                entity_name="thing",
                list_command="thing list",
                stdout_console=MagicMock(),
            )
            == "aaa111"
        )

    def test_partial_unique_match_with_dict_accessor(self):
        """Entity-specific config: dict-shaped items via ``id_of``/``title_of``.

        This is the canonical consolidation test - the download path uses
        :class:`ArtifactDict` and reaches the same matching logic without
        first reshaping its inputs.
        """
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        items = [
            {"id": "art_aaa", "title": "First"},
            {"id": "art_bbb", "title": "Second"},
        ]
        result = resolve_partial_id_in_items(
            "art_a",
            items,
            entity_name="artifact",
            list_command="artifact list",
            id_of=lambda a: a["id"],
            title_of=lambda a: a["title"],
            error_factory=ValueError,
            stdout_console=MagicMock(),
        )
        assert result == "art_aaa"

    def test_ambiguous_partial_match_raises_via_error_factory(self):
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        items = [
            {"id": "art_aaa", "title": "First"},
            {"id": "art_aab", "title": "Second"},
        ]
        # When error_factory=ValueError, the consolidated resolver raises
        # ValueError (not click.ClickException) so the download path's
        # ``except ValueError`` continues to work.
        with pytest.raises(ValueError) as exc:
            resolve_partial_id_in_items(
                "art_a",
                items,
                entity_name="artifact",
                list_command="artifact list",
                id_of=lambda a: a["id"],
                title_of=lambda a: a["title"],
                error_factory=ValueError,
            )

        # The default canonical wording is "Ambiguous ID 'X' matches N artifacts:".
        # The download_helpers wrapper translates this to historical wording -
        # but at THIS layer the canonical message is what surfaces.
        assert "Ambiguous" in str(exc.value)
        assert "art_a" in str(exc.value)

    def test_no_match_raises_via_error_factory(self):
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        items = [{"id": "art_xyz", "title": "Only"}]
        with pytest.raises(ValueError) as exc:
            resolve_partial_id_in_items(
                "art_a",
                items,
                entity_name="artifact",
                list_command="artifact list",
                id_of=lambda a: a["id"],
                title_of=lambda a: a["title"],
                error_factory=ValueError,
            )
        # Canonical wording at this layer ("No artifact found starting with..."
        # + the discoverability hint pointing at ``artifact list``).
        assert "No artifact found starting with" in str(exc.value)
        assert "artifact list" in str(exc.value)

    def test_empty_partial_id_raises_via_error_factory(self):
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        with pytest.raises(ValueError, match="cannot be empty"):
            resolve_partial_id_in_items(
                "   ",
                [{"id": "x", "title": "y"}],
                entity_name="artifact",
                list_command="artifact list",
                id_of=lambda a: a["id"],
                title_of=lambda a: a["title"],
                error_factory=ValueError,
            )

    def test_default_error_factory_is_click_exception(self):
        """Without an explicit ``error_factory``, the async-path default
        ``click.ClickException`` is used. This is the contract that the
        async ``_resolve_partial_id`` relies on so Click can render its
        familiar exit-1 + stderr error."""
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        with pytest.raises(click.ClickException):
            resolve_partial_id_in_items(
                "missing",
                [],
                entity_name="notebook",
                list_command="list",
            )

    def test_exact_match_wins_over_ambiguous_prefix(self):
        """The exact-id-wins rule must hold for the consolidated core too."""
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        items = [
            {"id": "abc", "title": "Exact"},
            {"id": "abc12345-6789-4abc-def0-1234567890ab", "title": "Long"},
        ]
        # 'abc' is an exact match for the first item AND a prefix of the
        # second. The exact match must win - no "Matched: ..." status, no
        # ambiguity error.
        mock_console = MagicMock()
        result = resolve_partial_id_in_items(
            "abc",
            items,
            entity_name="artifact",
            list_command="artifact list",
            id_of=lambda a: a["id"],
            title_of=lambda a: a["title"],
            error_factory=ValueError,
            stdout_console=mock_console,
        )
        assert result == "abc"
        mock_console.print.assert_not_called()

    def test_matched_status_routes_to_stderr_in_json_mode(self):
        """JSON-mode keeps stdout parseable by routing "Matched..." to stderr."""
        from notebooklm.cli.resolve import resolve_partial_id_in_items

        items = [{"id": "abc12345", "title": "First"}]
        stdout_console = MagicMock()
        stderr_console = MagicMock()

        result = resolve_partial_id_in_items(
            "abc",
            items,
            entity_name="artifact",
            list_command="artifact list",
            id_of=lambda a: a["id"],
            title_of=lambda a: a["title"],
            error_factory=ValueError,
            json_output=True,
            stdout_console=stdout_console,
            stderr_output_console=stderr_console,
        )

        assert result == "abc12345"
        stdout_console.print.assert_not_called()
        stderr_console.print.assert_called_once()
        printed = stderr_console.print.call_args[0][0]
        assert "Matched" in printed


class TestEntitySpecificPartialArtifactId:
    """Cover the download-helpers entity-specific consolidated path.

    Acceptance criterion: "entity-specific partial-ID resolution for
    artifacts". The download path delegates to ``resolve_partial_id_in_items``
    via :func:`notebooklm.cli.download_helpers.resolve_partial_artifact_id`
    with the dict-shape accessors and ``ValueError`` factory.
    """

    def test_consolidated_path_handles_artifact_dict_shape(self):
        from notebooklm.cli.download_helpers import resolve_partial_artifact_id

        artifacts = [
            {"id": "abc111-aaaa-4abc-def0-000000000001", "title": "First", "created_at": 1},
            {"id": "xyz222-aaaa-4abc-def0-000000000002", "title": "Second", "created_at": 2},
        ]
        # Unique prefix resolves through the consolidated core.
        assert resolve_partial_artifact_id(artifacts, "abc") == "abc111-aaaa-4abc-def0-000000000001"

    def test_consolidated_path_preserves_historical_not_found_wording(self):
        """The download user-visible error retains its historical
        wording ("Artifact 'X' not found") even though the consolidated
        core uses different wording at the resolve.py layer.

        This is the exact contract from
        ``tests/unit/test_download_helpers.py::test_no_match_raises`` and
        downstream user-visible CLI error envelopes.
        """
        from notebooklm.cli.download_helpers import resolve_partial_artifact_id

        artifacts = [{"id": "xyz222-aaaa-4abc-def0-000000000002", "title": "Only", "created_at": 1}]
        with pytest.raises(ValueError) as exc:
            resolve_partial_artifact_id(artifacts, "abc")

        assert "Artifact 'abc' not found" in str(exc.value)

    def test_consolidated_path_preserves_historical_ambiguous_wording(self):
        """Ambiguous-match error retains the "Ambiguous partial ID 'X' matches: ..."
        wording downstream callers rely on."""
        from notebooklm.cli.download_helpers import resolve_partial_artifact_id

        artifacts = [
            {"id": "abc111", "title": "Meeting Notes", "created_at": 1},
            {"id": "abc222", "title": "Debate Session", "created_at": 2},
        ]
        with pytest.raises(ValueError) as exc:
            resolve_partial_artifact_id(artifacts, "abc")

        msg = str(exc.value)
        assert "Ambiguous partial ID 'abc'" in msg
        assert "Meeting Notes" in msg
        assert "Debate Session" in msg

    def test_consolidated_path_uses_valueerror_not_clickexception(self):
        """The download path's downstream ``except ValueError`` contract is
        preserved by the ``error_factory=ValueError`` config."""
        import click as _click

        from notebooklm.cli.download_helpers import resolve_partial_artifact_id

        with pytest.raises(ValueError) as exc:
            resolve_partial_artifact_id([], "abc")
        assert not isinstance(exc.value, _click.ClickException)

    def test_full_uuid_not_in_list_preserves_not_found_contract(self):
        """``resolve_partial_artifact_id`` keeps ``allow_full_id_passthrough=False``.

        A canonical-shape UUID absent from ``artifacts`` MUST raise the
        historical "Artifact '<id>' not found" wording rather than passing
        through and silently 404-ing at the backend. Pins the wrapper-level
        integration contract that the core-level
        :class:`TestResolvePartialIdInItems` test exercises in isolation.
        """
        from notebooklm.cli.download_helpers import resolve_partial_artifact_id

        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        artifacts = [
            {"id": "xyz22222-aaaa-4abc-def0-000000000002", "title": "Only", "created_at": 1}
        ]

        with pytest.raises(ValueError) as exc:
            resolve_partial_artifact_id(artifacts, full_uuid)

        assert str(exc.value) == f"Artifact '{full_uuid}' not found"
