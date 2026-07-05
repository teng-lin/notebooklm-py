# ADR-0024: MCP remote file transfer (signed-URL side-channel)

## Status

Proposed.

## Context

The MCP server now runs over a remote HTTP transport for the claude.ai connector
(ADR-precedent: #1645 remote transport, #1647 self-hosted OAuth). Two tools still
assume the **stdio** model where the server's filesystem *is* the user's
filesystem:

- `source_add` with `source_type="file"` takes `path` — a path **on the server
  host** (`src/notebooklm/mcp/tools/sources.py:160`).
- `artifact_download` takes `path` — the output file **on the server host**
  (`src/notebooklm/mcp/tools/artifacts.py:344`).

Over the claude.ai connector the server is in a container behind a
Cloudflare/Tailscale tunnel. A path argument the user supplies refers to a
filesystem they cannot see, and a file the server writes lands somewhere they
cannot reach. So "upload a local PDF" and "download my podcast" are both broken
on the remote connector even though they work over stdio.

MCP itself offers no help here:

- **No upload affordance.** claude.ai does not stream a user-selected local file
  into a tool call. Tool arguments are JSON; base64-in-a-string has no client UI,
  is not wired to chat-attached files, and dies on request-size limits. (#1803 later
  added `source_upload_bytes` for the narrow case where an agent already *holds* the
  bytes and neither the browser nor the `agent_upload` POST can move them: it passes
  ≤10,000 chars of base64 in-channel — ≈7 KB — and the connector adds it server-side.
  The request-size limit is exactly why that cap is so small; anything larger still
  takes the signed-URL flow this ADR defines.)
- **No usable download affordance.** A tool *result* can carry base64
  image/audio/`EmbeddedResource`, but a podcast/video is tens of MB; base64'd
  through JSON-RPC it blows the channel and claude.ai will not materialize it as a
  saved file.

So binaries must travel **outside** the MCP JSON-RPC channel.

The user confirmed local-binary **upload** (e.g. a PDF on their laptop) is in
scope, not just URL/YouTube/Drive sources. So both directions need a real
byte-transfer path.

## Prior art & ecosystem alignment (2026)

Before designing this we checked whether the MCP ecosystem has standardized file
transfer (research with citations; key sources below). Findings:

- **Upload has NO native MCP primitive.** The official File Uploads Working Group
  charter (2026-04-23, Anthropic) states servers today "resort to prose
  instructions asking for base64 strings or local paths"; the proposed
  declarative-file-input descriptor (**SEP-2356**) is **Draft**, not shippable,
  and the charter lists **presigned upload URLs** as a candidate approach. So the
  signed-URL side-channel is the **current accepted best practice**, not a
  reinvention. The industry convention (FutureSearch, Tigris, LibreChat) is
  exactly: a tool returns a **presigned/HMAC-signed URL** (short TTL, size +
  content-type enforced, single-use), the bytes move **out-of-band**, and only a
  lightweight reference rides the protocol — "the context window is for control
  messages, not bulk data."
- **Download HAS a native primitive** — Resources + `BlobResourceContents`, and
  claude.ai connectors do support binary resources — **but** custom-connector tool
  results are capped at **~150 000 characters** (~110 KB binary after base64).
  Every NotebookLM artifact (podcast, video, slide deck, PDF) is far larger, so
  the native path is unusable for our payloads and a **signed download URL is the
  right call**. We return it as a `resource_link` so claude.ai renders it clickable.

How this design aligns:

- The upload endpoint accepts a **raw body over POST *or* PUT**, so it serves both
  delivery paths the ecosystem uses: a human opening the link and uploading via
  the browser (`fetch` POST), **and** Claude's **code-execution sandbox** `curl`-ing
  a file it already holds to the presigned URL (the FutureSearch pattern). One
  handler, both work — no extra code.
- **Operational constraint (must document):** the sandbox-PUT path only works if
  the user has **Code Execution enabled and the server domain whitelisted**
  (claude.ai Settings → Capabilities → additional allowed domains); otherwise the
  PUT fails despite a valid signature. The browser-upload path has no such
  requirement and is the universal fallback.
- **Migration note (forward path = SEP-2631).** The standardization most likely to
  land is **SEP-2631 "File Objects and Transfer"** (Draft, opened 2026-04-22; extends
  SEP-2356) — and it *standardizes this very side-channel*: protocol-native
  `files/authorizeUpload` / `files/authorizeDownload` control-plane methods that hand
  the client a presigned **out-of-band** HTTPS URL (bytes stay out of JSON-RPC), plus
  `x-mcp-file` URI-string inputs and a `FileValue` output type (uri / displayName /
  mimeType / size / digest). So this ADR is **forward-compatible, not a stopgap to be
  thrown away**: the existing signed-URL endpoints (`_filelink` / `_fileroutes`) become
  the presigned targets the client negotiates, and migration mainly moves the UX into
  the client's **native file picker** — removing the manual browser round-trip that is
  today's upload friction. Blocked on SEP-2631 landing **and** FastMCP + claude.ai
  implementing it; until all three, the side-channel is both the shippable approach and
  the pattern the spec is converging on. (SEP-2356's declarative-file-input alone, being
  base64-leaning, was never sufficient for our payload sizes.) Tracked in #1656.

Sources: WG charter https://modelcontextprotocol.io/community/file-uploads/charter
· SEP-2356 https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2356
· SEP-2631 (File Objects and Transfer) https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2631
· claude.ai connector guide https://claude.com/docs/connectors/building
· FutureSearch upload pattern https://futuresearch.ai/blog/mcp-large-dataset-upload/
· Tigris signed-download https://www.tigrisdata.com/blog/mcp-server-sharing/

## Decision

Add a **signed-URL HTTP side-channel mounted on the same FastMCP http app**. The
MCP tools broker short-lived signed URLs; the user's browser does the actual byte
transfer directly against the tunnel. No bytes cross MCP/claude.ai.

Two custom routes on the FastMCP app (verified: `FastMCP.custom_route(path,
methods=[...])` exists in fastmcp 3.2.0, takes a Starlette `Request -> Response`):

```
GET      /files/dl/{token}   -> stream the artifact   (Starlette FileResponse)
GET      /files/ul/{token}   -> minimal upload page (file picker + fetch POST)
POST|PUT /files/ul/{token}   -> stream raw body -> add source
```
(`POST` serves the browser `fetch`; `PUT` serves a sandbox `curl` — same handler.)

**Download flow** (`artifact_download`, http transport):
1. Tool returns the signed link as a `resource_link` content item (claude.ai
   renders it clickable) plus `{ "status": "download_ready",
   "url": "<base>/files/dl/<token>", "expires_at": … }` — instead of writing to a
   server path. (A native `BlobResourceContents` is not used: the ~150 KB connector
   cap rules it out for real artifacts.)
2. Browser GET → handler verifies the token, runs the existing download core into
   a private temp dir, returns `FileResponse` with a `BackgroundTask` cleanup
   (mirrors `server/routes/artifacts.py:327`).

**Upload flow** (`source_add type=file`, http transport):
1. Tool returns `{ "status": "upload_required", "url": "<base>/files/ul/<token>",
   "expires_at": … }` instead of reading a server path.
2. Browser GET → one-screen HTML page with a file picker.
3. The page uploads the file as a **raw request body** via
   `fetch(url + "?filename=" + encodeURIComponent(file.name),
   {method:"POST", headers:{"Content-Type": file.type || "application/octet-stream"},
   body: file})` — **not** a multipart form. A raw body omits the filename, and
   NotebookLM's upload **requires** the real basename+extension (an extensionless
   name 400s, `_source/upload.py:411`), so the page passes the browser-selected
   `file.name` as a query param and the type as `Content-Type`. The handler reads
   `request.stream()` chunk-by-chunk into a `0o600` temp file (named from the
   **sanitized** `?filename`) with a **running byte cap** (the real DoS defense),
   after an early `Content-Length` reject. It then runs the neutral `source_add`
   core with `source_type="file"`, deletes the temp in a `finally` (incl.
   mid-stream disconnect), and returns an HTML "added (id=…)" page.
4. The agent confirms with `source_wait` / `source_list`.

The raw-body upload (vs. `<form enctype=multipart/form-data>` /
`request.form()`) is deliberate: it avoids the `python-multipart` dependency
(present only in the `server` extra, **not** `mcp`) and avoids Starlette spooling
the entire multipart body to disk *before* the per-chunk size check can run — the
disk-exhaustion hole that the REST route only closes with app-level
Content-Length middleware the FastMCP custom routes do not inherit. The
agent-supplied `title` / `mime` are carried in the **upload token** (signed, not
user-tamperable); the **filename** (which the token cannot know — it is minted
before the user picks a file) arrives as the sanitized `?filename=` query param
from the browser/sandbox and seeds the temp file's basename+extension.

### Stateless, token-encoded — no registry, one scoped inline sweeper (`ul`)

The signed token **encodes the operation parameters**, so the handlers hold no
server-side state:

- download token payload: `{op:"dl", nb, atype, fmt?, aid?, exp, jti}`
- upload token payload:   `{op:"ul", nb, title?, mime?, exp, jti}`

Carrying `title`/`mime` in the upload token preserves the current
`source_add type=file` parameters across the browser round-trip — and because
they are signed, the uploader cannot tamper with them.

Token = `base64url(json(payload)) + "." + base64url(HMAC-SHA256(key, body))`
(stdlib `hmac`/`hashlib`/`base64`/`json`/`secrets` — no new dependency;
`itsdangerous` is not installed). Verify enforces a max token length **before** any
decode/HMAC work, re-pads base64url, recomputes the MAC in constant time
(`hmac.compare_digest`), and checks `exp`.

There is **no ref-registry and no *background* sweeper**. The one piece of state
is the `ul` single-use tracker added by #1746 (see *Residual risk* below): an
ephemeral, bounded, **inline-swept** in-process set of consumed `jti`s that dies
with the process just like the signing key. `dl` remains fully stateless
(multi-use within its TTL). For downloads the token is still the state.

### Auth model (verified against fastmcp 3.2.0)

The browser opening a signed URL **cannot** carry the MCP bearer/OAuth
credential, so the side-channel must authenticate itself. This is exactly how
FastMCP mounts routes (`fastmcp/server/http.py`):

- `auth.get_middleware()` is added as **global** middleware — it *authenticates*
  (populates request scopes) but does **not** reject unauthenticated requests
  (`BearerAuthBackend.authenticate` returns `None`, it does not raise; `MultiAuth`
  / `McpBearerAuthProvider` inherit this non-rejecting base).
- `RequireAuthMiddleware` wraps **only** the MCP route (streamable-http transport:
  `http.py:333-343`).
- Custom routes (streamable-http block: `http.py:355-357`) are appended unwrapped
  and reached **without** the bearer gate.

Therefore the **HMAC signed token is the sole, sufficient auth** for `/files/*`,
which is correct (the browser has no bearer). A regression test pins this
FastMCP behavior so an upgrade that starts gating custom routes fails loudly.

### Signing key and public base URL

- **Key:** ephemeral `secrets.token_bytes(32)` generated at server start. Tokens
  are short-TTL (upload **15 min**, download **30 min**); a restart invalidating
  outstanding links is acceptable and removes a secret to manage. No config. The
  token rides in the URL **path**, so it is captured by tunnel access logs
  (Cloudflare/Tailscale), browser history, and any `Referer` — the short TTL +
  `no-referrer` bound the window, and the blast radius is the single tenant's own
  account (a one-shot download token would be needed for multi-tenant; the
  stateless design wins here).
  The payload is **signed, not encrypted**: a leaked URL exposes the base64-decoded
  notebook id / artifact type / title to whoever reads the log. Accepted — these are
  the single tenant's own low-sensitivity metadata, and the HMAC's job is to prevent
  *forgery*, not disclosure (encrypting would require opaque server-side state,
  defeating the stateless design). `PUBLIC_URL` rejects userinfo (`user:pass@host`);
  it intentionally does NOT reject private/loopback hosts (operator-set config that
  mirrors the OAuth base-URL validator — a private value just yields a link the
  operator's own browser can't reach, not an exfiltration vector).
- **Base URL:** `NOTEBOOKLM_MCP_PUBLIC_URL`, falling back to
  `NOTEBOOKLM_MCP_OAUTH_BASE_URL` (the public https tunnel URL the OAuth flow
  already requires). Either value is validated as a **bare https origin** by the
  same check `_oauth.py` already applies to the OAuth base URL (https scheme, no
  path/query/fragment) — extracted into a shared `_validate_bare_https_origin`
  helper so a `/mcp`-suffixed or non-https value can't produce broken/unsafe links.

**No startup crash.** File transfer is an *optional* capability. A bearer-only
remote deployment with no public URL set is still valid (chat/etc. work) — the
server does **not** `SystemExit`. The config is simply absent, and the two file
tools fail clean **at call time** with "remote file transfer is not configured;
set NOTEBOOKLM_MCP_PUBLIC_URL". (An earlier draft proposed a startup `SystemExit`;
that would break bearer-only remote servers that never use file transfer — both
reviews flagged it, and it is rejected.)

### Transport branch

`create_server` gains an optional file-transfer config (signer + validated public
base URL), built only in the `__main__.py` http branch and carried on `AppState`.
When present the two tools emit URLs; when absent (stdio, or http without a public
URL) they keep / fall back to the existing path-based behavior **unchanged** on
stdio, or the clean "not configured" error on http. stdio is untouched.

### Reaching the live client from a custom route

A `custom_route` handler receives a Starlette `Request`, not an MCP `Context`, so
it cannot use the tools' `get_client(ctx)`. The single process-wide client is
reached via `request.app.state.fastmcp_server` (FastMCP sets this on the Starlette
app) → `._lifespan_result` (the `AppState` yielded by the lifespan), guarded by
`._lifespan_result_set`. `_lifespan_result` is a FastMCP private; a regression
test pins both this access path and the no-bearer reachability so a FastMCP
upgrade that changes either fails loudly. Handlers take `(request)` only and read
`request.path_params["token"]` (a `(request, token)` signature crashes Starlette's
`request_response`).

## Consequences

**Positive**
- Local-binary upload and artifact download work over the claude.ai connector.
- Reuses the proven cores: `_app.download` + `download_core.execute_download`, the
  `source_add` file core + `validate_upload_path`, the 200 MiB cap, the temp-spool
  + `BackgroundTask` cleanup patterns from `server/routes/*`.
- No new dependency; no persistent state; no background sweeper (the #1746 `ul`
  single-use tracker is an ephemeral, inline-swept in-process set — no background task).
- stdio behavior unchanged.

**Negative / risks**
- Depends on FastMCP not force-gating custom routes (pinned by a regression test).
- A leaked upload URL within its 15-min TTL lets someone add a source **to the
  single tenant's own notebook**; a leaked download URL within 30 min streams one
  artifact. Single-tenant blast radius, short TTL. **Update (#1746):** `ul` replay-burn
  is **no longer skipped** — upload tokens are now single-use (one *successful* add per
  token), because a leaked `ul` link is a content-agnostic write/injection primitive
  (see *Residual risk* below). `dl` replay-burn **is** still deliberately skipped
  (multi-use within its 30-min TTL — Range/resume needs it). Note the tension:
  shortening `UPLOAD_TTL` to bound a leak also shrinks the retry window — a 200 MiB
  upload over a poor link can take ~13 min, near the 15-min TTL — so `ul` single-use
  is recorded **on success only**, leaving a failed-then-retried upload able to reuse
  the same link. Don't shorten `ul` without weighing that.
  A leaked upload token's *concurrent* replay is now also bounded by the atomic jti
  claim (`try_begin`): a second concurrent POST of the same token is rejected before it
  spools, so the pre-#1746 N×200 MiB concurrent-spool window collapses to the narrow
  race of two POSTs that both pass the claim before either commits — itself capped by
  `_MAX_CONCURRENT_UPLOADS` and self-cleaning (`finally: _cleanup`).
- Two new internet-facing routes on the tunnel — covered by: a streamed byte cap
  (real defense) plus a `Content-Length` early-reject (413); `title`/`mime` carried
  in the signed token (not user-tamperable) and the `?filename` basename-sanitized;
  the reused path-traversal guard +
  served-path-inside-tempdir assertion; token binding to op+notebook with a
  pre-decode length cap. The signed-token is itself in the URL path, so the HTML
  pages send `Referrer-Policy: no-referrer` + `Cache-Control: no-store`, set
  `X-Frame-Options: DENY` / a restrictive CSP, and HTML-escape all interpolated
  values; the short TTL bounds the value of a token that leaks via history/logs.
- The upload page is a tiny static HTML page (file picker + a `fetch()` POST); not
  a polished UI.

**Rejected alternatives**
- *Bytes through MCP results (base64).* Size limits + claude.ai won't save files.
- *Stateful upload broker with a ref registry + TTL sweep.* Unneeded once the
  token encodes params; more code and a background task for no benefit.
- *Reuse the FastAPI `server/` extra routes directly.* Different deployment
  (separate ASGI app, FastAPI `Depends`); the side-channel belongs on the MCP app.
  Shared *logic* is reused; the FastAPI plumbing is not.

### Residual risk: signed-token replay within TTL (`ul` now single-use — #1746; `dl` accepted)

`FileLinkSigner.verify` (`_filelink.py`) checks the length cap, the HMAC, `exp`, and
the `op` claim; a token that passes those checks passes them **every time** until it
expires. Leakage is plausible, not theoretical: the token rides in the URL **path**,
so it is captured by claude.ai, browser history, tunnel access logs
(Cloudflare/Tailscale), and any `Referer`. The `no-referrer` / `no-store` headers
narrow, but do not eliminate, that surface. The two ops differ in blast radius:

- A leaked **`ul`** token is a **content-agnostic write primitive**: its payload is
  only `{nb, title?, mime?}` and the uploaded bytes are the raw request body, so
  whoever holds the link can POST **arbitrary content** (≤200 MiB) as a *source* into
  the owner's notebook — a content / prompt-injection vector, not merely a same-file
  replay.
- A leaked **`dl`** token can **re-exfiltrate** the current latest artifact of that
  type until it expires (`DOWNLOAD_TTL` = 30 min). The token pins
  `{nb, atype, fmt?, aid?}`, not a byte snapshot, so a replay serves whatever the
  download core resolves at replay time.

**Decision (updated by #1746): `ul` is single-use; `dl` stays multi-use.**

`ul` — **single-use enforced.** Every token now carries a random `jti` (128-bit
`secrets`, injected by `sign()` and covered by the MAC). The `/files/ul` POST route
atomically **claims** the jti (`ConsumedJtiStore.try_begin`) before spooling and
**commits** (burns) it only after a *successful* `source_add`; a failed / aborted /
429'd upload **rolls back** the claim from the route's `finally` so the link can be
retried. Consequences:

- A sequential replay (the realistic leaked-from-logs case) is rejected with a flat
  403 before any spool, once the token's one successful use has happened.
- The atomic claim also rejects a *concurrent* duplicate POST before it spools,
  collapsing the pre-#1746 N×200 MiB concurrent-spool window to the narrow race of two
  POSTs that both pass the claim before either commits (bounded by
  `_MAX_CONCURRENT_UPLOADS`).
- Recording **on success only** preserves the large-file retry window this ADR
  protects (a 200 MiB upload over a poor link can take ~13 min): a failed upload does
  not burn the link.

`dl` — **replay-within-TTL accepted (multi-use).** The `jti` is present but **not**
enforced for downloads, because `GET /files/dl/{token}` is streamed directly and a
`Range`/resumable client legitimately re-issues the GET (a reconnect with `Range:`)
to resume a dropped stream — single-use would 403 the resume and break large-artifact
downloads. `dl` is also lower severity (re-reads the *same* artifact; not a write
primitive) and is already bounded by:

- **Short TTL** — 30 min caps the replay window.
- **Ephemeral per-process signing key** — `secrets.token_bytes(32)` minted at server
  start invalidates every outstanding token on restart.
- **Single-tenant scope** — blast radius is the operator's own account; no cross-tenant
  escalation.
- **HMAC integrity** — a token cannot be forged or tampered with; replay needs a
  *legitimately issued* token.
- **Download concurrency cap** — `_MAX_CONCURRENT_DOWNLOADS = 4` (#1681) bounds
  concurrent in-flight downloads and holds each slot for the artifact's whole temp-disk
  lifetime, released from a `finally` at end-of-stream (a `FileResponse` subclass), so
  a replay cannot fan out unbounded fetches and a disconnect/aborted `Range` cannot
  leak a slot.

**Why the `ul` reversal is consistent with the stateless design.** The two original
objections to a `jti` seen-set are answered, not ignored: the store is **ephemeral,
bounded (8192), and inline-swept** — it dies with the process exactly like the signing
key, and its non-durability across a restart is moot because the key rotation already
invalidates every token on restart. There is still **no ref-registry and no background
sweeper**; `dl` remains fully stateless.

**What triggered the reversal, and what would change it further.** The original ADR
enumerated three conditions that would flip the tradeoff — multi-tenant, TTLs
lengthened, or a reported replay incident. #1746 is **none of those**; it is an added
**fourth** trigger: the 2026-07-02 multi-model MCP gap review **re-weighted the `ul`
severity** — reframing a leaked upload token as a *write / content-injection
primitive* rather than a same-file re-read — which raised it above the "re-exfiltrate
low-sensitivity metadata" framing the original decision weighed. The three original
conditions still stand as reasons to revisit **`dl`** single-use and/or shorten the
TTLs; none has fired for `dl`, and `dl`'s Range/resume constraint is the standing
reason it stays multi-use.

### Why the REST server is out of scope

The REST server (`server/` extra) **already** supports binary file transfer
natively: `POST /v1/notebooks/{id}/sources/file` (multipart upload,
`server/routes/sources.py:178`) and `POST /v1/notebooks/{id}/artifacts/download`
(`FileResponse`, `server/routes/artifacts.py:327`, documented at
`installation.md:414`). It needs **no** signed-URL side-channel because a REST
client is a programmatic HTTP client that carries the bearer token and streams
bytes directly — it has neither of the two constraints that force this design
(the claude.ai connector's browser cannot carry the MCP credential into a tool
call, and the JSON-RPC channel cannot carry bytes). This ADR is therefore
**MCP-only** by design, not by omission.
