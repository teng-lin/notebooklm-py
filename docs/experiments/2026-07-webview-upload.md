# WebView upload probe (Phase 2) — results

**Question (Gate A):** does the `<input type="file">` picker fire and can bytes `fetch`-POST
back, across the surfaces where a mobile user would add a file? The answer decides whether
the Phase 3 **in-app widget** is worth building, or whether the Phase 1 **link flow** is the
whole mobile story.

This plan splits the probe (see `docs/plans/remote-mcp-file-upload-plan-v4.md`):

- **2a — external-browser probe (this doc, automatable):** open the *existing* signed
  `/files/ul/{token}` page — already an `<input type=file>` + `fetch` POST — in the device's
  real browser. This is exactly the Phase 1 link flow. **Zero new code.**
- **2b — in-app iframe probe:** does the picker fire *inside Claude's own app WebView*? Needs
  a `ui://` widget iframe the repo doesn't have yet → it is Phase 3's first (throwaway-if-red)
  commit, **not** a standalone experiment.

## Method

- **Automated (engine-level):** a throwaway harness
  (`scratchpad/probe_2a.py`) starts the real FastMCP `http_app()` with the `/files/*` routes,
  mints a real `ul` token, and drives the page headless in **both** Playwright engines —
  Chromium (Blink) and WebKit (Safari's engine) — attaching a file and asserting the POST adds
  a source. Fresh single-use token per engine.
- **On-device (manual, pending):** run `notebooklm-mcp` with `NOTEBOOKLM_MCP_PUBLIC_URL` set to
  a public HTTPS URL, connect it on the phone, call `source_add(source_type="file")` to mint a
  link, open `human_upload.url` in the device browser AND inside Claude's app, and record each
  cell.

## Results matrix

| Surface | Engine | picker_opened | file_readable | fetch/POST ok | Verdict | Evidence |
|---|---|:--:|:--:|:--:|---|---|
| claude.ai web (Chrome/Edge), Claude Desktop | Blink | ✅ | ✅ | ✅ | **GREEN** | automated (`probe_2a.py`) |
| WebKit **engine** (Playwright — proxy for Safari/iOS engine, NOT the iOS Safari app/device) | WebKit | ✅ | ✅ | ✅ | **GREEN (engine-level only)** | automated (`probe_2a.py`) |
| Claude **iOS — in-app** WKWebView | WebKit (embedded) | ? | ? | ? | PENDING (device) — this is **2b/Phase 3** | — |
| Claude **Android — external browser** (Chrome) | Blink | ✅ | ✅ | ✅ | **GREEN — live** | on-device 2026-07-14 (below) |
| Claude **iOS / Android — in-app** WebView | embedded | ? | ? | ? | PENDING (device) — this is **2b/Phase 3** | not tested (we used the external browser) |
| ChatGPT (web/mobile) | — | — | — | — | feature-detect `window.openai.uploadFile` | snippet below |

**Prior (unchanged, informs 2b):** iOS WKWebView presents the picker natively with zero host
code — the WebKit-engine GREEN above is consistent with that. Android in-app WebView typically
**no-ops** unless the host app implements `WebChromeClient.onShowFileChooser()` — an
app-embedding property the engine-level probe **cannot** see, so it stays a real device unknown.

## Phase 2a conclusion

**The external-browser link flow works on both engines.** That is the Phase 1 mobile story, and
it is validated end-to-end (picker → file read → POST → source added) — not just assumed. So
regardless of how 2b lands, mobile users have a working path today.

## Live device validation — Claude **Android**, 2026-07-14 — ✅ PASSED

Ran the full Phase 1 loop against a live dev deployment (`nlm-dev` stack, `peopleconf` account,
tunnel `notebooklm-test.hantekllc.com`, image `phase1-dev` built from this branch; prod stack
untouched throughout). Observed server-side end-to-end:

```text
source_add(source_type="file")          → minted /files/ul/<token>   (POST /mcp)
GET  /files/ul/<full-token>             → 200  (upload page opened in Chrome)
POST notebooklm.google.com/upload/…resumable → 200  (bytes streamed to NotebookLM)
POST /files/ul/<token>?filename=Sanya-Lingshui.pdf → 200  (source added; completion recorded)
await_upload(<url>)                      → {"status":"received","source_id":"a7f1e52e-…"}
source_wait                              → READY, ok:true, 1/1, no import error
```

Sanya-Lingshui.pdf (4.8 MB) landed and became queryable. The Phase 1 code path is confirmed live:
the `/files/ul` POST wrote `{source_id,name,size,mime}` into the in-process completion map on
`ConsumedJtiStore`, and `await_upload` read it back in the same process — no DB, exactly as
designed. Opening was via the **external browser** (Chrome), so the in-app WebView cell (2b) is
still untested.

## ⚠️ Defect found (blocking UX, not a code bug): the opaque signed-URL is fragile through mobile chat

Getting the ~250-char `/files/ul/<token>` URL from the chat into the browser **failed three times**
before a byte-exact copy finally worked. Three distinct corruption modes, all server-verified:

1. **Tap-truncation** → `GET /files/ul/` with the token dropped entirely → **404**.
2. **Model re-typing the URL into prose** dropped a UUID segment (`…-4ac6-4628…`, missing `91b7-`)
   → HMAC mismatch → **403**. (The server always signs the fully-resolved notebook id, so a short
   nb inside the token proves the corruption is downstream of minting — the LLM garbled it.)
3. **`O`→`0` substitution + injected `%20` spaces** (autocorrect/OCR-style) → **403**.

Only the exact string copied from the **`source_add` tool result** (`human_upload.url`), not the
model's rendered link, verified. **Conclusion:** routing a long opaque token through model text /
a mobile tap-target is unreliable by construction.

### Fix — short-link indirection (SHIPPED in this PR)
Resolved by handing the user a short random id → token link (`…/u/<shortid>`, ~16 chars) that
`302`-redirects to the canonical `/files/ul/<token>` page server-side. The mapping lives in a new
in-process, TTL-swept `ShortLinkStore` on `FileTransferConfig` (no DB, same contract as
`ConsumedJtiStore`); `_broker_upload` returns the short link for the human path and the direct
`/files/ul` POST target for the agent path, over one token; `await_upload` accepts either.
**Re-validated live on Android:** `/u/yvhW-egX` (9 chars) → `302` → `GET /files/ul/<token> 200` →
`POST …?filename=main.pdf 200` → source added — on the **first** clean attempt, where the long URL
had failed three times. The user only ever handled the short link; the server expanded it losslessly.

## Incidental findings (feed Phase 3)

- **Page CSP is strict:** `default-src 'none'; script-src 'unsafe-inline'; style-src
  'unsafe-inline'; connect-src 'self'; form-action 'none'; base-uri 'none'`
  (`_fileroutes.py:_HTML_SECURITY_HEADERS`). `connect-src 'self'` means the page can only
  `fetch` its own origin — fine for Phase 1 (same-origin POST), but **Phase 3's direct-PUT from
  a widget iframe to the server origin will need CORS + a relaxed `connect-src` / `_meta.ui`
  `connectDomains`**, exactly as the plan flags. (Also surfaced because the strict `no
  'unsafe-eval'` CSP blocks Playwright's `eval_on_selector`/`wait_for_function` — the harness
  polls via non-eval APIs.)
- **Single-use `ul` token confirmed live:** reusing one token across the two engine runs
  returned `403` on the second — the jti burned on first success (ADR-0024 / #1746).

## ChatGPT host-upload feature-detect (Phase 5 interop signal)

Run in the ChatGPT client (apps-sdk widget or console) to see if a host-brokered upload exists:

```js
typeof window.openai?.uploadFile === "function"
```

`true` → ChatGPT offers native mobile upload today (the Phase 3 fallback-ladder's top rung, and
the interop evidence for the Phase 5 `ui/uploadFile` proposal).

## Gate A status

- **External-browser / link flow (2a):** ✅ GREEN — **live-validated on Android 2026-07-14** (full
  loop above). Phase 1 is the working, portable mobile path today.
- **In-app widget (2b):** ⏳ PENDING the two on-device cells. Build the `ui://` probe as Phase 3's
  first commit; if the in-app picker is red on a platform, that platform keeps the link flow and we
  escalate upstream (the `ui/uploadFile` proposal), per the plan.

## Next up (priority order, from what the live test taught us)
1. ~~Short-link indirection~~ — **DONE** (shipped in this PR; see the fix section above).
2. **Phase 2b** in-app WebView probe (Phase 3's first commit) — the cheap gate that decides whether
   the Phase 3 widget is worth building.
3. Phase 3 direct-PUT widget — only if 2b is green (needs the `ui://` substrate + CORS work).
4. Phase 5 SEP-2631 adapter (parallel, independent).
