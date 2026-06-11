#!/usr/bin/env python3
"""ONE-TIME: clear every cookie value in Cookie/Set-Cookie headers, in place.

Re-scrubs the committed corpus for the name-agnostic cookie leak: cookies that
were NOT on the name-anchored allowlist (``_ga`` / ``_ga_<id>`` / ``_gcl_au`` /
``AEC`` / ``SEARCH_SAMESITE`` …) kept their REAL values in committed cassettes.
This walks every ``tests/cassettes/*.yaml``, parses it with PyYAML to recover
each request ``Cookie:`` and response ``Set-Cookie:`` header's LOGICAL value,
computes the name-agnostic scrub via ``cassette_patterns.scrub_cookie_header`` /
``scrub_set_cookie``, and splices the cleared ``name=value`` pairs back into the
RAW text by literal substitution.

Why literal raw-text substitution (not yaml.dump round-trip): re-dumping the
whole file would reformat every body scalar and produce a massive noisy diff.
Cookie pairs are verified to never split across a YAML fold boundary in this
corpus, so each ``name=realvalue`` substring is contiguous in the raw text and a
literal replace to ``name=SCRUBBED`` is exact and formatting-preserving. Bodies
and byte-count prefixes are untouched.

Retained as a historical repair utility; use
``tests/scripts/check_cassettes_clean.py`` for ongoing cassette cleanliness
checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

try:
    from yaml import CSafeLoader as Loader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as Loader  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"
_CASSETTE_DIR = _TESTS_DIR / "cassettes"
sys.path.insert(0, str(_TESTS_DIR))

from cassette_patterns import (  # noqa: E402
    SCRUB_PLACEHOLDERS,
    find_cookie_leaks,
    scrub_cookie_header,
    scrub_set_cookie,
)


def _pairs(original: str, scrubbed: str) -> list[tuple[str, str]]:
    """Return ``(old_segment, new_segment)`` for each pair the scrub changed.

    Splits the original and scrubbed header value on ``;`` (their segment lists
    are aligned because scrubbing only rewrites VALUES, never the count/order of
    segments) and yields the stripped ``name=value`` pairs that differ.
    """
    out: list[tuple[str, str]] = []
    o_segs = original.split(";")
    s_segs = scrubbed.split(";")
    if len(o_segs) != len(s_segs):
        return out
    for o, s in zip(o_segs, s_segs, strict=True):
        o_core = o.strip()
        s_core = s.strip()
        if o_core != s_core and o_core:
            out.append((o_core, s_core))
    return out


def _rescrub_file(path: Path) -> tuple[bool, int]:
    raw = path.read_text(encoding="utf-8")
    data = yaml.load(raw, Loader=Loader)
    if not isinstance(data, dict):
        return False, 0

    replacements: list[tuple[str, str]] = []
    for interaction in data.get("interactions") or []:
        if not isinstance(interaction, dict):
            continue
        req = interaction.get("request") or {}
        resp = interaction.get("response") or {}
        for hk, hv in (req.get("headers") or {}).items():
            if isinstance(hk, str) and hk.lower() == "cookie":
                vals = hv if isinstance(hv, list) else [hv]
                for v in vals:
                    if isinstance(v, str):
                        replacements.extend(_pairs(v, scrub_cookie_header(v)))
        for hk, hv in (resp.get("headers") or {}).items():
            if isinstance(hk, str) and hk.lower() == "set-cookie":
                vals = hv if isinstance(hv, list) else [hv]
                for v in vals:
                    if isinstance(v, str):
                        replacements.extend(_pairs(v, scrub_set_cookie(v)))

    if not replacements:
        return False, 0

    new_raw = raw
    for old_seg, new_seg in replacements:
        # ``old_seg`` is a contiguous ``name=realvalue`` substring (verified to
        # never split across a fold boundary). Replace ALL occurrences (the same
        # cookie value recurs across every interaction in a cassette).
        if old_seg and old_seg in new_raw:
            new_raw = new_raw.replace(old_seg, new_seg)

    if new_raw == raw:
        return False, 0
    path.write_text(new_raw, encoding="utf-8")
    return True, len(new_raw.encode("utf-8")) - len(raw.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    targets = [Path(p) for p in argv] if argv else sorted(_CASSETTE_DIR.glob("*.yaml"))
    changed = 0
    for path in targets:
        did, diff = _rescrub_file(path)
        if did:
            changed += 1
            print(f"scrubbed  {path.name}  ({diff:+d} bytes)")
    print(f"\nSummary: {changed}/{len(targets)} cassettes re-scrubbed.")

    # Verify: no cookie leak survives in any header of any target.
    residual = 0
    parse_failures = 0
    for path in targets:
        try:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)
        except Exception as exc:  # noqa: BLE001
            # A cassette we just rewrote no longer parses as YAML — treat that as
            # a verification failure, not a silent skip, or the script could exit
            # success after producing an unloadable cassette.
            parse_failures += 1
            print(f"FAILED to parse rewritten cassette {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        for interaction in data.get("interactions") or []:
            if not isinstance(interaction, dict):
                continue
            req = interaction.get("request") or {}
            resp = interaction.get("response") or {}
            for hk, hv in (req.get("headers") or {}).items():
                if isinstance(hk, str) and hk.lower() == "cookie":
                    for v in hv if isinstance(hv, list) else [hv]:
                        if isinstance(v, str) and find_cookie_leaks(v):
                            residual += len(find_cookie_leaks(v))
            for hk, hv in (resp.get("headers") or {}).items():
                if isinstance(hk, str) and hk.lower() == "set-cookie":
                    for v in hv if isinstance(hv, list) else [hv]:
                        if isinstance(v, str) and find_cookie_leaks(v, set_cookie=True):
                            residual += len(find_cookie_leaks(v, set_cookie=True))
    print(f"Residual cookie leaks after re-scrub: {residual}")
    if parse_failures:
        print(f"Unparseable cassettes after re-scrub: {parse_failures}", file=sys.stderr)
    print(f"(known placeholders: {sorted(SCRUB_PLACEHOLDERS)[:3]} ...)")
    return 1 if (residual or parse_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
