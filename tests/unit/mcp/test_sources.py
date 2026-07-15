"""Unit tests for the source MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution
reaching the tool, the per-``type`` ``source_add`` dispatch, the confirm
preview-then-delete flow, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm._types.sources import SourceType  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    NetworkError,
    RPCError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)
from notebooklm.mcp._errors import tool_error_payload  # noqa: E402 - after importorskip guard
from notebooklm.mcp.tools._content_sanity import (  # noqa: E402 - after importorskip guard
    _THIN_SOURCE_CHAR_THRESHOLD,
)
from notebooklm.rpc.types import SourceStatus  # noqa: E402 - after importorskip guard
from notebooklm.types import Label, Source  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


@dataclass
class FakeSource:
    id: str
    title: str | None = None

    # ``kind``/``status`` are properties (not fields) → mirror real Source: dropped
    # by to_jsonable but read by the tool's _source_view to add string labels.
    # NOTE: ``kind`` is hardcoded WEB_PAGE, so any source_wait test that lands this in
    # the ``ready`` bucket triggers the #1698 thin-content fetch — mock
    # ``client.sources.get_fulltext`` (ample content) or a swallowed error yields a
    # green-for-the-wrong-reason pass. Use FakeReadyTextSource for a non-web-page READY.
    @property
    def is_ready(self) -> bool:
        return True

    @property
    def is_error(self) -> bool:
        return False

    @property
    def kind(self) -> SourceType:
        return SourceType.WEB_PAGE

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.READY


@dataclass
class FakeNotReadySource:
    """A source that exists but is still processing (``is_ready`` False)."""

    id: str
    title: str | None = None

    @property
    def is_ready(self) -> bool:
        return False

    @property
    def is_error(self) -> bool:
        return False

    @property
    def kind(self) -> SourceType:
        return SourceType.PDF

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.PROCESSING


@dataclass
class FakeFailedSource:
    """A source whose import failed (status ERROR) — the ghost row left by a
    broken ``source_add``. Exercises the synchronous failure-signal path."""

    id: str
    title: str | None = None

    @property
    def is_ready(self) -> bool:
        return False

    @property
    def is_error(self) -> bool:
        return True

    @property
    def kind(self) -> SourceType:
        return SourceType.WEB_PAGE

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.ERROR


@dataclass
class FakeReadyTextSource:
    """A READY non-web-page source (pasted text). ``FakeSource`` hardcodes
    ``kind=WEB_PAGE``; this fake exists so the source_wait thin-content sanity
    check can prove it NEVER flags (or even fetches) a non-web-page source —
    legitimately short pasted text / transcripts must not be warned about."""

    id: str
    title: str | None = None

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def is_error(self) -> bool:
        return False

    @property
    def kind(self) -> SourceType:
        return SourceType.PASTED_TEXT

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.READY


@dataclass
class FakeFulltext:
    """Stand-in for ``SourceFulltext`` (what ``client.sources.get_fulltext`` returns)."""

    content: str = ""
    char_count: int = 0
    source_id: str = ""
    title: str = ""


NB_ID = "11111111-1111-1111-1111-111111111111"
SRC_ID = "33333333-3333-3333-3333-333333333333"
SRC2_ID = "44444444-4444-4444-4444-444444444444"


async def test_source_list(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Doc")])
    result = await mcp_call("source_list", {"notebook": NB_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "sources": [{"id": SRC_ID, "title": "Doc", "kind": "web_page", "status_label": "ready"}],
        "total": 1,
        "offset": 0,
        "has_more": False,
    }
    mock_client.sources.list.assert_awaited_once_with(NB_ID)


async def test_source_list_status_filter(mcp_call, mock_client) -> None:
    """``status`` narrows the list to sources whose ``status_label`` matches."""
    mock_client.sources.list = AsyncMock(
        return_value=[
            FakeSource(id=SRC_ID, title="Ready Doc"),
            FakeFailedSource(id=SRC2_ID, title="Broken Import"),
        ]
    )
    result = await mcp_call("source_list", {"notebook": NB_ID, "status": "error"})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "sources": [
            {
                "id": SRC2_ID,
                "title": "Broken Import",
                "kind": "web_page",
                "status_label": "error",
            }
        ],
        "total": 1,
        "offset": 0,
        "has_more": False,
    }


async def test_source_list_status_filter_no_match(mcp_call, mock_client) -> None:
    """A filter matching nothing yields an empty list (notebook_id still present)."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Ready Doc")])
    result = await mcp_call("source_list", {"notebook": NB_ID, "status": "error"})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "sources": [],
        "total": 0,
        "offset": 0,
        "has_more": False,
    }


async def test_source_list_invalid_status_filter_rejected(mcp_call, mock_client) -> None:
    """An out-of-enum ``status`` is rejected at the schema boundary (Literal).

    Pydantic's exact wording varies by version, so assert loosely that the allowed
    labels surface in the error — matching ``test_source_read_invalid_format``.
    """
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_list", {"notebook": NB_ID, "status": "failed"})
    msg = str(excinfo.value).lower()
    assert "error" in msg and "ready" in msg


async def test_source_list_status_filter_enum_parity(mcp_list_tools) -> None:
    """The ``status`` filter's accepted values are exactly the lower-cased
    ``SourceStatus`` member names (the same vocabulary ``status_label`` emits).

    This pins the hand-written ``Literal`` to the enum so a future ``SourceStatus``
    member can't silently become unfilterable: adding one without extending the
    ``Literal`` trips this guard.
    """
    tools = {t.name: t for t in await mcp_list_tools()}
    status_schema = tools["source_list"].inputSchema["properties"]["status"]
    # ``status: Literal[...] | None`` serializes as an ``anyOf`` of {enum} + {null}.
    # Pull the one branch that carries the enum list.
    enum_values = next(branch["enum"] for branch in status_schema["anyOf"] if "enum" in branch)
    assert set(enum_values) == {s.name.lower() for s in SourceStatus}


async def test_source_list_status_filter_non_ready_labels(mcp_call, mock_client) -> None:
    """Non-ready labels filter too: a ``processing`` / ``error`` source is returned
    when filtering by its own label (the ``ready`` case is covered above; the full
    label set is pinned to the enum by ``test_source_list_status_filter_enum_parity``)."""
    for fake in (FakeNotReadySource(id=SRC_ID, title="P"), FakeFailedSource(id=SRC_ID, title="E")):
        mock_client.sources.list = AsyncMock(return_value=[fake])
        label = fake.status.name.lower()
        result = await mcp_call("source_list", {"notebook": NB_ID, "status": label})
        sources = result.structured_content["sources"]
        assert [s["status_label"] for s in sources] == [label]


async def test_source_list_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    @dataclass
    class FakeNotebook:
        id: str
        title: str

    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.sources.list = AsyncMock(return_value=[])
    result = await mcp_call("source_list", {"notebook": "My Notebook"})
    assert result.structured_content["notebook_id"] == NB_ID
    mock_client.sources.list.assert_awaited_with(NB_ID)


async def test_source_list_compact(mcp_call, mock_client) -> None:
    """``detail="compact"`` projects each source to a 5-field roster row.

    Uses a real ``Source`` so ``created_at`` (dropped by the minimal fakes)
    actually serializes: the row is exactly ``{id, title, kind, status_label,
    created_at}`` — no ``url`` / raw ``status`` / ``_type_code`` — for a low-token
    listing with no extra read.
    """
    from datetime import datetime, timezone

    from notebooklm.types import Source

    src = Source(
        id=SRC_ID,
        title="Doc",
        _type_code=3,  # pdf
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        status=SourceStatus.READY,
    )
    mock_client.sources.list = AsyncMock(return_value=[src])
    result = await mcp_call("source_list", {"notebook": NB_ID, "detail": "compact"})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "sources": [
            {
                "id": SRC_ID,
                "title": "Doc",
                "kind": "pdf",
                "status_label": "ready",
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        "total": 1,
        "offset": 0,
        "has_more": False,
    }


async def test_source_list_compact_composes_with_status_filter(mcp_call, mock_client) -> None:
    """``detail="compact"`` still honors the ``status`` filter (shaping is orthogonal)."""
    mock_client.sources.list = AsyncMock(
        return_value=[
            FakeSource(id=SRC_ID, title="Ready Doc"),
            FakeFailedSource(id=SRC2_ID, title="Broken Import"),
        ]
    )
    result = await mcp_call(
        "source_list", {"notebook": NB_ID, "detail": "compact", "status": "error"}
    )
    rows = result.structured_content["sources"]
    assert [r["id"] for r in rows] == [SRC2_ID]
    assert set(rows[0]) == {"id", "title", "kind", "status_label", "created_at"}
    assert rows[0]["status_label"] == "error"


async def test_source_list_compact_null_created_at(mcp_call, mock_client) -> None:
    """A still-processing source (no decoded ``created_at``) serializes ``created_at``
    to ``None`` in the compact row — the field is always present, never omitted."""
    from notebooklm.types import Source

    src = Source(
        id=SRC_ID, title="Importing", _type_code=3, created_at=None, status=SourceStatus.PROCESSING
    )
    mock_client.sources.list = AsyncMock(return_value=[src])
    result = await mcp_call("source_list", {"notebook": NB_ID, "detail": "compact"})
    row = result.structured_content["sources"][0]
    assert set(row) == {"id", "title", "kind", "status_label", "created_at"}
    assert row["created_at"] is None
    assert row["status_label"] == "processing"


async def test_source_list_default_is_full_unchanged(mcp_call, mock_client) -> None:
    """The default call (no ``detail``) is byte-identical to before (full projection)."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Doc")])
    result = await mcp_call("source_list", {"notebook": NB_ID})
    assert result.structured_content["sources"] == [
        {"id": SRC_ID, "title": "Doc", "kind": "web_page", "status_label": "ready"}
    ]


async def test_source_read(mcp_call, mock_client) -> None:
    """Returns the source metadata AND its full text content + char_count."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="hello world", char_count=11)
    )
    result = await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
        "source": {
            "id": SRC_ID,
            "title": "Doc",
            "kind": "web_page",
            "status_label": "ready",
        },
        "content": "hello world",
        "char_count": 11,
        "truncated": False,
        "output_format": "text",
    }
    mock_client.sources.get_or_none.assert_awaited_once_with(NB_ID, SRC_ID)
    mock_client.sources.get_fulltext.assert_awaited_once_with(NB_ID, SRC_ID, output_format="text")


async def test_source_read_windowing(mcp_call, mock_client) -> None:
    """offset/max_chars window the body; char_count stays full; truncated reflects it."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="abcdefghij", char_count=10)
    )
    result = await mcp_call(
        "source_read",
        {"notebook": NB_ID, "source": SRC_ID, "offset": 2, "max_chars": 3},
    )
    sc = result.structured_content
    assert sc["content"] == "cde"
    assert sc["char_count"] == 10  # full length, not the window
    assert sc["truncated"] is True
    # A window covering the remainder is not truncated.
    result2 = await mcp_call(
        "source_read",
        {"notebook": NB_ID, "source": SRC_ID, "offset": 7, "max_chars": 100},
    )
    assert result2.structured_content["content"] == "hij"
    assert result2.structured_content["truncated"] is False


async def test_source_read_default_cap(mcp_call, mock_client) -> None:
    """Omitting max_chars caps the body at the default (10k), not the full text."""
    big = "x" * 25_000
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content=big, char_count=len(big))
    )
    result = await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    assert len(sc["content"]) == 10_000  # bounded by the default cap
    assert sc["char_count"] == 25_000  # full length still reported
    assert sc["truncated"] is True


async def test_source_read_negative_window_is_validation_error(mcp_call, mock_client) -> None:
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_read",
            {"notebook": NB_ID, "source": SRC_ID, "max_chars": -1},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_source_read_offset_past_end_returns_null(mcp_call, mock_client) -> None:
    """An offset past the body end yields an empty slice → normalized to null."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="abc", char_count=3)
    )
    result = await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID, "offset": 99})
    assert result.structured_content["content"] is None


async def test_source_wait_negative_timeout_is_validation_error(mcp_call, mock_client) -> None:
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "timeout": -1.0})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_wait_zero_interval_is_validation_error(mcp_call, mock_client) -> None:
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "interval": 0.0})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_wait_all_over_cap_is_rejected(mcp_call, mock_client) -> None:
    """The omitted-``sources`` wait-all path is capped at MAX_WAIT_SOURCE_IDS via the
    shared fan-out backstop (parity with REST, which rejected this all along — #1871).
    Without the backstop MCP silently waited on every source; REST returned 400."""
    from notebooklm._app.source_wait import MAX_WAIT_SOURCE_IDS

    over = MAX_WAIT_SOURCE_IDS + 1
    mock_client.sources.list = AsyncMock(
        return_value=[FakeSource(id=f"{i:08d}-0000-0000-0000-000000000000") for i in range(over)]
    )
    mock_client.sources.wait_all_until_ready = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID})
    assert "VALIDATION" in str(excinfo.value)
    # Listed to learn the count, but no poll was started past the cap.
    mock_client.sources.wait_all_until_ready.assert_not_awaited()


def test_drive_mime_choices_match_core_map() -> None:
    """The MCP drive-MIME tuple is duplicated from the core's ``_DRIVE_MIME_MAP``;
    pin them equal so a new core MIME type can't silently lag the MCP validation."""
    from notebooklm._app import source_mutations as mut_core
    from notebooklm.mcp.tools.sources import _DRIVE_MIME_CHOICES

    assert set(_DRIVE_MIME_CHOICES) == set(mut_core._DRIVE_MIME_MAP)


async def test_source_read_markdown_format(mcp_call, mock_client) -> None:
    """``output_format='markdown'`` is forwarded to the fulltext fetch."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="# Heading", char_count=9)
    )
    result = await mcp_call(
        "source_read",
        {"notebook": NB_ID, "source": SRC_ID, "output_format": "markdown"},
    )
    assert result.structured_content["content"] == "# Heading"
    assert result.structured_content["output_format"] == "markdown"
    mock_client.sources.get_fulltext.assert_awaited_once_with(
        NB_ID, SRC_ID, output_format="markdown"
    )


async def test_source_read_invalid_format_rejected(mcp_call, mock_client) -> None:
    """An out-of-enum ``output_format`` is rejected at the schema boundary.

    Typing the param as ``Literal["text", "markdown"]`` makes FastMCP/Pydantic emit
    a JSON-schema enum and reject anything else before the tool body runs — agents
    see the allowed values in the tool schema.
    """
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_read",
            {"notebook": NB_ID, "source": SRC_ID, "output_format": "pdf"},
        )
    msg = str(excinfo.value).lower()
    assert "text" in msg and "markdown" in msg


async def test_source_read_markdown_missing_extra_is_config_error(mcp_call, mock_client) -> None:
    """``output_format='markdown'`` without the ``markdownify`` extra surfaces a CONFIG
    error (with the install hint), not a bug-class UNEXPECTED."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        side_effect=ImportError(
            "The 'markdown' format requires the 'markdownify' package. "
            "Install it with: pip install 'notebooklm-py[markdown]'"
        )
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_read",
            {"notebook": NB_ID, "source": SRC_ID, "output_format": "markdown"},
        )
    msg = str(excinfo.value)
    assert "CONFIG" in msg
    assert "markdownify" in msg  # the actionable install hint survives


async def test_source_read_text_import_error_not_remapped(mcp_call, mock_client) -> None:
    """An ImportError on the TEXT path is genuinely unexpected — it must NOT be
    relabeled CONFIG (the remap is restricted to the markdown case)."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(side_effect=ImportError("unrelated boom"))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert "CONFIG" not in str(excinfo.value)


async def test_source_read_not_ready_returns_null_without_fetch(mcp_call, mock_client) -> None:
    """A still-processing source returns metadata + content=null and does NOT fetch
    the body (gating on status avoids both a wasted RPC and masking a genuine
    not-found)."""
    mock_client.sources.get_or_none = AsyncMock(
        return_value=FakeNotReadySource(id=SRC_ID, title="Doc")
    )
    mock_client.sources.get_fulltext = AsyncMock(return_value=FakeFulltext(content="x"))
    result = await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content["source"] == {
        "id": SRC_ID,
        "title": "Doc",
        "kind": "pdf",
        "status_label": "processing",
    }
    assert result.structured_content["content"] is None
    assert result.structured_content["char_count"] == 0
    assert result.structured_content["output_format"] == "text"
    mock_client.sources.get_fulltext.assert_not_called()


async def test_source_read_ready_but_gone_propagates_not_found(mcp_call, mock_client) -> None:
    """A READY source whose fulltext fetch raises NOT_FOUND (e.g. deleted between the
    metadata and body calls) propagates as NOT_FOUND — it is NOT masked as
    content=null."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(side_effect=SourceNotFoundError(SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert "NOT_FOUND" in str(excinfo.value) or "not found" in str(excinfo.value).lower()


async def test_source_read_empty_body_normalized_to_null(mcp_call, mock_client) -> None:
    """An empty extracted body (``""``) is surfaced as ``null``, not an empty string."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="", char_count=0)
    )
    result = await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content["content"] is None


async def test_source_read_resolves_source_by_name(mcp_call, mock_client) -> None:
    """A non-id ``source`` ref resolves by exact title within the notebook."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Paper")])
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Paper"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="body", char_count=4)
    )
    result = await mcp_call("source_read", {"notebook": NB_ID, "source": "Paper"})
    assert result.structured_content["source_id"] == SRC_ID
    mock_client.sources.get_or_none.assert_awaited_once_with(NB_ID, SRC_ID)


@dataclass
class FakeGuide:
    """Stand-in for ``SourceGuide`` (what ``client.sources.get_guide`` returns).

    ``execute_source_guide`` reads ``.summary`` / ``.keywords`` by attribute, so a
    plain stub suffices — no need to build the real frozen dataclass.
    """

    summary: str = ""
    keywords: tuple[str, ...] = ()


async def test_source_read_summary(mcp_call, mock_client) -> None:
    """Returns the AI summary + keywords (keywords as a JSON list)."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Paper"))
    mock_client.sources.get_guide = AsyncMock(
        return_value=FakeGuide(summary="A short overview.", keywords=("alpha", "beta"))
    )
    result = await mcp_call(
        "source_read", {"detail": "summary", "notebook": NB_ID, "source": SRC_ID}
    )
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
        "summary": "A short overview.",
        "keywords": ["alpha", "beta"],
    }
    mock_client.sources.get_guide.assert_awaited_once_with(NB_ID, SRC_ID)


async def test_source_read_summary_resolves_source_by_name(mcp_call, mock_client) -> None:
    """A non-id ``source`` ref resolves by exact title within the notebook."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Paper")])
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Paper"))
    mock_client.sources.get_guide = AsyncMock(
        return_value=FakeGuide(summary="Body summary.", keywords=("topic",))
    )
    result = await mcp_call(
        "source_read", {"detail": "summary", "notebook": NB_ID, "source": "Paper"}
    )
    assert result.structured_content["source_id"] == SRC_ID
    mock_client.sources.get_guide.assert_awaited_once_with(NB_ID, SRC_ID)


async def test_source_read_summary_existing_source_empty_guide_is_success(
    mcp_call, mock_client
) -> None:
    """A real source with no guide yet (still processing) returns empty
    summary/keywords — a valid state, NOT NOT_FOUND (distinct from a missing id)."""
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Paper"))
    mock_client.sources.get_guide = AsyncMock(return_value=FakeGuide(summary="", keywords=()))
    result = await mcp_call(
        "source_read", {"detail": "summary", "notebook": NB_ID, "source": SRC_ID}
    )
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
        "summary": "",
        "keywords": [],
    }


async def test_source_read_summary_missing_full_uuid_is_not_found(mcp_call, mock_client) -> None:
    """A full-UUID ref skips list resolution, so a non-existent source reaches the
    existence guard → NOT_FOUND (not a misleading empty-guide success), and the
    guide RPC is never attempted."""
    mock_client.sources.get_or_none = AsyncMock(return_value=None)
    mock_client.sources.get_guide = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_read", {"detail": "summary", "notebook": NB_ID, "source": SRC_ID})
    assert "NOT_FOUND" in str(excinfo.value)
    mock_client.sources.get_guide.assert_not_called()


async def test_source_read_invalid_detail_rejected(mcp_call, mock_client) -> None:
    """An out-of-enum ``detail`` is rejected at the Literal schema boundary, no RPC."""
    mock_client.sources.get_or_none = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID, "detail": "bogus"})
    mock_client.sources.get_or_none.assert_not_called()


@pytest.mark.parametrize(("arg", "value"), [("max_chars", -1), ("offset", -1)])
async def test_source_read_summary_still_validates_windowing(
    mcp_call, mock_client, arg, value
) -> None:
    """Windowing args are validated UNCONDITIONALLY — a bad ``max_chars``/``offset``
    errors even in summary mode (where they are ignored), never silently passes."""
    mock_client.sources.get_or_none = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_read",
            {"notebook": NB_ID, "source": SRC_ID, "detail": "summary", arg: value},
        )
    assert arg in str(excinfo.value)
    mock_client.sources.get_or_none.assert_not_called()


async def test_source_rename(mcp_call, mock_client) -> None:
    mock_client.sources.rename = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Renamed"))
    result = await mcp_call(
        "source_rename", {"notebook": NB_ID, "source": SRC_ID, "new_title": "Renamed"}
    )
    assert result.structured_content == {
        "status": "renamed",
        "source": {"id": SRC_ID, "title": "Renamed"},
        "notebook_id": NB_ID,
    }
    mock_client.sources.rename.assert_awaited_once_with(NB_ID, SRC_ID, "Renamed")


async def test_source_delete_without_confirm_previews(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Doomed")])
    mock_client.sources.delete = AsyncMock(return_value=None)
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {
            "action": "delete_source",
            "notebook_id": NB_ID,
            "source_id": SRC_ID,
            "title": "Doomed",
        },
    }
    mock_client.sources.delete.assert_not_called()


async def test_source_delete_with_confirm_deletes(mcp_call, mock_client) -> None:
    mock_client.sources.delete = AsyncMock(return_value=None)
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": SRC_ID, "confirm": True})
    assert result.structured_content == {
        "status": "deleted",
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
    }
    mock_client.sources.delete.assert_awaited_once_with(NB_ID, SRC_ID)


# ---------------------------------------------------------------------------
# source_wait — both modes share ONE aggregate contract:
#   {notebook_id, ok, ready, timed_out, failed, not_found,
#    ready_count, timed_out_count, failed_count, not_found_count, total_count}
# ``ready`` carries _source_view rows; the three error buckets carry
# {source_id, error}. ``ok`` is True iff all three error buckets are empty. The
# ``*_count`` scalars mirror the bucket lengths and ``total_count`` folds all
# four buckets (#1822) — additive to the arrays, which stay for compatibility.
# ---------------------------------------------------------------------------

_AGGREGATE_BUCKET_KEYS = ("ready", "timed_out", "failed", "not_found")
_AGGREGATE_COUNT_KEYS = {
    "ready_count",
    "timed_out_count",
    "failed_count",
    "not_found_count",
    "total_count",
}
_AGGREGATE_KEYS = {"notebook_id", "ok", *_AGGREGATE_BUCKET_KEYS} | _AGGREGATE_COUNT_KEYS


def _assert_aggregate_shape(structured: dict[str, Any]) -> None:
    """Pin the aggregate keys + count invariants so they aren't re-asserted per test."""
    assert set(structured) == _AGGREGATE_KEYS
    assert isinstance(structured["ok"], bool)
    for key in _AGGREGATE_BUCKET_KEYS:
        assert isinstance(structured[key], list)
        # Each ``*_count`` scalar equals its bucket length (#1822).
        assert structured[f"{key}_count"] == len(structured[key])
    assert structured["total_count"] == sum(len(structured[key]) for key in _AGGREGATE_BUCKET_KEYS)


def _dispatch_wait_until_ready(by_id: dict[str, Any]) -> Any:
    """Build a ``wait_until_ready`` side_effect dispatching on the source id.

    The tool calls ``wait_until_ready(notebook_id, source_id, timeout=…,
    initial_interval=…)`` (per source), so ``source_id`` is the 2nd positional
    arg. ``by_id`` maps a source id to either a ``FakeSource`` (returned ready) or
    an ``Exception`` instance (raised) — letting one fan-out mix ready/failed/etc.
    """

    def _side_effect(_notebook_id: str, source_id: str, **_kwargs: Any) -> Any:
        outcome = by_id[source_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return AsyncMock(side_effect=_side_effect)


def _dispatch_wait_all(by_id: dict[str, Any]) -> Any:
    """Build a ``wait_all_until_ready`` side_effect dispatching on the source id.

    The multi-source tool calls ``wait_all_until_ready(notebook_id, source_ids,
    timeout=…, initial_interval=…)`` ONCE (single-snapshot loop, #1870) and gets
    back one result per id, in input order. ``by_id`` maps a source id to either
    a ``FakeSource`` or a terminal ``Exception`` instance — but here the failures
    are RETURNED in the list, never raised, mirroring the real neutral union.
    """

    def _side_effect(_notebook_id: str, source_ids: list[str], **_kwargs: Any) -> list[Any]:
        return [by_id[source_id] for source_id in source_ids]

    return AsyncMock(side_effect=_side_effect)


async def test_source_wait_single_source_ready(mcp_call, mock_client) -> None:
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Ready")
    )
    # FakeSource is a READY web_page, so the thin-content sanity check fetches its
    # body; return ample content so NO warning is added (assert below pins the exact
    # ready row). Without this the fetch would await a bare MagicMock and the
    # swallowed TypeError would mask the assertion — green for the wrong reason.
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert sc["ready"] == [
        {"id": SRC_ID, "title": "Ready", "kind": "web_page", "status_label": "ready"}
    ]
    assert sc["timed_out"] == sc["failed"] == sc["not_found"] == []


async def test_source_wait_single_source_not_found(mcp_call, mock_client) -> None:
    """A resolved full-UUID source the backend can't find → ``not_found`` bucket."""
    mock_client.sources.wait_until_ready = AsyncMock(side_effect=SourceNotFoundError(SRC_ID))
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is False
    assert sc["ready"] == []
    assert sc["not_found"] == [{"source_id": SRC_ID, "error": f"Source not found: {SRC_ID}"}]


async def test_source_wait_single_source_timeout(mcp_call, mock_client) -> None:
    mock_client.sources.wait_until_ready = AsyncMock(
        side_effect=SourceTimeoutError(SRC_ID, 5.0, last_status=1)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is False
    assert sc["timed_out"] and sc["timed_out"][0]["source_id"] == SRC_ID
    assert sc["failed"] == sc["not_found"] == []


async def test_source_wait_single_source_failed(mcp_call, mock_client) -> None:
    mock_client.sources.wait_until_ready = AsyncMock(
        side_effect=SourceProcessingError(SRC_ID, status=3)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is False
    assert sc["failed"] and sc["failed"][0]["source_id"] == SRC_ID
    assert sc["timed_out"] == sc["not_found"] == []


async def test_source_wait_single_source_name_miss_raises(mcp_call, mock_client) -> None:
    """An UNRESOLVABLE non-UUID ``source`` ref is an input error → ToolError NOT_FOUND,
    NOT a ``not_found`` bucket entry (the resolver raises before the wait loop)."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Other")])
    mock_client.sources.wait_until_ready = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "source": "No Such Title"})
    assert "NOT_FOUND" in str(excinfo.value)
    mock_client.sources.wait_until_ready.assert_not_called()


async def test_source_wait_all_sources_all_ready(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(
        return_value=[FakeSource(id=SRC_ID), FakeSource(id=SRC2_ID)]
    )
    mock_client.sources.wait_for_sources = AsyncMock()
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {SRC_ID: FakeSource(id=SRC_ID, title="A"), SRC2_ID: FakeSource(id=SRC2_ID, title="B")}
    )
    # Both fakes are READY web_pages → the thin-content check fetches each body;
    # ample content ⇒ no warning (keeps the ready-id set assertion exact).
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert {row["id"] for row in sc["ready"]} == {SRC_ID, SRC2_ID}
    assert sc["timed_out"] == sc["failed"] == sc["not_found"] == []
    # The aggregate uses the single-snapshot wait_all_until_ready, NOT the
    # throw-on-first wait_for_sources helper (which would discard partial progress).
    mock_client.sources.wait_for_sources.assert_not_called()


async def test_source_wait_all_sources_partial_progress(mcp_call, mock_client) -> None:
    """One call mixing ready + timeout + failed + not_found keeps the ready ones
    (partial progress) and sets ok=False — the core of #1669."""
    ready_id, timeout_id, failed_id, missing_id = (
        "10000000-0000-0000-0000-000000000001",
        "20000000-0000-0000-0000-000000000002",
        "30000000-0000-0000-0000-000000000003",
        "40000000-0000-0000-0000-000000000004",
    )
    mock_client.sources.list = AsyncMock(
        return_value=[FakeSource(id=i) for i in (ready_id, timeout_id, failed_id, missing_id)]
    )
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {
            ready_id: FakeSource(id=ready_id, title="OK"),
            timeout_id: SourceTimeoutError(timeout_id, 5.0),
            failed_id: SourceProcessingError(failed_id, status=3),
            missing_id: SourceNotFoundError(missing_id),
        }
    )
    # The lone ready source is a READY web_page → thin-content check fetches it;
    # ample content ⇒ no warning, so the ready bucket stays exactly [ready_id].
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is False
    assert [row["id"] for row in sc["ready"]] == [ready_id]
    assert [e["source_id"] for e in sc["timed_out"]] == [timeout_id]
    assert [e["source_id"] for e in sc["failed"]] == [failed_id]
    assert [e["source_id"] for e in sc["not_found"]] == [missing_id]
    # Explicit counts mirror the buckets and total across all four (#1822).
    assert sc["ready_count"] == 1
    assert sc["timed_out_count"] == 1
    assert sc["failed_count"] == 1
    assert sc["not_found_count"] == 1
    assert sc["total_count"] == 4


async def test_source_wait_all_sources_empty_notebook(mcp_call, mock_client) -> None:
    """A notebook with no sources → all buckets empty, ok=True."""
    mock_client.sources.list = AsyncMock(return_value=[])
    result = await mcp_call("source_wait", {"notebook": NB_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc == {
        "notebook_id": NB_ID,
        "ok": True,
        "ready": [],
        "timed_out": [],
        "failed": [],
        "not_found": [],
        "ready_count": 0,
        "timed_out_count": 0,
        "failed_count": 0,
        "not_found_count": 0,
        "total_count": 0,
    }


async def test_source_wait_all_sources_forwards_interval(mcp_call, mock_client) -> None:
    """The all-sources branch honors the advertised ``timeout``/``interval`` per source."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID)])
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {SRC_ID: FakeSource(id=SRC_ID, title="A")}
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    await mcp_call("source_wait", {"notebook": NB_ID, "timeout": 30.0, "interval": 3.0})
    mock_client.sources.wait_all_until_ready.assert_awaited_once_with(
        NB_ID, [SRC_ID], timeout=30.0, initial_interval=3.0
    )


async def test_source_wait_all_sources_propagates_unexpected_error(mcp_call, mock_client) -> None:
    """An UNEXPECTED escape from the snapshot poll (e.g. an RPCError — not one of the
    3 handled per-source failures the loop RETURNS) propagates as ToolError. The
    single-snapshot loop is one coroutine, so there are no sibling pollers to leak
    (#1870)."""
    slow_id, raiser_id = (
        "50000000-0000-0000-0000-000000000005",
        "60000000-0000-0000-0000-000000000006",
    )
    mock_client.sources.list = AsyncMock(
        return_value=[FakeSource(id=slow_id), FakeSource(id=raiser_id)]
    )
    mock_client.sources.wait_all_until_ready = AsyncMock(side_effect=RPCError("unexpected boom"))
    mock_client.sources.wait_for_sources = AsyncMock()

    with pytest.raises(ToolError):
        await mcp_call("source_wait", {"notebook": NB_ID})
    mock_client.sources.wait_for_sources.assert_not_called()


# ---------------------------------------------------------------------------
# source_wait — thin-content sanity warning (#1698): a READY web_page whose
# fetched text is suspiciously thin is flagged (likely dead link / soft-404 /
# paywall ghost source). web-page only; never rejects; best-effort.
# ---------------------------------------------------------------------------


async def test_source_wait_thin_web_page_warns(mcp_call, mock_client) -> None:
    """A READY web_page with < the thin threshold (100) chars gets a warning,
    yet stays READY/ok (advisory only)."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Ghost")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="tiny", char_count=4)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert len(sc["ready"]) == 1
    row = sc["ready"][0]
    assert row["id"] == SRC_ID
    assert "warning" in row
    assert "4 chars" in row["warning"]
    assert "source_read" in row["warning"]
    mock_client.sources.get_fulltext.assert_awaited_once_with(NB_ID, SRC_ID, output_format="text")


async def test_source_wait_ample_web_page_no_warning(mcp_call, mock_client) -> None:
    """A READY web_page at/above the threshold is not flagged."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Real")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 100, char_count=100)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    assert sc["ready"][0].get("warning") is None


@pytest.mark.parametrize(
    ("char_count", "expect_warning"),
    [
        (_THIN_SOURCE_CHAR_THRESHOLD - 1, True),  # just under → char-thin warning
        (_THIN_SOURCE_CHAR_THRESHOLD, False),  # exactly at → not flagged (gate is ``<``)
    ],
)
async def test_source_wait_thin_threshold_boundary(
    mcp_call, mock_client, char_count: int, expect_warning: bool
) -> None:
    """Pin the char-thin boundary to the documented threshold constant, so the number
    in ``docs/mcp-guide.md`` (and the copyable trigger downstream users build against)
    can't silently drift from :data:`_THIN_SOURCE_CHAR_THRESHOLD`. The gate is ``<``:
    ``threshold - 1`` warns, exactly ``threshold`` does not."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Boundary")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * char_count, char_count=char_count)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    warning = result.structured_content["ready"][0].get("warning")
    if expect_warning:
        assert warning is not None
        # Pin the char-thin branch specifically (not merely "some warning"): the
        # message reports the count and points at source_read — the exact shape the
        # doc tells downstream integrators to assert against.
        assert f"{char_count} chars" in warning
        assert "little/no text extracted" in warning
        assert 'source_read (detail="full")' in warning
    else:
        assert warning is None


async def test_source_wait_zero_char_web_page_warns(mcp_call, mock_client) -> None:
    """A READY web_page with 0 extracted chars warns too — the wording covers the
    not-yet-indexed / empty case rather than asserting a false 'dead link'."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Empty")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="", char_count=0)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    row = sc["ready"][0]
    assert "warning" in row
    assert "0 chars" in row["warning"]
    assert "not-yet-indexed" in row["warning"]


async def test_source_wait_non_web_page_not_flagged_or_fetched(mcp_call, mock_client) -> None:
    """A READY non-web-page source (short pasted text) is NEVER flagged, and its
    body is NEVER fetched — the kind gate protects legitimately short content."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Short note")
    )
    mock_client.sources.get_fulltext = AsyncMock()
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    assert sc["ready"][0].get("warning") is None
    mock_client.sources.get_fulltext.assert_not_called()


async def test_source_wait_thin_check_fetch_failure_degrades(mcp_call, mock_client) -> None:
    """A body-fetch failure must NEVER break the wait — it degrades to no warning."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Flaky")
    )
    mock_client.sources.get_fulltext = AsyncMock(side_effect=RuntimeError("transport boom"))
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert sc["ready"][0].get("warning") is None


async def test_source_wait_all_sources_thin_warning_per_item(mcp_call, mock_client) -> None:
    """Across the all-sources fan-out: only the thin web_page is flagged; the ample
    web_page and the non-web-page are not. ``get_fulltext`` is called for the two
    web_pages only (the pasted-text source is skipped by the kind gate)."""
    thin_id, ample_id, text_id = SRC_ID, SRC2_ID, "55555555-5555-5555-5555-555555555555"
    mock_client.sources.list = AsyncMock(
        return_value=[
            FakeSource(id=thin_id),
            FakeSource(id=ample_id),
            FakeReadyTextSource(id=text_id),
        ]
    )
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {
            thin_id: FakeSource(id=thin_id, title="Ghost"),
            ample_id: FakeSource(id=ample_id, title="Real"),
            text_id: FakeReadyTextSource(id=text_id, title="Note"),
        }
    )

    def _fulltext(_nb: str, source_id: str, **_kwargs: Any) -> Any:
        char_count = 5 if source_id == thin_id else 300
        return FakeFulltext(content="y" * char_count, char_count=char_count)

    mock_client.sources.get_fulltext = AsyncMock(side_effect=_fulltext)
    result = await mcp_call("source_wait", {"notebook": NB_ID})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    warned = {row["id"]: row.get("warning") for row in sc["ready"]}
    assert warned[thin_id] is not None and "5 chars" in warned[thin_id]
    assert warned[ample_id] is None
    assert warned[text_id] is None
    # Only the two web_page sources are fetched; the pasted-text source is skipped.
    fetched_ids = {call.args[1] for call in mock_client.sources.get_fulltext.await_args_list}
    assert fetched_ids == {thin_id, ample_id}


# ---------------------------------------------------------------------------
# source_wait — soft-404 boilerplate body scan: a READY web_page that sails past
# the char-thin rule but whose (short) body matches a dead-link / error-page
# phrase is flagged. Body-only — the title is NEVER scanned.
# ---------------------------------------------------------------------------


async def test_source_wait_soft_404_body_phrase_warns(mcp_call, mock_client) -> None:
    """A 1,766-char READY web_page whose body is 'Whoops! broken link' boilerplate
    (well above the 100-char thin threshold) is flagged via the body phrase scan —
    the regression for the reported soft-404."""
    # Pad to EXACTLY 1766 chars so char_count == len(content) (the impl gates on
    # char_count but scans content; keep the fixture honest to the reported case).
    prefix = "Whoops! The page you requested has a broken link. "
    body = prefix + "x" * (1766 - len(prefix))
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Article | Example")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content=body, char_count=1766)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    sc = result.structured_content
    assert sc["ok"] is True
    row = sc["ready"][0]
    assert "warning" in row
    assert "1766 chars" in row["warning"]
    assert "soft-404" in row["warning"]
    assert "source_read" in row["warning"]


async def test_source_wait_long_healthy_body_with_phrase_not_flagged(mcp_call, mock_client) -> None:
    """A healthy page at/above the 2000-char body-scan limit is NOT scanned, even if
    its body happens to mention 'broken link' (length-gated out)."""
    body = "We explain how to find a broken link on your site. " + "content " * 400
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Fixing dead links")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content=body, char_count=2500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content["ready"][0].get("warning") is None


async def test_source_wait_body_scan_limit_boundary_not_flagged(mcp_call, mock_client) -> None:
    """Boundary: a body of EXACTLY _SOFT_404_BODY_SCAN_LIMIT (2000) chars is NOT
    scanned (the gate is ``< limit``) — locks against an off-by-one if it ever
    became ``<=``. The body contains a dead-link phrase, so a scan WOULD flag it."""
    body = "Whoops! broken link. " + "x" * (2000 - len("Whoops! broken link. "))
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Edge")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content=body, char_count=2000)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content["ready"][0].get("warning") is None


async def test_source_wait_no_phrase_short_body_not_flagged(mcp_call, mock_client) -> None:
    """A short (sub-2000) healthy body with no dead-link phrase is not flagged."""
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Real")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="A genuine short article body.", char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content["ready"][0].get("warning") is None


async def test_source_wait_title_phrase_not_flagged(mcp_call, mock_client) -> None:
    """Locks the no-title-scan contract: a page TITLED like an error-page topic
    ("Broken Link Checker" / "HTTP 404 errors explained") with a clean, phrase-free
    body is NOT flagged — only the body is scanned, never the title.

    The body is deliberately sub-:data:`_SOFT_404_BODY_SCAN_LIMIT` (char_count=500)
    so the phrase-scan path IS active — a regression that folded the title into the
    scan would fire here and flip the assertion (a ``char_count >= 2000`` body would
    short-circuit the scan and pass vacuously)."""
    body = "A thorough guide to website maintenance tooling."
    for title in ("Broken Link Checker", "HTTP 404 errors explained"):
        mock_client.sources.wait_until_ready = AsyncMock(
            return_value=FakeSource(id=SRC_ID, title=title)
        )
        mock_client.sources.get_fulltext = AsyncMock(
            return_value=FakeFulltext(content=body, char_count=500)
        )
        result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
        assert result.structured_content["ready"][0].get("warning") is None


# ---------------------------------------------------------------------------
# source_add batch — content-sanity warning on synchronously-READY web_page items
# (Task B reuses _annotate_thin_warnings). Most adds return PROCESSING (no fetch);
# this covers the ready case + the leak guard (single mode never fetches).
# ---------------------------------------------------------------------------


async def test_source_add_batch_ready_soft_404_carries_warning(mcp_call, mock_client) -> None:
    """A synchronously-READY web_page item whose body matches a dead-link phrase
    carries the soft-404 warning in its batch result."""
    body = "Whoops! that's a broken link. " + "filler " * 200
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Dead"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content=body, char_count=1500)
    )
    result = await mcp_call("source_add", {"notebook": NB_ID, "urls": ["https://dead.example.com"]})
    item = result.structured_content["results"][0]
    assert item["status"] == "added"
    assert "soft-404" in item["warning"]


async def test_source_add_batch_ready_healthy_no_warning(mcp_call, mock_client) -> None:
    """A ready item with ample, healthy body carries no warning."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Real"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_add", {"notebook": NB_ID, "urls": ["https://real.example.com"]})
    assert "warning" not in result.structured_content["results"][0]


async def test_source_add_batch_processing_item_not_fetched(mcp_call, mock_client) -> None:
    """A still-PROCESSING added item is not content-checked (no fetch, no warning)."""
    mock_client.sources.add_url = AsyncMock(
        return_value=FakeNotReadySource(id=SRC_ID, title="Pending")
    )
    mock_client.sources.get_fulltext = AsyncMock()
    result = await mcp_call(
        "source_add", {"notebook": NB_ID, "urls": ["https://pending.example.com"]}
    )
    item = result.structured_content["results"][0]
    assert item["status"] == "added"
    assert "warning" not in item
    mock_client.sources.get_fulltext.assert_not_called()


async def test_source_add_batch_fetch_failure_does_not_abort(mcp_call, mock_client) -> None:
    """A content-check fetch failure degrades to no warning — the item stays added
    and the batch is never aborted."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Flaky"))
    mock_client.sources.get_fulltext = AsyncMock(side_effect=RuntimeError("transport boom"))
    result = await mcp_call(
        "source_add", {"notebook": NB_ID, "urls": ["https://flaky.example.com"]}
    )
    sc = result.structured_content
    assert sc["added"] == 1
    item = sc["results"][0]
    assert item["status"] == "added"
    assert "warning" not in item


async def test_source_add_single_url_never_content_checked(mcp_call, mock_client) -> None:
    """Leak guard: single-mode source_add(source_type='url') must NEVER fetch the
    body — the content-sanity helper stays confined to batch + source_wait."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="One"))
    mock_client.sources.get_fulltext = AsyncMock()
    await mcp_call(
        "source_add",
        {"notebook": NB_ID, "source_type": "url", "url": "https://one.example.com"},
    )
    mock_client.sources.get_fulltext.assert_not_called()


async def test_source_add_text(mcp_call, mock_client) -> None:
    mock_client.sources.add_text = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Notes"))
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "source_type": "text", "text": "hello world", "title": "Notes"},
    )
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "status": "added",
        "source": {"id": SRC_ID, "title": "Notes", "kind": "web_page", "status_label": "ready"},
    }
    mock_client.sources.add_text.assert_awaited_once_with(NB_ID, "Notes", "hello world")


async def test_source_add_url(mcp_call, mock_client) -> None:
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Page"))
    result = await mcp_call(
        "source_add", {"notebook": NB_ID, "source_type": "url", "url": "https://example.com/a"}
    )
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "status": "added",
        "source": {"id": SRC_ID, "title": "Page", "kind": "web_page", "status_label": "ready"},
    }
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, "https://example.com/a")


async def test_source_add_surfaces_import_failure(mcp_call, mock_client) -> None:
    """When the add response already reflects ERROR, source_add flags it inline:
    a top-level ``warning`` plus ``status_label='error'`` on the echoed source."""
    mock_client.sources.add_url = AsyncMock(
        return_value=FakeFailedSource(id=SRC_ID, title="Broken")
    )
    result = await mcp_call(
        "source_add", {"notebook": NB_ID, "source_type": "url", "url": "https://example.com/bad"}
    )
    sc = result.structured_content
    assert sc["source"]["status_label"] == "error"
    assert "warning" in sc
    assert "source_delete" in sc["warning"]


async def test_source_add_drive(mcp_call, mock_client) -> None:
    mock_client.sources.add_drive = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Sheet"))
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "drive",
            "document_id": "drivefile123",
            "title": "Sheet",
            "mime_type": "google-sheets",
        },
    )
    # SourceAddDriveResult carries the source plus the drive provenance fields.
    assert result.structured_content == {
        "status": "added",
        "source": {"id": SRC_ID, "title": "Sheet", "kind": "web_page", "status_label": "ready"},
        "notebook_id": NB_ID,
        "file_id": "drivefile123",
        "mime_type": "google-sheets",
    }
    mock_client.sources.add_drive.assert_awaited_once()
    called_args = mock_client.sources.add_drive.await_args.args
    assert called_args[0] == NB_ID
    assert called_args[1] == "drivefile123"


async def test_source_add_missing_input_is_validation_error(mcp_call, mock_client) -> None:
    """type=url with no url projects as a VALIDATION ToolError."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_add", {"notebook": NB_ID, "source_type": "url"})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_add_drive_bad_mime_is_validation_error(mcp_call, mock_client) -> None:
    """A bogus drive mime_type projects as VALIDATION (not UNEXPECTED)."""
    mock_client.sources.add_drive = AsyncMock(return_value=FakeSource(id=SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "source_type": "drive",
                "document_id": "drivefile123",
                "mime_type": "bogus",
            },
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    # The error steers the user to the upload path for non-importable Drive types.
    assert "file" in msg
    assert "download" in msg.lower()
    mock_client.sources.add_drive.assert_not_called()


@pytest.mark.parametrize("extra", [{}, {"wait": True}], ids=["plain", "wait"])
async def test_source_add_drive_missing_mime_is_validation_error(
    extra, mcp_call, mock_client
) -> None:
    """An omitted drive mime_type is rejected (no google-doc default) and no add
    RPC runs, so no error source stub is left behind (#1827) — on both the plain
    and the wait=True (folded add-and-wait, #1890) source_add paths."""
    mock_client.sources.add_drive = AsyncMock(return_value=FakeSource(id=SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "source_type": "drive",
                "document_id": "drivefile123",
                **extra,
            },
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    assert "mime_type" in msg
    # The message also points at the upload path for non-importable Drive types.
    assert "file" in msg
    assert "download" in msg.lower()
    # Rejected before resolve_notebook / the add RPC — nothing is persisted.
    mock_client.sources.add_drive.assert_not_called()
    mock_client.notebooks.get.assert_not_called()


async def test_source_add_mime_type_has_no_schema_enum(mcp_list_tools) -> None:
    """``mime_type`` deliberately stays a free-text ``str`` — NOT a ``Literal`` — so its
    schema carries NO ``enum`` (issue #1759, decision b). It is dual-use: ``source_type=
    "file"`` accepts arbitrary MIME, only ``drive`` is restricted (enforced at runtime).
    A future well-meaning narrowing to ``Literal`` (which would reject valid ``file`` MIME)
    fails here."""
    tools = {t.name: t for t in await mcp_list_tools()}
    mime_schema = tools["source_add"].inputSchema["properties"]["mime_type"]
    # No flat ``enum`` and no ``enum`` inside any ``anyOf``/``oneOf`` branch (the shape a
    # ``Literal | None`` would take).
    assert "enum" not in mime_schema
    branches = (mime_schema.get("anyOf") or []) + (mime_schema.get("oneOf") or [])
    assert all("enum" not in branch for branch in branches)


@pytest.mark.parametrize(
    ("source_type", "good", "foreign"),
    [
        # url consumes `url`; text/path/document_id are foreign.
        ("url", {"url": "https://example.com/a"}, {"text": "hi"}),
        ("url", {"url": "https://example.com/a"}, {"path": "/tmp/x"}),
        ("url", {"url": "https://example.com/a"}, {"document_id": "drivefile123"}),
        # text consumes `text`; url/path/document_id are foreign.
        ("text", {"text": "hello"}, {"url": "https://example.com/a"}),
        ("text", {"text": "hello"}, {"path": "/tmp/x"}),
        ("text", {"text": "hello"}, {"document_id": "drivefile123"}),
        # file consumes `path`; url/text/document_id are foreign.
        ("file", {"path": "/tmp/doc.pdf"}, {"url": "https://example.com/a"}),
        ("file", {"path": "/tmp/doc.pdf"}, {"text": "hi"}),
        ("file", {"path": "/tmp/doc.pdf"}, {"document_id": "drivefile123"}),
        # drive consumes `document_id`; url/text/path are foreign. ``mime_type`` is
        # required for drive (#1827), so it rides along in ``good`` to reach the
        # foreign-scalar check (it is metadata, not a content scalar).
        ("drive", {"document_id": "drivefile123", "mime_type": "pdf"}, {"url": "https://e.com/a"}),
        ("drive", {"document_id": "drivefile123", "mime_type": "pdf"}, {"text": "hi"}),
        ("drive", {"document_id": "drivefile123", "mime_type": "pdf"}, {"path": "/tmp/x"}),
        # youtube consumes `url`; text/path/document_id are foreign.
        ("youtube", {"url": "https://www.youtube.com/watch?v=abc"}, {"text": "hi"}),
        ("youtube", {"url": "https://www.youtube.com/watch?v=abc"}, {"path": "/tmp/x"}),
        ("youtube", {"url": "https://www.youtube.com/watch?v=abc"}, {"document_id": "d"}),
    ],
)
async def test_source_add_single_rejects_foreign_content_scalar(
    mcp_call, mock_client, source_type, good, foreign
) -> None:
    """A content scalar that this source_type does not consume fails closed —
    BEFORE any notebook I/O (mirrors batch mode). A notebook *title* is used so
    a single ``notebooks.list`` lookup would be observable were rejection late."""
    mock_client.notebooks.list = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": "Some Notebook", "source_type": source_type, **good, **foreign},
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    # The message names the offending scalar (part of the rejection contract).
    (foreign_key,) = foreign
    assert foreign_key in msg
    # Rejection is pre-resolve, so a name is never looked up.
    mock_client.notebooks.list.assert_not_called()


async def test_source_add_single_lists_all_foreign_scalars(mcp_call, mock_client) -> None:
    """Multiple foreign content scalars are all named in one rejection."""
    mock_client.notebooks.list = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": "Some Notebook",
                "source_type": "text",
                "text": "hello",
                "url": "https://example.com/a",
                "path": "/tmp/x",
            },
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    assert "url" in msg and "path" in msg
    mock_client.notebooks.list.assert_not_called()


async def test_source_add_single_metadata_not_rejected(mcp_call, mock_client) -> None:
    """``title`` / ``mime_type`` are optional metadata, NOT content scalars — a
    valid single add carrying them alongside its one content scalar still works."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Page"))
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "url",
            "url": "https://example.com/a",
            "title": "My Page",
            "mime_type": "text/html",
        },
    )
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "status": "added",
        "source": {"id": SRC_ID, "title": "Page", "kind": "web_page", "status_label": "ready"},
    }
    # The add actually proceeded (not silently rejected). A url source ignores
    # title/mime_type downstream — add_url takes only (notebook_id, url) — so the
    # point here is that supplying them does not trip the content-scalar gate.
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, "https://example.com/a")


async def test_source_read_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise SourceNotFoundError(SRC_ID)

    mock_client.sources.get_or_none = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert "NOT_FOUND" in str(excinfo.value)


async def test_source_read_missing_full_uuid_projects_not_found(mcp_call, mock_client) -> None:
    """A full-UUID ref skips list resolution; a None get_or_none must NOT return
    {"source": null} as success — it projects NOT_FOUND."""
    mock_client.sources.get_or_none = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})
    assert "NOT_FOUND" in str(excinfo.value)


async def test_source_add_youtube_rejects_non_youtube_url(mcp_call, mock_client) -> None:
    """type=youtube with a non-YouTube URL projects as VALIDATION."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Page"))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "source_type": "youtube", "url": "https://example.com/not-yt"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_youtube_accepts_youtube_url(mcp_call, mock_client) -> None:
    """type=youtube with a genuine YouTube URL is accepted."""
    yt = "https://www.youtube.com/watch?v=abc123"
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Vid"))
    result = await mcp_call("source_add", {"notebook": NB_ID, "source_type": "youtube", "url": yt})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "status": "added",
        "source": {"id": SRC_ID, "title": "Vid", "kind": "web_page", "status_label": "ready"},
    }
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, yt)


# --- Batch mode (urls=[...]) -------------------------------------------------


async def test_source_add_batch_all_success(mcp_call, mock_client) -> None:
    """A batch of valid URLs returns a per-item ``added`` list, in input order."""
    by_url = {
        "https://example.com/a": FakeSource(id=SRC_ID, title="A"),
        "https://example.com/b": FakeSource(id=SRC2_ID, title="B"),
    }
    mock_client.sources.add_url = AsyncMock(side_effect=lambda _nb, url: by_url[url])
    # FakeSource is a READY web_page, so each added item is routed through the
    # content-sanity helper (Task B) → mock get_fulltext with ample text so no thin
    # warning is added and the strict-dict-equality below holds.
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "urls": ["https://example.com/a", "https://example.com/b"]},
    )
    assert result.structured_content == {
        "status": "added",
        "notebook_id": NB_ID,
        "added": 2,
        "failed": 0,
        "results": [
            {
                "input": "https://example.com/a",
                "status": "added",
                "source_id": SRC_ID,
                "title": "A",
                "status_label": "ready",
            },
            {
                "input": "https://example.com/b",
                "status": "added",
                "source_id": SRC2_ID,
                "title": "B",
                "status_label": "ready",
            },
        ],
    }
    assert mock_client.sources.add_url.await_count == 2
    # Both ready web_page items were content-checked.
    assert mock_client.sources.get_fulltext.await_count == 2


async def test_source_add_batch_partial_failure(mcp_call, mock_client) -> None:
    """One bad URL does NOT abort the batch and is reported per-item, not collapsed."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Good"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "urls": ["https://good.example.com", "ftp://bad.example.com"]},
    )
    sc = result.structured_content
    assert sc["added"] == 1
    assert sc["failed"] == 1
    assert sc["results"][0] == {
        "input": "https://good.example.com",
        "status": "added",
        "source_id": SRC_ID,
        "title": "Good",
        "status_label": "ready",
    }
    bad = sc["results"][1]
    assert bad["input"] == "ftp://bad.example.com"
    assert bad["status"] == "error"
    assert bad["error"]["code"] == "VALIDATION"
    # The disallowed scheme is rejected by validate_url before reaching the client.
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, "https://good.example.com")
    # The one ready item was content-checked; the rejected entry never reaches it.
    mock_client.sources.get_fulltext.assert_awaited_once_with(NB_ID, SRC_ID, output_format="text")


async def test_source_add_batch_non_url_entry_errors_not_text(mcp_call, mock_client) -> None:
    """Non-URL entries error as VALIDATION — never silently added as text/file."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID))
    mock_client.sources.add_text = AsyncMock(return_value=FakeSource(id=SRC_ID))
    mock_client.sources.add_file = AsyncMock(return_value=FakeSource(id=SRC_ID))
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "urls": ["just some text", "/etc/hosts"]},
    )
    sc = result.structured_content
    assert sc["added"] == 0
    assert sc["failed"] == 2
    assert [item["status"] for item in sc["results"]] == ["error", "error"]
    assert all(item["error"]["code"] == "VALIDATION" for item in sc["results"])
    mock_client.sources.add_url.assert_not_called()
    mock_client.sources.add_text.assert_not_called()
    mock_client.sources.add_file.assert_not_called()


async def test_source_add_batch_fatal_error_aborts_the_call(mcp_call, mock_client) -> None:
    """A mid-batch FATAL failure (network/auth/rate-limit/5xx) aborts the whole tool
    call rather than being masked as a per-item error in a success envelope (#1871).
    The batch stops at the first fatal item — the second URL is never attempted."""
    mock_client.sources.add_url = AsyncMock(
        side_effect=[NetworkError("boom"), FakeSource(id=SRC2_ID, title="Second")]
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "urls": ["https://first.example.com", "https://second.example.com"],
            },
        )
    assert "NETWORK" in str(excinfo.value)
    # Aborted at the first fatal item — the batch did NOT continue to the second URL.
    assert mock_client.sources.add_url.await_count == 1


async def test_source_add_batch_isolates_non_fatal_input_error(mcp_call, mock_client) -> None:
    """A per-URL INPUT failure (a 404 / not-found — non-fatal) is isolated as an
    ``error`` item while the rest of the batch proceeds (the other half of #1871)."""
    mock_client.sources.add_url = AsyncMock(
        side_effect=[SourceNotFoundError("missing"), FakeSource(id=SRC2_ID, title="Second")]
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "urls": ["https://first.example.com", "https://second.example.com"]},
    )
    sc = result.structured_content
    assert sc["added"] == 1
    assert sc["failed"] == 1
    assert sc["results"][0]["status"] == "error"
    assert sc["results"][0]["error"] == tool_error_payload(SourceNotFoundError("missing"))
    assert sc["results"][1]["status"] == "added"
    assert sc["results"][1]["source_id"] == SRC2_ID
    # Non-fatal → the batch continued and attempted the second URL.
    assert mock_client.sources.add_url.await_count == 2


async def test_source_add_batch_over_cap_rejected_before_any_add(mcp_call, mock_client) -> None:
    """More than MAX_BATCH_URLS urls is rejected as VALIDATION BEFORE any add I/O —
    the cap is shared with the REST endpoint (_app.source_batch.MAX_BATCH_URLS)."""
    from notebooklm._app.source_batch import MAX_BATCH_URLS

    mock_client.sources.add_url = AsyncMock()
    urls = [f"https://{i}.example.com" for i in range(MAX_BATCH_URLS + 1)]
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_add", {"notebook": NB_ID, "urls": urls})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_awaited()


async def test_source_wait_over_id_cap_rejected(mcp_call, mock_client) -> None:
    """More than MAX_WAIT_SOURCE_IDS explicit refs is rejected (shared cap)."""
    from notebooklm._app.source_wait import MAX_WAIT_SOURCE_IDS

    refs = [f"src-{i}" for i in range(MAX_WAIT_SOURCE_IDS + 1)]
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "sources": refs})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_wait_over_timeout_cap_rejected(mcp_call, mock_client) -> None:
    """A timeout above MAX_WAIT_TIMEOUT is rejected (shared cap)."""
    from notebooklm._app.source_wait import MAX_WAIT_TIMEOUT

    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "timeout": MAX_WAIT_TIMEOUT + 1})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_add_batch_flags_failed_import(mcp_call, mock_client) -> None:
    """An added-but-errored source is reported status='added' with status_label
    'error' + an inline warning — the #1679 failure-signaling, per batch item."""
    mock_client.sources.add_url = AsyncMock(
        return_value=FakeFailedSource(id=SRC_ID, title="Broken")
    )
    result = await mcp_call(
        "source_add", {"notebook": NB_ID, "urls": ["https://broken.example.com"]}
    )
    sc = result.structured_content
    # The add CALL succeeded (row created) → status 'added'; the async import errored.
    assert sc["added"] == 1
    assert sc["failed"] == 0
    item = sc["results"][0]
    assert item["status"] == "added"
    assert item["status_label"] == "error"
    assert "Import failed" in item["warning"]


async def test_source_add_batch_propagates_cancellation(mock_client) -> None:
    """Per-item isolation must NOT swallow CancelledError (a BaseException)."""
    import asyncio

    from notebooklm.mcp.tools.sources import _add_url_batch

    mock_client.sources.add_url = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _add_url_batch(mock_client, NB_ID, ["https://example.com/a"], allow_internal=False)


async def test_source_add_batch_allow_internal_passthrough(mcp_call, mock_client) -> None:
    """``allow_internal`` is forwarded to every batch entry (and is not rejected)."""
    internal = "http://127.0.0.1:8080/x"
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Local"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "urls": [internal], "allow_internal": True},
    )
    sc = result.structured_content
    assert sc["added"] == 1
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, internal)
    # The ready item is content-checked like any other added web_page.
    mock_client.sources.get_fulltext.assert_awaited_once_with(NB_ID, SRC_ID, output_format="text")


async def test_source_add_batch_internal_rejected_without_allow_internal(
    mcp_call, mock_client
) -> None:
    """The same internal URL errors per-item (not raised) when allow_internal is off."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID))
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "urls": ["http://127.0.0.1:8080/x"]},
    )
    sc = result.structured_content
    assert sc["added"] == 0
    assert sc["results"][0]["status"] == "error"
    assert sc["results"][0]["error"]["code"] == "VALIDATION"
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_batch_empty_array_is_validation_error(mcp_call, mock_client) -> None:
    """An empty ``urls`` list is rejected BEFORE any notebook I/O (uses a name ref)."""
    mock_client.notebooks.list = AsyncMock(return_value=[])
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_add", {"notebook": "Some Notebook", "urls": []})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()
    # Mode validation runs before resolve_notebook, so a name is never looked up.
    mock_client.notebooks.list.assert_not_called()


async def test_source_add_batch_conflicts_with_source_type(mcp_call, mock_client) -> None:
    """``urls`` together with ``source_type`` is an ambiguous-mode VALIDATION error."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "source_type": "url", "urls": ["https://example.com/a"]},
        )
    assert "VALIDATION" in str(excinfo.value)


@pytest.mark.parametrize(
    "scalar",
    [
        {"url": "https://example.com/x"},
        {"text": "hi"},
        {"title": "nope"},
        {"path": "/tmp/x"},
        {"bytes_base64": "AAAA"},
        {"filename": "x.bin"},
        {"document_id": "drivefile123"},
        {"mime_type": "google-doc"},
    ],
)
async def test_source_add_batch_conflicts_with_scalar(mcp_call, mock_client, scalar) -> None:
    """ANY single-mode scalar supplied with ``urls`` is rejected (fail-closed)."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "urls": ["https://example.com/a"], **scalar},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_batch_conflicts_with_wait(mcp_call, mock_client) -> None:
    """``wait=True`` is single-mode only — pairing it with a ``urls`` batch is rejected
    before any add (#1890)."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "urls": ["https://example.com/a"], "wait": True},
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    assert "wait" in msg
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_missing_mode_is_validation_error(mcp_call, mock_client) -> None:
    """Neither ``source_type`` nor ``urls`` now fails in the body (source_type optional)."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_add", {"notebook": NB_ID})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_add_batch_youtube_accepted(mcp_call, mock_client) -> None:
    """A YouTube URL in the batch is accepted and added via add_url."""
    yt = "https://www.youtube.com/watch?v=abc123"
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Vid"))
    # FakeSource is a READY web_page → the Task B content-check fetches its body;
    # mock ample text so no thin warning is added and the strict equality holds
    # (without this the fetch hits a bare MagicMock and the swallowed error would let
    # the test pass for the wrong reason).
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_add", {"notebook": NB_ID, "urls": [yt]})
    sc = result.structured_content
    assert sc["added"] == 1
    assert sc["results"][0] == {
        "input": yt,
        "status": "added",
        "source_id": SRC_ID,
        "title": "Vid",
        "status_label": "ready",
    }
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, yt)
    mock_client.sources.get_fulltext.assert_awaited_once_with(NB_ID, SRC_ID, output_format="text")


# ---------------------------------------------------------------------------
# source_wait subset targeting (#1745)
# ---------------------------------------------------------------------------


async def test_source_wait_subset_ready(mcp_call, mock_client) -> None:
    """Subset mode: waits only on specified sources; returns aggregate outcomes."""
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {
            SRC_ID: FakeSource(id=SRC_ID, title="A"),
            SRC2_ID: FakeSource(id=SRC2_ID, title="B"),
        }
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    # Subset targeting with a list of IDs.
    result = await mcp_call("source_wait", {"notebook": NB_ID, "sources": [SRC_ID, SRC2_ID]})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert {row["id"] for row in sc["ready"]} == {SRC_ID, SRC2_ID}
    assert sc["timed_out"] == sc["failed"] == sc["not_found"] == []


async def test_source_wait_subset_comma_string(mcp_call, mock_client) -> None:
    """Subset mode: accepts comma-separated string, coerced to list."""
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {
            SRC_ID: FakeSource(id=SRC_ID, title="A"),
            SRC2_ID: FakeSource(id=SRC2_ID, title="B"),
        }
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "sources": f"{SRC_ID},{SRC2_ID}"})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert {row["id"] for row in sc["ready"]} == {SRC_ID, SRC2_ID}


async def test_source_wait_subset_partial_failures(mcp_call, mock_client) -> None:
    """Subset mode: wait on multiple sources with some failing/timing out."""
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {
            SRC_ID: FakeSource(id=SRC_ID, title="A"),
            SRC2_ID: SourceTimeoutError(SRC2_ID, 5.0),
        }
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "sources": [SRC_ID, SRC2_ID]})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is False
    assert [row["id"] for row in sc["ready"]] == [SRC_ID]
    assert [e["source_id"] for e in sc["timed_out"]] == [SRC2_ID]


async def test_source_wait_subset_unresolvable_raises(mcp_call, mock_client) -> None:
    """An unresolvable ref in subset raises NOT_FOUND before waiting."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Doc")])
    mock_client.sources.wait_all_until_ready = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "sources": [SRC_ID, "unresolvable_ref"]})
    assert "NOT_FOUND" in str(excinfo.value)
    mock_client.sources.wait_all_until_ready.assert_not_called()


async def test_source_wait_mutual_exclusion(mcp_call, mock_client) -> None:
    """Providing both source and sources raises a validation error."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID, "sources": [SRC2_ID]})
    assert "VALIDATION" in str(excinfo.value)
    assert "not both" in str(excinfo.value)


async def test_source_wait_empty_sources_raises(mcp_call, mock_client) -> None:
    """Providing an empty sources list/string raises a validation error."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": NB_ID, "sources": []})
    assert "VALIDATION" in str(excinfo.value)
    assert "was empty" in str(excinfo.value)


async def test_source_wait_subset_json_array_string(mcp_call, mock_client) -> None:
    """Subset mode: accepts a JSON-array string, coerced to a list (end-to-end)."""
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {
            SRC_ID: FakeSource(id=SRC_ID, title="A"),
            SRC2_ID: FakeSource(id=SRC2_ID, title="B"),
        }
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call(
        "source_wait", {"notebook": NB_ID, "sources": f'["{SRC_ID}","{SRC2_ID}"]'}
    )
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert sc["ok"] is True
    assert {row["id"] for row in sc["ready"]} == {SRC_ID, SRC2_ID}


async def test_source_wait_subset_dedupes(mcp_call, mock_client) -> None:
    """Subset mode: a repeated ref collapses to one ready row + one deduped poll id."""
    mock_client.sources.wait_all_until_ready = _dispatch_wait_all(
        {SRC_ID: FakeSource(id=SRC_ID, title="A")}
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "sources": [SRC_ID, SRC_ID]})
    sc = result.structured_content
    _assert_aggregate_shape(sc)
    assert [row["id"] for row in sc["ready"]] == [SRC_ID]
    # Deduped BEFORE waiting: the single snapshot loop is handed one id, not two.
    mock_client.sources.wait_all_until_ready.assert_awaited_once()
    assert mock_client.sources.wait_all_until_ready.await_args.args[1] == [SRC_ID]


async def test_source_wait_empty_sources_fails_before_notebook_lookup(
    mcp_call, mock_client
) -> None:
    """Empty-``sources`` is an input error that fires BEFORE any I/O.

    A bad notebook ref must NOT mask the VALIDATION error: the guard runs before
    ``resolve_notebook``, so ``client.notebooks.list`` is never awaited.
    """
    mock_client.notebooks.list = AsyncMock(side_effect=AssertionError("resolve_notebook ran"))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_wait", {"notebook": "no-such-notebook", "sources": []})
    assert "VALIDATION" in str(excinfo.value)
    assert "was empty" in str(excinfo.value)
    mock_client.notebooks.list.assert_not_awaited()


# ---------------------------------------------------------------------------
# source_list label filter (#1745)
# ---------------------------------------------------------------------------


async def test_source_list_label_filter(mcp_call, mock_client) -> None:
    """Filtering source list by label returns only member sources."""

    mock_client.labels = MagicMock()
    mock_client.labels.list = AsyncMock(
        return_value=[Label(id="lbl_work", name="Work", source_ids=[SRC_ID])]
    )
    mock_client.labels.sources = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="WorkDoc")])

    result = await mcp_call("source_list", {"notebook": NB_ID, "label": "Work"})
    sc = result.structured_content
    assert sc["notebook_id"] == NB_ID
    assert len(sc["sources"]) == 1
    assert sc["sources"][0]["id"] == SRC_ID
    assert sc["sources"][0]["title"] == "WorkDoc"
    mock_client.labels.sources.assert_awaited_once_with(NB_ID, "lbl_work")


async def test_source_list_label_and_status_filter(mcp_call, mock_client) -> None:
    """Label and status filters compose to further narrow the result."""

    mock_client.labels = MagicMock()
    mock_client.labels.list = AsyncMock(
        return_value=[Label(id="lbl_work", name="Work", source_ids=[SRC_ID, SRC2_ID])]
    )
    mock_client.labels.sources = AsyncMock(
        return_value=[
            FakeSource(id=SRC_ID, title="Ready Doc"),
            FakeFailedSource(id=SRC2_ID, title="Broken Doc"),
        ]
    )

    result = await mcp_call("source_list", {"notebook": NB_ID, "label": "Work", "status": "error"})
    sc = result.structured_content
    assert len(sc["sources"]) == 1
    assert sc["sources"][0]["id"] == SRC2_ID
    assert sc["sources"][0]["status_label"] == "error"


async def test_source_list_unknown_label_raises(mcp_call, mock_client) -> None:
    """An unknown label raises NOT_FOUND structured error."""
    mock_client.labels = MagicMock()
    mock_client.labels.list = AsyncMock(return_value=[])

    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_list", {"notebook": NB_ID, "label": "Unknown"})
    assert "No label found matching" in str(excinfo.value)


async def test_source_list_ambiguous_label_raises(mcp_call, mock_client) -> None:
    """An ambiguous label token raises AMBIGUOUS_* structured error."""

    mock_client.labels = MagicMock()
    mock_client.labels.list = AsyncMock(
        return_value=[
            Label(id="lbl_work1", name="Work", source_ids=[]),
            Label(id="lbl_work2", name="Work", source_ids=[]),
        ]
    )

    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_list", {"notebook": NB_ID, "label": "Work"})
    assert "matches 2 labels" in str(excinfo.value)
    assert "Use a label id instead" in str(excinfo.value)


# ---------------------------------------------------------------------------
# source_add(wait=True) — single-mode add + source_wait folded into one call
# (#1890, was source_add_and_wait). Returns the source_wait aggregate PLUS a
# top-level ``source_id`` (always present, since the source persists even when
# the wait fails / times out).
# ---------------------------------------------------------------------------


async def test_source_add_wait_url_ready(mcp_call, mock_client) -> None:
    """URL add then wait → ready aggregate carrying the resolved source_id."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Page"))
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Page")
    )
    # FakeSource is a READY web_page → thin-content check fetches its body; ample
    # content means NO warning is added (so the ready row asserts exactly).
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="x" * 500, char_count=500)
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "wait": True, "source_type": "url", "url": "https://example.com/a"},
    )
    sc = result.structured_content
    assert set(sc) == _AGGREGATE_KEYS | {"source_id"}
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is True
    assert sc["ready"] == [
        {"id": SRC_ID, "title": "Page", "kind": "web_page", "status_label": "ready"}
    ]
    assert sc["timed_out"] == sc["failed"] == sc["not_found"] == []
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, "https://example.com/a")
    # The wait polls the id returned by the add.
    assert mock_client.sources.wait_until_ready.await_args.args[1] == SRC_ID


async def test_source_add_wait_text_ready(mcp_call, mock_client) -> None:
    """Text add then wait → ready; a pasted-text source is never thin-checked."""
    mock_client.sources.add_text = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Notes")
    )
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Notes")
    )
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "wait": True,
            "source_type": "text",
            "text": "hello world",
            "title": "Notes",
        },
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is True
    assert sc["ready"] == [
        {"id": SRC_ID, "title": "Notes", "kind": "pasted_text", "status_label": "ready"}
    ]
    mock_client.sources.add_text.assert_awaited_once_with(NB_ID, "Notes", "hello world")
    mock_client.sources.get_fulltext.assert_not_called()


async def test_source_add_wait_drive_ready(mcp_call, mock_client) -> None:
    """Drive add then wait → ready aggregate + source_id (no drive provenance keys)."""
    mock_client.sources.add_drive = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Sheet")
    )
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Sheet")
    )
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "wait": True,
            "source_type": "drive",
            "document_id": "drivefile123",
            "mime_type": "google-sheets",
        },
    )
    sc = result.structured_content
    assert set(sc) == _AGGREGATE_KEYS | {"source_id"}
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is True
    mock_client.sources.add_drive.assert_awaited_once()


async def test_source_add_wait_drive_pdf_kind_not_spreadsheet(mcp_call, mock_client) -> None:
    """The ready aggregate labels a Drive PDF as ``pdf``, not ``google_spreadsheet``.

    ``wait_until_ready`` re-reads the source from GET_NOTEBOOK, where a Drive PDF
    again decodes to the ambiguous type code 14 (→ GOOGLE_SPREADSHEET). The declared
    ``mime_type='pdf'`` must still win on the final waited/aggregated source (#1828),
    just as it does for plain ``source_add``.
    """
    # add_drive returns the raw ambiguous code; the core re-stamps it to PDF.
    mock_client.sources.add_drive = AsyncMock(
        return_value=Source(id=SRC_ID, title="Report.pdf", _type_code=14)
    )
    # wait_until_ready re-reads the source and again gets the ambiguous code 14.
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=Source(id=SRC_ID, title="Report.pdf", _type_code=14)
    )
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "wait": True,
            "source_type": "drive",
            "document_id": "drivefile123",
            "mime_type": "pdf",
        },
    )
    sc = result.structured_content
    assert sc["ok"] is True
    (ready_row,) = sc["ready"]
    assert ready_row["kind"] == "pdf"
    assert ready_row["kind"] != "google_spreadsheet"


async def test_source_add_wait_stdio_file_ready(mcp_call, mock_client, tmp_path) -> None:
    """A stdio (local-path) file add-and-wait reaches READY — pins the file branch."""
    doc = tmp_path / "doc.pdf"
    doc.write_text("hello")
    mock_client.sources.add_file = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="doc.pdf")
    )
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="doc.pdf")
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "wait": True, "source_type": "file", "path": str(doc)},
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is True
    mock_client.sources.add_file.assert_awaited_once()


async def test_source_add_wait_import_failure_lands_in_failed(mcp_call, mock_client) -> None:
    """An add that processes to ERROR → wait raises SourceProcessingError → ``failed``
    bucket, ``ok`` False, and the source_id is still surfaced so the caller can delete it."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Bad"))
    mock_client.sources.wait_until_ready = AsyncMock(side_effect=SourceProcessingError(SRC_ID))
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "wait": True, "source_type": "url", "url": "https://example.com/bad"},
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is False
    assert sc["ready"] == []
    assert sc["failed"] == [{"source_id": SRC_ID, "error": str(SourceProcessingError(SRC_ID))}]


async def test_source_add_wait_timeout_lands_in_timed_out(mcp_call, mock_client) -> None:
    """A slow import → wait raises SourceTimeoutError → ``timed_out`` bucket + source_id."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Slow"))
    mock_client.sources.wait_until_ready = AsyncMock(side_effect=SourceTimeoutError(SRC_ID, 5.0))
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "wait": True,
            "source_type": "url",
            "url": "https://example.com/slow",
            "timeout": 5,
        },
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is False
    assert sc["timed_out"] == [{"source_id": SRC_ID, "error": str(SourceTimeoutError(SRC_ID, 5.0))}]


async def test_source_add_wait_thin_web_page_warns(mcp_call, mock_client) -> None:
    """A READY web page with thin text carries the same non-blocking warning source_wait adds."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Dead"))
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Dead")
    )
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="", char_count=_THIN_SOURCE_CHAR_THRESHOLD - 1)
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "wait": True, "source_type": "url", "url": "https://example.com/dead"},
    )
    sc = result.structured_content
    assert sc["ok"] is True
    assert "warning" in sc["ready"][0]


async def test_source_add_wait_remote_file_rejected(mcp_call, mock_client, monkeypatch) -> None:
    """A remote (http) file add-and-wait is rejected up front — the upload is a
    separate step, so there is no source to wait on. No add call is made."""
    import notebooklm.mcp.tools.sources as sources_mod

    monkeypatch.setattr(sources_mod, "_is_http_transport", lambda: True)
    mock_client.sources.add_file = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "wait": True, "source_type": "file", "path": "/tmp/doc.pdf"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_file.assert_not_called()


async def test_source_add_wait_negative_timeout_is_validation_error(mcp_call, mock_client) -> None:
    """A negative timeout is rejected before any add/wait I/O."""
    mock_client.sources.add_url = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "wait": True,
                "source_type": "url",
                "url": "https://x.example",
                "timeout": -1,
            },
        )
    assert "timeout" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_wait_zero_interval_is_validation_error(mcp_call, mock_client) -> None:
    """A zero interval is rejected before any add/wait I/O."""
    mock_client.sources.add_url = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "wait": True,
                "source_type": "url",
                "url": "https://x.example",
                "interval": 0,
            },
        )
    assert "interval" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_wait_rejects_foreign_content_scalar(mcp_call, mock_client) -> None:
    """A content scalar the source_type does not consume is rejected (mirrors source_add)."""
    mock_client.sources.add_url = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "wait": True,
                "source_type": "url",
                "url": "https://example.com/a",
                "text": "smuggled",
            },
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_wait_drive_missing_document_id(mcp_call, mock_client) -> None:
    """drive add-and-wait with no document_id is a VALIDATION error, no add call."""
    mock_client.sources.add_drive = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_add", {"notebook": NB_ID, "wait": True, "source_type": "drive"})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_drive.assert_not_called()


async def test_source_add_wait_allow_internal_passthrough(mcp_call, mock_client) -> None:
    """allow_internal lets an internal-host URL through the add SSRF guard, then waits."""
    internal = "http://127.0.0.1:8080/x"
    mock_client.sources.add_url = AsyncMock(return_value=FakeReadyTextSource(id=SRC_ID))
    mock_client.sources.wait_until_ready = AsyncMock(return_value=FakeReadyTextSource(id=SRC_ID))
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "wait": True,
            "source_type": "url",
            "url": internal,
            "allow_internal": True,
        },
    )
    assert result.structured_content["ok"] is True
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, internal)


async def test_source_add_wait_internal_rejected_without_allow_internal(
    mcp_call, mock_client
) -> None:
    """Without allow_internal the SSRF guard rejects an internal-host URL before any add."""
    mock_client.sources.add_url = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "wait": True,
                "source_type": "url",
                "url": "http://127.0.0.1:8080/x",
            },
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_wait_youtube_ready(mcp_call, mock_client) -> None:
    """A youtube add-and-wait dispatches through the YouTube-host guard, then waits."""
    yt = "https://www.youtube.com/watch?v=abc"
    mock_client.sources.add_url = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Vid")
    )
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="Vid")
    )
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "wait": True, "source_type": "youtube", "url": yt},
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is True
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, yt)


async def test_source_add_wait_youtube_rejects_non_youtube_url(mcp_call, mock_client) -> None:
    """source_type='youtube' with a non-YouTube URL is a VALIDATION error, no add."""
    mock_client.sources.add_url = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "wait": True,
                "source_type": "youtube",
                "url": "https://example.com/x",
            },
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_wait_not_found_bucket(mcp_call, mock_client) -> None:
    """A wait that raises SourceNotFoundError → ``not_found`` bucket + top-level source_id."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Ghost"))
    mock_client.sources.wait_until_ready = AsyncMock(side_effect=SourceNotFoundError(SRC_ID))
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "wait": True, "source_type": "url", "url": "https://example.com/ghost"},
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is False
    assert sc["not_found"] == [{"source_id": SRC_ID, "error": f"Source not found: {SRC_ID}"}]


async def test_source_add_wait_add_failure_raises_no_bucket(mcp_call, mock_client) -> None:
    """When the ADD itself fails (e.g. an RPCError), no source exists to wait on: the
    error flows through mcp_errors() as a tool error — it does NOT land in a wait
    bucket, and no source_id is returned. The wait is never reached."""
    mock_client.sources.add_url = AsyncMock(side_effect=RPCError("boom"))
    mock_client.sources.wait_until_ready = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "wait": True, "source_type": "url", "url": "https://example.com/a"},
        )
    mock_client.sources.wait_until_ready.assert_not_called()


async def test_source_add_wait_bytes_composes_add_and_wait(mcp_call, mock_client) -> None:
    """The two folded modes compose: an in-channel bytes file-add with ``wait=True``
    spools the bytes, adds them, then waits to READY → the source_wait aggregate with a
    top-level ``source_id`` (#1890)."""
    import base64

    mock_client.sources.add_file = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="doc.pdf")
    )
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeReadyTextSource(id=SRC_ID, title="doc.pdf")
    )
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "file",
            "bytes_base64": base64.b64encode(b"%PDF-1.4 hi").decode(),
            "filename": "doc.pdf",
            "wait": True,
        },
    )
    sc = result.structured_content
    assert sc["source_id"] == SRC_ID
    assert sc["ok"] is True
    mock_client.sources.add_file.assert_awaited_once()
    mock_client.sources.wait_until_ready.assert_awaited_once()
