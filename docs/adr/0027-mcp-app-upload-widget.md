# ADR-0027: In-app MCP-App upload widget (opt-in)

## Status

Accepted (experimental / opt-in).

## Context

ADR-0024 gives remote MCP clients a mobile file-upload path via a signed `/files/ul/<token>`
link the user opens in a browser. It works and is live-validated, but the link itself is a
~250-char opaque token that is fragile through a mobile chat (the short-link indirection
mitigated that). The nicer UX is uploading **without leaving the chat**.

"MCP Apps" (SEP-1865, shipped in claude.ai on 2026-01-26) lets a tool declare a `ui://` HTML
resource that the host renders in a sandboxed iframe with a JSON-RPC-over-postMessage bridge.
That makes an in-app `<input type=file>` possible. Open questions the spike had to settle
on-device: does claude.ai actually render a widget from a plain connector; does the file picker
fire inside the sandbox; can the widget upload cross-origin.

## Decision

Ship an **opt-in** in-app upload widget (`NOTEBOOKLM_MCP_UPLOAD_WIDGET=1`, off by default),
built on the existing ADR-0024 machinery — the widget POSTs bytes to the same `/files/ul` route,
reusing the broker, the in-process completion map, and `await_upload`. No new upload transport.

Rendering in claude.ai needs gates the MCP-Apps spec leaves optional/implicit and FastMCP does
not emit on its own; we add them via FastMCP's `meta=` + `app=` plumbing (verified against the
`primevalsoup/mcp-apps-claude-demo` write-up of ext-apps#671):

- the resource's `_meta.ui.domain = sha256("<public-url>/mcp")[:32] + ".claudemcpcontent.com"`
  (self-computed from the configured public URL — not a host-issued credential);
- the **flat** `_meta["ui/resourceUri"]` on the tool, beside the spec-nested `ui.resourceUri`
  (claude.ai reads the flat one);
- mimeType `text/html;profile=mcp-app` (auto-stamped for `ui://` resources);
- the widget itself sends `ui/notifications/initialized` unconditionally, or the iframe stays
  hidden.

The widget's cross-origin POST needs CORS on `/files/ul`: an `OPTIONS` preflight handler plus
`Access-Control-Allow-Origin: *`. `*` is safe here because the signed single-use token is the
sole auth — no cookies/ambient credentials (ADR-0024), so a page that cannot mint a token gains
nothing.

**Opt-in, not default**, because: MCP-Apps is new and host-specific; the render gates are
undocumented and can shift; `ui.domain` is deployment-specific; and it only works on the http
transport with a public URL. So it stays off the default tool surface (and the ADR-0025
tool-count / schema-char budgets) unless a deployment enables it.

## Consequences

- Live-validated end-to-end on Claude Android: widget renders, picker fires in the sandbox, file
  readable, upload lands, source added.
- The `ui.domain` gate is computed per-process from the public URL; a deployment behind a
  different URL than configured will not render (fails closed, silently — the link flow remains).
- CORS on `/files/ul` is now permissive-origin; acceptable given token-only auth.
- Follow-ups (not in the first cut): a progress bar, the fallback ladder
  (`window.openai.uploadFile` → direct-PUT → link), and auto-wiring `await_upload` so the model
  confirms the add without a second prompt.
- **Requires stateless HTTP.** An MCP-Apps host reads the `ui://` widget resource on a connection
  without the chat `Mcp-Session-Id`; a stateful FastMCP server rejects that ("Missing session ID"
  → "fail to fetch app content"). Enabling the widget therefore auto-enables
  `FASTMCP_STATELESS_HTTP` (overridable). Stateless is safe here — every tool is request/response,
  with no server-push/subscription state.
- One host resource, the MCP-Apps standard mime `text/html;profile=mcp-app`, serves both claude.ai
  and ChatGPT (a `text/html+skybridge` variant is unnecessary; OpenAI's SDK accepts the standard
  mime). ChatGPT caches the template per conversation, so the first call in a new chat may not
  render (call again) — a client-side quirk, not fixable server-side.
- If a host changes its render requirements, the widget silently stops rendering; the signed-link
  flow (ADR-0024) is the durable fallback and stays the default.
