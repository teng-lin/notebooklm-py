"""Custom Click group with sectioned help output.

Organizes CLI commands into logical sections for better discoverability.

Section assignment is hardcoded in :class:`SectionedGroup` (see
``command_sections`` and ``command_groups``). When you add a new top-level
command, bin it explicitly — otherwise it lands in the "Other" safety-net
bin, which exists only for commands deliberately tagged
``category="misc"`` (set the attribute on the Click command after creation).

A unit test (`tests/unit/cli/test_grouped.py`) enforces this contract: any
unbinned, untagged top-level command will fail the suite, surfacing the
discoverability regression at PR review time.
"""

import sys
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, NoReturn

import click

from .error_handler import exit_with_code, output_error


def _json_requested(args: Sequence[str] | None) -> bool:
    """Return true when the raw command line includes the ``--json`` flag."""
    if args is None:
        args = sys.argv[1:]
    for arg in args:
        if arg == "--":
            return False
        if arg == "--json":
            return True
    return False


def _emit_json_click_error(exc: click.ClickException) -> NoReturn:
    """Emit a Click exception through the canonical JSON error envelope."""
    output_error(
        exc.format_message(),
        "VALIDATION_ERROR",
        json_output=True,
        exit_code=exc.exit_code,
    )


class SectionedGroup(click.Group):
    """Click group that displays commands organized in sections.

    Instead of a flat alphabetical list, commands are grouped by function:
    - Session: login, use, status, clear, doctor, auth
    - Notebooks: list, create, delete, rename, summary, metadata
    - Chat: ask, configure, history
    - Command Groups: source, artifact, note, share, research, profile, agent,
      skill, language (show subcommands)
    - Artifact Actions: generate, download (show types)
    - Other: only commands explicitly tagged ``category="misc"``
    """

    # Regular commands - show help text
    command_sections = OrderedDict(
        [
            ("Session", ["login", "use", "status", "clear", "doctor", "auth", "completion"]),
            ("Notebooks", ["list", "create", "delete", "rename", "summary", "metadata"]),
            ("Chat", ["ask", "configure", "history"]),
        ]
    )

    # Command groups - show sorted subcommands instead of help text
    command_groups = OrderedDict(
        [
            (
                "Command Groups (use: notebooklm <group> <command>)",
                [
                    "source",
                    "artifact",
                    "note",
                    "label",
                    "share",
                    "research",
                    "profile",
                    "agent",
                    "skill",
                    "language",
                    "mcp",
                ],
            ),
            ("Artifact Actions (use: notebooklm <action> <type>)", ["generate", "download"]),
        ]
    )

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        """Run the CLI while honoring ``--json`` for Click validation errors.

        Click renders parser and callback ``ClickException`` failures itself
        before command bodies run, which bypasses the normal ``handle_errors``
        JSON path. Running the superclass in non-standalone mode lets this root
        boundary convert those failures once for every current and future
        subcommand option.
        """
        try:
            rv = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.ClickException as exc:
            if not standalone_mode:
                raise
            if _json_requested(args):
                _emit_json_click_error(exc)
            else:
                exc.show()
                exit_with_code(exc.exit_code)
        except click.Abort:
            if not standalone_mode:
                raise
            if _json_requested(args):
                output_error("Cancelled by user", "CANCELLED", json_output=True, exit_code=1)
            else:
                click.echo("Aborted!", file=sys.stderr)
                exit_with_code(1)

        if standalone_mode:
            exit_with_code(0)
        return rv

    def format_commands(self, ctx, formatter):
        """Override to display commands in sections."""
        commands = {name: self.get_command(ctx, name) for name in self.list_commands(ctx)}

        # Regular command sections (show help text)
        for section, cmd_names in self.command_sections.items():
            rows = []
            for name in cmd_names:
                cmd = commands.get(name)
                if cmd is not None and not cmd.hidden:
                    help_text = cmd.get_short_help_str(limit=formatter.width)
                    rows.append((name, help_text))
            if rows:
                with formatter.section(section):
                    formatter.write_dl(rows)

        # Command group sections (show sorted subcommands)
        for section, group_names in self.command_groups.items():
            rows = []
            for name in group_names:
                if name in commands:
                    cmd = commands[name]
                    if isinstance(cmd, click.Group):
                        subcmds = ", ".join(sorted(cmd.list_commands(ctx)))
                        rows.append((name, subcmds))
            if rows:
                with formatter.section(section):
                    formatter.write_dl(rows)

        # Safety net: show any commands not in any section. By convention this
        # bin is reserved for commands explicitly tagged ``category="misc"``;
        # unbinned-and-untagged commands still appear here so the CLI never
        # silently hides them, but the test in tests/unit/cli/test_grouped.py
        # treats them as a regression and fails the build.
        all_listed = set(sum(self.command_sections.values(), []))
        all_listed |= set(sum(self.command_groups.values(), []))
        unlisted = [
            (n, c)
            for n, c in commands.items()
            if n not in all_listed and c is not None and not c.hidden
        ]
        if unlisted:
            with formatter.section("Other"):
                formatter.write_dl(
                    [(n, c.get_short_help_str(limit=formatter.width)) for n, c in unlisted]
                )
