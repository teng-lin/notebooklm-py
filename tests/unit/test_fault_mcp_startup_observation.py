"""Report polling tolerates replacement races without hiding broken evidence."""

import asyncio
import errno
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests._fault_server.mcp_startup_report import observe_report


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(13, "Access denied")])
async def test_report_observation_retries_transient_open_failure(tmp_path, monkeypatch, error):
    path = tmp_path / "report.json"
    read = Mock(side_effect=[error, '{"waiters": 0}', '{"waiters": 1}'])
    monkeypatch.setattr(Path, "read_text", read)

    assert await observe_report(path, lambda state: state["waiters"] >= 1) == {"waiters": 1}
    assert read.call_count == 3
    read.assert_called_with(encoding="utf-8")


async def test_report_observation_keeps_deadline_when_access_stays_denied(tmp_path, monkeypatch):
    read = Mock(side_effect=PermissionError(13, "Access denied"))
    monkeypatch.setattr(Path, "read_text", read)

    with pytest.raises(asyncio.TimeoutError):
        await observe_report(tmp_path / "report.json", lambda state: True, timeout=0.05)
    assert read.call_count >= 1


async def test_report_observation_rejects_corrupt_json(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        await observe_report(path, lambda state: True)


async def test_report_observation_propagates_other_io_failures(tmp_path, monkeypatch):
    error = OSError(errno.EIO, "Read failed")
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=error))

    with pytest.raises(OSError) as caught:
        await observe_report(tmp_path / "report.json", lambda state: True)
    assert caught.value is error
