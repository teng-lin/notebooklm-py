"""CLI contract baseline: pins the public command tree, options, and help.

This file characterizes public CLI behavior so later refactors (and intentional
surface changes) can be compared against the JSON baseline generated from
``build_cli_contract``. Regenerate the baseline after an intended change with::

    uv run python tests/unit/cli/test_cli_contract.py > tests/fixtures/cli_contract_baseline.json
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

import notebooklm.cli.helpers as helpers_module
import notebooklm.client as client_module
from notebooklm.notebooklm_cli import cli

ROOT_COMMAND = "notebooklm"


def _find_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Could not locate repository root")


BASELINE_PATH = _find_repo_root() / "tests/fixtures/cli_contract_baseline.json"

TRACKED_GROUPS = (
    "download",
    "source",
    "generate",
    "artifact",
    "session",
    "profile",
    "notebook",
    "chat",
    "note",
    "label",
    "share",
    "research",
)

CLICK_GROUPS = (
    "agent",
    "download",
    "source",
    "generate",
    "artifact",
    "language",
    "profile",
    "note",
    "label",
    "share",
    "research",
    "skill",
)

TOP_LEVEL_SURFACES = {
    "session": ("login", "auth", "use", "status", "clear"),
    "notebook": ("list", "create", "delete", "rename", "metadata", "summary"),
    "chat": ("ask", "configure", "history"),
}

EXTRA_TOP_LEVEL_COMMANDS = ("completion", "doctor")

HELP_SNIPPETS = {
    "": ("NotebookLM CLI", "notebooklm login", "completion"),
    "completion": ("Print the shell completion script", "bash", "zsh", "fish"),
    "download": ("Download generated content", "cinematic-video", "flashcards"),
    "download audio": ("Download audio", "--latest", "--no-clobber"),
    "source add": ("--follow-symlinks", "--mime-type", "--json"),
    "share public": ("--enable", "--disable", "--json"),
    "research wait": ("--import-all", "--cited-only", "--timeout"),
    "label generate": ("--scope", "all", "unlabeled", "--yes"),
}


class _Stub:
    def __init__(self, stub_id: str, title: str = "") -> None:
        self.id = stub_id
        self.title = title


class _CompletionCtx:
    def __init__(self, notebook_id: str) -> None:
        self.params = {"notebook_id": notebook_id}
        self.parent = None


def _ctx_with_notebook(notebook_id: str = "nb_contract") -> _CompletionCtx:
    return _CompletionCtx(notebook_id)


def _command_for(path: str) -> click.Command:
    cmd: click.Command = cli
    if not path or path == ROOT_COMMAND:
        return cmd
    for part in path.split():
        if not isinstance(cmd, click.Group):
            raise AssertionError(f"{path!r} traversed through non-group {cmd!r}")
        next_cmd = cmd.get_command(click.Context(cmd), part)
        if next_cmd is None:
            raise AssertionError(f"missing command path: {path!r}")
        cmd = next_cmd
    return cmd


def _json_default(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return [_json_default(v) for v in value]
    if isinstance(value, list):
        return [_json_default(v) for v in value]
    return str(value)


def _type_contract(param_type: click.ParamType) -> dict[str, object]:
    data: dict[str, object] = {"name": param_type.name}
    if isinstance(param_type, click.Choice):
        data["choices"] = list(param_type.choices)
        data["case_sensitive"] = param_type.case_sensitive
    if isinstance(param_type, click.IntRange):
        data["min"] = param_type.min
        data["max"] = param_type.max
        data["clamp"] = param_type.clamp
    if isinstance(param_type, click.Path):
        data["exists"] = param_type.exists
        data["file_okay"] = param_type.file_okay
        data["dir_okay"] = param_type.dir_okay
        data["writable"] = param_type.writable
        data["readable"] = param_type.readable
        data["executable"] = param_type.executable
        data["resolve_path"] = param_type.resolve_path
        data["allow_dash"] = param_type.allow_dash
    return data


def _has_custom_shell_complete(param: click.Option) -> bool:
    return getattr(param, "_custom_shell_complete", None) is not None


def _visible_command_names(group: click.Group) -> list[str]:
    ctx = click.Context(group)
    names = group.list_commands(ctx)
    return [name for name in names if not getattr(group.get_command(ctx, name), "hidden", False)]


def _param_contract(param: click.Parameter) -> dict[str, object]:
    base: dict[str, object] = {
        "name": param.name,
        "required": param.required,
        "type": _type_contract(param.type),
    }
    if isinstance(param, click.Option):
        base.update(
            {
                "kind": "option",
                "opts": list(param.opts),
                "secondary_opts": list(param.secondary_opts),
                "default": _json_default(param.default),
                "envvar": _json_default(param.envvar),
                "is_flag": param.is_flag,
                "multiple": param.multiple,
                "help": param.help,
                "has_custom_shell_complete": _has_custom_shell_complete(param),
            }
        )
    else:
        base.update({"kind": "argument", "nargs": param.nargs})
    return base


def _command_contract(path: str) -> dict[str, object]:
    cmd = _command_for(path)
    data: dict[str, object] = {
        "class": type(cmd).__name__,
        "params": [_param_contract(param) for param in cmd.params],
        "short_help": cmd.get_short_help_str(),
    }
    if isinstance(cmd, click.Group):
        data["commands"] = _visible_command_names(cmd)
    return data


def _option_by_name(path: str, name: str) -> click.Option:
    for param in _command_for(path).params:
        if isinstance(param, click.Option) and param.name == name:
            return param
    raise AssertionError(f"{path!r} has no option named {name!r}")


def _iter_command_paths(path: str) -> list[str]:
    cmd = _command_for(path)
    paths = [path]
    if isinstance(cmd, click.Group):
        for child in _visible_command_names(cmd):
            child_path = f"{path} {child}" if path else child
            paths.extend(_iter_command_paths(child_path))
    return paths


def _tracked_command_paths() -> list[str]:
    paths: list[str] = [ROOT_COMMAND]
    for group in CLICK_GROUPS:
        paths.extend(_iter_command_paths(group))
    for commands in TOP_LEVEL_SURFACES.values():
        for name in commands:
            paths.extend(_iter_command_paths(name))
    for name in EXTRA_TOP_LEVEL_COMMANDS:
        paths.extend(_iter_command_paths(name))
    return sorted(set(paths))


def _same_params(left: click.Command, right: click.Command) -> bool:
    return [_param_contract(p) for p in left.params] == [_param_contract(p) for p in right.params]


def build_cli_contract() -> dict[str, object]:
    """Return the deterministic public CLI inventory used by the baseline."""
    download_cinematic_video = _command_for("download cinematic-video")
    download_video = _command_for("download video")
    generate_cinematic_video = _command_for("generate cinematic-video")
    generate_video = _command_for("generate video")
    return {
        "schema_version": 1,
        "tracked_surfaces": list(TRACKED_GROUPS),
        "root_commands": _visible_command_names(cli),
        "top_level_surfaces": {key: list(value) for key, value in TOP_LEVEL_SURFACES.items()},
        "click_groups": {
            group: _visible_command_names(_command_for(group)) for group in CLICK_GROUPS
        },
        "aliases": {
            "download cinematic-video": {
                "canonical": "download video",
                "same_callback": download_cinematic_video.callback is download_video.callback,
                "same_params": _same_params(download_cinematic_video, download_video),
            },
            "generate cinematic-video": {
                "canonical": "generate video --format cinematic",
                "same_callback": generate_cinematic_video.callback is generate_video.callback,
                "same_params": _same_params(generate_cinematic_video, generate_video),
            },
        },
        "completion_callbacks": {
            "notebook": _has_custom_shell_complete(_option_by_name("source list", "notebook_id")),
            "download_artifact": _has_custom_shell_complete(
                _option_by_name("download audio", "artifact_id")
            ),
        },
        "commands": {path: _command_contract(path) for path in _tracked_command_paths()},
    }


def test_cli_contract_matches_baseline() -> None:
    """Public command tree, options, defaults, help, and aliases match the baseline."""
    expected = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert build_cli_contract() == expected


@pytest.mark.parametrize("path,snippets", HELP_SNIPPETS.items(), ids=lambda value: value or "root")
def test_representative_help_snippets_remain_visible(path: str, snippets: tuple[str, ...]) -> None:
    argv = [*path.split(), "--help"] if path else ["--help"]
    result = CliRunner().invoke(cli, argv)

    assert result.exit_code == 0, result.output
    for snippet in snippets:
        assert snippet in result.output


def test_completion_callbacks_return_value_help_shape_and_50_row_caps() -> None:
    from notebooklm.cli import completion, options

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.sources.list = AsyncMock(
        return_value=[_Stub(f"src_{index:03d}", f"Source {index}") for index in range(60)]
    )
    fake_client.artifacts.list = AsyncMock(
        return_value=[_Stub(f"art_{index:03d}", f"Artifact {index}") for index in range(60)]
    )

    with (
        patch.object(completion, "resolve_notebook", return_value="nb_contract"),
        patch.object(helpers_module, "get_auth_tokens", return_value=object()),
        patch.object(client_module, "NotebookLMClient", return_value=fake_client),
    ):
        source_items = options._complete_sources(_ctx_with_notebook(), None, "src_")
        artifact_items = options._complete_artifacts(_ctx_with_notebook(), None, "art_")

    assert len(source_items) == 50
    assert (source_items[0].value, source_items[0].help) == ("src_000", "Source 0")
    assert source_items[-1].value == "src_049"
    assert len(artifact_items) == 50
    assert (artifact_items[0].value, artifact_items[0].help) == ("art_000", "Artifact 0")
    assert artifact_items[-1].value == "art_049"


def test_completion_callbacks_are_silent_on_failures(capsys: pytest.CaptureFixture[str]) -> None:
    from notebooklm.cli import completion, options

    with patch.object(helpers_module, "get_auth_tokens", side_effect=RuntimeError("no auth")):
        assert options._complete_notebooks(_ctx_with_notebook(), None, "nb_") == []

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.sources.list = AsyncMock(side_effect=RuntimeError("offline"))
    fake_client.artifacts.list = AsyncMock(side_effect=RuntimeError("offline"))

    with (
        patch.object(completion, "resolve_notebook", return_value="nb_contract"),
        patch.object(helpers_module, "get_auth_tokens", return_value=object()),
        patch.object(client_module, "NotebookLMClient", return_value=fake_client),
    ):
        assert options._complete_sources(_ctx_with_notebook(), None, "src_") == []
        assert options._complete_artifacts(_ctx_with_notebook(), None, "art_") == []

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    ("case_id", "setup", "expected_exit", "expected_code", "legacy_error"),
    [
        ("auth_required", "missing_storage", 1, "AUTH_REQUIRED", False),
        ("returned_download_error", "empty_artifacts", 1, None, True),
        ("typed_user_error", "rate_limited", 1, "RATE_LIMITED", False),
        ("unexpected_error", "runtime_error", 2, "UNEXPECTED_ERROR", False),
    ],
)
def test_json_stdout_routing_and_exit_codes_for_download_runtime(
    case_id: str,
    setup: str,
    expected_exit: int,
    expected_code: str | None,
    legacy_error: bool,
) -> None:
    from notebooklm.auth import AuthTokens
    from notebooklm.exceptions import RateLimitError

    from .conftest import create_mock_client, inject_client

    auth = AuthTokens(
        cookies={
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        },
        csrf_token="csrf",
        session_id="session",
    )
    mock_client = create_mock_client()
    if setup == "empty_artifacts":
        mock_client.artifacts.list = AsyncMock(return_value=[])
    elif setup == "rate_limited":
        mock_client.artifacts.list = AsyncMock(side_effect=RateLimitError("quota", retry_after=7))
    elif setup == "runtime_error":
        mock_client.artifacts.list = AsyncMock(side_effect=RuntimeError("boom"))

    with patch.object(helpers_module, "get_auth_tokens") as mock_get_auth_tokens:
        if setup == "missing_storage":
            mock_get_auth_tokens.side_effect = FileNotFoundError("Storage file not found")
        else:
            mock_get_auth_tokens.return_value = auth

        result = CliRunner().invoke(
            cli,
            ["download", "audio", "--json", "-n", "nb_123"],
            catch_exceptions=False,
            obj=inject_client(mock_client),
        )

    assert result.exit_code == expected_exit, case_id
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    if legacy_error:
        assert isinstance(payload["error"], str)
        assert "No completed audio artifacts" in payload["error"]
        assert "code" not in payload
    else:
        assert payload["error"] is True
        assert payload["code"] == expected_code


# ---------------------------------------------------------------------------
# Uniform ``--json`` error-envelope + exit-code contract (issue #1214 part b).
#
# Today the typed-envelope matrix is pinned for the ``download`` command only
# (see ``test_json_stdout_routing_and_exit_codes_for_download_runtime``). The
# class of regression this guards: a *new* ``--json``-bearing command that
# forgets to route failures through ``handle_errors`` and instead leaks a raw
# traceback, a bare string, or a non-JSON line to stdout. Such a command
# silently breaks every automation parsing the typed envelope.
#
# The canonical envelope (``cli/error_handler.py::_output_error``) is::
#
#     {"error": true, "code": "<UPPER_SNAKE>", "message": "<str>", ...}
#
# with exit code 1 for user/application errors (auth, rate-limit, validation),
# 2 for unexpected bugs, 130 for Ctrl-C, and 0 for success. This test drives a
# uniform *missing-auth* failure (the same trigger the download test uses for
# its ``AUTH_REQUIRED`` case) across EVERY ``--json`` command discovered by
# command-tree introspection and asserts the typed envelope + exit code 1 +
# clean stderr.
# ---------------------------------------------------------------------------

# Commands whose ``--json`` output is intentionally NOT the error envelope:
# diagnostic / local-state commands that report status and succeed (exit 0)
# even without usable auth, emitting their own structured payload. Each is an
# explicit, documented exemption so the exemption is intentional rather than an
# accidental gap. Mirrors the prompt's documented exemptions
# (``agent show`` / ``skill install`` / ``profile``); the concrete commands
# that actually carry ``--json`` today and bypass the error envelope are:
JSON_CONTRACT_EXEMPTIONS: dict[str, str] = {
    "auth check": "Diagnostic command: emits a status report payload and exits 0.",
    "doctor": "Diagnostic/repair command: emits a checks report payload, not the error envelope.",
    "language get": "Settings read: emits the resolved language, no auth required.",
    "language list": "Settings read: emits the supported-language table, no auth required.",
    "language set": "Settings write helper: emits the applied language payload.",
    "profile list": "Local profile listing: reads on-disk profiles, no auth required.",
    "status": "Context status: emits the active-notebook context payload, no auth required.",
}

# Dummy required-argument values so a command body is reached past Click's
# argument parsing. Keyed by Click parameter name.
_JSON_CONTRACT_DUMMY_ARGS = {
    "artifact_id": "art_1",
    "question": "hello",
    "title": "Untitled",
    "description": "desc",
    "code": "en",
    "email": "person@example.com",
    "content": "body",
    # label group positional ARGUMENTS only (Click options like --scope/--emoji/
    # --yes do NOT get entries here — this table is for arguments).
    "label_ref": "lbl_1",
    "label_refs": "lbl_1",
    "name": "My Label",
    "new_name": "Renamed",
    "emoji_value": "📄",
    "source_ids": "src_1",
}


def _split_stderr_runner() -> CliRunner:
    """A ``CliRunner`` that captures stderr separately when Click supports it.

    Click 8.2 removed the ``mix_stderr`` constructor parameter (stderr is split
    by default). Inspect the signature so the kwarg is passed only when present,
    rather than relying on a ``TypeError`` to detect the unsupported case.
    """
    import inspect

    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def _has_json_flag(cmd: click.Command) -> bool:
    for param in cmd.params:
        if isinstance(param, click.Option) and (
            param.name in ("json_output", "json") or "--json" in param.opts
        ):
            return True
    return False


def _json_command_paths() -> list[str]:
    """Every visible leaf command that exposes a ``--json`` flag."""
    paths: list[str] = []

    def walk(cmd: click.Command, path: list[str]) -> None:
        if isinstance(cmd, click.Group):
            ctx = click.Context(cmd)
            for name in cmd.list_commands(ctx):
                sub = cmd.get_command(ctx, name)
                if sub is None or getattr(sub, "hidden", False):
                    continue
                walk(sub, [*path, name])
        elif _has_json_flag(cmd):
            paths.append(" ".join(path))

    walk(cli, [])
    return sorted(paths)


def _choice_value(param: click.Parameter) -> str:
    if isinstance(param.type, click.Choice):
        return param.type.choices[0]
    return "x"


def _build_json_invocation(path: str) -> list[str]:
    cmd = _command_for(path)
    argv = [*path.split(), "--json"]
    for param in cmd.params:
        if isinstance(param, click.Argument) and param.required:
            count = param.nargs if param.nargs and param.nargs > 0 else 1
            for _ in range(count):
                argv.append(_JSON_CONTRACT_DUMMY_ARGS.get(param.name, _choice_value(param)))
        elif isinstance(param, click.Option) and param.required:
            argv.append(param.opts[0])
            # "1" works for INT/STRING options and is enough to clear Click's
            # parser so the command body (and its auth bootstrap) is reached.
            # If a --json command ever gains a required option with a
            # restrictive type (UUID, existing-Path, ...) that rejects "1",
            # Click would exit 2 before auth; add such names to a dummy-option
            # map here, mirroring _JSON_CONTRACT_DUMMY_ARGS for arguments.
            argv.append(_choice_value(param) if isinstance(param.type, click.Choice) else "1")
    if any(isinstance(param, click.Option) and param.name == "notebook_id" for param in cmd.params):
        argv.extend(["-n", "nb_contract"])
    return argv


def _enforced_json_command_paths() -> list[str]:
    return [path for path in _json_command_paths() if path not in JSON_CONTRACT_EXEMPTIONS]


def test_json_contract_exemptions_are_real_commands() -> None:
    """Every exemption must name a real ``--json`` command (no stale entries)."""
    json_paths = set(_json_command_paths())
    stale = sorted(name for name in JSON_CONTRACT_EXEMPTIONS if name not in json_paths)
    assert not stale, (
        "Stale --json contract exemptions (no such --json command — remove from "
        f"JSON_CONTRACT_EXEMPTIONS): {stale}"
    )


def test_json_contract_covers_a_representative_command_set() -> None:
    """Guard against the introspection silently matching nothing."""
    enforced = _enforced_json_command_paths()
    # If this ever drops sharply, the command-tree walk likely broke or a whole
    # group started bypassing the envelope; both deserve a hard failure.
    assert len(enforced) >= 30, enforced


@pytest.mark.parametrize("command_path", _enforced_json_command_paths())
def test_json_error_envelope_and_exit_code_are_uniform(command_path: str) -> None:
    """Every non-exempt ``--json`` command emits the typed envelope on failure.

    Drives a uniform missing-auth failure and asserts the canonical
    ``{"error": true, "code": <str>, "message": <str>}`` envelope on stdout,
    exit code 1, and empty stderr — the same shape pinned for ``download`` in
    ``test_json_stdout_routing_and_exit_codes_for_download_runtime``.
    """
    argv = _build_json_invocation(command_path)
    runner = _split_stderr_runner()

    with patch.object(
        helpers_module,
        "get_auth_tokens",
        side_effect=FileNotFoundError("Storage file not found"),
    ):
        result = runner.invoke(cli, argv, catch_exceptions=False)

    assert result.exit_code == 1, (
        f"{command_path!r} should exit 1 on missing auth, got "
        f"{result.exit_code}. stdout={result.stdout!r}"
    )

    try:
        stderr = result.stderr
    except ValueError:  # pragma: no cover - runner without split capture
        stderr = ""
    assert stderr == "", f"{command_path!r} wrote to stderr in --json mode: {stderr!r}"

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        raise AssertionError(
            f"{command_path!r} did not emit JSON in --json mode: {result.stdout!r}"
        ) from exc

    assert payload.get("error") is True, f"{command_path!r} envelope missing error=true: {payload}"
    assert isinstance(payload.get("code"), str) and payload["code"], (
        f"{command_path!r} envelope missing string code: {payload}"
    )
    assert isinstance(payload.get("message"), str), (
        f"{command_path!r} envelope missing string message: {payload}"
    )


if __name__ == "__main__":
    print(json.dumps(build_cli_contract(), indent=2, sort_keys=True))
