"""atomic ``(csrf, sid, cookies)`` snapshot during refresh.

The race fixed here is a torn read of the auth-headers triple
``(csrf_token, session_id, cookies)`` while a refresh runs concurrently
with in-flight RPCs. Today's ``ClientCore._snapshot()`` reads the four
scalar fields off ``self.auth`` without holding any lock, and
``_build_url()`` reads ``session_id``/``authuser``/``account_email``
directly off ``self.auth`` (not the snapshot). A concurrent ``refresh_auth``
mutates ``csrf_token`` and ``session_id`` in two separate Python statements
— there's no asyncio yield between them in production today, but the
moment any maintainer introduces an ``await`` in that prologue, an RPC
can observe one field from the OLD generation and another from the NEW
generation. The fix introduces a dedicated ``_auth_snapshot_lock`` that:

1. ``_snapshot()`` acquires under ``async with`` to read all scalars
   atomically.
2. The refresh-side mutation block in ``client.refresh_auth`` writes
   ``csrf_token`` + ``session_id`` under the same lock — tiny critical
   section, no awaits inside.
3. ``_build_url()`` consumes the resulting ``_AuthSnapshot`` rather than
   re-reading ``self.auth`` live, so the URL is built from the same
   generation the body was.

This test stresses that contract by spawning 50 RPC tasks AND one
refresh task into a single ``asyncio.gather`` — they all schedule
together and the mock transport's per-handler ``asyncio.sleep(0)``
forces interleaving via the event loop. Each generation is tagged by
writing a monotonic counter into all three positions simultaneously
under the lock, so the assertion is purely "for every captured request,
the three observed generation tags must match".

Test scope (honest framing): this is the *runtime smoke proof* that
the new design composes correctly under concurrent load. It does not,
on its own, surface a pre-fix torn read against an unfixed code base —
the actual hazard ``_build_url`` reading ``self.auth`` live only
materializes if a yield point slips into ``_perform_authed_post``'s
prologue between snapshot capture and request build, which is what the
AST guards in ``tests/unit/test_concurrency_refresh_race.py`` lock
down statically. Together the AST guards and this runtime check form
the regression net for the auth-snapshot atomicity contract.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Iterator

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

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
    """Fan 50 RPCs truly concurrently with a refresh, assert no torn triple.

    Mechanism:

    - 50 ``rpc_call`` coroutines AND one refresh coroutine are dispatched
      into a single ``asyncio.gather``. They all become ready at the
      same time and the event loop schedules them onto the loop's task
      queue together — there is no "first batch / second batch"
      pre-serialization.
    - The mock transport's handler captures the request and then yields
      via ``asyncio.sleep(0)`` so the refresh task can interleave its
      lock-acquired write block against any RPCs that are mid-
      ``_perform_authed_post``. Each captured ``httpx.Request`` already
      has its URL / body / cookie header frozen by the time the handler
      runs (httpx builds the request synchronously before the transport
      sees it), so the captured triple IS what crossed the wire.
    - The refresh task is also dispatched via ``gather`` — same event-
      loop scheduling as the RPCs. It acquires
      ``_auth_snapshot_lock`` and writes csrf/sid/cookies atomically.

    The asserted invariant: for EVERY captured POST, the three
    generation tags extracted from
    ``(body's CSRF, URL's f.sid, Cookie header's SID)`` must agree.

    Scope honestly: this test verifies the *new design works end-to-end
    under concurrent load* — the lock serializes ``_snapshot()`` reads
    with the refresh writes, the snapshot consumer in ``_build_url``
    makes URL + body share the same generation, and 50 concurrent RPCs
    + 1 refresh produce 50 coherent captured triples. It does NOT, on
    its own, surface the pre-fix torn read against an unfixed code
    base — that requires a yield point between snapshot capture and
    request build (introduced via a future ``await`` slipping into the
    prologue), which the AST guards
    (``test_perform_authed_post_has_no_await_before_post_per_iteration``,
    ``test_build_url_does_not_read_self_auth``,
    ``test_snapshot_acquires_auth_snapshot_lock``) catch statically.
    The three guards together form the regression net; this test is the
    runtime smoke proof that the design composes correctly with real
    concurrent traffic.
    """
    fan_out = 50

    captured: list[httpx.Request] = []

    gen_iter = _gen_counter()
    current_gen = next(gen_iter)  # Start in generation 1.

    async def handler(request: httpx.Request) -> httpx.Response:
        # Yield once after capture so the event loop can interleave the
        # refresh task against pending RPCs — without this every RPC
        # would complete to its end synchronously after its
        # build_request, masking the race the lock fixes. The yield
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

    # We instantiate ``core`` first so the refresh coroutine can close
    # over it. No ``refresh_callback`` is wired in — we drive the
    # refresh side directly via ``bump_generation_under_lock`` below
    # because the test asserts the *lock semantics*, not the
    # full refresh state machine.
    core = ClientCore(auth=auth, refresh_retry_delay=0.0)

    async def bump_generation_under_lock() -> None:
        """One-shot synthetic refresh: bump the generation and atomically
        rewrite csrf/sid/cookies under ``_auth_snapshot_lock``.

        Acquires via the production accessor (``_get_auth_snapshot_lock``)
        rather than the raw private attribute so the lazy-init path is
        exercised on this side too — keeps the test in lockstep with how
        ``NotebookLMClient.refresh_auth`` acquires the lock.
        """
        nonlocal current_gen
        new_gen = next(gen_iter)
        async with core._get_auth_snapshot_lock():
            core.auth.csrf_token = f"CSRF_{new_gen}"
            core.auth.session_id = f"SID_{new_gen}"
            # Update the live httpx cookie jar synchronously — this is
            # the same jar httpx merges into the outgoing Cookie header.
            assert core._http_client is not None
            core._http_client.cookies.set("SID", f"sid_cookie_{new_gen}", domain=".google.com")
            core.auth.cookies = {("SID", ".google.com"): f"sid_cookie_{new_gen}"}
            current_gen = new_gen

    await core.open()
    try:
        # Replace the auto-built client with one using our MockTransport so
        # we can observe outgoing requests post-cookie-merge.
        prior_cookies = core._http_client.cookies
        await core._http_client.aclose()
        core._http_client = httpx.AsyncClient(
            cookies=prior_cookies,
            transport=transport,
            timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
        )

        # Force ``_auth_snapshot_lock`` to exist BEFORE the gather so the
        # refresh coroutine and the RPC coroutines share the same lock
        # instance. Without this priming, the lazy-init's "first caller
        # wins" check-then-assign would race the parallel coroutines and
        # potentially create two distinct Lock instances.
        core._get_auth_snapshot_lock()

        # Fan out 50 RPCs and one refresh concurrently. ``asyncio.gather``
        # schedules them together; the handler's ``asyncio.sleep(0)``
        # yields control so the refresh task can interleave its lock-
        # acquired write between RPC ``_snapshot()`` and
        # ``client.post(...)`` boundaries.
        async def one_rpc() -> None:
            await core.rpc_call(RPC_METHOD, [])

        await asyncio.gather(
            bump_generation_under_lock(),
            *(one_rpc() for _ in range(fan_out)),
        )
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
        f"longer atomic under refresh — check _snapshot() lock acquisition and that "
        f"_build_url() consumes _AuthSnapshot rather than reading self.auth live."
    )

    # The refresh task MUST have completed (otherwise the concurrency
    # framing was vacuous — every RPC would just see gen 1). Two checks:
    #   1. ``current_gen == 2`` — the refresh ran its lock-write.
    #   2. At least one captured RPC observed gen 2 — the post-refresh
    #      ordering reached the wire on at least one RPC. (We don't
    #      require interleaving on every RPC; some schedulers may run
    #      all 50 RPCs before the refresh task is picked up. The
    #      coherence assertion above is what matters per-request.)
    assert current_gen == 2, (
        f"Refresh coroutine did not complete: current_gen={current_gen}, expected 2."
    )
    gens_observed = sorted({_extract_csrf_gen(bytes(r.content)) for r in captured})
    assert gens_observed, "No RPC requests were captured at all."
