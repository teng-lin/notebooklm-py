"""Unit tests for the private source listing service."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.sources import WebSourcesAPI
from notebooklm._web.sources.listing import SourceLister
from notebooklm.exceptions import RPCError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import Source, SourceType


class RecordingRpc:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[RPCMethod, list[Any], str | None]] = []

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        self.calls.append((method, params, source_path))
        return self.response


def source_entry(
    source_id: str,
    *,
    title: str = "Source",
    metadata: list[Any] | None = None,
    status: list[Any] | None = None,
) -> list[Any]:
    return [
        [source_id],
        title,
        metadata or [None, 11, [1704067200, 0], None, 5],
        status or [None, 2],
    ]


@pytest.mark.asyncio
async def test_list_uses_exact_get_notebook_rpc_shape() -> None:
    rpc = RecordingRpc([["Notebook", []]])
    lister = SourceLister(rpc)

    assert await lister.list("nb_123") == []

    assert rpc.calls == [
        (
            RPCMethod.GET_NOTEBOOK,
            # #1549: GET_NOTEBOOK tail migrated to the nested template block.
            [
                "nb_123",
                None,
                [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
                None,
                0,
            ],
            "/notebook/nb_123",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "Empty or invalid notebook response"),
        ([], "Empty or invalid notebook response"),
        (["notebook"], "Unexpected notebook structure"),
        ([["Notebook"]], "Unexpected notebook structure"),
        ([["Notebook", "not-a-list"]], "Sources data for nb_123 is not a list"),
    ],
)
async def test_malformed_payloads_log_and_raise(
    payload: Any,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Strict decoding is the only mode (the ``NOTEBOOKLM_STRICT_DECODE=0``
    # soft-mode opt-out was retired in v0.7.0): a malformed payload always logs
    # the diagnostic warning AND raises, rather than silently returning ``[]``
    # (issue #1159).
    lister = SourceLister(RecordingRpc(payload))
    caplog.set_level("WARNING", logger="notebooklm._sources")

    with pytest.raises(RPCError):
        await lister.list("nb_123")

    assert message in caplog.text
    assert caplog.records[0].name == "notebooklm._sources"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "API response structure changed"),
        ([], "API response structure changed"),
        (["notebook"], "API response structure changed"),
        ([["Notebook"]], "API response structure changed"),
        ([["Notebook", "not-a-list"]], "sources data is str, not list"),
    ],
)
async def test_strict_mode_raises_rpc_error_for_malformed_payloads(
    payload: Any,
    message: str,
) -> None:
    lister = SourceLister(RecordingRpc(payload))

    with pytest.raises(RPCError, match=message):
        await lister.list("nb_123", strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "API response structure changed"),
        ([], "API response structure changed"),
        (["notebook"], "API response structure changed"),
        ([["Notebook"]], "API response structure changed"),
        ([["Notebook", "not-a-list"]], "sources data is str, not list"),
    ],
)
async def test_malformed_payloads_raise_by_default_under_strict_decode(
    payload: Any,
    message: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for issue #1159: with strict-decode at its default (ON), a
    # malformed/error-shaped response must raise rather than silently return
    # an empty list, even without an explicit ``strict=True``.
    monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
    lister = SourceLister(RecordingRpc(payload))
    caplog.set_level("WARNING", logger="notebooklm._sources")

    with pytest.raises(RPCError, match=message):
        await lister.list("nb_123")

    # The drift WARNING on the historical "SourcesAPI.list:" prefix must still
    # fire in strict mode so log-based monitoring keeps its breadcrumb.
    assert "SourcesAPI.list:" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_null_sources_slot_is_empty_notebook_not_malformed(
    strict: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuinely empty notebook elides the sources slot as ``None`` (see
    # tests/cassettes/web/notebook_zero_sources.yaml). Per issue #1159 this is a
    # valid empty state and must return ``[]`` regardless of strict mode,
    # never raising or warning.
    monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
    lister = SourceLister(
        RecordingRpc([["Notebook Title", None, "nb_123", "", None, [1, False], None, None, None]])
    )

    assert await lister.list("nb_123", strict=strict) == []


@pytest.mark.asyncio
async def test_url_precedence_uses_index_7_then_5_and_ignores_bare_http() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry(
                            "src_both",
                            metadata=[
                                "https://bare.example/ignored",
                                11,
                                [1704067200, 0],
                                None,
                                5,
                                ["https://index-5.example/source"],
                                None,
                                ["https://index-7.example/source"],
                            ],
                        ),
                        source_entry(
                            "src_index_5",
                            metadata=[
                                "https://bare.example/ignored",
                                11,
                                [1704067200, 0],
                                None,
                                9,
                                ["https://youtube.example/watch?v=abc"],
                                None,
                                None,
                            ],
                        ),
                        source_entry(
                            "src_bare",
                            metadata=[
                                "https://bare.example/ignored",
                                11,
                                [1704067200, 0],
                                None,
                                5,
                                None,
                                None,
                                None,
                            ],
                        ),
                    ],
                ]
            ]
        )
    )

    sources = await lister.list("nb_123")

    assert [source.url for source in sources] == [
        "https://index-7.example/source",
        "https://youtube.example/watch?v=abc",
        None,
    ]


@pytest.mark.asyncio
async def test_status_and_type_code_parsing() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_processing", status=[None, SourceStatus.PROCESSING]),
                        source_entry("src_unknown_status", status=[None, 999]),
                        source_entry(
                            "src_non_int_type",
                            metadata=[None, 11, [1704067200, 0], None, "5"],
                        ),
                    ],
                ]
            ]
        )
    )

    sources = await lister.list("nb_123")

    assert sources[0].status == SourceStatus.PROCESSING
    assert sources[0]._type_code == 5
    assert sources[1].status == SourceStatus.UNKNOWN
    assert sources[1].is_ready is False
    assert sources[2]._type_code is None


@pytest.mark.asyncio
async def test_list_filters_with_or_within_axes_and_and_across_axes() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry(
                            "src_pdf_ready",
                            title="PDF ready",
                            metadata=[None, 11, [1704067200, 0], None, 3],
                            status=[None, SourceStatus.READY],
                        ),
                        source_entry(
                            "src_pdf_error",
                            title="PDF error",
                            metadata=[None, 11, [1704067200, 0], None, 3],
                            status=[None, SourceStatus.ERROR],
                        ),
                        source_entry(
                            "src_web_ready",
                            title="Web ready",
                            metadata=[None, 11, [1704067200, 0], None, 5],
                            status=[None, SourceStatus.READY],
                        ),
                        source_entry(
                            "src_youtube_processing",
                            title="YouTube processing",
                            metadata=[None, 11, [1704067200, 0], None, 9],
                            status=[None, SourceStatus.PROCESSING],
                        ),
                    ],
                ]
            ]
        )
    )

    sources = await lister.list(
        "nb_123",
        statuses={SourceStatus.READY, SourceStatus.ERROR},
        types={SourceType.PDF, SourceType.WEB_PAGE},
    )

    assert [source.id for source in sources] == [
        "src_pdf_ready",
        "src_pdf_error",
        "src_web_ready",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"statuses": SourceStatus.READY}, "statuses must be a collection"),
        ({"statuses": {2}}, "statuses must contain only SourceStatus"),
        ({"types": SourceType.PDF}, "types must be a collection"),
        ({"types": {"pdf"}}, "types must contain only SourceType"),
    ],
)
async def test_list_rejects_untyped_filters_before_rpc(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    rpc = RecordingRpc([["Notebook", []]])
    lister = SourceLister(rpc)

    with pytest.raises(TypeError, match=message):
        await lister.list("nb_123", **kwargs)

    assert rpc.calls == []


@pytest.mark.asyncio
async def test_explicit_empty_filter_matches_no_sources() -> None:
    lister = SourceLister(
        RecordingRpc([["Notebook", [source_entry("src_1"), source_entry("src_2")]]])
    )

    assert await lister.list("nb_123", statuses=set()) == []


@pytest.mark.asyncio
async def test_sources_api_list_delegates_strict_and_filters() -> None:
    api = WebSourcesAPI(MagicMock(), supervisor=MagicMock(), uploader=MagicMock())
    expected = [Source(id="src_1", _type_code=3, status=SourceStatus.READY)]
    api._lister.list = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await api.list(
        "nb_123",
        strict=True,
        statuses={SourceStatus.READY},
        types={SourceType.PDF},
    )

    assert result is expected
    api._lister.list.assert_awaited_once_with(
        "nb_123",
        strict=True,
        statuses={SourceStatus.READY},
        types={SourceType.PDF},
    )


@pytest.mark.asyncio
async def test_nested_drive_source_id_is_extracted() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        [
                            [None, True, ["drive_src"]],
                            "Drive Source",
                            [None, 11, [1704067200, 0], None, 2],
                            [None, 2],
                        ],
                    ],
                ]
            ]
        )
    )

    sources = await lister.list("nb_123")

    assert len(sources) == 1
    assert sources[0].id == "drive_src"
    assert sources[0].title == "Drive Source"


@pytest.mark.asyncio
async def test_malformed_source_id_shape_logs_and_skips(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_valid", title="Valid"),
                        [
                            [None, True, []],
                            "Broken",
                            [None, 11, [1704067200, 0], None, 2],
                            [None, 2],
                        ],
                    ],
                ]
            ]
        )
    )
    caplog.set_level("WARNING", logger="notebooklm._sources")

    sources = await lister.list("nb_123")

    assert [source.id for source in sources] == ["src_valid"]
    assert "Skipping source with unexpected id shape" in caplog.text
    assert "[None, True, []]" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_row",
    [None, "not-a-row", []],
)
async def test_strict_list_rejects_malformed_source_rows(bad_row: Any) -> None:
    lister = SourceLister(RecordingRpc([["Notebook", [source_entry("src_valid"), bad_row]]]))

    with pytest.raises(RPCError, match="malformed source row at index 1"):
        await lister.list("nb_123", strict=True)


@pytest.mark.asyncio
async def test_strict_list_rejects_source_without_usable_id() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_valid"),
                        [[None, True, []], "Broken", None, [None, 2]],
                    ],
                ]
            ]
        )
    )

    with pytest.raises(RPCError, match="source row at index 1 has no usable id"):
        await lister.list("nb_123", strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_row", "detail"),
    [
        ([["src_incomplete"], "Title"], "metadata block is missing"),
        (
            [["src_incomplete"], "Title", [None, 11, [1704067200, 0], None]],
            "metadata type code is missing",
        ),
        (
            [["src_incomplete"], "Title", [None, 11, [1704067200, 0], None, "5"]],
            "metadata type code is malformed",
        ),
        (
            [["src_incomplete"], "Title", [None, 11, [1704067200, 0], None, 5]],
            "status block is missing",
        ),
        (
            [["src_incomplete"], "Title", [None, 11, [1704067200, 0], None, 5], []],
            "status code is missing",
        ),
        (
            [
                ["src_incomplete"],
                "Title",
                [None, 11, [1704067200, 0], None, 5],
                [None, "2"],
            ],
            "status code is malformed",
        ),
    ],
)
async def test_strict_list_rejects_incomplete_id_bearing_rows(
    bad_row: list[Any],
    detail: str,
) -> None:
    lister = SourceLister(RecordingRpc([["Notebook", [bad_row]]]))

    with pytest.raises(RPCError, match=rf"incomplete source row at index 0 \({detail}"):
        await lister.list("nb_123", strict=True)


@pytest.mark.asyncio
async def test_tolerant_list_keeps_incomplete_id_bearing_row_as_unknown() -> None:
    row = [["src_incomplete"], "Title"]
    lister = SourceLister(RecordingRpc([["Notebook", [row]]]))

    sources = await lister.list("nb_123")

    assert len(sources) == 1
    assert sources[0].id == "src_incomplete"
    assert sources[0].status is SourceStatus.UNKNOWN
    assert sources[0].kind is SourceType.UNKNOWN


@pytest.mark.asyncio
async def test_list_dedups_duplicate_ids_keeping_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #1919: research imports (and ghost/probe rows) can leave the same
    # id-bearing source in ``nb_info[1]`` more than once. The enumeration must
    # dedup by resolved id, keeping the first occurrence, so ``source_list`` /
    # ``metadata.sources`` never over-count.
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_dup", title="First"),
                        source_entry("src_unique", title="Unique"),
                        source_entry("src_dup", title="Second"),
                    ],
                ]
            ]
        )
    )
    caplog.set_level("DEBUG", logger="notebooklm._sources")

    sources = await lister.list("nb_123")

    assert [source.id for source in sources] == ["src_dup", "src_unique"]
    # The FIRST occurrence wins (later duplicate dropped).
    assert sources[0].title == "First"
    assert "duplicate source id" in caplog.text


@pytest.mark.asyncio
async def test_strict_list_rejects_conflicting_duplicate_ids() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_dup", title="First"),
                        source_entry("src_dup", title="Second"),
                    ],
                ]
            ]
        )
    )

    with pytest.raises(RPCError, match="conflicting duplicate source row at index 1"):
        await lister.list("nb_123", strict=True)


@pytest.mark.asyncio
async def test_strict_list_dedups_identical_rows_for_exact_unique_count() -> None:
    row = source_entry("src_dup", title="Same")
    lister = SourceLister(RecordingRpc([["Notebook", [row, row]]]))

    sources = await lister.list("nb_123", strict=True)

    assert len(sources) == 1
    assert sources[0].id == "src_dup"


@pytest.mark.asyncio
async def test_created_at_uses_shared_timestamp_parser() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry(
                            "src_timestamp",
                            metadata=[None, 11, [1704067200, 123], None, 5],
                        ),
                        source_entry(
                            "src_bad_timestamp",
                            metadata=[None, 11, [None], None, 5],
                        ),
                    ],
                ]
            ]
        )
    )

    sources = await lister.list("nb_123")

    assert sources[0].created_at is not None
    assert int(sources[0].created_at.timestamp()) == 1704067200
    assert sources[1].created_at is None


@pytest.mark.asyncio
async def test_list_preserves_download_and_revision_metadata() -> None:
    metadata: list[Any] = [None] * 15
    metadata[1] = 4048
    metadata[2] = [1704067200, 0]
    metadata[3] = ["revision-id", [1704067300, 0]]
    metadata[4] = 8
    metadata[14] = [1704067400, 0]
    entry = source_entry("src_enriched", metadata=metadata)
    entry.extend(
        [
            None,
            "https://contribution.usercontent.google.com/download?c=token",
            "https://drive.google.com/viewer/upload?ds=token",
            ["blobref", None, "text/markdown"],
        ]
    )
    lister = SourceLister(RecordingRpc([["Notebook", [entry]]]))

    [source] = await lister.list("nb_123")

    assert source.download_url == "https://contribution.usercontent.google.com/download?c=token"
    assert source.viewer_url == "https://drive.google.com/viewer/upload?ds=token"
    assert source.content_mime == "text/markdown"
    assert source.word_count == 4048
    assert source.revision_id == "revision-id"
    assert source.revision_timestamp is not None
    assert int(source.revision_timestamp.timestamp()) == 1704067300
    assert source.last_modified_at is not None
    assert int(source.last_modified_at.timestamp()) == 1704067400


@pytest.mark.asyncio
async def test_get_filters_list_results() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_1", title="One"),
                        source_entry("src_2", title="Two"),
                    ],
                ]
            ]
        )
    )

    source = await lister.get("nb_123", "src_2")

    assert source is not None
    assert source.id == "src_2"
    assert source.title == "Two"


@pytest.mark.asyncio
async def test_get_returns_none_when_source_not_found() -> None:
    lister = SourceLister(
        RecordingRpc(
            [
                [
                    "Notebook",
                    [
                        source_entry("src_1", title="One"),
                        source_entry("src_2", title="Two"),
                    ],
                ]
            ]
        )
    )

    assert await lister.get("nb_123", "missing") is None


@pytest.mark.asyncio
async def test_get_can_use_late_bound_list_hook() -> None:
    lister = SourceLister(RecordingRpc([["Notebook", []]]))
    calls: list[str] = []

    async def list_sources(notebook_id: str):
        calls.append(notebook_id)
        return [
            Source(id="src_1", title="One"),
            Source(id="src_2", title="Two"),
        ]

    source = await lister.get("nb_123", "src_2", list_sources=list_sources)

    assert calls == ["nb_123"]
    assert source is not None
    assert source.id == "src_2"
