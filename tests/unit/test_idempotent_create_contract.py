"""Focused regression coverage for idempotent-create provenance (#1988)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._idempotency import (
    _CreateResultKind,
    _IdempotentCreateResult,
    idempotent_create,
)
from notebooklm._source.add import SourceAddService
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import NetworkError
from notebooklm.types import Source


@pytest.mark.asyncio
async def test_idempotent_create_marks_fresh_create() -> None:
    result = await idempotent_create(
        AsyncMock(return_value="created"), AsyncMock(return_value=None)
    )
    assert result == _IdempotentCreateResult("created", _CreateResultKind.CREATED)


@pytest.mark.asyncio
async def test_idempotent_create_marks_probe_match_without_retry() -> None:
    create = AsyncMock(side_effect=NetworkError("lost response"))
    probe = AsyncMock(return_value="existing")

    result = await idempotent_create(create, probe)

    assert result.value == "existing"
    assert result.kind is _CreateResultKind.PROBED
    create.assert_awaited_once()
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_idempotent_create_retries_after_probe_miss() -> None:
    create = AsyncMock(side_effect=[NetworkError("first"), "second"])
    probe = AsyncMock(return_value=None)

    result = await idempotent_create(create, probe)

    assert result.value == "second"
    assert result.kind is _CreateResultKind.CREATED
    assert create.await_count == 2
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_idempotent_create_reraises_last_exception_by_identity() -> None:
    error = NetworkError("still unavailable")
    create = AsyncMock(side_effect=[NetworkError("first"), error])

    with pytest.raises(NetworkError) as raised:
        await idempotent_create(create, AsyncMock(return_value=None))

    assert raised.value is error


@pytest.mark.asyncio
async def test_probe_result_never_honors_url_title_even_after_wait() -> None:
    api = SourcesAPI(MagicMock(), uploader=MagicMock())
    existing = Source(id="existing", title="Drive title", url="https://example.test")
    api._adder.add_url = AsyncMock(
        return_value=_IdempotentCreateResult(existing, _CreateResultKind.PROBED)
    )
    api.wait_until_ready = AsyncMock(  # type: ignore[method-assign]
        return_value=Source(id="existing", title="Ready")
    )
    api.rename = AsyncMock()  # type: ignore[method-assign]

    result = await api.add_url("nb", existing.url, title="Do not retitle", wait=True)

    assert result.title == "Drive title"
    api.wait_until_ready.assert_not_awaited()
    api.rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_service_default_return_remains_source() -> None:
    service = SourceAddService()
    source = Source(id="fresh", title="Upstream")

    result = await service.add_url(
        "nb",
        "https://example.test",
        add_youtube_source=AsyncMock(),
        add_url_source=AsyncMock(return_value=[[[["fresh"], "Upstream"]]]),
        list_sources=AsyncMock(return_value=[]),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=MagicMock(),
    )

    assert isinstance(result, Source)
    assert result.id == source.id


@pytest.mark.asyncio
async def test_private_service_preserves_probe_provenance_through_wait() -> None:
    service = SourceAddService()
    existing = Source(id="existing", title="Upstream", url="https://example.test")
    ready = Source(id="existing", title="Ready", url=existing.url)

    result = await service.add_url(
        "nb",
        existing.url,
        wait=True,
        add_youtube_source=AsyncMock(),
        add_url_source=AsyncMock(side_effect=NetworkError("lost response")),
        list_sources=AsyncMock(return_value=[existing]),
        wait_until_ready=AsyncMock(return_value=ready),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=MagicMock(),
        return_result=True,
    )

    assert isinstance(result, _IdempotentCreateResult)
    assert result.kind is _CreateResultKind.PROBED
    assert result.value is ready
