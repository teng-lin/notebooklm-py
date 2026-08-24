"""Atomic ``(csrf, sid, cookies)`` generations during provider refresh.

P8 separates the mutable provider acquisition session from the backend-private
HTTP session. A refresh may mutate the provider cookie jar before it finishes
extracting tokens, but requests must continue to receive the previous cached
immutable generation until the whole provider transaction publishes. The
backend terminal then clones that one generation synchronously before POST, so
its URL/body route and cookies cannot come from different generations.

This test deliberately pauses a provider refresh after its cookie mutation but
before its token mutation. Half the RPC fan-out runs in that torn mutable
window and must still send generation 1. After the transaction publishes, the
other half must send generation 2. Every captured request must be internally
coherent, and both committed generations must reach the wire.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Iterator

import httpx
import pytest

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test

# Mock-only test (no real HTTP, no cassette) — opt out of the
# integration-tree enforcement hook in ``tests/integration/conftest.py``.
pytestmark = pytest.mark.allow_no_vcr

# -- Generation tagging -----------------------------------------------------
#
# Each "generation" of credentials is a monotonic integer N. We encode N
# into all three axes simultaneously:
#   csrf_token  = f"CSRF_{N}"            (goes into request body via f.req)
#   session_id  = f"SID_{N}"             (goes into URL via f.sid=)
#   cookies     = SID=sid_cookie_{N}     (goes into Cookie: header)
#
# When the test asserts coherence, it extracts the N from each axis and
# requires all three to be equal per captured request.
RPC_METHOD = RPCMethod.LIST_NOTEBOOKS
RPC_METHOD_ID = RPC_METHOD.value


def _synthetic_rpc_response_text(rpc_id: str = RPC_METHOD_ID) -> str:
    """Minimal valid batchexecute response that decodes to ``[]``."""
    inner = json.dumps([])
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _gen_counter() -> Iterator[int]:
    i = 0
    while True:
        i += 1
        yield i


def _extract_csrf_gen(body: bytes) -> int:
    """Extract generation N from ``CSRF_N`` embedded in the request body."""
    text = body.decode("utf-8", errors="replace")
    # The body is URL-encoded form data; ``at=CSRF_N`` lives in there.
    decoded = urllib.parse.unquote_plus(text)
    m = re.search(r"CSRF_(\d+)", decoded)
    assert m is not None, f"Could not locate CSRF tag in body: {text!r}"
    return int(m.group(1))


def _extract_sid_gen(url: str) -> int:
    """Extract generation N from ``f.sid=SID_N`` in the URL query."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    sid_values = qs.get("f.sid", [])
    assert sid_values, f"Could not locate f.sid in URL: {url!r}"
    m = re.match(r"SID_(\d+)", sid_values[0])
    assert m is not None, f"Could not parse SID tag from f.sid={sid_values[0]!r}"
    return int(m.group(1))


def _extract_cookie_gen(cookie_header: str) -> int:
    """Extract generation N from ``SID=sid_cookie_N`` in the Cookie header."""
    m = re.search(r"sid_cookie_(\d+)", cookie_header)
    assert m is not None, f"Could not locate sid_cookie tag in Cookie: {cookie_header!r}"
    return int(m.group(1))


@pytest.mark.asyncio
async def test_concurrent_refresh_does_not_tear_auth_triple_across_fan_out():
    """Fan out RPCs across an in-progress provider transaction without tears."""
    fan_out = 50

    captured: list[httpx.Request] = []

    gen_iter = _gen_counter()
    current_gen = next(gen_iter)  # Start in generation 1.

    async def handler(request: httpx.Request) -> httpx.Response:
        # Yield once after capture so the event loop can interleave the
        # refresh task against pending RPCs. The yield
        # lands AFTER the request was constructed (httpx merged the
        # cookies and wrote the URL / body before the transport handler
        # runs), so the captured request IS what crossed the wire.
        if request.method == "POST":
            captured.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, text=_synthetic_rpc_response_text())
        return httpx.Response(500, text="unexpected GET")

    transport = httpx.MockTransport(handler)

    auth = AuthTokens(
        csrf_token=f"CSRF_{current_gen}",
        session_id=f"SID_{current_gen}",
        cookies={("SID", ".google.com"): f"sid_cookie_{current_gen}"},
    )

    # No refresh callback is needed: the harness drives the provider's public
    # whole-transaction boundary directly and pauses inside its work callback.
    core = NotebookLMClient(auth=auth)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def bump_generation_through_provider() -> None:
        """Pause between provider cookie mutation and token publication."""
        nonlocal current_gen
        new_gen = next(gen_iter)

        async def work() -> AuthTokens:
            nonlocal current_gen
            provider_client = core._provider._kernel.get_http_client()
            provider_client.cookies.set("SID", f"sid_cookie_{new_gen}", domain=".google.com")
            refresh_started.set()
            await release_refresh.wait()
            core.auth.csrf_token = f"CSRF_{new_gen}"
            core.auth.session_id = f"SID_{new_gen}"
            core.auth.cookies = {("SID", ".google.com"): f"sid_cookie_{new_gen}"}
            current_gen = new_gen
            return core.auth

        await core._provider.run_refresh_transaction(work)

    await core.__aenter__()
    try:
        # Replace the auto-built client with one using our MockTransport so
        # we can observe outgoing requests post-cookie-merge.
        prior_cookies = core._backend._kernel.get_http_client().cookies
        await core._backend._kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._backend._kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )

        async def one_rpc() -> None:
            await core._backend._runtime.rpc_call(RPC_METHOD, [])

        refresh = asyncio.create_task(bump_generation_through_provider())
        await refresh_started.wait()
        try:
            # The provider's mutable jar is generation 2 while its cached
            # immutable commit remains generation 1.
            await asyncio.gather(*(one_rpc() for _ in range(fan_out // 2)))
            release_refresh.set()
            await refresh
            await asyncio.gather(*(one_rpc() for _ in range(fan_out // 2)))
        finally:
            release_refresh.set()
            await asyncio.gather(refresh, return_exceptions=True)
    finally:
        await core.close()

    # Assertion: every captured request must be coherent across all
    # three axes. Mixed generations (e.g. csrf=1, sid=2, cookies=1)
    # indicate a torn read — the exact regression the snapshot lock prevents.
    assert len(captured) == fan_out, f"Expected {fan_out} POSTs captured, got {len(captured)}"
    torn = []
    for i, req in enumerate(captured):
        url = str(req.url)
        body = bytes(req.content)
        cookie_header = req.headers.get("cookie", "")
        try:
            csrf_gen = _extract_csrf_gen(body)
            sid_gen = _extract_sid_gen(url)
            cookie_gen = _extract_cookie_gen(cookie_header)
        except AssertionError as exc:
            torn.append((i, f"extract-failed: {exc}"))
            continue
        if not (csrf_gen == sid_gen == cookie_gen):
            torn.append(
                (
                    i,
                    f"torn: csrf={csrf_gen}, sid={sid_gen}, cookies={cookie_gen}",
                )
            )

    assert not torn, (
        f"{len(torn)}/{len(captured)} requests carried mixed-generation auth state. "
        f"Sample: {torn[:5]}. This indicates the (csrf, sid, cookies) triple is no "
        f"longer atomic under refresh — check provider transaction publication "
        f"and backend generation installation."
    )

    # Both committed generations must reach the wire; otherwise the paused
    # refresh window or post-publication half of the test was vacuous.
    assert current_gen == 2, (
        f"Refresh coroutine did not complete: current_gen={current_gen}, expected 2."
    )
    gens_observed = sorted({_extract_csrf_gen(bytes(r.content)) for r in captured})
    assert gens_observed == [1, 2]
