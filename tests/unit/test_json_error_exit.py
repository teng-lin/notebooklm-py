"""Exit-code contract for ``--json`` error payloads.

When a CLI command supports ``--json`` and fails, it must emit a parseable
JSON error payload AND exit with a nonzero status. Otherwise downstream
automation reads the JSON document, finds an ``error`` field, but
``$?`` reports success — the worst-of-both-worlds failure mode that closes
the loop on the stdout-purity work.

This sweep parametrizes over every CLI command with a ``--json`` flag and
asserts both halves of the contract for at least one realistic failure mode.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Artifact,
    GenerationStatus,
    Notebook,
    Source,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_auth_env(monkeypatch) -> Generator[None, None, None]:
    """Stub auth loading + token fetch so --json paths run offline.

    Covers three CLI auth entry points so this test file is portable across
    macOS/Ubuntu/Windows CI runners that have no ``~/.notebooklm`` storage:

    1. ``load_auth_from_storage`` — used by ``with_client``-decorated commands
       (source/artifact/chat/note/share/research/notebook/session).
    2. ``fetch_tokens_with_domains`` — token fetch on the same path.
    3. ``AuthTokens.from_storage`` — used directly by ``download`` commands,
       which bypass ``with_client``.

    Also clears ``NOTEBOOKLM_AUTH_JSON`` so a stray empty env var on the
    runner can't trip the "set but empty" pre-flight check.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    # Stub object that download commands hand to NotebookLMClient(auth); the
    # client itself is patched, so the auth value is never inspected.
    stub_auth = MagicMock(name="AuthTokens-stub")
    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as mock_fetch,
        patch(
            "notebooklm.auth.AuthTokens.from_storage", new_callable=AsyncMock
        ) as mock_from_storage,
    ):
        mock_load.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        mock_fetch.return_value = ("csrf_token", "session_id")
        mock_from_storage.return_value = stub_auth
        yield


def _stub_notebooks() -> list[Notebook]:
    return [
        Notebook(
            id="abc123def456ghi789jkl",
            title="First Notebook",
            created_at=datetime(2024, 1, 1),
            is_owner=True,
        ),
    ]


def _stub_sources() -> list[Source]:
    return [Source(id="src123def456ghi789jkl", title="Source A")]


def _make_client(extra_setup=None) -> MagicMock:
    """Build a mock client that satisfies the common --json bootstrap calls."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
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
    client.notebooks.list = AsyncMock(return_value=_stub_notebooks())
    client.notebooks.get = AsyncMock(return_value=_stub_notebooks()[0])
    client.sources.list = AsyncMock(return_value=_stub_sources())
    client.artifacts.list = AsyncMock(return_value=[])
    client.research.poll = AsyncMock(return_value={"status": "no_research"})
    if extra_setup is not None:
        extra_setup(client)
    return client


def _patch_modules() -> list:
    """Patch NotebookLMClient in every cli module that constructs it."""
    import importlib

    modules = [
        "notebooklm.cli.notebook",
        "notebooklm.cli.chat",
        "notebooklm.cli.session",
        "notebooklm.cli.share",
        "notebooklm.cli.source",
        "notebooklm.cli.artifact",
        "notebooklm.cli.research",
        "notebooklm.cli.note",
        "notebooklm.cli.generate",
        "notebooklm.cli.download",
    ]
    return [patch.object(importlib.import_module(name), "NotebookLMClient") for name in modules]


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


def _safe_stderr(result) -> str:
    """Read ``result.stderr`` without tripping click's ``mix_stderr`` guard.

    Older clicks (<8.2) raise ``ValueError`` when accessing ``.stderr`` on a
    ``CliRunner`` that mixed streams. Newer ones expose it unconditionally.
    Diagnostic output should never mask the real assertion failure.
    """
    try:
        return result.stderr
    except (ValueError, AttributeError):
        return "<stderr unavailable: mix_stderr=True>"


def _assert_json_error_contract(result, case_id: str) -> dict:
    """Assert (1) nonzero exit, (2) stdout parses as JSON, (3) error marker present.

    Returns the parsed JSON document for further inspection.
    """
    stderr = _safe_stderr(result)
    assert result.exit_code != 0, (
        f"{case_id}: expected nonzero exit, got {result.exit_code}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{stderr}"
    )
    assert result.stdout.strip(), f"{case_id}: empty stdout (no JSON payload emitted)"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{case_id}: stdout is not valid JSON ({exc})\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{stderr}"
        )
    assert isinstance(payload, dict), f"{case_id}: top-level JSON must be an object"
    # Either {"error": true, ...} (json_error_response) or {"error": "msg", ...}
    # or {"status": "error"|"timeout"|"not_found", ...} — accept any of these
    # canonical error markers.
    has_error_field = "error" in payload and payload["error"] not in (None, False)
    has_error_status = payload.get("status") in {
        "error",
        "timeout",
        "not_found",
        "no_research",
        "failed",
    }
    assert has_error_field or has_error_status, (
        f"{case_id}: JSON payload lacks an error marker — got {payload!r}"
    )
    return payload


# ---------------------------------------------------------------------------
# Customizers: each maps a failure scenario onto the mock client.
# ---------------------------------------------------------------------------


def _fail_chat_ask(client: MagicMock) -> None:
    client.chat.ask = AsyncMock(side_effect=RuntimeError("network unreachable"))


def _fail_artifact_list(client: MagicMock) -> None:
    client.artifacts.list = AsyncMock(side_effect=RuntimeError("auth: 401 Unauthorized"))


def _fail_source_list(client: MagicMock) -> None:
    client.sources.list = AsyncMock(side_effect=RuntimeError("net down"))


def _fail_note_list(client: MagicMock) -> None:
    client.notes.list = AsyncMock(side_effect=RuntimeError("net down"))


def _fail_share_status(client: MagicMock) -> None:
    client.sharing.get_status = AsyncMock(side_effect=RuntimeError("forbidden"))


def _fail_notebook_list(client: MagicMock) -> None:
    client.notebooks.list = AsyncMock(side_effect=RuntimeError("net down"))


def _research_no_research(client: MagicMock) -> None:
    # research wait + status both surface "no_research" as a failure.
    client.research.poll = AsyncMock(return_value={"status": "no_research"})


def _download_no_artifacts(client: MagicMock) -> None:
    # No completed artifacts of the requested kind -> the generic download path
    # returns {"error": "No completed ... artifacts found"} and now must exit 1.
    client.artifacts.list = AsyncMock(return_value=[])


def _artifact_wait_failed(client: MagicMock) -> None:
    # Resolve_artifact_id walks client.artifacts.list — give it one entry whose
    # ID matches the CLI argument, then return a "failed" GenerationStatus.
    client.artifacts.list = AsyncMock(
        return_value=[
            Artifact(
                id="art123def456ghi789jkl",
                title="Artifact A",
                _artifact_type=1,
                status=3,
                created_at=datetime(2024, 1, 1),
            )
        ]
    )
    client.artifacts.wait_for_completion = AsyncMock(
        return_value=GenerationStatus(
            task_id="art123def456ghi789jkl",
            status="failed",
            url=None,
            error="generation crashed",
        )
    )


def _artifact_wait_timeout(client: MagicMock) -> None:
    client.artifacts.list = AsyncMock(
        return_value=[
            Artifact(
                id="art123def456ghi789jkl",
                title="Artifact A",
                _artifact_type=1,
                status=3,
                created_at=datetime(2024, 1, 1),
            )
        ]
    )
    client.artifacts.wait_for_completion = AsyncMock(side_effect=TimeoutError("timed out"))


# ---------------------------------------------------------------------------
# Parametrized sweep
# ---------------------------------------------------------------------------


# (case_id, argv, customize_fn-or-None)
JSON_ERROR_CASES: list[tuple[str, list[str], object]] = [
    # source group: client raises -> @with_client routes to json_error_response.
    ("source_list_unauthorized", ["source", "list", "-n", "abc", "--json"], _fail_source_list),
    # artifact group
    (
        "artifact_list_unauthorized",
        ["artifact", "list", "-n", "abc", "--json"],
        _fail_artifact_list,
    ),
    (
        "artifact_wait_failed_status",
        [
            "artifact",
            "wait",
            "art123def456ghi789jkl",
            "-n",
            "abc123def456ghi789jkl",
            "--timeout",
            "1",
            "--interval",
            "1",
            "--json",
        ],
        _artifact_wait_failed,
    ),
    (
        "artifact_wait_timeout",
        [
            "artifact",
            "wait",
            "art123def456ghi789jkl",
            "-n",
            "abc123def456ghi789jkl",
            "--timeout",
            "1",
            "--interval",
            "1",
            "--json",
        ],
        _artifact_wait_timeout,
    ),
    # download group: no completed artifacts -> result contains "error" key.
    (
        "download_audio_no_artifacts",
        ["download", "audio", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    (
        "download_video_no_artifacts",
        ["download", "video", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    (
        "download_infographic_no_artifacts",
        ["download", "infographic", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    (
        "download_slide_deck_no_artifacts",
        ["download", "slide-deck", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    (
        "download_report_no_artifacts",
        ["download", "report", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    (
        "download_mind_map_no_artifacts",
        ["download", "mind-map", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    (
        "download_data_table_no_artifacts",
        ["download", "data-table", "-n", "abc123def456ghi789jkl", "--json"],
        _download_no_artifacts,
    ),
    # research group: no research running -> nonzero exit.
    (
        "research_wait_no_research",
        ["research", "wait", "-n", "abc123def456ghi789jkl", "--json"],
        _research_no_research,
    ),
    # note + share + notebook + chat: client raising trips @with_client's json
    # error path (which already exits 1 -> regression guard).
    ("note_list_failure", ["note", "list", "-n", "abc", "--json"], _fail_note_list),
    ("share_status_failure", ["share", "status", "-n", "abc", "--json"], _fail_share_status),
    ("notebook_list_failure", ["list", "--json"], _fail_notebook_list),
    ("chat_ask_failure", ["ask", "hi", "-n", "abc", "--json"], _fail_chat_ask),
]


@pytest.mark.parametrize(
    "case_id,argv,customize",
    JSON_ERROR_CASES,
    ids=[c[0] for c in JSON_ERROR_CASES],
)
def test_json_error_exit_contract(
    case_id: str,
    argv: list[str],
    customize,
    runner: CliRunner,
    mock_auth_env,
) -> None:
    """Failure paths in --json mode must exit nonzero AND emit valid JSON."""
    client = _make_client(customize)
    result = _run_with_mock_client(runner, argv, client)
    _assert_json_error_contract(result, case_id)


# ---------------------------------------------------------------------------
# Spot checks for specific code paths the audit called out.
# ---------------------------------------------------------------------------


def test_auth_check_json_exits_nonzero_on_missing_storage(
    tmp_path, runner: CliRunner, monkeypatch
) -> None:
    """`auth check --json` must exit nonzero when storage is missing.

    Previously: status="error" in the JSON payload but exit code 0.
    """
    # NOTEBOOKLM_AUTH_JSON would short-circuit the storage-existence check;
    # ensure it's unset for this run.
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))

    missing = tmp_path / "definitely-does-not-exist" / "storage_state.json"
    result = runner.invoke(
        cli,
        ["--storage", str(missing), "auth", "check", "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code != 0, (
        f"auth check --json must exit nonzero on missing storage; "
        f"got {result.exit_code}\nstdout:\n{result.stdout}"
    )
    payload = json.loads(result.stdout)
    assert payload.get("status") == "error", payload


def test_download_audio_non_json_mode_still_exits_nonzero(runner: CliRunner, mock_auth_env) -> None:
    """Regression guard: non-JSON failure still exits nonzero (unchanged behavior)."""
    client = _make_client(_download_no_artifacts)
    result = _run_with_mock_client(
        runner,
        ["download", "audio", "-n", "abc123def456ghi789jkl"],
        client,
    )
    assert result.exit_code != 0, (
        f"non-JSON failure must keep exiting nonzero; got {result.exit_code}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{_safe_stderr(result)}"
    )
