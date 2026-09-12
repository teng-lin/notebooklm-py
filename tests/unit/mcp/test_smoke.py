"""Unit coverage for the shared deployed-server smoke driver."""

from __future__ import annotations

import pytest

from notebooklm.mcp._smoke import (
    DOWNLOADABLE_ARTIFACT_TYPES,
    parse_args,
    pick_downloadable_artifact,
)


def test_parse_args_uses_current_studio_vocabulary() -> None:
    """Accept the public slide-deck spelling and reject the retired vocabulary."""
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
    """Select a completed downloadable artifact rather than notes or pending items."""
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
    """Require a resolved URL when the web backend downloads URL-backed media."""
    selected = pick_downloadable_artifact(
        [
            {"id": "missing", "type": "audio", "status_label": "ready"},
            {"id": "ready", "type": "audio", "status_label": "ready", "url": "https://x"},
        ],
        backend="web",
    )
    assert selected and selected["id"] == "ready"


def test_pick_downloadable_artifact_keeps_android_slide_fallback() -> None:
    """Retain an Android slide deck that can resolve its URL during download."""
    fallback = {"id": "slide", "type": "slide-deck", "status_label": "completed"}
    assert pick_downloadable_artifact([fallback], backend="android") is fallback


class _Result:
    def __init__(self, structured_content):
        """Expose the configured payload through the MCP structured-content attribute."""
        self.structured_content = structured_content


class _Response:
    def __init__(self, *, status_code=200, payload=None, content=b"ok"):
        """Build a fixed HTTP response for upload and download paths."""
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = "response"

    def json(self):
        """Return the configured JSON payload without making a network request."""
        return self._payload


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize("first_page_notes", [0, 50])
async def test_run_uses_current_studio_tools(monkeypatch, backend, first_page_notes) -> None:
    """Exercise paginated artifact selection and downloads for each backend."""
    from notebooklm.mcp import _smoke

    calls = []

    class FakeMcp:
        async def __aenter__(self):
            """Expose the recording MCP client within the driver context."""
            return self

        async def __aexit__(self, *_args):
            """Leave the fixture context without swallowing driver failures."""
            return None

        async def call_tool(self, name, arguments):
            """Record calls and serve the configured upload, source, and Studio responses."""
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
            """Expose the fixed-response HTTP transport to the driver."""
            return self

        async def __aexit__(self, *_args):
            """Finish the transport context while propagating exceptions."""
            return None

        async def post(self, _url, **_kwargs):
            """Confirm the fixture upload without sending file bytes over a network."""
            return _Response(payload={"source_id": "source-1"})

        async def get(self, _url):
            """Return nonempty download bytes for the selected fixture artifact."""
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
    assert calls[-1][1]["artifact_type"] == ("slide-deck" if backend == "android" else "report")


async def test_run_rejects_cleartext_before_attaching_bearer(capsys) -> None:
    """Reject non-loopback HTTP before creating a credential-bearing transport."""
    from notebooklm.mcp import _smoke

    args = _smoke.parse_args(
        ["--base-url", "http://mcp.example.com", "--bearer", "secret", "--notebook", "nb"]
    )
    assert await _smoke.run(args) is False
    assert "must use HTTPS" in capsys.readouterr().out


async def test_run_allows_explicit_loopback_http(monkeypatch) -> None:
    """Permit explicitly authorized loopback HTTP through transport creation."""
    from notebooklm.mcp import _smoke

    class StopAfterTransport(Exception):
        pass

    def stop(_transport):
        """Stop immediately after transport validation to avoid a live request."""
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
    """Keep capability-bearing URLs out of diagnostic output on malformed results."""
    from notebooklm.mcp import _smoke

    class FakeMcp:
        async def __aenter__(self):
            """Expose the malformed-result fixture without connecting to a server."""
            return self

        async def __aexit__(self, *_args):
            """Propagate any unexpected failure from the MCP fixture context."""
            return None

        async def call_tool(self, _name, _arguments):
            """Return an invalid upload result carrying a URL that must stay redacted."""
            return _Result({"status": "unexpected", "url": "https://secret.example/capability"})

    class FakeHttp:
        async def __aenter__(self):
            """Enter an inert HTTP context for a failure before any upload."""
            return self

        async def __aexit__(self, *_args):
            """Finish the inert HTTP context without suppressing failures."""
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
