#!/usr/bin/env python3
"""Pure-Python cassette guard — replacement for ``tests/check_cassettes_clean.sh``.

Walks ``tests/cassettes/*.yaml`` (or any explicit paths passed on the command
line) and reports any cassette that contains sensitive data.  Uses the
canonical pattern registry in ``tests/cassette_patterns.py``.

Key differences vs. the legacy bash script:

* Cross-platform — pure stdlib, runs on Linux / macOS / Windows.
* Explicit placeholder allowlist (``SCRUB_PLACEHOLDERS``) instead of the bash
  "starts with S" heuristic — closes the cookie-value leak gap (a real token
  whose first byte is ``S`` no longer slips through).
* ``--strict`` flag disables the repair allowlist for CI gating once
  cleanup is done.
* Reports ``file:line`` for every leak so a developer can jump straight to
  the offending interaction.

Usage::

    uv run python tests/scripts/check_cassettes_clean.py
    uv run python tests/scripts/check_cassettes_clean.py path/to/cassette.yaml
    uv run python tests/scripts/check_cassettes_clean.py --strict

Exit codes:
    0 — every scanned cassette is clean
    1 — one or more leaks detected (printed to stdout)

Implementation note:  the tool reads each cassette line-by-line (no PyYAML
parse) so that:

* It runs in O(stream) memory even on multi-megabyte cassettes.
* Reported line numbers map directly to the file on disk.
* It can also scan partial / malformed YAML that a real recorder might emit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python tests/scripts/check_cassettes_clean.py` from anywhere by
# putting the repo root on sys.path so ``tests.cassette_patterns`` resolves.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.cassette_patterns import is_clean  # noqa: E402

DEFAULT_CASSETTE_DIR = _REPO_ROOT / "tests" / "cassettes"
DEFAULT_ALLOWLIST = _REPO_ROOT / "tests" / "scripts" / "cassette_repair_allowlist.txt"


def _load_allowlist(path: Path) -> set[str]:
    """Read the repair allowlist as a set of cassette basenames.

    Blank lines and ``#`` comments are ignored.  Entries are interpreted as
    cassette basenames (e.g. ``chat_ask.yaml``) — paths or globs are not
    supported, by design, to keep the file boring and auditable.
    """
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _iter_cassettes(paths: list[str]) -> list[Path]:
    """Resolve CLI arguments into a concrete list of cassette files.

    * If no paths are given, scan ``tests/cassettes/*.yaml``.
    * If a directory is given, scan ``*.yaml`` inside it (non-recursive — the
      bash original was also single-level).
    * If a file is given, scan it directly.
    * Non-existent paths are silently skipped — matches the bash original's
      "scan what exists" behaviour and keeps the tool friendly to pre-commit
      hooks that may pass deleted-but-still-staged paths.
    """
    if not paths:
        if not DEFAULT_CASSETTE_DIR.exists():
            return []
        return sorted(DEFAULT_CASSETTE_DIR.glob("*.yaml"))

    resolved: list[Path] = []
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_dir():
            resolved.extend(sorted(candidate.glob("*.yaml")))
        elif candidate.is_file():
            resolved.append(candidate)
    return resolved


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return ``(line_number, leak_description)`` for each leak.

    Reads the cassette line-by-line (no PyYAML parse) so the tool runs in
    streaming memory even on multi-megabyte cassettes, ``file:line`` numbers
    map directly to the on-disk file, and the guard can scan partial/malformed
    YAML a recorder might emit before VCR finalises the file.

    Each leak description is the human-readable string emitted by
    :func:`tests.cassette_patterns.is_clean` (e.g. ``"Leak (cookie header):
    cookie 'SID' value 'abc' is not a known scrub placeholder"``).
    """
    leaks: list[tuple[int, str]] = []
    try:
        # ``errors="replace"`` guarantees a corrupted cassette never crashes
        # the guard mid-scan; we still produce useful output.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, start=1):
                ok, line_leaks = is_clean(line)
                if ok:
                    continue
                for description in line_leaks:
                    leaks.append((line_no, description))
    except OSError as exc:
        print(f"{path}: could not read ({exc})", file=sys.stderr)
    return leaks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan VCR cassettes for sensitive-data leaks. "
            "Returns exit 1 on any leak; otherwise exit 0."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Cassette file(s) or directory. If omitted, scans "
            "tests/cassettes/*.yaml from the repo root."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Ignore the repair allowlist. Use this in CI after the phase-2 "
            "cassette cleanup is done so newly-leaking cassettes can no "
            "longer be silently allow-listed."
        ),
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help=(
            "Path to the repair allowlist file "
            "(default: tests/scripts/cassette_repair_allowlist.txt)."
        ),
    )
    args = parser.parse_args(argv)

    cassettes = _iter_cassettes(args.paths)
    if not cassettes:
        # Fresh checkout with no recorded cassettes — that's a valid clean
        # state, matching the bash original's behaviour.
        print("OK: no cassettes to scan")
        return 0

    allowlist = set() if args.strict else _load_allowlist(args.allowlist)

    scanned = 0
    skipped = 0
    leaked_files: list[Path] = []
    total_leaks = 0

    for cassette in cassettes:
        if not args.strict and cassette.name in allowlist:
            skipped += 1
            continue
        scanned += 1
        leaks = _scan_file(cassette)
        if leaks:
            leaked_files.append(cassette)
            total_leaks += len(leaks)
            try:
                rel = cassette.relative_to(_REPO_ROOT)
            except ValueError:
                rel = cassette
            for line_no, description in leaks:
                print(f"{rel}:{line_no}: {description}")

    # Summary always prints, even on the happy path, so the operator gets a
    # one-line confirmation that the guard actually ran.
    print(
        f"\nSummary: {scanned} cassettes scanned"
        + (f", {skipped} allow-listed" if skipped else "")
        + f", {total_leaks} leaks found in {len(leaked_files)} files."
    )

    return 1 if leaked_files else 0


if __name__ == "__main__":
    raise SystemExit(main())
