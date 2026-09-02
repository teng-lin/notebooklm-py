"""Tests for ``scripts/check_rpc_health.py`` exit-code policy.

The nightly canary previously exited ``0`` on every ``ERROR`` status by
labelling them "transient". That meant a silently-broken canary stayed
green in CI while the drift detector was effectively offline.

These tests pin down the new policy:

    * MISMATCH                   -> exit 1   (RPC ID drift)
    * Non-transient ERROR        -> exit 3   (timeouts, parse errors, etc.)
    * Transient rate-limit ERROR -> exit 0   (HTTP 429 / RESOURCE_EXHAUSTED)
    * All OK                     -> exit 0

Priority when statuses collide: MISMATCH (1) > non-transient ERROR (3) > OK (0).
AUTH (2) is signalled earlier via ``sys.exit(2)``; every failure class
``load_auth`` maps to it — missing storage, ``ValueError``, ``httpx.HTTPError``
— is exercised in the auth-failure section near the end of this file.

Cookie-domain scoping of the same ``load_auth`` path is a separate concern and
lives in ``tests/unit/test_scripts_auth_cookie_domains.py`` (issue #2019).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

# ``scripts`` is importable as a namespace package (``pythonpath = ["."]`` in
# pyproject), so a plain import is enough — and it yields the SAME module object
# as ``tests/unit/test_capture_rpc_registry.py`` and
# ``tests/unit/test_scripts_auth_cookie_domains.py`` use. The previous
# ``importlib.util.spec_from_file_location`` preamble registered a second,
# independent module under the bare name ``check_rpc_health``; patching one
# copy left the other untouched.
from scripts import check_rpc_health

CheckStatus = check_rpc_health.CheckStatus
CheckResult = check_rpc_health.CheckResult
compute_exit_code = check_rpc_health.compute_exit_code
is_transient_error = check_rpc_health.is_transient_error
partition_errors = check_rpc_health.partition_errors
print_summary = check_rpc_health.print_summary
setup_temp_resources = check_rpc_health.setup_temp_resources
make_rpc_request = check_rpc_health.make_rpc_request


class _Abort(BaseException):
    pass


def _result(
    name: str,
    status: CheckStatus,
    *,
    error: str | None = None,
) -> CheckResult:
    """Build a CheckResult with a stub RPCMethod-like object.

    ``print_summary`` accesses ``result.method.name`` and
    ``result.expected_id``, so we use a small ducktype rather than
    importing the real ``RPCMethod`` enum (which would add a heavy
    dependency for what is purely a logic test).
    """

    class _Method:
        def __init__(self, n: str) -> None:
            self.name = n

    return CheckResult(  # type: ignore[no-any-return]
        method=_Method(name),  # type: ignore[arg-type]
        status=status,
        expected_id=f"id_{name}",
        found_ids=[],
        error=error,
    )


# ---------------------------------------------------------------------------
# is_transient_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429",
        "HTTP 429: Too Many Requests",
        "rpc error: code = RESOURCE_EXHAUSTED desc = quota exceeded",
        "RESOURCE_EXHAUSTED",
        # The decoder raises RateLimitError with these user-displayable
        # messages; before the marker was added they leaked through the
        # "Parse error: ..." branch as non-transient and farmed dup
        # issues. Pin both phrasings (decoder.py:117 and :470).
        "Parse error: API rate limit exceeded. Please wait before retrying.",
        "Parse error: API rate limit or quota exceeded. Please wait before retrying.",
        # ``httpx.ReadTimeout`` with an empty message stringifies to the
        # class name (see issue #864 fallback in make_rpc_request) and is
        # a Google-side flake that passes on retry — see #1004 and the
        # 2026-05-20 occurrence. Classifying it as transient prevents
        # auto-opening dup issues for single-run timeouts.
        "ReadTimeout",
    ],
)
def test_transient_markers_match(message: str) -> None:
    assert is_transient_error(message) is True


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "HTTP 500",
        "HTTP 503",
        "Parse error: unexpected token",
        "Connection timeout",
        "RPC ID not found in response",
    ],
)
def test_non_transient_markers_do_not_match(message: str | None) -> None:
    assert is_transient_error(message) is False


@pytest.mark.parametrize(
    "message",
    [
        # Only ``httpx.ReadTimeout`` is treated as a Google-side flake.
        # Connect/Write/Pool timeouts point at client- or network-side
        # problems (DNS, broken proxy, exhausted connection pool) and
        # should keep tripping the canary so they get investigated.
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
    ],
)
def test_non_readtimeout_timeouts_stay_non_transient(message: str) -> None:
    assert is_transient_error(message) is False


# ---------------------------------------------------------------------------
# partition_errors
# ---------------------------------------------------------------------------


def test_partition_errors_separates_transient_from_real() -> None:
    results = [
        _result("ok", CheckStatus.OK),
        _result("rate", CheckStatus.ERROR, error="HTTP 429"),
        _result("timeout", CheckStatus.ERROR, error="Connection timeout"),
        _result("parse", CheckStatus.ERROR, error="Parse error: bad JSON"),
        _result("quota", CheckStatus.ERROR, error="RESOURCE_EXHAUSTED on RPC"),
        # RateLimitError raised by the decoder reaches the canary wrapped in
        # the "Parse error: ..." prefix — it must still partition as transient.
        _result(
            "ratelimit_leak",
            CheckStatus.ERROR,
            error="Parse error: API rate limit or quota exceeded. Please wait before retrying.",
        ),
        # ``httpx.ReadTimeout("")`` surfaces as the bare class name via the
        # str-or-classname fallback in make_rpc_request (issue #864).
        # Treated as transient per issue #1004 — a single-run Google-side
        # timeout should not auto-open a non-transient issue.
        _result("read_timeout", CheckStatus.ERROR, error="ReadTimeout"),
        _result("mismatch", CheckStatus.MISMATCH),
    ]
    non_transient, transient = partition_errors(results)
    assert [r.method.name for r in non_transient] == ["timeout", "parse"]
    assert [r.method.name for r in transient] == [
        "rate",
        "quota",
        "ratelimit_leak",
        "read_timeout",
    ]


@pytest.mark.asyncio
async def test_make_rpc_request_builds_the_url_and_auth_route() -> None:
    """Query shape and CSRF body for a probe request.

    Cookies are deliberately *not* asserted here. They no longer travel in a
    hand-built header — the probes share one client carrying the
    domain-scoped jar (#2054) — so a ``FakeClient`` cannot observe them.
    Cookie coverage lives in ``test_scripts_auth_cookie_domains.py``, which
    drives the real ``build_probe_client`` and asserts on the recorded
    request, the only view that would notice a re-added manual header.
    """
    captured: dict[str, Any] = {}

    class FakeClient:
        async def post(
            self,
            url: str,
            *,
            content: str,
            headers: dict[str, str],
        ) -> httpx.Response:
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return httpx.Response(
                200,
                text=")]}'\n\n[]",
                request=httpx.Request("POST", url),
            )

    auth = check_rpc_health.AuthTokens(
        cookies={
            "SID": "sid",
            "__Secure-1PSIDTS": "ts",
            "APISID": "apisid",
            "SAPISID": "sapisid",
        },
        csrf_token="csrf",
        session_id="session",
        authuser=2,
        account_email="bob@example.com",
    )

    response_text, error = await make_rpc_request(
        FakeClient(),
        auth,
        check_rpc_health.RPCMethod.CREATE_NOTEBOOK,
        ["Title"],
        source_path="/notebook/nb_123",
    )

    assert response_text == ")]}'\n\n[]"
    assert error is None
    assert "Cookie" not in captured["headers"], (
        "the probe must not hand-build a Cookie header; the client jar carries them"
    )
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["rpcids"] == [check_rpc_health.RPCMethod.CREATE_NOTEBOOK.value]
    assert query["source-path"] == ["/notebook/nb_123"]
    assert query["f.sid"] == ["session"]
    assert query["hl"] == [check_rpc_health.get_default_language()]
    assert query["rt"] == ["c"]
    assert query["authuser"] == ["bob@example.com"]
    assert "at=csrf" in captured["content"]

    captured.clear()
    default_auth = check_rpc_health.AuthTokens(
        cookies={
            "SID": "sid",
            "__Secure-1PSIDTS": "ts",
            "APISID": "apisid",
            "SAPISID": "sapisid",
        },
        csrf_token="csrf",
        session_id="session",
    )

    await make_rpc_request(
        FakeClient(),
        default_auth,
        check_rpc_health.RPCMethod.LIST_NOTEBOOKS,
        [],
    )

    default_query = parse_qs(urlparse(captured["url"]).query)
    assert "authuser" not in default_query


# Regression fixtures for issue #864: ``httpx.ReadTimeout`` raised from
# anyio's bare ``TimeoutError()`` has ``str(e) == ""``. Without the
# class-name fallback the empty error string is swallowed by ``if error:``
# checks and mislabeled as "Empty response from server". Each call site
# that consumes ``make_rpc_request`` gets its own regression test below.


class _TimingOutClient:
    async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
        raise httpx.ReadTimeout("")


@pytest.fixture
def timing_out_auth() -> check_rpc_health.AuthTokens:
    return check_rpc_health.AuthTokens(
        cookies={"SID": "sid"},
        csrf_token="csrf",
        session_id="session",
    )


@pytest.mark.asyncio
async def test_make_rpc_request_surfaces_class_name_for_empty_message_errors(
    timing_out_auth: check_rpc_health.AuthTokens,
) -> None:
    response_text, error = await make_rpc_request(
        _TimingOutClient(),
        timing_out_auth,
        check_rpc_health.RPCMethod.GET_SUGGESTED_REPORTS,
        [[2], "nb"],
    )
    assert response_text is None
    assert error == "ReadTimeout"


@pytest.mark.asyncio
async def test_make_rpc_call_propagates_empty_message_errors_without_relabeling(
    timing_out_auth: check_rpc_health.AuthTokens,
) -> None:
    found_ids, error = await check_rpc_health.make_rpc_call(
        _TimingOutClient(),
        timing_out_auth,
        check_rpc_health.RPCMethod.GET_SUGGESTED_REPORTS,
        [[2], "nb"],
    )
    assert found_ids == []
    assert error == "ReadTimeout"


@pytest.mark.asyncio
async def test_test_rpc_method_with_data_propagates_empty_message_errors(
    timing_out_auth: check_rpc_health.AuthTokens,
) -> None:
    result, data = await check_rpc_health.test_rpc_method_with_data(
        _TimingOutClient(),
        timing_out_auth,
        check_rpc_health.RPCMethod.CREATE_NOTEBOOK,
        ["Title"],
    )
    assert data is None
    assert result.status is CheckStatus.ERROR
    assert result.error == "ReadTimeout"


@pytest.mark.asyncio
async def test_check_method_propagates_empty_message_errors(
    timing_out_auth: check_rpc_health.AuthTokens,
) -> None:
    # ``check_method`` is the third call site that consumes the
    # ``(found_ids, error)`` tuple. Pin its behavior so the
    # class-name fallback can't silently regress only here.
    result = await check_rpc_health.check_method(
        _TimingOutClient(),
        timing_out_auth,
        check_rpc_health.RPCMethod.GET_SOURCE,
        notebook_id="nb",
    )
    assert result.status is CheckStatus.ERROR
    assert result.error == "ReadTimeout"


def test_is_transient_error_classifies_readtimeout_as_transient() -> None:
    # Pinned policy (see scripts/check_rpc_health.py docstring + the
    # comment on TRANSIENT_ERROR_MARKERS): ``httpx.ReadTimeout`` against
    # Google's RPC endpoints is a server-side flake that passes on retry,
    # so it is filtered out of the auto-open issue path (#1004). Other
    # timeouts (Connect/Write/Pool) stay non-transient because they
    # point at client- or network-side problems worth surfacing. If this
    # policy ever changes, update this test deliberately — don't drop it.
    assert is_transient_error("ReadTimeout") is True
    assert is_transient_error("Chat rate limit reached: terminal stream sequence 3") is True
    assert is_transient_error("ConnectTimeout") is False
    assert is_transient_error("WriteTimeout") is False
    assert is_transient_error("PoolTimeout") is False


@pytest.mark.asyncio
async def test_setup_temp_resources_uses_canonical_create_notebook_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    async def fake_test_rpc_method_with_data(
        client: object,
        auth: object,
        method: Any,
        params: list[Any],
        source_path: str = "/",
    ) -> tuple[CheckResult, None]:
        captured["method"] = method
        captured["params"] = params
        captured["source_path"] = source_path
        return (
            CheckResult(
                method=method,
                status=CheckStatus.ERROR,
                expected_id=method.value,
                found_ids=[],
                error="stop after create",
            ),
            None,
        )

    monkeypatch.setattr(
        check_rpc_health,
        "test_rpc_method_with_data",
        fake_test_rpc_method_with_data,
    )

    results: list[CheckResult] = []
    temp = await setup_temp_resources(object(), object(), results)
    _ = capsys.readouterr()

    assert captured["method"] is check_rpc_health.RPCMethod.CREATE_NOTEBOOK
    assert captured["source_path"] == "/"
    assert captured["params"][0].startswith("RPC-Health-Check-")
    assert captured["params"][1:] == [
        None,
        None,
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    ]
    assert results[0].status == CheckStatus.ERROR
    assert temp.notebook_id is None


# ---------------------------------------------------------------------------
# get_test_params: GET_SUGGESTED_REPORTS routing
# ---------------------------------------------------------------------------


def test_get_suggested_reports_prefers_stable_read_only_notebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In --full mode the temp notebook has no indexed sources, so
    GET_SUGGESTED_REPORTS returns an empty body and trips the empty-response
    guard. When NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID is set, the canary should
    route this method there even if the caller passes the temp ID.
    """
    monkeypatch.setenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "stable_nb")
    params = check_rpc_health.get_test_params(
        check_rpc_health.RPCMethod.GET_SUGGESTED_REPORTS,
        "temp_nb",
    )
    assert params == [[2], "stable_nb"]


def test_get_suggested_reports_prefers_generation_notebook_when_read_only_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In --full mode, use the generation notebook as the stable fallback
    when a dedicated read-only notebook is not configured.
    """
    monkeypatch.delenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_GENERATION_NOTEBOOK_ID", "generation_nb")
    params = check_rpc_health.get_test_params(
        check_rpc_health.RPCMethod.GET_SUGGESTED_REPORTS,
        "temp_nb",
    )
    assert params == [[2], "generation_nb"]


def test_get_suggested_reports_falls_back_to_caller_notebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the stable secret, fall back to the notebook_id the caller
    passed (preserves quick-mode behaviour where there's no temp notebook).
    """
    monkeypatch.delenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_GENERATION_NOTEBOOK_ID", raising=False)
    params = check_rpc_health.get_test_params(
        check_rpc_health.RPCMethod.GET_SUGGESTED_REPORTS,
        "caller_nb",
    )
    assert params == [[2], "caller_nb"]


# ---------------------------------------------------------------------------
# compute_exit_code priority
# ---------------------------------------------------------------------------


def _counts(**overrides: int) -> Counter[Any]:
    """Build a Counter with all statuses defaulted to 0."""
    base: dict[Any, int] = dict.fromkeys(CheckStatus, 0)
    base.update({getattr(CheckStatus, k.upper()): v for k, v in overrides.items()})
    return Counter(base)


def test_exit_code_all_ok() -> None:
    assert compute_exit_code(_counts(ok=10), []) == 0


def test_exit_code_only_transient_errors() -> None:
    # Counts include the ERROR, but the non_transient list is empty.
    counts = _counts(ok=9, error=1)
    assert compute_exit_code(counts, []) == 0


def test_exit_code_non_transient_error() -> None:
    counts = _counts(ok=9, error=1)
    non_transient = [_result("timeout", CheckStatus.ERROR, error="Connection timeout")]
    assert compute_exit_code(counts, non_transient) == 3


def test_exit_code_mismatch_alone() -> None:
    counts = _counts(ok=9, mismatch=1)
    assert compute_exit_code(counts, []) == 1


def test_exit_code_mismatch_beats_non_transient_error() -> None:
    """Priority: MISMATCH (1) wins over non-transient ERROR (3)."""
    counts = _counts(ok=8, mismatch=1, error=1)
    non_transient = [_result("timeout", CheckStatus.ERROR, error="Connection timeout")]
    assert compute_exit_code(counts, non_transient) == 1


# ---------------------------------------------------------------------------
# print_summary (integration over the helpers)
# ---------------------------------------------------------------------------


def test_print_summary_all_match_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [_result(f"m{i}", CheckStatus.OK) for i in range(3)]
    assert print_summary(results) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out


def test_print_summary_only_rate_limit_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        _result("ok", CheckStatus.OK),
        _result("rate", CheckStatus.ERROR, error="HTTP 429"),
    ]
    assert print_summary(results) == 0
    out = capsys.readouterr().out
    assert "transient" in out.lower()
    assert "RESULT: PASS" in out


def test_print_summary_non_transient_error_returns_three(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        _result("ok", CheckStatus.OK),
        _result("timeout", CheckStatus.ERROR, error="Connection timeout"),
    ]
    assert print_summary(results) == 3
    out = capsys.readouterr().out
    assert "non-transient ERROR detected in methods: timeout" in out


def test_print_summary_mismatch_plus_error_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        _result("drift", CheckStatus.MISMATCH),
        _result("timeout", CheckStatus.ERROR, error="Connection timeout"),
    ]
    assert print_summary(results) == 1
    out = capsys.readouterr().out
    assert "RESULT: FAIL - RPC ID mismatches detected" in out


def test_print_summary_lists_affected_methods_on_exit_three(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        _result("timeout", CheckStatus.ERROR, error="Connection timeout"),
        _result("parse", CheckStatus.ERROR, error="Parse error: bad JSON"),
        _result("rate", CheckStatus.ERROR, error="HTTP 429"),
    ]
    assert print_summary(results) == 3
    out = capsys.readouterr().out
    # Extract the "affected methods" header line so we only inspect the list
    # of method names (the surrounding explanatory text legitimately mentions
    # "rate-limit transients").
    header_line = next(
        line for line in out.splitlines() if "non-transient ERROR detected in methods:" in line
    )
    affected = header_line.split("methods:", 1)[1]
    # Both non-transient methods appear in the affected list…
    assert "timeout" in affected and "parse" in affected
    # …and the transient one is NOT listed as an affected method.
    assert "rate" not in affected


# ---------------------------------------------------------------------------
# check_chat_query: GenerateFreeFormStreamed (PATH_NOT_METHOD) drift probe
#
# The streamed-chat orchestration RPC has no obfuscated method ID to echo back
# (it is a hardcoded ``v1`` URL path), so the canary probes its WIRE SHAPE:
# 200 + a recognizable stream frame -> OK; zero parseable chunks -> ERROR
# (drift). These tests pin that classification (issue #1492).
# ---------------------------------------------------------------------------


def _chat_auth() -> Any:
    return check_rpc_health.AuthTokens(
        cookies={"SID": "sid"},
        csrf_token="csrf",
        session_id="session",
        authuser=2,
        account_email="bob@example.com",
    )


class _ChatClient:
    """Minimal httpx-like client returning a fixed response (or raising)."""

    def __init__(
        self,
        *,
        text: str = "",
        status: int = 200,
        raises: Exception | None = None,
        response_headers: dict[str, str] | None = None,
    ):
        self._text = text
        self._status = status
        self._raises = raises
        self._response_headers = response_headers or {}
        self.url: str | None = None
        self.content: str | None = None

    async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
        self.url = url
        self.content = content
        if self._raises is not None:
            raise self._raises
        return httpx.Response(
            self._status,
            text=self._text,
            headers=self._response_headers,
            request=httpx.Request("POST", url),
        )


def _wrb_fr_body(inner: Any) -> str:
    """Build an anti-XSSI-prefixed body wrapping one ``wrb.fr`` frame."""
    frame = json.dumps([["wrb.fr", "GenerateFreeFormStreamed", json.dumps(inner)]])
    return ")]}'\n" + frame


def test_chat_probe_uses_query_endpoint_path() -> None:
    """The probe's CheckResult is labelled with the streamed-chat path, not an
    RPCMethod value, and matches the library's ``_QUERY_ENDPOINT_PATH``.
    """
    assert check_rpc_health.CHAT_QUERY_PROBE.value == check_rpc_health._QUERY_ENDPOINT_PATH
    assert "GenerateFreeFormStreamed" in check_rpc_health.CHAT_QUERY_PROBE.value


@pytest.mark.asyncio
async def test_chat_probe_skipped_without_notebook() -> None:
    result = await check_rpc_health.check_chat_query(_ChatClient(), _chat_auth(), None)
    assert result.status is CheckStatus.SKIPPED
    assert result.method.name == "GENERATE_FREE_FORM_STREAMED"


@pytest.mark.asyncio
async def test_chat_probe_ok_on_parseable_frame() -> None:
    # An empty-list ``wrb.fr`` frame is a recognized (heartbeat) frame: the
    # wire shape is intact even though there's no answer text. -> OK.
    client = _ChatClient(text=_wrb_fr_body([]))
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.OK
    # The probe hit the streamed-chat endpoint, not batchexecute.
    assert "GenerateFreeFormStreamed" in (client.url or "")


@pytest.mark.asyncio
async def test_chat_probe_error_on_zero_parseable_chunks() -> None:
    # Empty/garbage body -> ChatResponseParseError -> ERROR (this is the
    # wire-drift signal the chat canary exists to catch).
    client = _ChatClient(text=")]}'\n\n")
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.ERROR
    assert "Parse error" in (result.error or "")


@pytest.mark.asyncio
async def test_chat_probe_error_on_strict_decode_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strict positional drift in the wire decoder raises UnknownRPCMethodError
    # (a DecodingError subclass), NOT ChatResponseParseError. The probe must
    # CATCH it and return ERROR rather than letting it escape and crash the
    # canary (#1492 review). This is the drift the chat canary exists to catch.
    from notebooklm.exceptions import UnknownRPCMethodError

    def _raise_drift(_text: str) -> Any:
        raise UnknownRPCMethodError("safe_index drift at path ()[0]")

    monkeypatch.setattr(check_rpc_health, "parse_streaming_chat_response", _raise_drift)
    client = _ChatClient(text=_wrb_fr_body([]))
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.ERROR
    assert "Parse error" in (result.error or "")


@pytest.mark.asyncio
async def test_chat_probe_ok_on_recognized_server_error_frame() -> None:
    # An ``"er"`` frame is a RECOGNIZED frame (the parser raises ChatError):
    # the wire contract is intact, the server merely declined. -> OK.
    body = ")]}'\n" + json.dumps([["er", "GenerateFreeFormStreamed", 7]])
    client = _ChatClient(text=body)
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.OK


@pytest.mark.asyncio
async def test_chat_probe_ok_on_rate_limit_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    # A terminal stream sequence without an RPC payload raises RateLimitError
    # (server quota exhausted): the wire contract is intact, the server merely
    # declined. -> OK (#2323).
    from notebooklm.exceptions import RateLimitError

    def _raise_rate_limit(_text: str) -> Any:
        raise RateLimitError(
            "Chat rate limit reached: the request ended before the server "
            "returned an RPC payload (terminal stream sequence 3). Retry later."
        )

    monkeypatch.setattr(check_rpc_health, "parse_streaming_chat_response", _raise_rate_limit)
    client = _ChatClient(text=")]}'\n")
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.OK
    assert "Server declined (recognized frame)" in (result.error or "")
    assert "rate limit reached" in (result.error or "")


@pytest.mark.asyncio
async def test_chat_probe_error_on_http_status() -> None:
    client = _ChatClient(status=500, text="boom")
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.ERROR
    assert result.error == "HTTP 500"


@pytest.mark.asyncio
async def test_chat_probe_surfaces_class_name_for_empty_message_errors() -> None:
    # Mirror the make_rpc_request #864 fallback: ``httpx.ReadTimeout("")``
    # stringifies to "" and must surface as the class name, not be swallowed.
    client = _ChatClient(raises=httpx.ReadTimeout(""))
    result = await check_rpc_health.check_chat_query(client, _chat_auth(), "nb_123")
    assert result.status is CheckStatus.ERROR
    assert result.error == "ReadTimeout"
    # And ReadTimeout is classified as transient downstream (does not fail the
    # canary) — same posture as the method-ID probes.
    assert is_transient_error(result.error) is True


# ---------------------------------------------------------------------------
# auth-failure paths -> exit 2
#
# Every failure class ``load_auth`` maps to the AUTH exit code gets a test.
# Before #2019 only the missing-file arm was covered, so the ``ValueError`` and
# ``httpx.HTTPError`` arms — including the ``scrub_secrets`` call that keeps
# ``f.sid=<session_id>`` out of the CI log — shipped unverified.
# ---------------------------------------------------------------------------


def test_main_exits_two_when_auth_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A genuinely absent storage file must surface as exit code 2.

    Nothing is patched: ``NOTEBOOKLM_HOME`` is pointed at an empty directory so
    the real loader resolves a real (nonexistent) path and raises
    ``FileNotFoundError`` for itself. That also insulates the test from the
    developer's own ``~/.notebooklm/storage_state.json``.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["check_rpc_health.py"])

    with pytest.raises(SystemExit) as excinfo:
        check_rpc_health.main()
    assert excinfo.value.code == 2


def test_main_exits_two_on_unhandled_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unhandled crash in main must return exit code 2 (infrastructure failure),
    never exit 1 which the workflow interprets as an RPC mismatch (#2323).
    Secrets in the exception and formatted traceback must be scrubbed.
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    monkeypatch.setattr("sys.argv", ["check_rpc_health.py"])

    async def _crashing_run(*_args: Any, **_kwargs: Any) -> Any:
        # Include a secret token that should be redacted by scrub_secrets
        raise RuntimeError("Crash with SID=secret_cookie_value_here")

    monkeypatch.setattr(check_rpc_health, "run_health_check", _crashing_run)
    exit_code = check_rpc_health.main()
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "FATAL: RPC health check crashed:" in err
    assert "secret_cookie_value_here" not in err
    assert "***" in err


async def _load_auth_raising(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Run ``load_auth`` with the private storage owner raising ``error``."""
    from notebooklm._auth import tokens as tokens_module

    async def load(_self: object, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(tokens_module.StoredAuthLoader, "load", load)
    await check_rpc_health.load_auth(None)


@pytest.mark.asyncio
async def test_load_auth_uses_private_owner_with_exact_arguments_and_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from notebooklm._auth import tokens as tokens_module

    auth = object()
    captured: dict[str, Any] = {}

    class Loaded:
        pass

    loaded = Loaded()
    loaded.auth = auth

    async def load(_self: object, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return loaded

    monkeypatch.setattr(tokens_module.StoredAuthLoader, "load", load)
    path = Path("storage.json")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        result = await check_rpc_health.load_auth(path)

    assert result is auth
    assert captured == {
        "path": path,
        "profile": None,
        "policy": check_rpc_health.LoadPolicy(),
        "auth_type": check_rpc_health.AuthTokens,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("refresh failed"),
        OSError("permission denied"),
        asyncio.CancelledError(),
        _Abort("abort"),
    ],
)
async def test_load_auth_preserves_unmapped_error_identity(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    with pytest.raises(type(error)) as excinfo:
        await _load_auth_raising(monkeypatch, error)
    assert excinfo.value is error


async def test_load_auth_exits_two_and_names_the_source_on_value_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``ValueError`` exits 2 and says which auth source produced it.

    The file branch of ``_load_storage_state`` re-raises a bare ``json`` error
    with no context, so without the source line a corrupt storage_state.json
    prints only ``Expecting value: line 1 column 1 (char 0)``.
    """
    with pytest.raises(SystemExit) as excinfo:
        await _load_auth_raising(monkeypatch, ValueError("Expecting value: line 1 column 1"))
    assert excinfo.value.code == 2

    stderr = capsys.readouterr().err
    assert "Expecting value: line 1 column 1" in stderr
    assert "Auth source: NOTEBOOKLM_AUTH_JSON env var" in stderr


async def test_load_auth_exits_two_and_scrubs_secrets_on_http_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transport failure exits 2 with the session id redacted.

    ``httpx`` exception strings echo the full request URL, which carries
    ``f.sid=<session_id>``. This report is printed straight into the Actions log
    and into the auto-filed issue body, so the redaction is load-bearing.
    """
    leaky = httpx.ConnectError(
        "All connection attempts failed for "
        "https://notebooklm.google.com/batchexecute?f.sid=-8891234567890123456"
    )
    with pytest.raises(SystemExit) as excinfo:
        await _load_auth_raising(monkeypatch, leaky)
    assert excinfo.value.code == 2

    stderr = capsys.readouterr().err
    assert "Network error while fetching auth tokens" in stderr
    assert "-8891234567890123456" not in stderr, (
        "the session id must not reach the CI log or the auto-filed issue body"
    )


# ---------------------------------------------------------------------------
# GET_CUSTOMIZATION_CHOICES studio option-table cross-check (#2284)
# ---------------------------------------------------------------------------

CustomizationStatus = check_rpc_health.CustomizationStatus
build_customization_choices_params = check_rpc_health.build_customization_choices_params
check_customization_table = check_rpc_health.check_customization_table
compare_customization_choices = check_rpc_health.compare_customization_choices
make_raw_rpc_request = check_rpc_health.make_raw_rpc_request

_CHOICES_ID = check_rpc_health.RPCMethod.GET_CUSTOMIZATION_CHOICES.value
_OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
# The four families as served live on 2026-09-02 (labels verbatim, descriptions elided).
# CINEMATIC is a dedicated generation flow and is not always included in this
# account/UI availability table; its absence is not removal of wire format 3.
_LIVE_TABLE: list[Any] = [
    [
        [[[1, "Deep Dive", "d"], [2, "Brief", "d"], [3, "Critique", "d"], [4, "Debate", "d"]]],
        [[[1, "Explainer", "d"], [2, "Brief", "d"], [4, "Short", "d"]]],
        [[[1, "Detailed Deck", "d"], [2, "Presenter Slides", "d"]]],
        [[["Briefing Doc", "d", "directive"]]],
    ]
]


def _probe_auth() -> check_rpc_health.AuthTokens:
    return check_rpc_health.AuthTokens(
        cookies={"SID": "sid"},
        csrf_token="csrf",
        session_id="session",
    )


def _choices_frame(payload: Any) -> str:
    inner = "null" if payload is None else json.dumps(json.dumps(payload))
    return ")]}'\n\n" + f'[["wrb.fr","{_CHOICES_ID}",{inner},null,null,null,"generic"]]'


def test_build_customization_choices_params_is_the_production_shape() -> None:
    """Context only, plus the notebook id when known — the old ``[nbctx, None, 3]`` drew 3 (#2284)."""
    assert build_customization_choices_params(None) == [_OPTS]
    assert build_customization_choices_params("nb_42") == [_OPTS, "nb_42"]


@pytest.mark.asyncio
async def test_make_raw_rpc_request_threads_id_into_query_and_body() -> None:
    """The raw id lands in BOTH the ``rpcids=`` query param and the ``f.req`` body."""
    captured: dict[str, Any] = {}

    class FakeClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            captured["url"] = url
            captured["content"] = content
            return httpx.Response(200, text=")]}'\n\n[]", request=httpx.Request("POST", url))

    text, error = await make_raw_rpc_request(
        FakeClient(),
        _probe_auth(),
        "OcvKNc",
        [["x"]],
        source_path="/notebook/nb_1",
    )
    assert error is None
    assert text == ")]}'\n\n[]"
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["rpcids"] == ["OcvKNc"]
    assert query["source-path"] == ["/notebook/nb_1"]
    assert "OcvKNc" in captured["content"]
    # The fallback RPCMethod (LIST_NOTEBOOKS) must NOT leak into the wire.
    assert check_rpc_health.RPCMethod.LIST_NOTEBOOKS.value not in captured["content"]


def test_compare_customization_choices_matches_live_table_without_cinematic_row() -> None:
    status, detail = compare_customization_choices(_LIVE_TABLE)
    assert status is CustomizationStatus.MATCH
    assert "required customization choices are present" in detail


def test_compare_customization_choices_matches_when_optional_cinematic_row_is_served() -> None:
    """CINEMATIC has appeared in sqTeoe too; both availability shapes are valid."""
    table = json.loads(json.dumps(_LIVE_TABLE))
    table[0][1][0].insert(2, [3, "Cinematic", "d"])
    status, detail = compare_customization_choices(table)
    assert status is CustomizationStatus.MATCH
    assert "required customization choices are present" in detail


def test_compare_customization_choices_drifts_on_a_new_code() -> None:
    """A served row the client does not model (e.g. AudioFormat 5 "Lecture") is DRIFT."""
    table = json.loads(json.dumps(_LIVE_TABLE))
    table[0][0][0].append([5, "Lecture", "d"])
    status, detail = compare_customization_choices(table)
    assert status is CustomizationStatus.DRIFT
    assert "audio: served codes not modelled [(5, 'Lecture')]" in detail


def test_compare_customization_choices_drifts_on_a_dropped_member_only() -> None:
    """Codes decide DRIFT; a label rename is reported but is not drift on its own."""
    table = json.loads(json.dumps(_LIVE_TABLE))
    del table[0][1][0][2]  # video: Short (4) no longer served
    status, detail = compare_customization_choices(table)
    assert status is CustomizationStatus.DRIFT
    assert "video: modelled codes not served [(4, 'SHORT')]" in detail
    assert "served codes not modelled" not in detail


def test_compare_customization_choices_reports_a_label_rename_without_drift() -> None:
    """Labels follow ``hl`` and Google's copy; only the codes are load-bearing (#1597)."""
    table = json.loads(json.dumps(_LIVE_TABLE))
    table[0][1][0][2] = [4, "Panel", "d"]  # video: Short renamed, code unchanged
    status, detail = compare_customization_choices(table)
    assert status is CustomizationStatus.MATCH
    assert "video: label differs from member name [(4, 'Panel', 'SHORT')]" in detail


def test_compare_customization_choices_drifts_on_an_unmodelled_family() -> None:
    table = json.loads(json.dumps(_LIVE_TABLE))
    table[0].append([[[1, "Infographic style", "d"]]])
    status, detail = compare_customization_choices(table)
    assert status is CustomizationStatus.DRIFT
    assert "1 unmodelled family slot(s) served" in detail


def test_compare_customization_choices_drifts_on_an_empty_family_or_no_presets() -> None:
    table = json.loads(json.dumps(_LIVE_TABLE))
    table[0][2] = [[]]
    table[0][3] = [[]]
    status, detail = compare_customization_choices(table)
    assert status is CustomizationStatus.DRIFT
    assert "slide_deck: no rows served" in detail
    assert "reports: no well-formed presets served" in detail
    # Presets that are present but all malformed count as none.
    table[0][3] = [[["no directive", "d", ""]]]
    _, detail = compare_customization_choices(table)
    assert "reports: no well-formed presets served" in detail


@pytest.mark.asyncio
async def test_check_customization_table_matches_on_the_live_shape() -> None:
    captured: dict[str, Any] = {}

    class LiveClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            captured["url"] = url
            captured["content"] = content
            return httpx.Response(
                200, text=_choices_frame(_LIVE_TABLE), request=httpx.Request("POST", url)
            )

    status, detail = await check_customization_table(LiveClient(), _probe_auth(), "nb_1")
    assert status is CustomizationStatus.MATCH
    assert parse_qs(urlparse(captured["url"]).query)["rpcids"] == [_CHOICES_ID]
    # The probe sends the production request shape, not the old ``[nbctx, None, 3]``.
    sent = json.loads(parse_qs(captured["content"])["f.req"][0])[0][0]
    assert sent[0] == _CHOICES_ID
    assert json.loads(sent[1]) == [_OPTS, "nb_1"]


@pytest.mark.asyncio
async def test_check_customization_table_drifts_on_a_served_difference() -> None:
    table = json.loads(json.dumps(_LIVE_TABLE))
    table[0][0][0].append([5, "Lecture", "d"])

    class DriftClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                200, text=_choices_frame(table), request=httpx.Request("POST", url)
            )

    status, detail = await check_customization_table(DriftClient(), _probe_auth(), None)
    assert status is CustomizationStatus.DRIFT
    assert "Lecture" in detail


@pytest.mark.asyncio
async def test_check_customization_table_unknown_on_transport_error() -> None:
    """A transport failure never alarms; it draws UNKNOWN (no conclusion)."""

    class FailingClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            raise httpx.ReadTimeout("")

    status, detail = await check_customization_table(FailingClient(), _probe_auth(), "nb_1")
    assert status is CustomizationStatus.UNKNOWN
    assert detail == "ReadTimeout"


@pytest.mark.asyncio
async def test_check_customization_table_rejection_is_unknown_not_a_crash() -> None:
    """A status-bearing null is a probe failure (UNKNOWN), never an empty table or a crash."""

    class RejectedClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            body = ")]}'\n\n" + f'[["wrb.fr","{_CHOICES_ID}",null,null,null,[3],"generic"]]'
            return httpx.Response(200, text=body, request=httpx.Request("POST", url))

    status, detail = await check_customization_table(RejectedClient(), _probe_auth(), "nb_1")
    assert status is CustomizationStatus.UNKNOWN
    assert detail.startswith("RPC error")


@pytest.mark.asyncio
async def test_check_customization_table_absent_id_still_propagates() -> None:
    """The absent-id drift guard (UnknownRPCMethodError) must stay loud."""

    class RotatedClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            body = ")]}'\n\n" + '[["wrb.fr","zzzzzz","[[]]",null,null,null,"generic"]]'
            return httpx.Response(200, text=body, request=httpx.Request("POST", url))

    with pytest.raises(check_rpc_health.UnknownRPCMethodError):
        await check_customization_table(RotatedClient(), _probe_auth(), "nb_1")


@pytest.mark.asyncio
async def test_check_customization_table_bad_envelope_is_drift() -> None:
    class ReshapedClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            return httpx.Response(
                200, text=_choices_frame([42]), request=httpx.Request("POST", url)
            )

    status, detail = await check_customization_table(ReshapedClient(), _probe_auth(), None)
    assert status is CustomizationStatus.DRIFT
    assert detail.startswith("unrecognized envelope")


def test_compute_exit_code_customization_drift_is_four() -> None:
    """DRIFT draws exit 4 when no higher-priority RPC drift fired."""
    clean = Counter({CheckStatus.OK: 3})
    assert check_rpc_health.compute_exit_code(clean, [], CustomizationStatus.DRIFT) == 4
    assert check_rpc_health.compute_exit_code(clean, [], CustomizationStatus.MATCH) == 0
    assert check_rpc_health.compute_exit_code(clean, [], CustomizationStatus.UNKNOWN) == 0
    # RPC drift outranks the enum drift.
    mismatch = Counter({CheckStatus.MISMATCH: 1})
    assert check_rpc_health.compute_exit_code(mismatch, [], CustomizationStatus.DRIFT) == 1
    real_error = [_result("e", CheckStatus.ERROR, error="Parse error: boom")]
    assert check_rpc_health.compute_exit_code(Counter(), real_error, CustomizationStatus.DRIFT) == 3


def test_print_summary_reports_customization_drift(capsys: pytest.CaptureFixture[str]) -> None:
    results = [_result("ok", CheckStatus.OK)]
    rc = print_summary(results, CustomizationStatus.DRIFT)
    out = capsys.readouterr().out
    assert rc == 4
    assert "CUSTOMIZATION DRIFT" in out
    assert "sqTeoe option tables = DRIFT" in out


def test_print_summary_match_is_pass(capsys: pytest.CaptureFixture[str]) -> None:
    results = [_result("ok", CheckStatus.OK)]
    rc = print_summary(results, CustomizationStatus.MATCH)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sqTeoe option tables = MATCH" in out
    assert "RESULT: PASS" in out


# ---------------------------------------------------------------------------
# Rebrand-host lane (notebook.google.com)
#
# The lane exists to answer "does the rebrand host serve batchexecute and
# streamed chat?" from the nightly run. Its defining property is ISOLATION: a
# rebrand probe must never reach ``compute_exit_code``. The workflow files ONE
# non-transient-ERROR issue deduped by title alone, so a rebrand probe folded
# into that lane would open an issue on the first failing night and then
# suppress every main-lane degradation issue after it — disabling the gate the
# lane exists to feed. These tests pin that isolation, the state-change
# semantics, and the deliberately-unanswered upload sub-question.
# ---------------------------------------------------------------------------

RebrandProbeStatus = check_rpc_health.RebrandProbeStatus
RebrandProbe = check_rpc_health.RebrandProbe
build_rebrand_state = check_rpc_health.build_rebrand_state
load_rebrand_state = check_rpc_health.load_rebrand_state


def _probe(capability: str, status: RebrandProbeStatus) -> RebrandProbe:
    return RebrandProbe(capability, status, "detail")


def test_rebrand_probe_targets_the_alias_host() -> None:
    """The lane always asks about the rebrand host, whatever the run selected."""
    from notebooklm._env import PERSONAL_BASE_HOST

    assert check_rpc_health.rebrand_probe_host() == PERSONAL_BASE_HOST


def test_compute_exit_code_cannot_see_the_rebrand_lane() -> None:
    """Structural guard: no rebrand input may reach the exit-code computation.

    If a future change threads the lane in here, the naive-implementation trap
    is back — one permanently-failing rebrand probe would suppress the main
    lane's title-deduped issue forever.

    The exact parameter list is pinned too, so exit-coding ANY new lane stays a
    deliberate decision. ``build_label_status`` is such a decision: it has its own
    exit code (5) and its own deduped issue title, so it cannot suppress the
    main lane's issue the way a rebrand probe would.
    """
    import ast
    import inspect
    import textwrap

    params = list(inspect.signature(check_rpc_health.compute_exit_code).parameters)
    assert not any("rebrand" in name for name in params)
    assert params == [
        "counts",
        "non_transient_errors",
        "customization_status",
        "build_label_status",
    ]

    # Body only — the docstring explains the isolation and legitimately says
    # "rebrand"; executable code referencing it would be the regression.
    tree = ast.parse(textwrap.dedent(inspect.getsource(check_rpc_health.compute_exit_code)))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    body = func.body[1:] if ast.get_docstring(func) else func.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "rebrand" not in code.lower()


def test_rebrand_probes_are_not_check_results() -> None:
    """Lane output is a distinct type, so it cannot be appended to ``results``."""
    probe = check_rpc_health.rebrand_upload_probe()
    assert not isinstance(probe, CheckResult)
    assert not isinstance(probe.status, CheckStatus)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, None),
        (429, RebrandProbeStatus.UNKNOWN),
        (500, RebrandProbeStatus.UNKNOWN),
        (503, RebrandProbeStatus.UNKNOWN),
        (302, RebrandProbeStatus.ABSENT),
        (404, RebrandProbeStatus.ABSENT),
        # Auth answers are NOT endpoint answers: an unauthenticated request
        # proves nothing about whether the host serves the RPC (#2062).
        (401, RebrandProbeStatus.UNAUTHENTICATED),
        (403, RebrandProbeStatus.UNAUTHENTICATED),
        (407, RebrandProbeStatus.UNAUTHENTICATED),
    ],
)
def test_classify_rebrand_status(status_code: int, expected: RebrandProbeStatus | None) -> None:
    verdict = check_rpc_health.classify_rebrand_status(status_code)
    if expected is None:
        assert verdict is None
    else:
        assert verdict is not None and verdict[0] is expected


@pytest.mark.parametrize(
    "location",
    [
        "https://accounts.google.com/ServiceLogin?continue=https://notebook.google.com/",
        "https://accounts.google.com/v3/signin/identifier",
        "/ServiceLogin?passive=1209600",
        "https://accounts.youtube.com/accounts/SetSID",
        # The app's own sign-in bounce, measured 2026-08-04 on an
        # unauthenticated fetch. It matches none of the accounts.google.com
        # forms above, and before the ``login`` path marker it scored ABSENT —
        # a signed-out night claiming the endpoint was gone (#2073).
        "https://notebook.google.com/login?continue=https://notebook.google.com/",
        "/login?continue=https://notebook.google.com/",
    ],
)
def test_rebrand_login_redirect_is_unauthenticated(location: str) -> None:
    """A bounce into Google's sign-in flow says "signed out", not "absent"."""
    verdict = check_rpc_health.classify_rebrand_status(302, location)
    assert verdict is not None and verdict[0] is RebrandProbeStatus.UNAUTHENTICATED


@pytest.mark.parametrize(
    "location",
    ["https://notebook.google.com/app", "/app/notebook", "", None, "::not a url::"],
)
def test_rebrand_non_login_redirect_stays_absent(location: str | None) -> None:
    """Only sign-in redirects are excused — everything else still answers ABSENT."""
    verdict = check_rpc_health.classify_rebrand_status(302, location)
    assert verdict is not None and verdict[0] is RebrandProbeStatus.ABSENT


def test_retarget_url_keeps_path_and_query() -> None:
    retargeted = check_rpc_health.retarget_url(
        "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?rpcids=abc",
        "notebook.google.com",
    )
    assert retargeted.startswith("https://notebook.google.com/_/LabsTailwindUi/data/batchexecute")
    assert "rpcids=abc" in retargeted


def _batchexecute_body(rpc_id: str) -> str:
    """Minimal chunked batchexecute envelope echoing ``rpc_id``."""
    frame = json.dumps([["wrb.fr", rpc_id, json.dumps([])]])
    return ")]}'\n\n" + str(len(frame)) + "\n" + frame


@pytest.mark.asyncio
async def test_rebrand_batchexecute_present_when_id_echoes() -> None:
    from notebooklm.rpc import RPCMethod

    client = _ChatClient(text=_batchexecute_body(RPCMethod.LIST_NOTEBOOKS.value))
    probe = await check_rpc_health.probe_rebrand_batchexecute(
        client, _chat_auth(), "notebook.google.com"
    )
    assert probe.status is RebrandProbeStatus.PRESENT
    assert probe.capability == check_rpc_health.REBRAND_BATCHEXECUTE
    # It really asked the rebrand host, on the batchexecute path.
    assert (client.url or "").startswith("https://notebook.google.com/_/LabsTailwindUi/data/")


@pytest.mark.asyncio
async def test_rebrand_batchexecute_absent_on_app_shell() -> None:
    """A 200 that is the HTML app shell is ABSENT, not a parse ERROR."""
    client = _ChatClient(text="<!doctype html><title>Gemini Notebook</title>")
    probe = await check_rpc_health.probe_rebrand_batchexecute(
        client, _chat_auth(), "notebook.google.com"
    )
    assert probe.status is RebrandProbeStatus.ABSENT


@pytest.mark.asyncio
async def test_rebrand_batchexecute_absent_on_redirect() -> None:
    client = _ChatClient(status=302, text="")
    probe = await check_rpc_health.probe_rebrand_batchexecute(
        client, _chat_auth(), "notebook.google.com"
    )
    assert probe.status is RebrandProbeStatus.ABSENT
    assert "302" in probe.detail


@pytest.mark.asyncio
async def test_rebrand_batchexecute_unauthenticated_on_401() -> None:
    """A 401 is a statement about our credentials, not about the endpoint."""
    client = _ChatClient(status=401, text="")
    probe = await check_rpc_health.probe_rebrand_batchexecute(
        client, _chat_auth(), "notebook.google.com"
    )
    assert probe.status is RebrandProbeStatus.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_rebrand_chat_unauthenticated_on_sign_in_redirect() -> None:
    client = _ChatClient(
        status=302,
        text="",
        response_headers={"Location": "https://accounts.google.com/ServiceLogin?continue=x"},
    )
    probe = await check_rpc_health.probe_rebrand_chat(
        client, _chat_auth(), "notebook.google.com", "nb_123"
    )
    assert probe.status is RebrandProbeStatus.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_rebrand_batchexecute_unknown_on_transport_error() -> None:
    """A flake must never be recorded — UNKNOWN carries the old state forward."""
    client = _ChatClient(raises=httpx.ReadTimeout(""))
    probe = await check_rpc_health.probe_rebrand_batchexecute(
        client, _chat_auth(), "notebook.google.com"
    )
    assert probe.status is RebrandProbeStatus.UNKNOWN
    assert probe.detail == "ReadTimeout"


@pytest.mark.asyncio
async def test_rebrand_batchexecute_unknown_on_rate_limit() -> None:
    client = _ChatClient(status=429, text="")
    probe = await check_rpc_health.probe_rebrand_batchexecute(
        client, _chat_auth(), "notebook.google.com"
    )
    assert probe.status is RebrandProbeStatus.UNKNOWN


@pytest.mark.asyncio
async def test_rebrand_chat_present_on_parseable_stream() -> None:
    client = _ChatClient(text=_wrb_fr_body([]))
    probe = await check_rpc_health.probe_rebrand_chat(
        client, _chat_auth(), "notebook.google.com", "nb_123"
    )
    assert probe.status is RebrandProbeStatus.PRESENT
    assert "GenerateFreeFormStreamed" in (client.url or "")
    assert (client.url or "").startswith("https://notebook.google.com/")


@pytest.mark.asyncio
async def test_rebrand_chat_present_on_recognized_server_frame() -> None:
    """An ``"er"`` frame proves the endpoint is live — it just declined."""
    body = ")]}'\n" + json.dumps([["er", "GenerateFreeFormStreamed", 7]])
    client = _ChatClient(text=body)
    probe = await check_rpc_health.probe_rebrand_chat(
        client, _chat_auth(), "notebook.google.com", "nb_123"
    )
    assert probe.status is RebrandProbeStatus.PRESENT


@pytest.mark.asyncio
async def test_rebrand_chat_present_on_rate_limit_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate-limit frame proves the endpoint is live — it just declined (#2323)."""
    from notebooklm.exceptions import RateLimitError

    def _raise_rate_limit(_text: str) -> Any:
        raise RateLimitError("Chat rate limit reached")

    monkeypatch.setattr(check_rpc_health, "parse_streaming_chat_response", _raise_rate_limit)
    client = _ChatClient(text=")]}'\n")
    probe = await check_rpc_health.probe_rebrand_chat(
        client, _chat_auth(), "notebook.google.com", "nb_123"
    )
    assert probe.status is RebrandProbeStatus.PRESENT
    assert "recognized server frame" in probe.detail


@pytest.mark.asyncio
async def test_rebrand_chat_absent_on_unparseable_body() -> None:
    client = _ChatClient(text=")]}'\n\n")
    probe = await check_rpc_health.probe_rebrand_chat(
        client, _chat_auth(), "notebook.google.com", "nb_123"
    )
    assert probe.status is RebrandProbeStatus.ABSENT


@pytest.mark.asyncio
async def test_rebrand_chat_unknown_without_notebook() -> None:
    probe = await check_rpc_health.probe_rebrand_chat(
        _ChatClient(), _chat_auth(), "notebook.google.com", None
    )
    assert probe.status is RebrandProbeStatus.UNKNOWN


def test_rebrand_upload_is_explicitly_not_probed() -> None:
    """The third sub-question is recorded as open, not silently omitted.

    ``check_rpc_health.py`` exercises ADD_SOURCE_FILE as an RPC only and issues
    no Scotty POST anywhere, so an upload verdict cannot be derived here.
    """
    probe = check_rpc_health.rebrand_upload_probe()
    assert probe.capability == check_rpc_health.REBRAND_UPLOAD
    assert probe.status is RebrandProbeStatus.NOT_PROBED
    assert "manual" in probe.detail


@pytest.mark.asyncio
async def test_probe_rebrand_host_returns_all_three_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_rpc_health, "CALL_DELAY", 0.0)
    client = _ChatClient(text=")]}'\n\n")
    probes = await check_rpc_health.probe_rebrand_host(client, _chat_auth(), "nb_123")
    assert [p.capability for p in probes] == [
        check_rpc_health.REBRAND_BATCHEXECUTE,
        check_rpc_health.REBRAND_CHAT,
        check_rpc_health.REBRAND_UPLOAD,
    ]


# --- state / transition semantics -------------------------------------------


def test_rebrand_acknowledged_baseline_epoch() -> None:
    """The #2077/#2078 evidence and cache epoch must advance together."""
    assert check_rpc_health.REBRAND_STATE_VERSION == 2
    assert {
        check_rpc_health.REBRAND_BATCHEXECUTE: RebrandProbeStatus.PRESENT.value,
        check_rpc_health.REBRAND_CHAT: RebrandProbeStatus.PRESENT.value,
    } == check_rpc_health.REBRAND_BASELINE_STATE


def test_rebrand_steady_state_reports_no_change() -> None:
    """Acknowledged PRESENT observations are steady state and file nothing."""
    probes = [
        _probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.PRESENT),
        _probe(check_rpc_health.REBRAND_CHAT, RebrandProbeStatus.PRESENT),
        check_rpc_health.rebrand_upload_probe(),
    ]
    state = build_rebrand_state("notebook.google.com", probes, load_rebrand_state(None))
    assert state["changed"] is False
    assert state["transitions"] == []


def test_rebrand_absent_to_present_is_a_state_change() -> None:
    probes = [_probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.PRESENT)]
    previous = {check_rpc_health.REBRAND_BATCHEXECUTE: RebrandProbeStatus.ABSENT.value}
    state = build_rebrand_state("notebook.google.com", probes, previous)
    assert state["changed"] is True
    assert state["transitions"] == [
        {
            "capability": check_rpc_health.REBRAND_BATCHEXECUTE,
            "from": "ABSENT",
            "to": "PRESENT",
            "detail": "detail",
        }
    ]


def test_rebrand_present_to_absent_is_a_state_change() -> None:
    probes = [_probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.ABSENT)]
    state = build_rebrand_state("notebook.google.com", probes, load_rebrand_state(None))
    assert state["changed"] is True
    assert state["transitions"] == [
        {
            "capability": check_rpc_health.REBRAND_BATCHEXECUTE,
            "from": "PRESENT",
            "to": "ABSENT",
            "detail": "detail",
        }
    ]
    assert "STATE CHANGE: batchexecute: PRESENT->ABSENT" in "\n".join(
        check_rpc_health.format_rebrand_lane(state, probes)
    )


def test_rebrand_present_stays_quiet_on_the_next_run() -> None:
    """The lane reports transitions, not a recurring condition."""
    probes = [_probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.PRESENT)]
    previous = {check_rpc_health.REBRAND_BATCHEXECUTE: "PRESENT"}
    state = build_rebrand_state("notebook.google.com", probes, previous)
    assert state["changed"] is False


@pytest.mark.parametrize(
    "status",
    [s for s in RebrandProbeStatus if s not in check_rpc_health.RECORDED_REBRAND_STATUSES],
)
def test_rebrand_unrecorded_statuses_carry_previous_state_forward(
    status: RebrandProbeStatus,
) -> None:
    """Parametrized over the enum: a new non-recorded verdict is covered on arrival."""
    previous = {check_rpc_health.REBRAND_BATCHEXECUTE: "PRESENT"}
    state = build_rebrand_state(
        "notebook.google.com",
        [RebrandProbe(check_rpc_health.REBRAND_BATCHEXECUTE, status, "flake")],
        previous,
    )
    assert state["changed"] is False
    assert state["state"][check_rpc_health.REBRAND_BATCHEXECUTE] == "PRESENT"


def test_rebrand_recorded_statuses_are_exactly_present_and_absent() -> None:
    """The recorded set is the state machine's contract; widening it is the bug.

    UNAUTHENTICATED in particular must stay out: recording it would let a night
    with a broken CI profile overwrite a PRESENT and file a false
    ``PRESENT->ABSENT``-shaped availability change (#2062).
    """
    assert {
        RebrandProbeStatus.PRESENT,
        RebrandProbeStatus.ABSENT,
    } == check_rpc_health.RECORDED_REBRAND_STATUSES


def test_rebrand_auth_failure_never_erases_a_recorded_present() -> None:
    """The whole point of the UNAUTHENTICATED verdict, end to end."""
    previous = {
        check_rpc_health.REBRAND_BATCHEXECUTE: "PRESENT",
        check_rpc_health.REBRAND_CHAT: "PRESENT",
    }
    probes = [
        RebrandProbe(
            check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.UNAUTHENTICATED, "401"
        ),
        RebrandProbe(
            check_rpc_health.REBRAND_CHAT, RebrandProbeStatus.UNAUTHENTICATED, "302 sign-in"
        ),
    ]
    state = build_rebrand_state("notebook.google.com", probes, previous)
    assert state["transitions"] == []
    assert state["changed"] is False
    assert state["state"] == previous
    # The observation is still reported — it is carried, not hidden.
    assert state["observed"][check_rpc_health.REBRAND_BATCHEXECUTE] == "UNAUTHENTICATED"


def test_rebrand_state_records_the_run_that_wrote_it() -> None:
    """The stamp the workflow matches against its own run id (#2062)."""
    probes = [_probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.ABSENT)]
    state = build_rebrand_state("notebook.google.com", probes, load_rebrand_state(None), "42")
    assert state["run_id"] == "42"
    # Unstamped by default, for local runs that have no run identity.
    assert (
        build_rebrand_state("notebook.google.com", probes, load_rebrand_state(None))["run_id"]
        is None
    )


def test_rebrand_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "rebrand-state.json"
    probes = [_probe(check_rpc_health.REBRAND_CHAT, RebrandProbeStatus.ABSENT)]
    state = build_rebrand_state("notebook.google.com", probes, load_rebrand_state(path))
    check_rpc_health.write_rebrand_state(path, state)
    assert load_rebrand_state(path)[check_rpc_health.REBRAND_CHAT] == "ABSENT"


@pytest.mark.parametrize("invalid_status", ["PRESNT", ["PRESENT"], {"value": "PRESENT"}])
def test_rebrand_state_ignores_unknown_capabilities_and_invalid_statuses(
    tmp_path: Path, invalid_status: object
) -> None:
    path = tmp_path / "rebrand-state.json"
    path.write_text(
        json.dumps(
            {
                "version": check_rpc_health.REBRAND_STATE_VERSION,
                "state": {
                    check_rpc_health.REBRAND_BATCHEXECUTE: invalid_status,
                    check_rpc_health.REBRAND_CHAT: "ABSENT",
                    "future-capability": "ABSENT",
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_rebrand_state(path) == {
        check_rpc_health.REBRAND_BATCHEXECUTE: "PRESENT",
        check_rpc_health.REBRAND_CHAT: "ABSENT",
    }


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        json.dumps({"version": 999, "state": {"batchexecute": "PRESENT"}}),
        # The prior baseline epoch must not replay its now-acknowledged ABSENT
        # values if an older cache entry is restored after #2077/#2078.
        json.dumps({"version": 1, "state": {"batchexecute": "ABSENT", "chat": "ABSENT"}}),
        json.dumps(
            {
                "version": check_rpc_health.REBRAND_STATE_VERSION,
                "state": "not-a-mapping",
            }
        ),
        json.dumps(["not", "a", "mapping"]),
    ],
)
def test_rebrand_unusable_state_file_degrades_to_baseline(tmp_path: Path, content: str) -> None:
    """A lost/corrupt cache falls back to acknowledged repository evidence."""
    path = tmp_path / "rebrand-state.json"
    path.write_text(content, encoding="utf-8")
    assert load_rebrand_state(path) == check_rpc_health.REBRAND_BASELINE_STATE


def test_rebrand_state_write_failure_is_not_fatal(tmp_path: Path) -> None:
    """The lane is advisory: an unwritable path must not take the canary down."""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    check_rpc_health.write_rebrand_state(blocker / "state.json", {"version": 1})


def test_format_rebrand_lane_lines() -> None:
    probes = [
        _probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.PRESENT),
        check_rpc_health.rebrand_upload_probe(),
    ]
    previous = {check_rpc_health.REBRAND_BATCHEXECUTE: RebrandProbeStatus.ABSENT.value}
    state = build_rebrand_state("notebook.google.com", probes, previous)
    lines = check_rpc_health.format_rebrand_lane(state, probes)
    joined = "\n".join(lines)
    assert "STATE CHANGE: batchexecute: ABSENT->PRESENT" in joined
    assert "NOT_PROBED" in joined
    assert all(line.startswith("REBRAND") for line in lines)


def test_format_rebrand_lane_flags_an_auth_failure_as_inconclusive() -> None:
    """An operator reading the report must not mistake it for a real ABSENT."""
    probes = [
        RebrandProbe(check_rpc_health.REBRAND_CHAT, RebrandProbeStatus.UNAUTHENTICATED, "HTTP 401")
    ]
    state = build_rebrand_state("notebook.google.com", probes, load_rebrand_state(None))
    joined = "\n".join(check_rpc_health.format_rebrand_lane(state, probes))
    assert "UNAUTHENTICATED" in joined
    assert "STATE CHANGE" not in joined
    assert "carried forward" in joined


def test_format_rebrand_lane_says_so_when_nothing_changed() -> None:
    probes = [_probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.PRESENT)]
    state = build_rebrand_state("notebook.google.com", probes, load_rebrand_state(None))
    joined = "\n".join(check_rpc_health.format_rebrand_lane(state, probes))
    assert "STATE CHANGE" not in joined
    assert "no state change" in joined


@pytest.mark.parametrize("status", list(RebrandProbeStatus))
def test_print_summary_exit_code_is_rebrand_independent(
    capsys: pytest.CaptureFixture[str], status: RebrandProbeStatus
) -> None:
    """Every rebrand verdict — including a changed one — still exits 0."""
    results = [_result("ok", CheckStatus.OK)]
    state = build_rebrand_state(
        "notebook.google.com",
        [RebrandProbe(check_rpc_health.REBRAND_BATCHEXECUTE, status, "d")],
        load_rebrand_state(None),
    )
    rc = print_summary(results, CustomizationStatus.MATCH, state)
    out = capsys.readouterr().out
    assert rc == 0
    assert "REBRAND:" in out
    assert "RESULT: PASS" in out


# --- --base-url ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://notebook.google.com",
        "https://notebooklm.google.com",
        "https://notebook.google.com/",
    ],
)
def test_base_url_override_accepts_personal_hosts(raw: str) -> None:
    assert check_rpc_health.validate_base_url_override(raw) == raw.rstrip("/")


@pytest.mark.parametrize(
    "raw",
    [
        "https://notebooklm.cloud.google.com",  # enterprise: different tenant model
        "http://notebook.google.com",  # not https
        "https://evil.example.com",
        "https://notebook.google.com:8443",
        "https://notebook.google.com/path",
        "https://notebook.google.com?q=1",
        "",
    ],
)
def test_base_url_override_rejects_everything_else(raw: str) -> None:
    with pytest.raises(ValueError):
        check_rpc_health.validate_base_url_override(raw)


def test_base_url_override_leaves_the_environment_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation only checks — applying the selection is a separate step."""
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    check_rpc_health.validate_base_url_override("https://notebook.google.com")
    assert "NOTEBOOKLM_BASE_URL" not in os.environ

    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    with pytest.raises(ValueError):
        check_rpc_health.validate_base_url_override("https://evil.example.com")
    assert os.environ["NOTEBOOKLM_BASE_URL"] == "https://notebooklm.google.com"


def test_apply_base_url_override_is_what_the_client_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from notebooklm._env import get_base_url

    # ``setenv`` (not ``delenv``) so monkeypatch owns the key and its teardown
    # removes it again: ``apply_base_url_override`` writes to the real process
    # environment, and a leak would re-point every later test's base host.
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    check_rpc_health.apply_base_url_override("https://notebook.google.com")
    assert get_base_url() == "https://notebook.google.com"


def test_describe_base_url_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    assert check_rpc_health.describe_base_url_source(False) == "default"
    assert check_rpc_health.describe_base_url_source(True) == "--base-url"
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    assert check_rpc_health.describe_base_url_source(False) == "NOTEBOOKLM_BASE_URL"


def test_main_rejects_a_bad_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad ``--base-url`` fails at argument parsing — before any auth load."""
    monkeypatch.setattr(
        sys, "argv", ["check_rpc_health.py", "--base-url", "https://evil.example.com"]
    )
    with pytest.raises(SystemExit) as excinfo:
        check_rpc_health.main()
    assert excinfo.value.code == 2


def test_main_threads_the_rebrand_state_into_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``main`` wires the lane end to end: flag -> run -> report, exit unchanged."""
    captured: dict[str, Any] = {}

    async def _fake_run(
        full_mode: bool = False,
        *,
        base_url_source: str = "default",
        rebrand_state_path: Path | None = None,
        rebrand_previous_state_path: Path | None = None,
        rebrand_run_id: str | None = None,
    ) -> tuple[list[CheckResult], CustomizationStatus, dict[str, Any], Any]:
        captured["base_url_source"] = base_url_source
        captured["rebrand_state_path"] = rebrand_state_path
        captured["rebrand_previous_state_path"] = rebrand_previous_state_path
        captured["rebrand_run_id"] = rebrand_run_id
        state = build_rebrand_state(
            "notebook.google.com",
            [_probe(check_rpc_health.REBRAND_BATCHEXECUTE, RebrandProbeStatus.PRESENT)],
            {check_rpc_health.REBRAND_BATCHEXECUTE: RebrandProbeStatus.ABSENT.value},
        )
        return (
            [_result("ok", CheckStatus.OK)],
            CustomizationStatus.MATCH,
            state,
            check_rpc_health.classify_build_label(check_rpc_health.DEFAULT_BL, ""),
        )

    monkeypatch.setattr(check_rpc_health, "run_health_check", _fake_run)
    # ``main`` applies ``--base-url`` to the real process environment; let
    # monkeypatch own the key so its teardown undoes that for later tests.
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    state_file = tmp_path / "rebrand-state.json"
    previous_file = tmp_path / "rebrand-state-prev.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_rpc_health.py",
            "--base-url",
            "https://notebook.google.com",
            "--rebrand-state-file",
            str(state_file),
            "--rebrand-previous-state-file",
            str(previous_file),
            "--rebrand-run-id",
            "12345",
        ],
    )

    rc = check_rpc_health.main()
    out = capsys.readouterr().out

    # A PRESENT rebrand state change does NOT move the exit code.
    assert rc == 0
    assert captured["base_url_source"] == "--base-url"
    assert captured["rebrand_state_path"] == state_file
    # Read and write paths stay separate, so a state file restored from cache
    # but never rewritten cannot be mistaken for this run's output (#2062).
    assert captured["rebrand_previous_state_path"] == previous_file
    assert captured["rebrand_run_id"] == "12345"
    assert "STATE CHANGE: batchexecute: ABSENT->PRESENT" in out


# ---------------------------------------------------------------------------
# Build-label lane (``bl`` / ``_env.DEFAULT_BL``)
#
# ``bl`` is a pinned constant with no other guard: cassettes replay whatever was
# recorded, so the offline suite passes no matter how old the pin gets, and the
# live streaming endpoint accepts arbitrary labels (measured 2026-08-04 — the
# pinned label, the served label, and a fabricated 1970 one all returned complete
# cited answers). #2073 found it five months / 154 label-days behind. These tests
# pin the lane's two jobs: read the served label honestly, and alarm only once the
# pin has actually been left unattended.
# ---------------------------------------------------------------------------

BuildLabelStatus = check_rpc_health.BuildLabelStatus
classify_build_label = check_rpc_health.classify_build_label
fetch_app_shell = check_rpc_health.fetch_app_shell
DEFAULT_BL = check_rpc_health.DEFAULT_BL

SHELL_HTML = '<!doctype html><script>var x="{label}";</script>'


def _label(days_after_pin: int) -> str:
    """Return a well-formed label ``days_after_pin`` days after the pinned one."""
    from datetime import timedelta

    from notebooklm._env import build_label_date

    pinned = build_label_date(DEFAULT_BL)
    assert pinned is not None
    stamp = (pinned + timedelta(days=days_after_pin)).strftime("%Y%m%d")
    return f"boq_labs-tailwind-frontend_{stamp}.01_p0"


class _ShellClient:
    """Minimal httpx-like client replaying a scripted sequence of GET responses."""

    def __init__(self, *responses: httpx.Response, raises: Exception | None = None):
        self._responses = list(responses)
        self._raises = raises
        self.urls: list[str] = []

    async def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        if self._raises is not None:
            raise self._raises
        response = self._responses.pop(0)
        # httpx responses need a request to report a URL; give them this one.
        response.request = httpx.Request("GET", url)
        return response


def _response(status: int, *, text: str = "", location: str | None = None) -> httpx.Response:
    headers = {"location": location} if location is not None else {}
    return httpx.Response(status, text=text, headers=headers)


@pytest.mark.asyncio
async def test_fetch_app_shell_returns_a_direct_200() -> None:
    client = _ShellClient(_response(200, text=SHELL_HTML.format(label=DEFAULT_BL)))
    html, detail = await fetch_app_shell(client, "https://notebook.google.com/")
    assert html is not None and DEFAULT_BL in html
    assert detail == ""


@pytest.mark.asyncio
async def test_fetch_app_shell_follows_the_rebrand_hop() -> None:
    """The measured shape: the legacy host 302s to the alias host's root."""
    client = _ShellClient(
        _response(302, location="https://notebook.google.com/"),
        _response(200, text=SHELL_HTML.format(label=DEFAULT_BL)),
    )
    html, _ = await fetch_app_shell(client, "https://notebooklm.google.com/")
    assert html is not None
    assert client.urls == ["https://notebooklm.google.com/", "https://notebook.google.com/"]


@pytest.mark.asyncio
async def test_fetch_app_shell_stops_at_the_sign_in_bounce() -> None:
    """A signed-out run draws no conclusion — and does not follow the bounce."""
    client = _ShellClient(
        _response(302, location="https://notebook.google.com/login?continue=https://x/")
    )
    html, detail = await fetch_app_shell(client, "https://notebook.google.com/")
    assert html is None
    assert "sign-in" in detail
    assert len(client.urls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://example.com/",  # off-host: never follow the jar there
        "https://notebook.google.com/app/notebook",  # app host, but not the shell
        # Right host, right path, cleartext: following it would put the jar on
        # the wire unencrypted for every cookie not marked Secure.
        "http://notebook.google.com/",
    ],
)
async def test_fetch_app_shell_refuses_a_hop_off_the_app_root(location: str) -> None:
    client = _ShellClient(_response(302, location=location))
    html, detail = await fetch_app_shell(client, "https://notebook.google.com/")
    assert html is None
    assert "not the app shell" in detail
    assert len(client.urls) == 1


@pytest.mark.asyncio
async def test_fetch_app_shell_gives_up_on_a_redirect_loop() -> None:
    loop = [_response(302, location="https://notebook.google.com/") for _ in range(4)]
    client = _ShellClient(*loop)
    html, detail = await fetch_app_shell(client, "https://notebook.google.com/")
    assert html is None
    assert "redirects" in detail


@pytest.mark.asyncio
async def test_fetch_app_shell_reports_a_transport_error() -> None:
    client = _ShellClient(raises=httpx.ConnectError("connection refused"))
    html, detail = await fetch_app_shell(client, "https://notebook.google.com/")
    assert html is None
    assert "connection refused" in detail


@pytest.mark.asyncio
async def test_fetch_app_shell_reports_a_server_error() -> None:
    client = _ShellClient(_response(503))
    html, detail = await fetch_app_shell(client, "https://notebook.google.com/")
    assert html is None
    assert "503" in detail


def test_classify_build_label_current_when_the_pin_matches() -> None:
    probe = classify_build_label(DEFAULT_BL, "")
    assert probe.status is BuildLabelStatus.CURRENT
    assert probe.days_behind == 0


def test_classify_build_label_drifted_inside_the_window() -> None:
    """Ordinary week-to-week drift is a PASS, not an alarm."""
    probe = classify_build_label(_label(7), "")
    assert probe.status is BuildLabelStatus.DRIFTED
    assert probe.days_behind == 7


def test_classify_build_label_stale_past_the_window() -> None:
    days = check_rpc_health.BUILD_LABEL_STALE_AFTER_DAYS + 1
    probe = classify_build_label(_label(days), "")
    assert probe.status is BuildLabelStatus.STALE
    assert probe.days_behind == days
    assert str(days) in probe.detail


def test_classify_build_label_boundary_day_is_not_yet_stale() -> None:
    """Exactly at the window is still inside it — STALE means *past* the limit."""
    probe = classify_build_label(_label(check_rpc_health.BUILD_LABEL_STALE_AFTER_DAYS), "")
    assert probe.status is BuildLabelStatus.DRIFTED


def test_classify_build_label_pin_ahead_is_not_stale() -> None:
    """A shell older than the pin is an odd cohort, never a staleness alarm."""
    probe = classify_build_label(_label(-200), "")
    assert probe.status is BuildLabelStatus.DRIFTED
    assert "AHEAD" in probe.detail


def test_classify_build_label_unknown_without_an_observation() -> None:
    probe = classify_build_label(None, "HTTP 302 redirect to sign-in (not signed in)")
    assert probe.status is BuildLabelStatus.UNKNOWN
    assert probe.served is None
    assert "sign-in" in probe.detail


def test_classify_build_label_unknown_when_the_served_label_is_undatable() -> None:
    """A reformatted label is 'no conclusion', never a fabricated staleness."""
    probe = classify_build_label("boq_labs-tailwind-frontend_YYYYMMDD.02_p0", "")
    assert probe.status is BuildLabelStatus.UNKNOWN


@pytest.mark.asyncio
async def test_check_build_label_reads_the_live_shell() -> None:
    served = _label(3)
    client = _ShellClient(_response(200, text=SHELL_HTML.format(label=served)))
    probe = await check_rpc_health.check_build_label(client)
    assert probe.served == served
    assert probe.status is BuildLabelStatus.DRIFTED


@pytest.mark.asyncio
async def test_check_build_label_never_raises() -> None:
    """The lowest-severity lane must not be able to skip the one that follows it.

    ``check_build_label`` runs immediately before the rebrand-host lane, so an
    escaping exception would cost that night's rebrand observation. Anything
    unexpected degrades to UNKNOWN, which alarms nothing.
    """
    client = _ShellClient(raises=RuntimeError("something nobody predicted"))
    probe = await check_rpc_health.check_build_label(client)
    assert probe.status is BuildLabelStatus.UNKNOWN
    assert "something nobody predicted" in probe.detail


@pytest.mark.asyncio
async def test_check_build_label_is_unknown_on_a_shell_without_a_label() -> None:
    client = _ShellClient(_response(200, text="<html>maintenance</html>"))
    probe = await check_rpc_health.check_build_label(client)
    assert probe.status is BuildLabelStatus.UNKNOWN
    assert probe.served is None


def test_compute_exit_code_stale_build_label_is_five() -> None:
    """STALE draws exit 5 — below every live-breakage code, above OK."""
    clean = Counter({CheckStatus.OK: 3})
    assert (
        check_rpc_health.compute_exit_code(
            clean, [], CustomizationStatus.MATCH, BuildLabelStatus.STALE
        )
        == 5
    )
    for benign in (BuildLabelStatus.CURRENT, BuildLabelStatus.DRIFTED, BuildLabelStatus.UNKNOWN):
        assert check_rpc_health.compute_exit_code(clean, [], CustomizationStatus.MATCH, benign) == 0
    # Every live-breakage signal outranks a stale pin: it must never mask one.
    mismatch = Counter({CheckStatus.MISMATCH: 1})
    assert (
        check_rpc_health.compute_exit_code(
            mismatch, [], CustomizationStatus.MATCH, BuildLabelStatus.STALE
        )
        == 1
    )
    real_error = [_result("e", CheckStatus.ERROR, error="Parse error: boom")]
    assert (
        check_rpc_health.compute_exit_code(
            Counter(), real_error, CustomizationStatus.MATCH, BuildLabelStatus.STALE
        )
        == 3
    )
    assert (
        check_rpc_health.compute_exit_code(
            clean, [], CustomizationStatus.DRIFT, BuildLabelStatus.STALE
        )
        == 4
    )


def test_compute_exit_code_defaults_to_no_build_label_opinion() -> None:
    """Callers that pass no verdict cannot be alarmed by one."""
    assert check_rpc_health.compute_exit_code(Counter({CheckStatus.OK: 1}), []) == 0


def test_print_summary_reports_a_stale_build_label(capsys: pytest.CaptureFixture[str]) -> None:
    stale = classify_build_label(_label(check_rpc_health.BUILD_LABEL_STALE_AFTER_DAYS + 30), "")
    rc = print_summary([_result("ok", CheckStatus.OK)], CustomizationStatus.MATCH, None, stale)
    out = capsys.readouterr().out
    assert rc == 5
    assert "BUILDLBL: STALE" in out
    assert "RESULT: STALE BUILD LABEL" in out
    # The report must name the file to edit, not just the fact of drift.
    assert "src/notebooklm/_env.py" in out


def test_print_summary_drift_inside_the_window_still_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    drifted = classify_build_label(_label(10), "")
    rc = print_summary([_result("ok", CheckStatus.OK)], CustomizationStatus.MATCH, None, drifted)
    out = capsys.readouterr().out
    assert rc == 0
    assert "BUILDLBL: DRIFTED" in out
    assert "RESULT: PASS" in out


def test_print_summary_without_a_build_label_verdict_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = print_summary([_result("ok", CheckStatus.OK)], CustomizationStatus.MATCH)
    out = capsys.readouterr().out
    assert rc == 0
    assert "BUILDLBL" not in out


def test_format_build_label_lane_flags_an_active_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator override must not be mistaken for what ships to users."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "boq_labs-tailwind-frontend_20990101.01_p0")
    lines = check_rpc_health.format_build_label_lane(classify_build_label(DEFAULT_BL, ""))
    joined = "\n".join(lines)
    assert "NOTEBOOKLM_BL=" in joined
    assert "committed pin" in joined


def test_format_build_label_lane_says_what_to_do_when_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTEBOOKLM_BL", raising=False)
    stale = classify_build_label(_label(check_rpc_health.BUILD_LABEL_STALE_AFTER_DAYS + 1), "")
    joined = "\n".join(check_rpc_health.format_build_label_lane(stale))
    assert "ACTION" in joined
    assert "DEFAULT_BL" in joined
    assert "NOTEBOOKLM_BL=" not in joined
