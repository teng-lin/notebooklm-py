"""Native subprocess EOF/late-message ownership independent of OS scheduling."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import StdioServerParameters
from pydantic import ValidationError

from tests._fault_server import mcp_stdio_session as transport

pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.mark.parametrize(
    ("primary_failure", "invalid_frame"), [(False, False), (True, False), (False, True)]
)
async def test_stdio_drains_late_notification_after_session_closes(
    tmp_path: Path, monkeypatch, primary_failure, invalid_frame
):
    gate = tmp_path / "received"
    # Emit a protocol message only AFTER the transport closes stdin. Keep the
    # child alive until that exact message reaches the shutdown receiver.
    script = """
import json, sys, time
from pathlib import Path
sys.stdin.read()
print("not-json" if sys.argv[2] == "invalid" else json.dumps({"jsonrpc":"2.0","method":"notifications/tools/list_changed"}), flush=True)
deadline = time.monotonic() + 8
while not Path(sys.argv[1]).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(2)
    time.sleep(0.01)
"""
    describe = transport._describe_message

    def observed_message(message):
        evidence = describe(message)
        gate.touch()
        return evidence

    monkeypatch.setattr(transport, "_describe_message", observed_message)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", script, str(gate), "invalid" if invalid_frame else "valid"],
    )
    primary = ValueError("original caller failure")
    with (tmp_path / "stderr.log").open("w") as errors:
        if primary_failure:
            with pytest.raises(ValueError) as caught:
                async with transport.stdio_session(params, tmp_path, errors):
                    raise primary
            assert caught.value is primary
        elif invalid_frame:
            with pytest.raises(ValidationError):
                async with transport.stdio_session(params, tmp_path, errors):
                    pass
        else:
            async with transport.stdio_session(params, tmp_path, errors):
                pass
    report = json.loads((tmp_path / "stdio-cleanup.json").read_text())
    assert gate.exists()
    assert report["drainer_settled"]
    assert report["failures"] == (
        [{"step": "late_protocol", "error_type": "ValidationError"}] if invalid_frame else []
    )
    assert report["late_message_count"] == 1
    assert report["late_messages"] == (
        [{"kind": "ValidationError"}]
        if invalid_frame
        else [{"kind": "JSONRPCNotification", "method": "notifications/tools/list_changed"}]
    )
    assert report["primary_error"] == ("ValueError" if primary_failure else None)
