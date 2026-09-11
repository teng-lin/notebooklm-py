#!/usr/bin/env python3
"""Run the shared deployed-server MCP smoke driver.

Usage: python scripts/mcp_live_smoke.py --help
"""

from notebooklm.mcp._smoke import main

if __name__ == "__main__":
    raise SystemExit(main())
