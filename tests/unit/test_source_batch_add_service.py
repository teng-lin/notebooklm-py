"""True-batch ADD_SOURCE semantics for the existing MCP/REST batch endpoints."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._web.rows.sources import unwrap_add_source_rows
from notebooklm._web.sources.batch import SourceBatchAddService
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceAddError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import Source


def _extract_youtube_video_id(_url: str) -> None:
    return None


def _url_row(
    source_id: str, url: str, *, status: SourceStatus = SourceStatus.PROCESSING
) -> list[Any]:
    metadata: list[Any] = [None, None, None, None, 5, None, None, [url]]
    return [[source_id], url, metadata, [None, status.value]]


def _deep_bare_url_payload(source_id: str, url: str) -> list[Any]:
    metadata: list[Any] = [url, None, None, None, 5]
    return [[[[source_id], url, metadata, [None, SourceStatus.PROCESSING.value]]]]


class RecordingRpc:
    def __init__(self, result: Any = None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append({"method": method, "params": params, **kwargs})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    "payload",
    [
        [["src-a"], ["src-b"]],
        [[["src-a"], "A"], [["src-b"], "B"]],
        [[["src-a"], "A"], [[["src-b"], "B"]]],
        [[[["src-a"], "A"]], [[["src-b"], "B"]]],
        [[[["src-a"], "A"], [["src-b"], "B"]]],
    ],
)
def test_unwrap_add_source_rows_accepts_known_repeated_envelopes(payload: Any) -> None:
    assert [Source.from_api_response(row).id for row in unwrap_add_source_rows(payload)] == [
        "src-a",
        "src-b",
    ]


@pytest.mark.asyncio
async def test_true_batch_uses_one_add_sources_rpc_and_preserves_order() -> None:
    urls = ["https://a.example.com", "https://b.example.com"]
    rpc = RecordingRpc([_url_row("src-a", urls[0]), _url_row("src-b", urls[1])])
    list_sources = AsyncMock()

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=list_sources,
        extract_youtube_video_id=_extract_youtube_video_id,
        logger=logging.getLogger(__name__),
    )

    assert [item.url for item in outcomes] == urls
    assert [item.source.id if item.source else None for item in outcomes] == ["src-a", "src-b"]
    assert [item.error for item in outcomes] == [None, None]
    assert rpc.calls == [
        {
            "method": RPCMethod.ADD_SOURCE,
            "params": [
                [
                    [None, None, [urls[0]], None, None, None, None, None, None, None, 1],
                    [None, None, [urls[1]], None, None, None, None, None, None, None, 1],
                ],
                "nb-1",
                [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            ],
            "source_path": "/notebook/nb-1",
            "allow_null": False,
            "disable_internal_retries": True,
            "operation_variant": "url",
        }
    ]
    list_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_true_batch_preserves_mixed_web_page_and_youtube_wire_shapes() -> None:
    web_url = "https://example.com/article"
    youtube_url = "https://www.youtube.com/watch?v=abc123"
    rpc = RecordingRpc([_url_row("src-web", web_url), _url_row("src-yt", youtube_url)])

    await SourceBatchAddService().add_urls(
        "nb-1",
        [web_url, youtube_url],
        rpc=rpc,
        list_sources=AsyncMock(),
        extract_youtube_video_id=lambda url: "abc123" if url == youtube_url else None,
        logger=logging.getLogger(__name__),
    )

    specs = rpc.calls[0]["params"][0]
    assert specs == [
        [None, None, [web_url], None, None, None, None, None, None, None, 1],
        [None, None, None, None, None, None, None, [youtube_url], None, None, 1],
    ]


@pytest.mark.asyncio
async def test_complete_legacy_short_rows_are_zipped_positionally() -> None:
    urls = ["https://a.example.com", "https://b.example.com"]
    rpc = RecordingRpc([["src-a"], ["src-b"]])
    list_sources = AsyncMock()

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=list_sources,
        extract_youtube_video_id=_extract_youtube_video_id,
        logger=logging.getLogger(__name__),
    )

    assert [item.url for item in outcomes] == urls
    assert [item.source.id if item.source else None for item in outcomes] == ["src-a", "src-b"]
    list_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_canonicalized_urls_are_zipped_in_documented_response_order() -> None:
    urls = ["https://youtu.be/abc123", "https://example.com"]
    rpc = RecordingRpc(
        [
            _url_row("src-yt", "https://www.youtube.com/watch?v=abc123"),
            _url_row("src-web", "https://example.com/"),
        ]
    )

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=AsyncMock(),
        extract_youtube_video_id=lambda url: (
            "abc123" if "youtu.be/abc123" in url or "v=abc123" in url else None
        ),
        logger=logging.getLogger(__name__),
    )

    assert [item.url for item in outcomes] == urls
    assert [item.source.id if item.source else None for item in outcomes] == [
        "src-yt",
        "src-web",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_urls",
    [
        ["https://a.example.com", "https://other.example.com"],
        ["https://a.example.com", "https://a.example.com"],
    ],
)
async def test_complete_response_with_wrong_positional_identity_fails_closed(
    response_urls: list[str],
) -> None:
    urls = ["https://a.example.com", "https://b.example.com"]
    rpc = RecordingRpc(
        [
            _url_row("src-first", response_urls[0]),
            _url_row("src-second", response_urls[1]),
        ]
    )

    with pytest.raises(SourceAddError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            urls,
            rpc=rpc,
            list_sources=AsyncMock(),
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "did not match request order" in str(raised.value)


@pytest.mark.asyncio
async def test_sparse_deep_row_preserves_bare_metadata_url_for_attribution() -> None:
    urls = ["https://good.example.com", "https://bad.example.com"]
    rpc = RecordingRpc(_deep_bare_url_payload("src-good", urls[0]))

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=AsyncMock(return_value=[]),
        extract_youtube_video_id=_extract_youtube_video_id,
        logger=logging.getLogger(__name__),
    )

    assert outcomes[0].source is not None
    assert outcomes[0].source.id == "src-good"
    assert outcomes[0].source.url == urls[0]
    assert isinstance(outcomes[1].error, SourceAddError)


@pytest.mark.asyncio
async def test_sparse_canonical_youtube_url_matches_requested_video_identity() -> None:
    urls = ["https://youtu.be/abc123", "https://bad.example.com"]
    rpc = RecordingRpc([_url_row("src-yt", "https://www.youtube.com/watch?v=abc123")])

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=AsyncMock(return_value=[]),
        extract_youtube_video_id=lambda url: (
            "abc123" if "youtu.be/abc123" in url or "v=abc123" in url else None
        ),
        logger=logging.getLogger(__name__),
    )

    assert outcomes[0].source is not None
    assert outcomes[0].source.id == "src-yt"
    assert isinstance(outcomes[1].error, SourceAddError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_url", "response_url"),
    [
        (
            "http://[2001:db8::1]:80/article",
            "http://[2001:0DB8:0000:0000:0000:0000:0000:0001]/article",
        ),
        (
            "https://user%2fname:pa%3ass@example.com/item%2fpart?q=%ab",
            "https://user%2Fname:pa%3Ass@example.com/item%2Fpart?q=%AB",
        ),
    ],
)
async def test_sparse_equivalent_url_spellings_share_one_identity(
    requested_url: str,
    response_url: str,
) -> None:
    urls = [requested_url, "https://bad.example.com"]
    rpc = RecordingRpc([_url_row("src-good", response_url)])

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=AsyncMock(return_value=[]),
        extract_youtube_video_id=_extract_youtube_video_id,
        logger=logging.getLogger(__name__),
    )

    assert outcomes[0].source is not None
    assert outcomes[0].source.id == "src-good"
    assert isinstance(outcomes[1].error, SourceAddError)


@pytest.mark.asyncio
async def test_partial_batch_reconciles_omission_and_keeps_positional_outcomes() -> None:
    urls = [
        "https://good-a.example.com",
        "https://bad.example.com",
        "https://good-b.example.com",
    ]
    rpc = RecordingRpc([_url_row("src-a", urls[0]), _url_row("src-b", urls[2])])
    ghost = Source(id="ghost-bad", title="Bad", url=urls[1], status=SourceStatus.ERROR)
    list_sources = AsyncMock(return_value=[ghost])

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=list_sources,
        extract_youtube_video_id=_extract_youtube_video_id,
        logger=logging.getLogger(__name__),
    )

    assert [item.source.id if item.source else None for item in outcomes] == [
        "src-a",
        None,
        "src-b",
    ]
    assert isinstance(outcomes[1].error, SourceAddError)
    assert "ghost-bad" in str(outcomes[1].error)
    assert "Existing matching ERROR source row" in str(outcomes[1].error)
    assert classify(outcomes[1].error).category is ErrorCategory.SOURCE_ADD
    list_sources.assert_awaited_once_with("nb-1", statuses={SourceStatus.ERROR})


@pytest.mark.asyncio
async def test_all_failed_rpc_is_still_a_per_item_result_array() -> None:
    urls = ["https://bad-a.example.com", "https://bad-b.example.com"]
    rejected = RPCError("all sources rejected", method_id=RPCMethod.ADD_SOURCE.value, rpc_code=9)
    rpc = RecordingRpc(error=rejected)
    list_sources = AsyncMock(return_value=[])

    outcomes = await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=list_sources,
        extract_youtube_video_id=_extract_youtube_video_id,
        logger=logging.getLogger(__name__),
    )

    assert [item.url for item in outcomes] == urls
    assert all(item.source is None for item in outcomes)
    assert all(item.error is not None and item.error.cause is rejected for item in outcomes)
    list_sources.assert_awaited_once_with("nb-1", statuses={SourceStatus.ERROR})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        NetworkError("connection reset after write"),
        RateLimitError("rate limit response after write"),
        ServerError("upstream failed after write"),
    ],
)
async def test_transport_failure_preserves_type_is_unconfirmed_and_never_replayed(
    transport_error: Exception,
) -> None:
    rpc = RecordingRpc(error=transport_error)
    list_sources = AsyncMock()

    with pytest.raises(type(transport_error)) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            ["https://a.example.com", "https://b.example.com"],
            rpc=rpc,
            list_sources=list_sources,
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert raised.value is transport_error
    assert len(rpc.calls) == 1
    list_sources.assert_not_awaited()
    assert getattr(raised.value, "unconfirmed", False) is True
    assert classify(raised.value).category is ErrorCategory.RPC
    assert "do not blindly retry" in str(raised.value).lower()
    assert "reconcile" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_auth_failure_preserves_typed_reauthentication_contract() -> None:
    auth_error = AuthError("csrf token expired")
    rpc = RecordingRpc(error=auth_error)
    list_sources = AsyncMock()

    with pytest.raises(AuthError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            ["https://a.example.com", "https://b.example.com"],
            rpc=rpc,
            list_sources=list_sources,
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert raised.value is auth_error
    assert getattr(raised.value, "unconfirmed", False) is False
    assert len(rpc.calls) == 1
    list_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_rpc_failure_is_not_misreported_as_all_rejected() -> None:
    rpc = RecordingRpc(
        error=RPCError("response decode drift", method_id=RPCMethod.ADD_SOURCE.value)
    )

    with pytest.raises(RPCError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            ["https://a.example.com", "https://b.example.com"],
            rpc=rpc,
            list_sources=AsyncMock(),
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert len(rpc.calls) == 1
    assert getattr(raised.value, "unconfirmed", False) is True
    assert "without the documented all-rejected status" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, [], [[]], [123], [None], [""]])
async def test_malformed_success_payload_is_unconfirmed_not_per_item_rejection(
    payload: Any,
) -> None:
    rpc = RecordingRpc(payload)
    list_sources = AsyncMock()

    with pytest.raises(SourceAddError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            ["https://a.example.com"],
            rpc=rpc,
            list_sources=list_sources,
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "committed subset is unknown" in str(raised.value)
    assert len(rpc.calls) == 1
    list_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_sparse_short_rows_fail_closed_when_omissions_cannot_be_located() -> None:
    rpc = RecordingRpc([["src-only"]])

    with pytest.raises(SourceAddError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            ["https://a.example.com", "https://b.example.com"],
            rpc=rpc,
            list_sources=AsyncMock(),
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "partial batch response" in str(raised.value)


@pytest.mark.asyncio
async def test_partially_admitted_duplicate_url_is_reported_as_ambiguous() -> None:
    url = "https://same.example.com"
    rpc = RecordingRpc([_url_row("src-one", url)])

    with pytest.raises(SourceAddError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            [url, url],
            rpc=rpc,
            list_sources=AsyncMock(),
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "partially admitted identical URLs" in str(raised.value)
    assert "do not blindly retry" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_sparse_response_with_unrequested_url_fails_closed() -> None:
    rpc = RecordingRpc([_url_row("src-other", "https://other.example.com")])

    with pytest.raises(SourceAddError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            ["https://a.example.com", "https://b.example.com"],
            rpc=rpc,
            list_sources=AsyncMock(),
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "unrequested source" in str(raised.value)


@pytest.mark.asyncio
async def test_sparse_response_with_too_many_rows_for_one_identity_fails_closed() -> None:
    urls = [
        "https://a.example.com",
        "https://b.example.com",
        "https://c.example.com",
    ]
    rpc = RecordingRpc([_url_row("src-a1", urls[0]), _url_row("src-a2", urls[0])])

    with pytest.raises(SourceAddError) as raised:
        await SourceBatchAddService().add_urls(
            "nb-1",
            urls,
            rpc=rpc,
            list_sources=AsyncMock(),
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert getattr(raised.value, "unconfirmed", False) is True
    assert "more sources than requested" in str(raised.value)


@pytest.mark.asyncio
async def test_reconciliation_failure_preserves_known_positional_outcomes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    urls = ["https://good.example.com", "https://bad.example.com"]
    rpc = RecordingRpc([_url_row("src-good", urls[0])])
    list_sources = AsyncMock(side_effect=RPCError("listing drifted"))

    with caplog.at_level(logging.WARNING):
        outcomes = await SourceBatchAddService().add_urls(
            "nb-1",
            urls,
            rpc=rpc,
            list_sources=list_sources,
            extract_youtube_video_id=_extract_youtube_video_id,
            logger=logging.getLogger(__name__),
        )

    assert outcomes[0].source is not None
    assert outcomes[0].source.id == "src-good"
    assert isinstance(outcomes[1].error, SourceAddError)
    assert "ERROR source row" not in str(outcomes[1].error)
    assert "failed to list ERROR rows" in caplog.text
