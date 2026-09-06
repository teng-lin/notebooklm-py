"""Isolated Click runner for the adapter fault scenarios.

The parent scenario may run concurrently with other stress cohorts.  Click's
test runner changes process-global stdio and CLI authentication is patched only
for this test fixture, so this module runs both in a short-lived child process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from notebooklm._app.errors import unconfirmed_hint
from notebooklm.cli import helpers as cli_helpers
from notebooklm.notebooklm_cli import cli

from .adapter_scenarios import _CREATE, _client_factory, _enqueue_create, _enqueue_read
from .http import HttpFaultServer
from .web import synthetic_auth


async def _invoke(args: list[str], factory: Any) -> Any:
    def run() -> Any:
        def factory_ignoring_auth(*_args: Any, **_kwargs: Any) -> Any:
            return factory()

        with patch.object(cli_helpers, "get_auth_tokens", return_value=synthetic_auth()):
            return CliRunner().invoke(cli, args, obj={"client_factory": factory_ignoring_auth})

    return await asyncio.to_thread(run)


async def _run(scenario: str) -> dict[str, Any]:
    server = HttpFaultServer()
    if scenario == "read":
        _enqueue_read(server)
        fault_args = ["list", "--json"]
        retries = 0
    elif scenario == "create":
        _enqueue_create(server)
        fault_args = ["create", "Committed once", "--json"]
        retries = 5
    else:
        raise ValueError(f"unknown CLI worker scenario: {scenario}")

    opened: list[Any] = []
    recovery: list[str] = []
    await server.__aenter__()
    report: dict[str, Any] | None = None
    try:
        before = await _invoke(
            ["list", "--json"], _client_factory(server, opened, server_retries=0)
        )
        outcome = await _invoke(
            fault_args,
            _client_factory(server, opened, server_retries=retries, recovery=recovery),
        )
        before_body = json.loads(before.output)
        body = json.loads(outcome.output)
        report = {
            "preflight_exit": before.exit_code,
            "preflight_id": before_body["notebooks"][0]["id"],
            "exit_code": outcome.exit_code,
            "code": body.get("code"),
            "unconfirmed": body.get("unconfirmed"),
            "commit_state": body.get("commit_state"),
            "hint_is_unconfirmed": str(body.get("hint", "")) == unconfirmed_hint(None),
            "recovery_ids": recovery,
            "request_count": len(server.journal),
            "create_requests": sum(record.route == _CREATE for record in server.journal),
            "committed": list(server.committed),
            "http_trace": [
                {
                    "sequence": record.sequence,
                    "method": record.route.method,
                    "host": record.route.host,
                    "path": record.route.path,
                    "rpc_id": record.route.rpc_id,
                    "action": record.action,
                }
                for record in server.journal
            ],
        }
    finally:
        await server.aclose()
    if report is None:
        raise AssertionError("CLI worker did not produce an adapter report")
    # The returned mapping deliberately excludes request bodies, Click output,
    # and exception text.  It contains only fixed result fields after settle.
    report["clean"] = (
        server.active_handlers == 0
        and not server.errors
        and server.remaining() == 0
        and len(opened) == 2
        and all(not client._lifecycle.is_open() for client in opened)
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("read", "create"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(_run(args.scenario))
    args.report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
