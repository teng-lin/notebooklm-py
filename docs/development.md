# Contributing Guide

**Status:** Active
**Last Updated:** 2026-05-14

This guide covers everything you need to contribute to `notebooklm-py`: architecture overview, testing, and releasing.

> **New contributor?** Start with [CONTRIBUTING.md](../CONTRIBUTING.md) at the
> repo root for the install/lint/test workflow and PR conventions, then come
> back here for architectural context once you're ready to write code.

---

## Architecture

### Package Structure

```
src/notebooklm/
├── __init__.py          # Public exports
├── client.py            # NotebookLMClient main class
├── auth.py              # Authentication handling
├── types.py             # Dataclasses and type definitions
├── _core.py             # Core HTTP/RPC infrastructure
├── _notebooks.py        # NotebooksAPI implementation
├── _sources.py          # SourcesAPI implementation
├── _artifacts.py        # ArtifactsAPI implementation
├── _chat.py             # ChatAPI implementation
├── _research.py         # ResearchAPI implementation
├── _notes.py            # NotesAPI implementation
├── _settings.py         # SettingsAPI implementation
├── _sharing.py          # SharingAPI implementation
├── rpc/                 # RPC protocol layer
│   ├── __init__.py
│   ├── types.py         # RPCMethod enum and constants
│   ├── encoder.py       # Request encoding
│   └── decoder.py       # Response parsing
└── cli/                 # CLI implementation
    ├── __init__.py      # CLI package exports
    ├── helpers.py       # Shared utilities
    ├── session.py       # login, use, status, clear
    ├── notebook.py      # list, create, delete, rename
    ├── source.py        # source add, list, delete
    ├── artifact.py      # artifact list, get, delete
    ├── generate.py      # generate audio, video, etc.
    ├── download.py      # download audio, video, etc.
    ├── chat.py          # ask, configure, history
    └── ...
```

### Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         CLI Layer                           │
│   cli/session.py, cli/notebook.py, cli/generate.py, etc.    │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      Client Layer                           │
│  NotebookLMClient → NotebooksAPI, SourcesAPI, ArtifactsAPI  │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                       Core Layer                            │
│              ClientCore → _rpc_call(), HTTP client          │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                        RPC Layer                            │
│        encoder.py, decoder.py, types.py (RPCMethod)         │
└─────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **CLI** | `cli/*.py` | User commands, input validation, Rich output |
| **Client** | `client.py`, `_*.py` | High-level Python API, returns typed dataclasses |
| **Core** | `_core.py` | HTTP client, request counter, RPC abstraction |
| **RPC** | `rpc/*.py` | Protocol encoding/decoding, method IDs |

### Boundary Guardrails

The architecture tests encode the current layer contract:

- `tests/unit/test_public_shims.py` has a documented public import manifest.
  When a docs change adds or removes a supported import path, update the
  manifest in the same PR so public API drift is intentional and reviewable.
- `tests/unit/test_cli_boundary.py` parses `src/notebooklm/cli/**/*.py` and
  rejects CLI imports from `notebooklm._*`, `notebooklm.rpc.*`, or `_private`
  names exposed by public modules. Promote needed symbols through a public
  facade (`notebooklm.types`, `notebooklm.auth`, `notebooklm.research`, etc.)
  before using them from the CLI.
- Auth internals may move under `notebooklm._auth` during architecture work,
  but first-party callers continue to import through `notebooklm.auth`. The
  compatibility manifest in `tests/unit/test_public_shims.py` enforces the
  current first-party surface for that move; it is not a broader public API
  decision, and removing a listed name needs a separate deprecation plan.
- `tests/unit/test_init_order.py` records the temporary baseline of feature
  APIs that still access `ClientCore` private state directly. Future capability
  migration PRs should reduce that baseline as private state moves behind
  explicit `ClientCore` methods; do not add new entries unless the PR also
  explains the follow-up migration path.

### Key Design Decisions

**Why underscore prefixes?** Files like `_notebooks.py` are internal implementation. Public API stays clean (`from notebooklm import NotebookLMClient`).

**Why namespaced APIs?** `client.notebooks.list()` instead of `client.list_notebooks()` - better organization, scales well, tab-completion friendly.

**Why async?** Google's API can be slow. Async enables concurrent operations and non-blocking downloads.

### Adding New Features

**New RPC Method:**
1. Capture traffic (see [RPC Development Guide](rpc-development.md))
2. Add to `rpc/types.py`: `NEW_METHOD = "AbCdEf"`
3. Implement in appropriate `_*.py` API class
4. Add dataclass to `types.py` if needed
5. Add CLI command if user-facing

**New API Class:**
1. Create `_newfeature.py` with `NewFeatureAPI` class
2. Add to `client.py`: `self.newfeature = NewFeatureAPI(self._core)`
3. Export types from `__init__.py`

---

## Concurrency Model

Multiple `notebooklm` processes (parallel CLI runs, an in-process keepalive
beside a cron-driven `notebooklm auth refresh`, container start-up races,
`xargs -P` fan-outs) can target the same `NOTEBOOKLM_HOME` simultaneously.
The library coordinates with **cross-process file locks** (POSIX `flock` /
Windows `LockFileEx`, via the [`filelock`](https://pypi.org/project/filelock/)
package) so reads and writes against shared on-disk state never tear or
clobber a sibling's update.

All locks are sibling files next to the resource they guard (zero-byte,
left on disk after release — `filelock` reuses them).

| Lock file | Owner | Scope | Acquisition |
|---|---|---|---|
| `<profile>/storage_state.json.lock` | `auth.save_cookies_to_storage` (`auth.py:1935`) | Read-merge-write of `storage_state.json` (cookie sync after a rotation or 302) | Blocking exclusive |
| `<profile>/.storage_state.json.rotate.lock` | `auth._poke_session` (`auth.py:2817`) | Cross-process dedup of the `accounts.google.com/RotateCookies` keepalive POST | Non-blocking exclusive (`LOCK_NB`); skip on contention |
| `<home>/.migration.lock` | `migration.migrate_to_profiles` (`migration.py:28`) | One-shot legacy→profile layout migration on startup | Blocking exclusive, 30s timeout (raises `MigrationLockTimeoutError`) |
| `<profile>/context.json.lock` | `cli.helpers.set_context` / `clear_context` via `_atomic_io.atomic_update_json` (`_atomic_io.py:136`) | Read-modify-write of the active-notebook/account-routing context for a profile | Blocking exclusive, 10s timeout |

Design notes:

- **Two layered storage locks (not one).** The `.lock` and `.rotate.lock`
  files protect the *same* `storage_state.json` but serve different access
  patterns: a long-running save must not block — or be blocked by — a
  best-effort rotation poke. Keeping them separate prevents the keepalive
  from queueing behind a slow cookie write (and vice-versa).
- **Fail-open on lock infrastructure failure.** When the lock file itself
  cannot be created (read-only home dir, NFS without `flock`, permission
  denied), `_poke_session` proceeds *without* coordination rather than
  wedging forever. A duplicate rotation across processes is bounded and
  harmless; a permanently-suppressed rotation is not.
- **Locks are sibling files, never the resource itself.** `filelock` reuses
  the sentinel across invocations, so cleanup is not required — and a
  TOCTOU race between unlink and reacquire is avoided.
- **In-process serializers complement, not replace, file locks.**
  `auth._poke_session` also takes an `asyncio.Lock` keyed on
  `(event_loop, profile)` to dedupe an `asyncio.gather` fan-out before
  reaching the cross-process flock — the file lock only sees one
  contender per process per rate-limit window.

Path resolution for all locked resources flows through `paths.py`
(`get_storage_path`, `get_context_path`, `get_home_dir`), so a `--storage`
override or a different `NOTEBOOKLM_PROFILE` automatically yields a distinct
lock sibling and the two invocations never contend.

---

## Testing

### Prerequisites

1. **Install dependencies** (canonical contributor flow — see [docs/installation.md#e-contributor](installation.md#e-contributor) for details):
   ```bash
   uv sync --frozen --extra browser --extra dev --extra markdown
   uv run playwright install chromium
   uv run pre-commit install
   ```

   The `browser` extra is required for the default `uv run pytest` suite because
   several unit tests import and patch `playwright.sync_api`. The command
   `uv sync --frozen --extra dev` installs the test tools, but not Playwright.

   CI runs the same lint gate with `uv run pre-commit run --all-files`, so local hook results should match the `quality` job.

2. **Authenticate:**
   ```bash
   notebooklm login
   ```

3. **Create read-only test notebook** (required for E2E tests):
   - Create notebook at [NotebookLM](https://notebooklm.google.com)
   - Add multiple sources (text, URL, etc.)
   - Generate artifacts (audio, quiz, etc.)
   - Set env var: `export NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID="your-id"`

### Quick Reference

```bash
# Unit + integration tests (no auth needed)
uv run pytest

# E2E tests (requires auth + test notebook)
uv run pytest tests/e2e -m readonly        # Read-only tests only
uv run pytest tests/e2e -m "not variants"  # Skip parameter variants
uv run pytest tests/e2e --include-variants # All tests including variants

# Select a profile for E2E tests
uv run pytest tests/e2e -m e2e --profile work
```

### Selecting a profile for E2E tests

The E2E suite picks up the active NotebookLM profile from (highest precedence first):

1. `--profile <name>` pytest flag
2. `NOTEBOOKLM_PROFILE` environment variable
3. `default_profile` from `~/.notebooklm/config.json`
4. `default`

The auto-created notebook ID cache files
(`generation_notebook_id`, `multi_source_notebook_id`) are written under the
active profile directory (`~/.notebooklm/profiles/<name>/`), so each profile
keeps its own cache and never reuses notebook IDs from another Google account.

#### Notebook ID env vars are profile-agnostic

The notebook ID env vars (`NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`,
`NOTEBOOKLM_GENERATION_NOTEBOOK_ID`, `NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID`)
are **not** profile-scoped — they're read as-is regardless of which profile
is active. If you set them in `.env` and switch profiles, the test will try
to access notebooks that don't exist in the other Google account.

**Recommendation:** leave the generation/multi-source env vars unset and let
the per-profile cache files handle it. Only `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`
needs to be set; if you switch profiles often, override it inline:

```bash
NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<work-nb-id> \
  uv run pytest tests/e2e -m e2e --profile work
```

### Test Structure

```
tests/
├── unit/                            # No network, fast, mock everything
├── integration/                     # Mocked HTTP responses + VCR cassettes
│   ├── test_artifacts.py            # ArtifactsAPI integration
│   ├── test_artifacts_drift.py      # CREATE_ARTIFACT payload drift guard
│   ├── test_auto_refresh.py         # Keepalive/refresh integration
│   ├── test_chat.py                 # ChatAPI integration
│   ├── test_cli_source_delete.py    # CLI source-delete path
│   ├── test_core.py                 # ClientCore + RPC plumbing
│   ├── test_download_multi_artifact.py
│   ├── test_get_summary_drift.py    # GET_NOTEBOOK_SUMMARY drift guard
│   ├── test_notebooks.py            # NotebooksAPI integration
│   ├── test_notes.py                # NotesAPI integration
│   ├── test_research_api.py         # ResearchAPI integration
│   ├── test_settings.py             # SettingsAPI integration
│   ├── test_sharing.py              # SharingAPI integration
│   ├── test_skill_packaging.py      # Packaging smoke (skills, entry-points)
│   ├── test_sources.py              # SourcesAPI integration
│   ├── test_vcr_comprehensive.py    # End-to-end VCR walkthrough
│   ├── test_vcr_example.py          # VCR pattern reference
│   ├── test_vcr_real_api.py         # VCR against real-API cassettes
│   ├── cli_vcr/                     # CLI → Client → RPC VCR tests
│   └── concurrency/                 # Cross-process / asyncio races
└── e2e/                             # Real API calls (requires auth)
```

The `*_drift.py` tests are payload-shape canaries: they decode a recorded
RPC response (or assemble a synthetic one) and assert the live decoder still
produces the expected dataclass. They fail loudly when Google changes a
payload field, so the failure shows up here before users hit it.

### VCR Testing (Recorded HTTP)

VCR tests record HTTP interactions for offline, deterministic replay. We have two levels:

**Client-level VCR tests** (`tests/integration/test_vcr_*.py`):
- Test Python API methods directly
- Verify RPC encoding/decoding with real responses

**CLI VCR tests** (`tests/integration/cli_vcr/`):
- Test the full CLI → Client → RPC path
- Use Click's CliRunner with VCR cassettes
- Verify CLI commands work end-to-end without mocking the client

```bash
# Run all VCR tests
uv run pytest tests/integration/

# Run only CLI VCR tests
uv run pytest tests/integration/cli_vcr/
```

Sensitive data (cookies, tokens, emails) is automatically scrubbed from cassettes.

### Cassette recording

Maintainers re-record cassettes against the live API when an RPC payload
shape changes. Recording is opt-in (`NOTEBOOKLM_VCR_RECORD=1`) and requires
a valid `notebooklm login` session.

Two notebook env vars steer which notebook the recording session targets.
**Neither UUID is committed** — both are per-maintainer secrets (notebook IDs
are linkable to a Google account):

| Env var | Used by | Notebook role |
|---------|---------|---------------|
| `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID` | read-heavy cassettes (`list`, `download`, `get`) | A maintainer-owned notebook pre-populated with sources + artifacts. Tests only READ from it. |
| `NOTEBOOKLM_GENERATION_NOTEBOOK_ID` | mutation/generation cassettes (`add source`, `generate`, `delete`) | A **separate** maintainer-owned notebook used only for destructive/generation flows, so the read-only notebook stays pristine. |

#### One-time setup — generation notebook

Run the setup script once per Google account that records cassettes:

```bash
uv run python tests/scripts/setup-generation-notebook.py
```

The script is idempotent: it reuses an existing notebook titled
`VCR Generation Notebook (Tier 8)` if one already exists, otherwise creates it.
It prints the notebook UUID and an `export` line. Copy the export line into
your maintainer environment (e.g. `~/.zshrc` or a profile-specific `.env`
file you do NOT commit):

```bash
export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<printed-uuid>
```

The script is a manual maintainer helper — CI never runs it.

#### Recording a cassette

```bash
# Re-record (or record-new) cassettes; sensitive data auto-scrubbed
NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/test_vcr_*.py -v
```

The scrubbing pipeline (`tests/vcr_config.py`) redacts cookies, CSRF tokens,
emails, and other sensitive patterns before the cassette hits disk. Verify
the result with the cassette guard before committing:

```bash
# Current guard (a Python replacement is planned)
tests/check_cassettes_clean.sh
```

#### Synthetic error cassettes

> [!WARNING]
> **Error cassettes generated through this plumbing are SYNTHETIC.** They
> validate the client's exception-mapping branches (`RateLimitError`,
> `ServerError`, the auth-refresh path), NOT Google's actual error response
> shapes. If you need to validate a real-world error shape, capture a live
> recording instead — these synthetic shapes are intentionally minimal.

The `NOTEBOOKLM_VCR_RECORD_ERRORS` env var opts a recording session into
substituting the next outgoing batchexecute RPC with a synthetic error
response. Three modes are supported:

| Mode            | HTTP status | Maps to                                         |
|-----------------|-------------|-------------------------------------------------|
| `429`           | 429         | `RateLimitError` (after retry budget exhausted) |
| `5xx`           | 500         | `ServerError`   (after retry budget exhausted) |
| `expired_csrf`  | 400         | auth-refresh path (NotebookLM uses 400, not 401)|

The plumbing has three opt-in layers:

1. **Env var**: `NOTEBOOKLM_VCR_RECORD_ERRORS=<mode>` activates the transport
   wrapper inside `ClientCore.open()`.
2. **Pytest marker**: `@pytest.mark.synthetic_error("<mode>")` sets the env
   var for the duration of a single test (auto-reverted on teardown).
3. **Filename prefix**: cassettes recorded under this mode MUST be named
   `error_synthetic_<mode>_<slug>.yaml` — use
   `tests.cassette_patterns.synthetic_error_cassette_name(mode, slug)` to
   build the filename so reviewers can tell synthetic shapes apart from
   real recordings at a glance.

Example recording session (this is the workflow a maintainer uses to
record the actual error cassettes — the transport-wrapper module itself
ships only the plumbing):

```bash
NOTEBOOKLM_VCR_RECORD=1 \
NOTEBOOKLM_VCR_RECORD_ERRORS=429 \
  uv run pytest tests/integration/test_error_cassettes.py::test_rate_limit_records
```

Production behavior is unchanged when `NOTEBOOKLM_VCR_RECORD_ERRORS` is
unset — the transport wrapper is only constructed when the env var resolves
to a recognized mode, and a typo'd value resolves to `None` (the recording
session continues without substitution).

### Per-method RPC coverage gate

`tests/scripts/check_method_coverage.py` enforces, on every PR, that each
member of `RPCMethod` has **both**:

1. **A test reference** — at least one file under `tests/` (excluding the
   gate script itself) mentions the enum member by its qualified name
   (`RPCMethod.LIST_NOTEBOOKS`) OR by its raw RPC id string value
   (`"wXbhsf"`).
2. **A cassette covering the RPC id** — at least one cassette YAML under
   `tests/cassettes/` contains the RPC id string in its body.

The gate is a pure-text static check (no pytest, no network) and runs in the
`quality` job of `test.yml`.

**Adding a new `RPCMethod`?** Ship it with:
- a unit or integration test that imports the enum member (or asserts on its
  raw id), AND
- at least one cassette whose recorded request/response body contains the
  RPC id.

**Pre-existing gaps.** A small `PREEXISTING_GAPS` set inside the script
grandfathers methods that lacked coverage when the gate first landed
(currently: `GET_INTERACTIVE_HTML`, `GET_SUGGESTED_REPORTS`,
`IMPORT_RESEARCH`, `REFRESH_SOURCE`). The set is a **one-way ratchet** —
it must not grow. When you backfill coverage for a grandfathered method,
delete its entry from `PREEXISTING_GAPS` in the same PR. The gate prints a
`NOTICE:` to stderr when a `PREEXISTING_GAPS` entry has acquired full
coverage so maintainers see the prompt to remove it.

```bash
# Run locally before pushing changes that touch RPCMethod
uv run python tests/scripts/check_method_coverage.py
```

### E2E Fixtures

| Fixture | Use Case |
|---------|----------|
| `read_only_notebook_id` | List/download existing artifacts |
| `temp_notebook` | Add/delete sources (auto-cleanup) |
| `generation_notebook_id` | Generate artifacts (CI-aware cleanup) |

### Rate Limiting

NotebookLM has undocumented rate limits. Generation tests may be skipped when rate limited:
- Use `uv run pytest tests/e2e -m readonly` for quick validation
- Wait a few minutes between full test runs
- `SKIPPED (Rate limited by API)` is expected behavior, not failure

### Writing New Tests

```
Need network?
├── No → tests/unit/
├── Mocked → tests/integration/
└── Real API → tests/e2e/
    └── What notebook?
        ├── Read-only → read_only_notebook_id + @pytest.mark.readonly
        ├── CRUD → temp_notebook
        └── Generation → generation_notebook_id
            └── Parameter variant? → add @pytest.mark.variants
```

---

## Logging and observability

### Levels — when to emit what

- **WARNING** — data loss, protocol drift, schema mismatch, unexpected non-2xx that isn't auth-recoverable. Actionable.
- **INFO** — coarse-grained lifecycle events (login complete, profile switched). Rare in library code; CLI uses INFO for user-facing progress.
- **DEBUG** — expected fallbacks, hot-path parser branches, polling status, request/response metadata. Off by default; enable via `NOTEBOOKLM_LOG_LEVEL=DEBUG` or `notebooklm -vv`.
- **Silent + comment** — best-effort discovery loops (browser cookie scan, alternative profile locations). `except` body is `pass` or `continue` with a single-line `# best-effort: <what we tried>` comment.

### Credential redaction

The package handler installed by `configure_logging()` has a `RedactingFilter` attached. It runs for every record reaching the handler, including records originating in child loggers (`notebooklm._core`, `notebooklm._chat`, etc.) via Python logging's default propagation. The filter scrubs:

- CSRF tokens (`at=...`)
- Session IDs (`f.sid=...`)
- Google session cookies (`SAPISID`, `SID`, `HSID`, `SSID`, `__Secure-1PSID`, `__Secure-3PSID`)
- `Authorization: Bearer <token>` headers
- `Cookie: <jar>` headers

The filter pre-renders `record.exc_info` traceback into a scrubbed `record.exc_text` while preserving `record.exc_info` itself. The live exception object is not mutated.

To add a new secret pattern: edit `_REDACT_PATTERNS` in `src/notebooklm/_logging.py` and add a unit test in `tests/unit/test__logging.py` before merging.

### Attaching your own handler

`notebooklm` propagates to root by default, so `caplog`, `basicConfig`, and similar workflows work without configuration. To capture notebooklm logs in a dedicated handler:

```python
import logging
from notebooklm._logging import apply_redaction

handler = logging.handlers.SysLogHandler(...)
apply_redaction(handler)
logging.getLogger("notebooklm").addHandler(handler)
```

`apply_redaction()` attaches the `RedactingFilter` and wraps the formatter so your handler also benefits from credential scrubbing.

### Style — always lazy formatting

Use `%`-style log calls, not f-strings:

```python
logger.warning("Failed for %s in %.2fs", name, elapsed)  # OK
logger.warning(f"Failed for {name} in {elapsed:.2f}s")    # BAD
```

f-string eager evaluation defeats lazy formatting and (although the filter would still scrub via `record.getMessage()`) makes profile-time cost unconditional.

### Third-party loggers

`httpx`, `urllib3`, and `asyncio` can emit at DEBUG with full URLs and headers containing notebooklm-py credentials. The CLI calls `install_redaction` automatically when `-vv` is set:

```python
from notebooklm._logging import install_redaction
install_redaction("httpx", "urllib3")
```

Library consumers must do the same if they enable DEBUG on these loggers. If a third-party library sets `propagate=False` on its internal loggers (rare), pass child names explicitly:

```python
install_redaction("httpx._client", "urllib3.connectionpool")
```

### Trade-offs

The `RedactingFilter` preserves `record.exc_info` (the live exception object) so handlers like Sentry can still access it. However:

- Standard `logging.Formatter` uses `record.exc_text` (scrubbed by our filter) and does NOT re-render from `exc_info`. Safe.
- Custom formatters that ignore `exc_text` and read `exc_info` directly may render an unredacted traceback. **Mitigation**: wrap such handlers with `apply_redaction()` so the formatter is decorated and post-scrubs the final output regardless of which exception attribute it reads.
- Records propagate to root by default (`notebooklm.propagate = True`) so `caplog` and `basicConfig` work without changes. Our filter mutates the record before propagation, so downstream handlers (including root's) see the scrubbed version. **Caveat**: if a user attaches an unredacted handler directly to a child logger (`notebooklm._core`), that handler fires *before* propagation reaches our parent handler. Mitigation: `apply_redaction(child_handler)`.
- Applications that want notebooklm logs *isolated* from root can set `logging.getLogger('notebooklm').propagate = False` themselves.

---

## CI/CD

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push/PR | Unit tests, linting, type checking |
| `nightly.yml` | Daily 6 AM UTC | E2E tests with real API |
| `rpc-health.yml` | Daily 7 AM UTC | RPC method ID monitoring (see [stability.md](stability.md#automated-rpc-health-check)) |
| `testpypi-publish.yml` | Manual dispatch | Publish to TestPyPI |
| `verify-package.yml` | Manual dispatch | Verify TestPyPI or PyPI install + E2E |
| `publish.yml` | Tag push | Publish to PyPI |

### Setting Up Nightly E2E Tests

1. Get storage state: `cat ~/.notebooklm/storage_state.json`
2. Add GitHub secrets:
   - `NOTEBOOKLM_AUTH_JSON`: Storage state JSON
   - `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`: Your test notebook ID

### Maintaining Secrets

| Task | Frequency |
|------|-----------|
| Refresh credentials | Every 1-2 weeks |
| Check nightly results | Daily |

### Troubleshooting CI/CD Auth

**First step:** Run `notebooklm auth check --json` in your workflow to diagnose issues.

#### "NOTEBOOKLM_AUTH_JSON environment variable is set but empty"

**Cause:** The `NOTEBOOKLM_AUTH_JSON` env var is set to an empty string.

**Solution:**
- Ensure the GitHub secret is properly configured
- Check the secret isn't empty or whitespace-only
- Verify the workflow syntax: `${{ secrets.NOTEBOOKLM_AUTH_JSON }}`

#### "must contain valid Playwright storage state with a 'cookies' key"

**Cause:** The JSON in `NOTEBOOKLM_AUTH_JSON` is missing the required structure.

**Solution:** Ensure your secret contains valid Playwright storage state JSON:
```json
{
  "cookies": [
    {"name": "SID", "value": "...", "domain": ".google.com", ...},
    ...
  ],
  "origins": []
}
```

#### "Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set"

**Cause:** You're trying to run `notebooklm login` in CI/CD where `NOTEBOOKLM_AUTH_JSON` is set.

**Why:** The `login` command saves to a file, which conflicts with environment-based auth.

**Solution:**
- Don't run `login` in CI/CD - use the env var for auth instead
- If you need to refresh auth, do it locally and update the secret

#### Session expired in CI/CD

**Cause:** Google sessions expire periodically (typically every 1-2 weeks).

**Solution:**
1. Re-run `notebooklm login` locally
2. Copy the contents of `~/.notebooklm/storage_state.json`
3. Update your GitHub secret

#### Multiple accounts in CI/CD

Use separate secrets and set `NOTEBOOKLM_AUTH_JSON` per job:

```yaml
jobs:
  account-1:
    env:
      NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_ACCOUNT1 }}
    steps:
      - run: notebooklm list

  account-2:
    env:
      NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_ACCOUNT2 }}
    steps:
      - run: notebooklm list
```

#### Debugging CI/CD auth issues

Add diagnostic steps to your workflow:

```yaml
- name: Debug auth
  run: |
    # Comprehensive auth check (preferred)
    notebooklm auth check --json

    # Check if env var is set (without revealing content)
    if [ -n "$NOTEBOOKLM_AUTH_JSON" ]; then
      echo "NOTEBOOKLM_AUTH_JSON is set (length: ${#NOTEBOOKLM_AUTH_JSON})"
    else
      echo "NOTEBOOKLM_AUTH_JSON is not set"
    fi
```

The `auth check --json` output shows:
- Whether storage/env var is being used
- Which cookies are present
- Cookie domains (important for regional users)
- Any validation errors

---

## Getting Help

- Check existing implementations in `_*.py` files
- Look at test files for expected structures
- See [RPC Development Guide](rpc-development.md) for protocol details
- See [CONTRIBUTING.md](../CONTRIBUTING.md) for install, lint, and PR workflow
- Open an issue with captured request/response (sanitized)
