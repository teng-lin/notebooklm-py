"""Phase 3 (dev-gated): an in-app MCP-App upload widget.

Renders an ``<input type=file>`` inline in Claude's sandboxed iframe so a mobile user can
pick a file and upload it **without leaving the chat** — the widget POSTs the bytes directly
to the existing ``/files/ul/<token>`` route (same broker, same completion map, same
``await_upload``). This is the Phase 3 direct-in-app path; the shipped link flow stays the
fallback.

**Only registered when ``NOTEBOOKLM_MCP_DEV_UI=1``** (and a public URL is configured), so it
never enters the prod manifest / tool-count. It is an experiment — revert if the picker or
render fails in Claude.

Rendering in claude.ai needs undocumented gates that FastMCP does not emit on its own but which
its ``meta=`` + ``app=`` plumbing lets us add (verified against
github.com/primevalsoup/mcp-apps-claude-demo, the #671 workaround):
  * the resource's ``_meta.ui.domain`` = ``sha256("<connector-url>/mcp")[:32] + .claudemcpcontent.com``
  * the FLAT ``_meta["ui/resourceUri"]`` on the tool (what claude.ai actually reads), beside the
    spec-nested ``_meta.ui.resourceUri``
  * mimeType ``text/html;profile=mcp-app`` (auto-stamped for ``ui://`` resources)
  * the widget itself sends ``ui/notifications/initialized`` unconditionally (client-side, below).
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Any

from fastmcp import Context
from fastmcp.apps import AppConfig, ResourceCSP

from ._confirm import READ_ONLY
from ._context import get_client, get_file_transfer
from ._errors import mcp_errors
from ._resolve import resolve_notebook

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ._filelink import FileTransferConfig

_WIDGET_URI = "ui://notebooklm/upload-v1"
_DEV_FLAG = "NOTEBOOKLM_MCP_DEV_UI"


def _widget_domain(base_url: str) -> str:
    """The claude.ai render gate: ``sha256("<base>/mcp")[:32] + .claudemcpcontent.com``."""
    endpoint = f"{base_url.rstrip('/')}/mcp"
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32] + ".claudemcpcontent.com"


#: The widget: demo-proven claude.ai handshake + a file picker that POSTs bytes to the
#: ``upload_url`` handed in via the tool result. Self-contained (no external assets).
_WIDGET_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<style>
 body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:14px;background:transparent;color:#1c2420}
 .card{border:1px solid #dde2da;border-radius:10px;padding:16px;max-width:520px;background:#fff}
 .head{font-size:14px;font-weight:650;color:#2f7d31}
 input[type=file]{display:block;margin:12px 0;font-size:15px}
 button{font-size:15px;padding:9px 16px;border-radius:8px;border:0;background:#2f7d31;color:#fff}
 button[disabled]{opacity:.5}
 #out{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;margin-top:12px;color:#4a564e}
 @media(prefers-color-scheme:dark){body{color:#e6eae4}.card{background:#1d231f;border-color:#313a33}#out{color:#b7c0b8}}
</style></head><body>
<div class="card">
 <div class="head">📎 Add a file to NotebookLM</div>
 <div id="sub" style="font-size:12px;color:#6b7a6e;margin-top:3px">starting…</div>
 <input id="f" type="file" disabled>
 <button id="up" disabled>Upload</button>
 <div id="out"></div>
</div>
<script type="module">
 const sub=document.getElementById('sub'),out=document.getElementById('out');
 const log=m=>{out.textContent+=(out.textContent?"\\n":"")+m;size();};
 const post=m=>{try{window.parent.postMessage(m,"*")}catch(e){}};
 let initialized=false, uploadUrl=null;
 function ready(h){if(initialized)return;initialized=true;sub.textContent=(h||"host")+" · ready";
   post({jsonrpc:"2.0",method:"ui/notifications/initialized",params:{}});}
 post({jsonrpc:"2.0",id:1,method:"ui/initialize",params:{capabilities:{},protocolVersion:"2026-01-26",
   clientInfo:{name:"nlm-upload",version:"1"},appCapabilities:{availableDisplayModes:["inline"]}}});
 setTimeout(()=>ready(null),500);
 function size(){post({jsonrpc:"2.0",method:"ui/notifications/size-changed",
   params:{height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth}});}
 function consider(p){ // tool result carries {structuredContent:{upload_url,...}} or content[].text
   let d=p&&p.structuredContent;
   if(!d&&p&&p.content)for(const c of p.content)if(c&&c.type==="text"){try{d=JSON.parse(c.text)}catch(e){}}
   if(d&&d.upload_url){uploadUrl=d.upload_url;document.getElementById('f').disabled=false;
     sub.textContent="pick a file to add"+(d.notebook?" to "+d.notebook:"");}
 }
 window.addEventListener("message",ev=>{let d=ev.data;if(d==null)return;
   if(typeof d==="string"){try{d=JSON.parse(d)}catch(e){return}}
   if(d.result&&!d.method){ready(d.result.hostInfo&&d.result.hostInfo.name);
     if(d.result.toolResult)consider(d.result.toolResult);return;}
   if(typeof d.method==="string"){if(d.method.includes("tool"))consider(d.params||{});
     else if(d.id!=null)post({jsonrpc:"2.0",id:d.id,result:{}});}});
 const fi=document.getElementById('f'),btn=document.getElementById('up');
 fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files[0]);});
 btn.addEventListener('click',async()=>{
   const file=fi.files&&fi.files[0]; if(!file||!uploadUrl){log("no file or no upload url yet");return;}
   btn.disabled=true;log("uploading "+file.name+" ("+file.size+" B)…");
   try{
     const res=await fetch(uploadUrl+"?filename="+encodeURIComponent(file.name),
       {method:"POST",headers:{"Accept":"application/json","Content-Type":file.type||"application/octet-stream"},body:file});
     const text=await res.text();
     log("["+res.status+"] "+text.slice(0,200));
     if(res.ok)sub.textContent="✅ added — you can close this and continue in chat";
   }catch(e){log("❌ upload failed (CSP/CORS/network): "+e);}
 });
</script></body></html>"""


def register_upload_widget(mcp: FastMCP, config: FileTransferConfig | None) -> None:
    """Dev-only: mount the in-app upload widget. No-op unless ``NOTEBOOKLM_MCP_DEV_UI=1`` and a
    file-transfer (public URL) config is present — so it never enters the prod manifest."""
    if os.environ.get(_DEV_FLAG) != "1" or config is None:
        return

    domain = _widget_domain(config.base_url)

    @mcp.resource(
        _WIDGET_URI,  # ui:// → mime auto text/html;profile=mcp-app
        app=AppConfig(
            domain=domain,  # → _meta.ui.domain (the claude.ai render gate)
            csp=ResourceCSP(connect_domains=[config.base_url.rstrip("/")]),  # widget → /files/ul
            prefers_border=True,
        ),
    )
    def _upload_widget_html() -> str:
        return _WIDGET_HTML

    @mcp.tool(
        annotations=READ_ONLY,
        meta={"ui/resourceUri": _WIDGET_URI},  # FLAT key claude.ai actually reads
        app=AppConfig(resource_uri=_WIDGET_URI, visibility=["model"]),
    )
    async def add_file_widget(ctx: Context, notebook: str) -> dict[str, Any]:
        """DEV: open an in-app file picker to add a file to a notebook (mobile upload widget)."""
        with mcp_errors():
            cfg = get_file_transfer(ctx)
            if cfg is None:
                return {"error": "file transfer not configured"}
            nb_id = await resolve_notebook(get_client(ctx), notebook)
            upload_url = cfg.upload_url(
                {"nb": nb_id}
            )  # direct /files/ul POST target for the widget
            # structuredContent is pushed into the widget by the host; it reads upload_url from here.
            return {"upload_url": upload_url, "notebook_id": nb_id, "notebook": notebook}
