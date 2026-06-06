"""Independent cassette *projection* helper for cli_vcr depth-2 assertions (#1452).

Why this module exists — and the hard independence rule
-------------------------------------------------------
The cli_vcr tests run the real CLI -> Client -> RPC path and replay HTTP from a
recorded cassette. A depth-1 assertion checks the ``--json`` envelope *shape*;
this module enables a depth-2 assertion that checks the *values* the CLI emitted
against the values that actually sit in the recorded RPC response — catching
fabrication (an id the CLI invented), a drop (a recorded row the CLI lost), a
duplicate, or a miscount.

To be a real oracle, that comparison must come from an **independent** reading of
the cassette. If this helper imported the production decoder
(``notebooklm.rpc.decoder`` / ``notebooklm._row_adapters`` / ``notebooklm._types``)
the assertion would be a tautology: "the CLI's decode equals the decoder's
decode" proves nothing. So this module is built on **stdlib + ``yaml`` only** and
re-implements just enough of the batchexecute envelope walk (copied as our own
code from the patterns in ``tests/_guardrails/test_cassette_shapes.py`` — not
imported) to reach the payload.

The projection is deliberately a **coarse, shallow** read. It is a *projection
layer*, not a second decoder: it pulls the handful of well-known leaf tokens
(ids, urls, top-level type/status codes) by a simple top-level walk and never
replicates the adapters' deep positional field-mapping. Catching field-*position*
confusion is a separate (adapter-level) concern; this layer catches
fabrication / drop / duplicate / miscount, which a shallow read suffices for. If
this file ever grows the adapters' field constants and object construction it has
crossed into circularity — keep it shallow.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

# Cassettes live at ``tests/cassettes`` — three parents up from this file
# (``tests/integration/cli_vcr/_cassette_expectations.py``).
_CASSETTE_DIR = Path(__file__).resolve().parents[2] / "cassettes"

# Google's anti-XSSI guard prefixes every batchexecute response body. Copied
# (not imported) from ``tests/_guardrails/test_cassette_shapes.py``.
_XSSI_PREFIX = ")]}'"


def _strip_xssi(body: str) -> str:
    """Drop the Google anti-XSSI ``)]}'`` prefix and the blank line after it."""
    if body.startswith(_XSSI_PREFIX):
        return body[len(_XSSI_PREFIX) :].lstrip("\n")
    return body


def _rpcids_from_uri(uri: str) -> list[str]:
    """Return the rpcids named in a request URL's ``rpcids=`` query param."""
    raw = parse_qs(urlparse(uri).query).get("rpcids", [""])[0]
    return [p for p in raw.split(",") if p]


def _wrb_payloads(body: str, rpc_id: str) -> list[str]:
    """Return the raw slot-[2] JSON *strings* of every ``wrb.fr`` envelope for ``rpc_id``.

    Walks the chunked batchexecute frames (alternating ``<int-byte-count>\\n``
    ``<json-line>\\n`` records, with the count line occasionally omitted for
    trivial bodies), parses each JSON frame, and collects the third slot of any
    ``["wrb.fr", "<rpc_id>", "<json-string>", ...]`` envelope. Housekeeping
    envelopes (``di`` / ``af.httprm`` / ``e``) and other rpc ids are ignored.

    Slot [2] is itself a JSON *string* (the nested array, JSON-encoded once
    more) — the caller does the second ``json.loads`` so this stays a pure
    envelope extractor.
    """
    payloads: list[str] = []
    lines = _strip_xssi(body).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        try:
            int(line)  # byte-count prefix
        except ValueError:
            # No count prefix — try to parse this line as a JSON frame directly.
            chunk_line = lines[i]
            i += 1
        else:
            i += 1
            if i >= len(lines):
                break
            chunk_line = lines[i]
            i += 1
        try:
            chunk = json.loads(chunk_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, list):
            continue
        for envelope in chunk:
            # Matching ``envelope[1] == rpc_id`` already excludes the
            # housekeeping ``di`` / ``af.httprm`` / ``e`` envelopes (a real rpc id
            # is never one of those), so no separate housekeeping filter is needed.
            if (
                isinstance(envelope, list)
                and len(envelope) >= 3
                and envelope[0] == "wrb.fr"
                and envelope[1] == rpc_id
                and isinstance(envelope[2], str)
            ):
                payloads.append(envelope[2])
    return payloads


def load_rpc_payload(cassette_name: str, rpc_id: str, occurrence: int = 0) -> list:
    """Load the decoded RPC payload for ``rpc_id`` from a cassette.

    Selects the cassette interaction whose request URL ``rpcids=`` query param
    names ``rpc_id``, walks the chunked batchexecute frames of its response,
    finds the ``["wrb.fr", rpc_id, "<json-string>", ...]`` envelope, and returns
    ``json.loads`` of slot [2] (the *second* ``json.loads`` — slot [2] is a JSON
    string wrapping the nested array).

    A cassette may answer the same rpc id more than once (e.g. a list call
    repeated for two render paths); ``occurrence`` selects which (0-based,
    counted across all matching interactions in file order).

    Raises:
        LookupError: when no ``occurrence``-th ``rpc_id`` payload is found.
    """
    cassette_path = _CASSETTE_DIR / cassette_name
    data = yaml.safe_load(cassette_path.read_text(encoding="utf-8")) or {}
    seen = 0
    for interaction in data.get("interactions") or []:
        uri = (interaction.get("request") or {}).get("uri") or ""
        if rpc_id not in _rpcids_from_uri(uri):
            continue
        body = ((interaction.get("response") or {}).get("body") or {}).get("string") or ""
        for raw_payload in _wrb_payloads(body, rpc_id):
            if seen == occurrence:
                decoded = json.loads(raw_payload)
                if not isinstance(decoded, list):
                    raise LookupError(
                        f"{rpc_id} payload in {cassette_name} is not a list: "
                        f"{type(decoded).__name__}"
                    )
                return decoded
            seen += 1
    raise LookupError(
        f"no occurrence {occurrence} of rpc id {rpc_id!r} found in cassette {cassette_name!r} "
        f"(saw {seen})"
    )


@dataclass(frozen=True)
class Projection:
    """A coarse, shallow projection of a list-RPC payload.

    Carries only the fabrication/drop/duplicate/miscount-catching aggregates —
    never a per-field positional decode:

    * ``count`` — number of top-level rows the projection saw.
    * ``ids`` — the set of id tokens (UUID-or-string leaf at the row's id slot).
    * ``urls`` — the set of ``http(s)://`` tokens found in a row's metadata.
    * ``type_codes`` — histogram of the raw integer type codes.
    * ``status_codes`` — histogram of the raw integer status codes.
    """

    count: int
    ids: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    type_codes: Counter[int] = field(default_factory=Counter)
    status_codes: Counter[int] = field(default_factory=Counter)


def _id_token(envelope: object) -> str | None:
    """Pull the first string leaf from a row's id slot (shallow, <=2 deep).

    Source id slots come as ``"id"`` (flat), ``["id"]`` (typical), or
    ``[None, True, ["id"]]`` (drive-backed). Rather than encode those exact
    positions (the adapter's job), grab the first string found in a shallow
    walk — coarse but independent and sufficient to compare id *sets*.
    """
    if isinstance(envelope, str):
        return envelope
    if isinstance(envelope, list):
        for element in envelope:
            if isinstance(element, str):
                return element
            if isinstance(element, list):
                for nested in element:
                    if isinstance(nested, str):
                        return nested
    return None


def _shallow_http_url(metadata: object) -> str | None:
    """Return the first ``http(s)://`` token in a row's metadata sub-list.

    Looks only at the leading element of each top-level metadata slot — a coarse
    read that finds the canonical/youtube/bare-url slots without committing to
    their exact indices.
    """
    if not isinstance(metadata, list):
        return None
    for slot in metadata:
        if isinstance(slot, str) and slot.startswith(("http://", "https://")):
            return slot
        if isinstance(slot, list) and slot:
            first = slot[0]
            if isinstance(first, str) and first.startswith(("http://", "https://")):
                return first
    return None


def _int_at(seq: object, *path: int) -> int | None:
    """Return the int reached by following ``path`` into nested lists, else ``None``.

    A tiny shallow descent (``bool`` is rejected — it is an ``int`` subclass but
    never a type/status code). Used by the two projections to read a leaf integer
    code without committing to a deep positional decode.
    """
    cursor: object = seq
    for index in path:
        if not isinstance(cursor, list) or len(cursor) <= index:
            return None
        cursor = cursor[index]
    return cursor if isinstance(cursor, int) and not isinstance(cursor, bool) else None


def project_source_list(payload: list) -> Projection:
    """Project a ``GET_NOTEBOOK`` (``rLM1Ne``) payload's source list.

    The source list sits at ``payload[0][1]`` (the notebook's second slot). Each
    entry is ``[<id-slot>, <title>, <metadata>, <status-block>, ...]``. We pull
    the id token, a shallow http(s) url, the type code (``metadata[4]``), and the
    status code (``status-block[1]``) — a coarse read, never the adapter's full
    positional mapping.
    """
    notebook = payload[0] if payload and isinstance(payload[0], list) else None
    rows: list = (
        notebook[1]
        if isinstance(notebook, list) and len(notebook) > 1 and isinstance(notebook[1], list)
        else []
    )
    ids: set[str] = set()
    urls: set[str] = set()
    type_codes: Counter[int] = Counter()
    status_codes: Counter[int] = Counter()
    count = 0
    for row in rows:
        if not isinstance(row, list):
            continue
        count += 1
        token = _id_token(row[0]) if row else None
        if token:
            ids.add(token)
        metadata = row[2] if len(row) > 2 else None
        url = _shallow_http_url(metadata)
        if url:
            urls.add(url)
        type_code = _int_at(metadata, 4)
        if type_code is not None:
            type_codes[type_code] += 1
        status_code = _int_at(row[3], 1) if len(row) > 3 else None
        if status_code is not None:
            status_codes[status_code] += 1
    return Projection(count, ids, urls, type_codes, status_codes)


def project_artifact_list(payload: list) -> Projection:
    """Project a ``LIST_ARTIFACTS`` (``gArtLc``) payload's artifact rows.

    The rows sit at ``payload[0]``. Each row is
    ``[<id>, <title>, <type-code>, <error>, <status-code>, ...]`` — id at the
    row's leading slot, type code at index 2, status code at index 4. Note the
    CLI artifact list is a *superset* of this projection: it merges note-backed
    mind maps from a separate RPC, so assertions must use containment / a count
    floor, not equality (artifact ids are also not all UUID-shaped).
    """
    rows: list = payload[0] if payload and isinstance(payload[0], list) else []
    ids: set[str] = set()
    type_codes: Counter[int] = Counter()
    status_codes: Counter[int] = Counter()
    count = 0
    for row in rows:
        if not isinstance(row, list):
            continue
        count += 1
        token = _id_token(row[0]) if row else None
        if token:
            ids.add(token)
        type_code = _int_at(row, 2)
        if type_code is not None:
            type_codes[type_code] += 1
        status_code = _int_at(row, 4)
        if status_code is not None:
            status_codes[status_code] += 1
    return Projection(count, ids, set(), type_codes, status_codes)


__all__ = [
    "Projection",
    "load_rpc_payload",
    "project_artifact_list",
    "project_source_list",
]
