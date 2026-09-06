# Security Policy

## Supported Versions

Only the latest minor release receives security fixes. Earlier `0.x` releases predate the current API surface and are no longer maintained — please upgrade to the latest version.

| Version | Supported          |
| ------- | ------------------ |
| 0.8.x   | :white_check_mark: |
| < 0.8   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it by:

1. **DO NOT** open a public GitHub issue
2. Email the maintainers directly (see `pyproject.toml` for contact info)
3. Include a detailed description of the vulnerability
4. Allow reasonable time for a fix before public disclosure

## Credential Security

This library stores authentication credentials locally. Please understand these security considerations.

The in-process credential model (owners, recovery, cancellation) is in
[docs/security.md](docs/security.md). This file is the operator-facing threat model for
those files **and** for the experimental MCP/REST hosts that open them.

### Storage Locations

By default, files are stored per-profile under `~/.notebooklm/profiles/<profile>/` (configurable via the `NOTEBOOKLM_HOME` and `NOTEBOOKLM_PROFILE` environment variables). Legacy layouts store files directly in the root of `~/.notebooklm/` (representing the `default` profile). Permission modes below are POSIX modes; Windows uses the inherited filesystem ACLs and intentionally skips `chmod`.

| File Path | Contents | Permissions |
|-----------|----------|-------------|
| `profiles/<profile>/storage_state.json` | Google session cookies (Web) | `0o600` (owner-only) |
| `profiles/<profile>/master_token.json` | Google master token (Android / headless). **Account-equivalent** — it can mint new sessions | `0o600` (owner-only) |
| `profiles/<profile>/browser_profile/` | Playwright Chromium profile | `0o700` (owner-only) |
| `profiles/<profile>/context.json` | Active profile context / metadata | `0o600` (owner-only) |
| `oauth/<slug>.json` | MCP self-hosted OAuth clients + **long-lived refresh tokens** (deployment-scoped; override with `NOTEBOOKLM_MCP_OAUTH_STATE_PATH`) | `0o600` (owner-only) |
| `config.json` | Global CLI config (e.g. language/active profile) | Default |
| `storage_state.json` *(legacy)* | Fallback root storage state (for `default` profile) | `0o600` (owner-only) |
| `master_token.json` *(legacy)* | Fallback root master token | `0o600` (owner-only) |
| `browser_profile/` *(legacy)* | Fallback root Playwright profile | `0o700` (owner-only) |
| `context.json` *(legacy)* | Fallback root active notebook context | `0o600` (owner-only, POSIX) |

### Security Best Practices

1. **Protect your credentials**
   - `storage_state.json` contains live Google session cookies
   - `master_token.json` is Google-account equivalent (Android / headless). Anyone with that file can mint new NotebookLM sessions for the account
   - Never share, commit, or expose either file. Treat the MCP OAuth state file the same way

2. **Add to .gitignore**
   ```gitignore
   .notebooklm/
   ```

3. **Credential rotation**
   - Re-run `notebooklm login` periodically to refresh Web cookies
   - Sessions typically last days to weeks before expiring
   - Rotate or revoke a master token immediately if its file may have been exposed

4. **If credentials are compromised**
   - Immediately revoke access at [Google Security Settings](https://myaccount.google.com/permissions)
   - For a leaked master token, remove its associated device/session in [Google Account security](https://myaccount.google.com/device-activity). A password change or deleting local files alone does not revoke an attacker's copy; see [master-token troubleshooting](docs/troubleshooting.md#authentication-errors)
   - Delete the `~/.notebooklm/` directory **and** any MCP OAuth state file (`<home>/oauth/*.json` or `NOTEBOOKLM_MCP_OAUTH_STATE_PATH`)
   - Restart any `notebooklm-mcp` / `notebooklm-server` process that had the files open
   - Re-authenticate with `notebooklm login`

5. **CI/CD usage**
   - Do not commit credentials to repositories
   - Use `NOTEBOOKLM_AUTH_JSON` environment variable for secure, file-free Web authentication
   - Store the JSON value in GitHub Secrets or similar secure secret management
   - The env var approach keeps credentials in memory only, never written to disk
   - Do not put a `master_token.json` or OAuth state file in CI logs or shared artifact stores

### What This Library Does NOT Do

- Does not transmit credentials to any third party
- Does not store Google account passwords (CLI login is browser-based; MCP OAuth uses a separate operator-set `NOTEBOOKLM_MCP_OAUTH_PASSWORD`)
- Does not access data outside of NotebookLM except for operator-selected local files (CLI file add; MCP stdio `source_add(path=...)` reads a **server-host** path the process can open — see below), opt-in browser-cookie extraction during login/refresh, and the experimental MCP/REST hosts described below
- Does not modify Google account settings

## MCP and REST hosting

`notebooklm-mcp` and `notebooklm-server` are **experimental, single-tenant** adapters. They are not a multi-tenant product and are not covered by the library's semver guarantees. Both front account-equivalent Google credentials for whoever can reach the process.

Operator setup: [docs/mcp-guide.md](docs/mcp-guide.md) and
[docs/installation.md#rest-api-server](docs/installation.md#rest-api-server).
Signed MCP file links: [ADR-0024](docs/adr/0024-mcp-remote-file-transfer.md).

### Master tokens

`master_token.json` is Google-account equivalent, not a weaker sibling of
`storage_state.json`. The Android backend mints short-lived bearers from it; headless
Web recovery can mint cookies from it. A leaked master token is a durable account
credential. See [docs/security.md](docs/security.md).

### MCP OAuth refresh tokens

`notebooklm-mcp` HTTP + OAuth (`NOTEBOOKLM_MCP_OAUTH_PASSWORD` +
`NOTEBOOKLM_MCP_OAUTH_BASE_URL`) **does** issue long-lived tokens. Refresh tokens are
long-lived and written `0600` under the OAuth state file (default
`<home>/oauth/<slug>.json`, keyed on the issuer; override with
`NOTEBOOKLM_MCP_OAUTH_STATE_PATH`). Treat that file as a full-account secret, same
tier as `master_token.json`.

Rotating `NOTEBOOKLM_MCP_OAUTH_PASSWORD` does not revoke already-issued refresh
tokens. Real revocation is delete that file + restart. A legacy profile-dir
`oauth_state.json` that was migrated once is renamed `.migrated` and is never
re-read, so deleting the live file does not resurrect those tokens.

Open OAuth Dynamic Client Registration (DCR) lets a caller register a client
without the login password. Registering a client does not bypass the login
password: phishing still requires the owner to authenticate on `/login`. The
login page shows the (escaped) redirect target so a rogue client is visible
before the password is typed.

### Stdio `source_add(path=...)`

On **stdio**, `source_add(source_type="file", path=...)` reads a file on the
**server host**. Host-path uploads are off until the operator sets
`NOTEBOOKLM_MCP_ALLOWED_ROOTS` to explicit upload directories. Separate multiple
roots with the OS path separator (`:` on POSIX, `;` on Windows); for example,
`NOTEBOOKLM_MCP_ALLOWED_ROOTS=/srv/notebooklm-uploads` allows that directory.
The user's home, NotebookLM home, and filesystem root are rejected as roots.
Paths outside configured roots, symlinks, and non-files are rejected.
Accepted files are opened without following symlinks or junctions and copied
into a private temporary directory before the tool awaits client access.
Both backends upload that copy, which is removed on completion, failure, or
cancellation; replacing the caller path cannot redirect a later backend open.

Known credential filenames (`storage_state.json`, `master_token.json`) and
Playwright profile directories are refused even inside an allowed root. The
shared CLI file-add validator also refuses these known credential paths.
Keep upload directories separate from credential storage and grant the server
process only the filesystem access it needs.

Remote HTTP does **not** open a host `path` for file add; it returns a signed
upload URL instead. `bytes_base64` accepts bytes already supplied in-channel
and does not open a caller-selected host path. This boundary implements
[#2385](https://github.com/teng-lin/notebooklm-py/issues/2385).

### Signed `/files/dl` and `/files/ul` URLs

MCP `/files/dl` and `/files/ul` are HMAC-URL auth only (not bearer/OAuth). A
browser opening a signed link cannot carry the MCP credential, and FastMCP does
not wrap these custom routes with the MCP bearer/OAuth gate. A leaked URL is a
timed capability: ordinary upload links last 15 minutes, widget upload pools
last 60 minutes, and download links last 30 minutes. Upload links can be consumed
earlier; all tokens die on process restart because the signing key is ephemeral. See
[ADR-0024](docs/adr/0024-mcp-remote-file-transfer.md).

### Loopback vs token

- **MCP loopback HTTP may be tokenless.** `NOTEBOOKLM_MCP_TOKEN` / OAuth is
  required to bind a **non-loopback** interface. A default `127.0.0.1` HTTP
  server can run without a bearer. The Host-header DNS-rebinding guard remains
  (requests whose `Host` is not a loopback literal are rejected). The
  `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1` flag alone does not drop that guard.
- **REST `/v1` routes require a bearer token.** `GET /healthz` is tokenless.
  Configure the token with `--token-file` /
  `NOTEBOOKLM_SERVER_TOKEN_FILE` (preferred), or `NOTEBOOKLM_SERVER_TOKEN`.
  `notebooklm-server` refuses to start without a token. By default every `/v1`
  route requires both a loopback peer and a loopback `Host`.
  `NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1` disables both loopback checks,
  including the Host-header rebinding guard; bearer authentication stays required.

### `GET /healthz` is liveness, not readiness

REST `GET /healthz` is a public, token-less **liveness** probe. It returns
`{"ok": true}` when startup catches an authentication failure (for example,
stale credentials or a missing profile) and keeps the app running without an
open NotebookLM client. Other startup failures, including network errors,
abort startup and leave the endpoint unavailable. It does not report version,
account, or whether `/v1` can serve. Readiness would be a separate contract and
is not implemented.

## Dependency Security

This library keeps the base dependency set small and puts optional surfaces behind
extras:

| Dependency | Scope | Purpose |
|------------|-------|---------|
| `httpx` | base | HTTP client |
| `click` | base | CLI framework |
| `rich` | base | Terminal output |
| `filelock` | base | Cross-process file locking for profile/context writes |
| `markdownify` | `markdown` extra | HTML-to-Markdown conversion |
| `playwright` | `browser` extra | Interactive/headless browser login |
| `rookie-cookies` | `cookies` extra | Opt-in browser-cookie import |
| `fastmcp` | `mcp` extra | MCP server adapter |
| `fastapi`, `uvicorn[standard]`, `python-multipart` | `server` extra | Optional REST server and file uploads |

### Auditing Dependencies

`pip-audit` in CI still exports `browser+dev+markdown` by default
(`.github/workflows/dependency-audit.yml`). That is the CLI/contributor graph.
The **internet-facing** graph for a hosted MCP or REST deployment is the `mcp`
and/or `server` extras; audit those before exposing `notebooklm-mcp` or
`notebooklm-server`.

```bash
# Mirror CI: audit the locked selected-extra graph
uv sync --frozen --extra browser --extra dev --extra markdown
uv run python -m pip install "pip-audit>=2.7.0,<3"
uv export --frozen --extra browser --extra dev --extra markdown --format requirements-txt --no-emit-project \
  | uv run pip-audit --strict --require-hashes --disable-pip -r /dev/stdin

# Hosted MCP/REST (internet-facing graph) — include those extras too
uv export --frozen --extra browser --extra dev --extra markdown --extra mcp --extra server \
  --format requirements-txt --no-emit-project \
  | uv run pip-audit --strict --require-hashes --disable-pip -r /dev/stdin
```

The `cookies` extra remains an explicit opt-in because browser-cookie extraction
reads protected browser stores and is not needed for ordinary interactive login.

## Known Limitations

### Undocumented API

This library uses Google's internal APIs, which means:

- **No official security guarantees** from Google
- **API changes without notice** may break functionality
- **Rate limiting** may be applied by Google
- **Account restrictions** are possible for unusual usage patterns

### Session Security

- CLI/Web sessions are cookie-based (standard web authentication)
- CSRF tokens are required and automatically handled
- The CLI does not mint Google API keys. `notebooklm-mcp` HTTP + OAuth **does**
  issue long-lived refresh tokens (see [MCP OAuth refresh tokens](#mcp-oauth-refresh-tokens))

## Questions?

For security questions that are not vulnerabilities, open a [GitHub Discussion](https://github.com/teng-lin/notebooklm-py/discussions).
