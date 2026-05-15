"""Regression test for T7.D4 — download paths must not block the event loop.

Audit item #30 (`thread-safety-concurrency-audit.md` §30):

> `_download_urls_batch()` and `_download_url()` call `load_httpx_cookies()`
> (synchronous JSON read) directly from `async def`. `download_report()`
> and `download_mind_map()` call `Path.write_text()` directly on the loop.
> Slow storage / large payloads stall every other concurrent task.

This module pins the post-fix invariant: each blocking sync call site
must execute via ``asyncio.to_thread`` (or an equivalent offload) so a
slow filesystem cannot freeze sibling coroutines for the duration of
the call.

Assertion methodology — thread-id capture
-----------------------------------------

Each test patches the production call site (either the ``to_thread``
target itself or a method called from inside the ``to_thread``
closure) with a recording stub that captures ``threading.get_ident()``.
After the download runs, the test asserts the captured thread id
differs from the loop thread id. If the production wrap
(``await asyncio.to_thread(...)``) is in place, the stub runs on the
default ThreadPoolExecutor and the ids differ. If a regression removes
the wrap, the stub runs on the loop thread and the ids match.

Why not measure scheduler responsiveness directly (heartbeat-gap)
.................................................................

An earlier version of these tests fired a 10 ms heartbeat coroutine
during the download and asserted the max gap between heartbeat ticks
stayed below a threshold. That pattern proved flaky on shared CI:

* macOS 3.14:    ~55 ms green-run jitter (the original tuning point).
* Ubuntu 3.11:   170.8 ms green-run jitter (PR #621 run 25928433246).
* Windows 3.10-14: 170-220 ms typical, 2594 ms outlier on Win 3.11.

A 200 ms regression signal sits inside the 170-220 ms baseline noise
on Linux + Windows runners — no single threshold can discriminate
"offloaded" from "regressed." Widening the stub trades wall time for
the same problem at the next jitter level.

The thread-id check is a *positive* assertion of the property the
heartbeat-gap was inferring. It needs no threshold, no scheduler
timing, and survives every matrix entry uniformly.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.types import ArtifactDownloadError

# T8.D11 — mock-based loop-blocking detection tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _assert_offloaded_to_worker_thread(
    captured_thread_id: int | None,
    loop_thread_id: int,
    *,
    call_site: str,
    wrap_target: str,
) -> None:
    """Assert ``captured_thread_id`` came from a worker thread, not the loop.

    Args:
        captured_thread_id: The thread id observed inside the patched stub,
            or ``None`` if the stub never ran.
        loop_thread_id: ``threading.get_ident()`` captured on the loop
            thread before the download was awaited.
        call_site: Human-readable name of the production async function
            being tested (e.g. ``"_download_url"``), used in the failure
            message so the diagnostic points at the right code.
        wrap_target: Human-readable name of the synchronous call that
            must be offloaded (e.g. ``"load_httpx_cookies"``).
    """
    assert captured_thread_id is not None, (
        f"{call_site}'s {wrap_target} stub never ran — check the patch target."
    )
    assert captured_thread_id != loop_thread_id, (
        f"{call_site} ran {wrap_target} on the event-loop thread "
        f"(thread id {captured_thread_id}). It must be wrapped in "
        "asyncio.to_thread so slow synchronous I/O cannot stall "
        "concurrent tasks (T7.D4, audit §30)."
    )


@pytest.fixture
def mock_artifacts_api() -> tuple[ArtifactsAPI, MagicMock]:
    """``ArtifactsAPI`` wired to a mock ``ClientCore``.

    Same shape as the unit-test fixture in ``tests/unit/test_artifact_downloads.py``
    so future readers can cross-reference the protocol shaping. We keep
    a local copy here because importing across the unit/integration
    boundary in pytest is fragile when both define ``mock_artifacts_api``
    at module scope.
    """
    mock_core = MagicMock()
    mock_core.rpc_call = AsyncMock()
    mock_core.get_source_ids = AsyncMock(return_value=[])
    api = ArtifactsAPI(mock_core)
    return api, mock_core


@pytest.mark.asyncio
async def test_download_report_runs_write_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``download_report`` must offload its ``Path.write_text`` to a thread.

    The production path wraps ``output.write_text(...)`` inside an
    ``asyncio.to_thread(_write_markdown)`` closure. We patch
    ``Path.write_text`` with a recording stub that captures the thread
    id on which it runs and still performs the real write (so the
    file-exists sanity check at the end stays meaningful). If a
    regression removes the wrap, the recording stub runs on the loop
    thread and the assertion fires.
    """
    api, _ = mock_artifacts_api
    output_path = tmp_path / "report.md"

    # Minimal "completed report" shape that `_select_artifact` will accept.
    # See ``tests/unit/test_artifact_downloads.py::TestDownloadReport`` for
    # the canonical structure; index 7 is the markdown payload.
    report_artifact_list = [
        [
            "report_001",  # id
            "Report Title",  # title
            2,  # type code: REPORT
            None,
            3,  # status: COMPLETED
            None,
            None,
            ["# Test Report\n\nT7.D4 regression body."],  # markdown content
        ]
    ]

    loop_thread_id = threading.get_ident()
    original_write_text = Path.write_text
    captured: list[int] = []

    def recording_write_text(self: Path, *args: object, **kwargs: object) -> int:
        captured.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list,
        patch.object(Path, "write_text", recording_write_text),
    ):
        mock_list.return_value = report_artifact_list
        result = await api.download_report("nb_t7d4", str(output_path))

    assert result == str(output_path)
    assert output_path.exists(), "download_report should still produce the file"

    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="download_report",
        wrap_target="Path.write_text",
    )


@pytest.mark.asyncio
async def test_download_mind_map_runs_write_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``download_mind_map`` must offload its JSON write to a thread.

    The production path wraps ``json.dump(...)`` inside an
    ``asyncio.to_thread(_write_json)`` closure. A legacy alternative
    that used ``Path.write_text`` is also patched, so a refactor that
    rewrites the write API in either direction is still covered. We
    require AT LEAST ONE of the two write APIs to fire (the production
    path uses ``json.dump``; ``Path.write_text`` is the fallback) and
    that the firing site ran on a worker thread, not the loop.

    Originally pointed out by coderabbit on PR #579: patching only
    ``Path.write_text`` would silently miss the production ``json.dump``
    path.
    """
    import notebooklm._artifacts as artifacts_module

    api, _ = mock_artifacts_api
    output_path = tmp_path / "mindmap.json"

    json_content = json.dumps({"name": "Root", "children": [{"name": "T7.D4"}]})
    # Shape matches the canonical mind-map row used elsewhere in the test
    # suite: index 1 holds the [meta, content_str] pair.
    mind_map_rows = [
        [
            "mindmap_001",  # mm[0] = id
            [None, json_content],  # mm[1][1] = JSON string
            None,
            None,
            "Mind Map Title",  # mm[4] = title
        ]
    ]

    loop_thread_id = threading.get_ident()
    original_json_dump = json.dump
    original_write_text = Path.write_text
    captured_json: list[int] = []
    captured_write: list[int] = []

    def recording_json_dump(*args: object, **kwargs: object) -> None:
        captured_json.append(threading.get_ident())
        return original_json_dump(*args, **kwargs)  # type: ignore[arg-type]

    def recording_write_text(self: Path, *args: object, **kwargs: object) -> int:
        captured_write.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch(
            "notebooklm._artifacts._mind_map.list_mind_maps",
            new=AsyncMock(return_value=mind_map_rows),
        ),
        # Patch the `json` module as imported by `_artifacts` so the
        # closure inside `download_mind_map` resolves to the stub.
        patch.object(artifacts_module.json, "dump", recording_json_dump),
        # Cover the legacy ``Path.write_text``-based path too so a
        # rewrite either direction is caught by this test.
        patch.object(Path, "write_text", recording_write_text),
    ):
        result = await api.download_mind_map("nb_t7d4", str(output_path))

    assert result == str(output_path)
    assert output_path.exists(), "download_mind_map should still produce the file"

    # Require the firing write site to have run off-loop. The production
    # code uses json.dump; if a future refactor swaps to Path.write_text
    # the other capture list catches it.
    captured = captured_json or captured_write
    wrap_target = "json.dump" if captured_json else "Path.write_text"
    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="download_mind_map",
        wrap_target=wrap_target,
    )


@pytest.mark.asyncio
async def test_concurrent_downloads_both_offload_writes(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """End-to-end fan-out: report + mind-map concurrently must both offload.

    Integration-flavored cousin of the two single-call tests above. We
    fan out one ``download_report`` and one ``download_mind_map`` under
    ``asyncio.gather`` and require BOTH write sites — ``Path.write_text``
    (report) and ``json.dump`` (mind-map) — to have run on a worker
    thread. A regression on either path leaves its capture matching the
    loop thread and fails the assertion.
    """
    import notebooklm._artifacts as artifacts_module

    api, _ = mock_artifacts_api
    report_path = tmp_path / "report.md"
    mindmap_path = tmp_path / "mindmap.json"

    report_artifact_list = [
        [
            "report_002",
            "Report Title",
            2,
            None,
            3,
            None,
            None,
            ["# Fanout Report\n\nT7.D4 concurrent body."],
        ]
    ]
    mind_map_rows = [
        [
            "mindmap_002",
            [None, json.dumps({"name": "FanoutRoot"})],
            None,
            None,
            "Fanout Mind Map",
        ]
    ]

    loop_thread_id = threading.get_ident()
    original_write_text = Path.write_text
    original_json_dump = json.dump
    captured_write: list[int] = []
    captured_json: list[int] = []

    def recording_write_text(self: Path, *args: object, **kwargs: object) -> int:
        captured_write.append(threading.get_ident())
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def recording_json_dump(*args: object, **kwargs: object) -> None:
        captured_json.append(threading.get_ident())
        return original_json_dump(*args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list,
        patch(
            "notebooklm._artifacts._mind_map.list_mind_maps",
            new=AsyncMock(return_value=mind_map_rows),
        ),
        patch.object(Path, "write_text", recording_write_text),
        patch.object(artifacts_module.json, "dump", recording_json_dump),
    ):
        mock_list.return_value = report_artifact_list
        report_result, mindmap_result = await asyncio.gather(
            api.download_report("nb_t7d4", str(report_path)),
            api.download_mind_map("nb_t7d4", str(mindmap_path)),
        )

    assert report_result == str(report_path)
    assert mindmap_result == str(mindmap_path)
    assert report_path.exists()
    assert mindmap_path.exists()

    _assert_offloaded_to_worker_thread(
        captured_write[0] if captured_write else None,
        loop_thread_id,
        call_site="download_report (concurrent)",
        wrap_target="Path.write_text",
    )
    _assert_offloaded_to_worker_thread(
        captured_json[0] if captured_json else None,
        loop_thread_id,
        call_site="download_mind_map (concurrent)",
        wrap_target="json.dump",
    )


@pytest.mark.asyncio
async def test_download_urls_batch_cookie_load_runs_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
) -> None:
    """``_download_urls_batch`` must offload its ``load_httpx_cookies`` call.

    Empty URL list keeps the test sealed from the network — the only
    work between the cookie load and the return is opening + closing
    an ``httpx.AsyncClient``, which doesn't touch the network until
    the first request.
    """
    api, _ = mock_artifacts_api
    api._storage_path = tmp_path / "fake_storage_state.json"

    loop_thread_id = threading.get_ident()
    captured: list[int] = []

    def recording_load_httpx_cookies(path: object = None) -> dict:
        captured.append(threading.get_ident())
        return {}

    with patch(
        "notebooklm._artifacts.load_httpx_cookies",
        new=recording_load_httpx_cookies,
    ):
        result = await api._download_urls_batch([])

    # Sanity: empty input → empty result, no failures fabricated.
    assert result.succeeded == []
    assert result.failed == []

    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="_download_urls_batch",
        wrap_target="load_httpx_cookies",
    )


@pytest.mark.asyncio
async def test_download_url_cookie_load_runs_off_loop_thread(
    mock_artifacts_api: tuple[ArtifactsAPI, MagicMock],
    tmp_path: Path,
    httpx_mock,
) -> None:
    """``_download_url`` must offload its ``load_httpx_cookies`` call.

    The subsequent HTTP request is intercepted by ``httpx_mock`` with a
    404 so ``_download_url`` raises ``ArtifactDownloadError`` (and the
    test stays sealed from the real network — the URL must still match
    the production trusted-domain whitelist to clear validation, but no
    bytes hit the wire). The thread-id capture has already happened by
    the time the HTTP step runs.
    """
    api, _ = mock_artifacts_api
    api._storage_path = tmp_path / "fake_storage_state.json"
    output_path = tmp_path / "download.bin"

    # URL must clear the production trusted-domain check
    # (``.googleapis.com``) BEFORE ``load_httpx_cookies`` runs — the
    # validation happens first, and a rejected URL would raise
    # ``ArtifactDownloadError("Untrusted download domain")`` before the
    # cookie load and leave ``captured`` empty (turning this test into
    # a false negative). The path is arbitrary because ``httpx_mock``
    # intercepts the request before it leaves the process.
    url = "https://storage.googleapis.com/never-resolved-t7d4.bin"
    httpx_mock.add_response(url=url, status_code=404)

    loop_thread_id = threading.get_ident()
    captured: list[int] = []

    def recording_load_httpx_cookies(path: object = None) -> dict:
        captured.append(threading.get_ident())
        return {}

    with (
        patch(
            "notebooklm._artifacts.load_httpx_cookies",
            new=recording_load_httpx_cookies,
        ),
        pytest.raises(ArtifactDownloadError),
    ):
        await api._download_url(url, str(output_path))

    _assert_offloaded_to_worker_thread(
        captured[0] if captured else None,
        loop_thread_id,
        call_site="_download_url",
        wrap_target="load_httpx_cookies",
    )
