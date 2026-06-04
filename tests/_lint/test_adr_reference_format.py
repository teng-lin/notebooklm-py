"""Enforce a single ADR-reference format: ``ADR-NNNN`` (4-digit, zero-padded).

ADR design docs are named ``docs/adr/NNNN-*.md`` with a 4-digit zero-padded
number (``0001-`` … ``0019-``). Prose references must use the **same** 4-digit
width so a reference maps unambiguously onto its file.

Before this gate the two diverged badly: references drifted between a 3-digit
short form (``ADR-NNN``, 626 occurrences) and the 4-digit form (``ADR-0019``,
49) — and the *same* ADR was spelled both ways in CLAUDE.md, the docs, the
tests, and the ADRs' own cross-references. A one-time scripted sweep unified
everything to the 4-digit form; this gate keeps it that way.

Two invariants, plus self-tests of the detector:

1. Every ``ADR-<digits>`` reference in tracked text uses exactly 4 digits.
2. Every referenced ``ADR-NNNN`` maps to an existing ``docs/adr/NNNN-*.md``.

If check 1 fails, someone wrote a non-4-digit form (``ADR-NN`` / ``ADR-NNN`` /
``ADR-NNNNN``) instead of ``ADR-0019``. If check 2 fails, a reference points at
an ADR number that has no file (a typo, or a doc citing an ADR never written).

This module is itself scanned by the gate (it is *not* excluded): the examples
above use letter placeholders (``ADR-NNN``) and the self-tests build malformed
strings at runtime, so the file contains no malformed literal of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR_DIR = _REPO_ROOT / "docs" / "adr"

# An ADR reference: the literal ``ADR-`` followed by a run of digits. The digit
# run is captured so its width can be checked; a non-digit (or end) terminates
# it, so a 5-digit ``ADR-NNNNN`` run is caught and rejected.
_ADR_REF = re.compile(r"ADR-(\d+)")

_CANONICAL_WIDTH = 4

# Text file types that can carry a prose ADR reference.
_SCANNED_SUFFIXES = {
    ".md",
    ".py",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".rst",
}

# Directories that are never scanned (vendored / generated / VCS / worktrees).
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".worktrees",
    ".claude",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}


def find_misformatted_adr_refs(text: str) -> list[str]:
    """Return the distinct ``ADR-<digits>`` refs in *text* whose width is not 4.

    Pure function over a string so it is unit-testable without touching disk.
    """
    return sorted(
        {
            match.group(0)
            for match in _ADR_REF.finditer(text)
            if len(match.group(1)) != _CANONICAL_WIDTH
        }
    )


def _scanned_files() -> list[Path]:
    """All tracked-ish text files to scan — including this module itself.

    Prunes skipped directories *during* traversal rather than walking the whole
    tree and filtering, so a large ``.venv`` / ``node_modules`` (present in CI
    after ``uv sync``) is never descended into. This file is deliberately *not*
    excluded — it carries no malformed literal (see the module docstring), so the
    gate polices its own references too.
    """
    out: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for path in entries:
            if path.is_dir():
                if path.name not in _SKIP_DIRS:
                    walk(path)
            elif path.suffix.casefold() in _SCANNED_SUFFIXES:
                # casefold so an upper/mixed-case suffix (.MD, .YAML) still matches.
                out.append(path)

    walk(_REPO_ROOT)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_all_adr_references_are_four_digit() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _scanned_files():
        bad = find_misformatted_adr_refs(_read(path))
        if bad:
            offenders[str(path.relative_to(_REPO_ROOT))] = bad
    assert not offenders, (
        "ADR references must use the 4-digit zero-padded form (e.g. ADR-0019), "
        "matching the docs/adr/NNNN-*.md filenames. Misformatted references:\n"
        + "\n".join(f"  {file}: {', '.join(refs)}" for file, refs in sorted(offenders.items()))
    )


def test_every_adr_reference_resolves_to_a_file() -> None:
    existing = {p.name[:_CANONICAL_WIDTH] for p in _ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")}
    assert existing, "no ADR files found under docs/adr/ — gate would be vacuous"
    referenced: set[str] = set()
    for path in _scanned_files():
        for match in _ADR_REF.finditer(_read(path)):
            if len(match.group(1)) == _CANONICAL_WIDTH:
                referenced.add(match.group(1))
    orphans = sorted(referenced - existing)
    assert not orphans, (
        "These ADR references have no matching docs/adr/NNNN-*.md file: "
        + ", ".join(f"ADR-{n}" for n in orphans)
    )


# --- non-vacuous self-tests of the detector ---
# Malformed examples are built at runtime so the literals never appear in this
# file as scannable violations (belt-and-braces with the _scanned_files skip).


def test_detector_flags_three_digit() -> None:
    three = "ADR-" + "013"
    assert find_misformatted_adr_refs(f"see {three} for the rationale") == [three]


def test_detector_accepts_four_digit() -> None:
    assert find_misformatted_adr_refs("see ADR-0013 and ADR-0019 below") == []


def test_detector_flags_one_and_five_digit() -> None:
    one = "ADR-" + "9"
    five = "ADR-" + "00190"
    assert find_misformatted_adr_refs(f"{one} and {five}") == sorted([one, five])
