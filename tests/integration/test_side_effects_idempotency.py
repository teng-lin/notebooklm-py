"""Side-effect RPC idempotency regression tests (Tier 9, P0-3 + P1-2).

This file validates the Wave-2 classifications added to
``IDEMPOTENCY_REGISTRY`` for the five mutating side-effect RPCs:

* ``DELETE_NOTEBOOK`` → ``IDEMPOTENT_SET_OP`` (delete is idempotent)
* ``DELETE_SOURCE``   → ``IDEMPOTENT_SET_OP``
* ``DELETE_ARTIFACT`` → ``IDEMPOTENT_SET_OP``
* ``REFRESH_SOURCE``  → ``AT_LEAST_ONCE_ACCEPTED`` (extra fetch is acceptable)
* ``SHARE_NOTEBOOK``  → ``NON_IDEMPOTENT_NO_RETRY`` (suppresses blind retry;
                       no reliable probe/retry wrapper exists, so transport
                       loss is surfaced as unconfirmed)

It also exercises the P1 create containment contract: notebook and source
creates make one mutation request, perform no pre-create probe, never re-POST,
and preserve conservative commit evidence across the full executor path.

Tests use ``httpx.MockTransport`` — no cassettes, no network. They are
opted out of the VCR tier enforcement via
``pytestmark = pytest.mark.allow_no_vcr``.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

import notebooklm._runtime.helpers as _runtime_helpers
from notebooklm import NotebookLMClient, ServerError
from notebooklm._web.policy import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test

pytestmark = pytest.mark.allow_no_vcr


# ---------------------------------------------------------------------------
# Helpers — minimal batchexecute response builders + mock-transport client
# ---------------------------------------------------------------------------


def _wrb_response(rpc_id: str, payload: object) -> str:
    """Build a single-RPC batchexecute response body.

    Mirrors the on-the-wire format used everywhere in the test suite:
    ``)]}}'\\n<len>\\n<chunk>\\n``.
    """
    inner = json.dumps(payload)
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _list_notebooks_response(notebooks: list[tuple[str, str]]) -> str:
    """Build a LIST_NOTEBOOKS response from ``[(notebook_id, title), ...]``."""
    raw = [
        [title, None, nb_id, "📘", None, [None, None, None, None, None, [1704067200, 0]]]
        for nb_id, title in notebooks
    ]
    return _wrb_response(RPCMethod.LIST_NOTEBOOKS.value, [raw])


async def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Open a real lifecycle generation, then install the mock transport."""
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
    """Extract the ``rpcids=`` query param from a batchexecute request URL."""
    for key, value in request.url.params.multi_items():
        if key == "rpcids":
            return value
    return None


# ===========================================================================
# Registry classifications — direct lookup
# ===========================================================================


def test_delete_notebook_classified_idempotent_set_op() -> None:
    """``DELETE_NOTEBOOK`` is an idempotent set-op (registry entry only)."""
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.DELETE_NOTEBOOK)
    assert entry.policy is IdempotencyPolicy.IDEMPOTENT_SET_OP
    assert entry.notes  # non-empty rationale


def test_delete_source_classified_idempotent_set_op() -> None:
    """``DELETE_SOURCE`` is an idempotent set-op."""
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.DELETE_SOURCE)
    assert entry.policy is IdempotencyPolicy.IDEMPOTENT_SET_OP


def test_delete_artifact_classified_idempotent_set_op() -> None:
    """``DELETE_ARTIFACT`` is an idempotent set-op."""
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.DELETE_ARTIFACT)
    assert entry.policy is IdempotencyPolicy.IDEMPOTENT_SET_OP


def test_refresh_source_classified_at_least_once_accepted() -> None:
    """``REFRESH_SOURCE`` accepts at-least-once retry semantics."""
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.REFRESH_SOURCE)
    assert entry.policy is IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED


def test_share_notebook_classified_non_idempotent_no_retry() -> None:
    """``SHARE_NOTEBOOK`` has no reliable probe wrapper and cannot replay."""
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.SHARE_NOTEBOOK)
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert "no reliable probe/retry wrapper" in entry.notes


# ===========================================================================
# Delete RPCs keep today's retry behavior (IDEMPOTENT_SET_OP is silent)
# ===========================================================================


async def test_delete_notebook_retries_remain_enabled(
    auth_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``IDEMPOTENT_SET_OP`` MUST be behavior-neutral: the transport's
    inner retry loop continues to fire on 5xx — today's behavior is
    preserved, the registry just documents *why* it is safe.
    """
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.DELETE_NOTEBOOK.value:
            request_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    sleep_calls = 0

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    # Object-form (ADR-0007): patch ``sleep`` on the ``asyncio`` module
    # object that ``_runtime.helpers.resolve_sleep`` re-reads on every
    # call. ``_runtime_helpers.asyncio`` IS the singleton ``asyncio``
    # module, so this is functionally identical to the string-target
    # form while staying off the forbidden-monkeypatch allowlist.
    monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", _no_sleep)

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError):
            await client.notebooks.delete("nb_x")
        # initial + 2 retries = 3 POSTs (IDEMPOTENT_SET_OP leaves caller-False alone)
        assert request_count == 3, (
            f"DELETE_NOTEBOOK with IDEMPOTENT_SET_OP expected 3 POSTs "
            f"(initial + 2 retries), got {request_count}"
        )
        # Bite-check: the patched sleep was actually invoked between
        # retries, proving the object-form patch reached the production
        # ``resolve_sleep`` seam (2 retries → 2 backoff sleeps).
        assert sleep_calls >= 1, "patched asyncio.sleep was never invoked"
    finally:
        await client.close()


async def test_delete_source_retries_remain_enabled(
    auth_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DELETE_SOURCE`` retries continue under IDEMPOTENT_SET_OP."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        if _rpc_id_in_request(request) == RPCMethod.DELETE_SOURCE.value:
            request_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    sleep_calls = 0

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    # Object-form (ADR-0007): see ``test_delete_notebook_retries_remain_enabled``.
    monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", _no_sleep)

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError):
            await client.sources.delete("nb_x", "src_x")
        assert request_count == 3, f"expected 3 POSTs, got {request_count}"
        # Bite-check: patched sleep observed between retries.
        assert sleep_calls >= 1, "patched asyncio.sleep was never invoked"
    finally:
        await client.close()


async def test_delete_artifact_retries_remain_enabled(
    auth_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DELETE_ARTIFACT`` retries continue under IDEMPOTENT_SET_OP."""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        if _rpc_id_in_request(request) == RPCMethod.DELETE_ARTIFACT.value:
            request_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    sleep_calls = 0

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    # Object-form (ADR-0007): see ``test_delete_notebook_retries_remain_enabled``.
    monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", _no_sleep)

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError):
            await client.artifacts.delete("nb_x", "art_x")
        assert request_count == 3, f"expected 3 POSTs, got {request_count}"
        # Bite-check: patched sleep observed between retries.
        assert sleep_calls >= 1, "patched asyncio.sleep was never invoked"
    finally:
        await client.close()


# ===========================================================================
# REFRESH_SOURCE — AT_LEAST_ONCE_ACCEPTED emits a rate-limited WARN
# ===========================================================================


async def test_refresh_source_emits_rate_limited_warn(
    auth_tokens,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``REFRESH_SOURCE`` emits exactly one WARN to flag at-least-once
    semantics, and the warn is rate-limited so 5 invocations produce
    ≤2 lines (mirrors the registry's per-(method, variant) throttle)."""
    # Clear the rate-limit ledger so a window tripped by a prior test
    # doesn't suppress the WARN we expect here.
    import notebooklm._web.policy as policy_mod

    monkeypatch.setattr(policy_mod, "_at_least_once_last_logged", {})

    invocations = 5
    refresh_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        if _rpc_id_in_request(request) == RPCMethod.REFRESH_SOURCE.value:
            refresh_count += 1
            # REFRESH_SOURCE's success response is a no-data null body
            # (the API uses allow_null=True). Mirror that shape.
            return httpx.Response(200, text=_wrb_response(RPCMethod.REFRESH_SOURCE.value, None))
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with caplog.at_level(logging.WARNING, logger="notebooklm._idempotency"):
            for _ in range(invocations):
                ok = await client.sources.refresh("nb_x", "src_x")
                assert ok is None  # v0.8.0 (#1290): returns None on success
    finally:
        await client.close()

    warn_records = [
        r
        for r in caplog.records
        if r.name.startswith("notebooklm._idempotency") and r.levelno >= logging.WARNING
    ]
    assert refresh_count == invocations, f"transport saw {refresh_count} REFRESH_SOURCE calls"
    assert 1 <= len(warn_records) <= 2, (
        f"AT_LEAST_ONCE_ACCEPTED emitted {len(warn_records)} WARN lines for "
        f"{invocations} calls; expected 1-2 (rate-limited)"
    )
    # The WARN message names REFRESH_SOURCE explicitly so operators can
    # grep logs for the affected RPC.
    assert any("REFRESH_SOURCE" in r.getMessage() for r in warn_records)


# ===========================================================================
# SHARE_NOTEBOOK — no reliable probe, no blind retry, unconfirmed on transport loss
# ===========================================================================


async def test_share_notebook_does_not_retry_on_5xx(
    auth_tokens,
) -> None:
    """``SHARE_NOTEBOOK`` is NON_IDEMPOTENT_NO_RETRY, which forces
    ``disable_internal_retries=True`` inside the executor — a 5xx MUST
    surface immediately as unconfirmed because no reliable probe wrapper
    can decide whether the ACL mutation landed before re-issuing.

    Today a blind retry would risk re-sending invitation emails or
    double-flipping public/private access; this test pins the policy.
    """
    share_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal share_count
        if _rpc_id_in_request(request) == RPCMethod.SHARE_NOTEBOOK.value:
            share_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    # No sleep-seam patch is needed here: NON_IDEMPOTENT_NO_RETRY forces
    # ``disable_internal_retries=True`` → exactly 1 POST with no retry
    # loop, so no backoff sleep ever fires. The assertion below
    # (``share_count == 1``) is what pins the suppressed-retry policy;
    # a regression to blind retries fails it directly (with real
    # ``asyncio.sleep`` adding wall-time but still surfacing the bug).

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens, server_error_max_retries=5)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError) as exc_info:
            await client.sharing.set_public("nb_x", True)
        # NON_IDEMPOTENT_NO_RETRY forces disable_internal_retries=True → exactly 1 POST.
        # Even with server_error_max_retries=5, the registry suppresses retries and the
        # public workflow preserves the unresolved commit outcome explicitly.
        assert share_count == 1, (
            f"SHARE_NOTEBOOK with NON_IDEMPOTENT_NO_RETRY expected 1 POST "
            f"(no blind retry), got {share_count}"
        )
        assert exc_info.value.unconfirmed is True
    finally:
        await client.close()


# ===========================================================================
# P1-2 — NotebooksAPI.create probe propagates NetworkError
# ===========================================================================


async def test_notebooks_create_probe_propagates_network_error(
    auth_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notebook create on 5xx sends once and performs no baseline/readback probe."""
    list_call_count = 0
    create_call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_call_count, create_call_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_call_count += 1
            if list_call_count == 1:
                # Baseline list — empty.
                return httpx.Response(200, text=_list_notebooks_response([]))
            # Probe list — simulate a transport-level connection failure.
            # Raising httpx.ConnectError from the handler lets the client
            # see it as a connection failure (translated to NetworkError).
            raise httpx.ConnectError("simulated probe-time network drop")
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_call_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    # Skip backoff sleeps so the test doesn't pay the inner-retry wall time
    # on the probe's LIST_NOTEBOOKS retries (LIST_NOTEBOOKS is explicitly
    # retry-safe, so the transport still retries 5xx/network errors there).
    sleep_calls = 0

    async def _no_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    # Object-form (ADR-0007): see ``test_delete_notebook_retries_remain_enabled``.
    monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", _no_sleep)

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError) as caught:
            await client.notebooks.create("Some Title")
    finally:
        await client.close()

    assert getattr(caught.value, "unconfirmed", False) is True
    assert sleep_calls == 0
    assert list_call_count == 0
    assert create_call_count == 1


async def test_notebooks_create_probe_propagates_non_network_exception(
    auth_tokens,
) -> None:
    """A second scripted create response is never consumed after a 5xx."""
    list_call_count = 0
    create_call_count = 0
    nb_id_after_retry = "nb_after_retry"
    title = "Retry Title"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_call_count, create_call_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.LIST_NOTEBOOKS.value:
            list_call_count += 1
            if list_call_count == 1:
                # Baseline — empty.
                return httpx.Response(200, text=_list_notebooks_response([]))
            # Probe — return a payload that won't decode into a notebook
            # list. ``_wrb_response`` wraps a malformed inner payload so
            # the decoder raises a DecodingError (NOT a NetworkError).
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.LIST_NOTEBOOKS.value, "definitely-not-a-list"),
            )
        if rpc_id == RPCMethod.CREATE_NOTEBOOK.value:
            create_call_count += 1
            if create_call_count == 1:
                return httpx.Response(502, text="bad gateway")
            # A second create would succeed if one were issued. It must not be:
            # this response is the duplicate the assertion below rules out.
            return httpx.Response(
                200,
                text=_wrb_response(
                    RPCMethod.CREATE_NOTEBOOK.value,
                    [
                        title,
                        None,
                        nb_id_after_retry,
                        "📘",
                        None,
                        [None, None, None, None, None, [1704067200, 0]],
                    ],
                ),
            )
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = await _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(ServerError) as caught:
            await client.notebooks.create(title)
    finally:
        await client.close()

    # The load-bearing assertion: no transport or outer retry may consume the
    # scripted second response.
    assert create_call_count == 1, (
        f"expected 1 CREATE_NOTEBOOK call (the probe could not confirm, so no "
        f"retry was permitted), got {create_call_count}"
    )
    assert getattr(caught.value, "unconfirmed", False) is True
    assert list_call_count == 0


@pytest.mark.parametrize(
    "operation",
    ["notebook", "url", "youtube", "drive", "file"],
)
@pytest.mark.asyncio
async def test_create_families_send_once_without_preflight_or_repost(
    auth_tokens,
    tmp_path,
    operation: str,
) -> None:
    """All demoted create families stop after one 5xx mutation attempt."""
    expected_method = {
        "notebook": RPCMethod.CREATE_NOTEBOOK,
        "url": RPCMethod.ADD_SOURCE,
        "youtube": RPCMethod.ADD_SOURCE,
        "drive": RPCMethod.ADD_SOURCE,
        "file": RPCMethod.ADD_SOURCE_FILE,
    }[operation]
    mutation_calls = 0
    readback_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation_calls, readback_calls
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == expected_method.value:
            mutation_calls += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.GET_NOTEBOOK.value:
            readback_calls += 1
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.GET_NOTEBOOK.value, [["Notebook", []]]),
            )
        return httpx.Response(404, text=f"unexpected rpc_id={rpc_id}")

    upload = tmp_path / "one-send.txt"
    upload.write_text("payload")
    client = await _make_client_with_transport(
        httpx.MockTransport(handler), auth_tokens, server_error_max_retries=5
    )
    try:
        with pytest.raises(Exception) as caught:
            if operation == "notebook":
                await client.notebooks.create("One send")
            elif operation == "url":
                await client.sources.add_url("nb", "https://example.com/article")
            elif operation == "youtube":
                await client.sources.add_url("nb", "https://youtube.com/watch?v=dQw4w9WgXcQ")
            elif operation == "drive":
                await client.sources.add_drive("nb", "drive-id", "Drive")
            else:
                await client.sources.add_file("nb", upload)
    finally:
        await client.close()

    assert mutation_calls == 1
    # File registration performs one read-only candidate inspection after
    # uncertainty; no family performs the retired pre-create probe.
    assert readback_calls == (1 if operation == "file" else 0)
    assert getattr(caught.value, "commit_state", None) is CommitState.UNKNOWN
    assert getattr(caught.value, "unconfirmed", False) is True


@pytest.mark.parametrize(
    ("failure", "status", "expected_commit_state"),
    [
        pytest.param(None, 429, CommitState.UNKNOWN, id="http-429"),
        pytest.param(None, 502, CommitState.UNKNOWN, id="http-5xx"),
        pytest.param(None, 401, CommitState.UNKNOWN, id="http-auth"),
        pytest.param(httpx.WriteError, None, CommitState.UNKNOWN, id="write"),
        pytest.param(httpx.ConnectError, None, CommitState.NOT_SENT, id="connect"),
        pytest.param(httpx.PoolTimeout, None, CommitState.NOT_SENT, id="pool"),
    ],
)
@pytest.mark.asyncio
async def test_mutation_transport_evidence_never_reposts(
    auth_tokens,
    failure: type[httpx.RequestError] | None,
    status: int | None,
    expected_commit_state: CommitState,
) -> None:
    """Never replay writes; preserve ambiguity only once transport dispatch is possible."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure is not None:
            raise failure("synthetic transport failure", request=request)
        assert status is not None
        return httpx.Response(status, text="synthetic status")

    client = await _make_client_with_transport(
        httpx.MockTransport(handler), auth_tokens, server_error_max_retries=5
    )
    try:
        with pytest.raises(Exception) as caught:
            await client.notebooks.create("One send")
    finally:
        await client.close()

    assert calls == 1
    assert getattr(caught.value, "commit_state", None) is expected_commit_state
    assert getattr(caught.value, "unconfirmed", False) is (
        expected_commit_state is CommitState.UNKNOWN
    )
