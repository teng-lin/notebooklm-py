"""Idempotency-policy regression tests for the note creation RPC family.

Tier 9 Wave 2 task ``b-research-notes`` (P0-3-notes). ``CREATE_NOTE`` is
classified as ``NON_IDEMPOTENT_NO_RETRY`` for both operation variants:

* ``"plain"`` — the default ``NoteService.create_note`` path. Params
  ``[notebook_id, "", [1], None, title]``. The server ignores the title
  slot; ``UPDATE_NOTE`` follows up to set it. A 5xx mid-CREATE_NOTE
  leaves no client-visible ``note_id``, so even a probe against
  ``GET_NOTES_AND_MIND_MAPS`` cannot reliably bind to the row this
  specific call committed.
* ``"saved_from_chat"`` — the 7-element variant built by
  ``build_save_chat_as_note_params`` (issue #660). Title is settable
  but the server may apply smart-title generation, breaking a
  title-based probe. Content (the chat answer) is also not a safe
  fingerprint — chat answers can recur if the user re-saves the same
  result twice intentionally.

Both variants share the policy but are registered with explicit variant
keys so the registry documents the two distinct param shapes. Variant
strings: ``"plain"`` (5-element) and ``"saved_from_chat"`` (7-element).

The commit-lost-response model used by these tests:

1. Mock transport returns 502 on the first CREATE_NOTE request.
2. ``effective_disable_internal_retries=True`` (forced by the policy)
   means the inner retry loop fires zero times — the 502 surfaces as
   ``ServerError``.
3. Test asserts exactly ONE request landed on the wire.
"""

from __future__ import annotations

import json

import httpx
import pytest

from notebooklm import NotebookLMClient, ServerError
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.rpc import RPCMethod
from notebooklm.types import AskResult, ChatReference

# Mock-transport tests; no HTTP / no cassette.
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
    client._session._kernel.http_client = httpx.AsyncClient(
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
# Registry classification — both CREATE_NOTE variants are NON_IDEMPOTENT_NO_RETRY
# ---------------------------------------------------------------------------


def test_create_note_default_variant_classified_non_idempotent() -> None:
    """The default ``(CREATE_NOTE, None)`` entry is NON_IDEMPOTENT_NO_RETRY.

    The plain ``NoteService.create_note`` path uses the 5-element
    params and is the default variant when callers don't pass an explicit
    ``operation_variant``.
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.CREATE_NOTE)
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert entry.notes


def test_create_note_plain_variant_classified_non_idempotent() -> None:
    """The explicit ``"plain"`` variant is NON_IDEMPOTENT_NO_RETRY.

    Same policy as the default; the variant key exists to document the
    5-element param shape distinct from the 7-element saved-from-chat
    shape. The decoupling lets a future change classify one variant
    differently without touching the other.
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(
        RPCMethod.CREATE_NOTE,
        operation_variant="plain",
    )
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert entry.notes


def test_create_note_saved_from_chat_variant_classified_non_idempotent() -> None:
    """The ``"saved_from_chat"`` variant is NON_IDEMPOTENT_NO_RETRY.

    Used by ``_chat_notes.save_chat_answer_as_note`` (issue #660). The
    7-element params carry rich content; title-based probes break under
    server-side smart-title generation, and chat-answer fingerprints are
    not unique enough for safe dedupe.
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(
        RPCMethod.CREATE_NOTE,
        operation_variant="saved_from_chat",
    )
    assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert entry.notes


# ---------------------------------------------------------------------------
# Commit-lost-response behavior: exactly ONE CREATE_NOTE per call
# ---------------------------------------------------------------------------


async def test_create_note_plain_no_inner_retry_on_5xx(auth_tokens) -> None:
    """A 502 on ``notes.create()`` fires exactly ONE CREATE_NOTE POST.

    ``NotesAPI.create`` → ``NoteService.create_note`` issues
    CREATE_NOTE (and on success, a follow-up UPDATE_NOTE). With the
    plain variant classified NON_IDEMPOTENT_NO_RETRY, the inner retry
    loop is disabled and the CREATE_NOTE 502 surfaces immediately. The
    UPDATE_NOTE never runs because the create raised.
    """
    notebook_id = "nb_test"
    create_count = 0
    update_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count, update_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.CREATE_NOTE.value:
            create_count += 1
            return httpx.Response(502, text="bad gateway")
        if rpc_id == RPCMethod.UPDATE_NOTE.value:
            update_count += 1
            return httpx.Response(200, text=_wrb_response(RPCMethod.UPDATE_NOTE.value, []))
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=3)
    try:
        with pytest.raises(ServerError):
            await client.notes.create(notebook_id, title="My Note", content="hello")
    finally:
        await client._session._kernel.get_http_client().aclose()

    assert create_count == 1, (
        f"expected exactly 1 CREATE_NOTE (NON_IDEMPOTENT_NO_RETRY), got {create_count}"
    )
    # CREATE_NOTE failed, so the follow-up UPDATE_NOTE must not have run.
    assert update_count == 0, (
        f"expected 0 UPDATE_NOTE after CREATE_NOTE failure, got {update_count}"
    )


async def test_save_answer_as_note_no_inner_retry_on_5xx(auth_tokens) -> None:
    """A 502 on ``chat.save_answer_as_note()`` fires exactly ONE CREATE_NOTE POST.

    The saved-from-chat variant is a single round-trip — no follow-up
    UPDATE_NOTE. Classifying it NON_IDEMPOTENT_NO_RETRY forces the inner
    retry loop off so the 502 surfaces after a single POST.
    """
    notebook_id = "nb_test"
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.CREATE_NOTE.value:
            create_count += 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens, server_error_max_retries=3)
    ask_result = AskResult(
        answer="The sky is blue [1].",
        conversation_id="conv_test",
        turn_number=1,
        is_follow_up=False,
        references=[
            ChatReference(
                source_id="src_a",
                cited_text="The sky appears blue due to Rayleigh scattering.",
                start_char=0,
                end_char=48,
                citation_number=1,
                chunk_id="chunk_1",
                passage_id=None,
            ),
        ],
    )
    try:
        with pytest.raises(ServerError):
            await client.chat.save_answer_as_note(notebook_id, ask_result, title="Chat note")
    finally:
        await client._session._kernel.get_http_client().aclose()

    assert create_count == 1, (
        f"expected exactly 1 CREATE_NOTE (saved_from_chat variant, "
        f"NON_IDEMPOTENT_NO_RETRY), got {create_count}"
    )


# ---------------------------------------------------------------------------
# Happy path stays unchanged — exactly ONE CREATE_NOTE on 200
# ---------------------------------------------------------------------------


async def test_create_note_happy_path_one_post(auth_tokens) -> None:
    """A successful ``notes.create()`` issues exactly ONE CREATE_NOTE.

    Regression-protects the "no behavioral drift on the success path"
    acceptance: classifying the RPC as NON_IDEMPOTENT_NO_RETRY must not
    introduce extra requests when the call succeeds normally. The
    follow-up UPDATE_NOTE is part of the normal create flow (one
    persists the row, the other persists title + content).
    """
    notebook_id = "nb_test"
    create_count = 0
    update_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count, update_count
        rpc_id = _rpc_id_in_request(request)
        if rpc_id == RPCMethod.CREATE_NOTE.value:
            create_count += 1
            # Response shape: [[note_id, ...]] — see NoteService.create_note parse.
            return httpx.Response(
                200, text=_wrb_response(RPCMethod.CREATE_NOTE.value, [["note_xyz"]])
            )
        if rpc_id == RPCMethod.UPDATE_NOTE.value:
            update_count += 1
            return httpx.Response(200, text=_wrb_response(RPCMethod.UPDATE_NOTE.value, []))
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)
    client = _make_client_with_transport(transport, auth_tokens)
    try:
        note = await client.notes.create(notebook_id, title="Happy", content="body")
    finally:
        await client._session._kernel.get_http_client().aclose()

    assert note.id == "note_xyz"
    assert create_count == 1
    assert update_count == 1  # follow-up update is the success-path norm
