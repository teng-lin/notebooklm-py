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
AUTH (2) is signalled earlier via ``sys.exit(2)`` and is exercised in the
auth-failure test by invoking ``main()`` with a missing storage env var.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

# Load scripts/check_rpc_health.py as a module. The ``scripts`` directory
# is not a package, so we go through importlib rather than a normal import.
# Registering the module in ``sys.modules`` before executing it is required
# so that ``@dataclass`` can resolve forward references back to this module
# during class construction.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_rpc_health.py"
_spec = importlib.util.spec_from_file_location("check_rpc_health", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_rpc_health = importlib.util.module_from_spec(_spec)
sys.modules["check_rpc_health"] = check_rpc_health
_spec.loader.exec_module(check_rpc_health)


CheckStatus = check_rpc_health.CheckStatus
CheckResult = check_rpc_health.CheckResult
compute_exit_code = check_rpc_health.compute_exit_code
is_transient_error = check_rpc_health.is_transient_error
partition_errors = check_rpc_health.partition_errors
print_summary = check_rpc_health.print_summary
setup_temp_resources = check_rpc_health.setup_temp_resources
make_rpc_request = check_rpc_health.make_rpc_request


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
async def test_make_rpc_request_uses_flat_cookie_header_and_auth_route() -> None:
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
    cookie_header = captured["headers"]["Cookie"]
    assert "SID=sid" in cookie_header
    assert "__Secure-1PSIDTS=ts" in cookie_header
    assert "('SID'," not in cookie_header
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

    def __init__(self, *, text: str = "", status: int = 200, raises: Exception | None = None):
        self._text = text
        self._status = status
        self._raises = raises
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
# main() auth-failure path -> exit 2
# ---------------------------------------------------------------------------


def test_main_exits_two_when_auth_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ``NOTEBOOKLM_AUTH_JSON`` must surface as exit code 2.

    Even if the developer running the test happens to have a local
    ``~/.notebooklm/storage_state.json``, we patch the loader to simulate
    a fresh CI environment with no credentials available.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setattr("sys.argv", ["check_rpc_health.py"])

    def _missing() -> dict[str, str]:
        raise FileNotFoundError("simulated missing storage_state.json")

    monkeypatch.setattr(check_rpc_health, "load_auth_from_storage", _missing)

    with pytest.raises(SystemExit) as excinfo:
        check_rpc_health.main()
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# sqTeoe studio-customization cohort tripwire
# ---------------------------------------------------------------------------

CohortStatus = check_rpc_health.CohortStatus
build_customization_choices_params = check_rpc_health.build_customization_choices_params
check_customization_cohort = check_rpc_health.check_customization_cohort
make_raw_rpc_request = check_rpc_health.make_raw_rpc_request


def _cohort_auth() -> check_rpc_health.AuthTokens:
    return check_rpc_health.AuthTokens(
        cookies={"SID": "sid"},
        csrf_token="csrf",
        session_id="session",
    )


def test_build_customization_choices_params_shape() -> None:
    """The reverse-engineered ``[nbctx, None, artifact_type]`` shape is verified."""
    params = build_customization_choices_params("nb_42")
    assert params == [
        [
            2,
            None,
            None,
            [1, None, None, None, None, None, None, None, None, None, [1]],
            ["nb_42"],
        ],
        None,
        3,  # artifact_type = video
    ]


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
        _cohort_auth(),
        "sqTeoe",
        [["x"]],
        source_path="/notebook/nb_1",
    )
    assert error is None
    assert text == ")]}'\n\n[]"
    query = parse_qs(urlparse(captured["url"]).query)
    assert query["rpcids"] == ["sqTeoe"]
    assert query["source-path"] == ["/notebook/nb_1"]
    # The body's f.req carries the override id, kept in sync with the query param.
    assert "sqTeoe" in captured["content"]
    # The fallback RPCMethod (LIST_NOTEBOOKS) must NOT leak into the wire.
    assert check_rpc_health.RPCMethod.LIST_NOTEBOOKS.value not in captured["content"]


@pytest.mark.asyncio
async def test_check_customization_cohort_gated_on_null() -> None:
    """A genuine null payload is GATED (the expected steady state, NOT a failure)."""

    class NullClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            # wrb.fr frame whose payload is JSON null -> decode_response(..., allow_null) == None
            body = ')]}\'\n\n[["wrb.fr","sqTeoe","null",null,null,null,"generic"]]'
            return httpx.Response(200, text=body, request=httpx.Request("POST", url))

    status, detail = await check_customization_cohort(NullClient(), _cohort_auth(), "nb_1")
    assert status is CohortStatus.GATED
    assert "gated" in detail.lower()


@pytest.mark.asyncio
async def test_check_customization_cohort_migrated_on_non_null() -> None:
    """A non-null payload is the loud MIGRATED signal (cohort flipped)."""

    class MigratedClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            body = ')]}\'\n\n[["wrb.fr","sqTeoe","[[1,2,3]]",null,null,null,"generic"]]'
            return httpx.Response(200, text=body, request=httpx.Request("POST", url))

    status, _ = await check_customization_cohort(MigratedClient(), _cohort_auth(), "nb_1")
    assert status is CohortStatus.MIGRATED


@pytest.mark.asyncio
async def test_check_customization_cohort_unknown_on_transport_error() -> None:
    """A transport failure never alarms; it draws UNKNOWN (no cohort conclusion)."""

    class FailingClient:
        async def post(self, url: str, *, content: str, headers: dict[str, str]) -> httpx.Response:
            raise httpx.ReadTimeout("")

    status, detail = await check_customization_cohort(FailingClient(), _cohort_auth(), "nb_1")
    assert status is CohortStatus.UNKNOWN
    assert detail == "ReadTimeout"


@pytest.mark.asyncio
async def test_check_customization_cohort_unknown_without_notebook() -> None:
    status, _ = await check_customization_cohort(object(), _cohort_auth(), None)
    assert status is CohortStatus.UNKNOWN


def test_compute_exit_code_cohort_migrated_is_four() -> None:
    """MIGRATED draws exit 4 when no higher-priority drift fired."""
    clean = Counter({CheckStatus.OK: 3})
    assert check_rpc_health.compute_exit_code(clean, [], CohortStatus.MIGRATED) == 4
    assert check_rpc_health.compute_exit_code(clean, [], CohortStatus.GATED) == 0
    # RPC drift outranks the cohort flip.
    mismatch = Counter({CheckStatus.MISMATCH: 1})
    assert check_rpc_health.compute_exit_code(mismatch, [], CohortStatus.MIGRATED) == 1
    real_error = [_result("e", CheckStatus.ERROR, error="Parse error: boom")]
    assert check_rpc_health.compute_exit_code(Counter(), real_error, CohortStatus.MIGRATED) == 3


def test_print_summary_reports_cohort_flip(capsys: pytest.CaptureFixture[str]) -> None:
    results = [_result("ok", CheckStatus.OK)]
    rc = print_summary(results, CohortStatus.MIGRATED)
    out = capsys.readouterr().out
    assert rc == 4
    assert "COHORT FLIP" in out
    assert "sqTeoe customization choices = MIGRATED" in out


def test_print_summary_gated_is_pass(capsys: pytest.CaptureFixture[str]) -> None:
    results = [_result("ok", CheckStatus.OK)]
    rc = print_summary(results, CohortStatus.GATED)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sqTeoe customization choices = GATED" in out
    assert "RESULT: PASS" in out
