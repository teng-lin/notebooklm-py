"""Regression tests for the probe-then-retry idempotency for create RPCs.

Audit item #2 (`thread-safety-concurrency-audit.md` §2):
Pre-fix, mutating create RPCs (CREATE_NOTEBOOK, ADD_SOURCE) ran inside
the shared transport retry loop, so a 5xx / network blip
between server-side commit and client-side response triggered a naive
re-POST that duplicated the resource.

Post-fix:
- Per-call ``disable_internal_retries`` flag suppresses the inner retry
  loop for declared mutating create RPCs.
- An API-layer ``_idempotency.idempotent_create`` wrapper owns
  probe-then-retry with API-specific probes:
    - notebooks.create → baseline-diff by title
    - sources.add_url → list-then-url-match, baseline-diff (#2204)
    - sources._add_youtube_source → same wrapper as sources.add_url
- ``sources.add_text`` is decision-not-to-fix: the new ``idempotent=True``
  keyword raises ``NonIdempotentRetryError`` rather than silently
  duplicating (no reliable server-side dedupe key for text sources).

Test plan:
1. notebooks.create — idempotent on 5xx retry (probe finds existing)
2. notebooks.create — re-creates when probe finds nothing
3. notebooks.create — raises on ambiguous probe
4. sources.add_url — idempotent on 5xx retry
5. sources.add_url (YouTube) — idempotent on 5xx retry
6. sources.add_text — raises NonIdempotentRetryError when idempotent=True
7. sources.add_text — default behavior unchanged
8. disable_internal_retries — propagates through to RuntimeTransport.perform_authed_post
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

import notebooklm._runtime.helpers as _runtime_helpers
from notebooklm import (
    NonIdempotentRetryError,
    NotebookLMClient,
    RPCError,
    ServerError,
)
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm.rpc import RPCMethod
from tests._helpers.client_factory import build_client_shell_for_tests

# mock-transport idempotency tests; no HTTP, no cassette. Opt out
# of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrb_response(rpc_id: str, payload) -> str:
    """Build a single-RPC batchexecute response body.

    Mirrors the on-the-wire format ``)]}}'\\n<len>\\n<chunk>\\n`` used
    everywhere else in the test suite.
    """
    inner = json.dumps(payload)
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _list_notebooks_response(notebooks: list[tuple[str, str]]) -> str:
    """Build a LIST_NOTEBOOKS response from ``[(notebook_id, title), ...]``.

    Schema mirrors the canonical fixture in ``tests/integration/conftest.py``:
    each entry is ``[title, sources_or_none, id, emoji, ?, [..., [ts, 0]]]``.
    """
    raw = [
        [title, None, nb_id, "📘", None, [None, None, None, None, None, [1704067200, 0]]]
        for nb_id, title in notebooks
    ]
    return _wrb_response(RPCMethod.LIST_NOTEBOOKS.value, [raw])


def _create_notebook_response(notebook_id: str, title: str) -> str:
    """Build a CREATE_NOTEBOOK success response.

    The response decodes into the args of ``Notebook.from_api_response``.
    Shape is the same single-row layout as a list entry.
    """
    return _wrb_response(
        RPCMethod.CREATE_NOTEBOOK.value,
        [
            title,
            None,
            notebook_id,
            "📘",
            None,
            [None, None, None, None, None, [1704067200, 0]],
        ],
    )


def _get_notebook_with_sources_response(
    notebook_id: str,
    sources: list[tuple[str, str, str]],
    *,
    type_code: int = 5,
) -> str:
    """Build a GET_NOTEBOOK response that ``SourcesAPI.list`` parses.

    ``sources`` is ``[(source_id, title, url), ...]``. The shape mirrors
    the parsing path in ``SourcesAPI.list`` (which reads from
    ``GET_NOTEBOOK``): each src entry is roughly
    ``[[id], title, metadata_with_url_at_[7], status]``.

    ``type_code`` is the source-type code at ``metadata[4]`` — 5 (web page) by
    default, 9 for YouTube (``SourceType``). It does not steer the probe, which
    reads only ``source.url``, but a YouTube test that hands the probe a
    web-page row is not exercising a YouTube row.
    """
    src_rows = []
    for src_id, title, url in sources:
        # metadata: url at index [7] (matches Source.from_api_response /
        # _extract_source_url precedence, allow_bare_http=False).
        metadata: list = [None] * 8
        metadata[4] = type_code
        metadata[7] = [url]
        # status block at src[3] — [_, READY=2]
        status_block = [None, 2]
        src_rows.append([[src_id], title, metadata, status_block])
    nb_info = ["Test Notebook", src_rows]
    return _wrb_response(RPCMethod.GET_NOTEBOOK.value, [nb_info])


async def _open_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Open a ``NotebookLMClient`` whose live HTTP client uses ``transport``.

    The real root lifecycle creates and activates the resource generation;
    only the HTTP client factory is injected to keep traffic synthetic.
    """

    def _client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    client = build_client_shell_for_tests(
        auth_tokens,
        server_error_max_retries=server_error_max_retries,
        async_client_factory=_client_factory,
    )
    await client.__aenter__()
    generation = client._collaborators.call_supervisor._current
    assert generation is not None
    epoch = client._collaborators.lifecycle._epoch
    assert generation.epoch == epoch
    assert client._collaborators.web_transport._active_epoch == epoch
    assert client._collaborators.kernel._active_epoch == epoch
    assert client._collaborators.auth_coord._active_epoch == epoch
    return client


def _rpc_id_in_request(request: httpx.Request) -> str | None:
    """Extract the ``rpcids=`` query param from a batchexecute request URL."""
    for key, value in request.url.params.multi_items():
        if key == "rpcids":
            return value
    return None


# ---------------------------------------------------------------------------
# Tests: notebooks.create idempotency
# ---------------------------------------------------------------------------


async def test_notebooks_create_idempotent_on_5xx_retry(auth_tokens) -> None:
    """A 5xx on CREATE_NOTEBOOK followed by a probe finding the title returns the existing notebook.

    Before the fix: the inner retry loop would re-POST CREATE_NOTEBOOK and
    duplicate the notebook. After the fix: the create RPC fires once, the
    probe (LIST_NOTEBOOKS) returns the new notebook, and we return it
    without re-issuing the create.
    """
    nb_id_existing = "nb_pre"
    nb_id_new = "nb_new"
    title = "My Notebook"

    create_rpc_count = 0
    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_rpc_count, list_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            if list_rpc_count == 1:
                # Baseline: only the pre-existing notebook
                return httpx.Response(
                    200, text=_list_notebooks_response([(nb_id_existing, "Other")])
                )
            # Probe after the 5xx: server actually committed the new notebook
            return httpx.Response(
                200,
                text=_list_notebooks_response([(nb_id_existing, "Other"), (nb_id_new, title)]),
            )
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_rpc_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        notebook = await client.notebooks.create(title)
    finally:
        await client.close()

    assert notebook.id == nb_id_new
    assert notebook.title == title
    # Exactly ONE CREATE_NOTEBOOK request was sent (no naive re-POST)
    assert create_rpc_count == 1, f"expected 1 CREATE_NOTEBOOK, got {create_rpc_count}"
    # Two LIST_NOTEBOOKS: baseline + post-failure probe
    assert list_rpc_count == 2, f"expected 2 LIST_NOTEBOOKS, got {list_rpc_count}"


async def test_notebooks_create_re_creates_when_probe_finds_nothing(auth_tokens) -> None:
    """If the probe finds no matching new notebook, the create is retried.

    Models the case where the 5xx genuinely indicates the request never
    landed server-side. ``idempotent_create`` must detect the empty
    probe and retry the create.
    """
    nb_id_new = "nb_after_retry"
    title = "Fresh Notebook"

    create_rpc_count = 0
    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_rpc_count, list_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            # Both baseline and probe return empty (no new notebook landed)
            return httpx.Response(200, text=_list_notebooks_response([]))
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_rpc_count += 1
            if create_rpc_count == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, text=_create_notebook_response(nb_id_new, title))
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        notebook = await client.notebooks.create(title)
    finally:
        await client.close()

    assert notebook.id == nb_id_new
    # Two CREATE_NOTEBOOK calls: original (502) + retry (200)
    assert create_rpc_count == 2, f"expected 2 CREATE_NOTEBOOK, got {create_rpc_count}"
    # Two LIST_NOTEBOOKS: baseline + probe after the 502
    assert list_rpc_count == 2, f"expected 2 LIST_NOTEBOOKS, got {list_rpc_count}"


async def test_notebooks_create_probe_decode_failure_aborts_instead_of_retrying(
    auth_tokens, caplog
) -> None:
    """A probe that cannot answer aborts the create instead of retrying (#2220).

    ``notebooks.create`` is the fourth instance of the probe-then-create shape
    (the three source paths are the others), and #2220's argument is that they
    move together. A drifted LIST_NOTEBOOKS makes the decoder raise, which is
    not a transport signal but does leave the probe unable to say whether the
    create landed — so re-issuing CREATE_NOTEBOOK would risk two notebooks with
    the same title and hand back neither id.
    """
    title = "Undecodable Probe"
    create_rpc_count = 0
    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_rpc_count, list_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            if list_rpc_count == 1:
                return httpx.Response(200, text=_list_notebooks_response([]))
            # Structurally undecodable list envelope -> decode error, not 5xx.
            return httpx.Response(
                200, text=_wrb_response(RPCMethod.LIST_NOTEBOOKS.value, "nonsense")
            )
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_rpc_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        try:
            with pytest.raises(RPCError, match="Cannot confirm notebook"):
                await client.notebooks.create(title)
        finally:
            await client.close()

    # The load-bearing assertion: ONE create, not two.
    assert create_rpc_count == 1, f"expected 1 CREATE_NOTEBOOK, got {create_rpc_count}"
    assert "will not be retried" in caplog.text


async def test_notebooks_create_baseline_failure_warns_but_proceeds(auth_tokens, caplog) -> None:
    """A failed *baseline* still degrades — loudly — rather than failing the create (#2220).

    The asymmetry with the probe above is the point of the change: at baseline
    time nothing has been written, so proceeding is safe and refusing would
    break creates that would otherwise succeed. The record has to actually
    reach a handler, though: the ``notebooklm`` logger defaults to WARNING, so
    the old DEBUG line was dropped before anything could see it and the call ran
    with a degraded probe in silence.

    Pins the resilience half of the #2232 sentinel: an unavailable baseline
    disarms the *probe*, it must never fail a create that otherwise succeeds.
    """
    title = "Baseline Blind"
    nb_id = "nb_created"
    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            # The baseline read is undecodable; the create then succeeds.
            return httpx.Response(
                200, text=_wrb_response(RPCMethod.LIST_NOTEBOOKS.value, "nonsense")
            )
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            return httpx.Response(200, text=_create_notebook_response(nb_id, title))
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        try:
            notebook = await client.notebooks.create(title)
        finally:
            await client.close()

    assert notebook.id == nb_id
    assert list_rpc_count == 1
    # Emitted at a level the default configuration actually passes through.
    # The distinctive clause, not just "baseline list() failed" — that substring
    # was in the pre-#2232 message too, so it no longer pins what the record says.
    assert "baseline list() failed" in caplog.text
    assert "surface as an ambiguity error" in caplog.text


async def test_notebooks_create_baseline_failure_refuses_to_adopt_a_pre_existing_notebook(
    auth_tokens,
) -> None:
    """The #2232 scenario end to end: no baseline + one same-titled notebook.

    The baseline read fails, the create fails at the transport level, and the
    probe then finds a notebook with the requested title that was *already*
    there. Pre-fix the failed baseline degraded to an empty ``set()``, the
    ``nb.id not in baseline_ids`` filter matched everything, and the caller was
    handed ``nb_pre_existing`` as if ``create`` had just made it — after which
    every ``sources.add_*`` and ``chat.ask`` in that session writes into someone
    else's notebook, with nothing anywhere to signal it.

    The load-bearing assertion is negative: whatever else happens, the call must
    not *return* that notebook.
    """
    title = "Quarterly Review"
    pre_existing_id = "nb_pre_existing"

    create_rpc_count = 0
    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_rpc_count, list_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            if list_rpc_count == 1:
                # Baseline read is undecodable -> baseline unavailable.
                return httpx.Response(
                    200, text=_wrb_response(RPCMethod.LIST_NOTEBOOKS.value, "nonsense")
                )
            # Probe: the same-titled notebook that was there all along. The
            # create never committed, so this is NOT ours.
            return httpx.Response(200, text=_list_notebooks_response([(pre_existing_id, title)]))
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_rpc_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(RPCError, match="disambiguate") as exc_info:
            await client.notebooks.create(title)
    finally:
        await client.close()

    # Names the row the caller is being sent to look at, and says why it cannot
    # be attributed — otherwise "check your notebook list" is unactionable.
    assert pre_existing_id in str(exc_info.value)
    assert "baseline" in str(exc_info.value)
    # Unconfirmed, exactly like the sibling source paths: nothing threw inside
    # the probe, so without the marker this reads as a plain rejection and the
    # adapters would hand the caller a "retry me" hint for a create that may
    # already have landed.
    assert getattr(exc_info.value, "unconfirmed", False) is True
    assert classify(exc_info.value).category is ErrorCategory.RPC
    assert classify(exc_info.value).retriable is False
    # The ambiguity aborts the loop rather than firing a second create.
    assert create_rpc_count == 1, f"expected 1 CREATE_NOTEBOOK, got {create_rpc_count}"


async def test_notebooks_create_baseline_failure_still_retries_when_probe_finds_nothing(
    auth_tokens,
) -> None:
    """No baseline and no match is still evidence: the create did not land.

    The sentinel must gate on *matches*, not on its own absence. A probe that
    lists cleanly and finds no notebook with this title has answered the
    question the baseline was only ever needed to disambiguate, so the retry
    proceeds and the create succeeds. Raising here instead would turn every
    blipped baseline into a hard failure on an otherwise recoverable 502 —
    the regression a blanket ``if baseline_ids is None: raise`` would cause.
    """
    title = "Nothing There Yet"
    nb_id_new = "nb_after_retry"

    create_rpc_count = 0
    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_rpc_count, list_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            if list_rpc_count == 1:
                return httpx.Response(
                    200, text=_wrb_response(RPCMethod.LIST_NOTEBOOKS.value, "nonsense")
                )
            # Probe: a notebook exists, but not one with this title.
            return httpx.Response(200, text=_list_notebooks_response([("nb_other", "Unrelated")]))
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_rpc_count += 1
            if create_rpc_count == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, text=_create_notebook_response(nb_id_new, title))
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        notebook = await client.notebooks.create(title)
    finally:
        await client.close()

    assert notebook.id == nb_id_new
    assert create_rpc_count == 2, f"expected 2 CREATE_NOTEBOOK, got {create_rpc_count}"


async def test_notebooks_create_raises_on_ambiguous_probe(auth_tokens) -> None:
    """If the probe finds two new notebooks with the same title, raise rather than guess."""
    title = "Duplicate Title"

    list_rpc_count = 0
    create_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_rpc_count, create_rpc_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_rpc_count += 1
            if list_rpc_count == 1:
                # Empty baseline so both new notebooks fall in the "diff"
                return httpx.Response(200, text=_list_notebooks_response([]))
            # Probe: TWO new notebooks with the same title (e.g. another
            # client created one concurrently while ours was in flight).
            return httpx.Response(
                200,
                text=_list_notebooks_response([("nb_a", title), ("nb_b", title)]),
            )
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_rpc_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(RPCError, match="disambiguate") as exc_info:
            await client.notebooks.create(title)
    finally:
        await client.close()

    # The probe listed fine but cannot attribute either notebook, so the create's
    # outcome is unknown — an unconfirmed create (#2220), not a plain protocol
    # error. The marker is what keeps a caller (or an adapter) from being told to
    # retry a create that may already have landed.
    assert getattr(exc_info.value, "unconfirmed", False) is True
    assert classify(exc_info.value).category is ErrorCategory.RPC
    assert classify(exc_info.value).retriable is False
    # The load-bearing half: an ambiguous probe must ABORT the retry loop, not
    # merely be classified well after another create has already gone out.
    # Classification alone would still pass if the loop kept going.
    assert create_rpc_count == 1, (
        f"expected 1 CREATE_NOTEBOOK (ambiguity aborts the loop), got {create_rpc_count}"
    )


# ---------------------------------------------------------------------------
# Tests: sources.add_url idempotency
# ---------------------------------------------------------------------------


async def test_sources_add_url_idempotent_on_5xx_retry(auth_tokens) -> None:
    """ADD_SOURCE 5xx + probe(list)-finds-url returns the source it created.

    The source is absent from the pre-create baseline and present at probe
    time, so it is provably the one this call committed (#2204).
    """
    notebook_id = "nb_test"
    url = "https://example.com/article"
    src_id = "src_existing"

    add_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count, get_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            return httpx.Response(503, text="service unavailable")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            # SourcesAPI.list calls GET_NOTEBOOK. Any list before the first
            # create attempt is the pre-create baseline; later ones are probes.
            get_count += 1
            rows = [] if add_count == 0 else [(src_id, "Existing", url)]
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, rows),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, url)
    finally:
        await client.close()

    assert source.id == src_id
    assert source.url == url
    # Exactly ONE ADD_SOURCE: no naive re-POST after the 503
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    # TWO GET_NOTEBOOKs: the pre-create baseline plus the probe (#2204).
    assert get_count == 2, f"expected baseline + probe GET_NOTEBOOK, got {get_count}"


async def test_sources_add_youtube_idempotent_on_5xx_retry(auth_tokens) -> None:
    """YouTube branch of add_url shares the same probe-then-retry path."""
    notebook_id = "nb_test"
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    src_id = "src_yt"

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
            rows = [] if add_count == 0 else [(src_id, "Video Title", url)]
            return httpx.Response(
                200,
                # type_code 9: a real YouTube row, not a web-page row wearing a
                # YouTube URL — otherwise this test never sees a YouTube shape.
                text=_get_notebook_with_sources_response(notebook_id, rows, type_code=9),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, url)
    finally:
        await client.close()

    assert source.id == src_id
    assert source.url == url
    assert source.kind == "youtube"
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    assert get_count == 2, f"expected baseline + probe GET_NOTEBOOK, got {get_count}"


# ---------------------------------------------------------------------------
# Tests: sources.add_text — decision-not-to-fix
# ---------------------------------------------------------------------------


async def test_sources_add_text_raises_when_idempotent_True(auth_tokens) -> None:
    """``idempotent=True`` MUST raise before any RPC fires.

    No transport response is queued — the handler asserts no request
    arrives.
    """
    seen_request = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = True
        return httpx.Response(500, text="should not be called")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(NonIdempotentRetryError, match="add_text cannot be marked idempotent"):
            await client.sources.add_text("nb_test", "Title", "Content", idempotent=True)
    finally:
        await client.close()

    assert not seen_request, "add_text(idempotent=True) must raise before issuing any RPC"


async def test_sources_add_text_default_behavior_unchanged(auth_tokens) -> None:
    """Without ``idempotent=``, ``add_text`` issues exactly one ADD_SOURCE on success."""
    notebook_id = "nb_test"
    src_id = "src_text"
    title = "My Note"
    add_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal add_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.ADD_SOURCE.value:
            add_count += 1
            # ADD_SOURCE returns a single-source row that
            # Source.from_api_response can parse. Shape mirrors the
            # add-source response: nested list with the id at [0][0][0]
            # and the title at [0][0][1] for the deeply-nested format.
            return httpx.Response(
                200,
                text=_wrb_response(
                    RPCMethod.ADD_SOURCE.value,
                    [[[src_id], title, [None] * 8, [None, 1]]],
                ),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _open_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_text(notebook_id, title, "the body")
    finally:
        await client.close()

    assert source.id == src_id
    assert add_count == 1, f"expected exactly 1 ADD_SOURCE call, got {add_count}"


# ---------------------------------------------------------------------------
# Tests: disable_internal_retries propagation
# ---------------------------------------------------------------------------


async def test_disable_internal_retries_propagates_to_perform_authed_post(
    auth_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``disable_internal_retries=True`` short-circuits the inner 5xx retry loop.

    With ``server_error_max_retries=2`` and a transport that always
    returns 502:
      - default ``disable_internal_retries=False`` issues 3 POSTs
        (initial + 2 retries) before raising.
      - ``disable_internal_retries=True`` issues exactly 1 POST.

    Swaps the retry seam's ``asyncio.sleep`` for a no-op so the test doesn't
    pay the exponential-backoff wall time on the default-retries path, and
    asserts that seam was actually exercised.
    """
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(502, text="bad gateway")

    transport = httpx.MockTransport(handler)

    # Skip backoff sleeps — only the *count* of retries matters here.
    sleep_calls = 0

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        return None

    # Object-form patch against a locally-imported seam alias (ADR-0007 Form 2):
    # ``resolve_sleep`` re-reads ``asyncio.sleep`` from the ``_runtime.helpers``
    # module global on every call, so swapping ``sleep`` on that module's
    # ``asyncio`` reference is what the retry loop observes.
    monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", _no_sleep)

    # --- with disable_internal_retries=True: exactly 1 POST ------------
    client = await _open_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        request_count = 0
        with pytest.raises(ServerError):
            await client._rpc_executor.rpc_call(
                RPCMethod.LIST_NOTEBOOKS,
                [None, 1, None, [2]],
                disable_internal_retries=True,
            )
        assert request_count == 1, (
            f"with disable_internal_retries=True expected 1 POST, got {request_count}"
        )
    finally:
        await client.close()

    # --- without the flag: default retry loop fires ---------------------
    client = await _open_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        request_count = 0
        with pytest.raises(ServerError):
            await client._rpc_executor.rpc_call(
                RPCMethod.LIST_NOTEBOOKS,
                [None, 1, None, [2]],
            )
        # initial + 2 retries = 3 POSTs
        assert request_count == 3, f"with default retries expected 3 POSTs, got {request_count}"
    finally:
        await client.close()

    # Bite-check: the patched seam must actually have been exercised. The
    # default-retries path fires one backoff sleep per retry (2 retries), so a
    # wrong-namespace patch that silently no-ops would leave ``sleep_calls`` at
    # zero and fail here rather than passing on stale behaviour.
    assert sleep_calls >= 2, (
        f"patched asyncio.sleep seam was not exercised (sleep_calls={sleep_calls}); "
        "the Form-2 object-form patch did not land on the live retry seam"
    )
