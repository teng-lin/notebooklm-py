"""Bridge legacy ``http_client.post`` mocks to the streaming POST API.

The RPC POST path now uses :meth:`httpx.AsyncClient.stream` so the
running size guard in :data:`notebooklm._core_transport.MAX_RPC_RESPONSE_BYTES`
can fire. Many integration tests in this tree predate that switch and
still express intent as ``client._http_client.post = fake_post`` or
``patch.object(..., "post", side_effect=err)``. This module exposes the
same ``install_post_as_stream`` helper as ``tests/unit/conftest.py`` so
both test trees can adapt legacy mocks without rewriting each call site.

Why a sibling module instead of putting this in ``conftest.py``: the
``tests/integration/concurrency/`` subdirectory has its own ``conftest.py``,
and ``from conftest import ...`` from a test in that subdir resolves to
the closer conftest. Pytest adds ``tests/integration/`` to ``sys.path``
when collecting any of its tests (because ``conftest.py`` lives here),
so ``from _streaming_mock import install_post_as_stream`` works from
both ``tests/integration/`` and ``tests/integration/concurrency/`` without
``sys.path`` hackery.
"""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest


def install_post_as_stream(
    monkeypatch: pytest.MonkeyPatch | None,
    http_client: Any,
    fake_post: Callable[..., Awaitable[Any]],
) -> None:
    """Adapt a ``fake_post(...) -> Response`` mock to the streaming API.

    Installs an ``async with client.stream(...)``-compatible fake on
    ``http_client.stream`` that delegates to ``fake_post``, preserving
    call-count side effects, raised exceptions, and returned responses.

    For ``MagicMock`` responses lacking real ``aiter_bytes`` plumbing,
    the helper re-wraps them into a real :class:`httpx.Response` carrying
    the canned text so the streaming wrapper's ``aiter_bytes`` + rebuild
    path works on it. Real :class:`httpx.Response` instances are passed
    through unchanged.
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
        # wrapper's ``aiter_bytes`` + rebuild path works on it.
        text = getattr(response, "text", "")
        payload = text.encode("utf-8") if isinstance(text, str) else bytes(text or b"")
        raw_status = getattr(response, "status_code", 200)
        # MagicMock auto-mocks attributes, so ``status_code`` might be a Mock
        # whose ``__int__`` returns 1. Only treat real ints as set; otherwise
        # default to 200 (the canonical "no error" status the success-path
        # tests are implicitly asserting against).
        status = raw_status if isinstance(raw_status, int) else 200
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
        http_client.stream = fake_stream
