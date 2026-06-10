"""Regression tests for the browser-cookie login ``LoginIO`` resolver (#1393).

The login DAG inverts its presentation / exit / async side effects behind a
caller-injected ``LoginIO`` sink (``cli/services/login/io_seam.py``). Service
entry points that are reached *without* an explicit ``io`` — most notably a bare
``_sync_server_language_to_config()`` call, or a library consumer that imported
only ``cli.services.login`` — must still resolve the real console / exit / async
behavior instead of raising. ``resolve_login_io`` therefore lazily registers the
command-layer default sink the first time it is needed.

These tests pin:

* ``resolve_login_io`` returns an injected sink unchanged.
* ``resolve_login_io`` self-heals when no factory is registered (so it never
  raises ``RuntimeError`` on the fallback path).
* A bare ``_sync_server_language_to_config()`` does not raise even from a
  *cold* interpreter where the command layer was never imported (run in a
  subprocess so the import side effect cannot mask the regression).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from notebooklm.cli.services.login import io_seam
from tests._fixtures.login_io import RecordingLoginIO


def test_resolve_returns_injected_sink_unchanged():
    io = RecordingLoginIO()
    assert io_seam.resolve_login_io(io) is io


def test_resolve_self_heals_when_no_factory_registered(monkeypatch):
    """With the factory cleared, ``resolve_login_io`` re-registers the default.

    Mirrors the #1393 cubic finding: a sink-resolving entry point reached
    without an injected ``io`` (and without the command layer pre-imported on
    that path) must not raise — the resolver registers the command-layer
    default lazily and returns a working sink.
    """
    monkeypatch.setattr(io_seam, "_default_io_factory", None)
    sink = io_seam.resolve_login_io(None)
    # The concrete command-layer sink satisfies the LoginIO Protocol.
    assert hasattr(sink, "emit")
    assert hasattr(sink, "fail")
    assert hasattr(sink, "run_async")
    # And the factory is now registered for subsequent resolves.
    assert io_seam._default_io_factory is not None


def test_bare_sync_language_does_not_raise_from_cold_start():
    """``_sync_server_language_to_config()`` survives a cold interpreter (#1393).

    Run in a subprocess so the command-layer registration cannot already be
    cached from this test session — the regression (a ``RuntimeError`` from the
    sink resolver) would only surface when ``cli.playwright_login_io`` was never
    imported on the call path.
    """
    script = textwrap.dedent(
        """
        import sys
        from unittest.mock import MagicMock, patch

        # The command layer (which registers the default sink) must NOT have
        # been imported yet — otherwise the cold-start path isn't exercised.
        assert "notebooklm.cli.playwright_login_io" not in sys.modules

        import notebooklm.cli.services.login.refresh as r

        with patch.object(r, "NotebookLMClient") as cls:
            # Force the language fetch to fail so the warning-emitting branch
            # runs; that branch resolves the sink and calls io.emit / io.run_async.
            cls.from_storage = MagicMock(side_effect=Exception("boom"))
            r._sync_server_language_to_config()  # must NOT raise RuntimeError

        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
