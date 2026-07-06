"""Live e2e smoke for the CLI **binary**, invoked as a child process via
``sys.executable -m notebooklm.notebooklm_cli`` (not a raw ``notebooklm`` binary —
CI PATH fragility). The notebook is passed by ``-n <id>`` / ``NOTEBOOKLM_NOTEBOOK``,
never ``notebooklm use`` (which mutates shared profile state).

These exercise the parts only a real subprocess can: argument parsing, the
console-script wiring, and the ``--json`` stdout/stderr split — including the
contract that on a LIVE failure ``--json`` still emits a single valid JSON object
on **stdout** (logs go to stderr).

Requires auth and the ``mcp`` extra (``importorskip`` — keeps this module skipping
in lock-step with the rest of the MCP/CLI-live suite when the extra is absent);
auto-marked ``e2e`` by ``conftest.pytest_itemcollected``.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

# Skip in lock-step with the rest of the MCP/CLI-live suite when the extra is absent.
pytest.importorskip("fastmcp")

from .conftest import requires_auth, run_cli  # noqa: E402 - after importorskip guard

pytestmark = pytest.mark.e2e


def _is_rate_limited(proc) -> bool:
    """True only on the real limiter signal (HTTP 429 / "rate limit") — NOT any
    message that merely contains "rate", which could mask an unrelated failure."""
    blob = f"{proc.stdout}\n{proc.stderr}".lower()
    return any(phrase in blob for phrase in ("rate limit", "rate-limited", "429"))


@requires_auth
class TestCliLive:
    """The CLI binary against the live account."""

    @pytest.mark.readonly
    def test_list_json(self):
        """``list --json`` returns a JSON array/object of notebooks on stdout."""
        proc = run_cli("list", "--json")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        # ``list --json`` emits the notebooks payload; accept either a bare list
        # or an enveloped object so the test is robust to the exact shape.
        notebooks = payload if isinstance(payload, list) else payload.get("notebooks", payload)
        assert isinstance(notebooks, list)

    @pytest.mark.readonly
    def test_status_json_shape(self):
        """``status --json`` is a LOCAL-context/JSON-shape smoke (no live API).

        It reads on-disk context, not the live API — included only to prove the
        binary + ``--json`` envelope work end to end.
        """
        proc = run_cli("status", "--json")
        assert proc.returncode == 0, proc.stderr
        assert isinstance(json.loads(proc.stdout), dict)

    @pytest.mark.readonly
    @pytest.mark.live_chat_ask
    def test_ask_json(self, read_only_notebook_id):
        """``ask -n <read_only> --json`` returns a structured answer on stdout."""
        proc = run_cli(
            "ask",
            "What is this notebook about?",
            "-n",
            read_only_notebook_id,
            "--json",
        )
        if proc.returncode != 0 and _is_rate_limited(proc):
            pytest.skip(f"chat rate-limited: {proc.stdout or proc.stderr}")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert isinstance(payload, dict)

    def test_source_add_then_list_json(self, temp_notebook):
        """``source add <url> -n <temp> --json`` then ``source list`` confirms it.

        Asserts the SPECIFIC added source id shows up in the live listing —
        ``temp_notebook`` already seeds one source, so a bare ``assert sources``
        would pass even if the add path no-op'd.
        """
        nb = temp_notebook.id
        add = run_cli("source", "add", "https://example.com", "-n", nb, "--json")
        assert add.returncode == 0, add.stderr
        added = json.loads(add.stdout)
        assert isinstance(added, dict)
        added_id = added.get("id") or added.get("source_id") or added.get("source", {}).get("id")
        assert added_id, f"source add returned no id: {added}"

        listing = run_cli("source", "list", "-n", nb, "--json")
        assert listing.returncode == 0, listing.stderr
        payload = json.loads(listing.stdout)
        sources = payload if isinstance(payload, list) else payload.get("sources", [])
        assert any(s.get("id") == added_id for s in sources), (
            f"added source {added_id!r} not found in the live listing"
        )

    @pytest.mark.readonly
    def test_json_stdout_purity_on_failure(self):
        """A LIVE failure under ``--json`` still emits VALID JSON on stdout.

        Drives ``ask`` against a bogus notebook id: the command fails (non-zero
        exit), but the ``--json`` contract requires a single parseable JSON error
        object on **stdout**, with human/log output confined to stderr.
        """
        bogus = f"nonexistent-{uuid4().hex}"
        proc = run_cli("ask", "hello", "-n", bogus, "--json")
        assert proc.returncode != 0
        # stdout must be exactly one valid JSON object (the error envelope).
        error = json.loads(proc.stdout)
        assert isinstance(error, dict)
        assert error.get("error") or error.get("code") or error.get("message")

    @pytest.mark.variants
    def test_generate_through_cli_wiring(self, generation_notebook_id):
        """One generate-through-CLI wiring smoke (returns an id; no poll-to-done)."""
        proc = run_cli(
            "generate",
            "report",
            "a short briefing",
            "-n",
            generation_notebook_id,
            "--json",
        )
        if proc.returncode != 0 and _is_rate_limited(proc):
            pytest.skip(f"generation rate-limited: {proc.stdout or proc.stderr}")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert isinstance(payload, dict)
