"""Unit tests for ``scripts/rescrub-cassettes.py``.

These tests build synthetic "bad" cassettes that contain the exact leak
shapes the script is meant to clean — an ``/ogw/`` avatar URL plus a
chunked WRB response whose declared byte counts are correct for the
pre-scrub payload — then run the script in a tmp_path and assert:

* The output no longer contains the raw avatar URL (the canonical
  ``SCRUBBED_AVATAR_URL`` placeholder appears in its place).
* The chunked ``<count>\\n<payload>\\n`` byte-count prefixes were
  re-derived to match the post-scrub payload byte length — this is the
  ``recompute_chunk_prefix`` pass the script promises to run after
  every string substitution.
* The script is idempotent: a second run on the already-cleaned tree
  reports zero changes and produces a byte-identical file.

One additional regression test asserts the avatar-URL regex the script
hard-codes mirrors the canonical pattern in
``tests/cassette_patterns.py``. The script deliberately does NOT call
``scrub_string`` (see its module docstring), so the registry/script
drift could go unnoticed if the canonical pattern were ever tightened
or widened — this test pins them together.

The script is invoked via :func:`runpy.run_path` rather than imported
in-process — there's a hyphen in its filename (``rescrub-cassettes.py``)
so it isn't a valid Python module name. ``runpy`` handles that cleanly
and the script's exit code surfaces as ``SystemExit`` from its
``__main__`` block.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "rescrub-cassettes.py"
_TESTS_DIR = _REPO_ROOT / "tests"

# Ensure ``cassette_patterns`` resolves regardless of where pytest is run.
# This is the same trick the script uses.
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


def _run_script(*argv: str) -> int:
    """Invoke ``scripts/rescrub-cassettes.py`` as ``__main__``.

    The script is not importable as a module (hyphen in its filename), so
    we use :func:`runpy.run_path` and trap its ``SystemExit`` to obtain
    the exit code. We restore ``sys.argv`` afterwards so test isolation
    holds even if the script aborts mid-run.
    """
    saved_argv = sys.argv[:]
    sys.argv = [str(_SCRIPT), *argv]
    try:
        try:
            runpy.run_path(str(_SCRIPT), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            return int(code) if isinstance(code, int) else 0
        return 0
    finally:
        sys.argv = saved_argv


def _build_bad_cassette(path: Path) -> None:
    """Write a synthetic cassette carrying both leak shapes the script fixes.

    Layout:

    * Interaction 0 — HTML body containing two ``/ogw/`` avatar URLs.
      After scrubbing each becomes ``SCRUBBED_AVATAR_URL``; the body
      shrinks by exactly the captured-token length per occurrence.
    * Interaction 1 — chunked XSSI batchexecute response. The first chunk
      embeds an avatar URL inside a stringified WRB payload (the same
      shape the display-name scrubber was designed to catch). The byte-count prefix is set
      to the PRE-scrub payload length, so a script that only substitutes
      but doesn't re-derive counts will leave a stale prefix and the
      shape-lint byte-count assertion would later fail.
    """
    avatar_url = (
        "https://lh3.googleusercontent.com/ogw/"
        "AF2bZyi16LQ_0jOcB_3NwTmyCfSFpN74FaCfwF0mWwtxF--cwSQ=s32-c-mo"
    )
    html_body = f'<html><body><img src="{avatar_url}"><img src="{avatar_url}"></body></html>'
    # Chunked WRB body. The payload references the avatar URL inside the
    # third-position of a wrb.fr envelope so scrub_string collapses it to
    # SCRUBBED_AVATAR_URL and the byte count needs to drop accordingly.
    chunk_payload = (
        f'[["wrb.fr","JFMDGd","[[null,[\\"{avatar_url}\\"]]]",null,null,null,"generic"]]'
    )
    chunk_count = len(chunk_payload.encode("utf-8"))
    chunked_body = f")]}}'\n\n{chunk_count}\n{chunk_payload}\n"

    cassette = {
        "interactions": [
            {
                "request": {
                    "body": "",
                    "headers": {"Host": ["notebooklm.google.com"]},
                    "method": "GET",
                    "uri": "https://notebooklm.google.com/",
                },
                "response": {
                    "body": {"string": html_body},
                    "headers": {"Content-Type": ["text/html"]},
                    "status": {"code": 200, "message": "OK"},
                },
            },
            {
                "request": {
                    "body": "f.req=%5B%5D",
                    "headers": {"Host": ["notebooklm.google.com"]},
                    "method": "POST",
                    "uri": "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?rpcids=JFMDGd",
                },
                "response": {
                    "body": {"string": chunked_body},
                    "headers": {"Content-Type": ["application/json+protobuf"]},
                    "status": {"code": 200, "message": "OK"},
                },
            },
        ],
        "version": 1,
    }
    path.write_text(yaml.dump(cassette), encoding="utf-8")


def test_script_scrubs_ogw_avatar_and_recomputes_byte_counts(tmp_path: Path) -> None:
    """End-to-end: bad cassette in, clean cassette out (avatar + byte counts)."""
    cassette_path = tmp_path / "bad_avatar.yaml"
    _build_bad_cassette(cassette_path)

    pre = cassette_path.read_text(encoding="utf-8")
    assert "/ogw/" in pre, "fixture itself must contain the leak"

    exit_code = _run_script(str(cassette_path))
    assert exit_code == 0

    post = cassette_path.read_text(encoding="utf-8")
    # The avatar URLs are gone; the placeholder is in their place.
    assert "/ogw/" not in post, f"leak survived rescrub:\n{post}"
    assert "SCRUBBED_AVATAR_URL" in post, f"placeholder not emitted:\n{post}"

    # Byte counts are correct on the chunked interaction. We re-parse and
    # validate every digit-only header against the next-line payload, the
    # same way ``test_cassette_shapes._byte_count_failures`` does.
    data = yaml.safe_load(post)
    chunked = data["interactions"][1]["response"]["body"]["string"]
    assert chunked.startswith(")]}'\n\n"), "chunked framing must survive scrub"
    lines = chunked[len(")]}'\n\n") :].split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        try:
            declared = int(line)
        except ValueError:
            i += 1
            continue
        i += 1
        if i >= len(lines):
            break
        actual = len(lines[i].encode("utf-8"))
        assert declared == actual, (
            f"chunk@line{i}: prefix declares {declared} bytes but payload is {actual} bytes"
        )
        i += 1


def test_script_is_idempotent(tmp_path: Path) -> None:
    """Running the script a second time on a clean tree is a no-op."""
    cassette_path = tmp_path / "bad_avatar.yaml"
    _build_bad_cassette(cassette_path)

    # First pass — cleans the file.
    assert _run_script(str(cassette_path)) == 0
    after_first = cassette_path.read_text(encoding="utf-8")

    # Second pass — file must be byte-identical to the first-pass output.
    assert _run_script(str(cassette_path)) == 0
    after_second = cassette_path.read_text(encoding="utf-8")

    assert after_first == after_second, (
        "Re-running the script changed already-clean output — non-idempotent."
    )


def test_script_skips_files_without_leaks(tmp_path: Path) -> None:
    """A cassette with no scrub targets must be left byte-identical."""
    cassette = {
        "interactions": [
            {
                "request": {
                    "body": "",
                    "headers": {"Host": ["notebooklm.google.com"]},
                    "method": "GET",
                    "uri": "https://notebooklm.google.com/",
                },
                "response": {
                    "body": {"string": "<html><body>hello</body></html>"},
                    "headers": {"Content-Type": ["text/html"]},
                    "status": {"code": 200, "message": "OK"},
                },
            }
        ],
        "version": 1,
    }
    cassette_path = tmp_path / "clean.yaml"
    cassette_path.write_text(yaml.dump(cassette), encoding="utf-8")
    pre = cassette_path.read_text(encoding="utf-8")

    assert _run_script(str(cassette_path)) == 0

    assert cassette_path.read_text(encoding="utf-8") == pre, (
        "Script touched a cassette with no leaks."
    )


@pytest.mark.parametrize("name", ["empty.yaml"])
def test_script_handles_empty_cassette(tmp_path: Path, name: str) -> None:
    """An empty / non-dict cassette must not crash the script."""
    cassette_path = tmp_path / name
    cassette_path.write_text("", encoding="utf-8")
    assert _run_script(str(cassette_path)) == 0


def test_avatar_pattern_matches_registry() -> None:
    """The script's avatar-URL regex must mirror cassette_patterns'.

    ``scripts/rescrub-cassettes.py`` deliberately copies the avatar-URL
    pattern out of :data:`cassette_patterns.SENSITIVE_PATTERNS` (section
    13) rather than calling ``scrub_string`` — see the script's module
    docstring for why. This test pins the two together so a future
    tightening of the canonical pattern (e.g. adding a path segment, or
    swapping ``=`` for a different sizing-suffix delimiter) doesn't
    silently leave the script's regex stale.

    The check is structural: we walk the canonical
    ``SENSITIVE_PATTERNS`` list, locate the entry whose replacement is
    ``SCRUBBED_AVATAR_URL``, and assert it equals the regex the script
    exposes. Both sides are plain ``(regex, replacement)`` pairs so the
    test fails loudly if either drifts.
    """
    # Importing the script via runpy.run_path would execute its
    # ``main(argv=sys.argv[1:])`` block; we only want its module globals.
    # Load it with run_path and ``run_name`` set to something other than
    # ``__main__`` so the ``if __name__ == "__main__":`` guard skips.
    script_ns = runpy.run_path(str(_SCRIPT), run_name="not_main")
    script_pattern = script_ns["_AVATAR_URL_RE"].pattern
    script_replacement = script_ns["_AVATAR_URL_REPLACEMENT"]

    from cassette_patterns import SENSITIVE_PATTERNS

    avatar_entries = [
        (pat, repl) for pat, repl in SENSITIVE_PATTERNS if repl == "SCRUBBED_AVATAR_URL"
    ]
    assert len(avatar_entries) == 1, (
        f"Expected exactly one SCRUBBED_AVATAR_URL entry in registry, got {len(avatar_entries)}"
    )
    registry_pattern, registry_replacement = avatar_entries[0]
    assert script_pattern == registry_pattern, (
        "scripts/rescrub-cassettes.py avatar regex drifted from "
        f"cassette_patterns.SENSITIVE_PATTERNS: script={script_pattern!r} "
        f"vs registry={registry_pattern!r}"
    )
    assert script_replacement == registry_replacement
