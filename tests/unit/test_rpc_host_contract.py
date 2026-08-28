"""Host contract for the RPC path (#2067 precursor).

Google serves the personal app from two hosts — ``notebook.google.com`` and,
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

The rest of the module pins the third property: **every HTTP-status error names
the host**, on all of the paths a real failure can take — the mapper's 4xx/5xx
arms, its 401/403 fallback, and the retry-exhausted transport 429 that bypasses
the mapper entirely.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from notebooklm._web.params.chat_stream import build_streaming_chat_request
from notebooklm._web.transport.errors import TransportRateLimited
from notebooklm._web.transport.request_types import AuthSnapshot
from notebooklm.auth import AuthTokens
from notebooklm.rpc import (
    ClientError,
    RateLimitError,
    RPCError,
    RPCMethod,
    ServerError,
    get_batchexecute_url,
    get_query_url,
    get_upload_url,
)
from tests._helpers.client_factory import build_client_shell_for_tests

DEFAULT_HOST = "https://notebook.google.com"

REQUEST_HOST = "rpc.example.test"


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


def _status_error(
    status: int,
    *,
    url: str = f"https://{REQUEST_HOST}/_/LabsTailwindUi/data/batchexecute",
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _executor() -> Any:
    tokens = AuthTokens(cookies={"SID": "sid"}, csrf_token="CSRF", session_id="SID")
    return build_client_shell_for_tests(tokens)._rpc_executor


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (429, RateLimitError),
        (404, ClientError),
        (500, ServerError),
        # 401/403 fall through to the bare ``RPCError``. They are excluded from
        # the ClientError arm because the auth layer owns them, but a wrong-host
        # session is at least as likely to surface here as a 404 — so the
        # fallback needs the host too.
        (401, RPCError),
        (403, RPCError),
    ],
    ids=["429", "404", "500", "401", "403"],
)
def test_every_status_error_names_the_host(status: int, expected_type: type[Exception]) -> None:
    """No HTTP-status arm may drop the host, including the 401/403 fallback."""
    with pytest.raises(expected_type) as raised:
        _executor().raise_rpc_error_from_http_status(
            _status_error(status), RPCMethod.LIST_NOTEBOOKS
        )

    assert f"on {REQUEST_HOST}" in str(raised.value), (
        f"HTTP {status} lost the host from its message; a wrong-host session is "
        "then indistinguishable from a permissions failure (#2067)"
    )


def test_host_comes_from_the_failed_request_not_the_environment(monkeypatch) -> None:
    """The message names the host that failed, not whatever the env says *now*.

    Endpoint builders resolve ``NOTEBOOKLM_BASE_URL`` lazily on every call, so a
    re-point while an RPC is in flight would otherwise attribute the failure to
    a host it never touched.
    """
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.google.com")

    with pytest.raises(ClientError) as raised:
        _executor().raise_rpc_error_from_http_status(_status_error(404), RPCMethod.LIST_NOTEBOOKS)

    assert f"on {REQUEST_HOST}" in str(raised.value)
    assert "notebook.google.com" not in str(raised.value)


def test_mapper_does_not_raise_over_an_unusable_base_url(monkeypatch) -> None:
    """An unresolvable host degrades the message; it never masks the HTTP error.

    Reached when the failed request carries no host *and* the configured base
    URL is invalid. An error mapper that raised in place of the error it reports
    would turn a diagnosable 404 into a ``ValueError`` from the ``except`` block.
    """
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://not-a-google-host.example")

    with pytest.raises(ClientError) as raised:
        _executor().raise_rpc_error_from_http_status(
            _status_error(404, url="file:///tmp/no-host"),
            RPCMethod.LIST_NOTEBOOKS,
        )

    assert "calling LIST_NOTEBOOKS:" in str(raised.value)
    assert " on " not in str(raised.value)


@pytest.mark.asyncio
async def test_retry_exhausted_429_names_the_host(monkeypatch) -> None:
    """The 429 a real RPC reaches is the transport's, not the mapper's.

    ``RuntimeTransport`` converts HTTP 429 into ``TransportRateLimited`` once the
    retry budget is exhausted — or immediately when retries are disabled — so
    ``_execute_once`` handles it before the status mapper is ever consulted.
    Pinning the host here keeps the two 429 paths from drifting apart.
    """
    core = build_client_shell_for_tests(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="CSRF", session_id="SID")
    )
    original = _status_error(429)

    async def _rate_limited(**_kwargs: Any) -> httpx.Response:
        raise TransportRateLimited(
            "429",
            retry_after=7,
            response=original.response,
            original=original,
        )

    monkeypatch.setattr(core._composed.transport, "perform_authed_post", _rate_limited)

    with pytest.raises(RateLimitError) as raised:
        await core._rpc_executor._execute_once(
            RPCMethod.LIST_NOTEBOOKS,
            [],
            "/notebook/abc",
            True,
            False,
        )

    assert f"on {REQUEST_HOST}" in str(raised.value)
    assert raised.value.retry_after == 7
