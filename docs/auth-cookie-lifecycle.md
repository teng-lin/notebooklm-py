# Auth cookie lifecycle — design notes and field findings

**Last Updated:** 2026-07-04

> **Status:** current design notes for the auth refresh stack in `main`.
> The numbered recovery ladder below is the canonical taxonomy, shared with
> [docs/troubleshooting.md](troubleshooting.md#authentication-errors):
>
> - **L1** — per-call `RotateCookies` POST (default ON)
> - **L2** — periodic background keepalive (`keepalive=N` client kwarg)
> - **L3** — headless re-auth from a persisted browser profile / loopback CDP
> - **L4** — **master-token re-mint** (`[headless]` extra; no browser, fully automatic)
> - **L5** — `NOTEBOOKLM_REFRESH_CMD` external recovery script
> - **L6** — manual `notebooklm login`
> - **L7** — OS-scheduled `notebooklm auth refresh` (cron / launchd / systemd)
>
> For long-lived, unattended, headless, server, or CI use, **L4 master-token
> re-mint is the recommended path** — see [Recommended setup](#recommended-setup).
> Deep field notes, ruled-out experiments, and the internal-persistence hazard
> log have been moved to the [Appendix](#appendix-field-notes--historical-findings)
> so the body stays focused on durable mechanics.

## TL;DR

NotebookLM has no public OAuth surface. The library authenticates by carrying
Google session cookies (`SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `OSID`, and
friends) extracted from a real browser sign-in. Two clocks govern their validity:

- **`__Secure-1PSIDTS` has a *recommended* rotation cadence of ~600 s**
  (self-reported by Google as `["identity.hfcr",600]` on the `RotateCookies`
  response). This is a *hint*, not a hard rejection TTL: the prior value keeps
  authenticating far longer — commonly hours to days on a stable IP / non-Workspace
  account. Worst-case profiles (datacenter egress, cross-IP, Workspace policy,
  incomplete extraction) can collapse that to hours or less.
- **`SID` and `__Secure-1PSID`** have very long server-side lifetimes (months to
  years) and effectively don't expire under normal usage.
- **Cookie set completeness matters more than freshness.** Google rejects cookie
  sets missing `__Secure-1PSIDTS` together with any one other cookie, even though
  removing `__Secure-1PSIDTS` alone is recoverable — see
  [§3.3](#33-empirical-cookie-requirements).

A long-lived client must therefore drive `*PSIDTS` rotation itself. The cleanest
mechanism is a direct `POST` to `https://accounts.google.com/RotateCookies` —
Google's dedicated unsigned rotation endpoint, the **L1** primitive at the bottom
of a tiered recovery design that escalates as failure modes get harder.

The recovery ladder runs cheapest-to-heaviest — **L1** per-call `RotateCookies`
POST, **L2** background keepalive, **L3** headless re-auth / loopback CDP, **L4**
master-token re-mint, **L5** `NOTEBOOKLM_REFRESH_CMD`, **L6** manual `notebooklm
login`, **L7** scheduled `notebooklm auth refresh` (the same taxonomy as
[troubleshooting.md](troubleshooting.md#authentication-errors); per-layer detail in
[§4](#4--the-recovery-ladder)). L1/L2 keep a live session fresh but can't revive a
dead one; L3 needs the profile's browser session still alive; **L4 is the only
fully-automatic layer that revives a fully-expired session with no browser.**

**L4 (master-token re-mint) is the standout for headless/unattended use.** Unlike
L3 it needs no browser at refresh time, and unlike L5/L6 it is fully automatic.
One durable master token — one human sign-in, then good for months — re-mints web
cookies on demand and self-heals an expired session in-process, coalesced through
the `AuthRefreshCoordinator` single-flight. See [§4.4](#44-l4--master-token-re-mint)
and [ADR-0023](adr/0023-master-token-headless-auth.md).

`NOTEBOOKLM_REFRESH_CMD` ([#336](https://github.com/teng-lin/notebooklm-py/pull/336))
is a complementary, reactive hook: it runs a user-supplied recovery command on
auth-expiry signals, then retries the token fetch once. It is **orthogonal to
L1–L4** — those proactively keep the session fresh or re-mint it in-process, while
`NOTEBOOKLM_REFRESH_CMD` is the "we lost the session anyway, run my recovery
script" lever. See [§6.2](#62-notebooklm_refresh_cmdcommand-line-l5).

L1 works today on every account type tested. Long-running Python workers should
add L2; unattended/headless/server/CI deployments should adopt L4; idle profiles
between processes can add L7. If Google extends DBSC enforcement to non-Chrome
cookie paths, L3's CDP arm becomes the primary browser-backed recovery path.

---

## Available auth methods

There are five ways to give the library credentials. Pick by deployment shape;
they compose (e.g. `--master-token` for the durable credential plus
`NOTEBOOKLM_REFRESH_CMD` as a belt-and-suspenders reactive hook).

| Method | Command / env | Best for | Survives cookie expiry unattended? | Setup cost |
|---|---|---|:-:|---|
| **(a) Interactive login** | `notebooklm login` (Playwright Google sign-in into a private Chromium profile) | Desktop / interactive use | No — re-login when prompted | Low (one browser sign-in) |
| **(b) Browser-cookie reuse** | `notebooklm login --browser-cookies <browser>` (rookiepy extraction from an existing profile) | Reusing a browser you already sign into | Only while the source browser session stays alive; pairs with L7 cron | Low (no interaction) |
| **(c) Master token** ⭐ | `notebooklm login --master-token` (`[headless]` extra; durable token, headless L4 re-mint) | **Servers / CI / unattended / headless** | **Yes** — re-mints automatically, no browser | Medium (one bootstrap sign-in, ship `master_token.json`) |
| **(d) Inline auth JSON** | `NOTEBOOKLM_AUTH_JSON=<storage_state payload>` | CI / ephemeral containers with no on-disk profile | No — env-var auth has no writeable file, so L3/L4 decline | Low (paste a secret) |
| **(e) External refresh hook** | `NOTEBOOKLM_REFRESH_CMD=<command>` | Custom recovery (CookieCloud pull, browser re-extract) layered on any of the above | Depends on the script | Medium (write + secure a script) |

Notes: **(c) is the recommended default for long-lived headless use** — the only
method that both survives full cookie expiry *and* needs no browser at refresh
time. **(d) `NOTEBOOKLM_AUTH_JSON`** carries a full `storage_state.json` payload
inline (credential-equivalent) with no backing file, so the file-backed recovery
layers (L3/L4) decline — good for short-lived CI jobs, but pair with (c) or (e) for
anything long-running. **(e) `NOTEBOOKLM_REFRESH_CMD`** is not a credential source
on its own; it is the reactive hook that fires after auth has already expired
([§6.2](#62-notebooklm_refresh_cmdcommand-line-l5)).

---

## Recommended setup

### Interactive desktop user

Just `notebooklm login`. The Playwright Chromium flow handles it; re-login when
prompted (typically days to weeks between prompts).

### Long-lived in-process client (agent, MCP server, worker)

```python
async with NotebookLMClient.from_storage(keepalive=600) as client:
    ...
```

L1 fires on `from_storage()`; L2 fires every 600 s while the client is open. This
keeps `*PSIDTS` rotating for as long as the process lives.

### Unattended / headless / server / CI — use the master token (L4)

This is the recommended path. No browser at refresh time, survives full cookie
expiry, and no `storage_state.json` to keep re-shipping.

1. `pip install "notebooklm-py[headless]"`.
2. One-time, on a machine with a browser (dedicated / throwaway account):
   `notebooklm -p <profile> login --master-token --account you@gmail.com`.
3. Ship the bootstrapped profile — **both** `master_token.json` and the
   `storage_state.json` the bootstrap just minted (each `0600`) — to the server.
   (A clean server with *only* `master_token.json` and no `storage_state.json`
   needs one `notebooklm -p <profile> auth refresh` to mint and passively
   validate the initial cookies; shipping both skips that step.)
4. Run commands normally. Cookies are minted on bootstrap and **re-minted
   automatically** when the session dies (L4, [§4.4](#44-l4--master-token-re-mint));
   request conditional recovery with `notebooklm -p <profile> auth refresh`, or
   use the legacy forced route
   `notebooklm -p <profile> login --master-token-refresh` when specifically needed.

Caveats: the master token is a full-account, infostealer-grade credential — use a
dedicated account, keep the file `0600`, never log or commit it. One account is
**single-consumer**: N workers re-minting concurrently can invalidate each other's
`SID`. The master token inherits the standing DBSC risk (server-minted cookies
could be rejected if enforcement extends to this path), but re-mint is itself the
mitigation while it isn't enforced. See
[ADR-0023](adr/0023-master-token-headless-auth.md) and
[installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci).

### Unattended without the `[headless]` extra — browser-cookie extract + cron

If you can't or won't use a master token, extract from a real browser and refresh
on a schedule:

1. Sign in to NotebookLM once in Firefox (or any rookiepy-supported browser).
2. `notebooklm -p <profile> login --browser-cookies firefox`.
3. Schedule L7: `7,27,47 */1 * * * notebooklm --profile <profile> auth refresh`
   (off-minute schedule avoids fleet collision).
4. Keeping the source browser running with a Google tab adds resilience, but even
   a closed browser works for hours-to-days while `RotateCookies` keeps
   succeeding from `SID` alone.

> **Browser support:** `--browser-cookies` accepts any of the ~16 browsers rookiepy
> reads on the host (`arc`, `brave`, `chrome`, `edge`, `firefox`, `opera`, `safari`,
> `vivaldi`, …; see `_ROOKIEPY_BROWSER_ALIASES` in
> `cli/services/login/cookie_jar.py`). **Firefox is the recommended path on
> Windows** because Chrome 127+ App-Bound Encryption makes Chrome reads
> admin-or-bust. Scope a Firefox Multi-Account Container with
> `firefox::<container-name>` (unscoped extraction merges every container and can
> pick the wrong session); scope a Chromium profile with `chrome::<profile>`.

### Workspace / Enterprise with admin session-binding

Currently **not supported.** Admin-policy session binding is a Workspace beta that
requires DBSC-compatible flows. Request an exemption from your admin or use a
personal Google account for automation.

---

## 1 · Problem statement

NotebookLM uses Google's internal `batchexecute` RPC. There is no documented API
key, no OAuth scope, no service account path. Every project that automates
NotebookLM does so with **scraped session cookies** from a logged-in browser. The
library exposes those via `notebooklm login` (Playwright-driven Google sign-in
into a private Chromium profile) and `notebooklm login --browser-cookies <browser>`
(rookiepy-driven extraction from an existing profile). Both produce a
`storage_state.json` that authenticates every subsequent RPC.

The keepalive question is: **what keeps `storage_state.json` valid between
user-driven re-authentications?** The naïve "cookies have expiry timestamps; trust
them" answer is wrong on two counts: the most consequential cookie
(`__Secure-1PSIDTS`) has a server-side recommended rotation cadence not encoded in
its `Expires` attribute (the on-disk `Expires` is irrelevant to server-side
validity), and even cookies with a year-long `Expires` are **revoked early by
Google's risk model** when the access pattern looks unusual (no JS, no fingerprint,
IP changes, long idle gaps). So the library must actively refresh.

---

## 2 · Background: Google session auth, rotation, and DBSC

Vocabulary the rest of the doc uses. Skip to [§3](#3--threat-model) if you've
already spent time inside Google's identity surface.

### 2.1 The cookie taxonomy

Google authenticates a browser session with a **family of ~15 cookies**, not a
single bearer token. Each cookie has a distinct role; the family is designed so
revoking or rotating any one slot doesn't invalidate the others. The set is shared
across `*.google.com` properties — Search, Drive, Gmail, NotebookLM, YouTube,
Workspace — which is why a sign-in to any one produces auth artifacts the rest of
the ecosystem accepts.

Naming conventions:

- **`__Secure-` prefix.** The cookie's `Secure` attribute must be set, so it's
  never sent over plaintext HTTP. Google sets this on every meaningful auth cookie.
- **`__Host-` prefix.** Stricter: the cookie must also set `Path=/`, must not set
  `Domain=` (pinned to the exact issuing origin), and must be `Secure`. Used for
  the most scope-sensitive cookies (`__Host-GAPS`, `__Host-1PLSID`, …).
- **`1P` vs `3P`.** First-party vs third-party context. `__Secure-1PSID` is used
  when the request originates from a `*.google.com` page; `__Secure-3PSID` is the
  variant Google sends on third-party pages that embed Google content. They rotate
  independently. We typically need both because intermediate rotation redirects
  cross the 1P/3P boundary.
- **`*SID` / `*SIDTS` / `*SIDCC`.** Three cookie *families* that separate
  **identity** (who you are, slow to change) from **freshness** (you're using the
  session right now, fast to expire):

  | Family | Role | Recommended rotation cadence | Stale-value validity |
  |---|---|---|---|
  | `*SID` (`SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, …) | Long-lived identity | Months → ~1 year | Practically never expires for active accounts |
  | `*SIDTS` (`__Secure-1PSIDTS`, `__Secure-3PSIDTS`) | Rotating freshness partner of `*SID` | **~600 s** (Google's self-report) | Hours-to-days on a stable IP / non-Workspace profile |
  | `*SIDCC` (`SIDCC`, `__Secure-1PSIDCC`, …) | Per-request "session continuity check" | Issued on every request | Not enforced for accept/reject |

A few cookies sit outside this taxonomy:

- **`OSID`, `__Secure-OSID`** — per-product session, set on `notebooklm.google.com`
  and `myaccount.google.com`. Re-issued on each sign-in.
- **`LSID`, `__Host-1PLSID`, `__Host-3PLSID`** — identity-service cookies on
  `accounts.google.com`. Long-lived.
- **`__Host-GAPS`** — anti-takeover binding cookie. Long-lived; part of how Google
  detects suspicious cross-device reuse.

The library treats these uniformly: extract the full set at sign-in, persist them
in `storage_state.json`, replay them on every RPC.
`_is_allowed_cookie_domain` (in `_auth/cookie_policy.py`, re-exported through
`auth.py`) gates which `Set-Cookie` headers from a redirect chain are worth
keeping, matching against `ALLOWED_COOKIE_DOMAINS` plus the regional
`google.<cctld>` set.

### 2.2 How cookie rotation works

"Rotation" means: the server periodically issues a new value for a short-lived
cookie (`Set-Cookie: __Secure-1PSIDTS=<fresh>; …`), and the browser is expected to
overwrite its on-disk copy. If the client falls behind, the server eventually
stops accepting the old value and the session is dead until re-auth.

Two clocks run in parallel:

- The **identity clock** (`*SID`) ticks in months. Google extends it silently as
  long as it sees activity; for a daily-active user it effectively never expires.
- The **freshness clock** (`*PSIDTS`) has a recommended rotation cadence of ~600 s,
  self-reported in the `RotateCookies` response body as `["identity.hfcr",600]`
  (`hfcr` = "high-frequency cookie rotation"; `600` = seconds). This is a rotation
  *hint*, not a hard expiration: stale values keep working for hours or days
  depending on server-side state, but long-idle sessions eventually drift into
  sign-in redirects.

Rotation is **server-driven**: the client posts to a rotation endpoint; the server
inspects the existing `*SID` (and optionally a DBSC proof — see §2.3) and returns a
fresh `*PSIDTS`. The client only chooses *when* to fire.

**Crucially: pure RPC traffic against `notebooklm.google.com` does not trigger
rotation.** `batchexecute` accepts the existing cookies, but Google only mints a
fresh `*PSIDTS` when something talks to the *identity* surface (`accounts.google.com`,
the NotebookLM homepage GET, the `RotateCookies` POST). A client that only calls
`batchexecute` silently drifts past the rotation window and starts failing — exactly
what L1/L2 target. We use `RotateCookies` because it rotates deterministically for
both browser-bound and Firefox-extracted sessions (see
[Appendix](#appendix-field-notes--historical-findings)).

### 2.3 Device-Bound Session Credentials (DBSC)

DBSC is Google's response to **infostealer cookie theft**: malware exfiltrates the
cookie jar and an attacker replays it from another machine. DBSC binds a session to
**a private key in tamper-resistant hardware** (TPM / Secure Enclave / Strongbox) on
the original device — the browser generates a non-extractable keypair at sign-in and
registers the public key, then signs a server nonce on every rotation. The enforcing
endpoint is **`accounts.google.com/RotateBoundCookies`**, the bound-cookie analog of
the unsigned `RotateCookies` we use; an attacker who steals the jar can't sign the
next bound rotation, so the stolen session dies instead of renewing. The
[W3C spec](https://w3c.github.io/webappsec-dbsc/) is deliberately structured so only
hardware-attesting browsers can implement it — no Python HTTP client can, and no
public OSS DBSC client exists outside Chrome (see [A3](#a3--ruled-out-experiments)).

**Current enforcement state.** DBSC is rolling out. Enforcement currently targets
**Chrome itself** — Chrome refuses to use cookies that weren't bound at sign-in,
even on the same machine. Non-Chrome HTTP clients (httpx, curl, Firefox) can still
hit the legacy unsigned `RotateCookies` endpoint without a DBSC proof, so every
HTTP-only strategy in this document works today. The day Google extends enforcement
to that endpoint, they break together; the in-tree escape is to parasitize a real
DBSC-enrolled Chrome session through the L3 CDP attach arm, or to source cookies
via an operator-provided `NOTEBOOKLM_REFRESH_CMD` (e.g. CookieCloud federation).
See [§7 canaries](#7--canaries-and-signals) for the tripwires that would signal the
transition.

### 2.4 How browser-cookie extraction works

`notebooklm login --browser-cookies <browser>` reads cookies directly out of an
installed browser's profile rather than minting fresh ones via Playwright. It is a
**variant of manual login (L6)** and a common backing command for
`NOTEBOOKLM_REFRESH_CMD` (L5) — it is **not** a recovery layer of its own, and in
particular it is not L4 (L4 is the master token).

Browsers store cookies in encrypted SQLite databases, with the decryption key in the
OS credential store (Keychain / DPAPI / libsecret). **Chrome 127+ adds App-Bound
Encryption (ABE)** — a second layer bound to Chrome's signed binary that defeats
user-space readers; `browser_cookie3` doesn't handle it and `rookiepy` needs admin
from Chrome 130+ ([rookie#50](https://github.com/thewh1teagle/rookie/issues/50)).
**Firefox has no ABE** (Mozilla treats local file-access attackers as out-of-scope),
so its cookies stay readable by any user-space process — hence the Windows Firefox
recommendation. The library uses `rookiepy` (~16 browsers) and reshapes the result
via `_auth/cookies.py::convert_rookiepy_cookies_to_storage_state` into a
Playwright-compatible `storage_state.json`, indistinguishable downstream from a
Playwright-minted one. Extraction asks for the full multi-domain set
(`ALLOWED_COOKIE_DOMAINS + GOOGLE_REGIONAL_CCTLDS`) because dropping any one breaks
specific paths (e.g. losing `.notebooklm.google.com` cookies breaks artifact
downloads).

### 2.5 Three timers people confuse

| Timer | Magnitude | Lives in | Meaning |
|---|---|---|---|
| **`*PSIDTS` rotation cadence** | ~600 s | Google's identity surface | Recommended active-client refresh interval (`["identity.hfcr",600]`). Not a hard rejection TTL — prior values stay valid much longer on stable profiles. |
| **`*SIDCC` sliding window** | ~5 min | Google's RPC surface | A different cookie family; rotates on nearly every request; not load-bearing for our auth. |
| **Client-side rotation throttle** | 60 s | `_auth/keepalive.py` | Don't fire two `RotateCookies` POSTs within a minute (avoids 429). Unrelated to how often Google *requires* rotation. |

Reports that "cookies are expiring faster" usually trace to the session entering a
risk-flagged state (§3.1) or to the rotation mechanism failing until `*SID` finally
ages out — not to a shorter hard rejection TTL.

### 2.6 Domain tiering: REQUIRED vs OPTIONAL cookie domains

Not every Google cookie a logged-in browser holds is load-bearing for NotebookLM.
The library splits the cookie-source domain list into two tiers
(`_auth/cookie_policy.py`):

| Tier | Constant | Domains | Extracted by default |
|---|---|---|:-:|
| **REQUIRED** | `REQUIRED_COOKIE_DOMAINS` | `.google.com`, `notebooklm.google.com` (+ regional ccTLDs), `accounts.google.com`, `.googleusercontent.com`, `drive.google.com` | ✅ |
| **OPTIONAL** | `OPTIONAL_COOKIE_DOMAINS_BY_LABEL` | `youtube`, `docs`, `myaccount`, `mail` | ❌ (opt-in via `--include-domains=<label>[,…]` or `=all`) |

The REQUIRED tier is exactly the set traced through every exercised code path (API
host, identity carriers, authenticated media downloads, Drive-source ingest);
removing any one breaks an observed flow. **Data minimization** motivates the split:
`storage_state.json` is a high-value target, and the OPTIONAL tier carries cookies
that would let an attacker read Gmail / Drive / YouTube — none of which any
NotebookLM path needs. The control is enforced at **extraction time** (what
`rookiepy.load(domains=...)` is asked for), so excluded cookies are never written to
disk ([#483](https://github.com/teng-lin/notebooklm-py/pull/483)). Opt in only when a
sibling flow needs it — e.g.
`notebooklm login --browser-cookies firefox --include-domains=youtube,docs`.

---

## 3 · Threat model

### 3.1 What kills a session in practice

In rough order of likelihood:

1. **`*PSIDTS` rotation drift.** Cookies on disk go stale because nothing rotates
   them. Any RPC after the grace period fails with a redirect to
   `accounts.google.com/v3/signin/…`. **The dominant failure mode for unattended
   use** — and the one the recovery ladder exists to defeat.
2. **Risk-scored revalidation.** Google flags the access pattern (new IP, no
   fingerprint, suspicious cadence, geography mismatch) and forces full re-auth.
   Less predictable; days-to-weeks into a long-running deployment.
3. **Password change or manual sign-out** anywhere — invalidates all sessions
   instantly.
4. **Workspace policy timeouts.** Some org admins enforce re-auth intervals; varies
   by tenant.
5. **DBSC enforcement (emerging).** See §2.3. Does not affect non-Chrome HTTP
   clients today; the long-term threat.

Cookie decay clocks by class:

| Cookie | Rotation / expiry signal | Lifecycle |
|---|---|---|
| `__Secure-1PSIDTS` / `*-3PSIDTS` | Recommended cadence ~600 s (`["identity.hfcr",600]`); not a hard TTL | Refreshed opportunistically; stale values work for hours-to-days, then drift into sign-in redirects |
| `SIDCC` / `__Secure-*SIDCC` | ~5 min sliding window | Ephemeral; generally not load-bearing for auth |
| `SID`, `HSID`, `SSID`, `APISID`, `SAPISID` (+ `__Secure-` cousins) | Months → ~1 year | Long-lived identity; not rotated by us |
| `OSID`, `__Secure-OSID` | Per-product session | Re-issued on each sign-in |
| `LSID`, `__Host-*LSID`, `__Host-GAPS` | Long-lived | Identity-service / anti-takeover cookies |

### 3.2 Internal persistence hazards (pointer)

A separate failure class is easy to misattribute to Google: the library corrupting
its own cookie state during the read-merge-write cycle. Historically several such
hazards existed (a stale-in-memory-clobbers-fresh-disk race, `(name, domain)`
path-collapse, sibling-domain allow-list asymmetry, round-trip attribute erosion).
**All of them are resolved in-tree** — the persistence path is now snapshot/delta,
CAS-guarded, cross-process flocked, and fully `(name, domain, path)`-aware. If users
report cookies "expiring fast", walk the
[diagnostic checklist](#a2--diagnosing-cookies-expire-fast) in the Appendix (which
also records the historical hazards and their fixes) before assuming Google changed
anything.

### 3.3 Empirical cookie requirements

Which cookies does Google *actually* require? This backs the library's two-tier
`_validate_required_cookies()` pre-flight (see `_auth/cookies.py` —
`MINIMUM_REQUIRED_COOKIES` and `_has_valid_secondary_binding()` for the
authoritative values; the historical permissive `{"SID"}` check was replaced in
[#371](https://github.com/teng-lin/notebooklm-py/issues/371)).

Method: take a known-good `storage_state.json`, drop one or two cookies at a time,
run `notebooklm list`, and record whether Google accepts the call or redirects to
login. Single-cookie removal is highly recoverable — every cookie except `SID` can
be dropped individually with the call still succeeding (Google reissues most of
them mid-call). Pair-wise removal exposes a precise accept-rule.

**The accept-rule model.** Google accepts the NotebookLM homepage GET when both
hold:

1. **Identity present:** `SID` is valid, and `__Secure-1PSIDTS` is either directly
   present or recoverable via a `RotateCookies` POST — which itself requires the
   full ambient cookie set to authenticate.
2. **At least one secondary binding present:** `OSID`, OR both `APISID` and
   `SAPISID`.

| Variant | `SID` | `OSID` | `APISID+SAPISID` | `__Secure-1PSIDTS` (or recoverable) | Result |
|---|:-:|:-:|:-:|:-:|:-:|
| Baseline | ✓ | ✓ | ✓ | ✓ | OK |
| Drop `__Secure-1PSIDTS` only | ✓ | ✓ | ✓ | recoverable | OK |
| Drop `__Secure-1PSIDTS` + any one other | ✓ | ✓ | ✓ | broken (mint POST fails) | FAIL |
| Drop `OSID` only | ✓ | ✗ | ✓ | ✓ | OK (AP\*SID path) |
| Drop `APISID + SAPISID` | ✓ | ✓ | ✗ | ✓ | OK (OSID path) |
| Drop `APISID + OSID` | ✓ | ✗ | ✗ | ✓ | FAIL |
| Drop `SAPISID + OSID` | ✓ | ✗ | ✗ | ✓ | FAIL |

Before #371 the library trusted any storage with `SID` present, which let
Google-rejected cookie sets reach the wire — the "auth expires immediately after
`notebooklm login`" pattern
([#133](https://github.com/teng-lin/notebooklm-py/issues/133),
[#332](https://github.com/teng-lin/notebooklm-py/issues/332)). The pre-flight now
catches it with a two-tier check:

```python
MINIMUM_REQUIRED_COOKIES = {"SID", "__Secure-1PSIDTS"}  # Tier 1: raise

def _has_valid_secondary_binding(cookie_names: set[str]) -> bool:  # Tier 2: warn
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID"} <= cookie_names
```

Tier 1 raises on unambiguous evidence; Tier 2 warns once per process so partial
extractions surface without breaking edge-case flows (e.g. Workspace SSO) that
haven't been ablated.

**Caveats.** These observations came from a single non-Workspace, stable-IP
profile, testing `notebooks.list`. Workspace accounts may have different
accept-rules. This is a model fit, not a confirmed server mechanism, and the
freshness clock (§3.1) still applies on top of it — a session with a valid
accept-tuple can still be killed by Google's risk model.

---

## 4 · The recovery ladder

The library escalates progressively as cheaper mechanisms fail (see the ladder
table in the [TL;DR](#tldr)). Each layer is a fallback for the one below it: L1/L2
are HTTP-only and cheap; L3 drives a browser and only helps if the profile's Google
session is still alive; **L4 re-mints from a durable master token with no browser —
the best unattended recovery**; L5 delegates policy to the operator; L6 is manual;
L7 is proactive scheduling for idle profiles.

### 4.1 L1 — per-call `RotateCookies` POST

Fires inside the token-fetch path on every CLI invocation and client open. A best-
effort `POST https://accounts.google.com/RotateCookies` that mints a fresh
`*PSIDTS`. Default ON; disable with `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`. Wrapped
in three concentric guards (disk-mtime fast-path → in-process `asyncio.Lock` +
per-profile monotonic timestamp → cross-process non-blocking flock) so an L1 caller,
an L2 loop, and a fan-out of parallel CLI invocations keyed to the same
`storage_state.json` don't stampede the endpoint into a 429. Mechanics in
[§5](#5--the-rotatecookies-primitive).

### 4.2 L2 — background keepalive task

`NotebookLMClient(keepalive=N)` starts an `asyncio.Task` that pokes
`RotateCookies` every N seconds (floor 60 s) while the client is open. Self-paced,
so it bypasses the L1 fast-path guards but still performs the atomic per-profile
claim, so a sibling L1 poke sees the in-flight rotation and skips. Covers agents,
MCP servers, and long-running workers.

### 4.3 L3 — headless re-auth / CDP attach

When the homepage GET 302s to the Google login page, the first-party cookies are
fully dead and neither L1 nor L2 can help. `from_storage(allow_headless=True)`,
`refresh_auth(allow_headless=True)`, `auth refresh --allow-headless`, or
`NOTEBOOKLM_HEADLESS_REAUTH=1` drives an unattended
headless browser against the **persisted profile that is a sibling of this client's
storage file** — `browser_profile/` beside a conventional `storage_state.json`,
or `<storage_path>.browser_profile/` for a normal-length custom filename; names
that would exceed the filesystem component limit use a stable hash of the
canonical storage path — never the ambient profile. This lets two custom storage
files in the same directory retain separate Google sessions. The ownership marker
written during path preparation is ignored when deciding whether the directory
contains reusable Chrome state. L3 silently re-mints cookies, reloads them, and
retries the homepage GET once. Set
`NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL=http://127.0.0.1:9222` to attach
to an already-running local Chrome instead; non-loopback hosts are refused because
a CDP endpoint is account-equivalent. If the profile is missing, Playwright is
unavailable, env-var auth has no writeable file, or the browser session is also
dead, the original auth-expiry error stands. Owner:
`_auth/headless_reauth.py`; shared cold/mid-session adapters and cold
single-flight coordination live in `_auth/recovery.py`.

### 4.4 L4 — master-token re-mint

When the profile holds a `master_token.json` (written by
`notebooklm login --master-token`, the `[headless]` extra), a fully-expired session
re-mints **in process, with no browser** — the recovery the `RotateCookies` /
headless-browser ladder can't provide off-device.

- **Credential.** A durable Google **master token** (`aas_et/…`), obtained once from
  `accounts.google.com/EmbeddedSetup` and stored `0600`. It mints fresh web cookies
  on demand (`perform_oauth → OAuthLogin?issueuberauth=1 → MergeSession`) and
  survives password changes until explicitly revoked. It also bootstraps the initial
  `storage_state.json`.
- **Where it fires.** `_auth/recovery.py::try_master_token_reauth`, as layer 4
  after L1 (homepage), L2 (`RotateCookies`), and L3 (headless browser) are
  exhausted. Both cold token loading and mid-session `refresh_auth_session`
  delegate to this adapter. It mints a new session, persists it under the
  storage lock, reloads the jar, and retries the homepage GET once. Equivalent
  same-loop cold callers share one complete recovery task; live-client RPCs
  retain their `AuthRefreshCoordinator` single-flight.
- **Cold start.** The normal file-backed token loader invokes the same L4 adapter
  after a confirmed login redirect, so a session already dead at process start
  recovers without a forced pre-mint.
- **PSIDTS interaction.** A re-mint yields `SID`+`APISID`+`SAPISID` but not
  `__Secure-1PSIDTS`; the mint itself fires one best-effort `RotateCookies` POST to
  add it, and the inline PSIDTS recovery
  ([§5.4](#54-inline-__secure-1psidts-cold-start-recovery)) mints it from the
  secondary binding on reload if Google withheld it — so L1 keepalive then proceeds
  normally on the fresh session.
- **Security & limits.** The master token is full-account and infostealer-grade —
  dedicated/throwaway account only, never logged or committed. Each re-mint is a new
  session, so one account is **single-consumer**: concurrent re-mints can invalidate
  each other's `SID`. DBSC is the standing risk; re-mint is itself the mitigation
  while it isn't enforced. See [ADR-0023](adr/0023-master-token-headless-auth.md).

### 4.5 L5–L7

- **L5 — `NOTEBOOKLM_REFRESH_CMD`.** An operator-supplied recovery command run on
  an auth-expiry signal, with one retry. Same-loop callers coalesce on one
  subprocess and cancellation-safe. See [§6.2](#62-notebooklm_refresh_cmdcommand-line-l5).
- **L6 — `notebooklm login`.** Baseline manual recovery: interactive browser
  sign-in, or `--browser-cookies <browser>` extraction (§2.4).
- **L7 — `notebooklm auth refresh`.** A one-shot token fetch driven by cron /
  launchd / systemd / Task Scheduler / k8s CronJob, for profiles idle between Python
  runs. Recommended cadence 15–20 min.

---

## 5 · The `RotateCookies` primitive

The L1/L2 rotation POST and the L4 re-mint's PSIDTS top-up all share one endpoint.

### 5.1 The endpoint

```
POST https://accounts.google.com/RotateCookies
Content-Type: application/json
Origin: https://accounts.google.com

[000,"-0000000000000000000"]
```

The body is a JSPB (array-shaped) sentinel. `000` is `0` written with leading zeros
(valid in Google's JSPB parser, invalid in strict JSON); `"-0000000000000000000"`
is a sentinel meaning "I have no prior `__Secure-1PSIDTS`, mint a fresh one from the
persistent identity (`SID`/`PSID`) alone." The pattern is borrowed from
[`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/utils/rotate_1psidts.py),
which has run it in production at scale.

### 5.2 The successful response

```
HTTP/1.1 200 OK
Set-Cookie: __Secure-1PSIDTS=<new>; Domain=.google.com; Secure; HttpOnly
Set-Cookie: __Secure-3PSIDTS=<new>; Domain=.google.com; Secure; HttpOnly
Set-Cookie: SIDCC=<new>; Domain=.google.com; Secure
…

)]}'  [["identity.hfcr",600],["di",<integer>]]
```

`)]}'` is Google's anti-XSSI prefix. `["identity.hfcr",600]` declares the
recommended next-rotation interval (600 s); `["di",N]` is an opaque session counter.
`_auth/storage.py::save_cookies_to_storage` captures the rotated `Set-Cookie`
headers and persists them atomically.

Why `RotateCookies` and not the older `CheckCookie` GET: `RotateCookies` rotates
`*PSIDTS` unconditionally for **both** browser-bound (Playwright) and unbound
(Firefox-extracted) sessions, whereas `CheckCookie` only rotated for unbound ones.
Historical detail in the [Appendix](#a1--rotatecookies-vs-checkcookie).

### 5.3 Rate limiting and the three-guard throttle

Hammering `RotateCookies` triggers HTTP 429. The mitigation is a 60-second floor,
enforced by three concentric guards (`_auth/keepalive.py`,
[#346](https://github.com/teng-lin/notebooklm-py/pull/346) +
[#348](https://github.com/teng-lin/notebooklm-py/pull/348)):

1. **Disk-mtime fast-path** — skip without any lock if `storage_state.json` was
   rewritten within `_KEEPALIVE_RATE_LIMIT_SECONDS` (60 s). A 2 s tolerance absorbs
   filesystem mtime granularity; a far-future mtime is treated as not-recent.
2. **In-process throttle** — inside an `asyncio.Lock` keyed by
   `(running loop, storage_path)`, re-check mtime plus a per-profile monotonic
   timestamp stamped under a `threading.Lock`. Deduplicates an `asyncio.gather`
   fan-out; the timestamp is bumped *before* the network await so a hung
   `accounts.google.com` doesn't make N callers each wait the full timeout.
3. **Cross-process non-blocking flock** (`.storage_state.json.rotate.lock` via
   `LOCK_NB`) — if another process holds it, skip; they're rotating now. Distinct
   from the save-path lock so a long save never blocks rotations. Locks **fail
   open** on read-only / NFS filesystems: rotation proceeds rather than wedging.

Together the three guards cover sequential CLI invocations (mtime fast-path), an
`asyncio.gather` fan-out from one process (in-process lock + stamp), an L1 caller
racing the L2 loop (per-profile monotonic stamp), and simultaneous processes
(cross-process flock). The per-`(loop, profile)` lock dict is a `WeakKeyDictionary`
keyed on the loop object, so a short-lived `asyncio.run()` loop's inner dict is
reclaimed on GC.

### 5.4 Inline `__Secure-1PSIDTS` cold-start recovery

When a profile has the persistent `__Secure-1PSID` but no transient
`__Secure-1PSIDTS` (a common cold-start snapshot), `_recover_psidts_inline`
(`_auth/psidts_recovery.py`) makes a preflight `RotateCookies` POST during client
startup to mint the missing cookie before the first RPC. It fires only when `SID`
is present, a secondary binding is intact (`OSID`, or `APISID` + `SAPISID`), and
no live `__Secure-1PSIDTS` would be **sent to** `accounts.google.com` — that is,
the cookie is absent, expired, or scoped to a domain that does not route to the
rotate URL. That last condition is RFC 6265 selection against the URL the
decision is about, not a domain-priority ranking: a `__Secure-1PSIDTS` scoped to
`.notebooklm.google.com` (or, post-rebrand, `.notebook.google.com`) never reaches
`accounts.google.com`, while a host-scoped one on `accounts.google.com` does
(issue #2057).

The separate "did the heal land?" check (`_psidts_is_live`) is deliberately
domain-blind instead, because it must predict the retrying preflight rather than
the request jar. **Do not merge the two predicates.** Their differences are
intentional, and each one has a failure mode behind it:

| | should we fire? (`_psidts_routes_to_rotate`) | did it land? (`_psidts_is_live`) |
|---|---|---|
| domain | RFC 6265 routing to the rotate URL | blind — matches the preflight |
| duplicate `(name, domain, path)` | any expired twin disqualifies the identity, mirroring the jar's one-cookie-per-identity replacement | ignored |
| malformed `expires` | row cannot be sent, so it does not count → fire | row is on disk and the preflight will see it → counts as present |
| known-expired row | does not route | does not count (issue #1273) |
| empty `value` | skipped | skipped |

The duplicate rule is the sharp edge. Applying it to the heal check turns a
working session into a permanent failure loop: `save_cookies_to_storage`
CAS-matches the *first* stored row for an identity, so a stale same-identity
twin survives the save, and the heal would report failure on every load while
the preflight keeps passing. That twin is issue #1523's data shape — #1523 fixed
the producer, and nothing on the load/save path removes an existing one.

Note also that `_psidts_is_live` models `extract_cookies_from_storage`
specifically; the sibling loader `_build_httpx_cookies_from_storage_strict`
converts every row and can still reject a state it accepts.

Recovery honors
`NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`, and uses a cross-process flock
(`psidts_recovery.lock`) so concurrent cold-start processes don't fan out identical
recovery calls. This is what lets the L4 re-mint (which produces `SID` +
`APISID`/`SAPISID` but not `*PSIDTS`) heal into a complete jar on reload. See
[ADR-0013](adr/0013-composable-session-capabilities.md#consequences).

---

## 6 · Operational levers (environment variables)

Auth-refresh env vars live under `src/notebooklm/_auth/` and are re-exported through
the public `notebooklm.auth` facade where compatibility requires it. See also
[configuration.md#environment-variables](configuration.md#environment-variables).

### 6.1 `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`

Disables the `RotateCookies` POST entirely. Both L1 and L2 honor it (the L2 task
still wakes on its interval — only the network call becomes a no-op; pass
`keepalive=None` to stop the loop itself). Set it on restricted networks that block
outbound POSTs to `accounts.google.com`, for regression triage, or in test
environments that mock the auth surface.

### 6.2 `NOTEBOOKLM_REFRESH_CMD=<command-line>` (L5)

Reactive recovery hook (merged in
[#336](https://github.com/teng-lin/notebooklm-py/pull/336), hardened to
`shell=False` by default in
[#475](https://github.com/teng-lin/notebooklm-py/pull/475); owner
`_auth/refresh.py`). When token fetch fails with an auth-expiry signal (the
"`Authentication expired or invalid`" / `accounts.google.com` redirect), the
library:

1. Parses the command with `shlex.split` (POSIX) / `CommandLineToArgvW` (Windows)
   and runs it via `subprocess.run(argv, shell=False, …)` with a 60 s timeout. Set
   `NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1` to opt back into `shell=True` (a `WARNING`
   is logged each invocation).
2. Sets `NOTEBOOKLM_REFRESH_PROFILE` / `NOTEBOOKLM_REFRESH_STORAGE_PATH` in the
   child env so the script knows which profile to refresh.
3. Sets `_NOTEBOOKLM_REFRESH_ATTEMPTED=1` to prevent recursive refresh loops.
4. Scrubs `NOTEBOOKLM_AUTH_JSON` from the child env (credential-equivalent; the
   script gets the on-disk path via step 2 instead).
5. Reloads cookies from `storage_state.json` and replays the token fetch once.

> **SECURITY — inherited environment.** The refresh command inherits the **full
> parent environment** (minus the `NOTEBOOKLM_AUTH_JSON` scrub) so it can find
> `PATH`/`HOME`/proxy settings and re-invoke this library. There is deliberately no
> allowlist. Any other secret in the launching shell is inherited by the command and
> every grandchild, and is visible via `/proc/<pid>/environ` to the same UID.
> Operators MUST NOT keep unrelated secrets in the launching environment
> ([#1274](https://github.com/teng-lin/notebooklm-py/issues/1274)).

Same-loop fan-out coalesces on one shielded in-flight subprocess, so cancellation of
one caller doesn't cancel the shared command; cross-loop coalescing is best-effort
(cross-loop client reuse is unsupported per
[ADR-0004](adr/0004-loop-affinity-contract.md)).

This is **orthogonal to L1–L4**: those proactively keep the session fresh (L1/L2) or
re-mint it in-process (L3/L4), while `NOTEBOOKLM_REFRESH_CMD` runs only after auth
has already fully expired — useful for password-change / manual-sign-out recovery or
a custom CookieCloud / browser-cookie re-extract flow. Common shapes:

```bash
export NOTEBOOKLM_REFRESH_CMD='notebooklm login --browser-cookies firefox'
export NOTEBOOKLM_REFRESH_CMD='/opt/scripts/pull-cookies-from-cloud.sh'
```

The library does not validate the command's output; the operator must ensure it
produces a valid `storage_state.json`.

### 6.3 `NOTEBOOKLM_HEADLESS_REAUTH=1` and `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL` (L3)

Opt into automatic L3 headless re-auth during mid-RPC refresh (explicit
`await client.refresh_auth(allow_headless=True)` needs no env var). The CDP URL, if
set, attaches to an already-running loopback Chrome instead of launching the stored
profile; non-loopback hosts are refused. Details in [§4.3](#43-l3--headless-re-auth--cdp-attach).

---

## 7 · Canaries and signals

Tripwires that would signal the threat model shifting:

| Signal | What it means | Action |
|---|---|---|
| `RotateCookies` returns 401 in production | DBSC extended to non-Chrome paths for some accounts | Harden the L3 CDP arm; steer users to `NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL` or an L5 CookieCloud flow |
| `RotateCookies` returns 200 but no `*PSIDTS` in `Set-Cookie` | Silent failure — cookies on disk aren't rotating | WARN + alert; manual re-auth required |
| Gemini-API's bare-sentinel rotation reported decaying under DBSC | Upstream canary for the shared primitive | Assess whether our user base is affected; plan a mitigation |
| Chrome macOS DBSC GA announced | macOS users start getting DBSC enrollment | Several months' warning before consumer accounts may be enforced |
| Workspace session-binding leaves beta | More org admins will enable it | Document explicit non-support more clearly |

---

## 8 · References

**Project peers**

- [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) — reference
  for `RotateCookies` rotation
  ([source](https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/utils/rotate_1psidts.py)).
  Our L1 mirrors it; its lack of any reactive fallback is the gap our L3/L4/L5 close.
- [easychen/CookieCloud](https://github.com/easychen/CookieCloud) +
  [PyCookieCloud](https://github.com/lupohan44/PyCookieCloud) — DBSC-immune cookie
  federation, a viable `NOTEBOOKLM_REFRESH_CMD` source; no in-tree client.
- [dsdanielpark/Bard-API](https://github.com/dsdanielpark/Bard-API) (archived) — the
  cautionary tale: reactive/manual-only cookie management proved untenable.

**Cookie extraction**

- [`thewh1teagle/rookie`](https://github.com/thewh1teagle/rookie) (rookiepy),
  [`borisbabic/browser_cookie3`](https://github.com/borisbabic/browser_cookie3),
  [`n8henrie/pycookiecheat`](https://github.com/n8henrie/pycookiecheat)

**DBSC**

- [Google DBSC announcement](https://blog.google/security/protecting-cookies-with-device-bound-session-credentials/),
  [Chrome DBSC Windows GA](https://developer.chrome.com/blog/dbsc-windows-announcement),
  [W3C DBSC spec](https://w3c.github.io/webappsec-dbsc/),
  [Workspace session-binding](https://knowledge.workspace.google.com/admin/security/prevent-cookie-theft-with-session-binding)

**In-repo**

- [ADR-0023 — master-token headless auth](adr/0023-master-token-headless-auth.md)
- [ADR-0013 — composable session capabilities](adr/0013-composable-session-capabilities.md)
- [ADR-0004 — loop-affinity contract](adr/0004-loop-affinity-contract.md)
- [troubleshooting.md#authentication-errors](troubleshooting.md#authentication-errors),
  [installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci),
  [configuration.md#environment-variables](configuration.md#environment-variables)

---

## Appendix: field notes & historical findings

Condensed war-stories and ruled-out experiments, kept for triage context. None of
this is required to operate the library.

### A1 · `RotateCookies` vs `CheckCookie`

The original L1 mechanism used
`GET accounts.google.com/CheckCookie?continue=…notebooklm.google.com/`, relying on
a redirect chain that *might* pass through `accounts.youtube.com/SetSID` and set a
fresh `*PSIDTS`. Field probing showed this rotates `*PSIDTS` only for
Firefox-extracted (unbound) profiles — a 3-hop chain including `SetSID` — and **not**
for Playwright-extracted (bound) profiles, whose 2-hop chain has no `SetSID` step
and no `*PSIDTS` in any `Set-Cookie`. The bound-session poke still touched the
identity surface and observably extended server-side session validity, but did not
rotate the cookie. A direct `RotateCookies` POST removes the discretion: it rotates
unconditionally for both session types, at a ~100% success rate in all field
captures, with no DBSC challenge on the unsigned endpoint.

### A2 · Diagnosing "cookies expire fast"

The persistence pipeline could historically corrupt its own cookie state. Before
assuming Google changed anything:

1. **Compare `__Secure-1PSIDTS` on disk before/after a `notebooklm` call** spaced
   > 60 s apart with no other writer. No change ⇒ rotation isn't firing — check
   `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` and the mtime guard.
2. **With multiple processes sharing the file**, run at `NOTEBOOKLM_LOG_LEVEL=DEBUG`
   and look for "Keepalive RotateCookies skipped: storage refreshed before flock
   acquired" — that means the guards are working.
3. **Check `storage_state.json` mtime cadence** — hours-old mtime after active
   sessions means rotation isn't sticking.
4. **Only after the above**, investigate Google-side causes (risk-scoring, Workspace
   policy, DBSC).

**Historical persistence hazards (all resolved in-tree)** — recorded so older bug
reports still make sense:

- **Stale-in-memory clobbers fresh-disk ("few-hours" pattern).** A `keepalive=None`
  process could write its stale in-memory `*PSIDTS` over a fresher value a sibling
  rotated. Resolved by open-time snapshot + write-only-deltas with value-CAS guards
  (`_cookie_persistence.py`), on top of the cross-process flock
  ([#344](https://github.com/teng-lin/notebooklm-py/pull/344) +
  [#361](https://github.com/teng-lin/notebooklm-py/pull/361)). Still, prefer
  `keepalive=N` or a single cron-driven rotator.
- **`(name, domain)` path-collapse.** The persistence merge is now fully
  `(name, domain, path)`-aware end-to-end, so path-scoped variants survive a
  load→save round-trip ([#369](https://github.com/teng-lin/notebooklm-py/pull/369)).
- **Sibling-domain allow-list asymmetry.** The auth-jar and persistence filters were
  collapsed into one canonical `_is_allowed_cookie_domain` with siblings covered
  symmetrically ([#360](https://github.com/teng-lin/notebooklm-py/issues/360)).
- **Round-trip attribute erosion.** Both load paths now build a faithful
  `http.cookiejar.Cookie` preserving `path` / `secure` / `httpOnly` across cycles
  ([#365](https://github.com/teng-lin/notebooklm-py/pull/365)), keeping `__Host-`
  invariants intact.
- **`expires=-1` flattens age.** `*PSIDTS` rotations arrive without `Max-Age` and are
  stored as session cookies, so on-disk age is unknowable — the only staleness signal
  is the file mtime. By design, not a bug.

Across the OSS ecosystem this is the most defensive cookie-persistence implementation
surveyed: peers (Gemini-API, yt-dlp, CookieCloud) are "last writer wins" with no
flock, no atomic replace, and full-jar overwrite. Our threat model (long-lived
clients + cron `auth refresh` + parallel CLI invocations on one file) genuinely needs
the snapshot/delta/CAS/flock defenses.

### A3 · Ruled-out experiments

Investigated and rejected; documented so contributors don't re-investigate:

- **`undetected-chromedriver` / `selenium-stealth` for Google login** — WebDriver
  stealth loses to Google's signal-fusion model; login has been repeatedly broken
  across Chrome bumps. Don't use for Google flows.
- **`puppeteer-extra-plugin-stealth` / `playwright-stealth`** — patches fingerprint
  leaks only, not TLS / IP reputation / behavioral signals; works for resumed
  sessions, fails for fresh `accounts.google.com` sign-in.
- **Persistent Playwright headless context as a keepalive daemon** — fragile against
  known Playwright bugs (cookies missing in `launch_persistent_context`, profile-DB
  corruption in long-lived contexts). Prefer CDP-attach (the L3 arm) if a
  headless-browser path is needed.
- **Client-side DBSC implementation** — impossible from Python: the W3C spec is
  built around a TPM-bound key and platform attestation Chrome implements, with no
  extension point for a non-browser client. If DBSC extends to non-Chrome paths, the
  escape is to parasitize a DBSC-enrolled Chrome via the L3 CDP arm, or source
  cookies via an operator-provided `NOTEBOOKLM_REFRESH_CMD`.
- **Reading the Chrome cookie DB on Chrome 127+** — App-Bound Encryption makes this
  admin-or-bust on Windows; the yt-dlp ecosystem has converged on Firefox as the
  only reliable `--cookies-from-browser` source. Infostealer-adjacent ABE-bypass
  forks are inappropriate to ship. Document `--browser-cookies firefox` as the
  Windows path.

### A4 · Anti-pattern — persisting `storage_state` on a redirect-to-login

If you write your own Playwright keepalive instead of using `notebooklm auth
refresh` or `keepalive=N`, the most damaging mistake is calling
`context.storage_state(path=…)` unconditionally at the end of each cycle. When the
session has aged out, `page.goto("https://notebooklm.google.com/")` 302s to
`accounts.google.com/…/SignIn`, the login page sets a handful of anonymous cookies
(`NID`, `OTZ`, `__Host-GAPS`, `_ga`, …), and an unconditional `storage_state`
serializes **only those**, dropping every real auth cookie. The next cold start
finds a useless file and recovery requires a fresh interactive login. The rule for
any wrapper that owns its own persistence: **gate the write on a confirmed-authed
page URL, or better, on a successful library API call.**

```python
from notebooklm import NotebookLMClient, AuthError

async def verify_and_save(context, STORAGE):
    try:
        async with NotebookLMClient.from_storage() as client:
            await client.notebooks.list()  # confirms auth
    except (AuthError, ValueError):
        return  # don't overwrite a good file with a bad jar
    await context.storage_state(path=STORAGE)
```

The supported keepalive surfaces (`notebooklm auth refresh`, `keepalive=N`) already
gate their writes correctly.

### A5 · Open questions

- **Exact `*PSIDTS` stale-value acceptance distribution.** Acceptance varies by
  account, IP, Workspace policy, and extraction quality; longitudinal data would let
  us tune L2's 60 s floor more precisely.
- **DBSC enrollment status for Playwright-launched Chromium.** Assumed non-bound on
  macOS/Linux (no TPM), possibly bound on Windows; untested.
- **Whether `RotateBoundCookies` returns interpretable errors for unsigned
  attempts** — could let us detect a DBSC enforcement transition proactively rather
  than reactively.

---

## Changelog

- **2026-07-04 (v0.8.0 rewrite)** — Restructured and trimmed for the v0.8.0
  release. Now leads with the master-token headless path (L4) as the recommended
  approach for unattended / headless / server / CI use, adds an early
  [Available auth methods](#available-auth-methods) comparison and a
  [Recommended setup](#recommended-setup) section, and consolidates the recovery
  ladder onto the single canonical **L1–L7** taxonomy shared with
  troubleshooting.md — reflecting the integrated L3 headless re-auth and L4
  master-token re-mint. Corrected the prior status blockquote, relabeled
  browser-cookie extraction (a variant of L6 / a common L5 command, **not** L4),
  removed the obsolete "L5-A" label, and updated the `NOTEBOOKLM_REFRESH_CMD`
  scope note to "orthogonal to L1–L4". Dated empirical claims (specific probe
  runs, hour-counts, month-stamped DBSC timeline) were replaced with durable,
  timeless statements. The internal cookie-jar-fidelity hazard log, the
  `RotateCookies`-vs-`CheckCookie` deep dive, the ruled-out experiments, and the
  ecosystem comparison were compressed into the
  [Appendix](#appendix-field-notes--historical-findings).
- **Earlier revisions (2026-05 → 2026-06)** — initial writeup and field experiment;
  merged-code sync (L1 `RotateCookies` POST, three-guard throttle); §2 background
  (cookie taxonomy, rotation model, DBSC, extraction); internal-threat
  cookie-jar-fidelity analysis; domain tiering (REQUIRED vs OPTIONAL); path-aware
  persistence follow-ups. Superseded by the v0.8.0 rewrite above; see git history
  for the full detail.
