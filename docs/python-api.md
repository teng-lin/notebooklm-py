# Python API Reference

**Status:** Active
**Last Updated:** 2026-05-15

Complete reference for the `notebooklm` Python library.

## Quick Start

```python
import asyncio
from notebooklm import NotebookLMClient

async def main():
    # Create client from saved authentication
    async with await NotebookLMClient.from_storage() as client:
        # List notebooks
        notebooks = await client.notebooks.list()
        print(f"Found {len(notebooks)} notebooks")

        # Create a new notebook
        nb = await client.notebooks.create("My Research")
        print(f"Created: {nb.id}")

        # Add sources
        await client.sources.add_url(nb.id, "https://example.com/article")

        # Ask a question
        result = await client.chat.ask(nb.id, "Summarize the main points")
        print(result.answer)

        # Generate a podcast
        status = await client.artifacts.generate_audio(nb.id)
        await client.artifacts.wait_for_completion(nb.id, status.task_id)
        output_path = await client.artifacts.download_audio(nb.id, "podcast.mp3")
        print(f"Audio saved to: {output_path}")

asyncio.run(main())
```

---

## Core Concepts

### Concurrency model

`NotebookLMClient` is **async re-entrant on a single event loop**. You can freely await multiple operations concurrently via `asyncio.gather` or `asyncio.TaskGroup`:

```python
notebooks, sources = await asyncio.gather(
    client.notebooks.list(),
    client.sources.list(notebook_id),
)
```

The client is **not thread-safe**. Do not share a `NotebookLMClient` across threads or across multiple event loops. Create one client per loop. The loop-affinity guard added in T7.G2 raises a clear `RuntimeError` on the authed POST hot path if you do — see the [Concurrency contract](#concurrency-contract) section below for the full guarantees, non-guarantees, and production patterns.

If we ever provide thread-safety, it will be a versioned, opt-in API change. Do not assume it.

### Async Context Manager

The client must be used as an async context manager to properly manage HTTP connections:

```python
# Correct - uses context manager
async with await NotebookLMClient.from_storage() as client:
    ...

# Also correct - manual management
client = await NotebookLMClient.from_storage()
await client.__aenter__()
try:
    ...
finally:
    await client.__aexit__(None, None, None)
```

### Authentication

The client requires valid Google session cookies obtained via browser login:

```python
# From storage file (recommended)
client = await NotebookLMClient.from_storage()
client = await NotebookLMClient.from_storage("/path/to/storage_state.json")

# From a named profile
client = await NotebookLMClient.from_storage(profile="work")

# From AuthTokens directly
from notebooklm import AuthTokens
auth = AuthTokens(
    cookies={"SID": "...", "HSID": "...", ...},
    csrf_token="...",
    session_id="..."
)
client = NotebookLMClient(auth)

# AuthTokens also supports profiles (from_storage is async)
auth = await AuthTokens.from_storage(profile="work")
```

**Building a storage state from existing browser cookies (`[cookies]` extra):**

Install with the optional `cookies` extra to pull cookies from a locally installed browser via [rookiepy](https://pypi.org/project/rookiepy/) — useful for headless environments where you cannot run Playwright (full extras matrix: [docs/installation.md#optional-extras-matrix](installation.md#optional-extras-matrix)):

```bash
pip install "notebooklm-py[cookies]"
```

```python
import json
import os
import rookiepy
from notebooklm import NotebookLMClient
from notebooklm.auth import (
    REQUIRED_COOKIE_DOMAINS,
    convert_rookiepy_cookies_to_storage_state,
)

# Pull Google cookies from Chrome (or .firefox(), .edge(), .safari(), .load() for auto-detect).
# REQUIRED_COOKIE_DOMAINS mirrors the CLI's extraction set so rotation, media
# downloads, and Drive flows all have the cookies they need.
raw = rookiepy.chrome(domains=list(REQUIRED_COOKIE_DOMAINS))
storage_state = convert_rookiepy_cookies_to_storage_state(raw)

# Persist for future runs; restrict to owner-only on POSIX since this file holds auth cookies
storage_path = "/path/to/storage_state.json"
with open(storage_path, "w") as f:
    json.dump(storage_state, f)
if os.name != "nt":
    os.chmod(storage_path, 0o600)

async with await NotebookLMClient.from_storage(storage_path) as client:
    notebooks = await client.notebooks.list()
```

`convert_rookiepy_cookies_to_storage_state(rookiepy_cookies)` converts the
cookie list returned by `rookiepy` into the storage-state format
`NotebookLMClient.from_storage()` expects:

- **Key remap:** `http_only` → `httpOnly`, `expires=None` →
  `expires=-1` (Playwright's session-cookie convention), `sameSite="None"`.
- **Filtering:** cookies missing `name`/`value`/`domain`, or from domains
  outside the auth allowlist (regional Google ccTLDs + `REQUIRED_COOKIE_DOMAINS`
  ∪ `OPTIONAL_COOKIE_DOMAINS`), are silently skipped.
- **Return:** `{"cookies": [...], "origins": []}` — drop straight into
  `storage_state.json`.

Cookie extraction (and Google-account selection) happens in the
`rookiepy.<browser>(...)` call: the storage state reflects whichever Google
account is currently active in the source browser. To pick up cookies for
optional surfaces (YouTube, Docs, MyAccount, Mail), extend the rookiepy
`domains=` argument with `OPTIONAL_COOKIE_DOMAINS` (or a label-specific
subset via `OPTIONAL_COOKIE_DOMAINS_BY_LABEL`) — both imported from
`notebooklm.auth` alongside `REQUIRED_COOKIE_DOMAINS`. The CLI equivalent
is `notebooklm login --browser-cookies <browser> [--include-domains youtube,docs,...]`.

**Environment Variable Support:**

The library respects these environment variables for authentication:

| Variable | Description |
|----------|-------------|
| `NOTEBOOKLM_HOME` | Base directory for config files (default: `~/.notebooklm`) |
| `NOTEBOOKLM_PROFILE` | Active profile name (default: `default`) |
| `NOTEBOOKLM_AUTH_JSON` | Inline auth JSON - no file needed (for CI/CD) |

**Precedence** (highest to lowest):
1. Explicit `path` argument to `from_storage()`
2. `NOTEBOOKLM_AUTH_JSON` environment variable
3. Explicit `profile` argument to `from_storage(profile="work")`
4. `NOTEBOOKLM_PROFILE` environment variable (resolves to `~/.notebooklm/profiles/<name>/storage_state.json`)
5. Active profile from `~/.notebooklm/active_profile`
6. `~/.notebooklm/profiles/default/storage_state.json`
7. `~/.notebooklm/storage_state.json` (legacy fallback)

**CI/CD Example:**
```python
import os

# Set auth JSON from environment (e.g., GitHub Actions secret)
os.environ["NOTEBOOKLM_AUTH_JSON"] = '{"cookies": [...]}'

# Client automatically uses the env var
async with await NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
```

### Error Handling

The library raises `RPCError` for API failures:

```python
from notebooklm import RPCError

try:
    result = await client.notebooks.create("Test")
except RPCError as e:
    print(f"RPC failed: {e}")
    # Common causes:
    # - Session expired (re-run `notebooklm login`)
    # - Rate limited (wait and retry)
    # - Invalid parameters
```

### Authentication & Token Refresh

**Automatic Refresh:** The client automatically refreshes CSRF tokens when authentication errors are detected. This happens transparently during any API call - you don't need to handle it manually.

When an RPC call fails with an auth error (HTTP 401/403 or auth-related message):
1. The client fetches fresh tokens from the NotebookLM homepage
2. Waits briefly to avoid rate limiting
3. Retries the failed request automatically

**Manual Refresh:** For proactive refresh (e.g., before a long-running operation):

```python
async with await NotebookLMClient.from_storage() as client:
    # Manually refresh CSRF token and session ID
    await client.refresh_auth()
```

**Note:** If your session cookies have fully expired (not just CSRF tokens), you'll need to re-run `notebooklm login`.

### Idempotency

**Probe-then-retry for create operations.** When a network or server error (5xx / 429 / connection drop) interrupts a create call, the client surfaces the failure immediately rather than blindly retrying. For the methods listed below, the client then probes the server to discover whether the resource was already created before attempting a retry. This prevents duplicate resources when the server accepted the request but the response was lost in transit. The probe runs automatically — no opt-in keyword is required.

The following methods are idempotent under retry:

| Method | Probe |
|---|---|
| `client.notebooks.create(title)` | Snapshot notebook IDs *before*, list *after* a transport failure, return the single new notebook with the matching title (or raise on ambiguity). |
| `client.sources.add_url(notebook_id, url)` | List the notebook's sources, return the existing source whose `url` exactly matches. |
| `client.sources.add_url(notebook_id, youtube_url)` | Same probe via canonical YouTube URL. |

`client.sources.add_text(notebook_id, title, content)` is **not** retry-safe: text sources lack a reliable server-side dedupe key (titles aren't unique; content isn't exposed in the source list). The default behavior is unchanged from previous releases. If you want explicit failure rather than possible silent duplication on retry, opt in:

```python
from notebooklm import NonIdempotentRetryError

try:
    await client.sources.add_text(nb_id, "Title", "Content", idempotent=True)
except NonIdempotentRetryError:
    # Embed a UUID in the title and dedupe client-side instead.
    ...
```

`client.sources.add_file(...)` and `client.sources.add_drive(...)` are not yet covered by the probe-then-retry wrapper — a transport failure during these calls may produce a duplicate source on retry. Tracked as a separate fix.

---

## Concurrency contract

This section is the canonical answer to "is `NotebookLMClient` safe to use from
multiple coroutines / threads / processes / event loops?" — synthesizing the
guarantees, non-guarantees, and production patterns established by the
Tier 7 thread-safety remediation arc (Waves 2–5, ~30 PRs merged 2026-05-14/15;
see `.sisyphus/plans/tier-7-thread-safety-concurrency.md` for the audit trail).

If you only read one subsection, read **[Non-guarantees](#non-guarantees)** —
the guard rails are narrow.

### Guarantees

**Per-loop async safety.** A `NotebookLMClient` instance is bound to the
event loop on which it was opened. A loop-affinity guard (T7.G2) checks
the active loop on the authed POST hot path — `rpc_call()` →
`query_post()` → `_perform_authed_post()` — and raises a clear `RuntimeError`
when the instance is re-used from a different loop. **Scope limitation:** the
guard fires on the hot path only. Two cold paths reach asyncio primitives
before the guard runs:

- `ChatAPI.ask` calls `next_reqid()` before its `query_post() →
  _perform_authed_post` chain. The first cross-loop call's `_reqid_lock` will
  raise a deep asyncio `RuntimeError` (not the friendly G2 message).
- `close()` awaits `save_cookies` + `aclose` and never routes through
  `_perform_authed_post`; cross-loop close gets a deep asyncio error.

In both cases you still get a `RuntimeError` — just an opaque one. **Best
practice:** one client per loop, full stop.

**Refresh deduplication** (T7.C1, audit §13). Concurrent RPCs that all
trigger a token refresh share a single underlying refresh attempt via
`_refresh_lock` + `asyncio.shield`. Waiter cancellation does not kill the
shared refresh task; the next caller in line picks up the finished tokens.

**Reqid monotonicity** (T7.A2, existing). `next_reqid()` returns a monotonic
sequence across concurrent coroutines on the same client. Guarded by
`_reqid_lock`.

**Per-attempt and across-attempt auth snapshot atomicity** (T7.F2,
audit §12). `_auth_snapshot_lock` serializes `_AuthSnapshot` reads against
the refresh-side mutation block. `_build_url` consumes the snapshot rather
than reading live `session_id` / `authuser` / `account_email` fields, so a
refresh completing mid-RPC no longer produces a URL built from a mix of
pre- and post-refresh credentials. (This obsoletes the warning in the
older "Concurrency model" subsection above.)

**Idempotent create RPCs** (T7.B2, audit §5). The following calls are
idempotent under retry via probe-then-create (when `idempotent=True`,
which is the default):

- `client.notebooks.create(title)`
- `client.sources.add_url(notebook_id, url)` (YouTube URLs are auto-detected
  and routed through the YouTube source pathway internally)

`client.sources.add_text(notebook_id, title, content)` is **declared
non-idempotent**: text sources lack a reliable server-side dedupe key
(Google permits duplicate titles, and content is not exposed in source
listings). With `idempotent=True` it raises `NonIdempotentRetryError`. If
you set `disable_internal_retries=True` on the client, the probe-then-retry
wrapper is skipped entirely and the caller is responsible for retry
semantics.

**Cancellation safety.** Several paths are now shielded against
cancellation (audit §§13, 15, 16, 21):

- **`close()`** is shielded; Ctrl-C during shutdown will not leak the
  underlying `httpx.AsyncClient` (T7.B4 — subsumes T7.C2).
- **`refresh_auth()`** runs the shared refresh task under `asyncio.shield`;
  cancelling a waiter does not kill the shared refresh (T7.C1).
- **Upload finalize** is shielded; on cancel signal we issue a best-effort
  Scotty (Google's internal resumable upload service) cancel to release the
  server-side upload slot (T7.C3).
- **`notes.create`** shields the `UPDATE_NOTE` finalize step and cleans up
  the partial note on cancel (T7.C4).
- **`wait_for_sources`** cancels sibling pollers on the first poller's
  failure rather than letting them race to emit error messages (T7.E1).
- **`wait_for_completion`** uses a leader/follower polling-dedupe registry
  with a shielded leader task — follower cancellation does not kill the
  leader's poll (T7.E2).

**Idempotent file uploads.** `SourcesAPI.add_file` closes its file handle
under a TOCTOU-safe path and gates concurrent uploads via the
`max_concurrent_uploads` semaphore so a large fan-out can't exhaust the
per-process file descriptor limit (T7.D3, audit §23).

### Non-guarantees

**NOT thread-safe.** A `NotebookLMClient` instance must not be shared
across OS threads. The internal locks (`_refresh_lock`, `_reqid_lock`,
`_auth_snapshot_lock`) are `asyncio.Lock` instances and do not protect
against concurrent OS-thread access. If you need a client per thread,
construct one per thread.

**NOT reusable across event loops.** Per the loop-affinity guard above
(T7.G2), the hot path raises `RuntimeError` when an instance is re-used
on a different loop. Cold paths (`next_reqid()`, `close()`) raise an
opaque asyncio `RuntimeError` instead — same outcome, less helpful
message.

**`_conversation_cache` is per-instance.** Chat-conversation IDs cached
inside a `NotebookLMClient` are not shared across clients in the same
process and never persisted across processes. Two clients pointed at the
same notebook will not share follow-up context.

**Cookies in storage are eventually-consistent across processes.** When
multiple processes share a storage path, an OS-level file lock plus a
snapshot/delta merge (see `docs/auth-keepalive.md` §3.4) keep concurrent
writers from corrupting the file. They may, however, observe brief
staleness — a write committed by process A may not be visible to a
sibling read in process B until the next refresh cycle. Within a single
process, in-process dedupe ensures only one keepalive task runs per
canonicalized storage path (T7.G6).

### Production patterns

**One client per app, dependency-injected.** A `NotebookLMClient` is
designed to be a long-lived process resource. In FastAPI, attach it to
the app lifespan:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request
from notebooklm import NotebookLMClient

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with await NotebookLMClient.from_storage() as client:
        app.state.notebooklm = client
        yield
    # client.close() happens via __aexit__

def get_client(request: Request) -> NotebookLMClient:
    return request.app.state.notebooklm

app = FastAPI(lifespan=lifespan)

@app.get("/notebooks")
async def list_notebooks(client: NotebookLMClient = Depends(get_client)):
    return await client.notebooks.list()
```

Constraint: FastAPI runs on a single event loop per worker, so one client
per worker is correct. If you run multiple Uvicorn workers, each worker
owns its own client. **Do not** stash a `NotebookLMClient` on a
process-global outside the lifespan — multi-worker servers fork the
process and you will end up with the same client object referencing
different event loops.

**`ConnectionLimits` tuning** (T7.B3). The HTTP pool defaults
(`max_connections=100`, `max_keepalive_connections=50`,
`keepalive_expiry=30.0`) are sized for typical batchexecute fan-out: a
few dozen concurrent RPCs against a single host with keep-alives held
for an interactive session. Tune via `notebooklm.types.ConnectionLimits`:

```python
from notebooklm import NotebookLMClient
from notebooklm.types import ConnectionLimits

limits = ConnectionLimits(
    max_connections=200,         # widen the pool for a heavy worker
    max_keepalive_connections=100,
    keepalive_expiry=60.0,
)
client = NotebookLMClient(auth, limits=limits, max_concurrent_rpcs=64)
```

For single-request CLI workloads the defaults are wasteful but harmless.

**`max_concurrent_rpcs` knob** (T7.H1, audit §8). A semaphore at
`_perform_authed_post` caps simultaneous in-flight RPC POSTs. Default
`16` — well below the default pool size so short-lived helper requests
(refresh GETs, upload preflights) still have pool headroom. Pass `None`
to opt out entirely (e.g. when an external rate-limiter handles
back-pressure). The backoff for 429 / 5xx retries is held **inside** the
semaphore for a circuit-breaker effect: a slow request keeps its slot
while it waits, so the gate naturally throttles fan-out when the server
is unhappy.

Worst-case slot hold time:

| Path | Bound | Default |
|---|---|---|
| 429 retry loop | `rate_limit_max_retries × MAX_RETRY_AFTER_SECONDS` | 3 × 300 = 900s |
| 5xx / network retry loop | `server_error_max_retries × 30s` (capped backoff) | 3 × 30 = 90s |

If your workload's tail latency is sensitive, lower
`rate_limit_max_retries` or tighten the semaphore — slot hold time on
the 429 path is the load-bearing variable.

**Constraint** (enforced at construction): `max_concurrent_rpcs ≤
ConnectionLimits.max_connections`. A higher RPC ceiling than the pool
capacity would let the semaphore admit requests the pool can't fulfill,
producing opaque `httpx.PoolTimeout` errors instead of clean
back-pressure. The `NotebookLMClient.__init__` / `from_storage()`
constructor raises `ValueError` if this constraint is violated. The
semaphore floor (`max_concurrent_rpcs ≥ 1` when not `None`) is enforced
inside `ClientCore`.

**`max_concurrent_uploads` knob** (T7.D3, audit §23). Default `4`. Gates
file-upload streaming independently from the RPC throttle because
uploads use their own `httpx.AsyncClient` (Scotty endpoint) and do not
share the RPC connection pool. The motivation is FD exhaustion: each
in-flight upload holds one open file descriptor for the duration of the
upload, so an unbounded fan-out blows the per-process FD limit. `None`
resolves to the default (4); truly unbounded uploads are intentionally
not supported. Must be `≥ 1` when set explicitly.

**Rate-limit retry defaults** (T7.H2). `rate_limit_max_retries=3`,
`server_error_max_retries=3`. The 429 path honors the `Retry-After`
header when parseable (clamped at `MAX_RETRY_AFTER_SECONDS = 300s`);
when the header is absent or unparseable, the loop falls back to
exponential backoff `min(2^attempt, 30)` seconds with ±20% jitter
(where `attempt` starts at `0`, so the first retry sleeps ~1 s ± 20%
before doubling), matching the 5xx path. Set either to `0` to restore
the pre-T7.H2 behavior of raising `RateLimitError` / `ServerError`
immediately.

**Observability hooks.** The client exposes stdlib-only observability so
applications can choose their own metrics backend:

```python
from notebooklm import NotebookLMClient, correlation_id

events = []

async with await NotebookLMClient.from_storage(on_rpc_event=events.append) as client:
    with correlation_id("batch-import-42"):
        await client.notebooks.list()

    snapshot = client.metrics_snapshot()
    print(snapshot.rpc_calls_succeeded, snapshot.rpc_queue_wait_seconds_max)
```

`on_rpc_event` receives a `RpcTelemetryEvent` for each logical RPC
completion. `metrics_snapshot()` returns cumulative counters for RPC
success/failure, retry counts, semaphore queue waits, upload queue waits,
and internal lock wait time. The package does not depend on Prometheus or
OpenTelemetry; forward these values to whichever backend your service uses.

**Graceful shutdown.** Long-lived services can stop admitting new client
operations and wait for in-flight operations before closing:

```python
await client.close(drain=True, drain_timeout=30.0)
```

`client.drain(timeout=...)` is also available when your framework owns
transport shutdown separately. Once drain starts, new operations raise
`RuntimeError`; if the timeout expires, the client remains in draining mode.
`close(drain=True, ...)` still closes the transport after a drain timeout and
then re-raises the timeout.

**Upload-timeout configuration** (T7.H3). `client.sources.add_file(...)`
and the related upload entry points accept an `upload_timeout` argument
that is decoupled from the global `timeout`. A long-running upload of
a large file should not have to widen the global HTTP timeout to
succeed; pass `upload_timeout=600.0` (or larger) to the relevant call
sites instead.

### Single-process multi-tenant guidance

For a service that handles multiple NotebookLM tenants (different
`AuthTokens`, typically one per user), spin up **one
`NotebookLMClient` per tenant**. There is no cross-tenant
`_conversation_cache` bleed (the cache is per-instance), and the
loop-affinity guard plus the per-instance refresh state means tenants
cannot accidentally observe each other's auth.

Cookie storage paths must be canonicalized so two clients pointing at
the same logical storage file don't run racing keepalive loops; T7.G6
handles this automatically for the keepalive code path.

### Constraints enforced at construction

These validations run in `NotebookLMClient.__init__` /
`NotebookLMClient.from_storage()` (and `ClientCore.__init__` for the
floor checks). All raise `ValueError`:

- `max_concurrent_rpcs ≤ ConnectionLimits.max_connections` when both are
  set (skipped when either is `None`).
- `max_concurrent_rpcs ≥ 1` when not `None`.
- `max_concurrent_uploads ≥ 1` when not `None`.
- `rate_limit_max_retries ≥ 0`.
- `server_error_max_retries ≥ 0`.
- `keepalive` must be `None` or a positive finite number; values below
  `keepalive_min_interval` (default `60s`) are clamped up to that floor.

---

**Last full audit**: 2026-05-15, HEAD `bbcfe69` (T7.Z1; 0 new CRITICAL findings vs the 2026-05-14 baseline at HEAD `89773ec`).

---

## API Reference

### NotebookLMClient

Main client class providing access to all APIs.

```python
class NotebookLMClient:
    notebooks: NotebooksAPI    # Notebook operations
    sources: SourcesAPI        # Source management
    artifacts: ArtifactsAPI    # Artifact operations (audio, video, reports, etc.)
    chat: ChatAPI              # Conversations
    research: ResearchAPI      # Web/Drive research
    notes: NotesAPI            # User notes
    settings: SettingsAPI      # User settings (language, etc.)
    sharing: SharingAPI        # Notebook sharing
    auth: AuthTokens           # Current authentication tokens
    is_connected: bool         # Connection state

    @classmethod
    async def from_storage(
        cls, path: str | None = None, timeout: float = 30.0,
        profile: str | None = None,
        keepalive: float | None = None,
        keepalive_min_interval: float = 60.0,
        rate_limit_max_retries: int = 3,
        server_error_max_retries: int = 3,
        limits: ConnectionLimits | None = None,
    ) -> "NotebookLMClient"

    def __init__(
        self, auth: AuthTokens, timeout: float = 30.0,
        storage_path: Path | None = None,
        keepalive: float | None = None,
        keepalive_min_interval: float = 60.0,
        rate_limit_max_retries: int = 3,
        server_error_max_retries: int = 3,
        limits: ConnectionLimits | None = None,
    )

    async def refresh_auth(self) -> AuthTokens

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any
```

`RPCMethod` is imported from `notebooklm.rpc` for raw-RPC calls; `Any` is
`typing.Any`. `_is_retry` is present only to preserve parity with the core
delegator and should normally be left as `False`.

**Long-lived clients:** pass `keepalive=<seconds>` to spawn a background task
that periodically pokes `accounts.google.com` and persists any rotated
`__Secure-1PSIDTS` cookie to `storage_state.json`. This keeps a worker /
agent / long-running `async with` block from silently staling out. Disabled
by default (`keepalive=None`). Values below `keepalive_min_interval` (default
`60.0`) are clamped up to that floor. See [Cookie freshness for long-running
/ unattended use](troubleshooting.md#cookie-freshness-for-long-running--unattended-use)
for the full layered story.

**Retry behavior:** the client retries transient failures transparently.

- `server_error_max_retries` (default `3`) retries HTTP 5xx and network-layer
  `httpx.RequestError` (timeouts, connect errors) with exponential backoff
  capped at 30 seconds (`min(2 ** attempt, 30)`, plus ±20% jitter to
  desynchronize concurrent retries). Set to `0` to disable.
- `rate_limit_max_retries` (default `3` since T7.H2 — audit §11) retries
  HTTP 429 responses. Each retry sleeps for the server's `Retry-After`
  value when parseable; otherwise the loop falls back to the same
  capped-exponential-backoff schedule used for 5xx (`min(2 ** attempt,
  30)` seconds with ±20% jitter) so the positive default is still
  useful when Google omits the hint. Set to `0` to restore the
  pre-T7.H2 contract of raising `RateLimitError` immediately (e.g. when
  the calling code implements its own bespoke back-off policy). Mutating
  create RPCs (`notebooks.create`, `sources.add_url`) opt out of this loop
  via `disable_internal_retries` so the API-layer `idempotent_create`
  wrapper can own probe-then-retry recovery — see T7.B2.
- `limits` accepts a `ConnectionLimits` dataclass to tune the underlying
  `httpx` connection pool. The default (`ConnectionLimits()`) sets
  `max_connections=100`, `max_keepalive_connections=50`,
  `keepalive_expiry=30.0` — sized for typical batchexecute fan-out. Widen
  for heavy concurrent workloads such as FastAPI/Django services that share
  one client across many requests.

```python
from notebooklm import ConnectionLimits, NotebookLMClient

# Default ``rate_limit_max_retries=3`` is on; widen the pool for a heavy worker
async with await NotebookLMClient.from_storage(
    limits=ConnectionLimits(max_connections=200, max_keepalive_connections=100),
) as client:
    ...

# Opt out of automatic 429 retries (e.g. for a bespoke back-off layer)
async with await NotebookLMClient.from_storage(rate_limit_max_retries=0) as client:
    ...
```

---

### NotebooksAPI (`client.notebooks`)

**CLI equivalent:** [Notebook Commands](cli-reference.md#notebook-commands) — `notebooklm list`, `create`, `delete`, `rename`, `summary`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list()` | - | `list[Notebook]` | List all notebooks |
| `create(title)` | `title: str` | `Notebook` | Create a notebook |
| `get(notebook_id)` | `notebook_id: str` | `Notebook` | Get notebook details |
| `delete(notebook_id)` | `notebook_id: str` | `bool` | Delete a notebook |
| `rename(notebook_id, new_title)` | `notebook_id: str, new_title: str` | `Notebook` | Rename a notebook |
| `get_description(notebook_id)` | `notebook_id: str` | `NotebookDescription` | Get AI summary and topics |
| `get_metadata(notebook_id)` | `notebook_id: str` | `NotebookMetadata` | Get notebook metadata and sources |
| `get_summary(notebook_id)` | `notebook_id: str` | `str` | Get raw summary text |
| `share(notebook_id, public=True, artifact_id=None)` | `notebook_id: str, bool, str \| None` | `dict` | Create or update a share link |
| `get_share_url(notebook_id, artifact_id=None)` | `notebook_id: str, str \| None` | `str` | Get a share URL |
| `remove_from_recent(notebook_id)` | `notebook_id: str` | `None` | Remove from recently viewed |
| `get_raw(notebook_id)` | `notebook_id: str` | `Any` | Get raw API response data |

**Example:**
```python
# List all notebooks
notebooks = await client.notebooks.list()
for nb in notebooks:
    print(f"{nb.id}: {nb.title} ({nb.sources_count} sources)")

# Create and rename
nb = await client.notebooks.create("Draft")
nb = await client.notebooks.rename(nb.id, "Final Version")

# Get AI-generated description (parsed with suggested topics)
desc = await client.notebooks.get_description(nb.id)
print(desc.summary)
for topic in desc.suggested_topics:
    print(f"  - {topic.question}")

# Get raw summary text (unparsed)
summary = await client.notebooks.get_summary(nb.id)
print(summary)

# Get metadata for automation or exports
metadata = await client.notebooks.get_metadata(nb.id)
print(metadata.title)

# Enable public sharing and fetch the URL
await client.sharing.set_public(nb.id, public=True)
url = await client.notebooks.get_share_url(nb.id)
print(url)
```

**get_summary vs get_description:**
- `get_summary()` returns the raw summary text string
- `get_description()` returns a `NotebookDescription` object with the parsed summary and a list of `SuggestedTopic` objects for suggested questions

---

### SourcesAPI (`client.sources`)

**CLI equivalent:** [Source Commands](cli-reference.md#source-commands-notebooklm-source-cmd) — `notebooklm source add`, `list`, `get`, `fulltext`, `guide`, `rename`, `refresh`, `delete`, `wait`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list(notebook_id)` | `notebook_id: str` | `list[Source]` | List sources |
| `get(notebook_id, source_id)` | `str, str` | `Source \| None` | Get source details (returns None if not found) |
| `get_fulltext(notebook_id, source_id, *, output_format="text")` | `str, str, *, output_format: Literal["text", "markdown"]` | `SourceFulltext` | Get full content; `"markdown"` requires the optional `markdownify` extra |
| `get_guide(notebook_id, source_id)` | `str, str` | `dict` | Get AI-generated summary and keywords |
| `add_url(notebook_id, url, wait=False, wait_timeout=120.0)` | `str, str, bool, float` | `Source` | Add URL source (autodetects YouTube URLs and routes them appropriately) |
| `add_text(notebook_id, title, content, wait=False, wait_timeout=120.0)` | `str, str, str, bool, float` | `Source` | Add text content |
| `add_file(notebook_id, file_path, mime_type=None, wait=False, wait_timeout=120.0, *, title=None, on_progress=None)` | `str, str \| Path, str \| None, bool, float, *, str \| None, Callable \| None` | `Source` | Upload file. `mime_type` is **deprecated** and ignored (server infers from filename); passing non-`None` raises `DeprecationWarning`. `title` (keyword-only) sets the display name via a post-upload `UPDATE_SOURCE` and forces a brief registration wait even when `wait=False`. `on_progress(bytes_sent, total_bytes)` may be sync or async. |
| `add_drive(notebook_id, file_id, title, mime_type)` | `str, str, str, str` | `Source` | Add Google Drive doc |
| `rename(notebook_id, source_id, new_title)` | `str, str, str` | `Source` | Rename source |
| `refresh(notebook_id, source_id)` | `str, str` | `bool` | Refresh URL/Drive source |
| `check_freshness(notebook_id, source_id)` | `str, str` | `bool` | Check if source needs refresh |
| `delete(notebook_id, source_id)` | `str, str` | `bool` | Delete source |
| `wait_until_ready(notebook_id, source_id, timeout=120.0, ...)` | `str, str, float, ...` | `Source` | Poll until `status == READY` (fully processed). Raises `SourceTimeoutError`/`SourceProcessingError`/`SourceNotFoundError`. |
| `wait_until_registered(notebook_id, source_id, timeout=30.0, ...)` | `str, str, float, ...` | `Source` | Poll until the source is visible server-side (any non-ERROR status). Completes quickly (seconds for typical sources); intended for narrow follow-up RPCs (e.g. `UPDATE_SOURCE`) that only require registration, not full processing. |
| `wait_for_sources(notebook_id, source_ids, timeout=120.0, **kwargs)` | `str, list[str], float, ...` | `list[Source]` | Wait for multiple sources to become ready **in parallel**. Per-source timeout; `**kwargs` are forwarded to `wait_until_ready`. |

**Example:**
```python
from pathlib import Path

# Add various source types
await client.sources.add_url(nb_id, "https://example.com/article")
await client.sources.add_url(nb_id, "https://youtube.com/watch?v=...")  # YouTube URLs autodetected
await client.sources.add_text(nb_id, "My Notes", "Content here...")
await client.sources.add_file(nb_id, Path("./document.pdf"))

# Upload a file with a custom display title (rename happens after upload via
# UPDATE_SOURCE — a brief registration wait runs even when wait=False so the
# rename can land). The mime_type kwarg is deprecated; the server infers
# MIME type from the filename extension.
await client.sources.add_file(nb_id, Path("./document.pdf"), title="Q4 Strategy Memo")

# Wait for several uploads to finish processing in parallel
ids = [
    (await client.sources.add_url(nb_id, "https://example.com/a")).id,
    (await client.sources.add_url(nb_id, "https://example.com/b")).id,
]
ready = await client.sources.wait_for_sources(nb_id, ids, timeout=180)

# Narrow wait: only block until the source is visible server-side (not fully
# processed). Use this before follow-up RPCs like UPDATE_SOURCE.
registered = await client.sources.wait_until_registered(nb_id, ids[0])

# List and manage
sources = await client.sources.list(nb_id)
for src in sources:
    print(f"{src.id}: {src.title} ({src.kind})")

await client.sources.rename(nb_id, src.id, "Better Title")
await client.sources.refresh(nb_id, src.id)  # Re-fetch URL content

# Check if a source needs refreshing (content changed)
is_fresh = await client.sources.check_freshness(nb_id, src.id)
if not is_fresh:
    await client.sources.refresh(nb_id, src.id)

# Get full indexed content (what NotebookLM uses for answers)
fulltext = await client.sources.get_fulltext(nb_id, src.id)
print(f"Content ({fulltext.char_count} chars): {fulltext.content[:500]}...")

# Get AI-generated summary and keywords
guide = await client.sources.get_guide(nb_id, src.id)
print(f"Summary: {guide['summary']}")
print(f"Keywords: {guide['keywords']}")
```

---

### ArtifactsAPI (`client.artifacts`)

**CLI equivalent:** [Artifact Commands](cli-reference.md#artifact-commands-notebooklm-artifact-cmd) — `notebooklm artifact list`, `get`, `rename`, `delete`, `export`, `poll`, `wait`. Generation methods map to [Generate Commands](cli-reference.md#generate-commands-notebooklm-generate-type) (`notebooklm generate <type>`); download methods map to [Download Commands](cli-reference.md#download-commands-notebooklm-download-type) (`notebooklm download <type>`).

#### Core Methods

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list(notebook_id, type=None)` | `str, int` | `list[Artifact]` | List artifacts |
| `get(notebook_id, artifact_id)` | `str, str` | `Artifact \| None` | Get artifact details (returns None if not found) |
| `delete(notebook_id, artifact_id)` | `str, str` | `bool` | Delete artifact |
| `rename(notebook_id, artifact_id, new_title)` | `str, str, str` | `None` | Rename artifact |
| `poll_status(notebook_id, task_id)` | `str, str` | `GenerationStatus` | Check generation status |
| `wait_for_completion(notebook_id, task_id, ...)` | `str, str, ...` | `GenerationStatus` | Wait for generation. Pass `on_status_change(status)` for sync or async progress callbacks. |

#### Type-Specific List Methods

**CLI equivalent:** `notebooklm artifact list --type <audio|video|slide-deck|quiz|flashcard|infographic|data-table|mind-map|report>` (see [Artifact Commands](cli-reference.md#artifact-commands-notebooklm-artifact-cmd)).

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list_audio(notebook_id)` | `str` | `list[Artifact]` | List audio overview artifacts |
| `list_video(notebook_id)` | `str` | `list[Artifact]` | List video overview artifacts |
| `list_reports(notebook_id)` | `str` | `list[Artifact]` | List report artifacts (Briefing Doc, Study Guide, Blog Post) |
| `list_quizzes(notebook_id)` | `str` | `list[Artifact]` | List quiz artifacts |
| `list_flashcards(notebook_id)` | `str` | `list[Artifact]` | List flashcard artifacts |
| `list_infographics(notebook_id)` | `str` | `list[Artifact]` | List infographic artifacts |
| `list_slide_decks(notebook_id)` | `str` | `list[Artifact]` | List slide deck artifacts |
| `list_data_tables(notebook_id)` | `str` | `list[Artifact]` | List data table artifacts |

#### Generation Methods

**CLI equivalent:** [Generate Commands](cli-reference.md#generate-commands-notebooklm-generate-type) — `notebooklm generate audio`, `video`, `slide-deck`, `quiz`, `flashcards`, `infographic`, `data-table`, `mind-map`, `report`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `generate_audio(...)` | See below | `GenerationStatus` | Generate podcast |
| `generate_video(...)` | See below | `GenerationStatus` | Generate video |
| `generate_report(...)` | See below | `GenerationStatus` | Generate report |
| `generate_quiz(...)` | See below | `GenerationStatus` | Generate quiz |
| `generate_flashcards(...)` | See below | `GenerationStatus` | Generate flashcards |
| `generate_slide_deck(...)` | See below | `GenerationStatus` | Generate slide deck |
| `generate_infographic(...)` | See below | `GenerationStatus` | Generate infographic |
| `generate_data_table(...)` | See below | `GenerationStatus` | Generate data table |
| `generate_mind_map(...)` | See below | `dict` | Generate mind map |

#### Downloading Artifacts

**CLI equivalent:** [Download Commands](cli-reference.md#download-commands-notebooklm-download-type) — `notebooklm download audio`, `video`, `slide-deck`, `infographic`, `report`, `mind-map`, `data-table`, `quiz`, `flashcards`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `download_audio(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download audio to file (MP4/MP3) |
| `download_video(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download video to file (MP4) |
| `download_infographic(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download infographic to file (PNG) |
| `download_slide_deck(notebook_id, output_path, artifact_id=None, output_format="pdf")` | `str, str, str, str` | `str` | Download slide deck as PDF or PPTX (`output_format`: `"pdf"` or `"pptx"`) |
| `download_report(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download report as Markdown (.md) |
| `download_mind_map(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download mind map as JSON (.json) |
| `download_data_table(notebook_id, output_path, artifact_id=None)` | `str, str, str` | `str` | Download data table as CSV (.csv) |
| `download_quiz(notebook_id, output_path, artifact_id=None, output_format="json")` | `str, str, str, str` | `str` | Download quiz (json/markdown/html) |
| `download_flashcards(notebook_id, output_path, artifact_id=None, output_format="json")` | `str, str, str, str` | `str` | Download flashcards (json/markdown/html) |

**Download Methods:**

```python
# Download the most recent completed audio overview
path = await client.artifacts.download_audio(nb_id, "podcast.mp4")

# Download a specific audio artifact by ID
path = await client.artifacts.download_audio(nb_id, "podcast.mp4", artifact_id="abc123")

# Download video overview
path = await client.artifacts.download_video(nb_id, "video.mp4")

# Download infographic
path = await client.artifacts.download_infographic(nb_id, "infographic.png")

# Download slide deck as PDF
path = await client.artifacts.download_slide_deck(nb_id, "./slides.pdf")
# Returns: "./slides.pdf"

# Download report as Markdown
path = await client.artifacts.download_report(nb_id, "./study-guide.md")
# Extracts markdown content from Briefing Doc, Study Guide, Blog Post, etc.

# Download mind map as JSON
path = await client.artifacts.download_mind_map(nb_id, "./concept-map.json")
# JSON structure: {"name": "Topic", "children": [{"name": "Subtopic", ...}]}

# Download data table as CSV
path = await client.artifacts.download_data_table(nb_id, "./data.csv")
# CSV uses UTF-8 with BOM encoding for Excel compatibility

# Download quiz as JSON (default)
path = await client.artifacts.download_quiz(nb_id, "quiz.json")

# Download quiz as markdown with answers marked
path = await client.artifacts.download_quiz(nb_id, "quiz.md", output_format="markdown")

# Download flashcards as JSON (normalizes f/b to front/back)
path = await client.artifacts.download_flashcards(nb_id, "cards.json")

# Download flashcards as markdown
path = await client.artifacts.download_flashcards(nb_id, "cards.md", output_format="markdown")
```

**Notes:**
- If `artifact_id` is not specified, downloads the first completed artifact of that type
- Raises `ValueError` if no completed artifact is found
- Some URLs require browser-based download (handled automatically)
- Report downloads extract the markdown content from the artifact
- Mind map downloads return a JSON tree structure with `name` and `children` fields
- Data table downloads parse the complex rich-text format into CSV rows/columns
- Quiz/flashcard formats: `json` (structured), `markdown` (readable), `html` (raw)
- Downloads automatically use the storage path from `from_storage(path=...)` or the resolved profile for cookie authentication

#### Export Methods

Export artifacts to Google Docs or Google Sheets.

**CLI equivalent:** `notebooklm artifact export <id> --title TEXT --type [docs|sheets]` (see [Artifact Commands](cli-reference.md#artifact-commands-notebooklm-artifact-cmd)).

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `export_report(notebook_id, artifact_id, title="Export", export_type=ExportType.DOCS)` | `str, str, str, ExportType` | `Any` | Export report to Google Docs/Sheets |
| `export_data_table(notebook_id, artifact_id, title="Export")` | `str, str, str` | `Any` | Export data table to Google Sheets |
| `export(notebook_id, artifact_id=None, content=None, title="Export", export_type=ExportType.DOCS)` | `str, str \| None, str \| None, str, ExportType` | `Any` | Generic export to Docs/Sheets. All trailing parameters are optional with defaults; pass `content=...` to export inline content without a pre-existing artifact. |

**Export Types (ExportType enum):**
- `ExportType.DOCS` (1): Export to Google Docs
- `ExportType.SHEETS` (2): Export to Google Sheets

```python
from notebooklm import ExportType

# Export a report to Google Docs
result = await client.artifacts.export_report(
    nb_id,
    artifact_id="report_123",
    title="My Briefing Doc",
    export_type=ExportType.DOCS
)
# result contains the Google Docs URL

# Export a data table to Google Sheets
result = await client.artifacts.export_data_table(
    nb_id,
    artifact_id="table_456",
    title="Research Data"
)
# result contains the Google Sheets URL

# Generic export (e.g., export any artifact to Sheets). All trailing
# parameters have defaults: `artifact_id=None`, `content=None`,
# `title="Export"`, `export_type=ExportType.DOCS`. Supply `content=...`
# instead of `artifact_id=...` to export inline text without a pre-existing
# artifact.
result = await client.artifacts.export(
    nb_id,
    artifact_id="artifact_789",
    title="Exported Content",
    export_type=ExportType.SHEETS
)
```

**Generation Methods:**

```python
from notebooklm import (
    AudioFormat,
    AudioLength,
    VideoFormat,
    VideoStyle,
    ReportFormat,
    QuizQuantity,
    QuizDifficulty,
)

# Audio (podcast)
status = await client.artifacts.generate_audio(
    notebook_id,
    source_ids=None,           # List of source IDs (None = all)
    instructions="...",        # Custom instructions
    audio_format=AudioFormat.DEEP_DIVE,  # DEEP_DIVE, BRIEF, CRITIQUE, DEBATE
    audio_length=AudioLength.DEFAULT,    # SHORT, DEFAULT, LONG
    language="en"
)

# Video
status = await client.artifacts.generate_video(
    notebook_id,
    source_ids=None,
    instructions="...",
    video_format=VideoFormat.EXPLAINER,  # EXPLAINER, BRIEF, CINEMATIC
    video_style=VideoStyle.AUTO_SELECT,  # AUTO_SELECT, CLASSIC, WHITEBOARD, KAWAII, ANIME, etc.
    language="en"
)

# Report
status = await client.artifacts.generate_report(
    notebook_id,
    report_format=ReportFormat.STUDY_GUIDE,  # BRIEFING_DOC, STUDY_GUIDE, BLOG_POST, CUSTOM
    source_ids=None,
    language="en",
    custom_prompt=None,          # Used with ReportFormat.CUSTOM
    extra_instructions="..."     # Optional append for built-in formats
)

# Quiz
status = await client.artifacts.generate_quiz(
    notebook_id,
    source_ids=None,
    instructions="...",
    quantity=QuizQuantity.MORE,        # FEWER, STANDARD, MORE (MORE aliases STANDARD)
    difficulty=QuizDifficulty.MEDIUM,  # EASY, MEDIUM, HARD
)
```

**Waiting for Completion:**

```python
# Start generation
status = await client.artifacts.generate_audio(nb_id)

# Wait with polling
final = await client.artifacts.wait_for_completion(
    nb_id,
    status.task_id,
    timeout=300,      # Max wait time in seconds
    poll_interval=5   # Seconds between polls
)

if final.is_complete:
    path = await client.artifacts.download_audio(nb_id, "podcast.mp3")
    print(f"Saved to: {path}")
else:
    print(f"Failed or timed out: {final.status}")
```

---

### ChatAPI (`client.chat`)

**CLI equivalent:** [Chat Commands](cli-reference.md#chat-commands) — `notebooklm ask`, `configure`, `history`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `ask(notebook_id, question, ...)` | `str, str, ...` | `AskResult` | Ask a question |
| `configure(notebook_id, ...)` | `str, ...` | `None` | Set chat persona |
| `get_history(notebook_id, limit=100, conversation_id=None)` | `str, int, str` | `list[tuple[str, str]]` | Get Q&A pairs from most recent conversation |
| `get_conversation_id(notebook_id)` | `str` | `str \| None` | Get most recent conversation ID from server |

**ask() Parameters:**
```python
async def ask(
    notebook_id: str,
    question: str,
    source_ids: list[str] | None = None,  # Limit to specific sources (None = all)
    conversation_id: str | None = None,   # Continue existing conversation
) -> AskResult
```

**Example:**
```python
from notebooklm import ChatGoal, ChatResponseLength

# Ask questions (uses all sources)
result = await client.chat.ask(nb_id, "What are the main themes?")
print(result.answer)

# Access source references (cited in answer as [1], [2], etc.)
for ref in result.references:
    print(f"Citation {ref.citation_number}: Source {ref.source_id}")

# Ask using only specific sources
result = await client.chat.ask(
    nb_id,
    "Summarize the key points",
    source_ids=["src_001", "src_002"]
)

# Continue conversation
result = await client.chat.ask(
    nb_id,
    "Can you elaborate on the first point?",
    conversation_id=result.conversation_id
)

# Configure persona
await client.chat.configure(
    nb_id,
    goal=ChatGoal.LEARNING_GUIDE,
    response_length=ChatResponseLength.LONGER,
    custom_prompt="Focus on practical applications"
)
```

---

### ResearchAPI (`client.research`)

**CLI equivalent:** [Research Commands](cli-reference.md#research-commands-notebooklm-research-cmd) (`notebooklm research status`, `wait`) plus `notebooklm source add-research` ([Source: `add-research`](cli-reference.md#source-add-research)) for the combined start-and-import workflow.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `start(notebook_id, query, source, mode)` | `str, str, str="web", str="fast"` | `dict \| None` | Start research (mode: "fast" or "deep"); raises `ValidationError` on invalid source/mode |
| `poll(notebook_id)` | `str` | `dict` | Check research status |
| `import_sources(notebook_id, task_id, sources)` | `str, str, list` | `list[dict]` | Import findings |

**Method Signatures:**

```python
async def start(
    notebook_id: str,
    query: str,
    source: str = "web",   # "web" or "drive"
    mode: str = "fast",    # "fast" or "deep" (deep only for web)
) -> dict | None:
    """
    Returns: {"task_id": str, "report_id": str, "notebook_id": str, "query": str, "mode": str},
        or None if the RPC returns an empty/unexpected payload
    Raises: ValidationError if source/mode combination is invalid
    """

async def poll(notebook_id: str) -> dict:
    """
    Returns a dict for the LATEST research task. Top-level keys:
      - task_id:   str       — task/report identifier
      - status:    str       — "completed" | "in_progress" | "no_research"
      - query:     str       — original research query
      - sources:   list[dict]
      - summary:   str       — summary text when present
      - report:    str       — deep-research report markdown when present
      - tasks:     list[dict] — ALL parsed tasks (same shape as the top-level
                                latest-task fields), additive across polls

    Each source dict may include:
      - url, title
      - result_type:        int — 1=web, 2=drive, 5=deep-research report entry
      - research_task_id:   str — task/report ID that produced this source
      - report_markdown:    str — deep-research report markdown (for type-5 entries)
    """

async def import_sources(notebook_id: str, task_id: str, sources: list[dict]) -> list[dict]:
    """
    sources: list of dicts with 'url' and 'title' keys. Deep-research entries
        from poll() may also include 'report_markdown', 'result_type', and
        'research_task_id'.
    Returns: list of imported sources with 'id' and 'title'.

    Raises:
      - ValidationError if `sources` contains entries from more than one
        research task (`research_task_id` mismatch). Import each task's
        sources in a separate call.

    Caveats:
      - The API response can under-report — fewer items may come back than
        were actually imported. After this call, re-list with
        `client.sources.list(notebook_id)` to verify the final source set.
      - Entries without a `url` and without a complete report (`title` +
        `report_markdown` + `result_type == 5`) are skipped with a warning.
    """
```

**Example:**
```python
# Start fast web research (default)
result = await client.research.start(nb_id, "AI safety regulations")
if result is None:
    raise RuntimeError("Research start returned None")
task_id = result["task_id"]

# Start deep web research
result = await client.research.start(nb_id, "quantum computing", source="web", mode="deep")
if result is None:
    raise RuntimeError("Research start returned None")
task_id = result["task_id"]

# Start fast Drive research
result = await client.research.start(nb_id, "project docs", source="drive", mode="fast")
if result is None:
    raise RuntimeError("Research start returned None")

# Poll until complete
import asyncio
while True:
    status = await client.research.poll(nb_id)
    if status["status"] == "completed":
        break
    await asyncio.sleep(10)

# Import discovered sources
imported = await client.research.import_sources(nb_id, task_id, status["sources"][:5])
print(f"Imported {len(imported)} sources")
```

---

### NotesAPI (`client.notes`)

**CLI equivalent:** [Note Commands](cli-reference.md#note-commands-notebooklm-note-cmd) — `notebooklm note list`, `create`, `get`, `save`, `rename`, `delete`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `list(notebook_id)` | `str` | `list[Note]` | List text notes (excludes mind maps) |
| `create(notebook_id, title="New Note", content="")` | `str, str, str` | `Note` | Create note |
| `get(notebook_id, note_id)` | `str, str` | `Optional[Note]` | Get note by ID |
| `update(notebook_id, note_id, content, title)` | `str, str, str, str` | `None` | Update note content and title |
| `delete(notebook_id, note_id)` | `str, str` | `bool` | Delete note |
| `list_mind_maps(notebook_id)` | `str` | `list[Any]` | List mind maps in the notebook |
| `delete_mind_map(notebook_id, mind_map_id)` | `str, str` | `bool` | Delete a mind map |

**Example:**
```python
# Create and manage notes
note = await client.notes.create(nb_id, title="Meeting Notes", content="Discussion points...")
notes = await client.notes.list(nb_id)

# Update a note
await client.notes.update(nb_id, note.id, "Updated content", "New Title")

# Delete a note
await client.notes.delete(nb_id, note.id)
```

**Mind Maps:**

Mind maps are stored internally using the same structure as notes but contain JSON data with hierarchical node information. The `list()` method excludes mind maps automatically, while `list_mind_maps()` returns only mind maps.

```python
# List all mind maps in a notebook
mind_maps = await client.notes.list_mind_maps(nb_id)
for mm in mind_maps:
    mm_id = mm[0]  # Mind map ID is at index 0
    print(f"Mind map: {mm_id}")

# Delete a mind map
await client.notes.delete_mind_map(nb_id, mind_map_id)
```

**Note:** Mind maps are detected by checking if the content contains `'"children":' or `'"nodes":'` keys, which indicate JSON mind map data structure.

---

### SettingsAPI (`client.settings`)

**CLI equivalent:** [Language Commands](cli-reference.md#language-commands-notebooklm-language-cmd) — `notebooklm language get`, `set`, `list`. Account limits and tier do not yet have a dedicated CLI surface; use `notebooklm status` for context.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `get_output_language()` | none | `Optional[str]` | Get current output language setting |
| `get_account_limits()` | none | `AccountLimits` | Get account-level limits such as max notebooks and sources per notebook |
| `get_account_tier()` | none | `AccountTier` | Get current NotebookLM subscription tier |
| `set_output_language(language)` | `str` | `Optional[str]` | Set output language for artifact generation |

**Example:**
```python
# Get current language setting
lang = await client.settings.get_output_language()
print(f"Current language: {lang}")  # e.g., "en", "ja", "zh_Hans"

# Get server-reported account limits
limits = await client.settings.get_account_limits()
print(f"Notebook limit: {limits.notebook_limit}")

# Get current NotebookLM subscription tier
tier = await client.settings.get_account_tier()
print(f"Account tier: {tier.plan_name or tier.tier}")

# Set language for artifact generation
result = await client.settings.set_output_language("ja")  # Japanese
print(f"Language set to: {result}")
```

**Important:** Language is a **GLOBAL setting** that affects all notebooks in your account. The tier string is internal NotebookLM metadata; use `get_account_limits()` for quota decisions because the raw tier name may not match the active notebook/source limits. Supported languages include:
- `en` (English), `ja` (日本語), `zh_Hans` (中文简体), `zh_Hant` (中文繁體)
- `ko` (한국어), `es` (Español), `fr` (Français), `de` (Deutsch), `pt_BR` (Português)
- And [over 70 other languages](cli-reference.md#language-commands-notebooklm-language-cmd)

---

### SharingAPI (`client.sharing`)

**CLI equivalent:** [Share Commands](cli-reference.md#share-status-public-view-level-add-update-remove) — `notebooklm share status`, `public`, `view-level`, `add`, `update`, `remove`.

| Method | Parameters | Returns | Description |
|--------|------------|---------|-------------|
| `get_status(notebook_id)` | `str` | `ShareStatus` | Get current sharing configuration |
| `set_public(notebook_id, public)` | `str, bool` | `ShareStatus` | Enable/disable public link sharing |
| `set_view_level(notebook_id, level)` | `str, ShareViewLevel` | `ShareStatus` | Set what viewers can access |
| `add_user(notebook_id, email, permission, notify, welcome_message)` | `str, str, SharePermission, bool, str` | `ShareStatus` | Share with a user |
| `update_user(notebook_id, email, permission)` | `str, str, SharePermission` | `ShareStatus` | Update user's permission |
| `remove_user(notebook_id, email)` | `str, str` | `ShareStatus` | Remove user's access |

**Example:**
```python
from notebooklm import SharePermission, ShareViewLevel

# Get current sharing status
status = await client.sharing.get_status(notebook_id)
print(f"Public: {status.is_public}")
print(f"Users: {[u.email for u in status.shared_users]}")

# Enable public sharing (anyone with link)
status = await client.sharing.set_public(notebook_id, True)
print(f"Share URL: {status.share_url}")

# Set view level (what viewers can access)
await client.sharing.set_view_level(notebook_id, ShareViewLevel.CHAT_ONLY)

# Share with specific users
status = await client.sharing.add_user(
    notebook_id,
    "colleague@example.com",
    SharePermission.VIEWER,
    notify=True,
    welcome_message="Check out my research!"
)

# Update user permission
status = await client.sharing.update_user(
    notebook_id,
    "colleague@example.com",
    SharePermission.EDITOR
)

# Remove user access
status = await client.sharing.remove_user(notebook_id, "colleague@example.com")

# Disable public sharing
status = await client.sharing.set_public(notebook_id, False)
```

**Permission Levels:**
- `SharePermission.OWNER` - Full control (read-only, cannot be assigned)
- `SharePermission.EDITOR` - Can edit notebook content
- `SharePermission.VIEWER` - Read-only access

**View Levels:**
- `ShareViewLevel.FULL_NOTEBOOK` - Viewers can access chat, sources, and notes
- `ShareViewLevel.CHAT_ONLY` - Viewers can only access the chat interface

---

## Data Types

### Notebook

```python
@dataclass
class Notebook:
    id: str
    title: str
    created_at: Optional[datetime]
    sources_count: int
    is_owner: bool
```

### Source

```python
@dataclass
class Source:
    id: str
    title: Optional[str]
    url: Optional[str]
    created_at: Optional[datetime]
    status: int                          # 1=processing, 2=ready, 3=error, 5=preparing (defaults to READY)

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""

    @property
    def is_ready(self) -> bool:
        """status == SourceStatus.READY"""

    @property
    def is_processing(self) -> bool:
        """status == SourceStatus.PROCESSING"""

    @property
    def is_error(self) -> bool:
        """status == SourceStatus.ERROR"""
```

> **Deprecated:** `Source.source_type` emits `DeprecationWarning` and will be removed in v0.5.0 — use `Source.kind` instead. See [stability.md → Currently Deprecated](stability.md#currently-deprecated) for the full migration table.

**Type Identification:**

Use the `.kind` property to identify source types. It returns a `SourceType` enum which is also a `str`, enabling both enum and string comparisons:

```python
from notebooklm import SourceType

# Enum comparison (recommended)
if source.kind == SourceType.PDF:
    print("This is a PDF")

# String comparison (also works)
if source.kind == "pdf":
    print("This is a PDF")

# Use in f-strings
print(f"Type: {source.kind}")  # "Type: pdf"
```

### Artifact

```python
@dataclass
class Artifact:
    id: str
    title: str
    _artifact_type: int             # Internal type code; field order matters. Access via .kind.
    status: int                     # 1=processing, 2=pending, 3=completed, 4=failed
    created_at: Optional[datetime]
    url: Optional[str]
    _variant: int | None = None     # Internal variant for type-4 artifacts (1=flashcards, 2=quiz).

    @property
    def kind(self) -> ArtifactType:
        """Get artifact type as ArtifactType enum."""

    @property
    def is_completed(self) -> bool:
        """Check if artifact generation is complete."""

    @property
    def is_quiz(self) -> bool:
        """Check if this is a quiz artifact."""

    @property
    def is_flashcards(self) -> bool:
        """Check if this is a flashcards artifact."""

    @property
    def report_subtype(self) -> str | None:
        """Title-derived report subtype: 'briefing_doc', 'study_guide',
        'blog_post', or 'report' for type-2 artifacts; None otherwise.
        Use this instead of parsing titles in caller code.
        """
```

**Note on `_artifact_type` / `_variant`:** these are private (leading-underscore) fields with `repr=False` and are part of the dataclass for `from_api_response()` round-tripping. Always consume them via the public `.kind`, `.is_quiz`, `.is_flashcards`, and `.report_subtype` accessors — the underscore prefix signals that direct access is unsupported and subject to change without notice.

> **Deprecated:** `Artifact.artifact_type` and `Artifact.variant` emit `DeprecationWarning` and will be removed in v0.5.0 — use `Artifact.kind` (plus `.is_quiz` / `.is_flashcards`) instead. See [stability.md → Currently Deprecated](stability.md#currently-deprecated) for the full migration table.

**Type Identification:**

Use the `.kind` property to identify artifact types. It returns an `ArtifactType` enum which is also a `str`:

```python
from notebooklm import ArtifactType

# Enum comparison (recommended)
if artifact.kind == ArtifactType.AUDIO:
    print("This is an audio overview")

# String comparison (also works)
if artifact.kind == "audio":
    print("This is an audio overview")

# Check specific types
if artifact.is_quiz:
    print("This is a quiz")
elif artifact.is_flashcards:
    print("This is a flashcard deck")
```

### GenerationStatus

Returned by `poll_status`, `wait_for_completion`, and most artifact generation methods (`generate_audio`, `generate_video`, `generate_report`, `generate_quiz`, `generate_flashcards`, `generate_slide_deck`, `generate_infographic`, `generate_data_table`). Note that `generate_mind_map` returns a `dict[str, Any]` instead — the mind map is delivered as JSON inline rather than polled.

```python
@dataclass
class GenerationStatus:
    task_id: str                          # Same value as Artifact.id once complete
    status: str                           # "pending" | "in_progress" | "completed" | "failed" | "not_found"
    url: str | None = None                # Populated for media artifacts when status == "completed"
    error: str | None = None
    error_code: str | None = None         # e.g. "USER_DISPLAYABLE_ERROR" for rate limits
    metadata: dict[str, Any] | None = None

    @property
    def is_complete(self) -> bool:
        """Check if generation is complete."""

    @property
    def is_failed(self) -> bool:
        """Check if generation failed."""

    @property
    def is_pending(self) -> bool:
        """Check if generation is pending."""

    @property
    def is_not_found(self) -> bool:
        """Check if the artifact is absent from the poll response.

        Distinct from ``is_pending``: a *pending* artifact exists in the
        artifact list and is queued, while *not_found* means the artifact
        has either not yet appeared (brief lag after creation) or was
        silently removed server-side (e.g. after a daily-quota rejection).
        ``wait_for_completion`` treats a sustained run of ``not_found``
        responses as a failure — see its ``max_not_found`` parameter.
        """

    @property
    def is_rate_limited(self) -> bool:
        """Check if generation failed due to rate limiting."""
```

**`url` semantics:** `poll_status` populates `url` for media artifact types (audio, video, infographic, slide-deck PDF) as soon as the server reports the asset as ready. Slide decks expose the PDF URL here; for the editable PowerPoint, use `client.artifacts.download_slide_deck(..., output_format="pptx")` instead.

```python
status = await client.artifacts.generate_audio(notebook_id)
final = await client.artifacts.wait_for_completion(notebook_id, status.task_id)
if final.is_complete and final.url:
    # Stream the asset directly instead of re-fetching artifact metadata
    ...
```

### AskResult

```python
@dataclass
class AskResult:
    answer: str                        # The answer text with inline citations [1], [2], etc.
    conversation_id: str               # ID for follow-up questions
    turn_number: int                   # Turn number in conversation
    is_follow_up: bool                 # Whether this was a follow-up question
    references: list[ChatReference]    # Source references cited in the answer
    raw_response: str                  # First 1000 chars of raw API response

@dataclass
class ChatReference:
    source_id: str                     # UUID of the source
    citation_number: int | None        # Citation number in answer (1, 2, etc.)
    cited_text: str | None             # Actual text passage being cited
    start_char: int | None             # Start position in source content
    end_char: int | None               # End position in source content
    chunk_id: str | None               # Internal chunk ID (for debugging)
```

**Important:** The `cited_text` field often contains only a snippet or section header, not the full quoted passage. The `start_char`/`end_char` positions reference NotebookLM's internal chunked index, which does not directly correspond to positions in the raw fulltext returned by `get_fulltext()`.

Use `SourceFulltext.find_citation_context()` to locate citations in the fulltext:

```python
fulltext = await client.sources.get_fulltext(notebook_id, ref.source_id)
matches = fulltext.find_citation_context(ref.cited_text)  # Returns list[(context, position)]

if matches:
    context, pos = matches[0]  # First match
    if len(matches) > 1:
        print(f"Warning: {len(matches)} matches found, using first")
else:
    context = None  # Not found - may occur if source was modified
```

**Tip:** Cache `fulltext` when processing multiple citations from the same source to avoid repeated API calls.

### ShareStatus

```python
@dataclass
class ShareStatus:
    notebook_id: str                   # The notebook ID
    is_public: bool                    # Whether publicly accessible
    access: ShareAccess                # RESTRICTED or ANYONE_WITH_LINK
    view_level: ShareViewLevel         # FULL_NOTEBOOK or CHAT_ONLY
    shared_users: list[SharedUser]     # List of users with access
    share_url: str | None              # Public URL if is_public=True
```

### SharedUser

```python
@dataclass
class SharedUser:
    email: str                         # User's email address
    permission: SharePermission        # OWNER, EDITOR, or VIEWER
    display_name: str | None           # User's display name
    avatar_url: str | None             # URL to user's avatar image
```

### AccountLimits

Returned by `client.settings.get_account_limits()`. Use these fields for
quota decisions in preference to the raw tier string — the server-reported
limits are what NotebookLM actually enforces.

```python
@dataclass(frozen=True)
class AccountLimits:
    notebook_limit: int | None = None  # Max notebooks the account can hold
    source_limit: int | None = None    # Max sources per notebook
    raw_limits: tuple[Any, ...] = ()   # Untouched RPC payload for forensic use
```

### AccountTier

Returned by `client.settings.get_account_tier()`. Raw tier metadata from
NotebookLM's homepage tier RPC. `plan_name` is the user-facing label when
available (e.g. `"NotebookLM Pro"`); `tier` is the internal identifier
(`"STANDARD"`, `"PLUS"`, `"PRO"`, `"PRO_DASHER_END_USER"`, `"ULTRA"`).

```python
@dataclass(frozen=True)
class AccountTier:
    tier: str | None = None        # Internal tier identifier
    plan_name: str | None = None   # User-facing plan label
```

```python
tier = await client.settings.get_account_tier()
label = tier.plan_name or tier.tier or "unknown"
```

### SourceFulltext

```python
@dataclass
class SourceFulltext:
    source_id: str                     # UUID of the source
    title: str                         # Source title
    content: str                       # Full indexed text content
    url: str | None                    # Original URL (if applicable)
    char_count: int                    # Character count

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""

    def find_citation_context(
        self,
        cited_text: str,
        context_chars: int = 200,
    ) -> list[tuple[str, int]]:
        """Search for citation text, return list of (context, position) tuples."""
```

> **Deprecated:** `SourceFulltext.source_type` emits `DeprecationWarning` and will be removed in v0.5.0 — use `SourceFulltext.kind` instead. See [stability.md → Currently Deprecated](stability.md#currently-deprecated) for the full migration table.

**Type Identification:**

Like `Source`, use the `.kind` property to get the source type:

```python
fulltext = await client.sources.get_fulltext(nb_id, source_id)
print(f"Content type: {fulltext.kind}")  # "pdf", "web_page", etc.
```

---

## Enums

### Audio Generation

```python
class AudioFormat(Enum):
    DEEP_DIVE = 1   # In-depth discussion
    BRIEF = 2       # Quick summary
    CRITIQUE = 3    # Critical analysis
    DEBATE = 4      # Two-sided debate

class AudioLength(Enum):
    SHORT = 1
    DEFAULT = 2
    LONG = 3
```

### Video Generation

```python
class VideoFormat(Enum):
    EXPLAINER = 1
    BRIEF = 2
    CINEMATIC = 3

class VideoStyle(Enum):
    AUTO_SELECT = 1
    CUSTOM = 2
    CLASSIC = 3
    WHITEBOARD = 4
    KAWAII = 5
    ANIME = 6
    WATERCOLOR = 7
    RETRO_PRINT = 8
    HERITAGE = 9
    PAPER_CRAFT = 10
```

### Quiz/Flashcards

```python
class QuizQuantity(Enum):
    FEWER = 1
    STANDARD = 2
    MORE = 2  # Alias of STANDARD used by the CLI/web UI

class QuizDifficulty(Enum):
    EASY = 1
    MEDIUM = 2
    HARD = 3
```

### Reports

```python
class ReportFormat(str, Enum):
    BRIEFING_DOC = "briefing_doc"
    STUDY_GUIDE = "study_guide"
    BLOG_POST = "blog_post"
    CUSTOM = "custom"
```

### Infographics

```python
class InfographicOrientation(Enum):
    LANDSCAPE = 1
    PORTRAIT = 2
    SQUARE = 3

class InfographicDetail(Enum):
    CONCISE = 1
    STANDARD = 2
    DETAILED = 3
```

### Slide Decks

```python
class SlideDeckFormat(Enum):
    DETAILED_DECK = 1
    PRESENTER_SLIDES = 2

class SlideDeckLength(Enum):
    DEFAULT = 1
    SHORT = 2
```

### Export

```python
class ExportType(Enum):
    DOCS = 1    # Export to Google Docs
    SHEETS = 2  # Export to Google Sheets
```

### Sharing

```python
class ShareAccess(Enum):
    RESTRICTED = 0        # Only explicitly shared users
    ANYONE_WITH_LINK = 1  # Public link access

class ShareViewLevel(Enum):
    FULL_NOTEBOOK = 0     # Chat + sources + notes
    CHAT_ONLY = 1         # Chat interface only

class SharePermission(Enum):
    OWNER = 1             # Full control (read-only, cannot assign)
    EDITOR = 2            # Can edit notebook
    VIEWER = 3            # Read-only access
```

### Source and Artifact Types

```python
class SourceType(str, Enum):
    """Source types - use with source.kind property.

    This is a str enum, enabling both enum and string comparisons:
        source.kind == SourceType.PDF   # True
        source.kind == "pdf"            # Also True
    """
    GOOGLE_DOCS = "google_docs"
    GOOGLE_SLIDES = "google_slides"
    GOOGLE_SPREADSHEET = "google_spreadsheet"
    PDF = "pdf"
    PASTED_TEXT = "pasted_text"
    WEB_PAGE = "web_page"
    GOOGLE_DRIVE_AUDIO = "google_drive_audio"
    GOOGLE_DRIVE_VIDEO = "google_drive_video"
    YOUTUBE = "youtube"
    MARKDOWN = "markdown"
    DOCX = "docx"
    CSV = "csv"
    EPUB = "epub"
    IMAGE = "image"
    MEDIA = "media"
    UNKNOWN = "unknown"

class ArtifactType(str, Enum):
    """Artifact types - use with artifact.kind property.

    This is a str enum that hides internal variant complexity.
    Quizzes and flashcards are distinguished automatically.
    """
    AUDIO = "audio"
    VIDEO = "video"
    REPORT = "report"
    QUIZ = "quiz"
    FLASHCARDS = "flashcards"
    MIND_MAP = "mind_map"
    INFOGRAPHIC = "infographic"
    SLIDE_DECK = "slide_deck"
    DATA_TABLE = "data_table"
    UNKNOWN = "unknown"

class SourceStatus(Enum):
    PROCESSING = 1  # Source is being processed (indexing content)
    READY = 2       # Source is ready for use
    ERROR = 3       # Source processing failed
    PREPARING = 5   # Source is being prepared/uploaded (pre-processing stage)
```

**Usage Example:**
```python
from notebooklm import SourceType, ArtifactType

# List sources by type using .kind property
sources = await client.sources.list(nb_id)
for src in sources:
    if src.kind == SourceType.PDF:
        print(f"PDF: {src.title}")
    elif src.kind == SourceType.MEDIA:
        print(f"Audio/Video: {src.title}")
    elif src.kind == SourceType.IMAGE:
        print(f"Image (OCR'd): {src.title}")
    elif src.kind == SourceType.UNKNOWN:
        print(f"Unknown type: {src.title}")

# List artifacts by type using .kind property
artifacts = await client.artifacts.list(nb_id)
for art in artifacts:
    if art.kind == ArtifactType.AUDIO:
        print(f"Audio: {art.title}")
    elif art.kind == ArtifactType.VIDEO:
        print(f"Video: {art.title}")
    elif art.kind == ArtifactType.QUIZ:
        print(f"Quiz: {art.title}")
```

### Chat Configuration

```python
class ChatGoal(Enum):
    DEFAULT = 1        # General purpose
    CUSTOM = 2         # Uses custom_prompt
    LEARNING_GUIDE = 3 # Educational focus

class ChatResponseLength(Enum):
    DEFAULT = 1
    LONGER = 4
    SHORTER = 5

class ChatMode(Enum):
    """Predefined chat modes for common use cases (service-level enum)."""
    DEFAULT = "default"          # General purpose
    LEARNING_GUIDE = "learning_guide"  # Educational focus
    CONCISE = "concise"          # Brief responses
    DETAILED = "detailed"        # Verbose responses
```

**ChatGoal vs ChatMode:**
- `ChatGoal` is an RPC-level enum used with `client.chat.configure()` for low-level API configuration
- `ChatMode` is a service-level enum providing predefined configurations for common use cases

---

## Advanced Usage

### Custom RPC Calls

For undocumented features, you can make raw RPC calls:

```python
from notebooklm.rpc import RPCMethod

async with await NotebookLMClient.from_storage() as client:
    # Each RPCMethod member has its own params shape (a nested list) and
    # source_path; mirror the higher-level APIs when in doubt.
    result = await client.rpc_call(
        RPCMethod.CREATE_NOTEBOOK,
        params=["My Notebook", None, None, [2], [1]],
    )
```

### Handling Rate Limits

Google rate limits aggressive API usage:

```python
import asyncio
from notebooklm import RPCError

async def safe_create_notebooks(client, titles):
    for title in titles:
        try:
            await client.notebooks.create(title)
        except RPCError:
            # Wait and retry on rate limit
            await asyncio.sleep(10)
            await client.notebooks.create(title)
        # Add delay between operations
        await asyncio.sleep(2)
```

### Streaming Chat Responses

The chat endpoint supports streaming (internal implementation):

```python
# Standard (non-streaming) - recommended
result = await client.chat.ask(nb_id, "Question")
print(result.answer)

# Streaming is handled internally by the library
# The ask() method returns the complete response
```
