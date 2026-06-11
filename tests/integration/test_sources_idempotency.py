"""Variant-keyed idempotency tests for ADD_SOURCE + ADD_SOURCE_FILE.

Tier 9 Wave 2 (P0-3-sources, P1-2-sources): the previous behavior in
``_source/add.py``/``_source/upload.py`` relied on the inner transport
retry loop to handle 5xx for mutating create RPCs, which could duplicate
sources when the server already committed the write before returning the
5xx. The fix is two-fold:

1. Variant-keyed registry entries — ``(ADD_SOURCE, "url"|"drive")`` and
   ``(ADD_SOURCE_FILE, None)`` flip to ``PROBE_THEN_CREATE``;
   ``(ADD_SOURCE, "text")`` flips to ``NON_IDEMPOTENT_NO_RETRY``. The
   registry forces ``disable_internal_retries=True`` at the executor.

2. Probe-then-create wrappers — for the three PROBE_THEN_CREATE variants,
   ``idempotent_create`` issues a single create attempt, and on a
   retryable transport error (5xx / 429 / network) runs a probe before
   the second attempt. The probe is variant-specific:

     - ``add_url`` probes by ``source.url == url``
     - ``add_drive`` probes by ``/d/<file_id>`` URL-segment marker with a
       trailing boundary (avoids interior-substring + prefix-collision
       false-positives)
     - ``register_file_source`` probes by baseline-diff + ``source.title ==
       filename`` (filenames are not identity-bearing, so the wrapper
       captures source-ids before the create and filters probe matches to
       sources that appeared after the create started)

   The "commit-lost-response" test sequence is: first call returns 200
   (server commits + returns success), second call returns 503 (lost
   response from a third-party retry); the probe short-circuits and the
   wrapper returns the existing source without re-issuing the create.

3. Probe-failure propagation — a probe that raises ``NetworkError``
   (transport-layer) must propagate so the caller sees the original
   failure mode, not a silent retry on top of a broken probe.

These tests use mock ``httpx.MockTransport`` (NOT VCR cassettes) per the
Codex iter-1 critique: cassette-based replay can't model the "first call
returns 200, second call returns 503" sequence because each route is
flatly keyed by request shape, not call order.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import notebooklm._runtime.helpers as _runtime_helpers
from notebooklm import NotebookLMClient
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.exceptions import NetworkError, NotebookLMError
from notebooklm.rpc import RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test

# Mock-transport idempotency tests; no HTTP, no cassette. Opt out of the
# tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


# ---------------------------------------------------------------------------
# Helpers (mirror tests/integration/concurrency/test_idempotency_create.py)
# ---------------------------------------------------------------------------


def _wrb_response(rpc_id: str, payload) -> str:
    """Build a single-RPC batchexecute response body."""
    inner = json.dumps(payload)
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _get_notebook_with_sources_response(
    notebook_id: str,
    sources: list[tuple[str, str, str | None]],
) -> str:
    """Build a GET_NOTEBOOK response that ``SourcesAPI.list`` parses.

    ``sources`` is ``[(source_id, title, url_or_None), ...]``. The metadata
    layout matches the parsing path in ``Source.from_api_response``: ``url``
    at index ``[7]`` when present (matches ``_extract_source_url`` precedence
    with ``allow_bare_http=False``).
    """
    src_rows: list = []
    for src_id, title, url in sources:
        metadata: list = [None] * 8
        if url is not None:
            metadata[7] = [url]
        status_block = [None, 2]  # READY
        src_rows.append([[src_id], title, metadata, status_block])
    nb_info = ["Test Notebook", src_rows]
    return _wrb_response(RPCMethod.GET_NOTEBOOK.value, [nb_info])


def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Construct a NotebookLMClient backed by a mock transport.

    Mirrors the helper used in tests/integration/concurrency/
    test_idempotency_create.py: stub in a pre-built httpx.AsyncClient
    wired to the supplied mock transport, bypassing the full
    ``ClientLifecycle.open()`` path that would otherwise build a real connection
    pool.
    """
    client = NotebookLMClient(
        auth_tokens,
        server_error_max_retries=server_error_max_retries,
    )
    install_http_client_for_test(
        client._collaborators.kernel,
        httpx.AsyncClient(
            transport=transport,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        ),
    )
    return client


def _rpc_id_in_request(request: httpx.Request) -> str | None:
    for key, value in request.url.params.multi_items():
        if key == "rpcids":
            return value
    return None


# ---------------------------------------------------------------------------
# add_url — commit-lost-response (PROBE_THEN_CREATE, variant="url")
# ---------------------------------------------------------------------------


async def test_add_url_probe_short_circuits_when_first_response_lost(auth_tokens) -> None:
    """First ADD_SOURCE call commits server-side but client sees 503; probe wins.

    Models the commit-lost-response failure mode: the server processed the
    create successfully, but the response was lost (e.g. proxy timeout
    returned 503 to the caller). The probe finds the new source already
    landed and returns it; only ONE ADD_SOURCE actually fires.
    """
    notebook_id = "nb_test"
    url = "https://example.com/article"
    src_id = "src_lost_response"

    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            # First call (the server committed but the response was lost)
            # — the client sees a 503.
            return httpx.Response(503, text="service unavailable")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, [(src_id, "Article", url)]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, url)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert source.url == url
    # Exactly ONE ADD_SOURCE (no naive re-POST after the 503)
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    # Exactly ONE GET_NOTEBOOK (the probe)
    assert get_count == 1, f"expected 1 GET_NOTEBOOK probe, got {get_count}"


# ---------------------------------------------------------------------------
# add_drive — commit-lost-response (PROBE_THEN_CREATE, variant="drive")
# ---------------------------------------------------------------------------


async def test_add_drive_probe_short_circuits_when_first_response_lost(auth_tokens) -> None:
    """Drive sources: 503 + ``/d/<file_id>`` segment probe returns existing source.

    Drive sources canonically embed the file_id as a path segment of
    ``source.url`` (typical shape: ``https://docs.google.com/document/d/<file_id>/edit``).
    The probe matches by ``/d/<file_id>/`` segment marker (with trailing
    boundary) so neither interior-substring nor prefix collisions can
    spuriously match — see the dedicated false-positive tests below.
    """
    notebook_id = "nb_test"
    file_id = "drive_file_abc123xyz"
    title = "My Drive Doc"
    src_id = "src_drive_lost"
    drive_url = f"https://docs.google.com/document/d/{file_id}/edit"

    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, [(src_id, title, drive_url)]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_drive(notebook_id, file_id, title)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert file_id in (source.url or "")
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    assert get_count == 1, f"expected 1 GET_NOTEBOOK probe, got {get_count}"


async def test_add_drive_probe_matches_segment_at_end_of_url(auth_tokens) -> None:
    """Drive probe correctly handles ``/d/<file_id>`` at the very end of the URL.

    Some Drive URLs are stored without a trailing ``/edit`` or other path
    suffix (e.g. ``https://docs.google.com/document/d/<file_id>``). The
    end-of-string branch of the probe ensures these still match.
    """
    notebook_id = "nb_test"
    file_id = "drive_file_xyz123"
    title = "Untrailing Drive Doc"
    src_id = "src_drive_no_trailing"
    drive_url = f"https://docs.google.com/document/d/{file_id}"  # no trailing /

    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, [(src_id, title, drive_url)]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_drive(notebook_id, file_id, title)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    assert get_count == 1, f"expected 1 GET_NOTEBOOK probe, got {get_count}"


@pytest.mark.parametrize(
    "other_drive_url",
    [
        # Interior substring: contains ``abc`` but not as a ``/d/abc/`` segment.
        "https://docs.google.com/document/d/xabcy/edit",
        # Prefix collision: another file_id begins with the target file_id.
        # ``/d/abc`` IS a substring of ``/d/abcdef/edit`` but the trailing
        # boundary check rejects it.
        "https://docs.google.com/document/d/abcdef/edit",
    ],
    ids=["interior_substring", "prefix_collision"],
)
async def test_add_drive_probe_does_not_substring_match_unrelated_file_id(
    auth_tokens, other_drive_url: str
) -> None:
    """Drive probe uses ``/d/<file_id>/`` segment match with trailing boundary.

    Regression guard against two false-positive shapes the probe must reject:

    * **Interior substring**: ``/d/xabcy/`` contains ``abc`` as a naked
      substring, so the original ``file_id in source.url`` check would
      spuriously match.
    * **Prefix collision**: ``/d/abc`` is also a substring of ``/d/abcdef/``,
      so the simpler ``/d/<file_id>`` segment marker (without a trailing
      boundary) would still false-positive. Real-world Drive IDs are 33–44
      character Base64URL strings making prefix collisions astronomically
      unlikely, but the boundary check is essentially free.

    In both cases the probe must return ``None`` so ``idempotent_create``
    retries the create rather than returning the unrelated source.
    """
    from notebooklm.exceptions import ServerError

    notebook_id = "nb_test"
    target_file_id = "abc"  # short file_id chosen to maximize collision chance
    title = "My Drive Doc"

    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(
                    notebook_id, [("src_other", title, other_drive_url)]
                ),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_drive(notebook_id, target_file_id, title)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # The probe must NOT have spuriously matched the unrelated Drive
    # source — instead the wrapper retried, exhausted, and re-raised.
    assert add_count >= 1


# ---------------------------------------------------------------------------
# register_file_source (ADD_SOURCE_FILE) — commit-lost-response
# (PROBE_THEN_CREATE, variant=None)
# ---------------------------------------------------------------------------


async def test_register_file_source_probe_short_circuits_when_first_response_lost(
    auth_tokens, tmp_path: Path
) -> None:
    """File uploads: 503 on ADD_SOURCE_FILE + baseline-diff probe returns new source.

    The full add_file flow is three steps (register → start_resumable → finalize).
    Here we exercise only the register step's idempotency: the test patches
    the upload stages to no-op so the probe-then-retry behavior of
    ``register_file_source`` is observable in isolation.

    Because filenames are not identity-bearing (two uploads of ``report.pdf``
    are legitimately two distinct sources), the probe uses a baseline-diff
    pattern: it captures source IDs BEFORE the create attempt and only
    counts sources that appear AFTER the create as "the upload landed."
    This test exercises the typical case: baseline returns no matching
    sources, the create gets 503, the probe finds the new source, and the
    wrapper short-circuits with exactly 1 ADD_SOURCE_FILE call.
    """
    notebook_id = "nb_test"
    filename = "my_document.pdf"
    src_id = "src_file_lost"

    test_file = tmp_path / filename
    test_file.write_bytes(b"%PDF-1.4 minimal pdf")

    register_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal register_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE_FILE.value:
            register_count += 1
            return httpx.Response(503, text="service unavailable")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            # Call 1: baseline — empty notebook (no pre-existing file).
            # Call 2: probe after the 503 — the new source has landed
            # server-side; the wrapper should return its id without retrying.
            if get_count == 1:
                return httpx.Response(
                    200,
                    text=_get_notebook_with_sources_response(notebook_id, []),
                )
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, [(src_id, filename, None)]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        # Stub start_resumable_upload + upload_file_streaming so this test
        # exercises only the ADD_SOURCE_FILE register step's idempotency.
        with (
            patch.object(
                client.sources._uploader,
                "start_resumable_upload",
                AsyncMock(return_value="https://upload.example/scotty"),
            ),
            patch.object(
                client.sources._uploader,
                "upload_file_streaming",
                AsyncMock(return_value=None),
            ),
        ):
            source = await client.sources.add_file(notebook_id, test_file)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert source.id == src_id
    # Exactly ONE ADD_SOURCE_FILE register request (no naive re-POST)
    assert register_count == 1, f"expected 1 ADD_SOURCE_FILE, got {register_count}"
    # TWO GET_NOTEBOOK calls: baseline + probe-after-failure
    assert get_count == 2, f"expected 2 GET_NOTEBOOK calls (baseline + probe), got {get_count}"


async def test_register_file_source_does_not_match_pre_existing_filename(
    auth_tokens, tmp_path: Path
) -> None:
    """File uploads: baseline-diff prevents matching a pre-existing same-named source.

    Regression guard for the original Codex critical finding: filenames are
    NOT identity-bearing. If the notebook already has ``report.pdf`` from a
    previous upload and the user calls ``add_file(notebook_id, report.pdf)``
    again, a transport failure during the second register must NOT cause
    the wrapper to return the OLD source's id — that would silently
    redirect the new upload onto the existing source.

    Scenario:
      - baseline list returns the pre-existing ``report.pdf`` source
      - create gets 503 (no second source landed server-side either)
      - probe list returns the SAME pre-existing source
      - filtered by baseline_ids, the probe finds zero "new" matches
      - the wrapper retries the create, which 503s again, and exhausts
        attempts → original ServerError surfaces

    The load-bearing assertion is that the wrapper does NOT return the
    pre-existing source's id under any failure mode.
    """
    from notebooklm.exceptions import ServerError

    notebook_id = "nb_test"
    filename = "report.pdf"
    pre_existing_src_id = "src_OLD_report"

    test_file = tmp_path / filename
    test_file.write_bytes(b"%PDF-1.4 minimal pdf")

    register_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal register_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE_FILE.value:
            register_count += 1
            return httpx.Response(503, text="service unavailable")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            # Both baseline and probe return the SAME pre-existing source.
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(
                    notebook_id, [(pre_existing_src_id, filename, None)]
                ),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with (
            patch.object(
                client.sources._uploader,
                "start_resumable_upload",
                AsyncMock(return_value="https://upload.example/scotty"),
            ),
            patch.object(
                client.sources._uploader,
                "upload_file_streaming",
                AsyncMock(return_value=None),
            ),
            pytest.raises(ServerError),
        ):
            await client.sources.add_file(notebook_id, test_file)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # The pre-existing source's id was never returned — instead, the
    # original transport error propagated after retries were exhausted.
    # The exact register_count is implementation-defined (idempotent_create
    # default is 2 attempts), but it must be at least 1.
    assert register_count >= 1, f"expected ≥1 ADD_SOURCE_FILE, got {register_count}"
    assert get_count >= 1, f"expected ≥1 GET_NOTEBOOK, got {get_count}"


async def test_register_file_source_baseline_unavailable_raises_on_ambiguity(
    auth_tokens, tmp_path: Path
) -> None:
    """Baseline fetch failure + same-name match → raise ``SourceAddError``.

    When the baseline GET_NOTEBOOK fails (e.g. transient 5xx) AND the probe
    later finds a same-named source, the wrapper cannot safely distinguish
    "this upload landed" from "a pre-existing source has the same filename."
    Surfacing this as an ambiguity is the correct behavior — silently
    returning the existing source would direct the subsequent upload stream
    to the wrong source (the original CodeRabbit critical concern).

    Scenario:
      - baseline list raises (server 503)
      - create gets 503
      - probe lists notebook → finds same-named source
      - baseline_ids is None (sentinel) → wrapper raises SourceAddError
        rather than returning the existing source's id
    """
    notebook_id = "nb_test"
    filename = "report.pdf"
    pre_existing_src_id = "src_OLD_report"

    test_file = tmp_path / filename
    test_file.write_bytes(b"%PDF-1.4 minimal pdf")

    register_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal register_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE_FILE.value:
            register_count += 1
            return httpx.Response(503, text="service unavailable")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            # First call (baseline) — 503 to simulate transport failure.
            # Subsequent calls (probe) — return the pre-existing source.
            if get_count == 1:
                return httpx.Response(503, text="service unavailable")
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(
                    notebook_id, [(pre_existing_src_id, filename, None)]
                ),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with (
            patch.object(
                client.sources._uploader,
                "start_resumable_upload",
                AsyncMock(return_value="https://upload.example/scotty"),
            ),
            patch.object(
                client.sources._uploader,
                "upload_file_streaming",
                AsyncMock(return_value=None),
            ),
            pytest.raises(NotebookLMError, match="baseline snapshot was unavailable"),
        ):
            await client.sources.add_file(notebook_id, test_file)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # Pre-existing source's id was NOT silently returned — instead the
    # baseline-unavailable ambiguity guard fired.
    assert register_count >= 1
    assert get_count >= 2  # baseline + at least one probe


# ---------------------------------------------------------------------------
# add_text — NON_IDEMPOTENT_NO_RETRY enforcement
# ---------------------------------------------------------------------------


async def test_add_text_no_probe_no_retry_under_5xx(
    auth_tokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_text under 5xx must surface the failure immediately.

    No probe (no reliable dedupe key) and no retry — the registry
    classifies (ADD_SOURCE, "text") as NON_IDEMPOTENT_NO_RETRY, which
    force-disables the inner transport retry loop. The caller sees the
    503 on the first attempt and exactly one ADD_SOURCE request fires.

    asyncio.sleep is patched to a no-op so a regression that re-enables
    retries doesn't pay backoff wall time before the test catches it. The
    assertion on the surfaced exception type tolerates either
    ``ServerError`` or ``SourceAddError`` because ``add_text`` historically
    wraps ``RPCError`` subclasses (including ``ServerError``) in
    ``SourceAddError`` — the load-bearing assertion is the request count.
    """
    notebook_id = "nb_test"
    title = "Some Note"
    content = "some content"
    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            return httpx.Response(503, text="service unavailable")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            # Should NEVER be called for add_text.
            get_count += 1
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, []),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    async def _no_sleep(_seconds: float) -> None:
        return None

    # Object-form patch against the locally-imported seam alias (ADR-0007
    # Form 2): mutate the ``asyncio`` module reference that
    # ``_runtime.helpers`` reads, instead of a string-target patch. This is a
    # *defensive* shim — under the correct NON_IDEMPOTENT_NO_RETRY behavior
    # ``add_text`` never retries, so ``asyncio.sleep`` is never reached; the
    # patch only bounds wall-time if a regression re-enables retries. Because
    # the green path never sleeps, the seam-binding itself is asserted
    # (``resolve_sleep`` is the production read path) rather than call count.
    monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", _no_sleep)
    assert _runtime_helpers.resolve_sleep(None) is _no_sleep, (
        "object-form patch must target the seam production reads via "
        "resolve_sleep(None); a wrong-namespace alias would silently no-op"
    )

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(NotebookLMError):
            await client.sources.add_text(notebook_id, title, content)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # Exactly ONE ADD_SOURCE attempt: no retry loop, no probe.
    assert add_count == 1, (
        f"add_text must fire exactly 1 ADD_SOURCE under NON_IDEMPOTENT_NO_RETRY; got {add_count}"
    )
    assert get_count == 0, f"add_text must not probe; got {get_count} GET_NOTEBOOK"


# ---------------------------------------------------------------------------
# Probe-failure propagation (P1-2)
# ---------------------------------------------------------------------------


async def test_add_url_probe_network_error_propagates(auth_tokens) -> None:
    """When the probe itself fails with NetworkError, propagate the failure.

    Previously the probe wrapper caught any Exception and returned ``None``,
    which made ``idempotent_create`` re-issue the create on top of a broken
    probe — duplicating the resource on the very next attempt. The fix
    surfaces transport-layer probe failures directly so the caller can act
    on them (refresh auth, back off, etc.) instead of silently retrying.

    Triggered by:
      - first ADD_SOURCE returns 502 → enters probe branch
      - GET_NOTEBOOK raises a transport-level failure (httpx.ConnectError),
        which ``rpc_call`` translates into NetworkError
      - probe propagates NetworkError → idempotent_create surfaces it
        to the caller
    """
    notebook_id = "nb_test"
    url = "https://example.com/article"

    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_count += 1
            # Synthesize a transport-layer connect failure for the probe.
            raise httpx.ConnectError("probe synthetic connection error")
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(NetworkError, match="probe synthetic connection error"):
            await client.sources.add_url(notebook_id, url)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # Probe was attempted (the original create failed with 502, then the
    # probe was issued and raised).
    assert add_count == 1, f"expected 1 ADD_SOURCE before probe, got {add_count}"
    assert get_count >= 1, f"probe must run; got {get_count}"


# ---------------------------------------------------------------------------
# Registry sanity check: variant-keyed entries are present and classified
# ---------------------------------------------------------------------------


def test_registry_has_variant_entries_for_add_source_and_add_source_file() -> None:
    """Smoke-test that the four required registry entries exist and have the
    right policy classification.

    This guards against accidental regressions where a refactor drops the
    registry registration in ``_idempotency.py`` but leaves the per-variant
    plumbing intact — the executor would silently fall back to UNCLASSIFIED
    (today's retries) and the duplicate-source bug would resurrect.
    """
    url_entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="url")
    drive_entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="drive")
    text_entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="text")
    file_entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE_FILE, operation_variant=None)

    assert url_entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
    assert drive_entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
    assert text_entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert file_entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
