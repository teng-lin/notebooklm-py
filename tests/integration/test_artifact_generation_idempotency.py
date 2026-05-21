"""Idempotency tests for CREATE_ARTIFACT and GENERATE_MIND_MAP (P0-3).

These RPCs are mutating writes whose params carry no caller-supplied client
token (see ``_artifact_generation.py``: CREATE_ARTIFACT shape at lines 75-99
/ 143-161 / 266-291 / etc., and GENERATE_MIND_MAP shape at lines 595-604).
Every positional slot is structural — type code, source ids, language,
config block — and the response is what surfaces a server-allocated
``artifact_id`` (``ArtifactGenerationService.parse_generation_result`` in
``_artifact_generation.py`` reads ``result[0][0]``). Without a token slot,
the only safe retry policy is
:attr:`~notebooklm._idempotency.IdempotencyPolicy.PROBE_THEN_CREATE`, which
forces the transport's inner retry loop OFF so a 5xx after server-side
commit cannot trigger a duplicate write.

This file exercises that classification end-to-end:

1. The registry classifies both methods as PROBE_THEN_CREATE.
2. A 503 on the first POST surfaces as a single ``ServerError`` to the
   caller — i.e. ``_perform_authed_post`` does NOT silently re-POST.
   This is the "commit-lost-response" safety property.
3. Happy-path calls still return the artifact / mind-map cleanly.

Wave 2 follow-up: a caller-owned ``idempotent_create`` wrapper around
``ArtifactGenerationService.call_generate`` can later layer
probe-and-return semantics on top of this foundation (using
``client.artifacts.list()`` as the baseline-diff probe). That work is
out of scope here per the b-generation task spec.
"""

from __future__ import annotations

import json

import httpx
import pytest

from notebooklm import NotebookLMClient, RateLimitError, ServerError
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.rpc import RPCMethod

# Mock-transport idempotency tests; no HTTP, no cassette. Opt out of the
# tier-enforcement hook in ``tests/integration/conftest.py``.
pytestmark = pytest.mark.allow_no_vcr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrb_response(rpc_id: str, payload: object) -> str:
    """Build a single-RPC ``batchexecute`` response body.

    Mirrors the on-the-wire format ``)]}}'\\n<len>\\n<chunk>\\n`` used by
    the other mock-transport idempotency tests
    (``tests/integration/concurrency/test_idempotency_create.py``).
    """
    inner = json.dumps(payload)
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _create_artifact_response(artifact_id: str) -> str:
    """Build a CREATE_ARTIFACT success response.

    Shape mirrors the fixture used in ``test_artifacts_integration.py``::

        [[artifact_id, "Audio Overview", "2024-01-05", None, 1]]

    Decoded by ``_parse_generation_result``:
    ``result[0][0]`` → ``artifact_id``, ``result[0][4]`` → status code.
    """
    return _wrb_response(
        RPCMethod.CREATE_ARTIFACT.value,
        [[artifact_id, "Audio Overview", "2024-01-05", None, 1]],
    )


def _generate_mind_map_response(mind_map_json: str) -> str:
    """Build a GENERATE_MIND_MAP success response.

    Shape: ``[[mind_map_json_str]]`` — decoded by ``generate_mind_map``
    via ``result[0][0]`` (line 614-622 of ``_artifact_generation.py``).
    """
    return _wrb_response(RPCMethod.GENERATE_MIND_MAP.value, [[mind_map_json]])


def _get_notebook_response(notebook_id: str = "nb_test") -> str:
    """Build a GET_NOTEBOOK response with one source.

    The ``generate_audio`` / ``generate_mind_map`` call paths fetch source
    ids via ``GET_NOTEBOOK`` when ``source_ids=None`` is passed (the
    default). Reused across every handler in this file.
    """
    return _wrb_response(
        RPCMethod.GET_NOTEBOOK.value,
        [
            [
                "Test Notebook",
                [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                notebook_id,
                "📘",
                None,
                [None, None, None, None, None, [1704067200, 0]],
            ]
        ],
    )


def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens: object,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Construct a ``NotebookLMClient`` wired to a mock transport.

    Bypasses the real ``Session.open()`` path (which would build a real
    ``httpx.AsyncClient`` + cookie jar) by stubbing in a pre-built
    ``AsyncClient`` whose transport is the test's mock. Mirrors the helper
    in ``tests/integration/concurrency/test_idempotency_create.py``.
    """
    client = NotebookLMClient(
        auth_tokens,  # type: ignore[arg-type]
        server_error_max_retries=server_error_max_retries,
    )
    client._session._http_client = httpx.AsyncClient(
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
# Registry classification
# ---------------------------------------------------------------------------


class TestRegistryClassification:
    """Both methods MUST register as PROBE_THEN_CREATE at the (method, None) slot.

    This is the contract that lets ``RpcExecutor`` resolve
    ``effective_disable_internal_retries=True`` for the call sites that
    pass ``operation_variant=None`` (i.e. every CREATE_ARTIFACT and
    GENERATE_MIND_MAP caller in ``_artifact_generation.py``).
    """

    def test_create_artifact_classified_as_probe_then_create(self) -> None:
        entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.CREATE_ARTIFACT)
        assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
        # CREATE_ARTIFACT params carry no caller-supplied token slot, so
        # client_token_field MUST remain unset.
        assert entry.client_token_field is None

    def test_generate_mind_map_classified_as_probe_then_create(self) -> None:
        entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.GENERATE_MIND_MAP)
        assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
        assert entry.client_token_field is None

    def test_create_artifact_variant_none_explicit(self) -> None:
        """Passing ``operation_variant=None`` (the b1 plumbed call-site
        kwarg) resolves to the same (method, None) PROBE_THEN_CREATE entry.

        This guards against a future variant table being added for
        CREATE_ARTIFACT and silently masking the PROBE_THEN_CREATE
        classification for the no-variant path.
        """
        entry = IDEMPOTENCY_REGISTRY.get_entry(
            RPCMethod.CREATE_ARTIFACT,
            operation_variant=None,
        )
        assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE

    def test_generate_mind_map_variant_none_explicit(self) -> None:
        entry = IDEMPOTENCY_REGISTRY.get_entry(
            RPCMethod.GENERATE_MIND_MAP,
            operation_variant=None,
        )
        assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE


# ---------------------------------------------------------------------------
# Commit-lost-response: 5xx must NOT trigger transport-level re-POST
# ---------------------------------------------------------------------------


async def test_create_artifact_503_does_not_re_post(auth_tokens) -> None:
    """A 503 on CREATE_ARTIFACT surfaces as ServerError after a single POST.

    Before classification: the inner ``_perform_authed_post`` retry loop
    would re-POST CREATE_ARTIFACT on the 5xx, duplicating the
    server-side commit (the original audit P0-3 failure mode).

    After classification (PROBE_THEN_CREATE):
    ``effective_disable_internal_retries=True`` is forced by the registry,
    so the first 5xx surfaces immediately. Exactly ONE CREATE_ARTIFACT
    POST hits the wire — no naive re-POST.
    """
    create_count = 0
    get_notebook_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count, get_notebook_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            # generate_audio fetches source ids; return one source.
            get_notebook_count += 1
            return httpx.Response(200, text=_get_notebook_response())
        if rpc_id == RPCMethod.CREATE_ARTIFACT.value:
            create_count += 1
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(404, text=f"unexpected rpc: {rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.artifacts.generate_audio(notebook_id="nb_test")
    finally:
        await client._session._http_client.aclose()

    # Exactly ONE CREATE_ARTIFACT POST despite ``server_error_max_retries=3``
    # being configured: the PROBE_THEN_CREATE policy forced retries off.
    assert create_count == 1, f"expected 1 CREATE_ARTIFACT POST, got {create_count}"
    # Sanity-check the source-fetch did happen exactly once (pre-flight,
    # unaffected by classification).
    assert get_notebook_count == 1


async def test_create_artifact_429_does_not_re_post(auth_tokens) -> None:
    """A 429 on CREATE_ARTIFACT surfaces as ``RateLimitError`` after one POST.

    ``_perform_authed_post`` shares the same ``disable_internal_retries``
    short-circuit for both 429 and 5xx paths
    (``_authed_transport.py:325-361`` for 429,
    ``_authed_transport.py:363-409`` for 5xx). The PROBE_THEN_CREATE
    classification must therefore prevent rate-limit retries from
    silently re-issuing a committed-but-throttled-response request.
    """
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            return httpx.Response(200, text=_get_notebook_response())
        if rpc_id == RPCMethod.CREATE_ARTIFACT.value:
            create_count += 1
            return httpx.Response(429, text="rate limited")
        return httpx.Response(404, text=f"unexpected rpc: {rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(RateLimitError):
            await client.artifacts.generate_audio(notebook_id="nb_test")
    finally:
        await client._session._http_client.aclose()

    assert create_count == 1, f"expected 1 CREATE_ARTIFACT POST, got {create_count}"


async def test_generate_mind_map_503_does_not_re_post(auth_tokens) -> None:
    """A 503 on GENERATE_MIND_MAP surfaces as ServerError after a single POST.

    Symmetric to ``test_create_artifact_503_does_not_re_post``. The
    GENERATE_MIND_MAP call site at ``_artifact_generation.py:606-611``
    must inherit the PROBE_THEN_CREATE classification and disable the
    transport's inner retry loop.
    """
    mind_map_count = 0
    get_notebook_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mind_map_count, get_notebook_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            get_notebook_count += 1
            return httpx.Response(200, text=_get_notebook_response())
        if rpc_id == RPCMethod.GENERATE_MIND_MAP.value:
            mind_map_count += 1
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(404, text=f"unexpected rpc: {rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError):
            await client.artifacts.generate_mind_map(notebook_id="nb_test")
    finally:
        await client._session._http_client.aclose()

    assert mind_map_count == 1, f"expected 1 GENERATE_MIND_MAP POST, got {mind_map_count}"
    assert get_notebook_count == 1


# ---------------------------------------------------------------------------
# Happy path: classification is invisible to a clean call
# ---------------------------------------------------------------------------


async def test_create_artifact_happy_path_still_returns_artifact(auth_tokens) -> None:
    """A clean 200 response under PROBE_THEN_CREATE classification still works.

    Guards against a regression where forcing
    ``disable_internal_retries=True`` somehow changes the success path
    (it should not — retries are only suppressed; the first successful
    response is returned as-is).
    """
    artifact_id = "artifact_happy"
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            return httpx.Response(200, text=_get_notebook_response())
        if rpc_id == RPCMethod.CREATE_ARTIFACT.value:
            create_count += 1
            return httpx.Response(200, text=_create_artifact_response(artifact_id))
        return httpx.Response(404, text=f"unexpected rpc: {rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        status = await client.artifacts.generate_audio(notebook_id="nb_test")
    finally:
        await client._session._http_client.aclose()

    assert status.task_id == artifact_id
    assert create_count == 1


async def test_generate_mind_map_happy_path_still_returns_mind_map(auth_tokens) -> None:
    """A clean 200 response under PROBE_THEN_CREATE for GENERATE_MIND_MAP works.

    Symmetric guard to ``test_create_artifact_happy_path_still_returns_artifact``.

    The mind-map flow also persists a note after the RPC succeeds; we
    stub the ``note_service.create_note`` seam on the artifacts API so
    the test stays focused on the RPC-layer behavior and doesn't pull
    in the full notes-API path. (Phase 5 moved the persistence call off
    the module-level ``_mind_map.create_note`` shim and onto the
    injected ``NoteService`` instance.)
    """
    from unittest.mock import AsyncMock

    from notebooklm.types import Note

    mind_map_dict = {"name": "Test Mind Map", "children": []}
    mind_map_json = json.dumps(mind_map_dict)

    mind_map_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mind_map_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            return httpx.Response(200, text=_get_notebook_response())
        if rpc_id == RPCMethod.GENERATE_MIND_MAP.value:
            mind_map_count += 1
            return httpx.Response(200, text=_generate_mind_map_response(mind_map_json))
        return httpx.Response(404, text=f"unexpected rpc: {rpc_id}")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)

    stub_note = Note(id="note_stub", notebook_id="nb_test", title="Test Mind Map", content="")
    client.artifacts._note_service.create_note = AsyncMock(return_value=stub_note)  # type: ignore[method-assign]

    try:
        result = await client.artifacts.generate_mind_map(notebook_id="nb_test")
    finally:
        await client._session._http_client.aclose()

    assert result["mind_map"] == mind_map_dict
    assert result["note_id"] == "note_stub"
    assert mind_map_count == 1
