"""Separate subprocess lane for process death, never a power-loss durability claim."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from notebooklm._auth.paths import _storage_state_lock_path
from notebooklm._auth.storage_lock import LockRequest, LockState, StorageLockManager

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Transfer
from .web import rpc_response
from .web_transfers import (
    ASSET,
    FINAL,
    GET_NOTEBOOK,
    LIST_ASSETS,
    MEDIA,
    REGISTER,
    SOURCE,
    UPLOAD,
    _audio_rows,
    _registration,
    _start,
)
from .web_workflows import _source_list_response

SCENARIOS = ("storage_before_replace", "download_before_publish", "commit_before_ack")
_COMMON = [
    "cleanup_succeeded",
    "child_live_at_boundary",
    "abrupt_exit",
    "no_completion_receipt",
    "restart_succeeded",
    "children_settled",
    "listener_settled",
    "service_clean",
    "temporary_state_removed",
]
REQUIRED_CHECKS = {
    "storage_before_replace": [
        *_COMMON,
        "complete_staging",
        "lock_held_before_kill",
        "lock_released_after_kill",
        "old_credentials_preserved",
        "fresh_reader_recovers",
        "orphan_retention_explicit",
    ],
    "download_before_publish": [
        *_COMMON,
        "complete_staging",
        "old_destination_preserved",
        "fresh_download_recovers",
        "orphan_retention_explicit",
        "bounded_read_requests",
        "no_remote_mutation",
    ],
    "commit_before_ack": [
        *_COMMON,
        "independent_commit",
        "ack_held_before_kill",
        "read_only_reconciliation",
        "no_duplicate_commit",
        "exact_dispatches",
    ],
}


async def _spawn(mode: str, directory: Path, processes: list, port: int = 0):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
    env["NOTEBOOKLM_PROFILE"] = "agent-process-death"
    env["NOTEBOOKLM_HOME"] = str(directory / "home")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests._fault_server.process_death_worker",
        mode,
        str(directory),
        "--port",
        str(port),
        cwd=root,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    processes.append(process)
    return process


async def _event(process) -> dict:
    line = await asyncio.wait_for(process.stdout.readline(), 8)
    if not line:
        raise AssertionError("child ended before expected boundary")
    return json.loads(line)


async def _kill(result: ScenarioResult, process) -> None:
    result.require("child_live_at_boundary", process.returncode is None)
    process.kill()  # SIGKILL on POSIX; TerminateProcess on Windows.
    output, _ = await asyncio.wait_for(process.communicate(), 3)
    result.require(
        "abrupt_exit", process.returncode == (-signal.SIGKILL if os.name == "posix" else 1)
    )
    result.require("no_completion_receipt", not output.strip())
    result.record(
        "termination",
        mechanism="SIGKILL" if os.name == "posix" else "TerminateProcess",
        exit_code=process.returncode,
        completion_receipt=False,
        caller_outcome="unobserved",
        cleanup_expected=False,
    )


async def _restart(result: ScenarioResult, mode: str, directory: Path, processes: list, port=0):
    process = await _spawn(mode, directory, processes, port)
    output, _ = await asyncio.wait_for(process.communicate(), 8)
    events = [json.loads(line) for line in output.splitlines()]
    result.record("restart", events=events, exit_code=process.returncode)
    result.require("restart_succeeded", process.returncode == 0 and len(events) == 1)
    return events[0]


def _lock_state(path: Path) -> LockState:
    request = LockRequest(_storage_state_lock_path(path), blocking=False, operation="fault-probe")
    with StorageLockManager().acquire(request) as state:
        return state


async def _storage(result, directory, processes):
    path = directory / "storage_state.json"
    old = {
        "cookies": [
            {
                "name": "SID",
                "value": "synthetic-old",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    process = await _spawn("storage_crash", directory, processes)
    event = await _event(process)
    result.record("boundary", event=event)
    staging = list(directory.glob(".storage_state.json.*.tmp"))
    result.require(
        "complete_staging",
        event["kind"] == "staged"
        and len(staging) == 1
        and json.loads(staging[0].read_text())["cookies"][0]["value"] == "synthetic-new"
        and hashlib.sha256(staging[0].read_bytes()).hexdigest() == event["sha256"],
    )
    result.require("lock_held_before_kill", _lock_state(path) is LockState.CONTENDED)
    await _kill(result, process)
    result.require(
        "old_credentials_preserved",
        path.read_bytes() == before and json.loads(path.read_text()) == old,
    )
    result.require("lock_released_after_kill", _lock_state(path) is LockState.HELD)
    recovered = await _restart(result, "storage_recover", directory, processes)
    result.require(
        "fresh_reader_recovers",
        recovered.get("saved") is True
        and json.loads(path.read_text())["cookies"][0]["value"] == "synthetic-new",
    )
    result.require(
        "orphan_retention_explicit",
        staging[0].exists() and list(directory.glob(".storage_state.json.*.tmp")) == staging,
    )
    result.record(
        "retention", orphan_files=1, blocks_restart=False, removed_by="parent_temporary_directory"
    )


async def _download(result, directory, processes, server):
    server.enqueue(
        LIST_ASSETS,
        *[Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [_audio_rows()])) for _ in range(2)],
    )
    server.enqueue(
        ASSET, *[Reply(body=MEDIA, headers={"content-type": "audio/wav"}) for _ in range(2)]
    )
    await server.__aenter__()
    destination = directory / "asset.wav"
    destination.write_bytes(b"previous asset")
    process = await _spawn("download_crash", directory, processes, server.address[1])
    event = await _event(process)
    result.record("boundary", event=event)
    staging = list(directory.glob("asset.wav.*.tmp"))
    result.require(
        "complete_staging",
        event["kind"] == "staged"
        and len(staging) == 1
        and staging[0].read_bytes() == MEDIA
        and event["sha256"] == hashlib.sha256(MEDIA).hexdigest(),
    )
    await _kill(result, process)
    result.require("old_destination_preserved", destination.read_bytes() == b"previous asset")
    recovered = await _restart(result, "download_recover", directory, processes, server.address[1])
    result.require(
        "fresh_download_recovers",
        recovered.get("valid") is True and destination.read_bytes() == MEDIA,
    )
    result.require(
        "orphan_retention_explicit",
        staging[0].exists() and list(directory.glob("asset.wav.*.tmp")) == staging,
    )
    result.record(
        "retention", orphan_files=1, blocks_restart=False, removed_by="parent_temporary_directory"
    )
    result.require(
        "bounded_read_requests",
        len(server.journal) == 4 and sum(record.route == ASSET for record in server.journal) == 2,
    )
    result.require("no_remote_mutation", not server.committed)


async def _commit(result, directory, processes, server):
    payload = b"synthetic upload before process termination\n"
    (directory / "source.txt").write_bytes(payload)
    server.enqueue(REGISTER, _registration())
    server.enqueue(UPLOAD, _start())
    server.enqueue(
        FINAL,
        Transfer(
            require_session=True,
            expected_size=len(payload),
            expected_digest=hashlib.sha256(payload).hexdigest(),
            commit_id=SOURCE,
            gates={"commit": "ack"},
            response=Reply(),
        ),
    )
    server.enqueue(GET_NOTEBOOK, Reply(body=_source_list_response(SOURCE)))
    await server.__aenter__()
    process = await _spawn("commit_crash", directory, processes, server.address[1])
    event = await _event(process)
    result.record("boundary", event=event)
    await server.wait_for_gate("ack", timeout=8)
    final = [record for record in server.journal if record.route == FINAL]
    result.require(
        "independent_commit",
        server.committed == [SOURCE]
        and len(final) == 1
        and final[0].body_complete
        and final[0].body_digest == hashlib.sha256(payload).hexdigest(),
    )
    result.require(
        "ack_held_before_kill",
        event["kind"] == "operation_started"
        and final[0].response_status is None
        and not server.gate("ack").is_set(),
    )
    await _kill(result, process)
    server.release("ack")
    recovered = await _restart(result, "reconcile", directory, processes, server.address[1])
    result.require("read_only_reconciliation", recovered.get("candidate_ids") == [SOURCE])
    result.require("no_duplicate_commit", server.committed == [SOURCE])
    result.require(
        "exact_dispatches",
        len(server.journal) == 4
        and all(
            sum(record.route == route for record in server.journal) == 1
            for route in (REGISTER, UPLOAD, FINAL, GET_NOTEBOOK)
        ),
    )
    result.record(
        "reconciliation",
        automatic_replay=False,
        durable_receipt=False,
        prior_caller_outcome="unobserved",
        candidate_count=1,
    )


async def run_scenario(name: str) -> ScenarioResult:
    if name not in SCENARIOS:
        raise ValueError("unknown process-death scenario")
    result = ScenarioResult("web", name, f"pytest-process-{name}")
    result.record(
        "plan",
        required_checks=REQUIRED_CHECKS[name],
        lane="pytest-only-process-death",
        child_boundary_timeout=8,
        child_http_timeout=20,
        restart_timeout=8,
        cleanup_timeout=3,
        scenario_watchdog=25,
        commit_limit=1 if name == "commit_before_ack" else 0,
        request_limit=0 if name == "storage_before_replace" else 4,
        gates=["ack"] if name == "commit_before_ack" else ["child-staged"],
    )
    processes: list[asyncio.subprocess.Process] = []
    primary: BaseException | None = None
    cleanup_errors: list[str] = []
    server = HttpFaultServer(hosts=["lh3.googleusercontent.com"])
    with tempfile.TemporaryDirectory(prefix="fault-process-death-") as temp:
        try:
            if name == "storage_before_replace":
                await _storage(result, Path(temp), processes)
            elif name == "download_before_publish":
                await _download(result, Path(temp), processes, server)
            else:
                await _commit(result, Path(temp), processes, server)
        except BaseException as error:
            primary = error
            result.record("failure", error_type=type(error).__name__)
            raise
        finally:
            server.release("ack")
            for process in processes:
                try:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.communicate(), 3)
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
            try:
                await asyncio.wait_for(server.aclose(), 3)
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
            result.record(
                "http_trace",
                requests=[
                    {
                        "sequence": item.sequence,
                        "method": item.route.method,
                        "rpc_id": item.route.rpc_id,
                        "body_bytes": item.body_bytes,
                        "body_digest": item.body_digest,
                        "body_complete": item.body_complete,
                        "response_status": item.response_status,
                    }
                    for item in server.journal
                ],
                committed=list(server.committed),
            )
            result.record(
                "cleanup",
                child_exit_codes=[p.returncode for p in processes],
                active_handlers=server.active_handlers,
                errors=list(server.errors),
                cleanup_errors=cleanup_errors,
                primary_error=None if primary is None else type(primary).__name__,
            )
            if primary is None:
                result.require("cleanup_succeeded", not cleanup_errors)
    result.require("temporary_state_removed", not Path(temp).exists())
    result.require("children_settled", all(p.returncode is not None for p in processes))
    result.require("listener_settled", server.active_handlers == 0)
    result.require(
        "service_clean",
        not server.errors and server.remaining() == 0 and server.unobserved_required_gates == 0,
    )
    return result
