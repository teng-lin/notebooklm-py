"""Regression test for the close-cancellation transport-leak shield.

The audit covered whether the ``asyncio.shield`` wrapped around
``self._http_client.aclose()`` inside :meth:`notebooklm._core.ClientCore.close`
correctly survives a cancellation that lands while ``aclose`` itself is
in flight, exercised through the user-facing ``__aexit__`` surface (not
the bare ``close()`` task path already covered by the companion
``test_cancel_mid_close_does_not_leak_transport``).

The shield itself ships earlier (PR #526, sha ``d8b5bd6``). The
follow-up acceptance criterion was a complementary repro that exercises
a different cancel-injection site:

- Client opened with ``keepalive=...`` so a background poke task is alive
  and the close sequence has to drive a real keepalive teardown.
- ``_rotate_cookies`` monkeypatched to hang on an unset ``asyncio.Event``
  so the keepalive task is genuinely parked when close starts cancelling
  it. CancelledError is intentionally NOT trapped — the keepalive task
  must remain cancellable so that ``_core.close()`` can tear it down and
  reach the shielded ``aclose`` block.
- The httpx client's ``aclose`` is also monkeypatched to insert a short
  ``await asyncio.sleep(0.2)`` so the close path doesn't run to
  completion in microseconds; otherwise the outer ``wait_for`` timeout
  would never fire and the cancel-during-aclose path the shield
  protects would never be exercised.
- ``__aexit__`` driven through :func:`asyncio.wait_for(timeout=0.1)` so
  the outer cancel reliably arrives while the slowed ``aclose`` is in
  flight — i.e. inside the shielded await.
- We hold a reference to ``client._core._http_client`` captured before
  the cancel (close nulls the attribute on success) and assert
  ``http_client_ref.is_closed`` is true afterwards — proof that the
  shielded ``aclose`` in the outer ``finally`` ran to completion.

The shield satisfies this invariant; the follow-up source change was
intentionally a no-op (the audit's job was to confirm the shield is
positioned correctly, which it is). This test is the regression
artifact — verified to fail loudly when the shield is removed (the
cancelled-mid-``aclose`` path then leaves ``is_closed=False``).
"""

from __future__ import annotations

import asyncio
import re

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens

ROTATE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")


@pytest.fixture
def keepalive_auth() -> AuthTokens:
    """``AuthTokens`` without a storage path — keepalive in memory only.

    Skipping ``storage_path`` keeps the test free of tmp-path fixtures and
    lets ``save_cookies`` short-circuit on the ``_keepalive_storage_path
    is None`` branch.
    """
    return AuthTokens(
        cookies={
            "SID": "test_sid",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test_hsid",
        },
        csrf_token="test_csrf",
        session_id="test_session",
    )


@pytest.mark.asyncio
@pytest.mark.no_default_keepalive_mock
async def test_close_during_keepalive_cancel_does_not_leak_transport(
    keepalive_auth: AuthTokens,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``__aexit__`` cancelled mid-``aclose`` must still close the transport.

    Repro setup:
    - keepalive enabled (background poke task alive),
    - ``_rotate_cookies`` patched to hang on an unset ``asyncio.Event``
      so the keepalive teardown path is non-trivial,
    - ``aclose`` patched to insert a 0.2 s sleep so the cancel from
      ``wait_for`` reliably lands while we're inside the shielded
      block,
    - ``__aexit__`` wrapped in ``wait_for(timeout=0.1)`` so the cancel
      fires during ``aclose``.

    The shield in :meth:`ClientCore.close` wraps
    ``self._http_client.aclose()`` in ``asyncio.shield`` inside an
    outer ``finally``. Without that shield, a cancel arriving inside
    ``aclose`` aborts the close and leaks the httpx transport. With
    it, the captured ``http_client_ref.is_closed`` must read ``True``
    once the shielded Task has had a moment to finish.
    """
    # The keepalive route is allow-listed but the patched _rotate_cookies
    # below intercepts before any HTTP call goes out. The mock is here
    # only to keep ``no_default_keepalive_mock`` from making other
    # opportunistic auth pokes fail.
    httpx_mock.add_response(
        url=ROTATE_URL_RE,
        is_optional=True,
        is_reusable=True,
        status_code=204,
    )

    # The unset event the patched rotate hangs on. Never set by the
    # test — the keepalive task's only exit is the ``CancelledError``
    # that ``_core.close()`` injects via ``_keepalive_task.cancel()``.
    hang_event = asyncio.Event()
    rotate_entered = asyncio.Event()

    async def _hanging_rotate(*_args: object, **_kwargs: object) -> None:
        """Park the keepalive loop on an unset event.

        With keepalive's poke stuck here, ``_core.close()`` has to
        cancel the keepalive task and ``gather()`` it before reaching
        ``save_cookies`` and the shielded ``aclose``. The outer
        ``wait_for(timeout=0.1)`` below — combined with the patched
        ``_slow_aclose`` — injects a cancel inside the shielded
        ``aclose`` await. The shield in the outer ``finally`` must
        still drive ``aclose()`` to completion; that's what the
        assertion below proves.

        ``CancelledError`` is intentionally NOT trapped: the keepalive
        task must remain cancellable so that ``close()`` can tear it
        down. (Swallowing the cancel would just hang ``gather()``
        forever and the outer wait_for would never see ``close()``
        reach its shielded finally.)
        """
        rotate_entered.set()
        await hang_event.wait()

    monkeypatch.setattr("notebooklm._core._rotate_cookies", _hanging_rotate)

    # ``keepalive_min_interval`` clamps short intervals up to its floor
    # (default 60s). Pass ``keepalive_min_interval=0.01`` so a 0.05s
    # keepalive actually fires within the test window.
    client = NotebookLMClient(
        keepalive_auth,
        keepalive=0.05,
        keepalive_min_interval=0.01,
    )

    # Open the client and let the keepalive loop enter ``_rotate_cookies``
    # so we know the patched hang is active when we cancel.
    await client.__aenter__()
    try:
        # Save the transport ref BEFORE the cancel — successful close
        # sets ``_core._http_client = None`` (inner finally), so we'd
        # have no handle otherwise.
        http_client_ref = client._core._http_client
        assert http_client_ref is not None, "open() must have installed a transport"

        # Slow down ``aclose()`` so the outer ``wait_for(timeout=0.1)``
        # below reliably injects a ``CancelledError`` while the shielded
        # close is in flight. Without this the entire close path
        # finishes in microseconds (mock transport, no real connections
        # to drain) and the cancel never lands inside ``aclose`` — the
        # very path the shield exists to protect. The bug pre-B4 was
        # that a cancel landing here would skip aclose entirely; the
        # shield's job is to keep it running.
        original_aclose = http_client_ref.aclose
        aclose_started = asyncio.Event()

        async def _slow_aclose() -> None:
            aclose_started.set()
            # Hold long enough that ``wait_for(timeout=0.1)`` fires
            # while we're parked here, but short enough that the
            # shielded variant still completes within the test's poll
            # window below.
            await asyncio.sleep(0.2)
            await original_aclose()

        # Patching the bound method on the instance — _core.close()
        # calls ``self._http_client.aclose()`` which dispatches off
        # the instance attribute first. ``setattr`` shadows the class
        # method for this one instance.
        monkeypatch.setattr(http_client_ref, "aclose", _slow_aclose)

        # Wait for the patched rotate to be called at least once so the
        # keepalive task is parked inside the hang when we trigger close.
        try:
            await asyncio.wait_for(rotate_entered.wait(), timeout=2.0)
        except (TimeoutError, asyncio.TimeoutError):
            pytest.fail(
                "patched _rotate_cookies never entered — keepalive task did "
                "not start, repro setup is invalid"
            )

        # Drive ``__aexit__`` through a short ``wait_for`` so a cancel
        # arrives while ``_slow_aclose`` is in its 0.2 s sleep — i.e.
        # the cancel lands inside the shielded await of aclose, the
        # exact path the shield was added for.
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await asyncio.wait_for(
                client.__aexit__(None, None, None),
                timeout=0.1,  # short — cancel fires during _slow_aclose's sleep
            )

        # Confirm the cancel actually landed mid-aclose. If aclose_started
        # never fired, the cancel arrived before we reached the shielded
        # block and the test wouldn't be exercising the shield.
        assert aclose_started.is_set(), (
            "test invariant: ``aclose`` must have been entered before the "
            "outer wait_for cancel fired; otherwise the cancel didn't land "
            "inside the shielded block"
        )

        # Bounded poll: ``asyncio.shield`` raises ``CancelledError`` in
        # the outer ``wait_for`` immediately, but the inner ``aclose``
        # Task keeps running. The shield is doing its job iff the
        # transport eventually reports ``is_closed`` even though the
        # outer await was cancelled.
        for _ in range(100):  # up to ~1.0 s — generous for slow CI
            if http_client_ref.is_closed:
                break
            await asyncio.sleep(0.01)

        assert http_client_ref.is_closed, (
            "transport leaked: cancel during _slow_aclose left the httpx "
            "client open — the asyncio.shield in ClientCore.close was "
            "either removed, repositioned, or no longer wraps aclose()"
        )
    finally:
        # Release the patched hang so any still-pending keepalive task
        # can exit cleanly before pytest-asyncio's loop teardown runs.
        hang_event.set()
