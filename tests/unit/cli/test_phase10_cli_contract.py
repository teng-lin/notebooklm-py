"""Phase 10 CLI contract baseline before runtime refactors.

This file intentionally characterizes public behavior without moving code. Later
T11/T12 slices can refactor runtime/auth/completion internals while comparing
against the JSON baseline generated from ``build_phase10_cli_contract``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

ROOT_COMMAND = "notebooklm"


def _find_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("Could not locate repository root")


BASELINE_PATH = (
    _find_repo_root()
    / ".sisyphus/phases/architecture-remediation/runs/"
    / "phase-10-cli-contract-baseline.json"
)

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


def build_phase10_cli_contract() -> dict[str, object]:
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


def test_phase10_cli_contract_matches_baseline() -> None:
    """Public command tree, options, defaults, help, and aliases match T11.0."""
    expected = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert build_phase10_cli_contract() == expected


@pytest.mark.parametrize("path,snippets", HELP_SNIPPETS.items(), ids=lambda value: value or "root")
def test_representative_help_snippets_remain_visible(path: str, snippets: tuple[str, ...]) -> None:
    argv = [*path.split(), "--help"] if path else ["--help"]
    result = CliRunner().invoke(cli, argv)

    assert result.exit_code == 0, result.output
    for snippet in snippets:
        assert snippet in result.output


def test_completion_callbacks_return_value_help_shape_and_50_row_caps() -> None:
    from notebooklm.cli import options

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
        patch.object(options, "_resolve_notebook_for_completion", return_value="nb_contract"),
        patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
        patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
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
    from notebooklm.cli import options

    with patch("notebooklm.cli.helpers.get_auth_tokens", side_effect=RuntimeError("no auth")):
        assert options._complete_notebooks(_ctx_with_notebook(), None, "nb_") == []

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.sources.list = AsyncMock(side_effect=RuntimeError("offline"))
    fake_client.artifacts.list = AsyncMock(side_effect=RuntimeError("offline"))

    with (
        patch.object(options, "_resolve_notebook_for_completion", return_value="nb_contract"),
        patch("notebooklm.cli.helpers.get_auth_tokens", return_value=object()),
        patch("notebooklm.client.NotebookLMClient", return_value=fake_client),
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

    from .conftest import create_mock_client, patch_client_for_module

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

    with (
        patch("notebooklm.cli.helpers.get_auth_tokens") as mock_get_auth_tokens,
        patch_client_for_module("download") as mock_client_cls,
    ):
        if setup == "missing_storage":
            mock_get_auth_tokens.side_effect = FileNotFoundError("Storage file not found")
        else:
            mock_get_auth_tokens.return_value = auth
            mock_client_cls.return_value = mock_client

        result = CliRunner().invoke(
            cli,
            ["download", "audio", "--json", "-n", "nb_123"],
            catch_exceptions=False,
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


if __name__ == "__main__":
    print(json.dumps(build_phase10_cli_contract(), indent=2, sort_keys=True))
