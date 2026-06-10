"""MCP chat-tool VCR test (reuse-only).

``chat_ask`` over ``chat_ask.yaml`` — pins the real-decode → MCP-wire-shape
serialization of a chat answer WITH citations (this recording carries seven
references, so it exercises the citation path).

RPC fan-out (matches the recorded cassette): ``chat_ask`` calls
``client.chat.ask(notebook, question, conversation_id=None)``. With
``source_ids`` defaulting to ``None`` the client fetches the notebook's source
ids (``GET_NOTEBOOK`` → ``rLM1Ne``), POSTs the streamed ask
(``GenerateFreeFormStreamed``), then fetches the new conversation id post-ask
(``GET_LAST_CONVERSATION_ID`` → ``hPTbtc``) — exactly the three RPCs the
cassette recorded. The streamed body's source-id list is rebuilt from the
cassette's own ``rLM1Ne`` response, so it matches the recorded streamed body.

(The sibling ``chat_ask_with_references.yaml`` was tried first but its recorded
``GenerateFreeFormStreamed`` request body does not match a replayed ask — the
streamed-body matcher rejects it — so it does not reuse cleanly. ``chat_ask.yaml``
replays end-to-end AND already carries citations, so it covers the same shape.)

Not covered here (no reusable cassette):

* ``chat_ask`` multi-source / explicit-references variants — ``chat_ask.yaml``
  already exercises the citation path, and the alternative cassettes do not
  reuse: ``chat_ask_with_references.yaml`` fails the streamed-body matcher (see
  above), and ``chat_ask_multi_source.yaml`` records only the trailing
  ``GET_LAST_CONVERSATION_ID`` (``hPTbtc``) leg — it was recorded for the
  *explicit ``source_ids``* path, which the MCP ``chat_ask`` tool does not expose
  (the tool always lets the client resolve sources via ``GET_NOTEBOOK``), so it
  lacks the ``rLM1Ne`` + streamed-ask legs the tool would issue.
* ``chat_configure`` — its core RPC is ``RENAME_NOTEBOOK`` (``s0tc2d``) carrying
  a chat-settings param block; the only ``s0tc2d`` cassettes
  (``notebooks_rename.yaml`` / ``cli_notebook_rename.yaml``) record the *rename*
  param shape (``[notebook_id, [[null,null,null,[null, title]]], null,
  "generic"]``), which is structurally distinct from the chat-config body
  (``[notebook_id, [[null×7, chat_settings]]]``) under the ``freq`` matcher.
  ``settings_set_output_language.yaml`` is a different RPC entirely
  (``GET_USER_SETTINGS`` ``ZwVcOc`` / ``SET_USER_SETTINGS`` ``hT54vc``), not the
  per-notebook chat config. No recorded cassette matches ``chat_configure``'s RPC.

The notebook is invoked by its recorded full UUID so the resolver skips its
``LIST_NOTEBOOKS`` preflight.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``chat_ask.yaml`` was recorded against this notebook.
CHAT_NOTEBOOK_ID = "bb00c9e3-656c-4fd2-b890-2b71e1cf3814"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("chat_ask.yaml")
async def test_mcp_chat_ask_with_references_over_vcr() -> None:
    """``chat_ask`` returns the recorded answer + citations through the real client.

    End-to-end: FastMCP ``Client`` → ``chat_ask`` tool → ``client.chat.ask`` →
    recorded ``rLM1Ne`` (source ids) + streamed ask + ``hPTbtc`` (conversation
    id) RPCs. Asserts the serialized ``AskResult`` wire shape: the answer text,
    the conversation id, and the citation list (each a serialized
    ``ChatReference`` carrying a ``source_id``).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "chat_ask",
            {"notebook": CHAT_NOTEBOOK_ID, "question": "What is this notebook about?"},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # ``AskResult`` → ``{"answer", "conversation_id", "turn_number",
    # "is_follow_up", "references", "raw_response"}`` via to_jsonable.
    answer = structured["answer"]
    assert isinstance(answer, str) and answer.strip(), "expected a non-empty recorded answer"
    assert structured["conversation_id"], "expected a server-recorded conversation id"
    references = structured["references"]
    assert isinstance(references, list)
    assert references, "expected at least one recorded citation (references cassette)"
    first_ref = references[0]
    assert isinstance(first_ref, dict)
    # Each citation is a serialized ChatReference pointing at a source.
    assert first_ref.get("source_id"), "recorded citation is missing a source_id"
