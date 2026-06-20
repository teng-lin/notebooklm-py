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

import httpx
import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli._chromium_profiles as chromium_profiles
import notebooklm.cli.helpers as helpers_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Artifact,
    GenerationStatus,
    Label,
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
    # Stub object that download commands hand to the injected client factory;
    # the factory ignores the auth value and returns the mock client, so the
    # auth is never inspected.
    stub_auth = MagicMock(name="AuthTokens-stub")
    with (
        patch.object(helpers_module, "load_auth_from_storage") as mock_load,
        patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch,
        patch.object(
            auth_module.AuthTokens, "from_storage", new_callable=AsyncMock
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
        "labels",
    ):
        setattr(client, ns, MagicMock())
    client.notebooks.list = AsyncMock(return_value=_stub_notebooks())
    client.notebooks.get = AsyncMock(return_value=_stub_notebooks()[0])
    client.sources.list = AsyncMock(return_value=_stub_sources())
    # Default label list: resolve_label_id walks this; tests customize per-case.
    client.labels.list = AsyncMock(return_value=[])
    client.artifacts.list = AsyncMock(return_value=[])
    client.research.poll = AsyncMock(return_value={"status": "no_research"})
    if extra_setup is not None:
        extra_setup(client)
    return client


def _run_with_mock_client(runner: CliRunner, args: list[str], client: MagicMock):
    """Invoke the CLI with ``client`` injected via ``ctx.obj``.

    The dual-path resolver (``cli.auth_runtime.resolve_client_factory``) reads
    ``ctx.obj["client_factory"]`` first, so seeding it makes every command
    construct ``client`` instead of the real ``NotebookLMClient`` -- the
    replacement for the old per-``*_cmd``-module ``patch(...)`` sweep.
    """

    def factory(auth=None, **kwargs):
        return client

    return runner.invoke(cli, args, obj={"client_factory": factory}, catch_exceptions=False)


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


def _auth_inspect_rookiepy_cookies() -> list[dict[str, object]]:
    """Return a minimal valid rookiepy cookie list for account-discovery tests."""
    return [
        {
            "domain": ".google.com",
            "name": name,
            "value": f"{name}-value",
            "path": "/",
            "secure": True,
            "expires": 9999,
            "http_only": False,
        }
        for name in ("SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSIDTS")
    ]


# ---------------------------------------------------------------------------
# Customizers: each maps a failure scenario onto the mock client.
# ---------------------------------------------------------------------------


def _fail_chat_ask(client: MagicMock) -> None:
    client.chat.ask = AsyncMock(side_effect=RuntimeError("network unreachable"))


def _fail_suggest_prompts(client: MagicMock) -> None:
    client.notebooks.suggest_prompts = AsyncMock(side_effect=RuntimeError("network unreachable"))


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


def _fail_research_cancel(client: MagicMock) -> None:
    # `research cancel` is fire-and-forget and never raises on an unknown id,
    # but a genuine transport failure from the cancel RPC must still surface as
    # the typed JSON error envelope (not a bare traceback).
    client.research.cancel = AsyncMock(side_effect=RuntimeError("net down"))


def _source_add_research_start_failed(client: MagicMock) -> None:
    """``source add-research --json`` failure: ``client.research.start`` returns falsy.

    The service returns ``outcome="start_failed"`` which the command handler
    converts to the typed JSON envelope (``VALIDATION_ERROR``, exit 1) per
    ADR-0015.
    """
    client.research.start = AsyncMock(return_value={})


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


def _fail_label_list(client: MagicMock) -> None:
    client.labels.list = AsyncMock(side_effect=RuntimeError("net down"))


def _fail_label_create(client: MagicMock) -> None:
    client.labels.create = AsyncMock(side_effect=RuntimeError("create failed"))


def _fail_label_generate(client: MagicMock) -> None:
    client.labels.generate = AsyncMock(side_effect=RuntimeError("generate failed"))


def _label_not_found(client: MagicMock) -> None:
    """No labels exist, so resolve_label_id raises NOT_FOUND for the lookup verbs."""
    client.labels.list = AsyncMock(return_value=[])


def _label_ambiguous_name(client: MagicMock) -> None:
    """Two labels share the name 'Dup', so resolve_label_id raises AMBIGUOUS_NAME."""
    client.labels.list = AsyncMock(
        return_value=[
            Label(id="lblaaa111", name="Dup", emoji="📄", source_ids=["s1"]),
            Label(id="lblbbb222", name="Dup", emoji="🧠", source_ids=[]),
        ]
    )


def _fail_notebook_create(client: MagicMock) -> None:
    """`notebook create --json` failure: client.notebooks.create raises.

    ``with_client`` wraps the body in ``handle_errors``, which catches
    ``Exception`` and routes to ``_output_error`` with code
    ``UNEXPECTED_ERROR`` (exit 2 — canonical envelope on stdout).
    """
    client.notebooks.create = AsyncMock(side_effect=RuntimeError("notebook quota exceeded"))


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
    # download flag-conflict: post-parse UsageError sites routed through the
    # JSON envelope per ADR-0015 (services/download.py build_download_plan).
    # One entry per conflict pair so a future regression in any of the three
    # _emit_flag_conflict call sites surfaces here.
    (
        "download_flag_conflict_json",
        [
            "download",
            "audio",
            "-n",
            "abc123def456ghi789jkl",
            "--force",
            "--no-clobber",
            "--json",
        ],
        None,
    ),
    (
        "download_flag_conflict_latest_earliest_json",
        [
            "download",
            "audio",
            "-n",
            "abc123def456ghi789jkl",
            "--latest",
            "--earliest",
            "--json",
        ],
        None,
    ),
    (
        "download_flag_conflict_all_artifact_json",
        [
            "download",
            "audio",
            "-n",
            "abc123def456ghi789jkl",
            "--all",
            "--artifact",
            "art123def456ghi789jkl",
            "--json",
        ],
        None,
    ),
    # research group: no research running -> nonzero exit.
    (
        "research_wait_no_research",
        ["research", "wait", "-n", "abc123def456ghi789jkl", "--json"],
        _research_no_research,
    ),
    # research cancel: a transport failure surfaces as the typed JSON envelope.
    (
        "research_cancel_rpc_failure_json",
        ["research", "cancel", "run_456", "-n", "abc123def456ghi789jkl", "--json"],
        _fail_research_cancel,
    ),
    # source add-research failure-to-start: ADR-0015 typed envelope on the
    # `start_failed` outcome from services/source_research.py.
    (
        "source_add_research_failure_json",
        ["source", "add-research", "topic", "-n", "abc123def456ghi789jkl", "--json"],
        _source_add_research_start_failed,
    ),
    # source add-research --cited-only without --import-all: post-parse
    # UsageError site routed through the JSON envelope per ADR-0015.
    (
        "source_research_cited_only_conflict_json",
        [
            "source",
            "add-research",
            "topic",
            "-n",
            "abc123def456ghi789jkl",
            "--cited-only",
            "--json",
        ],
        None,
    ),
    # ADR-0015 §2: post-parse UsageError under --json must route through the
    # typed JSON envelope rather than Click's parse-time usage text. The
    # gate fires synchronously at the top of ``research_wait`` before any
    # client call, so no customizer is needed.
    (
        "research_wait_cited_only_conflict_json",
        [
            "research",
            "wait",
            "-n",
            "abc123def456ghi789jkl",
            "--cited-only",
            "--json",
        ],
        None,
    ),
    # source add-research --no-wait with --import-all: same post-parse
    # flag-conflict path, same ADR-0015 JSON envelope.
    (
        "source_add_research_no_wait_import_all_conflict_json",
        [
            "source",
            "add-research",
            "topic",
            "-n",
            "abc123def456ghi789jkl",
            "--no-wait",
            "--import-all",
            "--json",
        ],
        None,
    ),
    # note + share + notebook + chat: client raising trips @with_client's json
    # error path (which already exits 1 -> regression guard).
    ("note_list_failure", ["note", "list", "-n", "abc", "--json"], _fail_note_list),
    # label group: list/create/generate raise on the client; the lookup verbs
    # (sources/rename/emoji/add/delete) surface the resolver's NOT_FOUND when no
    # label matches; the ambiguity case lists candidate ids (AMBIGUOUS_NAME).
    ("label_list_failure", ["label", "list", "-n", "abc", "--json"], _fail_label_list),
    (
        "label_create_failure",
        ["label", "create", "Papers", "-n", "abc", "--json"],
        _fail_label_create,
    ),
    (
        "label_generate_failure",
        ["label", "generate", "-n", "abc", "--json"],
        _fail_label_generate,
    ),
    (
        "label_sources_not_found",
        ["label", "sources", "lbl_missing", "-n", "abc", "--json"],
        _label_not_found,
    ),
    (
        "label_rename_not_found",
        ["label", "rename", "lbl_missing", "New", "-n", "abc", "--json"],
        _label_not_found,
    ),
    (
        "label_emoji_not_found",
        ["label", "emoji", "lbl_missing", "📄", "-n", "abc", "--json"],
        _label_not_found,
    ),
    (
        "label_add_not_found",
        ["label", "add", "lbl_missing", "src_1", "-n", "abc", "--json"],
        _label_not_found,
    ),
    (
        "label_remove_not_found",
        ["label", "remove", "lbl_missing", "src_1", "-n", "abc", "--json"],
        _label_not_found,
    ),
    (
        "label_delete_not_found",
        ["label", "delete", "lbl_missing", "-n", "abc", "--yes", "--json"],
        _label_not_found,
    ),
    (
        "label_sources_ambiguous_name",
        ["label", "sources", "Dup", "-n", "abc", "--json"],
        _label_ambiguous_name,
    ),
    ("share_status_failure", ["share", "status", "-n", "abc", "--json"], _fail_share_status),
    ("notebook_list_failure", ["list", "--json"], _fail_notebook_list),
    ("chat_ask_failure", ["ask", "hi", "-n", "abc", "--json"], _fail_chat_ask),
    (
        "suggest_prompts_failure",
        ["suggest-prompts", "-n", "abc", "--json"],
        _fail_suggest_prompts,
    ),
    # notebook create: with_client + RuntimeError -> UNEXPECTED_ERROR envelope.
    (
        "notebook_create_failure",
        ["create", "My Notebook", "--json"],
        _fail_notebook_create,
    ),
    # doctor + profile-list: filesystem-driven failures wrapped in the
    # canonical ADR-0015 JSON error envelope.
    ("doctor_failure", ["doctor", "--json"], None),
    ("profile_list_unauthorized", ["profile", "list", "--json"], None),
    # Per ADR-0015, post-parse ``ClickException`` validation failures in command
    # bodies and the service layer they call now flow through ``output_error``
    # and must emit a typed JSON envelope on stdout under ``--json``. The two
    # cases below cover both subclasses in the generate command tree:
    #   * ``click.UsageError`` from ``services/generate.py`` — flag-conflict
    #     validation inside the plan builder.
    #   * ``click.BadParameter`` from ``generate_cmd.py:resolve_language`` —
    #     language-code validation.
    # Both subclasses route through the same envelope shape (the shape is
    # determined by ``output_error``, not by the exception type).
    (
        "generate_video_style_conflict_json",
        [
            "generate",
            "video",
            "--style",
            "custom",
            "-n",
            "abc123def456ghi789jkl",
            "--json",
        ],
        None,
    ),
    (
        "generate_audio_language_invalid_json",
        [
            "generate",
            "audio",
            "-n",
            "abc123def456ghi789jkl",
            "--language",
            "xx_INVALID",
            "--json",
        ],
        None,
    ),
    # ADR-0015 §2: ``ask`` ``--new`` and ``--conversation-id`` are mutually
    # exclusive. Under --json the gate emits the typed JSON envelope and
    # exits 1; under text mode it still raises Click's UsageError.
    (
        "chat_new_and_conversation_id_conflict_json",
        [
            "ask",
            "hi",
            "-n",
            "abc",
            "--new",
            "--conversation-id",
            "existing-conv",
            "--json",
        ],
        None,
    ),
]


# ``pytest.param`` wraps tuple entries with marks; harvest ids from the raw
# values so ``parametrize(..., ids=...)`` continues to receive plain strings.
def _case_ids(cases) -> list[str]:
    out: list[str] = []
    for entry in cases:
        if hasattr(entry, "values"):  # pytest.ParameterSet
            out.append(entry.values[0])
        else:
            out.append(entry[0])
    return out


# Filesystem-driven failure cases bypass the mock-client harness — they
# trigger errors by mocking module-level filesystem helpers instead of
# raising on a mock client method.
_FS_FAILURE_PATCH_TARGETS = {
    "doctor_failure": "notebooklm.cli.doctor_cmd.get_storage_path",
    "profile_list_unauthorized": "notebooklm.cli.profile_cmd.list_profiles",
}


@pytest.mark.parametrize(
    "case_id,argv,customize",
    JSON_ERROR_CASES,
    ids=_case_ids(JSON_ERROR_CASES),
)
def test_json_error_exit_contract(
    case_id: str,
    argv: list[str],
    customize,
    runner: CliRunner,
    mock_auth_env,
) -> None:
    """Failure paths in --json mode must exit nonzero AND emit valid JSON."""
    fs_patch_target = _FS_FAILURE_PATCH_TARGETS.get(case_id)
    if fs_patch_target is not None:
        # doctor / profile-list: patch the module-level helper to raise OSError,
        # then invoke without the mock-client harness (these commands don't
        # construct NotebookLMClient at all).
        with patch(fs_patch_target, side_effect=OSError("permission denied")):
            result = runner.invoke(cli, argv, catch_exceptions=True)
    else:
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


def test_auth_inspect_unknown_browser(runner: CliRunner) -> None:
    """``auth inspect --json`` must envelope unknown-browser helper outcomes."""
    result = runner.invoke(
        cli,
        ["auth", "inspect", "--browser", "not-a-browser", "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1, result.stdout
    payload = _assert_json_error_contract(result, "auth_inspect_unknown_browser")
    assert payload["code"] == "UNKNOWN_BROWSER"
    assert payload["browser"] == "not-a-browser"
    assert "Unknown browser" in payload["message"]
    assert _safe_stderr(result) == ""


def test_auth_inspect_network_failure(runner: CliRunner) -> None:
    """``auth inspect --json`` must envelope account-discovery transport errors."""
    mock_rookiepy = MagicMock()
    mock_rookiepy.chrome = MagicMock(return_value=_auth_inspect_rookiepy_cookies())

    async def fail_enumerate(*args, **kwargs):
        raise httpx.RequestError("offline")

    with (
        patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
        patch.object(chromium_profiles, "discover_chromium_profiles", return_value=[]),
        patch.object(auth_module, "enumerate_accounts", new=fail_enumerate),
    ):
        result = runner.invoke(
            cli,
            ["auth", "inspect", "--browser", "chrome", "--json"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1, result.stdout
    payload = _assert_json_error_contract(result, "auth_inspect_network_failure")
    assert payload["code"] == "NETWORK_ERROR"
    assert "offline" in payload["message"]
    assert _safe_stderr(result) == ""


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


def test_download_flag_conflict_json_emits_typed_envelope(runner: CliRunner, mock_auth_env) -> None:
    """ADR-0015 spot-check: ``--force --no-clobber --json`` emits the typed
    ``VALIDATION_ERROR`` envelope and exits 1, not Click's exit-2 usage text.
    """
    client = _make_client()
    result = _run_with_mock_client(
        runner,
        [
            "download",
            "audio",
            "-n",
            "abc123def456ghi789jkl",
            "--force",
            "--no-clobber",
            "--json",
        ],
        client,
    )
    assert result.exit_code == 1, (
        f"expected exit 1 (VALIDATION_ERROR), got {result.exit_code}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{_safe_stderr(result)}"
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "error": True,
        "code": "VALIDATION_ERROR",
        "message": "Cannot specify both --force and --no-clobber",
    }, payload


def test_download_flag_conflict_text_mode_raises_click_usage_error(
    runner: CliRunner, mock_auth_env
) -> None:
    """Regression guard: in text mode (no ``--json``) the flag conflict still
    raises ``click.UsageError`` so Click's parser renders usage text on stderr
    and exits 2 (ADR-0015 Rule 4 — text-mode path preserved for this site).
    """
    client = _make_client()
    result = _run_with_mock_client(
        runner,
        [
            "download",
            "audio",
            "-n",
            "abc123def456ghi789jkl",
            "--force",
            "--no-clobber",
        ],
        client,
    )
    assert result.exit_code == 2, (
        f"expected Click UsageError exit 2, got {result.exit_code}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{_safe_stderr(result)}"
    )
    # Click writes the message to stderr (or to stdout when CliRunner mixes
    # streams). Don't depend on stream split; just verify the message text
    # surfaced and stdout has no JSON payload.
    combined = result.stdout + _safe_stderr(result)
    assert "Cannot specify both --force and --no-clobber" in combined, combined
    assert not result.stdout.strip().startswith("{"), (
        f"text mode must not emit JSON on stdout; got: {result.stdout!r}"
    )
