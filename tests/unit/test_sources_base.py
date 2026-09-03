"""Contract tests for the backend-neutral :class:`SourcesAPI`."""

from __future__ import annotations

import ast
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest

from notebooklm._sources import SourcesAPI
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.exceptions import RPCError, SourceNotFoundError, ValidationError
from notebooklm.types import Source, SourceStatus, SourceType


class _ConcreteSources(SourcesAPI):
    def __init__(self, sources: list[Source] | Exception) -> None:
        super().__init__()
        self._listed = sources
        self.list_calls: list[
            tuple[str, bool, Collection[SourceStatus] | None, Collection[SourceType] | None]
        ] = []
        self.upload_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> list[Source]:
        self.list_calls.append((notebook_id, strict, statuses, types))
        if isinstance(self._listed, Exception):
            raise self._listed
        return self._listed

    async def _unsupported(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _send_upload(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: Any,
    ) -> Source:
        self.upload_calls.append(
            (
                (notebook_id, file_path, mime_type),
                {
                    "wait": wait,
                    "wait_timeout": wait_timeout,
                    "title": title,
                    "on_progress": on_progress,
                },
            )
        )
        return Source(id="uploaded", title=title)

    add_url = _unsupported
    search = _unsupported
    add_text = _unsupported
    add_drive = _unsupported
    add_drive_file = _unsupported
    list_play_books = _unsupported
    add_play_book = _unsupported
    delete = _unsupported
    rename = _unsupported
    refresh = _unsupported
    check_freshness = _unsupported
    get_guide = _unsupported
    get_fulltext = _unsupported
    _send_add_urls_async = _unsupported
    _send_append_text = _unsupported
    _send_copy = _unsupported


@pytest.mark.asyncio
async def test_get_and_get_or_none_inline_the_abstract_list_identity_match() -> None:
    expected = Source(id="src_2", title="Target")
    api = _ConcreteSources([Source(id="src_1"), expected])

    assert await api.get_or_none("nb_1", "src_2") is expected
    assert await api.get("nb_1", "src_2") is expected
    assert api.list_calls == [
        ("nb_1", False, None, None),
        ("nb_1", False, None, None),
    ]


@pytest.mark.asyncio
async def test_get_or_none_returns_none_and_get_raises_on_a_real_miss() -> None:
    api = _ConcreteSources([])

    assert await api.get_or_none("nb_1", "missing") is None
    with pytest.raises(SourceNotFoundError):
        await api.get("nb_1", "missing")


@pytest.mark.asyncio
async def test_get_or_none_propagates_list_failures() -> None:
    api = _ConcreteSources(RPCError("transport failed"))

    with pytest.raises(RPCError, match="transport failed"):
        await api.get_or_none("nb_1", "src_1")


@pytest.mark.asyncio
async def test_add_file_normalizes_title_and_calls_the_single_upload_hook() -> None:
    api = _ConcreteSources([])

    def progress(_sent: int, _total: int) -> None:
        return None

    result = await api.add_file(
        "nb_1",
        Path("report.pdf"),
        "application/pdf",
        wait=True,
        wait_timeout=42.0,
        title="  Quarterly report  ",
        on_progress=progress,
    )

    assert result == Source(id="uploaded", title="Quarterly report")
    assert api.upload_calls == [
        (
            ("nb_1", Path("report.pdf"), "application/pdf"),
            {
                "wait": True,
                "wait_timeout": 42.0,
                "title": "Quarterly report",
                "on_progress": progress,
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "   "])
async def test_add_file_rejects_blank_title_before_the_upload_hook(title: str) -> None:
    api = _ConcreteSources([])

    with pytest.raises(ValidationError, match="Title cannot be empty"):
        await api.add_file("nb_1", "missing.pdf", title=title)

    assert api.upload_calls == []


@pytest.mark.asyncio
async def test_batch_adapter_seam_is_typed_nonabstract_and_unsupported_by_default() -> None:
    assert "_add_urls_batch" not in SourcesAPI.__abstractmethods__
    with pytest.raises(NotImplementedError):
        await _ConcreteSources([])._add_urls_batch("nb_1", ["https://example.com"])


def test_web_facade_inherits_every_neutral_concrete_workflow() -> None:
    for name in (
        "get",
        "get_or_none",
        "add_file",
        "add_urls_async",
        "append_text",
        "copy",
        "wait_until_ready",
        "wait_all_until_ready",
        "wait_until_registered",
        "wait_for_sources",
    ):
        assert name not in WebSourcesAPI.__dict__
        assert getattr(WebSourcesAPI, name) is getattr(SourcesAPI, name)


def test_neutral_source_package_keeps_moved_exports_lazy() -> None:
    import notebooklm._source as source_package
    from notebooklm._web.sources.listing import SourceLister

    package_path = Path(__file__).parents[2] / "src" / "notebooklm" / "_source" / "__init__.py"
    tree = ast.parse(package_path.read_text(encoding="utf-8"))

    eager_web_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom)) and "_web" in ast.unparse(node)
    ]
    eager_import_module_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "import_module"
    ]
    assert eager_web_imports == []
    assert eager_import_module_calls == []
    assert source_package.SourceLister is SourceLister
