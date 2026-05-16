# Troubleshooting

**Status:** Active
**Last Updated:** 2026-05-14

Common issues, known limitations, and workarounds for `notebooklm-py`.

## Common Errors

### Authentication Errors

**First step:** Run `notebooklm auth check` to diagnose auth issues:
```bash
notebooklm auth check          # Quick local validation
notebooklm auth check --test   # Full validation with network test
notebooklm auth check --json   # Machine-readable output for CI/CD
```

This shows:
- Storage file location and validity
- Which cookies are present and their domains
- Whether NOTEBOOKLM_AUTH_JSON or NOTEBOOKLM_HOME is being used
- (With `--test`) Whether token fetch succeeds

#### Automatic Token Refresh

The client **automatically refreshes** CSRF tokens when authentication errors are detected. This happens transparently:

- When an RPC call fails with an auth error, the client:
  1. Fetches fresh CSRF token and session ID from the NotebookLM homepage
  2. Waits briefly to avoid rate limiting
  3. Retries the failed request once
- Concurrent requests share a single refresh task to prevent token thrashing
- If refresh fails, the original error is raised with the refresh failure as cause

This means most "CSRF token expired" errors resolve automatically.

#### Cookie freshness for long-running / unattended use

Google rotates `__Secure-1PSIDTS` (the freshness partner of `__Secure-1PSID`) on its own cadence; the on-disk `Expires` field is **not** a reliable predictor of server-side validity. The library handles freshness in five layers, ordered cheapest to heaviest:

1. **Per-call rotation poke** (default ON) — every `fetch_tokens` makes a best-effort POST to `accounts.google.com/RotateCookies`. Disable with `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`.
2. **Periodic background poke** — pass `keepalive=<seconds>` to `NotebookLMClient` for clients held open for hours.
3. **External recovery script** — `NOTEBOOKLM_REFRESH_CMD` runs when auth has fully expired, then retries once.
4. **Manual re-login** — `notebooklm login`.
5. **External scheduler** — `notebooklm auth refresh` driven by cron / launchd / systemd / Task Scheduler / k8s CronJob, for idle profiles with no Python process running. Recommended cadence: 15–20 minutes.

Most users only need layer 1 — it's on by default and requires no configuration. For the full strategy (trade-offs between layers, all knobs like `keepalive_min_interval` and `NOTEBOOKLM_REFRESH_CMD_USE_SHELL`, and ready-to-paste launchd / systemd / cron / Task Scheduler / k8s CronJob recipes), see **[docs/auth-keepalive.md#tldr](auth-keepalive.md#tldr)** for a quick orientation, then [§4 The architecture](auth-keepalive.md#4--the-architecture) for the per-layer deep dive.

#### macOS: `--browser-cookies` prompts for your password

On macOS, Chrome (and Edge / Brave / Opera) encrypts its cookies file with a key stored in the **macOS Keychain** under the entry `Chrome Safe Storage`. By default that entry's ACL only allows `Google Chrome.app` itself to read the key without prompting; any other process — Python, Terminal, cron, an editor — gets a "wants to use the *Chrome Safe Storage* key" dialog. This is how macOS Keychain protects local data and applies to every cookie-extraction tool (`rookiepy`, `browser-cookie3`, `pycookiecheat`), not just `notebooklm-py`.

Workarounds, ordered by hassle:

1. **Click "Always Allow" in the prompt.** Adds the calling Python interpreter to the Keychain entry's ACL so subsequent runs of *that exact binary* should stop prompting. Caveat: rebuilding your venv (e.g. `uv venv` again) usually changes the interpreter path and you'll be re-prompted once for the new path.

2. **Use Touch ID instead of typing the password.** macOS Sonoma+ accepts Touch ID for Keychain dialogs — see *System Settings → Touch ID & Password*.

3. **Pre-unlock the login keychain in your shell** (best for cron jobs after one initial interactive run):
   ```bash
   security unlock-keychain ~/Library/Keychains/login.keychain-db
   ```
   Prompts once for your login password, then any process in the same login session can read entries you've already approved without re-prompting until the keychain auto-locks.

4. **Use Firefox as the cookie source.** Firefox stores cookies in a plain SQLite DB (no Keychain), so `notebooklm login --browser-cookies firefox` runs with **no prompt at all** — provided you're logged into Google in Firefox.
   ```bash
   notebooklm login --browser-cookies firefox
   ```
   This is the simplest answer for unattended macOS use.

   **Firefox Multi-Account Containers note.** If your Google session
   lives inside a container, unscoped `--browser-cookies firefox` will
   merge cookies from every container into one jar (see issues
   [#366](https://github.com/teng-lin/notebooklm-py/issues/366) /
   [#367](https://github.com/teng-lin/notebooklm-py/issues/367)) and
   produce an inconsistent session. Use the explicit container syntax:
   ```bash
   notebooklm login --browser-cookies 'firefox::Work'    # named container
   notebooklm login --browser-cookies 'firefox::none'    # no-container default
   ```
   When a container is in use, the unscoped form also emits a yellow
   warning pointing at this syntax.

5. **Truly headless servers.** `--browser-cookies` is not the right tool — there's no live browser to extract from. Either re-extract on a workstation and ship `storage_state.json` to the server, or accept that human interaction is needed when cookies finally expire.

Quick diagnostic:
```bash
security find-generic-password -s 'Chrome Safe Storage' -a 'Chrome' -w >/dev/null && echo OK || echo "ACL or lock issue"
```
Prints `OK` without prompting → keychain is unlocked and your user has access; the prompt you saw is the per-binary ACL re-asking for a new caller (your Python). Click *Always Allow* once and that binary is permanently approved. If it prompts → run `security unlock-keychain` first.

#### "Unauthorized" or redirect to login page

**Cause:** Session cookies expired (happens every few weeks).

**Note:** Automatic token refresh handles CSRF/session ID expiration. This error only occurs when the underlying cookies (set during `notebooklm login`) have fully expired.

**Solution:**
```bash
notebooklm login
```

#### "Failed to extract CSRF token (SNlM0e)" / "CSRF token not found in HTML"

**Cause:** The CSRF token (`SNlM0e`) couldn't be extracted from the NotebookLM page response. The exact wording depends on which code path raised it:

- `Failed to extract CSRF token (SNlM0e). Page structure may have changed or authentication expired. Preview: '...'` — raised by `refresh_auth()` when the WIZ_global_data extraction fails ([`client.py`](../src/notebooklm/client.py)).
- `CSRF token not found in HTML. Final URL: <url> This may indicate the page structure has changed.` — raised by the lower-level extractor when no auth redirect was detected ([`auth.py`](../src/notebooklm/auth.py)).
- `Failed to extract 'SNlM0e' from NotebookLM HTML response. This usually means Google changed the page structure. Preview: '...'` — raised as `AuthExtractionError` directly (rare; usually wrapped by one of the messages above) ([`exceptions.py`](../src/notebooklm/exceptions.py)).

A related auth-redirect message — `Authentication expired. Run 'notebooklm login' to re-authenticate.` (or `Authentication expired or invalid. ...`) — surfaces the same root cause when the page redirected to Google's login flow.

**Note:** These errors should rarely surface, since the client automatically retries with a fresh CSRF token on auth failures (see *Automatic Token Refresh* above). When one does reach you, the automatic refresh also failed.

**Solution (if auto-refresh fails):**
```python
# In Python — manual refresh
await client.refresh_auth()
```
Or re-run `notebooklm login` if session cookies are also expired. If the failure persists across re-login, the page structure has likely changed — file an issue and include the `Preview:` snippet from the error.

#### Browser opens but login fails

**Cause:** Google detecting automation and blocking login.

**Solution:**
1. Delete the browser profile: `rm -rf ~/.notebooklm/browser_profile/`
2. Run `notebooklm login` again
3. Complete any CAPTCHA or security challenges Google presents
4. Ensure you're using a real mouse/keyboard (not pasting credentials via script)

### RPC Errors

#### "RPCError: No result found for RPC ID: XyZ123"

**Cause:** The RPC method ID may have changed (Google updates these periodically), or:
- Rate limiting from Google
- Account quota exceeded
- API restrictions

**Diagnosis:**
```bash
# Enable debug mode to see what RPC IDs the server returns
NOTEBOOKLM_DEBUG_RPC=1 notebooklm <your-command>
```

This will show output like:
```
DEBUG: Looking for RPC ID: Ljjv0c
DEBUG: Found RPC IDs in response: ['NewId123']
```

If the IDs don't match, the method ID has changed. Report the new ID in a GitHub issue.

**Workaround:**
- Wait 5-10 minutes and retry
- Try with fewer sources selected
- Reduce generation frequency

#### RPC method ID rotated by Google — self-patch with `NOTEBOOKLM_RPC_OVERRIDES`

Google rotates undocumented batchexecute method IDs without warning. When
this happens, `notebooklm-py` raises `UnknownRPCMethodError` with the new ID
the server now uses (see the previous section's diagnosis recipe). Rather
than waiting for a release, you can patch the mapping for your process with
the `NOTEBOOKLM_RPC_OVERRIDES` environment variable.

**Format:** JSON object mapping `RPCMethod` member names (the Python enum
member name, not the obfuscated value) to the override RPC ID:

```bash
export NOTEBOOKLM_RPC_OVERRIDES='{"LIST_NOTEBOOKS": "newId123", "CREATE_NOTEBOOK": "newId456"}'
notebooklm list
```

Or in Python:

```python
import os
os.environ["NOTEBOOKLM_RPC_OVERRIDES"] = '{"LIST_NOTEBOOKS": "newId123"}'

from notebooklm import NotebookLMClient
# Subsequent client calls send the override IDs on the wire.
```

**Behavior:**

- The override is applied at BOTH the URL `rpcids=` query parameter AND the
  request body `f.req` payload, so the wire format stays consistent.
- The override is gated on the configured base host being a known Google
  NotebookLM endpoint (`notebooklm.google.com` or
  `notebooklm.cloud.google.com`). Overrides do NOT apply to non-Google
  hosts, so this env var cannot be weaponised to leak custom RPC IDs to a
  hostile endpoint.
- Method names not listed in the override map continue to use the canonical
  IDs from `notebooklm.rpc.types.RPCMethod`.
- Malformed input (invalid JSON, top-level array, etc.) is logged at
  `WARNING` and treated as no overrides.
- The first time a distinct override set is applied in a process, the
  mapping is logged at `INFO` so you can confirm the config you intended is
  live.

**Discovering the new ID:** see the `NOTEBOOKLM_DEBUG_RPC=1` recipe above —
the `Found RPC IDs in response: [...]` line tells you what the server is
now returning. Cross-reference against the call site that failed.

Please also report the rotated IDs in a GitHub issue so the canonical
mapping in `src/notebooklm/rpc/types.py` can be updated for everyone.

#### How to get the full response preview from an RPCError

`RPCError.raw_response` is truncated to **80 chars + `"..."`** by default so
error messages stay readable in logs and CLI output. When you need the
full body to diagnose schema drift or a malformed response, opt in:

```bash
NOTEBOOKLM_DEBUG=1 notebooklm <your-command>
```

Or in Python, set the env var before instantiating the client:

```python
import os
os.environ["NOTEBOOKLM_DEBUG"] = "1"

from notebooklm import NotebookLMClient
# Subsequent RPCError instances will carry the full untruncated body.
```

The value must be exactly `"1"` — `"0"`, `"true"`, etc. are treated as
unset (still truncated).

#### "RPCError: [3]" or "UserDisplayableError"

**Cause:** Google API returned an error, typically:
- Invalid parameters
- Resource not found
- Rate limiting

**Solution:**
- Check that notebook/source IDs are valid
- Add delays between operations (see Rate Limiting section)

### Generation Failures

#### Audio/Video generation returns None

**Cause:** Known issue with artifact generation under heavy load or rate limiting.

**Workaround:**
```bash
# Use --wait to see if it eventually succeeds
notebooklm generate audio --wait

# Or poll manually
notebooklm artifact poll <task_id>
```

#### Mind map or data table "generates" but doesn't appear

**Cause:** Generation may silently fail without error.

**Solution:**
- Wait 60 seconds and check `artifact list`
- Try regenerating with different/fewer sources

### File Upload Issues

#### Text/Markdown files upload but return None

**Cause:** Known issue with native text file uploads.

**Workaround:** Use `add_text` instead:
```bash
# Instead of: notebooklm source add ./notes.txt
# Do:
notebooklm source add "$(cat ./notes.txt)"
```

Or in Python:
```python
content = Path("notes.txt").read_text()
await client.sources.add_text(nb_id, "My Notes", content)
```

#### Large files time out

**Cause:** Files over ~20MB may exceed upload timeout.

**Solution:** Split large documents or use text extraction locally.

---

### Protected Website Content Issues

#### X.com / Twitter content incorrectly parsed as error page

**Symptoms:**
- Source title shows "Fixing X.com Privacy Errors" or similar error message
- Generated content discusses browser extensions instead of the actual article
- Source appears to process successfully but contains wrong content

**Cause:** X.com (Twitter) has aggressive anti-scraping protections. When NotebookLM attempts to fetch the URL, it receives an error page or compatibility warning instead of the actual content.

**Solution - Use `bird` CLI to pre-fetch content:**

The `bird` CLI can fetch X.com content and output clean markdown:

```bash
# Step 1: Install bird (macOS/Linux)
brew install steipete/tap/bird

# Step 2: Fetch X.com content as markdown
bird read "https://x.com/username/status/1234567890" > article.md

# Step 3: Add the local markdown file to NotebookLM
notebooklm source add ./article.md
```

**Alternative methods:**

**Using browser automation:**
```bash
# If you have playwright/browser-use available
# Fetch content via browser and save as markdown
```

**Manual extraction:**
1. Open the X.com post in a browser
2. Copy the text content
3. Save to a `.md` file
4. Add the file to NotebookLM

**Verification:**

Always verify the source was correctly parsed:
```bash
notebooklm source list
# Check that the title matches the actual article, not an error message
```

If the title contains error-related text, remove the source and use the pre-fetch method:
```bash
# Remove incorrectly parsed source
notebooklm source delete <source_id>
# Or, if you only have the exact title:
notebooklm source delete-by-title "Exact Source Title"

# Then re-add using the bird CLI method above
```

**Other affected sites:**
- Some paywalled news sites
- Sites requiring JavaScript execution for content
- Sites with aggressive bot detection

---

## Known Limitations

### Rate Limiting

Google enforces strict rate limits on the batchexecute endpoint.

**Symptoms:**
- RPC calls return `None`
- `RPCError` with ID `R7cb6c`
- `UserDisplayableError` with code `[3]`

**Best Practices:**

**CLI:** Use `--retry` for automatic exponential backoff:
```bash
notebooklm generate audio --retry 3   # Retry up to 3 times on rate limit
notebooklm generate video --retry 5   # Works with all generate commands
```

**Python:**
```python
import asyncio

# Add delays between intensive operations
for url in urls:
    await client.sources.add_url(nb_id, url)
    await asyncio.sleep(2)  # 2 second delay

# Use exponential backoff on failures
async def retry_with_backoff(coro, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await coro
        except RPCError:
            wait = 2 ** attempt  # 1, 2, 4 seconds
            await asyncio.sleep(wait)
    raise Exception("Max retries exceeded")
```

### Cannot start a brand-new conversation from the SDK (issue #659)

`client.chat.ask(notebook_id, question)` with `conversation_id=None`
(and the CLI's `notebooklm ask --new`) attach the question to the user's
**current** conversation on the notebook — they do not start a brand-new
one. This matches the web UI's default behavior: the UI continues the
most-recent conversation unless you explicitly click "New conversation"
in the UI.

The SDK fetches the server-recorded conversation_id via `hPTbtc` after
each new-conversation ask and returns it on `AskResult.conversation_id`,
so follow-ups using that id work correctly. Repeated `ask()` calls
without `conversation_id` extend the same conversation; they do not
restart.

If you need a truly fresh conversation, create a new notebook:

```bash
notebooklm create "Fresh thread"
notebooklm use <new-notebook-id>
notebooklm ask "Starting question"
```

The dedicated "create conversation" RPC the web UI's "+ New" button
calls has not been reverse-engineered. PRs welcome.

**History:** Before this fix, the SDK minted a client-side `uuid.uuid4()`
at the wire `conversation_id` slot. The server accepted the question and
returned an answer, but the conversation was orphaned from the web UI
conversation list and follow-ups using `result.conversation_id` produced
ghost turns the server never recorded. Live API testing confirmed the
correct contract is to send `null` and recover the id via `hPTbtc`.

### Quota Restrictions

Some features have daily/hourly quotas:
- **Audio Overviews:** Limited generations per day per account
- **Video Overviews:** More restricted than audio
- **Deep Research:** Consumes significant backend resources

### Download Requirements

Artifact downloads (audio, video, images) use `httpx` with cookies from your storage state. **Playwright is NOT required for downloads**—only for the initial `notebooklm login`.

If downloads fail with authentication errors:

**Solution:** Ensure your authentication is valid:
```bash
# Re-authenticate if cookies have expired
notebooklm login

# Or copy a fresh storage_state.json from another machine
```

**Custom auth paths:** When using `from_storage(path=...)` or `from_storage(profile="work")`,
artifact downloads automatically use the same storage path for cookie authentication.
If you are on an older version where downloads fail with "Storage file not found" pointing
to the default location, upgrade or set `NOTEBOOKLM_HOME` as a workaround.

### URL Expiry

Download URLs for audio/video are temporary:
- Expire within hours
- Always fetch fresh URLs before downloading:

```python
# Get fresh artifact list before download
artifacts = await client.artifacts.list(nb_id)
audio = next(a for a in artifacts if a.kind == "audio")
# Use audio.url immediately
```

---

## Platform-Specific Issues

### Linux

**Playwright missing dependencies:**
```bash
playwright install-deps chromium
```

**`playwright install chromium` fails with `TypeError: onExit is not a function`:**

This is an environment-specific Playwright install failure that has been observed with some newer Playwright builds on Linux. `notebooklm-py` only needs a working browser install for `notebooklm login`; the workaround is to install a known-good Playwright version in a clean virtual environment.

**Workaround** (intentionally uses `pip` rather than the canonical `uv sync --frozen` flow from [installation.md#e-contributor](installation.md#e-contributor) — this workaround needs to *override* the `playwright>=1.40.0` constraint to a specific older version, which `uv sync --frozen` would refuse):
```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install "playwright==1.57.0"
python -m playwright install chromium
pip install -e ".[all]"
```

**Why this order matters:**
- `python -m playwright ...` ensures you use the Playwright module from the active virtual environment
- installing the browser before `pip install -e ".[all]"` avoids picking up an older broken global `playwright` executable
- if you already have another `playwright` on your system, verify with `which playwright` after activation
- using `pip` here (not `uv sync --frozen`) is deliberate: this workaround needs to override the project's resolved `playwright` version with a specific older release, which the locked `uv` flow would block

If you need a non-editable install from Git instead of a local checkout, replace the last step with:
```bash
pip install "git+https://github.com/<your-user>/notebooklm-py@<branch>"
```

**No display available (headless server):**
- Browser login requires a display
- Authenticate on a machine with GUI, then copy `storage_state.json`

### macOS

**Chromium not opening:**
```bash
# Re-install Playwright browsers
playwright install chromium
```

**Security warning about Chromium:**
- Allow in System Preferences → Security & Privacy

### Windows

**CLI hangs indefinitely (issue #75):**

On certain Windows environments (particularly when running inside Sandboxie or similar sandboxing software), the CLI may hang indefinitely at startup. This is caused by the default `ProactorEventLoop` blocking at the IOCP (I/O Completion Ports) layer.

**Symptoms:**
- CLI starts but never responds
- Process appears frozen with no output
- Happens consistently in sandboxed environments

**Solution:** The library automatically sets `WindowsSelectorEventLoopPolicy` at CLI startup to avoid this issue. If you're using the Python API directly and encounter hanging, add this before any async code:

```python
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

**Unicode encoding errors on non-English Windows (issue #75, #80):**

Windows systems with non-English locales (Chinese cp950, Japanese cp932, etc.) may fail with `UnicodeEncodeError` when outputting Unicode characters like checkmarks (✓) or emojis.

**Symptoms:**
- `UnicodeEncodeError: 'cp950' codec can't encode character`
- Error occurs when printing status output with Rich tables

**Solution:** The library automatically sets `PYTHONUTF8=1` at CLI startup. For Python API usage, either:
1. Set `PYTHONUTF8=1` environment variable before running
2. Run Python with `-X utf8` flag: `python -X utf8 your_script.py`

**Path issues:**
- Use forward slashes or raw strings: `r"C:\path\to\file"`
- Ensure `~` expansion works: use `Path.home()` in Python

### WSL

**Browser opens in Windows, not WSL:**
- This is expected behavior
- Storage file is saved in WSL filesystem

---

## Debugging Tips

### Logging Configuration

`notebooklm-py` provides structured logging to help debug issues. The variables below are the logging-relevant subset; for the full environment-variable reference (storage, profile, network, decoder strictness, RPC overrides, etc.) and precedence rules, see [docs/configuration.md#environment-variables](configuration.md#environment-variables).

**Environment Variables (logging-specific):**

| Variable | Default | Effect |
|----------|---------|--------|
| `NOTEBOOKLM_LOG_LEVEL` | `WARNING` | Set to `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `NOTEBOOKLM_DEBUG_RPC` | (unset) | Legacy: Set to `1` to enable `DEBUG` level |
| `NOTEBOOKLM_DEBUG` | (unset) | Set to `1` to preserve the full raw RPC response body on `RPCError.raw_response` (default: truncated to 80 chars + `"..."`) |

**When to use each level:**

```bash
# WARNING (default): Only show warnings and errors
notebooklm list

# INFO: Show major operations (good for scripts/automation)
NOTEBOOKLM_LOG_LEVEL=INFO notebooklm source add https://example.com
# Output:
#   14:23:45 INFO [notebooklm._sources] Adding URL source: https://example.com

# DEBUG: Show all RPC calls with timing (for troubleshooting API issues)
NOTEBOOKLM_LOG_LEVEL=DEBUG notebooklm list
# Output:
#   14:23:45 DEBUG [notebooklm._core] RPC LIST_NOTEBOOKS starting
#   14:23:46 DEBUG [notebooklm._core] RPC LIST_NOTEBOOKS completed in 0.842s
```

**Programmatic use:**

```python
import logging
import os

# Set before importing notebooklm
os.environ["NOTEBOOKLM_LOG_LEVEL"] = "DEBUG"

from notebooklm import NotebookLMClient
# Now all notebooklm operations will log at DEBUG level
```

### Test Basic Operations

Start simple to isolate issues:

```bash
# 1. Can you list notebooks?
notebooklm list

# 2. Can you create a notebook?
notebooklm create "Test"

# 3. Can you add a source?
notebooklm source add "https://example.com"
```

### Network Debugging

If you suspect network issues:

```python
import httpx

# Test basic connectivity
async with httpx.AsyncClient() as client:
    r = await client.get("https://notebooklm.google.com")
    print(r.status_code)  # Should be 200 or 302
```

---

## Getting Help

1. Check this troubleshooting guide
2. Search [existing issues](https://github.com/teng-lin/notebooklm-py/issues)
3. Open a new issue with:
   - Command/code that failed
   - Full error message
   - Python version (`python --version`)
   - Library version (`notebooklm --version`)
   - Operating system
