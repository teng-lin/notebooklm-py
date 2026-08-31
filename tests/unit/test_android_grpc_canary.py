"""Unit tests for ``scripts/android_grpc_canary.py``.

The canary is exercised two ways, neither touching the network or a profile:

* the pure protobuf helpers (fingerprint, unknown-field count, baseline
  parsing) on hand-built messages and documents, and
* the full ``run_canary`` flow over a real ``AndroidSession`` talking to an
  in-process ``grpc.aio`` server (same pattern as
  ``tests/unit/android/test_chat_fake_server.py``), so the schema step sees
  genuinely deserialized responses — including ones with trailing unknown
  bytes injected by the fake server's serializer.

Drift is judged against a baseline file, never against zero unknown fields:
the recovered app protos do not declare every field Google sends, so a
non-zero unknown count is the live steady state (38 on ``GetProject`` when
this was written).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from notebooklm._android.auth import BearerCredential, BearerProvider
from notebooklm._android.chat import AndroidChatAPI
from notebooklm._android.notebooks import AndroidNotebooksAPI
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    read_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.mint_service import MintedOAuthToken
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.exceptions import AuthError
from scripts import android_grpc_canary as canary

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
NOTEBOOK_ID = "canary-notebook-7f3a"
# Field 2047, varint, value 1 — a tag no generated message declares.
UNKNOWN_FIELD_BYTES = bytes([0xF8, 0x7F, 0x01])
ANDROID_BACKENDS = {"notebooks": "android", "chat": "android", "sources": "android"}
ZERO_SHAPE = "0" * 64


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Bearer:
    """Generation-aware fake: ``invalidate`` forces the next ``get`` to re-mint."""

    def __init__(self) -> None:
        self.generation = 0
        self.invalidated: list[int] = []
        self._cached: BearerCredential | None = None

    async def activate(self, epoch: int) -> None:
        self.epoch = epoch

    async def get(self, expected_epoch: int) -> BearerCredential:
        assert expected_epoch == self.epoch
        if self._cached is None:
            self.generation += 1
            self._cached = BearerCredential("fake-server-token", self.generation)
        return self._cached

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)
        if self._cached is not None and self._cached.generation == generation:
            self._cached = None

    async def prepare_close(self) -> None:
        return None


class _Sources:
    async def list(self, notebook_id: str) -> list[Any]:
        return []


class _SourceIds:
    async def get_source_ids(self, notebook_id: str) -> list[str]:
        return []


class _Service:
    """Fake orchestration service for the two read-only canary RPCs.

    ``GetProject`` answers with the full recovered ``read_pb2.GetProjectResponse``
    — the same bytes the public ``notebooks.get`` decodes through its partial
    ``Wire*`` projection and the raw probe decodes in full.
    """

    def __init__(
        self,
        *,
        project_unknown_bytes: bytes = b"",
        sessions_unknown_bytes: bytes = b"",
        abort_get_project: tuple[grpc.StatusCode, str] | None = None,
        with_session: bool = True,
        with_emoji: bool = False,
    ) -> None:
        self.project_unknown_bytes = project_unknown_bytes
        self.sessions_unknown_bytes = sessions_unknown_bytes
        self.abort_get_project = abort_get_project
        self.with_session = with_session
        self.with_emoji = with_emoji
        self.get_project_calls = 0
        self.list_sessions_calls = 0

    async def get_project(self, request: Any, context: Any) -> Any:
        self.get_project_calls += 1
        if self.abort_get_project is not None:
            code, details = self.abort_get_project
            await context.abort(code, details)
        project = read_pb2.Project(id=request.project_id, title="Canary")
        if self.with_emoji:
            project.emoji = "x"
        return read_pb2.GetProjectResponse(project=project)

    async def list_sessions(self, request: Any, context: Any) -> Any:
        del context
        self.list_sessions_calls += 1
        if not self.with_session:
            return chat_pb2.ListChatSessionsResponse()
        return chat_pb2.ListChatSessionsResponse(
            sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
        )

    def handler(self) -> Any:
        def serialize_project(message: Any) -> bytes:
            return message.SerializeToString() + self.project_unknown_bytes

        def serialize_sessions(message: Any) -> bytes:
            return message.SerializeToString() + self.sessions_unknown_bytes

        return grpc.method_handlers_generic_handler(
            _SERVICE,
            {
                "GetProject": grpc.unary_unary_rpc_method_handler(
                    self.get_project,
                    request_deserializer=read_pb2.GetProjectRequest.FromString,
                    response_serializer=serialize_project,
                ),
                "ListChatSessions": grpc.unary_unary_rpc_method_handler(
                    self.list_sessions,
                    request_deserializer=chat_pb2.ListChatSessionsRequest.FromString,
                    response_serializer=serialize_sessions,
                ),
            },
        )


@asynccontextmanager
async def _running_client(service: _Service, bearer: _Bearer) -> AsyncIterator[Any]:
    """A client-shaped namespace wired to a real session over a fake gRPC server."""
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((service.handler(),))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    def secure_channel(_target: str, _credentials: object, *, options: Any) -> Any:
        return grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=options)

    grpc_loader = SimpleNamespace(
        ssl_channel_credentials=lambda: object(),
        aio=SimpleNamespace(secure_channel=secure_channel),
    )
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=2,
    )
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    session = AndroidSession(
        bearer,  # type: ignore[arg-type]
        supervisor,
        timeout=2.0,
        grpc_loader=lambda: grpc_loader,
    )
    opened = False
    # Everything after ``server.start()`` sits inside the try so a setup
    # failure (session open, API construction) still stops the server.
    try:
        session.set_bound_loop(loop)
        session.reset_after_open()
        await session.open(loop, 1)
        opened = True
        notebooks = AndroidNotebooksAPI(session, _Sources())
        chat = AndroidChatAPI(
            session=session,
            loop_guard=supervisor,
            notebooks=_SourceIds(),
            chat_timeout=2.0,
        )
        client = SimpleNamespace(
            backends=dict(ANDROID_BACKENDS),
            _android_session=session,
            _android_bearer_provider=bearer,
            notebooks=notebooks,
            chat=chat,
        )
        yield client
    finally:
        if opened:
            await session.prepare_close()
            await session.close_resources()
        await server.stop(0)


def _factory(service: _Service, bearer: _Bearer) -> canary.ClientFactory:
    return lambda: _running_client(service, bearer)


async def _run(
    service: _Service,
    bearer: _Bearer | None = None,
    *,
    baseline_path: Path | None = None,
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await canary.run_canary(
        _factory(service, bearer or _Bearer()),
        NOTEBOOK_ID,
        baseline_path=baseline_path,
        out=lines.append,
    )
    return code, lines


def _line(lines: list[str], prefix: str) -> str:
    matches = [line for line in lines if line.startswith(prefix)]
    assert len(matches) == 1, (prefix, lines)
    return matches[0]


def _observed(lines: list[str]) -> dict[str, dict[str, Any]]:
    """Read the SHAPE/UNKNOWN diagnostics back into baseline-document form."""
    document: dict[str, dict[str, Any]] = {}
    for line in lines:
        kind, _, rest = line.partition(" ")
        if kind == "SHAPE":
            rpc, _, digest = rest.partition(" ")
            document.setdefault(rpc, {})["shape"] = digest
        elif kind == "UNKNOWN":
            rpc, _, count = rest.partition(" ")
            document.setdefault(rpc, {})["unknown_fields"] = int(count)
    return document


def _write_baseline(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "canary_baseline.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Full flow over the fake server — diagnostic mode (no baseline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_steps_pass_exits_zero() -> None:
    service = _Service()
    bearer = _Bearer()
    code, lines = await _run(service, bearer)

    assert code == 0
    assert _line(lines, "OK open") == "OK open backends=android"
    assert _line(lines, "OK bearer") == "OK bearer generation 1->2"
    assert _line(lines, "OK get_project") == "OK get_project id round-trip"
    assert _line(lines, "OK list_chat_sessions") == "OK list_chat_sessions conversation=present"
    assert _line(lines, "UNKNOWN GetProject") == "UNKNOWN GetProject 0"
    assert _line(lines, "UNKNOWN ListChatSessions") == "UNKNOWN ListChatSessions 0"
    assert _line(lines, "OK schema GetProject") == (
        "OK schema GetProject unknown_fields=0 (no baseline; diagnostic only)"
    )
    assert _line(lines, "OK schema ListChatSessions") == (
        "OK schema ListChatSessions unknown_fields=0 (no baseline; diagnostic only)"
    )
    assert not [line for line in lines if line.startswith(("FAIL", "WARN"))]
    # One raw probe per RPC on top of the public call.
    assert service.get_project_calls == 2
    assert service.list_sessions_calls == 2
    assert bearer.invalidated == [1]
    assert bearer.generation == 2


@pytest.mark.asyncio
async def test_output_carries_hashes_but_no_ids_or_tokens() -> None:
    service = _Service()
    _code, lines = await _run(service)

    shape_project = _line(lines, "SHAPE GetProject ")
    shape_sessions = _line(lines, "SHAPE ListChatSessions ")
    for line in (shape_project, shape_sessions):
        digest = line.split(" ")[2]
        assert len(digest) == 64
        assert int(digest, 16) >= 0
    joined = "\n".join(lines)
    assert NOTEBOOK_ID not in joined
    assert "conversation-1" not in joined
    assert "fake-server-token" not in joined
    assert "Canary" not in joined


@pytest.mark.asyncio
async def test_absent_conversation_is_still_a_pass() -> None:
    code, lines = await _run(_Service(with_session=False))

    assert code == 0
    assert _line(lines, "OK list_chat_sessions") == "OK list_chat_sessions conversation=absent"


@pytest.mark.asyncio
async def test_unknown_fields_are_diagnostic_without_a_baseline() -> None:
    """A non-zero unknown count alone never fails: it is the live steady state."""
    service = _Service(
        project_unknown_bytes=UNKNOWN_FIELD_BYTES,
        sessions_unknown_bytes=UNKNOWN_FIELD_BYTES * 2,
    )
    code, lines = await _run(service)

    assert code == 0
    # The public decode tolerates unknown fields...
    assert _line(lines, "OK get_project")
    # ...and the strict probe reports them without judging.
    assert _line(lines, "UNKNOWN GetProject") == "UNKNOWN GetProject 1"
    assert _line(lines, "UNKNOWN ListChatSessions") == "UNKNOWN ListChatSessions 2"
    assert _line(lines, "OK schema GetProject") == (
        "OK schema GetProject unknown_fields=1 (no baseline; diagnostic only)"
    )
    assert not [line for line in lines if line.startswith("FAIL")]


@pytest.mark.asyncio
async def test_raw_get_project_probe_uses_the_full_recovered_schema() -> None:
    probes = {rpc: response_type for rpc, _m, _r, response_type in canary.raw_probes("nb")}
    assert probes["GetProject"] is read_pb2.GetProjectResponse
    assert probes["GetProject"] is not wire_pb2.WireGetProjectResponse
    assert probes["ListChatSessions"] is chat_pb2.ListChatSessionsResponse
    request = next(r for rpc, _m, r, _t in canary.raw_probes("nb") if rpc == "GetProject")
    assert request == read_pb2.GetProjectRequest(project_id="nb", include_audio_overview_ids=True)


def test_raw_probes_use_the_guarded_modules_method_constants() -> None:
    """The canary must not re-derive method paths it exists to guard."""
    from notebooklm._android import chat as android_chat
    from notebooklm._android import sources as android_sources

    methods = {rpc: method for rpc, method, _r, _t in canary.raw_probes("nb")}
    assert methods == {
        "GetProject": android_sources.GET_PROJECT_METHOD,
        "ListChatSessions": android_chat.LIST_CHAT_SESSIONS_METHOD,
    }
    assert canary.GET_PROJECT_METHOD is android_sources.GET_PROJECT_METHOD
    assert canary.LIST_CHAT_SESSIONS_METHOD is android_chat.LIST_CHAT_SESSIONS_METHOD
    # And the fake server above registers handlers under the very same service.
    fake_server_method = f"/{_SERVICE}/GetProject"
    assert fake_server_method == android_sources.GET_PROJECT_METHOD


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_match_exits_zero(tmp_path: Path) -> None:
    service = _Service(project_unknown_bytes=UNKNOWN_FIELD_BYTES)
    _code, diagnostic = await _run(service)
    baseline = _write_baseline(tmp_path, _observed(diagnostic))

    code, lines = await _run(service, baseline_path=baseline)

    assert code == 0
    assert (
        _line(lines, "OK schema GetProject") == "OK schema GetProject shape=match unknown_fields=1"
    )
    assert _line(lines, "OK schema ListChatSessions") == (
        "OK schema ListChatSessions shape=match unknown_fields=0"
    )
    assert not [line for line in lines if line.startswith(("FAIL", "WARN"))]
    # Diagnostics are still printed alongside the verdict.
    assert _line(lines, "SHAPE GetProject ")
    assert _line(lines, "UNKNOWN GetProject") == "UNKNOWN GetProject 1"


@pytest.mark.asyncio
async def test_shape_mismatch_fails_with_both_values(tmp_path: Path) -> None:
    service = _Service()
    _code, diagnostic = await _run(service)
    document = _observed(diagnostic)
    live_shape = document["GetProject"]["shape"]
    document["GetProject"]["shape"] = ZERO_SHAPE
    baseline = _write_baseline(tmp_path, document)

    code, lines = await _run(service, baseline_path=baseline)

    assert code == 1
    assert _line(lines, "FAIL schema GetProject") == (
        f"FAIL schema GetProject shape live={live_shape} baseline={ZERO_SHAPE}"
    )
    assert _line(lines, "OK schema ListChatSessions")


@pytest.mark.asyncio
async def test_live_shape_change_is_caught_by_a_stale_baseline(tmp_path: Path) -> None:
    """The realistic drift case: the server adds a field the baseline never saw."""
    _code, diagnostic = await _run(_Service())
    baseline = _write_baseline(tmp_path, _observed(diagnostic))

    code, lines = await _run(_Service(with_emoji=True), baseline_path=baseline)

    assert code == 1
    failure = _line(lines, "FAIL schema GetProject")
    assert failure.startswith("FAIL schema GetProject shape live=")
    assert "unknown_fields" not in failure
    assert _line(lines, "OK schema ListChatSessions")


@pytest.mark.asyncio
async def test_unknown_count_mismatch_fails_with_both_values(tmp_path: Path) -> None:
    service = _Service(project_unknown_bytes=UNKNOWN_FIELD_BYTES)
    _code, diagnostic = await _run(service)
    document = _observed(diagnostic)
    document["GetProject"]["unknown_fields"] = 38
    baseline = _write_baseline(tmp_path, document)

    code, lines = await _run(service, baseline_path=baseline)

    assert code == 1
    assert _line(lines, "FAIL schema GetProject") == (
        "FAIL schema GetProject unknown_fields live=1 baseline=38"
    )


@pytest.mark.asyncio
async def test_shape_and_count_mismatch_are_both_reported(tmp_path: Path) -> None:
    service = _Service()
    _code, diagnostic = await _run(service)
    document = _observed(diagnostic)
    live_shape = document["ListChatSessions"]["shape"]
    document["ListChatSessions"] = {"shape": ZERO_SHAPE, "unknown_fields": 3}
    baseline = _write_baseline(tmp_path, document)

    code, lines = await _run(service, baseline_path=baseline)

    assert code == 1
    assert _line(lines, "FAIL schema ListChatSessions") == (
        f"FAIL schema ListChatSessions shape live={live_shape} baseline={ZERO_SHAPE}; "
        "unknown_fields live=0 baseline=3"
    )
    assert _line(lines, "OK schema GetProject")


@pytest.mark.asyncio
async def test_rpc_missing_from_baseline_fails(tmp_path: Path) -> None:
    service = _Service()
    _code, diagnostic = await _run(service)
    document = _observed(diagnostic)
    del document["ListChatSessions"]
    baseline = _write_baseline(tmp_path, document)

    code, lines = await _run(service, baseline_path=baseline)

    assert code == 1
    assert _line(lines, "OK schema GetProject")
    assert _line(lines, "FAIL schema ListChatSessions") == (
        "FAIL schema ListChatSessions no baseline entry for this RPC"
    )


@pytest.mark.asyncio
async def test_missing_baseline_file_warns_and_exits_zero(tmp_path: Path) -> None:
    missing = tmp_path / "fixtures" / "canary_baseline.json"
    service = _Service(project_unknown_bytes=UNKNOWN_FIELD_BYTES)

    code, lines = await _run(service, baseline_path=missing)

    assert code == 0
    assert lines[0] == f"WARN baseline missing {missing}"
    assert _line(lines, "OK schema GetProject") == (
        "OK schema GetProject unknown_fields=1 (no baseline; diagnostic only)"
    )
    assert not [line for line in lines if line.startswith("FAIL")]
    # The canary never authors the baseline itself.
    assert not missing.exists()
    assert not missing.parent.exists()


@pytest.mark.asyncio
async def test_malformed_baseline_fails_but_still_probes(tmp_path: Path) -> None:
    broken = tmp_path / "canary_baseline.json"
    broken.write_text("{not json", encoding="utf-8")

    code, lines = await _run(_Service(), baseline_path=broken)

    assert code == 1
    assert lines[0].startswith("FAIL baseline JSONDecodeError: ")
    # Probes still run in diagnostic mode so the log stays useful.
    assert _line(lines, "SHAPE GetProject ")
    assert _line(lines, "OK schema GetProject")


def test_parse_baseline_accepts_the_documented_shape() -> None:
    parsed = canary.parse_baseline(
        {
            "GetProject": {"shape": "A" * 64, "unknown_fields": 38},
            "ListChatSessions": {"shape": "b" * 64, "unknown_fields": 0},
        }
    )
    assert parsed == {
        "GetProject": canary.BaselineEntry(shape="a" * 64, unknown_fields=38),
        "ListChatSessions": canary.BaselineEntry(shape="b" * 64, unknown_fields=0),
    }


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"GetProject": "abc"},
        {"GetProject": {"unknown_fields": 1}},
        {"GetProject": {"shape": "short", "unknown_fields": 1}},
        {"GetProject": {"shape": "a" * 64}},
        {"GetProject": {"shape": "a" * 64, "unknown_fields": -1}},
        {"GetProject": {"shape": "a" * 64, "unknown_fields": True}},
        {"GetProject": {"shape": "a" * 64, "unknown_fields": "38"}},
    ],
)
def test_parse_baseline_rejects_malformed_documents(document: object) -> None:
    with pytest.raises(ValueError, match="baseline"):
        canary.parse_baseline(document)


# ---------------------------------------------------------------------------
# Transport / auth failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "public_error", "raw_error"),
    [
        # Plain transport failure: the library message never embeds the id.
        (grpc.StatusCode.UNAVAILABLE, "ServerError", "ServerError"),
        # The most likely real failure: ``map_get_project_error`` turns NOT_FOUND
        # into ``NotebookNotFoundError``, whose message spells the id out. The
        # raw probe skips that mapping and surfaces the bare ``RPCError``.
        (grpc.StatusCode.NOT_FOUND, "NotebookNotFoundError", "RPCError"),
    ],
)
async def test_transport_error_fails_with_class_only_and_no_leak(
    status: grpc.StatusCode, public_error: str, raw_error: str
) -> None:
    secret_detail = "upstream-detail-ya29.SECRET-should-never-print"
    service = _Service(abort_get_project=(status, secret_detail))
    code, lines = await _run(service)

    assert code == 1
    failure = _line(lines, "FAIL get_project")
    assert failure.startswith(f"FAIL get_project {public_error}: ")
    schema_failure = _line(lines, "FAIL schema GetProject")
    assert schema_failure.startswith(f"FAIL schema GetProject {raw_error}: ")
    joined = "\n".join(lines)
    assert secret_detail not in joined
    assert "SECRET" not in joined
    assert "Traceback" not in joined
    assert NOTEBOOK_ID not in joined
    if status is grpc.StatusCode.NOT_FOUND:
        assert failure.startswith(
            "FAIL get_project NotebookNotFoundError: Notebook not found: <notebook-id>"
        )
    # The independent RPC still ran and passed.
    assert _line(lines, "OK list_chat_sessions")
    assert _line(lines, "OK schema ListChatSessions")


def test_report_redacts_the_notebook_id_on_every_line_kind() -> None:
    lines: list[str] = []
    report = canary.CanaryReport(lines.append, redact=NOTEBOOK_ID)
    report.ok("step", f"echoed {NOTEBOOK_ID}")
    report.fail("step", f"Notebook not found: {NOTEBOOK_ID} — {NOTEBOOK_ID}")
    report.shape(NOTEBOOK_ID, "a" * 64)
    report.unknown(NOTEBOOK_ID, 1)
    report.warn(f"baseline missing /tmp/{NOTEBOOK_ID}/b.json")

    assert lines == [
        "OK step echoed <notebook-id>",
        "FAIL step Notebook not found: <notebook-id> — <notebook-id>",
        "SHAPE <notebook-id> " + "a" * 64,
        "UNKNOWN <notebook-id> 1",
        "WARN baseline missing /tmp/<notebook-id>/b.json",
    ]
    assert NOTEBOOK_ID not in "\n".join(lines)
    # An empty redaction token is a no-op rather than a replace-everything bug.
    plain: list[str] = []
    canary.CanaryReport(plain.append).ok("step", "kept")
    assert plain == ["OK step kept"]


@pytest.mark.asyncio
async def test_stalled_bearer_refresh_is_reported_not_raised() -> None:
    class _StuckBearer(_Bearer):
        def invalidate(self, generation: int) -> None:
            # Swallow the invalidation so the second ``get`` returns the cached
            # credential and the generation never moves.
            self.invalidated.append(generation)

    bearer = _StuckBearer()
    code, lines = await _run(_Service(), bearer)

    assert code == 1
    assert _line(lines, "FAIL bearer") == (
        "FAIL bearer RuntimeError: forced refresh did not advance the bearer generation (1 -> 1)"
    )
    # The remaining steps still execute against the (still valid) cached bearer.
    assert _line(lines, "OK get_project")
    assert _line(lines, "OK schema GetProject")


# ---------------------------------------------------------------------------
# Forced refresh through the real BearerProvider
# ---------------------------------------------------------------------------


class _Reader:
    def read_master_token(self) -> MasterToken:
        return MasterToken(email="canary@example.com", android_id="abc", secret="aas_et/secret")


class _Minter:
    def __init__(self) -> None:
        self.calls = 0

    async def mint_oauth(self, master_token: MasterToken, spec: Any) -> MintedOAuthToken:
        self.calls += 1
        return MintedOAuthToken(
            token=f"ya29.minted-{self.calls}", expires_at=int(time.time()) + 3600
        )


@pytest.mark.asyncio
async def test_forced_refresh_advances_generation_on_real_provider() -> None:
    minter = _Minter()
    provider = BearerProvider(_Reader(), minter)
    provider.set_bound_loop(asyncio.get_running_loop())
    await provider.activate(1)
    client = SimpleNamespace(
        _android_session=SimpleNamespace(active_epoch=1),
        _android_bearer_provider=provider,
    )
    try:
        detail = await canary.check_bearer(client)
        # Without an invalidation the cached credential is reused: the step's
        # ``invalidate`` is what forced the second mint.
        cached = await provider.get(1)
        assert cached.generation == 2
    finally:
        await provider.prepare_close()

    assert detail == "generation 1->2"
    assert minter.calls == 2


@pytest.mark.asyncio
async def test_bearer_step_requires_an_active_session() -> None:
    client = SimpleNamespace(_android_session=None, _android_bearer_provider=None)
    with pytest.raises(RuntimeError, match="not active"):
        await canary.check_bearer(client)


class _ThrottledBearer(_Bearer):
    """Fail the re-mint ``failures`` times with ``AuthError`` before minting."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.rejected = 0

    async def get(self, expected_epoch: int) -> BearerCredential:
        if self._cached is None and self.generation > 0 and self.rejected < self.failures:
            self.rejected += 1
            raise AuthError("Android authentication could not mint an access token.")
        return await super().get(expected_epoch)


@pytest.mark.asyncio
async def test_transient_refresh_mint_failure_is_retried_then_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canary, "_REFRESH_RETRY_BACKOFF_SECONDS", 0.0)
    bearer = _ThrottledBearer(failures=1)
    code, lines = await _run(_Service(), bearer)

    assert code == 0
    assert _line(lines, "OK bearer") == "OK bearer generation 1->2"
    assert bearer.rejected == 1
    assert bearer.generation == 2


@pytest.mark.asyncio
async def test_persistent_refresh_mint_failure_is_a_distinct_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canary, "_REFRESH_RETRY_BACKOFF_SECONDS", 0.0)
    bearer = _ThrottledBearer(failures=2)
    code, lines = await _run(_Service(), bearer)

    assert code == 1
    assert _line(lines, "FAIL bearer") == (
        "FAIL bearer refresh-mint AuthError: Android authentication could not mint an access token."
    )
    assert bearer.rejected == 2
    # Exactly one retry: the third mint attempt belongs to the later steps,
    # which still run (the throttle has lifted by then in this fake).
    assert bearer.generation == 2
    assert _line(lines, "OK get_project")
    assert _line(lines, "OK schema GetProject")


@pytest.mark.asyncio
async def test_refresh_mint_retry_backs_off_exactly_once() -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    bearer = _ThrottledBearer(failures=1)
    await bearer.activate(1)
    client = SimpleNamespace(
        _android_session=SimpleNamespace(active_epoch=1),
        _android_bearer_provider=bearer,
    )
    assert await canary.check_bearer(client, sleep=sleep) == "generation 1->2"
    assert slept == [canary._REFRESH_RETRY_BACKOFF_SECONDS] == [5.0]

    # The first mint is not retried: a throttle there is the plain verdict.
    class _ColdThrottle(_Bearer):
        async def get(self, expected_epoch: int) -> BearerCredential:
            raise AuthError("cold mint refused")

    cold = _ColdThrottle()
    await cold.activate(1)
    client = SimpleNamespace(
        _android_session=SimpleNamespace(active_epoch=1),
        _android_bearer_provider=cold,
    )
    slept.clear()
    with pytest.raises(AuthError, match="cold mint refused"):
        await canary.check_bearer(client, sleep=sleep)
    assert slept == []


@pytest.mark.asyncio
async def test_schema_setup_failure_is_labelled_schema_not_close() -> None:
    lines: list[str] = []
    report = canary.CanaryReport(lines.append)
    await canary.check_schema(report, SimpleNamespace(), NOTEBOOK_ID, None)

    assert len(lines) == 1
    assert lines[0].startswith("FAIL schema AttributeError: ")
    assert not report.all_ok

    # And through the whole run the verdict stays ``schema``, never ``close``.
    client = SimpleNamespace(
        backends=dict(ANDROID_BACKENDS),
        _android_session=None,
        _android_bearer_provider=None,
        notebooks=SimpleNamespace(),
        chat=SimpleNamespace(),
    )
    full: list[str] = []
    assert await canary.run_canary(lambda: _FakeContext(client), NOTEBOOK_ID, out=full.append) == 1
    # ``_android_session`` resolves (to None) so each per-RPC probe fails on
    # its own ``schema <rpc>`` line — still ``schema``, never ``close``.
    assert [line.split(" ")[1] for line in full if line.startswith("FAIL")] == [
        "bearer",
        "get_project",
        "list_chat_sessions",
        "schema",
        "schema",
    ]
    assert not [line for line in full if line.startswith("FAIL close")]


# ---------------------------------------------------------------------------
# Fingerprint + unknown-field helpers (pure)
# ---------------------------------------------------------------------------


def _project_response(**fields: Any) -> Any:
    return read_pb2.GetProjectResponse(project=read_pb2.Project(**fields))


def test_fingerprint_is_stable_across_values_and_ids() -> None:
    first = _project_response(id="notebook-a", title="Alpha")
    second = _project_response(id="notebook-b", title="A much longer title")

    assert canary.shape_fingerprint(first) == canary.shape_fingerprint(second)
    assert canary.shape_pairs(first) == {
        ("project", 2),
        ("project.id", 2),
        ("project.title", 2),
    }


def test_fingerprint_changes_when_a_field_is_added() -> None:
    base = _project_response(id="notebook-a", title="Alpha")
    extended = _project_response(id="notebook-a", title="Alpha", emoji="x")

    assert canary.shape_fingerprint(base) != canary.shape_fingerprint(extended)
    assert canary.shape_pairs(extended) - canary.shape_pairs(base) == {("project.emoji", 2)}


def test_fingerprint_agrees_across_full_and_wire_projections() -> None:
    """Same bytes, same populated fields -> same hash whichever message decodes them."""
    full = _project_response(id="notebook-a", title="Alpha")
    projected = wire_pb2.WireGetProjectResponse.FromString(full.SerializeToString())

    assert canary.shape_fingerprint(projected) == canary.shape_fingerprint(full)


def test_fingerprint_ignores_repeated_cardinality_but_sees_nested_shape() -> None:
    one = chat_pb2.ListChatSessionsResponse(sessions=[common_pb2.ChatSession(chat_session_id="a")])
    two = chat_pb2.ListChatSessionsResponse(
        sessions=[
            common_pb2.ChatSession(chat_session_id="a"),
            common_pb2.ChatSession(chat_session_id="b"),
        ]
    )
    empty = chat_pb2.ListChatSessionsResponse()

    assert canary.shape_fingerprint(one) == canary.shape_fingerprint(two)
    assert canary.shape_fingerprint(one) != canary.shape_fingerprint(empty)
    assert canary.shape_pairs(one) == {("sessions", 2), ("sessions.chat_session_id", 2)}


def test_fingerprint_is_sha256_hex() -> None:
    digest = canary.shape_fingerprint(chat_pb2.ListChatSessionsResponse())
    assert len(digest) == 64
    assert digest == canary.shape_fingerprint(chat_pb2.ListChatSessionsResponse())


def test_wire_types_follow_the_protobuf_encoding_table() -> None:
    fields = read_pb2.GetProjectRequest.DESCRIPTOR.fields_by_name
    assert canary.wire_type_of(fields["project_id"]) == 2
    assert canary.wire_type_of(fields["include_audio_overview_ids"]) == 0


def test_count_unknown_fields_recurses_into_nested_messages() -> None:
    clean = chat_pb2.ListChatSessionsResponse(
        sessions=[common_pb2.ChatSession(chat_session_id="a")]
    )
    assert canary.count_unknown_fields(clean) == 0

    top_level = chat_pb2.ListChatSessionsResponse.FromString(
        clean.SerializeToString() + UNKNOWN_FIELD_BYTES
    )
    assert canary.count_unknown_fields(top_level) == 1

    inner = common_pb2.ChatSession(chat_session_id="a").SerializeToString() + UNKNOWN_FIELD_BYTES
    nested = chat_pb2.ListChatSessionsResponse.FromString(bytes([0x0A, len(inner)]) + inner)
    assert len(nested.sessions) == 1
    assert canary.count_unknown_fields(nested) == 1
    # Unknown fields do not change the known-field fingerprint.
    assert canary.shape_fingerprint(nested) == canary.shape_fingerprint(clean)


# ---------------------------------------------------------------------------
# Failure reporting + open/close verdicts (no server needed)
# ---------------------------------------------------------------------------


def test_describe_failure_scrubs_and_bounds() -> None:
    rendered = canary.describe_failure(
        RuntimeError("Authorization: Bearer ya29.abcdefghijklmnop\nsecond line " + "x" * 400)
    )
    assert rendered.startswith("RuntimeError: Authorization: Bearer ***")
    assert "ya29.abcdefghijklmnop" not in rendered
    assert "\n" not in rendered
    assert len(rendered) <= len("RuntimeError: ") + canary._MAX_FAILURE_DETAIL
    assert canary.describe_failure(ValueError()) == "ValueError"


class _FakeContext:
    def __init__(self, client: Any = None, *, enter_error: Exception | None = None) -> None:
        self._client = client
        self._enter_error = enter_error
        self.exited = False

    async def __aenter__(self) -> Any:
        if self._enter_error is not None:
            raise self._enter_error
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


@pytest.mark.asyncio
async def test_open_failure_is_a_fail_open_line_and_exit_one() -> None:
    lines: list[str] = []
    code = await canary.run_canary(
        lambda: _FakeContext(enter_error=AuthError("Android authentication could not mint.")),
        NOTEBOOK_ID,
        out=lines.append,
    )
    assert code == 1
    assert lines == ["FAIL open AuthError: Android authentication could not mint."]


@pytest.mark.asyncio
async def test_factory_error_is_a_fail_open_line() -> None:
    def factory() -> Any:
        raise RuntimeError("no profile")

    lines: list[str] = []
    assert await canary.run_canary(factory, NOTEBOOK_ID, out=lines.append) == 1
    assert lines == ["FAIL open RuntimeError: no profile"]


@pytest.mark.asyncio
async def test_partial_backend_selection_fails_open_and_stops() -> None:
    client = SimpleNamespace(backends={"notebooks": "android", "chat": "web"})
    context = _FakeContext(client)
    lines: list[str] = []
    assert await canary.run_canary(lambda: context, NOTEBOOK_ID, out=lines.append) == 1
    assert lines == [
        "FAIL open RuntimeError: Android backend was not fully selected: ['android', 'web']"
    ]
    assert context.exited


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_main_requires_a_notebook_id(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.delenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", raising=False)
    assert canary.main([], client_factory=lambda: _FakeContext()) == 2
    assert "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID" in capsys.readouterr().err


def test_main_reads_notebook_id_from_env_and_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", NOTEBOOK_ID)
    seen: list[str] = []

    class _Recording(_FakeContext):
        async def __aenter__(self) -> Any:
            seen.append("entered")
            return SimpleNamespace(backends={"notebooks": "web"})

    assert canary.main([], client_factory=_Recording) == 1
    assert seen == ["entered"]
    assert "FAIL open" in capsys.readouterr().out


def test_main_prefers_the_flag_over_env_and_forwards_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "env-notebook")
    captured: list[tuple[str, Path | None]] = []

    class _Capture(_FakeContext):
        async def __aenter__(self) -> Any:
            raise RuntimeError("stop")

    original = canary.run_canary

    async def spy(
        factory: Any,
        notebook_id: str,
        *,
        baseline_path: Any,
        missing_baseline_grace_until: Any = None,
        out: Any,
    ) -> int:
        captured.append((notebook_id, baseline_path))
        return await original(
            factory,
            notebook_id,
            baseline_path=baseline_path,
            missing_baseline_grace_until=missing_baseline_grace_until,
            out=out,
        )

    monkeypatch.setattr(canary, "run_canary", spy)
    assert canary.main(["--notebook-id", "flag-notebook"], client_factory=_Capture) == 1
    assert (
        canary.main(
            ["--notebook-id", "flag-notebook", "--baseline", "fixtures/b.json"],
            client_factory=_Capture,
        )
        == 1
    )
    assert captured == [("flag-notebook", None), ("flag-notebook", Path("fixtures/b.json"))]


def test_main_missing_baseline_warns_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", NOTEBOOK_ID)
    missing = tmp_path / "nope.json"

    class _Stop(_FakeContext):
        async def __aenter__(self) -> Any:
            raise RuntimeError("stop")

    canary.main(["--baseline", str(missing)], client_factory=_Stop)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"WARN baseline missing {missing}"
    assert not missing.exists()


def test_missing_baseline_is_a_warn_within_grace_and_a_fail_after(tmp_path: Path) -> None:
    from datetime import date

    lines: list[str] = []
    report = canary.CanaryReport(lines.append)
    missing = tmp_path / "absent" / "canary_baseline.json"

    assert (
        canary.load_baseline(
            missing, report, missing_grace_until=date(2026, 9, 14), today=date(2026, 9, 14)
        )
        is None
    )
    assert lines == [f"WARN baseline missing {missing}"]
    assert report.all_ok

    lines.clear()
    report = canary.CanaryReport(lines.append)
    assert (
        canary.load_baseline(
            missing, report, missing_grace_until=date(2026, 9, 14), today=date(2026, 9, 15)
        )
        is None
    )
    assert lines == [
        f"FAIL baseline missing {missing} and the bootstrap grace period ended 2026-09-14"
    ]
    assert not report.all_ok
    assert not missing.exists() and not missing.parent.exists()


def test_parser_accepts_iso_grace_date_and_rejects_garbage() -> None:
    from datetime import date

    args = canary.build_parser().parse_args(["--missing-baseline-grace-until", "2026-09-14"])
    assert args.missing_baseline_grace_until == date(2026, 9, 14)
    with pytest.raises(SystemExit):
        canary.build_parser().parse_args(["--missing-baseline-grace-until", "soon"])


def test_parse_baseline_rejects_non_hex_shape() -> None:
    with pytest.raises(ValueError, match="64-hex"):
        canary.parse_baseline({"GetProject": {"shape": "g" * 64, "unknown_fields": 0}})
