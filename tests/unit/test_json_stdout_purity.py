"""Stdout-purity contract for ``--json`` mode.

Any CLI command that supports ``--json`` MUST emit nothing on stdout except
the JSON payload, so that ``json.loads(result.stdout)`` succeeds for downstream
automation. Diagnostic prints (status text, partial-ID "Matched..." hints,
Rich live status) belong on stderr.

This test suite locks the contract for the two known violators called out in
audit K2 / codex #4 — and adds a parametrized sweep across every CLI command
that exposes a ``--json`` flag so future regressions surface immediately.

It also walks the live Click tree to assert that every ``--json``-capable
command has a sweep entry (or an explicitly-justified waiver) in both this
file's ``JSON_COMMANDS`` (success path) AND the sibling
``test_json_error_exit.py``'s ``JSON_ERROR_CASES`` (error path). Adding a new
``--json`` command without coverage fails this inventory test by name.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from notebooklm import paths as paths_module
from notebooklm.notebooklm_cli import cli
from notebooklm.rpc.types import ShareAccess, ShareViewLevel
from notebooklm.types import (
    Artifact,
    AskResult,
    Note,
    Notebook,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    ShareStatus,
    Source,
    SourceGuide,
)


def _research_task(spec: dict) -> ResearchTask:
    """Build a typed ``ResearchTask`` from a legacy poll/wait dict spec."""
    try:
        status = ResearchStatus(spec.get("status", "no_research"))
    except ValueError:
        status = ResearchStatus.FAILED
    raw_sources = spec.get("sources") or []
    sources = tuple(ResearchSource.from_public_dict(s) for s in raw_sources if isinstance(s, dict))
    return ResearchTask(
        task_id=spec.get("task_id", ""),
        status=status,
        query=spec.get("query", ""),
        sources=sources,
        summary=spec.get("summary", ""),
        report=spec.get("report", ""),
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
    client.research.poll = AsyncMock(return_value=_research_task({"status": "no_research"}))

    async def wait_for_research_completion(
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        interval: float = 5,
        initial_interval: float | None = None,
    ) -> ResearchTask:
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        effective_interval = initial_interval if initial_interval is not None else interval
        if effective_interval <= 0:
            raise ValueError("poll interval must be positive")
        pinned_task_id = task_id
        attempts = max(1, math.ceil(timeout / effective_interval) + 1)
        status: ResearchTask = _research_task({"status": "no_research"})
        for _ in range(attempts):
            status = await client.research.poll(notebook_id, task_id=pinned_task_id)
            if pinned_task_id is None and status.task_id:
                pinned_task_id = status.task_id
            status_val = status.status
            if status_val in ("completed", "failed"):
                return status
            if status_val == "no_research" and pinned_task_id is None:
                return status
        raise TimeoutError(f"Research task {pinned_task_id or 'unknown'} timed out")

    client.research.wait_for_completion = AsyncMock(side_effect=wait_for_research_completion)
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
        return_value=SourceGuide(summary="a summary", keywords=["k1", "k2"])
    )


def _customize_source_add_research(client: MagicMock) -> None:
    client.research.start = AsyncMock(
        return_value=ResearchStart(
            task_id="task_123",
            report_id=None,
            notebook_id="abc123def456ghi789jkl",
            query="",
            mode="fast",
        )
    )


def _customize_research_wait(client: MagicMock) -> None:
    # research wait polls until status == "completed". Return a completed
    # payload immediately so the loop exits on the first iteration.
    client.research.poll = AsyncMock(
        return_value=_research_task(
            {
                "status": "completed",
                "sources": [],
                "query": "",
                "report": "",
            }
        )
    )


def _customize_notebook_create(client: MagicMock) -> None:
    # `notebook create --json` calls `client.notebooks.create(title)`
    # and emits a JSON payload with the new notebook's id/title/created_at.
    client.notebooks.create = AsyncMock(
        return_value=Notebook(
            id="newxyz123abc456def789",
            title="My Notebook",
            created_at=datetime(2024, 1, 1),
            is_owner=True,
        )
    )


# ---------------------------------------------------------------------------
# Filesystem-driven --json commands (doctor, profile list).
# These cases bypass NotebookLMClient entirely and read ~/.notebooklm/...,
# so the mock-client harness can't drive them. The parametrized sweep
# dispatches on case_id and invokes them via the ``_setup_fs_<case>`` helpers
# below, which take ``tmp_path`` + ``monkeypatch`` like the live doctor test
# suite (tests/unit/cli/test_doctor.py) does.
# ---------------------------------------------------------------------------


FILESYSTEM_DRIVEN_CASES = frozenset({"doctor", "profile_list"})


def _setup_fs_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lay out a clean profile so ``doctor --json`` reports all-pass.

    Mirrors the ``test_doctor_reports_clean_profile_layout`` fixture in
    tests/unit/cli/test_doctor.py. A clean layout makes every check pass so the
    command exits 0 (since #1160 a lingering ``status: "fail"`` exits 1) while
    still emitting a single well-formed JSON document — which is what this
    success-path stdout-purity sweep asserts.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths_module.set_active_profile(None)
    paths_module._reset_config_cache()
    profile_dir = tmp_path / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    if sys.platform != "win32":
        profile_dir.chmod(0o700)
    storage = profile_dir / "storage_state.json"
    storage.write_text(
        json.dumps({"cookies": [{"name": "SID", "value": "x"}]}),
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"default_profile": "default"}),
        encoding="utf-8",
    )


def _setup_fs_profile_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lay out at least one profile so ``profile list --json`` emits a list."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths_module.set_active_profile(None)
    paths_module._reset_config_cache()
    profile_dir = tmp_path / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    if sys.platform != "win32":
        profile_dir.chmod(0o700)
    # No storage_state.json -> authenticated=False in the payload, which
    # is still a valid success case (profile list emits the row regardless).


_FS_SETUPS = {
    "doctor": _setup_fs_doctor,
    "profile_list": _setup_fs_profile_list,
}


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
    (
        "source_add_research",
        [
            "source",
            "add-research",
            "topic",
            "-n",
            "abc123def456ghi789jkl",
            "--no-wait",
            "--json",
        ],
        _customize_source_add_research,
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
    # doctor / profile / notebook-create coverage (meta-audit G9 + I7 + I9):
    # `doctor` and `profile list` read NOTEBOOKLM_HOME directly and don't
    # build a NotebookLMClient — the parametrized test dispatches on these
    # case_ids and uses the ``_setup_fs_<case>`` helpers above instead of
    # the mock-client harness. `notebook_create` goes through the standard
    # `with_client` path, so its customizer just preps the mock client.
    ("doctor", ["doctor", "--json"], None),
    ("profile_list", ["profile", "list", "--json"], None),
    ("notebook_create", ["create", "My Notebook", "--json"], _customize_notebook_create),
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` stdout must be a single parseable JSON document."""
    if case_id in FILESYSTEM_DRIVEN_CASES:
        # Filesystem-driven commands (doctor, profile list) skip the mock-client
        # harness — they read NOTEBOOKLM_HOME directly. Stage a clean layout
        # under tmp_path so the command emits valid JSON without touching the
        # caller's real profile. The active-profile/config cache lives at module
        # scope; reset on exit so a later test doesn't inherit our tmp profile.
        _FS_SETUPS[case_id](tmp_path, monkeypatch)
        try:
            result = runner.invoke(cli, argv, catch_exceptions=False)
        finally:
            paths_module.set_active_profile(None)
            paths_module._reset_config_cache()
    else:
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
# Dynamic inventory: every --json command in the Click tree must have a
# sweep entry (success + error) or an explicit waiver with rationale.
# ---------------------------------------------------------------------------


def _walk_json_command_paths(root: click.BaseCommand) -> set[tuple[str, ...]]:
    """Walk the Click tree and return the path tuple of every leaf command
    that exposes a ``--json`` option.

    The path is the sequence of group/command names from the root (e.g.
    ``("source", "list")``). Top-level commands have a 1-tuple path
    (e.g. ``("doctor",)``).
    """
    paths: set[tuple[str, ...]] = set()

    def visit(node: click.BaseCommand, path: tuple[str, ...]) -> None:
        if isinstance(node, click.Group):
            for name, child in node.commands.items():
                visit(child, path + (name,))
            return
        for param in node.params:
            if isinstance(param, click.Option) and "--json" in param.opts:
                paths.add(path)
                return

    visit(root, ())
    return paths


def _resolve_path_from_argv(argv: list[str], root: click.BaseCommand = cli) -> tuple[str, ...]:
    """Walk the Click tree using ``argv`` tokens to derive the command path.

    Stops on the first token that's not a sub-command name (a flag, a
    positional argument like a notebook ID, etc.) — so ``["ask", "hi",
    "-n", "abc", "--json"]`` resolves to ``("ask",)`` because ``ask`` is
    a leaf Command (not a Group).
    """
    path: list[str] = []
    current: click.BaseCommand = root
    for token in argv:
        if token.startswith("-"):
            break
        if isinstance(current, click.Group) and token in current.commands:
            current = current.commands[token]
            path.append(token)
        else:
            break
    return tuple(path)


# Waivers are dict[path -> rationale]. The dynamic inventory test accepts a
# --json command as "covered" if it's either in the sweep list OR in the
# matching waiver dict. Every waiver must carry a rationale string; silent
# waivers are forbidden (the lint below enforces non-empty rationale text).
#
# Goal: each entry that lands a real test moves out of the waiver dict and
# into JSON_COMMANDS / JSON_ERROR_CASES. The waiver dicts shrink monotonically
# as coverage grows.
_GENERATE_RATIONALE = (
    "RPC-side artifact generation; covered by integration tests against the "
    "real backend, not the JSON stdout-purity unit sweep."
)
_DOWNLOAD_RATIONALE_SUCCESS = (
    "Download success requires a real completed artifact + HTTP fetch; out of "
    "scope for the unit-level JSON stdout sweep — covered by e2e tests."
)
_DOWNLOAD_RATIONALE_ERROR = (
    "Download error envelope is covered for audio/video/infographic/slide-deck/"
    "report/mind-map/data-table in the error sweep; the remaining variants "
    "share the same generic download path and can grow sweep entries "
    "incrementally."
)
_AUTH_RATIONALE = (
    "Auth-flow command; success path depends on browser/cookie state and is "
    "covered by tests/unit/cli/test_session_*.py + test_doctor.py."
)
_MUTATION_RATIONALE_SUCCESS = (
    "Mutation command (delete/rename/save/refresh/clean/add); success path is "
    "covered by tests/unit/cli/test_<group>_*.py — sweep entries can land here "
    "incrementally as the JSON purity contract reaches each command."
)
_MUTATION_RATIONALE_ERROR = (
    "Mutation command; error path is covered by tests/unit/cli/test_<group>_*.py — "
    "sweep entries can land here incrementally as the JSON error envelope "
    "contract reaches each command."
)
_INTROSPECTION_RATIONALE = (
    "Read-only introspection command without a network round-trip in the "
    "success path; ``--json`` purity is checked indirectly by the format unit "
    "tests in tests/unit/cli/."
)


JSON_SUCCESS_WAIVED: dict[tuple[str, ...], str] = {
    # artifact group — get/poll/wait need a real or fully-stubbed generation
    # status payload that round-trips through the artifact formatter chain.
    ("artifact", "delete"): _MUTATION_RATIONALE_SUCCESS,
    ("artifact", "export"): _MUTATION_RATIONALE_SUCCESS,
    ("artifact", "get"): _MUTATION_RATIONALE_SUCCESS,
    ("artifact", "poll"): _MUTATION_RATIONALE_SUCCESS,
    ("artifact", "rename"): _MUTATION_RATIONALE_SUCCESS,
    ("artifact", "wait"): _MUTATION_RATIONALE_SUCCESS,
    # auth-flow commands (covered by dedicated test files).
    ("auth", "check"): _AUTH_RATIONALE,
    ("auth", "inspect"): _AUTH_RATIONALE,
    ("configure",): _AUTH_RATIONALE,
    # top-level notebook `delete` mutation — success path is covered by
    # tests/unit/cli/test_notebook.py::TestNotebookDelete.
    ("delete",): _MUTATION_RATIONALE_SUCCESS,
    # download group — success path needs a real artifact + HTTP fetch.
    ("download", "audio"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "cinematic-video"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "data-table"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "flashcards"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "infographic"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "mind-map"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "quiz"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "report"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "slide-deck"): _DOWNLOAD_RATIONALE_SUCCESS,
    ("download", "video"): _DOWNLOAD_RATIONALE_SUCCESS,
    # generate group — RPC-driven artifact creation, covered by integration tests.
    ("generate", "audio"): _GENERATE_RATIONALE,
    ("generate", "cinematic-video"): _GENERATE_RATIONALE,
    ("generate", "data-table"): _GENERATE_RATIONALE,
    ("generate", "flashcards"): _GENERATE_RATIONALE,
    ("generate", "infographic"): _GENERATE_RATIONALE,
    ("generate", "mind-map"): _GENERATE_RATIONALE,
    ("generate", "quiz"): _GENERATE_RATIONALE,
    ("generate", "report"): _GENERATE_RATIONALE,
    ("generate", "revise-slide"): _GENERATE_RATIONALE,
    ("generate", "slide-deck"): _GENERATE_RATIONALE,
    ("generate", "video"): _GENERATE_RATIONALE,
    # language group — read-only introspection of the current notebook's
    # language setting.
    ("language", "get"): _INTROSPECTION_RATIONALE,
    ("language", "list"): _INTROSPECTION_RATIONALE,
    ("language", "set"): _MUTATION_RATIONALE_SUCCESS,
    # note group — note list/save success is covered; remaining are mutations.
    ("note", "create"): _MUTATION_RATIONALE_SUCCESS,
    ("note", "delete"): _MUTATION_RATIONALE_SUCCESS,
    ("note", "get"): _MUTATION_RATIONALE_SUCCESS,
    ("note", "rename"): _MUTATION_RATIONALE_SUCCESS,
    ("note", "save"): _MUTATION_RATIONALE_SUCCESS,
    # share mutations (status/public/view-level are covered).
    ("share", "add"): _MUTATION_RATIONALE_SUCCESS,
    ("share", "remove"): _MUTATION_RATIONALE_SUCCESS,
    ("share", "update"): _MUTATION_RATIONALE_SUCCESS,
    # source group — list/fulltext/guide success are covered; remaining are
    # mutations or wait-loops.
    ("source", "add"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "add-drive"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "clean"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "delete"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "delete-by-title"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "get"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "refresh"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "rename"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "stale"): _MUTATION_RATIONALE_SUCCESS,
    ("source", "wait"): _MUTATION_RATIONALE_SUCCESS,
    # `use` mutates the active notebook context (filesystem state); a sweep
    # entry would duplicate tests/unit/cli/test_session_characterization.py.
    ("use",): _MUTATION_RATIONALE_SUCCESS,
}


JSON_ERROR_WAIVED: dict[tuple[str, ...], str] = {
    # artifact group — error envelope is covered for list + wait. Remaining
    # entries are mutations that surface @with_client's UNEXPECTED_ERROR
    # envelope on RPC failure; coverage can grow with the suite.
    ("artifact", "delete"): _MUTATION_RATIONALE_ERROR,
    ("artifact", "export"): _MUTATION_RATIONALE_ERROR,
    ("artifact", "get"): _MUTATION_RATIONALE_ERROR,
    ("artifact", "poll"): _MUTATION_RATIONALE_ERROR,
    ("artifact", "rename"): _MUTATION_RATIONALE_ERROR,
    ("artifact", "suggestions"): _MUTATION_RATIONALE_ERROR,
    # auth-flow error paths (covered by dedicated test files).
    ("auth", "check"): _AUTH_RATIONALE,
    ("auth", "inspect"): _AUTH_RATIONALE,
    ("configure",): _AUTH_RATIONALE,
    # top-level notebook `delete` mutation — error path is covered by
    # tests/unit/cli/test_notebook.py::TestNotebookDelete.
    ("delete",): _MUTATION_RATIONALE_ERROR,
    # download group — these error paths are covered for audio/video/...; the
    # remaining download_* cases below haven't been added yet.
    ("download", "cinematic-video"): _DOWNLOAD_RATIONALE_ERROR,
    ("download", "flashcards"): _DOWNLOAD_RATIONALE_ERROR,
    ("download", "quiz"): _DOWNLOAD_RATIONALE_ERROR,
    # generate group — RPC-driven; error envelope covered by integration tests.
    ("generate", "cinematic-video"): _GENERATE_RATIONALE,
    ("generate", "data-table"): _GENERATE_RATIONALE,
    ("generate", "flashcards"): _GENERATE_RATIONALE,
    ("generate", "infographic"): _GENERATE_RATIONALE,
    ("generate", "mind-map"): _GENERATE_RATIONALE,
    ("generate", "quiz"): _GENERATE_RATIONALE,
    ("generate", "report"): _GENERATE_RATIONALE,
    ("generate", "revise-slide"): _GENERATE_RATIONALE,
    ("generate", "slide-deck"): _GENERATE_RATIONALE,
    # history / metadata / research-status / status: success-only sweep is the
    # primary contract; error envelope can grow with the suite.
    ("history",): _INTROSPECTION_RATIONALE,
    ("metadata",): _INTROSPECTION_RATIONALE,
    ("research", "status"): _INTROSPECTION_RATIONALE,
    ("status",): _INTROSPECTION_RATIONALE,
    # language group — see success rationale.
    ("language", "get"): _INTROSPECTION_RATIONALE,
    ("language", "list"): _INTROSPECTION_RATIONALE,
    ("language", "set"): _MUTATION_RATIONALE_ERROR,
    # note + share error paths for the remaining mutations.
    ("note", "create"): _MUTATION_RATIONALE_ERROR,
    ("note", "delete"): _MUTATION_RATIONALE_ERROR,
    ("note", "get"): _MUTATION_RATIONALE_ERROR,
    ("note", "rename"): _MUTATION_RATIONALE_ERROR,
    ("note", "save"): _MUTATION_RATIONALE_ERROR,
    ("share", "add"): _MUTATION_RATIONALE_ERROR,
    ("share", "public"): _MUTATION_RATIONALE_ERROR,
    ("share", "remove"): _MUTATION_RATIONALE_ERROR,
    ("share", "update"): _MUTATION_RATIONALE_ERROR,
    ("share", "view-level"): _MUTATION_RATIONALE_ERROR,
    # source group — list error is covered; remaining mutations + introspection.
    ("source", "add"): _MUTATION_RATIONALE_ERROR,
    ("source", "add-drive"): _MUTATION_RATIONALE_ERROR,
    ("source", "clean"): _MUTATION_RATIONALE_ERROR,
    ("source", "delete"): _MUTATION_RATIONALE_ERROR,
    ("source", "delete-by-title"): _MUTATION_RATIONALE_ERROR,
    ("source", "fulltext"): _INTROSPECTION_RATIONALE,
    ("source", "get"): _MUTATION_RATIONALE_ERROR,
    ("source", "guide"): _INTROSPECTION_RATIONALE,
    ("source", "refresh"): _MUTATION_RATIONALE_ERROR,
    ("source", "rename"): _MUTATION_RATIONALE_ERROR,
    ("source", "stale"): _INTROSPECTION_RATIONALE,
    ("source", "wait"): _MUTATION_RATIONALE_ERROR,
    # `use` context mutation — covered by session_characterization.
    ("use",): _MUTATION_RATIONALE_ERROR,
}


def _success_covered_paths() -> set[tuple[str, ...]]:
    return {_resolve_path_from_argv(argv) for _case_id, argv, _customize in JSON_COMMANDS}


def _load_error_cases() -> list[tuple[str, list[str], object]]:
    """Side-load ``JSON_ERROR_CASES`` from the sibling test file.

    ``tests/`` is collected by pytest but not exposed as a Python package
    (no ``__init__.py``), so a plain ``from tests.unit.test_json_error_exit
    import JSON_ERROR_CASES`` fails at runtime. Load the sibling module by
    file path instead — this also keeps the import lazy so a parse error in
    the sibling file surfaces here as a clear inventory-test failure
    instead of polluting this module's collection.
    """
    import importlib.util

    sibling_path = Path(__file__).parent / "test_json_error_exit.py"
    spec = importlib.util.spec_from_file_location(
        "tests.unit._test_json_error_exit_sibling", sibling_path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not load sibling test module from {sibling_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = module.JSON_ERROR_CASES
    # ``pytest.param(..., marks=...)`` wraps entries in ParameterSet (exposed
    # via ``.values``). Raw tuples have no such attribute. Normalize both.
    unwrapped: list[tuple[str, list[str], object]] = []
    for entry in cases:
        values = tuple(entry.values) if hasattr(entry, "values") else tuple(entry)
        unwrapped.append((values[0], list(values[1]), values[2]))
    return unwrapped


def _error_covered_paths() -> set[tuple[str, ...]]:
    return {_resolve_path_from_argv(argv) for _case_id, argv, _customize in _load_error_cases()}


def _build_coverage_report(
    discovered: set[tuple[str, ...]],
    success_paths: set[tuple[str, ...]],
    error_paths: set[tuple[str, ...]],
) -> str:
    """Render the inventory coverage report.

    Format intentionally matches the audit plan's "X of Y --json commands have
    success + error entries" wording so the line is grep-friendly.
    """
    total = len(discovered)
    both = len({p for p in discovered if p in success_paths and p in error_paths})
    return (
        "\nJSON purity coverage report:\n"
        f"  {len(success_paths)} of {total} --json commands have a success entry "
        "(JSON_COMMANDS sweep)\n"
        f"  {len(error_paths)} of {total} --json commands have an error entry "
        "(JSON_ERROR_CASES sweep)\n"
        f"  {both} of {total} --json commands have success + error entries\n"
        f"  Waivers (with rationale): {len(JSON_SUCCESS_WAIVED)} success, "
        f"{len(JSON_ERROR_WAIVED)} error\n"
    )


def test_all_json_commands_have_sweep_entry() -> None:
    """Every ``--json`` CLI command must appear in the sweep or in a waiver.

    The Click tree is the source of truth: any leaf command with a ``--json``
    flag MUST be covered by

      * a ``JSON_COMMANDS`` entry **or** ``JSON_SUCCESS_WAIVED`` rationale
        (success-path coverage), **and**
      * a ``JSON_ERROR_CASES`` entry **or** ``JSON_ERROR_WAIVED`` rationale
        (error-path coverage).

    Adding a new ``--json`` command without coverage fails this test by name —
    the message lists exactly which paths are uncovered, so the fix is to
    either land a sweep entry or add a justified waiver row to the relevant
    dict above.

    The coverage report is always rendered: on assertion failure it's
    appended to the message, and on pass it goes through pytest's captured
    stdout (visible under ``pytest -s`` or in test artifacts). The waiver
    dicts are designed to shrink monotonically as real sweep entries land.
    """
    discovered = _walk_json_command_paths(cli)
    success_paths = _success_covered_paths()
    error_paths = _error_covered_paths()

    missing_success = sorted(
        path for path in discovered if path not in success_paths and path not in JSON_SUCCESS_WAIVED
    )
    missing_error = sorted(
        path for path in discovered if path not in error_paths and path not in JSON_ERROR_WAIVED
    )

    # Stale-waiver lint: a waiver for a command that no longer has --json
    # (or no longer exists) is a footgun — remove it from the dict.
    stale_success_waivers = sorted(p for p in JSON_SUCCESS_WAIVED if p not in discovered)
    stale_error_waivers = sorted(p for p in JSON_ERROR_WAIVED if p not in discovered)

    # Silent-waiver lint: every waiver entry must carry a non-empty rationale.
    empty_success_rationale = sorted(p for p, r in JSON_SUCCESS_WAIVED.items() if not r.strip())
    empty_error_rationale = sorted(p for p, r in JSON_ERROR_WAIVED.items() if not r.strip())
    redundant_success_waivers = sorted(
        cmd_path for cmd_path in JSON_SUCCESS_WAIVED if cmd_path in success_paths
    )
    redundant_error_waivers = sorted(
        cmd_path for cmd_path in JSON_ERROR_WAIVED if cmd_path in error_paths
    )

    report = _build_coverage_report(discovered, success_paths, error_paths)
    # Emit to the captured stdout so ``pytest -s`` surfaces the line; pytest
    # also replays captured stdout on assertion failure, which is when the
    # operator most wants to see the split.
    print(report)

    failures: list[str] = []
    if missing_success:
        failures.append(
            "Missing success-path coverage (add to JSON_COMMANDS or "
            f"JSON_SUCCESS_WAIVED): {missing_success}"
        )
    if missing_error:
        failures.append(
            "Missing error-path coverage (add to JSON_ERROR_CASES in "
            f"test_json_error_exit.py or JSON_ERROR_WAIVED): {missing_error}"
        )
    if stale_success_waivers:
        failures.append(
            "Stale entries in JSON_SUCCESS_WAIVED (no longer have --json or "
            f"don't exist): {stale_success_waivers}"
        )
    if stale_error_waivers:
        failures.append(
            "Stale entries in JSON_ERROR_WAIVED (no longer have --json or "
            f"don't exist): {stale_error_waivers}"
        )
    if empty_success_rationale:
        failures.append(
            f"Silent waiver(s) in JSON_SUCCESS_WAIVED (empty rationale): {empty_success_rationale}"
        )
    if empty_error_rationale:
        failures.append(
            f"Silent waiver(s) in JSON_ERROR_WAIVED (empty rationale): {empty_error_rationale}"
        )
    if redundant_success_waivers:
        failures.append(
            "Redundant entries in JSON_SUCCESS_WAIVED (sweep entry now exists): "
            f"{redundant_success_waivers}"
        )
    if redundant_error_waivers:
        failures.append(
            "Redundant entries in JSON_ERROR_WAIVED (sweep entry now exists): "
            f"{redundant_error_waivers}"
        )
    assert not failures, "\n".join(failures) + "\n" + report


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
