"""Unit tests for the Phase 1 ``await_upload`` core helper (``_await_upload``).

The helper is transport-agnostic: it takes a resolved :class:`FileTransferConfig`
(real signer + in-process completion map), a token-or-URL, and returns a small
status dict. Testing it directly avoids standing up the whole HTTP MCP server
(which needs a public base URL to wire ``file_transfer`` at all).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402

import notebooklm.mcp._filelink as filelink  # noqa: E402
from notebooklm.exceptions import ValidationError  # noqa: E402
from notebooklm.mcp._filelink import FileLinkSigner, FileTransferConfig  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402
from notebooklm.mcp.tools._fileupload import (  # noqa: E402
    _await_upload,
    _broker_upload,
    _extract_ul_token,
)


def _cfg() -> FileTransferConfig:
    return FileTransferConfig(signer=FileLinkSigner(key=b"k" * 32), base_url="https://h.example")


def _mint(cfg: FileTransferConfig) -> tuple[str, str]:
    """Return ``(url, jti)`` for a fresh upload link."""
    url = cfg.upload_url({"nb": "nb1"})
    token = url.rsplit("/", 1)[1]
    jti = cfg.signer.verify(token, op="ul")["jti"]
    return url, jti


def test_extract_token_from_url_or_bare() -> None:
    assert _extract_ul_token("abc.def") == "abc.def"
    assert _extract_ul_token("https://h.example/files/ul/abc.def") == "abc.def"
    assert _extract_ul_token("https://h.example/files/ul/abc.def?filename=x#frag") == "abc.def"
    assert _extract_ul_token("  https://h.example/files/ul/abc.def/  ") == "abc.def"


async def test_received_when_result_already_recorded() -> None:
    cfg = _cfg()
    url, jti = _mint(cfg)
    cfg.jti_store.commit(jti, int(time.time()) + 60, result={"source_id": "s-1", "name": "r.pdf"})
    # accepts the full URL...
    out = await _await_upload(cfg, url, timeout_s=0, poll_interval_s=0)
    assert out["status"] == "received"
    assert out["source_id"] == "s-1"
    assert out["file"] == {"source_id": "s-1", "name": "r.pdf"}
    # ...and the bare token
    out2 = await _await_upload(cfg, jti and url.rsplit("/", 1)[1], timeout_s=0, poll_interval_s=0)
    assert out2["status"] == "received"


async def test_pending_when_not_yet_uploaded() -> None:
    cfg = _cfg()
    url, _ = _mint(cfg)
    out = await _await_upload(cfg, url, timeout_s=0, poll_interval_s=0)
    assert out["status"] == "pending"
    assert "re-invoke" in out["hint"]


async def test_expired_or_invalid_token() -> None:
    cfg = _cfg()
    out = await _await_upload(cfg, "not-a-real.token", timeout_s=0, poll_interval_s=0)
    assert out["status"] == "expired_or_invalid"
    assert "source_add" in out["hint"]


async def test_received_via_short_link() -> None:
    # await_upload must also accept the tap-friendly /u/<shortid> link (what the model now
    # hands the user), resolving it to the real token via the in-process short-link store.
    cfg = _cfg()
    url = cfg.short_upload_url({"nb": "nb1"})  # https://h.example/u/<shortid>
    shortid = url.rsplit("/", 1)[1]
    token = cfg.short_links.get(shortid)
    jti = cfg.signer.verify(token, op="ul")["jti"]
    cfg.jti_store.commit(jti, int(time.time()) + 60, result={"source_id": "s-short"})
    out = await _await_upload(cfg, url, timeout_s=0, poll_interval_s=0)
    assert out["status"] == "received"
    assert out["source_id"] == "s-short"


async def test_unknown_short_link_is_expired_or_invalid() -> None:
    cfg = _cfg()
    out = await _await_upload(cfg, "https://h.example/u/nope", timeout_s=0, poll_interval_s=0)
    assert out["status"] == "expired_or_invalid"


async def test_poll_picks_up_a_later_same_process_commit() -> None:
    # The whole point of the in-process map: a commit that lands WHILE await_upload is
    # polling is seen on the next tick (no DB, same event loop).
    cfg = _cfg()
    url, jti = _mint(cfg)

    async def _upload_lands() -> None:
        await asyncio.sleep(0.02)
        cfg.jti_store.commit(jti, int(time.time()) + 60, result={"source_id": "s-late"})

    lander = asyncio.create_task(_upload_lands())
    out = await _await_upload(cfg, url, timeout_s=2.0, poll_interval_s=0.01)
    await lander
    assert out["status"] == "received"
    assert out["source_id"] == "s-late"


async def test_broker_returns_short_human_link_direct_agent_link_one_token() -> None:
    # _broker_upload hands the human a tap-friendly /u/<shortid> and the agent the direct
    # /files/ul POST target — both over ONE token (one jti). await_upload accepts the short one.
    cfg = _cfg()
    out = _broker_upload(cfg, "nb1", title=None, mime_type=None, path="report.pdf")
    human = out["human_upload"]["url"]
    agent = out["agent_upload"]["url"]
    assert "/u/" in human and "/files/ul/" not in human  # short, tap-friendly
    assert "/files/ul/" in agent  # direct raw-POST target

    # both point at the SAME token (one jti, not two)
    agent_token = agent.split("/files/ul/", 1)[1].split("?", 1)[0]
    human_token = cfg.short_links.get(human.rsplit("/", 1)[1])
    assert human_token == agent_token

    # simulate the upload committing, then await via the SHORT link → received
    jti = cfg.signer.verify(human_token, op="ul")["jti"]
    exp = cfg.signer.verify(human_token, op="ul")["exp"]
    cfg.jti_store.commit(jti, exp, result={"source_id": "s-broker"})
    got = await _await_upload(cfg, human, timeout_s=0, poll_interval_s=0)
    assert got["status"] == "received"
    assert got["source_id"] == "s-broker"


async def test_non_finite_timeout_rejected() -> None:
    # A NaN/inf timeout would make the poll deadline unsatisfiable (infinite loop). Reject it.
    cfg = _cfg()
    url, _ = _mint(cfg)
    for bad in (float("inf"), float("nan"), float("-inf")):
        with pytest.raises(ValidationError):
            await _await_upload(cfg, url, timeout_s=bad)


async def test_recovers_committed_result_after_token_expiry(monkeypatch) -> None:
    # A large upload can commit its result just before the start-token expires; a later
    # await_upload must still surface the source_id, not return expired_or_invalid.
    cfg = _cfg()
    url, jti = _mint(cfg)
    exp = cfg.signer.verify(url.rsplit("/", 1)[1], op="ul")["exp"]
    cfg.jti_store.commit(jti, exp, result={"source_id": "s-late-exp"})
    # Freeze "now" past the token's expiry → normal verify fails, recovery path kicks in.
    monkeypatch.setattr(filelink.time, "time", lambda: exp + 5)
    out = await _await_upload(cfg, url, timeout_s=0, poll_interval_s=0)
    assert out["status"] == "received"
    assert out["source_id"] == "s-late-exp"


async def test_expired_token_with_no_committed_result_stays_invalid(monkeypatch) -> None:
    # Recovery is ONLY for committed uploads — an expired token with nothing committed is invalid.
    cfg = _cfg()
    url, _ = _mint(cfg)
    exp = cfg.signer.verify(url.rsplit("/", 1)[1], op="ul")["exp"]
    monkeypatch.setattr(filelink.time, "time", lambda: exp + 5)
    out = await _await_upload(cfg, url, timeout_s=0, poll_interval_s=0)
    assert out["status"] == "expired_or_invalid"


# --------------------------------------------------------------------------- #
# Progress keepalive (Item 3 / #1889) — the host sees liveness across the ~45s poll
# --------------------------------------------------------------------------- #
async def test_progress_keepalive_invoked_at_t0_and_each_tick() -> None:
    # The progress hook fires at t=0 AND on every poll tick, so an MCP host watching for a
    # stalled call sees liveness across the whole wait (not just once).
    cfg = _cfg()
    url, _ = _mint(cfg)
    calls = 0

    async def _progress() -> None:
        nonlocal calls
        calls += 1

    # Nothing committed → polls until the timeout; expect the t=0 emit plus ≥1 tick emit.
    out = await _await_upload(cfg, url, timeout_s=0.05, poll_interval_s=0.01, progress=_progress)
    assert out["status"] == "pending"
    assert calls >= 2


async def test_progress_keepalive_errors_are_swallowed() -> None:
    # Best-effort by contract: a keepalive that raises (no client progressToken / transient
    # notify failure) must NOT abort the poll or surface as an error.
    cfg = _cfg()
    url, jti = _mint(cfg)
    cfg.jti_store.commit(jti, int(time.time()) + 60, result={"source_id": "s-ok"})

    async def _boom() -> None:
        raise RuntimeError("notify channel closed")

    out = await _await_upload(cfg, url, timeout_s=0.05, poll_interval_s=0.01, progress=_boom)
    assert out["status"] == "received"  # the raising keepalive did not break the wait
    assert out["source_id"] == "s-ok"


async def test_await_upload_tool_delivers_progress_over_the_protocol() -> None:
    # End-to-end over the REAL MCP protocol (in-memory Client with a progressToken-bearing
    # request): the await_upload tool's keepalive reaches the client as an actual progress
    # notification — not merely that ctx.report_progress was called.
    cfg = _cfg()
    url, jti = _mint(cfg)
    cfg.jti_store.commit(jti, int(time.time()) + 60, result={"source_id": "s-ok"})

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield MagicMock()

    server = create_server(client_factory=factory, file_transfer=cfg)
    updates: list[tuple[float, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        updates.append((progress, message))

    async with Client(server) as client:
        result = await client.call_tool(
            "await_upload", {"upload_link": url, "timeout": 0.0}, progress_handler=on_progress
        )

    assert result.data["status"] == "received"
    assert updates, "the keepalive must reach the client as a real progress notification"
    # It carries a human-readable status message (an honest liveness ping).
    assert any(msg for _progress, msg in updates)
