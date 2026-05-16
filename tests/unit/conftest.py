"""Shared fixtures and helpers for tests/unit/.

The ``make_core`` async context manager is imported directly by sibling
test modules (e.g. ``from conftest import make_core``) — pytest adds the
test directory to ``sys.path`` so the sibling import works.
"""

from contextlib import asynccontextmanager

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens


@pytest.fixture
def auth_tokens():
    """Create test authentication tokens for unit tests.

    Overrides the root-level fixture (single-cookie) with the full required
    cookie set so httpx_mock-based tests previously living in
    ``tests/integration/`` (later moved to ``tests/unit/``) can keep
    asserting on per-cookie wire values (e.g. ``SID=test_sid``,
    ``HSID=test_hsid``) without modification. The root fixture remains the
    canonical minimal jar for tests that don't inspect cookie headers.
    """
    return AuthTokens(
        cookies={
            "SID": "test_sid",
            "HSID": "test_hsid",
            "SSID": "test_ssid",
            "APISID": "test_apisid",
            "SAPISID": "test_sapisid",
        },
        csrf_token="test_csrf_token",
        session_id="test_session_id",
    )


@asynccontextmanager
async def make_core(refresh_callback=None, transport=None, refresh_retry_delay=0.0):
    """Yield an opened ClientCore with optional mock transport; close cleanly.

    Args:
        refresh_callback: async callable returning ``AuthTokens`` (or raising)
            for use by ``_try_refresh_and_retry``. ``None`` skips refresh setup.
        transport: optional ``httpx.MockTransport`` so tests can observe the
            real ``httpx.Request`` after cookie merge.
        refresh_retry_delay: shortened in tests (default 0.0) to keep the
            suite fast — production default is 0.2s.
    """
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "old_sid_cookie"},
    )
    core = ClientCore(
        auth=auth,
        refresh_callback=refresh_callback,
        refresh_retry_delay=refresh_retry_delay,
    )
    await core.open()
    if transport is not None:
        # Replace the auto-built client with one that uses our transport so we
        # can observe real httpx.Request construction (cookie merge, headers).
        # Capture the cookie jar BEFORE aclose() — reading attributes off a
        # closed AsyncClient is brittle across httpx versions.
        prior_cookies = core._http_client.cookies
        await core._http_client.aclose()
        core._http_client = httpx.AsyncClient(
            cookies=prior_cookies,
            transport=transport,
            timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
        )
    try:
        yield core
    finally:
        await core.close()
