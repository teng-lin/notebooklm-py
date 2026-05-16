"""Unit tests for the private source content rendering service."""

from __future__ import annotations

import builtins
import logging
from typing import Any

import pytest

from notebooklm._source_content import SourceContentRenderer
from notebooklm.rpc import RPCMethod
from notebooklm.types import SourceNotFoundError

SOURCE_LOGGER = logging.getLogger("notebooklm._sources")


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


@pytest.mark.asyncio
async def test_text_mode_uses_exact_rpc_shape_and_extracts_nested_plaintext() -> None:
    rpc = RecordingRpc(
        [
            [
                "src_1",
                "Article",
                [None, None, None, None, 5, None, None, ["https://example.com"]],
            ],
            None,
            None,
            [[["First paragraph.", [0, 20, "Second paragraph."]]]],
        ]
    )
    renderer = SourceContentRenderer(rpc)

    fulltext = await renderer.get_fulltext("nb_1", "src_1")

    assert fulltext.title == "Article"
    assert fulltext.content == "First paragraph.\nSecond paragraph."
    assert fulltext._type_code == 5
    assert fulltext.url == "https://example.com"
    assert fulltext.char_count == len(fulltext.content)
    assert rpc.calls == [
        {
            "method": RPCMethod.GET_SOURCE,
            "params": [["src_1"], [2], [2]],
            "source_path": "/notebook/nb_1",
            "allow_null": True,
            "disable_internal_retries": False,
        }
    ]


@pytest.mark.asyncio
async def test_markdown_mode_uses_html_rpc_shape_and_converts_html() -> None:
    pytest.importorskip("markdownify")
    rpc = RecordingRpc(
        [
            ["src_md", "Markdown Source", [None, None, None, None, 5]],
            None,
            None,
            None,
            [None, "<h1>Title</h1><p>Hello <strong>world</strong>.</p>"],
        ]
    )
    renderer = SourceContentRenderer(rpc)

    fulltext = await renderer.get_fulltext("nb_1", "src_md", output_format="markdown")

    assert "# Title" in fulltext.content
    assert "**world**" in fulltext.content
    assert rpc.calls[0]["params"] == [["src_md"], [3], [3]]


@pytest.mark.asyncio
async def test_markdown_mode_missing_dependency_fails_before_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "markdownify":
            raise ImportError("No module named 'markdownify'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rpc = RecordingRpc([["src_md", "Markdown Source", []]])
    renderer = SourceContentRenderer(rpc)

    with pytest.raises(ImportError, match="notebooklm-py\\[markdown\\]"):
        await renderer.get_fulltext("nb_1", "src_md", output_format="markdown")

    assert rpc.calls == []


@pytest.mark.asyncio
async def test_invalid_output_format_fails_before_rpc() -> None:
    rpc = RecordingRpc([["src_1", "Article", []]])
    renderer = SourceContentRenderer(rpc)

    with pytest.raises(ValueError, match="text.*markdown"):
        await renderer.get_fulltext(
            "nb_1",
            "src_1",
            output_format="html",  # type: ignore[arg-type]
        )

    assert rpc.calls == []


@pytest.mark.asyncio
async def test_missing_html_rendition_logs_warning_and_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("markdownify")
    renderer = SourceContentRenderer(
        RecordingRpc([["src_yt", "Video", [None, None, None, None, 9]], None, None, None]),
        logger=SOURCE_LOGGER,
    )
    caplog.set_level("WARNING", logger="notebooklm._sources")

    fulltext = await renderer.get_fulltext("nb_1", "src_yt", output_format="markdown")

    assert fulltext.content == ""
    assert "no HTML rendition" in caplog.text
    assert "returned empty content" in caplog.text


@pytest.mark.asyncio
async def test_empty_text_content_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    renderer = SourceContentRenderer(
        RecordingRpc([["src_empty", "Empty", [None, None, None, None, 4]], None, None, [[]]]),
        logger=SOURCE_LOGGER,
    )
    caplog.set_level("WARNING", logger="notebooklm._sources")

    fulltext = await renderer.get_fulltext("nb_1", "src_empty")

    assert fulltext.content == ""
    assert fulltext.char_count == 0
    assert "returned empty content" in caplog.text


@pytest.mark.asyncio
async def test_missing_source_raises_not_found() -> None:
    renderer = SourceContentRenderer(RecordingRpc(None), logger=SOURCE_LOGGER)

    with pytest.raises(SourceNotFoundError):
        await renderer.get_fulltext("nb_1", "missing")


@pytest.mark.asyncio
async def test_url_and_type_parsing_uses_shared_metadata_rules() -> None:
    renderer = SourceContentRenderer(
        RecordingRpc(
            [
                [
                    "src_yt",
                    "Video",
                    [
                        "https://bare.example/ignored",
                        None,
                        None,
                        None,
                        9,
                        ["https://www.youtube.com/watch?v=abc123"],
                        None,
                        None,
                    ],
                ],
                None,
                None,
                [[["Transcript."]]],
            ]
        )
    )

    fulltext = await renderer.get_fulltext("nb_1", "src_yt")

    assert fulltext._type_code == 9
    assert fulltext.url == "https://www.youtube.com/watch?v=abc123"


def test_extract_all_text_handles_nesting_and_max_depth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    renderer = SourceContentRenderer(RecordingRpc(None), logger=SOURCE_LOGGER)

    assert renderer.extract_all_text([["hello", ["world"]], "", "tail"]) == [
        "hello",
        "world",
        "tail",
    ]

    caplog.set_level("WARNING", logger="notebooklm._sources")
    assert renderer.extract_all_text(["too", "deep"], max_depth=0) == []
    assert "Max recursion depth reached" in caplog.text


@pytest.mark.asyncio
async def test_get_guide_uses_exact_rpc_shape_and_parses_summary_keywords() -> None:
    rpc = RecordingRpc([[[None, ["Summary"], [["keyword1", "keyword2"]], []]]])
    renderer = SourceContentRenderer(rpc)

    guide = await renderer.get_guide("nb_1", "src_1")

    assert guide == {"summary": "Summary", "keywords": ["keyword1", "keyword2"]}
    assert rpc.calls == [
        {
            "method": RPCMethod.GET_SOURCE_GUIDE,
            "params": [[[["src_1"]]]],
            "source_path": "/notebook/nb_1",
            "allow_null": True,
            "disable_internal_retries": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        ["not_a_list"],
        [["not_a_list"]],
        [[[None, [], [], []]]],
        [[[None, [123], [["keyword"]], []]]],
        [[[None, ["Summary"], ["not_keyword_list"], []]]],
    ],
)
async def test_get_guide_shape_variants_return_stable_defaults(response: Any) -> None:
    renderer = SourceContentRenderer(RecordingRpc(response))

    guide = await renderer.get_guide("nb_1", "src_1")

    assert set(guide) == {"summary", "keywords"}
    assert isinstance(guide["summary"], str)
    assert isinstance(guide["keywords"], list)
