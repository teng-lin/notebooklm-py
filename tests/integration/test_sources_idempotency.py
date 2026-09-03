"""Variant-keyed idempotency tests for ADD_SOURCE + ADD_SOURCE_FILE.

Tier 9 Wave 2 (P0-3-sources, P1-2-sources): the previous behavior in
``_web/sources/add.py``/``_web/sources/upload.py`` relied on the inner transport
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

     - ``add_url`` probes by ``source.url == url``, filtered against a
       baseline of source ids captured before the create. A URL is not unique
       within a notebook, so an unfiltered match could return a pre-existing
       source and report a create that never landed (#2204).
     - ``add_drive`` probes by ``source.drive_document_id == file_id`` — the
       Drive ``documentId`` the backend echoes back in the source metadata.
       It previously probed a ``/d/<file_id>`` marker inside ``source.url``,
       which could never match because Drive sources carry no URL at all, so
       every retry duplicated the source (#2113).
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
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import notebooklm._runtime.helpers as _runtime_helpers
from notebooklm import NotebookLMClient
from notebooklm._web.policy import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.exceptions import (
    NetworkError,
    NotebookLMError,
    ServerError,
    SourceAddError,
    ValidationError,
)
from notebooklm.rpc import RPCError, RPCMethod
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
    src_rows: list = [_url_source_row(src_id, title, url) for src_id, title, url in sources]
    nb_info = ["Test Notebook", src_rows]
    return _wrb_response(RPCMethod.GET_NOTEBOOK.value, [nb_info])


def _url_source_row(src_id: str, title: str, url: str | None) -> list:
    """One non-Drive source row: URL (when any) at ``metadata[7][0]``.

    Mirrors the live-captured web-page shape
    ``[[id], title, [null, 28940, [ts, ns], [uuid, [ts, ns]], 5, null, 1, [url]], [null, 2]]``
    from ``tests/cassettes/web/sources_check_freshness_drive.yaml``, trimmed to the
    slots the probe paths read.
    """
    metadata: list = [None] * 8
    if url is not None:
        metadata[4] = 5  # SourceType.WEB_PAGE
        metadata[7] = [url]
    return [[src_id], title, metadata, [None, 2]]  # status block: READY


def _google_docs_source_row(src_id: str, title: str, document_id: str) -> list:
    """One Drive row whose id lands in ``metadata[0]`` (googleDocsMetadata).

    Shape copied verbatim (ids aside) from the live GET_NOTEBOOK capture in
    ``tests/cassettes/web/sources_check_freshness_drive.yaml`` and the live
    ADD_SOURCE capture in ``tests/cassettes/web/sources_add_drive.yaml``: the Drive
    metadata block sits at ``metadata[0]`` and **no** URL slot is populated —
    which is exactly why the old URL-based probe could never match (#2113).
    """
    metadata: list = [
        [document_id, "SCRUBBED_AONS", 12],
        911,
        [1769105469, 316769000],
        ["d4325602-2399-44c2-b45b-9df8f433189f", [1769105982, 178269000]],
        1,  # SourceType.GOOGLE_DOCS
        None,
        1,
    ]
    return [[src_id], title, metadata, [None, 2]]


def _drive_descriptor_source_row(src_id: str, title: str, document_id: str) -> list:
    """One Drive row whose id lands in ``metadata[9]`` (googleDriveSourceMetadata).

    The Drive-hosted-binary shape: the descriptor
    ``[document_id, kind_int, mime, ""]`` at ``metadata[9]`` plus the top-level
    MIME at ``metadata[19]``, as captured for #1832 (a Drive PDF arrives with
    the ambiguous ``type_code == 14``). No URL slot here either.
    """
    metadata: list = [None] * 20
    metadata[2] = [1769105469, 316769000]
    metadata[4] = 14  # ambiguous native-Sheet / Drive-binary code
    metadata[9] = [document_id, 8, "application/pdf", ""]
    metadata[19] = "application/pdf"
    return [[src_id], title, metadata, [None, 2]]


def _get_notebook_response(src_rows: list) -> str:
    """Build a GET_NOTEBOOK response from pre-built source rows."""
    return _wrb_response(RPCMethod.GET_NOTEBOOK.value, [["Test Notebook", src_rows]])


async def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Construct a NotebookLMClient backed by a mock transport.

    Open the real lifecycle first so the supervisor, transports, and epoch-one
    admission generation are active.  Only then swap the generation-owned HTTP
    client for the deterministic mock transport used by these integration tests.
    """
    client = NotebookLMClient(
        auth_tokens,
        server_error_max_retries=server_error_max_retries,
    )
    await client.__aenter__()
    kernel = client._web_runtime.kernel
    await kernel.get_http_client(expected_epoch=1).aclose()
    install_http_client_for_test(
        kernel,
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

    A neighbouring source that was already in the notebook proves the probe
    filters on the baseline rather than merely picking the first row.
    """
    notebook_id = "nb_test"
    url = "https://example.com/article"
    src_id = "src_lost_response"
    pre_existing = ("src_pre_existing", "Older Copy", url)

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
            # Any list before the first create attempt is the baseline
            # snapshot; anything after it is a probe.
            rows = [pre_existing]
            if add_count > 0:
                rows = [pre_existing, (src_id, "Article", url)]
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, rows),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, url)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert source.id == src_id, "the probe adopted the pre-existing same-URL source"
    assert source.url == url
    # Exactly ONE ADD_SOURCE (no naive re-POST after the 503)
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    # TWO GET_NOTEBOOKs: the pre-create baseline plus the probe.
    assert get_count == 2, f"expected baseline + probe GET_NOTEBOOK, got {get_count}"


#: The URL these ``add_url`` probe tests add. Each test builds its own rows with
#: :func:`_url_source_row`, so the pre-existing same-URL source is per-test, not
#: shared. Live-verified on #2204: two ``add_url`` calls with
#: ``https://example.com/`` produced two distinct source ids
#: (``9bed3c8a-…`` and ``0d2c15a1-…``), so a URL is NOT unique per notebook.
_PROBE_URL = "https://example.com/article"


def _url_probe_handler(
    *,
    baseline_rows: list,
    post_create_rows: list,
    counts: dict[str, int],
    baseline_status: int = 200,
):
    """Build a mock handler modelling the real ``add_url`` call sequence (#2204).

    ``add_url`` lists once *before* the create to snapshot a baseline, then the
    create 502s, then the probe lists again. Returning different notebook
    contents for the two GET_NOTEBOOK calls is what lets a test distinguish
    "the create landed" from "a same-URL source was already there".

    Mirrors :func:`_drive_probe_handler`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            # Keyed off the create rather than a call index so an internally
            # retried baseline still counts as one.
            if counts["add"] == 0:
                if baseline_status != 200:
                    return httpx.Response(baseline_status, text="baseline unavailable")
                return httpx.Response(200, text=_get_notebook_response(baseline_rows))
            return httpx.Response(200, text=_get_notebook_response(post_create_rows))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    return handler


async def test_add_url_probe_ignores_a_pre_existing_copy_of_the_same_url(
    auth_tokens,
) -> None:
    """A URL already in the notebook must not be adopted as "my create" (#2204).

    A URL is NOT unique within a notebook — the backend lets the same URL be
    added twice and ``SourceLister.list`` dedupes by source id, not by URL.
    Without the baseline filter the probe returns the pre-existing copy and
    reports success even though the create never landed, so the caller walks
    away holding a source id that belongs to an earlier add.
    """
    notebook_id = "nb_test"
    # Present before the add, and still the only match afterwards: the create
    # genuinely did not land.
    pre_existing = _url_source_row("src_pre_existing", "Older Copy", _PROBE_URL)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _url_probe_handler(
            baseline_rows=[pre_existing],
            post_create_rows=[pre_existing],
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_url(notebook_id, _PROBE_URL)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # The probe found no *new* match, so idempotent_create retried, exhausted
    # its two attempts, and re-raised the transport error rather than handing
    # back a source the caller did not create.
    assert counts["add"] == 2


async def test_add_url_probe_raises_when_baseline_unavailable_and_a_copy_exists(
    auth_tokens,
) -> None:
    """No baseline + a matching URL = ambiguity, surfaced rather than guessed.

    Mirrors ``add_drive`` / ``register_file_source``: when the pre-create
    snapshot could not be taken, a match may or may not predate the add, and
    silently picking one is the failure mode the baseline exists to prevent.
    """
    notebook_id = "nb_test"
    existing = _url_source_row("src_ambiguous", "Some Copy", _PROBE_URL)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _url_probe_handler(
            baseline_rows=[],
            post_create_rows=[existing],
            counts=counts,
            baseline_status=500,  # baseline snapshot unavailable
        )
    )
    # No executor-level retries: the baseline 500 should fail fast here, the
    # point of the test being what the *probe* does afterwards.
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(SourceAddError, match="pre-create baseline snapshot failed"):
            await client.sources.add_url(notebook_id, _PROBE_URL)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # The load-bearing half: an ambiguous probe must ABORT the retry loop.
    # Classification alone would still pass if a second create had already gone
    # out — which is the duplicate this whole path exists to prevent.
    assert counts["add"] == 1, f"expected 1 ADD_SOURCE, got {counts['add']}"


async def test_add_url_baseline_unavailable_without_a_match_still_retries(auth_tokens) -> None:
    """No baseline and no match is not ambiguous — it is simply "not committed".

    The ambiguity raise must fire only when there is something to be ambiguous
    about; otherwise a notebook that legitimately has no copy of the URL would
    turn a recoverable transport blip into a hard error.
    """
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    transport = httpx.MockTransport(
        _url_probe_handler(
            baseline_rows=[],
            post_create_rows=[_url_source_row("src_other", "Another Page", "https://other.test/")],
            counts=counts,
            baseline_status=500,  # baseline snapshot unavailable
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_url(notebook_id, _PROBE_URL)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # ServerError (the real failure), not SourceAddError — and the retry ran.
    assert counts["add"] == 2


async def test_add_url_probe_raises_when_multiple_new_matches_appear(auth_tokens) -> None:
    """Two *new* sources with the requested URL is ambiguity, not a match."""
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    transport = httpx.MockTransport(
        _url_probe_handler(
            baseline_rows=[],
            post_create_rows=[
                _url_source_row("src_new_a", "Article", _PROBE_URL),
                _url_source_row("src_new_b", "Article", _PROBE_URL),
            ],
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(SourceAddError, match="probe found 2 new sources"):
            await client.sources.add_url(notebook_id, _PROBE_URL)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()


@pytest.mark.parametrize(
    "rows_factory",
    [
        # A different URL entirely.
        pytest.param(
            lambda url: [_url_source_row("src_other", "Another Page", "https://other.test/")],
            id="different_url",
        ),
        # A prefix of the requested URL: the probe is exact equality, not a
        # substring/startswith test, so this must not match.
        pytest.param(
            lambda url: [_url_source_row("src_prefix", "Prefix", url[: len(url) - 4])],
            id="prefix_of_requested_url",
        ),
        # The requested URL with an extra path segment appended.
        pytest.param(
            lambda url: [_url_source_row("src_suffix", "Suffix", url + "/comments")],
            id="requested_url_is_a_prefix",
        ),
        # A row carrying no URL at all (e.g. a Drive or file source) decodes
        # ``url`` as ``None`` and can never match.
        pytest.param(
            lambda url: [_url_source_row("src_no_url", "A File", None)],
            id="row_without_a_url",
        ),
    ],
)
async def test_add_url_probe_does_not_match_unrelated_sources(auth_tokens, rows_factory) -> None:
    """The probe matches the requested URL exactly — nothing else.

    In each case the probe must return ``None`` so ``idempotent_create``
    retries the create rather than handing back the wrong source — so both
    attempts fire and the transport error is re-raised.
    """
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    transport = httpx.MockTransport(
        _url_probe_handler(
            # Baseline is EMPTY on purpose: these rows must be rejected by the
            # *match predicate*, not merely filtered out for pre-dating the add.
            baseline_rows=[],
            post_create_rows=rows_factory(_PROBE_URL),
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_url(notebook_id, _PROBE_URL)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # Both create attempts fired: the probe never spuriously matched.
    assert counts["add"] == 2


async def test_add_url_probe_matches_on_the_second_attempt(auth_tokens) -> None:
    """The baseline captured before attempt 1 is still correct at probe 2.

    Sequence: create fails, probe finds nothing, create fails again, and only
    then does the committed source surface (source lists lag). The single
    pre-create baseline must still identify it as new — and must still exclude
    the same-URL source that was there all along.
    """
    notebook_id = "nb_test"
    src_id = "src_late"
    pre_existing = _url_source_row("src_pre_existing", "Older Copy", _PROBE_URL)
    committed = _url_source_row(src_id, "Article", _PROBE_URL)
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            # baseline (get 1) and probe 1 (get 2) see only the older copy;
            # probe 2 finally sees the committed source.
            rows = [pre_existing, committed] if counts["get"] >= 3 else [pre_existing]
            return httpx.Response(200, text=_get_notebook_response(rows))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, _PROBE_URL)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert counts["add"] == 2, "both attempts should have fired before the probe matched"


async def test_add_url_probe_decode_failure_aborts_instead_of_retrying(auth_tokens, caplog) -> None:
    """A probe that cannot answer aborts the add instead of retrying (#2220).

    A drifted GET_NOTEBOOK makes the strict decoder raise ``RPCError``. That is
    not a transport signal, so it is not re-raised as one — but it does mean the
    probe cannot say whether the create landed, and "no match" is a claim it
    cannot support. Since this variant runs with no internal retries, re-issuing
    ``ADD_SOURCE`` here is how the duplicate the probe exists to prevent gets
    created. Exercised through the real client stack so the strict decoder, not
    a stubbed exception, produces the failure.
    """
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            if counts["add"] == 0:
                return httpx.Response(200, text=_get_notebook_response([]))
            # Structurally undecodable notebook envelope -> RPCError, not 5xx.
            return httpx.Response(200, text=_wrb_response(RPCMethod.GET_NOTEBOOK.value, "nonsense"))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    with caplog.at_level(logging.WARNING, logger="notebooklm._sources"):
        try:
            with pytest.raises(SourceAddError, match="Cannot confirm URL source") as exc_info:
                await client.sources.add_url(notebook_id, _PROBE_URL)
        finally:
            await client._web_runtime.kernel.get_http_client().aclose()

    # The load-bearing assertion: the create fired ONCE. Restore the probe's
    # ``return None`` and this becomes 2 — the duplicate this PR prevents.
    assert counts["add"] == 1
    assert "add_url: probe list() failed" in caplog.text
    assert "will not be retried" in caplog.text
    # Both halves of the story survive to the caller: the decode failure that
    # blinded the probe, and the 502 that made it run.
    assert isinstance(exc_info.value.cause, RPCError)
    assert isinstance(exc_info.value.__context__, RPCError)
    assert isinstance(exc_info.value.__context__.__context__, ServerError)


async def test_add_url_recovered_create_still_honors_the_requested_title(auth_tokens) -> None:
    """A probed-but-fresh URL add must still get the caller's title (#2204).

    Web-page and YouTube imports re-derive the display title server-side, so
    ``add_url`` issues a best-effort rename afterwards. That rename used to be
    skipped for a ``PROBED`` result, because a probe match could predate the
    call — but the probe now filters against a pre-create baseline, so its match
    is provably ours.
    """
    notebook_id = "nb_test"
    requested_title = "The Title I Asked For"
    upstream_title = "Whatever The Page Called Itself"
    src_id = "src_recovered"

    committed = _url_source_row(src_id, upstream_title, _PROBE_URL)
    counts = {"add": 0, "get": 0}
    renames: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            rows = [] if counts["add"] == 0 else [committed]
            return httpx.Response(200, text=_get_notebook_response(rows))
        if rpc_id == RPCMethod.UPDATE_SOURCE.value:
            renames.append((src_id, requested_title))
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.UPDATE_SOURCE.value, [[src_id], requested_title]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, _PROBE_URL, title=requested_title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert renames == [(src_id, requested_title)], "the recovery path skipped the rename"
    assert source.title == requested_title
    assert counts["add"] == 1


async def test_add_url_bulk_cost_is_one_baseline_read_per_call(auth_tokens) -> None:
    """Pin the request cost the docs claim for sequential bulk adds (#2204).

    ``add_url``'s docstring and the CHANGELOG tell callers a sequential bulk add
    goes from N+1 to 2N+1 requests, because the baseline is per-call and no
    preflight covers it. That is a numeric claim about backend load on the
    highest-traffic add path, so it is asserted rather than asserted-in-prose:
    three URLs must cost exactly three ``ADD_SOURCE``s and three
    ``GET_NOTEBOOK``s — one baseline each, no probes (every create succeeds).

    It also catches the regression a reader would most fear: a second baseline
    read sneaking into the path, which would double the cost again.
    """
    notebook_id = "nb_test"
    urls = [f"https://example.com/article-{index}" for index in range(3)]
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(
                200,
                text=_wrb_response(
                    RPCMethod.ADD_SOURCE.value,
                    [[_url_source_row(f"src_{counts['add']}", "Article", urls[counts["add"] - 1])]],
                ),
            )
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            return httpx.Response(200, text=_get_notebook_response([]))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        for url in urls:
            await client.sources.add_url(notebook_id, url)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert counts == {"add": 3, "get": 3}, (
        "a sequential bulk add should cost exactly one baseline GET_NOTEBOOK per "
        f"add_url and no probes; got {counts}"
    )


# ---------------------------------------------------------------------------
# add_drive — commit-lost-response (PROBE_THEN_CREATE, variant="drive")
# ---------------------------------------------------------------------------


_DRIVE_ROW_BUILDERS = {
    # metadata[0] — googleDocsMetadata.documentId. Live-observed on type codes
    # 1 (Docs) and 2 (Slides).
    "google_docs_metadata": _google_docs_source_row,
    # metadata[9] — googleDriveSourceMetadata.documentId. Live-observed on type
    # code 14 (Drive-hosted files, incl. native Sheets and Drive PDFs).
    "drive_descriptor": _drive_descriptor_source_row,
}

#: A Drive file id as it appears on the wire (44-char Base64URL), from the live
#: ADD_SOURCE capture in ``tests/cassettes/web/sources_add_drive.yaml``.
_DRIVE_FILE_ID = "1oAk_INJHbIPsIh49jgNqj3FESSGHZrzxFY7t05Lvvl0"


def _drive_probe_handler(
    *,
    baseline_rows: list,
    post_create_rows: list,
    counts: dict[str, int],
    baseline_status: int = 200,
):
    """Build a mock handler modelling the real add_drive call sequence.

    ``add_drive`` lists once *before* the create to snapshot a baseline, then
    the create 502s, then the probe lists again. Returning different notebook
    contents for the two GET_NOTEBOOK calls is what lets a test distinguish "the
    create landed" from "a copy was already there".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            # Any list before the first create attempt is the baseline snapshot;
            # anything after it is a probe. Keyed off the create rather than a
            # call index so an internally-retried baseline still counts as one.
            if counts["add"] == 0:
                if baseline_status != 200:
                    return httpx.Response(baseline_status, text="baseline unavailable")
                return httpx.Response(200, text=_get_notebook_response(baseline_rows))
            return httpx.Response(200, text=_get_notebook_response(post_create_rows))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    return handler


@pytest.mark.parametrize("slot", sorted(_DRIVE_ROW_BUILDERS), ids=sorted(_DRIVE_ROW_BUILDERS))
async def test_add_drive_probe_short_circuits_when_first_response_lost(
    auth_tokens, slot: str
) -> None:
    """Commit-lost-response on a Drive add must NOT duplicate the source (#2113).

    The server committed the create and then the response was lost (5xx). With
    ``disable_internal_retries=True`` on this variant, the probe is the only
    thing standing between the user and a second copy of the same Drive file.

    Both live-captured Drive row shapes are exercised: the ``documentId`` lands
    in ``metadata[0]`` for a Google-native Doc/Slides and in ``metadata[9]`` for
    a Drive-hosted file. Neither row carries a URL — the previous
    ``/d/<file_id>``-in-``source.url`` probe therefore matched nothing and let
    ``idempotent_create`` re-issue the add every single time.
    """
    notebook_id = "nb_test"
    title = "My Drive Doc"
    src_id = "src_drive_lost"

    # A neighbouring non-Drive source proves the probe skips rows whose
    # ``drive_document_id`` is ``None``.
    web_row = _url_source_row("src_web", "A Web Page", "https://example.com/article")
    committed = _DRIVE_ROW_BUILDERS[slot](src_id, title, _DRIVE_FILE_ID)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _drive_probe_handler(
            baseline_rows=[web_row],
            post_create_rows=[web_row, committed],
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert source.id == src_id
    # The committed source is recognised by its Drive documentId, not a URL.
    assert source.drive_document_id == _DRIVE_FILE_ID
    assert source.url is None
    # Exactly ONE ADD_SOURCE — the retry that would have duplicated the file
    # never fires — and exactly TWO lists (baseline + probe).
    assert counts == {"add": 1, "get": 2}


async def test_add_drive_probe_ignores_a_pre_existing_copy_of_the_same_file(
    auth_tokens,
) -> None:
    """A Drive file already in the notebook must not be adopted as "my create".

    ``documentId`` is NOT unique within a notebook — the backend lets the same
    Drive file be added twice, and the live capture in
    ``tests/cassettes/web/sources_check_freshness_drive.yaml`` contains exactly that
    (two source ids sharing one documentId). Without the baseline filter the
    probe would return the pre-existing copy and report success even though the
    create never landed, hiding the failure instead of surfacing it.
    """
    notebook_id = "nb_test"
    title = "My Drive Doc"
    # Present before the add, and still the only match afterwards: the create
    # genuinely did not land.
    pre_existing = _google_docs_source_row("src_pre_existing", "Older Copy", _DRIVE_FILE_ID)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _drive_probe_handler(
            baseline_rows=[pre_existing],
            post_create_rows=[pre_existing],
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # The probe found no *new* match, so idempotent_create retried, exhausted
    # its two attempts, and re-raised the transport error rather than handing
    # back a source the caller did not create.
    assert counts["add"] == 2


async def test_add_drive_probe_raises_when_baseline_unavailable_and_a_copy_exists(
    auth_tokens,
) -> None:
    """No baseline + a matching source = ambiguity, surfaced rather than guessed.

    Mirrors ``register_file_source``'s contract: when the pre-create snapshot
    could not be taken, a match may or may not predate the add, and silently
    picking one is the data-corruption mode the baseline exists to prevent.
    """
    notebook_id = "nb_test"
    title = "My Drive Doc"
    existing = _google_docs_source_row("src_ambiguous", "Some Copy", _DRIVE_FILE_ID)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _drive_probe_handler(
            baseline_rows=[],
            post_create_rows=[existing],
            counts=counts,
            baseline_status=500,  # baseline snapshot unavailable
        )
    )
    # No executor-level retries: the baseline 500 should fail fast here, the
    # point of the test being what the *probe* does afterwards.
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(SourceAddError, match="pre-create baseline snapshot failed") as exc_info:
            await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # The load-bearing half: an ambiguous probe must ABORT the retry loop.
    # Classification alone would still pass if a second create had already gone
    # out — which is the duplicate this whole path exists to prevent.
    assert counts["add"] == 1, f"expected 1 ADD_SOURCE, got {counts['add']}"

    # The baseline's own failure is retained as the cause, matching add_url: the
    # caller reads "baseline snapshot failed" long after that read happened, and
    # nothing else in the process can explain it.
    assert isinstance(exc_info.value.cause, ServerError)


async def test_add_drive_probe_raises_when_multiple_new_matches_appear(auth_tokens) -> None:
    """Two *new* sources with the requested documentId is ambiguity, not a match."""
    notebook_id = "nb_test"
    title = "My Drive Doc"
    first = _google_docs_source_row("src_new_a", title, _DRIVE_FILE_ID)
    second = _google_docs_source_row("src_new_b", title, _DRIVE_FILE_ID)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _drive_probe_handler(
            baseline_rows=[],
            post_create_rows=[first, second],
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(SourceAddError, match="probe found 2 new sources"):
            await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()


async def test_add_drive_rejects_a_blank_file_id_before_writing(auth_tokens) -> None:
    """A blank Drive id fails validation instead of POSTing an unmatchable add.

    A blank id can never be matched by the probe (a row's ``drive_document_id``
    is never ``""``), so without this guard a transport failure would retry the
    blank add and could leave two garbage sources behind.
    """
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["add"] += 1
        return httpx.Response(200, text="should never be reached")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ValidationError):
            await client.sources.add_drive("nb_test", "   ", "My Drive Doc")
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert counts["add"] == 0, "no RPC may be issued for a blank file_id"


@pytest.mark.parametrize(
    "existing_rows_factory",
    [
        # A different Drive file in the same notebook must not be adopted.
        pytest.param(
            lambda file_id: [_google_docs_source_row("src_other", "Other Doc", file_id + "_other")],
            id="different_drive_file",
        ),
        # A prefix collision: the stored id merely starts with the requested one.
        pytest.param(
            lambda file_id: [
                _drive_descriptor_source_row("src_other", "Other PDF", file_id + "SUFFIX")
            ],
            id="prefix_collision",
        ),
        # Non-Drive rows decode ``drive_document_id`` as ``None`` and must never
        # match — including a web source whose URL happens to embed the very
        # ``/d/<file_id>/`` slug the old probe keyed on.
        pytest.param(
            lambda file_id: [
                _url_source_row(
                    "src_web",
                    "A Web Page",
                    f"https://docs.google.com/document/d/{file_id}/edit",
                ),
                _url_source_row("src_web2", "Another Page", None),
            ],
            id="non_drive_rows",
        ),
    ],
)
async def test_add_drive_probe_does_not_match_unrelated_sources(
    auth_tokens, existing_rows_factory
) -> None:
    """The probe matches the Drive ``documentId`` exactly — nothing else.

    Exact equality on an identity-bearing field removes the whole
    substring/prefix false-positive class the URL probe had to defend against,
    and ``None`` on every non-Drive row means an unrelated source can never be
    returned in place of the one being created.

    In each case the probe must return ``None`` so ``idempotent_create``
    retries the create rather than handing back the wrong source — so both
    attempts fire and the transport error is re-raised.
    """
    notebook_id = "nb_test"
    title = "My Drive Doc"
    rows = existing_rows_factory(_DRIVE_FILE_ID)

    counts = {"add": 0, "get": 0}
    transport = httpx.MockTransport(
        _drive_probe_handler(
            # Baseline is EMPTY on purpose: these rows must be rejected by the
            # *match predicate*, not merely filtered out for pre-dating the add.
            # With them in the baseline this test passes even against a
            # substring match or the old `/d/<file_id>/` URL probe — it would
            # only be re-testing the pre-existing-copy case.
            baseline_rows=[],
            post_create_rows=rows,
            counts=counts,
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # Both create attempts fired: the probe never spuriously matched.
    assert counts["add"] == 2


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
    client = await _make_client_with_transport(transport, auth_tokens)
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
        await client._web_runtime.kernel.get_http_client().aclose()

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
    client = await _make_client_with_transport(transport, auth_tokens)
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
        await client._web_runtime.kernel.get_http_client().aclose()

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
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
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
            pytest.raises(NotebookLMError, match="pre-create baseline snapshot failed"),
        ):
            await client.sources.add_file(notebook_id, test_file)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

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
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(NotebookLMError):
            await client.sources.add_text(notebook_id, title, content)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

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
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(NetworkError, match="probe synthetic connection error"):
            await client.sources.add_url(notebook_id, url)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

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
    registry registration in ``_web/policy.py`` but leaves the per-variant
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


async def test_add_drive_recovered_create_still_honors_the_requested_title(auth_tokens) -> None:
    """A probed-but-fresh Drive add must still get the caller's title (#2113).

    Drive imports re-derive the display title server-side, so ``add_drive``
    issues a best-effort rename afterwards. That rename is normally skipped for
    a ``PROBED`` result, because a probe match may predate the call — but this
    probe filters against a pre-create baseline, so its match is provably ours.
    Without the freshness signal the caller silently gets the Drive name instead
    of the title they asked for whenever a create commits but loses its response.
    """
    notebook_id = "nb_test"
    requested_title = "The Title I Asked For"
    drive_name = "Whatever Drive Called It"
    src_id = "src_recovered"

    committed = _google_docs_source_row(src_id, drive_name, _DRIVE_FILE_ID)
    counts = {"add": 0, "get": 0}
    renames: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            rows = [] if counts["add"] == 0 else [committed]
            return httpx.Response(200, text=_get_notebook_response(rows))
        if rpc_id == RPCMethod.UPDATE_SOURCE.value:
            renames.append((src_id, requested_title))
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.UPDATE_SOURCE.value, [[src_id], requested_title]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, requested_title)
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert renames == [(src_id, requested_title)], "the recovery path skipped the rename"
    assert source.title == requested_title
    assert counts["add"] == 1


async def test_add_drive_probe_transport_error_propagates(auth_tokens) -> None:
    """A probe that itself 5xxes must propagate, not read as "no match".

    Mirrors ``test_add_url_probe_network_error_propagates``. Swallowing it would
    make a broken probe indistinguishable from "the create did not land", so
    ``idempotent_create`` would retry on top of it — recreating the very
    duplicate this probe exists to prevent.
    """
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            # Baseline succeeds; every later list (the probe) is a hard 503.
            if counts["add"] == 0:
                return httpx.Response(200, text=_get_notebook_response([]))
            return httpx.Response(503, text="probe unavailable")
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, "My Drive Doc")
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # The probe raised on the first attempt, so no second create was issued.
    assert counts["add"] == 1


async def test_add_drive_probe_decode_failure_aborts_instead_of_retrying(
    auth_tokens, caplog
) -> None:
    """A probe that cannot answer aborts the add instead of retrying (#2220).

    The Drive twin of ``test_add_url_probe_decode_failure_aborts_instead_of_retrying``;
    both paths are one pattern and #2220's whole argument is that they move
    together, so the Drive path is pinned separately rather than assumed.
    """
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            if counts["add"] == 0:
                return httpx.Response(200, text=_get_notebook_response([]))
            # Structurally undecodable notebook envelope -> RPCError, not 5xx.
            return httpx.Response(200, text=_wrb_response(RPCMethod.GET_NOTEBOOK.value, "nonsense"))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    with caplog.at_level(logging.WARNING, logger="notebooklm._sources"):
        try:
            with pytest.raises(SourceAddError, match="Cannot confirm Drive source") as exc_info:
                await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, "My Drive Doc")
        finally:
            await client._web_runtime.kernel.get_http_client().aclose()

    # One create, not two — see the add_url twin.
    assert counts["add"] == 1
    # Prefixed, so an add_url log could not satisfy this assertion.
    assert "add_drive: probe list() failed" in caplog.text
    assert "will not be retried" in caplog.text
    assert isinstance(exc_info.value.cause, RPCError)
    assert isinstance(exc_info.value.__context__.__context__, ServerError)


async def test_add_drive_probe_matches_on_the_second_attempt(auth_tokens) -> None:
    """The baseline captured before attempt 1 is still correct at probe 2.

    Sequence: create fails, probe finds nothing, create fails again, and only
    then does the committed source surface (source lists lag). The single
    pre-create baseline must still identify it as new.
    """
    notebook_id = "nb_test"
    src_id = "src_late"
    committed = _google_docs_source_row(src_id, "My Drive Doc", _DRIVE_FILE_ID)
    counts = {"add": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            counts["add"] += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            counts["get"] += 1
            # baseline (get 1) and probe 1 (get 2) see nothing; probe 2 sees it.
            rows = [committed] if counts["get"] >= 3 else []
            return httpx.Response(200, text=_get_notebook_response(rows))
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, "My Drive Doc")
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    assert source.id == src_id
    assert counts["add"] == 2, "both attempts should have fired before the probe matched"


async def test_add_drive_baseline_unavailable_without_a_match_still_retries(auth_tokens) -> None:
    """No baseline and no match is not ambiguous — it is simply "not committed".

    The ambiguity raise must fire only when there is something to be ambiguous
    about; otherwise a notebook that legitimately has no copy of the file would
    turn a recoverable transport blip into a hard error.
    """
    notebook_id = "nb_test"
    counts = {"add": 0, "get": 0}

    transport = httpx.MockTransport(
        _drive_probe_handler(
            baseline_rows=[],
            post_create_rows=[_url_source_row("src_web", "A Web Page", "https://example.com")],
            counts=counts,
            baseline_status=500,  # baseline snapshot unavailable
        )
    )
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=0)
    try:
        with pytest.raises(ServerError):
            await client.sources.add_drive(notebook_id, _DRIVE_FILE_ID, "My Drive Doc")
    finally:
        await client._web_runtime.kernel.get_http_client().aclose()

    # ServerError (the real failure), not SourceAddError — and the retry ran.
    assert counts["add"] == 2
