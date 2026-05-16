"""Error-path VCR cassettes via the synthetic-error plumbing.

SYNTHETIC error response — validates client exception mapping, not real Google
error shapes. The cassettes in this module carry the canonical synthetic-error
shapes from :mod:`tests.cassette_patterns.build_synthetic_error_response`
(the plumbing landed in PR #638): minimal JSON bodies whose ONLY purpose is
to drive the client's exception-mapping branches in
``src/notebooklm/_core.py`` — status code + a stub body, NOT Google's actual
error response semantics.

Three modes are exercised here:

- ``error_synthetic_429_rate_limit.yaml``  → :class:`RateLimitError` after the
  retry budget is exhausted.
- ``error_synthetic_500_server.yaml``      → :class:`ServerError`   after the
  retry budget is exhausted.
- ``error_synthetic_stale_csrf.yaml``      → triggers ``refresh_auth`` once,
  then the second 400 surfaces as :class:`ClientError` (we replay TWO failing
  POSTs from the cassette to capture the refresh-retry sequence end-to-end;
  the assertion is that the refresh path FIRED, not that it succeeded).

Why we don't validate Google's real error shapes here
-----------------------------------------------------
The cassettes are SYNTHETIC — the bodies and headers are constructed from
``build_synthetic_error_response(mode)``, not captured from a live Google
response. They prove that the client's exception-mapping code raises the right
type for each transport-layer status code, NOT that those status codes are
shaped exactly like what Google emits in production. If you need to validate
a real-world error shape (e.g. a quota-exhaustion 429 with a real
``Retry-After`` header and Google-flavored body), record it live instead.

Recording note (maintainers)
----------------------------
The synthetic-error transport wrapper in ``_core.py`` substitutes the response BEFORE
httpcore is reached — and VCR's record hook patches ``httpcore.
AsyncConnectionPool.handle_async_request``, so the wrapper ALSO bypasses the
record hook. As a consequence these cassettes are hand-written from the
canonical synthetic shapes in ``tests/cassette_patterns.py`` rather than
captured by running the tests under ``NOTEBOOKLM_VCR_RECORD=1``. The replay
path is unaffected — VCR returns the cassette's synthetic response to the
client's httpx pipeline normally, and the exception-mapping branches fire as
they would for a real upstream error.

The ``@pytest.mark.synthetic_error("<mode>")`` marker is intentionally NOT
used here: it would activate the transport wrapper during replay too,
short-circuiting VCR and making the cassette decorative. Leaving the env var
unset lets VCR's cassette drive the response, which is the behavior we want
the replay tests to exercise.

See ``docs/development.md`` (section "Synthetic error cassettes") and
``tests/cassette_patterns.py:build_synthetic_error_response`` for the canonical
synthetic shapes these cassettes carry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add tests directory to sys.path for vcr_config + conftest helpers, mirroring
# the pattern in ``test_auth_refresh_vcr.py``.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.auth import AuthTokens  # noqa: E402
from notebooklm.exceptions import (  # noqa: E402
    ClientError,
    RateLimitError,
    ServerError,
)
from vcr_config import notebooklm_vcr  # noqa: E402

# All tests in this module are VCR-tier. Skipped when cassettes are absent and
# we're not in record mode (``NOTEBOOKLM_VCR_RECORD=1``).
pytestmark = [pytest.mark.vcr, skip_no_cassettes]


def _synthetic_auth() -> AuthTokens:
    """Mock ``AuthTokens`` for the cassette-driven replay path.

    The cassette responses are synthetic, so the request never reaches a real
    server — the cookie / CSRF / session values are never validated. Mock
    values are sufficient and let these tests run in CI without any auth
    fixture. Mirrors the pattern in ``test_auth_refresh_vcr.py``.
    """
    return AuthTokens(
        cookies={
            "SID": "vcr_mock_sid",
            "HSID": "vcr_mock_hsid",
            "SSID": "vcr_mock_ssid",
            "APISID": "vcr_mock_apisid",
            "SAPISID": "vcr_mock_sapisid",
        },
        csrf_token="vcr_mock_csrf",
        session_id="vcr_mock_session",
    )


class TestErrorPaths:
    """Replay synthetic-error cassettes and assert client exception mapping."""

    @pytest.mark.asyncio
    async def test_429_rate_limit(self) -> None:
        """SYNTHETIC error response — validates client exception mapping, not real Google error shapes.

        Replays ``error_synthetic_429_rate_limit.yaml``: a single batchexecute
        POST returns HTTP 429 with a ``Retry-After: 1`` header and a minimal
        ``{"error": {"code": 429, ...}}`` body. With the rate-limit retry
        budget set to 0 on the client core, the first 429 surfaces directly
        as :class:`RateLimitError` (the documented mapping in
        ``_core.py::_TransportRateLimited`` → ``rpc_call`` exception handler).
        """
        client = NotebookLMClient(_synthetic_auth())
        # Disable rate-limit retries so the single synthetic 429 in the
        # cassette surfaces immediately as RateLimitError. The cassette only
        # has ONE interaction; with the default retry budget the client would
        # ask for a second cassette response that doesn't exist and VCR would
        # raise ``CannotOverwriteExistingCassetteException``.
        client._core._rate_limit_max_retries = 0

        with notebooklm_vcr.use_cassette("error_synthetic_429_rate_limit.yaml") as cassette:
            async with client:
                with pytest.raises(RateLimitError) as exc_info:
                    await client.notebooks.list()

        # The exception carries the method id and parsed ``Retry-After`` value
        # — both pieces of context the client surfaces to callers (see the
        # ``RateLimitError`` constructor in ``src/notebooklm/exceptions.py``).
        # Asserting both makes a regression where the mapping drops one of
        # them fail loudly instead of silently degrading the error message.
        assert exc_info.value.method_id is not None
        assert exc_info.value.retry_after == 1

        # Cassette played EXACTLY one interaction: the failing POST. Asserting
        # equality (not >=) is what catches a regression where the disabled
        # retry budget silently re-enables itself and the client double-asks
        # for the same cassette entry.
        assert cassette.play_count == 1, (
            f"Expected exactly 1 cassette interaction to play; got {cassette.play_count}"
        )

    @pytest.mark.asyncio
    async def test_5xx_server_error(self) -> None:
        """SYNTHETIC error response — validates client exception mapping, not real Google error shapes.

        Replays ``error_synthetic_500_server.yaml``: a single batchexecute POST
        returns HTTP 500 with a minimal ``{"error": {"code": 500, ...}}`` body.
        With the server-error retry budget set to 0 on the client core, the
        first 500 surfaces as :class:`ServerError` via the
        ``_TransportServerError`` → ``_raise_rpc_error_from_http_status`` chain
        in ``_core.py``.
        """
        client = NotebookLMClient(_synthetic_auth())
        # Disable 5xx retries so the single synthetic 500 in the cassette
        # surfaces immediately as ServerError. The retry-loop wiring itself
        # is exercised separately by the unit tests in
        # ``test_rate_limit_retry.py`` — here we focus on the terminal
        # exception-mapping branch.
        client._core._server_error_max_retries = 0

        with notebooklm_vcr.use_cassette("error_synthetic_500_server.yaml") as cassette:
            async with client:
                with pytest.raises(ServerError) as exc_info:
                    await client.notebooks.list()

        assert exc_info.value.status_code == 500
        assert exc_info.value.method_id is not None

        # Cassette played EXACTLY one interaction (the failing POST). Catches
        # a regression where disabled retry budget silently re-enables itself.
        assert cassette.play_count == 1, (
            f"Expected exactly 1 cassette interaction to play; got {cassette.play_count}"
        )

    @pytest.mark.asyncio
    async def test_expired_csrf_triggers_refresh(self) -> None:
        """SYNTHETIC error response — validates client exception mapping, not real Google error shapes.

        Replays ``error_synthetic_stale_csrf.yaml``: the first batchexecute
        POST returns HTTP 400 (NotebookLM's documented stale-CSRF response —
        see :func:`notebooklm._core.is_auth_error`); the client's auth-refresh
        branch fires once via the stub callback installed below; the second
        cassette interaction returns the same synthetic 400, which surfaces
        as :class:`ClientError` via the standard 4xx mapping in
        ``_raise_rpc_error_from_http_status``.

        The behavior under test is the REFRESH-PATH WIRING — that
        ``refresh_auth`` ran exactly once before the second 400 ended the
        attempt. The exact post-refresh exception type is incidental
        (``ClientError`` because 400 is not 401/403, 5xx, or 429); what
        matters is that the auth-refresh hook fired, observed via a spy
        installed on ``_core._refresh_callback`` and corroborated by the
        ``play_count == 2`` assertion on the cassette.
        """
        client = NotebookLMClient(_synthetic_auth())
        # Eliminate the post-refresh retry delay so the test runs fast under
        # replay (mirrors ``test_auth_refresh_vcr.py``).
        client._core._refresh_retry_delay = 0

        # In-process refresh callback that issues NO HTTP traffic. This is
        # what lets the cassette capture only the TWO synthetic batchexecute
        # interactions (failing POST → retried POST) without a homepage GET
        # leg. The production ``refresh_auth`` re-extracts ``SNlM0e`` /
        # ``FdrFJe`` from the homepage; the unit-style
        # ``test_auth_refresh_vcr.py`` exercises that full three-leg flow.
        refresh_calls: list[object] = []

        async def stub_refresh() -> AuthTokens:
            refresh_calls.append(None)
            # Mutate the in-memory CSRF token to simulate a successful refresh.
            # The retry loop in ``_perform_authed_post`` rebuilds the request
            # body from the live ``self.auth.csrf_token`` after refresh, so
            # this mutation is observable on the wire — and the cassette's
            # request-side body would carry the refreshed value if VCR
            # matched on body (it doesn't; the default matcher uses
            # method/path/rpcids).
            client._core.auth.csrf_token = "refreshed_csrf_token"
            client._core.update_auth_headers()
            return client._core.auth

        client._core._refresh_callback = stub_refresh

        with notebooklm_vcr.use_cassette("error_synthetic_stale_csrf.yaml") as cassette:
            async with client:
                # The first cassette interaction returns synthetic 400 →
                # auth-refresh fires → second cassette interaction returns
                # synthetic 400 → ClientError (4xx that isn't 401/403/429/5xx).
                with pytest.raises(ClientError) as exc_info:
                    await client.notebooks.list()

        assert exc_info.value.status_code == 400
        assert exc_info.value.method_id is not None

        # The auth-refresh branch fired exactly once — this is the
        # load-bearing assertion of this test. Asserting equality (not >=)
        # catches a regression where ``refreshed_this_call`` fails to flip and
        # the refresh path runs twice for one user call.
        assert len(refresh_calls) == 1, (
            f"refresh_auth should run exactly once for one stale-CSRF call; "
            f"got {len(refresh_calls)}"
        )

        # Cassette played EXACTLY two interactions: the failing POST and the
        # retried POST. No homepage GET because the stub refresh callback
        # avoids HTTP traffic. This shape assertion ties the test to the
        # cassette's on-wire structure: if a refactor accidentally adds an
        # extra round-trip (e.g. a second refresh probe) or drops the retry,
        # this assertion fails immediately rather than silently passing.
        assert cassette.play_count == 2, (
            f"Expected exactly 2 cassette interactions to play "
            f"(failing POST, retried POST); got {cassette.play_count}"
        )
