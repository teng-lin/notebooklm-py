"""Idempotency-policy regression tests for the research RPC family.

Tier 9 Wave 2 task ``b-research-notes`` (P0-3-research). Three RPCs are
classified as ``NON_IDEMPOTENT_NO_RETRY`` because none of them accept a
caller-supplied client-token slot and the available probe surface
(``ResearchAPI.poll`` / ``SourcesAPI.list``) cannot reliably disambiguate
a commit-lost retry from a pre-existing peer task:

* ``START_FAST_RESEARCH`` / ``START_DEEP_RESEARCH`` — multiple in-flight
  tasks for the same ``(notebook_id, query)`` are valid, so a query-based
  probe is ambiguous post-commit-lost.
* ``IMPORT_RESEARCH`` — source URLs may already exist in the notebook
  from prior workflows, so a URL-based probe cannot bind to "the row
  this specific import committed".

For ``NON_IDEMPOTENT_NO_RETRY``, the transport's inner 5xx retry loop is
forced off (``effective_disable_internal_retries=True``). The first
network/5xx failure surfaces immediately; the caller decides whether to
poll/list and retry manually.

The commit-lost-response model used by these tests:

1. Mock transport returns 502 on the FIRST start/import request.
2. ``effective_disable_internal_retries=True`` means the inner retry
   loop fires zero times — the 502 surfaces as ``ServerError``.
3. Test asserts exactly ONE request landed on the wire (no naive
   duplicate POST).

Mirrors the pattern in ``tests/integration/concurrency/test_idempotency_create.py``
(audit item #2, sources/notebooks PROBE_THEN_CREATE family).
"""

from __future__ import annotations

import json

import httpx
import pytest

from notebooklm import NotebookLMClient, ServerError
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.rpc import RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test

# Mock-transport tests; no HTTP / no cassette. Opt out of the
# tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


# ---------------------------------------------------------------------------
# Helpers (mirror the pattern from test_idempotency_create.py)
# ---------------------------------------------------------------------------


def _wrb_response(rpc_id: str, payload) -> str:
    """Build a single-RPC batchexecute response body."""
    inner = json.dumps(payload)
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Construct a ``NotebookLMClient`` wired to a mock httpx transport."""
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
    """Extract the ``rpcids=`` query param from a batchexecute request URL."""
    for key, value in request.url.params.multi_items():
        if key == "rpcids":
            return value
    return None


# ---------------------------------------------------------------------------
# Registry classification — every research RPC is NON_IDEMPOTENT_NO_RETRY
# ---------------------------------------------------------------------------


def test_start_fast_research_classified_non_idempotent() -> None:
    """START_FAST_RESEARCH is classified ``NON_IDEMPOTENT_NO_RETRY``.

    No client-token slot in the params shape ``[[query, source_type], None,
    1, notebook_id]``, and a probe by ``(notebook_id, query)`` is ambiguous
    when the user has previously started the same query on the same
    notebook (the server permits multiple in-flight peer tasks).
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.START_FAST_RESEARCH)
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert entry.notes  # human-readable rationale present


def test_start_deep_research_classified_non_idempotent() -> None:
    """START_DEEP_RESEARCH is classified ``NON_IDEMPOTENT_NO_RETRY``.

    Same rationale as ``START_FAST_RESEARCH``: param shape ``[None, [1],
    [query, source_type], 5, notebook_id]`` has no client-token slot.
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.START_DEEP_RESEARCH)
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert entry.notes


def test_import_research_classified_non_idempotent() -> None:
    """IMPORT_RESEARCH is classified ``NON_IDEMPOTENT_NO_RETRY``.

    Params ``[None, [1], task_id, notebook_id, source_array]`` correlate
    by research task_id, but the resulting source rows are not granular
    per-task on the wire, so a post-commit-lost ``sources.list()`` probe
    cannot reliably distinguish "rows from this batch" from "rows from a
    prior workflow that happened to import the same URLs".
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.IMPORT_RESEARCH)
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert entry.notes


# ---------------------------------------------------------------------------
# Commit-lost-response behavior: exactly ONE request per call
# ---------------------------------------------------------------------------


async def test_start_fast_research_no_inner_retry_on_5xx(auth_tokens) -> None:
    """START_FAST_RESEARCH on 502 fires exactly ONE POST (no inner retry).

    With ``NON_IDEMPOTENT_NO_RETRY`` the transport-layer retry loop is
    forced off. A naive duplicate POST would either start a second
    research task (if the first commit landed) or be benign (if it
    didn't); the client cannot tell which, so the policy refuses the
    retry and surfaces the first failure for the caller to act on.
    """
    notebook_id = "nb_test"
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.START_FAST_RESEARCH.value:
            request_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=3)
    try:
        with pytest.raises(ServerError):
            await client.research.start(notebook_id, "what is quantum computing?")
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # NON_IDEMPOTENT_NO_RETRY forces effective_disable_internal_retries=True
    # so the inner retry loop does not fire — exactly ONE POST.
    assert request_count == 1, (
        f"expected exactly 1 START_FAST_RESEARCH (NON_IDEMPOTENT_NO_RETRY), got {request_count}"
    )


async def test_start_deep_research_no_inner_retry_on_5xx(auth_tokens) -> None:
    """START_DEEP_RESEARCH on 503 fires exactly ONE POST (no inner retry).

    Same rationale as the fast-research test — deep-research shares the
    NON_IDEMPOTENT_NO_RETRY classification.
    """
    notebook_id = "nb_test"
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.START_DEEP_RESEARCH.value:
            request_count += 1
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=3)
    try:
        with pytest.raises(ServerError):
            await client.research.start(notebook_id, "deep dive query", mode="deep")
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert request_count == 1, (
        f"expected exactly 1 START_DEEP_RESEARCH (NON_IDEMPOTENT_NO_RETRY), got {request_count}"
    )


async def test_import_research_no_inner_retry_on_5xx(auth_tokens) -> None:
    """IMPORT_RESEARCH on 502 fires exactly ONE POST (no inner retry)."""
    notebook_id = "nb_test"
    task_id = "task_abc"
    sources = [
        {
            "url": "https://example.com/article",
            "title": "An Article",
            "result_type": 1,
            "research_task_id": task_id,
        },
    ]

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.IMPORT_RESEARCH.value:
            request_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=3)
    try:
        with pytest.raises(ServerError):
            await client.research.import_sources(notebook_id, task_id, sources)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert request_count == 1, (
        f"expected exactly 1 IMPORT_RESEARCH (NON_IDEMPOTENT_NO_RETRY), got {request_count}"
    )


# ---------------------------------------------------------------------------
# Happy path stays unchanged — exactly ONE POST on 200, classification
# does NOT regress the success path.
# ---------------------------------------------------------------------------


async def test_start_fast_research_happy_path_one_post(auth_tokens) -> None:
    """A successful START_FAST_RESEARCH fires exactly ONE POST.

    Regression-protects the "no behavioral drift on the success path"
    acceptance: classifying the RPC as NON_IDEMPOTENT_NO_RETRY must not
    introduce any extra request when the call succeeds normally.
    """
    notebook_id = "nb_test"
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.START_FAST_RESEARCH.value:
            request_count += 1
            # task_data envelope: [task_id, report_id, ...]
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.START_FAST_RESEARCH.value, ["task_xyz", "report_xyz"]),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        result = await client.research.start(notebook_id, "test query")
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert result is not None
    assert result.task_id == "task_xyz"
    assert request_count == 1
