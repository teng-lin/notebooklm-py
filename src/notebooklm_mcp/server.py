"""NotebookLM MCP Server.

Wraps the notebooklm-py client and exposes NotebookLM capabilities to
MCP-compatible AI systems via tools, resources, and prompts.

Logging goes to stderr only so it never interferes with the MCP/stdio protocol.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging: stderr ONLY – stdout is reserved for the MCP protocol
# ---------------------------------------------------------------------------
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])

logger = logging.getLogger("notebooklm_mcp")

# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="notebooklm",
    instructions=(
        "Interact with Google NotebookLM notebooks: list/create/delete notebooks, "
        "add web/text sources, ask questions, generate audio podcasts, quizzes, and mind maps. "
        "Run 'notebooklm login' once to authenticate before starting this server."
    ),
)

# ---------------------------------------------------------------------------
# Shared client singleton – lazily initialised on first tool call
# ---------------------------------------------------------------------------
_client_instance: Any = None
_client_lock = asyncio.Lock()


async def get_client():
    """Return the shared NotebookLMClient, creating it on first call.

    Raises:
        RuntimeError: If authentication storage is missing (user must run
            ``notebooklm login`` first).
    """
    global _client_instance

    async with _client_lock:
        if _client_instance is not None:
            return _client_instance

        try:
            from notebooklm import NotebookLMClient
        except ImportError as exc:
            raise RuntimeError(
                "notebooklm-py is not installed. "
                "Install it with: pip install 'notebooklm-py[browser]'"
            ) from exc

        try:
            client = await NotebookLMClient.from_storage()
            await client.__aenter__()
            _client_instance = client
            logger.info("NotebookLM client initialised successfully")
            return _client_instance
        except FileNotFoundError as exc:
            raise RuntimeError(
                "NotebookLM authentication storage not found. "
                "Run 'notebooklm login' to authenticate, then restart the MCP server."
            ) from exc
        except Exception as exc:
            logger.error("Failed to initialise NotebookLM client: %s", exc)
            raise RuntimeError(
                f"Failed to connect to NotebookLM ({exc}). "
                "Check your authentication by running 'notebooklm login'."
            ) from exc


async def _shutdown_client() -> None:
    """Cleanly close the shared client on server shutdown."""
    global _client_instance
    if _client_instance is not None:
        try:
            await _client_instance.__aexit__(None, None, None)
            logger.info("NotebookLM client closed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing NotebookLM client: %s", exc)
        finally:
            _client_instance = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notebook_to_dict(nb) -> dict:
    return {
        "id": nb.id,
        "title": nb.title,
        "created_at": nb.created_at.isoformat() if nb.created_at else None,
        "updated_at": nb.updated_at.isoformat() if nb.updated_at else None,
    }


def _source_to_dict(src) -> dict:
    return {
        "id": src.id,
        "title": src.title,
        "kind": str(src.kind) if src.kind else None,
        "status": str(src.status) if src.status else None,
        "url": getattr(src, "url", None),
    }


# ---------------------------------------------------------------------------
# TOOL 1 – List notebooks
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_list_notebooks",
    description="List all Google NotebookLM notebooks for the authenticated account.",
)
async def notebooklm_list_notebooks() -> str:
    """Return JSON array of all notebooks."""
    try:
        client = await get_client()
        notebooks = await client.notebooks.list()
        result = [_notebook_to_dict(nb) for nb in notebooks]
        return json.dumps(result, indent=2)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebooklm_list_notebooks failed: %s", exc)
        return f"ERROR: Failed to list notebooks – {exc}"


# ---------------------------------------------------------------------------
# TOOL 2 – Create notebook
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_create_notebook",
    description="Create a new Google NotebookLM notebook with the given title.",
)
async def notebooklm_create_notebook(title: str) -> str:
    """Create a notebook and return its metadata as JSON.

    Args:
        title: Display title for the new notebook.
    """
    if not title or not title.strip():
        return "ERROR: 'title' must not be empty"
    try:
        client = await get_client()
        nb = await client.notebooks.create(title.strip())
        return json.dumps(_notebook_to_dict(nb), indent=2)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebooklm_create_notebook failed: %s", exc)
        return f"ERROR: Failed to create notebook – {exc}"


# ---------------------------------------------------------------------------
# TOOL 3 – Delete notebook
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_delete_notebook",
    description="Permanently delete a NotebookLM notebook by its ID.",
)
async def notebooklm_delete_notebook(notebook_id: str) -> str:
    """Delete a notebook.

    Args:
        notebook_id: The unique notebook ID (e.g. from notebooklm_list_notebooks).
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"
    try:
        client = await get_client()
        await client.notebooks.delete(notebook_id.strip())
        return json.dumps({"deleted": True, "notebook_id": notebook_id.strip()})
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebooklm_delete_notebook failed: %s", exc)
        return f"ERROR: Failed to delete notebook '{notebook_id}' – {exc}"


# ---------------------------------------------------------------------------
# TOOL 4 – Add URL source
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_add_source_url",
    description=(
        "Add a web URL (or YouTube video URL) as a source to a NotebookLM notebook. "
        "Waits for the source to finish processing before returning."
    ),
)
async def notebooklm_add_source_url(notebook_id: str, url: str) -> str:
    """Ingest a URL into a notebook and return the created source as JSON.

    Args:
        notebook_id: Target notebook ID.
        url: Fully-qualified URL (https://...) or YouTube URL.
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"
    if not url or not url.strip():
        return "ERROR: 'url' must not be empty"
    try:
        client = await get_client()
        source = await client.sources.add_url(notebook_id.strip(), url.strip(), wait=True)
        return json.dumps(_source_to_dict(source), indent=2)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebooklm_add_source_url failed: %s", exc)
        return f"ERROR: Failed to add URL source – {exc}"


# ---------------------------------------------------------------------------
# TOOL 5 – Add text source
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_add_source_text",
    description=(
        "Add plain text as a source to a NotebookLM notebook. "
        "Waits for the source to finish processing before returning."
    ),
)
async def notebooklm_add_source_text(notebook_id: str, title: str, text: str) -> str:
    """Ingest text content into a notebook and return the created source as JSON.

    Args:
        notebook_id: Target notebook ID.
        title: Display name for the pasted-text source.
        text: The body of text to add as a source.
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"
    if not title or not title.strip():
        return "ERROR: 'title' must not be empty"
    if not text or not text.strip():
        return "ERROR: 'text' must not be empty"
    try:
        client = await get_client()
        source = await client.sources.add_text(
            notebook_id.strip(),
            title.strip(),
            text.strip(),
            wait=True,
        )
        return json.dumps(_source_to_dict(source), indent=2)
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebooklm_add_source_text failed: %s", exc)
        return f"ERROR: Failed to add text source – {exc}"


# ---------------------------------------------------------------------------
# TOOL 6 – Chat / ask
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_ask_chat",
    description=(
        "Ask a question to a NotebookLM notebook and receive an AI answer "
        "grounded in the notebook's sources."
    ),
)
async def notebooklm_ask_chat(notebook_id: str, query: str) -> str:
    """Query a notebook and return the answer text.

    Args:
        notebook_id: Target notebook ID.
        query: The question or prompt to send to the notebook.
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"
    if not query or not query.strip():
        return "ERROR: 'query' must not be empty"
    try:
        client = await get_client()
        result = await client.chat.ask(notebook_id.strip(), query.strip())
        return result.answer
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebooklm_ask_chat failed: %s", exc)
        return f"ERROR: Failed to get answer – {exc}"


# ---------------------------------------------------------------------------
# TOOL 7 – Generate audio podcast (HIGH PRIORITY)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_generate_audio_podcast",
    description=(
        "Generate an Audio Overview (podcast) for a NotebookLM notebook. "
        "Optionally provide custom instructions, language code (e.g. 'en', 'es', 'fr', 'de', 'ja'), "
        "and a local path to download the MP3. "
        "Generation typically takes 2–5 minutes. Returns the saved file path or status."
    ),
)
async def notebooklm_generate_audio_podcast(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
    download_path: str | None = None,
    timeout: float = 360.0,
) -> str:
    """Generate, wait for, optionally download, and return a podcast for a notebook.

    Args:
        notebook_id: Target notebook ID.
        instructions: Optional custom prompt for the podcast hosts (style, focus, etc.).
        language: BCP-47 language code for the podcast (default: 'en').
        download_path: Local filesystem path to save the MP3 file.
            If omitted, a file is written to the system temp directory.
        timeout: Maximum seconds to wait for generation (default: 360).

    Returns:
        JSON with generation status and, if downloaded, the absolute saved path.
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"

    notebook_id = notebook_id.strip()
    language = (language or "en").strip()

    try:
        client = await get_client()
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    # Step 1: Kick off generation
    try:
        status = await client.artifacts.generate_audio(
            notebook_id,
            language=language,
            instructions=instructions or None,
        )
        logger.info(
            "Audio generation started for notebook %s, task_id=%s", notebook_id, status.task_id
        )
    except Exception as exc:
        logger.error("generate_audio failed: %s", exc)
        return f"ERROR: Failed to start audio generation – {exc}"

    # Step 2: Poll until complete
    try:
        final_status = await client.artifacts.wait_for_completion(
            notebook_id,
            status.task_id,
            timeout=timeout,
        )
    except TimeoutError:
        return json.dumps(
            {
                "status": "timeout",
                "task_id": status.task_id,
                "message": (
                    f"Audio generation did not complete within {timeout}s. "
                    "It may still be processing. Re-run with a longer timeout."
                ),
            }
        )
    except Exception as exc:
        logger.error("wait_for_completion failed: %s", exc)
        return f"ERROR: Generation polling failed – {exc}"

    if not final_status.is_complete:
        return json.dumps(
            {
                "status": final_status.status,
                "task_id": status.task_id,
                "message": "Audio generation did not succeed.",
            }
        )

    # Step 3: Download the MP3
    if download_path:
        out_path = str(Path(download_path).expanduser().resolve())
    else:
        suffix = ".mp4"  # NotebookLM delivers audio as .mp4 container
        fd, out_path = tempfile.mkstemp(prefix="notebooklm_podcast_", suffix=suffix)
        os.close(fd)

    try:
        saved_path = await client.artifacts.download_audio(notebook_id, out_path)
        logger.info("Audio podcast saved to %s", saved_path)
        return json.dumps(
            {
                "status": "complete",
                "task_id": status.task_id,
                "file_path": str(Path(saved_path).resolve()),
                "language": language,
            },
            indent=2,
        )
    except Exception as exc:
        logger.error("download_audio failed: %s", exc)
        return json.dumps(
            {
                "status": "complete_but_download_failed",
                "task_id": status.task_id,
                "error": str(exc),
                "message": (
                    "Generation succeeded but download failed. "
                    "Try notebooklm_download_audio separately."
                ),
            }
        )


# ---------------------------------------------------------------------------
# TOOL 8 – Generate quiz
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_generate_quiz",
    description=(
        "Generate a quiz for a NotebookLM notebook and return the questions as JSON. "
        "Optionally provide custom instructions."
    ),
)
async def notebooklm_generate_quiz(
    notebook_id: str,
    instructions: str | None = None,
    timeout: float = 180.0,
) -> str:
    """Generate a quiz and return the parsed JSON content.

    Args:
        notebook_id: Target notebook ID.
        instructions: Optional custom instructions for the quiz (topic focus, style, etc.).
        timeout: Maximum seconds to wait for generation (default: 180).
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"

    notebook_id = notebook_id.strip()

    try:
        client = await get_client()
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    # Step 1: Generate
    try:
        status = await client.artifacts.generate_quiz(
            notebook_id,
            instructions=instructions or None,
        )
        logger.info(
            "Quiz generation started for notebook %s, task_id=%s", notebook_id, status.task_id
        )
    except Exception as exc:
        logger.error("generate_quiz failed: %s", exc)
        return f"ERROR: Failed to start quiz generation – {exc}"

    # Step 2: Poll
    try:
        final_status = await client.artifacts.wait_for_completion(
            notebook_id,
            status.task_id,
            timeout=timeout,
        )
    except TimeoutError:
        return json.dumps(
            {
                "status": "timeout",
                "task_id": status.task_id,
                "message": f"Quiz generation did not complete within {timeout}s.",
            }
        )
    except Exception as exc:
        logger.error("wait_for_completion (quiz) failed: %s", exc)
        return f"ERROR: Quiz polling failed – {exc}"

    if not final_status.is_complete:
        return json.dumps({"status": final_status.status, "task_id": status.task_id})

    # Step 3: Download to a temp file and read back JSON
    fd, tmp_path = tempfile.mkstemp(prefix="notebooklm_quiz_", suffix=".json")
    os.close(fd)
    try:
        saved = await client.artifacts.download_quiz(notebook_id, tmp_path)
        quiz_path = Path(saved)
        raw = quiz_path.read_text(encoding="utf-8")
        try:
            quiz_data = json.loads(raw)
        except json.JSONDecodeError:
            quiz_data = raw
        return json.dumps({"status": "complete", "quiz": quiz_data}, indent=2)
    except Exception as exc:
        logger.error("download_quiz failed: %s", exc)
        return json.dumps(
            {
                "status": "complete_but_download_failed",
                "task_id": status.task_id,
                "error": str(exc),
            }
        )
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# TOOL 9 – Generate mind map
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_generate_mind_map",
    description=(
        "Generate a hierarchical mind map for a NotebookLM notebook "
        "and return the JSON structure."
    ),
)
async def notebooklm_generate_mind_map(
    notebook_id: str,
    instructions: str | None = None,
    language: str = "en",
) -> str:
    """Generate and return a mind map for the notebook.

    Args:
        notebook_id: Target notebook ID.
        instructions: Optional custom instructions for the mind map.
        language: BCP-47 language code (default: 'en').
    """
    if not notebook_id or not notebook_id.strip():
        return "ERROR: 'notebook_id' must not be empty"

    notebook_id = notebook_id.strip()
    language = (language or "en").strip()

    try:
        client = await get_client()
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    try:
        result = await client.artifacts.generate_mind_map(
            notebook_id,
            language=language,
            instructions=instructions or None,
        )
        return json.dumps(
            {
                "status": "complete",
                "note_id": result.get("note_id"),
                "mind_map": result.get("mind_map"),
            },
            indent=2,
        )
    except Exception as exc:
        logger.error("generate_mind_map failed: %s", exc)
        return f"ERROR: Failed to generate mind map – {exc}"


# ---------------------------------------------------------------------------
# RESOURCE – notebook://{id}/metadata
# ---------------------------------------------------------------------------


@mcp.resource(
    uri="notebook://{notebook_id}/metadata",
    name="notebook_metadata",
    description=(
        "Full metadata for a NotebookLM notebook including its sources list. "
        "URI pattern: notebook://<notebook_id>/metadata"
    ),
    mime_type="application/json",
)
async def notebook_metadata(notebook_id: str) -> str:
    """Return notebook metadata + sources as JSON.

    Args:
        notebook_id: The notebook ID extracted from the resource URI.
    """
    try:
        client = await get_client()
        nb = await client.notebooks.get(notebook_id)
        sources = await client.sources.list(notebook_id)
        payload = {
            **_notebook_to_dict(nb),
            "sources": [_source_to_dict(s) for s in sources],
        }
        return json.dumps(payload, indent=2)
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.error("notebook_metadata resource failed for %s: %s", notebook_id, exc)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# RESOURCE – notebook://{id}/sources/{source_id}
# ---------------------------------------------------------------------------


@mcp.resource(
    uri="notebook://{notebook_id}/sources/{source_id}",
    name="notebook_source_fulltext",
    description=(
        "Full indexed text content of a specific source inside a NotebookLM notebook. "
        "URI pattern: notebook://<notebook_id>/sources/<source_id>"
    ),
    mime_type="text/plain",
)
async def notebook_source_fulltext(notebook_id: str, source_id: str) -> str:
    """Return the full ingested text of a source.

    Args:
        notebook_id: The notebook ID extracted from the URI.
        source_id: The source ID extracted from the URI.
    """
    try:
        client = await get_client()
        fulltext = await client.sources.get_fulltext(notebook_id, source_id)
        return fulltext.content or ""
    except RuntimeError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.error("notebook_source_fulltext failed for %s/%s: %s", notebook_id, source_id, exc)
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# PROMPT – notebooklm_deep_research
# ---------------------------------------------------------------------------


@mcp.prompt(
    name="notebooklm_deep_research",
    description=(
        "An end-to-end deep research workflow: create a notebook, ingest one or more URLs, "
        "summarise the content, and structure a multi-step research analysis."
    ),
)
def notebooklm_deep_research(topic: str, urls: str) -> str:
    """Return structured research instructions for an LLM agent.

    Args:
        topic: The research topic or question.
        urls: Comma-separated list of URLs to ingest as sources.
    """
    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    url_bullets = "\n".join(f"  - {u}" for u in url_list)

    return f"""You are a research assistant using Google NotebookLM.

Your task: conduct deep research on the topic: **{topic}**

## Step 1 – Create a notebook
Call `notebooklm_create_notebook` with title: "Research: {topic}"
Save the returned notebook_id for all subsequent steps.

## Step 2 – Ingest sources
For each URL below, call `notebooklm_add_source_url` with the notebook_id:
{url_bullets}

Wait for each source to report status "ready" before proceeding.

## Step 3 – Initial summarisation
Call `notebooklm_ask_chat` with the notebook_id and this query:
"Provide a comprehensive summary of all sources covering: {topic}"

## Step 4 – Deep-dive questions
Ask the following follow-up questions one by one using `notebooklm_ask_chat`:
  a) "What are the key claims or findings related to {topic}?"
  b) "What evidence supports or contradicts these claims?"
  c) "What are the main open questions or gaps in knowledge about {topic}?"
  d) "What practical conclusions or recommendations can be drawn?"

## Step 5 – Synthesise
Combine all answers into a structured research report with:
- Executive summary
- Key findings (with citations to sources)
- Contradictions or caveats
- Open questions
- Recommended next steps

Present the final report in Markdown format.
"""
