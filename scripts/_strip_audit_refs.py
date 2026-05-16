#!/usr/bin/env python3
"""Strip internal audit-tracker references from comments, docstrings, and markdown.

Conservative bulk pass: only drops *parenthetical* ID references that ride
alongside prose and a small number of whole-sentence meta-commentary lines
that introduce the now-removed ID convention. Inline noun-form refs (e.g.
``T7.F2:`` at the start of a comment, ``audit §27 failure #1`` mid-sentence,
``Pre-T7.F4``) are intentionally left for targeted hand edits because the
surrounding sentence needs rewriting.

Whitespace is preserved outside the deleted-token regions; the script never
collapses runs of whitespace or reformats multi-line constructs.

The script is run repeatedly across the cleanup phases (Phase 1: src/, docs/,
CHANGELOG, pyproject, CLAUDE.md; Phase 2: tests/; Phase 3: scripts/, CI).
Each invocation is restricted to a phase-specific allow-list of file paths so
that one phase's run cannot accidentally edit another phase's files.

Usage::

    python scripts/_strip_audit_refs.py phase1
    python scripts/_strip_audit_refs.py phase2
    python scripts/_strip_audit_refs.py phase3
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

T_ID = r"T\d+\.[A-Z][0-9a-z]*"
PR_T_ID = r"PR-" + T_ID
PHASE_T_ID = r"P\d+\.T\d+"
AUDIT_SEC = r"audit §\d+[a-z]?"
AUDIT_ROW = r"audit row [A-Z]?\d+[A-Za-z0-9]*"
AUDIT_SECTION = r"audit section \d+"
# CLI UX audit row IDs (``cli-ux-audit.md``): C1, C2, I1..I16, M1..M5.
# Match in parentheticals only; the bare letters are too common to strip
# unconditionally.
CLI_UX_ROW_ID = r"(?:[ICM]\d+(?:,\s*[ICM]\d+)*)"
# Phase markers (e.g. ``F8.T4``) used to coordinate landed-in-arc work.
PHASE_F_ID = r"F\d+\.T\d+"


# Parenthetical patterns — order matters: longest/most-specific first.
PAREN_PATTERNS: list[tuple[str, str]] = [
    # Combined inside one parens, with / or , separators
    (rf" \({T_ID}\s*[/,]\s*{AUDIT_SEC}\)", ""),
    (rf" \({AUDIT_SEC}\s*[/,]\s*{T_ID}\)", ""),
    (rf" \({T_ID}\s*[/,]\s*{AUDIT_SECTION}\)", ""),
    (rf" \({AUDIT_SECTION}\s*[/,]\s*{T_ID}\)", ""),
    # T + audit-section with extra tail text inside parens
    (rf" \({T_ID}\s+{AUDIT_SEC}(?:[^()]*)?\)", ""),
    (rf" \({AUDIT_SEC}\s+{T_ID}(?:[^()]*)?\)", ""),
    # Audit-section failure-numbered variant: (audit §27 failure #1)
    (rf" \({AUDIT_SEC}\s+failure\s+#\d+\)", ""),
    # Standalone audit-row, audit-section, audit-§
    (rf" \({AUDIT_ROW}(?:[^()]*)?\)", ""),
    (rf" \({AUDIT_SECTION}\)", ""),
    (rf" \({AUDIT_SEC}\)", ""),
    # Standalone T-tier and PR-T (must come last to avoid stealing combined matches)
    (rf" \({T_ID}\)", ""),
    (rf" \({PR_T_ID}\)", ""),
    (rf" \({PHASE_T_ID}\)", ""),
    (rf" \({PHASE_F_ID}\)", ""),
    # CLI UX audit row IDs in parentheticals (cli-ux-audit.md):
    # ``(I1)``, ``(I3, I4)``, ``(C1)``, ``(M2)``, etc.
    (rf" \({CLI_UX_ROW_ID}\)", ""),
    # Phase task ID paired with a CLI-UX row ID, with or without leading
    # ``-`` and with a ``/`` separator: ``(P5.T2 / I7)``, ``(M2 / P5.T3)``.
    (rf" \({PHASE_T_ID}\s*/\s*{CLI_UX_ROW_ID}\)", ""),
    (rf" \({CLI_UX_ROW_ID}\s*/\s*{PHASE_T_ID}\)", ""),
    # Multi-section audit references: ``(audit §§13, 15, 16, 21)``.
    (r" \(audit §§\d+(?:,\s*\d+)*\)", ""),
]


# Whole-sentence patterns that read as meta-commentary about the audit tags.
SENTENCE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\s*Per-arc audit IDs \([^)]*\) are noted in parentheses on each non-cli-ux entry\.",
        "",
    ),
    (
        r"\s*Audit-row IDs from `\.sisyphus/plans/cli-ux-audit\.md` \([^)]*\) are noted in parentheses on each entry\.",
        "",
    ),
]


# ---------------------------------------------------------------------------
# Phase scopes
# ---------------------------------------------------------------------------

PHASE_1_FILES: list[str] = [
    # src/notebooklm/**/*.py
    "src/notebooklm/__init__.py",
    "src/notebooklm/_artifacts.py",
    "src/notebooklm/_auth/cookie_policy.py",
    "src/notebooklm/_auth/storage.py",
    "src/notebooklm/_chat.py",
    "src/notebooklm/_core.py",
    "src/notebooklm/_core_transport.py",
    "src/notebooklm/_idempotency.py",
    "src/notebooklm/_logging.py",
    "src/notebooklm/_mind_map.py",
    "src/notebooklm/_notebooks.py",
    "src/notebooklm/_research.py",
    "src/notebooklm/_source_polling.py",
    "src/notebooklm/_sources.py",
    "src/notebooklm/auth.py",
    "src/notebooklm/cli/artifact.py",
    "src/notebooklm/cli/chat.py",
    "src/notebooklm/cli/download.py",
    "src/notebooklm/cli/error_handler.py",
    "src/notebooklm/cli/generate.py",
    "src/notebooklm/cli/helpers.py",
    "src/notebooklm/cli/note.py",
    "src/notebooklm/cli/notebook.py",
    "src/notebooklm/cli/options.py",
    "src/notebooklm/cli/research.py",
    "src/notebooklm/cli/session.py",
    "src/notebooklm/cli/source.py",
    "src/notebooklm/notebooklm_cli.py",
    "src/notebooklm/client.py",
    "src/notebooklm/exceptions.py",
    "src/notebooklm/types.py",
    # docs (user-facing).
    "docs/python-api.md",
    "docs/development.md",
    "docs/auth-keepalive.md",
    "docs/cli-exit-codes.md",
    "docs/cli-reference.md",
    # root
    "CHANGELOG.md",
    "pyproject.toml",
    "CLAUDE.md",
]


PHASE_SCOPES: dict[str, list[str]] = {
    "phase1": PHASE_1_FILES,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def transform_text(text: str) -> str:
    out = text
    for pat, repl in PAREN_PATTERNS:
        out = re.sub(pat, repl, out)
    for pat, repl in SENTENCE_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in PHASE_SCOPES:
        valid_phases = "|".join(sorted(PHASE_SCOPES.keys()))
        print(f"usage: {argv[0]} {{{valid_phases}}}", file=sys.stderr)
        return 2

    phase = argv[1]
    targets = PHASE_SCOPES[phase]

    changed: list[Path] = []
    missing: list[str] = []
    for rel in targets:
        path = REPO_ROOT / rel
        if not path.exists():
            missing.append(rel)
            continue
        text = path.read_text(encoding="utf-8")
        new = transform_text(text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path)

    if missing:
        print(f"WARNING: {len(missing)} target paths missing (skipped):")
        for m in missing:
            print(f"  {m}")

    print(f"Edited {len(changed)} files:")
    for p in sorted(changed):
        print(f"  {p.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
