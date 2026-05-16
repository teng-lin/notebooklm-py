#!/usr/bin/env python3
"""Per-method RPC coverage gate.

Walks every member of :class:`notebooklm.rpc.types.RPCMethod` and asserts that
each one has **both**:

1. **A test reference** — at least one file under ``tests/`` (excluding the
   coverage gate itself, its allowlist record, and helper data files that
   merely enumerate enum members) mentions the enum member by its qualified
   name (e.g. ``RPCMethod.LIST_NOTEBOOKS``) OR by its raw RPC id string value
   (e.g. ``"wXbhsf"``).
2. **A cassette covering the RPC id** — at least one cassette YAML under
   ``tests/cassettes/`` contains the raw RPC id string in its body. This is a
   pure-text grep — the cassette format is YAML but we never parse it; any
   occurrence of the id within the file counts, because ``batchexecute`` URLs
   and request bodies include the id verbatim.

Either failure prints a single line of the form::

    MISSING: RPCMethod.<NAME> (id=<rpc_id>): <which check failed; suggestion>

and the script exits 1. Exit 0 on full coverage.

Pre-existing-gap allowlist
--------------------------

When this gate first landed, some methods (notably newer or write-rarely
exercised ones) already lacked one or both forms of coverage. Forcing
contributors to backfill cassettes for unrelated methods before they could
ship anything new would have stalled the arc, so the gate accepts a
:data:`PREEXISTING_GAPS` set listing the (RPCMethod-name) entries grandfathered
in at landing time.

The allowlist is intended as a **one-way ratchet**:

* It **must not grow** when a new ``RPCMethod`` member is added — new methods
  must ship with at least one test reference and at least one cassette.
* It **may shrink** when a maintainer backfills coverage for a grandfathered
  method; they should delete the entry from :data:`PREEXISTING_GAPS` in the
  same PR.

The script is intentionally a static check (pure text grep on the cassette
files and on the contents of ``tests/``); it never runs pytest or imports
anything that needs a network. That keeps it deterministic, fast, and safe
to run as a CI gate on every PR.

Usage::

    uv run python tests/scripts/check_method_coverage.py

Exit codes:
    0 — every method (modulo :data:`PREEXISTING_GAPS`) is covered
    1 — one or more methods are missing required coverage
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running the script from any CWD by putting the repo root on sys.path
# so ``from notebooklm.rpc.types import RPCMethod`` resolves the same way the
# test suite resolves it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
for _path in (_SRC, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from notebooklm.rpc.types import RPCMethod  # noqa: E402

TESTS_DIR = _REPO_ROOT / "tests"
CASSETTES_DIR = TESTS_DIR / "cassettes"

# This script's own path — exclude it from the "test reference" search so the
# enum-walk loop doesn't trivially satisfy the test-reference check.
_SELF = Path(__file__).resolve()

# Files that mention every enum member structurally (the enum definition
# itself, or helper scripts that iterate over ``RPCMethod`` by reflection) and
# would therefore satisfy the test-reference check for everything. Excluding
# them keeps the gate meaningful — we want a *real* test asserting behaviour,
# not a structural enumeration.
_TEST_REFERENCE_EXCLUDES: frozenset[Path] = frozenset(
    {
        _SELF,
        # Add other reflective enumerators here as they appear.
    }
)

# Pre-existing gaps grandfathered in when this gate landed; new methods
# must NOT be added here. See module docstring for the one-way-ratchet
# policy. The script's bootstrap step (run once locally before commit)
# populated this set. Each entry is the ``RPCMethod.<NAME>`` member name
# (without the ``RPCMethod.`` prefix).
PREEXISTING_GAPS: frozenset[str] = frozenset(
    {
        # Captured by the bootstrap run when this gate landed. See module docstring
        # for the one-way-ratchet policy: shrink this set when you backfill
        # coverage; never add new entries when introducing a new RPCMethod.
        # ``frozenset`` (not ``set``) so the module-level constant cannot be
        # mutated at runtime, matching ``_TEST_REFERENCE_EXCLUDES`` above and
        # reinforcing the "only shrinks" contract structurally.
        "GET_INTERACTIVE_HTML",  # no test imports the enum or its id 'v9rmvd'
        "GET_SUGGESTED_REPORTS",  # no cassette body contains id 'ciyUvf'
        "IMPORT_RESEARCH",  # no cassette body contains id 'LBwxtb'
        "REFRESH_SOURCE",  # no cassette body contains id 'FLmJqe'
    }
)


def _iter_test_files() -> list[Path]:
    """Return every file under ``tests/`` to grep for enum references.

    We walk the directory tree once and snapshot it as a sorted list so
    repeated callers (and the per-method test-reference check) see a
    deterministic set. Cassettes live under ``tests/cassettes/`` but we
    intentionally exclude them — the cassette presence check covers those.
    """
    files: list[Path] = []
    for path in TESTS_DIR.rglob("*"):
        if not path.is_file():
            continue
        # Skip cassettes — they're covered by the separate cassette check.
        if CASSETTES_DIR in path.parents or path == CASSETTES_DIR:
            continue
        # Skip compiled bytecode under ``__pycache__``: ``.pyc`` files inline
        # source-level string constants like ``"wXbhsf"`` as raw UTF-8 bytes,
        # which would let a stale bytecode file silently satisfy the test-
        # reference check after the source was deleted. Cheap to skip, avoids
        # the spurious-match class entirely.
        if "__pycache__" in path.parts:
            continue
        if path.resolve() in _TEST_REFERENCE_EXCLUDES:
            continue
        files.append(path)
    files.sort()
    return files


def _iter_cassette_files() -> list[Path]:
    """Return every ``*.yaml`` cassette under ``tests/cassettes/`` (sorted).

    Sorted output keeps the gate deterministic when reporting failures and
    matches the style of the sister script ``check_cassettes_clean.py``.
    Returns an empty list cleanly when the directory is missing so the gate
    can run on fresh checkouts without exploding.
    """
    if not CASSETTES_DIR.exists():
        return []
    return sorted(CASSETTES_DIR.glob("*.yaml"))


def _file_contains(path: Path, needles: tuple[str, ...]) -> bool:
    """Return True iff ``path`` contains any of ``needles`` as raw text.

    Reads the file as bytes to avoid encoding surprises on large cassette
    files (some contain mojibake-survivable HTTP payloads) and matches the
    UTF-8 bytes of each needle. ``OSError`` on read is treated as "no
    match" rather than crashing the gate — a corrupted cassette would
    already trip ``check_cassettes_clean.py``.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return any(needle.encode("utf-8") in data for needle in needles)


def _has_test_reference(method: RPCMethod, test_files: list[Path]) -> bool:
    """Return True iff any ``tests/`` file references the method by name or id.

    Accepts either the qualified enum reference (``RPCMethod.LIST_NOTEBOOKS``)
    or the raw RPC id string (``"wXbhsf"``) — the latter catches tests that
    assert on encoded request payloads without importing the enum.
    """
    needles = (f"RPCMethod.{method.name}", method.value)
    return any(_file_contains(p, needles) for p in test_files)


def _has_cassette_coverage(method: RPCMethod, cassette_files: list[Path]) -> bool:
    """Return True iff any cassette body contains the raw RPC id string."""
    needles = (method.value,)
    return any(_file_contains(p, needles) for p in cassette_files)


def main() -> int:
    """Run the gate and return the process exit code.

    No CLI flags today — the gate is a pure pass/fail static check. We sort
    enum members by name before iterating so the failure output is stable
    across runs/machines. (The sister script ``check_cassettes_clean.py``
    accepts ``argv`` because it has ``--strict``/``--allowlist`` flags; this
    one has nothing to parse so we keep the signature flag-free rather than
    carrying an unused ``argv`` parameter.)
    """
    test_files = _iter_test_files()
    cassette_files = _iter_cassette_files()

    misses: list[str] = []
    unused_allowlist: list[str] = []

    for method in sorted(RPCMethod, key=lambda m: m.name):
        has_test = _has_test_reference(method, test_files)
        has_cassette = _has_cassette_coverage(method, cassette_files)

        if method.name in PREEXISTING_GAPS:
            # Track entries that no longer need allowlisting so maintainers
            # are nudged to shrink the ratchet.
            if has_test and has_cassette:
                unused_allowlist.append(method.name)
            continue

        if has_test and has_cassette:
            continue

        problems: list[str] = []
        if not has_test:
            problems.append(
                "missing test reference (add a test under tests/ that imports "
                f"RPCMethod.{method.name} or asserts on the raw id '{method.value}')"
            )
        if not has_cassette:
            problems.append(
                "missing cassette coverage (record a cassette under "
                f"tests/cassettes/ whose body contains the RPC id '{method.value}')"
            )
        misses.append(
            f"MISSING: RPCMethod.{method.name} (id={method.value}): {'; '.join(problems)}"
        )

    for line in misses:
        print(line)

    if unused_allowlist:
        # Not a hard failure — but call it out loudly so the next PR can
        # shrink the ratchet. Printed to stderr to keep stdout clean for
        # parseable failure lines.
        print(
            "NOTICE: PREEXISTING_GAPS entries now have full coverage and "
            "should be removed: " + ", ".join(sorted(unused_allowlist)),
            file=sys.stderr,
        )

    total = len(RPCMethod)  # ``EnumMeta`` defines ``__len__`` directly.
    grandfathered = len(PREEXISTING_GAPS)
    checked = total - grandfathered
    print(
        f"\nSummary: {total} RPCMethod members, "
        f"{grandfathered} grandfathered (PREEXISTING_GAPS), "
        f"{checked} actively enforced, "
        f"{len(misses)} missing coverage."
    )

    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
