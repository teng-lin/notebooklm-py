#!/usr/bin/env python3
"""Capture NotebookLM's live RPC id registry from the web bundle and diff it
against ``src/notebooklm/rpc/types.py``.

NotebookLM declares every ``batchexecute`` RPC in its (public, gstatic-served) JS
bundle as::

    _.uD("<rpc_id>", <ReqCtor>, <RespCtor>, [<flags>, "/<Service>.<Method>"])

The obfuscated ``<rpc_id>`` values are this project's #1 breakage class — they
rotate without notice and a stale id silently breaks the affected operation. This
script extracts the live ``id -> /Service.Method`` map and diffs it against the
ids we hardcode, surfacing four classes:

* CONFIRMED       — our id is still registered (shown with its decoded method name)
* ABSENT          — our id no longer appears in the bundle at all (rotation/stale — the alarm)
* PRESENT-UNPARSED— our id string is in the bundle but its registration form wasn't
                    parsed (not a rotation; a parser gap to widen, not an alert)
* UNMAPPED        — a live RPC the bundle declares that we don't expose, grouped by
                    service family: **current** (old `LabsTailwind*` consumer backend
                    — callable on our cohort now, just unexposed), **enterprise** (the
                    Discovery-Engine domain services — the NotebookLM Enterprise /
                    Agentspace surface on `discoveryengine.googleapis.com`, behind a
                    server-side VPC Service Controls perimeter; not consumer-callable,
                    not a consumer migration target), or **other**

Auth: discovering the bundle URL needs **one authenticated homepage read** (an
unauthenticated request only returns the login app); fetching the bundle itself is
unauthenticated (public CDN). Run ``notebooklm login`` first, or pass
``--bundle-file`` to analyse a pre-saved bundle offline (no auth/network).

Cohort note: the bundle is shared between the consumer NotebookLM app and the
enterprise (Agentspace / Vertex AI Search) surface, so it registers BOTH RPC
generations. The Discovery-Engine ids (e.g. ``AzXHBd``/``NotebookService.*``) are
the *enterprise* surface — gated off for consumer accounts by a server-side VPC
Service Controls perimeter (live-probed 2026-06-16: grpc 7 ``VPC_SERVICE_CONTROLS``
/ ``CONSUMER_INVALID`` on ``discoveryengine.googleapis.com``), not a consumer
cohort that is "about to migrate".

Usage::

    python scripts/capture_rpc_registry.py                 # human-readable diff
    python scripts/capture_rpc_registry.py --json          # machine-readable snapshot
    python scripts/capture_rpc_registry.py --check         # exit 1 if any of our ids are ABSENT
    python scripts/capture_rpc_registry.py --bundle-file bundle.js   # offline, no auth
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The NotebookLM web app's gstatic JS namespace. If Google renames the app this
# pattern must be updated (the script will then report "no bundle URL").
_APP = "boq-labs-tailwind"
_BUNDLE_URL_RE = re.compile(rf'https://www\.gstatic\.com/_/mss/{_APP}/_/js/[^"\\\s<>]+')

# A registration's two stable, quoted anchors: the ``/Service.Method`` path and
# the rpc id. We anchor on the path and scan *backward* for the nearest id, which
# is robust to nested ``[...]`` in the options array (a single forward regex
# spanning to the path breaks on the inner ``]``). Quote-agnostic (``"`` or
# ``'``) so a change in the bundle minifier's quote style doesn't blank the diff.
_METHOD_PATH_RE = re.compile(r"""["'](/[A-Za-z][\w]*\.[A-Za-z][\w]*)["']""")
_ID_TOKEN_RE = re.compile(r"""["']([A-Za-z0-9]{5,8})["']""")
# How far back from a path string to scan for its registration id. The
# ``_.uD(id, ReqCtor, RespCtor, [flags, path])`` form fits well within ~100 chars;
# 160 leaves headroom for longer minified constructor names.
_ID_LOOKBACK = 160

# Real obfuscated rpc ids are short alphanumerics; this filter keeps non-id enum
# constants (e.g. ``blog_post``) out of the diff.
_RPC_ID_RE = re.compile(r"[A-Za-z0-9]{5,8}")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138 Safari/537.36"

# Resolved relative to this file (scripts/ -> repo root) so the script runs from
# any working directory, not just the repo root.
_DEFAULT_TYPES = Path(__file__).resolve().parent.parent / "src" / "notebooklm" / "rpc" / "types.py"

# --- Service-family classification: consumer backend vs enterprise (Discovery Engine) ---
# "Current" (the consumer backend serving our cohort now) is detected *empirically*:
# any service one of our CONFIRMED ids resolves to is, by definition, working for us.
# Past that, the known Discovery-Engine domain services are tagged "enterprise" — they
# are the NotebookLM Enterprise / Agentspace surface (discoveryengine.googleapis.com),
# gated off for consumer accounts by a server-side VPC Service Controls perimeter, NOT a
# pre-migration consumer cohort. The old NotebookLM family shares the ``LabsTailwind``
# prefix (same consumer backend, callable on our cohort even where we don't expose it);
# anything else is "other" — itself a useful drift signal (a new, unclassified service).
_DISCOVERY_ENGINE_SERVICES = frozenset(
    {
        "NotebookService",
        "SourceService",
        "NoteService",
        "ArtifactService",
        "AudioOverviewService",
        "AccountService",
    }
)


def _service_of(method_path: str) -> str:
    """``/LabsTailwindOrchestrationService.AddSources`` -> ``LabsTailwindOrchestrationService``."""
    return method_path.lstrip("/").split(".", 1)[0]


def classify_service(service: str, current_services: set[str]) -> str:
    """Tag a service ``current`` / ``enterprise`` / ``other``.

    ``current`` = the consumer backend, works on our cohort today; ``enterprise`` =
    a Discovery-Engine domain service — the NotebookLM Enterprise / Agentspace
    surface, gated off for consumer accounts by a VPC Service Controls perimeter
    (NOT a consumer migration target); ``other`` = unclassified (investigate —
    possibly a new service). Empirical first (a service our CONFIRMED ids use is
    ``current``), then the known Discovery-Engine domain services, then the old
    ``LabsTailwind*`` consumer family.
    """
    if service in current_services:
        return "current"
    if service in _DISCOVERY_ENGINE_SERVICES:
        return "enterprise"
    if service.startswith("LabsTailwind"):
        return "current"
    return "other"


def parse_ids_from_text(types_text: str) -> dict[str, str]:
    """Return ``{rpc_id: ENUM_NAME}`` for the ``RPCMethod`` enum members."""
    match = re.search(r"class RPCMethod\b.*?(?=\nclass |\Z)", types_text, re.DOTALL)
    body = match.group(0) if match else types_text
    out: dict[str, str] = {}
    for name, value in re.findall(
        r"""^\s+([A-Z][A-Z0-9_]*)\s*=\s*["']([^"']+)["']""", body, re.MULTILINE
    ):
        if _RPC_ID_RE.fullmatch(value):
            out[value] = name
    return out


def extract_registry(bundle: str) -> dict[str, str]:
    """Return ``{rpc_id: /Service.Method}`` for every registration in the bundle.

    Anchored on each ``"/Service.Method"`` path: the rpc id is the nearest
    preceding quoted short token (the registration's first argument). Scanning
    backward from the path tolerates nested brackets in the options array that a
    single forward regex cannot span.
    """
    out: dict[str, str] = {}
    for match in _METHOD_PATH_RE.finditer(bundle):
        window = bundle[max(0, match.start() - _ID_LOOKBACK) : match.start()]
        ids = _ID_TOKEN_RE.findall(window)
        if ids:
            out[ids[-1]] = match.group(1)
    return out


def diff(ours: dict[str, str], live: dict[str, str], bundle: str) -> dict[str, dict[str, str]]:
    """Classify our ids vs the live registry into the four reporting buckets."""

    def _in_bundle(rpc_id: str) -> bool:
        return f'"{rpc_id}"' in bundle or f"'{rpc_id}'" in bundle

    confirmed = {i: live[i] for i in ours if i in live}
    present_unparsed = {i: ours[i] for i in ours if i not in live and _in_bundle(i)}
    absent = {i: ours[i] for i in ours if i not in live and not _in_bundle(i)}
    unmapped = {i: live[i] for i in live if i not in ours}
    return {
        "confirmed": confirmed,
        "present_unparsed": present_unparsed,
        "absent": absent,
        "unmapped": unmapped,
    }


def fetch_bundle() -> str:
    """Fetch and concatenate the gstatic app-bundle chunks (which carry the registry).

    One authenticated homepage read discovers the bundle URLs; the chunks are then
    fetched unauthenticated from the public CDN, **sequentially** (to avoid rate
    limiting) and **concatenated**, so the scan covers the whole frontend surface
    regardless of how Google splits the registry across chunks.
    """
    import httpx

    from notebooklm._env import get_base_url
    from notebooklm.auth import authuser_query, load_auth_from_storage

    def _fetch(
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        follow_redirects: bool = False,
        timeout: float = 60.0,
    ) -> httpx.Response:
        response = httpx.get(
            url,
            headers={"User-Agent": _UA},
            cookies=cookies,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    cookies = load_auth_from_storage()
    html = _fetch(
        f"{get_base_url()}/?{authuser_query(0)}",
        cookies=cookies,
        follow_redirects=True,
        timeout=30.0,
    ).text
    urls = sorted(set(_BUNDLE_URL_RE.findall(html)))
    if not urls:
        raise SystemExit(
            f"No {_APP} bundle URL found in the homepage — not authenticated for "
            "NotebookLM? Run `notebooklm login` (or pass --bundle-file)."
        )
    # Keep only genuine JS responses: raise_for_status rejects non-200, and this
    # rejects a 200 served with the wrong content-type (e.g. an HTML login/error
    # page), which would otherwise be parsed as a bundle and make every id ABSENT.
    bodies: list[str] = []
    for url in urls:
        response = _fetch(url)
        content_type = response.headers.get("content-type", "")
        if "javascript" in content_type or "text/plain" in content_type:
            bodies.append(response.text)
    if not bodies:
        raise SystemExit(f"No readable JS bundle content fetched from the {_APP} URLs.")
    return "\n".join(bodies)


def _print_report(
    ours: dict[str, str],
    live: dict[str, str],
    buckets: dict[str, dict[str, str]],
    current_services: set[str],
) -> None:
    """Print the human-readable diff (counts + per-bucket id listings) to stdout.

    ``current_services`` is the empirically-derived set of services our CONFIRMED
    ids resolve to; it drives the UNMAPPED service-family grouping
    (``current`` / ``enterprise`` / ``other``) via :func:`classify_service`.
    """
    confirmed, present, absent, unmapped = (
        buckets["confirmed"],
        buckets["present_unparsed"],
        buckets["absent"],
        buckets["unmapped"],
    )
    print(f"our ids: {len(ours)} | live registrations parsed: {len(live)}")
    print(
        f"CONFIRMED: {len(confirmed)}  ABSENT: {len(absent)}  "
        f"PRESENT-UNPARSED: {len(present)}  UNMAPPED: {len(unmapped)}\n"
    )
    print("CONFIRMED (our id -> live /Service.Method):")
    for rpc_id in sorted(confirmed, key=lambda i: ours[i]):
        print(f"  {rpc_id:<8} {ours[rpc_id]:<26} {confirmed[rpc_id]}")
    if absent:
        print("\nABSENT — id no longer in the bundle (rotation/stale; investigate):")
        for rpc_id in sorted(absent, key=lambda i: absent[i]):
            print(f"  {rpc_id:<8} {absent[rpc_id]}")
    if present:
        print("\nPRESENT-UNPARSED — id is in the bundle but registration not parsed (widen regex):")
        for rpc_id in sorted(present, key=lambda i: present[i]):
            print(f"  {rpc_id:<8} {present[rpc_id]}")
    # Group the unexposed RPCs by service family so "callable on our cohort now"
    # (current) is visually separated from the gated Discovery-Engine surface.
    fam_groups: dict[str, list[tuple[str, str]]] = {
        "current": [],
        "enterprise": [],
        "other": [],
    }
    for rpc_id, method in unmapped.items():
        fam_groups[classify_service(_service_of(method), current_services)].append((rpc_id, method))
    fam_labels = {
        "current": "UNMAPPED · consumer backend — callable on our cohort now, just not exposed",
        "enterprise": (
            "UNMAPPED · enterprise (Discovery Engine / Agentspace) — VPC-SC-gated, "
            "not consumer-callable, not a migration target"
        ),
        "other": "UNMAPPED · other / unclassified services (investigate)",
    }
    print(f"\nUNMAPPED — live RPCs we do not expose ({len(unmapped)}), by service family:")
    for fam in ("current", "enterprise", "other"):
        items = fam_groups[fam]
        if not items:
            continue
        print(f"\n  {fam_labels[fam]} ({len(items)}):")
        for rpc_id, method in sorted(items, key=lambda x: x[1]):
            print(f"    {rpc_id:<8} {method}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: load/fetch the bundle, diff vs rpc/types.py, report.

    Returns the process exit code: ``1`` when ``--check`` is set and any of our
    ids are ABSENT (a rotation/stale alarm), else ``0``.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--json", action="store_true", help="emit a JSON snapshot instead of a report"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if any of our ids are ABSENT")
    parser.add_argument(
        "--bundle-file", type=Path, help="analyse a saved bundle file (no auth/network)"
    )
    parser.add_argument("--types", type=Path, default=_DEFAULT_TYPES, help="path to rpc/types.py")
    args = parser.parse_args(argv)

    ours = parse_ids_from_text(args.types.read_text(encoding="utf-8"))
    bundle = args.bundle_file.read_text(encoding="utf-8") if args.bundle_file else fetch_bundle()
    live = extract_registry(bundle)
    buckets = diff(ours, live, bundle)
    # Services any CONFIRMED id resolves to are, empirically, serving our cohort.
    current_services = {_service_of(m) for m in buckets["confirmed"].values()}

    if args.json:
        print(
            json.dumps(
                {
                    "confirmed": {
                        i: {"name": ours[i], "method": m} for i, m in buckets["confirmed"].items()
                    },
                    "absent": buckets["absent"],
                    "present_unparsed": buckets["present_unparsed"],
                    "unmapped": {
                        i: {
                            "method": m,
                            "family": classify_service(_service_of(m), current_services),
                        }
                        for i, m in buckets["unmapped"].items()
                    },
                    "counts": {k: len(v) for k, v in buckets.items()} | {"ours": len(ours)},
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_report(ours, live, buckets, current_services)

    if args.check and buckets["absent"]:
        print(
            f"\nFAIL: {len(buckets['absent'])} of our RPC ids are no longer registered.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
