"""Public CLI contract introspection (the ``cli_contract`` baseline derive).

``build_cli_contract()`` walks the Click command tree and returns the
deterministic public CLI inventory (command tree, options, defaults, help,
aliases) that is frozen as the ``cli_contract`` regenerable baseline (ADR-0022,
``tests/fixtures/cli_contract_baseline.json``).

This pure-introspection core was extracted out of
``tests/unit/cli/test_cli_contract.py`` into this ``_``-prefixed non-test module
so the baseline registry (``tests/_baselines/registry.py``) can import
``build_cli_contract`` without one ``test_*`` module importing from another
(forbidden by ``tests/_guardrails/test_no_cross_test_imports.py``). The behavioural
CLI-contract tests still live in ``test_cli_contract.py`` and import these names
from here.
"""

from __future__ import annotations

import click

from notebooklm.notebooklm_cli import cli

ROOT_COMMAND = "notebooklm"

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
    if isinstance(value, (tuple, list)):
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
