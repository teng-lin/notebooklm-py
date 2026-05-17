#!/usr/bin/env python3
"""Re-encode a recorded cassette so its response body is gzip-compressed
and the response advertises ``Content-Encoding: gzip``.

Why this exists
---------------
``decode_compressed_response=True`` in :mod:`tests.vcr_config` strips
``Content-Encoding`` from the headers on record (after VCR transparently
gunzips the body), so 89/89 cassettes in ``tests/cassettes/`` carry NO
encoding header and a plaintext body. That hid issue #769 — every RPC
failed in production with ``Error -3 while decompressing data: incorrect
header check`` because :func:`notebooklm._core_transport._stream_post_with_size_cap`
rebuilt the response with the upstream ``Content-Encoding: gzip`` header
attached to *already-decoded* body bytes, triggering a second gzip-decode
inside :class:`httpx.Response.__init__`. Cassettes that don't carry the
header on replay simply skip that branch and pass.

This script ports a cassette to the production wire shape: gzip the
response body, restore the ``Content-Encoding: gzip`` header, and strip
the now-meaningless ``Transfer-Encoding`` and ``Content-Length`` headers
(the originals described the chunked / decoded wire form, not the gzipped
buffer we hand to vcrpy on replay).

Run from the repo root::

    uv run python tests/scripts/inject_gzip_into_cassette.py \\
        tests/cassettes/artifacts_revise_slide.yaml \\
        tests/cassettes/gzip_coverage/artifacts_revise_slide_gzipped.yaml

Idempotent: re-running on a cassette that already advertises
``Content-Encoding: gzip`` rewrites it from scratch using the gzipped
body that vcrpy has already deserialized to ``bytes``, so a second pass
produces a byte-identical file.

Library use:
    Other tooling can ``from tests.scripts.inject_gzip_into_cassette
    import inject_gzip_into_cassette`` and pass a cassette dict in
    memory.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path
from typing import Any

import yaml

# Use the SAFE loader: cassettes are arbitrary YAML on disk and a malicious
# ``!!python/object/apply`` tag would execute code under the full Loader.
# ``!!binary`` (the encoding we rely on for gzipped bodies) is part of the
# core YAML schema so it works under ``CSafeLoader`` / ``SafeLoader``.
try:
    from yaml import CSafeDumper as Dumper
    from yaml import CSafeLoader as Loader
except ImportError:  # pragma: no cover — libyaml is bundled with PyYAML wheels
    from yaml import SafeDumper as Dumper  # type: ignore[assignment]
    from yaml import SafeLoader as Loader


# Headers stripped from the response when we rewrite the body. The recorded
# Transfer-Encoding / Content-Length values described the chunked / decoded
# wire form; once we re-gzip the body, neither matches reality and leaving
# them in confuses httpx on replay. Content-Encoding is included so the
# rewrite always re-adds the canonical-cased header regardless of how the
# cassette was originally recorded.
_STRIP_RESPONSE_HEADERS = frozenset(
    {
        "transfer-encoding",
        "content-length",
        "content-encoding",
    }
)

_CONTENT_ENCODING_HEADER = "Content-Encoding"


def _has_content_encoding(headers: dict[str, list[str]]) -> bool:
    """Case-insensitive check for ``Content-Encoding`` in a cassette
    header dict."""
    return any(k.lower() == "content-encoding" for k in headers)


def _coerce_body_to_bytes(body_string: Any) -> bytes:
    """Normalize the recorded ``response.body.string`` to ``bytes``.

    PyYAML deserializes ``!!binary`` blobs as :class:`bytes` and plain
    block scalars as :class:`str`. The cassette format permits either,
    so accept both and return raw bytes.
    """
    if isinstance(body_string, bytes):
        return body_string
    if isinstance(body_string, str):
        return body_string.encode("utf-8")
    raise TypeError(
        f"Unexpected response.body.string type {type(body_string).__name__}; expected bytes or str."
    )


def _inject_into_response(response: dict[str, Any]) -> bool:
    """Mutate ``response`` in place so the body is gzip-compressed and
    ``Content-Encoding: gzip`` is set.

    Returns ``True`` when the body was (re)written; ``False`` only when
    the response has no ``body.string`` to process (missing key or
    ``None``). A response that already advertises ``Content-Encoding``
    is gunzipped first and then re-gzipped from the decoded form so the
    rewrite is idempotent — still a rewrite, still ``True``.
    """
    body = response.get("body") or {}
    if "string" not in body:
        return False

    raw = body["string"]
    if raw is None:
        return False

    body_bytes = _coerce_body_to_bytes(raw)

    headers = response.setdefault("headers", {})
    already_encoded = _has_content_encoding(headers)

    if already_encoded:
        # The body on disk is already gzipped — gunzip first so the
        # rewrite produces a byte-identical file on a second pass.
        try:
            body_bytes = gzip.decompress(body_bytes)
        except OSError as exc:  # pragma: no cover — defensive
            raise ValueError(
                "Response already advertises Content-Encoding but body is not valid gzip."
            ) from exc

    # ``gzip.compress`` writes one byte that varies across Python releases
    # (the OS field at offset 9): 3.10 picks an OS-dependent constant
    # while 3.11+ pins it to ``0xff`` (unknown). Pin ``mtime=0`` and
    # overwrite the OS field so the same source cassette produces a
    # byte-identical artifact under every Python in the CI matrix
    # (``3.10`` – ``3.14``). ``0xff`` is the documented "unknown" value
    # from RFC 1952 §2.3.1 and round-trips through every gzip decoder.
    gzipped = bytearray(gzip.compress(body_bytes, mtime=0))
    gzipped[9] = 0xFF
    body["string"] = bytes(gzipped)

    # Drop any case-variant of the headers in _STRIP_RESPONSE_HEADERS
    # (including Content-Encoding) so the rewrite re-adds the canonical
    # header name below.
    for key in list(headers):
        if key.lower() in _STRIP_RESPONSE_HEADERS:
            del headers[key]
    headers[_CONTENT_ENCODING_HEADER] = ["gzip"]

    return True


def inject_gzip_into_cassette(cassette: dict[str, Any]) -> int:
    """Rewrite every batchexecute-style response in ``cassette`` to be
    gzip-encoded.

    Only responses whose request is a POST to a path containing
    ``batchexecute`` are touched — those are the ones that flow through
    :func:`notebooklm._core_transport._stream_post_with_size_cap` and
    exercise the rebuild step that bit #769. SPA-shell GET responses are
    left alone so the cassette diff stays focused.

    Returns the number of responses that were rewritten.
    """
    rewritten = 0
    for interaction in cassette.get("interactions", []) or []:
        request = interaction.get("request") or {}
        if request.get("method", "").upper() != "POST":
            continue
        uri = request.get("uri", "") or ""
        if "batchexecute" not in uri:
            continue
        response = interaction.get("response") or {}
        if _inject_into_response(response):
            rewritten += 1
    return rewritten


def _load_cassette(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.load(fh, Loader=Loader)


def _dump_cassette(cassette: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(cassette, fh, Dumper=Dumper)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="cassette to read")
    parser.add_argument(
        "destination",
        type=Path,
        help="destination path (may equal source for in-place rewrite)",
    )
    args = parser.parse_args(argv)

    cassette = _load_cassette(args.source)
    rewritten = inject_gzip_into_cassette(cassette)
    if rewritten == 0:
        print(
            f"warning: no batchexecute POST interactions found in {args.source}",
            file=sys.stderr,
        )
        return 1

    _dump_cassette(cassette, args.destination)
    print(f"rewrote {rewritten} response(s) in {args.destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
