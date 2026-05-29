# Configuration

**Status:** Active
**Last Updated:** 2026-05-29

This guide covers storage locations, environment settings, and configuration options for `notebooklm-py`.

## File Locations

All data is stored under `~/.notebooklm/` by default, organized by profile:

```
~/.notebooklm/
├── config.json           # Global config: default_profile, language
├── profiles/
│   ├── default/          # Default profile (auto-created)
│   │   ├── storage_state.json    # Authentication cookies and session
│   │   ├── context.json          # CLI context (active notebook, conversation)
│   │   └── browser_profile/      # Persistent Chromium profile
│   ├── work/             # Named profile example
│   │   ├── storage_state.json
│   │   ├── context.json
│   │   └── browser_profile/
│   └── personal/
│       └── ...
```

`config.json` stores process-wide settings — the persisted default profile
name (under the `default_profile` key) and the configured interface language
(`language`). It is **global, not per-profile** (see
`src/notebooklm/paths.py` and `src/notebooklm/cli/language.py`).

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

**Cookie requirements** (empirically validated via single- and pair-wise ablation, see `auth-cookie-lifecycle.md` §3.5; enforced by `_validate_required_cookies()` in `auth.py`):

- **Tier 1 — strictly required (raises on absence):** `SID` AND `__Secure-1PSIDTS`. `SID` is the only individually-required cookie (`__Secure-1PSIDTS` is removable on its own because Google can re-mint it via `RotateCookies`), but the pair-wise check uncovered that as soon as `__Secure-1PSIDTS` and any one other auth cookie are both missing, Google rejects with `Authentication expired or invalid`. The library therefore enforces both up-front. Authoritative value: `MINIMUM_REQUIRED_COOKIES` in `auth.py`.
- **Tier 2 — secondary binding (logs a warning if absent):** either `OSID` is present, or both `APISID` and `SAPISID` are present. Without this, even valid Tier 1 cookies can't authenticate the homepage GET. Logged rather than raised so unverified edge-case flows (e.g. Workspace SSO) aren't broken by a too-strict client check.

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
  "created_at": "2026-05-01T17:43:21Z"
}
```

Field summary:

- `notebook_id` — currently selected notebook, written by `notebooklm use` and read by every command that takes `-n/--notebook`.
- `title`, `is_owner`, `created_at` — optional notebook metadata captured at selection time so `status` / display commands don't need an extra round-trip. Omitted when the CLI didn't have the values to write (see `src/notebooklm/cli/helpers.py:623-651`).

This file is managed automatically by `notebooklm use`, `notebooklm clear`, and the `auth` commands.

### Browser Profile (`browser_profile/`)

A persistent Chromium user data directory used during `notebooklm login`.

**Why persistent?** Google blocks automated login attempts. A persistent profile makes the browser appear as a regular user installation, avoiding bot detection.

**To reset:** Delete the `browser_profile/` directory and run `notebooklm login` again.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NOTEBOOKLM_HOME` | Base directory for all files | `~/.notebooklm` |
| `NOTEBOOKLM_PROFILE` | Active profile name | `default` |
| `NOTEBOOKLM_AUTH_JSON` | Inline authentication JSON (for CI/CD) | - |
| `NOTEBOOKLM_NOTEBOOK` | Default notebook ID for commands without `-n/--notebook` | - |
| `NOTEBOOKLM_HL` | Default interface/output language code (e.g. `en`, `ja`, `zh_Hans`) | `en` |
| `NOTEBOOKLM_BASE_URL` | NotebookLM base URL. Constrained to `https://notebooklm.google.com` (personal) or `https://notebooklm.cloud.google.com` (enterprise) | `https://notebooklm.google.com` |
| `NOTEBOOKLM_BL` | `bl` (build label) URL parameter for the chat streaming endpoint; override when chasing a regression tied to a specific frontend build snapshot | built-in default in `_env.DEFAULT_BL` |
| `NOTEBOOKLM_LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `WARNING` |
| `NOTEBOOKLM_DEBUG_RPC` | Legacy: Enable RPC debug logging (use `LOG_LEVEL=DEBUG` instead) | `false` |
| `NOTEBOOKLM_DEBUG` | Show untruncated RPC response bodies in error messages instead of the default 80-char preview (verbose; intended for deep debugging) | `0` |
| `NOTEBOOKLM_STRICT_DECODE` | Raise `UnknownRPCMethodError` on schema drift (default since PR 13.9a). Set to `0` to opt back into warn-and-fallback for one release window. | `1` |
| `NOTEBOOKLM_RPC_OVERRIDES` | JSON object mapping `RPCMethod` enum names to RPC ID strings (community self-patch when Google rotates a method ID; e.g. `{"LIST_NOTEBOOKS":"AbC123"}`) | - |
| `NOTEBOOKLM_REFRESH_CMD` | Optional command (argv list, or shell string with `_USE_SHELL=1`) invoked when auth refresh is required. Must exit `0` after writing a refreshed `storage_state.json`; the parent reloads from disk | - |
| `NOTEBOOKLM_REFRESH_CMD_USE_SHELL` | Opt the `NOTEBOOKLM_REFRESH_CMD` subprocess back into `shell=True` execution. Default `shell=False` (argv list) — set to the literal `1` (only `"1"` is honored — not `true`/`yes`/`on`) when the refresh command requires shell metacharacters | `0` |
| `NOTEBOOKLM_REFRESH_PROFILE` | Child-process hint set for `NOTEBOOKLM_REFRESH_CMD`; names the resolved profile being refreshed | resolved profile |
| `NOTEBOOKLM_REFRESH_STORAGE_PATH` | Child-process hint set for `NOTEBOOKLM_REFRESH_CMD`; path to the `storage_state.json` file the command must rewrite | resolved storage path |
| `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` | Disable the proactive `accounts.google.com/RotateCookies` poke that refreshes `__Secure-1PSIDTS` ahead of expiry | `0` |
| `NOTEBOOKLM_QUIET_DEPRECATIONS` | Suppress stderr deprecation notices for deprecated CLI flags | - |
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
| `NOTEBOOKLM_RPC_OVERRIDES` | **JSON object** mapping `RPCMethod` enum names to RPC ID strings (e.g. `{"LIST_NOTEBOOKS": "AbC123"}`). Overrides runtime RPC IDs — community self-patch when Google rotates a method ID. Empty string / unset disables the mechanism; invalid JSON or non-object payloads emit a `WARNING` and are ignored. | Process env, evaluated per RPC resolve (cached on the raw env string). | `notebooklm.rpc.overrides._parse_rpc_overrides` |
| `NOTEBOOKLM_QUIET_DEPRECATIONS` | Suppress stderr deprecation notices for deprecated CLI flags (e.g. `source add --mime-type` on file sources). Library-level `DeprecationWarning`s are unaffected. | Set to `1` to suppress; any other value (or unset) leaves the notice enabled. | individual CLI commands; see `NOTEBOOKLM_QUIET_DEPRECATIONS` section below |
| `NOTEBOOKLM_STRICT_DECODE` | Toggle the decoder's drift behavior — raise `UnknownRPCMethodError` (unset/`1`/`true`/`True`, default since PR 13.9a) vs warn-and-fallback opt-out (`0`/`false`/`False`/`no`/`off`/`""` or any other non-truthy value). | Process env on each decode call. | `_env.is_strict_decode_enabled` |
| `NOTEBOOKLM_BASE_URL` | NotebookLM base URL. Constrained to `https://notebooklm.google.com` (personal) or `https://notebooklm.cloud.google.com` (enterprise); other schemes/hosts/paths raise `ValueError`. | Process env on every base-URL lookup. | `_env.get_base_url` |
| `NOTEBOOKLM_BL` | `bl` (build label) URL parameter sent on the chat streaming endpoint (`ChatAPI.ask`). Pins the frontend build the request is attributed to. | Process env on every chat stream call; whitespace-only falls back to `_env.DEFAULT_BL`. | `_env.get_default_bl` |
| `NOTEBOOKLM_DEBUG` | When `1`, RPC error messages include the **full** untruncated response body instead of the default 80-char preview. Verbose; intended for deep debugging only. | Process env on each error formatting call. | `exceptions._truncate_response_preview` |
| `NOTEBOOKLM_REFRESH_CMD` | Optional command invoked when auth refresh is required. Must exit `0` after writing a refreshed `storage_state.json`; the parent reloads cookies from disk. Stdout/stderr are not parsed (only surfaced in the non-zero-exit error message). Parsing honors `NOTEBOOKLM_REFRESH_CMD_USE_SHELL`. | Process env on each refresh subprocess spawn. | `auth` refresh-spawn helper (constant `NOTEBOOKLM_REFRESH_CMD_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_REFRESH_CMD_USE_SHELL` | Opt the optional `NOTEBOOKLM_REFRESH_CMD` subprocess back into `shell=True`. Default `shell=False` parses the command with `shlex.split` and invokes it as an argv list (safer; resists shell-injection footguns when the env var is sourced from CI configs or container env files). | Process env on each refresh subprocess spawn. | `auth` refresh-spawn helper (constant `NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_REFRESH_PROFILE` | Child env var injected into `NOTEBOOKLM_REFRESH_CMD`; names the resolved NotebookLM profile that is being refreshed. Refresh scripts may read it, but setting it in the parent shell does not select the profile. | Set by `auth` refresh-spawn helper from the resolved profile. | `auth._run_refresh_cmd` |
| `NOTEBOOKLM_REFRESH_STORAGE_PATH` | Child env var injected into `NOTEBOOKLM_REFRESH_CMD`; points to the `storage_state.json` file the command must rewrite before exiting `0`. Refresh scripts may read it, but setting it in the parent shell does not select storage. | Set by `auth` refresh-spawn helper from the explicit storage path or profile-aware storage path. | `auth._run_refresh_cmd` |
| `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` | When `1`, disable the proactive `accounts.google.com/RotateCookies` poke that refreshes `__Secure-1PSIDTS` ahead of expiry. Useful when running behind a proxy that rejects the extra request, or in offline test fixtures. | Process env on every keepalive check. | `auth` keepalive guards (constant `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV` in `notebooklm.auth`) |
| `NOTEBOOKLM_VCR_RECORD_ERRORS` | Synthetic-error injection mode for VCR test cassettes. Lowercase-normalized; valid values are `429` (rate limit), `5xx` (server error), or `expired_csrf` (CSRF token expiration). Used to record synthetic error cassettes under VCR. | Process env on each request, evaluated by `ErrorInjectionMiddleware` to intercept and synthesize failures. | `_error_injection._get_error_injection_mode` |

**Boolean handling.** `NOTEBOOKLM_DEBUG_RPC` treats `1` / `true` / `yes`
(case-insensitive) as truthy; everything else is falsy.
`NOTEBOOKLM_STRICT_DECODE` treats `1` / `true` / `True` as truthy (the unset
default also resolves to truthy post-PR 13.9a); everything else (`0`,
`false`, `False`, `no`, `off`, `""`, or any other non-truthy value) is
falsy and opts back into the soft-mode fallback.
`NOTEBOOKLM_QUIET_DEPRECATIONS` requires the literal string `1`.
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
   `src/notebooklm/cli/language.py:111-151` for the resolver and
   `src/notebooklm/paths.py:331-337` for the storage location.
4. `en` (built-in default)

### NOTEBOOKLM_QUIET_DEPRECATIONS

Suppresses stderr deprecation notices emitted by CLI commands when a
deprecated flag or option is used. Useful in CI logs where the deprecation
signal would otherwise be repeated across every invocation in a pipeline.

```bash
export NOTEBOOKLM_QUIET_DEPRECATIONS=1
notebooklm source add ./report.pdf --type file --mime-type application/pdf
# (no "--mime-type is unused for file sources" notice on stderr)
```

Set the value to ``1`` to suppress the notice; any other value (including
``0`` or ``false``) leaves the deprecation notice enabled. The underlying
behavior — that the deprecated flag remains a no-op — is unchanged; only
the user-facing
warning text is silenced. Library-level `DeprecationWarning`s emitted from
the Python API (e.g. `client.sources.add_file(..., mime_type=...)`) are
**not** affected by this variable; use standard `warnings.filterwarnings`
to manage those programmatically.

### Timeouts

Every batchexecute RPC issued by the client (whether through `NotebookLMClient`
or any of the CLI commands) uses a **30-second** HTTP request timeout by
default, with a tighter **10-second** connection-establishment timeout. The
shorter connect timeout helps surface network-level issues quickly while the
longer read timeout accommodates slow server responses. The timeout is exposed as a
constructor argument on `NotebookLMClient` (`timeout=`)
for callers that need to tune it per-workload — see the
`DEFAULT_TIMEOUT` / `DEFAULT_CONNECT_TIMEOUT` constants in
`src/notebooklm/_session_config.py`. The chat streaming endpoint
(`ChatAPI.ask`) keeps its own longer per-stream deadlines because individual
chat responses can exceed 30 seconds, but this is configured at the client level
(via the `timeout=` argument on the `NotebookLMClient` constructor or `from_storage` initializer),
not on individual `ask` calls.

### Decoder strictness

NotebookLM's batchexecute responses are obfuscated, undocumented, and reshaped
by Google without notice. The decoder uses a shared `safe_index` helper to walk
nested response payloads. When it can't descend (an index is out of range, or
the value at a step isn't indexable), behavior depends on
`NOTEBOOKLM_STRICT_DECODE`:

| Value | Behavior |
|-------|----------|
| unset / `1` / `true` / `True` (default since PR 13.9a) | Raise `UnknownRPCMethodError` (a subclass of `DecodingError` / `RPCError`) with structured `method_id`, `path`, `source`, and `data_at_failure` attributes. |
| `0` / `false` / `False` / `no` / `off` / `""` (opt-out — or any other non-truthy value) | Log a warning with the failing path, `method_id`, `source` label, and a truncated repr of the data. Emit `DeprecationWarning` when fallback is used, then return `None` so legacy callers keep working during the final migration window. |

The flipped default (PR 13.9a) closes the Tier-12/13 soft-rollout window the
shared `safe_index` helper was introduced under: every call site that
descends through `safe_index` already handles both modes, and the
strict-by-default contract surfaces real schema drift (Google rotating a
response shape) as a typed exception instead of a silent `None` return. A
small number of legacy positional decoders in `_artifact_downloads`,
`_artifact_polling`, and `_chat_protocol` predate the helper and still have
their own feature-local error-recovery paths; they will be migrated to
`safe_index` in Tier 13.x follow-ups and are unaffected by this flip. Set
`NOTEBOOKLM_STRICT_DECODE=0` to opt back into the legacy warn-and-fallback for
one release if downstream code is not yet ready for the typed exception path.
As of v0.5.0, every fallback use also emits `DeprecationWarning` naming the
decoder `source`; the opt-out is scheduled for removal in v0.6.0 alongside
ADR-011's enforcement timeline.

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
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ File            ┃ Path                                     ┃ Source    ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ Profile         │ default                                  │ active    │
│ Home Directory  │ /home/user/.notebooklm                   │ default   │
│ Storage State   │ .../profiles/default/storage_state.json  │           │
│ Context         │ .../profiles/default/context.json        │           │
│ Browser Profile │ .../profiles/default/browser_profile     │           │
└─────────────────┴──────────────────────────────────────────┴───────────┘
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

Run `notebooklm --storage <path> status --paths` to see exactly which
context file is being used for notebook selection.

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

**Playwright is only required for the `notebooklm login` command.** All other operations use standard HTTP requests via `httpx`.

For the install + auth-bootstrap recipe (run `notebooklm login` on a workstation, copy `storage_state.json` to the server, set `NOTEBOOKLM_AUTH_JSON`), see the canonical Persona D guide: [docs/installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci).
