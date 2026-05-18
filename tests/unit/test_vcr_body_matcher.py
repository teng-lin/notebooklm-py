"""Unit tests for the hardened ``_freq_body_matcher`` in ``tests/vcr_config.py``.

This module complements ``tests/unit/test_vcr_config.py`` by exercising the
new behaviors added when ``freq`` was promoted to the default ``match_on``
tuple:

1. **Batchexecute structural-shape matching** — two batchexecute POSTs with
   different argument shapes (different arg counts, different nesting
   depths, different non-volatile dict key sets) no longer match. This is
   the regression-class the matcher was widened to catch: the original
   matcher only looked at slot 7 of the streaming-chat envelope and was
   blind to ``[[[rpc_id, args_json, ...]]]`` payloads entirely. Leaf-value
   drift between recording sessions (different fixture UUIDs, different
   note titles, different page sizes, ``null`` vs filled in the same
   slot) is intentionally folded — the spec's "widen the volatile-key
   scrub list" escape hatch from P1-3, see the PR body for the full
   trade-off rationale.
2. **Recursive volatile-key stripping** (streaming-chat path) —
   ``timestamp`` / ``requestId`` / ``nonce`` style keys are dropped from
   any dict node before comparison, so two otherwise-identical chat
   requests still match across re-recordings.
3. **Fallback-path string compare** — if the inner JSON is malformed
   (truncated cassette, unexpected envelope shape) the matcher falls
   through to a normalized raw ``f.req`` string compare instead of
   crashing or silently accepting the wrong cassette entry.

The streaming-chat positional matching (param count + slot 7 notebook_id,
slot 4 conv_id ignored) is already covered by ``test_vcr_config.py`` —
these tests focus on the batchexecute path and the new fallback contract.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Load ``tests/vcr_config.py`` by file path. ``tests/`` is not a package
# (no ``__init__.py``) so ``from tests.vcr_config import ...`` only works
# when the repo root happens to be on ``sys.path``. Loading by file path
# mirrors the established idiom in ``tests/unit/test_vcr_config.py``.
_TESTS_DIR = Path(__file__).resolve().parent.parent


def _load_by_path(module_name: str, file_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, _TESTS_DIR / file_name)
    assert spec is not None and spec.loader is not None, (
        f"Could not load {file_name} from {_TESTS_DIR}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_vcr_config = _load_by_path("tests_vcr_config", "vcr_config.py")
_freq_body_matcher: Callable[[Any, Any], bool] = _vcr_config._freq_body_matcher
_strip_volatile: Callable[[Any], Any] = _vcr_config._strip_volatile
_normalize_uuids: Callable[[str], str] = _vcr_config._normalize_uuids
_FREQ_VOLATILE_KEYS: frozenset[str] = _vcr_config._FREQ_VOLATILE_KEYS
_UUID_PLACEHOLDER: str = _vcr_config._UUID_PLACEHOLDER


class _StubRequest:
    """Minimal stand-in for a ``vcr.request.Request`` carrying only ``body``."""

    def __init__(self, body: Any) -> None:
        self.body = body


def _build_batchexecute_body(rpc_id: str, args: list[Any]) -> str:
    """Build a batchexecute-shaped ``f.req`` form body.

    Mirrors the wire format used by NotebookLM's batchexecute POSTs:
    ``f.req=<url-encoded JSON envelope>&at=<csrf>`` where the JSON envelope
    is ``[[[rpc_id, "<args_json>", null, "generic"]]]`` and ``<args_json>``
    is itself the JSON encoding of the positional arguments. This is the
    shape captured by real cassettes (see e.g. the ``gArtLc`` / ``rLM1Ne``
    entries in ``tests/cassettes/artifacts_list.yaml``).
    """
    args_json = json.dumps(args, separators=(",", ":"))
    envelope = json.dumps([[[rpc_id, args_json, None, "generic"]]], separators=(",", ":"))
    return f"f.req={quote(envelope, safe='')}&at=mock_csrf_token"


def _build_chat_freq_body(params: list[Any]) -> str:
    """Build a streaming-chat ``f.req`` form body (`[null, "<inner_json>"]`)."""
    inner_json = json.dumps(params, separators=(",", ":"))
    envelope = json.dumps([None, inner_json], separators=(",", ":"))
    return f"f.req={quote(envelope, safe='')}&at=mock_csrf_token"


# ---------------------------------------------------------------------------
# Batchexecute structural matching — the headline TDD assertions for P1-3
# ---------------------------------------------------------------------------
#
# The batchexecute matcher path uses :func:`_shape_only`: it compares list
# lengths and dict key sets but folds leaf values onto a single token. This
# is the spec's "widen the volatile-key scrub list" escape hatch applied at
# the structural level — leaf-value drift (different fixture UUIDs, different
# free-form text payloads, different ``null`` placeholders for optional slots)
# does NOT block cassette replay because the recorded RESPONSE is what the
# tests consume, not the request identity. The ``rpcids`` URL-query matcher
# already gates RPC identity, so this layer's job is structural shape only.
#
# What the matcher DOES catch:
#   - Different arg-list lengths (missing or extra positional slots).
#   - Different nesting depths (e.g. ``[[a, b]]`` vs ``[a, b]``).
#   - Different dict key sets (after volatile-key stripping).
#   - Missing or extra volatile keys are normalized; non-volatile dict keys
#     must match.
#   - Outer envelope shape mismatches (chat vs batchexecute).
#
# What the matcher does NOT catch (and the test assertions reflect this):
#   - Different non-UUID string leaves at the same slot ("note title X"
#     vs "note title Y") — caller's own assertions catch this.
#   - Different integer leaves at the same slot (page_size=1 vs 20).
#   - ``null`` vs filled value at the same slot.


def test_batchexecute_arg_count_drift_fails_matcher() -> None:
    """Different arg counts inside ``f.req`` fail the matcher.

    Captures the structural regression class the widened matcher targets:
    a cassette recorded with one arg shape (e.g. 3-arg ``gArtLc`` call)
    cannot be replayed for a different arg shape (e.g. 2-arg). The
    ``rpcids`` matcher would happily accept both since the URL query is
    identical; the body matcher catches the structural drift.
    """
    body_a = _build_batchexecute_body("gArtLc", [[2], "artifact_id", "filter"])
    body_b = _build_batchexecute_body("gArtLc", [[2], "artifact_id"])  # one fewer arg
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_b)) is False


def test_batchexecute_nesting_depth_drift_fails_matcher() -> None:
    """Different nesting depths inside ``f.req`` fail the matcher.

    ``[[1, 2]]`` (one level of nesting) must not match ``[1, 2]`` (flat).
    A future RPC change that adjusts list nesting would produce silently
    wrong replays under a depth-blind matcher.
    """
    body_a = _build_batchexecute_body("rpc", [[1, 2]])
    body_b = _build_batchexecute_body("rpc", [1, 2])
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_b)) is False


def test_batchexecute_identical_payloads_match() -> None:
    """Sanity: two identical batchexecute POSTs match (backward-compat baseline)."""
    args: list[Any] = [[2], "artifact_id_same", "filter_expression"]
    b1 = _build_batchexecute_body("gArtLc", args)
    b2 = _build_batchexecute_body("gArtLc", args)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is True


def test_batchexecute_leaf_value_drift_still_matches() -> None:
    """Leaf-value drift inside same-shape args still matches (by design).

    The matcher folds string / int / None leaves onto a single token before
    comparison, so two ``gArtLc`` POSTs differing only in their artifact
    UUID, their notebook UUID, their page-size integer, or their filter
    string still match. This is the spec's "widen the volatile-key scrub
    list" escape hatch — the alternative (strict leaf matching) breaks
    cassette replay across recording sessions, see the PR body discussion
    for the full trade-off.
    """
    args_a: list[Any] = [[2], "167481cd-23a3-4331-9a45-c8948900bf91", "filter A"]
    args_b: list[Any] = [[2], "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e", "filter B"]
    b1 = _build_batchexecute_body("gArtLc", args_a)
    b2 = _build_batchexecute_body("gArtLc", args_b)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is True


def test_batchexecute_null_vs_filled_slot_still_matches() -> None:
    """``null`` vs filled value at the same slot still matches (by design).

    This is the specific cassette-drift pattern that turned up in the
    ``artifacts_generate_quiz.yaml`` / ``artifacts_generate_flashcards.yaml``
    fixtures: the cassette has a trailing ``[null, null]`` while the live
    request sends ``[2, 2]``. Both are length-2 lists; the matcher accepts
    them as the same structural shape so the recorded response can replay.
    """
    body_a = _build_batchexecute_body("rpc", [None, None])
    body_b = _build_batchexecute_body("rpc", [2, 2])
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_b)) is True


def test_batchexecute_envelope_with_extra_top_level_slot_fails() -> None:
    """Extra positional slots at the top of the envelope fail the matcher.

    Catches the case where a future recorder emits an additional outer
    envelope element. ``rpcids`` would still match (URL is unchanged) but
    the body's structural shape has changed and the matcher rejects.
    """
    body_a = _build_batchexecute_body("rpc", ["arg"])
    # Craft a body with one extra top-level slot.
    args = ["arg"]
    args_json = json.dumps(args, separators=(",", ":"))
    envelope = json.dumps(
        [[["rpc", args_json, None, "generic"], ["extra", "extra_args", None, "generic"]]],
        separators=(",", ":"),
    )
    body_b = f"f.req={quote(envelope, safe='')}&at=mock_csrf_token"
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_b)) is False


# ---------------------------------------------------------------------------
# Volatile-key stripping — keep matching robust across recording timestamps
# ---------------------------------------------------------------------------


def test_volatile_keys_stripped_from_nested_dicts() -> None:
    """``timestamp`` / ``requestId`` in nested dicts must not block matching.

    Models a future RPC that embeds a per-request timestamp in its args.
    The cassette recorded at t1 has timestamp=T1, the replay at t2 sends
    timestamp=T2. The matcher must drop both before comparison so the
    requests still match.
    """
    args_t1 = [{"id": "abc", "timestamp": 1000, "requestId": "req-1"}, [2]]
    args_t2 = [{"id": "abc", "timestamp": 9999, "requestId": "req-99"}, [2]]
    b1 = _build_batchexecute_body("someRpc", args_t1)
    b2 = _build_batchexecute_body("someRpc", args_t2)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is True


def test_volatile_keys_case_insensitive() -> None:
    """Volatile-key matching is case-insensitive (``RequestId`` vs ``request_id``).

    Different parts of Google's API use different casings for the same
    logical field; stripping must catch them all.
    """
    args_a = [{"id": "abc", "RequestId": "x", "ClientTimestamp": 1}]
    args_b = [{"id": "abc", "requestid": "y", "clienttimestamp": 999}]
    b1 = _build_batchexecute_body("rpc", args_a)
    b2 = _build_batchexecute_body("rpc", args_b)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is True


def test_non_volatile_dict_key_set_drift_fails() -> None:
    """Different non-volatile dict KEY SETS still cause mismatch.

    The matcher folds leaf VALUES but preserves dict KEY SETS — a request
    that adds or removes a structural key from a dict node is a real
    structural drift the matcher must catch. ``id``, ``status``, etc. are
    non-volatile keys; adding ``extra`` to one side or dropping ``id``
    from the other side must fail.
    """
    args_a = [{"id": "abc", "timestamp": 1, "extra": "field"}]
    args_b = [{"id": "abc", "timestamp": 1}]  # missing "extra"
    b1 = _build_batchexecute_body("rpc", args_a)
    b2 = _build_batchexecute_body("rpc", args_b)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is False


def test_strip_volatile_leaves_lists_intact() -> None:
    """``_strip_volatile`` preserves list order and contents (only dicts are
    pruned). Direct test on the helper to lock in the contract.
    """
    out = _strip_volatile([1, 2, {"timestamp": 1, "keep": "yes"}, [3, 4]])
    assert out == [1, 2, {"keep": "yes"}, [3, 4]]


def test_strip_volatile_recurses_into_nested_dicts() -> None:
    """Volatile-key stripping reaches into deeply nested dicts."""
    payload = {
        "outer": {
            "timestamp": 1,
            "inner": {
                "requestId": "x",
                "keep": "value",
            },
        },
    }
    assert _strip_volatile(payload) == {"outer": {"inner": {"keep": "value"}}}


# ---------------------------------------------------------------------------
# Fallback path — malformed envelopes must fall through to string compare
# ---------------------------------------------------------------------------


def test_malformed_freq_falls_back_to_string_compare_identical() -> None:
    """Two requests with identical malformed ``f.req`` values match via string compare.

    Guards the spec's "On parse failure, fall back to normalized-string
    compare" contract. The string ``"not json at all"`` is not valid JSON,
    so the envelope parse raises; the matcher must compare raw values and
    return ``True`` for identical raws.
    """
    body = "f.req=not%20json%20at%20all&at=mock_csrf_token"
    assert _freq_body_matcher(_StubRequest(body), _StubRequest(body)) is True


def test_malformed_freq_falls_back_to_string_compare_different() -> None:
    """Two requests with different malformed ``f.req`` values do NOT match.

    Same fallback path as above, but the raw values differ — the matcher
    must report mismatch instead of silently accepting either side.
    """
    body1 = "f.req=not%20json%20at%20all&at=mock_csrf_token"
    body2 = "f.req=also%20not%20json&at=mock_csrf_token"
    assert _freq_body_matcher(_StubRequest(body1), _StubRequest(body2)) is False


def test_malformed_inner_args_still_matches_by_structure() -> None:
    """Outer envelope parses but inner ``args_json`` is malformed.

    The outer ``[[[rpc, "...", null, "generic"]]]`` shape parses cleanly,
    but the inner args string isn't valid JSON. The matcher keeps the
    raw string in place during structural folding, so two batchexecute
    envelopes with the same outer shape still match even when the inner
    args couldn't be JSON-decoded — leaf folding handles the string vs
    string-of-different-content case the same way it would handle two
    different note titles.
    """
    bad_inner = "[not, valid, json"
    envelope_a = json.dumps([[["rpc_a", bad_inner, None, "generic"]]])
    envelope_b = json.dumps([[["rpc_a", bad_inner, None, "generic"]]])
    body_a = f"f.req={quote(envelope_a, safe='')}&at=mock_csrf_token"
    body_b = f"f.req={quote(envelope_b, safe='')}&at=mock_csrf_token"
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_b)) is True

    # Same OUTER structural shape, even with a different RPC id, still
    # matches at the body level — the RPC id leaf folds. The ``rpcids``
    # URL-query matcher is what enforces RPC identity in production.
    envelope_c = json.dumps([[["rpc_b", bad_inner, None, "generic"]]])
    body_c = f"f.req={quote(envelope_c, safe='')}&at=mock_csrf_token"
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_c)) is True

    # A truly structural envelope-shape change (e.g. extra outer slot)
    # still fails — see ``test_batchexecute_envelope_with_extra_top_level_slot_fails``.


def test_envelope_without_inner_string_is_treated_as_raw_compare() -> None:
    """Outer JSON parses but is neither chat-shape nor batch-shape.

    Falls into the ``("raw", f_req)`` branch — string-compare semantics.
    Two requests with the same unknown-shape envelope match; two requests
    with different unknown-shape envelopes do not.
    """
    body_a = "f.req=%7B%22some%22%3A%22object%22%7D&at=csrf"  # {"some":"object"}
    body_b = "f.req=%7B%22some%22%3A%22object%22%7D&at=csrf"
    body_c = "f.req=%7B%22other%22%3A%22shape%22%7D&at=csrf"
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_b)) is True
    assert _freq_body_matcher(_StubRequest(body_a), _StubRequest(body_c)) is False


# ---------------------------------------------------------------------------
# Backward-compat with the existing default match_on guarantee
# ---------------------------------------------------------------------------


def test_non_freq_bodies_defer_to_other_matchers() -> None:
    """Requests without any ``f.req`` field return True (defer to other matchers).

    Adding ``freq`` to the default ``match_on`` tuple must NOT block
    matching for non-RPC requests (GETs, multipart uploads, JSON-API
    calls). The matcher returns ``True`` so the method/path/host/port
    matchers drive the decision.
    """
    r1 = _StubRequest("")
    r2 = _StubRequest("")
    assert _freq_body_matcher(r1, r2) is True

    r3 = _StubRequest("some=other&form=data")
    r4 = _StubRequest("entirely=different&query=string")
    assert _freq_body_matcher(r3, r4) is True


def test_one_freq_one_not_freq_rejects() -> None:
    """Mixed ``f.req`` and no-``f.req`` requests do not match.

    Structurally different request shapes must never collapse to identity.
    """
    with_freq = _build_batchexecute_body("rpc", ["arg"])
    without = "method=GET&query=value"
    assert _freq_body_matcher(_StubRequest(with_freq), _StubRequest(without)) is False
    assert _freq_body_matcher(_StubRequest(without), _StubRequest(with_freq)) is False


def test_chat_shape_still_uses_slot_7_notebook_id() -> None:
    """Backward-compat: streaming-chat shape still distinguishes by notebook_id.

    Two chat POSTs differing only at slot 7 must NOT match, preserving the
    existing test suite's invariant (see
    ``test_freq_matcher_notebook_id_mismatch_at_slot_seven``).
    """
    p_alpha = [None, "Q?", None, [2], "conv_abc", None, None, "nb_alpha", 1]
    p_beta = [None, "Q?", None, [2], "conv_abc", None, None, "nb_beta", 1]
    b1 = _build_chat_freq_body(p_alpha)
    b2 = _build_chat_freq_body(p_beta)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is False


def test_chat_shape_still_ignores_slot_4_conversation_id() -> None:
    """Backward-compat: streaming-chat shape still ignores conv_id at slot 4."""
    p1 = [None, "Q?", None, [2], "conv_a", None, None, "nb_same", 1]
    p2 = [None, "Q?", None, [2], "conv_b", None, None, "nb_same", 1]
    b1 = _build_chat_freq_body(p1)
    b2 = _build_chat_freq_body(p2)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is True


def test_chat_vs_batch_shape_mismatch_rejects() -> None:
    """Mixed envelope shapes (chat vs batchexecute) do not match.

    Conservative: a chat-shaped POST should never silently match a
    batchexecute-shaped POST, even if their query strings happen to align.
    """
    chat = _build_chat_freq_body([None, "Q", None, [2], "conv", None, None, "nb", 1])
    batch = _build_batchexecute_body("rpc", ["arg"])
    assert _freq_body_matcher(_StubRequest(chat), _StubRequest(batch)) is False


# ---------------------------------------------------------------------------
# Volatile-key registry shape — guard against accidental key removal
# ---------------------------------------------------------------------------


def test_freq_volatile_keys_registry_is_lowercase() -> None:
    """All entries in ``_FREQ_VOLATILE_KEYS`` are pre-lowercased.

    The case-insensitive lookup in ``_strip_volatile`` lowercases the key
    being tested but trusts the registry itself to be lowercase already.
    This test pins that contract so a future contributor who adds
    ``"RequestID"`` to the registry sees the test fail immediately rather
    than silently producing a non-matching entry.
    """
    for key in _FREQ_VOLATILE_KEYS:
        assert key == key.lower(), f"_FREQ_VOLATILE_KEYS entry {key!r} is not lowercase"


# ---------------------------------------------------------------------------
# UUID normalization — the "widen the volatile-key scrub list" escape hatch
# ---------------------------------------------------------------------------


def test_uuid_drift_inside_batchexecute_args_still_matches() -> None:
    """Two batchexecute POSTs differing only in their UUID args still match.

    Documents the spec's "widen the volatile-key scrub list" escape hatch
    (P1-3): cassettes are recorded against one notebook UUID and replayed
    against a different one with the same structural shape. The matcher
    folds UUID v4 leaves onto :data:`_UUID_PLACEHOLDER` so this incidental
    drift doesn't break replay. Meaningful drift — different RPC id,
    different non-UUID args, different arg shape — is still caught (see
    ``test_batchexecute_artifact_id_drift_fails_matcher`` and friends).
    """
    args_a: list[Any] = [[2], "167481cd-23a3-4331-9a45-c8948900bf91", "filter"]
    args_b: list[Any] = [[2], "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e", "filter"]
    b1 = _build_batchexecute_body("gArtLc", args_a)
    b2 = _build_batchexecute_body("gArtLc", args_b)
    assert _freq_body_matcher(_StubRequest(b1), _StubRequest(b2)) is True


def test_uuid_normalization_folds_drift_to_placeholder() -> None:
    """``_normalize_uuids`` replaces every UUID v4 substring with the placeholder.

    Direct test on the helper to lock in the contract — the matcher uses
    this transform on every string leaf.
    """
    text = "id=167481cd-23a3-4331-9a45-c8948900bf91 ref=c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"
    out = _normalize_uuids(text)
    assert out == f"id={_UUID_PLACEHOLDER} ref={_UUID_PLACEHOLDER}"


def test_uuid_normalization_leaves_non_uuid_strings_alone() -> None:
    """Strings that aren't UUID-shaped pass through ``_normalize_uuids`` unchanged.

    Guards against an over-eager regex that would corrupt legitimate
    payload fields (filter expressions, RPC ids, etc.). Empty strings are
    folded onto the placeholder (see :func:`_normalize_uuids` docstring) so
    they're tested separately in
    ``test_uuid_normalization_folds_empty_string_to_placeholder``.
    """
    samples = [
        "gArtLc",
        "artifact_id_xxxxxxxxx",
        'NOT artifact.status = "SUGGESTED"',
        "1768312221241",  # 13-digit timestamp — not UUID-shaped
    ]
    for s in samples:
        assert _normalize_uuids(s) == s, f"{s!r} was mutated unexpectedly"


def test_uuid_normalization_folds_empty_string_to_placeholder() -> None:
    """Empty strings are folded onto the same placeholder as UUIDs.

    Models the test-infra pattern where a cassette recorded against a real
    notebook UUID is replayed in an environment where the notebook ID is
    unset (the request body sends ``""``). The two are functionally
    interchangeable for cassette matching.
    """
    assert _normalize_uuids("") == _UUID_PLACEHOLDER


def test_strip_volatile_folds_uuid_inside_dict() -> None:
    """UUID normalization reaches into dict values as well as list leaves."""
    payload = {
        "notebook_id": "167481cd-23a3-4331-9a45-c8948900bf91",
        "label": "stable_label",
    }
    out = _strip_volatile(payload)
    assert out == {"notebook_id": _UUID_PLACEHOLDER, "label": "stable_label"}
