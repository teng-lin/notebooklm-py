"""E2E tests for the MCP server against the real NotebookLM API.

Real ``NotebookLMClient`` + real API, marked ``@pytest.mark.e2e`` (added
automatically by ``tests/e2e/conftest.py::pytest_itemcollected`` and required
explicitly here too). The MCP server is driven through FastMCP's in-memory
:class:`fastmcp.Client` against a server whose lifespan binds the real,
already-open ``client`` fixture via the ``client_factory`` seam.

Coverage:

* ``notebook_list`` (read-only) returns the live notebook set.
* The tool manifest: the core tools are present, deletes carry the
  ``destructiveHint`` annotation + a ``confirm`` parameter, and reads carry
  ``readOnlyHint``.
* A full ``create -> describe -> rename -> delete`` lifecycle driven entirely
  through MCP tools, with cleanup.
* Name resolution against live data: resolve a notebook by its title through
  ``notebook_describe`` and confirm it lands on the right id.

These require auth (``requires_auth``) and the ``mcp`` extra
(``pytest.importorskip("fastmcp")``). They are excluded from the default suite
(``addopts = --ignore=tests/e2e``) and only run via ``pytest tests/e2e -m e2e``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

# Require the `mcp` extra; skip the whole module cleanly when fastmcp is absent.
pytest.importorskip("fastmcp")

# Shared in-memory MCP driver lives in a ``_``-prefixed helper module so the
# sibling live-MCP test modules can reuse it without importing one ``test_*``
# module from another (forbidden by test_no_cross_test_imports). Re-exported
# under the historical ``_call`` / ``_mcp_client`` names this module already used.
from ._mcp_live_helpers import (  # noqa: E402 - after importorskip guard
    call_tool as _call,
)
from ._mcp_live_helpers import (  # noqa: E402 - after importorskip guard
    mcp_client as _mcp_client,
)
from ._mcp_live_helpers import (  # noqa: E402 - after importorskip guard
    pick_downloadable_artifact as _pick_downloadable_artifact,
)
from .conftest import requires_auth  # noqa: E402 - after importorskip guard

pytestmark = pytest.mark.e2e


@requires_auth
class TestMcpReadOnly:
    """Read-only MCP tools against the live account."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_notebook_list(self, client):
        """``notebook_list`` returns the live notebook set through MCP."""
        structured = await _call(client, "notebook_list")
        assert isinstance(structured, dict)
        assert "notebooks" in structured
        assert isinstance(structured["notebooks"], list)
        for nb in structured["notebooks"]:
            assert isinstance(nb, dict)
            assert nb.get("id")

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_server_info(self, client):
        """``server_info`` reports the version and a healthy auth probe."""
        structured = await _call(client, "server_info")
        assert structured["server"] == "notebooklm"
        assert structured["version"]
        # Auth came from real storage for the E2E run, so the probe must pass.
        assert structured["auth"]["authenticated"] is True
        assert structured["auth"]["sid_cookie"] is True


@requires_auth
class TestMcpManifest:
    """Tool-manifest presence + annotation contract against the live server."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_manifest_presence_and_annotations(self, client):
        """Core tools present; deletes DESTRUCTIVE+confirm; reads READ_ONLY."""
        async with _mcp_client(client) as mcp_client:
            tools = await mcp_client.list_tools()
        by_name = {tool.name: tool for tool in tools}

        # A representative slice of the core surface must be present.
        core = {
            "notebook_list",
            "notebook_create",
            "notebook_describe",
            "notebook_rename",
            "notebook_delete",
            "source_list",
            "chat_ask",
            "studio_list",
            "research_status",
            "note_save",
            "server_info",
        }
        missing = core - set(by_name)
        assert not missing, f"core tools missing from the manifest: {sorted(missing)}"

        # Every delete is DESTRUCTIVE and exposes a ``confirm`` parameter.
        for name in ("notebook_delete", "source_delete", "studio_delete"):
            tool = by_name[name]
            assert tool.annotations is not None, f"{name} has no annotations"
            assert tool.annotations.destructiveHint is True, f"{name} missing destructiveHint"
            assert "confirm" in tool.inputSchema.get("properties", {}), (
                f"{name} must expose a 'confirm' parameter"
            )

        # Every read tool carries readOnlyHint.
        for name in ("notebook_list", "source_list", "studio_list", "server_info"):
            tool = by_name[name]
            assert tool.annotations is not None, f"{name} has no annotations"
            assert tool.annotations.readOnlyHint is True, f"{name} missing readOnlyHint"


@requires_auth
class TestMcpLifecycle:
    """Full create -> describe -> rename -> delete lifecycle through MCP tools."""

    @pytest.mark.asyncio
    async def test_create_describe_rename_delete(
        self, client, created_notebooks, cleanup_notebooks
    ):
        title = f"E2E-MCP-{uuid4().hex[:8]}"
        renamed = f"{title}-renamed"

        # Create via MCP.
        created = await _call(client, "notebook_create", {"title": title})
        nb_id = created["notebook_id"]
        assert nb_id
        assert created["title"] == title
        created_notebooks.append(nb_id)

        # Describe via MCP (resolves the full id directly — read path).
        described = await _call(client, "notebook_describe", {"notebook": nb_id})
        assert described["notebook_id"] == nb_id
        assert "description" in described

        # Rename via MCP.
        renamed_result = await _call(
            client, "notebook_rename", {"notebook": nb_id, "new_title": renamed}
        )
        assert renamed_result == {"notebook_id": nb_id, "new_title": renamed}

        # Delete via MCP — first preview (confirm omitted), then confirm=True.
        preview = await _call(client, "notebook_delete", {"notebook": nb_id})
        assert preview["status"] == "needs_confirmation"
        assert preview["preview"]["notebook_id"] == nb_id

        deleted = await _call(client, "notebook_delete", {"notebook": nb_id, "confirm": True})
        assert deleted == {"status": "deleted", "notebook_id": nb_id}
        created_notebooks.remove(nb_id)


@requires_auth
class TestMcpNameResolution:
    """Name-resolution against live data: resolve a notebook by its title."""

    @pytest.mark.asyncio
    async def test_resolve_notebook_by_title(self, client, created_notebooks, cleanup_notebooks):
        """A notebook created with a unique title is reachable by that title."""
        title = f"E2E-MCP-Name-{uuid4().hex[:8]}"
        created = await _call(client, "notebook_create", {"title": title})
        nb_id = created["notebook_id"]
        created_notebooks.append(nb_id)

        # Drive describe with the TITLE (not the id) — the MCP resolver must
        # case-insensitively match the title against the live notebook list and
        # land on the same id.
        described = await _call(client, "notebook_describe", {"notebook": title})
        assert described["notebook_id"] == nb_id

        # Case-insensitive match also resolves to the same id.
        described_upper = await _call(client, "notebook_describe", {"notebook": title.upper()})
        assert described_upper["notebook_id"] == nb_id


# Tool → owning-test matrix. Every one of the 28 registered tools must map to a
# test (or a documented owner) so a newly-added tool fails ``test_tool_matrix``
# until it gains live coverage. ``test_tool_matrix`` asserts this set equals the
# live manifest.
TOOL_COVERAGE: dict[str, str] = {
    # notebooks / meta
    "notebook_list": "TestMcpReadOnly.test_notebook_list",
    "notebook_create": "TestMcpLifecycle.test_create_describe_rename_delete",
    "notebook_describe": "TestMcpLifecycle / TestMcpNameResolution",
    "notebook_rename": "TestMcpLifecycle.test_create_describe_rename_delete",
    "notebook_delete": "TestMcpLifecycle.test_create_describe_rename_delete",
    "server_info": "TestMcpReadOnly.test_server_info",
    # sources
    "source_add": "TestMcpSources.test_source_roundtrip / test_source_add_text",
    "source_list": "TestMcpSources.test_source_roundtrip",
    "source_read": "TestMcpSources.test_source_roundtrip",
    "source_rename": "TestMcpSources.test_source_roundtrip",
    "source_delete": "TestMcpSources.test_source_roundtrip",
    "source_wait": "TestMcpSources.test_source_roundtrip",
    # chat
    "chat_ask": "TestMcpChat.test_configure_then_ask",
    "chat_configure": "TestMcpChat.test_configure_then_ask",
    # notes
    "note_save": "TestMcpNotes.test_note_crud",
    # studio (notes + artifacts unified)
    "studio_list": "TestMcpArtifacts.test_artifact_list / TestMcpNotes.test_note_crud",
    "studio_generate": "TestMcpArtifacts.test_generate_report_wiring (variants)",
    "studio_status": "TestMcpArtifacts.test_generate_report_wiring (variants)",
    "studio_download": "TestMcpArtifacts.test_download_existing_artifact",
    "studio_rename": "tests/unit/mcp/test_studio.py (cross-type note/artifact rename; no live mutation)",
    "studio_delete": "TestMcpNotes.test_note_crud (note path) + tests/unit/mcp/test_studio.py (artifact path)",
    # research
    "research_start": "TestMcpResearch.test_start_status_cancel (variants)",
    "research_status": "TestMcpResearch.test_status_readonly",
    "research_cancel": "TestMcpResearch.test_start_status_cancel (variants)",
    "research_import": "tests/e2e research suite (CLI import roundtrip)",
}


@requires_auth
class TestMcpToolMatrix:
    """Every registered tool is accounted for by a live test (the 28-tool matrix)."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_tool_matrix(self, client):
        """The owning-test matrix must cover EXACTLY the live tool manifest.

        Guards against a new tool shipping without live MCP coverage: a tool
        added to the server but missing from ``TOOL_COVERAGE`` fails here.
        """
        async with _mcp_client(client) as mcp_client:
            tools = await mcp_client.list_tools()
        live = {tool.name for tool in tools}
        documented = set(TOOL_COVERAGE)
        assert documented == live, (
            f"tool/test matrix drift — only in manifest: {sorted(live - documented)}; "
            f"only in matrix: {sorted(documented - live)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_every_readonly_tool_is_callable(self, client, read_only_notebook_id):
        """Each read-only tool dispatches live and returns a structured dict.

        Covers the read-only surface that is callable with only a notebook (or
        nothing) plus ``source_read`` (a real source id is resolved from
        ``source_list``). ``studio_status`` needs a live ``task_id`` and is
        instead covered by ``TestMcpArtifacts`` (the generation wiring smoke).
        """
        nb = read_only_notebook_id

        # No-arg reads.
        assert isinstance(await _call(client, "notebook_list"), dict)
        assert isinstance(await _call(client, "server_info"), dict)

        # Notebook-scoped reads.
        for name in ("notebook_describe", "source_list", "studio_list"):
            structured = await _call(client, name, {"notebook": nb})
            assert isinstance(structured, dict), f"{name} returned {type(structured)}"

        # research_status with no in-flight task classifies cleanly (no_research / etc.).
        research = await _call(client, "research_status", {"notebook": nb})
        assert "status" in research

        # source_read needs a real source id — resolve one from the listing.
        listing = await _call(client, "source_list", {"notebook": nb})
        sources = listing.get("sources") or []
        if sources:
            src_id = sources[0]["id"]
            content = await _call(client, "source_read", {"notebook": nb, "source": src_id})
            assert content["source"]["id"] == src_id


@requires_auth
class TestMcpSources:
    """Source domain: add / wait / list / get / rename / delete through MCP tools."""

    @pytest.mark.asyncio
    async def test_source_roundtrip(self, client, temp_notebook):
        """A URL source round-trips: add → (wait) → list → get → rename → delete."""
        nb = temp_notebook.id

        added = await _call(
            client,
            "source_add",
            {"notebook": nb, "source_type": "url", "url": "https://example.com"},
        )
        assert isinstance(added, dict)
        src_id = (added.get("source") or {}).get("id")
        assert src_id, f"source_add did not return a source id: {added}"

        # Wait for processing (best-effort: the roundtrip's value is the wiring,
        # not example.com's fetch outcome — just assert it dispatched cleanly).
        waited = await _call(
            client, "source_wait", {"notebook": nb, "source": src_id, "timeout": 120.0}
        )
        assert waited.get("notebook_id") == nb
        # Unified aggregate contract: both modes carry an explicit ``ok`` signal.
        assert "ok" in waited

        listing = await _call(client, "source_list", {"notebook": nb})
        ids = [s["id"] for s in listing["sources"]]
        assert src_id in ids

        content = await _call(client, "source_read", {"notebook": nb, "source": src_id})
        assert content["source"]["id"] == src_id

        renamed = await _call(
            client,
            "source_rename",
            {"notebook": nb, "source": src_id, "new_title": "Renamed Source"},
        )
        assert isinstance(renamed, dict)

        # Confirm-gating: preview without confirm, then confirm=True deletes.
        preview = await _call(client, "source_delete", {"notebook": nb, "source": src_id})
        assert preview["status"] == "needs_confirmation"
        deleted = await _call(
            client, "source_delete", {"notebook": nb, "source": src_id, "confirm": True}
        )
        assert deleted["status"] == "deleted"
        assert deleted["source_id"] == src_id

    @pytest.mark.asyncio
    async def test_source_add_text(self, client, temp_notebook):
        """A text source adds through MCP and appears in the listing."""
        nb = temp_notebook.id
        added = await _call(
            client,
            "source_add",
            {
                "notebook": nb,
                "source_type": "text",
                "text": "Live MCP text source body for the e2e suite.",
                "title": "MCP Text Source",
            },
        )
        src_id = (added.get("source") or {}).get("id")
        assert src_id, f"text source_add did not return an id: {added}"
        listing = await _call(client, "source_list", {"notebook": nb})
        assert src_id in [s["id"] for s in listing["sources"]]


@requires_auth
class TestMcpChat:
    """Chat domain: configure then ask against the read-only notebook."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_configure_then_ask(self, client, read_only_notebook_id):
        """``chat_configure`` then ``chat_ask`` returns a non-empty answer."""
        nb = read_only_notebook_id

        configured = await _call(client, "chat_configure", {"notebook": nb, "chat_mode": "concise"})
        assert isinstance(configured, dict)

        answer = await _call(
            client, "chat_ask", {"notebook": nb, "question": "What is this notebook about?"}
        )
        assert answer.get("answer"), f"chat_ask returned no answer: {answer}"
        # citations land under ``references`` (the ChatAnswer wire field).
        assert isinstance(answer.get("references", []), list)


@requires_auth
class TestMcpNotes:
    """Note domain: create / list / update / delete via the consolidated Studio surface.

    ``note_save`` upserts (create then update); reading + deleting a note fold into
    the cross-type ``studio_list`` / ``studio_delete`` tools.
    """

    @pytest.mark.asyncio
    async def test_note_crud(self, client, temp_notebook):
        nb = temp_notebook.id

        created = await _call(
            client,
            "note_save",
            {"notebook": nb, "title": "E2E MCP Note", "content": "Initial body."},
        )
        note_id = created["note_id"]
        assert note_id
        # note_save create returns a `created: True` bool alongside status="created".
        assert created["created"] is True

        # The note surfaces in the merged Studio panel as a ``note``-typed item.
        listing = await _call(client, "studio_list", {"notebook": nb})
        note_item = next((it for it in listing["items"] if it["id"] == note_id), None)
        assert note_item is not None
        assert note_item["type"] == "note"

        updated = await _call(
            client, "note_save", {"notebook": nb, "note": note_id, "content": "Updated body."}
        )
        assert updated["status"] == "updated"
        assert updated["note_id"] == note_id

        preview = await _call(client, "studio_delete", {"notebook": nb, "item": note_id})
        assert preview["status"] == "needs_confirmation"
        deleted = await _call(
            client, "studio_delete", {"notebook": nb, "item": note_id, "confirm": True}
        )
        assert deleted["status"] == "deleted"
        assert deleted["item_id"] == note_id
        assert deleted["type"] == "note"


@requires_auth
class TestMcpArtifacts:
    """Artifact domain: list (read) + a download of an existing artifact, plus a
    generation **wiring smoke** (marked ``variants`` — heavy, no poll-to-done)."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_artifact_list(self, client, generation_notebook_id):
        """``studio_list`` returns the notebook's merged notes+artifacts as a list."""
        structured = await _call(client, "studio_list", {"notebook": generation_notebook_id})
        assert isinstance(structured["items"], list)

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_existing_artifact(self, client, generation_notebook_id, tmp_path):
        """Download an EXISTING artifact (no fresh generation) to a local path.

        Reuses whatever downloadable artifact the notebook already has (generation
        e2e populates them nightly). Skips cleanly when none is present so this
        never depends on cross-file test ordering.
        """
        listing = await _call(client, "studio_list", {"notebook": generation_notebook_id})
        candidate = _pick_downloadable_artifact(listing["items"])
        if candidate is None:
            pytest.skip("no existing downloadable artifact on the generation notebook")

        # A merged item's hyphenated ``type`` IS the studio_download key.
        dl_type = candidate["type"]
        out_path = tmp_path / f"artifact-{dl_type}"
        result = await _call(
            client,
            "studio_download",
            {
                "notebook": generation_notebook_id,
                "artifact_type": dl_type,
                "path": str(out_path),
            },
        )
        assert isinstance(result, dict)
        # The stdio download core writes the file and reports its path; assert
        # bytes landed on disk.
        written = Path(result.get("output_path") or out_path)
        assert written.exists(), f"download produced no file: {result}"
        assert written.stat().st_size > 0

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_report_wiring(self, client, generation_notebook_id):
        """Wiring smoke: ``studio_generate`` threads through and returns a
        ``task_id``; one ``studio_status`` poll dispatches. Does NOT poll to
        completion (the RPC health of generation is proven by ``test_generation``)."""
        generated = await _call(
            client,
            "studio_generate",
            {"notebook": generation_notebook_id, "artifact_type": "report"},
        )
        task_id = generated.get("task_id")
        assert task_id, f"studio_generate returned no task_id: {generated}"

        status = await _call(
            client,
            "studio_status",
            {"notebook": generation_notebook_id, "task_id": task_id},
        )
        assert status["notebook_id"] == generation_notebook_id
        assert "status" in status


@requires_auth
class TestMcpResearch:
    """Research domain: a read-only status smoke (nightly) + a start/cancel wiring
    smoke (``variants`` — spawns a backend job)."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_status_readonly(self, client, read_only_notebook_id):
        """``research_status`` classifies a notebook with no in-flight research."""
        structured = await _call(client, "research_status", {"notebook": read_only_notebook_id})
        assert "status" in structured
        assert structured["notebook_id"] == read_only_notebook_id

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_start_status_cancel(self, client, temp_notebook):
        """Wiring smoke: start a fast web research run, poll status once, cancel."""
        nb = temp_notebook.id
        started = await _call(
            client,
            "research_start",
            {"notebook": nb, "query": "history of machine learning", "mode": "fast"},
        )
        assert started["notebook_id"] == nb

        status = await _call(client, "research_status", {"notebook": nb})
        assert "status" in status

        # research_cancel MUST actually run — TOOL_COVERAGE claims it here, so a
        # missing run id is a real failure (not a silent skip that would let the
        # coverage matrix report a false positive).
        run_id = started.get("poll_task_id") or status.get("poll_task_id")
        assert run_id, f"research_start/status yielded no run id to cancel: {started} / {status}"
        cancelled = await _call(client, "research_cancel", {"notebook": nb, "run_id": run_id})
        # A freshly-started fast run is normally still in_progress at this
        # immediate poll, so the preflight fires the cancel (cancel_requested
        # True). Tolerate the race where it already reached a terminal state
        # (completed/failed) between start and preflight — then the tool honestly
        # reports cancel_requested False rather than a no-op "cancelled" lie.
        assert cancelled["cancel_requested"] is True or cancelled["status"] in (
            "completed",
            "failed",
        )
