#!/usr/bin/env python3
"""Standalone release sanity tool: drive a RUNNING remote MCP server's file
side-channel end to end and print PASS/FAIL.

This productionizes the upload+download round-trip the Layer-B e2e tests run
in-process, but against a real deployed server (the claude.ai connector backend)
— it is the bootstrap for the manual "MCP connector smoke" release checklist in
``docs/releasing.md``. Point it at the server's public base URL + bearer token;
it mints a signed upload URL through ``source_add(file)``, POSTs a small file to
it, confirms the source landed, then (optionally) mints a download URL for an
existing artifact and streams it back.

Usage::

    python scripts/mcp_live_smoke.py \
        --base-url https://your-tunnel.example.com \
        --bearer "$NOTEBOOKLM_MCP_TOKEN" \
        --notebook <notebook-id-or-name> \
        [--download-notebook <id>] [--artifact-type report]

Requires the ``mcp`` extra (``fastmcp`` + ``httpx``). Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit

#: Artifact ``_artifact_type`` values whose download is wired through
#: ``artifact_download`` (serialized form, underscored; the tool key is hyphenated).
_DOWNLOADABLE = {
    "audio",
    "video",
    "slide_deck",
    "infographic",
    "report",
    "mind_map",
    "data_table",
    "quiz",
    "flashcards",
}


def _ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live PASS/FAIL smoke for a running remote MCP server's file routes.",
    )
    parser.add_argument("--base-url", required=True, help="Public base URL of the MCP server.")
    parser.add_argument(
        "--bearer",
        default=os.environ.get("NOTEBOOKLM_MCP_TOKEN"),
        help="Bearer token (defaults to $NOTEBOOKLM_MCP_TOKEN).",
    )
    parser.add_argument(
        "--notebook",
        required=True,
        help="Notebook id or name to upload the smoke source into.",
    )
    parser.add_argument(
        "--download-notebook",
        default=None,
        help="Notebook to download an existing artifact from (default: --notebook).",
    )
    parser.add_argument(
        "--artifact-type",
        default=None,
        help="Force a download artifact_type (default: auto-pick an existing one).",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Only run the upload round-trip.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> bool:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    if not args.bearer:
        _fail("no bearer token: pass --bearer or set $NOTEBOOKLM_MCP_TOKEN")
        return False

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.bearer}"}
    transport = StreamableHttpTransport(f"{base_url}/mcp", headers=headers)

    passed = True
    async with Client(transport) as mcp, httpx.AsyncClient(timeout=120.0) as http:
        # --- upload round-trip -------------------------------------------------
        print("Upload round-trip:")
        body = b"notebooklm MCP live smoke upload.\n"
        result = await mcp.call_tool(
            "source_add",
            {
                "notebook": args.notebook,
                "source_type": "file",
                "title": "MCP live smoke",
                "mime_type": "text/plain",
            },
        )
        structured = result.structured_content or {}
        if structured.get("status") != "upload_required" or not structured.get("url"):
            _fail(f"source_add(file) did not return an upload URL: {structured}")
            return False
        _ok("minted signed upload URL")

        up = await http.post(
            structured["url"]
            + ("&" if urlsplit(structured["url"]).query else "?")
            + "filename=mcp-smoke.txt",
            content=body,
            headers={"Accept": "application/json", "Content-Type": "text/plain"},
        )
        if up.status_code != 200:
            _fail(f"upload POST returned {up.status_code}: {up.text}")
            return False
        source_id = up.json().get("source_id")
        if not source_id:
            _fail(f"upload response missing source_id: {up.text}")
            return False
        _ok(f"uploaded source {source_id}")

        listing = await mcp.call_tool("source_list", {"notebook": args.notebook})
        ids = [s["id"] for s in (listing.structured_content or {}).get("sources", [])]
        if source_id in ids:
            _ok("source confirmed live in source_list")
        else:
            _fail("uploaded source not found in source_list")
            passed = False

        # --- download round-trip ----------------------------------------------
        if args.skip_download:
            print("Download round-trip: skipped (--skip-download)")
            return passed

        print("Download round-trip:")
        dl_notebook = args.download_notebook or args.notebook
        art_listing = await mcp.call_tool("artifact_list", {"notebook": dl_notebook})
        artifacts = (art_listing.structured_content or {}).get("artifacts", [])
        if args.artifact_type:
            # Tolerate underscored values copied from artifact metadata
            # (slide_deck/mind_map/…); the download tool expects hyphens.
            dl_type = args.artifact_type.replace("_", "-")
        else:
            candidate = next(
                (
                    a
                    for a in artifacts
                    if a.get("_artifact_type") in _DOWNLOADABLE
                    and a.get("status") in (None, "ready", "completed")
                ),
                None,
            )
            if candidate is None:
                _fail("no existing downloadable artifact (pass --artifact-type or generate one)")
                return False
            dl_type = candidate["_artifact_type"].replace("_", "-")

        dl_result = await mcp.call_tool(
            "artifact_download", {"notebook": dl_notebook, "artifact_type": dl_type}
        )
        dl_structured = dl_result.structured_content or {}
        if dl_structured.get("status") != "download_ready" or not dl_structured.get("url"):
            _fail(f"artifact_download did not return a download URL: {dl_structured}")
            return False
        _ok(f"minted signed download URL for {dl_type}")

        resp = await http.get(dl_structured["url"])
        if resp.status_code != 200 or not resp.content:
            _fail(f"download GET returned {resp.status_code} ({len(resp.content)} bytes)")
            return False
        _ok(f"downloaded {len(resp.content)} bytes")

    return passed


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        passed = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - top-level smoke wants one clean line
        print(f"  FAIL  unexpected error: {exc}")
        passed = False
    print()
    print("RESULT: PASS" if passed else "RESULT: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
