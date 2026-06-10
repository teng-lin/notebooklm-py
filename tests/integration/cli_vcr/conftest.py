"""Shared fixtures for CLI integration tests.

These tests use VCR cassettes with real NotebookLMClient instances,
exercising the full CLI → Client → RPC path without mocking the client.

Placeholder ids (``PLACEHOLDER_NOTEBOOK_ID`` etc.) and the back-compat aliases
(``VCR_READONLY_NOTEBOOK_ID`` …) live in :mod:`._fixtures` — see that module's
docstring for *why* the ids are decorative (VCR matches on ``rpcids`` + body
shape, never on the notebook/source id). They are re-exported here so existing
``from .conftest import VCR_READONLY_SOURCE_ID`` imports keep resolving.

``assert_json_envelope`` validates the ``--json`` envelope *shape* (field names
and value types) against a per-family schema constant. It deliberately asserts
nothing about recorded *values* (titles, server ids, counts), so the assertions
survive a re-record against a different notebook (issue #1452).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

# Enum value *sets* only — an allowed-membership definition, NOT a decoder. Reading
# the canonical enum values from the public ``notebooklm`` types keeps the membership
# floor in lock-step with the source of truth without importing the decode path
# (``assert_semantic_invariants`` only checks ``value in <set>``). See issue #1452.
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import (
    ArtifactStatus,
    ArtifactType,
    SourceType,
    artifact_status_to_str,
    source_status_to_str,
)
from tests.integration.conftest import _is_vcr_record_mode, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from ._fixtures import (
    PLACEHOLDER_NOTEBOOK_ID,
    VCR_READONLY_NOTEBOOK_ID,
    VCR_READONLY_SOURCE_ID,
)

# Re-export for use by test files
__all__ = [
    "runner",
    "mock_context",
    "skip_no_cassettes",
    "notebooklm_vcr",
    "assert_command_success",
    "assert_json_envelope",
    "assert_semantic_invariants",
    "parse_json_output",
    "parse_json_dict",
    "VCR_READONLY_NOTEBOOK_ID",
    "VCR_READONLY_SOURCE_ID",
    "SOURCE_LIST_SCHEMA",
    "SOURCE_MUTATION_SCHEMA",
    "CHAT_ANSWER_SCHEMA",
    "ERROR_SCHEMA",
]


@pytest.fixture
def runner() -> CliRunner:
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_context(tmp_path: Path):
    """Mock context file with a test notebook ID.

    CLI commands that require a notebook ID will use this context.
    Use a full recorded notebook UUID rather than a short placeholder. A
    placeholder is treated as a partial ID by the CLI and triggers an extra
    LIST_NOTEBOOKS RPC before the command under test, which breaks replay now
    that VCR matches batchexecute calls by ``rpcids``.
    """
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({"notebook_id": PLACEHOLDER_NOTEBOOK_ID}), encoding="utf-8")

    with (
        patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
        patch("notebooklm.cli.context.get_context_path", return_value=context_file),
        patch("notebooklm.cli.resolve.get_context_path", return_value=context_file),
    ):
        yield context_file


@pytest.fixture
def mock_auth_for_vcr():
    """Mock authentication that works with VCR cassettes.

    VCR replays recorded responses regardless of auth tokens, so we use mock
    auth to avoid requiring real credentials.

    The layer-1 ``RotateCookies`` keepalive-poke disable that used to live
    here (``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1``) was globalized —
    see the ``_disable_keepalive_poke_for_vcr`` autouse fixture in
    ``tests/integration/conftest.py``. Every test that pulls this fixture
    also carries ``@pytest.mark.vcr`` (either directly or via a module-level
    ``pytestmark``), so the global autouse already disables the poke before
    this fixture runs.

    Recording (``NOTEBOOKLM_VCR_RECORD=1``) is the exception: the CLI must load
    the *real* profile's cookies/tokens to reach the live API, so the mock is
    skipped. The root ``_isolate_notebooklm_home`` fixture likewise defers to
    the real ``~/.notebooklm`` for vcr tests in record mode, so the normal
    ``load_auth_from_storage`` path resolves real auth (issue #1263).
    """
    if _is_vcr_record_mode():
        yield
        return
    mock_cookies = {
        "SID": "vcr_mock_sid",
        "HSID": "vcr_mock_hsid",
        "SSID": "vcr_mock_ssid",
        "APISID": "vcr_mock_apisid",
        "SAPISID": "vcr_mock_sapisid",
    }
    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage", return_value=mock_cookies),
        patch(
            "notebooklm.auth.fetch_tokens_with_domains",
            return_value=("vcr_mock_csrf", "vcr_mock_session"),
        ),
    ):
        yield


def assert_command_success(result, *, allow_no_context: bool = False) -> None:
    """Assert a CLI command completed successfully (exit 0).

    The default is **strict** (``exit_code == 0``): a permissive default that
    accepted exit 1 "for any reason" silently masked a genuinely-broken command
    (issue #1488: the download flow exited 1 with no file written, yet passed).
    Callers that legitimately expect a no-notebook-context exit-1 path opt in
    explicitly via ``allow_no_context=True``.

    Args:
        result: The CliRunner result object.
        allow_no_context: If True, exit code 1 (e.g. no notebook context) is
            also acceptable. Opt-in per call site, not the default.
    """
    acceptable_codes = (0, 1) if allow_no_context else (0,)
    assert result.exit_code in acceptable_codes, f"Command failed: {result.output}"


def parse_json_output(output: str) -> list | dict | None:
    """Parse JSON from CLI output, handling potential non-JSON prefixes.

    Returns the parsed JSON or None if no valid JSON found.
    """
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    # If whole output is not JSON, try finding the start of a JSON object.
    # This handles multi-line JSON with a prefix.
    brace_pos = output.find("{")
    bracket_pos = output.find("[")
    start_positions = [p for p in (brace_pos, bracket_pos) if p != -1]
    if start_positions:
        start_pos = min(start_positions)
        try:
            return json.loads(output[start_pos:])
        except json.JSONDecodeError:
            pass

    # Try each line (some output may have single-line JSON prefix)
    for line in output.strip().split("\n"):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    return None


def parse_json_dict(output: str) -> dict[str, Any]:
    """Parse CLI ``--json`` output and assert it is a JSON object.

    A typed convenience over :func:`parse_json_output` for the common
    ``--json``-envelope case: narrows the ``list | dict | None`` union to a
    ``dict`` (so callers can index fields without a type-checker complaint) and
    fails loudly when the output is not a single JSON object.
    """
    data = parse_json_output(output)
    assert isinstance(data, dict), f"Expected a JSON object, got: {output!r}"
    return data


# ---------------------------------------------------------------------------
# ``--json`` envelope shape validation (issue #1452)
# ---------------------------------------------------------------------------
# A schema maps a field name to a ``FieldSpec``. ``assert_json_envelope`` checks
# field *names* and *types* only — never recorded *values* — so an assertion
# survives a re-record against a different notebook. A re-record breaks one of
# these only when the response *shape* actually changes (a real signal worth
# catching); the fix is then to update the schema, not every test.


class FieldSpec:
    """Type/nullability/nesting spec for a single ``--json`` envelope field.

    ``types`` is the tuple of acceptable Python types for the value.
    ``nullable`` permits an explicit ``None``; ``optional`` permits the field to
    be *absent* from the payload entirely (some payloads omit a field rather
    than emit ``null``). ``item_schema`` (only meaningful when ``list`` is among
    ``types``) validates each element of a list of objects. A hand-rolled spec
    on purpose — no ``jsonschema`` dependency.
    """

    def __init__(
        self,
        *types: type,
        nullable: bool = False,
        optional: bool = False,
        item_schema: dict[str, FieldSpec] | None = None,
    ) -> None:
        self.types = types
        self.nullable = nullable
        self.optional = optional
        self.item_schema = item_schema


def _assert_field(path: str, value: Any, spec: FieldSpec) -> None:
    """Assert ``value`` matches ``spec`` (type + nullability + item shape)."""
    if value is None:
        assert spec.nullable, f"{path}: unexpected null"
        return
    # ``bool`` is a subclass of ``int``; keep them distinct so a schema that
    # asks for ``int`` does not silently accept ``True``.
    if int in spec.types and bool not in spec.types:
        assert not isinstance(value, bool), f"{path}: expected int, got bool"
    assert isinstance(value, spec.types), (
        f"{path}: expected {tuple(t.__name__ for t in spec.types)}, got {type(value).__name__}"
    )
    if spec.item_schema is not None and isinstance(value, list):
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            assert isinstance(item, dict), f"{item_path}: expected object"
            _assert_schema(item_path, item, spec.item_schema)


def _assert_schema(path: str, payload: dict[str, Any], schema: dict[str, FieldSpec]) -> None:
    """Assert every schema field is present in ``payload`` with the right shape.

    A field marked ``optional`` may be absent entirely; a field marked
    ``nullable`` must be present but may be ``None``.
    """
    for name, spec in schema.items():
        field_path = f"{path}.{name}"
        if name not in payload:
            assert spec.optional, f"{field_path}: missing required field"
            continue
        _assert_field(field_path, payload[name], spec)


def assert_json_envelope(result, *, schema: dict[str, FieldSpec]) -> None:
    """Assert the CLI ``--json`` output is an object matching ``schema``.

    Validates the envelope *shape* (required field names + value types), not the
    recorded values. ``result`` is a ``CliRunner`` result; its stdout must parse
    as a single JSON object.
    """
    data = parse_json_output(result.output)
    assert isinstance(data, dict), f"Expected a JSON object, got: {result.output!r}"
    _assert_schema("$", data, schema)


# ---------------------------------------------------------------------------
# Per-field semantic invariants (issue #1452, depth-2)
# ---------------------------------------------------------------------------
# Where ``assert_json_envelope`` pins *types*, ``assert_semantic_invariants``
# pins per-field *meaning* — catching the "valid type but wrong field" class
# (e.g. a title read out of the url slot: a ``str`` that satisfies the schema but
# fails to parse as a URL). All of these are notebook-agnostic: a ``url`` parses,
# an enum value is in the known set, a timestamp parses, a source id is
# UUID-shaped. None pins a *recorded value*, so they survive a re-record.
#
# The enum *value sets* below are read from the public ``notebooklm`` types as an
# allowed-membership definition. That is a set-membership check, NOT a decode —
# importing the enum's allowed values is fine; importing the positional decoder
# would make the assertion a tautology (the rule the projection helper obeys).

# 8-4-4-4-12 hex UUID, anchored. Source ids are reliably UUID-shaped; artifact
# ids are NOT (some are numeric), so the UUID invariant is opt-in per ``kind``.
_UUID_INVARIANT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Allowed string-enum value sets, derived from the canonical enums.
_SOURCE_TYPE_VALUES = frozenset(member.value for member in SourceType)
_ARTIFACT_TYPE_VALUES = frozenset(member.value for member in ArtifactType)
_SOURCE_STATUS_STR_VALUES = frozenset(
    source_status_to_str(member.value) for member in SourceStatus
) | {source_status_to_str(0)}
_ARTIFACT_STATUS_STR_VALUES = frozenset(
    artifact_status_to_str(member.value) for member in ArtifactStatus
) | {artifact_status_to_str(0)}


def _assert_url_field(item: dict[str, Any], field: str) -> None:
    """When ``item[field]`` is a present non-null string, it must parse as a URL.

    A URL must carry both a scheme and a netloc (``https`` + ``example.com``).
    This is the field-confusion canary: a title accidentally read out of the url
    slot is a valid ``str`` (passes the schema) but lacks a scheme/netloc.
    """
    value = item.get(field)
    if value is None:
        return
    assert isinstance(value, str), f"{field}={value!r} is not a string URL"
    parsed = urlparse(value)
    assert parsed.scheme and parsed.netloc, (
        f"{field}={value!r} does not parse as a URL (needs scheme + netloc)"
    )


def _assert_enum_field(item: dict[str, Any], field: str, allowed: frozenset[str]) -> None:
    """When ``item[field]`` is present and non-null, it must be in ``allowed``."""
    value = item.get(field)
    if value is None:
        return
    assert value in allowed, f"{field}={value!r} not in known enum values {sorted(allowed)}"


def _assert_timestamp_field(item: dict[str, Any], field: str) -> None:
    """When ``item[field]`` is a present non-null string, it must parse as a datetime.

    The CLI emits ``datetime.isoformat()`` for ``created_at``; a value that is a
    ``str`` but not parseable as an ISO timestamp means the wrong slot was read.
    """
    value = item.get(field)
    if value is None:
        return
    assert isinstance(value, str), f"{field}={value!r} is not a string timestamp"
    # ``datetime.fromisoformat`` only learned to parse a trailing ``Z`` in 3.11;
    # the CI matrix includes 3.10, so normalize ``Z`` -> ``+00:00`` first.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AssertionError(f"{field}={value!r} does not parse as a timestamp: {exc}") from exc


def assert_semantic_invariants(item: dict[str, Any], kind: str) -> None:
    """Assert per-field semantic invariants on one decoded ``--json`` list item.

    ``kind`` selects the field rules:

    * ``"source"`` — ``id`` is UUID-shaped; ``type`` (when present) is a known
      :class:`~notebooklm.types.SourceType` value; ``status`` (when present) is a
      known source status string; ``url`` (when present) parses as a URL;
      ``created_at`` (when present) parses as a timestamp.
    * ``"artifact"`` — ``type_id`` (when present) is a known
      :class:`~notebooklm.types.ArtifactType` value; ``status`` (when present) is
      a known artifact status string; ``created_at`` (when present) parses. The
      ``id`` is deliberately NOT UUID-checked — artifact ids are not all UUIDs.

    All rules are notebook-agnostic (no recorded values), so they survive a
    re-record. They catch the "valid type but wrong field" defect that a pure
    shape check (``assert_json_envelope``) cannot.
    """
    assert kind in {"source", "artifact"}, f"unknown semantic-invariant kind: {kind!r}"
    _assert_timestamp_field(item, "created_at")
    if kind == "source":
        source_id = item.get("id")
        assert isinstance(source_id, str) and _UUID_INVARIANT_RE.match(source_id), (
            f"source id is not UUID-shaped: {source_id!r}"
        )
        _assert_enum_field(item, "type", _SOURCE_TYPE_VALUES)
        _assert_enum_field(item, "status", _SOURCE_STATUS_STR_VALUES)
        _assert_url_field(item, "url")
    else:  # "artifact"
        _assert_enum_field(item, "type_id", _ARTIFACT_TYPE_VALUES)
        _assert_enum_field(item, "status", _ARTIFACT_STATUS_STR_VALUES)


# Per-family schemas. ``str()``-typed ids/titles are shape-only — value
# invariants (UUID-shaped id, non-empty title, ``count > 0``) are asserted by
# the tests themselves so the schema stays a pure structural contract.
_SOURCE_LIST_ITEM_SCHEMA: dict[str, FieldSpec] = {
    "index": FieldSpec(int),
    "id": FieldSpec(str),
    "title": FieldSpec(str, nullable=True),
    "type": FieldSpec(str, nullable=True),
    "url": FieldSpec(str, nullable=True),
    "status": FieldSpec(str, nullable=True),
    "status_id": FieldSpec(int, nullable=True),
    "created_at": FieldSpec(str, nullable=True),
}

SOURCE_LIST_SCHEMA: dict[str, FieldSpec] = {
    "notebook_id": FieldSpec(str),
    "notebook_title": FieldSpec(str, nullable=True),
    "sources": FieldSpec(list, item_schema=_SOURCE_LIST_ITEM_SCHEMA),
    "count": FieldSpec(int),
}

SOURCE_MUTATION_SCHEMA: dict[str, FieldSpec] = {
    "action": FieldSpec(str),
    "source_id": FieldSpec(str),
    "notebook_id": FieldSpec(str),
    "success": FieldSpec(bool),
    "status": FieldSpec(str),
}

CHAT_ANSWER_SCHEMA: dict[str, FieldSpec] = {
    "answer": FieldSpec(str),
    "references": FieldSpec(list),
}

# The ADR-0015 error envelope. Consumed by the Phase-2 error-contract tests
# (429 / 5xx / expired-csrf → JSON error body). The shape is fixed by
# ``cli/error_handler.py::_output_error``: ``{"error": true, "code": "<CODE>",
# "message": "<text>", ...command-specific extras}``. ``error`` is the literal
# boolean sentinel ``true`` (NOT a nested object), ``code`` is the machine
# ADR-0015 error code, and ``message`` is the human string. Extra fields
# (``retry_after`` on a rate-limit, ``method_id`` under ``-vv``) are intentionally
# NOT pinned here — a schema is a structural floor, so per-test assertions cover
# the variable extras.
ERROR_SCHEMA: dict[str, FieldSpec] = {
    "error": FieldSpec(bool),
    "code": FieldSpec(str),
    "message": FieldSpec(str),
}


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch ``asyncio.sleep`` to an immediate no-op.

    Async generate flows (e.g. interactive mind maps) poll
    ``LIST_ARTIFACTS`` with ``await asyncio.sleep(interval)`` backoff between
    attempts. During cassette replay the cassette already encodes the server
    progression, so the waits add only wall-clock time. Narrow on purpose:
    only ``asyncio.sleep`` is patched. Mirrors ``test_polling_vcr.fast_sleep``.
    """

    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
