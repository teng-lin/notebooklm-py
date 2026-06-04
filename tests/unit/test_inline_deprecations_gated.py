"""Formerly-inline warnings honor the right category + gate (#1369).

Four sites used to call ``warnings.warn(..., DeprecationWarning)`` inline,
bypassing the suppression gate ADR-0018 promises. The fix split them by what
they actually are:

* **Three genuine scheduled deprecations** — awaiting ``from_storage(...)``,
  ambiguous ``research.poll(task_id=None)``, and ``NotebooksAPI.share()`` — now
  route through ``notebooklm._deprecation.warn_deprecated``, so each fires a
  ``DeprecationWarning`` by default and goes silent under
  ``NOTEBOOKLM_QUIET_DEPRECATIONS``.
* **One permanent back-compat shim** — ``save_cookies_to_storage`` without
  ``original_snapshot`` — was a category error. It is not a scheduled removal;
  it is a runtime safety advisory about the stale-overwrite-fresh race
  (docs/auth-cookie-lifecycle.md §3.4.1). It is now a ``RuntimeWarning`` emitted
  inline, outside ADR-0018's scope: NOT gated by ``NOTEBOOKLM_QUIET_DEPRECATIONS``.

The structural recurrence guard lives in
``tests/_lint/test_no_inline_deprecation_warnings.py``; this file pins the
user-visible category + suppression behavior the lint can't observe.
"""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from notebooklm._auth.storage import save_cookies_to_storage
from notebooklm._research import ResearchAPI
from notebooklm._types.research import ResearchStatus, ResearchTask
from notebooklm.client import NotebookLMClient, _FromStorageContext


def _two_inflight_tasks() -> list[ResearchTask]:
    return [
        ResearchTask(task_id="task_A", status=ResearchStatus.COMPLETED, query="A"),
        ResearchTask(task_id="task_B", status=ResearchStatus.COMPLETED, query="B"),
    ]


def _from_storage_await_warns() -> None:
    # __await__ warns synchronously, then returns the build generator. Close it
    # without iterating so no auth I/O runs and no "coroutine never awaited"
    # ResourceWarning leaks from the unconsumed build coroutine.
    gen = _FromStorageContext(NotebookLMClient).__await__()
    gen.close()


def _research_poll_ambiguous_warns() -> None:
    ResearchAPI._select_polled_tasks(
        _two_inflight_tasks(),
        notebook_id="nb_ambig",
        task_id=None,
        warn_on_ambiguous=True,
    )


def _share_warns() -> None:
    from notebooklm._notebooks import NotebooksAPI

    api = NotebooksAPI.__new__(NotebooksAPI)
    share_manager = AsyncMock()
    share_manager.share.return_value = {"public": True}
    api._share_manager = share_manager
    asyncio.run(api.share("nb_123", public=True))


# (trigger, message-substring) for the three genuine gated deprecations.
DEPRECATION_SITES = [
    pytest.param(
        _from_storage_await_warns, "Awaiting NotebookLMClient.from_storage", id="from_storage_await"
    ),
    pytest.param(_research_poll_ambiguous_warns, "no task_id", id="research_poll_ambiguous"),
    pytest.param(_share_warns, "NotebooksAPI.share", id="notebooks_share"),
]


@pytest.mark.parametrize(
    ("trigger", "match"),
    [(p.values[0], p.values[1]) for p in DEPRECATION_SITES],
    ids=[p.id for p in DEPRECATION_SITES],
)
def test_deprecation_site_warns_by_default(trigger, match, monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
    with pytest.warns(DeprecationWarning, match=match):
        trigger()


@pytest.mark.parametrize(
    ("trigger", "match"),
    [(p.values[0], p.values[1]) for p in DEPRECATION_SITES],
    ids=[p.id for p in DEPRECATION_SITES],
)
def test_deprecation_site_silent_under_quiet_env(trigger, match, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # any would fail
        trigger()


def _save_cookies_warns(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": []}', encoding="utf-8")
    # original_snapshot=None takes the legacy full-merge path that warns.
    save_cookies_to_storage(httpx.Cookies(), storage, original_snapshot=None)


def test_save_cookies_emits_runtime_warning_not_deprecation(tmp_path, monkeypatch):
    # Permanent back-compat shim → RuntimeWarning, NOT DeprecationWarning.
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    with pytest.warns(RuntimeWarning, match="original_snapshot") as record:
        _save_cookies_warns(tmp_path)
    assert not any(issubclass(w.category, DeprecationWarning) for w in record)


def test_save_cookies_warning_is_NOT_gated_by_quiet_env(tmp_path, monkeypatch):
    # It left ADR-0018's scope, so NOTEBOOKLM_QUIET_DEPRECATIONS must NOT
    # silence it — the race advisory always fires on the unsafe path.
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    with pytest.warns(RuntimeWarning, match="original_snapshot"):
        _save_cookies_warns(tmp_path)
