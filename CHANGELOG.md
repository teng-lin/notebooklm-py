# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - UNRELEASED

The first release after the v0.4.x auth-keepalive series. Headline user-facing work: a top-to-bottom CLI UX overhaul (uniform `--json`, exit-code policy, shell completion, stdin pipes, SIGINT-resume), auth and cookie reliability hardening (inline PSIDTS cold-start recovery, fail-closed `notebooklm use`, concurrent-upload safety), and the v0.3-era deprecation removal cycle. **Read Breaking changes below before upgrading.**

### Breaking changes

Items that need attention when upgrading from 0.4.x. Full migration prose lives in the natural sections below.

- **`NOTEBOOKLM_STRICT_DECODE` now defaults to `1`** — RPC shape drift raises `UnknownRPCMethodError` (subclass of `RPCError`) at the decoder boundary instead of warning and returning `None`. Set `=0` to opt back into the legacy behavior for one release window (the soft-mode fallback itself now emits `DeprecationWarning` and is scheduled for removal in v0.6.0).
- **`rate_limit_max_retries` default raised from `0` to `3`** with exponential-backoff fallback. Programmatic users now inherit smart-retry behavior matching the CLI. Pass `rate_limit_max_retries=0` to restore the previous immediate-`RateLimitError` behavior. Mutating create RPCs already opt out via `disable_internal_retries=True`.
- **`server_error_max_retries` default raised from `0` to `3`** with the same exponential-backoff fallback, covering HTTP 5xx + retryable network errors (#629). Pass `server_error_max_retries=0` to restore immediate failure on 5xx.
- **`max_concurrent_rpcs` semaphore added with default `16`** (#630). High-fan-out callers (e.g. `asyncio.gather` over 100 RPCs) are now throttled by default instead of saturating the connection pool. Pass `max_concurrent_rpcs=None` to restore unbounded fan-out. Must satisfy `max_concurrent_rpcs <= ConnectionLimits.max_connections`.
- **`SourcesAPI.add_url` / `add_text` / `add_file` / `add_drive` now require `wait` and `wait_timeout` as keyword-only** (#756). Positional calls like `client.sources.add_url(nb_id, url, True)` now raise `TypeError`. Migrate to `client.sources.add_url(nb_id, url, wait=True)`. CLI is unaffected.
- **`notebooklm use <id>` fails closed when the notebook doesn't exist.** `use` now verifies the id with `NotebooksAPI.get(id)` before persisting and exits `1` without writing to `context.json` on a missing notebook / wire failure / auth-expiry. Pass `--force` to bypass verification. `NotebookNotFoundError` now inherits from both `RPCError` and `NotebookError`.
- **`source get` / `artifact get` / `note get` exit `1` on not-found (was `0`).** Matches the rest of the CLI's user-error convention so scripts can branch on the exit code. `--json` failure body uses the standard `{"error": true, "code": "NOT_FOUND", ...}` envelope.
- **`generate cinematic-video --format <non-cinematic>` exits `2` with a UsageError** instead of silently overriding the conflict. Drop the conflicting flag, or use `generate video --format <value>` if a non-cinematic format was intended.
- **`NOTEBOOKLM_REFRESH_CMD` defaults to `shell=False`** (security hardening for the shell-injection footgun when the env var is sourced from CI configs). Now parsed with `shlex.split` and invoked with `subprocess.run(argv, shell=False, ...)`. Set `NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1` (literal `"1"` only) to opt back into the legacy `shell=True`.
- **`source add` no longer follows symlinks by default.** A workspace symlink like `~/Downloads/foo.pdf → /etc/passwd` previously resolved and uploaded the target with no warning. The path now refuses symlink traversal with a `ClickException` (exit `1`) unless `--follow-symlinks` is explicit. Scripts that point at symlink-resolved paths must add the flag (#476).
- **YouTube cookies no longer scraped or trusted by default at login / refresh.** The cookie-domain allowlist split into REQUIRED (NotebookLM + Drive + RotateCookies) and OPTIONAL (YouTube / Docs / Mail / myaccount). Pass `--include-domains=youtube` (or `=all`) on `login` / `auth refresh --browser-cookies <browser>` / `auth inspect` to opt YouTube back in; pass `=docs`/`=mail`/`=myaccount` to opt those sibling domains in explicitly (#483).
- **`--storage <path>` no longer shares the default profile's notebook context.** A previously-run `notebooklm use <id>` against the default profile is invisible to a later `notebooklm --storage X.json <cmd>` (and vice versa) because `--storage` now derives a sibling `<path>.context.json`. Set the active notebook explicitly via `notebooklm --storage <path> use <id>`, `-n/--notebook`, or `NOTEBOOKLM_NOTEBOOK` env var (#467).
- **v0.3-era deprecated APIs removed** — `Source.source_type`, `Artifact.artifact_type`, `Artifact.variant`, `SourceFulltext.source_type`, `StudioContentType`, `DEFAULT_STORAGE_PATH`, `notebooklm.cli.language.save_config`. Migrate to the `.kind` property and `notebooklm.paths.get_storage_path()`. See **Removed** below.
- **Cookie identity widened to `(name, domain, path)`** per RFC 6265 §5.3. Writes remain backward-compatible (flat dicts / legacy 2-tuples still accepted); reads of `auth.cookies` with the old 2-tuple key now raise `KeyError`. Use `auth.cookies[("SID", ".google.com", "/")]`, `auth.flat_cookies["SID"]`, or `auth.cookie_header`.

### Added

#### Auth and reliability
- **Inline `__Secure-1PSIDTS` cold-start recovery.** When a storage file has `__Secure-1PSID` but no `__Secure-1PSIDTS`, a preflight POST to `accounts.google.com/RotateCookies` mints a fresh token before any RPC traffic, so cold-start workers no longer fail on the first call. Cross-process flock serializes concurrent cold starts; respects `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` ([#865](https://github.com/teng-lin/notebooklm-py/issues/865), [#872](https://github.com/teng-lin/notebooklm-py/pull/872)).
- **`NOTEBOOKLM_BASE_URL` env var for enterprise NotebookLM deployments** (#402). Routes RPC + auth traffic through a non-`google.com` base URL; cookie-domain allowlist auto-extends to the enterprise host. Previously enterprise users had to monkey-patch internals.
- **`NOTEBOOKLM_RPC_OVERRIDES` env-var escape hatch.** When Google rotates a `batchexecute` method ID, set e.g. `NOTEBOOKLM_RPC_OVERRIDES='{"LIST_NOTEBOOKS": "newId123"}'` to keep working until a patch ships. Overrides are gated to `notebooklm.google.com` / `accounts.google.com` base hosts so a redirected base can't pivot them (#486).
- **`ConnectionLimits` dataclass for httpx pool tuning.** Pass `ConnectionLimits(max_connections=200, ...)` to `NotebookLMClient(...)` for long-running agents and high-fan-out workers — no more monkey-patching internals (#527).
- **`max_concurrent_rpcs` constructor arg** (default `16`, #630). Bounds simultaneous in-flight RPCs to protect the connection pool under fan-out. `None` opts out — see **Breaking changes** for the default-shift note.
- **`--include-domains` flag on `login` / `auth refresh --browser-cookies <browser>` / `auth inspect`.** Backs the REQUIRED/OPTIONAL cookie-domain split described in **Breaking changes** — passing `=youtube`/`=docs`/`=mail`/`=myaccount` (or `=all`) opts those OPTIONAL domains back in. Accepts repeated-flag or comma-separated syntax (#483).

#### Chat
- **`client.chat.delete_conversation(notebook_id, conversation_id)` + `notebooklm ask --new` is now genuinely destructive** (#824). Captures the web UI's "Delete history" action (`J7Gthc` RPC) so callers can force-end a server-side conversation; the next `ask()` with no `conversation_id` starts a brand-new thread. **⚠ Deleted turns are not recoverable.** CLI prompts for confirmation; `--json` implies `--yes`.
- **`notebooklm ask --new` flag** (previously promised in the docstring but undeclared) — starts a fresh conversation, mutually exclusive with `--conversation-id`.
- **`notebooklm ask --timeout`** per-invocation HTTP timeout, mirroring `source add --timeout`.
- **`ChatReference.answer_range` + `.score`** (#686). Every reference now exposes the answer-text span it grounds (start/end char positions) and the model's relevance score — useful for highlighting cited passages and ranking sources.
- **`chat save` preserves inline citation hover anchors** (closes #660, [#675](https://github.com/teng-lin/notebooklm-py/pull/675)). Saved notes retain `[citation]`-style anchors so users can hover-preview the source passage that grounded each claim in the NotebookLM web UI.

#### CLI ergonomics
- **Uniform `--json` envelopes** on every detail and mutating command: `artifact get/rename/delete/poll/export`, eight `source` subcommands (`delete/rename/refresh/clean/get/delete-by-title/add-drive/stale`), `note get/save/create/delete/rename`, `notebooklm configure`, and `notebook use`. Detail commands mirror the underlying dataclasses; mutating commands emit `{"id": ..., "renamed|deleted|exported": true, ...}`.
- **Standard download flag set on `download quiz` / `download flashcards`** — `--all`, `--latest`, `--earliest`, `--name`, `--dry-run`, `--force`, `--no-clobber`, `--json` — so one wrapper script works across every artifact type.
- **Uniform `--timeout` / `--interval`** on `generate <kind> --wait`, `artifact wait`, and `source wait`.
- **`--limit=N` and `--no-truncate` on every `list` command, plus `--no-truncate` on `chat history`.** `chat history --no-truncate` lifts the hardcoded 50-char preview on Question/Answer columns.
- **Shell completion + ID-aware completers.** `notebooklm completion <bash|zsh|fish>` prints a completion script; once sourced, `-n/--notebook`, `-s/--source`, and `-a/--artifact` TAB-complete live IDs from the active profile.
- **SIGINT resume hint on long-running `--wait` ops.** Ctrl-C exits 130 with `Cancelled. Resume with: notebooklm artifact poll <task_id>` (or the parallel `source wait <source_id>`) instead of dumping a `KeyboardInterrupt` traceback. Under `--json`: `{"error": true, "code": "CANCELLED", "resume_hint": "..."}`.
- **Unix `-` stdin convention on `ask`, `note create`, `source add`, and `--prompt-file`.** `echo "what is X?" | notebooklm ask -` and similar pipelines now compose without temp files.
- **`NOTEBOOKLM_NOTEBOOK` env var + global `--quiet` flag.** `NOTEBOOKLM_NOTEBOOK=<id> notebooklm ask "..."` works without `-n/--notebook` or a prior `notebooklm use`. `--quiet` raises the package logger floor to ERROR (mutually exclusive with `-v/-vv`).
- **`source add` warns when a path-shaped argument doesn't exist.** A typo like `./missin.md` previously fell through to inline-text ingestion silently; an advisory stderr warning now fires before the source is added.
- **`--follow-symlinks` opt-in on `source add`.** See **Breaking changes** above; scripts that point at symlink-resolved paths must add the flag to keep working (#476).
- **`source clean` command** (#261). Bulk-delete failed/stale sources in a notebook; pairs with `source stale` for inspection. Supports `--all`, `--latest`, `--earliest`, `--dry-run`, and `--json`.
- **`notebooklm create --use` flag** (#220, #413). `create --use "title"` makes the new notebook the active context in one step. (Plain `create` no longer auto-switches the context — `--use` is the explicit opt-in.)
- **Chromium-profile selectors on `login --browser-cookies chromium:<profile>`** (#648). Pick a specific Chrome user profile (e.g. `chromium:Profile_1`) instead of always defaulting to the first profile. Useful for users with multiple Google accounts in one browser install.
- **`auth login --update` on `--all-accounts`** (#594). Replaces the stored state for an already-logged-in account instead of refusing on conflict.

#### Python API
- **Source fulltext markdown format.** `client.sources.get_fulltext(..., output_format="markdown")` and `source fulltext -f markdown` (closes #222). Requires the optional `markdownify` extra (`pip install "notebooklm-py[markdown]"`).
- **Public `client.rpc_call(method_id, params)`** (#646). A documented escape hatch for invoking any `batchexecute` RPC method directly when no high-level API wraps it yet. Pairs with `NOTEBOOKLM_RPC_OVERRIDES` for community self-patching while waiting on a fix.
- **Observability hooks + drain API on `NotebookLMClient`** (#643). New `on_rpc_event` callback (per-call timing + status), `client.metrics` snapshot, and `await client.drain()` for graceful shutdown. Designed for long-running agents needing visibility without monkey-patching.
- **Correlation IDs + categorized logging** (#430, #431). Every RPC carries an `X-Correlation-ID` (also surfaced on log records); log records are categorized (`rpc.call`, `rpc.retry`, `auth.refresh`, `upload.chunk`, …) for filtering. Credential redaction now covers every log surface by default.
- **Per-call upload timeouts on `sources.add_file` / `add_drive`** (#618). New `upload_timeout` / `chunk_timeout` keyword args for tuning large-file uploads against slow networks.

### Changed
- **Custom `--storage` downloads now use the selected auth file** ([#838](https://github.com/teng-lin/notebooklm-py/issues/838), [#888](https://github.com/teng-lin/notebooklm-py/pull/888)). `ArtifactDownloadService` previously snapshotted the session's storage path at construction time, so `--storage` overrides applied after construction were silently ignored on download. CLI `--storage` flag and mid-process profile switches are now inherited reliably.
- **`--storage <path>` derives a sibling `<path>.context.json` per file** (#467). Two `--storage` invocations against different files no longer leak notebook state through the default profile. Precedence: explicit `--storage` > profile > legacy home-root. (See **Breaking changes** for the script-impact note.)
- **Conversation IDs are now server-assigned** (#659, #667). `ChatAPI.ask()` returns whatever the server creates instead of minting a local UUID. Previously-saved conversation IDs from a v0.4.x session remain valid against the server.
- **Cross-event-loop reuse fails fast with `RuntimeError`** (#633). One `NotebookLMClient` instance is bound to its `open()`-time event loop; reusing it from a different loop (common in hot-reload servers, worker pools) now raises on the first authed POST instead of failing with cryptic httpx errors.
- **`notebook use` surfaces the typed auth-aware error on expired credentials.** Text mode shows the canonical "Not logged in" walkthrough with the `notebooklm login` remediation; `--json` emits the standard `AUTH_REQUIRED` envelope.
- **`download <type>` exception paths route through the typed error handler.** `--json` is honored on the exception path; `RateLimitError.retry_after` surfaces as both a JSON field and a "Retry after Ns" text line; `AuthError` shows the canonical re-auth hint.
- **`notebooklm login` and `notebooklm auth refresh` no longer leak Python tracebacks on unexpected failures.** Unexpected exceptions become a single friendly line + bug-report URL with exit code `2`; original traceback remains available at `-vv`.
- **`--wait` paths show a transient spinner with elapsed timer** and an empirical typical-duration hint where known (e.g. `typically 30-40 min` for cinematic-video). No-op under `--json`.
- **CLI group docstrings synced with the live registered subcommand set.** `source`, `download`, `artifact`, and `note` group `--help` blocks now enumerate every registered subcommand (previously missed `add-drive`, `add-research`, `clean`, `wait`, `cinematic-video`, `quiz`, `flashcards`, `suggestions`, `rename`).
- **`notebooklm --help` bins five previously-orphaned top-level commands** into primary sections: `auth` → **Session**; `metadata` → **Notebooks**; `agent` / `skill` / `language` → **Command Groups**.
- **`artifact poll` vs `artifact wait` `--help` clarified on ID kind.** `poll <task_id>` straight from `generate`; `wait <artifact_id>` resolved against `artifact list`.
- **First-run profile migration no longer races concurrent invocations** (#478). Previously two `notebooklm` invocations starting under a fresh home (container start-up races, parallel test runs, MCP worker pools) could both run the copy-and-delete migration. Lock waits past 30 s raise a domain-specific `MigrationLockTimeoutError(RuntimeError)`.
- **`RPCError.raw_response` previews capped at 80 chars; `NOTEBOOKLM_DEBUG=1` opts into full body.** Previously embedded a 500-char preview of the upstream response — noisy in CI and capable of leaking large server payloads (#479).
- **`RPCError.rpc_id` and `RPCError.code` deprecations revoked.** Both are now permanent aliases for `method_id` / `rpc_code` — removing exception diagnostic aliases can mask the original exception inside `except` handlers.
- **BREAKING: `note delete --json` without `--yes` and `note rename` lose-the-race now exit `1` (was `0`).** Two parallel surgical fixes to `cli/note.py` matching the broader `--json` exit-code convention (audit P1.T5). `notebooklm note delete <id> --json` without `--yes` now emits `{"error": true, "code": "VALIDATION_ERROR", "message": "Pass --yes to confirm deletion in --json mode", "id": ..., "notebook_id": ...}` + exit `1` (was the same payload as `{deleted: false, error: ...}` + exit `0`). `notebooklm note rename <id> "new"` when the note vanishes between the partial-ID resolve and the underlying `get` (e.g. a concurrent `note delete`) now emits the standard `{"error": true, "code": "NOT_FOUND", "message": "Note not found", "id": ..., "notebook_id": ...}` envelope + exit `1` (was `{renamed: false, error: ...}` + exit `0`). **Migration:** scripts branching on the exit code now correctly catch both misconfigurations; scripts parsing the JSON body must switch from `data["deleted"] == false` / `data["renamed"] == false` checks to `data["error"] == true` (or branch on `data["code"]`).

### Deprecated
- **`NotebookLMClient.rpc_call` kwargs `_is_retry`, `source_path`, `operation_variant`.** Emit `DeprecationWarning`; removal targets v0.6.0.
- **`NotesAPI.create_from_chat`.** Use `ChatAPI.save_answer_as_note`; removal targets v0.6.0.
- **`SourcesAPI.add_file` `mime_type` parameter.** Never wired into the resumable-upload RPC — the server derives MIME from the filename extension. Passing a non-`None` value now emits `DeprecationWarning`; removal targets v0.6.0. The separate `add_drive(..., mime_type=...)` parameter is unaffected.
- **`notebooklm source add --mime-type` on the file-source path.** A no-op when the resolved source type is `file`; using it now emits a stderr deprecation note (suppress via `NOTEBOOKLM_QUIET_DEPRECATIONS=1`). Removal targets v0.6.0. The same flag on `source add-drive` is unaffected.
- **`ArtifactsAPI.wait_for_completion(poll_interval=...)`.** Use `initial_interval=...`; `poll_interval` remains accepted until v0.6.0.
- **`NotebooksAPI.share()`.** Use `client.sharing.set_public()`. Scheduled for removal in a future major release.
- **`NOTEBOOKLM_STRICT_DECODE=0` soft-mode fallback.** Each use emits `DeprecationWarning` naming the decoder source; the soft-mode path is scheduled for removal in v0.6.0.
- **`ResearchAPI.poll(task_id=None)` default on multi-task notebooks.** When multiple research tasks are in flight, `poll()` with no `task_id` now emits `DeprecationWarning` (single-task notebooks: no warning, current behavior preserved). Scheduled for removal in a future major release.

### Removed
- **v0.3-era deprecation cycle complete.** Removed `Source.source_type`, `SourceFulltext.source_type`, `Artifact.artifact_type` (use `.kind`); `Artifact.variant` (use `.kind`, `.is_quiz`, `.is_flashcards`); `notebooklm.StudioContentType` (use `ArtifactType`); `notebooklm.DEFAULT_STORAGE_PATH` (use `notebooklm.paths.get_storage_path()`); `notebooklm.cli.language.save_config` (now private).
- **RPC raw-code `StudioContentType` aliases.** `notebooklm.rpc.types.StudioContentType` and `notebooklm.rpc.StudioContentType` removed; use `ArtifactType` for public code and `ArtifactTypeCode` only for low-level RPC internals.
- **`RPCMethod.DISCOVER_SOURCES`.** Unused enum entry never exercised by any `client.*` API.

### Fixed
- **Deep-research source import no longer requires leaving the "Add sources?" modal** ([#315](https://github.com/teng-lin/notebooklm-py/issues/315), [#882](https://github.com/teng-lin/notebooklm-py/pull/882)). The deep-research flow used to discover sources but skip the modal-confirm step, leaving sources pending until a separate UI action committed them. The CLI / `ResearchAPI.import_sources` now commits directly.
- **`DELETE_NOTE` no longer races shielded `UPDATE_NOTE` at cancel time** ([#876](https://github.com/teng-lin/notebooklm-py/pull/876)). Cancellation during an in-flight `NotesAPI.update(...)` could land a delete before the shielded write completed, then have the update resurrect the note. Cancel-time cleanup is now ordered so `DELETE_NOTE` waits for any shielded `UPDATE_NOTE` to settle.
- **Client close preserves the original exception** (#526). `NotebookLMClient.__aexit__` previously masked the original body exception when `aclose()` itself raised. Body exceptions are now preserved (chained via `__cause__`) while close-time failures still propagate; an inner shield guarantees the underlying httpx client is closed on every path.
- **Unique temp file per concurrent artifact download** (#523). Two parallel `download_*` calls against the same artifact used to share `<dest>.tmp` and clobber each other's bytes. Each invocation now allocates a unique temp file (PID + uuid suffix) and atomically renames into place.
- **`add_file` TOCTOU fix + `max_concurrent_uploads` knob** (#595). `SourcesAPI.add_file` used to open the source file twice — a path swap between the two opens could substitute a different file into a successful upload. The file is now opened once; the FD is held across size check + registration + upload. New `max_concurrent_uploads: int | None = 4` on `NotebookLMClient` caps simultaneous in-flight uploads (doubles as an FD-exhaustion guard for `asyncio.gather` fan-outs).
- **Research `task_id` cross-wire on concurrent in-flight tasks** (#619). Two research sessions in flight on the same notebook could let `ResearchAPI.poll(notebook_id)` silently return the latest task, mis-attributing source provenance to the caller's task. `poll()` gains an optional `task_id` discriminator; `import_sources()` raises the new `ResearchTaskMismatchError` (subclass of `ValidationError`) when a `research_task_id` on any source disagrees with the caller's `task_id`.
- **`RPCHealth` surfaces `httpx` exception class name on empty error messages** ([#874](https://github.com/teng-lin/notebooklm-py/pull/874)). Some `httpx` exception classes raise with empty `str(exc)`, which previously surfaced as a blank line. Health output now prefixes the class name (e.g. `ConnectTimeout:`).
- **`notebooklm login` install hint stripped the `[browser]` extra** (#416). Rich interpreted `[browser]` as a style tag, so the "Playwright not installed" message rendered as `pip install "notebooklm-py"` with no extras. Fixed by `markup=False`; also corrected the package name from `notebooklm` to `notebooklm-py`.
- **Per-create-RPC idempotency hardening** ([#801](https://github.com/teng-lin/notebooklm-py/pull/801), [#806](https://github.com/teng-lin/notebooklm-py/pull/806), [#808](https://github.com/teng-lin/notebooklm-py/pull/808), [#809](https://github.com/teng-lin/notebooklm-py/pull/809), [#813](https://github.com/teng-lin/notebooklm-py/pull/813)). Six-policy idempotency registry with probe-then-retry semantics for `ADD_SOURCE`, `ADD_SOURCE_FILE`, `CREATE_NOTE`, `CREATE_ARTIFACT`, `GENERATE_MIND_MAP`, and `START_RESEARCH` / `IMPORT_SOURCES`. Resolves duplicate-create on transient retries while still raising clear errors for genuine probe failures.

### Security
- **Comprehensive secret-leak audit closed across logging, auth, and URL handling** ([#746](https://github.com/teng-lin/notebooklm-py/pull/746), [#803](https://github.com/teng-lin/notebooklm-py/pull/803), [#903](https://github.com/teng-lin/notebooklm-py/pull/903)). A multi-iteration sweep tightening every surface that could leak credentials or grant codes:
  - `payload_preview`, `final_url`, and share-URL IDs scrubbed in error paths (#746).
  - `repr()` redaction on auth objects, `NOTEBOOKLM_REFRESH_CMD` stdout/stderr redaction, Playwright cookie-jar domain filter, atomic profile-state writes (#803).
  - Standalone `__Secure-1PSIDTS` / `__Secure-3PSIDTS` / `__Secure-1PAPISID` / `__Secure-3PAPISID` cookie redaction in `_logging.py` (previously only caught inside `Cookie:` / `Set-Cookie:` header values); `_safe_url` redacts the URL **path** with `/<redacted>` on Google OAuth hosts (`accounts.google.com`, `oauth2.googleapis.com`, `oauth2.googleusercontent.com`) and subdomains, so opaque grant codes in paths like `/o/oauth2/auth/<token>` no longer leak through `ValueError` interpolations or CSRF / session-id drift surfaces (#903).

## [0.4.1] - 2026-05-11

> **Compatibility note.** Despite a few additive items (`notebooklm auth refresh` CLI, `keepalive=` constructor argument on `NotebookLMClient`, `NOTEBOOKLM_REFRESH_CMD` env var, two new dataclass fields), 0.4.1 is shipped as a patch release because the dominant work — and the reason to ship now — is auth/cookie stability remediation. Bumping to v0.5.0 would force the long-deferred removal of v0.3-era deprecated APIs (see [Stability](docs/stability.md)) earlier than scheduled; we'd rather keep that change isolated from the auth-keepalive work. All additive items are backward compatible — existing code keeps working without changes.

### Added
- **`notebooklm auth refresh` CLI command** - One-shot keepalive that opens a session, triggers the layer-1 SIDTS rotation poke against `accounts.google.com`, persists the rotated cookies to `storage_state.json`, and exits. Designed to be scheduled by the OS (launchd / systemd / cron / Task Scheduler / k8s CronJob) to keep an idle profile from staling out between user-driven calls. Pairs naturally with `--quiet` for log-only-on-error cron output. Requires file/profile-backed authentication — explicitly refuses to run when `NOTEBOOKLM_AUTH_JSON` is set (no writable backing store). See `docs/troubleshooting.md` for per-OS scheduler recipes (#336).
- **Periodic keepalive task on `NotebookLMClient`** - Long-lived clients (agents, workers, multi-hour `async with` blocks) can opt into a background task that periodically POSTs `RotateCookies` to drive `__Secure-1PSIDTS` rotation, then persists rotated cookies to `storage_state.json` immediately so a crash doesn't lose the freshness. Disabled by default — pass `keepalive=<seconds>` to `NotebookLMClient(...)` or `NotebookLMClient.from_storage(...)` to enable. Values below `keepalive_min_interval` (default 60 s) are clamped up to that floor. The loop swallows transient errors at DEBUG and continues; cancellation on `__aexit__` is clean. Persistence runs off-loop via `asyncio.to_thread` so the loop never blocks on disk I/O. Closes the gap left by the per-call layer-1 poke for clients that never re-call `fetch_tokens` (#297, #312, #341).
- **Auto-refresh on auth expiry** - `fetch_tokens` now optionally runs a user-provided shell command when a Google session cookie has expired, reloads cookies from the same storage path, and retries once. Opt in by setting the `NOTEBOOKLM_REFRESH_CMD` environment variable to a command that rewrites `storage_state.json` (e.g. a sync script reading from a cookie vault). Refresh commands receive `NOTEBOOKLM_REFRESH_STORAGE_PATH` and `NOTEBOOKLM_REFRESH_PROFILE` so profile-aware scripts can target the active auth file. Covers every CLI entry point without changing the public API. Retry guards prevent refresh loops (#336).
- **`examples/refresh_browser_cookies.py`** - Sample `NOTEBOOKLM_REFRESH_CMD` script that re-extracts cookies from a live local browser via `notebooklm login --browser-cookies`. Provides a recovery path for unattended automation when the in-process keepalive isn't enough (idle gaps, force-logout, password change).
- **`Source.created_at` and `GenerationStatus.url` public dataclass fields** - `Source.created_at` is now populated for both nested and deeply-nested response paths. `GenerationStatus.url` is now populated by `poll_status` for media artifact types (audio, video, infographic, slide-deck PDF) so callers can stream the asset as soon as the status flips to ready (#349, #356).
- **`ALLOWED_COOKIE_DOMAINS` extended for sibling Google products** - The browser-cookie import path now accepts cookies from Google's sibling product domains, restoring `--browser-cookies` flows for users whose active Google session lives on a sibling surface rather than `notebooklm.google.com` directly (#362).

### Fixed
- **Cookies could silently stale out under sustained use** - `fetch_tokens` now POSTs to `https://accounts.google.com/RotateCookies` (Chrome's dedicated unsigned rotation endpoint) before hitting `notebooklm.google.com` to drive `__Secure-1PSIDTS` / `__Secure-3PSIDTS` rotation. Empirically validated against both DBSC-bound (Playwright-minted) and unbound (Firefox-imported) profiles. RPC traffic against `notebooklm.google.com` alone does not appear to trigger rotation, so a keepalive that hit NotebookLM alone could silently stale out. The rotated `Set-Cookie` lands in the live `httpx` jar and is persisted via `save_cookies_to_storage()` along the `fetch_tokens_with_domains` / `AuthTokens.from_storage` paths. A 60 s mtime guard rate-limits the layer-1 poke — the POST is skipped when storage was recently rotated. Failures log at DEBUG and never abort token fetch. Disable with `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` (e.g. networks that block `accounts.google.com`). Closes #312 (#345, #346).
- **Concurrent `RotateCookies` poke stampede** - The 60 s mtime guard only debounces *sequential* invocations; under `asyncio.gather` fan-out, parallel CLI loops, or MCP worker pools, all callers see the same stale `storage_state.json` mtime and stampede the POST. Three layered protections inside `_poke_session`: a per-event-loop, per-storage-path async lock registry plus a sync state lock for in-process dedup (an `asyncio.gather` of 10 fires exactly one POST), a non-blocking `LOCK_EX | LOCK_NB` flock on the new `.storage_state.json.rotate.lock` sentinel for cross-process dedup (parallel CLI loops / MCP workers skip silently when another process is rotating), and a failure-stampede protection where the timestamp updates regardless of POST outcome — so a 15 s timeout against a hung `accounts.google.com` doesn't let 10 fanned-out callers each wait the full timeout. The layer-2 keepalive loop now calls the bare `_rotate_cookies` directly (it's already self-paced via `keepalive_min_interval`) and `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` continues to disable both layers (#347, #348).
- **`Notebook.sources_count` parsed but never surfaced** - The `sources_count` field on the public `Notebook` dataclass is now populated from `data[1]` on both LIST and GET notebook shapes; previously it always read as `0` regardless of actual source count (#350).
- **`Artifact.url` unpopulated for media artifacts** - The `url` field on the public `Artifact` dataclass is now populated for media types (audio, video, infographic; slide-deck exposes the PDF URL — use `download_slide_deck(output_format="pptx")` for PPTX) so callers no longer need to drop down to `download_*` to obtain the asset URL (#349, #356).
- **Cross-process and refresh-path save races** - Close lifecycle and refresh-path saves now serialize correctly with the keepalive writer; concurrent writers no longer overwrite each other's rotated cookies (#344).
- **Keepalive ↔ close serialization; stop mutating caller `Auth`** - The keepalive task no longer races with `__aexit__`, and no longer mutates the `Auth` instance the caller passed in. Callers that share an `Auth` across multiple clients now get the isolation the API documented (#343).
- **Snapshot keepalive cookie jar; normalize explicit `storage_path`** - The keepalive task now snapshots the live `httpx` jar before writing (avoiding torn writes when an RPC is mid-flight); an explicit `storage_path=` argument to `NotebookLMClient` is normalized onto the `Auth` instance so the keepalive task writes to the file the caller actually pointed at (#342).
- **Per-domain cookie scoping on file upload** - File-upload requests now send only cookies whose `Domain` attribute applies to the upload host, instead of the full jar. Prevents upload rejection when the jar mixes cookies for `google.com`, `notebooklm.google.com`, and `googleusercontent.com` (#373, #374).
- **Two-tier cookie validation pre-flight** - Auth loaders now distinguish "missing-but-recoverable" from "fatal" cookie states before attempting an RPC, surfacing clearer errors and avoiding doomed requests against Google's identity surface (#372).
- **Preserve cookie attributes on load** - `Domain`, `Path`, `Secure`, `HttpOnly`, and `SameSite` attributes round-trip through storage load, restoring behaviors that depended on cross-host scoping (#365, #368).
- **Unify flat-cookie selection across loaders** - Legacy flat-cookie and modern Playwright storage shapes now share a single selection contract; subtle mismatches between the two paths are eliminated (#375, #376).
- **Tolerate non-numeric / out-of-range timestamp values on dataclasses** - `Notebook.created_at`, `Source.created_at`, and `Artifact.created_at` now catch `TypeError`, `ValueError`, `OSError`, and `OverflowError` from `datetime.fromtimestamp` and resolve to `None` instead of raising on edge-case server responses (#357).
- **`examples/refresh_browser_cookies.py` `--profile` placement** - The example invoked `... login --browser-cookies <b> --profile <p>` but `--profile` is a top-level Click option and was rejected after `login` (`Error: No such option: --profile`). Now invokes `... --profile <p> login --browser-cookies <b>` and works end-to-end against profile-backed storage.

### Infrastructure
- **Consolidated URL extraction** - `_extract_artifact_url`, per-type extractors (audio/video/infographic/slide-deck), and `_is_valid_artifact_url` moved to `types.py`. Readiness checks, `Artifact.url`, `GenerationStatus.url`, and the download paths now share one URL-selection contract: `mp4` quality-4 > any `mp4` > first valid URL for video. `SourcesAPI.get_fulltext` fixed for YouTube fulltext URLs at `metadata[5][0]` along the way (#349, #356).
- **Removed redundant `ArtifactsAPI` URL helpers** - Private `_is_valid_media_url` and `_find_infographic_url` shim methods removed; tests now exercise the canonical `types.py` helpers (#358).
- **E2E `--profile` pytest flag** - `pytest --profile <name>` scopes the E2E notebook ID cache to a named profile, so parallel multi-profile test runs don't collide on the cached notebook fixture (#340).

## [0.4.0] - 2026-05-09

### Added
- **Multi-account profiles** - Switch between Google accounts without re-authenticating (#227)
  - `notebooklm profile create/list/switch/rename/delete` commands
  - Global `--profile` / `-p` flag and `NOTEBOOKLM_PROFILE` environment variable to scope any command to a profile
  - Per-profile storage paths under `~/.notebooklm/profiles/<name>/`
  - Implicit default profile preserved for backward compatibility; existing `~/.notebooklm/storage_state.json` is auto-detected as the default profile (no manual migration needed)
- **`notebooklm doctor` diagnostic command** - `notebooklm doctor [--fix] [--json]` checks profile setup, auth, and migration status; reports actionable issues
- **Microsoft Edge SSO login** - `notebooklm login --browser msedge` for organizations that require Edge for SSO (#204)
- **Browser cookie import** - Reuse cookies from your existing browser session without driving Playwright
  - `notebooklm login --browser-cookies <browser>` (chrome, edge, firefox, safari, etc.)
  - New `convert_rookiepy_cookies_to_storage_state()` Python helper
  - Optional `[cookies]` extra installs `rookiepy` (`pip install "notebooklm-py[cookies]"`)
  - Honors the active profile: `notebooklm --profile <name> login --browser-cookies <browser>` writes to that profile's `storage_state.json`. Note that cookie extraction always pulls the source browser's currently-active Google account for `google.com` / `notebooklm.google.com` — to populate multiple profiles from the same browser, switch the active Google account in the browser between runs (or use a separate browser per profile).
- **EPUB source type** - Upload `.epub` files as notebook sources (#231)
- **Agent skill installation** - Install the bundled NotebookLM skill into local AI agents (#206, #207)
  - `notebooklm skill install` - Install into `~/.claude/skills/notebooklm` and `~/.agents/skills/notebooklm`
  - `notebooklm skill status` - Check installation state
  - `notebooklm agent show codex` / `notebooklm agent show claude` - Print bundled agent templates
- **Mind map customization** - `client.artifacts.generate_mind_map()` now accepts `language` and `instructions` parameters (#252)
- **`note list --json`** - Machine-readable note listings (#259)
- **Bare status codes in decoder errors** - Decoder surfaces server status codes on null RPC results for clearer diagnostics (#114, #294)

### Fixed
- **Cross-domain cookie preservation** - Login storage state retains cookies across `google.com` and `notebooklm.google.com` subdomains, restoring sessions for regional domains
- **NotebookLM subdomain cookies** - Subdomain cookies are no longer dropped during login (#334)
- **Video artifact detection** - Correctly detect completed video media URLs in polling responses (#333)
- **Research import on unavailable snapshots** - CLI gracefully handles missing source snapshots during research import (#335)
- **Source import retry** - Filtered partial-import retry payloads and tightened verification to avoid false positives (#321, #327)
- **Server-state verification on timeout** - Prevents duplicate inflation when source imports time out (#319)
- **Playwright navigation interruption** - Handles updated Playwright behavior on already-authenticated sessions (#214, #322)
- **Login subprocess on Windows** - Use `sys.executable` for Playwright subprocess calls (#279)
- **Legacy Windows Unicode output** - Sanitized output streams for legacy Windows consoles (#324)
- **Settings quota errors** - Use account limits when reporting create-quota failures (#328)
- **Chat references** - Emit references only from the winning chunk to avoid >600-element duplication (#300, #310)
- **Login retry mechanism** - Resolved race conditions and improved error handling on retry (#243)
- **Quota detection during polling** - Detect quota / daily-limit failures during artifact polling (#240)
- **Google account switching** - Fixed switching between Google accounts at login time (#246)
- **YouTube URL extraction** - Extract YouTube URLs at deeply-nested response positions (#265)
- **Bare-HTTP URL fallback** - Disabled brittle bare-HTTP fallback in `sources.list()` (#294)
- **Logout context cleanup** - Clear the active notebook context on `notebooklm logout`
- **Infographic URL extraction** - Aligned with download-path logic; added regression test (#229)
- **Custom storage path for downloads** - Artifact downloads now respect custom auth storage paths (#235)
- **Windows file permissions** - Skip Unix-only `0o600` calls on Windows and rely on Python 3.13+ ACL behavior (#225)
- **TOCTOU protection** - Hardened directory creation in `session.py` (#225)

### Changed
- **`rookiepy` is an optional `[cookies]` extra** - Excluded from `[all]` to avoid Python 3.13+ install issues; install with `pip install "notebooklm-py[cookies]"`
- **Login error detection** - Improved detection of missing browser binaries (e.g., `msedge` not installed)
- **Skill installation paths** - Hardened to handle alternative `~/.claude` and `~/.agents` layouts
- **Deprecation removal deferred to v0.5.0** - The deprecated APIs originally scheduled for removal in v0.4.0 — `StudioContentType`, `Source.source_type`, `SourceFulltext.source_type`, `Artifact.artifact_type`, `Artifact.variant`, and `DEFAULT_STORAGE_PATH` — continue to work and emit `DeprecationWarning`. Removal is now planned for v0.5.0 to give downstream users an extra release to migrate.

### Infrastructure
- Pinned `ruff==0.8.6` in dev deps to match pre-commit configuration
- Bumped `python-dotenv` (#299)
- Bumped `pytest` in the `uv` group
- Added contribution templates and PR quality guidelines for issues and PRs

## [0.3.4] - 2026-03-12

### Added
- **Notebook metadata export** - Added notebook metadata APIs and CLI export with a simplified sources list
  - New `notebooklm metadata` command with human-readable and `--json` output
  - New `NotebookMetadata` and `SourceSummary` public types
  - New `client.notebooks.get_metadata()` helper
- **Cinematic Video Overview support** - Added cinematic generation and download flows
  - `notebooklm generate video --format cinematic`
- **Infographic styles** - Added CLI support for selecting infographic visual styles
- **`source delete-by-title`** - Added explicit exact-title deletion command for sources

### Fixed
- **Research imports on timeout** - CLI research imports now retry on timeout with backoff
- **Metadata command behavior** - Aligned metadata output and implementation with current CLI patterns
- **Regional login cookies** - Improved browser login handling for regional Google domains
- **Notebook summary parsing** - Fixed notebook summary response parsing
- **Source delete UX** - Improved source delete resolution, ambiguity handling, and title-vs-ID errors
- **Empty downloads** - Raise an error instead of producing zero-byte files
- **Module execution** - Added `python -m notebooklm` support

### Changed
- **Documentation refresh** - Updated release, development, CLI, README, and Python API docs for current commands, APIs, and `uv` workflows
- **Public API surface** - Exported `NotebookMetadata`, `SourceSummary`, and `InfographicStyle`

## [0.3.3] - 2026-03-03

### Added
- **`ask --save-as-note`** - Save chat answers as notebook notes directly from the CLI (#135)
  - `notebooklm ask "question" --save-as-note` - Save response as a note
  - `notebooklm ask "question" --save-as-note --note-title "Title"` - Save with custom title
- **`history --save`** - Save full conversation history as a notebook note (#135)
  - `notebooklm history --save` - Save history with default title
  - `notebooklm history --save --note-title "Title"` - Save with custom title
  - `notebooklm history --show-all` - Show full Q&A content instead of preview
- **`generate report --append`** - Append custom instructions to built-in report format templates (#134)
  - Works with `briefing-doc`, `study-guide`, and `blog-post` formats (no effect on `custom`)
  - Example: `notebooklm generate report --format study-guide --append "Target audience: beginners"`
- **`generate revise-slide`** - Revise individual slides in an existing slide deck (#129)
  - `notebooklm generate revise-slide "prompt" --artifact <id> --slide 0`
- **PPTX download for slide decks** - Download slide decks as editable PowerPoint files (#129)
  - `notebooklm download slide-deck --format pptx` (web UI only offers PDF)

### Fixed
- **Partial artifact ID in download commands** - Download commands now support partial artifact IDs (#130)
- **Chat empty answer** - Fixed `ask` returning empty answer when API response marker changes (#123)
- **X.com/Twitter content parsing** - Fixed parsing of X.com/Twitter source content (#119)
- **Language sync on login** - Syncs server language setting to local config after `notebooklm login` (#124)
- **Python version check** - Added runtime check with clear error message for Python < 3.10 (#125)
- **RPC error diagnostics** - Improved error reporting for GET_NOTEBOOK and auth health check failures (#126, #127)
- **Conversation persistence** - Chat conversations now persist server-side; conversation ID shown in `history` output (#138)
- **History Q&A previews** - Fixed populating Q&A previews using conversation turns API (#136)
- **`generate report --language`** - Fixed missing `--language` option for report generation (#109)

### Changed
- **Chat history API** - Simplified history retrieval; removed `exchange_id`, improved conversation grouping with parallel fetching (#140, #141)
- **Conversation ID tracking** - Server-side conversation lookup via new `hPTbtc` RPC (`GET_LAST_CONVERSATION_ID`) replaces local exchange ID tracking
- **History Q&A population** - Now uses `khqZz` RPC (`GET_CONVERSATION_TURNS`) to fetch full Q&A turns with accurate previews (#136)

### Infrastructure
- Bumped `actions/upload-artifact` from v6 to v7 (#131)

## [0.3.2] - 2026-01-26

### Fixed
- **CLI conversation reset** - Fixed conversation ID not resetting when switching notebooks (#97)
- **UTF-8 file encoding** - Added explicit UTF-8 encoding to all file I/O operations (#93)
- **Windows Playwright login** - Restored ProactorEventLoop for Playwright login on Windows (#91)

### Infrastructure
- Fixed E2E test teardown hook for pytest 8.x compatibility (#101)
- Added 15-second delay between E2E generation tests to avoid rate limits (#95)

## [0.3.1] - 2026-01-23

### Fixed
- **Windows CLI hanging** - Fixed asyncio ProactorEventLoop incompatibility causing CLI to hang on Windows (#79)
- **Unicode encoding errors** - Fixed encoding issues on non-English Windows systems (#80)
- **Streaming downloads** - Downloads now use streaming with temp files to prevent corrupted partial downloads (#82)
- **Partial ID resolution** - All CLI commands now support partial ID matching for notebooks, sources, and artifacts (#84)
- **Source operations** - Fixed empty array handling and `add_drive` nesting (#73)
- **Guide response parsing** - Fixed 3-level nesting in `get_guide` responses (#72)
- **RPC health check** - Handle null response in health check scripts (#71)
- **Script cleanup** - Ensure temp notebook cleanup on failure or interrupt

### Infrastructure
- Added develop branch to nightly E2E tests with staggered schedule
- Added custom branch support to nightly E2E workflow for release testing

## [0.3.0] - 2026-01-21

### Added
- **Language settings** - Configure output language for artifact generation (audio, video, etc.)
  - New `notebooklm language list` - List all 80+ supported languages with native names
  - New `notebooklm language get` - Show current language setting
  - New `notebooklm language set <code>` - Set language (e.g., `zh_Hans`, `ja`, `es`)
  - Language is a **global** setting affecting all notebooks in your account
  - `--local` flag for offline-only operations (skip server sync)
  - `--language` flag on generate commands for per-command override
- **Sharing API** - Programmatic notebook sharing management
  - New `client.sharing.get_status(notebook_id)` - Get current sharing configuration
  - New `client.sharing.set_public(notebook_id, True/False)` - Enable/disable public link
  - New `client.sharing.set_view_level(notebook_id, level)` - Set viewer access (FULL_NOTEBOOK or CHAT_ONLY)
  - New `client.sharing.add_user(notebook_id, email, permission)` - Share with specific users
  - New `client.sharing.update_user(notebook_id, email, permission)` - Update user permissions
  - New `client.sharing.remove_user(notebook_id, email)` - Remove user access
  - New `ShareStatus`, `SharedUser` dataclasses for structured sharing data
  - New `ShareAccess`, `SharePermission`, `ShareViewLevel` enums
- **`SourceType` enum** - New `str, Enum` for type-safe source identification:
  - `GOOGLE_DOCS`, `GOOGLE_SLIDES`, `GOOGLE_SPREADSHEET`, `PDF`, `PASTED_TEXT`, `WEB_PAGE`, `YOUTUBE`, `MARKDOWN`, `DOCX`, `CSV`, `IMAGE`, `MEDIA`, `UNKNOWN`
- **`ArtifactType` enum** - New `str, Enum` for type-safe artifact identification:
  - `AUDIO`, `VIDEO`, `REPORT`, `QUIZ`, `FLASHCARDS`, `MIND_MAP`, `INFOGRAPHIC`, `SLIDES`, `DATA_TABLE`, `UNKNOWN`
- **`.kind` property** - Unified type access across `Source`, `Artifact`, and `SourceFulltext`:
  ```python
  # Works with both enum and string comparison
  source.kind == SourceType.PDF        # True
  source.kind == "pdf"                 # Also True
  artifact.kind == ArtifactType.AUDIO  # True
  artifact.kind == "audio"             # Also True
  ```
- **`UnknownTypeWarning`** - Warning (deduplicated) when API returns unknown type codes
- **`SourceStatus.PREPARING`** - New status (5) for sources in upload/preparation phase
- **E2E test coverage** - Added file upload tests for CSV, MP3, MP4, DOCX, JPG, Markdown with type verification
- **`--retry` flag for generation commands** - Automatic retry with exponential backoff on rate limits
  - `notebooklm generate audio --retry 3` - Retry up to 3 times on rate limit errors
  - Works with all generate commands (audio, video, quiz, etc.)
- **`ArtifactStatus.FAILED`** - New status (code 4) for artifact generation failures
- **Centralized exception hierarchy** - All errors now inherit from `NotebookLMError` base class
  - New `SourceAddError` with detailed failure messages for source operations
  - Granular exception types for better error handling in automation
- **CLI `share` command group** - Notebook sharing management from command line
  - `notebooklm share` - Enable public sharing
  - `notebooklm share --revoke` - Disable public sharing
- **Partial UUID matching for note commands** - `note get`, `note delete`, etc. now support partial IDs

### Fixed
- **Silent failures in CLI** - Commands now properly report errors instead of failing silently
- **Source type emoji display** - Improved consistency in `source list` output

### Changed
- **Source type detection** - Use API-provided type codes as source of truth instead of URL/extension heuristics
- **CLI file handling** - Simplified to always use `add_file()` for proper type detection

### Removed
- **`detect_source_type()`** - Obsolete heuristic function replaced by `Source.kind` property
- **`ARTIFACT_TYPE_DISPLAY`** - Unused constant replaced by `get_artifact_type_display()`

### Deprecated
The following emit `DeprecationWarning` when accessed and were originally scheduled for removal in v0.4.0.
See [Migration Guide](docs/stability.md#migrating-from-v02x-to-v030) for upgrade instructions.

> **Note:** Removal was subsequently deferred one release; see the [0.4.0] entry above. These names will now be removed in v0.5.0.

- **`Source.source_type`** - Use `.kind` property instead (returns `SourceType` str enum)
- **`Artifact.artifact_type`** - Use `.kind` property instead (returns `ArtifactType` str enum)
- **`Artifact.variant`** - Use `.kind`, `.is_quiz`, or `.is_flashcards` instead
- **`SourceFulltext.source_type`** - Use `.kind` property instead
- **`StudioContentType`** - Use `ArtifactType` (str enum) for user-facing code

## [0.2.1] - 2026-01-15

### Added
- **Authentication diagnostics** - New `notebooklm auth check` command for troubleshooting auth issues
  - Shows storage file location and validity
  - Lists cookies present and their domains
  - Detects `NOTEBOOKLM_AUTH_JSON` and `NOTEBOOKLM_HOME` usage
  - `--test` flag performs network validation
  - `--json` flag for machine-readable output (CI/CD friendly)
- **Structured logging** - Comprehensive DEBUG logging across library
  - `NOTEBOOKLM_LOG_LEVEL` environment variable (DEBUG, INFO, WARNING, ERROR)
  - RPC call timing and method tracking
  - Legacy `NOTEBOOKLM_DEBUG_RPC=1` still works
- **RPC health monitoring** - Automated nightly check for Google API changes
  - Detects RPC method ID mismatches before they cause failures
  - Auto-creates GitHub issues with `rpc-breakage` label on detection

### Fixed
- **Cookie domain priority** - Prioritize `.google.com` cookies over regional domains (e.g., `.google.co.uk`) for more reliable authentication
- **YouTube URL parsing** - Improved handling of edge cases in YouTube video URLs

### Documentation
- Added `auth check` to CLI reference and troubleshooting guide
- Consolidated CI/CD troubleshooting in development guide
- Added installation instructions to SKILL.md for Claude Code
- Clarified version numbering policy (PATCH vs MINOR)

## [0.2.0] - 2026-01-14

### Added
- **Source fulltext extraction** - Retrieve the complete indexed text content of any source
  - New `client.sources.get_fulltext(notebook_id, source_id)` Python API
  - New `source fulltext <source_id>` CLI command with `--json` and `-o` output options
  - Returns `SourceFulltext` dataclass with content, title, URL, and character count
- **Chat citation references** - Get detailed source references for chat answers
  - `AskResult.references` field contains list of `ChatReference` objects
  - Each reference includes `source_id`, `cited_text`, `start_char`, `end_char`, `chunk_id`
  - Use `notebooklm ask "question" --json` to see references in CLI output
- **Source status helper** - New `source_status_to_str()` function for consistent status display
- **Quiz and flashcard downloads** - Export interactive study materials in multiple formats
  - New `download quiz` and `download flashcards` CLI commands
  - Supports JSON, Markdown, and HTML output formats via `--format` flag
  - Python API: `client.artifacts.download_quiz()` and `client.artifacts.download_flashcards()`
- **Extended artifact downloads** - Download additional artifact types
  - New `download report` command (exports as Markdown)
  - New `download mind-map` command (exports as JSON)
  - New `download data-table` command (exports as CSV)
  - All download commands support `--all`, `--latest`, `--name`, and `--artifact` selection options

### Fixed
- **Regional Google domain authentication** - SID cookie extraction now works with regional Google domains (e.g., google.co.uk, google.de, google.cn) in addition to google.com
- **Artifact completion detection** - Media URL availability is now verified before reporting artifact as complete, preventing premature "ready" status
- **URL hostname validation** - Use proper URL parsing instead of string operations for security

### Changed
- **Pre-commit checks** - Added mypy type checking to required pre-commit workflow

## [0.1.4] - 2026-01-11

### Added
- **Source selection for chat and artifacts** - Select specific sources when using `ask` or `generate` commands
  - New `--sources` flag accepts comma-separated source IDs or partial matches
  - Works with all generation commands (audio, video, quiz, etc.) and chat
- **Research sources table** - `research status` now displays sources in a formatted table instead of just a count

### Fixed
- **JSON output broken in TTY terminals** - `--json` flag output was including ANSI color codes, breaking JSON parsing for commands like `notebooklm list --json`
- **Warning stacklevel** - `warnings.warn` calls now report correct source location

### Infrastructure
- **Windows CI testing** - Windows is now part of the nightly E2E test matrix
- **VCR.py integration** - Added recorded HTTP cassette support for faster, deterministic integration tests
- **Test coverage improvements** - Improved coverage for `_artifacts.py` (71% → 83%), `download.py`, and `session.py`

## [0.1.3] - 2026-01-10

### Fixed
- **PyPI README links** - Documentation links now work correctly on PyPI
  - Added `hatch-fancy-pypi-readme` plugin for build-time link transformation
  - Relative links (e.g., `docs/troubleshooting.md`) are converted to version-tagged GitHub URLs
  - PyPI users now see links pointing to the exact version they installed (e.g., `/blob/v0.1.3/docs/...`)
- **Development repository link** - Added prominent source link for PyPI users to find the GitHub repo

## [0.1.2] - 2026-01-10

### Added
- **Ruff linter/formatter** - Added to development workflow with pre-commit hooks and CI integration
- **Multi-version testing** - Docker-based test runner script for Python 3.10-3.14 (`/matrix` skill)
- **Artifact verification workflow** - New CI workflow runs 2 hours after nightly tests to verify generated artifacts

### Changed
- **Python version support** - Now supports Python 3.10-3.14 (dropped 3.9)
- **CI authentication** - Use `NOTEBOOKLM_AUTH_JSON` environment variable (inline JSON, no file writes)

### Fixed
- **E2E test cleanup** - Generation notebook fixture now only cleans artifacts once per session (was deleting artifacts between tests)
- **Nightly CI** - Fixed pytest marker from `-m e2e` to `-m "not variants"` (e2e marker didn't exist)
- macOS CI fix for Playwright version extraction (grep pattern anchoring)
- Python 3.10 test compatibility with mock.patch resolution

### Documentation
- Claude Code skill: parallel agent safety guidance
- Claude Code skill: timeout recommendations for all artifact types
- Claude Code skill: clarified `-n` vs `--notebook` flag availability

## [0.1.1] - 2026-01-08

### Added
- `NOTEBOOKLM_HOME` environment variable for custom storage location
- `NOTEBOOKLM_AUTH_JSON` environment variable for inline authentication (CI/CD friendly)
- Claude Code skill installation via `notebooklm skill install`

### Fixed
- Infographic generation parameter structure
- Mind map artifacts now persist as notes after generation
- Artifact export with proper ExportType enum handling
- Skill install path resolution for package data

### Documentation
- PyPI release checklist
- Streamlined README
- E2E test fixture documentation

## [0.1.0] - 2026-01-06

### Added
- Initial release of `notebooklm-py` - unofficial Python client for Google NotebookLM
- Full notebook CRUD operations (create, list, rename, delete)
- **Research polling CLI commands** for LLM agent workflows:
  - `notebooklm research status` - Check research progress (non-blocking)
  - `notebooklm research wait --import-all` - Wait for completion and import sources
  - `notebooklm source add-research --no-wait` - Start deep research without blocking
- **Multi-artifact downloads** with intelligent selection:
  - `download audio`, `download video`, `download infographic`, `download slide-deck`
  - Multiple artifact selection (--all flag)
  - Smart defaults and intelligent filtering (--latest, --earliest, --name, --artifact-id)
  - File/directory conflict handling (--force, --no-clobber, auto-rename)
  - Preview mode (--dry-run) and structured output (--json)
- Source management:
  - Add URL sources (with YouTube transcript support)
  - Add text sources
  - Add file sources (PDF, TXT, MD, DOCX) via native upload
  - Delete sources
  - Rename sources
- Studio artifact generation:
  - Audio overviews (podcasts) with 4 formats and 3 lengths
  - Video overviews with 9 visual styles
  - Quizzes and flashcards
  - Infographics, slide decks, and data tables
  - Study guides, briefing docs, and reports
- Query/chat interface with conversation history support
- Research agents (Fast and Deep modes)
- Artifact downloads (audio, video, infographics, slides)
- CLI with 27 commands
- Comprehensive documentation (API, RPC, examples)
- 96 unit tests (100% passing)
- E2E tests for all major features

### Fixed
- Audio overview instructions parameter now properly supported at RPC position [6][1][0]
- Quiz and flashcard distinction via title-based filtering
- Package renamed from `notebooklm-automation` to `notebooklm`
- CLI module renamed from `cli.py` to `notebooklm_cli.py`
- Removed orphaned `cli_query.py` file

### ⚠️ Beta Release Notice

This is the initial public release of `notebooklm-py`. While core functionality is tested and working, please note:

- **RPC Protocol Fragility**: This library uses undocumented Google APIs. Method IDs can change without notice, potentially breaking functionality. See [Troubleshooting](docs/troubleshooting.md) for debugging guidance.
- **Unofficial Status**: This is not affiliated with or endorsed by Google.
- **API Stability**: The Python API may change in future releases as we refine the interface.

### Known Issues

- **RPC method IDs may change**: Google can update their internal APIs at any time, breaking this library. Check the [RPC Development Guide](docs/rpc-development.md) for how to identify and update method IDs.
- **Rate limiting**: Heavy usage may trigger Google's rate limits. Add delays between bulk operations.
- **Authentication expiry**: CSRF tokens expire after some time. Re-run `notebooklm login` if you encounter auth errors.
- **Large file uploads**: Files over 50MB may fail or timeout. Split large documents if needed.

[Unreleased]: https://github.com/teng-lin/notebooklm-py/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.4...v0.4.0
[0.3.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/teng-lin/notebooklm-py/releases/tag/v0.1.0
