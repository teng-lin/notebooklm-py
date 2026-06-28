# Remote `notebooklm-mcp` — Docker + Cloudflare Tunnel

Run the MCP server as a **remote connector** (Claude Code / Claude.ai / Cursor)
behind a Cloudflare Tunnel: no public IP, no open ports, no TLS certificate to
manage. Single-tenant, self-hosted.

> ⚠️ **Use a dedicated / throwaway Google account.** The mounted
> `master_token.json` is a durable, full-account credential. Treat `./profile`
> and `.env` as secrets (both are gitignored).

## Prerequisites
- Docker + Docker Compose.
- A domain on Cloudflare (free plan is fine) for the Tunnel hostname.

## 1. Bootstrap the master token (once, on a machine with a browser)
```bash
pip install "notebooklm-py[browser,headless]"
notebooklm login --master-token --account you@example.com
```
This writes `master_token.json` (+ a minted `storage_state.json`) into the
profile dir. Copy that profile dir here:
```bash
mkdir -p deploy/profile
cp -r ~/.notebooklm/profiles/<profile>/. deploy/profile/
chmod 600 deploy/profile/master_token.json
# The container runs as uid 10001 (non-root). It must be able to READ
# master_token.json AND WRITE storage_state.json (+ its .lock) for cookie
# rotation, so give that uid ownership of the bind-mounted dir:
sudo chown -R 10001:10001 deploy/profile
```
The profile dir is mounted **read-write** — the server re-mints/rotates cookies
into `storage_state.json` here, so it must be writable by uid 10001 (a read-only
mount, or a dir the container uid can't write, makes the session die ~1 h in).

## 2. Configure secrets
```bash
cp deploy/.env.example deploy/.env
# NOTEBOOKLM_MCP_TOKEN: python -c "import secrets; print(secrets.token_urlsafe(32))"
# CF_TUNNEL_TOKEN: from the Cloudflare dashboard (next step)
```

## 3. Create the Cloudflare Tunnel
In the Cloudflare **Zero Trust** dashboard → **Networks → Tunnels**:
1. Create a tunnel; copy its **token** into `CF_TUNNEL_TOKEN` in `.env`.
2. Add a **Public Hostname** (e.g. `notebooklm-mcp.yourdomain.com`) →
   **Service** `http://notebooklm-mcp:9420`. Cloudflare auto-creates the DNS
   record and serves TLS with its own cert.

## 4. Run

The `Makefile` wraps the two build modes — one command each:

```bash
cd deploy
make dev                    # build + install THIS checkout (source) and start
make prod VERSION=0.8.0     # build + install a published PyPI release and start
make logs                   # tail the server log (expect: bound 0.0.0.0:9420)
make restart                # rebuild + recreate after a source/config change
make down                   # stop and remove
```

Equivalent raw compose (the image installs `notebooklm-py` two ways; build
context is the repo root):
- **From source (default):** `docker compose up -d --build` installs *this
  checkout* — you deploy the exact code in the repo (right for dev / an
  unreleased branch).
- **From a published release:** `docker compose build --build-arg
  NOTEBOOKLM_SPEC="notebooklm-py[mcp,headless]==0.8.0"` then `docker compose up -d`
  (or uncomment `build.args.NOTEBOOKLM_SPEC` in `docker-compose.yml`).

## 5. Connect from Claude Code
```bash
claude mcp add --transport http notebooklm \
  https://notebooklm-mcp.yourdomain.com/mcp \
  --header "Authorization: Bearer $NOTEBOOKLM_MCP_TOKEN"
```
(For Claude.ai / Desktop custom connectors, add the same URL under
**Settings → Connectors**. The static bearer works for Claude Code today; a
polished one-click OAuth flow is a future enhancement.)

## Notes & security
- **Two auth layers.** The `NOTEBOOKLM_MCP_TOKEN` bearer gates *who can use the
  endpoint*; the master token authenticates *the server to Google*. The master
  token **never** traverses the tunnel — only MCP tool calls/results do. The
  bearer **does** terminate at Cloudflare (Cloudflare can see it in transit, like
  any reverse-proxied request), so rotate it freely.
- **Fail-closed.** The server refuses to start on a non-loopback bind without
  `NOTEBOOKLM_MCP_TOKEN` set.
- **One container per account.** Do not scale replicas off one master token —
  concurrent re-mints invalidate each other's session.
- **Rotate the bearer**: change `NOTEBOOKLM_MCP_TOKEN` in `.env`,
  `docker compose up -d`, and update the `claude mcp add` header.
- **Files**: the connector moves text/references only. Add device files via
  Google Drive (`source_add` with a Drive id) or the NotebookLM app; consume
  generated podcasts/videos/slides in the NotebookLM app (same account).
- **Optional hardening**: instead of a single `rw` bind-mount, mount
  `master_token.json` as a separate read-only Docker secret and use a writable
  named volume for `storage_state.json` + `.storage_state.json.lock`.
