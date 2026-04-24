"""NotebookLM MCP Server.

Exposes Google NotebookLM capabilities to MCP-compatible AI systems
(Claude Desktop, Cursor, Copilot, etc.) via the Model Context Protocol.

Usage:
    # Authenticate first (one-time setup):
    notebooklm login

    # Run the MCP server:
    python -m notebooklm_mcp
"""

__version__ = "0.1.0"
