# Troubleshooting

**Status:** Active
**Last Updated:** 2026-04-01

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

Google rotates `__Secure-1PSIDTS` (the freshness partner of `__Secure-1PSID`) and emits the rotated value when the client touches an identity surface like `accounts.google.com`. The on-disk `Expires` field for `__Secure-1PSIDTS` is not a reliable predictor of server-side validity. Field observation on a stable IP with a non-Workspace account: a frozen `__Secure-1PSIDTS` survived **32+ hours** of continuous use without rotation, and Google naturally rotated it only **once** in 29 hours of probing. The earlier framing of "session dies after ~10-30 minutes" is the worst-case profile (datacenter egress, cross-IP, Workspace policy, or incomplete cookie extraction) rather than the typical one. The library handles cookie freshness in five layers, ordered from cheapest to heaviest:

1. **Per-call rotation poke (default ON).** Every `fetch_tokens` call makes a best-effort POST to `https://accounts.google.com/RotateCookies`. The rotated `Set-Cookie` lands in the httpx jar and is persisted on session close. Failures are logged at DEBUG and never abort the call. To protect `accounts.google.com` from rapid sequential CLI invocations, the poke is skipped if `storage_state.json` was modified within the last 60 seconds — well below Google's own declared rotation cadence (~600 s).
   - Disable in restricted networks: `export NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`

2. **Periodic background poke (opt-in, Python only).** For long-lived `NotebookLMClient` instances (workers, agents, `async with` blocks held open for hours) layer 1 is not enough — there are no `fetch_tokens` calls between RPCs to drive rotation. Pass `keepalive=<seconds>` and the client spawns an asyncio background task that pokes the same `RotateCookies` endpoint at the configured interval and persists any rotated `Set-Cookie` to `storage_state.json` immediately (so a crash doesn't lose the rotation). Persistence happens off-loop (`asyncio.to_thread`), so the loop never blocks the event loop on disk I/O.
   ```python
   async with await NotebookLMClient.from_storage(keepalive=600) as client:
       # SIDTS is refreshed every ~10 minutes while this block is open.
       ...
   ```
   - Disabled by default; pass `keepalive=None` to turn it off explicitly.
   - Must be `None` or a positive finite number; `keepalive_min_interval` (default 60 s) clamps small values up to avoid hammering Google's identity surface. Invalid inputs (`0`, negative, `nan`, `inf`) raise `ValueError`.
   - Persistence requires either `auth.storage_path` or the explicit `storage_path=` constructor arg to be set; without one of those, the loop pokes but does not write to disk.
   - Poke failures are logged at DEBUG and never propagate. Persistence failures (the more dangerous failure mode — a rotation succeeded in memory but never landed on disk) are logged at WARNING with the storage path. Either way, the next iteration retries.
   - Honors `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` (the poke itself becomes a no-op when set, but the task still wakes; pass `keepalive=None` to disable the loop entirely).

3. **External recovery script (opt-in).** When auth has fully expired (idle past the rotation window, force-logout, password change), `fetch_tokens` can shell out to a user-provided refresh script, reload `storage_state.json`, and retry once.
   - Wire it up:
     ```bash
     pip install 'notebooklm-py[cookies]'
     export NOTEBOOKLM_REFRESH_CMD="python /path/to/notebooklm-py/examples/refresh_browser_cookies.py"
     ```
   - The script in `examples/refresh_browser_cookies.py` re-runs `notebooklm login --browser-cookies` against your local browser. The library injects `NOTEBOOKLM_REFRESH_PROFILE` and `NOTEBOOKLM_REFRESH_STORAGE_PATH` so the script targets the right file. Retry is gated to once per process — a broken script can't loop.

4. **Re-login (manual).** If the recovery script also fails (e.g. browser isn't logged in either), run `notebooklm login` interactively.

5. **External scheduler (`notebooklm auth refresh` + cron / launchd / systemd).** Layers 1-2 only fire when a Python process is running. If you go idle longer than the SIDTS server window between calls (i.e. you don't make `fetch_tokens` calls and don't hold a `NotebookLMClient` open), no in-process layer can rotate. The fix is to wake the OS scheduler periodically and have it run a one-shot refresh.

   The command is:
   ```bash
   notebooklm auth refresh                 # one-shot, exit 0/1
   notebooklm --profile work auth refresh  # against a named profile
   ```

   Internally it opens a client (which triggers the layer-1 poke against `accounts.google.com` and a follow-on GET to `notebooklm.google.com` whose CSRF/session response is discarded — only the cookie-jar side effect matters) and persists rotated cookies to `storage_state.json`. Recommended cadence is **15-20 minutes**; tighter is wasteful, significantly looser may cross the SIDTS server-side validity window for your account/region.

   **macOS launchd** (`~/Library/LaunchAgents/com.user.notebooklm-keepalive.plist`):
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.user.notebooklm-keepalive</string>
     <key>ProgramArguments</key>
     <array>
       <string>/abs/path/.venv/bin/notebooklm</string>
       <string>--profile</string><string>work</string>
       <string>auth</string><string>refresh</string>
       <string>--quiet</string>
     </array>
     <key>StartInterval</key><integer>1200</integer>
     <key>RunAtLoad</key><true/>
     <key>StandardErrorPath</key><string>/tmp/notebooklm-keepalive.err</string>
   </dict>
   </plist>
   ```
   Load: `launchctl load ~/Library/LaunchAgents/com.user.notebooklm-keepalive.plist`. Note that `StartInterval` is wall-clock-based but launchd does not fire missed firings on wake from sleep — a Mac that sleeps for hours will skip every interval until the next active tick. Layer 1 (the per-call poke) covers the next user-driven call automatically; if the gap is long enough that SIDTS expired during sleep, layer 3 (`NOTEBOOKLM_REFRESH_CMD`) is your recovery.

   **Linux systemd user timer** (`~/.config/systemd/user/notebooklm-keepalive.{service,timer}`):
   ```ini
   # notebooklm-keepalive.service
   [Unit]
   Description=NotebookLM cookie keepalive

   [Service]
   Type=oneshot
   ExecStart=/abs/path/.venv/bin/notebooklm --profile work auth refresh --quiet
   ```
   ```ini
   # notebooklm-keepalive.timer
   [Unit]
   Description=Run NotebookLM keepalive every 20 minutes

   [Timer]
   OnBootSec=2min
   OnUnitActiveSec=20min
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```
   Enable: `systemctl --user enable --now notebooklm-keepalive.timer`. `Persistent=true` runs a missed firing after wake-from-suspend.

   **POSIX cron** (works on Linux / macOS, simplest fallback):
   ```cron
   7,27,47 * * * * /abs/path/.venv/bin/notebooklm --profile work auth refresh --quiet >>~/.notebooklm-keepalive.log 2>&1
   ```
   (Offset minutes — `7,27,47` instead of `*/20` — keeps you off the global cron fleet's `:00 / :20 / :40` collision marks; harmless either way for a single user, but a good habit if your cookie surface ever gets per-IP-rate-limited.)

   **Windows Task Scheduler** — create a task triggered "On a schedule, repeat every 20 minutes indefinitely", action "Start a program":
   - Program: `C:\path\to\.venv\Scripts\notebooklm.exe`
   - Arguments: `--profile work auth refresh --quiet`
   - "Run whether user is logged on or not" + "Run with highest privileges" off (user-level is fine).

   Or via PowerShell:
   ```powershell
   $action = New-ScheduledTaskAction -Execute "C:\path\to\.venv\Scripts\notebooklm.exe" `
     -Argument "--profile work auth refresh --quiet"
   $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
     -RepetitionInterval (New-TimeSpan -Minutes 20) `
     -RepetitionDuration ([TimeSpan]::FromDays(36500))
   Register-ScheduledTask -TaskName "NotebookLM Keepalive" -Action $action -Trigger $trigger
   ```
   `-RepetitionDuration` is required; `-RepetitionInterval` alone defaults to a 24-hour repetition window and the task silently stops firing after a day. The `36500` days (~100 years) is the idiomatic "indefinitely" value for `New-ScheduledTaskTrigger`.

   **Docker / k8s** — run as a sidecar with an entrypoint loop, or a CronJob:
   ```yaml
   apiVersion: batch/v1
   kind: CronJob
   metadata: {name: notebooklm-keepalive}
   spec:
     schedule: "7,27,47 * * * *"
     concurrencyPolicy: Forbid    # don't double-run if a fire is slow
     successfulJobsHistoryLimit: 1
     failedJobsHistoryLimit: 3
     jobTemplate:
       spec:
         template:
           spec:
             restartPolicy: OnFailure
             containers:
               - name: keepalive
                 image: your/notebooklm-image
                 command: ["notebooklm", "--profile", "work", "auth", "refresh", "--quiet"]
                 volumeMounts:
                   - {name: storage, mountPath: /root/.notebooklm}
             volumes:
               - {name: storage, persistentVolumeClaim: {claimName: notebooklm-storage}}
   ```
   `concurrencyPolicy: Forbid` ensures a slow fire (e.g. a 60s `_run_refresh_cmd` timeout if you also have layer 2 wired up) doesn't overlap with the next 20-minute schedule and end up with two writers racing on `storage_state.json`.

   What layer 5 cannot fix: server-side revocation (account locked, password changed, force sign-out) — that's still layer 3's job. Long device sleep where the OS scheduler doesn't fire — when the device wakes, the next call recovers via layer 1 if SIDTS is still alive, otherwise via layer 3.

For most users layer 1 alone is enough. Add layer 2 when you hold a `NotebookLMClient` open for hours (no per-call rotation drives it). Add layer 3 for unattended workflows that may hit a fully-expired session and need to recover automatically. Add layer 5 if you have an idle profile that needs to stay fresh between manual interactions and there's no Python process to drive layers 1-2.

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

#### "CSRF token missing" or "SNlM0e not found"

**Cause:** CSRF token expired or couldn't be extracted.

**Note:** This error should rarely occur now due to automatic retry. If you see it, it likely means the automatic refresh also failed.

**Solution (if auto-refresh fails):**
```python
# In Python - manual refresh
await client.refresh_auth()
```
Or re-run `notebooklm login` if session cookies are also expired.

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

**Workaround:**
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

`notebooklm-py` provides structured logging to help debug issues.

**Environment Variables:**

| Variable | Default | Effect |
|----------|---------|--------|
| `NOTEBOOKLM_LOG_LEVEL` | `WARNING` | Set to `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `NOTEBOOKLM_DEBUG_RPC` | (unset) | Legacy: Set to `1` to enable `DEBUG` level |

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
