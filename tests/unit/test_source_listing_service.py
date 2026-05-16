"""Unit tests for the private source listing service."""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._source_listing import SourceLister
from notebooklm.exceptions import RPCError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import Source


class RecordingRpc:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[RPCMethod, list[Any], str | None]] = []

    async def __call__(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
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
            ["nb_123", None, [2], None, 0],
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
async def test_malformed_payloads_log_and_return_empty(
    payload: Any,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lister = SourceLister(RecordingRpc(payload))
    caplog.set_level("WARNING", logger="notebooklm._sources")

    sources = await lister.list("nb_123")

    assert sources == []
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
        ([["Notebook", None]], "sources data is NoneType, not list"),
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
    assert sources[1].status == SourceStatus.READY
    assert sources[2]._type_code is None


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
