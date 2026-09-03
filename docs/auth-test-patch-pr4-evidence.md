# Auth test-patch PR 4 evidence

Base: `6aabddad7dac2386608b5e0e0e0f8eb66f454c5c`

## Scorecard and behavior

- `_browser` module patch sites: 66 to 30.
- Private-name sites: 17 to 4; public-name sites: 49 to 26.
- `HeadlessReauthState` owns the process-lifetime synchronous drive registry, while tests use fresh
  instances and injected capture gateways.
- Public entry signatures, local-only CDP validation, typed outcomes, stale-outcome protection, and
  per-storage-path/per-source coalescing remain unchanged.
- Lifecycle reset rejects active drivers and pre-lock record reservations, closing the lookup-to-lock
  race without retaining credential values.

## Supplemental strict local-CDP smoke

The strict-mode alternate-source smoke ran on 2026-09-03 from the pre-commit implementation
worktree against isolated NotebookLM profile
`agent-auth-patch-pr4` and the operator's authenticated local Chrome CDP endpoint:

```bash
cdp_ws_path=$(sed -n '2p' \
  '/Users/blackmyth/Library/Application Support/Google/Chrome/DevToolsActivePort')
NOTEBOOKLM_PROFILE=agent-auth-patch-pr4 \
NOTEBOOKLM_HEADLESS_REAUTH=1 \
NOTEBOOKLM_HEADLESS_REAUTH_REQUIRE_SUCCESS=1 \
NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL="ws://127.0.0.1:9222${cdp_ws_path}" \
uv run pytest -q tests/e2e/test_headless_reauth.py --no-cov
```

Result: `1 passed in 11.74s`; zero skips. Strict mode asserted
`HeadlessReauthStatus.SUCCESS` and the persisted-path outcome unconditionally.

This proves the local-CDP capture arm, but it does not replace the plan-required dedicated
reusable-profile run and is not claimed as final-SHA evidence. The isolated profile's Google
session is currently expired. Before this slice is merge-ready, re-authenticate
`agent-auth-patch-pr4`, unset
`NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL`, and record `1 passed` with zero skips from:

```bash
NOTEBOOKLM_PROFILE=agent-auth-patch-pr4 \
NOTEBOOKLM_HEADLESS_REAUTH=1 \
NOTEBOOKLM_HEADLESS_REAUTH_REQUIRE_SUCCESS=1 \
uv run pytest -q tests/e2e/test_headless_reauth.py --no-cov
```

No cassette, dependency, lockfile, credential format, or remote-CDP capability changed.
