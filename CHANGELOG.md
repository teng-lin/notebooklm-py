# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`notebooklm ask --timeout` CLI option** - Per-invocation HTTP request timeout for the `ask` command. Useful when long or complex prompts exceed the library's default 30 s timeout. Accepts a positive integer (seconds); omitting the flag preserves the existing library default, matching the established `source add --timeout` pattern. Supersedes #260.
- **Source fulltext markdown format** - Retrieve source content as structured Markdown with headings, tables, links, and emphasis preserved (closes #222)
  - New `output_format` parameter on `client.sources.get_fulltext()` (`"text"` default, `"markdown"`)
  - New `-f`/`--format` CLI option on `source fulltext` command
  - Requires optional `markdownify` dependency (`pip install notebooklm-py[markdown]`)

### Changed
- **Cookie identity widened to `(name, domain, path)` per RFC 6265 §5.3** - `CookieKey`, `DomainCookieMap`, `AuthTokens.cookies`, `extract_cookies_with_domains`, and `normalize_cookie_map` now key on the path-aware triple instead of the legacy `(name, domain)` pair. PR #363 already made the snapshot/delta save path path-aware; #406 closes the corresponding load-side and legacy-merge gaps so two cookies sharing `(name, domain)` at distinct paths (e.g. `OSID@/` and `OSID@/u/0/`) coexist end-to-end. **Writes remain fully backward compatible** — `AuthTokens(cookies={"SID": "..."})` (flat) and `AuthTokens(cookies={("SID", ".google.com"): "..."})` (legacy 2-tuple) both still work; `normalize_cookie_map` widens missing paths to `/` and `_update_cookie_input` collapses path-siblings back when the caller's target uses the legacy 2-tuple shape. **Reads of `auth.cookies` with the old 2-tuple key shape now raise `KeyError`** — callers that subscript the dict directly should update from `auth.cookies[("SID", ".google.com")]` to `auth.cookies[("SID", ".google.com", "/")]`, or use `auth.flat_cookies["SID"]` / `auth.cookie_header` when path doesn't matter. The same migration applies to the return value of `extract_cookies_with_domains`. Empirically, every Set-Cookie observed in captured Google traffic uses `path=/`, so this is correctness/headroom rather than a live bug fix (#369, #406).

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
