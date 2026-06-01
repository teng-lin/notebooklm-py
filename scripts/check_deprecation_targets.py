"""Release gate: a deprecation must never name the version shipping it.

The recurring defect (issue #1214): a ``DeprecationWarning`` message says the
feature "will be removed in vX.Y.Z" while ``pyproject.toml`` is *currently at*
vX.Y.Z. Shipping that release publishes a deprecation whose stated removal
target is the release itself — the shim is simultaneously "still here" and
"already past its removal version", which is incoherent and means the shim
should have been deleted (or the target bumped) before the release.

This gate scans every ``warnings.warn(...)`` / ``DeprecationWarning(...)``
message string under ``src/notebooklm/`` and fails if any names the version
in ``pyproject.toml`` as a *removal target* (``removed in vX.Y.Z`` /
``will be removed in vX.Y.Z`` / ``removal in vX.Y.Z``). It is wired alongside
the other ``scripts/check_*.py`` static gates and is also exercised by
``tests/unit/test_check_deprecation_targets.py``.

Allowlist
---------
``LAPSED_ALLOWLIST`` holds deprecation sites whose stated removal target has
*already lapsed* and whose shim removal is tracked separately. They are
allowlisted (keyed by file + the offending version) with the tracking issue so
this gate does not block the current release; once the tracking PR deletes the
shim, the matching site disappears and the entry should be dropped (a stale
allowlist entry is reported, keeping the list tightening).

Usage::

    python scripts/check_deprecation_targets.py
    python scripts/check_deprecation_targets.py --pyproject path/to/pyproject.toml

Exit codes:
    0  No deprecation message names the current release as its removal target
       (modulo documented allowlist entries).
    1  One or more offending deprecation messages found (printed with file:line).
    2  Argument / parse error (missing pyproject, unreadable version, etc.).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 path
    try:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]
    except ImportError:
        print(
            "tomli is required on Python 3.10. Install with: uv pip install tomli",
            file=sys.stderr,
        )
        sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# Calls whose first string argument is treated as a user-facing deprecation
# message:
#   * ``warnings.warn`` — the canonical attribute-access form.
#   * bare ``warn`` — the ``from warnings import warn; warn(...)`` form.
#   * ``DeprecationWarning(...)`` — a constructed/raised warning instance.
# The bare ``warn`` is broad on purpose (a release gate prefers a false
# positive over a missed deprecation); the version-naming regex in
# ``_removal_pattern`` keeps incidental ``warn()`` calls from matching unless
# they actually name the shipping version as a removal target.
_DEPRECATION_CALL_NAMES = frozenset({"warnings.warn", "warn", "DeprecationWarning"})


class _Offender:
    __slots__ = ("path", "lineno", "version", "snippet")

    def __init__(self, path: str, lineno: int, version: str, snippet: str) -> None:
        self.path = path
        self.lineno = lineno
        self.version = version
        self.snippet = snippet

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.version)


# ---------------------------------------------------------------------------
# Allowlist — deprecation sites whose stated removal target has already lapsed.
#
# These are removed by a separate tracking PR; allowlisting them (with the
# issue reference) keeps this gate from blocking the current release while the
# shim deletion lands. Keyed by (relative-posix-path, offending-version).
# ---------------------------------------------------------------------------
class _LapsedEntry:
    __slots__ = ("path", "version", "issue", "reason")

    def __init__(self, path: str, version: str, issue: int, reason: str) -> None:
        self.path = path
        self.version = version
        self.issue = issue
        self.reason = reason

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.version)


LAPSED_ALLOWLIST: tuple[_LapsedEntry, ...] = ()

_ALLOWLIST_BY_KEY = {entry.key: entry for entry in LAPSED_ALLOWLIST}


def _read_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    try:
        version = data["project"]["version"]
    except (KeyError, TypeError) as exc:  # pragma: no cover - malformed pyproject
        raise KeyError("project.version not found in pyproject.toml") from exc
    if not isinstance(version, str):  # pragma: no cover - defensive
        raise TypeError(f"project.version is not a string: {version!r}")
    return version


def _removal_pattern(version: str) -> re.Pattern[str]:
    """Match removal language naming *version* (with or without a ``v`` prefix).

    Examples matched (version == ``0.6.0``)::

        will be removed in v0.6.0
        removed in 0.6.0
        removal in v0.6.0
        scheduled for removal in 0.6.0
    """
    escaped = re.escape(version)
    return re.compile(
        r"(?:will\s+be\s+)?remov(?:ed|al)\s+in\s+v?" + escaped + r"\b",
        re.IGNORECASE,
    )


def _flatten_message(node: ast.AST) -> str:
    """Best-effort flatten of a (possibly f-string / concatenated) message arg.

    Concatenated string literals (``"a" "b"``) parse as a single ``Constant``;
    implicit ``+`` concatenation and f-strings need walking. We collect every
    string constant reachable from the first argument so split removal phrases
    like ``"... removed in " f"v{X}"`` are not the concern here (the version is
    a literal in the offending sites), but multi-literal messages still match.
    """
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return " ".join(parts)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _scan(version: str) -> list[_Offender]:
    pattern = _removal_pattern(version)
    offenders: list[_Offender] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node.func) not in _DEPRECATION_CALL_NAMES:
                continue
            # The message is the first positional arg, or the ``message=``
            # keyword (``warnings.warn(message=...)``). Checking both keeps a
            # keyword-form deprecation from bypassing the gate.
            message_node: ast.AST | None = node.args[0] if node.args else None
            if message_node is None:
                for kw in node.keywords:
                    if kw.arg == "message":
                        message_node = kw.value
                        break
            if message_node is None:
                continue
            message = _flatten_message(message_node)
            if pattern.search(message):
                snippet = message.strip()
                if len(snippet) > 100:
                    snippet = snippet[:97] + "..."
                offenders.append(_Offender(rel, node.lineno, version, snippet))
    return offenders


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--pyproject",
        default=str(REPO_ROOT / "pyproject.toml"),
        help="Path to pyproject.toml (default: repo root)",
    )
    args = ap.parse_args(argv)

    pyproject = Path(args.pyproject)
    if not pyproject.is_file():
        print(f"pyproject.toml not found: {pyproject}", file=sys.stderr)
        return 2
    try:
        version = _read_version(pyproject)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Could not read project.version: {exc}", file=sys.stderr)
        return 2

    offenders = _scan(version)

    blocking: list[_Offender] = []
    matched_allowlist_keys: set[tuple[str, str]] = set()
    for offender in offenders:
        if offender.key in _ALLOWLIST_BY_KEY:
            matched_allowlist_keys.add(offender.key)
            continue
        blocking.append(offender)

    stale = sorted(
        f"  {entry.path} (v{entry.version}, #{entry.issue})"
        for entry in LAPSED_ALLOWLIST
        if entry.key not in matched_allowlist_keys
    )

    # Report BOTH problems in one pass: a developer fixing a blocking offender
    # should also see any stale allowlist entry without needing a second run.
    if blocking:
        print(
            f"Deprecation message(s) name the current release version "
            f"v{version} as their removal target:",
            file=sys.stderr,
        )
        for offender in blocking:
            print(f"  {offender.path}:{offender.lineno}: {offender.snippet}", file=sys.stderr)
        print(
            "\nA deprecation must never point at the version shipping it. Either "
            "delete the shim before this release, or bump the stated removal "
            "target to a future version. If the removal is tracked separately, "
            "add the site to LAPSED_ALLOWLIST with its tracking issue.",
            file=sys.stderr,
        )

    if stale:
        print(
            "Stale deprecation-target allowlist entries (no matching deprecation "
            "message found — remove from LAPSED_ALLOWLIST):",
            file=sys.stderr,
        )
        for line in stale:
            print(line, file=sys.stderr)

    if blocking or stale:
        return 1

    allowlisted = len(matched_allowlist_keys)
    suffix = f" ({allowlisted} allowlisted, lapsed)" if allowlisted else ""
    print(
        f"OK: no deprecation message names the current release v{version} as a "
        f"removal target{suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
