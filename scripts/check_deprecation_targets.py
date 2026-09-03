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
``will be removed in vX.Y.Z`` / ``removal in vX.Y.Z``). It also parses the
immutable ``DEPRECATION_SPECS`` auth-storage table and its literal registered
calls without importing package code. Keys/callsites must match, versions must
be literal semantic versions, and public replacements must resolve structurally.
It is wired alongside the other ``scripts/check_*.py`` static gates and is also
exercised by ``tests/unit/test_check_deprecation_targets.py``.

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
       (modulo documented allowlist entries), and the registry is coherent.
    1  One or more offending messages or registry errors were found.
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
_REGISTERED_EMITTER = "warn_registered_deprecation"
_SPEC_TABLE = "DEPRECATION_SPECS"
_SPEC_FIELDS = ("key", "message", "category", "replacement", "since", "removal", "stacklevel")
_REGISTERED_SPEC_KEYS = frozenset(
    {
        "auth_tokens_flat_cookies",
        "auth_tokens_from_storage",
        "auth_tokens_sync_storage_construction",
        "client_rpc_call_android",
        "client_rpc_call_web",
    }
)
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")


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


def _target_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = [target.id for target in targets if isinstance(target, ast.Name)]
    return names[0] if len(names) == 1 else None


def _literal(node: ast.AST, field: str, problems: list[str]) -> object | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return node.value
    problems.append(f"{field} must be a string/integer literal")
    return None


def _spec_arguments(node: ast.Call, problems: list[str]) -> dict[str, ast.AST]:
    if len(node.args) > len(_SPEC_FIELDS):
        problems.append("DeprecationSpec has too many positional arguments")
        return {}
    values = dict(zip(_SPEC_FIELDS, node.args, strict=False))
    for keyword in node.keywords:
        if keyword.arg is None:
            problems.append("DeprecationSpec may not use ** expansion")
            continue
        if keyword.arg not in _SPEC_FIELDS:
            problems.append(f"DeprecationSpec has unknown field {keyword.arg!r}")
            continue
        if keyword.arg in values:
            problems.append(f"DeprecationSpec repeats field {keyword.arg!r}")
            continue
        values[keyword.arg] = keyword.value
    missing = sorted(set(_SPEC_FIELDS) - values.keys())
    if missing:
        problems.append(f"DeprecationSpec is missing fields: {', '.join(missing)}")
    return values


def _find_public_symbol(
    module: ast.Module, name: str
) -> ast.AST | tuple[int, str | None, str] | None:
    for statement in module.body:
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name == name:
                return statement
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                if (alias.asname or alias.name) == name:
                    return statement.level, statement.module, alias.name
    return None


def _module_source(module_parts: tuple[str, ...]) -> tuple[Path, bool] | None:
    """Return one unambiguous in-package module source and whether it is a package."""
    module_path = SRC_ROOT.joinpath(*module_parts)
    module_file = module_path.with_suffix(".py") if module_parts else SRC_ROOT / "__init__.py"
    package_file = module_path / "__init__.py" if module_parts else None
    candidates = [(module_file, False)]
    if package_file is not None:
        candidates.append((package_file, True))
    existing = [candidate for candidate in candidates if candidate[0].is_file()]
    if len(existing) != 1:
        return None
    return existing[0]


def _imported_module_parts(
    current_parts: tuple[str, ...],
    current_is_package: bool,
    level: int,
    imported_module: str | None,
) -> tuple[str, ...] | None:
    """Resolve an ``ImportFrom`` module relative to its containing package."""
    imported_parts = tuple(imported_module.split(".")) if imported_module else ()
    if level == 0:
        if not imported_parts or imported_parts[0] != "notebooklm":
            return None
        return imported_parts[1:]

    package_parts = current_parts if current_is_package else current_parts[:-1]
    parents_to_drop = level - 1
    if parents_to_drop > len(package_parts):
        return None
    if parents_to_drop:
        package_parts = package_parts[:-parents_to_drop]
    return package_parts + imported_parts


def _replacement_resolves(replacement: str) -> bool:
    """Resolve a dotted public target structurally, without importing package code."""
    parts = replacement.split(".")
    if len(parts) < 2 or parts[0] != "notebooklm" or any(not part for part in parts):
        return False

    # Prefer the longest concrete public module prefix.  Most historical
    # replacements start from a symbol re-exported by ``notebooklm.__init__``;
    # backend-selected raw replacements intentionally start from the public
    # ``notebooklm.raw`` module instead.
    module_parts: tuple[str, ...] = ()
    symbol_index = 1
    for index in range(len(parts) - 1, 1, -1):
        candidate = tuple(parts[1:index])
        if _module_source(candidate) is not None:
            module_parts = candidate
            symbol_index = index
            break
    symbol_name = parts[symbol_index]
    visited_reexports: set[tuple[tuple[str, ...], str]] = set()
    while True:
        visit = (module_parts, symbol_name)
        if visit in visited_reexports:
            return False
        visited_reexports.add(visit)

        source_info = _module_source(module_parts)
        if source_info is None:
            return False
        source, is_package = source_info
        module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        target = _find_public_symbol(module, symbol_name)
        if not isinstance(target, tuple):
            if target is None:
                return False
            break
        level, imported_module, symbol_name = target
        resolved_parts = _imported_module_parts(
            module_parts,
            is_package,
            level,
            imported_module,
        )
        if resolved_parts is None:
            return False
        module_parts = resolved_parts

    for attribute in parts[symbol_index + 1 :]:
        if not isinstance(target, ast.ClassDef):
            return False
        target = next(
            (
                statement
                for statement in target.body
                if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name == attribute
            ),
            None,
        )
        if target is None:
            return False
    return True


def _version_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _registered_deprecation_problems(version: str) -> list[str]:
    """Validate the immutable spec table and its literal registered callsites."""
    spec_path = SRC_ROOT / "_deprecation.py"
    modules: list[tuple[Path, ast.Module]] = []
    calls: dict[str, list[str]] = {}
    problems: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules.append((path, module))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node.func).rsplit(".", 1)[-1] != _REGISTERED_EMITTER:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if len(node.args) != 1 or node.keywords:
                problems.append(f"{rel}:{node.lineno}: registered call must have one literal key")
                continue
            key = _literal(node.args[0], "registered deprecation key", problems)
            if not isinstance(key, str):
                continue
            calls.setdefault(key, []).append(f"{rel}:{node.lineno}")

    # Synthetic inline-warning tests intentionally omit the registry module.
    # A real registered call without its table is still a hard failure.
    if not spec_path.is_file():
        if calls:
            problems.append(f"{_SPEC_TABLE} is missing")
        return problems

    spec_module = next(module for path, module in modules if path == spec_path)
    assignments = [
        node
        for node in spec_module.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _target_name(node) == _SPEC_TABLE
    ]
    if len(assignments) != 1:
        problems.append(f"{_SPEC_TABLE} must have exactly one assignment")
        return problems
    value = assignments[0].value
    if (
        not isinstance(value, ast.Call)
        or _call_name(value.func) != "MappingProxyType"
        or len(value.args) != 1
        or value.keywords
        or not isinstance(value.args[0], ast.Dict)
    ):
        problems.append(f"{_SPEC_TABLE} must be one literal MappingProxyType dictionary")
        return problems

    specs: dict[str, dict[str, object]] = {}
    table = value.args[0]
    for key_node, spec_node in zip(table.keys, table.values, strict=True):
        if key_node is None:
            problems.append(f"{_SPEC_TABLE} may not use ** expansion")
            continue
        table_key = _literal(key_node, "deprecation table key", problems)
        if not isinstance(table_key, str) or not table_key:
            problems.append("deprecation table key must be a non-empty string")
            continue
        if table_key in specs:
            problems.append(f"duplicate deprecation spec key: {table_key}")
            continue
        if not isinstance(spec_node, ast.Call) or _call_name(spec_node.func) != "DeprecationSpec":
            problems.append(f"{table_key}: value must be a literal DeprecationSpec call")
            continue
        arguments = _spec_arguments(spec_node, problems)
        if set(arguments) != set(_SPEC_FIELDS):
            continue
        parsed: dict[str, object] = {}
        for field in ("key", "message", "replacement", "since", "removal", "stacklevel"):
            parsed[field] = _literal(arguments[field], f"{table_key}.{field}", problems)
        category = arguments["category"]
        if not isinstance(category, ast.Name) or category.id != "DeprecationWarning":
            problems.append(f"{table_key}.category must be literal DeprecationWarning")
        parsed["category"] = "DeprecationWarning"
        specs[table_key] = parsed

    actual_keys = frozenset(specs)
    if actual_keys != _REGISTERED_SPEC_KEYS:
        problems.append(
            f"{_SPEC_TABLE} keys differ: expected {sorted(_REGISTERED_SPEC_KEYS)!r}, "
            f"got {sorted(actual_keys)!r}"
        )

    for table_key, spec in specs.items():
        if spec.get("key") != table_key:
            problems.append(f"{table_key}: table key and spec.key differ")
        message = spec.get("message")
        if not isinstance(message, str) or not message.strip():
            problems.append(f"{table_key}.message must be a non-empty string literal")
        replacement = spec.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip():
            problems.append(f"{table_key}.replacement must be a non-empty string literal")
        elif not _replacement_resolves(replacement):
            problems.append(f"{table_key}.replacement does not resolve: {replacement}")
        for field in ("since", "removal"):
            field_value = spec.get(field)
            if not isinstance(field_value, str) or not _SEMVER.fullmatch(field_value):
                problems.append(f"{table_key}.{field} must be a literal semantic version")
        since = spec.get("since")
        removal = spec.get("removal")
        valid_since = isinstance(since, str) and _SEMVER.fullmatch(since)
        valid_removal = isinstance(removal, str) and _SEMVER.fullmatch(removal)
        if valid_since and valid_removal and _version_key(since) >= _version_key(removal):
            problems.append(f"{table_key}.since must precede removal")
        if valid_removal and _SEMVER.fullmatch(version):
            if _version_key(removal) <= _version_key(version):
                problems.append(
                    f"{table_key}.removal {removal} is not after shipping version {version}"
                )
        stacklevel = spec.get("stacklevel")
        if not isinstance(stacklevel, int) or isinstance(stacklevel, bool) or stacklevel < 1:
            problems.append(f"{table_key}.stacklevel must be a positive integer literal")

    for key, locations in sorted(calls.items()):
        if key not in specs:
            problems.append(f"registered callsite has no spec: {key} at {', '.join(locations)}")
        elif len(locations) != 1:
            problems.append(f"registered spec {key} has {len(locations)} callsites")
    for key in sorted(specs.keys() - calls.keys()):
        problems.append(f"stale deprecation spec has no callsite: {key}")
    return problems


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
    registry_problems = _registered_deprecation_problems(version)

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

    if registry_problems:
        print("Registered deprecation specification errors:", file=sys.stderr)
        for problem in registry_problems:
            print(f"  {problem}", file=sys.stderr)

    if blocking or stale or registry_problems:
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
