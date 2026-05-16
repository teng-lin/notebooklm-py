"""End-to-end stale-CSRF refresh path under VCR.

This module records and replays the **full three-leg** auth-refresh flow that
``test_auto_refresh.py`` covers only at the httpx-mock layer:

1. **Failing batchexecute** — the client makes a ``LIST_NOTEBOOKS`` POST with a
   deliberately invalidated CSRF token; Google returns HTTP **400** (not 401 —
   stale CSRF is documented at ``_core.py:245-252``).
2. **Homepage GET** — ``_perform_authed_post`` classifies the 400 as an auth
   error via :func:`is_auth_error` and awaits ``refresh_auth``, which fetches
   the NotebookLM homepage to re-extract ``SNlM0e`` + ``FdrFJe``.
3. **Retried batchexecute** — the same RPC is replayed with the fresh CSRF and
   the server returns a normal 200 ``wrb.fr`` envelope.

Why this complements ``test_auto_refresh.py``
---------------------------------------------
The unit-style test in ``test_auto_refresh.py`` proves the retry-loop wiring
fires by swapping in a fake ``http_client.post`` and a fake refresh callback.
This VCR-backed test exercises the SAME state machine through the REAL httpx
client and the REAL ``refresh_auth`` implementation, using a recorded
homepage HTML so the regex extractor for ``SNlM0e`` / ``FdrFJe`` also runs.
The two layers catch different defect classes (mock-shape drift vs. extractor
drift); keep both.

Recording note
--------------
The client construction (``NotebookLMClient.from_storage(...)``) makes its
own homepage GET to mint the initial tokens. That GET is **outside** the
cassette context in record mode so the cassette captures EXACTLY the three
interactions of the refresh path — no auth-bootstrap noise. In replay mode
we substitute synthetic ``AuthTokens`` for the same reason, mirroring the
``mock_auth_for_vcr`` pattern in ``tests/integration/cli_vcr/conftest.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add tests directory to path for vcr_config + conftest helpers.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _vcr_record_mode, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.auth import AuthTokens  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

CASSETTE_NAME = "auth_rotate_cookies_refresh.yaml"

# Skip when no cassettes are available and we're not recording.
pytestmark = [pytest.mark.vcr, skip_no_cassettes]


async def _build_client_for_test() -> NotebookLMClient:
    """Build the client without polluting the cassette with auth-bootstrap traffic.

    Record mode:
        Load real auth via ``NotebookLMClient.from_storage()``. This makes a
        live homepage GET to mint the initial CSRF/session tokens, but that
        call happens OUTSIDE the cassette context (the caller decorates only
        the stale-CSRF section) so the cassette stays at exactly the three
        interactions of the refresh path.

    Replay mode:
        Use synthetic ``AuthTokens``. The cassette has recorded responses;
        the actual token values are irrelevant because VCR matches on
        method + path + ``rpcids`` (see ``vcr_config.notebooklm_vcr``).
    """
    if _vcr_record_mode:
        # ``from_storage`` performs a live homepage GET. Keep this OUT of the
        # cassette context so the recording captures only the refresh path.
        return await NotebookLMClient.from_storage()

    synthetic_auth = AuthTokens(
        cookies={
            "SID": "vcr_mock_sid",
            "HSID": "vcr_mock_hsid",
            "SSID": "vcr_mock_ssid",
            "APISID": "vcr_mock_apisid",
            "SAPISID": "vcr_mock_sapisid",
        },
        csrf_token="vcr_mock_initial_csrf",
        session_id="vcr_mock_session",
    )
    return NotebookLMClient(synthetic_auth)


@pytest.mark.asyncio
async def test_stale_csrf_triggers_refresh_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 from a stale-CSRF batchexecute round-trips through refresh_auth.

    The cassette captures three interactions in order:

    1. POST ``batchexecute?rpcids=wXbhsf`` with the invalidated CSRF →
       HTTP 400.
    2. GET ``https://notebooklm.google.com/`` → 200 with a fresh
       ``SNlM0e`` (CSRF) and ``FdrFJe`` (session) inside the WIZ_global_data
       page chrome.
    3. POST ``batchexecute?rpcids=wXbhsf`` with the refreshed CSRF →
       HTTP 200 with the normal ``LIST_NOTEBOOKS`` ``wrb.fr`` envelope.

    The 400 → refresh → retry sequence is what the ``rpc_call`` layer
    promises in the docstring at ``_core.py:864-894``. Asserting both
    that the call returned a value (the retry succeeded) AND that a
    refresh happened (token mutated mid-call) gives us a fail-loud
    guard against a regression where the retry path silently no-ops.
    """
    # Disable the layer-1 RotateCookies keepalive poke so its POST is not
    # captured into this cassette (recorded cassettes predate that poke;
    # keeping it on would surface as a cassette mismatch on replay).
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")

    client = await _build_client_for_test()
    # Eliminate the post-refresh retry delay so the test runs fast.
    client._core._refresh_retry_delay = 0

    # Track whether refresh_auth ran. We wrap the bound method so the
    # mutation is observable from outside the test. Using ``list[object]``
    # keeps the counter intent obvious without overspecifying element type.
    refresh_calls: list[object] = []
    original_refresh = client.refresh_auth

    async def tracking_refresh() -> AuthTokens:
        refresh_calls.append(None)
        return await original_refresh()

    # The refresh callback is captured by ClientCore at construction;
    # patch it on the core so the wrapper is what the retry loop sees.
    client._core._refresh_callback = tracking_refresh

    with notebooklm_vcr.use_cassette(CASSETTE_NAME) as cassette:
        async with client:
            # Deliberately corrupt the in-memory CSRF so the first
            # batchexecute is guaranteed to draw a 400 from Google (the
            # documented stale-CSRF response per ``_core.py:245-252``).
            # The ``update_auth_headers`` call is what actually plumbs the
            # new value into the live ``httpx.AsyncClient``'s default
            # header set.
            client._core.auth.csrf_token = "INVALID_CSRF_FOR_TEST"
            client._core.update_auth_headers()

            # This call's first attempt MUST 400; the rpc_call layer
            # then awaits refresh_auth (interaction 2 in the cassette)
            # and retries (interaction 3). A successful return here
            # means the whole pipeline replayed correctly.
            notebooks = await client.notebooks.list()

            # The retry path mutated the in-memory token away from
            # the corrupted value. Asserting the change makes this
            # test fail loudly if a future refactor accidentally
            # short-circuits the retry to "return the 400 unchanged".
            assert client._core.auth.csrf_token != "INVALID_CSRF_FOR_TEST", (
                "csrf_token should have been refreshed mid-call"
            )

    # The retry contract is: at most ONE refresh per ``rpc_call``. The
    # state machine guards this via ``refreshed_this_call`` in
    # ``_perform_authed_post``. Asserting equality (not >=) catches a
    # regression where the loop runs twice on the same call.
    assert len(refresh_calls) == 1, (
        f"refresh_auth should run exactly once for one stale-CSRF call; got {len(refresh_calls)}"
    )

    # Cassette played EXACTLY three interactions: failing POST, homepage
    # GET, retried POST. Asserting on ``play_count`` ties the test to the
    # on-wire shape of the refresh handshake — if a future refactor stops
    # issuing the homepage GET (e.g. cached token) or adds an extra round-
    # trip (e.g. a second refresh probe), this assertion fails immediately
    # rather than silently passing.
    assert cassette.play_count == 3, (
        f"Expected exactly 3 cassette interactions to play "
        f"(failing POST, homepage GET, retried POST); got {cassette.play_count}"
    )

    # ``notebooks.list()`` returns a list (possibly empty). The exact
    # contents come from the recorded response and are not the subject
    # of this test — what matters is that the call returned at all
    # instead of raising the original 400.
    assert isinstance(notebooks, list)
