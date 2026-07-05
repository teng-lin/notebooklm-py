"""CLI ↔ MCP adapter parity for artifact generation.

Both the CLI ``generate`` command and the MCP ``studio_generate`` tool are thin
adapters over the *same* ``_app/generate`` core, but each plugs in its own
notebook/source resolvers. When those resolvers disagree, the two surfaces drift
apart even though every per-adapter unit test passes — which is exactly how #1652
shipped: the CLI resolved an omitted ``source_ids`` to ``None`` ("all sources")
while the MCP passthrough sent an empty list ("zero sources"), and the backend
refused the latter (``<kind> generation is unavailable``).

These tests drive both adapters against a mocked client for the same logical
inputs and assert the downstream ``client.artifacts.generate_*`` call is
equivalent. A mock can't enforce the backend contract, but it *can* pin that the
two adapters agree — which is the property that broke.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("fastmcp")

from click.testing import CliRunner  # noqa: E402
from fastmcp import Client  # noqa: E402 - after importorskip guard

import notebooklm.auth as auth_module  # noqa: E402
from notebooklm._app import source_add as source_add_module  # noqa: E402
from notebooklm._types.artifacts import ArtifactStatus, ArtifactTypeCode  # noqa: E402
from notebooklm.cli import helpers as helpers_module  # noqa: E402
from notebooklm.cli.resolve import resolve_notebook_id, resolve_source_ids  # noqa: E402
from notebooklm.mcp._resolve import resolve_notebook  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402
from notebooklm.mcp.tools.studio import _passthrough_sources  # noqa: E402
from notebooklm.notebooklm_cli import cli  # noqa: E402
from notebooklm.types import Artifact, GenerationState  # noqa: E402

from .conftest import create_mock_client, inject_client  # noqa: E402

# UUID-shaped ids so BOTH adapters treat them as already-full (the MCP
# resolve_notebook skips the name lookup; the CLI resolve_source_ids skips the
# fuzzy client.sources.list match) — matching how MCP supplies full ids.
NB = "33333333-3333-3333-3333-333333333333"
SRC_A = "11111111-1111-1111-1111-111111111111"
SRC_B = "22222222-2222-2222-2222-222222222222"

#: (cli subcommand, MCP artifact_type, client method, mcp_extra, cli_extra) for every
#: source-using generate kind. ``mcp_extra``/``cli_extra`` carry a kind's REQUIRED
#: non-source input (data-table needs a description); they are NOT source_ids — the
#: tests add sources themselves. ``mind-map`` is intentionally omitted: it renders
#: synchronously via a different client path (tracked in #1653).
_KINDS = [
    ("quiz", "quiz", "generate_quiz", {}, []),
    ("audio", "audio", "generate_audio", {}, []),
    ("flashcards", "flashcards", "generate_flashcards", {}, []),
    ("video", "video", "generate_video", {}, []),
    ("cinematic-video", "cinematic-video", "generate_cinematic_video", {}, []),
    ("slide-deck", "slide-deck", "generate_slide_deck", {}, []),
    ("infographic", "infographic", "generate_infographic", {}, []),
    ("report", "report", "generate_report", {}, []),
    (
        "data-table",
        "data-table",
        "generate_data_table",
        {"instructions": "Compare key concepts"},
        ["Compare key concepts"],
    ),
]

_NAMESPACES = (
    "notebooks",
    "sources",
    "chat",
    "artifacts",
    "research",
    "notes",
    "sharing",
    "labels",
    "settings",
    "mind_maps",
)


@dataclass
class _FakeStatus:
    """Minimal generate() return the MCP serializer accepts."""

    task_id: str = "task-1"
    status: GenerationState = GenerationState.COMPLETED
    url: str | None = None
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] | None = field(default=None)

    @property
    def is_complete(self) -> bool:
        return True


def _normalize_source_ids(value: Any) -> Any:
    """Compare source_ids by content, not container type (tuple vs list)."""
    return None if value is None else sorted(value)


def _normalized_call(call: Any) -> tuple[tuple, dict]:
    """A captured generate-* call as ``(args, kwargs)`` for cross-adapter comparison.

    Only ``source_ids`` is normalized (its container type differs: tuple vs list);
    EVERY other positional/keyword arg is compared verbatim, so a divergence in any
    default (language, audio_format, quantity, …) — not just source_ids — is caught.
    """
    kwargs = dict(call.kwargs)
    kwargs["source_ids"] = _normalize_source_ids(kwargs.get("source_ids"))
    return tuple(call.args), kwargs


class _Captured(Exception):
    """Raised by the recorder to short-circuit each adapter after the captured call.

    NOTE: the MCP layer's ``mcp_errors`` wraps every exception into a ``ToolError``,
    so this type does NOT survive the MCP boundary — which is why the drivers can't
    simply ``suppress(_Captured)``. Instead the capture helpers below tolerate the
    abort ONLY when a call was actually recorded, and otherwise re-raise the real
    adapter error, so an unrelated pre-capture failure is never silently hidden.
    """


def _recorder() -> tuple[list[tuple[tuple, dict]], Any]:
    """Return (calls, fn): an async fn that records each call then raises the sentinel."""
    calls: list[tuple[tuple, dict]] = []

    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise _Captured

    return calls, fn


def _drive_mcp(
    tool: str, args: dict[str, Any], setup: Any = None
) -> tuple[Any, BaseException | None]:
    """Drive an MCP ``tool`` against a fresh mocked client.

    Returns ``(client, exc)`` where ``exc`` is the exception the call raised (or
    ``None``). Callers classify it — the generate path expects ``None``; the e2e
    capture path tolerates it only once a downstream call was recorded.
    """
    client = MagicMock()
    for ns in _NAMESPACES:
        setattr(client, ns, MagicMock())
    client.artifacts._list_for_download = None
    if setup is not None:
        setup(client)

    @contextlib.asynccontextmanager
    async def factory() -> Any:
        yield client

    captured: dict[str, BaseException] = {}

    async def run() -> None:
        async with Client(create_server(client_factory=factory)) as c:
            try:
                await c.call_tool(tool, args)
            except Exception as exc:  # noqa: BLE001 - caller classifies (see _mcp_capture)
                captured["exc"] = exc

    asyncio.run(run())
    return client, captured.get("exc")


def _drive_cli(argv: list[str], setup: Any = None) -> tuple[Any, Any]:
    """Drive the CLI with ``argv`` against a fresh mocked client.

    Returns ``(client, result)``; ``result.exception`` / ``result.exit_code`` carry
    any failure for the caller to classify (CliRunner captures exceptions, so a
    sentinel abort surfaces as a non-zero exit rather than propagating).
    """
    client = create_mock_client()
    client.artifacts._list_for_download = None
    if setup is not None:
        setup(client)
    with (
        patch.object(helpers_module, "load_auth_from_storage", return_value={"SAPISID": "x"}),
        patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch,
    ):
        mock_fetch.return_value = ("csrf", "session")
        result = CliRunner().invoke(cli, argv, obj=inject_client(client))
    return client, result


def _mcp_capture(
    tool: str, args: dict[str, Any], namespace: str, method: str, setup: Any = None
) -> Any:
    """Drive an MCP tool, returning the single recorded ``namespace.method`` call.

    If the tool never reached it, re-raise the REAL adapter error (chained) instead
    of a bare empty-list assert — so a pre-capture failure is diagnosable.
    """
    calls, fn = _recorder()

    def _setup(c: Any) -> None:
        if setup is not None:
            setup(c)
        setattr(getattr(c, namespace), method, fn)

    _, exc = _drive_mcp(tool, args, setup=_setup)
    if not calls:
        raise AssertionError(f"MCP {tool} never reached {namespace}.{method}") from exc
    return calls[0]


def _cli_capture(argv: list[str], namespace: str, method: str, setup: Any = None) -> Any:
    """CLI counterpart of :func:`_mcp_capture`."""
    calls, fn = _recorder()

    def _setup(c: Any) -> None:
        if setup is not None:
            setup(c)
        setattr(getattr(c, namespace), method, fn)

    _, result = _drive_cli(argv, setup=_setup)
    if not calls:
        raise AssertionError(
            f"CLI {argv} never reached {namespace}.{method}: {result.output}"
        ) from result.exception
    return calls[0]


def _mcp_generate_call(artifact_type: str, method: str, extra: dict[str, Any]) -> Any:
    """Drive the MCP ``studio_generate`` tool; return the captured generate-method call."""
    client, exc = _drive_mcp(
        "studio_generate",
        {"notebook": NB, "artifact_type": artifact_type, **extra},
        setup=lambda c: setattr(c.artifacts, method, AsyncMock(return_value=_FakeStatus())),
    )
    assert exc is None, f"MCP generate {artifact_type} unexpectedly raised: {exc!r}"
    return getattr(client.artifacts, method).await_args


def _cli_generate_call(cmd: str, method: str, extra_args: list[str]) -> Any:
    """Drive the CLI ``generate <cmd>``; return the captured generate-method call."""
    client, result = _drive_cli(
        ["generate", cmd, "-n", NB, *extra_args],
        setup=lambda c: setattr(
            c.artifacts,
            method,
            AsyncMock(return_value={"task_id": "task-1", "status": "processing"}),
        ),
    )
    assert result.exit_code == 0, result.output
    return getattr(client.artifacts, method).call_args


@pytest.mark.parametrize(
    "cmd,artifact_type,method,mcp_extra,cli_extra", _KINDS, ids=[k[0] for k in _KINDS]
)
def test_omitted_source_ids_parity(
    cmd: str, artifact_type: str, method: str, mcp_extra: dict, cli_extra: list[str]
) -> None:
    """Omitting sources: BOTH adapters must pass ``source_ids=None`` (= all sources).

    The #1652 regression: MCP sent an empty tuple (= zero sources, refused) while the
    CLI sent ``None``. This asserts they agree — and that the agreed value is ``None`` —
    across EVERY source-using generate kind (audio/video/slide-deck/report/…).
    """
    mcp_call = _mcp_generate_call(artifact_type, method, mcp_extra)
    cli_call = _cli_generate_call(cmd, method, cli_extra)

    mcp_src = _normalize_source_ids(mcp_call.kwargs.get("source_ids"))
    cli_src = _normalize_source_ids(cli_call.kwargs.get("source_ids"))
    assert cli_src is None, f"CLI {cmd} omitted-sources should resolve to None, got {cli_src!r}"
    assert mcp_src is None, f"MCP {cmd} omitted-sources should resolve to None, got {mcp_src!r}"
    # Full parity: notebook id (positional) AND every downstream kwarg agree, so a
    # divergence in any default — not just source_ids — fails here.
    assert _normalized_call(mcp_call) == _normalized_call(cli_call)
    assert mcp_call.args[0] == NB


@pytest.mark.parametrize(
    "cmd,artifact_type,method,mcp_extra,cli_extra", _KINDS, ids=[k[0] for k in _KINDS]
)
def test_explicit_source_ids_parity(
    cmd: str, artifact_type: str, method: str, mcp_extra: dict, cli_extra: list[str]
) -> None:
    """With explicit (full) ids, both adapters pass the SAME ids downstream (all kinds)."""
    mcp_call = _mcp_generate_call(
        artifact_type, method, {**mcp_extra, "source_ids": [SRC_A, SRC_B]}
    )
    cli_call = _cli_generate_call(cmd, method, [*cli_extra, "-s", SRC_A, "-s", SRC_B])

    mcp_src = _normalize_source_ids(mcp_call.kwargs.get("source_ids"))
    cli_src = _normalize_source_ids(cli_call.kwargs.get("source_ids"))
    assert mcp_src == cli_src == sorted([SRC_A, SRC_B])
    # And full parity on the rest of the call too.
    assert _normalized_call(mcp_call) == _normalized_call(cli_call)


#: Per-type OPTIONS the MCP exposes as agent-settable, with a NON-DEFAULT value and the
#: equivalent CLI flag(s). This guards that each kind's parameters/styles map to the SAME
#: downstream call across adapters — not just source_ids.
#:
#: Each row is ``(test_id, cmd, artifact_type, method, mcp_opts, cli_opts)``; the explicit
#: ``test_id`` keeps the two ``video`` rows distinct.
#:
#: NOTE on scope (#1654): video / slide-deck / infographic per-kind options ARE now
#: agent-settable via MCP and are covered here. ``infographic`` deliberately uses
#: ``style="professional"`` — a value present ONLY in the infographic style set, NOT the
#: video one (the two sets overlap on ``auto``/``anime``/``kawaii``) — so the case fails if
#: MCP validated/forwarded against the wrong (video) set. ``cinematic-video`` and
#: ``data-table`` expose no per-kind options. ``mind-map`` renders via a different client
#: path (``mind_maps.generate`` / ``generate_mind_map``, see #1653) so it is NOT in this
#: matrix; its ``map_kind`` + ``instructions`` parity is covered in
#: ``tests/unit/mcp/test_studio.py``.
_OPTION_CASES = [
    (
        "audio",
        "audio",
        "audio",
        "generate_audio",
        {"audio_format": "critique", "audio_length": "long"},
        ["--format", "critique", "--length", "long"],
    ),
    (
        "quiz",
        "quiz",
        "quiz",
        "generate_quiz",
        {"quantity": "more", "difficulty": "hard"},
        ["--quantity", "more", "--difficulty", "hard"],
    ),
    (
        "flashcards",
        "flashcards",
        "flashcards",
        "generate_flashcards",
        {"quantity": "fewer", "difficulty": "easy"},
        ["--quantity", "fewer", "--difficulty", "easy"],
    ),
    (
        "report",
        "report",
        "report",
        "generate_report",
        {"report_format": "study-guide"},
        ["--format", "study-guide"],
    ),
    (
        "video",
        "video",
        "video",
        "generate_video",
        {"video_format": "brief", "style": "classic"},
        ["--format", "brief", "--style", "classic"],
    ),
    (
        "video-custom-style",
        "video",
        "video",
        "generate_video",
        {"style": "custom", "style_prompt": "hand-drawn"},
        ["--style", "custom", "--style-prompt", "hand-drawn"],
    ),
    (
        "video-short",
        "video",
        "video",
        "generate_video",
        {"video_format": "short"},
        ["--format", "short"],
    ),
    (
        "slide-deck",
        "slide-deck",
        "slide-deck",
        "generate_slide_deck",
        {"deck_format": "presenter", "deck_length": "short"},
        ["--format", "presenter", "--length", "short"],
    ),
    (
        "infographic",
        "infographic",
        "infographic",
        "generate_infographic",
        {"orientation": "portrait", "detail": "detailed", "style": "professional"},
        ["--orientation", "portrait", "--detail", "detailed", "--style", "professional"],
    ),
]


@pytest.mark.parametrize(
    "test_id,cmd,artifact_type,method,mcp_opts,cli_opts",
    _OPTION_CASES,
    ids=[c[0] for c in _OPTION_CASES],
)
def test_explicit_option_parity(
    test_id: str,
    cmd: str,
    artifact_type: str,
    method: str,
    mcp_opts: dict,
    cli_opts: list[str],
) -> None:
    """Each kind's agent-settable OPTIONS/STYLES map to the SAME downstream call.

    A NON-DEFAULT value for every MCP-exposed option must produce an identical
    ``generate_*`` call from the CLI's equivalent flags — catching any per-type
    parameter-mapping divergence (the user's concern), not just source_ids.
    """
    mcp_call = _mcp_generate_call(artifact_type, method, mcp_opts)
    cli_call = _cli_generate_call(cmd, method, cli_opts)
    assert _normalized_call(mcp_call) == _normalized_call(cli_call)


def test_mind_map_instructions_parity() -> None:
    """CLI ``generate mind-map`` and MCP ``studio_generate`` deliver the SAME
    ``instructions`` to ``client.mind_maps.generate`` (interactive default).

    Mind-map is excluded from the matrices above (its interactive path goes through
    ``mind_maps.generate``, not ``artifacts.generate_*``), so this pins the cross-adapter
    contract for the #1654 instructions fix directly: MCP previously stored the value as
    ``description`` only and dropped it for mind-map.
    """
    note = "focus on the timeline"

    mcp_client, mcp_exc = _drive_mcp(
        "studio_generate",
        {"notebook": NB, "artifact_type": "mind-map", "instructions": note},
        setup=lambda c: setattr(c.mind_maps, "generate", AsyncMock(return_value={"id": "mm1"})),
    )
    assert mcp_exc is None, f"MCP mind-map unexpectedly raised: {mcp_exc!r}"

    cli_client, cli_result = _drive_cli(
        ["generate", "mind-map", "-n", NB, "--instructions", note],
        setup=lambda c: setattr(c.mind_maps, "generate", AsyncMock(return_value={"id": "mm1"})),
    )
    assert cli_result.exit_code == 0, cli_result.output

    mcp_instr = mcp_client.mind_maps.generate.await_args.kwargs["instructions"]
    cli_instr = cli_client.mind_maps.generate.await_args.kwargs["instructions"]
    assert mcp_instr == cli_instr == note


# ---------------------------------------------------------------------------
# Resolver parity — every CLI/MCP operation funnels notebook/source references
# through these shared resolvers, so pinning their agreement covers the
# divergence surface for ALL operations, not just generate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_ids", [(), (SRC_A, SRC_B)], ids=["omitted", "full-ids"])
def test_source_resolver_parity(source_ids: tuple[str, ...]) -> None:
    """CLI ``resolve_source_ids`` and MCP ``_passthrough_sources`` must agree.

    Omitted ⇒ ``None`` ("all sources"), NOT an empty list ("zero sources"); full ids
    pass through identically. Neither path may hit ``client.sources.list``. This is
    the exact contract whose violation caused #1652.
    """
    client = MagicMock()
    cli_out = asyncio.run(resolve_source_ids(client, NB, source_ids))
    mcp_out = asyncio.run(_passthrough_sources(client, NB, source_ids))
    assert _normalize_source_ids(cli_out) == _normalize_source_ids(mcp_out)
    if not source_ids:
        assert cli_out is None and mcp_out is None
    client.sources.list.assert_not_called()


def test_notebook_resolver_parity_full_uuid() -> None:
    """CLI ``resolve_notebook_id`` and MCP ``resolve_notebook`` agree on a full UUID
    (fast-path: return it unchanged, no listing) — the shared entry every op uses."""
    client = MagicMock()
    cli_out = asyncio.run(resolve_notebook_id(client, NB))
    mcp_out = asyncio.run(resolve_notebook(client, NB))
    assert cli_out == mcp_out == NB
    client.notebooks.list.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end per-operation parity for the heavier shared-core ops
# (source_add, research, download). Each drives BOTH adapters and captures the
# shared downstream call via a recorder that raises a sentinel — so neither
# adapter's return-serialization runs (which is what made these brittle). We
# compare the captured call, not the (discarded) result.
# ---------------------------------------------------------------------------


def test_source_add_url_parity() -> None:
    """source_add(url): both adapters build the SAME SourceAddPlan + notebook id.

    Both run through ``_app.source_add.execute_source_add`` → module-level
    ``add_source(sources, notebook_id=…, plan=…)``; patching that one symbol
    captures both adapters' calls.
    """
    calls, fn = _recorder()
    with patch.object(source_add_module, "add_source", fn):
        _, mcp_exc = _drive_mcp(
            "source_add", {"notebook": NB, "source_type": "url", "url": "https://example.com/a"}
        )
        _, cli_result = _drive_cli(["source", "add", "https://example.com/a", "-n", NB])
    assert len(calls) == 2, (
        f"both adapters must reach add_source; got {len(calls)} "
        f"(mcp_exc={mcp_exc!r}, cli_exit={cli_result.exit_code}, cli_out={cli_result.output!r})"
    )
    mcp_kwargs, cli_kwargs = calls[0][1], calls[1][1]
    assert mcp_kwargs["notebook_id"] == cli_kwargs["notebook_id"] == NB
    assert mcp_kwargs["plan"] == cli_kwargs["plan"]


def test_research_start_parity() -> None:
    """research: MCP ``research_start`` and CLI ``source add-research`` both call
    ``client.research.start(nb_id, query, source, mode)`` with the same args/defaults."""
    mcp = _mcp_capture(
        "research_start", {"notebook": NB, "query": "AI agents"}, "research", "start"
    )
    cli = _cli_capture(
        ["source", "add-research", "AI agents", "-n", NB, "--no-wait"], "research", "start"
    )
    assert mcp == cli  # (nb_id, query, source="web", mode="fast")


_AUDIO_ARTIFACT = Artifact(
    id="art1",
    title="Podcast",
    _artifact_type=ArtifactTypeCode.AUDIO.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
)


def test_download_audio_parity(tmp_path: Any) -> None:
    """download: MCP ``studio_download`` and CLI ``download audio`` resolve the same
    artifact and call ``client.artifacts.download_audio`` identically (via the shared
    ``execute_download``)."""
    out = str(tmp_path / "out.mp3")

    def setup(client: Any) -> None:
        client.artifacts._list_for_download = None
        client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])

    mcp = _mcp_capture(
        "studio_download",
        {"notebook": NB, "artifact_type": "audio", "path": out},
        "artifacts",
        "download_audio",
        setup=setup,
    )
    # OUTPUT_PATH is a positional arg on the CLI leaf (not -o).
    cli = _cli_capture(
        ["download", "audio", out, "-n", NB], "artifacts", "download_audio", setup=setup
    )
    assert mcp == cli
