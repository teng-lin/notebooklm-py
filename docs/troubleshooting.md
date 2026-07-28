# Troubleshooting

**Status:** Active
**Last Updated:** 2026-07-16

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

Google rotates `__Secure-1PSIDTS` (the freshness partner of `__Secure-1PSID`) on its own cadence; the on-disk `Expires` field is **not** a reliable predictor of server-side validity. The library handles freshness in layered fallbacks, ordered cheapest to heaviest:

1. **Per-call rotation poke** (default ON) — every `fetch_tokens` makes a best-effort POST to `accounts.google.com/RotateCookies`. Disable with `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`.
2. **Periodic background poke** — pass `keepalive=<seconds>` to `NotebookLMClient` for clients held open for hours.
3. **Layer-3 headless re-auth** — explicit Python opt-in via `await client.refresh_auth(allow_headless=True)`, or automatic mid-RPC opt-in with `NOTEBOOKLM_HEADLESS_REAUTH=1`. This drives the persisted browser profile, or attaches to a loopback Chrome DevTools endpoint from `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL`. Treat CDP as account-equivalent: only use `127.0.0.1` / `localhost`, never a remote browser.
4. **Layer-4 master-token re-mint** — when a `master_token.json` sits beside the profile's `storage_state.json` (the `[headless]` extra; `notebooklm login --master-token`), a fully-expired session re-mints fresh cookies from the durable master token in-process, after layers 1–3 are exhausted. This is what makes long-lived headless workers self-heal without a browser. See [installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci).
5. **External recovery script** — `NOTEBOOKLM_REFRESH_CMD` runs when auth has fully expired, then retries once.
6. **Manual re-login** — `notebooklm login` (or `notebooklm login --master-token-refresh` to force a master-token re-mint; cold-dead cookies at process start need this).
7. **External scheduler** — `notebooklm auth refresh` driven by cron / launchd / systemd / Task Scheduler / k8s CronJob, for idle profiles with no Python process running. Recommended cadence: 15–20 minutes.

> **Master-token troubleshooting:** `MasterTokenError: ... re-bootstrap` means the master token was revoked (password change / Google security action) — re-run `notebooklm login --master-token`. `... needs gpsoauth` means the `[headless]` extra isn't installed (`pip install "notebooklm-py[headless]"`). A minted jar "missing required cookies" indicates a MergeSession change — file an issue.
>
> **"This browser or app may not be secure" during `--master-token` sign-in:** Google blocks sign-in inside the automated browser the auto-capture launches. The client drops the obvious automation flags, but Google may still block — use one of the reliable paths instead:
> - **Attach to your own Chrome (recommended):** quit Chrome, relaunch it with `--remote-debugging-port=9222`, then `notebooklm login --master-token --account you@gmail.com --cdp-url http://127.0.0.1:9222`. It opens an EmbeddedSetup tab in your real (non-automated) browser, so Google allows sign-in, and scrapes the `oauth_token`.
> - **Capture the token manually:** in a normal browser sign in at `accounts.google.com/EmbeddedSetup`, copy the `oauth_token` cookie (DevTools → Application → Cookies → accounts.google.com), then `notebooklm login --master-token --account you@gmail.com --oauth-token <value>`. The `oauth_token` is single-use and short-lived — use it immediately.

Most users only need layer 1 — it's on by default and requires no configuration. For the full strategy (trade-offs between layers, including Python kwargs like `keepalive_min_interval` and environment variables like `NOTEBOOKLM_REFRESH_CMD_USE_SHELL`, and ready-to-paste launchd / systemd / cron / Task Scheduler / k8s CronJob recipes), see **[docs/auth-cookie-lifecycle.md#tldr](auth-cookie-lifecycle.md#tldr)** for a quick orientation, then [§4 The recovery ladder](auth-cookie-lifecycle.md#4--the-recovery-ladder) for the per-layer deep dive.

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

#### Windows: `Missing required cookies: __Secure-1PSIDTS` after login, and `--browser-cookies` "Could not decrypt"

On Windows, both credential paths can leave you without `__Secure-1PSIDTS` (the rotating freshness partner of `__Secure-1PSID` that every real RPC needs — see [Automatic Token Refresh](#automatic-token-refresh) above), so `notebooklm login` reports success but `notebooklm list` then fails with `Missing required cookies: __Secure-1PSIDTS` (issue [#1753](https://github.com/teng-lin/notebooklm-py/issues/1753)). Two distinct causes are in play:

- **`notebooklm login --browser chrome` (Playwright flow).** The interactive browser completes Google sign-in, but Google may serve an automation-detected session *without* the token-binding cookie (and sometimes without the secondary-binding cookies `OSID` / `APISID` + `SAPISID` the automatic `RotateCookies` recovery needs to re-mint it). When that happens the saved `storage_state.json` is genuinely incomplete and re-running the same flow reproduces it.
- **`notebooklm login --browser-cookies chrome` (or `edge`) → `Could not decrypt chrome cookies`.** Chrome 127+ (and current Edge) protect the cookie database with **App-Bound Encryption (ABE)**: the decryption key is bound to the browser process via a Windows service, so no external process can read it. This blocks every cookie-extraction library (`rookiepy`, `browser-cookie3`, `pycookiecheat`), not just `notebooklm-py`. There is no flag that bypasses ABE.

Note that `notebooklm doctor` may still say the auth check passed with an older client — the check historically only looked for `SID`. Current versions surface a **warn** row when `__Secure-1PSIDTS` is missing (`auth check --test` has always reported the real error). Trust `notebooklm auth check --test` / `notebooklm list` over a green `doctor` for "is this session actually usable".

Workarounds, most reliable first:

1. **Use Firefox as the cookie source.** Firefox stores cookies in a plain SQLite DB — **no App-Bound Encryption** — so extraction just works. Sign in to Google in Firefox, then:
   ```bash
   notebooklm login --browser-cookies firefox
   ```
   (If your Google session lives in a Multi-Account Containers tab, use the explicit `firefox::Container` / `firefox::none` syntax — see the [macOS section above](#macos---browser-cookies-prompts-for-your-password) for the container notes.) This is the simplest fix for the ABE case.

2. **Set up a master token (best for unattended / long-lived use).** `notebooklm login --master-token` (needs the `[headless]` extra: `pip install "notebooklm-py[headless]"`) stores a durable `master_token.json` beside your profile. When cookies are missing or fully expired, the client re-mints a complete, fresh cookie jar — including `__Secure-1PSIDTS` — from the master token in-process, so it does not depend on what the browser login happened to hand back. If Google blocks sign-in inside the automated capture window ("This browser or app may not be secure"), use the CDP-attach or manual `oauth_token` variants described in the [master-token troubleshooting note](#cookie-freshness-for-long-running--unattended-use) above. See also [installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci).

3. **Retry the Playwright login on a fresh profile.** Sometimes a stale persistent profile is the culprit rather than automation detection:
   ```bash
   notebooklm login --fresh
   ```
   If three attempts (normal, `--fresh` + password, `--fresh` + passkey) all reproduce the missing cookie, treat it as the automation-detection case and switch to Firefox or a master token above.

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

#### "NotebookLM redirected this request to its region / anti-abuse access gate"

**Cause:** The request to `notebooklm.google.com` was redirected to **`notebooklm.google/?location=unsupported`** — Google's region / anti-abuse risk-control gate (the marketing/landing page, which has no CSRF token). This is **not** a library bug, expired login, or page-structure change, and **re-running `notebooklm login` will not fix it** (the cookies are fine). It is driven by the *access environment*, not just the account's country, and fires even for accounts in supported regions when Google sees:

- a **VPN / proxy / datacenter / shared IP** (especially previously-abused ones),
- an **IP ↔ timezone ↔ browser-language mismatch**, or
- a **non-browser / automated access pattern** (a raw HTTP client without a real browser fingerprint).

**Confirm:** open `https://notebooklm.google.com` in a normal browser, signed in to the same account, on the same network. If it also redirects to `notebooklm.google/?location=unsupported`, the gate is environmental.

**Solution:** access from a **residential connection in a supported region**, keep your system **timezone/language consistent** with the IP's country, and avoid shared/datacenter VPN exit IPs. When the trigger is the **non-browser fingerprint** (a raw HTTP client) rather than the IP, the opt-in browser-TLS-impersonation transport can help: set `NOTEBOOKLM_TRANSPORT=curl_cffi` (requires the `curl_cffi` package) so requests carry a real browser's TLS fingerprint. (See issue [#1630](https://github.com/teng-lin/notebooklm-py/issues/1630).)

#### Browser opens but login fails

**Cause:** Google detecting automation and blocking login.

**Solution:**
1. Delete the browser profile: `rm -rf ~/.notebooklm/profiles/<profile>/browser_profile/` (or `~/.notebooklm/profiles/default/browser_profile/` for the default profile)
2. Run `notebooklm login` again
3. Complete any CAPTCHA or security challenges Google presents
4. Ensure you're using a real mouse/keyboard (not pasting credentials via script)

#### "Login not detected within 5 minutes" (especially on macOS)

**Cause:** The bundled Chromium that `notebooklm login` launches by default opened a fresh, signed-out browser, and its login-detection wait timed out — common on macOS where bundled Chromium can also be flaky (macOS 15+).

**Solution:** If you are already signed in to Google in **system Chrome**, retry with that browser so the existing session is reused instead of starting a fresh sign-in:

```bash
notebooklm login --browser chrome --storage <path>
```

`--browser chrome` drives your installed Google Chrome (with its signed-in profile), which usually detects the account immediately and sidesteps bundled-Chromium issues. `--browser msedge` is the equivalent for organizations that require Microsoft Edge for SSO.

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

#### "RPCError: [3]" (Invalid argument) / "UserDisplayableError"

**Cause:** Google's API rejected the request. Common cases:
- Invalid parameters or a not-found resource ID
- Account quota exceeded (for `create`, status `[3]` is also the notebook-limit signal)
- Rate limiting

**Solution:**
- Check that notebook/source IDs are valid
- Add delays between operations (see Rate Limiting section)

**If it only affects _write_ operations** (`create`, `source add`, `generate`) while reads (`list`, `ask`) keep working — **and the web UI still works** — the likely cause is that Google changed the request payload (wire format) for those RPCs and `notebooklm-py` is still sending the old shape. Google rolls these out gradually, so it can hit some accounts before others.

> **First, rule out a mis-decoded success.** Re-run the failing action, then check `notebooklm list`: if the resource was actually **created** despite the error, it's a *response*-decoding issue (share the **Response** below). If it was **not** created, Google rejected our **request** (share the **Payload** below).

**Help us fix it — share the web UI's payload (no cookies needed).** Either option below leaks nothing: cookies, the `at=` CSRF token, and `Set-Cookie` live in request/response *headers*, never inside the `f.req` payload.

> **⚠️ Never paste the raw `.har` itself.** A HAR contains your cookies, the `at=` CSRF token, and the full NotebookLM page HTML — which embeds API keys, the CSRF token, and your account email. Only ever share the **scrubber's output** below (or the single `f.req` line from Option B). The scrubber processes only `/batchexecute` calls and redacts every value, so the page HTML never reaches its output.

**Option A — thorough, auto-scrubbed (recommended; captures every RPC + its response).**

This walkthrough takes ~2 minutes. You never copy a cookie, a token, or the raw HAR — a small bundled script does the redaction for you.

1. **Open DevTools on the Network tab.** In Chrome/Edge press <kbd>F12</kbd> (or <kbd>Cmd</kbd>+<kbd>Opt</kbd>+<kbd>I</kbd> on macOS); in Firefox press <kbd>F12</kbd>. Click the **Network** tab at the top of the panel.
2. **Arm the capture.** Tick **Preserve log** (Chrome) / **Persist Logs** (Firefox) so a page reload doesn't wipe the capture, and confirm the round **● Record** button is red (it usually is by default).
3. **Reproduce the failure.** In the NotebookLM tab, perform the exact action that fails — e.g. create a notebook, or add a source. You'll see `batchexecute?rpcids=…` rows appear in the Network list. You can stop as soon as the action errors.
4. **Export the session to a file:**
   - **Chrome/Edge:** click the ⤓ **Export HAR…** download icon in the Network toolbar (or right-click any row → **Save all as HAR with content**).
   - **Firefox:** click the ⚙️ gear / **…** menu in the Network toolbar → **Save All As HAR**.

   Save it as `capture.har`. (The "with content" variant matters — it's what includes the *response* bodies the scrubber reports.)
5. **Scrub it.** From your `notebooklm-py` checkout, run the bundled script (stdlib-only — no install needed):

   ```console
   $ python scripts/scrub_rpc_har.py capture.har
   NotebookLM RPC capture — string values → <str:N>; cookies / headers / at= / Set-Cookie never read:

   CCqFvf  (CREATE_NOTEBOOK)
     request : ["<str:7>",null,null,[2],[1]]
     response: HTTP 200 | status_code=[3] | result=null

   1 call(s). Safe to share — no cookies / CSRF / session tokens are present (they live in headers, which this tool never reads).
   ```

   Narrow to a single RPC with `--rpcid` if the capture is noisy:

   ```console
   $ python scripts/scrub_rpc_har.py capture.har --rpcid CCqFvf
   ```

   The script reads **only** each request's `f.req` field and the response body — never the headers/cookies arrays, never the non-`batchexecute` page HTML — and replaces every text value with its length (`<str:7>`). It refuses to print if any raw string ever slips through, so the output is safe by construction.
6. **Paste that output** into the issue. Read it back first as a sanity check: every value should be `<str:N>`, never readable text.

   - `status_code=[3]` with `result=null` → Google rejected our **request** (a payload/wire-format change). This is what we need to fix it.
   - A non-null `result` → the call actually worked and this is a **response**-decode issue; share it just the same.

**Option B — quick, one RPC by hand (no script).** Use this if you can't run the script.

1. In DevTools → **Network**, click the `batchexecute?rpcids=…` POST for the failing call (e.g. `rpcids=CCqFvf` for create, `izAoDd` for add-source).
2. Open the **Payload** tab (Chrome) / **Request** tab (Firefox), copy **only** the `f.req` value — **not** the `at=` field beside it, and don't open the **Cookies**/**Headers** tabs.
3. Replace any free text (title, URL) with `REDACTED` and paste it. We diff it against what the library sends — `["<title>", null, null, [2], [1]]` for create — and update the payload.

### Generation Failures

#### Audio/Video generation is refused immediately

**Cause:** NotebookLM refused the generation kickoff synchronously (often quota,
feature availability, rate limiting, or an RPC shape drift). In v0.8.0 the
Python API raises instead of returning `None`.

**What to do:**
```bash
# Let the CLI surface the typed error envelope / message
notebooklm generate audio --wait --json

# If generation was accepted and you have a task id, poll manually
notebooklm artifact poll <task_id> --json
```

In Python, catch `RateLimitError`, `ArtifactFeatureUnavailableError`, or
`RPCError` depending on the failure. If kickoff succeeds and later polling
times out, use the timeout guidance below.

#### Audio/Video task times out as pending or in progress

**Cause:** NotebookLM accepted the generation task, but the upstream media queue
did not reach a terminal state before your wait budget. For media artifacts, the
SDK also keeps polling if NotebookLM marks the row completed before the media
URL is populated.

**Solution:**
- Increase the wait budget with `--timeout` or the Python
  `wait_for_completion(..., timeout=...)` argument.
- For `generate <kind> --wait`, the built-in media defaults are 1200s for
  audio, 1800s for standard video, and 3600s for cinematic video; pass a
  larger `--timeout` if your account's media queue is slower.
- `artifact wait` is intentionally generic and still defaults to 300s; when
  waiting manually on a media task ID, pass the matching media timeout.
- Catch `ArtifactPendingTimeoutError` to retry queued tasks separately from
  `ArtifactInProgressTimeoutError`, which means the task started but did not
  finish before the timeout.
- Log `exc.status_history` and `exc.status_transitions` for upstream queueing
  diagnostics instead of parsing the exception message.

#### Mind map or data table "generates" but doesn't appear

**Cause:** Generation may silently fail without error.

**Solution:**
- Wait 60 seconds and check `artifact list`
- Try regenerating with different/fewer sources

### File Upload Issues

#### HTML/XHTML files are rejected before upload

**Cause:** NotebookLM's file-upload endpoint rejects HTML-family uploads, even
though the web UI may accept pasted rich text.

**Solution:** Convert saved web pages to text, Markdown, or PDF before adding
them with your preferred extractor:

```bash
notebooklm source add ./article.txt
```

You can also pipe extracted text through stdin:

```bash
python extract_article_text.py ./article.html | notebooklm source add - --type text --title "Article"
```

#### Text/Markdown upload succeeds but processing/content is wrong

**Cause:** The upload was accepted, but NotebookLM processed unexpected content
or reported a source-processing error. Current `add_file()` returns a `Source`;
missing or untrusted source IDs raise `SourceAddError` instead of returning
`None`.

**Workaround:** When you control the text, bypass file-type inference and use
`add_text`:
```bash
# Instead of: notebooklm source add ./notes.txt
# Do:
notebooklm source add - --type text --title "My Notes" < ./notes.txt
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
- `RateLimitError` in Python, or CLI JSON with `code: "RATE_LIMITED"`
- `RPCError` with ID `R7cb6c`
- `UserDisplayableError` with code `[3]`

**Best Practices:**

**CLI:** Use `--retry` for automatic exponential backoff:
```bash
notebooklm generate audio --retry 3   # Retry up to 3 times on rate limit
notebooklm generate video --retry 5   # Works with most generate commands
```

*Note: `generate mind-map` is synchronous and does not accept the `--retry` option. All other `generate` subcommands support `--retry`.*

**Python:**
```python
import asyncio
from notebooklm import RPCError
from notebooklm.artifacts import with_rate_limit_retry

# Add delays between intensive operations
for url in urls:
    await client.sources.add_url(nb_id, url)
    await asyncio.sleep(2)  # 2 second delay

# Use the shared generation retry policy when starting artifacts
status = await with_rate_limit_retry(
    lambda: client.artifacts.generate_audio(nb_id),
    max_retries=3,
)

# For non-artifact RPC calls, retry by passing a fresh callable each attempt
async def retry_rpc_call(make_call, max_retries=3):
    for attempt in range(max_retries + 1):
        try:
            return await make_call()
        except RPCError:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(2**attempt)

notebook = await retry_rpc_call(lambda: client.notebooks.create("Research Notes"))
```

### Starting a brand-new conversation (resolves the older issue #659 workaround)

`client.chat.ask(notebook_id, question)` with `conversation_id=None`
attaches the question to the user's **current** conversation on the
notebook — by design. The SDK still fetches the server-recorded
conversation_id via `hPTbtc` after the ask and returns it on
`AskResult.conversation_id`, so follow-ups using that id work
correctly.

To force a brand-new server-side conversation, delete the existing
one first — this mirrors the web UI's "Delete history" button:

```python
last_conv_id = await client.chat.get_conversation_id(nb_id)
if last_conv_id:
    await client.chat.delete_conversation(nb_id, last_conv_id)
result = await client.chat.ask(nb_id, "Start fresh")
```

Or via the CLI (prompts for confirmation; `-y` skips):

```bash
notebooklm ask --new -y "Start fresh"
```

**This is destructive: deleted turns are not recoverable.** The CLI
shows the conversation's short id in the prompt and defaults to "No".
`--json` implies `--yes` so scripted callers don't hang on stdin.

**History:** Before the SDK gained `delete_conversation` it had no way
to honor the "fresh conversation" intent — both the SDK and the CLI's
`--new` flag would silently extend the most-recent conversation, so
users worked around it by creating a new notebook for each thread.
The `J7Gthc` RPC was reverse-engineered from the web UI's "Delete
history" button and removes the need for that workaround.

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

## Adapter-specific issues (MCP server, REST server)

The MCP and REST servers have their own setup and failure modes documented with
the features:

- **MCP server / remote connector** (stdio & HTTP transports, self-hosted OAuth,
  Cloudflare / Tailscale tunnels): see [mcp-guide.md#troubleshooting](mcp-guide.md#troubleshooting)
  and [deploy/README.md](../deploy/README.md).
- **REST server** (`notebooklm-server`): a non-loopback bind refuses to start
  without `NOTEBOOKLM_SERVER_TOKEN`; every `/v1` request needs the bearer (401
  otherwise). See [installation.md#rest-api-server](installation.md#rest-api-server).
- **`curl_cffi` transport**: `NOTEBOOKLM_TRANSPORT=curl_cffi` requires the
  `curl_cffi` package; see the region / anti-abuse gate section above.

### "Unknown tool" from an MCP host (claude.ai, ChatGPT, …) after upgrading the server

**Cause:** The MCP host fetches the server's tool list (`tools/list`) when it first
connects and **caches it against the connector**. After you upgrade a deployed
server to a version that **renamed or folded** a tool, the host keeps advertising
the *old* manifest, so a call to a since-removed tool reaches the server and fails
cleanly with `Unknown tool: '<name>'`. This is expected MCP-host caching, **not a
server bug** — the server's live manifest is correct (you can confirm with
`await Client(create_server()).list_tools()`). A server can't notify a host that
isn't connected, and `notifications/tools/list_changed` only reaches a *live*
session.

**Solution: remove and re-add the connector** in the host's connector settings.
A *reconnect* / re-auth / off-and-on toggle is often **not enough** — several
hosts (notably ChatGPT) re-authenticate but keep the cached tool list and do not
re-call `tools/list`, so the ghost tool persists across reconnects. Fully deleting
the connector and adding it again forces a fresh `tools/list`. (Newly-added
*optional parameters* on existing tools forward through the stale schema and keep
working without any refresh — only *removed/renamed tools* ghost.)

**Only removed or renamed _tools_ ghost this way — new _optional_ parameters
forward through the stale schema.** For example, when `source_upload_bytes` was folded
into `source_add(bytes_base64=…)` (and `studio_get_prompt` into
`studio_list(item=…)`), the removed tool names failed with `Unknown tool`, but
`source_add` with the new `bytes_base64` argument reached the upgraded server and
worked without a reconnect.

## Getting Help

1. Check this troubleshooting guide
2. Search [existing issues](https://github.com/teng-lin/notebooklm-py/issues)
3. Open a new issue with:
   - Command/code that failed
   - Full error message
   - Python version (`python --version`)
   - Library version (`notebooklm --version`)
   - Operating system
