#!/usr/bin/env python3
"""Regenerate a snapshot of the test-suite inventory: counts, buckets,
residual ``_core``/`rpc_call` migration debt, skip/xfail tallies, biggest
files.

Run from a contributor checkout::

    uv run python scripts/audit_test_suite.py

Designed to be re-run after every PR so reviewers can compare numbers
against the previous run without rebuilding the analysis machinery in
ad-hoc grep snippets.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def collect_total() -> tuple[int, int]:
    # Invoke pytest via the CURRENT interpreter (``sys.executable -m pytest``)
    # rather than ``uv run pytest``. The latter re-enters uv, which requires
    # uv being on PATH and a writable uv cache dir — neither is guaranteed
    # when the script is run from a vendored venv, a sandboxed CI runner, or
    # ``python -m scripts.audit_test_suite``. ``-m pytest`` stays in-process
    # with the venv that imported us, so the collection number is always
    # consistent with the interpreter actually running this script.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # ``tests?`` covers the singular form pytest emits when the suite has
    # exactly one collected test.
    match = re.search(r"(\d+)\s+tests?\s+collected", result.stdout)
    if not match:
        raise RuntimeError(
            "pytest --collect-only failed or produced unexpected output.\n"
            f"  returncode: {result.returncode}\n"
            f"  stdout (tail): {result.stdout[-500:]}\n"
            f"  stderr (tail): {result.stderr[-500:]}"
        )
    test_count = int(match.group(1))
    file_count = sum(1 for _ in TESTS.rglob("test_*.py"))
    return test_count, file_count


def bucket_counts() -> list[tuple[str, int]]:
    buckets = [
        ("tests/unit/ (root)", TESTS / "unit", False),
        ("tests/unit/cli/", TESTS / "unit" / "cli", True),
        ("tests/unit/concurrency/", TESTS / "unit" / "concurrency", True),
        ("tests/integration/ (root)", TESTS / "integration", False),
        ("tests/integration/concurrency/", TESTS / "integration" / "concurrency", True),
        ("tests/integration/cli_vcr/", TESTS / "integration" / "cli_vcr", True),
        ("tests/e2e/", TESTS / "e2e", True),
        ("tests/_guardrails/", TESTS / "_guardrails", True),
    ]
    out = []
    for label, path, recursive in buckets:
        if recursive:
            count = sum(1 for _ in path.rglob("test_*.py")) if path.exists() else 0
        else:
            count = sum(1 for p in path.glob("test_*.py") if p.is_file()) if path.exists() else 0
        out.append((label, count))
    return out


def cassettes() -> tuple[int, int]:
    root = TESTS / "cassettes"
    if not root.exists():
        return 0, 0
    files = [p for p in root.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _core_module_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm._core":
                    aliases.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "notebooklm":
                for alias in node.names:
                    if alias.name == "_core":
                        aliases.add(alias.asname or "_core")
    return aliases


def scan_core_monkeypatches() -> tuple[list[tuple[Path, int, str]], list[tuple[Path, int, str]]]:
    """Both string-target AND object-target monkeypatches of ``notebooklm._core``."""
    string_hits: list[tuple[Path, int, str]] = []
    object_hits: list[tuple[Path, int, str]] = []

    for path in TESTS.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue

        aliases = _core_module_aliases(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "setattr"
                and isinstance(func.value, ast.Name)
                and func.value.id == "monkeypatch"
            ):
                continue
            if not node.args:
                continue
            first = node.args[0]

            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if first.value.startswith("notebooklm._core."):
                    string_hits.append((path, node.lineno, first.value))
                continue

            if isinstance(first, ast.Name) and first.id in aliases:
                attr = "?"
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    attr = str(node.args[1].value)
                object_hits.append((path, node.lineno, f"{first.id}.{attr}"))

    return string_hits, object_hits


def _is_comment_line(lines: list[str], lineno: int) -> bool:
    """True when ``lineno`` (1-based) is a line whose first non-whitespace
    character is ``#``. Used to filter false-positive matches sitting inside
    commented-out code samples without rejecting matches whose surrounding
    code happens to wrap across lines."""
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].lstrip().startswith("#")
    return False


def _lineno_at(content: str, offset: int) -> int:
    """1-based line number for a character offset inside ``content``."""
    return content.count("\n", 0, offset) + 1


def scan_rpc_call_axis() -> tuple[list[tuple[Path, int]], set[Path]]:
    """``rpc_call = AsyncMock`` / ``monkeypatch.setattr(..., "rpc_call", ...)``.

    Uses ``re.finditer`` on the full file content rather than line-by-line
    matching so that a formatter wrapping a long monkeypatch call across
    multiple lines is still counted. ``AsyncMock`` is matched via a
    dotted-prefix prefix (``mock.AsyncMock``, ``unittest.mock.AsyncMock``,
    bare ``AsyncMock``) to track all canonical assignment shapes.
    """
    hits: list[tuple[Path, int]] = []
    assign_pattern = re.compile(r"\S+\.rpc_call\s*=\s*(?:\w+\.)*AsyncMock")
    mp_pattern = re.compile(r'monkeypatch\.setattr\(\s*[^,]+,\s*["\']rpc_call["\']')

    for path in TESTS.rglob("test_*.py"):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = content.splitlines()
        for pattern in (assign_pattern, mp_pattern):
            for m in pattern.finditer(content):
                lineno = _lineno_at(content, m.start())
                if _is_comment_line(lines, lineno):
                    continue
                hits.append((path, lineno))

    files = {p for p, _ in hits}
    return hits, files


def scan_skipped() -> list[tuple[Path, int, str]]:
    """``pytest.mark.{skip,skipif,xfail}`` references.

    Drops the leading ``@`` so module-level ``pytestmark = pytest.mark.skip
    (...)`` and ``pytestmark = [pytest.mark.skipif(...), ...]`` shapes are
    counted alongside the decorator form. Commented-out markers
    (``# @pytest.mark.skip``) are filtered out via line-prefix check.
    """
    hits: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"pytest\.mark\.(skip|skipif|xfail)\b")
    for path in TESTS.rglob("test_*.py"):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = content.splitlines()
        for m in pattern.finditer(content):
            lineno = _lineno_at(content, m.start())
            if _is_comment_line(lines, lineno):
                continue
            hits.append((path, lineno, m.group(1)))
    return hits


def big_files(top_n: int = 8) -> list[tuple[Path, int, int]]:
    rows = []
    test_def = re.compile(r"^\s*(async\s+)?def\s+test_")
    for path in TESTS.rglob("test_*.py"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        n_tests = sum(1 for line in lines if test_def.match(line))
        rows.append((path, len(lines), n_tests))
    rows.sort(key=lambda r: -r[1])
    return rows[:top_n]


def main() -> int:
    banner("Suite shape")
    total, files = collect_total()
    print(f"Total tests collected (pytest):  {total:>6}")
    print(f"Total test_*.py files (find):    {files:>6}")
    cas_n, cas_b = cassettes()
    print(f"Cassettes:                       {cas_n:>6} files  {fmt_bytes(cas_b)}")

    banner("Files per bucket")
    bucket_total = 0
    for label, n in bucket_counts():
        print(f"  {label:<38} {n:>4}")
        bucket_total += n
    print(f"  {'sum':<38} {bucket_total:>4}")
    if bucket_total != files:
        print(f"  ! mismatch: bucket-sum {bucket_total} vs find total {files}")

    banner("_core.X monkeypatch migration debt")
    string_hits, object_hits = scan_core_monkeypatches()
    print(f"String-target monkeypatch.setattr('notebooklm._core.X', ...):  {len(string_hits)}")
    for path, lineno, tgt in string_hits:
        print(f"  {path.relative_to(REPO_ROOT)}:{lineno}  -> {tgt}")
    print(f"Object-target monkeypatch.setattr(_core_alias, 'X', ...):       {len(object_hits)}")
    for path, lineno, tgt in object_hits:
        print(f"  {path.relative_to(REPO_ROOT)}:{lineno}  -> {tgt}")
    if not string_hits and not object_hits:
        print("  (none — _core.py demolition complete)")

    banner("rpc_call = AsyncMock axis (plan PR 9 cohort)")
    rpc_hits, rpc_files = scan_rpc_call_axis()
    print(f"Assignment / monkeypatch sites:  {len(rpc_hits)}")
    print(f"Files touched:                   {len(rpc_files)}")

    banner("Skipped / xfailed / skipif")
    skip_hits = scan_skipped()
    by_kind = Counter(kind for _, _, kind in skip_hits)
    for kind, n in sorted(by_kind.items()):
        print(f"  pytest.mark.{kind:<10} {n}")
    print(f"  total                {len(skip_hits)}")

    banner("Big files (top 8 by line count)")
    print(f"  {'lines':>6} {'def test_':>10}  path")
    for path, n_lines, n_tests in big_files():
        print(f"  {n_lines:>6} {n_tests:>10}  {path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
