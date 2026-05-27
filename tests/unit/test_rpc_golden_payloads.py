"""Golden RPC envelope + decoder coverage, parameterised over ``RPCMethod``.

This module pins, for every member of :class:`notebooklm.rpc.types.RPCMethod`:

1. The string method ID itself (catches enum-value drift).
2. The ``batchexecute`` ``f.req`` request envelope produced by
   :func:`notebooklm.rpc.encoder.encode_rpc_request` for a representative
   parameter list (catches encoder format drift and param-order regressions).
3. The Python payload returned by
   :func:`notebooklm.rpc.decoder.decode_response` when given a synthetic
   scrubbed response chunk for that method (catches decoder format drift).

For methods that have a documented downstream parser / dataclass mapper,
the fixture additionally pins the mapper output shape so the seam between
the raw decoded payload and the feature-level dataclass is also covered.

Each method has a fixture file at
``tests/fixtures/rpc_golden/<METHOD_NAME>.json``. A test that detects a
missing fixture fails the suite loudly so that adding a new ``RPCMethod``
member without also adding a fixture is a hard failure rather than a silent
gap.

Fixture schema is documented in ``tests/fixtures/rpc_golden/README.md``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from notebooklm.rpc.decoder import (
    collect_rpc_ids,
    decode_response,
    parse_chunked_response,
    strip_anti_xssi,
)
from notebooklm.rpc.encoder import encode_rpc_request
from notebooklm.rpc.types import RPCMethod

FIXTURE_ROOT: Path = Path(__file__).parents[1] / "fixtures" / "rpc_golden"


class _FixtureSchemaError(AssertionError):
    """Raised when a fixture is missing a required field or has the wrong type.

    Carries the file path and the dotted field name so a malformed fixture
    surfaces a structured failure instead of a raw KeyError / TypeError
    from elsewhere in the test. Inherits from :class:`AssertionError` (not
    :class:`ValueError`) so pytest renders it with the same friendly
    rewriting it applies to ``assert`` failures, and so any caller that
    handles ``AssertionError`` (e.g. pytest hooks, ``--tb=short``) treats
    it as a structured test failure rather than a generic exception.
    """


def _fixture_path(method: RPCMethod) -> Path:
    return FIXTURE_ROOT / f"{method.name}.json"


def _load_fixture(method: RPCMethod) -> dict[str, Any]:
    path = _fixture_path(method)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing golden fixture for RPCMethod.{method.name} at {path}. "
            f"Every RPCMethod enum value must have a fixture under "
            f"tests/fixtures/rpc_golden/. See the README in that directory "
            f"for the schema."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _require_field(
    fixture: dict[str, Any],
    dotted: str,
    expected_type: type | tuple[type, ...],
    *,
    method: RPCMethod,
) -> Any:
    """Look up ``dotted`` (e.g. ``"request.params"``) in ``fixture``.

    Raises :class:`_FixtureSchemaError` with the method name and field
    path if the key is missing or the value is the wrong type. Keeps the
    failure message structured so a malformed fixture is debuggable
    without grepping through raw stack traces.
    """
    current: Any = fixture
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if not isinstance(current, dict) or part not in current:
            raise _FixtureSchemaError(
                f"Fixture for RPCMethod.{method.name} is missing required "
                f"field {'.'.join(parts[: i + 1])!r} (file: {_fixture_path(method)})."
            )
        current = current[part]
    if not isinstance(current, expected_type):
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} field {dotted!r} has type "
            f"{type(current).__name__}, expected "
            f"{getattr(expected_type, '__name__', expected_type)} "
            f"(file: {_fixture_path(method)})."
        )
    return current


def _build_wire_response(chunks: list[Any]) -> str:
    """Serialise structured chunks back into the chunked-response wire format.

    Mirrors the shape that :func:`decode_response` expects after
    :func:`strip_anti_xssi`: each chunk is a JSON line preceded by a
    byte-count line. The full body starts with the canonical anti-XSSI
    prefix ``)]}'\\n`` so the decoder's prefix-stripping path is also
    exercised.
    """
    parts: list[str] = [")]}'"]
    for chunk in chunks:
        chunk_json = json.dumps(chunk, separators=(",", ":"))
        parts.append(str(len(chunk_json.encode("utf-8"))))
        parts.append(chunk_json)
    return "\n".join(parts) + "\n"


def _resolve_mapper(dotted: str, *, method: RPCMethod) -> Any:
    """Resolve ``"module.path:attr"`` to a callable.

    Used by fixtures that pin a downstream mapper / parser output shape in
    addition to the raw decoded payload. Wraps the import + getattr step
    in structured :class:`_FixtureSchemaError` so a missing module or
    attribute surfaces the fixture file path rather than a raw
    ``ModuleNotFoundError`` / ``AttributeError``.
    """
    module_name, _, attr = dotted.partition(":")
    if not module_name or not attr:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} declares mapper {dotted!r} "
            f"but it is not in 'module.path:attribute' form "
            f"(file: {_fixture_path(method)})."
        )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} mapper {dotted!r} references "
            f"unknown module {module_name!r} (file: {_fixture_path(method)})."
        ) from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} mapper {dotted!r}: module "
            f"{module_name!r} has no attribute {attr!r} "
            f"(file: {_fixture_path(method)})."
        ) from exc


ALL_METHODS: list[RPCMethod] = list(RPCMethod)


def test_every_rpc_method_has_a_fixture() -> None:
    """Adding a new ``RPCMethod`` without a fixture must fail the suite.

    This is the load-bearing guard called out in the task spec: future enum
    additions fail loudly rather than silently leaving coverage gaps.
    """
    missing = [m.name for m in ALL_METHODS if not _fixture_path(m).is_file()]
    assert not missing, (
        f"Missing golden fixtures for: {missing}. Add a JSON fixture under "
        f"tests/fixtures/rpc_golden/ for each. See the README there for the "
        f"schema."
    )


# Substrings that MUST NOT appear inside any fixture file. Catches future
# edits that paste real account / cookie / OAuth material into a fixture by
# mistake. The list mirrors the placeholder taxonomy in the directory README
# — anything that would never appear in a synthetic scrubbed payload.
_FORBIDDEN_FIXTURE_SUBSTRINGS: tuple[str, ...] = (
    "@gmail.com",
    "@google.com",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "Bearer ",
    "ya29.",  # OAuth access-token prefix
    "drive.google.com/",
    "docs.google.com/",
    "AIza",  # Google API-key prefix
)


def test_fixture_corpus_is_scrubbed() -> None:
    """Lint guard: no fixture may contain real-credential / real-PII substrings.

    The fixture directory sits outside the cassette-scrubber pipeline (per
    ADR-0006) because these payloads are synthetic by construction. This
    lint enforces that posture: if anyone edits a fixture and pastes a real
    cookie / OAuth token / Drive URL / email, this test fails before the
    leak lands in a commit. Pair the lint with the placeholder taxonomy
    documented in tests/fixtures/rpc_golden/README.md.
    """
    leaks: list[tuple[str, str]] = []
    # Scan JSON fixtures AND the README — the README contains worked
    # examples of placeholder shapes and is the most likely place for a
    # well-meaning contributor to paste a "real-looking" URL when updating
    # the schema docs.
    for path in sorted(FIXTURE_ROOT.iterdir()):
        if path.suffix not in (".json", ".md"):
            continue
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN_FIXTURE_SUBSTRINGS:
            if needle in text:
                leaks.append((path.name, needle))
    assert not leaks, (
        f"Forbidden non-scrubbed substring(s) found in fixture corpus: "
        f"{leaks}. Replace with the synthetic placeholders documented in "
        f"tests/fixtures/rpc_golden/README.md."
    )


def test_fixture_directory_has_no_orphans() -> None:
    """Every fixture file must correspond to a live ``RPCMethod`` member.

    Catches the inverse drift: a method is renamed/removed but its fixture
    file is left behind. Without this guard, the orphan would silently
    persist in the corpus.
    """
    valid_names = {m.name for m in ALL_METHODS}
    orphans = [path.stem for path in FIXTURE_ROOT.glob("*.json") if path.stem not in valid_names]
    assert not orphans, (
        f"Orphan fixtures with no corresponding RPCMethod member: {orphans}. "
        f"Remove the fixture file or restore the enum member."
    )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_fixture_has_required_schema(method: RPCMethod) -> None:
    """Every fixture must expose the required top-level schema fields.

    Runs the structural validator once per method so a malformed fixture
    surfaces as a clear ``_FixtureSchemaError`` here instead of a raw
    ``KeyError`` / ``TypeError`` from one of the downstream tests.
    """
    fixture = _load_fixture(method)
    _require_field(fixture, "method_name", str, method=method)
    _require_field(fixture, "method_id", str, method=method)
    _require_field(fixture, "request.params", list, method=method)
    _require_field(fixture, "request.expected_f_req", list, method=method)
    _require_field(fixture, "response.chunks", list, method=method)
    # expected_decoded is allowed to be None (allow_null path); we only
    # require the key to be present.
    if "expected_decoded" not in fixture.get("response", {}):
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} is missing required field "
            f"'response.expected_decoded' (file: {_fixture_path(method)})."
        )
    if "mapper" in fixture:
        _require_field(fixture, "mapper", str, method=method)
        if "mapper_expected" not in fixture:
            raise _FixtureSchemaError(
                f"Fixture for RPCMethod.{method.name} declares 'mapper' "
                f"but is missing 'mapper_expected' (file: {_fixture_path(method)})."
            )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_method_id_matches_fixture(method: RPCMethod) -> None:
    """The fixture's recorded ``method_id`` must match the enum value.

    This is the primary guard against accidental enum-value edits: any
    change to ``RPCMethod.<NAME>.value`` requires a matching fixture edit.
    """
    fixture = _load_fixture(method)
    fixture_method_name = _require_field(fixture, "method_name", str, method=method)
    fixture_method_id = _require_field(fixture, "method_id", str, method=method)
    assert fixture_method_name == method.name, (
        f"Fixture method_name {fixture_method_name!r} does not match "
        f"enum name {method.name!r} (file mislabelled?)"
    )
    assert fixture_method_id == method.value, (
        f"Fixture method_id {fixture_method_id!r} for {method.name} "
        f"does not match enum value {method.value!r}. If the wire ID truly "
        f"changed, update both rpc/types.py and the fixture together."
    )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_request_envelope_matches_fixture(method: RPCMethod) -> None:
    """The ``batchexecute`` ``f.req`` envelope must match the fixture.

    The fixture records both the input ``params`` and the expected encoded
    envelope. We re-encode and compare against the recorded envelope so any
    drift in :func:`encode_rpc_request` (nesting depth, param JSON-encoding,
    trailing markers) trips this assertion.
    """
    fixture = _load_fixture(method)
    params = _require_field(fixture, "request.params", list, method=method)
    expected = _require_field(fixture, "request.expected_f_req", list, method=method)

    encoded = encode_rpc_request(method, params)

    assert encoded == expected, (
        f"Encoded f.req envelope for {method.name} drifted from the fixture. "
        f"Got: {encoded!r}\nExpected: {expected!r}"
    )

    # Shape invariants — strictly redundant once the equality assertion above
    # passes (since ``encoded == expected`` means both share these properties),
    # but kept as a machine-checked specification of the batchexecute wire
    # format that survives even if a future contributor accidentally copies a
    # regressed encoder output into the fixture.
    assert isinstance(encoded, list) and len(encoded) == 1
    assert isinstance(encoded[0], list) and len(encoded[0]) == 1
    inner = encoded[0][0]
    assert inner[0] == method.value
    assert inner[2] is None
    assert inner[3] == "generic"
    # inner[1] is the JSON-encoded params string — re-decode and verify
    # round-trip equivalence to params.
    assert json.loads(inner[1]) == params


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_response_decoder_returns_expected_payload(method: RPCMethod) -> None:
    """The decoder must return the expected raw payload for the fixture.

    Builds a wire-format response from the fixture's structured ``chunks``
    field, feeds it to :func:`decode_response`, and compares the returned
    Python value to the fixture's ``expected_decoded`` value.

    Methods that legitimately return ``None`` on success (e.g. fire-and-
    forget RPCs whose ``wrb.fr`` payload is ``null``) opt into
    ``allow_null: true`` in the fixture; the decoder receives the same
    flag. For those, this test ALSO asserts that the synthetic response
    actually contains a ``wrb.fr`` row for ``method.value`` — without that
    cross-check, an ``allow_null=True`` fixture would silently pass even
    if its chunks named a wrong (or no) RPC ID, since
    :func:`decode_response` returns ``None`` either way under ``allow_null``.
    """
    fixture = _load_fixture(method)
    chunks = _require_field(fixture, "response.chunks", list, method=method)
    response = fixture["response"]
    allow_null = response.get("allow_null", False)
    if "expected_decoded" not in response:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} is missing required field "
            f"'response.expected_decoded' (file: {_fixture_path(method)})."
        )
    expected = response["expected_decoded"]

    raw_response = _build_wire_response(chunks)
    decoded = decode_response(raw_response, method.value, allow_null=allow_null)

    assert decoded == expected, (
        f"decode_response({method.name}) returned a payload that does not "
        f"match the fixture's expected_decoded.\n"
        f"Got: {decoded!r}\nExpected: {expected!r}"
    )

    # Independent of allow_null, the response chunks MUST contain a row
    # naming this method's RPC ID. This guards against the silent
    # pass-through that allow_null=True otherwise enables.
    parsed = parse_chunked_response(strip_anti_xssi(raw_response))
    found_ids = collect_rpc_ids(parsed)
    assert method.value in found_ids, (
        f"Synthetic response for {method.name} does not include a "
        f"'wrb.fr'/'er' row naming {method.value!r}; the fixture chunks "
        f"would let an allow_null decode silently pass. Found IDs: {found_ids!r}"
    )


@pytest.mark.parametrize("method", ALL_METHODS, ids=lambda m: m.name)
def test_mapper_output_shape_when_documented(method: RPCMethod) -> None:
    """Methods that document a downstream mapper must also pin its output.

    Most methods have inline ``safe_index`` extraction at the feature level
    and no centralised mapper; for those, this test is a no-op (the fixture
    omits ``mapper`` / ``mapper_expected``). For methods that DO have a
    clean importable mapper (e.g. research-task parsing), we invoke it on
    the decoded payload and assert against the fixture's recorded shape so
    the seam between decoder and feature-level dataclass is also covered.
    """
    fixture = _load_fixture(method)
    mapper_ref = fixture.get("mapper")
    if not mapper_ref:
        pytest.skip(f"{method.name}: no documented downstream mapper")

    if "mapper_expected" not in fixture:
        raise _FixtureSchemaError(
            f"Fixture for RPCMethod.{method.name} declares 'mapper' "
            f"but is missing 'mapper_expected' (file: {_fixture_path(method)})."
        )
    expected = fixture["mapper_expected"]
    mapper = _resolve_mapper(mapper_ref, method=method)
    decoded = fixture["response"]["expected_decoded"]
    mapped = mapper(decoded)

    # Mappers commonly return dataclass instances or lists thereof; compare
    # via the fixture-recorded shape (typically the public dict form or a
    # list of public dicts). The fixture decides the representation.
    if isinstance(mapped, list) and mapped and hasattr(mapped[0], "to_public_dict"):
        mapped_repr: Any = [item.to_public_dict() for item in mapped]
    elif hasattr(mapped, "to_public_dict"):
        mapped_repr = mapped.to_public_dict()
    else:
        mapped_repr = mapped

    assert mapped_repr == expected, (
        f"Mapper {mapper_ref!r} for {method.name} returned a shape that "
        f"does not match the fixture's mapper_expected.\n"
        f"Got: {mapped_repr!r}\nExpected: {expected!r}"
    )
