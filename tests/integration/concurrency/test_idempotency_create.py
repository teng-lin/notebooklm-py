"""Regression tests for the probe-then-retry idempotency for create RPCs.

Audit item #2 (`thread-safety-concurrency-audit.md` §2):
Pre-fix, mutating create RPCs (CREATE_NOTEBOOK, ADD_SOURCE) ran inside
`_perform_authed_post`'s transport retry loop, so a 5xx / network blip
between server-side commit and client-side response triggered a naive
re-POST that duplicated the resource.

Post-fix:
- Per-call ``disable_internal_retries`` flag suppresses the inner retry
  loop for declared mutating create RPCs.
- An API-layer ``_idempotency.idempotent_create`` wrapper owns
  probe-then-retry with API-specific probes:
    - notebooks.create → baseline-diff by title
    - sources.add_url → list-then-url-match
    - sources._add_youtube_source → list-then-url-match
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
8. disable_internal_retries — propagates through to _perform_authed_post
"""

from __future__ import annotations

import json

import httpx
import pytest

from notebooklm import (
    NonIdempotentRetryError,
    NotebookLMClient,
    RPCError,
    ServerError,
)
from notebooklm.rpc import RPCMethod

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
) -> str:
    """Build a GET_NOTEBOOK response that ``SourcesAPI.list`` parses.

    ``sources`` is ``[(source_id, title, url), ...]``. The shape mirrors
    the parsing path in ``SourcesAPI.list`` (which reads from
    ``GET_NOTEBOOK``): each src entry is roughly
    ``[[id], title, metadata_with_url_at_[7], status]``.
    """
    src_rows = []
    for src_id, title, url in sources:
        # metadata: url at index [7] (matches Source.from_api_response /
        # _extract_source_url precedence, allow_bare_http=False).
        metadata: list = [None] * 8
        metadata[7] = [url]
        # status block at src[3] — [_, READY=2]
        status_block = [None, 2]
        src_rows.append([[src_id], title, metadata, status_block])
    nb_info = ["Test Notebook", src_rows]
    return _wrb_response(RPCMethod.GET_NOTEBOOK.value, [nb_info])


def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Construct a ``NotebookLMClient`` whose underlying httpx client
    uses the supplied mock transport.

    Bypasses the full ``ClientCore.open()`` path (which would try to
    construct a real ``httpx.AsyncClient`` with cookies + connection
    pool) by stubbing in a pre-built ``AsyncClient`` wired to the
    ``transport`` argument.
    """
    client = NotebookLMClient(
        auth_tokens,
        server_error_max_retries=server_error_max_retries,
    )
    client._core._http_client = httpx.AsyncClient(
        transport=transport,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        notebook = await client.notebooks.create(title)
    finally:
        await client._core._http_client.aclose()

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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        notebook = await client.notebooks.create(title)
    finally:
        await client._core._http_client.aclose()

    assert notebook.id == nb_id_new
    # Two CREATE_NOTEBOOK calls: original (502) + retry (200)
    assert create_rpc_count == 2, f"expected 2 CREATE_NOTEBOOK, got {create_rpc_count}"
    # Two LIST_NOTEBOOKS: baseline + probe after the 502
    assert list_rpc_count == 2, f"expected 2 LIST_NOTEBOOKS, got {list_rpc_count}"


async def test_notebooks_create_raises_on_ambiguous_probe(auth_tokens) -> None:
    """If the probe finds two new notebooks with the same title, raise rather than guess."""
    title = "Duplicate Title"

    list_rpc_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_rpc_count
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
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(RPCError, match="disambiguate"):
            await client.notebooks.create(title)
    finally:
        await client._core._http_client.aclose()


# ---------------------------------------------------------------------------
# Tests: sources.add_url idempotency
# ---------------------------------------------------------------------------


async def test_sources_add_url_idempotent_on_5xx_retry(auth_tokens) -> None:
    """ADD_SOURCE 5xx + probe(list)-finds-url returns existing source."""
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
            # SourcesAPI.list calls GET_NOTEBOOK
            get_count += 1
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(notebook_id, [(src_id, "Existing", url)]),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, url)
    finally:
        await client._core._http_client.aclose()

    assert source.id == src_id
    assert source.url == url
    # Exactly ONE ADD_SOURCE: no naive re-POST after the 503
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    # Exactly ONE GET_NOTEBOOK (the probe — no baseline for sources)
    assert get_count == 1, f"expected 1 GET_NOTEBOOK probe, got {get_count}"


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
            return httpx.Response(
                200,
                text=_get_notebook_with_sources_response(
                    notebook_id, [(src_id, "Video Title", url)]
                ),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_url(notebook_id, url)
    finally:
        await client._core._http_client.aclose()

    assert source.id == src_id
    assert source.url == url
    assert add_count == 1, f"expected 1 ADD_SOURCE, got {add_count}"
    assert get_count == 1, f"expected 1 GET_NOTEBOOK probe, got {get_count}"


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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(NonIdempotentRetryError, match="add_text cannot be marked idempotent"):
            await client.sources.add_text("nb_test", "Title", "Content", idempotent=True)
    finally:
        await client._core._http_client.aclose()

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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        source = await client.sources.add_text(notebook_id, title, "the body")
    finally:
        await client._core._http_client.aclose()

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

    Patches ``asyncio.sleep`` to a no-op so the test doesn't pay the
    exponential-backoff wall time on the default-retries path.
    """
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(502, text="bad gateway")

    transport = httpx.MockTransport(handler)

    # Skip backoff sleeps — only the *count* of retries matters here.
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("notebooklm._core.asyncio.sleep", _no_sleep)

    # --- with disable_internal_retries=True: exactly 1 POST ------------
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        request_count = 0
        with pytest.raises(ServerError):
            await client._core.rpc_call(
                RPCMethod.LIST_NOTEBOOKS,
                [None, 1, None, [2]],
                disable_internal_retries=True,
            )
        assert request_count == 1, (
            f"with disable_internal_retries=True expected 1 POST, got {request_count}"
        )
    finally:
        await client._core._http_client.aclose()

    # --- without the flag: default retry loop fires ---------------------
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        request_count = 0
        with pytest.raises(ServerError):
            await client._core.rpc_call(
                RPCMethod.LIST_NOTEBOOKS,
                [None, 1, None, [2]],
            )
        # initial + 2 retries = 3 POSTs
        assert request_count == 3, f"with default retries expected 3 POSTs, got {request_count}"
    finally:
        await client._core._http_client.aclose()
