"""Oversized-question rejection streaming-chat VCR cassette.

Regression guard for discussion #1472. The streaming-chat endpoint rejects an
over-long question (above a ~5.5k-character ceiling) with a ``wrb.fr`` frame
that carries **no answer JSON** (null ``item[2]``) and a bare grpc-style status
at ``item[5]`` — e.g.::

    [["wrb.fr", null, null, null, null, [3]]]   # 3 == INVALID_ARGUMENT
    [["di", 198], ["af.httprm", 197, "...", 6]]
    [["e", 4, null, null, 132]]                  # stream bookkeeping, NOT the error

The pre-fix parser only surfaced ``"er"`` frames and the
``UserDisplayableError`` payload shape, so this bare-status rejection collapsed
into the generic ``ChatResponseParseError`` ("No parseable chunks ..."), masking
the real cause. The ``["e", ...]`` trailer is a batchexecute stream terminator
whose trailing number is a running byte count, not an error code — so the fix
must NOT key off it. ``_chat/wire.py`` now raises a :class:`ChatError`
("rejected by the server (status 3) ...") instead.

This cassette captures one real rejection so the surfacing stays covered in
integration (not just the synthetic unit test in
``tests/unit/test_streaming_chat_wire.py``).

Recording
---------
::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/test_chat_oversized_rejection_vcr.py -v -s

A fresh scratch notebook is created/deleted OUTSIDE the cassette context (its
now-deleted UUID is read back from the cassette on replay). No sources are
needed — the size rejection fires before retrieval. The ``ask`` call is expected
to raise ``ChatError`` during recording; the HTTP interaction is still captured
because VCR records at the transport layer, before the parser runs.

Replay
------
Opts into the ``freq`` body matcher (the ``GenerateFreeFormStreamed`` POST has no
``rpcids`` to match on); ``freq`` disambiguates by param count + ``notebook_id``
at slot 7. The recorded notebook id is read out of the cassette and passed back
into ``ask`` so the replayed request matches the recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml

from notebooklm import NotebookLMClient
from notebooklm.exceptions import ChatError
from tests.integration.conftest import _vcr_record_mode, get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "chat_ask_oversized_rejection.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

_MATCH_ON = ["method", "scheme", "host", "port", "path", "freq"]


def _oversized_question() -> str:
    """A deterministic, PII-free question past the ~5.5k-char ceiling.

    Built from a fixed word list cycled by index (no RNG, so it is identical
    across Python versions and between record and replay). The server limit is
    token-based, not character-based, so this targets ~10k chars — comfortably
    past the live trigger point (empirically ~8k for this vocabulary) while
    keeping the recorded cassette small.
    """
    words = (  # noqa: SIM905 — readable prose source, not a hand-maintained list
        "agent loop context window subagent skill orchestration retrieval citation "
        "notebook source artifact podcast transcript embedding token prompt latency "
        "concurrency idempotency schema decoder encoder envelope batchexecute rpc"
    ).split()
    out = ["Summarize the following requirement in detail:"]
    i = 0
    while sum(len(w) + 1 for w in out) < 10000:
        out.append(words[i % len(words)])
        i += 1
    return " ".join(out)


def _recorded_notebook_id() -> str:
    """Read the scratch ``notebook_id`` (slot 7) out of the recorded cassette."""
    assert CASSETTE_PATH.exists(), (
        f"cassette missing: {CASSETTE_PATH}. "
        "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
    )
    with CASSETTE_PATH.open(encoding="utf-8") as fh:
        cassette = yaml.safe_load(fh)

    chat = [
        i
        for i in cassette.get("interactions", [])
        if "GenerateFreeFormStreamed" in i.get("request", {}).get("uri", "")
    ]
    assert len(chat) == 1, (
        f"expected exactly one GenerateFreeFormStreamed interaction in {CASSETTE_NAME}, "
        f"found {len(chat)}"
    )
    body = chat[0]["request"]["body"]
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    params: list[Any] = json.loads(json.loads(parse_qs(body)["f.req"][0])[1])
    assert len(params) > 7, f"cassette params too short ({len(params)}); re-record."
    notebook_id = params[7]
    assert isinstance(notebook_id, str), f"slot 7 (notebook_id) not a string: {notebook_id!r}"
    return notebook_id


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_oversized_question_raises_chat_error_not_parse_error() -> None:
    """An over-long question surfaces a ``ChatError`` rejection, not a parse error.

    The assertion is the regression: pre-fix this raised
    ``ChatResponseParseError`` ("No parseable chunks ..."). The message must name
    the server status so the real failure (request too large) reaches the caller.
    """
    auth = await get_vcr_auth()
    question = _oversized_question()
    async with NotebookLMClient(auth) as client:
        if _vcr_record_mode:
            notebook = await client.notebooks.create("T#1472 scratch oversized rejection")
            notebook_id = notebook.id
            try:
                with (
                    notebooklm_vcr.use_cassette(CASSETTE_NAME, match_on=_MATCH_ON),
                    pytest.raises(ChatError),
                ):
                    await client.chat.ask(notebook_id, question)
            finally:
                try:
                    await client.notebooks.delete(notebook_id)
                except Exception:  # noqa: BLE001 — best-effort scratch cleanup
                    pass
        else:
            notebook_id = _recorded_notebook_id()
            with (
                notebooklm_vcr.use_cassette(CASSETTE_NAME, match_on=_MATCH_ON),
                pytest.raises(ChatError, match=r"rejected by the server \(status"),
            ):
                await client.chat.ask(notebook_id, question)
