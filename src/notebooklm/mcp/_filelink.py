"""HMAC-signed, self-describing file-transfer tokens for the MCP side-channel.

The remote (HTTP) MCP transport brokers short-lived **signed URLs** so the
claude.ai connector's browser can upload a local binary or download an artifact
*outside* the JSON-RPC channel (ADR-0024). The token **encodes the operation
parameters**, so the ``/files/*`` route handlers hold no server-side state — the
token is the state. No ref-registry, no TTL sweeper.

Token wire format::

    base64url(json(payload)) + "." + base64url(HMAC-SHA256(key, body))

where ``body`` is the base64url(json(payload)) string the MAC is computed over.
Stdlib ``hmac``/``hashlib``/``base64``/``json``/``secrets`` only — no new dependency
(``itsdangerous`` is not installed). :meth:`FileLinkSigner.verify` enforces a max
token length **before** any decode/HMAC work, re-pads base64url, recomputes the
MAC in constant time (:func:`hmac.compare_digest`), checks ``exp``, and matches
the operation. The signing key is an ephemeral ``secrets.token_bytes(32)`` minted
at server start (a restart invalidating outstanding links is acceptable and
removes a secret to manage).

Every minted token also carries a random ``jti`` (like ``exp``, injected by
:meth:`FileLinkSigner.sign` and covered by the MAC). ``jti`` powers a **scoped
single-use** guarantee for the ``ul`` (upload) op: the ``/files/ul`` POST route
claims + burns the jti via :class:`ConsumedJtiStore` so a leaked upload link
cannot be replayed as a content-injection write primitive (ADR-0024, #1746). The
store is an ephemeral, bounded, inline-swept in-process set that dies with the
process just like the signing key — there is no *background* sweeper. Downloads
(``dl``) stay multi-use so ``Range``/resumable clients keep working; their jti is
present but not enforced.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DOWNLOAD_TTL",
    "UPLOAD_TTL",
    "FileLinkError",
    "FileLinkSigner",
    "FileTransferConfig",
]

#: Signed-URL lifetimes. Upload links are shorter-lived than downloads: an upload
#: link grants WRITE (add a source) so its window is tighter; a download link only
#: streams one artifact. Both are bounded so a token leaked via tunnel logs /
#: browser history / a ``Referer`` is useful only briefly (the HTML pages also send
#: ``Referrer-Policy: no-referrer``).
UPLOAD_TTL = 15 * 60
DOWNLOAD_TTL = 30 * 60

#: Reject a token longer than this BEFORE any base64/HMAC/JSON work — an absurdly
#: long path segment must not drive decode/allocation cost. Real tokens are well
#: under 1 KiB; 4 KiB is generous headroom.
_MAX_TOKEN_LEN = 4096


class FileLinkError(Exception):
    """A token failed verification (over-length, bad MAC, expired, malformed, or
    operation mismatch). Carries no detail the handler echoes to the client — the
    routes return a flat 403 so a probe learns nothing about *why* it failed."""


def _b64url(raw: bytes) -> str:
    """URL-safe base64 without ``=`` padding (kept out of the URL path segment)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Decode an unpadded URL-safe base64 string (re-padding to a multiple of 4).

    Raises:
        FileLinkError: the input is not valid base64url.
    """
    pad = -len(value) % 4
    try:
        return base64.urlsafe_b64decode(value + ("=" * pad))
    except (binascii.Error, ValueError) as exc:  # malformed alphabet / length
        raise FileLinkError("malformed token encoding") from exc


@dataclass(frozen=True)
class FileLinkSigner:
    """Sign / verify the self-describing file-transfer tokens.

    The signer **owns expiry**: :meth:`sign` injects ``exp = now + ttl`` into the
    payload, so callers pass ``{op, nb, …}`` WITHOUT ``exp``.
    """

    #: ``repr=False`` keeps the raw HMAC key out of any ``repr()`` — so a future
    #: ``logger.debug(config)`` can never leak it to stderr (mirrors the OAuth
    #: password at ``_oauth.py``).
    key: bytes = field(repr=False)

    def sign(self, payload: dict[str, Any], ttl: int) -> str:
        """Return a signed token for ``payload`` valid for ``ttl`` seconds.

        ``exp`` and a random ``jti`` are injected here (callers never set either).
        The MAC covers the encoded body, so neither the parameters, the expiry, nor
        the jti are tamperable. The ``jti`` (128-bit CSPRNG, ``secrets``) uniquely
        names the token so the ``ul`` route can enforce single-use
        (:class:`ConsumedJtiStore`); it is injected for every op uniformly, but only
        ``ul`` enforces it (``dl`` stays multi-use for Range/resume).
        """
        body = dict(payload)
        body["exp"] = int(time.time()) + ttl
        body["jti"] = secrets.token_urlsafe(16)
        encoded = _b64url(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        mac = hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{_b64url(mac)}"

    def verify(self, token: str, *, op: str) -> dict[str, Any]:
        """Verify ``token`` and return its payload, or raise :class:`FileLinkError`.

        Order matters: the length cap runs BEFORE any decode, then the MAC is
        recomputed over the *received* body string and compared in constant time
        BEFORE the body JSON is decoded (so a forged token never reaches the JSON
        parser with attacker-chosen bytes). Finally ``exp`` and ``op`` are checked.

        Args:
            op: The operation the route serves (``"ul"`` / ``"dl"``). A token
                minted for the other operation is rejected (an upload link cannot
                be replayed against the download route or vice-versa).
        """
        if len(token) > _MAX_TOKEN_LEN:
            raise FileLinkError("token too long")
        parts = token.split(".")
        if len(parts) != 2:
            raise FileLinkError("malformed token")
        encoded, mac_b64 = parts
        if not encoded or not mac_b64:
            raise FileLinkError("malformed token")
        # A real body segment is base64url (ASCII). A non-ASCII char makes
        # ``.encode("ascii")`` raise — treat it as a malformed token (flat 403),
        # not an uncaught ``UnicodeEncodeError`` (a bare 500).
        try:
            encoded_ascii = encoded.encode("ascii")
        except UnicodeEncodeError as exc:
            raise FileLinkError("malformed token") from exc
        expected = hmac.new(self.key, encoded_ascii, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(mac_b64)):
            raise FileLinkError("bad signature")
        try:
            payload = json.loads(_b64url_decode(encoded))
        except (ValueError, TypeError) as exc:
            raise FileLinkError("malformed token body") from exc
        if not isinstance(payload, dict):
            raise FileLinkError("malformed token body")
        exp = payload.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool) or time.time() > exp:
            raise FileLinkError("token expired")
        if payload.get("op") != op:
            raise FileLinkError("operation mismatch")
        return payload


#: Cap on the consumed-jti record. The server mints one jti per file-tool call and
#: every entry is inline-swept once its token's ``exp`` passes (≤ ``UPLOAD_TTL``), so
#: the live set is tiny in practice; an attacker cannot mint jtis (no key). This bound
#: is pure defense-in-depth against a runaway — 8192 is generous headroom.
_MAX_SEEN_JTIS = 8192


@dataclass
class ConsumedJtiStore:
    """Bounded, TTL-swept record of consumed single-use upload token ids (``jti``),
    plus the set of ids currently mid-upload.

    Single process / single tenant: every method is fully synchronous and contains NO
    ``await``, so it runs atomically w.r.t. other coroutines on the one server event
    loop (no coroutine interleaves mid-method) — the same rule the ``_fileroutes``
    in-flight counters rely on, so no lock is needed. The store dies with the process,
    like the ephemeral signing key.

    Lifecycle per upload: :meth:`try_begin` (atomic claim) → :meth:`commit` (success,
    permanent) OR :meth:`rollback` (failure / abort / 429, freeing the jti for retry).
    ``try_begin`` being the single atomic check-and-mark closes the concurrent-replay
    race; ``commit`` / ``rollback`` are driven from the route's ``finally`` so a client
    disconnect (``CancelledError`` — which ``finally`` still runs on) can never wedge a
    jti in the active set.
    """

    #: jti -> exp (unix seconds); a permanently-burned single-use upload token.
    _seen: dict[str, int] = field(default_factory=dict)
    #: jtis currently mid-upload (claimed, not yet committed/rolled back). Bounded by
    #: the concurrent request count and always released in the route's ``finally``, so
    #: it needs no sweep or size cap.
    _active: set[str] = field(default_factory=set)

    def _sweep(self, now: int) -> None:
        # ``exp < now`` (strict), matching ``verify``'s ``time.time() > exp`` rejection:
        # a jti is dropped only once its token is already expired *and* rejectable, never
        # while ``verify`` would still accept the token — so the sweep opens no re-claim
        # window at the exact expiry second. ``now`` is the caller's ``int(time.time())``.
        for expired_jti in [j for j, exp in self._seen.items() if exp < now]:
            del self._seen[expired_jti]

    def try_begin(self, jti: str) -> bool:
        """Atomically claim ``jti`` for a single upload.

        Return ``False`` if the jti was already consumed (:attr:`_seen`) or is already
        mid-upload (:attr:`_active`) — a sequential replay or a concurrent duplicate;
        otherwise mark it active and return ``True``. No ``await`` runs between the
        check and the mark, so on the single server event loop the claim is atomic.
        """
        if jti in self._active or jti in self._seen:
            return False
        self._active.add(jti)
        return True

    def commit(self, jti: str, exp: int) -> None:
        """Burn ``jti`` permanently (its upload succeeded).

        Sweeps expired entries and enforces the size bound here — the only mutating hot
        path — so :meth:`try_begin` stays O(1).
        """
        self._active.discard(jti)
        self._sweep(int(time.time()))
        if jti not in self._seen and len(self._seen) >= _MAX_SEEN_JTIS:
            # Only evict when this commit actually GROWS the set (jti is new). Re-committing
            # an already-recorded jti just refreshes its exp in place, so it must not evict
            # a different valid entry and shrink the protection window. Practically
            # unreachable (single-tenant; one jti per file-tool call; all TTL-swept). Evict
            # the soonest-to-expire entry — the least loss of protection, since it is
            # closest to natural expiry anyway. ``_seen`` is non-empty here, so ``min`` is
            # safe.
            del self._seen[min(self._seen, key=self._seen.__getitem__)]
        self._seen[jti] = exp

    def rollback(self, jti: str) -> None:
        """Release a claimed-but-unfinished ``jti`` (upload failed / aborted / 429) so
        the same link can be retried — honors ADR-0024's large-file retry window."""
        self._active.discard(jti)


@dataclass(frozen=True)
class FileTransferConfig:
    """Resolved file-transfer config: the signer + the validated public base URL.

    Carried on :class:`~notebooklm.mcp._context.AppState`. The two file tools mint
    URLs through :meth:`upload_url` / :meth:`download_url`; the ``/files/*`` routes
    verify the tokens with the same :attr:`signer` and enforce ``ul`` single-use via
    :attr:`jti_store`. ``base_url`` is a bare https origin (validated by
    ``_validate_bare_https_origin``).
    """

    signer: FileLinkSigner
    base_url: str
    #: In-process single-use tracker for ``ul`` tokens. ``compare=False`` keeps this
    #: frozen dataclass hashable/comparable by (signer, base_url) only — the store is a
    #: mutable, dict-bearing (unhashable) object, so including it in ``__eq__``/
    #: ``__hash__`` would make every config unhashable and equality depend on live state.
    jti_store: ConsumedJtiStore = field(default_factory=ConsumedJtiStore, compare=False)

    def upload_url(self, payload: dict[str, Any]) -> str:
        """Sign ``payload`` with the upload TTL and build the ``/files/ul`` URL.

        The builder OWNS the ``op`` claim (stamps ``"ul"``) so the token always
        matches the route it is minted for — a caller cannot accidentally produce
        a token the route would 403.
        """
        return self._build("ul", self.signer.sign({**payload, "op": "ul"}, UPLOAD_TTL))

    def download_url(self, payload: dict[str, Any]) -> str:
        """Sign ``payload`` with the download TTL and build the ``/files/dl`` URL.

        The builder OWNS the ``op`` claim (stamps ``"dl"``) — see :meth:`upload_url`.
        """
        return self._build("dl", self.signer.sign({**payload, "op": "dl"}, DOWNLOAD_TTL))

    def _build(self, kind: str, token: str) -> str:
        return f"{self.base_url.rstrip('/')}/files/{kind}/{token}"
