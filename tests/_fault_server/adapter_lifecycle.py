"""Process boundary for live adapter ownership cohorts.

MCP's existing download counter belongs to one serving process. Each cohort
therefore owns a process, its listeners, and its static environment from launch;
no environment/global substitution spans awaits in the parent stress runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from functools import partial
from pathlib import Path

from .common import ScenarioResult


async def live_case(result: ScenarioResult, *, name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="fault-adapter-report-") as directory:
        report = Path(directory) / "report.json"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests._fault_server.adapter_lifecycle_worker",
            "--scenario",
            name,
            "--report",
            str(report),
            env={
                **os.environ,
                "NOTEBOOKLM_SERVER_TOKEN": "adapter-fault-token",
                "NOTEBOOKLM_MCP_CHAT_JOB_TIMEOUT": "3",
                # Even forced process teardown leaves all cohort-owned spools
                # under the parent directory, whose finally removes them.
                "TMPDIR": directory,
                "TMP": directory,
                "TEMP": directory,
            },
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), 10)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if report.is_file():
                worker = json.loads(report.read_text(encoding="utf-8"))
                for event in worker["events"]:
                    kind = event.pop("kind")
                    result.record("worker_plan" if kind == "plan" else kind, **event)
                result.checks.update(worker["checks"])
            result.record(
                "worker_cleanup",
                process_settled=process.returncode is not None,
                report_written=report.is_file(),
            )
        result.require("live_worker_succeeded", process.returncode == 0)
        result.require("live_worker_report_written", report.is_file())
        result.require(
            "live_worker_checks_passed", bool(worker["checks"]) and all(worker["checks"].values())
        )


IMPLEMENTATIONS = {
    name: partial(live_case, name=name)
    for name in (
        "adapter_rest_download_disconnect",
        "adapter_mcp_download_disconnect",
        "adapter_mcp_chat_start_disconnect",
    )
}
