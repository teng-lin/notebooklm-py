"""Goldens for the row-facing research payload builders and decoders (P9.3).

The positional grammar itself is pinned by
``test_semantic_research_slice_characterization.py``; these goldens pin the
request options each codec row attaches around it — the notebook route, the
default flags, and the import row's service-computed attempt window — so a row
cannot silently change what reaches the transport.
"""

from __future__ import annotations

from notebooklm._binding import CodecPayload
from notebooklm._semantic.records import (
    ResearchCancelInput,
    ResearchCancelResult,
    ResearchImportEntry,
    ResearchImportEntryKind,
    ResearchImportInput,
    ResearchMode,
    ResearchPollInput,
    ResearchSearchSource,
    ResearchStartInput,
)
from notebooklm._web.codec import research as codec


def test_start_payload_selects_the_mode_specific_grammar_and_routes_by_notebook() -> None:
    fast = codec.encode_research_start(
        ResearchStartInput("nb", "q", ResearchSearchSource.WEB, ResearchMode.FAST)
    )
    deep = codec.encode_research_start(
        ResearchStartInput("nb", "q", ResearchSearchSource.DRIVE, ResearchMode.DEEP)
    )
    assert fast == CodecPayload(params=[["q", 1], None, 1, "nb"], source_path="/notebook/nb")
    assert deep == CodecPayload(params=[None, [1], ["q", 2], 5, "nb"], source_path="/notebook/nb")
    for payload in (fast, deep):
        assert payload.allow_null is False
        assert payload.raise_on_null_status is False
        assert payload.attempt_timeout is None


def test_poll_and_cancel_payloads() -> None:
    assert codec.encode_research_poll(ResearchPollInput("nb")) == CodecPayload(
        params=[None, None, "nb"], source_path="/notebook/nb"
    )
    assert codec.encode_research_cancel(ResearchCancelInput("nb", "run_1")) == CodecPayload(
        params=[None, None, "run_1"], source_path="/notebook/nb"
    )


def test_import_payload_forwards_the_attempt_window_verbatim() -> None:
    entries = (
        ResearchImportEntry(
            kind=ResearchImportEntryKind.WEB, url="https://example.com", title="One"
        ),
        ResearchImportEntry(
            kind=ResearchImportEntryKind.REPORT, title="Report", report_markdown="# md"
        ),
    )
    payload = codec.encode_research_import(
        ResearchImportInput("nb", "task", entries, attempt_timeout=7.5)
    )
    assert payload == CodecPayload(
        params=[
            None,
            [1],
            "task",
            "nb",
            [
                codec.build_web_import_entry("https://example.com", "One"),
                codec.build_report_import_entry("Report", "# md"),
            ],
        ],
        source_path="/notebook/nb",
        attempt_timeout=7.5,
    )
    unbounded = codec.encode_research_import(ResearchImportInput("nb", "task", ()))
    assert unbounded.attempt_timeout is None


def test_row_decoders_wrap_the_existing_projections() -> None:
    poll = codec.decode_research_poll(ResearchPollInput("nb"), [])
    assert poll.tasks == ()
    assert codec.decode_research_cancel(ResearchCancelInput("nb", "run"), None) == (
        ResearchCancelResult()
    )
    imported = codec.decode_research_import(
        ResearchImportInput("nb", "task", ()), [[["src_1"], "One"]]
    )
    assert [(row.id, row.title) for row in imported.imported] == [("src_1", "One")]
