"""One-time: retarget recorded request hosts onto the post-rebrand host (#2067).

#2067 moved the default base host from ``notebooklm.google.com`` to
``notebook.google.com``. The VCR cassettes were recorded against the old
default and match on host, so every integration test would replay-miss.

**Why rewrite rather than re-record.** Google dual-serves ``batchexecute`` on
both hosts (ADR-0028), so a re-recording would differ from these fixtures only
in the host field -- at the cost of hundreds of fresh network round-trips,
fresh scrubbing, and a diff no reviewer could check. Rewriting the one field
that actually changed keeps the diff readable and the evidence intact.

**What it does not touch.** Only the *request* side is rewritten: ``uri`` and
the ``host`` / ``origin`` / ``referer`` request headers. Response bodies keep
every ``notebooklm.google.com`` they were recorded with -- those bytes are
Google's own HTML and rewriting them would fabricate content Google never sent.
Leaving them is not sloppiness; it is what dual-serving genuinely looks like on
the wire. The script hashes every response block before and after and refuses
to write if one changed.

**Two interaction-scoped skips**, both preserving real recorded behaviour:

* **3xx interactions** -- these record the legacy host redirecting *to* the
  rebrand host. Rewriting the request would fabricate a self-redirect.
* **Upload interactions carrying ``upload_id=``** -- these endpoints are
  server-directed: the client reads ``X-Goog-Upload-URL`` out of the *response*
  and derives its follow-up URL and ``Origin`` from it
  (``_upload_decode.py``, ``upload_payloads.py``). They stay on whatever host
  the recorded response named, so the uri/host/origin/referer of such an
  interaction must move together or not at all -- here, not at all.

Idempotent and re-runnable. Parses with PyYAML to *decide*, splices raw lines to
*apply*: a ``safe_dump`` round-trip would reflow the whole corpus into an
unreviewable diff.

Usage::

    python scripts/_onetime_retarget_cassette_hosts.py [--apply]

Defaults to a dry run that reports per-category and per-skip-reason counts.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Deliberately non-recursive. The recursive glob pulls in
# ``gzip_coverage/artifacts_revise_slide_gzipped.yaml``, whose test hardcodes
# the legacy URL and matches on host -- cassette and test are a coupled,
# self-consistent pair. Rewriting one without the other breaks it.
CASSETTE_GLOB = "tests/cassettes/*.yaml"

LEGACY_HOST = "notebooklm.google.com"
REBRAND_HOST = "notebook.google.com"

# Request headers whose value names the host the client chose.
HOST_HEADERS = frozenset({"host", "origin", "referer"})


def _response_digest(doc: Any) -> str:
    """Return a digest over every ``response`` block in a parsed cassette."""
    hasher = hashlib.sha256()
    for interaction in (doc or {}).get("interactions") or []:
        hasher.update(repr(interaction.get("response")).encode("utf-8"))
    return hasher.hexdigest()


def _is_redirect(interaction: dict[str, Any]) -> bool:
    status = (interaction.get("response") or {}).get("status") or {}
    code = status.get("code") if isinstance(status, dict) else None
    return isinstance(code, int) and 300 <= code < 400


def _is_server_directed_upload(interaction: dict[str, Any]) -> bool:
    """True when the endpoint was handed to the client by a prior response."""
    return "upload_id=" in str((interaction.get("request") or {}).get("uri") or "")


def _line_span(lines: list[str], start: int) -> int:
    """Return the index one past the ``- request:`` block beginning at ``start``."""
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("- ") or (
            lines[index].strip() and not lines[index].startswith(" ")
        ):
            return index
    return len(lines)


def _rewrite_request_lines(lines: list[str]) -> tuple[list[str], Counter]:
    """Rewrite host-bearing request lines in one ``- request:`` block."""
    counts: Counter = Counter()
    out: list[str] = []
    header_key: str | None = None
    in_response = False

    for line in lines:
        # Track which half of the interaction we are in. ``response:`` sits at
        # the same indent as ``request:`` inside an interaction.
        if re.match(r"^\s{2}response:\s*$", line):
            in_response = True
        elif re.match(r"^\s{2}request:\s*$", line):
            in_response = False

        # Track the enclosing key *before* any early exit: header values are
        # YAML sequence items on their own lines ("host:" then
        # "- notebooklm.google.com"), so the line that names the key never
        # contains the host itself.
        key_match = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*)$", line)
        if key_match:
            header_key = key_match.group(1).lower()

        if in_response or LEGACY_HOST not in line:
            out.append(line)
            continue

        if key_match and key_match.group(1).lower() == "uri":
            out.append(line.replace(LEGACY_HOST, REBRAND_HOST))
            counts["uri"] += 1
        elif header_key in HOST_HEADERS:
            # Header values are YAML sequence items on their own lines.
            out.append(line.replace(LEGACY_HOST, REBRAND_HOST))
            counts[header_key] += 1
        else:
            out.append(line)

    return out, counts


def process(path: Path, apply: bool) -> Counter:
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    before_digest = _response_digest(doc)

    interactions = (doc or {}).get("interactions") or []
    skip_flags = [
        "redirect"
        if _is_redirect(i)
        else ("server_directed_upload" if _is_server_directed_upload(i) else None)
        for i in interactions
    ]

    lines = raw.splitlines(keepends=True)
    starts = [n for n, line in enumerate(lines) if line.startswith("- request:")]
    if len(starts) != len(interactions):
        raise AssertionError(
            f"{path}: parsed {len(interactions)} interactions but found "
            f"{len(starts)} '- request:' anchors; refusing to splice"
        )

    counts: Counter = Counter()
    out: list[str] = lines[: starts[0]] if starts else lines
    for index, start in enumerate(starts):
        end = _line_span(lines, start)
        block = lines[start:end]
        reason = skip_flags[index]
        if reason:
            counts[f"skipped_{reason}"] += 1
            out.extend(block)
        else:
            rewritten, block_counts = _rewrite_request_lines(block)
            counts.update(block_counts)
            out.extend(rewritten)

    # Everything after the final interaction -- notably the top-level
    # ``version: 1`` key, which VCR requires and which sits outside every
    # ``- request:`` span. Dropping it silently corrupts the cassette in a way
    # the response digest cannot see, because it is not a response block.
    if starts:
        out.extend(lines[_line_span(lines, starts[-1]) :])

    new_raw = "".join(out)
    if new_raw != raw:
        counts["files_changed"] += 1
        after = yaml.safe_load(new_raw)
        if _response_digest(after) != before_digest:
            raise AssertionError(f"{path}: response bytes changed -- refusing to write")
        if set((after or {}).keys()) != set((doc or {}).keys()):
            raise AssertionError(
                f"{path}: top-level keys changed "
                f"{sorted((doc or {}).keys())} -> {sorted((after or {}).keys())}"
            )
        if len((after or {}).get("interactions") or []) != len(interactions):
            raise AssertionError(f"{path}: interaction count changed -- refusing to write")
        if apply:
            path.write_text(new_raw, encoding="utf-8")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    paths = sorted(PROJECT_ROOT.glob(CASSETTE_GLOB))
    totals: Counter = Counter()
    for path in paths:
        totals.update(process(path, args.apply))

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"[{mode}] cassettes scanned: {len(paths)}")
    for key in sorted(totals):
        print(f"  {key}: {totals[key]}")
    rewritten = sum(v for k, v in totals.items() if not k.startswith(("skipped_", "files_")))
    print(f"  total lines rewritten: {rewritten}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
