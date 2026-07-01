"""MCP source-tool VCR tests (reuse-only).

Full-stack coverage (FastMCP ``Client`` → ``source_*`` tool → ``_app`` core →
real :class:`~notebooklm.client.NotebookLMClient` → VCR-replayed RPC) for the
source tools, asserting the exact ``structured_content`` an MCP client receives.

Tools covered and the cassette each replays:

* ``source_list`` over ``sources_list.yaml`` (``GET_NOTEBOOK`` → ``rLM1Ne``).
* ``source_delete`` two-step confirm over ``sources_list.yaml`` (preview) +
  ``sources_delete.yaml`` (``DELETE_SOURCE`` → ``tGMBJ``).
* ``source_add`` ``url`` / ``text`` over ``sources_add_url.yaml`` /
  ``sources_add_text.yaml`` (``ADD_SOURCE`` → ``izAoDd``); ``file`` over
  ``sources_add_file.yaml`` (``ADD_SOURCE_FILE`` → ``o4cbdc`` + upload POSTs);
  ``drive`` over ``sources_add_drive.yaml`` (``ADD_SOURCE`` → ``izAoDd``).
* ``source_rename`` over ``sources_rename.yaml`` (``UPDATE_SOURCE`` → ``b7Wfje``).
* ``source_read`` over ``sources_get_fulltext.yaml`` — consumes BOTH the
  leading ``GET_NOTEBOOK`` (``rLM1Ne``, metadata via ``execute_source_get``) and
  the trailing ``hizoJc`` (``GET_SOURCE``, full text via ``execute_source_fulltext``).
* ``source_wait`` (single + all) over ``sources_wait.yaml`` (= ``sources_list.yaml``
  plus one real ``hizoJc`` ``GET_SOURCE`` interaction) — the poller probes source
  status via the same ``GET_NOTEBOOK`` list, and every recorded source is already
  ``READY`` so it resolves on the first poll; the lone web-page source's body is
  fetched for the #1698 content-sanity check.

Every tool is invoked with a FULL canonical UUID (the cassette's recorded
notebook/source id) so the resolver takes its full-UUID fast path and never adds
an extra ``LIST_*`` RPC the cassette lacks. ``source_add`` mutation bodies and
the ``UPDATE_SOURCE`` rename body have their id leaf UUID-normalized by the
matcher, so the *value* passed is decorative — the recorded response is replayed
regardless and its echoed fields (e.g. the rename echo's recorded id/title) are
what the assertions pin.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``sources_list.yaml`` was recorded against this notebook (``GET_NOTEBOOK`` →
# ``rLM1Ne``). The value is decorative — VCR matches on rpcids + body shape, not
# the id — but reusing the recorded id keeps intent obvious.
SOURCES_LIST_NOTEBOOK_ID = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"
# A source id present in ``sources_list.yaml``'s ``GET_NOTEBOOK`` list (status
# READY) — used by ``source_read`` / ``source_wait``, whose lookups
# filter that list and must find a matching row.
SOURCES_LIST_SOURCE_ID = "a474cd35-6c21-4e72-94a0-c38b5491b449"
SOURCES_LIST_SOURCE_TITLE = (
    "GitHub - shareAI-lab/learn-claude-code: How can we build a true AI agent? Like Claude Code."
)

# ``sources_delete.yaml`` notebook + a full-UUID source id (the delete body's
# leaf is UUID-normalized by the matcher, so any UUID-shaped id replays).
DELETE_NOTEBOOK_ID = "06f0c5bd-108f-4c8b-8911-34b2acc656de"
DELETE_SOURCE_ID = "ff503bfa-5e39-4281-a1d8-2a66c7b86724"

# --- source_add recorded notebooks (decorative; matcher ignores the value) ---
ADD_URL_NOTEBOOK_ID = "f66923f0-1df4-4ffe-9822-3ed63c558b1c"
ADD_FILE_NOTEBOOK_ID = "06f0c5bd-108f-4c8b-8911-34b2acc656de"
ADD_DRIVE_NOTEBOOK_ID = "55c6c0b4-bfb6-4f0c-8c52-5ef5bb00e5ee"

# --- source_rename recorded notebook + a source id from its GET_NOTEBOOK list ---
# The rename body's source-id leaf is UUID-normalized by the matcher, so the
# value is decorative; the ``UPDATE_SOURCE`` echo always replays the recorded
# source (id + title below), which is what the assertion pins.
RENAME_NOTEBOOK_ID = "06f0c5bd-108f-4c8b-8911-34b2acc656de"
RENAME_INPUT_SOURCE_ID = "a627aaef-e147-4c64-9f4e-f8aef245d000"
RENAME_ECHOED_SOURCE_ID = "b1b9efdd-b2af-4974-ad97-16025c05f1d7"
RENAME_ECHOED_TITLE = "VCR Test Renamed Source"

# ``sources_get_fulltext.yaml`` GET_NOTEBOOK was recorded against this notebook;
# ``source_read`` consumes its ``rLM1Ne`` (metadata) + ``hizoJc`` (full text).
GET_CONTENT_NOTEBOOK_ID = "167481cd-23a3-4331-9a45-c8948900bf91"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_list.yaml")
async def test_mcp_source_list_over_vcr() -> None:
    """``source_list`` returns the recorded sources through the real client.

    End-to-end: FastMCP ``Client`` → ``source_list`` tool →
    ``client.sources.list()`` → recorded ``GET_NOTEBOOK`` (``rLM1Ne``) RPC. The
    full-UUID notebook ref skips the resolver's ``LIST_NOTEBOOKS`` preflight, so
    the only RPC issued is the one the cassette holds.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("source_list", {"notebook": SOURCES_LIST_NOTEBOOK_ID})

    structured = result.structured_content
    assert isinstance(structured, dict)
    # The tool projects to ``{"notebook_id", "sources": [...]}`` via to_jsonable.
    assert structured["notebook_id"] == SOURCES_LIST_NOTEBOOK_ID
    sources = structured["sources"]
    assert isinstance(sources, list)
    assert sources, "expected at least one recorded source from the cassette"
    first = sources[0]
    assert isinstance(first, dict)
    # Each source carries an id + title decoded from the positional RPC row.
    assert first.get("id"), "recorded source is missing an id"
    assert "title" in first


@pytest.mark.asyncio
async def test_mcp_source_delete_two_step_confirm_over_vcr() -> None:
    """``source_delete`` confirm-gate: preview-then-delete over real cassettes.

    Step 1 (``confirm`` omitted): the tool resolves the source (full UUID, no
    list) then lists sources for the preview title (``GET_NOTEBOOK`` → ``rLM1Ne``,
    replayed from ``sources_list.yaml``) and returns a ``needs_confirmation``
    envelope WITHOUT issuing ``DELETE_SOURCE``.

    Step 2 (``confirm=True``): the tool issues the real ``DELETE_SOURCE``
    (``tGMBJ``) mutation, replayed from ``sources_delete.yaml``, and returns the
    deleted-status envelope.

    Two separate cassettes because the preview path needs the source-list RPC
    (which the delete cassette doesn't hold) while the confirmed path needs the
    delete RPC (which the list cassette doesn't hold).
    """
    # Step 1 — preview only (no delete RPC consumed beyond the list lookup).
    with notebooklm_vcr.use_cassette("sources_list.yaml"):
        async with build_mcp_client() as mcp_client:
            preview = await mcp_client.call_tool(
                "source_delete",
                {"notebook": SOURCES_LIST_NOTEBOOK_ID, "source": DELETE_SOURCE_ID},
            )

    preview_structured = preview.structured_content
    assert isinstance(preview_structured, dict)
    assert preview_structured["status"] == "needs_confirmation"
    inner = preview_structured["preview"]
    assert inner["action"] == "delete_source"
    # The resolved source id is echoed into the preview from the tool input.
    assert inner["source_id"] == DELETE_SOURCE_ID

    # Step 2 — confirmed delete replays the real DELETE_SOURCE mutation.
    with notebooklm_vcr.use_cassette("sources_delete.yaml"):
        async with build_mcp_client() as mcp_client:
            deleted = await mcp_client.call_tool(
                "source_delete",
                {
                    "notebook": DELETE_NOTEBOOK_ID,
                    "source": DELETE_SOURCE_ID,
                    "confirm": True,
                },
            )

    deleted_structured = deleted.structured_content
    assert isinstance(deleted_structured, dict)
    assert deleted_structured["status"] == "deleted"
    assert deleted_structured["notebook_id"] == DELETE_NOTEBOOK_ID
    assert deleted_structured["source_id"] == DELETE_SOURCE_ID


# ---------------------------------------------------------------------------
# source_add — one test per input variant the MCP tool supports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_add_url.yaml")
async def test_mcp_source_add_url_over_vcr() -> None:
    """``source_add source_type=url`` replays the recorded ``ADD_SOURCE`` RPC.

    The url flow runs ``_app.source_add`` (``build_source_add_plan`` →
    ``execute_source_add`` → ``client.sources.add_url``), a single ``ADD_SOURCE``
    (``izAoDd``) POST. ``to_jsonable`` projects the typed ``SourceAddResult`` to
    ``{"source": {<all Source fields>}}``.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_add",
            {
                "notebook": ADD_URL_NOTEBOOK_ID,
                "source_type": "url",
                "url": "https://en.wikipedia.org/wiki/Artificial_intelligence",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # The url flow wraps the added source under "source" (no notebook_id echo —
    # that is the drive flow's shape).
    assert set(structured) == {"source", "status"}
    source = structured["source"]
    assert isinstance(source, dict)
    assert source["id"] == "20d66b0b-787f-480e-a9c1-6823f7a12d8e"
    assert source["title"] == "Artificial intelligence - Wikipedia"
    assert source["url"] == "https://en.wikipedia.org/wiki/Artificial_intelligence"
    # The serialized Source carries the full typed-field set plus the agent-readable
    # ``kind``/``status_label`` labels source_add now adds (parity with source_list).
    assert set(source) == {
        "id",
        "title",
        "url",
        "_type_code",
        "created_at",
        "status",
        "kind",
        "status_label",
    }
    assert source["status"] == 2  # SourceStatus.READY
    assert source["status_label"] == "ready"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_add_text.yaml")
async def test_mcp_source_add_text_over_vcr() -> None:
    """``source_add source_type=text`` replays the recorded ``ADD_SOURCE`` RPC.

    Same ``ADD_SOURCE`` (``izAoDd``) method as the url flow, but a different
    request body shape (``add_text`` vs ``add_url``) — the ``freq`` body matcher
    keeps each cassette bound to its own add variant.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_add",
            {
                "notebook": ADD_URL_NOTEBOOK_ID,
                "source_type": "text",
                "text": "Inline pasted body for the VCR text source.",
                "title": "My Pasted Note",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == {"source", "status"}
    source = structured["source"]
    assert isinstance(source, dict)
    # The recorded ADD_SOURCE echo titles the source from the recording, not the
    # request — a text source has no url.
    assert source["id"] == "467b7f67-1b66-45fb-8cc7-6c04723f152d"
    assert source["title"] == "VCR Test Source"
    assert source["url"] is None
    assert source["status"] == 2  # SourceStatus.READY


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_add_file.yaml")
async def test_mcp_source_add_file_over_vcr(tmp_path) -> None:
    """``source_add source_type=file`` replays the upload + ``ADD_SOURCE_FILE`` RPC.

    The file flow validates a real on-disk path (``validate_upload_path`` checks
    ``is_file()``), uploads the bytes, then registers the source via
    ``ADD_SOURCE_FILE`` (``o4cbdc``). A freshly-registered file source is still
    ``PROCESSING`` (status 1) and carries no decoded type/url/created_at yet.
    """
    upload = tmp_path / "vcr_test_document.txt"
    upload.write_text("This is a test document for VCR cassette replay.")

    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_add",
            {
                "notebook": ADD_FILE_NOTEBOOK_ID,
                "source_type": "file",
                "path": str(upload),
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == {"source", "status"}
    source = structured["source"]
    assert isinstance(source, dict)
    assert source["id"] == "dc84ca28-2629-49ac-aec3-de45f0ec93e4"
    # The registered source is titled after the uploaded filename.
    assert source["title"] == "vcr_test_document.txt"
    assert source["url"] is None
    assert source["_type_code"] is None
    assert source["created_at"] is None
    assert source["status"] == 1  # SourceStatus.PROCESSING


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_add_drive.yaml")
async def test_mcp_source_add_drive_over_vcr() -> None:
    """``source_add source_type=drive`` replays the recorded ``ADD_SOURCE`` RPC.

    The drive flow runs ``_app.source_mutations.execute_source_add_drive`` (the
    neutral ``source_add`` core has no Drive path). Its result projects to a
    *richer* envelope than the other add variants: ``{"source", "notebook_id",
    "file_id", "mime_type"}`` — the latter three echoed from the request.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_add",
            {
                "notebook": ADD_DRIVE_NOTEBOOK_ID,
                "source_type": "drive",
                "document_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
                "mime_type": "google-doc",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == {"source", "notebook_id", "file_id", "mime_type", "status"}
    # The drive envelope echoes the request inputs alongside the added source.
    assert structured["notebook_id"] == ADD_DRIVE_NOTEBOOK_ID
    assert structured["file_id"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz"
    assert structured["mime_type"] == "google-doc"
    source = structured["source"]
    assert isinstance(source, dict)
    assert source["id"] == "ef72c03c-b429-41cb-ae79-8529d35d6d5b"
    assert source["title"] == "Rubisco Research: Status and Future"


# ---------------------------------------------------------------------------
# source_rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_rename.yaml")
async def test_mcp_source_rename_over_vcr() -> None:
    """``source_rename`` replays the recorded ``UPDATE_SOURCE`` mutation.

    With a full-UUID source ref the MCP tool skips list resolution and drives
    ``execute_source_rename`` with the pass-through resolver, so the only RPC
    issued is ``UPDATE_SOURCE`` (``b7Wfje``). The recorded echo is non-null, so
    ``rename`` returns the source from the echo *without* a ``GET_NOTEBOOK``
    refetch — the cassette's leading ``rLM1Ne`` interaction stays unplayed. The
    matcher UUID-normalizes the body's source-id leaf, so the *input* id is
    decorative; the assertion pins the recorded echo (id + title).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_rename",
            {
                "notebook": RENAME_NOTEBOOK_ID,
                "source": RENAME_INPUT_SOURCE_ID,
                "new_title": "Renamed via MCP",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == {"source", "notebook_id", "status"}
    assert structured["notebook_id"] == RENAME_NOTEBOOK_ID
    source = structured["source"]
    assert isinstance(source, dict)
    # The UPDATE_SOURCE echo carries the recorded source — not the request's
    # input id / new_title.
    assert source["id"] == RENAME_ECHOED_SOURCE_ID
    assert source["title"] == RENAME_ECHOED_TITLE
    assert source["status"] == 2  # SourceStatus.READY


# ---------------------------------------------------------------------------
# source_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_get_fulltext.yaml")
async def test_mcp_source_read_over_vcr() -> None:
    """``source_read`` returns the resolved source metadata AND its full text.

    ``execute_source_get`` calls ``client.sources.get_or_none`` (filters the
    notebook's ``GET_NOTEBOOK`` / ``rLM1Ne`` source list), then
    ``execute_source_fulltext`` issues ``GET_SOURCE`` (``hizoJc``) — so both
    cassette interactions are consumed. The full-UUID source ref skips the
    resolver's own list preflight, and the id must exist in the recorded list for
    the lookup to return a (non-``None``) source rather than NOT_FOUND.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_read",
            {
                "notebook": GET_CONTENT_NOTEBOOK_ID,
                "source": SOURCES_LIST_SOURCE_ID,
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # Now projects metadata PLUS the full-text fields.
    assert set(structured) == {
        "notebook_id",
        "source_id",
        "source",
        "content",
        "char_count",
        "truncated",
        "output_format",
    }
    assert structured["notebook_id"] == GET_CONTENT_NOTEBOOK_ID
    assert structured["source_id"] == SOURCES_LIST_SOURCE_ID
    source = structured["source"]
    assert isinstance(source, dict)
    assert source["id"] == SOURCES_LIST_SOURCE_ID
    assert source["title"] == SOURCES_LIST_SOURCE_TITLE
    assert source["url"] == "https://github.com/shareAI-lab/learn-claude-code"
    assert source["status"] == 2  # SourceStatus.READY
    # String labels accompany the raw codes (agent-readable).
    assert source["kind"] == "web_page"
    assert source["status_label"] == "ready"
    # Full text came back from the recorded GET_SOURCE interaction, bounded by the
    # default 10k cap: ``char_count`` is the FULL length, ``content`` is the (≤10k)
    # returned window, and ``truncated`` reflects whether the window omits any tail.
    assert structured["output_format"] == "text"
    content = structured["content"]
    assert isinstance(content, str) and content
    assert len(content) <= 10_000  # default cap applied
    assert structured["char_count"] >= len(content)
    assert structured["truncated"] == (structured["char_count"] > len(content))


# ---------------------------------------------------------------------------
# source_wait — single source + whole-notebook branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_wait.yaml")
async def test_mcp_source_wait_single_over_vcr() -> None:
    """``source_wait`` (single source) resolves immediately for a READY source.

    ``execute_source_wait`` drives ``client.sources.wait_until_ready``, whose
    poller probes source status via the same ``GET_NOTEBOOK`` (``rLM1Ne``) list.
    The recorded source is already ``READY``, so it resolves on the first poll
    and the tool returns the unified aggregate (``ok`` True, the source in
    ``ready``, all error buckets empty).

    The recorded source is a ``web_page``, so the #1698 content-sanity check
    issues a ``GET_SOURCE`` (``hizoJc``) fetch for it — replayed from
    ``sources_wait.yaml`` (= ``sources_list.yaml`` plus that one real interaction).
    Its body is ample (11,819 chars ≫ the thin threshold), so NO ``warning`` is
    attached: this end-to-end replay confirms a healthy ready page is not flagged.
    (The per-kind fetch logic — web-page-only, thin → warning — is pinned by the
    unit tests in ``tests/unit/mcp/test_sources.py``.)
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_wait",
            {
                "notebook": SOURCES_LIST_NOTEBOOK_ID,
                "source": SOURCES_LIST_SOURCE_ID,
                "timeout": 5.0,
                "interval": 0.1,
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # Unified aggregate shape, shared with the all-sources branch.
    assert set(structured) == {"notebook_id", "ok", "ready", "timed_out", "failed", "not_found"}
    assert structured["notebook_id"] == SOURCES_LIST_NOTEBOOK_ID
    assert structured["ok"] is True
    assert structured["timed_out"] == structured["failed"] == structured["not_found"] == []
    ready = structured["ready"]
    assert len(ready) == 1
    source = ready[0]
    assert source["id"] == SOURCES_LIST_SOURCE_ID
    assert source["status"] == 2  # SourceStatus.READY
    assert source["status_label"] == "ready"
    # The web-page body fetched ample text → no thin-content warning.
    assert "warning" not in source


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_wait.yaml", allow_playback_repeats=True)
async def test_mcp_source_wait_all_over_vcr() -> None:
    """``source_wait`` (no ``source``) waits for every source in the notebook.

    The all-sources branch lists sources (``GET_NOTEBOOK`` → ``rLM1Ne``) then
    fans out a per-source wait on each id. Every recorded source is already
    ``READY``, so they all resolve on the first poll and the tool returns the
    unified aggregate (``ok`` True, the sources in ``ready``, error buckets
    empty). ``allow_playback_repeats`` lets the per-source polls re-match the
    single recorded ``rLM1Ne`` interaction.

    The lone ``web_page`` source triggers the #1698 content-sanity ``GET_SOURCE``
    (``hizoJc``) fetch (the 7 ``pasted_text`` sources are skipped by the kind gate);
    its ample body yields no ``warning``. ``sources_wait.yaml`` carries that one
    extra interaction. (Web-page-only fetching is pinned by the unit tests.)
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "source_wait",
            {
                "notebook": SOURCES_LIST_NOTEBOOK_ID,
                "timeout": 5.0,
                "interval": 0.1,
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == {"notebook_id", "ok", "ready", "timed_out", "failed", "not_found"}
    assert structured["notebook_id"] == SOURCES_LIST_NOTEBOOK_ID
    assert structured["ok"] is True
    assert structured["timed_out"] == structured["failed"] == structured["not_found"] == []
    ready = structured["ready"]
    assert isinstance(ready, list)
    assert ready, "expected at least one ready source from the cassette"
    ids = {row["id"] for row in ready}
    assert SOURCES_LIST_SOURCE_ID in ids
    # Every returned source is READY (the wait only yields ready sources).
    assert all(row["status"] == 2 for row in ready)
    # The one web_page has ample text → no thin-content warning anywhere.
    assert all("warning" not in row for row in ready)
