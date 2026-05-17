"""Shared fixtures and helpers for tests/unit/.

The ``make_core`` async context manager is imported directly by sibling
test modules (e.g. ``from conftest import make_core``) — pytest adds the
test directory to ``sys.path`` so the sibling import works.
"""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens


def install_post_as_stream(
    monkeypatch: pytest.MonkeyPatch | None,
    http_client: Any,
    fake_post: Callable[..., Awaitable[Any]],
) -> None:
    """Adapt a ``fake_post(...) -> Response`` mock to the streaming API.

    The RPC POST path uses :meth:`httpx.AsyncClient.stream` (so a running
    size guard can enforce :data:`notebooklm._core_transport.MAX_RPC_RESPONSE_BYTES`).
    The bulk of the unit suite predates that switch and still expresses test
    intent as ``monkeypatch.setattr(client, "post", fake_post)``. This helper
    bridges the gap: it installs an ``async with client.stream(...)``-compatible
    fake on ``http_client.stream`` that delegates to the caller's existing
    ``fake_post`` (preserving call-count side effects, raised exceptions, and
    returned responses).

    For ``MagicMock`` responses lacking real ``aiter_bytes`` plumbing, the
    helper attaches a single-chunk async iterator over the mock's ``.text`` so
    the streaming wrapper's size-guard read loop terminates immediately.
    Real :class:`httpx.Response` instances are passed through unchanged —
    their built-in ``aiter_bytes`` works on already-buffered bodies.
    """

    @asynccontextmanager
    async def fake_stream(method: str, url: str, **kwargs: Any) -> Any:
        # ``fake_post`` historically takes ``(url, **kwargs)`` — match that
        # call site exactly so existing argument-introspection in tests keeps
        # working unchanged.
        response = await fake_post(url, **kwargs)
        # ``type(...) is`` — not ``isinstance(...)`` — because ``MagicMock(
        # spec=httpx.Response)`` passes the isinstance check, which would
        # leave the streaming wrapper trying to read ``response.headers`` and
        # other spec-enforced attributes the test never set, raising
        # ``AttributeError`` deep inside production code instead of going
        # through the friendly rewrap branch below.
        if type(response) is httpx.Response:
            yield response
            return

        # Non-``httpx.Response`` (MagicMock-style): re-wrap into a real
        # :class:`httpx.Response` carrying the canned text so the streaming
        # wrapper's ``aiter_bytes`` + rebuild path works on it. Returning a
        # ``spec=httpx.Response`` MagicMock directly is brittle because the
        # spec rejects ad-hoc attribute access (``response.headers = {}``
        # raises AttributeError), and the streaming wrapper reads several
        # attributes (``status_code``, ``headers``, ``request``,
        # ``aiter_bytes``) the mocks rarely set.
        text = getattr(response, "text", "")
        payload = text.encode("utf-8") if isinstance(text, str) else bytes(text or b"")
        raw_status = getattr(response, "status_code", 200)
        # MagicMock auto-mocks attributes, so ``status_code`` might be a Mock
        # whose ``__int__`` returns 1. Only treat real ints as set; otherwise
        # default to 200 (the canonical "no error" status the success-path
        # tests are implicitly asserting against).
        status = raw_status if isinstance(raw_status, int) else 200
        # Preserve mock-set headers (e.g. ``retry-after``) so 429-path
        # ``exc.response.headers.get(...)`` introspection still returns the
        # value the test pinned. ``MagicMock(spec=...)`` raises AttributeError
        # for spec-defined names the test didn't explicitly set, so catch
        # that too — not just the missing-default case ``getattr`` already
        # handles.
        try:
            raw_headers = getattr(response, "headers", None)
        except AttributeError:
            raw_headers = None
        try:
            headers = dict(raw_headers) if raw_headers else None
        except (TypeError, AttributeError):
            headers = None
        wrapped = httpx.Response(
            status_code=status,
            headers=headers,
            content=payload,
            request=httpx.Request("POST", url),
        )
        yield wrapped

    if monkeypatch is not None:
        monkeypatch.setattr(http_client, "stream", fake_stream)
    else:
        # MagicMock-style tests assign attributes directly rather than going
        # through ``monkeypatch.setattr``; honor that pattern for them.
        http_client.stream = fake_stream


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
