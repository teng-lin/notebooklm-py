"""Shared driver for smoke-testing a deployed NotebookLM MCP server.

The release helper and E2E suites import this module so Studio artifact selection
and the real-socket upload/download flow cannot drift independently of each other.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

DOWNLOADABLE_ARTIFACT_TYPES = frozenset(
    {
        "audio",
        "video",
        "slide-deck",
        "infographic",
        "report",
        "mind-map",
        "data-table",
        "quiz",
        "flashcards",
    }
)
_URL_BACKED_STUDIO_TYPES = frozenset({"audio", "video", "infographic", "slide-deck"})


def pick_downloadable_artifact(
    items: list[dict[str, Any]], *, backend: str
) -> dict[str, Any] | None:
    """Return the first ready Studio item whose payload the backend can resolve."""

    candidates = [
        item
        for item in items
        if item.get("type") in DOWNLOADABLE_ARTIFACT_TYPES
        and item.get("status_label") in (None, "ready", "completed")
        and (
            item.get("type") not in _URL_BACKED_STUDIO_TYPES
            or (backend == "android" and item.get("type") == "slide-deck")
            or bool(item.get("url"))
        )
    ]
    confirmed = next(
        (
            item
            for item in candidates
            if not (
                backend == "android" and item.get("type") == "slide-deck" and not item.get("url")
            )
        ),
        None,
    )
    return confirmed or next(iter(candidates), None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the thin release helper's command-line arguments."""

    parser = argparse.ArgumentParser(
        description="Live PASS/FAIL smoke for a running remote MCP server's file routes."
    )
    parser.add_argument("--base-url", required=True, help="Public base URL of the MCP server.")
    parser.add_argument(
        "--bearer",
        default=os.environ.get("NOTEBOOKLM_MCP_TOKEN"),
        help="Bearer token (defaults to $NOTEBOOKLM_MCP_TOKEN).",
    )
    parser.add_argument("--notebook", required=True, help="Notebook id or name for the smoke run.")
    parser.add_argument(
        "--download-notebook",
        default=None,
        help="Notebook to download from (default: --notebook).",
    )
    parser.add_argument(
        "--artifact-type",
        default=None,
        choices=sorted(DOWNLOADABLE_ARTIFACT_TYPES),
        help="Force a Studio artifact type instead of selecting a ready item.",
    )
    parser.add_argument(
        "--backend",
        choices=("web", "android"),
        default="web",
        help="Artifact backend used by the deployed server (default: web).",
    )
    parser.add_argument("--skip-download", action="store_true", help="Only run the upload leg.")
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow cleartext HTTP only for a loopback smoke server.",
    )
    return parser.parse_args(argv)


def _structured(result: object) -> Mapping[str, Any]:
    value = getattr(result, "structured_content", None)
    return value if isinstance(value, Mapping) else {}


async def run(args: argparse.Namespace) -> bool:
    """Exercise signed upload/download routes through a deployed MCP server."""

    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    if not args.bearer:
        print("  FAIL  no bearer token: pass --bearer or set $NOTEBOOKLM_MCP_TOKEN")
        return False

    base_url = args.base_url.rstrip("/")
    parsed_base = urlsplit(base_url)
    is_loopback_http = parsed_base.scheme == "http" and parsed_base.hostname in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    if parsed_base.scheme != "https" and not (args.allow_insecure_http and is_loopback_http):
        print("  FAIL  --base-url must use HTTPS (or explicit loopback-only insecure HTTP)")
        return False
    transport = StreamableHttpTransport(
        f"{base_url}/mcp", headers={"Authorization": f"Bearer {args.bearer}"}
    )
    passed = True
    async with Client(transport) as mcp, httpx.AsyncClient(timeout=120.0) as http:
        print("Upload round-trip:")
        result = await mcp.call_tool(
            "source_add",
            {
                "notebook": args.notebook,
                "source_type": "file",
                "title": "MCP live smoke",
                "mime_type": "text/plain",
            },
        )
        structured = _structured(result)
        if structured.get("status") != "upload_required" or not structured.get("url"):
            print(
                "  FAIL  source_add(file) did not return an upload URL: "
                f"status={structured.get('status')!r}, keys={sorted(structured)}"
            )
            return False
        print("  PASS  minted signed upload URL")

        upload_url = str(structured["url"])
        up = await http.post(
            upload_url + ("&" if urlsplit(upload_url).query else "?") + "filename=mcp-smoke.txt",
            content=b"notebooklm MCP live smoke upload.\n",
            headers={"Accept": "application/json", "Content-Type": "text/plain"},
        )
        if up.status_code != 200:
            print(f"  FAIL  upload POST returned {up.status_code}: {up.text}")
            return False
        source_id = up.json().get("source_id")
        if not source_id:
            print(f"  FAIL  upload response missing source_id: {up.text}")
            return False
        print(f"  PASS  uploaded source {source_id}")

        listing = await mcp.call_tool("source_list", {"notebook": args.notebook})
        source_ids = [source.get("id") for source in _structured(listing).get("sources", [])]
        if source_id in source_ids:
            print("  PASS  source confirmed live in source_list")
        else:
            print("  FAIL  uploaded source not found in source_list")
            passed = False

        if args.skip_download:
            print("Download round-trip: skipped (--skip-download)")
            return passed

        print("Download round-trip:")
        notebook = args.download_notebook or args.notebook
        candidate = None
        offset = 0
        while not args.artifact_type:
            studio = await mcp.call_tool("studio_list", {"notebook": notebook, "offset": offset})
            page = _structured(studio)
            items = page.get("items", [])
            candidate = pick_downloadable_artifact(items, backend=args.backend)
            if candidate or not page.get("has_more"):
                break
            if not items:
                print("  FAIL  studio_list reported more items but returned an empty page")
                return False
            offset += len(items)
        artifact_type = args.artifact_type or (candidate and candidate.get("type"))
        if not artifact_type:
            print(
                "  FAIL  no existing downloadable artifact (pass --artifact-type or generate one)"
            )
            return False

        download_args: dict[str, Any] = {
            "notebook": notebook,
            "artifact_type": artifact_type,
        }
        if candidate and candidate.get("id"):
            download_args["artifact_id"] = candidate["id"]
        download = await mcp.call_tool("studio_download", download_args)
        payload = _structured(download)
        if payload.get("status") != "download_ready" or not payload.get("url"):
            print(
                "  FAIL  studio_download did not return a download URL: "
                f"status={payload.get('status')!r}, keys={sorted(payload)}"
            )
            return False
        print(f"  PASS  minted signed download URL for {artifact_type}")
        response = await http.get(str(payload["url"]))
        if response.status_code != 200 or not response.content:
            print(
                f"  FAIL  download GET returned {response.status_code} "
                f"({len(response.content)} bytes)"
            )
            return False
        print(f"  PASS  downloaded {len(response.content)} bytes")
    return passed


def main(argv: list[str] | None = None) -> int:
    """Run the remote smoke and convert its result to a process exit code."""

    args = parse_args(argv)
    try:
        passed = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - top-level smoke reports one clean failure
        print(f"  FAIL  unexpected error: {exc}")
        passed = False
    print()
    print("RESULT: PASS" if passed else "RESULT: FAIL")
    return 0 if passed else 1
