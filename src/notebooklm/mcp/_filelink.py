"""HMAC-signed, self-describing file-transfer tokens for the MCP side-channel.

The remote (HTTP) MCP transport brokers short-lived **signed URLs** so the
claude.ai connector's browser can upload a local binary or download an artifact
*outside* the JSON-RPC channel (ADR-0024). The token **encodes the operation
parameters**, so the ``/files/*`` route handlers hold no server-side state — the
token is the state. No ref-registry, no TTL sweeper.

Token wire format::

    base64url(json(payload)) + "." + base64url(HMAC-SHA256(key, body))

where ``body`` is the base64url(json(payload)) string the MAC is computed over.
Stdlib ``hmac``/``hashlib``/``base64``/``json`` only — no new dependency
(``itsdangerous`` is not installed). :meth:`FileLinkSigner.verify` enforces a max
token length **before** any decode/HMAC work, re-pads base64url, recomputes the
MAC in constant time (:func:`hmac.compare_digest`), checks ``exp``, and matches
the operation. The signing key is an ephemeral ``secrets.token_bytes(32)`` minted
at server start (a restart invalidating outstanding links is acceptable and
removes a secret to manage).

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
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

        ``exp`` is injected here (callers never set it). The MAC covers the
        encoded body, so neither the parameters nor the expiry are tamperable.
        """
        body = dict(payload)
        body["exp"] = int(time.time()) + ttl
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


@dataclass(frozen=True)
class FileTransferConfig:
    """Resolved file-transfer config: the signer + the validated public base URL.

    Carried on :class:`~notebooklm.mcp._context.AppState`. The two file tools mint
    URLs through :meth:`upload_url` / :meth:`download_url`; the ``/files/*`` routes
    verify the tokens with the same :attr:`signer`. ``base_url`` is a bare https
    origin (validated by ``_validate_bare_https_origin``).
    """

    signer: FileLinkSigner
    base_url: str

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
