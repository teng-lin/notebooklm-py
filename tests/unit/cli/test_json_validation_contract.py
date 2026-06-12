"""Root-boundary guard for ``--json`` validation errors.

Click validates option values, missing arguments, and unknown options before
command callbacks run. Those failures must still honor ``--json`` so automation
never receives usage text when it asked for a machine-readable error.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.helpers as helpers_module
from notebooklm.cli.grouped import SectionedGroup
from notebooklm.notebooklm_cli import cli


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.3 captures stdout/stderr separately by default; mix_stderr was removed.
    return CliRunner()


@pytest.fixture
def mock_auth_env() -> Iterator[None]:
    with (
        patch.object(helpers_module, "load_auth_from_storage") as mock_load,
        patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch,
    ):
        mock_load.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        mock_fetch.return_value = ("csrf", "session")
        yield


@pytest.mark.parametrize(
    "case_id,argv",
    [
        ("invalid_limit", ["list", "--limit", "-1", "--json"]),
        ("invalid_interval", ["research", "wait", "--interval", "0", "--json"]),
        ("invalid_retry", ["generate", "audio", "--retry", "-1", "--json"]),
        ("missing_argument", ["note", "get", "--json"]),
        ("unknown_option", ["list", "--json", "--definitely-not-an-option"]),
        ("root_callback_validation", ["--quiet", "-v", "status", "--json"]),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_json_validation_errors_emit_json(
    case_id: str,
    argv: list[str],
    runner: CliRunner,
) -> None:
    result = runner.invoke(cli, argv, catch_exceptions=False)

    assert result.exit_code != 0, result.output
    assert result.stdout.strip(), f"{case_id}: expected JSON error on stdout"
    assert result.stderr == "", f"{case_id}: stderr should stay empty under --json"
    assert result.output == result.stdout, f"{case_id}: combined output should be JSON only"
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["message"]


def test_command_body_click_validation_emit_json(
    runner: CliRunner,
    mock_auth_env,
) -> None:
    result = runner.invoke(
        cli,
        ["note", "create", "positional", "--content", "flag", "-n", "nb_123", "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code != 0, result.output
    assert result.stderr == ""
    assert result.output == result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "VALIDATION_ERROR"
    assert "Cannot use both" in payload["message"]


def test_json_abort_emit_json(runner: CliRunner) -> None:
    @click.group(cls=SectionedGroup)
    def root() -> None:
        pass

    @root.command()
    @click.option("--json", "json_output", is_flag=True)
    def aborting(json_output: bool) -> None:  # noqa: ARG001
        raise click.Abort()

    result = runner.invoke(root, ["aborting", "--json"], catch_exceptions=False)

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.output == result.stdout
    payload = json.loads(result.stdout)
    assert payload == {
        "error": True,
        "code": "CANCELLED",
        "message": "Cancelled by user",
    }


def _walk_leaf_commands(
    command: click.BaseCommand,
    path: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            yield from _walk_leaf_commands(child, path + (name,))
        return
    if isinstance(command, click.Command):
        yield path, command


def _has_json_option(command: click.Command) -> bool:
    return any(
        isinstance(param, click.Option) and "--json" in param.opts for param in command.params
    )


def _invalid_value_for_type(param_type: click.ParamType) -> str | None:
    if isinstance(param_type, click.IntRange):
        if param_type.min is not None:
            return str(param_type.min - 1)
        if param_type.max is not None:
            return str(param_type.max + 1)
        return "not-an-int"
    if isinstance(param_type, click.types.IntParamType):
        return "not-an-int"
    if isinstance(param_type, click.Choice):
        return "__not_a_valid_choice__"
    return None


def _validated_json_option_cases() -> list[pytest.ParameterSet]:
    cases: list[pytest.ParameterSet] = []
    for path, command in _walk_leaf_commands(cli):
        if not _has_json_option(command):
            continue
        for param in command.params:
            if (
                not isinstance(param, click.Option)
                or "--json" in param.opts
                or param.is_flag
                or param.count
            ):
                continue
            value = _invalid_value_for_type(param.type)
            if value is None:
                continue
            opt = next((name for name in param.opts if name.startswith("--")), param.opts[0])
            case_id = "_".join((*path, opt.lstrip("-").replace("-", "_")))
            cases.append(pytest.param(case_id, [*path, opt, value, "--json"], id=case_id))
    return cases


@pytest.mark.parametrize(
    "case_id,argv",
    _validated_json_option_cases(),
)
def test_validated_json_options_emit_json_on_bad_values(
    case_id: str,
    argv: list[str],
    runner: CliRunner,
) -> None:
    result = runner.invoke(cli, argv, catch_exceptions=False)

    assert result.exit_code != 0, result.output
    assert result.stderr == "", f"{case_id}: stderr should stay empty under --json"
    assert result.output == result.stdout, f"{case_id}: combined output should be JSON only"
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "VALIDATION_ERROR"


def test_text_validation_errors_keep_click_usage_output(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["list", "--limit", "-1"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr
    assert not result.stdout.strip().startswith("{")
