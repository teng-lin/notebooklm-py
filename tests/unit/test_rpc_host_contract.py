"""Host contract for the RPC path (#2067 precursor).

Google serves the personal app from two hosts — ``notebooklm.google.com`` and,
since the Gemini Notebook rebrand, ``notebook.google.com`` (ADR-0028) — and
``NOTEBOOKLM_BASE_URL`` selects between them. Two properties of the RPC path
are load-bearing when that selection changes and neither was asserted:

1. **Which host the endpoint builders target under the default env.** Existing
   coverage (``test_rpc_types.py``, ``test_env_base_url.py``) asserts these only
   under the *enterprise* override, so a change to the default was invisible.
2. **That the RPC path sends no origin-bound header.** It sends none today —
   both request builders return an empty header dict, and this client computes
   no ``SAPISIDHASH``. Pinning that makes *adding* one a reviewed decision
   rather than a drive-by, which matters because such a header is bound to a
   host and nothing offline would catch it naming the wrong one.

Both assertions use **bare literals** deliberately. Writing them against
``get_base_url()`` would make them tautologies that pass under any value — the
defect class catalogued in #2055 — and the whole point is to fail loudly if the
default host changes without that change being intended.
"""

from __future__ import annotations

import pytest

from notebooklm._chat.wire import build_streaming_chat_request
from notebooklm._request_types import AuthSnapshot
from notebooklm.rpc import get_batchexecute_url, get_query_url, get_upload_url

DEFAULT_HOST = "https://notebooklm.google.com"


@pytest.mark.parametrize(
    "builder",
    [get_batchexecute_url, get_query_url, get_upload_url],
    ids=lambda f: f.__name__,
)
def test_endpoint_builders_target_the_default_host(monkeypatch, builder) -> None:
    """Under the default env every RPC endpoint is on the default host.

    Asserted against a literal, not ``get_base_url()``. If the default moves,
    this fails — which is the intent: the move should be a deliberate edit here,
    not a silent consequence elsewhere.
    """
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    assert builder().startswith(f"{DEFAULT_HOST}/"), (
        f"{builder.__name__} does not target {DEFAULT_HOST}; if the default host "
        "moved deliberately, update this literal and the CHANGELOG together"
    )


def test_streaming_chat_sends_no_origin_bound_header() -> None:
    """The chat builder sends no ``Origin`` / ``Referer`` / ``X-Same-Domain``.

    A wire census of the recorded corpus shows zero such headers on any
    batchexecute or streamed-chat request. They are host-bound, and no VCR
    ``match_on`` includes them, so one naming the wrong host would pass every
    offline test and fail every live call. This pins the current contract so
    adding one is a decision someone makes on purpose.
    """
    snapshot = AuthSnapshot(csrf_token="csrf", session_id="sid", authuser=0, account_email=None)
    _url, _body, extra_headers = build_streaming_chat_request(
        snapshot=snapshot,
        notebook_id="nb",
        question="hi",
        source_ids=[],
        conversation_history=None,
        conversation_id=None,
        reqid=1,
    )

    assert extra_headers == {}, (
        "the streamed-chat path gained a header. If it is origin-bound "
        "(Origin/Referer/X-Same-Domain/SAPISIDHASH) it must derive from the "
        "configured host and needs its own host-literal assertion — see #2067"
    )
