"""Cassette-shape regression lint.

This module walks every VCR cassette under ``tests/cassettes`` and asserts a
small set of structural invariants that the known cassette-shape regression
classes violate. The intent is to catch reintroductions of those defect
classes before they reach ``main``.

Assertions per batchexecute interaction (URL carries ``?rpcids=``):

* **A. rpcids ↔ WRB id alignment.** Every RPC ID named in the URL's ``rpcids``
  query parameter must appear as the second slot of a ``"wrb.fr"`` envelope in
  the chunked response. Surplus URL rpcids (named but never answered) are a
  defect; surplus WRB ids (answered but not requested) are likewise rejected.
  Envelope ids ``"di"``, ``"af.httprm"``, ``"e"`` are housekeeping and ignored.

* **B. f.req decodes.** ``f.req`` extracted from the urlencoded body must
  URL-decode and ``json.loads`` to a list. ``f.req=SCRUBBED`` trips this.

* **C. Chunked byte-counts are accurate.** Each integer prefix in the
  ``)]}'``-stripped response body must equal the UTF-8 byte length of the
  single JSON line that follows it.

* **D. No leaked patterns** (applies to ALL interactions): escaped display-
  name JSON literals like ``\\"Capitalized Two Words\\"``,
  ``lh3.googleusercontent.com/(a|ogw)/`` avatar URLs, and the literal IP
  ``108.5.149.175``.

In addition, for the specific RPC ID ``otS69`` (chat ask) the lint enforces
the new 9-param outer shape ``[null, "<inner-json-string>"]`` whose inner
JSON-decoded list carries at least 9 params.

Cassettes flagged by the audit's "needs re-recording" set are marked xfail
with explicit reasons referencing their follow-up repair work. When the
corresponding follow-up PR lands and re-records the cassette, the xfail
marker MUST be removed in that PR.

Non-batchexecute interactions (e.g. the streaming-query endpoint
``GenerateFreeFormStreamed``, GETs against the SPA shell, the legacy
``example_httpbin_*`` fixtures) skip the RPC-shape assertions because they
do not carry a ``rpcids`` query parameter or WRB envelope. The leak-pattern
check (D) still runs against their YAML text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import yaml

from _guardrails._cassette_shape_lint import _find_leaks

pytestmark = pytest.mark.repo_lint

# Prefer libyaml for the cassette-shape lint. The top-level cassette set is
# large enough that pure-Python SafeLoader dominates unit-suite runtime.
try:
    from yaml import CSafeLoader as _YamlSafeLoader
except ImportError:  # pragma: no cover - libyaml ships with PyYAML wheels
    from yaml import SafeLoader as _YamlSafeLoader  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Cassette discovery
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = REPO_ROOT / "tests" / "cassettes"
BAD_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "bad_cassettes"


def _real_cassettes() -> list[Path]:
    return sorted(CASSETTE_DIR.glob("*.yaml"))


# Cassettes that the cassette-hardening audit flagged for re-recording or
# re-scrubbing. Each entry carries the regression class so the xfail
# message points reviewers at the eventual fix.
#
# The "67 cassettes with /ogw/" dynamic set that used to surface here was
# cleared by a bulk re-scrub pass: the script in
# ``scripts/rescrub-cassettes.py`` collapsed every remaining avatar URL
# to ``SCRUBBED_AVATAR_URL`` and re-derived the chunked byte-counts in the
# same pass, so the dynamic ``/ogw/`` detector and its accompanying xfail
# branch are now dead code — both were removed.
AUDIT_REPAIR_LIST: dict[str, str] = {
    # artifacts_revise_slide.yaml was re-recorded against the live
    # REVISE_SLIDE RPC so f.req carries the real urlencoded JSON payload
    # again (only sensitive scalars scrubbed inside, not the whole body
    # collapsed to ``"SCRUBBED"``).
    # chat_ask.yaml + chat_ask_with_references.yaml were re-recorded
    # against the current 9-param streaming-chat builder
    # (src/notebooklm/_chat/api.py:459-469) with the ``freq`` body matcher
    # opted in per-cassette in tests/integration/test_vcr_comprehensive.py.
    # sources_add_file.yaml was repaired — upload tokens scrubbed in
    # place. sources_add_drive.yaml + sources_check_freshness_drive.yaml
    # were repaired — Drive AONS tokens scrubbed in place.
    # example_httpbin_{get,post}.yaml were deleted — the origin-IP leak
    # was in illustrative VCR examples, not real NotebookLM cassettes.
    # The example tests in test_vcr_example.py that used them were
    # removed in the same PR.
    # The 61 ``/ogw/`` avatar URL cassettes were bulk re-scrubbed — the
    # dynamic detector / xfail branch that used to live here was removed
    # in the same PR.
}


def _has_bytecount_drift(cassette: Path) -> bool:
    """Return True if the cassette has stale chunked byte-count prefixes.

    This is the chunked byte-count drift class: when the original network response used
    ``\\r\\n`` line endings, the byte-count was computed against the
    pre-strip bytes but the cassette stores the ``\\r``-stripped form, so
    every chunk prefix overshoots by exactly the number of stripped
    carriage returns. Byte-count re-derivation plus the bulk re-scrub
    fix this; until then we xfail affected cassettes so the rest of the
    lint stays enforceable.
    """
    try:
        data, _ = _load_cassette(cassette)
    except (yaml.YAMLError, OSError):
        return False
    for interaction in data.get("interactions") or []:
        body = (interaction.get("response") or {}).get("body") or {}
        if _byte_count_failures(body.get("string") or ""):
            return True
    return False


# Leak patterns (assertion D — applies to ALL interactions, including
# non-batchexecute): the minimal detectors and the DISPLAY_NAME_FALSE_POSITIVES
# allowlist live in the shared non-test helper _guardrails._cassette_shape_lint
# (``_find_leaks`` is imported at the top of this module); the canonical scrub
# registry lives in tests/cassette_patterns.py.


# ---------------------------------------------------------------------------
# Shape extractors
# ---------------------------------------------------------------------------

# RPC IDs for which a per-RPC shape guard fires. Right now only chat-ask is
# enforced; other RPC-specific shapes can be added here without changing the
# generic batchexecute checks.
CHAT_ASK_RPC_ID = "otS69"
CHAT_ASK_MIN_INNER_PARAMS = 9

# WRB envelope tags that are housekeeping and should not be treated as RPC
# responses.
HOUSEKEEPING_WRB_TAGS = frozenset({"di", "af.httprm", "e"})

XSSI_PREFIX = ")]}'"


def _strip_xssi(body: str) -> str:
    """Drop the Google anti-XSSI ``)]}'`` prefix and the blank line following."""
    if body.startswith(XSSI_PREFIX):
        rest = body[len(XSSI_PREFIX) :]
        # Either ``)]}'\n\n`` (most cassettes) or ``)]}'\n`` followed by data.
        return rest.lstrip("\n")
    return body


def _rpcids_from_url(uri: str) -> list[str]:
    """Return the list of rpcids named in the URL (comma-separated allowed)."""
    qs = parse_qs(urlparse(uri).query)
    raw = qs.get("rpcids", [""])[0]
    if not raw:
        return []
    return [p for p in raw.split(",") if p]


def _wrb_ids_from_response(body: str) -> list[str]:
    """Return RPC IDs seen in ``[\"wrb.fr\", <id>, ...]`` envelopes.

    Walks the chunked response (alternating ``<int>\\n<json>\\n`` records),
    parses each JSON record, and yields any envelope whose first slot is
    ``"wrb.fr"`` and second slot is a non-housekeeping string.
    """
    ids: list[str] = []
    payload = _strip_xssi(body)
    lines = payload.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        try:
            int(line)  # byte-count prefix
        except ValueError:
            # Not a count — try to parse as JSON directly (some cassettes
            # omit the count line entirely for trivial bodies).
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                i += 1
                continue
            ids.extend(_collect_wrb_ids(chunk))
            i += 1
            continue
        i += 1
        if i >= len(lines):
            break
        try:
            chunk = json.loads(lines[i])
        except json.JSONDecodeError:
            i += 1
            continue
        ids.extend(_collect_wrb_ids(chunk))
        i += 1
    return ids


def _collect_wrb_ids(chunk: Any) -> list[str]:
    """Extract RPC IDs from a parsed chunk (list-of-envelopes)."""
    if not isinstance(chunk, list):
        return []
    ids: list[str] = []
    for envelope in chunk:
        if (
            isinstance(envelope, list)
            and len(envelope) >= 2
            and envelope[0] == "wrb.fr"
            and isinstance(envelope[1], str)
            and envelope[1] not in HOUSEKEEPING_WRB_TAGS
        ):
            ids.append(envelope[1])
    return ids


def _byte_count_failures(body: str) -> list[str]:
    """Return descriptors for chunks whose declared byte count is wrong.

    Mirrors ``parse_chunked_response``'s line-wise format: lines alternate
    between an integer byte-count and the single JSON line whose UTF-8 byte
    length must equal that count.
    """
    failures: list[str] = []
    payload = _strip_xssi(body)
    lines = payload.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        try:
            declared = int(line)
        except ValueError:
            i += 1
            continue
        i += 1
        if i >= len(lines):
            break
        actual = len(lines[i].encode("utf-8"))
        if declared != actual:
            failures.append(
                f"chunk@line{i + 1}: prefix declares {declared} bytes but payload is {actual} bytes"
            )
        i += 1
    return failures


def _decode_freq(body: str | bytes | None) -> Any:
    """URL-decode + JSON-parse the ``f.req`` value of a urlencoded body.

    Raises ValueError if extraction or decoding fails. Returns ``None`` if
    the body simply has no ``f.req`` field (e.g. unrelated POST).
    """
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"request body is not valid UTF-8 ({exc})") from exc
    qs = parse_qs(body, keep_blank_values=True)
    if "f.req" not in qs:
        return None
    raw = qs["f.req"][0]
    decoded = unquote(raw)
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"f.req does not decode to JSON: raw={raw!r} decoded={decoded!r} ({exc})"
        ) from exc


# ---------------------------------------------------------------------------
# Core per-cassette lint
# ---------------------------------------------------------------------------


def _load_cassette(path: Path) -> tuple[dict[str, Any], str]:
    """Return the parsed cassette dict and its raw YAML text.

    Raw text is kept for leak-pattern scanning so we catch leaks that live
    inside escaped JSON-string payloads (the parsed structure would lose the
    escape characters in the regex).
    """
    # Explicit UTF-8 — cassettes routinely carry emoji bytes in notebook /
    # artifact titles. Default Python text mode uses the platform encoding
    # (cp1252 on Windows), which can't decode the 4-byte UTF-8 emoji
    # sequences.
    raw = path.read_text(encoding="utf-8")
    data = yaml.load(raw, Loader=_YamlSafeLoader) or {}
    return data, raw


def _lint_cassette(path: Path) -> list[str]:
    """Run all assertions on one cassette. Return list of failure messages."""
    failures: list[str] = []
    data, raw_text = _load_cassette(path)

    # D — leak patterns over the raw YAML text (includes both request URLs
    # and response bodies, before YAML re-quoting strips escapes).
    failures.extend(f"leak: {leak}" for leak in _find_leaks(raw_text))

    # synthetic-error cassettes (``error_synthetic_*.yaml``) carry
    # canonical error bodies from
    # ``tests.cassette_patterns.build_synthetic_error_response``: JSON
    # ``{"error": {...}}`` shapes whose ONLY purpose is to drive the client's
    # exception-mapping branches. They never contain a WRB envelope (real
    # Google error responses don't either) and they don't carry a chunked
    # XSSI body, so assertion A (rpcids ↔ WRB id alignment) and assertion C
    # (chunked byte-count accuracy) are not applicable. The B (f.req decode)
    # and D (leak patterns) checks still run.
    is_synthetic_error = path.name.startswith("error_synthetic_")

    interactions = data.get("interactions") or []
    for idx, interaction in enumerate(interactions):
        req = interaction.get("request") or {}
        resp = interaction.get("response") or {}
        uri = req.get("uri") or ""
        body = req.get("body")
        resp_body = (resp.get("body") or {}).get("string") or ""

        rpcids = _rpcids_from_url(uri)
        is_batchexecute = bool(rpcids)

        if not is_batchexecute:
            # Non-batchexecute (e.g. streaming-query, SPA shell GET, httpbin):
            # skip RPC-shape and byte-count checks; leak check already ran on
            # the whole text above.
            continue

        # B — f.req decodes
        try:
            freq = _decode_freq(body)
        except ValueError as exc:
            failures.append(f"interaction[{idx}] f.req decode failed: {exc}")
            freq = None

        # chat-ask shape guard (per-RPC). Only fires when the chat-ask
        # RPC ID is present in rpcids AND the f.req decoded.
        if CHAT_ASK_RPC_ID in rpcids and freq is not None:
            shape_err = _check_chat_ask_shape(freq)
            if shape_err:
                failures.append(f"interaction[{idx}] {shape_err}")

        if is_synthetic_error:
            # Synthetic error cassettes carry a JSON error body, not a WRB
            # envelope or chunked XSSI body — assertions A and C don't apply.
            continue

        # A — rpcids in URL must match WRB ids in response
        wrb_ids = _wrb_ids_from_response(resp_body)
        url_set = set(rpcids)
        wrb_set = set(wrb_ids)
        if url_set != wrb_set:
            failures.append(
                f"interaction[{idx}] rpcids mismatch: URL={sorted(url_set)} WRB={sorted(wrb_set)}"
            )

        # C — chunked byte counts
        failures.extend(f"interaction[{idx}] {bc}" for bc in _byte_count_failures(resp_body))

    return failures


def _check_chat_ask_shape(freq: Any) -> str | None:
    """Return error message if `freq` is not in the chat-ask 9-param shape.

    Real chat-ask `f.req` is ``[null, "<inner-json>"]`` whose inner JSON
    decodes to a list of at least 9 positional params.
    """
    if not (
        isinstance(freq, list) and len(freq) == 2 and freq[0] is None and isinstance(freq[1], str)
    ):
        return (
            "chat-ask shape regression: expected outer "
            f"[null, '<inner-json>'], got {type(freq).__name__} {freq!r:.120}"
        )
    try:
        inner = json.loads(freq[1])
    except json.JSONDecodeError as exc:
        return f"chat-ask inner JSON does not parse: {exc}"
    if not isinstance(inner, list) or len(inner) < CHAT_ASK_MIN_INNER_PARAMS:
        n = len(inner) if isinstance(inner, list) else "non-list"
        return (
            "chat-ask shape regression: inner f.req has "
            f"{n} params, need >= {CHAT_ASK_MIN_INNER_PARAMS}"
        )
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _xfail_reason(cassette: Path) -> str | None:
    """Return the xfail reason for a cassette in the audit's repair set, else None.

    Resolution order (most specific first):
      1. Explicit AUDIT_REPAIR_LIST — the cassettes the audit named.
      2. Byte-count drift present — chunked byte-count prefix drifted
         from payload length after sanitization.

    Each branch returns a different reason so future repair PRs can
    identify which xfail markers their work should clear.

    The ``/ogw/`` avatar URL branch that used to sit between (1) and (2)
    was removed — the bulk re-scrub script in
    ``scripts/rescrub-cassettes.py`` collapsed every remaining avatar URL
    and re-derived the affected chunk byte-counts in a single pass, so
    no real cassette can satisfy the old detector anymore.
    """
    if cassette.name in AUDIT_REPAIR_LIST:
        return AUDIT_REPAIR_LIST[cassette.name]
    if _has_bytecount_drift(cassette):
        return (
            "Byte-count re-derivation will fix: chunked byte-count "
            "prefix drifted from payload length after sanitization"
        )
    return None


@pytest.mark.parametrize(
    "cassette",
    _real_cassettes(),
    ids=lambda p: p.name,
)
def test_cassette_shape(cassette: Path, request: pytest.FixtureRequest) -> None:
    """Every real cassette must satisfy the shape invariants (xfail where audited)."""
    reason = _xfail_reason(cassette)
    if reason:
        request.applymarker(pytest.mark.xfail(reason=reason, strict=True))

    failures = _lint_cassette(cassette)
    assert not failures, f"Cassette {cassette.name} failed shape lint:\n  - " + "\n  - ".join(
        failures
    )


# ---------------------------------------------------------------------------
# Regression assertions: synthetic-bad fixtures must trip the lint, each on
# its targeted assertion class.
# ---------------------------------------------------------------------------


def test_bad_revise_slide_trips_freq_decode() -> None:
    """Regression: ``f.req=SCRUBBED`` must fail the f.req-decode assertion."""
    failures = _lint_cassette(BAD_FIXTURE_DIR / "bad_revise_slide.yaml")
    assert any("f.req decode failed" in f for f in failures), (
        f"Expected f.req decode failure, got: {failures}"
    )


def test_bad_chat_ask_trips_shape_guard() -> None:
    """Regression: a stale 5-param chat shape must trip the otS69 shape guard."""
    failures = _lint_cassette(BAD_FIXTURE_DIR / "bad_chat_ask.yaml")
    assert any("chat-ask shape regression" in f for f in failures), (
        f"Expected chat-ask shape regression, got: {failures}"
    )


def test_bad_sharing_trips_leak_check() -> None:
    """Regression: an escaped display-name JSON literal must trip the leak check."""
    failures = _lint_cassette(BAD_FIXTURE_DIR / "bad_sharing.yaml")
    assert any("escaped display-name" in f for f in failures), (
        f"Expected escaped display-name leak, got: {failures}"
    )


def test_bad_byte_count_trips_byte_count_check() -> None:
    """Regression: the chunk prefix must equal the payload's UTF-8 byte length."""
    failures = _lint_cassette(BAD_FIXTURE_DIR / "bad_byte_count.yaml")
    assert any("prefix declares" in f for f in failures), (
        f"Expected byte-count mismatch, got: {failures}"
    )


def test_audit_repair_list_entries_exist() -> None:
    """Every cassette in AUDIT_REPAIR_LIST must actually exist on disk.

    Guards against the xfail set drifting out of sync with the cassettes/
    directory (e.g. someone renames a cassette without updating this list).
    """
    missing = [name for name in AUDIT_REPAIR_LIST if not (CASSETTE_DIR / name).exists()]
    assert not missing, f"AUDIT_REPAIR_LIST references cassettes that no longer exist: {missing}"


# ---------------------------------------------------------------------------
# Encoding-header coverage (issue #773 follow-up to #769 / #771)
# ---------------------------------------------------------------------------
# ``decode_compressed_response=True`` in :mod:`tests.vcr_config` strips
# ``Content-Encoding`` from every recorded response. Pre-#751 that was fine
# because the response went through ``client.post`` once and decoded
# exactly once; #751 introduced the streaming rebuild path in
# :func:`notebooklm._streaming_post.stream_post_with_size_cap`, and the
# combination of an upstream gzip header re-applied to already-decoded
# bytes is what bit #769 in production. The existing cassette suite
# couldn't surface that regression because no cassette carried the
# header. This lint pins a floor on the encoding-header coverage so a
# future cassette wipe (or a regression in the gzip-injection workflow
# under ``tests/scripts/inject_gzip_into_cassette.py``) trips CI loudly.


def _all_cassettes_recursive() -> list[Path]:
    """Every cassette under ``tests/cassettes``, including subdirectories
    like ``gzip_coverage/``.

    Kept separate from :func:`_real_cassettes` (which is non-recursive on
    purpose — the existing shape lint targets the canonical recorded
    cassettes only, not derived sibling cassettes that may carry binary
    bodies or other non-canonical shapes).
    """
    return sorted(CASSETTE_DIR.rglob("*.yaml"))


# Cassette header lines for ``Content-Encoding: gzip`` follow the vcrpy
# YAML serialization shape: the case-insensitive header name on one line
# followed by ``- gzip`` (with optional surrounding whitespace) on the
# next. Anchored to ``^`` so the pattern does not match `gzip` mentioned
# in a request URL or response body. Compiled at module scope so the
# walk-the-cassette test stays cheap on Windows runners where libyaml
# isn't available and structural ``yaml.safe_load`` is ~10× slower —
# the original load-every-cassette implementation timed out at 60s on
# Windows Python 3.11 / 3.12.
_CONTENT_ENCODING_GZIP_RE = re.compile(
    r"^[ \t]*[Cc]ontent-[Ee]ncoding:\s*\n[ \t]*-\s*gzip\b",
    re.MULTILINE,
)


def test_at_least_one_cassette_advertises_content_encoding_gzip() -> None:
    """At least one cassette must carry ``Content-Encoding: gzip`` in a
    response header.

    Closes the test-coverage gap that hid #769. The fix lives in
    ``tests/cassettes/gzip_coverage/`` plus the helper in
    ``tests/scripts/inject_gzip_into_cassette.py`` that re-derives those
    cassettes from canonical recordings. If this assertion ever fails
    again, regenerate the gzip-coverage cassettes — do not silence the
    test.

    Scans raw cassette text rather than ``yaml.safe_load``-ing every
    cassette — see ``_CONTENT_ENCODING_GZIP_RE`` for why.
    """
    matches = [
        c
        for c in _all_cassettes_recursive()
        if _CONTENT_ENCODING_GZIP_RE.search(c.read_text(encoding="utf-8"))
    ]
    assert matches, (
        "No cassette under tests/cassettes/ advertises Content-Encoding: gzip "
        "on any response. The streaming rebuild path in "
        "notebooklm._streaming_post.stream_post_with_size_cap is invisible to "
        "VCR replay without it (see #769/#771). Regenerate the gzip-coverage "
        "cassette(s) via:\n"
        "    uv run python tests/scripts/inject_gzip_into_cassette.py "
        "tests/cassettes/<source>.yaml "
        "tests/cassettes/gzip_coverage/<source>_gzipped.yaml"
    )


def test_gzip_coverage_cassettes_round_trip_through_helper() -> None:
    """Every cassette under ``gzip_coverage/`` must be a fixed point of
    the gzip-injection helper.

    Catches drift between the committed cassettes and the helper's
    current encoding choices (compression level, header casing,
    stripped headers). Re-running the helper on a fixed cassette must
    produce a byte-identical file; otherwise the helper changed shape
    and the cassettes need regenerating in the same PR.
    """
    import importlib.util

    # Safe loader + dumper — cassettes are arbitrary YAML on disk and we
    # never need to serialize Python-specific tags here, so the safe
    # schema is the symmetric and audit-friendly choice. ``!!binary``
    # (the encoding gzipped bodies rely on) is part of the core YAML
    # schema and round-trips through ``CSafeLoader`` / ``CSafeDumper``.
    try:
        from yaml import CSafeDumper as Dumper
        from yaml import CSafeLoader as Loader
    except ImportError:  # pragma: no cover — libyaml ships with PyYAML wheels
        from yaml import SafeDumper as Dumper  # type: ignore[assignment]
        from yaml import SafeLoader as Loader

    # ``tests/`` is not a Python package, so import the helper by file path —
    # same pattern :mod:`tests.vcr_config` uses for sibling modules.
    helper_path = REPO_ROOT / "tests" / "scripts" / "inject_gzip_into_cassette.py"
    spec = importlib.util.spec_from_file_location(
        "tests_scripts_inject_gzip_into_cassette", helper_path
    )
    assert spec is not None and spec.loader is not None
    helper_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper_module)
    inject_gzip_into_cassette = helper_module.inject_gzip_into_cassette

    coverage_dir = CASSETTE_DIR / "gzip_coverage"
    cassettes = sorted(coverage_dir.glob("*.yaml")) if coverage_dir.is_dir() else []
    assert cassettes, (
        "No gzip-coverage cassettes found under tests/cassettes/gzip_coverage/. "
        "test_at_least_one_cassette_advertises_content_encoding_gzip should "
        "have failed first — investigate that failure before this one."
    )
    drifted: list[str] = []
    for cassette in cassettes:
        original = cassette.read_text(encoding="utf-8")
        data = yaml.load(original, Loader=Loader)
        rewritten = inject_gzip_into_cassette(data)
        if rewritten == 0:
            drifted.append(f"{cassette.name}: helper did not match any response")
            continue
        regen = yaml.dump(data, Dumper=Dumper)
        if regen != original:
            drifted.append(
                f"{cassette.name}: not a fixed point of inject_gzip_into_cassette. "
                "Regenerate via the helper and commit."
            )
    assert not drifted, "\n  - ".join(["gzip-coverage cassette drift:"] + drifted)
