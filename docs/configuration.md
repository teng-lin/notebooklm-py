# Configuration

**Status:** Active
**Last Updated:** 2026-09-02

This guide covers storage locations, environment settings, and configuration options for `notebooklm-py`.

## File Locations

All data is stored under `~/.notebooklm/` by default, organized by profile:

```
~/.notebooklm/
├── config.json           # Global config: default_profile, language
├── profiles/
│   ├── default/          # Default profile (auto-created)
│   │   ├── storage_state.json    # Authentication cookies and session
│   │   ├── master_token.json     # Durable headless/Android credential (optional)
│   │   ├── context.json          # CLI context (active notebook, conversation)
│   │   └── browser_profile/      # Persistent Chromium profile
│   ├── work/             # Named profile example
│   │   ├── storage_state.json
│   │   ├── master_token.json
│   │   ├── context.json
│   │   └── browser_profile/
│   └── personal/
│       └── ...
```

`config.json` stores process-wide settings — the persisted default profile
name (under the `default_profile` key) and the configured interface language
(`language`). It is **global, not per-profile** (see
`src/notebooklm/paths.py` and `src/notebooklm/cli/language_cmd.py`).

**Legacy layout:** If upgrading from a pre-profile version, the first run auto-migrates flat files into `profiles/default/`. The migration runs under a single-writer `filelock` rooted at `~/.notebooklm/.migration.lock`, so concurrent CLI invocations (e.g., container start-up races) cannot interleave copies — the loser of the lock re-checks the completion marker and no-ops (see `src/notebooklm/migration.py`). The legacy flat layout continues to work as a fallback.

You can relocate all files by setting `NOTEBOOKLM_HOME`:

```bash
export NOTEBOOKLM_HOME=/custom/path
# All files now go to /custom/path/profiles/<profile>/
```

### Storage State (`storage_state.json`)

Contains the authentication data extracted from your browser session:

```json
{
  "cookies": [
    {
      "name": "SID",
      "value": "...",
      "domain": ".google.com",
      "path": "/",
      "expires": 1234567890,
      "httpOnly": true,
      "secure": true,
      "sameSite": "Lax"
    },
    ...
  ],
  "origins": [],
  "notebooklm": {
    "version": 1,
    "account": {
      "authuser": 0,
      "email": "you@example.com"
    }
  }
}
```

**Cookie requirements** (empirically validated via single-, pair-, and three-way ablation; see [auth-cookie-lifecycle.md](auth-cookie-lifecycle.md#33-empirical-cookie-requirements); enforced by `_validate_required_cookies()` in `_auth/cookie_policy.py`):

- **Tier 1 — strictly required (raises on absence):** both `SID` and `__Secure-1PSIDTS`. Recovery may be able to re-mint `__Secure-1PSIDTS` before validation, but an input that reaches normal validation without either cookie is rejected. Authoritative value: `MINIMUM_REQUIRED_COOKIES` in `_auth/cookie_policy.py`.
- **Tier 2 — secondary binding (logs a warning if absent):** either `OSID` is present, or `APISID` and `SAPISID` are present **together with bare `LSID`** (the `LSID` conjunct is required — the pair alone fails, per the three-way ablation in #1977). Without this, even valid Tier 1 cookies can't authenticate the homepage GET. Logged rather than raised so unverified edge-case flows (e.g. Workspace SSO) aren't broken by a too-strict client check.

In practice: extract the full cookie set via `notebooklm login` and don't try to subset it. Partial extractions (a known failure mode of browser-cookies tooling under Chrome 127+ App-Bound Encryption) are the leading suspect for "auth expires immediately" reports — see [#371](https://github.com/teng-lin/notebooklm-py/issues/371).

The optional `notebooklm.account` block records the Google account route for
multi-account sessions. New `notebooklm login` runs write it when the active
account can be discovered safely. Older Playwright-created files can be repaired
with `notebooklm auth refresh` when the storage state maps to one visible
account; if several accounts are visible, use
`notebooklm login --browser-cookies <browser> --account EMAIL` to bind the
intended account explicitly. `auth refresh` only repairs absent metadata; if
metadata exists but points at the wrong account, re-bind explicitly with the
browser-cookie login path.

**Override location:**
```bash
notebooklm --storage /path/to/storage_state.json list
```

### Context File (`context.json`)

Stores the current CLI context, such as the active notebook:

```json
{
  "notebook_id": "abc123def456",
  "title": "Quarterly review notes",
  "is_owner": true,
  "role": "owner",
  "created_at": "2026-05-01T17:43:21Z"
}
```

Field summary:

- `notebook_id` — currently selected notebook, written by `notebooklm use` and read by every command that takes `-n/--notebook`.
- `title`, `is_owner`, `role`, `created_at` — optional notebook metadata captured at selection time so `status` / display commands don't need an extra round-trip. Omitted when the CLI didn't have the values to write (see `set_current_notebook` in `src/notebooklm/cli/context.py`). `role` is `"owner"` / `"editor"` / `"viewer"`; `is_owner` is the `role == "owner"` shorthand and is retained for backward compatibility. Contexts written before `role` existed carry only `is_owner`, and `status` falls back to it.

This file is managed automatically by `notebooklm use`, `notebooklm clear`, and the `auth` commands.

### Browser Profile (`browser_profile/`)

A persistent Chromium user data directory used during `notebooklm login`.

**Why persistent?** Google blocks automated login attempts. A persistent profile makes the browser appear as a regular user installation, avoiding bot detection.

**To reset:** Delete the `browser_profile/` directory and run `notebooklm login` again.

With `--storage <path>`, the browser profile is isolated with that storage file.
The conventional `storage_state.json` uses a `browser_profile/` directory beside
it; any normal-length custom filename uses `<path>.browser_profile`. For example,
`A.json` and `B.json` use `A.json.browser_profile/` and
`B.json.browser_profile/` respectively. If that filename would exceed the
255-byte component limit, the canonical storage path maps to a short stable
`storage-<hash>.browser_profile/` name; relative and absolute aliases map to the
same directory.

Browser directories newly created for explicit storage contain a
`.notebooklm-owned` marker. `login --fresh` refuses to recursively delete an
unowned sidecar, and `auth logout` skips it and reports the preserved path.
Canonical named-profile and legacy browser directories remain managed even when
their `storage_state.json` is selected explicitly with `--storage`; an arbitrary
same-named directory outside `NOTEBOOKLM_HOME` remains unowned. A marker-only
directory does not count as a reusable L3 browser session.

> **Upgrading:** Existing custom-storage browser sessions are not migrated. You
> may need one interactive sign-in per storage file. Do not copy or delete the
> old shared browser profile unless its account/profile ownership is known.

### Master Token (`master_token.json`)

Written only by `notebooklm login --master-token --account EMAIL`. It needs `gpsoauth`, supplied
by either the `[headless]` or `[android]` extra. It holds a durable Google master
token (mode `0600`) that mints/refreshes the profile's
`storage_state.json` cookies with no per-session browser. When present beside a
profile's `storage_state.json`, an expired session re-mints from it
automatically. If storage is absent, `notebooklm auth refresh` mints it from the
exact sibling token and passively validates once. The legacy login refresh flag
remains an unconditional forced re-mint.

```json
{"version": 1, "email": "...", "android_id": "<hex>", "master_token": "aas_et/..."}
```

> ⚠️ **Full-account, durable credential** — larger blast radius than
> `storage_state.json`; dedicated/throwaway account only. See
> [installation.md#alternative-master-token-auth-no-cookie-file-to-ship-survives-expiry](installation.md#alternative-master-token-auth-no-cookie-file-to-ship-survives-expiry).

## Environment Variables

### Backend preference

The client accepts `backend="web"` or `backend="android"` as an optional,
keyword-only construction argument. The same preference is available as the
root CLI flag `notebooklm --backend ...`, the `notebooklm-mcp --backend ...`
option, and the `notebooklm-server --backend ...` option. Resolution is always:

1. explicit SDK or frontend option;
2. `NOTEBOOKLM_BACKEND`;
3. `web`.

The preference is fixed when a client is constructed. `backend="android"`
installs Android adapters for every public namespace; the read-only
`client.backends` mapping consequently reports `android` for all eleven entries.
`client.raw` is selected with the backend: Web exposes `raw.call(...)`, while
Android exposes `raw.unary(...)` and `raw.unary_stream(...)`. The deprecated root
`client.rpc_call(...)` wrapper still accepts Web `RPCMethod` identifiers through
v0.x; on Android its first call creates a Web compatibility sidecar. That
sidecar never starts a keepalive task. Neither `auto` nor `mobile` is accepted.

Selecting Android does not read credentials during construction. At
`client.open()` / async-context entry it requires both the `android` extra and
a profile-backed `master_token.json`, from which it mints short-lived Android
bearer credentials. Bootstrap that profile with
`notebooklm login --master-token --account EMAIL`; a cookie-only storage file
or `NOTEBOOKLM_AUTH_JSON` is not sufficient for Android. There is no public raw
`master_token=` constructor argument. Android construction and normal open/close
do not build the Web Kernel, run Web cookie recovery, persist Web cookies, or
start Web keepalive. Android `from_storage(...)` uses a read-only, name-only
cookie bootstrap: its homepage token observation stays in memory and it never
pokes, heals, or merges profile cookies. Web-only constructor options (`cookie_saver`,
`cookie_rotator`, `keepalive`, `keepalive_min_interval`, and `limits`) remain
accepted for source compatibility but are ignored when Android is selected.

| Variable | Description | Default |
|----------|-------------|---------|
| `NOTEBOOKLM_HOME` | Base directory for all files | `~/.notebooklm` |
| `NOTEBOOKLM_PROFILE` | Active profile name | `default` |
| `NOTEBOOKLM_BACKEND` | Preferred namespace backend: `web` or `android`. Explicit SDK/CLI/MCP/REST options take precedence. | `web` |
| `NOTEBOOKLM_AUTH_JSON` | Inline authentication JSON (for CI/CD) | - |
| `NOTEBOOKLM_NOTEBOOK` | Default notebook ID for commands without `-n/--notebook` | - |
| `NOTEBOOKLM_HL` | Default interface/output language code (e.g. `en`, `ja`, `zh_Hans`) | `en` |
| `NOTEBOOKLM_BASE_URL` | Gemini Notebook base URL. Constrained to `https://notebook.google.com` (default) or `https://notebooklm.google.com` (pre-rebrand personal, still served) or `https://notebooklm.cloud.google.com` (enterprise) | `https://notebook.google.com` |
| `NOTEBOOKLM_BL` | `bl` (build label) URL parameter for the chat streaming endpoint; override when chasing a regression tied to a specific frontend build snapshot | built-in default in `_env.DEFAULT_BL` (drift-monitored nightly) |
| `NOTEBOOKLM_TRANSPORT` | HTTP transport backend: `httpx` (default) or `curl_cffi` (opt-in browser-TLS impersonation; requires the `curl_cffi` package). Use `curl_cffi` where the default transport is TLS-fingerprint-blocked. | `httpx` |
| `NOTEBOOKLM_LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `WARNING` |
| `NOTEBOOKLM_DEBUG_RPC` | Legacy: Enable RPC debug logging (use `LOG_LEVEL=DEBUG` instead) | `false` |
| `NOTEBOOKLM_DEBUG` | Show untruncated RPC response bodies in error messages instead of the default 80-char preview (verbose; intended for deep debugging) | `0` |
| `NOTEBOOKLM_STRICT_DECODE` | **Retired (ignored since v0.7.0).** Strict decoding is now the only mode: `safe_index` always raises `UnknownRPCMethodError` on schema drift. The former `0` warn-and-fallback opt-out was removed. | (ignored) |
| `NOTEBOOKLM_RPC_OVERRIDES` | JSON object mapping `RPCMethod` enum names to RPC ID strings (community self-patch when Google rotates a method ID; e.g. `{"LIST_NOTEBOOKS":"AbC123"}`) | - |
| `NOTEBOOKLM_REFRESH_CMD` | Optional command (argv list, or shell string with `_USE_SHELL=1`) invoked when auth refresh is required. Must exit `0` after writing a refreshed `storage_state.json`; the parent reloads from disk | - |
| `NOTEBOOKLM_REFRESH_CMD_USE_SHELL` | Opt the `NOTEBOOKLM_REFRESH_CMD` subprocess back into `shell=True` execution. Default `shell=False` (argv list) — set to the literal `1` (only `"1"` is honored — not `true`/`yes`/`on`) when the refresh command requires shell metacharacters | `0` |
| `NOTEBOOKLM_REFRESH_CMD_MIDSESSION` | Opt in (literal `1`) to running `NOTEBOOKLM_REFRESH_CMD` **mid-session** (the L2.5 rung), not only at cold start. Off by default for one release so operators whose commands assume cold-start-only invocation are not surprised inside long-lived servers; flips to default-on a later release ([ADR-0030](adr/0030-one-recovery-ladder.md)) | `0` |
| `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT` | Opt in (literal `1`) to routing the refresh command's captured `stdout`/`stderr` to the redacting **DEBUG** logger. Off by default — the default DEBUG line carries only basename + exit code + byte counts, so promoting the rung into long-lived servers does not widen exposure of whatever the command prints | `0` |
| `NOTEBOOKLM_REFRESH_PROFILE` | Child-process hint set for `NOTEBOOKLM_REFRESH_CMD`; names the resolved profile being refreshed | resolved profile |
| `NOTEBOOKLM_REFRESH_STORAGE_PATH` | Child-process hint set for `NOTEBOOKLM_REFRESH_CMD`; path to the `storage_state.json` file the command must rewrite | resolved storage path |
| `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` | Disable the proactive `accounts.google.com/RotateCookies` poke that refreshes `__Secure-1PSIDTS` ahead of expiry | `0` |
| `NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT` | Seconds the process waits at exit for an in-flight one-time migration of a pre-`v0.x` `context.json` account into `storage_state.json`. It is a ceiling, not a delay: a finished migration exits immediately. Raise it on a machine where the write is slow (antivirus, network storage); set `0` to never wait. An incomplete wait is always reported at `WARNING`, so note that `--quiet` (which raises the floor to `ERROR`) suppresses that signal. Non-numeric, negative, and non-finite values are refused with a warning and the default is used; a finite value above the platform join limit is clamped. | `30` |
| `NOTEBOOKLM_HEADLESS_REAUTH` | Opt in to layer-3 headless re-auth during cold construction and automatic refresh paths. Explicit `from_storage(allow_headless=True)`, `client.refresh_auth(allow_headless=True)`, or `auth refresh --allow-headless` does not require this env var. | `0` |
| `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL` | Optional loopback Chrome DevTools endpoint for layer-3 headless re-auth, e.g. `http://127.0.0.1:9222`. Non-loopback endpoints are ignored for credential safety. | - |
| `NOTEBOOKLM_MCP_TRANSPORT` | MCP server transport for `notebooklm-mcp`: `stdio` or `http` | `stdio` |
| `NOTEBOOKLM_MCP_HOST` | MCP HTTP transport bind host; non-loopback refused unless `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1` | `127.0.0.1` |
| `NOTEBOOKLM_MCP_PORT` | MCP HTTP transport bind port | `9420` |
| `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND` | Allow MCP HTTP transport to bind a non-loopback host. Use only behind a trusted proxy. | `0` |
| `NOTEBOOKLM_MCP_OAUTH_PASSWORD` | Password gating the self-hosted OAuth authorization server that lets claude.ai connect to the remote MCP server (≥16 chars). Set together with `NOTEBOOKLM_MCP_OAUTH_BASE_URL`; both unset → bearer-only. | - |
| `NOTEBOOKLM_MCP_OAUTH_BASE_URL` | Bare public HTTPS origin (no path) the self-hosted OAuth endpoints (`/authorize`, `/token`, `/.well-known/*`) mount under. Required with `NOTEBOOKLM_MCP_OAUTH_PASSWORD`; partial/weak/non-HTTPS config refuses to start. | - |
| `NOTEBOOKLM_MCP_PUBLIC_URL` | Public base URL for the remote MCP file upload/download signed-URL side-channel (falls back to `NOTEBOOKLM_MCP_OAUTH_BASE_URL`). Unset → `source_add type=file` / `artifact_download` return a "not configured" error. | - |
| `NOTEBOOKLM_MCP_TRUST_PROXY` | Trust the proxy-set `CF-Connecting-IP` header as the self-hosted-OAuth login-throttle key. Only enable behind a trusted proxy (e.g. the Cloudflare tunnel); default off keys on the socket peer. | `0` |
| `NOTEBOOKLM_MCP_STRICT_IDS` | Strict IDs-only mode for MCP tools: require a full canonical id for every `notebook`/`source`/`note`/`artifact` reference and reject names, titles, and short id prefixes (fail-fast, deterministic automation). | `0` |
| `NOTEBOOKLM_SERVER_TOKEN` | Bearer token required by every REST `/v1` request. The REST server refuses to start without it. | - |
| `NOTEBOOKLM_SERVER_HOST` | REST server bind host; non-loopback refused unless `NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1` | `127.0.0.1` |
| `NOTEBOOKLM_SERVER_PORT` | REST server bind port | `8000` |
| `NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND` | Allow REST server to bind a non-loopback host. Use only behind a trusted proxy. | `0` |
| `NOTEBOOKLM_SERVER_SOURCE_MUTATION_CONCURRENCY` | Max concurrent REST source create/rename/delete/batch handlers. | `4` |
| `NOTEBOOKLM_SERVER_SOURCE_WAIT_CONCURRENCY` | Max concurrent REST source wait handlers. | `4` |
| `NOTEBOOKLM_SERVER_GENERATION_CONCURRENCY` | Max concurrent REST artifact generation/retry handlers. | `2` |
| `NOTEBOOKLM_SERVER_DOWNLOAD_CONCURRENCY` | Max concurrent REST artifact download handlers. | `2` |
| `NOTEBOOKLM_SERVER_RESEARCH_CONCURRENCY` | Max concurrent REST research start/cancel/import handlers. | `2` |
| `NOTEBOOKLM_SERVER_CHAT_CONCURRENCY` | Max concurrent REST blocking chat ask handlers. | `4` |
| `NOTEBOOKLM_QUIET_DEPRECATIONS` | Suppress the project's public-API `DeprecationWarning`s (the one-off warnings routed through `warn_deprecated`, e.g. awaiting `from_storage(...)`). Set to a truthy value (`1` / `true` / `yes` / `on`, case-insensitive) to silence them; see `docs/deprecations.md`. | (warnings emitted) |
| `NOTEBOOKLM_FUTURE_ERRORS` | **Retired (removed in v0.8.0; ignored).** It was the v0.7.0 forward-compat preview gate for the v0.8.0 error contract; now that every break it staged is the default, the flag is a no-op — setting it has no effect. See `docs/deprecations.md`. | (ignored) |
| `NOTEBOOKLM_VCR_RECORD_ERRORS` | Synthetic-error injection mode for VCR test cassettes (`429`, `5xx`, `expired_csrf`) | - |

### Public config API vs internal resolvers

`src/notebooklm/_env.py` owns internal environment/default resolution for
runtime behavior. It reads process environment variables and contains internal
defaults such as `DEFAULT_BL` for the chat streaming build label.

`notebooklm.config` is the stable public import surface. It intentionally
re-exports only the supported endpoint/language helpers:
`DEFAULT_BASE_URL`, `ENTERPRISE_BASE_HOST`, `get_base_host`, `get_base_url`,
`get_default_language`, and `PERSONAL_BASE_HOST`. Existing imports
from `notebooklm.config` remain supported; internal-only `_env` names should
not be imported by downstream code.

### Env vars and precedence

Every `NOTEBOOKLM_*` variable read by the library and CLI, in one place. CLI
flags always win over env vars; env vars win over persisted profile config /
context; built-in defaults are the last fallback. The "Resolved by" column
points at the canonical resolver so the precedence rule for each variable can
be audited from one location.

| Variable | Purpose | Resolution order (highest → lowest) | Resolved by |
|----------|---------|-------------------------------------|-------------|
| `NOTEBOOKLM_PROFILE` | Active profile name. Selects which `~/.notebooklm/profiles/<name>/` directory backs storage and context. | `-p/--profile` flag → `NOTEBOOKLM_PROFILE` → `default_profile` from `~/.notebooklm/config.json` → `default` | `paths.resolve_profile` |
| `NOTEBOOKLM_AUTH_JSON` | Inline `storage_state.json` payload for CI/CD; bypasses on-disk profile storage entirely. | `--storage` flag → `NOTEBOOKLM_AUTH_JSON` → profile-aware `storage_state.json` → legacy fallback | `auth.load_auth_from_storage` |
| `NOTEBOOKLM_HOME` | Base directory for all per-profile files. | `NOTEBOOKLM_HOME` → `~/.notebooklm` | `paths.get_home_dir` |
| `NOTEBOOKLM_HL` | Default interface/output language for `generate <kind>` and the `hl` query parameter on every batchexecute RPC. | `--language` flag → `NOTEBOOKLM_HL` → `language` value from **global** `~/.notebooklm/config.json` (NOT per-profile) → `en` | `language.resolve_hl` |
| `NOTEBOOKLM_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` floor for the `notebooklm` package logger. | `--quiet` flag (forces `ERROR`) → `-v/-vv` flags (force `INFO`/`DEBUG`) → `NOTEBOOKLM_DEBUG_RPC=1` (forces `DEBUG`) → `NOTEBOOKLM_LOG_LEVEL` → `WARNING` | `_logging.configure_logging` + `notebooklm_cli.cli` |
| `NOTEBOOKLM_DEBUG_RPC` | Legacy alias that sets the package logger to `DEBUG`. Prefer `NOTEBOOKLM_LOG_LEVEL=DEBUG` for new code. | (See `NOTEBOOKLM_LOG_LEVEL`.) | `_logging.configure_logging` |
| `NOTEBOOKLM_NOTEBOOK` | Default notebook ID when no `-n/--notebook` flag is passed. Composes with `notebooklm use <id>` so per-shell overrides do not clobber the persisted active-notebook context. | `-n/--notebook` flag → `NOTEBOOKLM_NOTEBOOK` → active context (from `notebooklm use`) → error | `cli.helpers.require_notebook` (Click also reads it natively via `cli/options.py:notebook_option`'s `envvar=`) |
| `NOTEBOOKLM_RPC_OVERRIDES` | **JSON object** mapping `RPCMethod` enum names to RPC ID strings (e.g. `{"LIST_NOTEBOOKS": "AbC123"}`). Overrides runtime RPC IDs — community self-patch when Google rotates a method ID. Empty string / unset disables the mechanism; invalid JSON or non-object payloads emit a `WARNING` and are ignored. | Process env, evaluated per RPC resolve (cached on the raw env string). | `notebooklm._web.wire.overrides._parse_rpc_overrides` |
| `NOTEBOOKLM_QUIET_DEPRECATIONS` | Suppress the project's public-API `DeprecationWarning`s — the one-off warnings routed through `src/notebooklm/_deprecation.py::warn_deprecated` (e.g. awaiting `from_storage(...)`). Set to a truthy value (`1` / `true` / `yes` / `on`) to silence them. See `docs/deprecations.md`. | (warnings emitted) | `_deprecation._deprecations_quiet` / `deprecations_quiet` |
| `NOTEBOOKLM_FUTURE_ERRORS` | **Retired (removed in v0.8.0; ignored).** It was the v0.7.0 forward-compat preview gate for the v0.8.0 error contract (ADR-0019 / umbrella [#1346](https://github.com/teng-lin/notebooklm-py/issues/1346)). Now that every break it staged — `get()` raising `*NotFoundError`, the attribute-only typed returns, the removed `interval=` alias, the bool→`None` returns, the refusal-raises, and the mutate-existing fail-loud — is the default, the flag is a **no-op**: setting it has no effect. See `docs/deprecations.md`. | (ignored) | — |
| `NOTEBOOKLM_STRICT_DECODE` | **Retired (ignored since v0.7.0).** Strict decoding is the only mode — `safe_index` always raises `UnknownRPCMethodError` on schema drift. The former `0` warn-and-fallback opt-out was removed; setting the variable has no effect. | (ignored) | — |
| `NOTEBOOKLM_BASE_URL` | Gemini Notebook base URL. Constrained to `https://notebook.google.com` (default) or `https://notebooklm.google.com` (pre-rebrand personal host, still served — the documented rollback lever; if auth fails after switching, re-run `notebooklm login --fresh`) or `https://notebooklm.cloud.google.com` (enterprise); other schemes/hosts/paths raise `ValueError`. | Process env on every base-URL lookup. | `_env.get_base_url` |
| `NOTEBOOKLM_BL` | `bl` (build label) URL parameter sent on the chat streaming endpoint (`ChatAPI.ask`). Pins the frontend build the request is attributed to. The built-in `_env.DEFAULT_BL` is watched by the nightly canary's [build-label lane](rpc-development.md#build-label-lane-bl--_envdefault_bl), which compares it against the label Google actually serves; an override here does not change that verdict. | Process env on every chat stream call; whitespace-only falls back to `_env.DEFAULT_BL`. | `_env.get_default_bl` |
| `NOTEBOOKLM_DEBUG` | When `1`, RPC error messages include the **full** untruncated response body instead of the default 80-char preview. Verbose; intended for deep debugging only. | Process env on each error formatting call. | `exceptions._truncate_response_preview` |
| `NOTEBOOKLM_REFRESH_CMD` | Optional command invoked when auth refresh is required. Must exit `0` after writing a refreshed `storage_state.json`; the parent reloads cookies from disk. Stdout/stderr are not parsed (only surfaced in the non-zero-exit error message). Parsing honors `NOTEBOOKLM_REFRESH_CMD_USE_SHELL`. | Process env on each refresh subprocess spawn. | `auth` refresh-spawn helper (constant `NOTEBOOKLM_REFRESH_CMD_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_REFRESH_CMD_USE_SHELL` | Opt the optional `NOTEBOOKLM_REFRESH_CMD` subprocess back into `shell=True`. Default `shell=False` parses the command with `shlex.split` and invokes it as an argv list (safer; resists shell-injection footguns when the env var is sourced from CI configs or container env files). | Process env on each refresh subprocess spawn. | `auth` refresh-spawn helper (constant `NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_REFRESH_CMD_MIDSESSION` | Opt in (literal `1`) to firing `NOTEBOOKLM_REFRESH_CMD` mid-session (the L2.5 rung of the unified ladder), not just at cold start. Default off **for one release** — the rung was cold-start-only before, and enabling it inside a long-lived server changes when the operator's command runs; flips to default-on a later release. See [ADR-0030](adr/0030-one-recovery-ladder.md). | Read on the mid-session recovery path. | `auth._run_refresh_cmd` (constant `NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT` | Opt in (literal `1`) to routing the refresh command's captured `stdout`/`stderr` into the redacting DEBUG logger. Default off: because the mid-session promotion widens exposure of whatever the command prints inside long-lived servers, the default DEBUG line carries only basename + exit code + byte counts. | Process env / logging path on each refresh subprocess spawn. | `auth._run_refresh_cmd` (constant `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_REFRESH_PROFILE` | Child env var injected into `NOTEBOOKLM_REFRESH_CMD`; names the resolved NotebookLM profile that is being refreshed. Refresh scripts may read it, but setting it in the parent shell does not select the profile. | Set by `auth` refresh-spawn helper from the resolved profile. | `auth._run_refresh_cmd` |
| `NOTEBOOKLM_REFRESH_STORAGE_PATH` | Child env var injected into `NOTEBOOKLM_REFRESH_CMD`; points to the `storage_state.json` file the command must rewrite before exiting `0`. Refresh scripts may read it, but setting it in the parent shell does not select storage. | Set by `auth` refresh-spawn helper from the explicit storage path or profile-aware storage path. | `auth._run_refresh_cmd` |
| `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` | When `1`, disable the proactive `accounts.google.com/RotateCookies` poke that refreshes `__Secure-1PSIDTS` ahead of expiry. Useful when running behind a proxy that rejects the extra request, or in offline test fixtures. | Process env on every keepalive check. | `auth` keepalive guards (constant `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_PROMOTION_EXIT_TIMEOUT` | Seconds the process waits at exit for an in-flight one-time migration of a pre-`v0.x` `context.json` account into `storage_state.json`. A ceiling shared by all outstanding writers, not a delay — a finished migration exits immediately. `0` never waits. Unset, empty, or whitespace-only falls back to the default; non-numeric, negative, and non-finite (`inf`/`nan`) values are refused with a `WARNING` and the default is used; a finite value above `threading.TIMEOUT_MAX` is clamped. An incomplete wait is always reported at `WARNING` (so `--quiet`, which forces `ERROR`, suppresses it). | Process env, read once per process at exit → `30.0` | `_auth.profile_migration._promotion_exit_timeout` |
| `NOTEBOOKLM_HEADLESS_REAUTH` | Opt in to layer-3 headless re-auth for cold construction and automatic refresh paths. Explicit Python/CLI `allow_headless` flags do not require the env var. | Literal `1` enables; all other values disabled. | `_browser.headless_reauth.headless_reauth_env_enabled` |
| `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL` | Optional Chrome DevTools Protocol endpoint for layer-3 headless re-auth. Must be loopback (`127.0.0.1`, `::1`, or `localhost`); remote endpoints are ignored because CDP is account-equivalent. | Explicit function argument → env var → no CDP arm. | `_browser.headless_reauth.resolve_cdp_url` |
| `NOTEBOOKLM_MCP_TRANSPORT` | Default transport for `notebooklm-mcp`: `stdio` or `http`. CLI `--transport` wins. | `--transport` flag → env var → `stdio` | `mcp.__main__._build_parser` |
| `NOTEBOOKLM_MCP_HOST` | HTTP bind host for `notebooklm-mcp --transport http`. Non-loopback refused unless `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1`. | `--host` flag → env var → `127.0.0.1` | `mcp.__main__._build_parser` / `_serving.check_bind_allowed` |
| `NOTEBOOKLM_MCP_PORT` | HTTP bind port for `notebooklm-mcp --transport http`. | `--port` flag → env var → `9420` | `mcp.__main__._build_parser` / `_resolve_port` |
| `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND` | Allow MCP HTTP transport to bind a non-loopback host. Use only behind a trusted proxy. | Literal `1` enables; all other values disabled. | `mcp.__main__._check_http_bind_allowed` → `_serving.check_bind_allowed` |
| `NOTEBOOKLM_MCP_TRUST_PROXY` | Trust the proxy-set `CF-Connecting-IP` header as the self-hosted-OAuth login-throttle key. Enable only behind a trusted proxy (e.g. the Cloudflare tunnel); default off keys the throttle on the socket peer. | Literal `1` enables; all other values disabled. | `mcp._oauth.get_oauth_config` / `_client_ip` |
| `NOTEBOOKLM_MCP_STRICT_IDS` | Strict IDs-only mode: MCP `notebook`/`source`/`note`/`artifact` references must be a full canonical id; names, titles, and short id prefixes are rejected before any list call (deterministic automation). Off by default → default name/prefix/title resolution is unchanged. | Literal `1` enables; all other values disabled. | `mcp._resolve._strict_ids_enabled` |
| `NOTEBOOKLM_SERVER_TOKEN` | Bearer token required by every REST `/v1` request. The server refuses to start when unset/empty. | `--token` flag → env var → startup failure | `server.__main__._check_token_configured` / `server._auth.require_auth` |
| `NOTEBOOKLM_SERVER_HOST` | REST server bind host. Non-loopback refused unless `NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1`. | `--host` flag → env var → `127.0.0.1` | `server.__main__._build_parser` / `_serving.check_bind_allowed` |
| `NOTEBOOKLM_SERVER_PORT` | REST server bind port. | `--port` flag → env var → `8000` | `server.__main__._build_parser` / `_resolve_port` |
| `NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND` | Allow REST server to bind a non-loopback host. Use only behind a trusted proxy. | Literal `1` enables; all other values disabled. | `server.__main__._check_bind_allowed` → `_serving.check_bind_allowed` |
| `NOTEBOOKLM_SERVER_SOURCE_MUTATION_CONCURRENCY` | Max concurrent REST source create/rename/delete/batch handlers. | Env var → `4`; blank uses default; must be integer `>=1`. | `server._limits.ServerLimiters.from_env` |
| `NOTEBOOKLM_SERVER_SOURCE_WAIT_CONCURRENCY` | Max concurrent REST source wait handlers. | Env var → `4`; blank uses default; must be integer `>=1`. | `server._limits.ServerLimiters.from_env` |
| `NOTEBOOKLM_SERVER_GENERATION_CONCURRENCY` | Max concurrent REST artifact generation/retry handlers. | Env var → `2`; blank uses default; must be integer `>=1`. | `server._limits.ServerLimiters.from_env` |
| `NOTEBOOKLM_SERVER_DOWNLOAD_CONCURRENCY` | Max concurrent REST artifact download handlers. | Env var → `2`; blank uses default; must be integer `>=1`. | `server._limits.ServerLimiters.from_env` |
| `NOTEBOOKLM_SERVER_RESEARCH_CONCURRENCY` | Max concurrent REST research start/cancel/import handlers. | Env var → `2`; blank uses default; must be integer `>=1`. | `server._limits.ServerLimiters.from_env` |
| `NOTEBOOKLM_SERVER_CHAT_CONCURRENCY` | Max concurrent REST blocking chat ask handlers. | Env var → `4`; blank uses default; must be integer `>=1`. | `server._limits.ServerLimiters.from_env` |
| `NOTEBOOKLM_VCR_RECORD_ERRORS` | Synthetic-error injection mode for VCR test cassettes. Lowercase-normalized; valid values are `429` (rate limit), `5xx` (server error), or `expired_csrf` (CSRF token expiration). Used to record synthetic error cassettes under VCR. | Process env on each request, evaluated by `ErrorInjectionMiddleware` to intercept and synthesize failures. | `_error_injection._get_error_injection_mode` |

**Boolean handling.** `NOTEBOOKLM_DEBUG_RPC` treats `1` / `true` / `yes`
(case-insensitive) as truthy; everything else is falsy.
`NOTEBOOKLM_STRICT_DECODE` is ignored as of v0.7.0 (strict decoding is the
only mode); no value changes decoder behavior.
`NOTEBOOKLM_QUIET_DEPRECATIONS` treats `1` / `true` / `yes` / `on`
(case-insensitive) as truthy; any other value (including unset) leaves
deprecation warnings enabled. It silences the project's public-API
`DeprecationWarning`s — the `get()`-returns-`None` warning and deprecated
keyword aliases (e.g. `ResearchAPI.wait_for_completion`'s legacy `interval=`) —
without changing their behavior.
`NOTEBOOKLM_NOTEBOOK` is treated as unset when empty or whitespace-only so a
bare `export NOTEBOOKLM_NOTEBOOK=` does not block `notebooklm use` /
`-n/--notebook` from resolving.

**The `--quiet` global flag.** `notebooklm --quiet <subcommand>` suppresses
status prose and raises the `notebooklm` package logger floor to `ERROR` for
the duration of one invocation, so cron and CI logs stay clean while real
failures still surface. Structured `--json` payloads are still emitted. It is
mutually exclusive with `-v/-vv` — combining the two raises a `UsageError`
(exit `2`) since the resolved log levels conflict (`ERROR` vs `INFO`/`DEBUG`).
For shell-wide / always-on log suppression, use `NOTEBOOKLM_LOG_LEVEL`.

### NOTEBOOKLM_HOME

Relocates all configuration files to a custom directory:

```bash
export NOTEBOOKLM_HOME=/custom/path

# All files now go here:
# /custom/path/profiles/<profile>/storage_state.json
# /custom/path/profiles/<profile>/context.json
# /custom/path/profiles/<profile>/browser_profile/
```

**Use cases:**
- Per-project isolation
- Custom storage locations

### NOTEBOOKLM_PROFILE

Selects the active profile without changing the persisted default:

```bash
export NOTEBOOKLM_PROFILE=work
notebooklm list   # Uses ~/.notebooklm/profiles/work/
```

Equivalent to passing `-p work` on every command. The CLI flag takes precedence over the env var.

The **persisted default** is read from the `default_profile` key of
`~/.notebooklm/config.json` (set via `notebooklm profile switch <name>`). When
neither a `-p/--profile` flag nor `NOTEBOOKLM_PROFILE` is set, `paths.resolve_profile`
falls back to this value (and finally to `"default"` if `config.json` doesn't
exist or has no `default_profile` key).

### NOTEBOOKLM_AUTH_JSON

Provides authentication inline without writing files. Ideal for CI/CD:

```bash
export NOTEBOOKLM_AUTH_JSON='{"cookies":[...]}'
notebooklm list  # Works without any file on disk
```

**Precedence:**
1. `--storage` CLI flag (highest)
2. `NOTEBOOKLM_AUTH_JSON` environment variable
3. Profile-aware path: `$NOTEBOOKLM_HOME/profiles/<profile>/storage_state.json`
4. `~/.notebooklm/profiles/default/storage_state.json` (default)
5. `~/.notebooklm/storage_state.json` (legacy fallback)

**Note:** Cannot run `notebooklm login` when `NOTEBOOKLM_AUTH_JSON` is set.

### `NOTEBOOKLM_REFRESH_CMD`

Optional. If set, this command is invoked when an auth refresh is required —
replacing the default browser-cookie path. The contract is **exit-code based**:

1. The command must exit `0`.
2. On exit, it must have written a refreshed `storage_state.json` at the
   path in `NOTEBOOKLM_REFRESH_STORAGE_PATH`. The parent sets this child
   env var to the explicit storage path or the resolved profile-aware
   storage path before spawning the command.

The parent then reloads cookies from disk and retries the token fetch. Stdout
and stderr are **not** parsed — they are only captured for inclusion in the
error message when the command exits non-zero. Read by the
`NOTEBOOKLM_REFRESH_CMD_ENV` constant in `notebooklm.auth`.
The child process also receives `NOTEBOOKLM_REFRESH_PROFILE` with the resolved
profile name.

See also `NOTEBOOKLM_REFRESH_CMD_USE_SHELL` to opt back into `shell=True`
parsing.

By default the command fires only at **active cold start** — client construction
/ `AuthTokens.from_storage`, which run the recovery ladder when the stored
cookies are dead. **Passive readiness probes do NOT invoke it**: `auth check
--test --passive` (and other passive token fetches) use the strict, no-recovery
loader and never enter the refresh path, so a passive check reports expiry
without spawning the command. Set `NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1` to also
fire it **mid-session** (the L2.5 rung of the unified recovery ladder), e.g.
inside a long-lived server that has been running past cookie expiry. This is **opt-in for
one release** and flips to default-on afterward; enable it only once you have
confirmed your command is safe to invoke while the client is live. When you need
to see what the command printed, set `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT=1` to
route its captured `stdout`/`stderr` into the redacting DEBUG logger — the
default DEBUG line records only the command basename, exit code, and byte
counts. See [ADR-0030](adr/0030-one-recovery-ladder.md) for the ladder design.

### NOTEBOOKLM_HL

Sets the default interface/output language used by the client. The value is
passed as the `hl` query parameter on every batchexecute RPC call and is used by
the `generate audio|video|slide-deck|infographic|data-table|mind-map|report`
commands. In the Python API, omitted `language` on `ArtifactsAPI.generate_*`
keeps the historical `"en"` artifact default; pass `language=None` to opt in to
this `NOTEBOOKLM_HL` resolver.

```bash
export NOTEBOOKLM_HL=ja
notebooklm generate audio "deep dive"   # Japanese audio overview
```

Surrounding whitespace is stripped; an empty or whitespace-only value falls
back to `en`. For the generate commands, the resolution order is:

1. `--language` CLI flag
2. `NOTEBOOKLM_HL` environment variable
3. `language` value from the **global** `~/.notebooklm/config.json` (set via
   `notebooklm language set <code>`). The language is stored once per
   `NOTEBOOKLM_HOME`, **not** per profile — switching `notebooklm -p work`
   does not switch the configured language. See
   `src/notebooklm/cli/language_cmd.py` for the resolver and
   `src/notebooklm/paths.py` for the storage location.
4. `en` (built-in default)

### NOTEBOOKLM_QUIET_DEPRECATIONS

Suppresses the project's public-API `DeprecationWarning`s while you migrate. Set
it to a truthy value (`1`, `true`, `yes`, or `on`, case-insensitive) to silence
them; any other value (including unset) leaves them enabled.

It gates the one-off deprecation warnings routed through
`src/notebooklm/_deprecation.py::warn_deprecated` — e.g. awaiting
`NotebookLMClient.from_storage(...)` instead of using the `async with` form. (The
v0.7.0 error-contract runways it also gated — the `get()`-returns-`None` warning,
the `wait_for_completion(interval=...)` alias, and the dict-subscript bridge — all
**completed their removal in v0.8.0**, so those warnings no longer exist; see
[`deprecations.md`](deprecations.md).)

The helper that reads this variable is
`notebooklm._deprecation._deprecations_quiet` (public alias
`deprecations_quiet`).

```bash
# Silence the project's public-API deprecation warnings while migrating
export NOTEBOOKLM_QUIET_DEPRECATIONS=1
```

> Note: this variable does **not** affect `source add --mime-type` /
> `client.sources.add_file(mime_type=...)` — `mime_type` is a supported
> parameter and emits no warning.

### NOTEBOOKLM_FUTURE_ERRORS (removed in v0.8.0)

**Removed.** This was the v0.7.0 forward-compat preview gate for the v0.8.0 error
contract (ADR-0019 / umbrella
[#1346](https://github.com/teng-lin/notebooklm-py/issues/1346)): setting it made
the v0.7.0 warn-runways adopt their v0.8.0 raise-target early so you could test
forward-compatibility before the breaking flips shipped. v0.8.0 makes every one
of those flips the default — `get()` raising `*NotFoundError` on a miss, the
attribute-only typed returns, the removed `interval=` alias, the bool→`None`
returns, the refusal-raises, and the mutate-existing fail-loud — so the flag and
its resolver were deleted. **Setting `NOTEBOOKLM_FUTURE_ERRORS` now has no
effect.** Remove it from your environment / CI config. See
[`deprecations.md`](deprecations.md) for the full Removed-in-v0.8.0 table.

### Timeouts

Most batchexecute RPCs issued by the client (whether through `NotebookLMClient`
or any of the CLI commands) use a **30-second** HTTP request timeout by default,
with a tighter **10-second** connection-establishment timeout. The shorter
connect timeout helps surface network-level issues quickly while the read
timeout accommodates slow server responses. The timeout is exposed as a
constructor argument on `NotebookLMClient` (`timeout=`) for callers that need to
tune it per-workload — see the `DEFAULT_TIMEOUT` / `DEFAULT_CONNECT_TIMEOUT`
constants in `src/notebooklm/_runtime/config.py`.

The chat streaming endpoint (`ChatAPI.ask`) also exposes a separate per-read
silence window (`chat_timeout=`). Left unset it is **`max(180 s, timeout=)`** —
180 seconds because shared notebooks can be slow to send the first streamed chat
byte, floored at `timeout=` so a larger configured budget still reaches chat (see
[below](#how-the-per-rpc-windows-compose-with-timeout)); fast metadata RPCs stay
on the normal **30-second** timeout. A chat read timeout means the server
sent no stream bytes for that window, either before the first byte or between
chunks; it does not mean total generation time exceeded 30 seconds. Pass
`chat_timeout=None` to inherit the normal client timeout for chat. The CLI
`ask --request-timeout N` flag overrides both values for that invocation.

`IMPORT_RESEARCH` (`research.import_sources`) has a third window: the server
ingests every requested entry before answering one RPC, so the window scales
with batch size — **60 s + 3 s per requested source, capped at 240 s** — and is
tunable outright via `import_research_timeout=`.

#### How the per-RPC windows compose with `timeout=`

`timeout=` is the *base* read budget for every RPC. The two built-in per-RPC
windows above are **defaults, not caps**: they only ever lengthen that base,
never shorten it. So `NotebookLMClient(auth, timeout=600)` really does buy 600
seconds everywhere, including chat and IMPORT_RESEARCH (before this rule landed
they silently clamped such a client back to 180 s / 240 s — issue #2205).

| Construction | chat window | IMPORT_RESEARCH window (1 source) |
| --- | --- | --- |
| `NotebookLMClient(auth)` | 180 s | 63 s |
| `NotebookLMClient(auth, timeout=600)` | 600 s | 600 s |
| `NotebookLMClient(auth, timeout=600, chat_timeout=10)` | 10 s | 600 s |
| `NotebookLMClient(auth, import_research_timeout=900)` | 180 s | 900 s |

An explicitly passed `chat_timeout=` / `import_research_timeout=` is the
caller's final word and is used as given — including a value *below* `timeout=`,
so deliberately fast failure stays expressible. Only the untouched defaults
compose. Both kwargs read identically:

| value | meaning |
| --- | --- |
| unset | the built-in window, floored at `timeout=` |
| a number | exactly that window, replacing the built-in and the floor |
| `None` | inherit `timeout=` verbatim, with no per-RPC window at all |

A non-positive or non-finite value for either raises `ValueError` at
construction rather than silently producing a window that times out instantly.

Independently, an IMPORT_RESEARCH attempt made by
`import_sources_with_verification` is clamped to whatever is left of that call's
own `max_elapsed` retry budget, so a late retry cannot be *granted* a window
larger than the budget it has left. Note what that does and does not promise:
every timeout here is an `httpx` slot, and the read slot is an inactivity limit
between socket reads — so `max_elapsed` bounds when a new attempt may *start*,
not the wall-clock duration of one already in flight. Enforcing the latter would
mean cancelling an in-flight non-idempotent POST, which risks duplicated sources
the client can no longer see. When less than 10 seconds of that budget remains — too little for an
attempt to outlast connection establishment — the loop stops instead of sending
one: `IMPORT_RESEARCH` is non-idempotent, so an attempt whose result the client
cannot observe can still commit sources server-side and duplicate them. The
first attempt is exempt, so `max_elapsed=0` still means "try once".

### Decoder strictness

NotebookLM's batchexecute responses are obfuscated, undocumented, and reshaped
by Google without notice. The decoder uses a shared `safe_index` helper to walk
nested response payloads. When it can't descend (an index is out of range, or
the value at a step isn't indexable), it **raises**
`UnknownRPCMethodError` (a subclass of `DecodingError` / `RPCError`) with
structured `method_id`, `path`, `source`, and `data_at_failure` attributes.

Strict decoding is the only mode. The legacy `NOTEBOOKLM_STRICT_DECODE=0`
warn-and-return-`None` opt-out (which emitted a `DeprecationWarning` through
v0.5.0/v0.6.0) was **retired in v0.7.0**; the env var is now ignored. The
strict contract surfaces real schema drift (Google rotating a response shape)
as a typed exception instead of a silent `None` return, so callers that
previously treated `None` as a sentinel must handle `UnknownRPCMethodError`.

The same `UnknownRPCMethodError` is also raised by `decode_response()` when the
batchexecute response contains RPC IDs but not the one the call requested
(typically a sign that Google rotated the method ID).

> Background and rationale for the flip: see
> [`docs/adr/0011-schema-validation-policy.md`](adr/0011-schema-validation-policy.md).

## CLI Options

### Global Options

| Option | Description | Default |
|--------|-------------|---------|
| `--storage PATH` | Path to storage_state.json | `$NOTEBOOKLM_HOME/profiles/<profile>/storage_state.json` |
| `-p, --profile NAME` | Use a named profile | Active profile or `default` |
| `-v, --verbose` | Enable verbose output (`-v` for INFO, `-vv` for DEBUG) | - |
| `--quiet` | Suppress status output and INFO/WARN logs (only errors survive). Mutually exclusive with `-v`. | - |
| `--version` | Show version | - |
| `--help` | Show help | - |

### Viewing Configuration

See where your configuration files are located:

```bash
notebooklm status --paths
```

Output:
```
                 Configuration Paths
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ File             ┃ Path                                     ┃ Source    ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ Profile          │ default                                  │ default   │
│ Home Directory   │ /home/user/.notebooklm                   │ default   │
│ Profile Directory│ /home/user/.notebooklm/profiles/default  │           │
│ Storage State    │ .../profiles/default/storage_state.json  │           │
│ Context          │ .../profiles/default/context.json        │           │
│ Browser Profile  │ .../profiles/default/browser_profile     │           │
└──────────────────┴──────────────────────────────────────────┴───────────┘
```

## Session Management

### Session Lifetime

Authentication sessions are tied to Google's cookie expiration:
- Sessions typically last several days to weeks
- Google may invalidate sessions for security reasons
- Rate limiting or suspicious activity can trigger earlier expiration

### Refreshing Sessions

**Automatic Refresh:** CSRF tokens and session IDs are automatically refreshed when authentication errors are detected. This handles most "session expired" errors transparently.

**Manual Re-authentication:** If your session cookies have fully expired (automatic refresh won't help), re-authenticate:

```bash
notebooklm login
```

### Multiple Accounts

**Profiles (recommended):** Use named profiles to manage multiple Google accounts under a single home directory:

```bash
# Create and authenticate profiles
notebooklm profile create work
notebooklm -p work login
notebooklm -p work list

notebooklm profile create personal
notebooklm -p personal login
notebooklm -p personal list

# Switch the active profile
notebooklm profile switch work
notebooklm list   # Uses work profile

# List all profiles
notebooklm profile list

# Use env var for session-wide override
export NOTEBOOKLM_PROFILE=personal
notebooklm list   # Uses personal profile
```

Each profile stores its own `storage_state.json`, `context.json`, and `browser_profile/` under `~/.notebooklm/profiles/<name>/`.

**Alternative: `NOTEBOOKLM_HOME`** still works for full directory-level isolation:

```bash
export NOTEBOOKLM_HOME=~/.notebooklm-work
notebooklm login
```

**One-off override with `--storage`:**

```bash
notebooklm --storage /path/to/storage_state.json list
```

When `--storage <path>` is set, **two different context files** are used —
they are NOT the same file:

- **Notebook / conversation context** lives at a *suffixed* file
  `<path>.context.json` (`storage_path.with_suffix(storage_path.suffix + ".context.json")`,
  see `paths.get_context_path`). Two `--storage` invocations against different
  files cannot see each other's selected notebook, and neither pollutes the
  default profile context.
- **Account-routing metadata** (the `notebooklm.account` object — `authuser`
  index and optional `email`) lives in-band inside the selected
  `storage_state.json`. This keeps copied files and `NOTEBOOKLM_AUTH_JSON`
  secrets bound to the same Google account route as the original profile.
- **Persistent browser state** lives beside the selected storage file. A path
  named `storage_state.json` uses `browser_profile/`; other filenames append
  `.browser_profile` to the full filename when it fits, or use a stable
  canonical-path hash when it does not. Login, headless re-auth, doctor's L3
  readiness row, `status --paths`, and logout all resolve the same
  storage-specific directory. Recursive deletion by `login --fresh` or logout
  requires either the ownership marker or a canonical managed profile layout.

Run `notebooklm --storage <path> status --paths` to see exactly which
storage, context, and browser-profile paths are in use.

## CI/CD Configuration

### GitHub Actions (Recommended)

Use `NOTEBOOKLM_AUTH_JSON` for secure, file-free authentication:

```yaml
jobs:
  notebook-task:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install notebooklm-py
        run: pip install notebooklm-py

      # Pre-flight: fail fast and loud on missing/expired auth.
      # `auth check --json` returns exit 0 even when status is "error"; --test makes the network
      # call needed to detect expired cookies, and the `jq -e` flag converts a non-"ok" status
      # into a non-zero exit code so the runner step actually fails.
      - name: Verify auth (fail-fast on expired cookies)
        env:
          NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_JSON }}
        run: notebooklm auth check --test --json | jq -e '.status == "ok"'

      - name: List notebooks
        env:
          NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_JSON }}
        run: notebooklm list
```

**Benefits:**
- No file writes needed
- Secret stays in memory only
- Clean, simple workflow

### Obtaining the Secret Value

1. Run `notebooklm login` locally
2. If the file was created by an older Playwright login and lacks `notebooklm.account`, run `notebooklm auth refresh` to repair single-account states or re-login with `notebooklm login --browser-cookies <browser> --account EMAIL` for multi-account states
3. Copy the contents of `~/.notebooklm/profiles/default/storage_state.json` (the canonical write location; the legacy `~/.notebooklm/storage_state.json` is only read as a fallback)
4. Add as a GitHub repository secret named `NOTEBOOKLM_AUTH_JSON` (see [installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci) for trailing-newline + ephemeral-runner refresh notes)

### Alternative: File-Based Auth

If you prefer file-based authentication:

```yaml
- name: Setup NotebookLM auth
  run: |
    mkdir -p ~/.notebooklm/profiles/default
    echo "${{ secrets.NOTEBOOKLM_AUTH_JSON }}" > ~/.notebooklm/profiles/default/storage_state.json
    chmod 600 ~/.notebooklm/profiles/default/storage_state.json

- name: List notebooks
  run: notebooklm list
```

For profile-specific CI auth:

```yaml
- name: Setup work profile auth
  run: |
    mkdir -p ~/.notebooklm/profiles/work
    echo "${{ secrets.WORK_AUTH_JSON }}" > ~/.notebooklm/profiles/work/storage_state.json
    chmod 600 ~/.notebooklm/profiles/work/storage_state.json

- name: List notebooks (work)
  run: notebooklm -p work list
```

### Session Expiration

CSRF tokens are automatically refreshed during API calls. However, the underlying session cookies still expire. For long-running CI pipelines:
- Update the `NOTEBOOKLM_AUTH_JSON` secret every 1-2 weeks
- Monitor for persistent auth failures (these indicate cookie expiration)

## Debugging

### Enable Verbose Output

Some commands support verbose output via Rich console:

```bash
# Most errors are printed to stderr with details
notebooklm list 2>&1 | cat
```

### Enable RPC Debug Logging

```bash
NOTEBOOKLM_DEBUG_RPC=1 notebooklm list
```

### Logger namespace compatibility

Runtime transport and middleware logs still use the historical
`notebooklm._core` logger key via `CORE_LOGGER_NAME`. That name is a
compatibility logging contract for existing filters and tests; it does not
mean the deleted `_core.py` module or a concrete `Session` owner still exists.

### Check Authentication

Verify your session is working:

```bash
# Should list notebooks or show empty list
notebooklm list

# If you see "Unauthorized" or redirect errors, re-login
notebooklm login
```

### Check Configuration Paths

```bash
# See where files are being read from
notebooklm status --paths
```

### Network Issues

The CLI uses `httpx` for HTTP requests. Common issues:

- **Timeout**: Google's API can be slow; large operations may time out
- **SSL errors**: Ensure your system certificates are up to date
- **Proxy**: Set standard environment variables (`HTTP_PROXY`, `HTTPS_PROXY`) if needed

## Platform Notes

### macOS

Works out of the box. Chromium is downloaded automatically by Playwright.

### Linux

For Playwright system dependencies and the Chromium install on Debian/Ubuntu, see [docs/installation.md#platform-notes](installation.md#platform-notes) (and [troubleshooting.md#linux](troubleshooting.md#linux) if you hit `TypeError: onExit is not a function`).

### Windows

Works with PowerShell or CMD. Use backslashes for paths:

```powershell
notebooklm --storage C:\Users\Name\.notebooklm\storage_state.json list
```

Or set environment variable:

```powershell
$env:NOTEBOOKLM_HOME = "C:\Users\Name\custom-notebooklm"
notebooklm list
```

### WSL

Browser login opens in the Windows host browser. The storage file is saved in the WSL filesystem.

### Headless Servers & Containers

**Playwright is required only for interactive `notebooklm login` and the optional,
explicitly enabled layer-3 browser recovery path.** Ordinary RPC operations use
standard HTTP requests via `httpx`; a base install with supplied storage state
does not import Playwright.

For the install + auth-bootstrap recipe (run `notebooklm login` on a workstation, copy `storage_state.json` to the server, set `NOTEBOOKLM_AUTH_JSON`), see the canonical Persona D guide: [docs/installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci).
