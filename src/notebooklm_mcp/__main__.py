"""Entry point for ``python -m notebooklm_mcp``."""

import asyncio
import logging
import sys

logger = logging.getLogger("notebooklm_mcp")


def main() -> None:
    """Run the NotebookLM MCP server (stdio transport by default)."""
    from notebooklm_mcp.server import _shutdown_client, mcp

    async def _run() -> None:
        try:
            await mcp.run_async()
        finally:
            await _shutdown_client()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
