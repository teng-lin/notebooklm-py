"""Unit coverage for the shared deployed-server smoke driver."""

from __future__ import annotations

import pytest

from notebooklm.mcp._smoke import (
    DOWNLOADABLE_ARTIFACT_TYPES,
    parse_args,
    pick_downloadable_artifact,
)


def test_parse_args_uses_current_studio_vocabulary() -> None:
    args = parse_args(
        [
            "--base-url",
            "https://mcp.example.com/",
            "--bearer",
            "token",
            "--notebook",
            "nb-1",
            "--artifact-type",
            "slide-deck",
        ]
    )
    assert args.artifact_type == "slide-deck"
    assert "slide_deck" not in DOWNLOADABLE_ARTIFACT_TYPES


def test_pick_downloadable_artifact_skips_notes_and_pending_items() -> None:
    selected = pick_downloadable_artifact(
        [
            {"id": "note", "type": "note", "status_label": "ready"},
            {"id": "pending", "type": "report", "status_label": "processing"},
            {"id": "ready", "type": "report", "status_label": "completed"},
        ],
        backend="web",
    )
    assert selected == {"id": "ready", "type": "report", "status_label": "completed"}


def test_pick_downloadable_artifact_requires_web_url_for_url_backed_items() -> None:
    selected = pick_downloadable_artifact(
        [
            {"id": "missing", "type": "audio", "status_label": "ready"},
            {"id": "ready", "type": "audio", "status_label": "ready", "url": "https://x"},
        ],
        backend="web",
    )
    assert selected and selected["id"] == "ready"


def test_pick_downloadable_artifact_keeps_android_slide_fallback() -> None:
    fallback = {"id": "slide", "type": "slide-deck", "status_label": "completed"}
    assert pick_downloadable_artifact([fallback], backend="android") is fallback


class _Result:
    def __init__(self, structured_content):
        self.structured_content = structured_content


class _Response:
    def __init__(self, *, status_code=200, payload=None, content=b"ok"):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = "response"

    def json(self):
        return self._payload


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize("first_page_notes", [0, 50])
async def test_run_uses_current_studio_tools(monkeypatch, backend, first_page_notes) -> None:
    from notebooklm.mcp import _smoke

    calls = []

    class FakeMcp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name == "studio_list":
                if first_page_notes and arguments.get("offset", 0) == 0:
                    return _Result(
                        {
                            "items": [{"id": f"note-{i}", "type": "note"} for i in range(50)],
                            "has_more": True,
                            "offset": 0,
                        }
                    )
                return _Result(
                    {
                        "items": [
                            {
                                "id": "report-1",
                                "type": "slide-deck" if backend == "android" else "report",
                                "status_label": "ready",
                            }
                        ],
                        "has_more": False,
                    }
                )
            responses = {
                "source_add": _Result({"status": "upload_required", "url": "https://x/upload"}),
                "source_list": _Result({"sources": [{"id": "source-1"}]}),
                "studio_list": _Result(
                    {"items": [{"id": "report-1", "type": "report", "status_label": "ready"}]}
                ),
                "studio_download": _Result(
                    {"status": "download_ready", "url": "https://x/download"}
                ),
            }
            return responses[name]

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            return _Response(payload={"source_id": "source-1"})

        async def get(self, _url):
            return _Response(content=b"download")

    monkeypatch.setattr("fastmcp.Client", lambda _transport: FakeMcp())
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: FakeHttp())
    args = _smoke.parse_args(
        [
            "--base-url",
            "https://mcp.example.com",
            "--bearer",
            "token",
            "--notebook",
            "nb",
            "--backend",
            backend,
        ]
    )

    assert await _smoke.run(args) is True
    assert [name for name, _ in calls] == [
        "source_add",
        "source_list",
        "studio_list",
        *(["studio_list"] if first_page_notes else []),
        "studio_download",
    ]
    if first_page_notes:
        assert calls[-2][1]["offset"] == 50
    assert calls[-1][1]["artifact_id"] == "report-1"


async def test_run_rejects_cleartext_before_attaching_bearer(capsys) -> None:
    from notebooklm.mcp import _smoke

    args = _smoke.parse_args(
        ["--base-url", "http://mcp.example.com", "--bearer", "secret", "--notebook", "nb"]
    )
    assert await _smoke.run(args) is False
    assert "must use HTTPS" in capsys.readouterr().out


async def test_run_allows_explicit_loopback_http(monkeypatch) -> None:
    from notebooklm.mcp import _smoke

    class StopAfterTransport(Exception):
        pass

    def stop(_transport):
        raise StopAfterTransport

    monkeypatch.setattr("fastmcp.Client", stop)
    args = _smoke.parse_args(
        [
            "--base-url",
            "http://127.0.0.1:9420",
            "--allow-insecure-http",
            "--bearer",
            "local-token",
            "--notebook",
            "nb",
        ]
    )
    with pytest.raises(StopAfterTransport):
        await _smoke.run(args)


async def test_run_redacts_signed_url_from_failure_output(monkeypatch, capsys) -> None:
    from notebooklm.mcp import _smoke

    class FakeMcp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call_tool(self, _name, _arguments):
            return _Result({"status": "unexpected", "url": "https://secret.example/capability"})

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("fastmcp.Client", lambda _transport: FakeMcp())
    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: FakeHttp())
    args = _smoke.parse_args(
        ["--base-url", "https://mcp.example.com", "--bearer", "token", "--notebook", "nb"]
    )

    assert await _smoke.run(args) is False
    output = capsys.readouterr().out
    assert "status='unexpected'" in output
    assert "keys=['status', 'url']" in output
    assert "secret.example" not in output
