#!/usr/bin/env python3
"""Capture NotebookLM's live RPC id registry from the web bundle and diff it
against ``src/notebooklm/rpc/types.py``.

NotebookLM declares every ``batchexecute`` RPC in its (public, gstatic-served) JS
bundle as::

    _.fD("<rpc_id>", <ReqCtor>, <RespCtor>, [<flags>, "/<Service>.<Method>"])

(The registration helper is currently minified to ``_.fD``; it was ``_.uD`` in an
earlier bundle. The scraper does **not** depend on the helper name — it anchors on
the quoted ``"/<Service>.<Method>"`` path — so a future rename of this helper does
not blank the diff.)

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

Beyond the rpc-id registry, the same bundle carries the studio-feature **enum
maps** (``switch(code){case N:return "Label"}`` blocks for VideoFormat /
AudioFormat / app-variants), the ``Yp`` **quota-code** map (a feature-rollout
early-warning surface), and proto **required-field assertions** (schema-shape
drift). ``--check-enums`` extracts and diffs the switch enums against the int
enums in ``rpc/types.py`` with the same four-class spirit as the id diff, but a
distinct taxonomy — see :func:`diff_enums` for why ``NEW`` is report-only.

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
    python scripts/capture_rpc_registry.py --check-enums    # exit 1 on CHANGED/STALE studio enums
    python scripts/capture_rpc_registry.py --check --check-enums  # both gates (combine freely)
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


# ---------------------------------------------------------------------------
# Studio enum drift (switch(code){case N:return "Label"} maps)
# ---------------------------------------------------------------------------
#
# Beyond the rpc-id registry the bundle inlines the studio-feature enums as
# minified ``switch`` statements mapping an integer code to a display label,
# e.g. ``switch(a){case 1:return"Explainer";case 3:return"Cinematic";...}``.
# These are the human-facing labels for VideoFormat / AudioFormat / the
# app-variant picker — the same integers we hardcode in ``rpc/types.py`` and
# send on the wire. If Google ever renumbers a *selectable* format (the #1597
# alarm: the VideoStyle/format code an existing label maps to changes) every
# generate-* call silently produces the wrong artifact, so we diff them.

# A switch block: ``switch(<scrutinee>){ <case 1:return"X";case 2:return"Y";> }``.
# ``<scrutinee>`` is a short minified expr (kept <=30 chars so we don't span a
# huge unrelated ``switch``); the body is one-or-more ``case N:return"Label"``.
# Whitespace-tolerant so a minor minifier/pretty-printer change (spaces after
# ``return``, around ``:``, between arms) doesn't yield a false UNPARSED.
_SWITCH_BLOCK_RE = re.compile(
    r'switch\([^)]{1,30}\)\{\s*((?:case\s+\d+\s*:\s*return\s*"[^"]*"\s*;?\s*)+)'
)
# A single ``case N:return "Label"`` arm inside a matched block.
_SWITCH_CASE_RE = re.compile(r'case\s+(\d+)\s*:\s*return\s*"([^"]*)"')

# Label-anchoring registry: a recognizable *subset* of labels identifies which
# of our enums a switch block is. A block whose label set is a SUPERSET of an
# anchor set is attributed to that enum (so a block that gained an unreleased
# label still matches). Anchors are deliberately a handful of stable,
# distinctive labels — not the full set — so a NEW label never breaks
# attribution. The keys are our ``rpc/types.py`` enum class names.
_ENUM_LABEL_ANCHORS: dict[str, frozenset[str]] = {
    "VideoFormat": frozenset({"Explainer", "Cinematic"}),
    "AudioFormat": frozenset({"Deep Dive", "Critique", "Debate"}),
}


def _normalize_label(label: str) -> str:
    """``"Deep Dive"`` -> ``"DEEP_DIVE"`` so a bundle label can be matched to an
    enum *member* name (our members are ``UPPER_SNAKE``, the bundle labels are
    ``Title Case`` with spaces). Used to pair a live ``code -> label`` with our
    ``MEMBER_NAME -> value`` mapping when diffing.
    """
    return re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")


def extract_switch_enums(bundle: str) -> dict[str, dict[int, str]]:
    """Return ``{our_enum_name: {code: label}}`` for every switch block that a
    label anchor attributes to one of our enums.

    Each ``switch(code){case N:return "Label"}`` block is parsed into a
    ``{code -> label}`` map, then attributed to one of our enums by
    *label-anchoring*: if the block's label set is a superset of an anchor set
    in :data:`_ENUM_LABEL_ANCHORS`, it is that enum. Unattributed blocks (the
    bundle has many switches we don't care about) are dropped. If two blocks
    attribute to the same enum their maps are merged (later wins), which is
    harmless because the anchor guarantees they are the same logical enum.
    """
    out: dict[str, dict[int, str]] = {}
    for block_match in _SWITCH_BLOCK_RE.finditer(bundle):
        cases = {int(code): label for code, label in _SWITCH_CASE_RE.findall(block_match.group(1))}
        if not cases:
            continue
        labels = set(cases.values())
        for enum_name, anchor in _ENUM_LABEL_ANCHORS.items():
            if anchor <= labels:
                out.setdefault(enum_name, {}).update(cases)
    return out


def parse_enum_members_from_text(types_text: str, enum_name: str) -> dict[str, int]:
    """Return ``{MEMBER_NAME: int_value}`` for an ``(int, Enum)`` class in types.py.

    Mirrors :func:`parse_ids_from_text` but for the integer studio enums. Scoped
    to the named class body so members of other enums don't bleed in. Aliases
    (two names, same value — e.g. ``QUIZ_FLASHCARD = 4``) are all retained.
    """
    match = re.search(rf"class {re.escape(enum_name)}\b.*?(?=\nclass |\Z)", types_text, re.DOTALL)
    if not match:
        return {}
    out: dict[str, int] = {}
    for name, value in re.findall(
        r"""^\s+([A-Z][A-Z0-9_]*)\s*=\s*(\d+)""", match.group(0), re.MULTILINE
    ):
        out[name] = int(value)
    return out


def diff_enums(
    types_text: str, live_switch: dict[str, dict[int, str]]
) -> dict[str, list[dict[str, object]]]:
    """Diff our int enums against the bundle's switch maps into a FOUR-class taxonomy.

    Mirroring the id ``diff`` but for the studio enums. For each enum the bundle
    attributed (via label-anchoring), each of our members is paired to a live
    ``code -> label`` by normalizing the label to ``UPPER_SNAKE`` and matching it
    to a member name. The four buckets:

    * ``CHANGED`` — a label present in BOTH our enum and the bundle but mapped to
      a DIFFERENT integer (our ``EXPLAINER = 1`` but the bundle now returns
      "Explainer" for ``case 2``). **This is the #1597 alarm**: an existing,
      selectable format silently renumbered. Fails ``--check-enums``.
    * ``STALE`` — our member's integer is not present in the bundle's code set
      for that enum at all (the format we still send was retired). Also fails
      ``--check-enums``.
    * ``NEW`` — the bundle declares a code our enum lacks (a new display label).
      **REPORT-ONLY, never an alarm**: a switch arm is only a *display label*; a
      label can ship in the bundle long before the format is selectable on any
      cohort (proven live — the bundle listed Short / Whiteboard Animation /
      Lecture while they were not yet selectable). Adding the member eagerly off
      a bundle label would encode an unreleased/non-functional code.
    * ``UNPARSED`` — an enum we have a label anchor for but found no switch block
      to attribute (a recognizable region didn't parse). "Widen the regex", NOT
      an alarm — same posture as PRESENT-UNPARSED in the id diff.

    Returns ``{class -> [records]}`` where each record carries the enum name and
    the specifics needed to report and to drive the ``--check-enums`` exit code.
    """
    buckets: dict[str, list[dict[str, object]]] = {
        "changed": [],
        "stale": [],
        "new": [],
        "unparsed": [],
    }
    for enum_name in _ENUM_LABEL_ANCHORS:
        live_map = live_switch.get(enum_name)
        if not live_map:
            # We know this enum (we hold an anchor) but no block parsed for it.
            buckets["unparsed"].append({"enum": enum_name})
            continue

        ours = parse_enum_members_from_text(types_text, enum_name)
        # live label (normalized) -> code, for matching our members by name.
        live_by_norm_label = {_normalize_label(label): code for code, label in live_map.items()}
        our_codes = set(ours.values())

        for member, value in ours.items():
            live_code = live_by_norm_label.get(member)
            if live_code is None:
                # The label our member name corresponds to is not in the bundle.
                live_label = live_map.get(value)
                norm_live_label = _normalize_label(live_label) if live_label else None
                if value not in live_map or (norm_live_label in ours and norm_live_label != member):
                    # STALE either because our integer code vanished from the
                    # bundle entirely, OR it was repurposed: the code now maps to
                    # a label that normalizes to a DIFFERENT member already in our
                    # enum, so our member name still pointing at it is wrong.
                    buckets["stale"].append(
                        {"enum": enum_name, "member": member, "our_value": value}
                    )
                # else: our value is still a live code under a label that didn't
                # normalize back to any of our member names — neither CHANGED nor
                # STALE (likely a renamed/aliased label we don't track yet).
            elif live_code != value:
                buckets["changed"].append(
                    {
                        "enum": enum_name,
                        "member": member,
                        "label": live_map[live_code],
                        "our_value": value,
                        "live_value": live_code,
                    }
                )

        for code, label in sorted(live_map.items()):
            if code not in our_codes:
                buckets["new"].append({"enum": enum_name, "code": code, "label": label})

    return buckets


# ---------------------------------------------------------------------------
# Quota codes (Yp map) and proto required-field assertions
# ---------------------------------------------------------------------------
#
# Two more drift surfaces the same bundle carries — extracted for *visibility*
# (a report line + JSON), not gated. They are leading indicators, not contract
# breaks: a new quota code means a feature is rolling out server-side (an
# early-warning for "build support soon"), and a changed proto assertion means a
# request shape we encode may have grown a newly-required field.

# The ``Yp`` quota map: ``[<code>,{status:"...",result:{message:"...limits..."}}]``.
# The ``...limits...`` anchor in the message keeps this off unrelated result
# objects. Codes map to features (1 chat, 3 audio, 6 video, 7 reports, ...).
_QUOTA_CODE_RE = re.compile(r'\[(\d+),\{status:"[^"]*",result:\{message:"([^"]*limits[^"]*)"')

# Proto required-field assertions: ``"<Message> is missing field '<field>'"``.
# A drift in this set means a request message grew/lost a required field.
_PROTO_ASSERTION_RE = re.compile(r"\"(\w+) is missing field '(\w+)'\"")


def extract_quota_codes(bundle: str) -> dict[int, str]:
    """Return ``{quota_code: message}`` from the bundle's ``Yp`` quota map.

    A feature-rollout early-warning surface: a code we have not seen before means
    Google is provisioning quota for a feature that is rolling out. Report-only.
    """
    return {int(code): message for code, message in _QUOTA_CODE_RE.findall(bundle)}


def extract_proto_assertions(bundle: str) -> set[tuple[str, str]]:
    """Return ``{(message, field)}`` proto required-field assertions from the bundle.

    A schema-shape drift surface: ``"ExplainerVideoArtifact is missing field
    'generation_options'"`` means that proto requires ``generation_options``. A
    new assertion can mean a request we build needs a field we don't send.
    Report-only.
    """
    return set(_PROTO_ASSERTION_RE.findall(bundle))


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


def _print_enum_report(
    enum_buckets: dict[str, list[dict[str, object]]],
    quota: dict[int, str],
    proto: set[tuple[str, str]],
) -> None:
    """Print the studio-enum / quota / proto drift report (same style as the id diff).

    ``CHANGED``/``STALE`` are the alarms (a selectable format renumbered or
    retired); ``NEW`` and ``UNPARSED`` print but never alarm. Quota codes and
    proto assertions are report-only visibility surfaces.
    """
    changed, stale, new, unparsed = (
        enum_buckets["changed"],
        enum_buckets["stale"],
        enum_buckets["new"],
        enum_buckets["unparsed"],
    )
    print("\n" + "=" * 60)
    print("STUDIO ENUM DRIFT (switch code->label maps)")
    print(
        f"CHANGED: {len(changed)}  STALE: {len(stale)}  NEW: {len(new)}  UNPARSED: {len(unparsed)}"
    )
    if changed:
        print("\nCHANGED — a label's integer differs from ours (the #1597 alarm):")
        for r in changed:
            print(
                f"  {r['enum']}.{r['member']} ({r['label']!r}): "
                f"ours={r['our_value']} live={r['live_value']}"
            )
    if stale:
        print("\nSTALE — our member's value is no longer a live code (format retired):")
        for r in stale:
            print(f"  {r['enum']}.{r['member']} = {r['our_value']}")
    if new:
        print("\nNEW — bundle code we lack (REPORT-ONLY; a display label may be unreleased):")
        for r in new:
            print(f"  {r['enum']} case {r['code']} -> {r['label']!r}")
    if unparsed:
        print("\nUNPARSED — known enum with no switch block parsed (widen regex; not an alarm):")
        for r in unparsed:
            print(f"  {r['enum']}")

    print("\nQUOTA CODES (Yp map — feature-rollout early-warning; report-only):")
    if quota:
        for code, message in sorted(quota.items()):
            print(f"  {code:<3} {message}")
    else:
        print("  (none parsed)")

    print("\nPROTO REQUIRED-FIELD ASSERTIONS (schema-shape; report-only):")
    if proto:
        for message, field in sorted(proto):
            print(f"  {message} -> {field}")
    else:
        print("  (none parsed)")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: load/fetch the bundle, diff vs rpc/types.py, report.

    Returns the process exit code: ``1`` when a gate fires —  ``--check`` with any
    ABSENT id (id rotation), and/or ``--check-enums`` with any CHANGED/STALE
    studio enum (a selectable format renumbered or retired). The two gates
    combine (either firing exits ``1``); ``NEW``/``UNPARSED`` enum classes,
    quota codes and proto assertions are report-only and never affect the exit
    code. Without a gate flag the exit is always ``0`` (report mode).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--json", action="store_true", help="emit a JSON snapshot instead of a report"
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if any of our ids are ABSENT")
    parser.add_argument(
        "--check-enums",
        action="store_true",
        help="exit 1 if any studio enum is CHANGED or STALE (NEW/UNPARSED never fail)",
    )
    parser.add_argument(
        "--bundle-file", type=Path, help="analyse a saved bundle file (no auth/network)"
    )
    parser.add_argument("--types", type=Path, default=_DEFAULT_TYPES, help="path to rpc/types.py")
    args = parser.parse_args(argv)

    types_text = args.types.read_text(encoding="utf-8")
    ours = parse_ids_from_text(types_text)
    bundle = args.bundle_file.read_text(encoding="utf-8") if args.bundle_file else fetch_bundle()
    live = extract_registry(bundle)
    buckets = diff(ours, live, bundle)
    # Services any CONFIRMED id resolves to are, empirically, serving our cohort.
    current_services = {_service_of(m) for m in buckets["confirmed"].values()}

    live_switch = extract_switch_enums(bundle)
    enum_buckets = diff_enums(types_text, live_switch)
    quota = extract_quota_codes(bundle)
    proto = extract_proto_assertions(bundle)

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
                    "enums": enum_buckets,
                    "quota_codes": {str(code): message for code, message in quota.items()},
                    "proto_assertions": sorted(f"{m}.{f}" for m, f in proto),
                    "counts": {k: len(v) for k, v in buckets.items()}
                    | {"ours": len(ours)}
                    | {f"enum_{k}": len(v) for k, v in enum_buckets.items()},
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_report(ours, live, buckets, current_services)
        _print_enum_report(enum_buckets, quota, proto)

    exit_code = 0
    if args.check and buckets["absent"]:
        print(
            f"\nFAIL: {len(buckets['absent'])} of our RPC ids are no longer registered.",
            file=sys.stderr,
        )
        exit_code = 1
    if args.check_enums and (enum_buckets["changed"] or enum_buckets["stale"]):
        print(
            f"\nFAIL: {len(enum_buckets['changed'])} CHANGED and "
            f"{len(enum_buckets['stale'])} STALE studio enum value(s) — a selectable "
            "format was renumbered or retired; re-capture rpc/types.py enums.",
            file=sys.stderr,
        )
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
