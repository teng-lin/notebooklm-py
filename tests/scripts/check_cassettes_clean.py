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

from tests.cassette_patterns import (  # noqa: E402
    find_cookie_leaks,
    find_credential_leaks,
    is_clean,
)

DEFAULT_CASSETTE_DIR = _REPO_ROOT / "tests" / "cassettes"
# Extensions scanned in ``--secrets-only`` mode. Golden RPC fixtures are
# ``.json`` and the captured-page fixture is ``.html``; the default cassette
# scan only globs ``.yaml``, so a Google API key smuggled into a golden HTML
# fixture (the GET_INTERACTIVE_HTML.json shape) would slip past the cassette
# guard entirely. ``--secrets-only`` widens both the file types AND the
# directories scanned.
_SECRETS_ONLY_EXTENSIONS: tuple[str, ...] = (".yaml", ".yml", ".json", ".html", ".txt")
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


# Subdirectories of ``tests/cassettes/`` that hold ILLUSTRATIVE fixtures
# (not real recordings) and are filtered out of the recursive scan. These
# files use placeholder cookie / token values with intentional formatting
# quirks (truncated YAML strings, hand-edited content) that would trip the
# leak detector even though they contain no actual secrets — see the
# README in ``tests/cassettes/examples/`` for the design intent.
#
# The ``tests/integration/conftest.py`` cassette-availability check already
# excludes ``example_*`` cassettes from the "real recordings present"
# count via a similar filter; this constant carries the same exclusion
# semantic onto the guard tool.
_EXAMPLE_SUBDIRS: frozenset[str] = frozenset({"examples"})


def _iter_cassettes(
    paths: list[str],
    recursive: bool = False,
    extensions: tuple[str, ...] = (".yaml",),
    skip_examples: bool = True,
) -> list[Path]:
    """Resolve CLI arguments into a concrete list of cassette files.

    * If no paths are given, scan ``tests/cassettes/*.yaml`` (non-recursive)
      OR ``tests/cassettes/**/*.yaml`` when ``recursive=True``.
    * If a directory is given, scan ``*.yaml`` inside it (recursively when
      ``recursive=True``).
    * If a file is given, scan it directly.
    * Non-existent paths are silently skipped — matches the bash original's
      "scan what exists" behaviour and keeps the tool friendly to pre-commit
      hooks that may pass deleted-but-still-staged paths.
    * When ``skip_examples`` is set (the default), files under an
      ``examples/`` directory (any depth) and ``example_*`` files are excluded
      from directory scans — they are illustrative fixtures with placeholder
      cookies and intentional YAML quirks, not real recordings (see
      :data:`_EXAMPLE_SUBDIRS`). Their placeholder content trips the full
      :func:`is_clean` heuristics, so the default cassette scan filters them.

    ``skip_examples`` is set to ``False`` for ``--secrets-only`` scans: that
    mode only matches high-severity credential shapes (which never occur in
    placeholder fixtures), so there is no false-positive reason to skip
    ``examples/`` — and skipping them WOULD be a blind spot for credential
    hunting over a directory like ``tests/fixtures/`` (coderabbit review on
    #1266). Explicitly-named file paths are always scanned regardless.

    The ``recursive`` flag is what P1-5 adds: CI now scans subdirectories of
    ``tests/cassettes/`` (e.g. ``gzip_coverage/``) so a recorder cannot
    smuggle a leak into a nested folder. The default stays non-recursive so
    existing developer workflows (running the guard on a single file or the
    top-level directory) are unchanged.
    """
    glob_patterns = [(f"**/*{ext}" if recursive else f"*{ext}") for ext in extensions]

    def _globdir(d: Path) -> list[Path]:
        found: list[Path] = []
        for pat in glob_patterns:
            found.extend(d.glob(pat))
        return sorted(set(found))

    def _is_example_path(p: Path) -> bool:
        # An ``example_`` file at the top level OR any file under an
        # ``examples/`` directory anywhere is treated as illustrative and
        # excluded from directory scans (only when ``skip_examples``).
        if p.name.startswith("example_"):
            return True
        return any(part in _EXAMPLE_SUBDIRS for part in p.parts)

    def _keep(p: Path) -> bool:
        return not (skip_examples and _is_example_path(p))

    if not paths:
        if not DEFAULT_CASSETTE_DIR.exists():
            return []
        return [p for p in _globdir(DEFAULT_CASSETTE_DIR) if _keep(p)]

    resolved: list[Path] = []
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_dir():
            resolved.extend(p for p in _globdir(candidate) if _keep(p))
        elif candidate.is_file():
            # Explicit file paths are always scanned, even if under
            # ``examples/`` — the operator asked for them by name.
            resolved.append(candidate)
    return resolved


def _scan_cookie_headers_yaml(path: Path) -> list[tuple[int, str]]:
    """YAML-aware, name-agnostic cookie-value leak scan over a cassette.

    The line-by-line :func:`is_clean` pass below cannot see a cookie pair that a
    folded YAML scalar split onto a continuation line (the session-cookie anchor
    that scopes the name-agnostic check lives on a different physical line). This
    pass parses the cassette with PyYAML, recovers each request ``Cookie:`` and
    response ``Set-Cookie:`` header as its LOGICAL (joined) value, and runs the
    name-agnostic :func:`tests.cassette_patterns.find_cookie_leaks` on it — so an
    off-allowlist cookie (``_ga`` / ``_gcl_au`` / ``AEC`` …) whose real value
    survives in a folded scalar is caught regardless of line wrapping.

    Line number ``0`` is reported (the leak is logical, not tied to one physical
    line of a folded scalar). A YAML parse failure is non-fatal — the
    line-by-line pass still runs — so a malformed cassette degrades to the
    streaming scan rather than crashing the guard.
    """
    leaks: list[tuple[int, str]] = []
    try:
        import yaml

        try:
            from yaml import CSafeLoader as _Loader
        except ImportError:  # pragma: no cover - libyaml optional
            from yaml import SafeLoader as _Loader  # type: ignore[assignment]

        raw = path.read_text(encoding="utf-8", errors="replace")
        data = yaml.load(raw, Loader=_Loader)
    except Exception:  # noqa: BLE001 - degrade to line-scan on any parse/read error
        return leaks
    if not isinstance(data, dict):
        return leaks

    def _values(header_block: object, key: str) -> list[str]:
        if not isinstance(header_block, dict):
            return []
        for hk, hv in header_block.items():
            if isinstance(hk, str) and hk.lower() == key.lower():
                if isinstance(hv, list):
                    return [v for v in hv if isinstance(v, str)]
                if isinstance(hv, str):
                    return [hv]
        return []

    for interaction in data.get("interactions") or []:
        if not isinstance(interaction, dict):
            continue
        request = interaction.get("request") or {}
        response = interaction.get("response") or {}
        for value in _values(request.get("headers"), "Cookie"):
            for desc in find_cookie_leaks(value):
                leaks.append((0, desc))
        for value in _values(response.get("headers"), "Set-Cookie"):
            for desc in find_cookie_leaks(value, set_cookie=True):
                leaks.append((0, desc))
    return leaks


def _scan_file(path: Path, secrets_only: bool = False) -> list[tuple[int, str]]:
    """Return ``(line_number, leak_description)`` for each leak.

    Reads the cassette line-by-line (no PyYAML parse) so the tool runs in
    streaming memory even on multi-megabyte cassettes, ``file:line`` numbers
    map directly to the on-disk file, and the guard can scan partial/malformed
    YAML a recorder might emit before VCR finalises the file.

    Each leak description is the human-readable string emitted by
    :func:`tests.cassette_patterns.is_clean` (e.g. ``"Leak (cookie header):
    cookie 'SID' value 'abc' is not a known scrub placeholder"``).

    When ``secrets_only`` is set the per-line check uses
    :func:`tests.cassette_patterns.find_credential_leaks` instead — only the
    high-severity credential shapes (auth tokens + Google API keys), which never
    match legitimate placeholder fixture content. That is what makes scanning
    fixture directories full of ``"Scrubbed ..."`` placeholders viable.

    For full (non-``secrets_only``) scans a SECOND, YAML-aware cookie pass
    (:func:`_scan_cookie_headers_yaml`) runs name-agnostic cookie-value
    detection on each cassette's joined ``Cookie:`` / ``Set-Cookie:`` header
    values — catching off-allowlist cookies (``_ga`` …) that a folded YAML
    scalar split across lines the streaming pass cannot stitch back together.
    """
    leaks: list[tuple[int, str]] = []
    try:
        # ``errors="replace"`` guarantees a corrupted cassette never crashes
        # the guard mid-scan; we still produce useful output.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, start=1):
                if secrets_only:
                    line_leaks = find_credential_leaks(line)
                else:
                    ok, line_leaks = is_clean(line)
                    if ok:
                        continue
                for description in line_leaks:
                    leaks.append((line_no, description))
    except OSError as exc:
        print(f"{path}: could not read ({exc})", file=sys.stderr)

    if not secrets_only:
        # De-duplicate against the line-pass: the YAML pass catches folded-scalar
        # cookie leaks the streaming pass cannot stitch together; an on-allowlist
        # leak found by both is reported once.
        seen = {desc for _, desc in leaks}
        for line_no, desc in _scan_cookie_headers_yaml(path):
            if desc not in seen:
                seen.add(desc)
                leaks.append((line_no, desc))
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
            "Ignore the repair allowlist AND fail with exit code 1 if any "
            "allowlist entry is present in the working tree. Use this in "
            "CI: the allowlist is a one-way ratchet for cleanup, and "
            "``--strict`` flips it from 'best-effort suppressor' to "
            "'must be empty'."
        ),
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help=(
            "Scan ``tests/cassettes/**/*.yaml`` (recurse into subdirectories) "
            "instead of the default top-level-only ``tests/cassettes/*.yaml``. "
            "Required in CI so a recorder cannot smuggle a leak into a nested "
            "folder like ``tests/cassettes/gzip_coverage/``."
        ),
    )
    parser.add_argument(
        "--secrets-only",
        action="store_true",
        help=(
            "Scan only for high-severity credential shapes (Google auth tokens "
            "and Google API keys) instead of the full cookie/PII heuristics. "
            "Also widens the scanned file types to .json/.html/.txt (not just "
            ".yaml). Use this to scan fixture directories such as "
            "tests/fixtures/ that contain intentional placeholder content "
            "(e.g. 'Scrubbed Note Title') which would false-positive under the "
            "full is_clean() heuristics."
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

    extensions = _SECRETS_ONLY_EXTENSIONS if args.secrets_only else (".yaml",)
    # ``--secrets-only`` matches only credential shapes (no false positives on
    # placeholder content), so it must NOT skip ``examples/`` — doing so would
    # be a blind spot for credential hunting over fixture dirs (#1266). Default
    # (full-heuristic) scans keep skipping examples to avoid placeholder noise.
    cassettes = _iter_cassettes(
        args.paths,
        recursive=args.recursive,
        extensions=extensions,
        skip_examples=not args.secrets_only,
    )
    if not cassettes:
        # Fresh checkout with no recorded cassettes — that's a valid clean
        # state, matching the bash original's behaviour.
        print("OK: no cassettes to scan")
        return 0

    if args.strict:
        # Strict mode: ignore the allowlist for scan-skip purposes AND
        # require the allowlist to be empty (or non-existent). Any entry
        # present in the file is treated as a CI failure so the allowlist
        # cannot quietly grow past the cleanup phase.
        allowlist: set[str] = set()
        present_in_allowlist = _load_allowlist(args.allowlist)
        if present_in_allowlist:
            print(
                f"ERROR (--strict): {args.allowlist} contains "
                f"{len(present_in_allowlist)} entries; --strict requires the "
                "allowlist to be empty. Either drop the entries or run "
                "without --strict for the cleanup-in-progress workflow."
            )
            for entry in sorted(present_in_allowlist):
                print(f"  - {entry}")
            return 1
    else:
        allowlist = _load_allowlist(args.allowlist)

    scanned = 0
    skipped = 0
    leaked_files: list[Path] = []
    total_leaks = 0

    for cassette in cassettes:
        if not args.strict and cassette.name in allowlist:
            skipped += 1
            continue
        scanned += 1
        leaks = _scan_file(cassette, secrets_only=args.secrets_only)
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
