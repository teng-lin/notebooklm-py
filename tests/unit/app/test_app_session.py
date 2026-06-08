"""Tests for ``notebooklm._app.session`` — the transport-neutral session core.

Covers the three Click-free workflows that back ``use`` / ``status`` /
``auth logout``:

* :func:`verify_and_set_notebook` — resolve (injected) → ``notebooks.get`` → typed
  :class:`UseNotebookResult`, with the resolver/json-output forwarding asserted.
* :func:`read_status` — pure read+project of ``context.json`` into a
  :class:`StatusReport` (no-context, readable-payload, and unreadable-payload
  branches; ``--paths``/env-auth pass-through).
* :func:`execute_logout` — the storage-unlink → browser-rmtree → clear_context
  teardown ordering and the OSError → :class:`LogoutFailure` mapping per step,
  including the **lazy** ``context_path`` timing guard (a raising context-path
  callable must NOT abort logout before the storage/browser steps run).

Direct ``_app`` calls only — a ``MagicMock`` client + injected collaborators,
no Click / CliRunner (mirrors ``tests/unit/cli/test_label_listing.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.session import (
    LogoutFailure,
    LogoutInputs,
    LogoutOutcome,
    StatusContext,
    StatusInputs,
    StatusReport,
    UseNotebookResult,
    execute_logout,
    read_status,
    verify_and_set_notebook,
)
from notebooklm.types import Notebook

# ---------------------------------------------------------------------------
# verify_and_set_notebook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_and_set_notebook_resolves_then_gets() -> None:
    notebook = Notebook(id="nb_full_123", title="My Notebook")
    client = MagicMock()
    client.notebooks = MagicMock()
    client.notebooks.get = AsyncMock(return_value=notebook)
    resolver = AsyncMock(return_value="nb_full_123")

    result = await verify_and_set_notebook(
        client,
        "nb_full",
        json_output=False,
        resolve_notebook_id=resolver,
    )

    assert isinstance(result, UseNotebookResult)
    assert result.notebook is notebook
    assert result.resolved_id == "nb_full_123"
    # Resolver is called with the partial id + json-output forwarding.
    resolver.assert_awaited_once_with(client, "nb_full", json_output=False)
    # The resolved (not partial) id is what's verified against the server.
    client.notebooks.get.assert_awaited_once_with("nb_full_123")


@pytest.mark.asyncio
async def test_verify_and_set_notebook_forwards_json_output_flag() -> None:
    """``json_output`` is forwarded so the resolver routes diagnostics to stderr."""
    client = MagicMock()
    client.notebooks = MagicMock()
    client.notebooks.get = AsyncMock(return_value=Notebook(id="nb_1", title="t"))
    resolver = AsyncMock(return_value="nb_1")

    await verify_and_set_notebook(client, "nb", json_output=True, resolve_notebook_id=resolver)

    resolver.assert_awaited_once_with(client, "nb", json_output=True)


@pytest.mark.asyncio
async def test_verify_and_set_notebook_propagates_resolver_error() -> None:
    """A resolver ambiguity / no-match error propagates to the adapter unchanged."""
    client = MagicMock()
    client.notebooks = MagicMock()
    client.notebooks.get = AsyncMock()
    resolver = AsyncMock(side_effect=ValueError("ambiguous"))

    with pytest.raises(ValueError, match="ambiguous"):
        await verify_and_set_notebook(client, "nb", json_output=False, resolve_notebook_id=resolver)
    # The server is never hit when resolution fails.
    client.notebooks.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_and_set_notebook_propagates_get_error() -> None:
    """A ``notebooks.get`` failure (not-found/auth) propagates to the adapter."""
    client = MagicMock()
    client.notebooks = MagicMock()
    client.notebooks.get = AsyncMock(side_effect=RuntimeError("not found"))
    resolver = AsyncMock(return_value="nb_1")

    with pytest.raises(RuntimeError, match="not found"):
        await verify_and_set_notebook(client, "nb", json_output=False, resolve_notebook_id=resolver)


# ---------------------------------------------------------------------------
# read_status
# ---------------------------------------------------------------------------


def test_read_status_no_active_notebook() -> None:
    """No active id → ``has_context=False``; paths/env-auth still pass through."""
    inputs = StatusInputs(
        context_path=Path("/nonexistent/context.json"),
        notebook_id=None,
        path_info={"home": "/home/x"},
        has_env_auth=True,
    )
    report = read_status(inputs)

    assert isinstance(report, StatusReport)
    assert report.context == StatusContext(has_context=False)
    assert report.context.has_context is False
    assert report.paths == {"home": "/home/x"}
    assert report.has_env_auth is True


def test_read_status_reads_and_projects_context(tmp_path: Path) -> None:
    """A readable context file projects every field into the typed view."""
    context_file = tmp_path / "context.json"
    context_file.write_text(
        '{"title": "Notes", "is_owner": true, "created_at": "2026-01-01", '
        '"conversation_id": "conv_9"}',
        encoding="utf-8",
    )
    inputs = StatusInputs(
        context_path=context_file,
        notebook_id="nb_active",
        path_info=None,
        has_env_auth=False,
    )

    report = read_status(inputs)

    ctx = report.context
    assert ctx.has_context is True
    assert ctx.notebook_id == "nb_active"
    assert ctx.title == "Notes"
    assert ctx.is_owner is True
    assert ctx.created_at == "2026-01-01"
    assert ctx.conversation_id == "conv_9"
    assert ctx.payload_readable is True
    assert report.paths is None


def test_read_status_unreadable_payload_flags_not_readable(tmp_path: Path) -> None:
    """A corrupt/unparseable context file → has_context but payload_readable=False."""
    context_file = tmp_path / "context.json"
    context_file.write_text("{not valid json", encoding="utf-8")
    inputs = StatusInputs(
        context_path=context_file,
        notebook_id="nb_active",
        path_info=None,
        has_env_auth=False,
    )

    report = read_status(inputs)

    assert report.context.has_context is True
    assert report.context.notebook_id == "nb_active"
    assert report.context.payload_readable is False
    # No payload fields are projected when the file is unreadable.
    assert report.context.title is None
    assert report.context.conversation_id is None


def test_read_status_non_dict_context_flags_not_readable(tmp_path: Path) -> None:
    """A context file whose JSON root is a list/scalar (not an object) is the
    same failure class as corrupt JSON: payload_readable=False, no crash on
    ``data.get(...)`` (PR #1479 review)."""
    context_file = tmp_path / "context.json"
    context_file.write_text("[1, 2, 3]", encoding="utf-8")
    inputs = StatusInputs(
        context_path=context_file,
        notebook_id="nb_active",
        path_info=None,
        has_env_auth=False,
    )

    report = read_status(inputs)

    assert report.context.has_context is True
    assert report.context.notebook_id == "nb_active"
    assert report.context.payload_readable is False
    assert report.context.title is None


def test_read_status_missing_file_with_active_id_is_unreadable(tmp_path: Path) -> None:
    """An active id but a missing context file → OSError branch (payload unreadable)."""
    inputs = StatusInputs(
        context_path=tmp_path / "does_not_exist.json",
        notebook_id="nb_active",
        path_info=None,
        has_env_auth=False,
    )

    report = read_status(inputs)

    assert report.context.has_context is True
    assert report.context.payload_readable is False


def test_read_status_passes_paths_and_env_auth_through(tmp_path: Path) -> None:
    """``--paths`` info and env-auth flag survive the projection."""
    context_file = tmp_path / "context.json"
    context_file.write_text('{"title": "t"}', encoding="utf-8")
    inputs = StatusInputs(
        context_path=context_file,
        notebook_id="nb_1",
        path_info={"storage": "/s/state.json"},
        has_env_auth=True,
    )

    report = read_status(inputs)

    assert report.paths == {"storage": "/s/state.json"}
    assert report.has_env_auth is True


# ---------------------------------------------------------------------------
# execute_logout — teardown ordering + per-step OSError mapping
# ---------------------------------------------------------------------------


def _logout_inputs(
    *,
    storage_path: Path,
    browser_profile_dir: Path,
    clear_context: Callable[[], bool],
    context_path: Callable[[], Path],
    env_auth_remains: bool = False,
    rmtree: Callable[[Path], Any],
) -> LogoutInputs:
    return LogoutInputs(
        storage_path=storage_path,
        browser_profile_dir=browser_profile_dir,
        clear_context=clear_context,
        context_path=context_path,
        env_auth_remains=env_auth_remains,
        rmtree=rmtree,
    )


def test_logout_full_teardown_order(tmp_path: Path) -> None:
    """Storage unlink → browser rmtree → clear_context, all in order."""
    storage = tmp_path / "storage_state.json"
    storage.write_text("{}", encoding="utf-8")
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()

    order: list[str] = []

    def _rmtree(path: Path) -> None:
        order.append(f"rmtree:{path}")

    def _clear() -> bool:
        order.append("clear_context")
        return True

    def _ctx_path() -> Path:  # pragma: no cover - must NOT be called on success
        raise AssertionError("context_path must stay lazy on the success path")

    inputs = _logout_inputs(
        storage_path=storage,
        browser_profile_dir=browser_dir,
        clear_context=_clear,
        context_path=_ctx_path,
        rmtree=_rmtree,
    )

    outcome = execute_logout(inputs)

    assert isinstance(outcome, LogoutOutcome)
    assert outcome.failure is None
    assert outcome.removed_any is True
    # Storage file unlinked first.
    assert not storage.exists()
    # Browser rmtree ran before clear_context.
    assert order == [f"rmtree:{browser_dir}", "clear_context"]


def test_logout_no_artifacts_removed_any_false(tmp_path: Path) -> None:
    """Nothing on disk + clear_context returns False → removed_any stays False."""
    inputs = _logout_inputs(
        storage_path=tmp_path / "missing_storage.json",
        browser_profile_dir=tmp_path / "missing_browser",
        clear_context=lambda: False,
        context_path=lambda: tmp_path / "ctx.json",
        rmtree=MagicMock(),
        env_auth_remains=True,
    )

    outcome = execute_logout(inputs)

    assert outcome.removed_any is False
    assert outcome.failure is None
    assert outcome.env_auth_remains is True
    # rmtree never runs when the browser dir is absent.
    inputs.rmtree.assert_not_called()  # type: ignore[attr-defined]


def test_logout_storage_unlink_oserror_short_circuits(tmp_path: Path) -> None:
    """An OSError unlinking storage → kind='storage' failure; later steps skipped."""
    storage = MagicMock(spec=Path)
    storage.exists.return_value = True
    storage.unlink.side_effect = OSError("locked")

    rmtree = MagicMock()
    clear = MagicMock(return_value=True)

    inputs = _logout_inputs(
        storage_path=storage,
        browser_profile_dir=tmp_path / "browser",  # never reached (.exists not checked)
        clear_context=clear,
        context_path=lambda: tmp_path / "ctx.json",
        rmtree=rmtree,
    )

    outcome = execute_logout(inputs)

    assert outcome.failure is not None
    assert outcome.failure.kind == "storage"
    assert outcome.failure.path is storage
    assert "locked" in outcome.failure.error_message
    assert outcome.removed_any is False
    # Pipeline short-circuits: browser + context steps never run.
    rmtree.assert_not_called()
    clear.assert_not_called()


def test_logout_browser_rmtree_oserror_reports_partial_storage(tmp_path: Path) -> None:
    """An OSError on rmtree after storage was removed → partial_storage_removed=True."""
    storage = tmp_path / "storage_state.json"
    storage.write_text("{}", encoding="utf-8")
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()

    def _rmtree(path: Path) -> None:
        raise OSError("busy")

    clear = MagicMock(return_value=True)

    inputs = _logout_inputs(
        storage_path=storage,
        browser_profile_dir=browser_dir,
        clear_context=clear,
        context_path=lambda: tmp_path / "ctx.json",
        rmtree=_rmtree,
    )

    outcome = execute_logout(inputs)

    assert outcome.failure is not None
    assert outcome.failure.kind == "browser_profile"
    assert outcome.failure.path == browser_dir
    assert "busy" in outcome.failure.error_message
    # Storage was already removed before the rmtree failed.
    assert outcome.failure.partial_storage_removed is True
    assert outcome.removed_any is True
    # clear_context is skipped after the browser-step failure.
    clear.assert_not_called()


def test_logout_clear_context_oserror_maps_to_context_failure(tmp_path: Path) -> None:
    """clear_context raising OSError → kind='context'; context_path resolved lazily."""
    storage = tmp_path / "storage_state.json"
    storage.write_text("{}", encoding="utf-8")
    resolved_ctx = tmp_path / "context.json"

    ctx_path_calls: list[int] = []

    def _ctx_path() -> Path:
        ctx_path_calls.append(1)
        return resolved_ctx

    def _clear() -> bool:
        raise OSError("ctx locked")

    inputs = _logout_inputs(
        storage_path=storage,
        browser_profile_dir=tmp_path / "missing_browser",
        clear_context=_clear,
        context_path=_ctx_path,
        rmtree=MagicMock(),
    )

    outcome = execute_logout(inputs)

    assert outcome.failure is not None
    assert outcome.failure.kind == "context"
    assert outcome.failure.path == resolved_ctx
    assert "ctx locked" in outcome.failure.error_message
    # Storage was removed before the context step failed.
    assert outcome.removed_any is True
    # context_path is resolved exactly once — on the failure branch only.
    assert ctx_path_calls == [1]


def test_logout_context_path_is_lazy_and_does_not_abort_teardown(tmp_path: Path) -> None:
    """REGRESSION (fixed Critical): a raising ``context_path`` must NOT abort
    logout before the storage/browser teardown runs.

    The success path never invokes ``context_path``; even a callable that always
    raises must let storage-unlink + browser-rmtree complete and return a clean
    success outcome.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text("{}", encoding="utf-8")
    browser_dir = tmp_path / "browser"
    browser_dir.mkdir()

    rmtree = MagicMock()

    def _exploding_context_path() -> Path:
        raise RuntimeError("context_path must never be called on the success path")

    inputs = _logout_inputs(
        storage_path=storage,
        browser_profile_dir=browser_dir,
        clear_context=lambda: True,
        context_path=_exploding_context_path,
        rmtree=rmtree,
    )

    # Must NOT raise — the exploding context_path is never invoked here.
    outcome = execute_logout(inputs)

    assert outcome.failure is None
    assert outcome.removed_any is True
    # Storage + browser teardown both completed despite the booby-trapped lazy path.
    assert not storage.exists()
    rmtree.assert_called_once_with(browser_dir)


def test_logout_failure_is_frozen_and_typed() -> None:
    """:class:`LogoutFailure` carries a string error_message (hashable/redacted)."""
    failure = LogoutFailure(kind="storage", path=Path("/x"), error_message="boom")
    assert failure.partial_storage_removed is False
    # Frozen dataclass: assignment is rejected.
    with pytest.raises(FrozenInstanceError):
        failure.kind = "context"  # type: ignore[misc]
