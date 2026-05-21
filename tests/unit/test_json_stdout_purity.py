"""Stdout-purity contract for ``--json`` mode.

Any CLI command that supports ``--json`` MUST emit nothing on stdout except
the JSON payload, so that ``json.loads(result.stdout)`` succeeds for downstream
automation. Diagnostic prints (status text, partial-ID "Matched..." hints,
Rich live status) belong on stderr.

This test suite locks the contract for the two known violators called out in
audit K2 / codex #4 — and adds a parametrized sweep across every CLI command
that exposes a ``--json`` flag so future regressions surface immediately.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.rpc.types import ShareAccess, ShareViewLevel
from notebooklm.types import (
    Artifact,
    AskResult,
    Note,
    Notebook,
    ShareStatus,
    Source,
)

# ---------------------------------------------------------------------------
# Fixtures: minimal mocks needed to keep --json paths offline.
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_auth_env() -> Generator[None, None, None]:
    """Stub auth loading + token fetch so --json paths run offline."""
    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_load.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        mock_fetch.return_value = ("csrf_token", "session_id")
        yield


def _stub_notebooks() -> list[Notebook]:
    return [
        Notebook(
            id="abc123def456ghi789jkl",
            title="First Notebook",
            created_at=datetime(2024, 1, 1),
            is_owner=True,
        ),
        Notebook(
            id="xyz789uvw456rst123mno",
            title="Second Notebook",
            created_at=datetime(2024, 1, 2),
            is_owner=False,
        ),
    ]


def _stub_sources() -> list[Source]:
    return [
        Source(id="src123def456ghi789jkl", title="Source A"),
        Source(id="src999zzz888yyy777uvw", title="Source B"),
    ]


def _stub_artifacts() -> list[Artifact]:
    # _artifact_type=1 is AUDIO in rpc/types; full coverage isn't required —
    # we just need objects that round-trip through to-dict.
    return [
        Artifact(
            id="art123def456ghi789jkl",
            title="Artifact A",
            _artifact_type=1,
            status=3,
            created_at=datetime(2024, 1, 1),
        ),
    ]


def _stub_notes() -> list[Note]:
    return [
        Note(
            id="note123def456ghi789jkl",
            notebook_id="abc123def456ghi789jkl",
            title="Note A",
            content="content",
        ),
    ]


def _stub_share_status(notebook_id: str = "abc123def456ghi789jkl") -> ShareStatus:
    return ShareStatus(
        notebook_id=notebook_id,
        is_public=False,
        access=ShareAccess.RESTRICTED,
        view_level=ShareViewLevel.FULL_NOTEBOOK,
        share_url=None,
        shared_users=[],
    )


def _make_client(extra_setup=None) -> MagicMock:
    """Build a single mock client that satisfies every --json command path.

    The same mock is used across patches in many CLI modules — each test only
    exercises the methods relevant to that command, so over-mocking is harmless.
    """
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    # Namespaces
    for ns in (
        "notebooks",
        "sources",
        "artifacts",
        "chat",
        "research",
        "notes",
        "sharing",
    ):
        setattr(client, ns, MagicMock())

    # Common list/lookup stubs (resolve_*_id walks these).
    client.notebooks.list = AsyncMock(return_value=_stub_notebooks())
    client.notebooks.get = AsyncMock(
        return_value=_stub_notebooks()[0],
    )
    client.notebooks.get_metadata = AsyncMock(
        return_value=MagicMock(to_dict=lambda: {"id": "abc123def456ghi789jkl"})
    )
    client.sources.list = AsyncMock(return_value=_stub_sources())
    client.artifacts.list = AsyncMock(return_value=_stub_artifacts())
    client.artifacts.suggest_reports = AsyncMock(return_value=[])
    client.notes.list = AsyncMock(return_value=_stub_notes())
    client.research.poll = AsyncMock(return_value={"status": "no_research"})
    client.sharing.get_status = AsyncMock(return_value=_stub_share_status())
    client.chat.get_conversation_id = AsyncMock(return_value=None)
    client.chat.get_history = AsyncMock(return_value=[])

    if extra_setup is not None:
        extra_setup(client)
    return client


def _patch_modules() -> list:
    """Return patch objects for every cli module that constructs NotebookLMClient.

    Caller does the ``with`` dance themselves so they can swap in a fresh mock
    instance for each command invocation.
    """
    modules = [
        "notebooklm.cli.notebook_cmd",
        "notebooklm.cli.chat_cmd",
        "notebooklm.cli.session_cmd",
        "notebooklm.cli.share_cmd",
        "notebooklm.cli.source_cmd",
        "notebooklm.cli.artifact_cmd",
        "notebooklm.cli.research_cmd",
        "notebooklm.cli.note_cmd",
        "notebooklm.cli.generate_cmd",
        "notebooklm.cli.download_cmd",
    ]
    # Post-P3.T0: `*_cmd` modules are not shadowed, so direct string-form
    # `patch(...)` resolves correctly without importlib indirection.
    return [patch(f"{name}.NotebookLMClient") for name in modules]


def _run_with_mock_client(runner: CliRunner, args: list[str], client: MagicMock):
    """Invoke the CLI with NotebookLMClient mocked in every relevant module."""
    patches = _patch_modules()
    try:
        for p in patches:
            cls = p.start()
            cls.return_value = client
        return runner.invoke(cli, args, catch_exceptions=False)
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Sweep: every --json-enabled command must emit valid JSON on stdout.
# ---------------------------------------------------------------------------


# Each entry: (case_id, argv, optional client customization).
# We keep this list curated rather than fully auto-discovered because the
# argv shape (positional args, required flags) differs per command. Adding
# a new --json command should fail this sweep until it lands here.
def _customize_chat_ask(client: MagicMock) -> None:
    client.chat.ask = AsyncMock(
        return_value=AskResult(
            answer="answer text",
            conversation_id="conv-123",
            turn_number=1,
            is_follow_up=False,
            references=[],
            raw_response="",
        )
    )


def _customize_share_public(client: MagicMock) -> None:
    client.sharing.set_public = AsyncMock(return_value=_stub_share_status())


def _customize_share_view_level(client: MagicMock) -> None:
    client.sharing.set_view_level = AsyncMock(return_value=_stub_share_status())


def _customize_source_fulltext(client: MagicMock) -> None:
    # source fulltext --json calls asdict() on the result, so return a real
    # SourceFulltext dataclass instance (not a MagicMock).
    from notebooklm.types import SourceFulltext

    client.sources.get_fulltext = AsyncMock(
        return_value=SourceFulltext(
            source_id="src123def456ghi789jkl",
            title="Source A",
            content="some content",
            url=None,
            char_count=12,
        )
    )


def _customize_source_guide(client: MagicMock) -> None:
    client.sources.get_guide = AsyncMock(
        return_value={"summary": "a summary", "keywords": ["k1", "k2"]}
    )


def _customize_research_wait(client: MagicMock) -> None:
    # research wait polls until status == "completed". Return a completed
    # payload immediately so the loop exits on the first iteration.
    client.research.poll = AsyncMock(
        return_value={
            "status": "completed",
            "sources": [],
            "query": "",
            "report": "",
        }
    )


JSON_COMMANDS: list[tuple[str, list[str], object]] = [
    # source group
    ("source_list", ["source", "list", "-n", "abc123def456ghi789jkl", "--json"], None),
    (
        "source_fulltext",
        [
            "source",
            "fulltext",
            "src123def456ghi789jkl",
            "-n",
            "abc123def456ghi789jkl",
            "--json",
        ],
        _customize_source_fulltext,
    ),
    (
        "source_guide",
        [
            "source",
            "guide",
            "src123def456ghi789jkl",
            "-n",
            "abc123def456ghi789jkl",
            "--json",
        ],
        _customize_source_guide,
    ),
    # artifact group
    ("artifact_list", ["artifact", "list", "-n", "abc123def456ghi789jkl", "--json"], None),
    (
        "artifact_suggestions",
        ["artifact", "suggestions", "-n", "abc123def456ghi789jkl", "--json"],
        None,
    ),
    # research group
    ("research_status", ["research", "status", "-n", "abc123def456ghi789jkl", "--json"], None),
    (
        "research_wait",
        ["research", "wait", "-n", "abc123def456ghi789jkl", "--json"],
        _customize_research_wait,
    ),
    # share group
    ("share_status", ["share", "status", "-n", "abc123def456ghi789jkl", "--json"], None),
    (
        "share_public",
        ["share", "public", "-n", "abc123def456ghi789jkl", "--enable", "--json"],
        _customize_share_public,
    ),
    (
        "share_view_level",
        ["share", "view-level", "full", "-n", "abc123def456ghi789jkl", "--json"],
        _customize_share_view_level,
    ),
    # note group
    ("note_list", ["note", "list", "-n", "abc123def456ghi789jkl", "--json"], None),
    # notebook group (top-level via session/notebook modules)
    ("notebook_list", ["list", "--json"], None),
    ("notebook_metadata", ["metadata", "-n", "abc123def456ghi789jkl", "--json"], None),
    # session group
    ("status_cmd", ["status", "--json"], None),
    # chat group
    (
        "ask_cmd",
        ["ask", "hi", "-n", "abc123def456ghi789jkl", "--json"],
        _customize_chat_ask,
    ),
    (
        "history_cmd",
        ["history", "-n", "abc123def456ghi789jkl", "--json"],
        None,
    ),
]


@pytest.mark.parametrize(
    "case_id,argv,customize",
    JSON_COMMANDS,
    ids=[c[0] for c in JSON_COMMANDS],
)
def test_json_mode_stdout_is_parseable(
    case_id: str,
    argv: list[str],
    customize,
    runner: CliRunner,
    mock_auth_env,
) -> None:
    """``--json`` stdout must be a single parseable JSON document."""
    client = _make_client(customize)
    result = _run_with_mock_client(runner, argv, client)

    assert result.exit_code == 0, (
        f"{case_id} failed (exit={result.exit_code})\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), f"{case_id}: empty stdout"

    # The contract: stdout is pure JSON (one document).
    try:
        json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{case_id}: stdout is not valid JSON ({exc})\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Spot-check: "Matched..." partial-ID print routes to stderr in --json mode.
# ---------------------------------------------------------------------------


def test_matched_partial_id_goes_to_stderr_in_json_mode(runner: CliRunner, mock_auth_env) -> None:
    """Partial-ID resolution must not corrupt --json stdout."""
    client = _make_client()
    # Use a partial ID ("abc") that uniquely matches the first stub notebook,
    # so resolve_notebook_id takes the "Matched..." branch.
    result = _run_with_mock_client(runner, ["source", "list", "-n", "abc", "--json"], client)

    assert result.exit_code == 0, result.output
    # The diagnostic line must appear on stderr — never stdout.
    assert "Matched" in result.stderr, (
        f"Expected 'Matched' on stderr, got stderr={result.stderr!r}, stdout={result.stdout!r}"
    )
    assert "Matched" not in result.stdout, (
        f"'Matched' leaked into stdout, breaking JSON contract: {result.stdout!r}"
    )
    # And stdout still parses.
    json.loads(result.stdout)


def test_matched_partial_id_still_goes_to_stdout_in_human_mode(
    runner: CliRunner, mock_auth_env
) -> None:
    """Non-JSON mode keeps the diagnostic on stdout (unchanged UX)."""
    client = _make_client()
    result = _run_with_mock_client(runner, ["source", "list", "-n", "abc"], client)

    assert result.exit_code == 0, result.output
    # Without --json, the diagnostic continues to flow through the normal
    # stdout console (user-facing message).
    assert "Matched" in result.stdout
