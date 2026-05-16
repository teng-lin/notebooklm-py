"""Unit tests for the private source add service."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._source_add import SourceAddService
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import NetworkError, NonIdempotentRetryError, SourceAddError
from notebooklm.rpc import RPCError, RPCMethod
from notebooklm.types import Source


class RecordingRpc:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "method": method,
                "params": params,
                "source_path": source_path,
                "allow_null": allow_null,
                "disable_internal_retries": disable_internal_retries,
            }
        )
        return self.response


@pytest.fixture
def service() -> SourceAddService:
    return SourceAddService()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("tests.source_add")


def source_response(source_id: str, title: str = "Source") -> list[Any]:
    return [[[["src_" + source_id], title, [None, 0], [None, 2]]]]


@pytest.mark.asyncio
async def test_add_url_routes_youtube_through_late_bound_hook(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    add_youtube_source = AsyncMock(return_value=source_response("yt", "Video"))
    add_url_source = AsyncMock()

    source = await service.add_url(
        "nb_1",
        "https://youtu.be/video",
        add_youtube_source=add_youtube_source,
        add_url_source=add_url_source,
        list_sources=AsyncMock(return_value=[]),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value="video"),
        is_youtube_url=MagicMock(return_value=True),
        logger=logger,
    )

    assert source.id == "src_yt"
    add_youtube_source.assert_awaited_once_with("nb_1", "https://youtu.be/video")
    add_url_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_url_probe_returns_existing_after_transport_error(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    existing = Source(id="src_existing", url="https://example.com")
    add_url_source = AsyncMock(side_effect=NetworkError("temporary network failure"))

    source = await service.add_url(
        "nb_1",
        existing.url,
        add_youtube_source=AsyncMock(),
        add_url_source=add_url_source,
        list_sources=AsyncMock(return_value=[existing]),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=logger,
    )

    assert source is existing
    add_url_source.assert_awaited_once_with("nb_1", existing.url)


@pytest.mark.asyncio
async def test_add_url_wraps_generic_rpc_error(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc_error = RPCError("bad result")

    with pytest.raises(SourceAddError) as exc_info:
        await service.add_url(
            "nb_1",
            "https://example.com",
            add_youtube_source=AsyncMock(),
            add_url_source=AsyncMock(side_effect=rpc_error),
            list_sources=AsyncMock(return_value=[]),
            wait_until_ready=AsyncMock(),
            extract_youtube_video_id=MagicMock(return_value=None),
            is_youtube_url=MagicMock(return_value=False),
            logger=logger,
        )

    assert exc_info.value.url == "https://example.com"
    assert exc_info.value.cause is rpc_error


@pytest.mark.asyncio
async def test_add_text_uses_exact_rpc_shape_and_wait_hook(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc = RecordingRpc(source_response("text", "Title"))
    ready = Source(id="src_text", title="Title")
    wait_until_ready = AsyncMock(return_value=ready)

    result = await service.add_text(
        "nb_1",
        "Title",
        "content",
        wait=True,
        wait_timeout=9.0,
        rpc_call=rpc,
        wait_until_ready=wait_until_ready,
        logger=logger,
    )

    assert result is ready
    assert rpc.calls == [
        {
            "method": RPCMethod.ADD_SOURCE,
            "params": [
                [[None, ["Title", "content"], None, None, None, None, None, None]],
                "nb_1",
                [2],
                None,
                None,
            ],
            "source_path": "/notebook/nb_1",
            "allow_null": False,
            "disable_internal_retries": False,
        }
    ]
    wait_until_ready.assert_awaited_once_with("nb_1", "src_text", timeout=9.0)


@pytest.mark.asyncio
async def test_add_text_refuses_idempotent_flag(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    with pytest.raises(NonIdempotentRetryError):
        await service.add_text(
            "nb_1",
            "Title",
            "content",
            idempotent=True,
            rpc_call=AsyncMock(),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )


@pytest.mark.asyncio
async def test_add_drive_uses_exact_rpc_shape_and_wait_hook(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc = RecordingRpc(source_response("drive", "Drive Doc"))
    ready = Source(id="src_drive", title="Drive Doc")
    wait_until_ready = AsyncMock(return_value=ready)

    result = await service.add_drive(
        "nb_1",
        "drive_file",
        "Drive Doc",
        mime_type="application/pdf",
        wait=True,
        wait_timeout=7.0,
        rpc_call=rpc,
        wait_until_ready=wait_until_ready,
        logger=logger,
    )

    assert result is ready
    assert rpc.calls == [
        {
            "method": RPCMethod.ADD_SOURCE,
            "params": [
                [
                    [
                        ["drive_file", "application/pdf", 1, "Drive Doc"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                    ]
                ],
                "nb_1",
                [2],
                [1, None, None, None, None, None, None, None, None, None, [1]],
            ],
            "source_path": "/notebook/nb_1",
            "allow_null": True,
            "disable_internal_retries": False,
        }
    ]
    wait_until_ready.assert_awaited_once_with("nb_1", "src_drive", timeout=7.0)


@pytest.mark.asyncio
async def test_add_drive_raises_source_add_error_on_null_result(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    with pytest.raises(SourceAddError) as exc_info:
        await service.add_drive(
            "nb_1",
            "drive_file",
            "Drive Doc",
            rpc_call=RecordingRpc(None),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    assert exc_info.value.url == "Drive Doc"
    assert "API returned no data for Drive source: Drive Doc" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_drive_preserves_rpc_error_propagation(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc_error = RPCError("drive add failed")

    with pytest.raises(RPCError) as exc_info:
        await service.add_drive(
            "nb_1",
            "drive_file",
            "Drive Doc",
            rpc_call=AsyncMock(side_effect=rpc_error),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    assert exc_info.value is rpc_error


def test_extract_youtube_video_id_uses_injected_parser_and_helpers(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    parsed = SimpleNamespace(hostname="www.youtube.com", path="/watch", query="v=video_123")
    parse_url = MagicMock(return_value=parsed)
    extract_video_id = MagicMock(return_value="video_123")
    is_valid = MagicMock(return_value=True)

    result = service.extract_youtube_video_id(
        " https://www.youtube.com/watch?v=video_123 ",
        parse_url=parse_url,
        extract_video_id_from_parsed_url=extract_video_id,
        is_valid_video_id=is_valid,
        logger=logger,
    )

    assert result == "video_123"
    parse_url.assert_called_once_with("https://www.youtube.com/watch?v=video_123")
    extract_video_id.assert_called_once_with(parsed, "www.youtube.com")
    is_valid.assert_called_once_with("video_123")


def test_extract_youtube_video_id_parse_error_returns_none(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    result = service.extract_youtube_video_id(
        "https://www.youtube.com/watch?v=video_123",
        parse_url=MagicMock(side_effect=ValueError("parse error")),
        extract_video_id_from_parsed_url=MagicMock(),
        is_valid_video_id=MagicMock(),
        logger=logger,
    )

    assert result is None


@pytest.mark.asyncio
async def test_raw_url_helpers_disable_internal_retries(service: SourceAddService) -> None:
    rpc = RecordingRpc(source_response("url", "URL"))

    await service.add_url_source("nb_1", "https://example.com", rpc_call=rpc)
    await service.add_youtube_source("nb_1", "https://youtu.be/video", rpc_call=rpc)

    assert rpc.calls[0]["disable_internal_retries"] is True
    assert rpc.calls[0]["params"][0][0][2] == ["https://example.com"]
    assert rpc.calls[1]["disable_internal_retries"] is True
    assert rpc.calls[1]["allow_null"] is False
    assert rpc.calls[1]["params"][0][0][7] == ["https://youtu.be/video"]


@pytest.mark.asyncio
async def test_sources_api_add_url_uses_late_bound_facade_hooks() -> None:
    core = MagicMock()
    api = SourcesAPI(core)
    api._extract_youtube_video_id = MagicMock(return_value="video")  # type: ignore[method-assign]
    api._add_youtube_source = AsyncMock(return_value=source_response("yt", "Video"))  # type: ignore[method-assign]
    api._add_url_source = AsyncMock()  # type: ignore[method-assign]
    api.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
    api.wait_until_ready = AsyncMock(return_value=Source(id="ready"))  # type: ignore[method-assign]

    result = await api.add_url("nb_1", "https://youtu.be/video", wait=True, wait_timeout=3.0)

    assert result.id == "ready"
    api._add_youtube_source.assert_awaited_once_with("nb_1", "https://youtu.be/video")
    api._add_url_source.assert_not_awaited()
    api.wait_until_ready.assert_awaited_once_with("nb_1", "src_yt", timeout=3.0)
