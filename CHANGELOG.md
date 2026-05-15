# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

This release closes the **cli-ux-remediation** arc: a top-to-bottom pass that makes the CLI shell-, CI-, and agent-friendly. The headline changes are uniform `--json` envelopes on every mutating and detail command, an explicit codebase-wide exit-code policy (with two breaking corrections), graceful long-running waits with progress and SIGINT-resume, stdin / env-var / completion conventions across the surface, and the eradication of help-text drift and Python-traceback leaks. Audit-row IDs from `.sisyphus/plans/cli-ux-audit.md` (C1–C2, I1–I16, M1–M5) are noted in parentheses on each entry.

In addition to the cli-ux work, this release rolls up the **tiered-remediation** PRs — Tier-3 (`--storage` per-file isolation; `notebooklm use` fail-closed verification), Tier-5 (security + multi-process hardening: `shell=False` default for `NOTEBOOKLM_REFRESH_CMD`, single-writer migration under filelock, symlink rejection on `source add`, response-preview truncation, REQUIRED/OPTIONAL cookie-domain split), and Tier-6 (RPC-override escape hatch, dependency upper bounds + `pip-audit` CI, per-file coverage floors, install-doc block-mirror policy, `_mind_map` module decoupling). It also rolls up the **thread-safety-and-concurrency** work (Tier-7: `ConnectionLimits` dataclass, `__aexit__` exception arbitration, unique temp file per concurrent download) and the **documentation-fix** consistency pass (#528–#558). Per-arc audit IDs (`T3.x`, `T5.x`, `T6.x`, `T7.Bx`) are noted in parentheses on each non-cli-ux entry.

### Added
- **`notebooklm ask --new` flag now actually exists (I1).** The flag was promised in the docstring but undeclared; `--new` starts a fresh conversation and is mutually exclusive with `--conversation-id`.
- **`--json` on `artifact get / rename / delete / poll / export` (I3).** Detail commands mirror the underlying dataclasses; mutating commands emit `{"id": ..., "renamed|deleted|exported": true, ...}`. `delete` on a mind-map flags `"kind": "mind_map"`.
- **`--json` on eight `source` subcommands (I3).** `source delete / rename / refresh / clean / get / delete-by-title / add-drive / stale` all accept `--json` and emit structured output. `source stale --json` preserves its inverted exit-code semantics (0 = stale, 1 = fresh) and carries the boolean explicitly via `{"stale": ..., "fresh": ...}`.
- **`--json` on `note get / save / create / delete / rename` (I3).**
- **`--json` on `notebooklm configure` (alias `chat configure`) (I3).** Stable shape across both `--mode` and `--persona`/`--response-length` paths.
- **Standard download flag set on `download quiz` and `download flashcards` (I3, I4).** Both commands now expose `--all`, `--latest`, `--earliest`, `--name`, `--dry-run`, `--force`, `--no-clobber`, and `--json` so one wrapper script works across every artifact type.
- **Uniform `--timeout` and `--interval` on long-running ops (I6).** `generate <kind> --wait`, `artifact wait`, and `source wait` now share the same polling-cadence flag surface; pre-existing defaults preserved.
- **`source add` warns when a path-shaped argument doesn't exist (I8).** A typo like `./missin.md` previously fell through to inline-text ingestion silently; now an advisory stderr warning fires before the source is added (still added as text — no breaking exit-code change). Pass `--type text` to suppress.
- **`notebook use --json` (I12).** `use` emits `{"active_notebook_id", "success", "verified", "notebook"}`; `notebook create --use --json` now also surfaces `active_notebook_id`.
- **`--limit` and `--no-truncate` on every `list` command, plus `--no-truncate` on `chat history` (I16).** `notebooklm list`, `source list`, and `artifact list` gain `--limit=N` (client-side slice; reflected in the `--json` `count` field) and `--no-truncate` (table titles wrap instead of ellipsis). `chat history --no-truncate` lifts the hardcoded 50-char preview on Question/Answer columns.
- **Shell completion + ID-aware completers (M1).** New `notebooklm completion <bash|zsh|fish>` prints a Click-generated completion script to stdout. Once sourced, `-n/--notebook`, `-s/--source`, and `-a/--artifact` TAB-complete live IDs from the active profile (capped at 50, best-effort — never trips a traceback in your shell).
- **SIGINT resume hint on long-running `--wait` ops (M2).** Ctrl-C during `generate <kind> --wait`, `artifact wait`, or `source wait` exits 130 with `Cancelled. Resume with: notebooklm artifact poll <task_id>` (or the parallel `source wait <source_id>`) instead of dumping a `KeyboardInterrupt` traceback. Under `--json` the cancellation surfaces as `{"error": true, "code": "CANCELLED", "resume_hint": "..."}`.
- **Unix `-` (stdin) convention on `ask`, `note create`, `source add`, and `--prompt-file` (M3).** `echo "what is X?" | notebooklm ask -`, `cat notes.md | notebooklm note create --content -`, `notebooklm source add -`, and `notebooklm ask --prompt-file -` all read from stdin — matching the `cat`/`tee`/`jq` tradition so pipelines compose without temp files.
- **`NOTEBOOKLM_NOTEBOOK` env var + global `--quiet` flag (M4).** `NOTEBOOKLM_NOTEBOOK=<id> notebooklm ask "..."` now works without `-n/--notebook` or a prior `notebooklm use`. `notebooklm --quiet <subcommand>` raises the package logger floor to ERROR for one invocation (mutually exclusive with `-v/-vv`). A new "Env vars and precedence" section in `docs/configuration.md` documents every `NOTEBOOKLM_*` variable in one place.
- **`docs/cli-exit-codes.md` codifies the codebase-wide exit-code policy (M5).** `0` success, `1` user/app error, `2` system/unexpected, `130` SIGINT — citing `cli/error_handler.py` as the canonical implementation and calling out the two intentional exceptions (`source stale` is inverted; `source wait` is three-way).
- **`notebooklm ask --timeout` CLI option (Polish).** Per-invocation HTTP timeout for the `ask` command; matches the existing `source add --timeout` pattern (supersedes #260).
- **Source fulltext markdown format (Polish).** New `output_format="markdown"` on `client.sources.get_fulltext()` and a `-f/--format` CLI flag on `source fulltext` (closes #222). Requires the optional `markdownify` extra (`pip install "notebooklm-py[markdown]"`).
- **Canonical install guide at `docs/installation.md` (Polish).** Single source of truth organised by 6 personas (AI Agent, end user, library user, headless server, contributor, power user), with extras matrix, `[all]` vs `--all-extras` footgun callout, and platform notes. Replaces install instructions previously fragmented across 12+ files.
- **`tests/unit/test_install_docs.py` guardrails (Polish).** 9 automated checks that catch silent drift between `pyproject.toml`, `docs/installation.md`, and the agent-context files (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`).
- **`NOTEBOOKLM_RPC_OVERRIDES` env var for community self-patch (T6.A).** When Google rotates a `batchexecute` method ID, set e.g. `NOTEBOOKLM_RPC_OVERRIDES='{"LIST_NOTEBOOKS": "newId123"}'` and the resolved id is threaded through **both** lookup sites — the `rpcids=` URL query parameter (`_core._build_url`) and the `f.req` request body (`rpc.encoder.encode_rpc_request`) — so the wire never carries mismatched ids. The decoder consumes the same resolved id (the server echoes back whatever was sent). Defense in depth: overrides are gated on the configured base host being on `_env._ALLOWED_BASE_HOSTS` (`notebooklm.google.com` / `accounts.google.com`), so a redirected base can't pivot the overrides. Lets users on a broken release keep working until the next patch ships (#486).
- **`ConnectionLimits` dataclass for httpx pool tuning (T7.B3).** New public `ConnectionLimits(max_connections, max_keepalive_connections, keepalive_expiry)` exposes the connection-pool defaults previously baked into `_core.py`. Pass via `NotebookLMClient(connection_limits=ConnectionLimits(max_connections=200, ...))` (or `NotebookLMClient.from_storage(...)`) for long-running agents and high-fan-out workers that need to raise the pool ceiling without monkey-patching internals (#527).
- **`--include-domains` flag on `login` / `auth refresh` / `auth inspect` (T5.G).** The cookie-domain allowlist is now split into `REQUIRED_COOKIE_DOMAINS` (notebookLM + Drive + the RotateCookies surface — the empirically-justified minimum used by login + token refresh + source-add + chat-ask) and `OPTIONAL_COOKIE_DOMAINS` (YouTube / Docs / Mail / myaccount — siblings opted into for symmetry with a logged-in browser session). The runtime gate exact-matches REQUIRED only; the back-compat `ALLOWED_COOKIE_DOMAINS` union is preserved. **User-visible behavior shift:** YouTube cookies are no longer scraped at login or trusted at refresh unless `--include-domains=youtube` (or `=all`) is passed — the other siblings still pass the `.google.com` suffix tier so they're unaffected. Accepts both repeated-flag (`--include-domains=youtube --include-domains=docs`) and comma-separated (`--include-domains=youtube,docs`) syntax; unknown labels raise `click.BadParameter` listing the supported set. A migration note prints on default browser-cookies login pointing at the opt-in (#483).
- **`--follow-symlinks` flag on `source add` (T5.C).** **Security hardening:** `source add` used to silently follow symlinks during file-source auto-detection, so a workspace symlink like `~/Downloads/foo.pdf → /etc/passwd` would resolve and upload the target with no warning. The path now refuses to traverse symlinks before `Path.resolve()` unless `--follow-symlinks` is explicitly passed; a `ClickException` names the offending path and the opt-in flag (exit 1, no upload attempted). Broken symlinks fall through to the text branch — the contract is that the symlink target is never read as a file (#476).

### Changed
- **BREAKING: `source get` / `artifact get` / `note get` exit `1` on not-found (was `0`) (C1).** All three `get` commands now exit `1` when the requested ID does not resolve, matching the rest of the CLI's user-error convention so scripts can branch on the exit code without parsing prose. Under `--json` the failure body is the standard `{"error": true, "code": "NOT_FOUND", ...}` envelope; without `--json` the message moves from stdout to stderr. **Migration:** scripts that relied on exit-`0`-on-not-found (e.g. `set -e` probes or `{"found": false}` JSON checks) must switch to `if ! notebooklm source get "$ID"; then ...; fi` or branch on `code: "NOT_FOUND"`.
- **`artifact poll` vs `artifact wait` `--help` clarified on ID kind (C2).** Both `--help` blocks now spell out where each ID typically comes from (`poll <task_id>` straight from `generate`; `wait <artifact_id>` resolved against `artifact list`) and a "Common confusion" callout was added to `docs/cli-reference.md`. Decision recorded: document rather than unify, because the operational difference (one-shot vs blocking, pre- vs post-list-population) is load-bearing for `--wait`-style scripting.
- **BREAKING: `generate cinematic-video --format <non-cinematic>` now exits `2` with a UsageError (I2).** Previously a conflicting `--format mp4`/`explainer`/`brief` was silently overridden to `cinematic`, hiding user intent. The error now matches the existing `--style-prompt cannot be used with cinematic video` rejection. **Migration:** drop the conflicting flag, or use `generate video --format <value>` if a non-cinematic format was actually intended.
- **CLI group docstrings + `docs/cli-reference.md` synced with the live registered subcommand set (I5).** `source`, `download`, `artifact`, and `note` group `--help` blocks now enumerate every registered subcommand (previously missed `add-drive`, `add-research`, `clean`, `wait`, `cinematic-video`, `quiz`, `flashcards`, `suggestions`, `rename`); the reference doc no longer claims unimplemented options. A new `tests/unit/cli/test_help_text.py` snapshot walks every group and fails CI on future drift.
- **`--wait` paths show a transient spinner with elapsed timer (I7).** `generate <kind> --wait`, `artifact wait`, and `source wait` now wrap their blocking poll in a `console.status` spinner with operation kind, an empirical typical-duration hint where one is known (e.g. `typically 30-40 min` for cinematic-video), and a per-second elapsed counter. Spinner is transient and a no-op under `--json` so automation stdout stays pure JSON.
- **Unified `-n/--notebook` help text and consolidated `notebook_option` decorator (I9, I11).** Replaces 53 inline `click.option("-n", "--notebook", ...)` bypass sites across `cli/{artifact,source,note,chat,share,research,generate,notebook,download}.py` with the single canonical decorator, eliminating help-text drift. `helpers.require_notebook` error message now names the user-facing flag (`-n/--notebook`) instead of the internal kwarg (`notebook_id`). A programmatic guardrail asserts every `-n/--notebook` exposure stays consistent.
- **`notebooklm --help` bins five previously-orphaned top-level commands into primary sections (I10).** `auth` joins **Session**; `metadata` joins **Notebooks**; `agent`, `skill`, and `language` join **Command Groups**. The "Other" safety-net bin is now reserved for commands explicitly tagged `category="misc"` — a contract enforced by a new test that fails CI on any future un-binned command.
- **`notebook use` surfaces the typed auth-aware error on expired credentials (I13).** When `client.notebooks.get` raises `AuthError`, `use` now routes through `helpers.handle_auth_error` instead of the generic "Could not verify notebook... Pass --force" catch-all. Text mode shows the canonical "Not logged in" walkthrough with the `notebooklm login` remediation; `--json` emits the standard `AUTH_REQUIRED` envelope. The fail-closed contract and `--force` escape hatch are preserved.
- **`download <type>` exception paths route through the typed error handler (I14).** Every `download` subcommand now wraps the dispatch in `cli.error_handler.handle_errors`. `--json` is honored on the exception path (typed envelope on stdout instead of plain stderr); `RateLimitError.retry_after` surfaces as both a JSON field and a "Retry after Ns" text line; `AuthError` shows the canonical re-auth hint. Exit codes follow the typed policy (1 for library/user errors, 2 for unexpected/system bugs).
- **`notebooklm login` and `notebooklm auth refresh` no longer leak Python tracebacks on unexpected failures (I15).** Both commands now wrap their bodies in `cli/error_handler.py::handle_errors`. Unexpected exceptions become a single friendly line + bug-report URL with exit code `2`; the original traceback remains available at `-vv`. Typed errors (`AuthError`, `NetworkError`, `RateLimitError`, …) keep their dedicated friendly messages.
- **CI `verify-package.yml` extras consolidation (Polish).** `pip install "notebooklm-py[browser,dev]==<version>"` in the source-specific install steps replaces the inline `pip install pytest pytest-asyncio ...` test-deps step, eliminating a major drift surface between `pyproject.toml` `[dev]` and CI.
- **Contributor install canonicalised on `uv sync --frozen` (Polish).** Updated across `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/development.md`, `docs/releasing.md`. The previous `uv pip install -e ".[all]"` and `uv sync --all-extras` flows ignored the checked-in `uv.lock`.
- **SKILL.md install command now uses `[browser]` (Polish).** Plus a Python-version-aware optional `[cookies]` install, so AI agents following the skill get the Playwright auth flow out of the box.
- **Cookie identity widened to `(name, domain, path)` per RFC 6265 §5.3 (Polish).** `CookieKey`, `DomainCookieMap`, `AuthTokens.cookies`, `extract_cookies_with_domains`, and `normalize_cookie_map` now key on the path-aware triple. Writes remain backward compatible (flat dicts and legacy 2-tuples still accepted); reads of `auth.cookies` with the old 2-tuple key now raise `KeyError` — use `auth.cookies[("SID", ".google.com", "/")]`, `auth.flat_cookies["SID"]`, or `auth.cookie_header` (#369, #406).
- **`docs/cli-reference.md` final sync against shipped CLI surface (F8.T4).** Verified every section matches `notebooklm <cmd> --help` for the surface that landed across Phase 1–7: `--json` documented on every read command, `--limit`/`--no-truncate` on `list` commands and `chat history`, uniform `--timeout`/`--interval`/`--wait` on long-running ops, `--new` on `ask`, `--quiet` on the root group, `-` stdin convention on `ask` / `note create` / `source add` / `--prompt-file`, `completion <bash|zsh|fish>` command + ID-aware completers, and `NOTEBOOKLM_NOTEBOOK` env var.
- **BREAKING: `notebooklm use <id>` fails closed when the notebook doesn't exist (T3.D).** Previously `use` persisted a typo / network-error / auth-expiry id to `context.json` and quietly continued, leaving every downstream command broken against poisoned saved state. `use` now calls `NotebooksAPI.get(id)` *before* persisting and exits `1` without writing to `context.json` if the notebook resolves to a blank `Notebook(id="", title="")`, raises on the wire, or fails the auth health check. `NotebooksAPI.get` itself is hardened to raise `NotebookNotFoundError` on the parseable-but-empty payload Google returns for unknown IDs (previously it returned a blank `Notebook`). `NotebookNotFoundError` now inherits from **both** `RPCError` and `NotebookError` so existing `except` blocks on either keep working. Pass `--force` to bypass verification (for offline / debugging) — the persisted entry is banner-marked `(not verified — --force)`. **Migration:** scripts that depended on `use` always exiting `0` should now branch on the exit code, or pass `--force` when the IDs are guaranteed valid (#468).
- **`--storage <path>` isolates notebook context per file (T3.C).** Two `--storage` invocations against different files used to leak notebook state through the default profile's `context.json`. The CLI now derives a sibling context file `<path>.context.json` from `--storage`, with precedence `explicit --storage > profile > legacy home-root`. New `storage_path` keyword on `paths.get_context_path()` / `paths.get_path_info()` plus a thread-through of the `--storage` override from `click.get_current_context().obj` to `_get_context_value` / `_set_context_value` / `set_current_notebook` / `clear_context` / `handle_auth_error`. `status` and `auth logout` use `@click.pass_context` to read the override directly. `status --paths --json` now reports the sibling layout and labels the source as `"CLI flag (--storage)"` (#467).
- **BREAKING: `NOTEBOOKLM_REFRESH_CMD` defaults to `shell=False` (T5.A).** **Security hardening for a shell-injection footgun when the env var is sourced from CI configs or container env files.** `_run_refresh_cmd` now parses the value with `shlex.split` and invokes `subprocess.run(argv, shell=False, ...)`. Empty argv → `RuntimeError("NOTEBOOKLM_REFRESH_CMD parsed to empty argv")`; malformed quoting → `RuntimeError("... could not be parsed: <ValueError msg>")`. First-token logging uses `os.path.basename(argv[0])` only so absolute secrets-dir paths (e.g. `/home/user/.secrets/refresh.sh`) cannot leak through INFO logs. Set `NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1` (only the literal string `"1"` opts in — `"0"`/`"false"` stay safe) to keep the prior `shell=True` behavior for cmd strings that rely on shell features (pipes / redirection / `$VAR` expansion); each shell-mode invocation emits `logger.warning("Using shell-mode for NOTEBOOKLM_REFRESH_CMD (opt-in)")` so the trade-off stays visible. On Windows `shlex.split(cmd, posix=False)` is used so backslash-containing paths produced by `subprocess.list2cmdline` round-trip cleanly. **Migration:** rewrite shell-dependent refresh commands as a single argv (`shlex.join` / `subprocess.list2cmdline`), or opt into the legacy behavior via `NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1` (#475).
- **`migrate_to_profiles()` serializes under a cross-process filelock (T5.B).** Two `notebooklm` invocations starting under a fresh home (container start-up races, parallel test runs, MCP worker pools) could both run the copy + delete migration concurrently and leave partial state visible to either. The migration body now acquires `<home>/.migration.lock` and re-checks the `.migration_complete` marker inside the lock; the loser of the race no-ops cleanly. Lock waits past 30 s raise a domain-specific `MigrationLockTimeoutError(RuntimeError)` (the underlying `filelock.Timeout` is preserved as `__cause__`). Legacy-source unlink/rmtree is now tolerant of already-removed paths so a crashed mid-run migration doesn't trip the next runner. Marker re-check reads `default_dir / _MIGRATION_MARKER` (the path where `migration.py` actually writes the marker), not the home root (#478).
- **`RPCError.raw_response` previews capped at 80 chars; `NOTEBOOKLM_DEBUG=1` opts into full body (T5.D).** Error logs and CLI output used to embed a 500-char verbatim preview of the upstream response — noisy in CI and capable of leaking large server payloads into terminals. The cap is now **80 chars + `"..."`** by default, centralized behind a single `_truncate_response_preview` helper in `exceptions.py` so `RPCError.__init__`, `UnknownRPCMethodError.__init__`, and `decode_response.response_preview` all behave consistently. Set `NOTEBOOKLM_DEBUG=1` to preserve the full untruncated body for deep debugging — only the literal string `"1"` opts in (`"0"`, `"true"`, etc. fall back to truncation, tested as a footgun guard). `UnknownRPCMethodError`'s widened `Any` payload type is preserved — only string payloads are truncated. Documented in `docs/troubleshooting.md` env-var table + "How to get the full response preview" section (#479).

### Removed
- **`RPCMethod.DISCOVER_SOURCES` enum entry (T8.E1).** Unused; no client invocation since pre-0.x — see audit DISCOVER_SOURCES finding. The `qXyaNe` ID was never exercised by any `client.*` API and the rpc-health canary already short-circuited it via `ALWAYS_SKIP_METHODS`. Removed from `rpc/types.py`, `scripts/check_rpc_health.py` (skip list + comment block), `tests/unit/test_rpc_health_coverage.py` (UNAVAILABLE_SKIP_LIST entry; the constant itself is preserved as an empty frozenset for future "exists but not probable" entries), and the Legacy/Unused table in `docs/rpc-reference.md`.

### Deprecated
- **`SourcesAPI.add_file` `mime_type` parameter (Polish).** The `mime_type` argument on `client.sources.add_file()` was never wired into the resumable-upload RPC payload (the server derives MIME from the filename extension). Passing a non-`None` value now emits `DeprecationWarning`; the parameter is scheduled for removal in the next minor release. Migration: drop the argument; rely on the filename extension. The separate `add_drive(..., mime_type=...)` Drive-source parameter is unaffected.
- **`notebooklm source add --mime-type` on the file-source path (Polish).** The CLI flag is a no-op when the resolved source type is `file`; using it now prints a stderr deprecation note (suppress via `NOTEBOOKLM_QUIET_DEPRECATIONS=1`). The same flag on `notebooklm source add-drive` (Drive sources) is unaffected.

### Fixed
- **`notebooklm login` install hint stripped the `[browser]` extra (Polish).** The "Playwright not installed" message rendered as `pip install "notebooklm-py"` (no extras) because Rich interpreted `[browser]` as a style tag. Fixed by passing `markup=False` for the install-command line; the same lines also correct the package name from `notebooklm` to `notebooklm-py`.
- **`__aexit__` exception arbitration + close-leak shield (T7.B4).** `NotebookLMClient.__aexit__` previously masked the original body exception when `aclose()` itself raised, surfacing the close-time error to callers instead. The arbitration now preserves the body exception (chained via `__cause__` where applicable) while still propagating cleanup-time failures, and an inner shield guarantees the underlying httpx client is closed even when intermediate cleanup steps raise. Long-running agents that share a client across many operations no longer lose stack traces from the original failure when shutdown happens to fail too (#526).
- **Unique temp file per concurrent artifact download (T7.B1).** Concurrent `download_*` invocations against the same artifact wrote to a shared `<dest>.tmp` path, so two parallel callers could clobber each other's bytes mid-stream and one would see a corrupted file. Each invocation now allocates a unique temp file (PID + uuid suffix) and atomically renames into the final destination, preventing concurrent-conflict corruption when an `asyncio.gather` fans out over the same artifact (#523).
- **`add_file` TOCTOU close + `max_concurrent_uploads` knob (T7.D3).** `SourcesAPI.add_file` used to open the source file twice — once implicitly for `stat()`-based size probing, again inside `_upload_file_streaming` — and a path swap between those two moments could substitute a different file into a successful upload. The file is now opened once during validation; the FD is held across the size check (`os.fstat(fd.fileno())`), source registration, upload-session start, and the streamed POST body, with `try`/`with` guaranteeing release on every exit path (including `CancelledError`). A new `max_concurrent_uploads: int | None = 4` knob on `NotebookLMClient` and `from_storage()` caps the simultaneous in-flight uploads; each in-flight upload holds one open file descriptor for its duration, so the cap doubles as an FD-exhaustion guard against fan-out callers (`asyncio.gather` over 100 `add_file` calls now holds at most 4 FDs at a time instead of 100). The semaphore is per-instance and independent of the RPC connection pool because uploads use their own `httpx.AsyncClient` against the Scotty endpoint. `None` resolves to the default — unbounded uploads are intentionally rejected (`max_concurrent_uploads=0`/`<0` raises `ValueError` at construction).

### Infrastructure
- **`tests/integration/` tier-enforcement collection hook (T8.D11).** New `pytest_collection_modifyitems` hook in `tests/integration/conftest.py` raises `pytest.UsageError` at collection time when a test under `tests/integration/` lacks all three of: `@pytest.mark.vcr`, `@notebooklm_vcr.use_cassette(...)` (detected by walking the `wrapt.FunctionWrapper` chain for the VCR `CassetteContextDecorator`), or the explicit `@pytest.mark.allow_no_vcr` opt-out. The opt-out marker is registered once in `pyproject.toml` `[tool.pytest.ini_options].markers` and applied at module level to the existing mock-only files that live under `tests/integration/` for tree-organization reasons (`test_skill_packaging.py`, `test_core.py`, `test_auto_refresh.py`, `test_artifacts_drift.py`, `test_get_summary_drift.py`, `test_download_multi_artifact.py`, and the `concurrency/test_*` subtree). New mock-only tests should land in `tests/unit/`; `allow_no_vcr` is a transitional escape hatch, not a default. A pytester-based regression test (`tests/unit/test_tier_enforcement_hook.py`) pins the hook's behavior with a committed, durable assertion — including the `use_cassette` wrapper-introspection branch — so weakening the hook fails CI.
- **`ArtifactsAPI` ↔ `NotesAPI` decoupled via `_mind_map` module (T6.F).** Mind-map RPC primitives (`list_mind_maps`, `create_note`, `update_note`, `fetch_all_notes_and_mind_maps`) moved to a new `_mind_map` module that both `ArtifactsAPI` and `NotesAPI` import directly. `ArtifactsAPI.__init__` no longer requires `notes_api=client.notes`, which removes the load-bearing init order in `NotebookLMClient`. The `notes_api` keyword is retained as an optional, ignored argument for backward compatibility with existing callers. `NotesAPI` keeps its public surface — including the `_is_deleted` / `_extract_content` / `_parse_note` private helpers used by unit tests — by delegating to the shared module. New `tests/unit/test_init_order.py` pins init-order-agnostic construction plus mind-map regression coverage for `generate_mind_map` / `list` / `download_mind_map` (#489).
- **Dependency upper bounds + `pip-audit` CI workflow (T6.G).** Every entry in `[project.dependencies]` and each `[project.optional-dependencies]` extra now carries an upper bound — pre-1.0 packages (httpx) cap on the next minor; 1.x+ packages cap on the next major. Bounds are conservative enough to leave headroom for transitive resolution but tight enough that a breaking new major can't quietly land in CI. New `.github/workflows/dependency-audit.yml` runs `pip-audit --strict` against the locked env on push to `main`, on every PR that touches the manifest, and nightly. Soft-launched with `continue-on-error: true` for the first release cycle so a transient advisory database hiccup doesn't block merges; flipping to hard-fail is a one-line follow-up (#490).
- **Per-file coverage floor enforcement (T6.C).** `coverage.py`'s `[tool.coverage.report]` only supports a global `fail_under`, so individual files that have always lagged the project-wide 90% used to silently regress whenever a refactor touched them — the global gate stayed green because the strong files compensated. `scripts/check_coverage_thresholds.py` now reads `[tool.notebooklm.per_file_coverage_floors]` from `pyproject.toml` and fails CI when individual files drop below their listed floor, in addition to the global gate. Floors are set ~2% below the current measured coverage for four named files (`cli/_firefox_containers.py`, `cli/doctor.py`, `cli/profile.py`, `cli/session.py`) so transient flakes don't trip CI on unrelated PRs. `__main__.py` floor is `0` (CLI entry isn't unit-tested) so it's an explicit "we know it's uncovered" rather than a silent gap (#491).
- **`docs/installation.md` ↔ `CONTRIBUTING.md` block-mirror enforcement (T6.D).** `scripts/check_ci_install_parity.py` already enforced the canonical install command across both files; it now also enforces a block-mirror policy: every fenced ` ```bash ` block in `installation.md` must EITHER appear verbatim in `CONTRIBUTING.md` OR be marked `<!-- not mirrored: <reason> -->` on the line directly before its opening fence. Forces each install-doc edit to consciously decide whether the new block belongs in the contributor-mirrored subset — silent drift becomes a CI failure with the missing/unmarked block named. New `--installation` flag points at the source of truth so the script is parametric (#492).

### Documentation
- **`documentation-fix` consistency pass (#528–#558).** A 17-PR run resolving the audit in `.sisyphus/plans/documentation-audit.md`: breaking-example fixes in `docs/python-api.md` (#547) and `docs/cli-reference.md` (#537, #545); `__all__` ↔ `__init__.py` parity in `docs/stability.md` (#531); `docs/installation.md` filelock dep + login flow + timestamp fixes (#529); `docs/configuration.md` profile/language + env-var table backfill (#539); `docs/troubleshooting.md` auth-error text + cookie dedup (#532); `docs/development.md` test-tree + concurrency model + cross-links (#538); `docs/releasing.md` pre-release scripts (#534); `docs/rpc-development.md` non-transient ERROR triage (#535); `docs/rpc-reference.md` `SHARE_ARTIFACT` shape + tier strings (#534); `docs/auth-keepalive.md` T5.G domain split (#540); CLI-reference T3.D/T5.C/T5.G flag backfill (#537, #545); `docs/python-api.md` consistency pass (#558); `docs/cli-reference.md` consistency pass (#557); README slim with `docs/installation.md` cross-link (#533); CONTRIBUTING ↔ `docs/development.md` cross-link (#528). No source-code changes.

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
