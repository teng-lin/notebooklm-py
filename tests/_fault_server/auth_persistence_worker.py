"""Isolated public-refresh composition over real sockets and atomic storage.

Filesystem substitutions are confined to this child process. No patch spans a
concurrent parent cohort; the real profile lock, merge and atomic writer still run.
Only labels, counts and boolean evidence are exported.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from notebooklm import _atomic_io
from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_store import ProfileStore
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

from . import web
from .common import ScenarioResult
from .environment import isolated_environment
from .http import HttpFaultServer, Reply, Route, Stall

_OLD = "fault-persistence-old-secret"
_NEW = "fault-persistence-new-secret"
_ERROR = "fault-persistence-error-secret"
_HOME = Route.homepage()
_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def run(variant: str, directory: Path, result: ScenarioResult) -> ScenarioResult:
    required = (
        "refresh_remains_usable",
        "local_fault_observed",
        "previous_file_preserved",
        "previous_file_parseable",
        "staging_removed",
        "failure_observable",
        "logs_exclude_secrets",
        "recovery_refresh",
        "fresh_reader_observes_commit",
        "same_client_probe",
        "exact_network_work",
        "no_remote_mutation",
        "client_closed",
        "server_settled",
    )
    sequence = variant == "sequence"
    if sequence:
        required += (
            "failed_save_baseline_unchanged",
            "cancelled_read_settled",
            "close_with_fault_preserves_state",
            "reopen_keeps_retryable_baseline",
            "old_generation_transport_closed",
            "recovered_save_advances_order",
            "cancelled_and_recovered_auth_routed",
        )
    result.record(
        "plan",
        required_checks=list(required),
        faults=[variant],
        operation_timeout=8,
        cleanup_timeout=3,
        pytest_only=True,
    )
    storage = directory / "storage_state.json"
    jar = CookieJar(
        tuple(
            Cookie(name, ".google.com", "/", _OLD, secure=True)
            for name in ("SID", "__Secure-1PSIDTS")
        )
    )
    storage.write_text(json.dumps(jar.to_storage_state()), encoding="utf-8")
    original = storage.read_bytes()
    auth = AuthTokens(
        cookies={(cookie.name, cookie.domain, cookie.path): cookie.value for cookie in jar},
        csrf_token=web.OLD_CSRF,
        session_id=web.OLD_SESSION,
        cookie_jar=jar.to_httpx(),
        storage_path=storage,
    )
    server = HttpFaultServer()
    response = web.homepage_response()
    server.enqueue(
        _HOME,
        *[
            Reply(
                body=response,
                headers={"Set-Cookie": f"SID={_NEW}; Domain=.google.com; Path=/; Secure"},
            )
            for _ in range(2)
        ],
    )
    if sequence:
        server.enqueue(
            _READ,
            Stall(
                "headers",
                "cancel-read",
                Reply(body=web.list_response(_READ.rpc_id or "", [("cancelled", "Read")])),
            ),
        )
    server.enqueue(
        _READ, Reply(body=web.list_response(_READ.rpc_id or "", [("recovered", "Read")]))
    )
    logs = _Logs()
    logger = logging.getLogger("notebooklm.auth")
    logger.addHandler(logs)
    faults: list[str] = []
    real_dump = json.dump
    real_replace = _atomic_io.replace_file_atomically

    def failed_dump(data: Any, handle: Any, **kwargs: Any) -> None:
        if Path(handle.name).parent == directory:
            handle.write('{"cookies":[')
            handle.flush()
            faults.append("nonzero_write")
            raise OSError(errno.ENOSPC, _ERROR)
        real_dump(data, handle, **kwargs)

    def failed_replace(source: Path, destination: Path) -> None:
        if destination == storage:
            # Observe real complete JSON staging before refusing publication.
            json.loads(source.read_text(encoding="utf-8"))
            faults.append("complete_staging")
            raise PermissionError(errno.EACCES, _ERROR)
        real_replace(source, destination)

    client = None
    primary_error = None
    pending: asyncio.Task | None = None
    try:
        await server.__aenter__()
        with patch.object(web, "synthetic_auth", return_value=auth):
            client = web.build_fault_client(server, timeout=3, server_error_max_retries=0)
        await client.__aenter__()
        persistence = client._web_runtime.cookie_persistence
        key = ProfileStore(storage).ordering_key
        initial_state = persistence._states[key]
        baseline_before = initial_state.baseline
        sequence_before = initial_state.last_applied_sequence
        old_transport = client._web_runtime.kernel.http_client
        target, name, replacement = (
            (json, "dump", failed_dump)
            if variant == "write"
            else (_atomic_io, "replace_file_atomically", failed_replace)
        )
        with patch.object(target, name, replacement):
            refreshed = await asyncio.wait_for(client.refresh_auth(), 8)
            result.require(
                "refresh_remains_usable",
                refreshed.csrf_token == web.NEW_CSRF
                and refreshed.cookie_jar is not None
                and refreshed.cookie_jar.get("SID", domain=".google.com") == _NEW,
            )
            result.require(
                "local_fault_observed",
                faults == ["nonzero_write" if variant == "write" else "complete_staging"],
            )
            result.require("previous_file_preserved", storage.read_bytes() == original)
            previous = ProfileStore(storage).read_document().cookies()
            result.require("previous_file_parseable", any(c.value == _OLD for c in previous))
            result.require("staging_removed", not list(directory.glob("*.tmp")))
            result.require(
                "failure_observable",
                any("Failed to write updated cookies" in message for message in logs.messages),
            )
            if sequence:
                state = persistence._states[key]
                result.require(
                    "failed_save_baseline_unchanged",
                    state.baseline == baseline_before
                    and state.last_applied_sequence == sequence_before,
                )
                pending = asyncio.create_task(client.notebooks.list())
                await server.wait_for_gate("cancel-read", timeout=3)
                pending.cancel()
                outcome = await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)
                result.require(
                    "cancelled_read_settled",
                    pending.done() and isinstance(outcome[0], asyncio.CancelledError),
                )
                fault_count = len(faults)
                await asyncio.wait_for(client.close(drain=False), 3)
                result.require(
                    "close_with_fault_preserves_state",
                    len(faults) > fault_count
                    and storage.read_bytes() == original
                    and state.baseline == baseline_before
                    and state.last_applied_sequence == sequence_before,
                )
                result.require(
                    "old_generation_transport_closed",
                    old_transport is not None and old_transport.is_closed,
                )
                server.release("cancel-read")
                await asyncio.wait_for(client.__aenter__(), 3)
                reopened = persistence._states[key]
                result.require(
                    "reopen_keeps_retryable_baseline",
                    reopened.baseline == baseline_before
                    and reopened.last_applied_sequence == sequence_before
                    and client._web_runtime.kernel.http_client is not old_transport,
                )
                result.record(
                    "sequence",
                    states=[
                        "refresh-save-failed",
                        "read-dispatched",
                        "read-cancelled",
                        "close-save-failed",
                        "reopened",
                    ],
                    failed_save_attempts=len(faults),
                    cancelled_calls=1,
                )
        recovered = await asyncio.wait_for(client.refresh_auth(), 8)
        result.require("recovery_refresh", recovered.csrf_token == web.NEW_CSRF)
        if sequence:
            state = persistence._states[key]
            result.require(
                "recovered_save_advances_order",
                state.baseline != baseline_before and state.last_applied_sequence > sequence_before,
            )
        fresh = ProfileStore(storage).read_document().cookies()
        result.require("fresh_reader_observes_commit", any(c.value == _NEW for c in fresh))
        probe = await asyncio.wait_for(client.notebooks.list(), 8)
        result.require("same_client_probe", [item.id for item in probe] == ["recovered"])
        result.require(
            "exact_network_work",
            [record.route for record in server.journal]
            == ([_HOME, _READ, _HOME, _READ] if sequence else [_HOME, _HOME, _READ]),
        )
        result.require("no_remote_mutation", not server.committed)
        if sequence:
            reads = [record for record in server.journal if record.route == _READ]
            result.require(
                "cancelled_and_recovered_auth_routed",
                all(
                    record.csrf == web.NEW_CSRF
                    and record.session_id == web.NEW_SESSION
                    and record.cookie_values.get("SID") == _NEW
                    for record in reads
                ),
            )
        result.record(
            "http_trace",
            requests=len(server.journal),
            refreshes=2,
            response_digest=hashlib.sha256(response).hexdigest(),
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if pending is not None:
            if not pending.done():
                pending.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)
            except BaseException as error:
                cleanup_errors.append(error)
        server.release("cancel-read")
        if client is not None:
            try:
                await asyncio.wait_for(client.close(drain=False), 3)
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            await asyncio.wait_for(server.aclose(), 3)
        except BaseException as error:
            cleanup_errors.append(error)
        logger.removeHandler(logs)
        result.record(
            "cleanup",
            client_closed=client is None or not client._lifecycle.is_open(),
            active_handlers=server.active_handlers,
            cleanup_errors=[type(error).__name__ for error in cleanup_errors],
            primary_error=None if primary_error is None else type(primary_error).__name__,
        )
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0]
    result.require("client_closed", client is not None and not client._lifecycle.is_open())
    result.require(
        "server_settled",
        server.active_handlers == 0 and not server.errors and server.remaining() == 0,
    )
    result.require(
        "logs_exclude_secrets",
        all(
            secret not in message
            for secret in (
                _OLD,
                _NEW,
                _ERROR,
                web.OLD_CSRF,
                web.OLD_SESSION,
                web.NEW_CSRF,
                web.NEW_SESSION,
            )
            for message in logs.messages
        ),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("write", "replace", "sequence"), required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = ScenarioResult("web", f"auth_persistence_{args.variant}", "auth-persistence-child")
    failed = False
    try:
        with isolated_environment():
            asyncio.run(run(args.variant, args.directory, result))
    except BaseException as error:
        failed = True
        result.record("worker_error", error_type=type(error).__name__)
    finally:
        args.report.write_text(
            json.dumps({"events": result.events, "checks": result.checks}), encoding="utf-8"
        )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
