# ADR-0036: Browser acquisition package and neutral login orchestration

## Status

Accepted.

## Context

Browser acquisition had accumulated inside `_auth` even though launching a
browser, waiting for navigation, and capturing an OAuth token are mechanisms
for obtaining credentials rather than authentication-domain policy. The layout
forced the recovery ladder to know about Playwright, gave the CLI direct
private imports, and made it difficult to prove that the base wheel remained
usable without the `browser` extra.

Interactive login also mixed three distinct responsibilities: application call
order, browser implementation, and CLI presentation. Moving files alone would
have preserved those crossings. The boundary instead needs an explicit neutral
contract that supports interactive login, opt-in headless recovery, doctor
readiness, and one-time OAuth capture without publishing browser implementation
types.

## Decision

Create `notebooklm._browser` as the sole private package for browser acquisition.
It owns `browser_capture.py`, `browser_launch_errors.py`, `navigation_errors.py`,
`headless_reauth.py`, and `oauth_token.py`. All in-process Playwright imports
under `src/notebooklm` live there. The one exception is the equality-pinned
`CHROMIUM_PROBE_SOURCE` string in `cli/services/playwright_login.py`, executed
by a fresh child interpreter before the bundled Chromium install is attempted.

Keep `_auth` independent of `_browser`. `_auth/recovery_rungs.py` defines a
closed, browser-neutral L3 outcome and one installed-rung registry. `auth.py`
installs a lazy default adapter, so importing the base package loads neither
`_browser` nor Playwright; the browser implementation is imported only if L3
actually runs.

Add `_app/login_browser.py` as markup-free orchestration. It owns option
validation, path preparation, and the fixed execution order:

```text
availability → Chromium preflight → typed profile/opening/path events
             → browser capture → account-metadata repair
```

The CLI retains Click/Rich rendering, exits, and the subprocess-bounded
Chromium preflight. It passes a bound zero-argument preflight callable and
typed event renderer into the app operation.

First-party callers cross through five coarse lazy auth-facade operations:
`browser_login_channels`, `ensure_browser_login_available`,
`run_browser_login_capture`, `check_headless_reauth_readiness`, and
`capture_browser_oauth_token`. Two auth-policy functions are eager canonical
identity aliases: `app_host_scope_note` and
`filter_storage_state_cookies_by_domain_policy`. These seven names are
first-party-only and remain outside `auth.__all__`; equality-pinned caller
ledgers constrain their exact `_app`/CLI consumers. OAuth wrappers scrub
`browser`, `cdp_url`, and `timeout_s` from retained failure frames.

The enforced dependency direction is:

```text
cli ─┬→ _app ─→ auth facade ─→ _browser ─→ _auth policy/storage
     └────────→ auth facade ─────────────→ _auth recovery-rung registry
```

`cli` may import `_app` but not `_auth` or `_browser`; `_app` may use the public
facade but not private runtime siblings. Exact-ledgered CLI callers may also use
the public auth facade directly for readiness, OAuth capture, and canonical
policy aliases. `_browser` may depend on the audited `_auth` policy/storage
allowlist but never on the facade, app, CLI, or request runtime.

Release and PR workflows build the wheel and run `scripts/check_base_wheel.py`
in a new environment. The smoke proves base imports and CLI help work without
Playwright, channel discovery stays Playwright-free, every `_browser` module is
packaged, Playwright is optional metadata, and installing `[browser]` supplies
it.

## Consequences

- Browser implementation and Playwright dependency ownership are visible in
  one package, while `_auth` retains the recovery policy and result contract.
- The CLI no longer needs a private-browser boundary exception, and login call
  order is testable without Click, Rich, or a real browser.
- The auth facade gains seven deliberately narrow first-party names. This is
  accepted instead of exposing browser plan/result/protocol classes or making
  them public API.
- `notebooklm.notebooklm_cli` may load the Playwright-free channel-registry leaf
  because choices are resolved at import time; it must never load Playwright.
- The four moved browser modules retain their existing private behavior and
  logger/exception contracts. The six deprecated consolidation shims were
  removed early by the explicit policy override recorded in ADR-0033.
- Packaging verification takes longer because it creates an isolated base
  install and then installs the browser extra. This cost buys evidence that an
  extras-populated contributor environment cannot provide.

## Alternatives considered

- **Keep browser implementation in `_auth`.** Rejected: acquisition mechanics
  and an optional third-party runtime would continue to shape the auth-domain
  package and its recovery coordinator.
- **Let the CLI import `_browser` directly.** Rejected: it would preserve a
  private-package exception and duplicate orchestration across adapters.
- **Expose browser plans/results from `auth.py`.** Rejected: coarse operations
  are enough for first-party callers and avoid turning implementation types into
  facade commitments.
- **Move all presentation into `_app`.** Rejected: Rich markup, exit policy,
  Chromium installation, and human interaction are adapter responsibilities.
- **Rely on the normal test environment for no-extra coverage.** Rejected: that
  environment already installs Playwright and cannot detect an accidental base
  dependency or eager import.
