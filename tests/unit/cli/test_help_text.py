"""Snapshot tests asserting CLI group docstrings list every registered subcommand.

This guardrail prevents help-text drift: when a new subcommand is added to a
``click.group()`` (e.g. ``source clean``, ``artifact suggestions``), the group's
``--help`` output must continue to enumerate it in the docstring "Commands:"
block. Without this test, contributors could silently land a new subcommand
that ``--help`` users would never discover from the group's overview.

Coverage scope: the four CLI groups whose docstrings explicitly list their
subcommands as a discoverability aid (``source``, ``artifact``, ``note``,
``download``). Other groups (``profile``, ``share``, ``research``, etc.) rely
on Click's auto-generated subcommand table and are not in scope here.

Hidden subcommands: none of the in-scope groups currently mark any subcommand
``hidden=True``. If a future contributor adds one, this test will require it
to be listed in the docstring too — adjust the comprehension to filter
``c.hidden`` or move the hidden command out of the scoped groups.
"""

from __future__ import annotations

import re

import click
import pytest
from click.testing import CliRunner

from notebooklm.cli.artifact_cmd import artifact
from notebooklm.cli.download_cmd import download
from notebooklm.cli.note_cmd import note
from notebooklm.cli.source_cmd import source


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# Groups whose docstring "Commands:" / "Types:" block must enumerate every
# registered subcommand. Keep this list in sync with cli/__init__.py.
GROUPS: list[tuple[str, click.Group]] = [
    ("source", source),
    ("artifact", artifact),
    ("note", note),
    ("download", download),
]


def _contains_command_entry(text: str, command: str) -> bool:
    """Whole-token match for ``command`` as a left-column entry in ``text``.

    Plain ``in`` matches substrings — ``"add" in "add-drive"`` is True — so
    using it for command-presence checks lets a missing ``add`` slip through
    unnoticed if ``add-drive`` is registered. Match the command at the start
    of a line followed by ≥2 spaces (Click's column gap in ``--help`` /
    docstring tables) or end-of-line.
    """
    return re.search(rf"(?m)^\s*{re.escape(command)}(?:\s{{2,}}|\s*$)", text) is not None


@pytest.mark.parametrize("group_name,group", GROUPS, ids=[g[0] for g in GROUPS])
def test_group_help_lists_every_subcommand(
    group_name: str,
    group: click.Group,
    runner: CliRunner,
) -> None:
    """Every subcommand registered on a group must appear in its rendered ``--help``.

    Click auto-generates a "Commands:" table at the bottom of ``--help``, so
    this test almost always passes by virtue of that table alone. It exists
    as a tripwire for the rare case where the group is configured to suppress
    the auto-table or a subcommand is registered with ``hidden=True`` (which
    would also need filtering — see module docstring). The stricter
    docstring-only check is :func:`test_group_docstring_lists_every_subcommand`.
    """
    result = runner.invoke(group, ["--help"])
    assert result.exit_code == 0, (
        f"`{group_name} --help` failed with exit {result.exit_code}: {result.output}"
    )

    missing = [
        subcmd for subcmd in group.commands if not _contains_command_entry(result.output, subcmd)
    ]
    assert not missing, (
        f"`{group_name} --help` is missing subcommand(s): {missing}. "
        f"Update the group docstring 'Commands:' block in "
        f"src/notebooklm/cli/{group_name}_cmd.py to include them."
    )


@pytest.mark.parametrize("group_name,group", GROUPS, ids=[g[0] for g in GROUPS])
def test_group_docstring_lists_every_subcommand(
    group_name: str,
    group: click.Group,
) -> None:
    """Each subcommand must appear in the group's hand-written docstring.

    Click's auto-generated "Commands:" table can mask docstring drift in the
    rendered ``--help`` output (the same name shows up twice). This stricter
    check inspects ``group.help`` (the docstring) directly so a missing entry
    in the curated "Commands:" / "Types:" block is caught even when Click's
    table papers over it.
    """
    docstring = group.help or ""
    assert docstring, f"`{group_name}` group has no docstring"

    missing = [
        subcmd for subcmd in group.commands if not _contains_command_entry(docstring, subcmd)
    ]
    assert not missing, (
        f"`{group_name}` group docstring is missing subcommand(s): {missing}. "
        f"Update the docstring 'Commands:' / 'Types:' block in "
        f"src/notebooklm/cli/{group_name}_cmd.py."
    )
