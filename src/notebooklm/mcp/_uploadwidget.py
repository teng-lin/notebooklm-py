"""Experimental in-app MCP-App upload widget (opt-in).

Renders an ``<input type=file>`` inline in an MCP-Apps host's sandboxed iframe (e.g. claude.ai)
so a mobile user can pick a file and upload it **without leaving the chat** — the widget POSTs
the bytes directly to the existing ``/files/ul/<token>`` route (same broker, same completion
map, same ``await_upload``). The shipped signed-link flow stays the portable fallback.

**Opt-in: only registered when ``NOTEBOOKLM_MCP_UPLOAD_WIDGET=1``** (and the http transport has a
public URL), so it stays out of the default tool surface / tool-count. Experimental because
MCP-Apps rendering is new (Jan 2026), host-specific, and depends on the gates below which a host
can change.

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

from ._context import get_client, get_file_transfer
from ._errors import mcp_errors
from ._resolve import resolve_notebook

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ._filelink import FileTransferConfig

_WIDGET_URI = "ui://notebooklm/upload-v1"
#: Opt-in flag. Off by default — the MCP-Apps widget is experimental (renders only in
#: MCP-Apps hosts like claude.ai, needs the http transport + a public URL, and depends on
#: host-specific render gates that can shift), so it stays out of the default tool surface.
_WIDGET_FLAG = "NOTEBOOKLM_MCP_UPLOAD_WIDGET"


def _widget_domain(base_url: str) -> str:
    """The claude.ai render gate: ``sha256("<base>/mcp")[:32] + .claudemcpcontent.com``."""
    endpoint = f"{base_url.rstrip('/')}/mcp"
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32] + ".claudemcpcontent.com"


#: The widget: cross-host (claude.ai / ChatGPT / Grok / other MCP-Apps hosts) — reads the tool
#: result from either the postMessage bridge (claude.ai/Grok) or ``window.openai.toolOutput``
#: (ChatGPT), then a universal ``<input type=file>`` + direct-PUT of the bytes to ``upload_url``.
#: Feature-detects ``window.openai.uploadFile`` (OpenAI native upload) for the interop signal.
#: Self-contained (no external assets). "Build to the strict (claude.ai) target → renders everywhere."
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
 const oai=window.openai;               // ChatGPT/Grok inject this; claude.ai does not
 const hasNative=!!(oai&&typeof oai.uploadFile==="function");  // OpenAI native upload (interop signal)
 let initialized=false, uploadUrl=null;
 function ready(h){if(initialized)return;initialized=true;
   sub.textContent=(h||(oai?"ChatGPT":"host"))+" · ready"+(hasNative?" · native upload available":"");
   post({jsonrpc:"2.0",method:"ui/notifications/initialized",params:{}});}  // claude.ai render gate
 post({jsonrpc:"2.0",id:1,method:"ui/initialize",params:{capabilities:{},protocolVersion:"2026-01-26",
   clientInfo:{name:"nlm-upload",version:"1"},appCapabilities:{availableDisplayModes:["inline"]}}});
 setTimeout(()=>ready(oai?"ChatGPT":null),500);
 function size(){post({jsonrpc:"2.0",method:"ui/notifications/size-changed",
   params:{height:document.documentElement.scrollHeight,width:document.documentElement.scrollWidth}});}
 function consider(p){ // tool result: {structuredContent:{upload_url}} | {toolResult:…} | content[].text | raw obj
   if(!p)return; if(p.toolResult)p=p.toolResult; // unwrap the ui/notifications/tool-result envelope
   let d=p.structuredContent;
   // Gate fallbacks on upload_url, not truthiness: a structuredContent without upload_url must not
   // block the content[]/raw fallbacks, and a later text fragment must not overwrite a good result.
   if(!d?.upload_url&&Array.isArray(p.content))for(const c of p.content)if(c&&c.type==="text"){
     try{const parsed=JSON.parse(c.text);if(parsed?.upload_url)d=parsed}catch(e){}}
   if(!d?.upload_url&&p.upload_url)d=p;
   if(d&&d.upload_url&&!uploadUrl){uploadUrl=d.upload_url;document.getElementById('f').disabled=false;
     sub.textContent="pick a file to add"+(d.notebook?" to "+d.notebook:"");}
 }
 // claude.ai / Grok: tool result arrives via postMessage. We deliberately don't allowlist
 // ev.origin (host origin differs per platform — claude.ai / chatgpt.com / Grok): the only thing
 // a message can influence is uploadUrl, and (a) the resource CSP connect-src pins uploads to
 // config.base_url and (b) /files/ul requires a server-signed single-use token, so a spoofed URL
 // can't exfiltrate or add anything. CSP + signed token are the guard, not the frame origin.
 window.addEventListener("message",ev=>{let d=ev.data;if(d==null)return;
   if(typeof d==="string"){try{d=JSON.parse(d)}catch(e){return}}
   if(d.result&&!d.method){ready(d.result.hostInfo&&d.result.hostInfo.name);
     if(d.result.toolResult)consider(d.result.toolResult);return;}
   if(typeof d.method==="string"){if(d.method.includes("tool"))consider(d.params||{});
     else if(d.id!=null)post({jsonrpc:"2.0",id:d.id,result:{}});}});
 // ChatGPT: tool result arrives on window.openai.toolOutput (set at/after load)
 function pullOai(){if(oai&&oai.toolOutput)consider(oai.toolOutput);}
 window.addEventListener("openai:set_globals",pullOai);
 // ChatGPT fetches the template lazily on the FIRST call, so the iframe can attach AFTER the
 // one-shot ui/notifications/tool-result fires — toolOutput is the durable fallback. Poll it until
 // the upload_url lands (first render often sets it late) instead of a few fixed tries, else the
 // first widget of a chat renders but stays stuck with no upload target.
 let _pt=0;const _pi=setInterval(()=>{pullOai();if(uploadUrl||++_pt>66)clearInterval(_pi);},300);
 const fi=document.getElementById('f'),btn=document.getElementById('up');
 fi.addEventListener('change',()=>{btn.disabled=!(fi.files&&fi.files[0]);});
 btn.addEventListener('click',async()=>{
   const file=fi.files&&fi.files[0]; if(!file||!uploadUrl){log("no file or no upload url yet");return;}
   if(file.size>200*1024*1024){log("❌ file exceeds the 200 MB limit — pick a smaller file");return;} // mirrors server MAX_UPLOAD_BYTES
   btn.disabled=true;log("uploading "+file.name+" ("+file.size+" B)…");
   try{
     const res=await fetch(uploadUrl+"?filename="+encodeURIComponent(file.name),
       {method:"POST",headers:{"Accept":"application/json","Content-Type":file.type||"application/octet-stream"},body:file});
     const text=await res.text();
     log("["+res.status+"] "+text.slice(0,200));
     if(res.ok)sub.textContent="✅ added — you can close this and continue in chat";
     else btn.disabled=false; // non-2xx: token uncommitted → link stays retryable, let them click again
   }catch(e){log("❌ upload failed (CSP/CORS/network): "+e);btn.disabled=false;} // transient failure → retryable
 });
</script></body></html>"""


def register_upload_widget(mcp: FastMCP, config: FileTransferConfig | None) -> None:
    """Opt-in: mount the in-app upload widget. No-op unless ``NOTEBOOKLM_MCP_UPLOAD_WIDGET=1``
    and a file-transfer (public URL) config is present — so it stays out of the default tool
    surface (and off the tool-count / schema-char budgets) unless a deployment enables it."""
    if os.environ.get(_WIDGET_FLAG) != "1" or config is None:
        return

    domain = _widget_domain(config.base_url)
    base = config.base_url.rstrip("/")

    # ONE resource, the MCP-Apps standard mime ``text/html;profile=mcp-app`` — which both hosts now
    # accept (per developers.openai.com/apps-sdk; ``openai/*`` keys are backward-compat extensions).
    # A second ``text/html+skybridge`` resource does NOT work: claude.ai FOLLOWS the tool's
    # ``openai/outputTemplate`` too, and can't render the skybridge mime → "fail to fetch app
    # content". So both meta pointers below target this single resource.
    @mcp.resource(
        _WIDGET_URI,  # ui:// → mime auto text/html;profile=mcp-app
        meta={  # ChatGPT reads openai/widgetCSP; harmless to claude.ai (which reads ui.csp via app=)
            "openai/widgetCSP": {"connect_domains": [base], "resource_domains": []}
        },
        app=AppConfig(
            domain=domain,  # → _meta.ui.domain (the claude.ai render gate)
            csp=ResourceCSP(connect_domains=[base]),  # widget → /files/ul
            prefers_border=True,
        ),
    )
    def _upload_widget_html() -> str:
        return _WIDGET_HTML

    @mcp.tool(
        # NOT read-only: it mints an upload_url that the /files/ul route accepts to ADD a source
        # (capability creation). A readOnlyHint would let hosts auto-invoke it without the consent
        # a mutation warrants — leave it unannotated.
        # claude.ai reads ui/resourceUri (flat) + ui.resourceUri (nested via app=); ChatGPT reads
        # openai/outputTemplate. All three point at the ONE mcp-app resource → renders on both.
        meta={"ui/resourceUri": _WIDGET_URI, "openai/outputTemplate": _WIDGET_URI},
        app=AppConfig(resource_uri=_WIDGET_URI, visibility=["model"]),
    )
    async def source_add_widget(ctx: Context, notebook: str) -> dict[str, Any]:
        """Open an in-app file picker to add a file to a notebook (experimental mobile upload
        widget). Renders inline in MCP-Apps hosts (e.g. claude.ai); the user picks a file and the
        widget uploads it directly. Call ``await_upload`` with the returned ``upload_url`` to
        confirm the add landed."""
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
