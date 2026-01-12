# Troubleshooting

**Status:** Active
**Last Updated:** 2026-01-12

Common issues, known limitations, and workarounds for `notebooklm-py`.

## Common Errors

### Authentication Errors

See [Authentication Lifecycle](#authentication-lifecycle) for background on token types.

#### Automatic Token Refresh

The client **automatically refreshes** CSRF tokens when authentication errors are detected. This happens transparently:

- When an RPC call fails with an auth error, the client:
  1. Fetches fresh CSRF token and session ID from the NotebookLM homepage
  2. Waits briefly to avoid rate limiting
  3. Retries the failed request once
- Concurrent requests share a single refresh task to prevent token thrashing
- If refresh fails, the original error is raised with the refresh failure as cause

This means most "CSRF token expired" errors resolve automatically.

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

## Known Limitations

### Rate Limiting

Google enforces strict rate limits on the batchexecute endpoint.

**Symptoms:**
- RPC calls return `None`
- `RPCError` with ID `R7cb6c`
- `UserDisplayableError` with code `[3]`

**Best Practices:**
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

### Authentication Lifecycle

notebooklm-py uses two types of tokens with different lifetimes:

| Token Type | Lifetime | Auto-Refresh? | When Expired |
|------------|----------|---------------|--------------|
| **CSRF Token** (SNlM0e) | Hours | ✅ Yes | Automatic retry via `refresh_callback` |
| **Session Cookies** (SID, HSID, etc.) | Weeks | ❌ No | Run `notebooklm login` |

**CSRF tokens** expire frequently (hours) but are automatically refreshed when auth errors are detected. This is transparent to users—the library retries failed requests after refreshing.

**Session cookies** are the underlying Google login and last weeks. When these expire, you must re-authenticate:

```bash
notebooklm login
```

The browser profile at `$NOTEBOOKLM_HOME/browser_profile/` (default: `~/.notebooklm/browser_profile/`) remembers your Google login, so re-authentication is typically just pressing ENTER (no password re-entry needed).

**For CI/CD and automation:**
- Use `NOTEBOOKLM_AUTH_JSON` environment variable
- Update the secret every 1-2 weeks when sessions expire
- CSRF auto-refresh handles day-to-day expiration

**Session expiration triggers:**
- Time-based expiration (weeks)
- Security events (password change, suspicious activity)
- Heavy API usage may trigger earlier expiration

### Non-Functional RPC Methods

Some discovered RPC endpoints don't work as expected:

| RPC ID | Issue | Workaround |
|--------|-------|------------|
| `BnLyuf` (GET_ARTIFACT) | Returns 400 | Use `list` and filter |
| `hizoJc` (GET_SOURCE) | Unreliable | Get from notebook metadata |

### Download Requirements

Artifact downloads (audio, video, images) use `httpx` with cookies from your storage state. **Playwright is NOT required for downloads**—only for the initial `notebooklm login`.

If downloads fail with authentication errors:

**Solution:** Ensure your authentication is valid:
```bash
# Re-authenticate if cookies have expired
notebooklm login

# Or copy a fresh storage_state.json from another machine
```

### URL Expiry

Download URLs for audio/video are temporary:
- Expire within hours
- Always fetch fresh URLs before downloading:

```python
# Get fresh artifact list before download
artifacts = await client.artifacts.list(nb_id)
audio = next(a for a in artifacts if a.artifact_type == "audio")
# Use audio.url immediately
```

---

## Platform-Specific Issues

### Linux

**Playwright missing dependencies:**
```bash
playwright install-deps chromium
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

### Enable Verbose Output

Check what's happening under the hood:

```bash
# See full error messages
notebooklm list 2>&1

# Verify auth is working
notebooklm status
```

### Check Storage State

Verify your session file is valid:

```bash
# Check file exists and has content
cat ~/.notebooklm/storage_state.json | python -m json.tool | head -20
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

## CI/CD Issues

### "NOTEBOOKLM_AUTH_JSON environment variable is set but empty"

**Cause:** The `NOTEBOOKLM_AUTH_JSON` env var is set to an empty string.

**Solution:**
- Ensure the GitHub secret is properly configured
- Check the secret isn't empty or whitespace-only
- Verify the workflow syntax: `${{ secrets.NOTEBOOKLM_AUTH_JSON }}`

### "must contain valid Playwright storage state with a 'cookies' key"

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

### "Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set"

**Cause:** You're trying to run `notebooklm login` in CI/CD where `NOTEBOOKLM_AUTH_JSON` is set.

**Why:** The `login` command saves to a file, which conflicts with environment-based auth.

**Solution:**
- Don't run `login` in CI/CD - use the env var for auth instead
- If you need to refresh auth, do it locally and update the secret

### Session expired in CI/CD

**Cause:** Google sessions expire periodically (typically every 1-2 weeks).

**Solution:**
1. Re-run `notebooklm login` locally
2. Copy the contents of `~/.notebooklm/storage_state.json`
3. Update your GitHub secret

### Multiple accounts in CI/CD

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

### Debugging CI/CD auth issues

Add diagnostic steps to your workflow:

```yaml
- name: Debug auth
  run: |
    # Check if env var is set (without revealing content)
    if [ -n "$NOTEBOOKLM_AUTH_JSON" ]; then
      echo "NOTEBOOKLM_AUTH_JSON is set (length: ${#NOTEBOOKLM_AUTH_JSON})"
    else
      echo "NOTEBOOKLM_AUTH_JSON is not set"
    fi
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
