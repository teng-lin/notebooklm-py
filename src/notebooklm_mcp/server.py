"""NotebookLM MCP Server.

Wraps the notebooklm-py client and exposes NotebookLM capabilities to
MCP-compatible AI systems via tools, resources, and prompts.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactFeatureUnavailableError,
    ArtifactInProgressTimeoutError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactTimeoutError,
    AuthError,
    AuthExtractionError,
    ChatResponseParseError,
    ClientError,
    ConfigurationError,
    DecodingError,
    IdempotencyVariantError,
    NetworkError,
    NonIdempotentRetryError,
    NotebookLimitError,
    NotebookNotFoundError,
    NotFoundError,
    RateLimitError,
    ResearchTaskMismatchError,
    ResearchTimeoutError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    ValidationError,
    WaitTimeoutError,
)

if TYPE_CHECKING:
    from notebooklm import NotebookLMClient

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


async def get_client() -> "NotebookLMClient":
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
            return _client_instance
        except FileNotFoundError as exc:
            raise RuntimeError(
                "NotebookLM authentication storage not found. "
                "Run 'notebooklm login' to authenticate, then restart the MCP server."
            ) from exc
        except Exception as exc:
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
        except Exception:  # noqa: BLE001
            pass
        finally:
            _client_instance = None


def _reset_client_for_testing() -> None:
    """Reset the singleton client instance.

    Intended for use in tests only. Allows test fixtures to clear the
    singleton without directly accessing module internals.
    """
    global _client_instance
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


def _handle_exception(exc: Exception, operation: str) -> str:
    """Format an exception into a structured error string for MCP tools.

    Args:
        exc: The caught exception.
        operation: Human-readable name of the operation that failed.
    """
    if isinstance(exc, AuthError):
        return (
            f"ERROR: Authentication failed during {operation}. "
            "Your Google session cookies may have expired. "
            "Run 'notebooklm login' in your terminal to re-authenticate."
        )

    if isinstance(exc, AuthExtractionError):
        return (
            f"ERROR: Failed to extract authentication tokens during {operation}. "
            "Google may have changed their page structure. Try 'notebooklm login' again, "
            "and if it persists, check for 'notebooklm-py' updates."
        )

    if isinstance(exc, RateLimitError):
        retry_after = getattr(exc, "retry_after", None)
        msg = f"ERROR: Rate limit exceeded during {operation}."
        if retry_after:
            msg += f" Please wait {retry_after} seconds before retrying."
        else:
            msg += " Please wait a few minutes and try again."
        return msg

    if isinstance(exc, ArtifactFeatureUnavailableError):
        return (
            f"ERROR: Feature unavailable during {operation}. "
            f"NotebookLM reports that {exc.artifact_type} generation is gated or disabled for this account."
        )

    if isinstance(exc, NotebookNotFoundError):
        return f"ERROR: Notebook not found: {exc.notebook_id}. Verify the ID and try again."

    if isinstance(exc, SourceNotFoundError):
        return f"ERROR: Source not found: {exc.source_id}. Verify the ID and try again."

    if isinstance(exc, ArtifactNotFoundError):
        return f"ERROR: Artifact not found: {exc.artifact_id}. Verify the ID and try again."

    if isinstance(exc, NotFoundError):
        return f"ERROR: Resource not found during {operation}. {exc}"

    if isinstance(exc, ArtifactTimeoutError):
        phase = "pending"
        if isinstance(exc, ArtifactInProgressTimeoutError):
            phase = "in-progress"
        return json.dumps(
            {
                "status": "timeout",
                "operation": operation,
                "task_id": exc.task_id,
                "notebook_id": exc.notebook_id,
                "timeout_seconds": exc.timeout_seconds,
                "last_status": exc.last_status,
                "stalled_phase": phase,
                "status_history": exc.status_history,
                "message": (
                    f"Generation timed out during {phase} phase after {exc.timeout_seconds}s. "
                    "The task may still be processing upstream. "
                    "You can poll for status later using its task_id."
                ),
            },
            indent=2,
        )

    if isinstance(exc, SourceTimeoutError):
        return (
            f"ERROR: Timed out waiting for source {exc.source_id} to become ready "
            f"after {exc.timeout}s (last status: {exc.last_status})."
        )

    if isinstance(exc, ResearchTimeoutError):
        return (
            f"ERROR: Research task {exc.task_id} in notebook {exc.notebook_id} "
            f"timed out after {exc.timeout}s (last status: {exc.last_status})."
        )

    if isinstance(exc, WaitTimeoutError):
        return f"ERROR: Operation '{operation}' timed out. {exc}"

    if isinstance(exc, (RuntimeError, TimeoutError)):
        return f"ERROR: {exc}"

    if isinstance(exc, ChatResponseParseError):
        return f"ERROR: Failed to parse chat response during {operation}. This may indicate an API change."

    if isinstance(exc, ArtifactDownloadError):
        return f"ERROR: Failed to download {exc.artifact_type} artifact {exc.artifact_id or ''}: {exc}"

    if isinstance(exc, ArtifactParseError):
        return f"ERROR: Failed to parse {exc.artifact_type} artifact data: {exc}"

    if isinstance(exc, ArtifactNotReadyError):
        return f"ERROR: Artifact {exc.artifact_id or ''} is not ready (status: {exc.status})."

    if isinstance(exc, ResearchTaskMismatchError):
        return f"ERROR: Research task mismatch during {operation}. {exc}"

    if isinstance(exc, SourceProcessingError):
        return (
            f"ERROR: Source {exc.source_id} failed to process. "
            f"Status code: {exc.status}. Ensure the content is valid and accessible."
        )

    if isinstance(exc, SourceAddError):
        msg = f"ERROR: Failed to add source {exc.url} during {operation}."
        if exc.cause:
            msg += f" Cause: {exc.cause}"
        return msg

    if isinstance(exc, NotebookLimitError):
        limit_info = f" (Count: {exc.current_count}/{exc.limit})" if exc.limit else f" (Count: {exc.current_count})"
        return f"ERROR: Notebook limit reached{limit_info}. Delete old notebooks and try again."

    if isinstance(exc, NonIdempotentRetryError):
        return f"ERROR: Cannot guarantee single-write semantics for {operation} on retry. {exc}"

    if isinstance(exc, IdempotencyVariantError):
        return f"ERROR: Unknown operation variant during {operation}. {exc}"

    if isinstance(exc, ValidationError):
        return f"ERROR: Validation failed during {operation}. {exc}"

    if isinstance(exc, ConfigurationError):
        return f"ERROR: Configuration error during {operation}. {exc}"

    if isinstance(exc, (ServerError, ClientError)):
        return f"ERROR: HTTP {exc.status_code} error during {operation} – {exc}"

    if isinstance(exc, RPCTimeoutError):
        return f"ERROR: RPC request timed out during {operation} after {exc.timeout_seconds}s."

    if isinstance(exc, RPCResponseTooLargeError):
        return f"ERROR: RPC response too large during {operation} ({exc.bytes_read} bytes exceeds {exc.limit_bytes} byte limit)."

    if isinstance(exc, NetworkError):
        return (
            f"ERROR: Network failure during {operation}. "
            f"Check your internet connection and try again. Original error: {exc.original_error}"
        )

    if isinstance(exc, UnknownRPCMethodError):
        msg = f"ERROR: Unknown RPC method encountered during {operation}."
        if exc.method_id:
            msg += f" (Method ID: {exc.method_id})"
        msg += " This may indicate an API change by Google. Check for 'notebooklm-py' updates."
        return msg

    if isinstance(exc, DecodingError):
        return f"ERROR: Failed to decode RPC response during {operation}. This may indicate an API change."

    if isinstance(exc, RPCError):
        msg = f"ERROR: RPC failure during {operation} – {exc}."
        if exc.method_id:
            msg += f" (Method: {exc.method_id})"
        return msg

    # Fallback for unexpected errors
    return f"ERROR: Unexpected error during {operation} – {type(exc).__name__}: {exc}"


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
    except Exception as exc:
        return _handle_exception(exc, "listing notebooks")


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
    except Exception as exc:
        return _handle_exception(exc, "creating notebook")


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
    except Exception as exc:
        return _handle_exception(exc, f"deleting notebook '{notebook_id}'")


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
    except Exception as exc:
        return _handle_exception(exc, "adding URL source")


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
    except Exception as exc:
        return _handle_exception(exc, "adding text source")


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
    except Exception as exc:
        return _handle_exception(exc, "getting answer")


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
    except Exception as exc:
        return _handle_exception(exc, "connecting to NotebookLM")

    # Step 1: Kick off generation
    try:
        status = await client.artifacts.generate_audio(
            notebook_id,
            language=language,
            instructions=instructions or None,
        )
    except Exception as exc:
        return _handle_exception(exc, "starting audio generation")

    # Step 2: Poll until complete
    try:
        final_status = await client.artifacts.wait_for_completion(
            notebook_id,
            status.task_id,
            timeout=timeout,
        )
    except Exception as exc:
        return _handle_exception(exc, "polling audio generation")

    if not final_status.is_complete:
        return json.dumps(
            {
                "status": final_status.status,
                "task_id": status.task_id,
                "message": "Audio generation did not succeed.",
            }
        )

    # Step 3: Download the MP3
    temp_file_to_cleanup = None
    if download_path:
        out_path = str(Path(download_path).expanduser().resolve())
    else:
        suffix = ".mp3"  # NotebookLM delivers audio as .mp3 container
        fd, out_path = tempfile.mkstemp(prefix="notebooklm_podcast_", suffix=suffix)
        os.close(fd)
        temp_file_to_cleanup = out_path

    try:
        saved_path = await client.artifacts.download_audio(notebook_id, out_path)
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
    finally:
        # Clean up temporary file if we created one and the download failed
        if temp_file_to_cleanup:
            try:
                Path(temp_file_to_cleanup).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


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
    except Exception as exc:
        return _handle_exception(exc, "connecting to NotebookLM")

    # Step 1: Generate
    try:
        status = await client.artifacts.generate_quiz(
            notebook_id,
            instructions=instructions or None,
        )
    except Exception as exc:
        return _handle_exception(exc, "starting quiz generation")

    # Step 2: Poll
    try:
        final_status = await client.artifacts.wait_for_completion(
            notebook_id,
            status.task_id,
            timeout=timeout,
        )
    except Exception as exc:
        return _handle_exception(exc, "polling quiz generation")

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
            # Always return a consistent structure
            return json.dumps({"status": "complete", "quiz": quiz_data}, indent=2)
        except json.JSONDecodeError:
            # If not valid JSON, wrap in a consistent error structure
            return json.dumps(
                {
                    "status": "complete_but_download_failed",
                    "task_id": status.task_id,
                    "error": "Quiz data is not valid JSON",
                    "raw_content": raw,
                }
            )
    except Exception as exc:
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
    except Exception as exc:
        return _handle_exception(exc, "connecting to NotebookLM")

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
        return _handle_exception(exc, "generating mind map")


# ---------------------------------------------------------------------------
# TOOL 10 – Troubleshooting
# ---------------------------------------------------------------------------


@mcp.tool(
    name="notebooklm_troubleshoot",
    description=(
        "Analyze a NotebookLM error message and provide diagnostic advice. "
        "Helps determine if an error is due to auth expiry, rate limits, "
        "API changes, or platform-specific issues (like X.com/Twitter scraping)."
    ),
)
async def notebooklm_troubleshoot(
    error_message: str,
    operation: str | None = None,
    notebook_id: str | None = None,
    source_id: str | None = None,
) -> str:
    """Diagnose a NotebookLM error and return actionable advice.

    Args:
        error_message: The raw error message or exception string.
        operation: (Optional) The tool or action that was being performed.
        notebook_id: (Optional) The notebook ID involved.
        source_id: (Optional) The source ID involved.
    """
    error_lower = error_message.lower()
    diagnosis = "Unknown error"
    action_steps = ["Check the error message for details."]
    debug_hint = None

    # 1. Authentication issues
    if any(k in error_lower for k in ["unauthorized", "csrf", "snlm0e", "fdrfje", "login", "authentication failed"]):
        diagnosis = "Authentication expired or session cookies invalid."
        action_steps = [
            "Run 'notebooklm auth check --test' to diagnose the specific auth issue.",
            "Run 'notebooklm login' in your local terminal to refresh cookies.",
            "If using --browser-cookies on macOS and getting prompts, try 'Always Allow' or using Firefox.",
            "If using Firefox Multi-Account Containers, use the 'firefox::ContainerName' syntax.",
        ]
        debug_hint = "If re-login fails, try deleting the browser profile in ~/.notebooklm/profiles/."

    # 2. Rate limiting
    elif any(k in error_lower for k in ["rate limit", "r7cb6c", "[3]", "429"]):
        diagnosis = "Google NotebookLM rate limit or quota exceeded."
        action_steps = [
            "Wait 5-10 minutes and retry the operation.",
            "If using the CLI, try adding the --retry flag (e.g. --retry 3).",
            "Reduce the frequency of intensive operations like audio generation.",
            "Check if you have reached the daily quota for Audio/Video overviews.",
        ]
        debug_hint = "Intensive operations like deep research or media generation have tighter quotas."

    # 3. RPC method drift or failures
    elif "rpc id" in error_lower or "unknownrpc" in error_lower or "no result found for rpc id" in error_lower:
        diagnosis = "RPC method mapping may have drifted or changed upstream."
        action_steps = [
            "Wait a few minutes and retry (sometimes transient).",
            "Check for 'notebooklm-py' library updates: 'pip install -U notebooklm-py'.",
            "Report the new RPC ID to the maintainers if it persists.",
        ]
        debug_hint = "Try 'NOTEBOOKLM_DEBUG_RPC=1' to see the actual RPC IDs returned by the server."

    # 4. X.com / Twitter issues
    elif any(k in error_lower for k in ["x.com", "twitter"]) or (
        operation == "adding URL source" and "privacy" in error_lower
    ):
        diagnosis = "X.com (Twitter) anti-scraping protection detected."
        action_steps = [
            "Pre-fetch the content using the 'bird' CLI: 'bird read <URL> > source.md'.",
            "Then add the local markdown file instead of the URL.",
            "Alternatively, use browser automation (Playwright) to fetch the markdown locally.",
        ]
        debug_hint = "Check if the source title shows 'Fixing X.com Privacy Errors' — if so, the ingest failed."

    # 5. File upload issues
    elif "html" in error_lower and "add" in (operation or "").lower():
        diagnosis = "NotebookLM rejects direct HTML/XHTML file uploads."
        action_steps = [
            "Convert the HTML file to plain text, Markdown, or PDF first.",
            "Use 'notebooklm source add' with the converted file.",
        ]
    elif "none" in error_lower and operation == "adding text source":
        diagnosis = "Known issue with native text file uploads returning None."
        action_steps = [
            "Use 'notebooklm source add' with the raw text content instead of a file path.",
            "In Python, use client.sources.add_text() instead of add_file().",
        ]

    # 6. Artifact generation issues
    elif (operation and "generate" in operation.lower()) or "artifact" in error_lower:
        if "timeout" in error_lower:
            diagnosis = "Generation task timed out before completion (media queue delay)."
            action_steps = [
                "The task may still be running upstream. Use 'notebooklm artifact list' to check status.",
                "Retry with a higher --timeout value (audio=1200, video=1800, cinematic=3600).",
            ]
        elif "none" in error_lower or "unavailable" in error_lower:
            diagnosis = "Generation feature is unavailable or returned no result."
            action_steps = [
                "This account may have reached its generation quota for today.",
                "Wait 24 hours or try a notebook with fewer sources.",
                "Ensure you have at least one valid source in the notebook.",
            ]
        elif "mind map" in error_lower or "data table" in error_lower:
            diagnosis = "Generation may have silently failed."
            action_steps = [
                "Wait 60 seconds and check 'notebooklm artifact list'.",
                "Try regenerating with different or fewer sources.",
            ]

    # 7. Resource not found
    elif "not found" in error_lower:
        diagnosis = "The requested resource (notebook, source, or artifact) does not exist."
        action_steps = [
            "Verify the ID is correct using 'notebooklm_list_notebooks' or 'notebook_metadata'.",
            "Ensure you are working in the correct profile/account.",
            "If it's an artifact download, fetch fresh URLs as they expire in hours.",
        ]

    # 8. Notebook limits
    elif "notebook limit reached" in error_lower:
        diagnosis = "The account has reached the maximum number of notebooks (approx 100)."
        action_steps = [
            "Delete old or unused notebooks using 'notebooklm_delete_notebook'.",
            "You can also use the web UI to delete notebooks if you prefer.",
        ]

    # 9. Network / SSL / Timeout issues
    elif any(k in error_lower for k in ["network", "connection", "ssl", "timeout", "deadline"]):
        diagnosis = "Network connectivity or transport timeout issue."
        action_steps = [
            "Check your internet connection and proxy/VPN settings.",
            "If direct connection to Google is blocked, ensure your environment allows it.",
            "For direct API users, check if you are sharing a client across threads (it's not thread-safe).",
        ]
        debug_hint = "On Windows, if the CLI hangs, ensure WindowsSelectorEventLoopPolicy is set."

    # 10. Windows specific (issue #75, #80)
    elif "unicodeencodeerror" in error_lower and os.name == "nt":
        diagnosis = "Windows Unicode encoding error (CP950/CP932/etc)."
        action_steps = [
            "Set the environment variable 'PYTHONUTF8=1' before running the command.",
            "Or run Python with the '-X utf8' flag.",
        ]

    result = {
        "diagnosis": diagnosis,
        "action_steps": action_steps,
        "context": {
            "operation": operation,
            "notebook_id": notebook_id,
            "source_id": source_id,
        },
    }
    if debug_hint:
        result["debug_hint"] = debug_hint

    return json.dumps(result, indent=2)


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
    except Exception as exc:
        return _handle_exception(exc, "fetching metadata")


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
    except Exception as exc:
        return _handle_exception(exc, "fetching source full text")


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
