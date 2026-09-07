"""The isolated auth worker settles every owner without hiding its primary error."""

from types import SimpleNamespace

import pytest

from tests._fault_server import auth_persistence_worker as worker
from tests._fault_server.common import ScenarioResult


async def test_client_close_failure_preserves_primary_and_closes_server(monkeypatch, tmp_path):
    server = worker.HttpFaultServer()
    primary = ValueError("primary")
    closed = []

    class Client:
        _lifecycle = SimpleNamespace(is_open=lambda: False)

        async def __aenter__(self):
            return self

        async def refresh_auth(self):
            raise primary

        async def close(self, **kwargs):
            closed.append(True)
            raise OSError("secondary")

    monkeypatch.setattr(worker, "HttpFaultServer", lambda: server)
    monkeypatch.setattr(worker.web, "build_fault_client", lambda *args, **kwargs: Client())
    result = ScenarioResult("web", "auth_persistence_write", "cleanup-probe")
    with pytest.raises(ValueError) as caught:
        await worker.run("write", tmp_path, result)
    assert caught.value is primary
    assert closed == [True]
    assert server._server is None and server.active_handlers == 0
    cleanup = result.events[-1]
    assert cleanup["kind"] == "cleanup"
    assert cleanup["primary_error"] == "ValueError"
    assert cleanup["cleanup_errors"] == ["OSError"]
