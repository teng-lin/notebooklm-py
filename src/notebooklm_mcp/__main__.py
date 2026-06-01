"""Entry point for ``python -m notebooklm_mcp``."""

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger("notebooklm_mcp")


def main() -> None:
    """Run the NotebookLM MCP server.
    
    By default, runs SSE transport on localhost:8000.
    Use --host and --port to customize the binding address.
    """
    parser = argparse.ArgumentParser(description="Run the NotebookLM MCP server")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the MCP server to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the MCP server to (default: 8000)"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="sse",
        help="Transport type to use (default: sse)"
    )
    
    args = parser.parse_args()
    
    from notebooklm_mcp.server import _shutdown_client, mcp

    async def _run() -> None:
        try:
            if args.transport == "stdio":
                await mcp.run_stdio_async()
            else:  # sse
                logger.info(f"Starting MCP server on {args.host}:{args.port}")
                await mcp.run_sse_async(host=args.host, port=args.port)
        finally:
            await _shutdown_client()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
