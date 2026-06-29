# MCP server guide

> **Experimental / preview.** The MCP server ships behind the optional `mcp` extra. Its
> tool surface (names, parameters, output shapes) is **not** covered by the library's semver
> guarantees and may change between releases. `pip install notebooklm-py` is unaffected — the
> server and its dependencies only arrive with the `mcp` extra.

The MCP server exposes NotebookLM to any [Model Context Protocol](https://modelcontextprotocol.io)
client (Claude Desktop, Claude Code, Cursor, Windsurf, …) as a set of **25 tools** — manage
notebooks and sources, chat over a notebook's sources, generate and download studio artifacts,
and run deep research. It is a thin adapter over the same business logic the CLI uses, so it
behaves identically to `notebooklm <command>`.

## Install

The server is behind the `mcp` extra (pulls in `fastmcp`):

```bash
pip install "notebooklm-py[mcp]"
# or run with no install, straight from PyPI:
uvx --from "notebooklm-py[mcp]" notebooklm-mcp --help
```

## Authenticate (once)

The server reuses the CLI's stored credentials — it does **not** log in on its own. Authenticate
once before starting it:

```bash
notebooklm login
# or, if you didn't install the package:
uvx --from "notebooklm-py[mcp]" notebooklm login
```

Credentials are stored per profile under `~/.notebooklm/`. The server binds the **active profile**
at startup (override with `--profile`, below). See [configuration.md](configuration.md) for profiles
and multi-account setup.

## Connect a client

The fastest path is the auto-config command, which writes the server block into a client's MCP
config (idempotent, never clobbers other servers):

```bash
notebooklm mcp install claude-desktop   # or: claude-code | cursor | windsurf
```

| Client | Config written |
|--------|----------------|
| `claude-desktop` | `claude_desktop_config.json` (per-OS location) |
| `claude-code` | `~/.claude.json` (user scope) |
| `cursor` | `~/.cursor/mcp.json` |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` |

It writes a block that launches the server via `uvx` (so only `uv` needs to be on the host):

```jsonc
{
  "mcpServers": {
    "notebooklm": {
      "command": "uvx",
      "args": ["--from", "notebooklm-py[mcp]", "notebooklm-mcp"]
    }
  }
}
```

Restart the client after installing. For a one-click Claude Desktop bundle, see
[`desktop-extension/README.md`](../desktop-extension/README.md).

## Run it directly

The console script is `notebooklm-mcp`:

```bash
notebooklm-mcp                         # stdio transport (default — for desktop hosts)
notebooklm-mcp --profile work          # bind a specific auth profile
notebooklm-mcp --transport http        # loopback streamable-HTTP on 127.0.0.1:9420
notebooklm-mcp --transport http --port 9000
```

| Flag | Default | Notes |
|------|---------|-------|
| `--profile` | active profile | which stored auth profile the process binds |
| `--transport` | `stdio` | `stdio` (subprocess hosts) or `http` (loopback) |
| `--host` | `127.0.0.1` | http only; non-loopback is **refused** unless `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1` |
| `--port` | `9420` | http only |
| `--log-level` | `INFO` | logs go to **stderr**; stdout stays pure JSON-RPC |

There is no `--token` flag — the HTTP bearer token is **env-only**
(`NOTEBOOKLM_MCP_TOKEN`) so it cannot leak via `ps aux`.

`stdio` is right for Claude Desktop/Code, Cursor, and Windsurf (they launch the server as a
subprocess). Use `http` for a local web client or to share one running server across clients on
the same machine. The HTTP transport is loopback-only by default; binding to a non-loopback
address requires **both** the explicit `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1` override **and** a
`NOTEBOOKLM_MCP_TOKEN` — the server fails closed (refuses to start) on a network bind without a
token, since it fronts a full Google account.

## Remote deployment (Docker + a tunnel)

Because master-token auth keeps the session alive unattended (no browser), the HTTP transport can
run as a **remote connector** reachable from Claude Code, Claude Desktop, claude.ai, and mobile.
The [`deploy/`](../deploy/) directory ships a turn-key Docker + Compose stack with a **tunnel
sidecar** — pick one via a Compose profile — so you get HTTPS with **no public IP, no open ports,
and no TLS certificate to manage** (the tunnel terminates TLS at its edge).

**Common setup (both tunnels):**
```bash
# 1. bootstrap the master token once (a machine with a browser):
notebooklm login --master-token --account you@example.com      # writes ~/.notebooklm/profiles/default
# 2. secrets:
cp deploy/.env.example deploy/.env                              # edit per the steps below
#    NOTEBOOKLM_PROFILE_DIR defaults to ~/.notebooklm/profiles/default (override for a throwaway profile)
```

**Two auth methods coexist on one `/mcp`** (FastMCP `MultiAuth`):
- **Claude Code / Desktop** → the static `NOTEBOOKLM_MCP_TOKEN` bearer (an `Authorization` header).
- **claude.ai (web/mobile)** → optional **self-hosted OAuth** (one password, no external IdP):
  set `NOTEBOOKLM_MCP_OAUTH_PASSWORD` (≥16 random chars) + `NOTEBOOKLM_MCP_OAUTH_BASE_URL`
  (the **bare public origin**, no `/mcp`). Unset → bearer-only.

### Tunnel A — Cloudflare (needs a domain in your Cloudflare account)
1. Cloudflare **Zero Trust → Networks → Tunnels**: create a tunnel; copy its token to
   `CF_TUNNEL_TOKEN` in `.env`.
2. Add a **Public Hostname** (e.g. `notebooklm.yourdomain.com`) → **Service**
   `http://notebooklm-mcp:9420` — the **docker service name**, not `localhost`; route the **whole
   host** (`/`), not a `/mcp`-scoped ingress (the OAuth routes live at the root). Cloudflare
   auto-creates the proxied DNS record and serves a valid cert.
3. `.env`: `NOTEBOOKLM_MCP_OAUTH_BASE_URL=https://notebooklm.yourdomain.com` (bare origin).
4. Run: `cd deploy && make dev` (Cloudflare is the default profile).

### Tunnel B — Tailscale Funnel (no domain — free, stable `*.ts.net` HTTPS)
Best when you don't own a domain: Tailscale Funnel gives a stable public HTTPS hostname on
Tailscale's domain, free on the personal plan, no DNS to manage. **One-time tailnet setup in
the admin console** (these are policy/feature prerequisites, not per-machine toggles):
1. Enable **MagicDNS** and **HTTPS certificates** (admin console → DNS; → HTTPS Certificates).
2. Grant the **`funnel` node attribute**: admin console → **Settings → General → Funnel →
   Manage → Node attributes → Add node attribute** → `funnel` (JSON preview:
   `{"target": ["*"], "attr": ["funnel"]}`).
3. Create a **normal auth key** (Settings → Keys) → `.env` `TS_AUTHKEY` (there's no
   "Funnel-capable" key type; Funnel comes from the policy in step 2).

Then:
4. `.env`: `NOTEBOOKLM_MCP_OAUTH_BASE_URL=https://notebooklm-mcp.<your-tailnet>.ts.net` (bare origin).
   Find `<your-tailnet>` on the admin console **DNS** page (the **"Tailnet name"**, e.g.
   `tailXXXXXX.ts.net`).
5. Run: `cd deploy && make dev TUNNEL=tailscale`. The sidecar (`deploy/tailscale/funnel.json` via
   `TS_SERVE_CONFIG`, mounted as a directory) funnels public `:443 /` → `notebooklm-mcp:9420`;
   the node is `TS_HOSTNAME=notebooklm-mcp`, so the origin is `https://notebooklm-mcp.<tailnet>.ts.net`.
   Confirm the served URL with `docker compose --profile tailscale exec tailscale tailscale serve status`.

**Verify** either tunnel (the OAuth metadata must serve at the root over a valid cert):
```bash
curl https://<host>/.well-known/oauth-authorization-server     # 200 JSON; issuer == your base URL
```

**Connect:**
- **Claude Code:** `claude mcp add --transport http notebooklm https://<host>/mcp --header "Authorization: Bearer $NOTEBOOKLM_MCP_TOKEN"`
- **claude.ai:** Settings → Connectors → **Add custom connector** → `https://<host>/mcp` (the URL
  **with** `/mcp`) → it registers (DCR) and opens the server's password page.

Full step-by-step + security model: [`deploy/README.md`](../deploy/README.md). Use a
**dedicated/throwaway Google account** — the mounted `master_token.json` is a durable full-account
credential. Multi-tenant hosting is out of scope for this single-tenant setup.

### File upload & download (remote)

The MCP/JSON-RPC channel can't carry binaries, so over a remote connector
`source_add type=file` and `artifact_download` broker a **short-lived signed URL**
served by the same container; your browser does the byte transfer (see
[ADR-0024](adr/0024-mcp-remote-file-transfer.md)). This is the standard pattern for
remote MCP file transfer — MCP has no native file-upload primitive, and its native
download (binary Resources) is capped far below a podcast/video.

**Enable it:** set `NOTEBOOKLM_MCP_PUBLIC_URL` to your bare public origin (the same
host as the tunnel, no `/mcp`). It falls back to `NOTEBOOKLM_MCP_OAUTH_BASE_URL`, so
if you configured claude.ai OAuth above, **file transfer is already on**. Unset on a
bearer-only deploy → the two file tools return a clear "not configured" error
(everything else still works; the server does not refuse to start).

- **Upload a local file:** `source_add type=file` returns an `upload_required` link.
  Open it in your browser, pick the file, and it's added to the notebook. (Claude can
  also `PUT` a file it already holds to that link from its **code-execution sandbox** —
  but that requires Code Execution enabled **and your server domain whitelisted** in
  claude.ai Settings → Capabilities → additional allowed domains, or the `PUT` fails.)
- **Download an artifact:** `artifact_download` returns a `download_ready` link (a
  clickable `resource_link`); open it to stream the podcast/video/PDF to your device.
- Links are HMAC-signed and short-lived (upload 15 min, download 30 min) and expire on
  a server restart. Google Drive (`source_add` with a Drive id) remains a no-browser
  alternative for adding files. stdio (local) installs are unchanged — they still read
  and write real local paths directly.

## Core concepts

These conventions hold across every tool:

- **Name *or* ID.** Every `notebook`/`source`/`note` argument accepts a human title **or** an ID
  (full, or a unique prefix). Use the matching `*_list` tool to discover them. An ambiguous name or
  prefix returns a `VALIDATION` error listing the candidates so you can retry with an exact ID.
- **Destructive tools need confirmation.** `notebook_delete`, `source_delete`, and `note_delete`
  take `confirm` (default `false`). Called without it, they return a `needs_confirmation` preview
  (with the resolved title) and delete **nothing**; call again with `confirm=true` to execute.
- **Long-running work is non-blocking.** `artifact_generate` returns immediately with a `task_id`;
  poll `artifact_status` until it's complete, then `artifact_download`. Research is the same shape:
  `research_start` → `research_status` → `research_import`.
- **Structured errors.** Failures arrive as `CODE: message (retriable=…)`, where `CODE` is one of
  `AUTH`, `RATE_LIMITED`, `NOT_FOUND`, `VALIDATION`, `TIMEOUT`, `NETWORK`, `SERVER`, `RPC`,
  `CONFIG`, `NOTEBOOK_LIMIT`, `ARTIFACT_TIMEOUT`, `SOURCE_MUTATION`, `ERROR`, or `UNEXPECTED`. The
  `retriable` flag tells an agent whether a retry could succeed (e.g. `RATE_LIMITED`, `TIMEOUT`,
  `NETWORK`). Many errors also carry an actionable `hint` (e.g. `AUTH → run notebooklm login`).

## Workflows

The examples below are MCP **tool calls** an agent makes (not shell commands).

### Add sources and ask a question

```text
nb = notebook_create(title="Quantum Computing")
source_add(notebook="Quantum Computing", source_type="url", url="https://arxiv.org/abs/...")
source_add(notebook="Quantum Computing", source_type="text", title="Notes", text="...")
source_wait(notebook="Quantum Computing")                 # block until sources finish processing
chat_ask(notebook="Quantum Computing", question="What are the open problems?")
```

`source_type` is one of `url`, `text`, `file` (local `path`), `drive` (a
`document_id` + `mime_type`), or `youtube`. URL and YouTube adds reject
internal/loopback hosts by default; pass `allow_internal=true` only for
deliberate local NotebookLM tests. `chat_ask` continues the most-recent
conversation unless you pass a `conversation_id`.

### Generate and download a studio artifact

```text
task = artifact_generate(notebook="Quantum Computing", artifact_type="audio")
artifact_status(notebook="Quantum Computing", task_id="<task_id from above>")   # poll until complete
artifact_download(notebook="Quantum Computing", artifact_type="audio", path="podcast.mp3")
```

`artifact_type` is one of `audio`, `video`, `cinematic-video`, `slide-deck`, `quiz`, `flashcards`,
`infographic`, `data-table`, `mind-map`, `report`. Agent-settable options are `audio_format` /
`audio_length` (audio), `quantity` / `difficulty` (quiz, flashcards), and `report_format` (report);
the other kinds use fixed defaults.

### Run deep research and import the findings

```text
task = research_start(notebook="Quantum Computing", query="post-quantum cryptography", source="web", mode="deep")
research_status(notebook="Quantum Computing", task_id=task["task_id"])
research_import(notebook="Quantum Computing", task_id=task["task_id"])
```

`source` is `web` or `drive`; `mode` is `fast` or `deep`. Pass the `task_id`
returned by `research_start` when polling or importing so the request is pinned
to the intended research task; omitting it is allowed only when the notebook has
a single in-flight task.

## Tool reference

| Domain | Tools |
|--------|-------|
| **Notebooks** | `notebook_list` · `notebook_create(title)` · `notebook_describe(notebook)` · `notebook_rename(notebook, new_title)` · `notebook_delete(notebook, confirm)` |
| **Sources** | `source_list(notebook)` · `source_get_content(notebook, source)` · `source_rename(notebook, source, new_title)` · `source_delete(notebook, source, confirm)` · `source_wait(notebook, source?, timeout, interval)` · `source_add(notebook, source_type, ..., allow_internal?)` |
| **Chat** | `chat_ask(notebook, question, conversation_id?)` · `chat_configure(notebook, goal?, response_length?)` |
| **Notes** | `note_create(notebook, title, content)` · `note_list(notebook)` · `note_update(notebook, note, content)` · `note_delete(notebook, note, confirm)` |
| **Artifacts** | `artifact_list(notebook)` · `artifact_generate(notebook, artifact_type, …)` · `artifact_status(notebook, task_id)` · `artifact_download(notebook, artifact_type, path, output_format?)` |
| **Research** | `research_start(notebook, query, source, mode)` · `research_status(notebook, task_id?)` · `research_import(notebook, task_id)` |
| **Server** | `server_info` — version + local auth health |

Tools that only read are annotated read-only; the three `*_delete` tools are annotated destructive
and require `confirm`. A host that honors MCP annotations can auto-allow the read-only calls and
gate the destructive ones.

## Troubleshooting

- **`AUTH` errors / "not authenticated".** Run `notebooklm login` (or `notebooklm -p <profile> login`)
  in a terminal, then restart the server. Check with the `server_info` tool, which reports auth health.
- **`uvx` / `uv` not found.** Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux)
  or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows). The desktop launcher also
  searches common install dirs beyond `PATH`.
- **Client doesn't see the tools.** Confirm the config was written (`notebooklm mcp install <client>`)
  and **restart the client** — most hosts only read MCP config at startup.
- **Wrong account.** The server binds one profile per process. Start it with `--profile <name>`, or set
  `NOTEBOOKLM_PROFILE`. See [configuration.md](configuration.md#multiple-accounts).
- **`RATE_LIMITED`.** NotebookLM enforces per-account quotas; the error is `retriable=true` — back off
  and retry.

## See also

- [installation.md](installation.md#running-the-mcp-server-mcp-extra) — the `mcp` extra + run/connect summary
- [`desktop-extension/README.md`](../desktop-extension/README.md) — one-click Claude Desktop `.mcpb` bundle
- [configuration.md](configuration.md) — profiles, multi-account, storage
- [cli-reference.md](cli-reference.md) — the equivalent CLI commands
