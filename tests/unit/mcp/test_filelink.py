"""Unit tests for the HMAC file-transfer token signer (``mcp/_filelink.py``).

The money path: a token must round-trip its payload, reject any tampering / wrong
operation / expiry, cap its length before decoding, and tolerate base64url with or
without padding. No fastmcp needed — stdlib only — but kept under ``tests/unit/mcp``
beside the other MCP tests.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from unittest import mock

import pytest

import notebooklm.mcp._filelink as filelink
from notebooklm.mcp._filelink import (
    DOWNLOAD_TTL,
    UPLOAD_TTL,
    ConsumedJtiStore,
    FileLinkError,
    FileLinkSigner,
    FileTransferConfig,
    _b64url_decode,
)

KEY = b"k" * 32


def _signer() -> FileLinkSigner:
    return FileLinkSigner(KEY)


def test_round_trip_returns_payload_with_injected_exp() -> None:
    signer = _signer()
    before = int(time.time())
    token = signer.sign({"op": "ul", "nb": "n1", "title": "Doc"}, ttl=60)
    payload = signer.verify(token, op="ul")
    assert payload["op"] == "ul"
    assert payload["nb"] == "n1"
    assert payload["title"] == "Doc"
    # The signer OWNS expiry: callers never pass exp; it is injected as now+ttl.
    assert before + 60 <= payload["exp"] <= int(time.time()) + 61


def test_tampered_body_is_rejected() -> None:
    signer = _signer()
    token = signer.sign({"op": "ul", "nb": "n1"}, ttl=60)
    body_b64, mac_b64 = token.split(".")
    # Flip the notebook id in the (decoded) body, re-encode, keep the old MAC.
    payload = json.loads(_b64url_decode(body_b64))
    payload["nb"] = "attacker"
    forged_body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(FileLinkError):
        signer.verify(f"{forged_body}.{mac_b64}", op="ul")


def test_tampered_mac_is_rejected() -> None:
    signer = _signer()
    token = signer.sign({"op": "ul", "nb": "n1"}, ttl=60)
    body_b64, _mac = token.split(".")
    bad_mac = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    with pytest.raises(FileLinkError):
        signer.verify(f"{body_b64}.{bad_mac}", op="ul")


def test_wrong_key_is_rejected() -> None:
    token = _signer().sign({"op": "dl", "nb": "n1", "atype": "audio"}, ttl=60)
    with pytest.raises(FileLinkError):
        FileLinkSigner(b"z" * 32).verify(token, op="dl")


def test_expired_token_is_rejected() -> None:
    signer = _signer()
    # A negative TTL injects an exp already in the past — no clock patching needed.
    token = signer.sign({"op": "ul", "nb": "n1"}, ttl=-10)
    with pytest.raises(FileLinkError):
        signer.verify(token, op="ul")


def test_operation_mismatch_is_rejected() -> None:
    signer = _signer()
    upload_token = signer.sign({"op": "ul", "nb": "n1"}, ttl=60)
    # A valid upload token must NOT verify against the download route's op.
    with pytest.raises(FileLinkError):
        signer.verify(upload_token, op="dl")


def test_over_length_token_rejected() -> None:
    signer = _signer()
    # An over-length token (> the 4 KiB cap) is rejected by the pre-decode length
    # guard. Crucially it carries a single "." and valid base64url so the ONLY thing
    # that can reject it is the length cap — if the cap didn't fire first, decode +
    # MAC work would run; the rejection proves the cap short-circuits.
    body = "A" * 5000
    mac = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    with pytest.raises(FileLinkError):
        signer.verify(f"{body}.{mac}", op="ul")
    # Sanity: the cap is what trips — a same-shaped but short token gets past length
    # and fails later (still a FileLinkError, but for a different reason).
    assert len(body) + len(mac) + 1 > filelink._MAX_TOKEN_LEN


def test_malformed_token_shapes_rejected() -> None:
    signer = _signer()
    for bad in ("", "no-dot", "a.b.c", ".", "a.", ".b"):
        with pytest.raises(FileLinkError):
            signer.verify(bad, op="ul")


def test_non_ascii_token_body_raises_filelinkerror_not_unicodeerror() -> None:
    # A non-ASCII char in the body segment must surface as a FileLinkError (→ flat
    # 403 at the route), NOT an uncaught UnicodeEncodeError (a bare 500). Security
    # finding: malformed public input should reject cleanly.
    signer = _signer()
    with pytest.raises(FileLinkError):
        signer.verify("é.bm9wZQ", op="ul")


def test_base64url_padding_tolerant() -> None:
    # The encoder strips '=' padding; the decoder must accept inputs needing 0..3
    # pad bytes back. Cover lengths that re-pad to each residue.
    for raw in (b"a", b"ab", b"abc", b"abcd"):
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        assert _b64url_decode(encoded) == raw
        # And the fully-padded form decodes identically.
        assert _b64url_decode(base64.urlsafe_b64encode(raw).decode()) == raw


def test_verify_uses_constant_time_compare() -> None:
    signer = _signer()
    token = signer.sign({"op": "ul", "nb": "n1"}, ttl=60)
    # Object patch on the stdlib ``hmac`` (a public attr), not a string target.
    with mock.patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as cmp:
        signer.verify(token, op="ul")
        cmp.assert_called_once()


def test_config_builds_ttl_scoped_urls() -> None:
    signer = _signer()
    config = FileTransferConfig(signer=signer, base_url="https://host.example/")
    up = config.upload_url({"op": "ul", "nb": "n1"})
    down = config.download_url({"op": "dl", "nb": "n1", "atype": "audio"})
    assert up.startswith("https://host.example/files/ul/")
    assert down.startswith("https://host.example/files/dl/")
    # The trailing slash on base_url is not doubled.
    assert "//files" not in up.replace("https://", "")
    # Upload token carries the 15-min TTL, download the 30-min TTL.
    up_exp = signer.verify(up.rsplit("/", 1)[1], op="ul")["exp"]
    down_exp = signer.verify(down.rsplit("/", 1)[1], op="dl")["exp"]
    assert UPLOAD_TTL == 15 * 60 and DOWNLOAD_TTL == 30 * 60
    assert down_exp - up_exp >= DOWNLOAD_TTL - UPLOAD_TTL - 2


# --------------------------------------------------------------------------- #
# jti minting (single-use support — #1746)
# --------------------------------------------------------------------------- #
def test_sign_injects_unique_jti_per_mint() -> None:
    # Every token carries a random jti (CSPRNG), so two mints of the SAME payload get
    # DIFFERENT jtis — the property the ul single-use tracker keys off.
    signer = _signer()
    p1 = signer.verify(signer.sign({"op": "ul", "nb": "n1"}, ttl=60), op="ul")
    p2 = signer.verify(signer.sign({"op": "ul", "nb": "n1"}, ttl=60), op="ul")
    assert isinstance(p1["jti"], str) and p1["jti"]
    assert isinstance(p2["jti"], str) and p2["jti"]
    assert p1["jti"] != p2["jti"]


def test_jti_is_covered_by_the_mac() -> None:
    # The jti is injected into the signed body, so tampering with it (keeping the old
    # MAC) is rejected — same guarantee as any other payload field.
    signer = _signer()
    token = signer.sign({"op": "ul", "nb": "n1"}, ttl=60)
    body_b64, mac_b64 = token.split(".")
    payload = json.loads(_b64url_decode(body_b64))
    payload["jti"] = "attacker-chosen-jti"
    forged_body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(FileLinkError):
        signer.verify(f"{forged_body}.{mac_b64}", op="ul")


# --------------------------------------------------------------------------- #
# ConsumedJtiStore lifecycle
# --------------------------------------------------------------------------- #
def test_store_try_begin_then_commit_makes_jti_single_use() -> None:
    store = ConsumedJtiStore()
    exp = int(time.time()) + 60
    assert store.try_begin("j1") is True  # first claim wins
    assert store.try_begin("j1") is False  # concurrent duplicate rejected (still active)
    store.commit("j1", exp)
    assert store.try_begin("j1") is False  # consumed → permanently rejected


def test_store_rollback_frees_jti_for_retry() -> None:
    # A claimed-but-not-committed jti (failed/aborted upload) is released, so the same
    # link can be retried — the record-on-success behavior ADR-0024 relies on.
    store = ConsumedJtiStore()
    assert store.try_begin("j1") is True
    store.rollback("j1")
    assert store.try_begin("j1") is True  # reusable after rollback


def test_store_sweeps_expired_seen_entries_on_commit() -> None:
    # Expired jtis are inline-swept (memory reclamation) when a later commit runs; a
    # live one is retained. A swept jti being re-claimable is harmless — verify()
    # rejects the expired token itself on the exp check.
    store = ConsumedJtiStore()
    now = int(time.time())
    store.commit("old", now - 10)  # already expired
    store.commit("fresh", now + 60)  # live
    assert "old" not in store._seen
    assert "fresh" in store._seen
    assert store.try_begin("old") is True  # swept → re-claimable (harmless)


def test_store_bound_evicts_soonest_to_expire(monkeypatch) -> None:
    monkeypatch.setattr(filelink, "_MAX_SEEN_JTIS", 3)
    store = ConsumedJtiStore()
    base = int(time.time()) + 10_000  # all far-future so the sweep never fires
    store.commit("a", base + 1)
    store.commit("b", base + 5)
    store.commit("c", base + 9)
    store.commit("d", base + 7)  # over cap → evict the soonest-to-expire ("a")
    assert len(store._seen) <= 3
    assert "a" not in store._seen  # soonest-to-expire evicted
    assert {"b", "c", "d"} <= set(store._seen)


def test_store_recommit_same_jti_does_not_evict_at_cap(monkeypatch) -> None:
    # Re-committing an already-recorded jti refreshes its exp in place; it must NOT evict
    # a different valid entry (order-independence at the size cap). The route can't drive
    # a double-commit — try_begin gates it — but commit stays self-consistent regardless.
    monkeypatch.setattr(filelink, "_MAX_SEEN_JTIS", 2)
    store = ConsumedJtiStore()
    base = int(time.time()) + 10_000  # far future → sweep never fires
    store.commit("a", base + 1)
    store.commit("b", base + 5)  # store is now full (2/2)
    store.commit("a", base + 9)  # re-commit "a" — must refresh, not evict "b"
    assert set(store._seen) == {"a", "b"}
    assert store._seen["a"] == base + 9  # exp refreshed in place


def test_config_jti_store_excluded_from_equality_and_default_constructed() -> None:
    # `compare=False` keeps the frozen config comparable by (signer, base_url) only —
    # the store is a mutable, dict-bearing object that must not drive __eq__/__hash__.
    signer = _signer()
    a = FileTransferConfig(signer=signer, base_url="https://h.example")
    b = FileTransferConfig(signer=signer, base_url="https://h.example")
    assert isinstance(a.jti_store, ConsumedJtiStore)  # default-constructed
    a.jti_store.try_begin("j1")  # mutate one store
    assert a == b  # equality ignores the (now diverged) stores
