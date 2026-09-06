#!/usr/bin/env python3
"""Run seeded local fault cohorts through the production clients and real sockets.

Run from a source checkout with the dev extra installed::

    python scripts/stress_fault_server.py --backend both --iterations 100 \
        --seed 17 --concurrency 4 --json-report fault-report.json

A seed fixes the ordered scenario assignments, not OS scheduling or latency.
Each cohort owns its server/client; concurrency scenarios also drive multiple
operations through one shared client. No account or remote service is used.
Exit 0 means every cohort passed; 1 means a failure/timeout; 2 means bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import os
import platform
import random
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# The helpers are intentionally source-only, not part of the installed package.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests._fault_server.common import ScenarioFailure, ScenarioResult  # noqa: E402
from tests._fault_server.environment import isolated_environment  # noqa: E402

Scenario = Callable[..., Coroutine[Any, Any, ScenarioResult]]
Registry = Mapping[tuple[str, str], Scenario]
_MAX_ITERATIONS = 100_000
_MAX_CONCURRENCY = 64


@dataclass(frozen=True)
class RunConfig:
    backend: str = "both"
    seed: int = 0
    iterations: int = 100
    concurrency: int = 4
    timeout: float = 120.0
    scenario_timeout: float = 15.0
    cleanup_timeout: float = 5.0
    scenarios: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.backend not in {"web", "android", "both"}:
            raise ValueError("backend must be web, android, or both")
        for name, maximum in (("iterations", _MAX_ITERATIONS), ("concurrency", _MAX_CONCURRENCY)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        for name in ("timeout", "scenario_timeout", "cleanup_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")


@dataclass(frozen=True)
class OperationPlan:
    operation_id: str
    backend: str
    scenario: str


def load_registry(backend: str) -> dict[tuple[str, str], Scenario]:
    """Import only selected transports, after environment isolation is active."""
    registry: dict[tuple[str, str], Scenario] = {}
    for selected in ("web", "android") if backend == "both" else (backend,):
        module = importlib.import_module(f"tests._fault_server.{selected}_scenarios")
        names = module.SCENARIOS
        if not isinstance(names, tuple) or not names or tuple(sorted(set(names))) != names:
            raise ValueError(f"{selected} SCENARIOS must be a nonempty sorted unique tuple")
        for name in names:
            if not isinstance(name, str) or not name:
                raise ValueError(f"invalid {selected} scenario name")
            registry[selected, name] = module.run_scenario
    return registry


def build_plan(config: RunConfig, registry: Registry) -> list[OperationPlan]:
    """Shuffle complete decks so every available fault recurs under load."""
    candidates = sorted(
        key for key in registry if config.backend == "both" or key[0] == config.backend
    )
    if config.scenarios:
        unknown = set(config.scenarios) - {name for _, name in candidates}
        if unknown:
            raise ValueError(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        candidates = [key for key in candidates if key[1] in config.scenarios]
    if not candidates:
        raise ValueError("no scenarios selected")
    rng = random.Random(config.seed)
    plan: list[OperationPlan] = []
    while len(plan) < config.iterations:
        deck = candidates.copy()
        rng.shuffle(deck)
        for backend, scenario in deck:
            plan.append(OperationPlan(f"operation-{len(plan):06d}", backend, scenario))
            if len(plan) == config.iterations:
                break
    return plan


def _source_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _validate_evidence(result: ScenarioResult) -> None:
    """Require recorded plans and checks, not only a claimed passing summary."""
    if not result.checks or not all(value is True for value in result.checks.values()):
        raise ValueError("scenario completed without nonempty passing invariant checks")
    if not any(event.get("kind") == "plan" for event in result.events):
        raise ValueError("scenario completed without a recorded fault plan")
    recorded: dict[str, bool] = {}
    for event in result.events:
        if event.get("kind") != "check":
            continue
        name = event.get("name")
        if not isinstance(name, str) or name in recorded or event.get("passed") is not True:
            raise ValueError("scenario has duplicate, invalid, or failed check evidence")
        recorded[name] = True
    if recorded != result.checks:
        raise ValueError("scenario check summary does not match recorded evidence")
    # Validate even direct event-list mutations by scenario code.
    json.dumps({"events": result.events, "checks": result.checks}, allow_nan=False)


async def run_stress(config: RunConfig, registry: Registry) -> dict[str, Any]:
    """Run a fixed worker pool and retain diagnostics for every planned cohort."""
    plans = build_plan(config, registry)
    results = [ScenarioResult(p.backend, p.scenario, p.operation_id) for p in plans]
    operations: list[dict[str, Any]] = [
        {**asdict(plan), "status": "not_started", "events": result.events, "checks": result.checks}
        for plan, result in zip(plans, results, strict=True)
    ]
    report: dict[str, Any] = {
        "schema_version": 1,
        "source_revision": _source_revision(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config": asdict(config),
        "registry": [{"backend": b, "scenario": s} for b, s in sorted(registry)],
        "plan": [asdict(plan) for plan in plans],
        "operations": operations,
        "failures": [],
        "reproducibility": "Seed and plan fix logical faults, not OS scheduling or latency.",
    }
    started = time.monotonic()
    remaining = iter(range(len(plans)))
    scenario_tasks: set[asyncio.Task[ScenarioResult]] = set()

    async def execute(index: int) -> None:
        plan, result, operation = plans[index], results[index], operations[index]
        operation["status"] = "running"
        began = time.monotonic()
        result.record("started", **asdict(plan))
        task = asyncio.create_task(
            registry[plan.backend, plan.scenario](
                plan.scenario, operation_id=plan.operation_id, result=result
            ),
            name=f"fault-scenario-{plan.operation_id}",
        )
        scenario_tasks.add(task)
        try:
            done, _ = await asyncio.wait((task,), timeout=config.scenario_timeout)
            if not done:
                raise asyncio.TimeoutError
            returned = task.result()
            if returned is not result or (result.backend, result.scenario, result.operation_id) != (
                plan.backend,
                plan.scenario,
                plan.operation_id,
            ):
                raise ValueError("scenario returned an inconsistent result identity")
            _validate_evidence(result)
            operation["status"] = "passed"
        except asyncio.TimeoutError:
            operation["status"] = "timed_out"
            operation["error"] = f"scenario exceeded {config.scenario_timeout:g}s deadline"
        except asyncio.CancelledError:
            operation["status"] = "canceled"
            operation["error"] = "run canceled before scenario completed"
            raise
        except Exception as exc:
            operation["status"] = "failed"
            operation["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
            if isinstance(exc, ScenarioFailure) and exc.result is not result:
                operation["error"] += " (ScenarioFailure carried a different result)"
        finally:
            if not task.done():
                task.cancel()
                _, pending = await asyncio.wait((task,), timeout=config.cleanup_timeout)
                if pending:
                    operation["status"] = "failed"
                    operation["error"] = "scenario did not finish cancellation cleanup"
                    operation["elapsed_seconds"] = time.monotonic() - began
                    # This worker's cohort still occupies its slot. Stop this
                    # worker rather than admitting another cohort over the cap.
                    raise RuntimeError("scenario cleanup timed out")
            if task.done():
                # Retrieve cleanup exceptions even after timeout/cancellation.
                if not task.cancelled() and task.exception() is not None:
                    error = task.exception()
                    if operation["status"] in {"timed_out", "canceled"}:
                        operation["cleanup_error"] = f"{type(error).__name__}: {str(error)[:1000]}"
                scenario_tasks.discard(task)
            operation["elapsed_seconds"] = time.monotonic() - began

    async def worker() -> None:
        for index in remaining:
            await execute(index)

    workers = [
        asyncio.create_task(worker(), name=f"fault-worker-{index}")
        for index in range(min(config.concurrency, len(plans)))
    ]
    try:
        _, pending = await asyncio.wait(workers, timeout=config.timeout)
        if pending:
            report["failures"].append(f"run exceeded {config.timeout:g}s deadline")
    except asyncio.CancelledError:
        report["failures"].append("run interrupted")
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(workers, timeout=config.cleanup_timeout)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                report["failures"].append(f"worker failed: {task.exception()}")
        if pending:
            report["failures"].append(
                f"{len(pending)} workers did not finish cleanup within {config.cleanup_timeout:g}s"
            )
            for task in pending:
                task.cancel()
        if scenario_tasks:
            for scenario_task in scenario_tasks:
                if not scenario_task.done():
                    scenario_task.cancel()
            done_scenarios, pending_scenarios = await asyncio.wait(
                scenario_tasks, timeout=config.cleanup_timeout
            )
            for scenario_task in done_scenarios:
                if not scenario_task.cancelled():
                    scenario_task.exception()
            if pending_scenarios:
                report["failures"].append(
                    f"{len(pending_scenarios)} scenarios resisted cancellation; cleanup incomplete"
                )
    counts = {
        status: sum(operation["status"] == status for operation in operations)
        for status in ("passed", "failed", "timed_out", "canceled", "not_started", "running")
    }
    report["summary"] = {
        **counts,
        "total": len(plans),
        "elapsed_seconds": time.monotonic() - started,
        "ok": counts["passed"] == len(plans) and not report["failures"],
    }
    return report


def _run_cli(config: RunConfig, registry: Registry) -> dict[str, Any]:
    """Own the CLI loop so broken scenario cleanup cannot hang asyncio.run()."""
    loop = asyncio.new_event_loop()
    loop_errors: list[str] = []

    def exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        error = context.get("exception")
        loop_errors.append(
            f"event loop error: {context.get('message', 'unknown')}: "
            f"{type(error).__name__}: {str(error)[:1000]}"
        )
        loop.default_exception_handler(context)

    loop.set_exception_handler(exception_handler)
    report: dict[str, Any] | None = None
    main_task = loop.create_task(run_stress(config, registry))
    try:
        try:
            report = loop.run_until_complete(main_task)
        except KeyboardInterrupt:
            main_task.cancel()
            report = loop.run_until_complete(main_task)
        return report
    finally:
        pending = asyncio.all_tasks(loop)
        if pending and report is not None:
            report["failures"].append(
                "CLI found leaked tasks after run: " + ", ".join(t.get_name() for t in pending)
            )
            report["summary"]["ok"] = False
        for task in pending:
            task.cancel()
        if pending:
            done, survivors = loop.run_until_complete(
                asyncio.wait(pending, timeout=config.cleanup_timeout)
            )
            for task in done:
                if not task.cancelled():
                    task.exception()
            if survivors and report is not None:
                report["failures"].append(
                    "CLI shutdown left pending tasks: " + ", ".join(t.get_name() for t in survivors)
                )
                report["summary"]["ok"] = False
        if loop_errors and report is not None:
            report["failures"].extend(loop_errors)
            report["summary"]["ok"] = False
        loop.close()


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Atomically replace the report so interruption leaves the previous file usable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = stream.name
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("web", "android", "both"), default="both")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--scenario-timeout", type=float, default=15)
    parser.add_argument("--json-report", type=Path, default=Path("fault-report.json"))
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--list-scenarios", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = RunConfig(
            backend=args.backend,
            seed=args.seed,
            iterations=args.iterations,
            concurrency=args.concurrency,
            timeout=args.timeout,
            scenario_timeout=args.scenario_timeout,
            scenarios=tuple(args.scenario),
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        with isolated_environment():
            registry = load_registry(config.backend)
            # Validate selection before creating an event loop or opening sockets.
            build_plan(config, registry)
            if args.list_scenarios:
                for backend, scenario in sorted(registry):
                    print(f"{backend}/{scenario}")
                return 0
            report = _run_cli(config, registry)
    except ValueError as exc:
        parser.error(str(exc))
    except (Exception, KeyboardInterrupt) as exc:
        report = {
            "schema_version": 1,
            "config": asdict(config),
            "failures": [f"{type(exc).__name__}: {str(exc)[:1000]}"],
            "summary": {"ok": False},
        }
    try:
        write_report(args.json_report, report)
    except (OSError, TypeError, ValueError) as exc:
        print(f"Could not write fault report: {exc}", file=sys.stderr)
        return 1
    summary = report["summary"]
    print(
        f"{'PASS' if summary['ok'] else 'FAIL'} seed={config.seed} "
        f"passed={summary.get('passed', 0)}/{config.iterations} report={args.json_report}"
    )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
