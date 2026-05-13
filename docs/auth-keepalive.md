# Auth keepalive — design notes and field findings

> **Status:** design notes for the L1/L2/L3 keepalive code already in `main`
> (L1 `RotateCookies` POST + 60 s mtime guard merged via
> [#346](https://github.com/teng-lin/notebooklm-py/pull/346); concurrent-poke
> throttling via [#348](https://github.com/teng-lin/notebooklm-py/pull/348);
> L2 background task via
> [#341](https://github.com/teng-lin/notebooklm-py/pull/341); L3
> `notebooklm auth refresh` CLI shipped) plus the L5/L6 escalation paths that
> are still proposed. Reflects empirical observations from a multi-hour A/B/C
> field experiment in May 2026 and cross-project review of two ecosystem peers
> ([HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) and
> [easychen/CookieCloud](https://github.com/easychen/CookieCloud)). Update as
> the threat model evolves; flag stale claims with `<!-- stale: <date> -->`.

## TL;DR

NotebookLM has no public OAuth surface. The library authenticates by carrying
Google session cookies (`SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `OSID`,
and friends) extracted from a real browser sign-in. Two clocks govern how long
those cookies stay valid:

- **`__Secure-1PSIDTS` has a *recommended* rotation cadence of ~600 s** (self-reported by Google as `["identity.hfcr",600]` on the `RotateCookies` response), but the **prior value remains valid for far longer than 600 s**. Empirical observation on a stable IP, non-Workspace account: a frozen `__Secure-1PSIDTS` continued authenticating for **32+ hours** without any client-side rotation, and Google naturally rotated it only **once** in 29 hours of continuous probing. The "10-minute server-side TTL" framing earlier in this project's history was too strong; 600 s is what active clients are *expected* to do, not what gets enforced. Worst-case profiles (datacenter egress, cross-IP, Workspace policy, incomplete extraction) can collapse this to hours or less.
- **`SID` and `__Secure-1PSID`** have very long server-side lifetimes (months
  to years for daily-active accounts) and effectively don't expire under
  normal usage as long as Google sees periodic activity.
- **Cookie set completeness matters more than freshness.** Pair-wise ablation showed Google rejects any cookie set where `__Secure-1PSIDTS` is missing along with any one other cookie, even though removing `__Secure-1PSIDTS` alone is recoverable. See §3.5 for the full accept-rule model.

A long-lived client must therefore drive `*PSIDTS` rotation itself. Empirically
the cleanest mechanism is a direct `POST` to
`https://accounts.google.com/RotateCookies` — Google's dedicated unsigned
rotation endpoint. This is the L1 primitive at the bottom of a tiered
recovery design that escalates progressively as failure modes get harder.

The headline tradeoffs:

| Layer | Mechanism | Cost | Survives DBSC? | Ship status |
|---|---|---|---|---|
| **L1** | Per-call `RotateCookies` POST + triple-guard throttle | ~150 ms / call (skipped if recently rotated) | No, but DBSC isn't enforced on this path today | Merged ([#346](https://github.com/teng-lin/notebooklm-py/pull/346) + [#348](https://github.com/teng-lin/notebooklm-py/pull/348)) |
| **L2** | Background `keepalive=N` task | One POST every N s | Same as L1 | Merged ([#341](https://github.com/teng-lin/notebooklm-py/pull/341), races closed in [#342](https://github.com/teng-lin/notebooklm-py/pull/342)–[#344](https://github.com/teng-lin/notebooklm-py/pull/344)) |
| **L3** | OS-scheduled `notebooklm auth refresh` | One POST per cron tick | Same as L1 | Merged (`auth refresh` subcommand) |
| **L4** | `--browser-cookies <browser>` re-extract via rookiepy | One sqlite read + L1 POST | Yes (non-Chrome browsers not DBSC-enrolled) | Already supported (~16 browsers; Firefox is the recommended Windows path) |
| **L5** | CDP-attach to user's running Chrome | Higher; needs Chrome on `:9222` | **Yes** (inherits Chrome's TPM-bound key) | Proposed; deferred until L1 weakens |
| **L6** | CookieCloud client (browser extension + self-hosted server) | User infra; richest UX | **Yes** | Optional follow-up |

A separate, complementary refresh hook also lives in the codebase:
``NOTEBOOKLM_REFRESH_CMD`` ([#336](https://github.com/teng-lin/notebooklm-py/pull/336))
runs an arbitrary user-supplied shell command on auth-expiry signals (the
"`Authentication expired`" redirect), then retries token fetch once. It's
orthogonal to L1–L3 — those proactively keep `*PSIDTS` fresh, while
`NOTEBOOKLM_REFRESH_CMD` is the reactive "we lost the session anyway, run
my recovery script" lever. See §9 below.

L1 is empirically working today on every account type tested. L4 is the
recommended unattended path for `notebooklm-py` users in May 2026. L5 is
specified but not implemented; it's the durability insurance for the day
Google extends DBSC enforcement to non-Chrome cookie paths.

---

## 1 · Problem statement

NotebookLM uses Google's internal `batchexecute` RPC. There is no documented
API key, no OAuth scope, no service account path. Every project that
automates NotebookLM does so with **scraped session cookies** from a logged-in
browser. The library exposes those via `notebooklm login` (Playwright-driven
Google sign-in into a private Chromium profile) and
`notebooklm login --browser-cookies <browser>` (rookiepy-driven extraction
from an existing Chrome/Firefox/Edge profile).

Both produce a `storage_state.json` file with the cookie set the library uses
to authenticate every subsequent RPC. The keepalive question is: **what
keeps `storage_state.json` valid as time passes between user-driven
re-authentications?**

The naïve answer ("cookies have expiry timestamps; trust them") is wrong on
two counts:

1. The most consequential auth cookie (`__Secure-1PSIDTS`) has a **server-side
   recommended rotation cadence of ~600 s** (Google's own self-report) that's
   not encoded in the cookie's `Expires` attribute. The on-disk `Expires` field
   is irrelevant to its server-side validity. The *recommended* cadence is
   distinct from the *actual* validity window — empirically the prior value
   keeps working for hours-to-days on stable network identities. See §3.5 for
   ablation data.
2. Even cookies with a year-long `Expires` will be **revoked early by Google's
   risk model** if the access pattern looks unusual (no JS execution, no
   browser fingerprint, IP changes, long inactivity gaps).

So the library must actively refresh.

---

## 2 · Background: Google session auth, rotation, and DBSC

This section establishes the vocabulary the rest of the doc uses. Skip
ahead to §3 if you've already spent time inside Google's identity surface.

### 2.1 The cookie taxonomy

Google authenticates a browser session with a **family of ~15 cookies**,
not a single bearer token. Each cookie has a distinct role; the family
is designed so revoking or rotating any one slot doesn't invalidate the
others. The cookie set is shared across `*.google.com` properties —
Search, Drive, Gmail, NotebookLM, YouTube, Workspace — which is why a
sign-in to any one of them produces auth artifacts the rest of the
ecosystem will accept.

Naming conventions:

- **`__Secure-` prefix.** A browser-enforced rule: the cookie's `Secure`
  attribute must be set, so it's never transmitted over plaintext HTTP.
  Google sets this on every meaningful auth cookie.
- **`__Host-` prefix.** Stricter than `__Secure-`. The cookie must also
  set `Path=/`, must not set `Domain=` (so it's pinned to the exact
  origin that issued it), and must be `Secure`. Used for the most
  scope-sensitive cookies (`__Host-GAPS`, `__Host-1PLSID`, …).
- **`1P` vs `3P`.** First-party vs third-party context. `__Secure-1PSID`
  is the SID Google uses when the request originates from a
  `*.google.com` page; `__Secure-3PSID` is the variant Google sends on
  third-party pages that embed Google content (sign-in widgets, fonts
  referers, …). They rotate independently and have slightly different
  scopes. We typically need both because intermediate redirects during
  rotation cross the 1P/3P boundary.
- **`*SID` / `*SIDTS` / `*SIDCC`.** Three different cookie *families*,
  not variants of one cookie. They cooperate to separate **identity** —
  who you are, slow to change — from **freshness** — you're using the
  session right now, fast to expire:

  | Family | Role | Recommended rotation cadence | Empirical validity of a stale value |
  |---|---|---|---|
  | `*SID` (also `HSID`, `SSID`, `APISID`, `SAPISID`, …) | Long-lived identity ("user X, session Y") | Months → ~1 year | Same — practically never expires for active accounts |
  | `*SIDTS` (`__Secure-1PSIDTS`, `__Secure-3PSIDTS`) | Rotating freshness partner of `*SID` | **~600 s** (Google's self-report) | **Hours-to-days** on stable IP / non-Workspace (measured: 32+ h frozen still authenticating) |
  | `*SIDCC` (`SIDCC`, `__Secure-1PSIDCC`, `__Secure-3PSIDCC`) | Per-request "session continuity check" | Issued on every request | Not enforced for accept/reject — Google reissues but doesn't validate freshness |

A few cookies sit outside this taxonomy:

- **`OSID`, `__Secure-OSID`** — per-product session, set on
  `notebooklm.google.com` and `myaccount.google.com`. Re-issued on each
  sign-in; refreshes during normal product use.
- **`LSID`, `__Host-1PLSID`, `__Host-3PLSID`** — identity-service
  cookies on `accounts.google.com` itself. Long-lived.
- **`__Host-GAPS`** — anti-takeover binding cookie. Long-lived; presence
  is part of how Google detects suspicious cross-device session reuse.

The library treats all of these uniformly: extract the full set at
sign-in, persist them in `storage_state.json`, replay them on every
RPC. `_is_allowed_cookie_domain` (in `auth.py`) is the gate that decides
which Set-Cookie headers from a redirect chain are worth keeping; it
matches against `ALLOWED_COOKIE_DOMAINS` plus the regional
`google.<cctld>` set.

### 2.2 How cookie rotation works

"Rotation" here means: the server periodically issues a new value for a
short-lived cookie (`Set-Cookie: __Secure-1PSIDTS=<fresh>; …`), and the
browser is expected to overwrite its on-disk copy. If the browser falls
behind, the server eventually stops accepting the old value and the
session is dead until the user signs in again.

Two clocks run in parallel:

- The **identity clock** (`*SID`) ticks in months. Google extends it
  silently as long as it sees activity; for a daily-active user it
  effectively never expires.
- The **freshness clock** (`*PSIDTS`) ticks in **~10 minute** intervals.
  The server self-reports the cadence in the `RotateCookies` response
  body as `["identity.hfcr",600]` (`hfcr` = "high-frequency cookie
  rotation"; `600` = seconds). Every active browser must hit an
  identity surface roughly that often, or `*PSIDTS` ages out and every
  subsequent RPC fails with a redirect to
  `accounts.google.com/v3/signin/...`.

Server-driven, not client-driven: the client posts to a rotation
endpoint, the server inspects the existing `*SID` (and optionally a
DBSC proof — see §2.3), and if everything checks out it returns a fresh
`*PSIDTS` in `Set-Cookie`. The client only chooses *when* to fire the
rotation; the cadence is the server's call.

> **Has Google shortened the 600 s cadence?** As of May 2026, no public
> evidence suggests so. Gemini-API still defaults
> `refresh_interval=600` ([source](https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/utils/rotate_1psidts.py)),
> the `["identity.hfcr",600]` self-report is unchanged in field
> captures, and recent
> [Gemini-API#319](https://github.com/HanaokaYuzu/Gemini-API/issues/319) /
> [#203](https://github.com/HanaokaYuzu/Gemini-API/issues/203) reports
> attribute "cookies expire after a few hours" to refresh-mechanism
> failure (SID-class aging out once freshness rotation has stalled
> entirely), not to a server-side TTL reduction.

**Crucially: pure RPC traffic against `notebooklm.google.com` does not
trigger rotation.** NotebookLM's `batchexecute` endpoint accepts the
existing cookies and serves the request, but Google only mints a fresh
`*PSIDTS` when something talks to the *identity* surface
(`accounts.google.com`, `accounts.youtube.com/SetSID`, the NotebookLM
homepage GET). A long-lived client that only calls `batchexecute` will
silently drift past the rotation window and start failing. This is
exactly the failure mode that motivates L1/L2/L3.

Several identity surfaces *can* trigger rotation when touched:
`accounts.google.com/CheckCookie`, `accounts.youtube.com/SetSID`, the
NotebookLM homepage redirect chain, and the dedicated `RotateCookies`
POST. We picked `RotateCookies` because it's the only one that rotates
deterministically for both browser-bound and Firefox-extracted sessions
(see §5.4).

### 2.3 Device-Bound Session Credentials (DBSC)

DBSC is Google's response to **infostealer cookie theft**: malware
exfiltrates the cookie jar from a victim's machine, ships it to a
remote attacker, who then replays the cookies from a different machine
and inherits the victim's session. Until DBSC, the only practical
defenses were Google's risk heuristics (new IP, no fingerprint,
suspicious cadence) — useful but fundamentally guess-work.

DBSC binds a session to **a private key that lives in tamper-resistant
hardware** on the original device. The shape of the protocol:

1. **At sign-in**, the browser generates a keypair *inside* a TPM (on
   Windows) or the platform-attestation chain equivalent (Secure
   Enclave on macOS, Strongbox on Android). The private key is
   non-extractable by design — the OS will only sign things with it on
   behalf of the calling process.
2. The browser **registers the public key** with Google as part of the
   sign-in flow. Google associates the public key with the new session.
3. On every subsequent rotation, Google issues a **server-generated
   nonce**. The browser **signs the nonce** with the TPM-bound private
   key and sends the signature alongside the rotation request.
4. Google validates the signature against the registered public key
   before issuing fresh cookies. No valid signature → no rotation.

The endpoint that enforces this is
**`accounts.google.com/RotateBoundCookies`** — the bound-cookie analog
of the unsigned `RotateCookies` we currently use. It returns rotated
cookies only if the signature checks out.

The protective property: an attacker who exfiltrates the cookie jar
gets nothing time-limited. Within ~10 minutes the freshness cookie ages
out, the attacker can't sign the next rotation, and the stolen session
is dead.

The [W3C DBSC spec](https://w3c.github.io/webappsec-dbsc/) is
**deliberately structured** so that only browsers with hardware key
attestation can implement it. There's no extension point a Python HTTP
client could fulfill: even with TPM access (which Python doesn't have
on any platform out of the box), Chrome additionally proves *integrity
of the calling process* via platform attestation chains. This is why
§7.4 calls a client-side DBSC implementation impossible.

The current rollout (April 2026, Chrome 146 GA Windows) only enforces
DBSC against **Chrome itself** — i.e. Chrome refuses to use cookies
that weren't bound at sign-in, even on the same machine. Non-Chrome
HTTP clients (httpx, curl, Firefox) can still hit the legacy unsigned
`RotateCookies` endpoint without a DBSC proof. The day Google extends
enforcement to that endpoint, every L1–L3 strategy in this document
breaks at the same time, and the only escape is to parasitize a real
DBSC-enrolled Chrome session (L5 / L6).

### 2.4 How browser cookie extraction works (the L4 dependency)

L4 (`notebooklm login --browser-cookies <browser>`) reads cookies
directly out of an installed browser's profile rather than minting
fresh ones via Playwright. Faster, doesn't require user interaction,
and — for Firefox — produces a cookie set the unsigned `RotateCookies`
endpoint accepts indefinitely. Some background on why this is harder
than it sounds:

- **Browsers store cookies in encrypted SQLite databases.** Chrome
  keeps them in
  `~/Library/Application Support/Google/Chrome/Default/Network/Cookies`
  (macOS) and equivalents on other OSes; Firefox uses `cookies.sqlite`.
  The schema is straightforward, but cookie *values* are encrypted at
  rest.
- **The encryption key lives in the OS credential store.** Chrome's
  cookie key is held in Keychain under "Chrome Safe Storage" on macOS,
  protected by DPAPI on Windows, and stored via libsecret/kwallet on
  Linux. Reading cookies = reading the key from the OS store +
  decrypting with AES-GCM.
- **Chrome 127+ adds App-Bound Encryption (ABE).** A second layer where
  the *value* is re-encrypted with a key bound to Chrome's signed
  binary, rotated at every Chrome launch. This was added specifically
  to defeat infostealers reading the SQLite + keychain in user space.
  Reading ABE-encrypted cookies requires either (a) running as the
  same signed binary, or (b) a Windows-admin / kernel-level bypass.
- **`browser_cookie3` (the ecosystem default) does not handle ABE.**
  As of May 2026, it returns garbage for Chrome cookies on Windows and
  silently-incomplete data on macOS.
- **`rookiepy` claims ABE support** but in practice requires admin
  privileges from Chrome 130+ on Windows
  ([rookie#50](https://github.com/thewh1teagle/rookie/issues/50)).
- **Firefox doesn't have ABE.** Mozilla's threat model treats local
  attackers (anything reading the user's home dir) as out-of-scope, so
  Firefox cookies remain readable by any user-space process with file
  access. This is what makes Firefox the recommended unattended option
  in §8.3.

The library uses `rookiepy` (Rust extension with a Python binding)
rather than implementing extraction itself. `rookiepy` covers ~16
browsers across all three platforms; `_ROOKIEPY_BROWSER_ALIASES` in
`cli/session.py` maps user-facing names (`firefox`, `arc`, `vivaldi`,
…) to its functions, and `convert_rookiepy_cookies_to_storage_state`
in `auth.py` reshapes the result into a Playwright-compatible
`storage_state.json`. From the rest of the codebase's perspective,
browser-extracted cookies are indistinguishable from Playwright-minted
ones.

A note on cookie-jar fidelity: Google's set spans multiple domains
(`.google.com`, `.accounts.google.com`, regional ccTLDs like
`.google.co.uk`, plus `.notebooklm.google.com`). When extracting we ask
for all of them — `_login_with_browser_cookies` builds the `domains`
list from `ALLOWED_COOKIE_DOMAINS + GOOGLE_REGIONAL_CCTLDS` — because
dropping any one silently breaks specific code paths (e.g. losing
`.notebooklm.google.com`-scoped cookies breaks artifact downloads).

#### Firefox Multi-Account Containers

`rookiepy` 0.5.6 issues `SELECT host, path, isSecure, expiry, name,
value, isHttpOnly, sameSite FROM moz_cookies` with no filter on the
`originAttributes` column ([investigation in #366](https://github.com/teng-lin/notebooklm-py/issues/366)).
Firefox stores per-container cookies with `originAttributes =
'^userContextId=N…'`, so cookies from every Multi-Account Container
(plus the no-container default) get merged into a single jar. The
`moz_cookies` UNIQUE constraint is `(name, host, path, originAttributes)`,
so duplicate `(host, name, path)` rows across containers really exist;
which one wins after merging is arbitrary. For users who isolate their
Google session in a container (a common privacy practice), unscoped
`--browser-cookies firefox` silently produces an inconsistent or wrong
session.

To target a specific container, use the `firefox::<container-name>`
syntax (ported from yt-dlp's [container-aware extractor](https://github.com/yt-dlp/yt-dlp/blob/c8695f52a91f0d2aabbba7b7200c1099bfa9a3e5/yt_dlp/cookies.py#L149-L177)):

```bash
# Read cookies only from the named container:
notebooklm login --browser-cookies 'firefox::Work'

# Read cookies only from the no-container default:
notebooklm login --browser-cookies 'firefox::none'

# Unscoped (back-compat): merges every container. Emits a yellow warning
# if the profile is actually using containers.
notebooklm login --browser-cookies firefox
```

Container names match against `containers.json` adjacent to
`cookies.sqlite`. Both user-defined `name` fields and built-in
`l10nID`-derived labels are recognised (e.g. `firefox::Personal`
matches the stock `userContextPersonal.label`). The extractor bypasses
`rookiepy` entirely and talks to `cookies.sqlite` directly via
`sqlite3` (the DB is copied to a temp dir first, so a running Firefox
doesn't lock us out). See `src/notebooklm/_firefox_containers.py` for
the implementation.

### 2.5 Three timers people confuse

When reading code or issue threads, distinguish:

| Timer | Magnitude | Lives in | Meaning |
|---|---|---|---|
| **`*PSIDTS` server-side TTL** | ~600 s (10 min) | Google's identity surface | After this, Google rejects the cookie value. Self-reported as `["identity.hfcr",600]`. |
| **`*SIDCC` sliding window** | ~5 min | Google's RPC surface | Different cookie family. Rotates on nearly every request; not load-bearing for our auth. |
| **Client-side rotation throttle** | 60 s | Our `auth.py` and Gemini-API's `rotate_1psidts.py` | Don't fire two `RotateCookies` POSTs within a minute. Avoids 429. Has nothing to do with how often Google *requires* rotation. |

Reports that "cookies are expiring faster" usually trace to either the
session entering a risk-flagged state (§3.2) or to the rotation
mechanism failing for hours and `*SID` finally aging out — not to a
shorter server-side TTL.

---

## 3 · Threat model

### 3.1 Cookie classes and their decay clocks

| Cookie | Server-side TTL | Lifecycle |
|---|---|---|
| `__Secure-1PSIDTS` (and `*-3PSIDTS`) | ~10 min, declared by Google in `RotateCookies` response body as `[["identity.hfcr",600],...]` | Designed to be rotated frequently; the canonical "rotating freshness partner" of `*PSID` |
| `SIDCC`, `__Secure-1PSIDCC`, `__Secure-3PSIDCC` | ~5 min sliding window | Rotates on nearly every request to Google; ephemeral, generally not load-bearing for auth |
| `SID`, `HSID`, `SSID`, `APISID`, `SAPISID` | Months to ~1 year (issued `Max-Age`) | Long-lived identity; rotated by Chrome periodically through normal browsing but not by us |
| `__Secure-1PSID`, `__Secure-3PSID`, `__Secure-1PAPISID`, `__Secure-3PAPISID` | Same as above, "Secure" cousins | Same lifecycle |
| `OSID`, `__Secure-OSID` | Per-product session cookie set on `notebooklm.google.com` and `myaccount.google.com` | Re-issued on each sign-in; refreshes during normal product use |
| `LSID`, `__Host-1PLSID`, `__Host-3PLSID` | Long-lived | Identity service cookies on `accounts.google.com` |
| `__Host-GAPS` | Long-lived | Anti-takeover binding cookie |

### 3.2 What kills a session in practice

In rough order of likelihood:

1. **`*PSIDTS` rotation drift.** Cookies on disk become stale because nothing
   rotates them. Any RPC after the ~10–30 min grace period fails with a
   redirect to `accounts.google.com/v3/signin/...`. **This is the dominant
   failure mode for unattended use.**
2. **Risk-scored revalidation.** Google flags the access pattern (new IP,
   no fingerprint, suspicious cadence, geography mismatch) and forces full
   re-auth. Less predictable; happens days-to-weeks into a long-running
   deployment.
3. **Password change or manual sign-out** anywhere — invalidates all
   sessions instantly.
4. **Workspace policy timeouts.** Some org admins enforce 8h/30d re-auth
   intervals; varies by tenant.
5. **DBSC enforcement (emerging).** Google is rolling out Device-Bound
   Session Credentials. As of the GA on Chrome 146 Windows (April 9, 2026),
   *Chrome* clients without a TPM-signed proof can't refresh `*PSIDTS`.
   Currently does not affect non-Chrome HTTP clients (us); the legacy
   unsigned `RotateCookies` path remains open. This is **the long-term
   threat**.

### 3.3 The DBSC timeline (as of May 2026)

- **Apr 9, 2026:** Chrome 146 GA on Windows includes consumer-account DBSC
  enforcement against Chrome clients ([blog.google
  security](https://blog.google/security/protecting-cookies-with-device-bound-session-credentials/),
  [Chrome dev blog](https://developer.chrome.com/blog/dbsc-windows-announcement)).
  ~85% of active Windows Chrome installs are TPM 2.0 capable, per Google's
  own telemetry.
- **macOS:** "Upcoming Chrome release," no firm date.
- **Linux:** Explicitly deferred. No timeline.
- **Workspace:** Session-binding policy is admin-opt-in beta
  ([Workspace admin docs](https://knowledge.workspace.google.com/admin/security/prevent-cookie-theft-with-session-binding)),
  not enforced by default.
- **Non-Chrome HTTP clients (us):** Not currently enforced. The unsigned
  `RotateCookies` endpoint accepts our POSTs without DBSC challenge.

`RotateBoundCookies` (the DBSC analog of `RotateCookies`) requires a
TPM-bound private key registered with Google at sign-in. The
[W3C DBSC spec](https://w3c.github.io/webappsec-dbsc/) is
deliberately structured to prevent non-browser implementation. **There is no
public OSS DBSC client outside Chrome itself, and there cannot be one
without TPM access.**

### 3.4 Internal threats: cookie-jar fidelity in the persistence pipeline

A separate failure mode that's easy to misattribute to Google: the
library can corrupt its own cookie state during the read-merge-write
cycle. **If users report cookies "expiring fast" or "dying after a few
hours", before assuming Google has changed something, walk this section
first.** None of these are theoretical — they come straight from
reading `auth.py` against the lifecycle of `NotebookLMClient` /
`fetch_tokens_with_domains` / `save_cookies_to_storage`.

#### 3.4.1 Stale in-memory clobbers fresh disk (the "few-hours" pattern)

> **Resolved in #361.** ``ClientCore`` now captures an open-time
> ``CookieSnapshotKey -> CookieSnapshotValue`` snapshot of its jar; ``save_cookies_to_storage``
> accepts an ``original_snapshot=...`` kwarg and, when provided, writes only
> the deltas (cookies whose persisted tuple differs from the snapshot) plus deletions
> (cookies present in the snapshot but absent from the jar) — both arms
> CAS-guarded against the current on-disk cookie value so a sibling-process
> value write on the same key is never clobbered. Cookies the in-process code never
> touched are left to whatever a sibling process may have written, so the
> stale-overwrite-fresh race below cannot fire. The
> ``original_snapshot=None`` form remains as a public-API back-compat shim
> but emits a ``DeprecationWarning``; every in-tree caller passes a
> snapshot. See ``tests/unit/test_auth_cookie_save_race.py`` for the
> canonical timeline test plus value-update CAS and refresh-cmd
> re-snapshot coverage.

The original failure timeline (historical — the resolution box above
describes the in-tree fix):

| t | Process A (long-lived, `keepalive=None`) | Process B (CLI invocation) | Disk state |
|---|---|---|---|
| 0 | `from_storage()` → reads `*PSIDTS=OLD` | — | `OLD` |
| +5 m | working (batchexecute traffic only; never touches identity surface) | `from_storage()` rotates → `*PSIDTS=NEW` → saves under flock | `NEW` |
| +10 m | `close()` → save runs under flock → reads disk (`NEW`) → A's in-memory (`OLD`) differs → **A writes `OLD`** (pre-#361 only) | done | **`OLD` (clobbered)** |
| +60 m+ | next request to `notebooklm.google.com` fails — rotation never effectively landed | | |

The cross-process flock added in
[#344](https://github.com/teng-lin/notebooklm-py/pull/344) prevents
interleaved writes but not stale-overwrites-fresh. #361 added the
snapshot/delta machinery on top to close the remaining gap.

**Defensive comparison across the ecosystem.** This codebase is, as far
as a survey can establish, the *most defensive* OSS implementation:

| Project | Atomic temp-replace | Flock | Per-cookie merge | Stale-overwrite-fresh |
|---|---|---|---|---|
| `notebooklm-py` (us) | ✅ | ✅ (post-#344) | ✅ path-aware snapshot/delta CAS (post-#361) | ✅ closed |
| HanaokaYuzu/Gemini-API | ❌ | ❌ | ❌ (full-jar overwrite) | ❌ |
| yt-dlp ([cookies.py#L1333-L1352](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/cookies.py#L1333-L1352)) | ❌ (`f.truncate(0)` then write) | ❌ | ❌ (full-jar overwrite) | ❌ |
| Bard-API, ytmusicapi, gpsoauth, browser_cookie3, rookiepy | n/a (read-only) | n/a | n/a | n/a |
| easychen/CookieCloud | ❌ | ❌ | ❌ | ❌ (by design) |

yt-dlp's design is read-mostly — cookies extracted fresh from the
browser per invocation, no long-lived process mutating shared state —
so it gets away with full-overwrite-no-flock-no-temp-replace. Our
threat model (long-lived clients + cron-driven `auth refresh` +
parallel CLI invocations all writing the same `storage_state.json`)
genuinely needs the defenses we have. The peer-ecosystem state of the
art is "last writer wins, hope for the best."

Fix shipped in #361 (write-only-deltas + dirty-flag against open-time
snapshot, with value-CAS guards against the live on-disk value on both
write and deletion). Attribute-only refreshes are still detected and
persisted as deltas, but attribute-only sibling drift does not block later
value rotations; the stale-overwrite hazard is about cookie values. The
two alternatives considered and rejected:

- **Generation counter** stamped on every cookie write — would require
  every external writer to opt in to the new format and breaks
  compatibility with Playwright's `storage_state.json` schema.
- **Full bidirectional sync** — overkill for a session-token store;
  the snapshot/delta CAS shape converges to the same correctness without
  a schema change.

Mitigations available today (still useful even with the fix in place):

- Pass `keepalive=N` to long-lived `NotebookLMClient` instances so
  rotation actually fires in-process (in-memory stays fresh, save is
  always correct).
- Or, run a single rotator (cron-driven `notebooklm auth refresh`) and
  ensure no parallel long-lived processes write to the same
  `storage_state.json`.

#### 3.4.2 The `(name, domain)` collapse — `path` ignored

> **Resolved in #361 (side effect).** The snapshot/delta path now uses a
> path-aware ``CookieSnapshotKey(name, domain, path)`` NamedTuple, and
> ``build_httpx_cookies_from_storage`` loads all path variants into the live
> jar. Two storage entries with the same ``(name, domain)`` but different paths
> are distinct keys and survive a load → save round trip in the live jar/save
> path. The legacy ``(name, domain)`` maps listed below (``AuthTokens.cookies``,
> ``AuthTokens.cookie_header``, ``_cookie_map_from_jar``,
> ``extract_cookies_with_domains``, etc.) are intentionally still lossy because
> their public return type cannot represent path; they are compatibility
> surfaces, not the persistence-merge hot path that fired §3.4.1.

Multiple paths in `auth.py` key cookies by `(name, domain)` and drop
`path`:

| Site | Effect |
|---|---|
| `extract_cookies_with_domains` (`auth.py:786`) | Two storage_state entries with same name+domain but different paths → first one wins, second silently dropped |
| `_cookie_map_from_jar` (`auth.py:1227`) | Same collapse on the way out of httpx |
| `cookies_by_key` in `save_cookies_to_storage` (`auth.py:997`) | Same collapse on save |

RFC 6265 treats `path` as part of cookie identity. If Google ever
path-scopes a rotation target — `OSID` for a per-product path is the
likely candidate, since it's already per-product — we silently keep
one variant and lose the rest. Empirically not a hot bug today, but a
trip-wire for future protocol changes.

Worse: the iteration order of `cookies_by_key`'s dict-comprehension
over `cookie_jar.jar` is **not specified by `http.cookiejar`** —
which variant survives the collapse depends on insertion order, which
depends on the order Google sent its `Set-Cookie` headers in the
response. So the bug is not just "we drop a variant" but "we
non-deterministically drop a variant", which makes failures hard to
reproduce.

#### 3.4.3 Sibling Google products in the cookie allowlist

> **Resolved in [#360](https://github.com/teng-lin/notebooklm-py/issues/360).**
> `ALLOWED_COOKIE_DOMAINS` now covers sibling Google products
> (`.youtube.com`, `accounts.youtube.com`, `drive.google.com`,
> `docs.google.com`, `myaccount.google.com`, `mail.google.com`), and
> the previously-split `_is_allowed_auth_domain` / `_is_allowed_cookie_domain`
> filters have been collapsed into a single canonical policy
> (`_is_allowed_cookie_domain`); the auth-side function is now a thin
> alias. `_login_with_browser_cookies` automatically widens its rookiepy
> `domains` list because it constructs it from `ALLOWED_COOKIE_DOMAINS`.

**Original problem.** `ALLOWED_COOKIE_DOMAINS` (`auth.py:66-74`) was
narrowly NotebookLM-shaped. Two layered issues:

1. **The extraction gap.** `_login_with_browser_cookies`
   (`cli/session.py:165-172`) passes `ALLOWED_COOKIE_DOMAINS +
   regional ccTLDs` as the `domains` list to `rookiepy.load()`.
   rookiepy was never asked for `.youtube.com`, `accounts.youtube.com`,
   `drive.google.com`, `myaccount.google.com`, `mail.google.com`, or any
   other sibling-product domain. They were absent from
   `storage_state.json` from the moment of extraction. The Playwright
   login path captured whatever the browser context touched, but
   `extract_cookies_with_domains` (strict filter) dropped them at load
   time. Either way, the runtime auth jar had nothing for those domains.

2. **The strict-vs-broad filter asymmetry.** Two filters with different
   policies — `_is_allowed_auth_domain` (exact match against
   `ALLOWED_COOKIE_DOMAINS` ∪ regional ccTLDs) and
   `_is_allowed_cookie_domain` (suffix-matches `.google.com`,
   `.googleusercontent.com`, `.usercontent.google.com`). Auth-jar
   building used the strict filter; persistence
   (`save_cookies_to_storage`'s `cookies_by_key`) used the broad one.
   The asymmetry zone — host-only cookies on subdomains like
   `mail.google.com`, `myaccount.google.com`, `chat.google.com`,
   `lh3.google.com` — got saved by the broad filter and dropped on
   next reload by the strict one. Residue of the incomplete fix in
   [#334 / `fea8315`](https://github.com/teng-lin/notebooklm-py/commit/fea8315)
   that broadened persistence without symmetrically broadening extraction.

**Why it didn't break in observed traffic.** Walking the cookies
actually exercised today:

- `batchexecute` RPC needs only `.google.com` / `accounts.google.com` /
  `notebooklm.google.com` — strict-allowed.
- YouTube/Drive source ingestion: `_sources.py` parses URLs locally;
  the fetch happens server-side on NotebookLM's backend.
- Artifact downloads: hit `*.googleusercontent.com` plus
  `.google.com`-scoped auth cookies. Both strict-allowed.
- Rotation: empirical capture (§5.3) shows `RotateCookies` returns 200
  directly with `Set-Cookie: __Secure-*PSIDTS=…; Domain=.google.com`.
  No traversal of `accounts.youtube.com` is required for the L1 path.

So no auth-relevant cookie was dropped in current flows. The fix is
defensive — symmetric extraction/save policy with sibling domains
covered, so future protocol shifts (signed Drive URLs, `CheckCookie`
chains, Drive-picker flows, YouTube-side rotation) don't turn the
asymmetry into a hot bug.

#### 3.4.4 The "differing-value-wins" merge heuristic

`_find_cookie_for_storage` (`auth.py:1098–1119`) handles the case where
`http.cookiejar` has normalized `Domain=accounts.google.com` to
`.accounts.google.com`. It walks variant keys and returns the first
candidate whose value differs from disk:

```python
for cookie in candidates:
    if cookie.value != stored_value:
        return cookie
return candidates[0]
```

Two failure shapes:

1. If multiple variants legitimately differ from disk after a
   rotation, **set iteration order picks the winner.** Python set
   iteration is implementation-defined (insertion-adjacent but not
   guaranteed); the "right" variant is not specified anywhere.
2. The fallback `return candidates[0]` after the loop is unreachable
   in correct flows but inherits the same ordering ambiguity if it
   ever fires.

Low-priority hazard but worth flagging: when this gets it wrong, the
symptom is "cookies look right on disk but fail when replayed."

#### 3.4.5 `expires=-1` flattens age information

`*PSIDTS` rotations come back from `RotateCookies` without `Max-Age` —
they're "browser session" cookies. `_cookie_to_storage_state`
(`auth.py:1080`) and `convert_rookiepy_cookies_to_storage_state`
(`auth.py:402`) write them as `expires=-1` (Playwright session-cookie
convention) and persist them indefinitely. This means:

- A `*PSIDTS` rotated 30 seconds ago is indistinguishable on disk from
  one rotated 30 hours ago.
- We can't write a "stale on-disk" detector based on cookie metadata —
  the only timestamp we have is the file's `mtime`.
- Diagnostics that print `expires` for debugging show `-1` for the
  cookie that matters most. Use file mtime instead.

#### 3.4.6 `__Host-` invariants are not enforced

> **Mitigated in #365** as a side benefit of fixing §3.4.7. Faithful
> `path`/`secure` preservation on load means `__Host-` cookies survive
> the round-trip without losing the prefix-mandated attributes; the
> remaining gap is `cookie.domain` normalization on the save side.

`__Host-` prefix cookies (`__Host-GAPS`, `__Host-1PLSID`,
`__Host-3PLSID`) **must** have empty `Domain` and `Path=/` per the
prefix rule. `_cookie_to_storage_state` writes whatever
`cookie.domain` happens to be at that point, so any normalization pass
that adds a leading dot to a `__Host-` cookie produces an invalid
shape. Browsers and well-behaved cookie jars discard these on load;
silent drops would manifest as occasional auth-flow flakes.

#### 3.4.7 Load-side attribute loss (round-trip erosion)

> **Resolved in #365.** Both load paths now construct a faithful
> `http.cookiejar.Cookie` via the `_storage_entry_to_cookie` helper,
> preserving `path`, `secure`, and `httpOnly` across load+save cycles.
> The analysis below is retained for historical context.

Every load path uses `cookies.set(name, value, domain=domain)` —
`build_httpx_cookies_from_storage` (`auth.py:822`) and
`load_httpx_cookies` (`auth.py:741`) both. httpx's `Cookies.set`
accepts only `name`, `value`, `domain`, and `path`; we pass none of
the other attributes we faithfully wrote out via
`_cookie_to_storage_state` (`secure`, `httpOnly`, `sameSite`,
non-default `path`).

Concretely, after one load:

| Attribute | On disk | After load (in-memory) |
|---|---|---|
| `path` | whatever was written | always `/` (httpx default) |
| `secure` | preserved on save | `False` (Cookie ctor default) |
| `httpOnly` | preserved on save | `False` |
| `sameSite` | always `"None"` (already hardcoded — see below) | not represented |

If we save back without intervening Set-Cookie observations to refill
the attributes, `_cookie_to_storage_state` (`auth.py:1080`) re-derives
all of these from the in-memory cookie object, which now reflects the
defaults. Each load+save cycle erodes attribute fidelity until disk
stabilizes at `Path=/`, `secure=false`, `httpOnly=false`,
`sameSite="None"`.

For `__Host-`-prefixed cookies this is a logical violation
(§3.4.6). For `__Secure-`-prefixed cookies the `Secure` attribute is
client-side enforcement; Google's server doesn't reject the cookie
just because we send it without a `Secure` assertion, so this is
mostly latent. But the round-trip erosion is real and would bite any
future cookie shape that does enforce attributes server-side.

Related: `convert_rookiepy_cookies_to_storage_state` and
`_cookie_to_storage_state` both **hardcode `sameSite: "None"`**
(`auth.py:405`, `auth.py:1083`). Real Google cookies are a mix of
`Lax` and `None`; we flatten them all to `None` on the way to disk.
Probably benign for our cross-site flow but it's another cell of the
fidelity table that's wrong.

#### 3.4.8 Diagnostic checklist for "cookies expire fast"

Before assuming Google has changed anything:

1. **Compare the `__Secure-1PSIDTS` value on disk before and after a
   `notebooklm` invocation.** If it doesn't change between calls
   spaced > 60 s apart and there's no other process writing the file,
   rotation isn't firing — check `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE`
   and the mtime guard.
2. **If multiple processes share the storage file**, run them with
   `NOTEBOOKLM_LOG_LEVEL=DEBUG` and look for "Keepalive RotateCookies
   skipped: storage refreshed before flock acquired" — that means the
   guards are working. If you see fresh saves immediately followed by
   sibling saves with stale values, you're likely on the legacy
   `original_snapshot=None` save path or a pre-#361 build.
3. **Check storage_state.json `mtime` cadence** — should be ≤ a few
   minutes after each active session if rotation is landing. Hours-old
   mtime means rotation isn't sticking.
4. **Diff the cookie set across two invocations**. Cookies appearing
   in one run and missing in the next: the §3.4.2 path-collapse and
   §3.4.3 whitelist-asymmetry shapes were closed by
   [#361](https://github.com/teng-lin/notebooklm-py/pull/363) and
   [#360](https://github.com/teng-lin/notebooklm-py/pull/362)
   respectively. New cookie-set drift is more likely to point at
   §3.4.7 round-trip attribute erosion.
5. **Only after the above all check out**, investigate Google-side
   causes (risk-scoring, Workspace policy, DBSC).

### 3.5 Empirical cookie requirements (single- and pair-wise ablation)

Tracked separately from §3.4: which cookies does Google *actually* require?
This section documents the empirical accept-rule that backs the library's
two-tier `_validate_required_cookies()` pre-flight (see `auth.py` —
`MINIMUM_REQUIRED_COOKIES` and `_has_valid_secondary_binding()` for the
authoritative values; the historical permissive `{"SID"}` check was
replaced in [#371](https://github.com/teng-lin/notebooklm-py/issues/371)).

**Methodology.** Take a known-good `storage_state.json`, drop one or two
cookies at a time, run `notebooklm --storage <variant> list`, record whether
Google accepts the call (200 + RPC succeeds) or redirects to login
(`accounts.google.com/v3/signin`). Tested on the `teng-lin-9414` profile, a
non-Workspace consumer account on stable home IP.

**Singleton ablation (16 candidate cookies, drop one at a time):** every
cookie *except* `SID` could be removed individually with `notebooks.list` still
succeeding. For most of them Google reissued the missing cookie via
`Set-Cookie` during the call and the library wrote it back automatically.
A handful (`HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSIDTS`,
`__Host-GAPS`) were not reissued — yet the call still succeeded. The library
is highly resilient to single-cookie absence in this regime.

**Pair-wise ablation (105 pairs of those 16 cookies, drop two at a time,
excluding pairs containing `SID`):** **16 of 105 pairs failed** with
`Authentication expired or invalid` → redirect to signin. The failure pattern
is precise:

- **14 failures involve `__Secure-1PSIDTS`** paired with any one of the
  remaining cookies. Although `__Secure-1PSIDTS` is individually removable
  (Google mints a fresh one via `RotateCookies`), that mint POST requires the
  rest of the cookie set to authenticate. Drop `__Secure-1PSIDTS` + anything
  else → recovery breaks.
- **2 failures don't involve `__Secure-1PSIDTS`:**
  - `APISID` + `OSID` removed
  - `SAPISID` + `OSID` removed

The two non-`__Secure-1PSIDTS` failures expose a separate accept-rule.

**The accept-rule model that fits 100% of observed outcomes.** Google accepts
the NotebookLM homepage GET when both hold:

1. **Identity present:** `SID` is valid (and `__Secure-1PSIDTS` is either
   directly present, or recoverable via `RotateCookies` POST — which means
   the full ambient cookie set must be present).
2. **At least one secondary binding present:**
   - Either `OSID` is present, OR
   - Both `APISID` *and* `SAPISID` are present.

Confirmation test (pair 28/105): dropping `APISID + SAPISID` together while
`OSID` remains → call succeeds. Model predicts OK; observed OK.

| Variant | `SID` | `OSID` | `APISID+SAPISID` pair | `__Secure-1PSIDTS` (or recoverable) | Predicted | Observed |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| Baseline | ✓ | ✓ | ✓ | ✓ | OK | OK |
| Drop `__Secure-1PSIDTS` only | ✓ | ✓ | ✓ | recoverable | OK | OK |
| Drop `__Secure-1PSIDTS` + any one other | ✓ | ✓ | ✓ | broken (mint POST fails) | FAIL | FAIL |
| Drop `OSID` only | ✓ | ✗ | ✓ | ✓ | OK (AP*SID path) | OK |
| Drop `APISID + SAPISID` | ✓ | ✓ | ✗ | ✓ | OK (OSID path) | OK |
| Drop `APISID + OSID` | ✓ | ✗ | ✗ | ✓ | FAIL | FAIL |
| Drop `SAPISID + OSID` | ✓ | ✗ | ✗ | ✓ | FAIL | FAIL |

The model fits all 105 + 16 = 121 data points without exception.

**Why this matters for `MINIMUM_REQUIRED_COOKIES`.** Before #371 the library
trusted any storage with `SID` present, which permitted Google-rejected cookie
sets to reach the wire. The result was the user-facing "auth expires
immediately after `notebooklm login`" pattern reported in
[#133](https://github.com/teng-lin/notebooklm-py/issues/133),
[#332](https://github.com/teng-lin/notebooklm-py/issues/332), and others.

The pre-flight now catches all 16 ablation failures via a two-tier check in
`_validate_required_cookies()`:

```python
MINIMUM_REQUIRED_COOKIES = {"SID", "__Secure-1PSIDTS"}  # Tier 1: raise

def _has_valid_secondary_binding(cookie_names: set[str]) -> bool:  # Tier 2: warn
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID"} <= cookie_names
```

Hybrid rollout: Tier 1 raises (unambiguous evidence); Tier 2 logs a warning
once per process so partial extractions surface without breaking edge-case
flows (e.g. Workspace SSO) that we haven't ablated. See
[#371](https://github.com/teng-lin/notebooklm-py/issues/371).

**Caveats.**

- All 121 ablation runs were on a single profile (non-Workspace, stable IP).
  Workspace accounts may have different accept-rules; we haven't tested.
- We tested `notebooks.list` only. Other code paths (chat, generate, download)
  share the same auth machinery but theoretically could have different
  sensitivities — though we haven't observed any.
- This is a *model fit* to 121 data points, not a confirmed mechanism. The
  exact server-side logic would require capturing the precise HTTP request
  on success vs failure and identifying the missing signal.
- The accept-rule is what governs *acceptance*. The freshness clock (§3.1)
  still applies on top of it — a session with a valid accept-tuple can still
  be killed by Google's risk model independent of which cookies are present.

**Reproducer.** `tests/manual/test_cookie_ablation.py` (one-shot singleton)
and `tests/manual/test_cookie_ablation_pairs.py` (105 pairs, ~10 min wall).
Each operates on copies of a known-good `storage_state.json` — never mutates
the original.


---

## 4 · The architecture

The library uses a tiered design that progressively escalates as cheaper
mechanisms fail. Each layer has a distinct trigger and target failure mode.

```
┌──────────────────────────────────────────────────────────────┐
│ L1: per-call RotateCookies POST                              │
│   - fires inside _fetch_tokens_with_jar before homepage GET  │
│   - cost: ~150ms per token fetch                             │
│   - covers: short interactive use, every CLI invocation      │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼ (long-lived clients also do)
┌──────────────────────────────────────────────────────────────┐
│ L2: NotebookLMClient(keepalive=N) background task            │
│   - asyncio.Task, fires _poke_session every N seconds        │
│   - opt-in via parameter; floor 60s                          │
│   - covers: agents, MCP servers, long-running workers        │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼ (idle profiles between processes)
┌──────────────────────────────────────────────────────────────┐
│ L3: notebooklm auth refresh (OS-scheduled)                   │
│   - cron / launchd / systemd / Task Scheduler / k8s          │
│   - calls fetch_tokens_with_domains, exits 0/1               │
│   - covers: profiles idle > SIDTS window between Python runs │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼ (when L1's HTTP-only path weakens)
┌──────────────────────────────────────────────────────────────┐
│ L4: notebooklm login --browser-cookies firefox (cron)        │
│   - rookiepy reads Firefox cookies.sqlite                    │
│   - works without Keychain prompt on macOS                   │
│   - DBSC-immune (Firefox isn't DBSC-enrolled by Google)      │
│   - requires Firefox installed and signed in                 │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼ (when DBSC extends to non-Chrome paths)
┌──────────────────────────────────────────────────────────────┐
│ L5 (proposed): CDP-attach to user's running Chrome           │
│   - Playwright connect_over_cdp("http://localhost:9222")     │
│   - harvest cookies from user's signed-in daily Chrome       │
│   - inherits Chrome's TPM-bound DBSC enrollment              │
│   - requires Chrome with --remote-debugging-port=9222        │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼ (alternate L5: cross-machine federation)
┌──────────────────────────────────────────────────────────────┐
│ L6 (optional): CookieCloud client integration                │
│   - browser extension watches user's daily Chrome cookies    │
│   - encrypts (AES via CryptoJS) + uploads to self-hosted     │
│     CookieCloud server                                       │
│   - notebooklm-py pulls fresh cookies on demand              │
│   - sidesteps Chrome 127+ App-Bound Encryption (extension    │
│     reads via chrome.cookies API, not SQLite)                │
└──────────────────────────────────────────────────────────────┘
```

Each layer is a fallback for the next one above. The first three are
HTTP-only (cheap, no browser dependency); L4 is the lightweight browser
path; L5 is the durability insurance; L6 is the federation play.

---

## 5 · L1 deep dive — the `RotateCookies` primitive

### 5.1 The endpoint

```
POST https://accounts.google.com/RotateCookies
Content-Type: application/json
Origin: https://accounts.google.com

[000,"-0000000000000000000"]
```

The body is a **JSPB (JavaScript Protocol Buffers) sentinel**. JSPB is
Google's array-shaped serialization format used by `batchexecute`,
`RotateCookies`, and similar internal endpoints. The two-element body
decomposes as:

- `000` — an integer literal `0` written with leading zeros. Invalid in
  strict JSON, valid in Google's JSPB parser. Probably a version or
  operation tag in slot 0.
- `"-0000000000000000000"` — a string of 19 zeros prefixed with `-`. This is
  a **sentinel value** that means "I don't have a prior `__Secure-1PSIDTS`,
  please mint a fresh one based on the persistent identity (`SID`/`PSID`)
  alone." Without this sentinel the endpoint requires the client's current
  `*PSIDTS` value as input.

The pattern is borrowed from
[`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/utils/rotate_1psidts.py),
which has been using it in production with a sizable user base.

### 5.2 The successful response

```
HTTP/1.1 200 OK
Set-Cookie: __Secure-1PSIDTS=<new_value>; Domain=.google.com; Secure; HttpOnly
Set-Cookie: __Secure-3PSIDTS=<new_value>; Domain=.google.com; Secure; HttpOnly
Set-Cookie: SIDCC=<new_value>; Domain=.google.com; Secure
Set-Cookie: __Secure-1PSIDCC=<new_value>; Domain=.google.com; Secure
Set-Cookie: __Secure-3PSIDCC=<new_value>; Domain=.google.com; Secure

)]}'  [["identity.hfcr",600],["di",<integer>]]
```

The `)]}'` prefix is Google's standard anti-XSSI token. The JSPB body
(`[["identity.hfcr",600],["di",N]]`) appears to encode:

- `["identity.hfcr",600]` — `identity.hfcr` likely "high-frequency cookie
  rotation"; `600` is the recommended next-rotation interval in seconds
  (10 minutes). **This validates the documented `*PSIDTS` rotation cadence
  directly.**
- `["di",N]` — opaque session/rotation counter (varies by profile).

The library's [`save_cookies_to_storage`](../src/notebooklm/auth.py)
captures the rotated `Set-Cookie` headers and persists them atomically to
`storage_state.json`.

### 5.3 Empirical validation (May 2026)

Field experiment configuration:

- **Probe A** (control): main code, no L1 poke, Playwright-extracted cookies.
- **Probe B**: `feat/auth-keepalive-bg-task` branch, L1 `CheckCookie` poke,
  Playwright-extracted cookies.
- **Probe C**: re-extracts Firefox cookies every cycle, main code.
- All probes run on a 5-minute cadence, instrumented to log redirect chains
  and `Set-Cookie` headers from each endpoint.

Results:

| | Probes | OK | Failures | First failure | `*PSIDTS` rotated via |
|---|---|---|---|---|---|
| **A (control)** | 33 | 4 | 29 | T+20m | never (died) |
| **B (CheckCookie L1)** | 35+ | 35+ | 0 | — | only via observation, not via the L1 GET (CheckCookie chain stops at 2 hops, no `SetSID`, no `*PSIDTS` in response) |
| **C (Firefox re-extract)** | 22+ | 22+ | 0 | — | every probe (CheckCookie chain has 3 hops including `accounts.youtube.com/SetSID`) |

Then we instrumented all probes to additionally hit `RotateCookies` directly
as a measurement (no production code change yet):

| | RotateCookies POST attempts | 200 + `*PSIDTS` in `Set-Cookie` | 401s |
|---|---|---|---|
| **B (Playwright/bound session)** | 6+ | **6+/6+** | 0 |
| **C (Firefox/unbound session)** | 7+ | **7+/7+** | 0 |

**100% rotation success rate across both session types.** No 401s, no
DBSC challenges, no `Sec-Session-*` headers in any response. The unsigned
`RotateCookies` POST is empirically the cleanest available rotation
primitive for both bound and unbound sessions today.

### 5.4 Why it's better than `CheckCookie`

The previous L1 mechanism (commits `eae3eaf` through `8047718`) used
`GET https://accounts.google.com/CheckCookie?continue=...notebooklm.google.com/`,
relying on Google to issue a redirect chain that *might* go through
`accounts.youtube.com/SetSID`, which *might* set fresh `*PSIDTS` cookies.
Empirically:

- **For Firefox-extracted (unbound) profiles:** the chain is 3 hops and
  `SetSID` does set fresh `*PSIDTS`. Works.
- **For Playwright-extracted (bound) profiles:** the chain is 2 hops, no
  `SetSID` step, no `*PSIDTS` in any `Set-Cookie`. The poke touches the
  identity surface (and, observably, extends server-side session validity
  through some untracked mechanism — B's session lived hours longer than A
  despite identical underlying cookies) but **does not rotate `*PSIDTS`**.

This is why the L1 docstring was originally inaccurate: "elicits
`__Secure-1PSIDTS` rotation" is true for unbound sessions and false for
bound ones.

`RotateCookies` POST removes the discretion: **direct rotation request,
unconditional response, both session types.**

### 5.5 Rate limiting and concurrency throttle

Gemini-API observed that hammering `RotateCookies` triggers HTTP 429. The
naïve mitigation is a **60-second cache-file mtime guard**: skip the POST if
the storage state was rewritten within the last minute. The
`[["identity.hfcr",600], ...]` self-reported interval is 600 s, so a 60 s
floor leaves a comfortable order of magnitude of headroom.

The merged implementation (`auth.py::_poke_session` and
`auth.py::_rotate_cookies`, [#346](https://github.com/teng-lin/notebooklm-py/pull/346)
+ [#348](https://github.com/teng-lin/notebooklm-py/pull/348)) wraps the POST
in **three concentric guards**, because a single mtime check is not enough
once you have an L1 caller, an L2 background loop, and a fan-out of
parallel CLI invocations all keyed to the same `storage_state.json`:

1. **Disk mtime fast-path** (`_is_recently_rotated`). If
   `storage_state.json` was rewritten within `_KEEPALIVE_RATE_LIMIT_SECONDS`
   (60 s), skip without acquiring any lock. A `_KEEPALIVE_PRECISION_TOLERANCE`
   of 2 s absorbs sub-second drift between `time.time()` and filesystem
   mtime resolution (notably Windows NTFS at lower clock granularity).
   A meaningfully-future mtime is treated as **not recent** — better to fire
   one extra rotation than wedge the guard until wall time catches up.
2. **In-process throttle** (`_get_poke_lock` + `_try_claim_rotation`).
   Inside an `asyncio.Lock` keyed by `(running event loop, storage_path)`,
   re-check the mtime *and* a per-profile monotonic timestamp stamped under
   a `threading.Lock`. The atomic check-and-stamp deduplicates an
   `asyncio.gather` fan-out so only one POST fires per process per
   rate-limit window. The timestamp is bumped **before** the network await
   so a 15 s timeout against a hung `accounts.google.com` does not let 10
   fanned-out callers each wait the full timeout.
3. **Cross-process non-blocking flock**
   (`.storage_state.json.rotate.lock` via `LOCK_NB`). When `storage_path`
   is set, try to take an exclusive flock; if another process holds it,
   skip — they're rotating right now. This handles `xargs -P`, parallel
   MCP workers, and similar parallel launches without queueing. The
   rotation lock is intentionally distinct from the
   `.storage_state.json.lock` used by `save_cookies_to_storage`, so a
   long-running save doesn't block rotations or vice versa.

The L2 background loop bypasses guards 1 and 2 (it's already self-paced via
`keepalive_min_interval`) and calls `_rotate_cookies` directly, which still
performs the atomic per-profile claim — so a layer-1 `_poke_session` on a
sibling event loop sees the in-flight rotation and skips.

### 5.6 Concurrency model: why three guards instead of one

| Failure mode | Caught by |
|---|---|
| User runs 10 sequential `notebooklm` CLI invocations | Disk mtime fast-path |
| `asyncio.gather([client.rpc(...) for _ in range(N)])` from one process | In-process `asyncio.Lock` + monotonic timestamp |
| L1 caller racing the L2 keepalive loop on the same profile | Per-profile monotonic timestamp under `threading.Lock` |
| Two CLI invocations or worker processes started simultaneously | Cross-process flock (`LOCK_NB`) |
| Hung `accounts.google.com` causing 15 s-per-caller fan-out | Stamp-before-await: timestamp claimed before the network call |
| Read-only filesystem / NFS without flock | Locks **fail open**: rotation proceeds rather than wedge forever |

The per-`(loop, profile)` lock dictionary is held in a
`WeakKeyDictionary` keyed on the loop *object*, so when a short-lived
`asyncio.run()` loop is garbage-collected its inner dict is reclaimed
automatically — bounded cache without an `id()`-reuse hazard.

---

## 6 · Comparison with related projects

### 6.1 [`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API)

Closest peer. Targets Google Bard / Gemini web UI rather than NotebookLM,
but the auth surface is identical (same `*.google.com` cookies, same
`RotateCookies` endpoint).

**Strengths:**
- The reference implementation of `RotateCookies` rotation
  (`src/gemini_webapi/utils/rotate_1psidts.py`).
- Cache-file-mtime rate-limit guard.
- Cache file keyed by `__Secure-1PSID` value
  (`.cached_cookies_<sid>.json`) — automatically scopes by Google account.
- Default-on background refresh (`auto_refresh=True`, 600s interval) for
  long-lived clients.
- CLI explicitly opts out (`auto_refresh=False`) since each invocation
  is short-lived.

**Weaknesses:**
- No reactive/recovery layer — when rotation fails, the client just dies.
  No L4-equivalent to fall back to.
- The init() docstring overpromises: claims to refresh "cookies and access
  token" but the background loop only rotates cookies, never re-runs
  `get_access_token`.
- Uses curl_cffi (browser-impersonating TLS); we use httpx. Their tighter
  fingerprint may explain why Gemini-API hasn't seen DBSC issues yet for
  most users.

**Canary:** issues
[#310](https://github.com/HanaokaYuzu/Gemini-API/pull/310) (Apr 2026 —
proposes "activity warmup + browser impersonation" as workaround for
Chrome's DBSC-related compat issues) and
[#319](https://github.com/HanaokaYuzu/Gemini-API/issues/319) (Apr 2026 —
`UNAUTHENTICATED` after rotation). When #310 ships as default, the simple
sentinel pattern is decaying.

### 6.2 [`easychen/CookieCloud`](https://github.com/easychen/CookieCloud)

A different category — browser-companion cookie federation. Browser
extension (Chrome/Edge/Firefox) watches cookies on configured domains,
encrypts with AES-CryptoJS using `MD5(uuid+password)[:16]` as key,
periodically uploads to a self-hosted server. Clients (Python, Go, JS, Deno)
download and decrypt.

**Strengths:**
- **Sidesteps Chrome 127+ App-Bound Encryption entirely.** The extension
  reads cookies via Chrome's own `chrome.cookies` API, not by reading the
  SQLite DB.
- DBSC-immune for the same reason — the cookies are sourced from the user's
  daily Chrome which handles all DBSC dance internally.
- Server is tiny (a Node.js or PHP daemon, single Docker container).
- End-to-end encrypted; server never sees plaintext.
- Cross-machine — your cron on a remote server can pull cookies refreshed
  by your laptop's daily Chrome.
- Active maintenance (v1.0.3 May 2026), Python client
  ([`PyCookieCloud`](https://github.com/lupohan44/PyCookieCloud)) is ~200
  LOC to integrate.

**Weaknesses:**
- Requires user to install browser extension AND self-host server.
- No upstream NotebookLM/Gemini integration — would need to be built.
- Some Chinese-origin codebase elements may give pause to Western
  enterprise users; the project itself is MIT, code is auditable.

### 6.3 [`dsdanielpark/Bard-API`](https://github.com/dsdanielpark/Bard-API) (archived)

Historical reference. **Archived April 2024.** No automated refresh — users
manually re-paste cookies on every breakage. Issue
[#231](https://github.com/dsdanielpark/Bard-API/issues/231) is the canonical
"we can't reliably automate `SNlM0e` refresh" thread that motivated
Gemini-API's design. The failure mode of *not* having an L1+ design is
visible here: project archived because manual cookie management was
untenable.

### 6.4 Crosscuts

Common patterns across the projects reviewed:

- **Docstring rot is universal.** Every project surveyed has docstrings that
  overpromise about what the refresh mechanism does. Worth being defensive
  about in our own.
- **`SID`-keyed cache files** (Gemini-API) are a nicer pattern than
  profile-name-keyed. Worth consideration for #345 MEDIUM-3.
- **Reactive-only is insufficient.** Bard-API's no-automated-refresh design
  ended in archival; users gave up because manual re-paste was
  untenable. Demonstrates why proactive L1/L2/L3 matters even when L4/L5
  recovery is in place.

---

## 7 · What we tried and ruled out

These approaches were investigated and rejected; documented here so future
contributors don't re-investigate them.

### 7.1 `undetected-chromedriver` / `selenium-stealth`

**Verdict: Don't use for Google login.**

- `ultrafunkamsterdam/undetected-chromedriver` — author has effectively
  migrated to `nodriver`. Google login broken since Chrome 110, re-broken
  on each major Chrome bump. Active issues against Chrome 142 in Jan 2026.
- `diprajpatra/selenium-stealth` — no meaningful release in years.
- The 2026 fork `praise2112/selenium-stealth` is more current but still
  loses to Google's signal-fusion model (TLS, behavioral, fingerprint).

Consensus across multiple 2026 guides: stop using WebDriver-based stealth
for Google flows.

### 7.2 `puppeteer-extra-plugin-stealth` / `playwright-stealth`

**Verdict: Don't use for `accounts.google.com` flows.**

Long-standing Google-login bugs:
[`berstend/puppeteer-extra#588`](https://github.com/berstend/puppeteer-extra/issues/588)
(2022, unfixed),
[`#898`](https://github.com/berstend/puppeteer-extra/issues/898)
(Chrome 122 broke meet.google.com).

Python `playwright-stealth` (v2.x) is the most active variant but Scrapfly
and AlterLab guides explicitly warn it patches *fingerprint leaks only*, not
TLS, IP reputation, or behavioral signals. Effective for resumed sessions
where cookies are already present, fails for fresh sign-in.

### 7.3 Persistent Playwright headless context as keepalive daemon

**Verdict: Don't ship.**

Two unresolved Playwright bugs make this fragile:

- [`microsoft/playwright#36139`](https://github.com/microsoft/playwright/issues/36139)
  — cookies missing in headless `launch_persistent_context`.
- [`microsoft/playwright#35466`](https://github.com/microsoft/playwright/issues/35466)
  — profile DB corruption in long-lived contexts.

If a headless-Playwright option is needed, prefer **CDP-attach** (Playwright
`connect_over_cdp` to user's running Chrome) — different code path, not
exposed to either bug.

### 7.4 Client-side DBSC implementation

**Verdict: Impossible from Python.**

The W3C DBSC spec is structured around a TPM-bound private key that signs
nonces from the server. Without TPM access (which isn't directly exposed
through Python on any platform) and the platform attestation chain Chrome
implements, no non-Chrome client can satisfy `RotateBoundCookies`. No
public OSS DBSC client exists; the spec is deliberately designed to prevent
one.

If/when DBSC extends to non-Chrome cookie paths, the only escape is to
parasitize a real DBSC-enrolled Chrome session via L5 (CDP attach) or L6
(CookieCloud).

### 7.5 Cookie database read on Chrome 127+

**Verdict: Increasingly unreliable; prefer Firefox.**

Chrome 127 introduced **App-Bound Encryption** for cookies on Windows.
`browser_cookie3` (latest v0.20.1) does **not** handle ABE; rookiepy claims
to but requires admin from Chrome 130+
([rookie#50](https://github.com/thewh1teagle/rookie/issues/50)). The
yt-dlp ecosystem has converged on
"[only Firefox `--cookies-from-browser` reliably works in 2026](https://dev.to/osovsky/6-ways-to-get-youtube-cookies-for-yt-dlp-in-2026-only-1-works-2cnb)."

Pragmatic forks for ABE bypass exist (CyberArk's "C4 Bomb",
xaitax/Chrome-App-Bound-Encryption-Decryption) but are infostealer-adjacent
and inappropriate for shipping in a legitimate CLI.

**Library recommendation:** Document `--browser-cookies firefox` as the
recommended path on Windows. Keep `--browser-cookies chrome` working but
note it may require admin or Keychain prompts.

---

## 8 · Recommendations by use case

### 8.1 Interactive desktop user

Just `notebooklm login`. The Playwright Chromium flow handles it. Re-login
when prompted (typically days to weeks between prompts).

### 8.2 Long-lived in-process client (agent, MCP server, worker)

```python
async with await NotebookLMClient.from_storage(keepalive=600) as client:
    ...
```

L1 fires on `from_storage()`, L2 fires every 600s while the client is open.
This was sufficient through the entire 24h+ window of our experiment.

### 8.3 Unattended / headless / CI / cron

Two stacks, in order of preference:

**Preferred (today, May 2026):**

1. Sign in to NotebookLM **once** in Firefox (or any rookiepy-supported
   browser — see note below).
2. `notebooklm login --browser-cookies firefox -p <profile>`.
3. Schedule a cron / launchd / systemd job:
   ```
   7,27,47 */1 * * * notebooklm --profile <profile> auth refresh
   ```
   (Off-minute schedule avoids fleet collision.)
4. Keep Firefox running with at least one Google tab. Even closed-Firefox
   works for hours-to-days as long as `RotateCookies` keeps succeeding from
   `SID` alone, but a running Firefox is an extra layer of resilience.

> **Browser support:** `--browser-cookies` accepts any of the ~16 browsers
> rookiepy can read on the host platform — `arc`, `brave`, `chrome`,
> `chromium`, `edge`, `firefox`, `ie`, `librewolf`, `octo`, `opera`,
> `opera-gx`, `safari`, `vivaldi`, `zen`. **Firefox is the recommended
> path on Windows** specifically because Chrome 127+ App-Bound Encryption
> makes Chrome cookie reads admin-or-bust (see §7.5). On macOS and Linux,
> any of the listed browsers work; Firefox just sidesteps the Keychain
> prompt that Chrome / Brave / Edge trigger on first read. See
> `_ROOKIEPY_BROWSER_ALIASES` in `cli/session.py` for the canonical list.

**With cookie federation (best UX, requires self-hosting):**

1. Self-host CookieCloud server.
2. Install CookieCloud browser extension in your daily Chrome, configure to
   sync `*.google.com`.
3. Use `PyCookieCloud` to pull cookies on demand (L6 — proposed, not yet
   shipped in `notebooklm-py`).

#### 8.3.1 Anti-pattern: persisting `storage_state` on a redirect-to-login

If you wrap the library in your own Playwright-based keepalive — instead
of using `notebooklm auth refresh` or the in-process `keepalive=N` option
— the most damaging mistake is to call `context.storage_state(path=...)`
unconditionally at the end of each cycle. The corruption sequence
(originally reported in
[#312](https://github.com/teng-lin/notebooklm-py/issues/312)):

1. Session has aged out — common on cloud-VPS IPs, where Google
   force-logs-out more aggressively than on residential IPs.
2. `await page.goto("https://notebooklm.google.com/")` 302s through
   `accounts.google.com/v3/signin/.../flowName=*SignIn`.
3. The login page sets six anonymous cookies — `NID`, `OTZ`,
   `__Host-GAPS`, `_ga`, `_ga_*`, `_gcl_au` — and a subsequent
   `context.storage_state(path=...)` serializes **only those**, dropping
   `SID`, `HSID`, `__Secure-1PSID`, `__Secure-3PSID`, `SAPISID`,
   `APISID`, and any `*PSIDTS`.
4. The next cold start finds a six-cookie storage file, fails every RPC,
   and the persistent Chrome profile takes the same Set-Cookie hit on
   each retry — the profile fallback dies along with the storage file.

**Recovery requires fresh interactive login.** No `auth refresh`, no
profile copy, no on-disk backup short of one you took yourself. This is
the same class of failure that `c7d7b0d` (#334, "keep NotebookLM
subdomain cookies") and `fea8315` ("preserve cross-domain cookies")
guard against on the library's *own* write path — but those guards live
inside `auth.py`'s save pipeline and don't help code that calls
Playwright directly.

The rule, for any wrapper that owns its own `context.storage_state`
call: gate persistence on a confirmed-authed page URL.

```python
SAFE_HOSTS = ("notebooklm.google.com",)  # extend if you legitimately
                                         # land on other authed surfaces

if any(h in page.url for h in SAFE_HOSTS):
    await context.storage_state(path=STORAGE)
else:
    logger.warning("skipping storage_state persist: page on %s", page.url)
    # treat as a no-op; let the next cycle retry, or raise an alert
    # for interactive re-login
```

Equivalently — and more robust, since URL-substring checks miss
edge cases like in-page JS-driven sign-in prompts — gate persistence on
a successful library API call rather than the URL:

```python
from notebooklm import NotebookLMClient, AuthError

try:
    async with await NotebookLMClient.from_storage() as client:
        await client.notebooks.list()  # confirms auth
except (AuthError, ValueError):
    # ValueError: from_storage()'s CSRF / session-id extraction
    #   detected a redirect to accounts.google.com during fetch_tokens
    #   (see auth.py:extract_csrf_token_from_html / extract_session_id_from_html)
    # AuthError: a subsequent RPC call decoded an auth-class failure
    return  # don't overwrite a good file with a bad jar
await context.storage_state(path=STORAGE)
```

If you don't actually need a custom wrapper, prefer the supported
keepalive surface — `notebooklm auth refresh` from cron (see the two
stacks above) or `NotebookLMClient(keepalive=N)` for in-process
clients. Both already gate their writes correctly under §3.4's
fidelity rules.

### 8.4 Workspace / Enterprise account with admin session-binding

Currently **not supported.** Document as such. The admin-policy session
binding is a Workspace-only beta and requires DBSC-compatible flows.
Library users should request an exemption from their admin or use a
personal Google account for automation.

---

## 9 · Operational levers (environment variables)

Two env vars in `auth.py` exist as escape hatches around the keepalive
machinery. Documented here so operators don't have to grep for them.

### 9.1 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`

Disables the `RotateCookies` POST entirely. Both L1 (`_poke_session` inside
`_fetch_tokens_with_jar`) and L2 (the `_keepalive_loop` background task) honour
this. The L2 task still wakes on its interval — only the network call becomes
a no-op — so to disable the loop *itself* pass `keepalive=None` to
`NotebookLMClient`.

When to set it:

- **Restricted networks** where outbound POSTs to `accounts.google.com` are
  blocked or rate-limited at the egress layer.
- **Regression triage** — if a user reports auth failures, asking them to
  re-run with this flag isolates whether the rotation poke is the cause.
- **Test environments** that mock the auth surface and don't want real
  POSTs leaking out.

### 9.2 `NOTEBOOKLM_REFRESH_CMD=<shell-command>`

Reactive recovery hook (merged in
[#336](https://github.com/teng-lin/notebooklm-py/pull/336),
`auth.py::_should_try_refresh` and `_run_refresh_cmd`). When token fetch
fails with an auth-expiry signal (the
"`Authentication expired or invalid`" / `accounts.google.com` redirect),
the library:

1. Runs the configured shell command via `subprocess.run(..., shell=True)`
   with a 60 s timeout.
2. Sets `NOTEBOOKLM_REFRESH_PROFILE` and `NOTEBOOKLM_REFRESH_STORAGE_PATH`
   in the child env so the script knows which profile to refresh.
3. Sets `_NOTEBOOKLM_REFRESH_ATTEMPTED=1` in the child env to prevent
   recursive refresh loops if the script itself invokes `notebooklm`.
4. Reloads cookies from `storage_state.json`, replays token fetch once.

A `ContextVar` (`_REFRESH_ATTEMPTED_CONTEXT`) gates same-task retries in
the parent process, and `_REFRESH_LOCK` + `_REFRESH_GENERATIONS` ensure
that a fan-out of N concurrent failing requests triggers exactly one
refresh, not N.

This is **orthogonal** to L1–L3:

- L1/L2/L3 keep `*PSIDTS` fresh proactively (no-op when nothing's broken).
- `NOTEBOOKLM_REFRESH_CMD` runs only on auth-expiry failure — it's the
  reactive last line of defense, useful when the upstream refresh has
  already failed (e.g. password change, manual sign-out, DBSC enforcement
  arriving on this client tomorrow). Common shapes:

  ```bash
  # Re-extract from running Firefox
  export NOTEBOOKLM_REFRESH_CMD='notebooklm login --browser-cookies firefox'

  # Sync from a CookieCloud server
  export NOTEBOOKLM_REFRESH_CMD='/opt/scripts/pull-cookies-from-cloud.sh'
  ```

  The library does not validate the command's contents — the operator is
  responsible for ensuring it produces a valid `storage_state.json`.

---

## 10 · Canaries and signals

When to panic:

| Signal | Source | What it means | Action |
|---|---|---|---|
| `RotateCookies` returns 401 in production | Library logs | DBSC has been extended to non-Chrome paths for at least some accounts | Escalate to L5 (CDP-attach) implementation |
| `RotateCookies` returns 200 but no `*PSIDTS` in `Set-Cookie` | Library logs | Silent failure mode — cookies on disk are not being rotated | Add WARN log and alert on this; manual re-auth required |
| [HanaokaYuzu/Gemini-API#310](https://github.com/HanaokaYuzu/Gemini-API/pull/310) merges as default | GitHub | Activity-warmup workaround needed in production for the broader Gemini-API user base | Plan to mirror their approach within 4 weeks |
| [HanaokaYuzu/Gemini-API#319](https://github.com/HanaokaYuzu/Gemini-API/issues/319) gets "me too" reports | GitHub | Account-specific failures spreading | Investigate whether our user base is affected |
| Chrome macOS DBSC GA announced | [Chrome dev blog](https://developer.chrome.com/) | macOS users will start getting DBSC enrollment | 3–6 months warning before consumer accounts may be enforced |
| Workspace session-binding moves out of beta | [Workspace admin docs](https://knowledge.workspace.google.com/admin/security/) | More org admins will enable it | Document explicit non-support clearer |

---

## 11 · Open questions

Things we don't know that would inform future iterations:

- **Exact `*PSIDTS` server-side TTL distribution.** We've seen the
  `["identity.hfcr",600]` declared interval. Anecdotal data from
  Gemini-API/Bard-API issue threads suggests 5-60 min variation by account.
  Real longitudinal data would let us tune L2's 60s floor more precisely.
- **What kept Probe B alive past T+20m without `*PSIDTS` rotation?** B used
  `CheckCookie` GET as L1, which observably did *not* rotate `*PSIDTS`.
  Yet B's session survived hours past A's death (same cookies, no L1).
  Most likely: server-side "session touched" extension via the unsigned
  rotation endpoint or identity-surface hit. Untested hypothesis.
- **DBSC enrollment status for Playwright-launched Chromium.** We assumed
  Playwright Chromium's session is non-DBSC-bound on macOS/Linux (no TPM)
  but might be bound on Windows. Untested. If Playwright Chromium can
  register a DBSC key, L5-A becomes more viable than current research
  suggests.
- **Whether `RotateBoundCookies` returns interpretable error codes** for
  unsigned attempts. Could let us detect DBSC enforcement transition
  proactively rather than reactively.

---

## 12 · References

### Project peers

- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) —
  reference for `RotateCookies` rotation
  ([source](https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/utils/rotate_1psidts.py))
- [easychen/CookieCloud](https://github.com/easychen/CookieCloud) +
  [PyCookieCloud](https://github.com/lupohan44/PyCookieCloud)
- [dsdanielpark/Bard-API](https://github.com/dsdanielpark/Bard-API) (archived)

### Cookie extraction libraries

- [`borisbabic/browser_cookie3`](https://github.com/borisbabic/browser_cookie3)
- [`thewh1teagle/rookie`](https://github.com/thewh1teagle/rookie) (rookiepy)
- [`n8henrie/pycookiecheat`](https://github.com/n8henrie/pycookiecheat)

### DBSC

- [Google's DBSC GA announcement (Apr 2026)](https://blog.google/security/protecting-cookies-with-device-bound-session-credentials/)
- [Chrome DBSC Windows GA blog](https://developer.chrome.com/blog/dbsc-windows-announcement)
- [W3C DBSC spec](https://w3c.github.io/webappsec-dbsc/)
- [Google Workspace session-binding (beta)](https://knowledge.workspace.google.com/admin/security/prevent-cookie-theft-with-session-binding)

### Internal references

- [#312 — `*PSIDTS` rotation requires `accounts.google.com` touch](https://github.com/teng-lin/notebooklm-py/issues/312)
- [#297 — `NOTEBOOKLM_REFRESH_CMD` proposal](https://github.com/teng-lin/notebooklm-py/issues/297) /
  [#336 — implementation merged](https://github.com/teng-lin/notebooklm-py/pull/336)
- [#341 — L2 background keepalive task](https://github.com/teng-lin/notebooklm-py/pull/341)
- [#342 / #343 / #344 — keepalive race fixes](https://github.com/teng-lin/notebooklm-py/pull/342)
- [#345 — Auth keepalive umbrella issue](https://github.com/teng-lin/notebooklm-py/issues/345) /
  [#346 — L1 RotateCookies POST + 60 s mtime guard merged](https://github.com/teng-lin/notebooklm-py/pull/346)
- [#347 / #348 — concurrent-poke throttle (three-guard model)](https://github.com/teng-lin/notebooklm-py/pull/348)

---

## Changelog

- **2026-05-09** — Initial writeup. Captures the field experiment results,
  cross-project review, RotateCookies-vs-CheckCookie finding, and the
  L1–L6 tiered architecture. DBSC threat model reflects rollout state as
  of Chrome 146 GA Windows.
- **2026-05-09 (rev 2)** — Synced doc to merged code state.
  - L1 (`RotateCookies` POST) is now merged via #346, not "proposed in
    #345"; concurrent-poke throttle merged via #348.
  - Section 5.5 rewritten to describe the **three concentric guards**
    actually implemented (disk mtime fast-path → in-process
    `asyncio.Lock` + per-profile monotonic timestamp under
    `threading.Lock` → cross-process non-blocking flock on
    `.storage_state.json.rotate.lock`). New §5.6 maps each failure mode to
    the guard that catches it.
  - New §9 documents `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` and
    `NOTEBOOKLM_REFRESH_CMD` (the latter merged in #336 — proactive
    L1/L2/L3 vs reactive `REFRESH_CMD` distinction made explicit).
    Subsequent sections renumbered (Canaries → §10, Open questions → §11,
    References → §12).
  - §8.3 clarifies that `--browser-cookies` accepts any of the ~16
    rookiepy-supported browsers (Firefox is the *Windows* recommendation,
    not a global one) and points at `_ROOKIEPY_BROWSER_ALIASES`.
- **2026-05-09 (rev 3)** — Added §2 *Background* covering the cookie
  taxonomy (`__Secure-` / `__Host-` prefixes, 1P vs 3P, the
  `*SID`/`*SIDTS`/`*SIDCC` family split), the rotation model (the
  identity vs freshness clocks, why `batchexecute` traffic doesn't
  rotate), the DBSC protocol (TPM-bound nonce signing,
  `RotateBoundCookies`, why no Python client can implement it), and how
  `rookiepy` extracts cookies from encrypted browser stores
  (Keychain/DPAPI/libsecret + Chrome 127+ App-Bound Encryption). New
  §2.5 disambiguates the three timers people confuse (server-side
  `*PSIDTS` TTL, `*SIDCC` window, client-side throttle). Verified via
  web search that no public evidence (as of 2026-05-09) suggests Google
  has shortened `*PSIDTS` rotation below the historical 600 s cadence;
  that note is captured inline in §2.2. Renumbered all sections from
  the old §2 onward (§2 → §3, …, §11 → §12), and updated the few §-
  cross-references in body text. No semantic changes to §3–§12 content.
- **2026-05-09 (rev 4)** — Added §3.4 *Internal threats: cookie-jar
  fidelity in the persistence pipeline*. Documents six fidelity hazards
  in `auth.py` with file:line references, the most important being
  §3.4.1 — a stale-overwrites-fresh race that the post-#344 cross-
  process flock does **not** cover. Verified via librarian survey of
  peer projects (Gemini-API, Bard-API, ytmusicapi, gpsoauth,
  CookieCloud, browser_cookie3, rookiepy) that none of them defend
  against this pattern either; HanaokaYuzu/Gemini-API
  ([client.py#L275-L306](https://github.com/HanaokaYuzu/Gemini-API/blob/fbe0790599ac8ee77692dabdce88a96110a33294/src/gemini_webapi/client.py#L275-L306))
  is more vulnerable than us (no flock, full overwrite on `close()`).
  §3.4.7 adds a diagnostic checklist for "cookies expire fast" reports
  that walks internal-causes-first before assuming Google changed
  anything — relevant to triaging the hour-scale-survival pattern in
  Gemini-API [#203](https://github.com/HanaokaYuzu/Gemini-API/issues/203)
  and similar reports.
