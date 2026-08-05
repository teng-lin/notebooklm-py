# Live authentication matrix

Run the repeatable, non-interactive checks from a credentialed workstation:

```bash
uv run --extra browser --extra cookies --extra headless \
  python scripts/live_auth_matrix.py \
  --profile teng-lin-9420 \
  --browser 'chromium::Profile 3' \
  --account teng.lin.9420@gmail.com \
  --base-url https://notebooklm.google.com \
  --output live-matrix.json
```

The runner records the exact Git revision and emits JSON results. All writes
go to a temporary `NOTEBOOKLM_HOME`; the source profile and browser cookie
store are read-only inputs.

Covered cells include baseline/live token checks, browser-cookie login, master
token re-mint, cookie import filtering, both NotebookLM hosts, concurrent
refresh, true mid-session recovery, deterministic transient-fault recovery
(503, connection failure, and read timeout), and crash-safe canonical writes.

Run interactive Playwright, initial master-token bootstrap, CDP capture,
Workspace/SSO, regional-account, and long-duration expiry cells separately.

The browser cookie extractor is host-sensitive. Use the host where the browser
profile currently has its NotebookLM binding; `notebook.google.com` and
`notebooklm.google.com` are both valid, and the matrix tests both hosts after
authentication. MCP transport checks are covered separately by
`scripts/mcp_live_smoke.py`.
