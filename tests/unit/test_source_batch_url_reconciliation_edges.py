"""Fail-closed edges of the true-batch ADD_SOURCE reconciliation.

The batch write is never replayed, so every branch that decides "this response
cannot be attributed positionally" is load-bearing: getting it wrong either
silently drops a committed source or reports a stranger's row as the caller's.
These tests pin the identity normalisation used for that decision and the
guards that reject an unattributable response.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm._web.sources.batch import (
    SourceBatchAddService,
    _unresolved_batch_error,
    _url_identity,
)
from notebooklm.exceptions import SourceAddError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import Source


def _no_youtube(_url: str) -> None:
    return None


def _url_row(
    source_id: str,
    url: str | None,
    *,
    status: SourceStatus = SourceStatus.PROCESSING,
) -> list[Any]:
    metadata: list[Any] = [None, None, None, None, 5, None, None, [url] if url else None]
    return [[source_id], url, metadata, [None, status.value]]


def _error_source(source_id: str, url: str | None) -> Source:
    return Source.from_api_response(
        _url_row(source_id, url, status=SourceStatus.ERROR),
        method_id=RPCMethod.ADD_SOURCE.value,
    )


class _RecordingRpc:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        del method, params, kwargs
        self.calls += 1
        return self.result


async def _add(
    rpc: _RecordingRpc,
    urls: list[str],
    *,
    list_sources: Any = None,
) -> list[Any]:
    return await SourceBatchAddService().add_urls(
        "nb-1",
        urls,
        rpc=rpc,
        list_sources=list_sources if list_sources is not None else AsyncMock(return_value=[]),
        extract_youtube_video_id=_no_youtube,
        logger=logging.getLogger(__name__),
    )


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://host.example:99999/p", id="port-out-of-range"),
        pytest.param("http://host.example:notaport/p", id="non-numeric-port"),
    ],
)
def test_url_identity_keeps_an_unparseable_port_distinct_instead_of_raising(url: str) -> None:
    """A response URL is untrusted wire data; normalisation must never raise.

    Returning the raw candidate keeps the value distinct from every real URL,
    so the caller fails closed on the mismatch rather than crashing mid-batch.
    """
    assert _url_identity(url, extract_youtube_video_id=_no_youtube) == ("raw", url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("not-a-url", id="no-scheme"),
        pytest.param("/relative/path", id="relative-path"),
        pytest.param("mailto:someone@host.example", id="scheme-without-hostname"),
    ],
)
def test_url_identity_falls_back_to_raw_without_a_scheme_and_hostname(url: str) -> None:
    """Canonicalisation needs both halves of an authority; anything else stays raw."""
    assert _url_identity(url, extract_youtube_video_id=_no_youtube) == ("raw", url)


def test_url_identity_keeps_a_bare_username_without_appending_an_empty_password() -> None:
    """A username-only userinfo must not grow a ``:`` separator for a missing password."""
    identity = _url_identity("https://us%2fer@Host.Example/p", extract_youtube_video_id=_no_youtube)

    assert identity == ("url", "https://us%2Fer@host.example/p")
    assert ":" not in identity[1].split("@", 1)[0].removeprefix("https://")


def test_unresolved_batch_error_previews_three_urls_and_reports_the_total() -> None:
    """An operator reconciling by hand needs the count, not a wall of URLs."""
    urls = [f"https://u{index}.example.com" for index in range(5)]

    error = _unresolved_batch_error(urls, "boom.", RuntimeError("cause"))

    # Assert on the previewed SET, not substring containment. Besides being the
    # stronger check (it pins exactly which three are shown, and their order),
    # ``"<url>" in <text>`` trips CodeQL's incomplete-url-substring-sanitization
    # rule, which cannot tell a message assertion from a URL security check.
    rendered = str(error)
    previewed = re.findall(r"'(https://[^']+)'", rendered)

    assert previewed == urls[:3]
    assert "… (5 total)" in rendered
    assert getattr(error, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_an_empty_url_batch_returns_no_outcomes_without_issuing_an_rpc() -> None:
    """An empty batch has nothing to commit, so it must not reach the wire at all."""
    rpc = _RecordingRpc([])

    assert await _add(rpc, []) == []
    assert rpc.calls == 0


@pytest.mark.asyncio
async def test_more_response_rows_than_requests_fails_closed_before_identity_matching() -> None:
    """A row count above the request count is decided on arithmetic alone.

    The surplus row here carries no URL, so only the cardinality guard can
    catch it: the later identity-matching guards would report the missing URL
    metadata instead and never notice the response was over-long.
    """
    urls = [f"https://u{index}.example.com" for index in range(5)]
    rows = [_url_row(f"src-{index}", url) for index, url in enumerate(urls)]
    rows.append(_url_row("src-extra", None))
    rpc = _RecordingRpc(rows)

    with pytest.raises(SourceAddError) as raised:
        await _add(rpc, urls)

    assert "more sources than requested" in str(raised.value)
    assert "… (5 total)" in str(raised.value)
    assert getattr(raised.value, "unconfirmed", False) is True
    assert rpc.calls == 1


@pytest.mark.asyncio
async def test_a_complete_response_whose_url_cannot_be_parsed_fails_closed() -> None:
    """An unparseable positional URL is not evidence the row matches the request."""
    urls = ["https://a.example.com"]
    rpc = _RecordingRpc([_url_row("src-a", "https://a.example.com:99999")])

    with pytest.raises(SourceAddError) as raised:
        await _add(rpc, urls)

    assert "did not match request order" in str(raised.value)
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_ghost_reconciliation_ignores_error_rows_that_cannot_be_attributed() -> None:
    """Only ERROR rows that identify a requested URL may enrich a per-item failure.

    A row without URL metadata cannot be matched at all, and a row for a URL
    this batch never requested belongs to some other write; attributing either
    one would put a stranger's source id in the caller's error message.
    """
    urls = ["https://a.example.com", "https://b.example.com"]
    rpc = _RecordingRpc([_url_row("src-a", urls[0])])
    list_sources = AsyncMock(
        return_value=[
            _error_source("ghost-urlless", None),
            _error_source("ghost-unrequested", "https://z.example.com"),
            _error_source("ghost-b", urls[1]),
        ]
    )

    outcomes = await _add(rpc, urls, list_sources=list_sources)

    assert outcomes[0].source is not None and outcomes[0].source.id == "src-a"
    assert outcomes[1].source is None
    message = str(outcomes[1].error)
    assert "ghost-b" in message
    assert "ghost-urlless" not in message
    assert "ghost-unrequested" not in message
    list_sources.assert_awaited_once_with("nb-1", statuses={SourceStatus.ERROR})
