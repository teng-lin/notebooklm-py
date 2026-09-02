"""Task orchestration in the MCP thin-content sanity pass.

``tests/unit/mcp/test_sources.py`` drives the warning classifier end to end
through ``source_wait``. These cases target the fan-out itself: the web-page
pre-filter, the in-place annotation contract, and the cancel-and-drain path
that keeps a failing or cancelled batch from leaking sibling fetches.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from notebooklm._types.sources import _SOURCE_TYPE_CODE_MAP, Source, SourceType
from notebooklm.mcp.tools import _content_sanity
from notebooklm.mcp.tools._content_sanity import (
    _annotate_thin_warnings,
    _thin_content_warning,
)
from notebooklm.rpc.types import SourceStatus

pytestmark = pytest.mark.asyncio


def _type_code(kind: SourceType) -> int:
    """Resolve a wire type code from the canonical map, not a magic number."""
    return next(code for code, value in _SOURCE_TYPE_CODE_MAP.items() if value is kind)


_WEB_PAGE_TYPE_CODE = _type_code(SourceType.WEB_PAGE)
_PASTED_TEXT_TYPE_CODE = _type_code(SourceType.PASTED_TEXT)


def _source(source_id: str, *, type_code: int = _WEB_PAGE_TYPE_CODE, ready: bool = True) -> Source:
    return Source(
        id=source_id,
        title=source_id,
        _type_code=type_code,
        status=SourceStatus.READY if ready else SourceStatus.PROCESSING,
    )


def _pairs(*sources: Source) -> list[tuple[dict[str, Any], Source]]:
    return [({"id": source.id}, source) for source in sources]


async def test_no_web_page_sources_schedules_no_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-web-page sources are filtered out before any task is created."""
    calls: list[str] = []

    async def _never(_client, _notebook_id, source):  # noqa: ANN001, ANN202
        calls.append(source.id)
        return "warned"

    monkeypatch.setattr(_content_sanity, "_thin_content_warning", _never)
    pairs = _pairs(_source("s1", type_code=_PASTED_TEXT_TYPE_CODE))

    await _annotate_thin_warnings(object(), "nb1", pairs)

    assert calls == []
    assert pairs[0][0] == {"id": "s1"}


async def test_warnings_are_attached_in_place_to_their_own_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _warn(_client, _notebook_id, source):  # noqa: ANN001, ANN202
        return None if source.id == "s2" else f"thin:{source.id}"

    monkeypatch.setattr(_content_sanity, "_thin_content_warning", _warn)
    pairs = _pairs(_source("s1"), _source("s2"), _source("s3"))

    await _annotate_thin_warnings(object(), "nb1", pairs)

    assert [view.get("warning") for view, _source in pairs] == ["thin:s1", None, "thin:s3"]


async def test_an_existing_warning_is_never_clobbered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _warn(_client, _notebook_id, _source):  # noqa: ANN001, ANN202
        return "thin"

    monkeypatch.setattr(_content_sanity, "_thin_content_warning", _warn)
    pairs = _pairs(_source("s1"))
    pairs[0][0]["warning"] = "import failed"

    await _annotate_thin_warnings(object(), "nb1", pairs)

    assert pairs[0][0]["warning"] == "import failed"


async def test_a_failing_fetch_cancels_and_drains_its_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sibling coroutine is left running when the batch escapes."""
    started = asyncio.Event()
    sibling_cancelled = False

    async def _warn(_client, _notebook_id, source):  # noqa: ANN001, ANN202
        nonlocal sibling_cancelled
        if source.id == "boom":
            await started.wait()
            raise RuntimeError("fetch exploded")
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            sibling_cancelled = True
            raise
        return None

    monkeypatch.setattr(_content_sanity, "_thin_content_warning", _warn)
    pairs = _pairs(_source("boom"), _source("slow"))

    with pytest.raises(RuntimeError, match="fetch exploded"):
        await _annotate_thin_warnings(object(), "nb1", pairs)

    assert sibling_cancelled is True
    assert "warning" not in pairs[1][0]


async def test_caller_cancellation_drains_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    running = asyncio.Event()
    cancelled = 0

    async def _warn(_client, _notebook_id, _source):  # noqa: ANN001, ANN202
        nonlocal cancelled
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled += 1
            raise
        return None

    monkeypatch.setattr(_content_sanity, "_thin_content_warning", _warn)
    pairs = _pairs(_source("s1"), _source("s2"))
    task = asyncio.create_task(_annotate_thin_warnings(object(), "nb1", pairs))
    await running.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled == 2


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_source("s1", ready=False), id="not-ready"),
        pytest.param(_source("s1", type_code=_PASTED_TEXT_TYPE_CODE), id="not-a-web-page"),
    ],
)
async def test_the_classifier_refuses_sources_it_must_never_flag(source: Source) -> None:
    """Short pasted text is legitimate; an unfinished source has nothing to read."""
    assert await _thin_content_warning(object(), "nb1", source) is None
