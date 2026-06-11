"""Side-effect RPC idempotency regression tests (Tier 9, P0-3 + P1-2).

This file validates the Wave-2 classifications added to
``IDEMPOTENCY_REGISTRY`` for the five mutating side-effect RPCs:

* ``DELETE_NOTEBOOK`` → ``IDEMPOTENT_SET_OP`` (delete is idempotent)
* ``DELETE_SOURCE``   → ``IDEMPOTENT_SET_OP``
* ``DELETE_ARTIFACT`` → ``IDEMPOTENT_SET_OP``
* ``REFRESH_SOURCE``  → ``AT_LEAST_ONCE_ACCEPTED`` (extra fetch is acceptable)
* ``SHARE_NOTEBOOK``  → ``PROBE_THEN_CREATE`` (suppresses blind retry; uses
                       ``GET_SHARE_STATUS`` as the probe RPC if a future
                       wrapper is added)

It also exercises the P1-2 fix to ``NotebooksAPI.create``: a
``NetworkError`` during the probe ``list()`` MUST propagate, not be
silently coerced to "no match", so the caller learns the prior create
may have committed server-side and the retry loop won't duplicate the
resource.

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
from notebooklm import NetworkError, NotebookLMClient
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
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


def _make_client_with_transport(
    transport: httpx.AsyncBaseTransport,
    auth_tokens,
    *,
    server_error_max_retries: int = 3,
) -> NotebookLMClient:
    """Construct a ``NotebookLMClient`` wired to the supplied mock transport.

    Bypasses the full ``ClientLifecycle.open()`` path so the test doesn't try
    to build a real ``httpx.AsyncClient`` with cookies + connection pool.
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


def test_share_notebook_classified_probe_then_create() -> None:
    """``SHARE_NOTEBOOK`` is PROBE_THEN_CREATE — ``GET_SHARE_STATUS`` exists
    as the server-side probe RPC, so a blind retry is unsafe and the
    transport retry loop MUST be suppressed."""
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.SHARE_NOTEBOOK)
    assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE


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
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
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
        await client._collaborators.kernel.get_http_client().aclose()


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
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError):
            await client.sources.delete("nb_x", "src_x")
        assert request_count == 3, f"expected 3 POSTs, got {request_count}"
        # Bite-check: patched sleep observed between retries.
        assert sleep_calls >= 1, "patched asyncio.sleep was never invoked"
    finally:
        await client._collaborators.kernel.get_http_client().aclose()


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
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=2)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError):
            await client.artifacts.delete("nb_x", "art_x")
        assert request_count == 3, f"expected 3 POSTs, got {request_count}"
        # Bite-check: patched sleep observed between retries.
        assert sleep_calls >= 1, "patched asyncio.sleep was never invoked"
    finally:
        await client._collaborators.kernel.get_http_client().aclose()


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
    import notebooklm._idempotency as idemp_mod

    monkeypatch.setattr(idemp_mod, "_at_least_once_last_logged", {})

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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with caplog.at_level(logging.WARNING, logger="notebooklm._idempotency"):
            for _ in range(invocations):
                ok = await client.sources.refresh("nb_x", "src_x")
                assert ok is None  # v0.8.0 (#1290): returns None on success
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

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
# SHARE_NOTEBOOK — PROBE_THEN_CREATE suppresses the blind transport retry
# ===========================================================================


async def test_share_notebook_does_not_retry_on_5xx(
    auth_tokens,
) -> None:
    """``SHARE_NOTEBOOK`` is PROBE_THEN_CREATE, which forces
    ``disable_internal_retries=True`` inside the executor — a 5xx MUST
    surface immediately so the caller (or a future probe-then-create
    wrapper) decides whether the ACL mutation landed before re-issuing.

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

    # No sleep-seam patch is needed here: PROBE_THEN_CREATE forces
    # ``disable_internal_retries=True`` → exactly 1 POST with no retry
    # loop, so no backoff sleep ever fires. The assertion below
    # (``share_count == 1``) is what pins the suppressed-retry policy;
    # a regression to blind retries fails it directly (with real
    # ``asyncio.sleep`` adding wall-time but still surfacing the bug).

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=5)
    try:
        from notebooklm import ServerError

        with pytest.raises(ServerError):
            await client.sharing.set_public("nb_x", True)
        # PROBE_THEN_CREATE forces disable_internal_retries=True → exactly 1 POST.
        # Even with server_error_max_retries=5, the registry suppresses retries.
        assert share_count == 1, (
            f"SHARE_NOTEBOOK with PROBE_THEN_CREATE expected 1 POST "
            f"(no blind retry), got {share_count}"
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()


# ===========================================================================
# P1-2 — NotebooksAPI.create probe propagates NetworkError
# ===========================================================================


async def test_notebooks_create_probe_propagates_network_error(
    auth_tokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``NetworkError`` raised by the probe ``list()`` MUST propagate
    out of ``NotebooksAPI.create``, not be silently coerced to "no match".

    Before the fix the probe's ``except Exception:`` clause swallowed
    everything and returned ``None``, which let ``idempotent_create``
    re-issue the create on the next attempt — potentially duplicating
    the notebook if the original create actually committed server-side.

    Test setup:
      * LIST_NOTEBOOKS returns an empty baseline on the FIRST call so
        ``baseline_ids = set()`` (the create can proceed).
      * CREATE_NOTEBOOK fails with 502 → the executor's retry loop is
        disabled because CREATE_NOTEBOOK is registered PROBE_THEN_CREATE,
        so the failure surfaces as a single ``ServerError`` (treated as
        a transport failure by ``idempotent_create``).
      * The probe call to LIST_NOTEBOOKS then fails with a *transport-
        layer* connection error that translates to ``NetworkError``.
      * ``NetworkError`` MUST propagate out instead of being swallowed.
    """
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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        with pytest.raises(NetworkError):
            await client.notebooks.create("Some Title")
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # Bite-check: the retry-safe LIST_NOTEBOOKS / CREATE_NOTEBOOK 5xx path
    # exercises the backoff sleep, so the patched seam was invoked —
    # proving the object-form patch reached production ``resolve_sleep``.
    assert sleep_calls >= 1, "patched asyncio.sleep was never invoked"

    # Sanity check: the probe was actually attempted and the create fired
    # once before the probe failed. LIST_NOTEBOOKS is retry-safe so the
    # inner transport retry loop fires for the probe — we don't pin a
    # precise count, only that the probe path was entered (>1 list call).
    assert list_call_count >= 2, (
        f"expected ≥2 LIST_NOTEBOOKS calls (baseline + probe), got {list_call_count}"
    )
    assert create_call_count >= 1, (
        f"expected ≥1 CREATE_NOTEBOOK call before probe NetworkError, got {create_call_count}"
    )


async def test_notebooks_create_probe_swallows_non_network_exception(
    auth_tokens,
) -> None:
    """A non-network exception (decoding error, unexpected RPC failure)
    during the probe MUST still be swallowed → return ``None`` →
    ``idempotent_create`` retries the create.

    This pins the *contract* that the P1-2 fix surgically widened the
    propagation only for ``NetworkError``: random other failures inside
    the probe path stay best-effort, matching the original intent.
    """
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
            # Second create succeeds — ``idempotent_create`` got the
            # swallowed-None probe back and retried per contract.
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
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        notebook = await client.notebooks.create(title)
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert notebook.id == nb_id_after_retry
    assert create_call_count == 2, (
        f"expected 2 CREATE_NOTEBOOK calls (initial + retry after non-network "
        f"probe failure), got {create_call_count}"
    )
