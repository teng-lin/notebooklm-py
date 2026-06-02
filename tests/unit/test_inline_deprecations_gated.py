"""Each formerly-inline deprecation honors NOTEBOOKLM_QUIET_DEPRECATIONS (#1369).

The four sites that used to call ``warnings.warn(..., DeprecationWarning)``
inline — bypassing the suppression gate ADR-018 promises — now route through
``notebooklm._deprecation.warn_deprecated``. This module proves each one:

* still fires a ``DeprecationWarning`` by default, and
* goes silent when ``NOTEBOOKLM_QUIET_DEPRECATIONS`` is set.

The structural recurrence guard lives in
``tests/_lint/test_no_inline_deprecation_warnings.py``; this file pins the
user-visible suppression behavior the lint can't observe.
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


# (trigger, message-substring) for the sites whose warn fires synchronously.
SITES = [
    pytest.param(
        _from_storage_await_warns, "Awaiting NotebookLMClient.from_storage", id="from_storage_await"
    ),
    pytest.param(_research_poll_ambiguous_warns, "no task_id", id="research_poll_ambiguous"),
]


def _share_warns() -> None:
    from notebooklm._notebooks import NotebooksAPI

    api = NotebooksAPI.__new__(NotebooksAPI)
    share_manager = AsyncMock()
    share_manager.share.return_value = {"public": True}
    api._share_manager = share_manager
    asyncio.run(api.share("nb_123", public=True))


def _save_cookies_warns(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": []}', encoding="utf-8")
    # original_snapshot=None takes the legacy full-merge path that warns.
    save_cookies_to_storage(httpx.Cookies(), storage, original_snapshot=None)


@pytest.mark.parametrize(
    ("trigger", "match"), [(p.values[0], p.values[1]) for p in SITES], ids=[p.id for p in SITES]
)
def test_site_warns_by_default(trigger, match, monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
    with pytest.warns(DeprecationWarning, match=match):
        trigger()


@pytest.mark.parametrize(
    ("trigger", "match"), [(p.values[0], p.values[1]) for p in SITES], ids=[p.id for p in SITES]
)
def test_site_silent_under_quiet_env(trigger, match, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # any would fail
        trigger()


def test_share_warns_by_default(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
    with pytest.warns(DeprecationWarning, match="NotebooksAPI.share"):
        _share_warns()


def test_share_silent_under_quiet_env(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        _share_warns()


def test_save_cookies_warns_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    with pytest.warns(DeprecationWarning, match="original_snapshot"):
        _save_cookies_warns(tmp_path)


def test_save_cookies_silent_under_quiet_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        _save_cookies_warns(tmp_path)
