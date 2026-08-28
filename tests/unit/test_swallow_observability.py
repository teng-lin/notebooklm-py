"""Tests for categorized observability at 12 swallowed-exception sites."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from notebooklm.exceptions import UnknownRPCMethodError

# Path to the repo's src/notebooklm/ — used by the silent-site source inspection tests.
SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"


# ---------------------------------------------------------------------------
# WARNING sites — drift detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_source_ids_warns_on_top_level_shape_drift(caplog):
    """Web get_source_ids: non-list at notebook_data[0] triggers WARNING."""
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=[{"unexpected": "dict"}]))
    api = WebNotebooksAPI(core)

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await api.get_source_ids("nb_drift")

    assert result == []
    drift_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "schema drift" in r.message
    ]
    assert drift_warnings, (
        f"expected schema drift warning, got: {[r.message for r in caplog.records]}"
    )
    assert "nb_drift" in drift_warnings[0].message


@pytest.mark.asyncio
async def test_get_source_ids_warns_on_inner_shape_drift(caplog):
    """Web get_source_ids: notebook_info[1] not list triggers WARNING."""
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    # notebook_data[0] is a list of length >1 but [1] is not a list
    core = make_fake_core(rpc_call=AsyncMock(return_value=[[None, "not a list", "x"]]))
    api = WebNotebooksAPI(core)

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await api.get_source_ids("nb_inner")

    assert result == []
    assert any("schema drift" in r.message and "nb_inner" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_get_source_ids_happy_path_no_warning(caplog):
    """Well-formed payload extracts source ids and emits no warning."""
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(
        rpc_call=AsyncMock(return_value=[[None, [[["src_alpha"]], [["src_beta"]]]]])
    )
    api = WebNotebooksAPI(core)

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await api.get_source_ids("nb_happy")

    assert result == ["src_alpha", "src_beta"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


@pytest.mark.asyncio
async def test_get_source_ids_empty_notebook_emits_no_drift_warning(caplog):
    """An empty notebook elides the sources slot — a valid empty state, not drift (#2131).

    Live shape on a freshly created, genuinely empty notebook: the envelope is
    healthy (the observed report was ``len=11``) but ``notebook_info[1]`` is
    ``None`` rather than ``[]``. Warning here fires on every empty notebook and
    erodes the one signal that must stay trustworthy — ``schema drift?`` is how
    an operator learns the positional payload shape really did change.
    """
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=[[None] * 11]))
    api = WebNotebooksAPI(core)

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await api.get_source_ids("nb_empty")

    assert result == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.asyncio
async def test_get_source_ids_warns_when_the_sources_slot_is_absent(caplog):
    """An *absent* sources slot is drift; only a present-and-null one is empty (#2131).

    Pins the boundary the carve-out must not cross. ``[[None]]`` is too short to
    hold slot 1 at all — a truncated envelope, not an empty notebook, whose
    reported shape carries a full ``len=11``. Written because the obvious
    implementation ("return quietly whenever the slot expression is ``None``")
    folds this case in and silently drops the warning it used to emit.
    """
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=[[None]]))
    api = WebNotebooksAPI(core)

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await api.get_source_ids("nb_short")

    assert result == []
    assert any("schema drift" in r.message and "nb_short" in r.message for r in caplog.records)


def test_qa_pairs_raises_on_unguarded_shape():
    """Web chat QA-pair parser raises when next_turn[4] is not indexable.

    Strict decoding is the only mode (the ``NOTEBOOKLM_STRICT_DECODE=0``
    soft-mode opt-out was retired in v0.7.0), so a drifted answer turn raises
    ``UnknownRPCMethodError`` rather than silently producing an empty answer.
    """
    from notebooklm._web.chat import WebChatAPI

    # next_turn[4] is None → None[0] raises TypeError, surfaced by safe_index.
    turns_data = [
        [
            [None, None, 1, "what?"],  # question turn (type=1)
            [None, None, 2, None, None],  # answer turn (type=2); next_turn[4] is None
        ]
    ]

    with pytest.raises(UnknownRPCMethodError):
        WebChatAPI._parse_turns_to_qa_pairs(turns_data)


@pytest.mark.asyncio
async def test_summary_raises_on_indexerror_drift():
    """Web notebook summary extraction raises when result[0][0][0] drifts.

    ``get_summary`` delegates the descent to ``_extract_summary`` (the single
    source of truth shared with ``get_description`` — #1485), so under strict
    decoding (the only mode) a *present-but-malformed* response raises
    ``UnknownRPCMethodError`` carrying the helper label
    ``source='_notebooks._extract_summary'``. A genuinely-absent summary
    (None / empty / null slot) returns "" instead and is covered in
    ``test_get_summary_drift.py``.
    """
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    api = WebNotebooksAPI.__new__(WebNotebooksAPI)
    # result[0] == [42]: the summary slot is present and non-None but holds an
    # int, so the inner result[0][0][0] descent raises TypeError — genuine
    # drift, distinct from a routinely-absent summary.
    mock_core = make_fake_core(rpc_call=AsyncMock(return_value=[[42]]))
    api._rpc = mock_core

    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_summary("nb_summary")

    assert exc_info.value.source == "_notebooks._extract_summary"


# ---------------------------------------------------------------------------
# DEBUG sites
# ---------------------------------------------------------------------------


# Removed: ``test_retry_after_non_integer_logs_debug`` was self-fulfilling — it
# called ``core_mod.logger.debug(...)`` inline rather than exercising production
# code. A later refactor replaced the original "Retry-After header not an integer" log
# site with the ``parse_retry_after`` helper, which returns ``None`` silently
# for unparseable input. Parse semantics are covered by
# ``tests/unit/test_retry_after.py``.


@pytest.mark.asyncio
async def test_description_partial_summary_logs_debug(caplog):
    """Web notebook partial summary (no topics) logs at DEBUG."""
    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    api = WebNotebooksAPI.__new__(WebNotebooksAPI)
    # outer[0][0] works but outer[1] raises (no topics shape)
    mock_core = make_fake_core(rpc_call=AsyncMock(return_value=[[["the summary"]]]))
    api._rpc = mock_core

    with caplog.at_level(logging.DEBUG, logger="notebooklm"):
        desc = await api.get_description("nb_partial")

    assert desc.summary == "the summary"
    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "Partial description" in r.message
    ]
    assert debug_records
    # And NO warnings
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_migration_config_unparseable_logs_debug(caplog, tmp_path, monkeypatch):
    """migration.py — unparseable migration config logs at DEBUG.

    After the fix the lock-protected ``atomic_update_json`` surfaces the
    parse failure as a ``json.JSONDecodeError`` which the helper catches
    and reports as "Migration config update failed".
    """
    import notebooklm.migration as mig

    bad = tmp_path / "config.json"
    bad.write_text("{ not json ")
    monkeypatch.setattr(mig, "get_config_path", lambda: bad)

    with caplog.at_level(logging.DEBUG, logger="notebooklm"):
        mig._set_default_profile_in_config()

    assert any(
        "Migration config update failed" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    )


def test_auth_corrupt_legacy_context_does_not_block_in_band_write(tmp_path):
    """auth.py — corrupt legacy ``context.json`` no longer blocks account writes.

    Pre-P1-20, account metadata was written into ``context.json`` itself, so
    a corrupt file there had to be recoverable inline. P1-20 moved the write
    target into ``storage_state.json`` under the ``notebooklm`` namespace key,
    so a corrupt sibling ``context.json`` is irrelevant to the write path.

    Since the master-token-relocation PR-0 (#2103), ``read_account_metadata``
    no longer returns a raw pass-through of the legacy sibling — closing the
    wrong-account hazard a missed legacy ``authuser`` created. The sole
    remaining legacy consumer, ``promote_legacy_account`` (called from
    ``read_account_metadata`` itself whenever in-band is absent), independently
    skips a malformed sibling on ``JSONDecodeError`` (same corruption tolerance
    ``_read_legacy_account`` always had) rather than raising. This test pins
    that the in-band write completes successfully even when the legacy sibling
    is unreadable, and that promotion does not touch (let alone repair) a file
    it cannot parse.
    """
    import json as _json

    import notebooklm.auth as auth
    from notebooklm._auth.storage import promote_legacy_account

    storage = tmp_path / "storage.json"
    storage.write_text("{}")
    ctx_path = storage.with_name("context.json")
    ctx_path.write_text("{ malformed ")

    auth.write_account_metadata(storage, authuser=0, email=None)

    # The in-band record landed in storage_state.json.
    storage_data = _json.loads(storage.read_text(encoding="utf-8"))
    assert storage_data["notebooklm"]["account"]["authuser"] == 0
    # The corrupt legacy file is untouched by the write path.
    assert ctx_path.read_text(encoding="utf-8") == "{ malformed "

    # Promotion (the sole remaining legacy consumer) also tolerates it: no
    # raise, and — since in-band already has a record — nothing to promote.
    assert promote_legacy_account(storage) is False
    assert ctx_path.read_text(encoding="utf-8") == "{ malformed "


def test_stream_parser_debug_guarded_by_isenabledfor(caplog):
    """chat_stream.py — non-JSON chunk debug log is guarded before it fires."""

    # Direct: ensure the module has a guarded debug call (structural check).
    src = (SRC_ROOT / "_web" / "rows" / "chat_stream.py").read_text(encoding="utf-8")
    assert "logger.isEnabledFor(logging.DEBUG)" in src
    assert "Stream parser" in src


# ---------------------------------------------------------------------------
# SILENT sites — source-inspection meta-tests
# ---------------------------------------------------------------------------


def _file_contains_best_effort_after_except(filepath: Path, except_line: int) -> bool:
    """Return True if a `# best-effort:` comment appears within 4 lines after except_line."""
    lines = filepath.read_text(encoding="utf-8").splitlines()
    window = lines[except_line - 1 : except_line + 4]
    text = "\n".join(window)
    return "# best-effort:" in text


# (relative-to-SRC_ROOT path, except-line). Lines refer to the `except ...:`
# statement; the helper scans the 4 lines following it for `# best-effort:`.
# Note: the previous ``cli/helpers.py:596`` site (``set_current_notebook``'s
# best-effort rewrite-from-scratch) was retired — that branch now
# uses :func:`notebooklm._atomic_io.atomic_update_json` with explicit
# JSONDecodeError handling that re-runs the mutator on an empty dict.
# Note: the previous ``cli/_firefox_containers.py:364`` site
# (``_row_to_rookie_cookies_dict``'s non-numeric-expiry branch) is no longer
# silent — it now logs a warning instead of passing.
_SILENT_SITES = [
    ("cli/_firefox_containers.py", 133),
    ("notebooklm_cli.py", 66),
]


@pytest.mark.parametrize(("relpath", "except_line"), _SILENT_SITES)
def test_silent_site_has_best_effort_comment(relpath: str, except_line: int):
    """Each silent swallow site is annotated with a `# best-effort:` comment."""
    assert _file_contains_best_effort_after_except(SRC_ROOT / relpath, except_line)
