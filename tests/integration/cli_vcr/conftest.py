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
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from integration.conftest import _is_vcr_record_mode, skip_no_cassettes  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

from ._fixtures import (  # noqa: E402
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
    "parse_json_output",
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


def assert_command_success(result, *, allow_no_context: bool = True) -> None:
    """Assert a CLI command completed without crashing.

    Args:
        result: The CliRunner result object.
        allow_no_context: If True, exit code 1 (no notebook context) is acceptable.
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
