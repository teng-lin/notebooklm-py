"""Every CLI leaf command must support ``--json`` -- unless explicitly waived.

Automation drives this CLI through ``--json`` (stable, machine-readable output
on stdout; diagnostics to stderr). A new command that ships without ``--json``
is a silent hole in that contract: scripts can't consume it and the gap is only
noticed when someone tries. This ratchet walks the live Click tree and asserts
the set of leaf commands lacking ``--json`` equals a frozen, classified
allowlist -- so adding a new command forces a *conscious* choice (grow
``--json`` or justify an entry here), and fixing a gap forces removing its
entry.

Exact-match (both directions):

* A new leaf command without ``--json`` and not in the allowlist FAILS -- add
  ``@json_option`` / ``@click.option("--json", ...)`` (the common case) or, if
  the command genuinely emits no structured data, add it here with a one-line
  reason.
* An allowlisted command that GAINS ``--json`` FAILS as STALE -- remove it from
  the allowlist (anti-rot; mirrors the stale-waiver checks in the sibling
  ``test_cli_rpc_envelope`` / exit-path gates).

Everything in the allowlist is EXEMPT by design: it emits no structured payload
(interactive auth, a shell-completion script, or packaged prose/skill content
that *is* the output). There is intentionally no "gap" tier -- the gaps were
closed when this gate landed; a new command either grows ``--json`` or earns a
justified exemption here.
"""

from __future__ import annotations

import click

from notebooklm.notebooklm_cli import cli

#: Leaf commands (full space-joined path) that legitimately lack ``--json``.
COMMANDS_WITHOUT_JSON: frozenset[str] = frozenset(
    {
        "login",  # interactive browser auth flow
        "completion",  # emits a shell completion script, not data
        "agent show",  # emits agent instruction prose (the text IS the payload)
        "skill show",  # emits packaged skill content
        "skill install",  # local file-install side-effect
        "skill uninstall",  # local file-removal side-effect
        "mcp install",  # wires the MCP server into an editor config
    }
)


def _leaf_commands_without_json(cmd: click.Command, path: list[str]) -> set[str]:
    if isinstance(cmd, click.Group):
        found: set[str] = set()
        # Deliberately includes ``hidden`` commands (iterating ``commands`` rather
        # than ``list_commands``): hiding a command from ``--help`` does not exempt
        # it from the JSON-output contract, so the gate stays broader than the
        # ``--help``-scoped contract walk in test_cli_contract.py.
        for name, sub in cmd.commands.items():
            found |= _leaf_commands_without_json(sub, [*path, name])
        return found
    has_json = any("--json" in p.opts for p in cmd.params if isinstance(p, click.Option))
    return set() if has_json else {" ".join(path)}


def test_new_cli_commands_support_json_output() -> None:
    actual = _leaf_commands_without_json(cli, [])

    missing = actual - COMMANDS_WITHOUT_JSON
    assert not missing, (
        "These CLI commands lack `--json`. Add a `--json` option (the norm), or "
        "if the command emits no structured data, add it to COMMANDS_WITHOUT_JSON "
        f"in {__file__} with a reason: {sorted(missing)}"
    )

    stale = COMMANDS_WITHOUT_JSON - actual
    assert not stale, (
        "These commands now support `--json` (or were renamed/removed) -- drop "
        f"them from COMMANDS_WITHOUT_JSON in {__file__}: {sorted(stale)}"
    )
