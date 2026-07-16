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
    "WIDGET_UPLOAD_TTL",
    "ConsumedJtiStore",
    "FileLinkError",
    "FileLinkSigner",
    "FileTransferConfig",
    "ShortLinkStore",
]

#: Signed-URL lifetimes. Upload links are shorter-lived than downloads: an upload
#: link grants WRITE (add a source) so its window is tighter; a download link only
#: streams one artifact. Both are bounded so a token leaked via tunnel logs /
#: browser history / a ``Referer`` is useful only briefly (the HTML pages also send
#: ``Referrer-Policy: no-referrer``).
UPLOAD_TTL = 15 * 60
DOWNLOAD_TTL = 30 * 60

#: Lifetime of a token in the in-app upload **widget's** pool (ADR-0027). Longer than
#: the single-link ``UPLOAD_TTL`` because the widget uploads a whole *batch*
#: sequentially through one browser: it mints the pool up front (all tokens share this
#: one mint instant), then uploads file[0], file[1], … one after another, so the LAST
#: token must still be live after the sum of every earlier file's transfer plus the
#: user's file-picking think-time. At the 15-min link TTL a slow mobile link could
#: expire a later token mid-batch → a silent 403, the file lost (#1894). An hour
#: comfortably covers a realistic mobile batch of documents/photos.
#:
#: The longer window is a different — and smaller — risk class than the human_upload
#: link's: a pool token is (a) single-use (it authorizes exactly one *successful* add,
#: burned via :class:`ConsumedJtiStore`), (b) notebook-scoped, and (c) never shown to
#: the user or placed in a URL bar — it lives only inside the widget's
#: ``structuredContent`` / ``fetch`` body, never a location bar, history entry, or
#: ``Referer``. The tight ``UPLOAD_TTL`` guards the *human* link the user opens in a
#: browser (a genuinely higher-exposure surface); the widget pool never enters it.
WIDGET_UPLOAD_TTL = 60 * 60

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

    def verify(self, token: str, *, op: str, allow_expired: bool = False) -> dict[str, Any]:
        """Verify ``token`` and return its payload, or raise :class:`FileLinkError`.

        Order matters: the length cap runs BEFORE any decode, then the MAC is
        recomputed over the *received* body string and compared in constant time
        BEFORE the body JSON is decoded (so a forged token never reaches the JSON
        parser with attacker-chosen bytes). Finally ``exp`` and ``op`` are checked.

        Args:
            op: The operation the route serves (``"ul"`` / ``"dl"``). A token
                minted for the other operation is rejected (an upload link cannot
                be replayed against the download route or vice-versa).
            allow_expired: Skip ONLY the ``exp`` check (MAC + shape + ``op`` are still
                enforced). Used exclusively to recover a *committed* upload's result
                when ``await_upload`` is re-invoked just after the start-token expired
                — the ``/files/ul`` POST already verified the token while it was live,
                so honoring the result is safe. NEVER use this to authorize a new write.
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
        if not isinstance(exp, int) or isinstance(exp, bool):
            raise FileLinkError("token expired")
        if not allow_expired and time.time() > exp:
            raise FileLinkError("token expired")
        if payload.get("op") != op:
            raise FileLinkError("operation mismatch")
        return payload


#: Cap on the consumed-jti record. The server mints one jti per file-tool call and
#: every entry is inline-swept once its token's ``exp`` passes (≤ the token's TTL —
#: ``UPLOAD_TTL``, or ``WIDGET_UPLOAD_TTL`` for a widget-pool token), so
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
    #: jti -> upload result ({source_id, name, size, mime}), recorded on a successful
    #: :meth:`commit` that carries one. This is the in-process **completion map**
    #: (Phase 1 ``await_upload``): the ``/files/ul`` POST route and the polling tool run
    #: in the same single process (ADR-0024), so a same-loop poll reads what the route
    #: wrote — no DB, no cross-process state. Keyed by jti and swept with :attr:`_seen`,
    #: so a record never outlives its token's ``exp`` (≤ that token's TTL — ``UPLOAD_TTL``,
    #: or ``WIDGET_UPLOAD_TTL`` for a widget-pool token).
    _results: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _sweep(self, now: int) -> None:
        # ``exp < now`` (strict), matching ``verify``'s ``time.time() > exp`` rejection:
        # a jti is dropped only once its token is already expired *and* rejectable, never
        # while ``verify`` would still accept the token — so the sweep opens no re-claim
        # window at the exact expiry second. ``now`` is the caller's ``int(time.time())``.
        for expired_jti in [j for j, exp in self._seen.items() if exp < now]:
            del self._seen[expired_jti]
            self._results.pop(expired_jti, None)

    def completed(self, jti: str) -> dict[str, Any] | None:
        """Return the recorded upload result for ``jti``, or ``None`` if not (yet)
        completed. ``None`` covers both "not uploaded yet" and "burned without a
        result"; the caller treats either as *pending*."""
        return self._results.get(jti)

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

    def commit(self, jti: str, exp: int, result: dict[str, Any] | None = None) -> None:
        """Burn ``jti`` permanently (its upload succeeded).

        When ``result`` is given it is recorded in the completion map (:meth:`completed`)
        so a same-process ``await_upload`` poll can surface the added source. Sweeps
        expired entries and enforces the size bound here — the only mutating hot path —
        so :meth:`try_begin` stays O(1).
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
            evicted = min(self._seen, key=self._seen.__getitem__)
            del self._seen[evicted]
            self._results.pop(evicted, None)  # keep the completion map in step with _seen
        self._seen[jti] = exp
        if result is not None:
            self._results[jti] = result

    def rollback(self, jti: str) -> None:
        """Release a claimed-but-unfinished ``jti`` (upload failed / aborted / 429) so
        the same link can be retried — honors ADR-0024's large-file retry window."""
        self._active.discard(jti)


#: Short-id length in random bytes. ``token_urlsafe(12)`` → 16 URL-safe chars (96 bits) —
#: still short enough to survive a mobile tap / model transcription, but wide enough that a
#: ``/u/<shortid>`` link (unauthenticated, resolves to a valid write-capable upload token for
#: its ≤15-min TTL, and the 302/404 route is an online oracle with no per-id rate limit here)
#: cannot be feasibly guessed. 48 bits was defensible but this keeps a comfortable margin.
_SHORT_ID_BYTES = 12
#: Bound mirrors ``_MAX_SEEN_JTIS`` — one entry per file-tool call, all TTL-swept.
_MAX_SHORT_LINKS = 8192


@dataclass
class ShortLinkStore:
    """In-process ``shortid -> (token, exp)`` map backing the tap-friendly ``/u/<shortid>``
    upload links.

    The long ``/files/ul/<token>`` URL (~250 chars) is fragile through a mobile chat: it gets
    tap-truncated, re-typed with dropped characters by the model, or autocorrected — every
    corruption breaks the HMAC (live-confirmed). A short random id sidesteps all of that; this
    store resolves it back to the real signed token server-side.

    Same contract as :class:`ConsumedJtiStore`: single process / single tenant, every method is
    synchronous (no ``await`` → atomic on the one event loop, no lock), TTL-swept, dies with the
    process. No DB, no background sweeper.
    """

    #: shortid -> (signed token, exp unix seconds). exp mirrors the token's own ``exp`` so a
    #: resolved link never outlives the token it points at.
    _links: dict[str, tuple[str, int]] = field(default_factory=dict)

    def _sweep(self, now: int) -> None:
        for sid in [s for s, (_t, exp) in self._links.items() if exp < now]:
            del self._links[sid]

    def put(self, token: str, exp: int) -> str:
        """Register ``token`` (valid until ``exp``) under a fresh short id and return the id.

        Sweeps expired entries and enforces the size bound here (the only growing path), so
        :meth:`get` stays O(1)."""
        now = int(time.time())
        self._sweep(now)
        if len(self._links) >= _MAX_SHORT_LINKS:
            # Evict the soonest-to-expire entry (least loss — closest to natural expiry).
            del self._links[min(self._links, key=lambda s: self._links[s][1])]
        shortid = secrets.token_urlsafe(_SHORT_ID_BYTES)
        self._links[shortid] = (token, exp)
        return shortid

    def get(self, shortid: str) -> str | None:
        """Return the token for ``shortid``, or ``None`` if unknown or its token has expired
        (an expired entry is dropped in passing)."""
        entry = self._links.get(shortid)
        if entry is None:
            return None
        token, exp = entry
        if int(time.time()) > exp:
            self._links.pop(shortid, None)
            return None
        return token


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
    #: In-process ``shortid -> token`` map backing the ``/u/<shortid>`` tap-friendly links
    #: (:meth:`short_upload_url`). ``compare=False`` for the same reason as :attr:`jti_store`.
    short_links: ShortLinkStore = field(default_factory=ShortLinkStore, compare=False)

    def short_upload_url(self, payload: dict[str, Any]) -> str:
        """Mint an upload token for ``payload`` and return a tap-friendly ``/u/<shortid>`` URL.

        Signs the same ``ul`` token :meth:`upload_url` would, registers it under a short id
        (keyed to the token's ``exp``), and returns the short URL — the long ``/files/ul``
        URL a ``GET /u/<shortid>`` redirects to. Robust to mobile-chat corruption (see
        :class:`ShortLinkStore`)."""
        token = self.signer.sign({**payload, "op": "ul"}, UPLOAD_TTL)
        # ``sign`` stamped ``exp = now + UPLOAD_TTL``; recompute it directly rather than
        # re-verifying the token (a full HMAC+decode) just to read one field back. This
        # ``now`` is a hair later, so the store entry expires at-or-after the token — never
        # before it (a short link is never live past its token). Negligible on a 15-min TTL.
        exp = int(time.time()) + UPLOAD_TTL
        shortid = self.short_links.put(token, exp)
        return f"{self.base_url.rstrip('/')}/u/{shortid}"

    def upload_url(self, payload: dict[str, Any], *, ttl: int = UPLOAD_TTL) -> str:
        """Sign ``payload`` with the upload TTL and build the ``/files/ul`` URL.

        The builder OWNS the ``op`` claim (stamps ``"ul"``) so the token always
        matches the route it is minted for — a caller cannot accidentally produce
        a token the route would 403.

        ``ttl`` defaults to the single-link :data:`UPLOAD_TTL`; the in-app upload
        widget passes the longer :data:`WIDGET_UPLOAD_TTL` for its sequentially-uploaded
        token pool (ADR-0027). The route enforces single-use regardless of TTL.
        """
        return self._build("ul", self.signer.sign({**payload, "op": "ul"}, ttl))

    def download_url(self, payload: dict[str, Any]) -> str:
        """Sign ``payload`` with the download TTL and build the ``/files/dl`` URL.

        The builder OWNS the ``op`` claim (stamps ``"dl"``) — see :meth:`upload_url`.
        """
        return self._build("dl", self.signer.sign({**payload, "op": "dl"}, DOWNLOAD_TTL))

    def _build(self, kind: str, token: str) -> str:
        return f"{self.base_url.rstrip('/')}/files/{kind}/{token}"
