"""Tests for Phase 1 T3 — categorized observability at 12 swallowed-exception sites.

See .sisyphus/plans/phase-1-implementation.md for the inventory and rationale.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Path to the repo's src/notebooklm/ — used by the silent-site source inspection tests.
SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"


# ---------------------------------------------------------------------------
# WARNING sites — drift detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_source_ids_warns_on_top_level_shape_drift(caplog):
    """_core.py:get_source_ids — non-list at notebook_data[0] triggers WARNING."""
    from notebooklm._core import ClientCore

    core = ClientCore.__new__(ClientCore)
    core.rpc_call = AsyncMock(return_value=[{"unexpected": "dict"}])

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await core.get_source_ids("nb_drift")

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
    """_core.py:get_source_ids — notebook_info[1] not list triggers WARNING."""
    from notebooklm._core import ClientCore

    core = ClientCore.__new__(ClientCore)
    # notebook_data[0] is a list of length >1 but [1] is not a list
    core.rpc_call = AsyncMock(return_value=[[None, "not a list", "x"]])

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await core.get_source_ids("nb_inner")

    assert result == []
    assert any("schema drift" in r.message and "nb_inner" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_get_source_ids_happy_path_no_warning(caplog):
    """Well-formed payload extracts source ids and emits no warning."""
    from notebooklm._core import ClientCore

    core = ClientCore.__new__(ClientCore)
    core.rpc_call = AsyncMock(return_value=[[None, [[["src_alpha"]], [["src_beta"]]]]])

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        result = await core.get_source_ids("nb_happy")

    assert result == ["src_alpha", "src_beta"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_qa_pairs_warns_on_unguarded_shape(caplog):
    """_chat.py: QA-pair parser warns when next_turn[4] is not indexable."""
    from notebooklm._chat import ChatAPI

    # next_turn[4] is a string — string[0] returns a char (no error), but
    # string[0][0] raises IndexError on empty char access. Use a value
    # whose [0][0] raises TypeError: a non-subscriptable nested object.
    # Simpler: next_turn[4] is None → None[0] → TypeError.
    turns_data = [
        [
            [None, None, 1, "what?"],  # question turn (type=1)
            [None, None, 2, None, None],  # answer turn (type=2); next_turn[4] is None
        ]
    ]

    chat = ChatAPI.__new__(ChatAPI)
    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        # Direct call to the private parser
        pairs = chat._parse_turns_to_qa_pairs(turns_data)  # type: ignore[arg-type]

    # Got at least the question (answer is empty due to except)
    assert pairs == [("what?", "")]
    # After T8.D2b, _chat._extract_next_turn_content delegates to safe_index,
    # which emits its own canonical "safe_index drift" warning (rather than
    # the older _chat-specific "schema drift" wording). Accept either so
    # this test survives future helper renames.
    assert any(
        ("schema drift" in r.message or "safe_index drift" in r.message)
        and r.levelno == logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_summary_warns_on_indexerror_drift(caplog, monkeypatch):
    """_notebooks.py: summary extraction warns when result[0][0][0] raises.

    Tier-1 T1.B2 migrated this site to ``safe_index``; the warning message
    now comes from ``notebooklm.rpc._safe_index`` and carries the call-site
    label ``source='_notebooks.get_summary'`` instead of the notebook id.
    """
    from notebooklm._notebooks import NotebooksAPI

    # Pin soft mode so safe_index warns instead of raising.
    monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)

    api = NotebooksAPI.__new__(NotebooksAPI)
    mock_core = MagicMock()
    # result[0][0] is a string, so [0] returns a char; result[0][0][0] is fine
    # We need a shape that raises IndexError. result[0] is an empty list →
    # result[0][0] raises IndexError.
    mock_core.rpc_call = AsyncMock(return_value=[[]])
    api._core = mock_core

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        summary = await api.get_summary("nb_summary")

    assert summary == ""
    assert any(
        "safe_index drift" in r.message and "_notebooks.get_summary" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# DEBUG sites
# ---------------------------------------------------------------------------


# Removed: ``test_retry_after_non_integer_logs_debug`` was self-fulfilling — it
# called ``core_mod.logger.debug(...)`` inline rather than exercising production
# code. Phase 3 replaced the original "Retry-After header not an integer" log
# site with the ``_parse_retry_after`` helper, which returns ``None`` silently
# for unparseable input. Parse semantics are covered by
# ``tests/unit/test_retry_after.py``.


@pytest.mark.asyncio
async def test_description_partial_summary_logs_debug(caplog):
    """_notebooks.py:273 — partial summary (no topics) logs at DEBUG."""
    from notebooklm._notebooks import NotebooksAPI

    api = NotebooksAPI.__new__(NotebooksAPI)
    mock_core = MagicMock()
    # outer[0][0] works but outer[1] raises (no topics shape)
    mock_core.rpc_call = AsyncMock(return_value=[[["the summary"]]])
    api._core = mock_core

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

    After T3.E the lock-protected ``atomic_update_json`` surfaces the
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


def test_auth_context_unreadable_recovers_under_lock(tmp_path):
    """auth.py — unreadable account context is recovered under the lock.

    Previously the recovery path lived outside the lock and emitted a DEBUG
    log before unlink-and-retry. After PR #465 the recovery is silent and
    happens inside :func:`atomic_update_json` via ``recover_from_corrupt``,
    so the structural assertion is now: the corrupt file is rewritten in
    place with a valid payload containing only our new metadata.
    """
    import json as _json

    import notebooklm.auth as auth

    storage = tmp_path / "storage.json"
    storage.write_text("{}")
    ctx_path = auth._account_context_path(storage)
    ctx_path.write_text("{ malformed ")

    auth.write_account_metadata(storage, authuser=0, email=None)

    # File is now valid JSON, and only contains the account-metadata key —
    # proving recovery treated the corrupt payload as an empty dict.
    data = _json.loads(ctx_path.read_text(encoding="utf-8"))
    assert auth._ACCOUNT_CONTEXT_KEY in data
    assert data[auth._ACCOUNT_CONTEXT_KEY]["authuser"] == 0


def test_stream_parser_debug_guarded_by_isenabledfor(caplog):
    """_chat_protocol.py — non-JSON chunk debug log is guarded before it fires."""

    # Direct: ensure the module has a guarded debug call (structural check).
    src = (SRC_ROOT / "_chat_protocol.py").read_text(encoding="utf-8")
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
# best-effort rewrite-from-scratch) was retired in T3.E — that branch now
# uses :func:`notebooklm._atomic_io.atomic_update_json` with explicit
# JSONDecodeError handling that re-runs the mutator on an empty dict.
_SILENT_SITES = [
    ("cli/_firefox_containers.py", 133),
    ("cli/_firefox_containers.py", 364),
    ("notebooklm_cli.py", 66),
]


@pytest.mark.parametrize(("relpath", "except_line"), _SILENT_SITES)
def test_silent_site_has_best_effort_comment(relpath: str, except_line: int):
    """Each silent swallow site is annotated with a `# best-effort:` comment."""
    assert _file_contains_best_effort_after_except(SRC_ROOT / relpath, except_line)
