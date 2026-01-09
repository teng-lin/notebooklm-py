"""Custom Click group with sectioned help output.

Organizes CLI commands into logical sections for better discoverability.
"""

from collections import OrderedDict

import click


class SectionedGroup(click.Group):
    """Click group that displays commands organized in sections.

    Instead of a flat alphabetical list, commands are grouped by function:
    - Session: login, use, status, clear
    - Notebooks: list, create, delete, rename, share, summary
    - Chat: ask, configure, history
    - Command Groups: source, artifact, note (show subcommands)
    - Artifact Actions: generate, download (show types)
    """

    # Regular commands - show help text
    command_sections = OrderedDict(
        [
            ("Session", ["login", "use", "status", "clear"]),
            ("Notebooks", ["list", "create", "delete", "rename", "share", "summary"]),
            ("Chat", ["ask", "configure", "history"]),
        ]
    )

    # Command groups - show sorted subcommands instead of help text
    command_groups = OrderedDict(
        [
            (
                "Command Groups (use: notebooklm <group> <command>)",
                ["source", "artifact", "note", "research"],
            ),
            ("Artifact Actions (use: notebooklm <action> <type>)", ["generate", "download"]),
        ]
    )

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

        # Safety net: show any commands not in any section
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
