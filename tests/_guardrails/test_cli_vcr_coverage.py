"""Guard: every CLI command GROUP has cli_vcr coverage or a reasoned exemption.

The ``cli_vcr`` suite (``tests/integration/cli_vcr/``) is the project's
behaviour net over the full CLI → Client → RPC path. A whole command *group*
landing with no cli_vcr coverage — and no recorded reason — is how the net
silently develops holes. This gate enumerates the top-level command groups of
``notebooklm.notebooklm_cli.cli`` and asserts each one is EITHER:

1. **Covered** — listed in :data:`GROUP_COVERAGE`, mapped to the
   ``tests/integration/cli_vcr/`` test file that exercises it (the file must
   exist), OR
2. **Exempt** — listed in :data:`COVERAGE_EXEMPT` with a one-line reason.

This is group-level, not leaf-command-level: 77 leaf commands across 12 groups
would be noise. A group is the right granularity — when a group is exercised at
all, the per-command depth is the job of the re-record-safe assertion tiers
(issue #1452), not this gate.

Mapping is **explicit** rather than ``test_<group>*.py`` filename inference,
because the file names do not all match the group (``language`` → ``test_settings.py``,
``note`` → ``test_notes.py``). An explicit ``group → file`` map is honest and
survives a rename better than a glob.

The exempt set is a **shrink-only ratchet** (mirrors
``test_module_size_ratchet.py``): a group may only leave the exempt set by
gaining real coverage — never by being added to dodge the gate. The two
self-draining checks enforce this:

* :func:`test_exempt_groups_have_no_cli_vcr_coverage` fails if an exempt group
  *does* have a ``GROUP_COVERAGE`` entry whose file exists — that group is now
  covered and MUST be removed from :data:`COVERAGE_EXEMPT` (the "newly-covered
  group must be removed" contract).
* :func:`test_every_cli_group_is_classified` fails if a brand-new group appears
  that is in neither map — new groups start gated.

Two exemption *reasons* exist, chosen by REALITY (verified by reading each
command module for a ``NotebookLMClient`` / ``run_client_workflow`` RPC path):

* ``"Phase-3: needs maintainer recording (#1452)"`` — the group hits the RPC API
  but has no cassettes yet; it flips on when a maintainer records (issue #1452
  Phase 3). These are the only entries expected to drain.
* ``"local-only, no RPC path"`` — the group never builds a client (filesystem /
  package-data only), so a VCR cassette would record nothing. These are
  permanent by design.

The gate is GREEN on ``main`` today.

Modelled on the ratchet lints in ``tests/_guardrails/`` (e.g.
``test_module_size_ratchet.py`` / ``test_no_module_shadowing.py``).
"""

from __future__ import annotations

from pathlib import Path

import click

from notebooklm.notebooklm_cli import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_VCR_DIR = REPO_ROOT / "tests" / "integration" / "cli_vcr"

# Exemption reason strings (kept as named constants so the two reasons are
# spelled identically everywhere and a typo can't fork them).
_REASON_NEEDS_RECORDING = "Phase-3: needs maintainer recording (#1452)"
_REASON_LOCAL_ONLY = "local-only, no RPC path"

# Covered groups → the cli_vcr test file that exercises them. The file name does
# not always match the group (the matcher keys on RPC shape, not the group
# name), so the mapping is explicit. Each path is relative to ``CLI_VCR_DIR``.
GROUP_COVERAGE: dict[str, str] = {
    "artifact": "test_artifacts.py",
    "download": "test_downloads.py",
    "generate": "test_generate.py",
    "label": "test_label.py",
    "language": "test_settings.py",  # `language` commands live in test_settings.py
    "note": "test_notes.py",
    "profile": "test_profile.py",
    "share": "test_share.py",
    "source": "test_sources.py",
}

# Groups with no cli_vcr coverage today → reason. Shrink-only: a group leaves
# this map ONLY by gaining real coverage (move it to GROUP_COVERAGE). Verified
# by reading each command module:
#   * ``research``/``auth`` build a NotebookLMClient (RPC path) but have no
#     cassettes yet — they flip on when a maintainer records (#1452 Phase 3).
#   * ``agent``/``skill``/``mcp`` never touch the RPC API (``agent show`` prints
#     packaged prompt templates; ``skill`` reads/writes skill files on disk;
#     ``mcp install`` reads/writes the MCP client's config file on disk), so a VCR
#     cassette would capture nothing — permanently local-only.
COVERAGE_EXEMPT: dict[str, str] = {
    "research": _REASON_NEEDS_RECORDING,
    "auth": _REASON_NEEDS_RECORDING,
    "agent": _REASON_LOCAL_ONLY,
    "skill": _REASON_LOCAL_ONLY,
    "mcp": _REASON_LOCAL_ONLY,
}

_VALID_REASONS = frozenset({_REASON_NEEDS_RECORDING, _REASON_LOCAL_ONLY})


def _cli_groups() -> set[str]:
    """The names of every top-level ``click.Group`` under ``cli``.

    Walks the live command tree so a newly-registered group is picked up
    automatically (and then fails :func:`test_every_cli_group_is_classified`
    until it is classified).
    """
    return {name for name, cmd in cli.commands.items() if isinstance(cmd, click.Group)}


def _covered_file_exists(group: str) -> bool:
    """True if ``group`` maps to a cli_vcr test file that exists on disk."""
    rel = GROUP_COVERAGE.get(group)
    return rel is not None and (CLI_VCR_DIR / rel).is_file()


def test_every_cli_group_is_classified() -> None:
    """Every CLI group must be covered OR exempt — a new group starts gated.

    A group in neither :data:`GROUP_COVERAGE` nor :data:`COVERAGE_EXEMPT` is an
    unclassified hole in the cli_vcr net. Add a cli_vcr test and map it in
    ``GROUP_COVERAGE``, or (only if it is RPC-less or genuinely needs recording)
    add it to ``COVERAGE_EXEMPT`` with a reason.
    """
    classified = set(GROUP_COVERAGE) | set(COVERAGE_EXEMPT)
    unclassified = sorted(_cli_groups() - classified)
    assert unclassified == [], (
        "CLI command group(s) have no cli_vcr coverage and no exemption "
        "(issue #1452 coverage gate). Add a cli_vcr test (and map it in "
        "GROUP_COVERAGE) or add a reasoned COVERAGE_EXEMPT entry:\n"
        + "\n".join(f"  {g}" for g in unclassified)
    )


def test_covered_groups_have_an_existing_test_file() -> None:
    """Every :data:`GROUP_COVERAGE` entry must point at a real, existing group + file.

    A dangling mapping (the group was renamed/removed, or the test file was
    deleted) would silently claim coverage that no longer exists.
    """
    groups = _cli_groups()
    broken = {
        group: rel
        for group, rel in GROUP_COVERAGE.items()
        if group not in groups or not (CLI_VCR_DIR / rel).is_file()
    }
    assert broken == {}, (
        "GROUP_COVERAGE has entries whose group no longer exists or whose test "
        f"file is missing — fix or remove them (group -> file): {broken}"
    )


def test_exempt_groups_have_no_cli_vcr_coverage() -> None:
    """A newly-covered group must be REMOVED from :data:`COVERAGE_EXEMPT` (shrink-only).

    This is the ratchet: the moment an exempt group gains a real cli_vcr test
    (a ``GROUP_COVERAGE`` mapping to an existing file), it is no longer exempt
    and must move out of ``COVERAGE_EXEMPT``. Keeping a covered group in the
    exempt set would let coverage silently regress behind a stale exemption.
    """
    regressed = sorted(g for g in COVERAGE_EXEMPT if _covered_file_exists(g))
    assert regressed == [], (
        "Group(s) now have cli_vcr coverage but are still in COVERAGE_EXEMPT "
        "(issue #1452 shrink-only ratchet). The exempt set may only shrink — "
        "remove each newly-covered group from COVERAGE_EXEMPT:\n"
        + "\n".join(f"  {g}" for g in regressed)
    )


def test_exempt_and_covered_sets_are_disjoint() -> None:
    """No group may be both covered and exempt.

    Belt-and-braces against the two maps drifting out of sync — a group in both
    is contradictory (and would defeat the shrink-only check above).
    """
    overlap = sorted(set(GROUP_COVERAGE) & set(COVERAGE_EXEMPT))
    assert overlap == [], (
        f"Group(s) appear in BOTH GROUP_COVERAGE and COVERAGE_EXEMPT: {overlap}. "
        "A covered group must not also be exempt."
    )


def test_every_exemption_has_a_known_reason() -> None:
    """Each :data:`COVERAGE_EXEMPT` reason must be one of the sanctioned strings.

    Forces every exemption into one of the two audited buckets (needs-recording
    vs local-only) so a free-text reason can't smuggle in an un-triaged hole.
    """
    bad = {g: r for g, r in COVERAGE_EXEMPT.items() if r not in _VALID_REASONS}
    assert bad == {}, (
        "COVERAGE_EXEMPT entries with an unrecognised reason — use one of "
        f"{sorted(_VALID_REASONS)} (group -> reason): {bad}"
    )


def test_exempt_groups_still_exist() -> None:
    """Every exempt group must still be a real CLI group (no stale entries).

    A removed/renamed group left in :data:`COVERAGE_EXEMPT` is dead weight that
    would mask a future re-introduction under the same name.
    """
    stale = sorted(g for g in COVERAGE_EXEMPT if g not in _cli_groups())
    assert stale == [], (
        "COVERAGE_EXEMPT references group(s) that no longer exist on the CLI — "
        f"remove the stale entries: {stale}"
    )
