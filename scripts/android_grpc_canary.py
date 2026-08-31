#!/usr/bin/env python3
"""Android gRPC canary - read-only drift probe for the private Android backend.

Layer 3 of the Android gRPC integration-testing plan. Where the Web
``check_rpc_health.py`` watches obfuscated batchexecute method IDs, the Android
backend speaks tag-addressed protobuf over gRPC, so the failure classes it can
drift on are different: bearer minting (gpsoauth), service/method routing, and
the protobuf *shape* of each response. This script exercises each once against
the dedicated read-only notebook and prints one line per step::

    OK open backends=android
    OK bearer generation 1->2
    OK get_project id round-trip
    OK list_chat_sessions conversation=present
    SHAPE GetProject <sha256 hex>
    UNKNOWN GetProject 38
    OK schema GetProject shape=match unknown_fields=38
    SHAPE ListChatSessions <sha256 hex>
    UNKNOWN ListChatSessions 0
    OK schema ListChatSessions shape=match unknown_fields=0

``SHAPE`` lines carry a structural fingerprint: a SHA-256 over the sorted set
of ``(field_path, wire_type)`` pairs populated in the response. It carries no
values, no ids and no text, so two runs against different notebooks with the
same populated fields print the same hex. ``UNKNOWN`` lines count protobuf
fields the recovered schema does not declare, at every nesting level.

Drift is judged against a hand-authored baseline (``--baseline PATH``), never
against zero: the recovered app protos do not declare every field Google's
server sends, so a non-zero unknown count (38 on ``GetProject`` at the time
of writing) is the steady state. ``GetProject`` is decoded with the full
recovered ``read_pb2.GetProjectResponse`` — exactly as ``sources.list`` does —
rather than the partial ``Wire*`` projection, so the count reflects the
complete known schema. With a baseline present, any RPC whose live shape or
unknown count differs from it is a ``FAIL`` printing both values. When the
flag is given but the file does not exist, the run prints
``WARN baseline missing <path>`` and stays green (diagnostic mode): the
``SHAPE``/``UNKNOWN`` lines of that run are what a human reviews and copies
into the baseline. The canary never writes the baseline or any other file.

Output hygiene: exception text is passed through ``scrub_secrets`` and
bounded, and the probed notebook id is replaced with ``<notebook-id>`` on
every emitted line (a ``NotebookNotFoundError`` otherwise spells it out).

Read-only by construction: ``GetProject`` and ``ListChatSessions`` are the two
RPCs on the wire. The only unary-stream RPC the Android backend exposes,
``GenerateFreeFormStreamed``, creates a chat turn in the notebook, so it is
deliberately NOT called here; ``ListChatSessions`` stands in as the second
read-only probe. Nothing is written to disk and no fixture is ever updated.

Exit codes:
    0 - every step passed (or a baseline was requested but is missing)
    1 - one or more steps failed (auth, routing, transport, an unreadable
        baseline, or a shape / unknown-count mismatch against the baseline)
    2 - argument error (no notebook id supplied)

Environment variables:
    NOTEBOOKLM_PROFILE - profile whose master token mints the bearer
    NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID - notebook probed when ``--notebook-id``
        is omitted

Baseline JSON shape::

    {"GetProject": {"shape": "<sha256 hex>", "unknown_fields": 38},
     "ListChatSessions": {"shape": "<sha256 hex>", "unknown_fields": 0}}

Usage:
    NOTEBOOKLM_PROFILE=<profile> NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<id> \\
        python scripts/android_grpc_canary.py
    python scripts/android_grpc_canary.py --notebook-id <id> \\
        --baseline tests/fixtures/android/canary_baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.unknown_fields import UnknownFieldSet

# The method paths come from the modules whose calls this canary guards, so
# the drift detector can never drift from the code it is checking. Neither
# module imports grpc at import time (the ``_android`` package stays lazy).
from notebooklm._android.chat import LIST_CHAT_SESSIONS_METHOD
from notebooklm._android.sources import GET_PROJECT_METHOD
from notebooklm._logging import scrub_secrets
from notebooklm.exceptions import AuthError

ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]
Emit = Callable[[str], object]

# Failure detail is bounded so a pathological exception message cannot flood
# the step summary; the class name is the diagnostic that matters.
_MAX_FAILURE_DETAIL = 200
# One retry of the forced re-mint after this pause (see ``check_bearer``).
_REFRESH_RETRY_BACKOFF_SECONDS = 5.0
# Every emitted line has the probed notebook id replaced with this token. The
# id is not a credential, but it is the one value most likely to be embedded
# in an exception (``NotebookNotFoundError`` says "Notebook not found: <id>")
# and the step summary is not covered by Actions' log masking.
REDACTED_NOTEBOOK_ID = "<notebook-id>"

# Protobuf wire types (https://protobuf.dev/programming-guides/encoding/).
_WIRE_VARINT = 0
_WIRE_I64 = 1
_WIRE_LEN = 2
_WIRE_GROUP = 3
_WIRE_I32 = 5
_WIRE_TYPE_BY_FIELD_TYPE: dict[int, int] = {
    FieldDescriptor.TYPE_INT32: _WIRE_VARINT,
    FieldDescriptor.TYPE_INT64: _WIRE_VARINT,
    FieldDescriptor.TYPE_UINT32: _WIRE_VARINT,
    FieldDescriptor.TYPE_UINT64: _WIRE_VARINT,
    FieldDescriptor.TYPE_SINT32: _WIRE_VARINT,
    FieldDescriptor.TYPE_SINT64: _WIRE_VARINT,
    FieldDescriptor.TYPE_BOOL: _WIRE_VARINT,
    FieldDescriptor.TYPE_ENUM: _WIRE_VARINT,
    FieldDescriptor.TYPE_FIXED64: _WIRE_I64,
    FieldDescriptor.TYPE_SFIXED64: _WIRE_I64,
    FieldDescriptor.TYPE_DOUBLE: _WIRE_I64,
    FieldDescriptor.TYPE_STRING: _WIRE_LEN,
    FieldDescriptor.TYPE_BYTES: _WIRE_LEN,
    FieldDescriptor.TYPE_MESSAGE: _WIRE_LEN,
    FieldDescriptor.TYPE_GROUP: _WIRE_GROUP,
    FieldDescriptor.TYPE_FIXED32: _WIRE_I32,
    FieldDescriptor.TYPE_SFIXED32: _WIRE_I32,
    FieldDescriptor.TYPE_FLOAT: _WIRE_I32,
}
_NESTED_FIELD_TYPES = frozenset({FieldDescriptor.TYPE_MESSAGE, FieldDescriptor.TYPE_GROUP})


# ---------------------------------------------------------------------------
# Pure protobuf helpers (unit-tested without a client)
# ---------------------------------------------------------------------------


def wire_type_of(field: Any) -> int:
    """Return the wire type a field descriptor's scalar element encodes as."""
    return _WIRE_TYPE_BY_FIELD_TYPE[field.type]


def _is_map_field(field: Any) -> bool:
    message_type = field.message_type
    return message_type is not None and bool(message_type.GetOptions().map_entry)


def _nested_messages(field: Any, value: Any) -> list[Any]:
    """Return the message values a populated message-typed field carries."""
    if _is_map_field(field):
        value_field = field.message_type.fields_by_name["value"]
        if value_field.type not in _NESTED_FIELD_TYPES:
            return []
        return list(value.values())
    if field.is_repeated:
        return list(value)
    return [value]


def shape_pairs(message: Any, prefix: str = "") -> set[tuple[str, int]]:
    """Collect ``(field_path, wire_type)`` for every populated field, recursively.

    Repeated elements share one path, so the set describes *which* fields are
    present rather than how many or what they hold. No values are read.
    """
    pairs: set[tuple[str, int]] = set()
    for field, value in message.ListFields():
        path = f"{prefix}{field.name}"
        pairs.add((path, wire_type_of(field)))
        if field.type not in _NESTED_FIELD_TYPES:
            continue
        if _is_map_field(field):
            entry = field.message_type
            pairs.add((f"{path}.key", wire_type_of(entry.fields_by_name["key"])))
            pairs.add((f"{path}.value", wire_type_of(entry.fields_by_name["value"])))
            child_prefix = f"{path}.value."
        else:
            child_prefix = f"{path}."
        for nested in _nested_messages(field, value):
            pairs.update(shape_pairs(nested, child_prefix))
    return pairs


def shape_fingerprint(message: Any) -> str:
    """SHA-256 over the sorted populated-field shape; carries no values or ids."""
    digest = hashlib.sha256()
    for path, wire_type in sorted(shape_pairs(message)):
        digest.update(f"{path}:{wire_type}\n".encode())
    return digest.hexdigest()


def count_unknown_fields(message: Any) -> int:
    """Count unknown fields at every nesting level of a decoded message."""
    total = len(UnknownFieldSet(message))
    for field, value in message.ListFields():
        if field.type not in _NESTED_FIELD_TYPES:
            continue
        for nested in _nested_messages(field, value):
            total += count_unknown_fields(nested)
    return total


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def describe_failure(error: BaseException) -> str:
    """Render ``Class: message`` with credential-shaped text scrubbed and bounded."""
    message = scrub_secrets(str(error)).strip().replace("\n", " ")
    if len(message) > _MAX_FAILURE_DETAIL:
        message = message[: _MAX_FAILURE_DETAIL - 1] + "…"
    name = type(error).__name__
    return f"{name}: {message}" if message else name


class CanaryReport:
    """Collect step verdicts and print them in the ``OK``/``FAIL``/``SHAPE`` grammar.

    ``redact`` (the probed notebook id) is replaced with
    :data:`REDACTED_NOTEBOOK_ID` on EVERY line this report emits, whatever
    produced the text, so no present or future exception message can carry
    the id into a log or step summary.
    """

    def __init__(self, emit: Emit, *, redact: str = "") -> None:
        self._emit = emit
        self._redact = redact
        self.failures: list[str] = []

    @property
    def all_ok(self) -> bool:
        return not self.failures

    def _line(self, text: str) -> None:
        if self._redact:
            text = text.replace(self._redact, REDACTED_NOTEBOOK_ID)
        self._emit(text)

    def ok(self, step: str, detail: str = "") -> None:
        self._line(f"OK {step} {detail}".rstrip())

    def fail(self, step: str, detail: str) -> None:
        self.failures.append(step)
        self._line(f"FAIL {step} {detail}".rstrip())

    def shape(self, rpc: str, fingerprint: str) -> None:
        self._line(f"SHAPE {rpc} {fingerprint}")

    def unknown(self, rpc: str, count: int) -> None:
        self._line(f"UNKNOWN {rpc} {count}")

    def warn(self, detail: str) -> None:
        self._line(f"WARN {detail}")


class StepFailure(Exception):
    """A step's own, already-sanitized verdict text (rendered verbatim)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def run_step(report: CanaryReport, step: str, work: Awaitable[str]) -> bool:
    """Await one step; any exception becomes a sanitized ``FAIL`` line."""
    try:
        detail = await work
    except asyncio.CancelledError:
        raise
    except StepFailure as verdict:
        report.fail(step, verdict.detail)
        return False
    except Exception as error:  # noqa: BLE001 - every failure class is a canary verdict
        report.fail(step, describe_failure(error))
        return False
    report.ok(step, detail)
    return True


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _read_proto() -> Any:
    from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
        read_pb2,
    )

    return cast(Any, read_pb2)


def _chat_proto() -> Any:
    from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
        chat_pb2,
    )

    return cast(Any, chat_pb2)


def check_backends(client: Any) -> str:
    """Every public namespace must have resolved to the Android backend."""
    backends = set(client.backends.values())
    if backends != {"android"}:
        raise RuntimeError(f"Android backend was not fully selected: {sorted(backends)}")
    return "backends=android"


async def check_bearer(
    client: Any,
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> str:
    """Mint a bearer, force a refresh, and require the generation to advance.

    The forced re-mint hits gpsoauth at the same cron minute as the Web
    ``health-check`` job, which mints from the same master token, so a
    transient throttle is retried once after a short backoff. A re-mint that
    still fails with ``AuthError`` is reported as the distinct
    ``refresh-mint`` verdict so triage can tell a throttle from a
    generation-advance failure.
    """
    session = client._android_session
    provider = client._android_bearer_provider
    epoch = None if session is None else session.active_epoch
    if provider is None or epoch is None:
        raise RuntimeError("Android session is not active")
    first = await provider.get(epoch)
    first_generation = first.generation
    provider.invalidate(first_generation)
    del first
    try:
        second = await provider.get(epoch)
    except AuthError:
        await sleep(_REFRESH_RETRY_BACKOFF_SECONDS)
        try:
            second = await provider.get(epoch)
        except AuthError as error:
            raise StepFailure(f"refresh-mint {describe_failure(error)}") from None
    second_generation = second.generation
    del second
    if second_generation <= first_generation:
        raise RuntimeError(
            f"forced refresh did not advance the bearer generation "
            f"({first_generation} -> {second_generation})"
        )
    return f"generation {first_generation}->{second_generation}"


async def check_get_project(client: Any, notebook_id: str) -> str:
    """Unary ``GetProject`` through the public API; the id must round-trip."""
    notebook = await client.notebooks.get(notebook_id)
    if notebook.id != notebook_id:
        raise RuntimeError("GetProject returned a different notebook id")
    return "id round-trip"


async def check_list_chat_sessions(client: Any, notebook_id: str) -> str:
    """Unary ``ListChatSessions`` through the public API (read-only, no turn)."""
    conversation_id = await client.chat.get_conversation_id(notebook_id)
    return f"conversation={'present' if conversation_id else 'absent'}"


def raw_probes(notebook_id: str) -> list[tuple[str, str, Any, type[Any]]]:
    """The raw unary calls, built exactly as the public methods build them.

    ``GetProject`` decodes with the FULL recovered ``read_pb2.GetProjectResponse``
    (the type ``AndroidSourcesAPI.list`` uses), not the partial
    ``WireGetProjectResponse`` projection ``notebooks.get`` reads, so the
    unknown-field count is measured against the complete known schema.
    """
    read_proto = _read_proto()
    chat_proto = _chat_proto()
    return [
        (
            "GetProject",
            GET_PROJECT_METHOD,
            read_proto.GetProjectRequest(project_id=notebook_id, include_audio_overview_ids=True),
            read_proto.GetProjectResponse,
        ),
        (
            "ListChatSessions",
            LIST_CHAT_SESSIONS_METHOD,
            chat_proto.ListChatSessionsRequest(project_id=notebook_id),
            chat_proto.ListChatSessionsResponse,
        ),
    ]


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineEntry:
    """The reviewed shape + unknown count one RPC is expected to reproduce."""

    shape: str
    unknown_fields: int


Baseline = Mapping[str, BaselineEntry]


def parse_baseline(document: object) -> Baseline:
    """Validate the ``{"<rpc>": {"shape": hex, "unknown_fields": n}}`` document."""
    if not isinstance(document, dict):
        raise ValueError("baseline must be a JSON object keyed by RPC name")
    entries: dict[str, BaselineEntry] = {}
    for rpc, record in document.items():
        if not isinstance(record, dict):
            raise ValueError(f"baseline entry for {rpc!r} must be an object")
        shape = record.get("shape")
        unknown = record.get("unknown_fields")
        if not isinstance(shape, str) or len(shape) != 64 or not _is_hex(shape):
            raise ValueError(f"baseline entry for {rpc!r} needs a 64-hex 'shape'")
        if isinstance(unknown, bool) or not isinstance(unknown, int) or unknown < 0:
            raise ValueError(f"baseline entry for {rpc!r} needs a non-negative 'unknown_fields'")
        entries[str(rpc)] = BaselineEntry(shape=shape.lower(), unknown_fields=unknown)
    return entries


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdefABCDEF" for character in value)


def load_baseline(
    path: Path | None,
    report: CanaryReport,
    *,
    missing_grace_until: date | None = None,
    today: date | None = None,
) -> Baseline | None:
    """Return the baseline to compare against, or ``None`` for diagnostic mode.

    A missing file is a ``WARN`` (the first run's ``SHAPE``/``UNKNOWN`` lines are
    what a reviewer authors the file from) until ``missing_grace_until`` has
    passed, after which it is a ``FAIL`` so the drift check cannot stay inert
    indefinitely; an unreadable file is always a ``FAIL``.
    """
    if path is None:
        return None
    if not path.is_file():
        current = today or date.today()
        if missing_grace_until is not None and current > missing_grace_until:
            report.fail(
                "baseline",
                f"missing {path} and the bootstrap grace period ended {missing_grace_until}",
            )
        else:
            report.warn(f"baseline missing {path}")
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return parse_baseline(document)
    except (OSError, ValueError) as error:
        report.fail("baseline", describe_failure(error))
        return None


async def check_schema(
    report: CanaryReport,
    client: Any,
    notebook_id: str,
    baseline: Baseline | None,
) -> None:
    """Strict decode: print each response's shape and unknown count, then judge.

    With a baseline, the live shape and unknown count must both reproduce the
    reviewed values. Without one, the probe is diagnostic and always passes.
    """
    # Setup failures (no session, a proto that fails to import) must carry
    # the ``schema`` label rather than escaping to the outer ``close`` handler.
    try:
        session = client._android_session
        probes = raw_probes(notebook_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - setup failure is a verdict
        report.fail("schema", describe_failure(error))
        return
    for rpc, method, request, response_type in probes:
        step = f"schema {rpc}"
        try:
            response = await session.unary(
                method,
                request,
                replay_safe=True,
                response_type=response_type,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - transport failure is a verdict
            report.fail(step, describe_failure(error))
            continue
        shape = shape_fingerprint(response)
        unknown = count_unknown_fields(response)
        report.shape(rpc, shape)
        report.unknown(rpc, unknown)
        if baseline is None:
            report.ok(step, f"unknown_fields={unknown} (no baseline; diagnostic only)")
            continue
        expected = baseline.get(rpc)
        if expected is None:
            report.fail(step, "no baseline entry for this RPC")
            continue
        mismatches: list[str] = []
        if shape != expected.shape:
            mismatches.append(f"shape live={shape} baseline={expected.shape}")
        if unknown != expected.unknown_fields:
            mismatches.append(f"unknown_fields live={unknown} baseline={expected.unknown_fields}")
        if mismatches:
            report.fail(step, "; ".join(mismatches))
        else:
            report.ok(step, f"shape=match unknown_fields={unknown}")


async def run_canary(
    client_factory: ClientFactory,
    notebook_id: str,
    *,
    baseline_path: Path | None = None,
    missing_baseline_grace_until: date | None = None,
    out: Emit = print,
) -> int:
    """Drive every step against one client; return the process exit code."""
    report = CanaryReport(out, redact=notebook_id)
    baseline = load_baseline(
        baseline_path, report, missing_grace_until=missing_baseline_grace_until
    )
    try:
        context = client_factory()
    except Exception as error:  # noqa: BLE001 - assembly failure is a verdict
        report.fail("open", describe_failure(error))
        return 1

    entered = False
    try:
        async with context as client:
            entered = True
            try:
                report.ok("open", check_backends(client))
            except Exception as error:  # noqa: BLE001
                report.fail("open", describe_failure(error))
                return 1
            await run_step(report, "bearer", check_bearer(client))
            await run_step(report, "get_project", check_get_project(client, notebook_id))
            await run_step(
                report,
                "list_chat_sessions",
                check_list_chat_sessions(client, notebook_id),
            )
            await check_schema(report, client, notebook_id, baseline)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - open/close failure is a verdict
        report.fail("close" if entered else "open", describe_failure(error))
        return 1
    return 0 if report.all_ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_client_factory(timeout: float) -> ClientFactory:
    def factory() -> AbstractAsyncContextManager[Any]:
        from notebooklm import NotebookLMClient

        return NotebookLMClient.from_storage(backend="android", timeout=timeout)

    return factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Android gRPC drift canary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--notebook-id",
        default=None,
        help="Notebook to probe (default: $NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-RPC timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=(
            "JSON baseline of reviewed SHAPE/UNKNOWN values per RPC. Present: any "
            "difference is a FAIL. Absent file: WARN and run in diagnostic mode. "
            "Never written by this script."
        ),
    )
    parser.add_argument(
        "--missing-baseline-grace-until",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Last day a missing --baseline file is only a WARN; after it the run "
            "FAILs, so the drift check cannot stay inert once bootstrap is over."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    notebook_id = args.notebook_id or os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "")
    if not notebook_id:
        print(
            "ERROR: pass --notebook-id or set NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID",
            file=sys.stderr,
        )
        return 2
    factory = client_factory or _default_client_factory(args.timeout)
    emit = functools.partial(print, flush=True)
    return asyncio.run(
        run_canary(
            factory,
            notebook_id,
            baseline_path=args.baseline,
            missing_baseline_grace_until=args.missing_baseline_grace_until,
            out=emit,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
