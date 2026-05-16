#!/usr/bin/env python3
"""Bulk re-scrub VCR cassettes for ``lh3.googleusercontent.com/(a|ogw)/`` avatar URLs.

Collapses every ``lh3.googleusercontent.com/(?:a|ogw)/<token>`` avatar URL to
the canonical ``SCRUBBED_AVATAR_URL`` placeholder and re-derives the chunked
``<count>\\n<payload>\\n`` byte-count prefixes inside every recorded response
body. Writes back only if anything changed; idempotent on a clean tree.

Why this script exists
----------------------
The avatar-URL scrubber (PR #565) added
``lh3.googleusercontent.com/(?:a|ogw)/<token>`` → ``SCRUBBED_AVATAR_URL`` to
the canonical pattern registry, but the ~67 cassettes recorded BEFORE that
pattern landed still embed the raw avatar URLs. They are listed in
``tests/scripts/cassette_repair_allowlist.txt`` under the "/ogw/ avatar URL
group" header; this script is the one-off tool that walks each of those
cassettes, re-scrubs them in place, and reports a byte-level diff so reviewers
can verify the change set.

Why we DON'T call ``scrub_string`` here
---------------------------------------
``cassette_patterns.scrub_string`` applies every pattern in
:data:`SENSITIVE_PATTERNS` — including the escaped-display-name
scrubber that anchors on ``\\"First Last\\"`` inside double-encoded WRB
payloads. That pattern carries a small false-positive allowlist
(``DISPLAY_NAME_FALSE_POSITIVES``) covering font families, UI titles, and
a handful of artifact / notebook titles from the test corpus — but the
existing cassettes contain many more legitimate two-Capitalized-word
titles ("Agent Architecture Quiz", "Study Guide", "Answer Key", "Context
Caching Economics", ...) that would be silently clobbered to
``SCRUBBED_NAME`` and break the cli-vcr tests that rely on matching
those titles in the parsed response.

So the script applies ONLY the avatar-URL scrubber by name. Every other
pattern in the registry — display names, emails, cookies, tokens — is
already correctly applied on record by ``vcr_config.scrub_response`` and
has been for every cassette in the tree; running them again here cannot
produce new scrubs (the registry is idempotent on its own placeholders),
but it CAN produce display-name false positives the recorder's allowlist
was never tuned to cover.

The byte-count re-derivation (``recompute_chunk_prefix``) runs on every
body unconditionally — it's a documented no-op on bodies that don't look
chunked, and a safety net for any chunked body whose avatar URL inside a
WRB payload shifted its length.

Architecture notes
------------------
The script parses each cassette with PyYAML (the same loader vcrpy uses —
``yaml.CLoader`` if libyaml is available, else ``yaml.Loader``) and applies
the scrubbers to every ``response.body.string`` field. After scrubbing, the
cassette is re-emitted with ``yaml.dump(data, Dumper=Dumper)`` — matching
vcrpy's own serializer (``vcrpy/serializers/yamlserializer.py``) — so a
clean cassette round-trips identically.

We deliberately do NOT scrub the raw YAML text: a regex applied to the
wrapped YAML form could match across YAML line-wrap boundaries and corrupt
the file structure. Parsing first guarantees the scrubber only ever sees
the semantic string content the recorder produced, never the YAML
serialization artifacts around it.

Usage
-----
::

    uv run python scripts/rescrub-cassettes.py            # rescrub all
    uv run python scripts/rescrub-cassettes.py <paths...> # rescrub specific files

Exit codes:
    0 — done (whether or not anything changed)
    1 — a cassette failed to parse or write
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# Use libyaml-backed loader/dumper when available — matches vcrpy's
# ``serializers/yamlserializer.py`` exactly so a clean cassette round-trips
# identically through this script.
try:
    from yaml import CDumper as Dumper
    from yaml import CLoader as Loader
except ImportError:  # pragma: no cover — libyaml is a hard dep on dev machines
    from yaml import Dumper, Loader  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"
_CASSETTE_DIR = _TESTS_DIR / "cassettes"

# ``tests/cassette_patterns.py`` lives under ``tests/`` (not a package) so we
# insert that directory on sys.path rather than the repo root.
sys.path.insert(0, str(_TESTS_DIR))

from cassette_patterns import recompute_chunk_prefix  # noqa: E402

# The single regex/replacement pair this script applies. Mirrored verbatim
# from ``cassette_patterns.SENSITIVE_PATTERNS`` (section 13 — see the comment
# block above for the rationale on why we copy it instead of running the
# whole registry). When the canonical pattern changes in
# ``cassette_patterns.py`` this constant should be updated in lockstep —
# the unit test ``test_avatar_pattern_matches_registry`` in
# ``tests/unit/test_rescrub_cassettes_script.py`` asserts the two stay in
# sync so the drift is caught at PR review time.
_AVATAR_URL_RE = re.compile(r"https?://lh3\.googleusercontent\.com/(?:a|ogw)/[A-Za-z0-9_=\-]+")
_AVATAR_URL_REPLACEMENT = "SCRUBBED_AVATAR_URL"


def _scrub_avatar_urls(text: str) -> str:
    """Replace every ``lh3...../(a|ogw)/<token>`` URL with the placeholder.

    Isolated from the rest of the canonical registry by design — see the
    module docstring for why this script doesn't call ``scrub_string``.
    """
    return _AVATAR_URL_RE.sub(_AVATAR_URL_REPLACEMENT, text)


def _rescrub_body(body: str | bytes) -> tuple[str | bytes, bool]:
    """Apply avatar-URL scrub + recompute_chunk_prefix to a single body value.

    Returns ``(new_body, changed)``. The body is preserved in its original
    type (``str`` or ``bytes``) so re-emitted cassettes match what vcrpy
    would have produced.

    Binary bodies that aren't valid UTF-8 (audio, images, gzipped chunks)
    are returned unchanged — the scrub patterns are text-oriented and
    cannot meaningfully apply to binary payloads.
    """
    if isinstance(body, bytes):
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            return body, False
        scrubbed = _scrub_avatar_urls(decoded)
        rederived = recompute_chunk_prefix(scrubbed)
        encoded = rederived.encode("utf-8")
        return encoded, encoded != body
    if isinstance(body, str):
        scrubbed = _scrub_avatar_urls(body)
        rederived = recompute_chunk_prefix(scrubbed)
        return rederived, rederived != body
    # ``body`` is something exotic (None, dict). Leave it alone.
    return body, False


def _rescrub_cassette(path: Path) -> tuple[bool, int]:
    """Re-scrub a single cassette file. Returns ``(changed, byte_diff)``.

    ``byte_diff`` is ``len(new_bytes) - len(old_bytes)`` — useful for the
    per-file diff stat the script prints at the end of a run.
    """
    raw = path.read_text(encoding="utf-8")
    data = yaml.load(raw, Loader=Loader)
    if not isinstance(data, dict):
        return False, 0

    body_changed = False
    for interaction in data.get("interactions") or []:
        body_container = (interaction.get("response") or {}).get("body") or {}
        if "string" not in body_container:
            continue
        new_body, changed = _rescrub_body(body_container["string"])
        if changed:
            body_container["string"] = new_body
            body_changed = True

    if not body_changed:
        return False, 0

    dumped = yaml.dump(data, Dumper=Dumper)
    # ``yaml.dump`` with the default ``Dumper`` returns ``str``; vcrpy's
    # serializer behaves the same way. Write back as UTF-8 — the cassettes
    # routinely carry emoji in artifact / notebook titles and the platform
    # encoding (cp1252 on Windows) cannot encode them.
    path.write_text(dumped, encoding="utf-8")
    return True, len(dumped.encode("utf-8")) - len(raw.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk re-scrub VCR cassettes for lh3.googleusercontent.com/(a|ogw) "
            "avatar URLs. Re-derives chunk byte-counts in the same pass. "
            "Idempotent."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "Cassette file(s) to re-scrub. If omitted, walks "
            "tests/cassettes/*.yaml from the repo root."
        ),
    )
    args = parser.parse_args(argv)

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        if not _CASSETTE_DIR.exists():
            print(f"No cassette directory at {_CASSETTE_DIR}", file=sys.stderr)
            return 0
        targets = sorted(_CASSETTE_DIR.glob("*.yaml"))

    if not targets:
        print("No cassettes to re-scrub.")
        return 0

    total_changed = 0
    total_byte_diff = 0
    failed: list[tuple[Path, str]] = []

    for path in targets:
        try:
            changed, byte_diff = _rescrub_cassette(path)
        except (yaml.YAMLError, OSError) as exc:
            failed.append((path, str(exc)))
            print(f"FAIL  {path.name}: {exc}", file=sys.stderr)
            continue
        if changed:
            total_changed += 1
            total_byte_diff += byte_diff
            sign = "+" if byte_diff >= 0 else ""
            print(f"scrubbed  {path.name}  ({sign}{byte_diff} bytes)")
        else:
            print(f"clean     {path.name}")

    print(
        f"\nSummary: {total_changed}/{len(targets)} cassettes re-scrubbed, "
        f"net byte diff {total_byte_diff:+d}."
    )
    if failed:
        print(f"FAILURES: {len(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
