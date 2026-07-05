# Remote `notebooklm-mcp` — Docker + a tunnel (Cloudflare or Tailscale)

Run the MCP server as a **remote connector** (Claude Code / claude.ai / Cursor)
behind a tunnel: no public IP, no open ports, no TLS certificate to manage.
Single-tenant, self-hosted. Pick **Cloudflare** (needs a domain) or **Tailscale
Funnel** (no domain). `make up` pulls a **prebuilt image** — no source checkout.

> ⚠️ **Use a dedicated / throwaway Google account.** The mounted
> `master_token.json` is a durable, full-account credential. Treat the mounted profile dir
> and `.env` as secrets (both are gitignored).

## Quick start (the easy path)
```bash
# 1. bootstrap the master token once, on a machine with a browser (see §1):
pip install "notebooklm-py[browser,headless]"
notebooklm login --master-token --account you@example.com
# 2. pick a tunnel + generate secrets (writes deploy/.env):
cd deploy && make setup
# 3. finish the tunnel setup (§3 — the one irreducible manual part), then:
make up
```
`make up` pulls the published image and starts it. The rest of this doc is the
detailed walk-through + the security model.

## Prerequisites
- Docker + Docker Compose.
- **Cloudflare tunnel:** a domain on Cloudflare (free plan is fine). **Tailscale
  Funnel:** a Tailscale account (no domain needed).

## 1. Bootstrap the master token (once, on a machine with a browser)
```bash
pip install "notebooklm-py[browser,headless]"
notebooklm login --master-token --account you@example.com
```
This writes `master_token.json` (+ a minted `storage_state.json`) into
`~/.notebooklm/profiles/<profile>/`. **You don't copy or chown anything** — the
container mounts that dir directly and runs as *your* uid:gid, so the files stay
owned by you (your `notebooklm` CLI keeps working) and are readable/writable with
no permission dance.

- **Default:** mounts `~/.notebooklm/profiles/default`.
- **Other profile:** set `NOTEBOOKLM_PROFILE_DIR` in `.env` (e.g. a
  dedicated/throwaway profile — recommended, since `master_token.json` is a
  full-account credential).

The dir is mounted **read-write** because the server re-mints/rotates cookies into
`storage_state.json` (+ its `.lock`) — a read-only mount makes the session die
~1 h in. Running as your uid is what makes that write work without a chown.
(`make` fills your uid/gid from `id` automatically; for raw `docker compose`, set
`NOTEBOOKLM_UID`/`NOTEBOOKLM_GID` in `.env`.)

## 2. Configure secrets
```bash
cd deploy && make setup        # recommended: picks a tunnel + generates the secrets → .env
```
Or by hand:
```bash
cp deploy/.env.example deploy/.env
# NOTEBOOKLM_MCP_TOKEN: python -c "import secrets; print(secrets.token_urlsafe(32))"
# CF_TUNNEL_TOKEN: from the Cloudflare dashboard (next step)
```

## 3. Choose a tunnel
The stack ships two tunnel sidecars as Compose **profiles** — pick one (the server
runs under either). Both terminate TLS at their edge, so there's no cert to manage and
no host ports are published.

> **Only using Claude Code / Cursor / Desktop (not claude.ai)? You may not need a public
> tunnel at all** — those clients send a bearer, so they need *reachability*, not a public
> URL. If the container's host is already on your **Tailscale tailnet** (tailscaled on the
> host, not in a sidecar), you can skip Funnel, OAuth, and the MagicDNS-cert / `funnel`
> node-attribute steps entirely, and reach the server over the private tailnet:
> ```bash
> # publish 9420 on the host's TAILSCALE IP only (never 0.0.0.0 — that would expose it
> # to your LAN/public), with no tunnel profile:
> NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1 \
>   docker compose run -d --service-ports -p "$(tailscale ip -4):9420:9420" notebooklm-mcp
> claude mcp add --transport http notebooklm \
>   http://<host>.<your-tailnet>.ts.net:9420/mcp \
>   --header "Authorization: Bearer $NOTEBOOKLM_MCP_TOKEN"
> ```
> Most private option — nothing is ever public (plain http is fine; the tailnet is
> encrypted). It's a bit more manual than `make up` because it's outside the tunnel
> profiles. The tunnels below exist only because **claude.ai** is a cloud client that
> needs a public HTTPS URL.

### 3A. Cloudflare Tunnel (needs a domain in your Cloudflare account)
In the Cloudflare dashboard → **Networking → Tunnels → Create Tunnel** (moved there
Feb 2026; the old Zero Trust dashboard now redirects here):
1. **Create Tunnel** → name it. This is a **Cloudflared** tunnel — hostname ingress; the
   tunnel's **Type** shows `cloudflared`. Copy the **token** it shows into `CF_TUNNEL_TOKEN`
   in `.env`. Ignore the `cloudflared` install/run commands on that page — the compose
   `cloudflared` sidecar runs it for you using the token.
2. On the tunnel's **Routes** tab → **Add route** → **Published application**. Under
   **Hostname**, set a **Subdomain** (e.g. `notebooklm`) + your **Domain** zone → giving
   `notebooklm.yourdomain.com`; set **Service URL** to `http://notebooklm-mcp:9420` — plain
   `http` (the compose service listens on HTTP; Cloudflare's edge terminates TLS), not the
   `https://` in the field's placeholder. Cloudflare auto-creates the DNS record + serves TLS.
   Leave **Path** empty so the **whole host** (`/`) is routed — not a `/mcp`-scoped path — so
   the root OAuth routes are reachable. (Profile: `cloudflare`, the default.)
> Optional: set `NOTEBOOKLM_MCP_TRUST_PROXY=1` in `.env` to key the per-IP login
> throttle on the tunnel's `CF-Connecting-IP` header. Default off keys on the socket
> peer (the tunnel egress) — one global throttle bucket. Only enable it behind a trusted
> proxy that sets the header; an exposed-directly origin could forge it to dodge the throttle.

### 3B. Tailscale Funnel (NO domain — free, stable `*.ts.net` HTTPS)
Best when you don't own a domain: Tailscale Funnel gives a stable public HTTPS
hostname on Tailscale's domain, free on the personal plan, no DNS to manage.
**One-time tailnet setup** (admin console — these are policy/feature prerequisites, not
per-machine toggles):
1. Enable **MagicDNS** and **HTTPS certificates** for the tailnet
   (admin console → DNS; → HTTPS Certificates).
2. Grant the **`funnel` node attribute** in the tailnet policy: admin console →
   **Settings → Funnel → Manage** (this deep-links to the ACL policy editor) and add a
   `nodeAttrs` block to the policy file, then **Save** — for a single self-hosted node
   you can scope it to all members:
   ```json
   { "nodeAttrs": [{ "target": ["*"], "attr": ["funnel"] }] }
   ```
3. Create a **normal auth key** (Settings → Keys) and put it in `.env` as `TS_AUTHKEY`.
   (There is no "Funnel-capable" key type — Funnel comes from the policy in step 2.)

Then the compose `tailscale` sidecar (profile `tailscale`) runs `tailscale/tailscale`
with `deploy/tailscale/funnel.json` (`TS_SERVE_CONFIG`), which funnels public `:443 /`
→ `notebooklm-mcp:9420` (so the OAuth routes at `/` AND `/mcp` are reachable). The node
is `TS_HOSTNAME=notebooklm-mcp`, so your public origin is
`https://notebooklm-mcp.<your-tailnet>.ts.net`.
> **Find `<your-tailnet>`** on the admin console **DNS** page — the **"Tailnet name"**
> shown there (e.g. `tailXXXXXX.ts.net`). After the sidecar is up you can also confirm
> the full URL with `docker compose --profile tailscale exec tailscale tailscale serve status`.
> Funnel only serves on ports 443/8443/10000 — the config uses **443**, so the URL has
> no port suffix. The serve config is bind-mounted as a **directory** (`./tailscale` →
> `/config`) per Tailscale's Docker requirement. Not live-verified in this repo —
> check `docker compose --profile tailscale logs tailscale` for the served URL on first run.

Then set the matching **OAuth base URL** in `.env` (bare origin — see step 6):
```
# Cloudflare:  NOTEBOOKLM_MCP_OAUTH_BASE_URL=https://notebooklm.yourdomain.com
# Tailscale:   NOTEBOOKLM_MCP_OAUTH_BASE_URL=https://notebooklm-mcp.<your-tailnet>.ts.net
```

## 4. Run

The `Makefile` wraps the tunnel choice (from `make setup`, or `TUNNEL=…`) and the
pull-vs-build modes — one command each:

```bash
cd deploy
make up                        # PULL the published image + start (the easy path)
make prod VERSION=<version>    # ...pin a specific published version
make dev                       # BUILD this checkout + start (contributors)
make dev TUNNEL=tailscale      # ...forcing the Tailscale Funnel sidecar for this run
make logs                      # tail the server log (expect: bound 0.0.0.0:9420)
make restart                   # rebuild this checkout + recreate after a source change
make down                      # stop and remove
```

`make up` uses this checkout's `pyproject.toml` version. If you copied only
`deploy/`, pass `VERSION=<version>` or set `NOTEBOOKLM_MCP_VERSION` in `.env`.
If that version has not been published to Docker Hub yet, pass a published
`VERSION=<version>` or use `make dev` to build this checkout.

Equivalent raw compose (`--profile` selects the tunnel; set `NOTEBOOKLM_MCP_VERSION`
first, or put it in `.env`):
- **Pull + run (any tunnel):** `export NOTEBOOKLM_MCP_VERSION=<version>` then
  `docker compose --profile cloudflare pull && docker compose --profile cloudflare up -d`
  (swap `tailscale` for the other tunnel).
- **Build from source:** add the build override —
  `docker compose -f docker-compose.yml -f docker-compose.build.yml --profile cloudflare up -d --build`.
- **A different image / tag:** set `NOTEBOOKLM_MCP_IMAGE` / `NOTEBOOKLM_MCP_VERSION` in `.env`.

## 5. Connect from Claude Code
```bash
claude mcp add --transport http notebooklm \
  https://notebooklm-mcp.yourdomain.com/mcp \
  --header "Authorization: Bearer $NOTEBOOKLM_MCP_TOKEN"
```
Claude **Desktop** also accepts the bearer. Claude **.ai** (web/mobile) and **ChatGPT**
do not — their connector UIs are OAuth-only — so use step 6 (+ step 7 for ChatGPT).

## 6. (Optional) Connect from claude.ai — self-hosted OAuth (one password)
claude.ai's connector UI has no bearer field; it speaks OAuth. Instead of an external
IdP, the server runs its own tiny OAuth authorization server gated by **one password**.
**Opt-in and additive** — leave both vars unset to stay bearer-only (Claude Code/Desktop
unaffected); when set, the bearer and OAuth work side by side on the same `/mcp`.

1. **`.env`** — set a strong password + your public URL (see `.env.example`):
   ```
   # a long random secret — the gate (>=16 chars):
   NOTEBOOKLM_MCP_OAUTH_PASSWORD=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
   # the BARE public https origin — NOT the /mcp connector URL (the OAuth endpoints
   # /authorize, /token, /register, /login, /.well-known/* mount at the ROOT):
   NOTEBOOKLM_MCP_OAUTH_BASE_URL=https://notebooklm.example.com
   ```
   `make dev` (or `make prod VERSION=…`). Both required together — partial/weak/
   non-https/has-a-path config refuses to start.
2. **Cloudflare tunnel** — the Public Hostname must route the WHOLE host (path `/`,
   not a `/mcp`-scoped ingress) to `http://notebooklm-mcp:9420`, so the root OAuth
   routes are reachable. (The `notebooklm.` subdomain is created automatically when you
   add the Public Hostname; the zone just has to be in your Cloudflare account.)
3. **Verify** (after `make dev` + the tunnel is up):
   ```
   curl https://notebooklm.example.com/.well-known/oauth-authorization-server
   ```
   `issuer` should be your bare origin and `authorization_endpoint` should be
   `…/authorize` (at the root). If they show `…/mcp/authorize`, your BASE_URL has the
   `/mcp` path — drop it.
4. **claude.ai → Customize → Connectors** (individual Pro/Max) or **Organization settings →
   Connectors** (Team/Enterprise owners) → **`+` → Add custom connector** → enter the URL
   **WITH** `/mcp`: `https://notebooklm.example.com/mcp`. Leave **Advanced settings** (OAuth
   Client ID/Secret) **blank** — the server supports DCR, so claude.ai registers itself, then
   opens the server's **password page** in your browser; enter the password → you're connected.
   Claude Code keeps using the bearer. (Custom connectors work on Free/Pro/Max/Team/Enterprise;
   Free is capped at one.)

   > **base URL vs connector URL:** `NOTEBOOKLM_MCP_OAUTH_BASE_URL` is the bare origin
   > (`https://host`); the claude.ai connector URL is that **+ `/mcp`**.

> **What it does NOT need vs an IdP:** no dashboard, no JWT template, no audience/email
> config — the password is the whole identity. Registered clients + tokens **persist**
> across restarts in `oauth_state.json` under the mounted profile, so a redeploy doesn't
> force re-login. **Treat `oauth_state.json` as a full-account secret** (it holds
> long-lived OAuth tokens — same tier as `master_token.json`).
> **Honest trade:** because the login page is served through your tunnel, **Cloudflare's
> edge sees the password in transit** (it terminates TLS) — use a throwaway Google
> account. Note: rotating the password does **not** revoke already-issued OAuth tokens
> (they're long-lived + persisted); **real revocation = delete `oauth_state.json` and
> restart**. (To remove Cloudflare from the path, self-host TLS instead.)

## 7. (Optional) Connect from ChatGPT — same OAuth, Developer Mode required
ChatGPT's custom MCP connectors speak the **same self-hosted OAuth** as claude.ai — there's
no bearer/API-key field, so step 6 (`NOTEBOOKLM_MCP_OAUTH_PASSWORD` + `NOTEBOOKLM_MCP_OAUTH_BASE_URL`)
is a prerequisite. It's also **web only** — ChatGPT has no MCP-connector UI on mobile. Which
plans get *full* write-capable Developer Mode vs. read/fetch-only MCP changes over time (it has
skewed toward Business/Enterprise/Edu for the write tools), so check
[OpenAI's Developer Mode doc](https://help.openai.com/en/articles/12584461) for current plan
support before relying on the write tools. Then:

1. **Enable Developer Mode:** ChatGPT → **Settings → Apps & Connectors → Advanced settings →
   Developer mode** (web only). This unlocks *full* MCP connectors with write tools; without it
   the connector UI is deep-research (search/fetch) only.
2. **Settings → Apps & Connectors** → click **Create** → **MCP Server URL** = the URL **WITH** `/mcp`
   (`https://notebooklm.example.com/mcp`), **Authentication = OAuth**. ChatGPT registers itself
   via **DCR** — same as claude.ai, so there's no redirect URI to allowlist on your side — then
   opens the server's **password page**; enter the password → connected.
3. **Don't rely on write prompts.** ChatGPT *may* ask before a write tool runs — it reads each
   tool's `readOnlyHint`/`destructiveHint` — but whether it prompts depends on the action and your
   per-connector permission settings, so it isn't a guarantee. The real backstop is server-side:
   our destructive/sharing-widening tools self-gate on `confirm=true`. Only "always allow" tools on
   a server you trust — Developer Mode is powerful,
   and a malicious server or prompt injection can destroy data.

> Bearer-only deploys (no OAuth) can't be added to ChatGPT **or** claude.ai — both connector UIs
> are OAuth-only. Enable step 6 for either.

## Notes & security
- **Two auth layers.** The `NOTEBOOKLM_MCP_TOKEN` bearer gates *who can use the
  endpoint*; the master token authenticates *the server to Google*. The master
  token **never** traverses the tunnel — only MCP tool calls/results do. The
  bearer **does** terminate at Cloudflare (Cloudflare can see it in transit, like
  any reverse-proxied request), so rotate it freely.
- **Fail-closed.** The server refuses to start on a non-loopback bind with no auth
  at all (neither `NOTEBOOKLM_MCP_TOKEN` nor self-hosted OAuth), and refuses
  partial/weak/non-https OAuth config.
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
