# Setup Reference

Part of the `notebooklm` skill. Read this when you need install detail beyond
the two `pip install` lines in [SKILL.md](../SKILL.md), CI/CD or parallel-agent
environment variables, or the full agent auth-verification walkthrough.

## Installation

**From PyPI (Recommended for AI agents — Python-version-aware):**
```bash
pip install "notebooklm-py[browser]"   # mandatory; errors must propagate

# [cookies] (rookiepy) is optional and known to FAIL TO BUILD on Python 3.13+.
# Skip it deliberately on 3.13+ rather than swallowing the error — that lets
# *real* install failures (typos, network, PyPI outages) surface for the agent.
if python -c "import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)"; then
    pip install "notebooklm-py[cookies]"   # errors propagate
else
    echo "Skipping [cookies] on Python 3.13+ (rookiepy unavailable). Use 'notebooklm login' interactively."
fi
```

> Full install matrix (extras, headless servers, contributor flow): [Installation guide on GitHub](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md).

**From GitHub (use latest release tag, NOT main branch):**
```bash
# Get the latest release tag (requires curl + jq)
if ! command -v jq >/dev/null; then
    echo "jq is required to read the latest release tag" >&2
    exit 1
fi
LATEST_TAG=$(
    curl -fsSL https://api.github.com/repos/teng-lin/notebooklm-py/releases/latest |
    jq -r '.tag_name'
)
# Includes [browser] so the interactive `notebooklm login` flow works.
pip install "notebooklm-py[browser] @ git+https://github.com/teng-lin/notebooklm-py@${LATEST_TAG}"
```

⚠️ **DO NOT install from main branch** (`pip install git+https://github.com/teng-lin/notebooklm-py`). The main branch may contain unreleased/unstable changes. Always use PyPI or a specific release tag, unless you are testing unreleased features.

## Skill install methods

- `notebooklm skill install` installs this whole skill directory (`SKILL.md`,
  `references/`, `scripts/`) into the supported local agent directories
  managed by the CLI (e.g. `.claude/skills/notebooklm/`, `.agents/skills/notebooklm/`).
- `npx skills add teng-lin/notebooklm-py` installs this skill from the GitHub
  repository into compatible agent skill directories.
- If you are already reading this file inside an installed agent skill
  directory, the skill is already installed. You only need the Python package
  and authentication below.
- If you are reading this file as part of the Claude Code plugin
  (`/plugin install notebooklm-mcp`), the skill is already bundled — don't
  also run `notebooklm skill install` for Claude Code.

**CLI-managed install:**
```bash
notebooklm skill install
```

## Prerequisites

**IMPORTANT:** Before using any command, you MUST authenticate:

```bash
notebooklm login          # Opens browser for Google OAuth
notebooklm list            # Verify authentication works
```

If commands fail with authentication errors, re-run `notebooklm login`.

## CI/CD, Multiple Accounts, and Parallel Agents

For automated environments, multiple accounts, or parallel agent workflows:

| Variable | Purpose |
|----------|---------|
| `NOTEBOOKLM_HOME` | Custom config directory (default: `~/.notebooklm`) |
| `NOTEBOOKLM_PROFILE` | Active profile name (default: `default`) |
| `NOTEBOOKLM_AUTH_JSON` | Inline auth JSON - no file writes needed |

**CI/CD setup:** Set `NOTEBOOKLM_AUTH_JSON` from a secret containing your `storage_state.json` contents.

**Multiple accounts:** Use named profiles (`notebooklm profile create work`, then `notebooklm -p work login`). Alternatively, use different `NOTEBOOKLM_HOME` directories per account.

**Parallel agents:** The CLI stores notebook context per profile (`~/.notebooklm/profiles/<profile>/context.json`, with a legacy fallback to `~/.notebooklm/context.json` for the implicit default profile). Multiple concurrent agents that share a profile and use `notebooklm use` can overwrite each other's context — use one of the isolation strategies below.

**Solutions for parallel workflows:**
1. **Always use explicit notebook ID** (recommended): Pass `-n <notebook_id>` / `--notebook <notebook_id>` on notebook-scoped commands instead of relying on `use`
2. **Per-agent isolation via profiles:** `export NOTEBOOKLM_PROFILE=agent-$ID` (each profile gets its own context file)
3. **Per-agent isolation via home:** Set unique `NOTEBOOKLM_HOME` per agent: `export NOTEBOOKLM_HOME=/tmp/agent-$ID`
4. **Use full UUIDs:** Avoid partial IDs in automation (they can become ambiguous)

## Sandboxed Agents (Claude Cowork / Headless)

Sandboxed, no-display agent environments — **Claude Cowork** (Anthropic's desktop agent for non-developers) and similar headless sandboxes — can't run `notebooklm login` (it needs a browser), and they reset between sessions. Everything else works with two adjustments:

1. **Bootstrap each session.** The sandbox resets, so install at the start of every session. You do **not** need `[browser]`/Playwright here — that extra exists only for the interactive `login` flow, which you run on a host machine, not in the sandbox. Chat, sources, generation, and download all run on the base install:
   ```bash
   pip install notebooklm-py   # no [browser] needed for queries/generation
   ```
   (This is the one place the mandatory `[browser]` install at the top of this guide does not apply — you are reusing auth, not logging in here.)
2. **Reuse a host-generated `storage_state.json`.** Log in once on a machine with a display (`notebooklm login`), then bring the resulting `storage_state.json` into a sandbox-accessible folder and point at it either way:
   ```bash
   # Per-invocation root flag (a persistent, sandbox-accessible path):
   notebooklm --storage /path/to/storage_state.json list
   # OR inline via env var (no file needed — e.g. from a Cowork-stored secret):
   export NOTEBOOKLM_AUTH_JSON="$(cat /path/to/storage_state.json)"
   notebooklm list
   ```

   > ⚠️ **`storage_state.json` / `NOTEBOOKLM_AUTH_JSON` are bearer credentials** — anyone holding them can act as your Google account. Keep the file `0600`, load it from the sandbox's secret store rather than a committed file, never print or log it, and `unset NOTEBOOKLM_AUTH_JSON` when finished.

**Verify** as in [Agent Setup Verification](#agent-setup-verification) below — e.g. `notebooklm --storage <path> auth check --test --json` (require `"status": "ok"` AND `"checks.token_fetch": true`).

**Context does not survive a reset** either: the selected-notebook context (`context.json`) is gone each session, so pass an explicit `-n/--notebook <id>` on notebook-scoped commands instead of relying on `notebooklm use`.

If Cowork reads `~/.claude/skills/`, `notebooklm skill install` registers this skill there automatically; otherwise build the uploadable archive on the host with `notebooklm skill package` (writes `notebooklm-skill.zip`, zipping the whole skill directory) and add it via **Claude Settings → Capabilities**. Full recipe (extras matrix, headless auth, CI env-var notes): [Installation guide on GitHub](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md#a-ai-agent-primary-persona).

## Agent Setup Verification

Before starting workflows, verify auth is in place. **Use `--test --json` (not bare `--json`)** — bare `--json` only proves the cookie file parses; `--test` makes a network call and proves the cookies still authenticate against Google.

1. `notebooklm auth check --test --json` → require BOTH `"status": "ok"` AND `"checks.token_fetch": true`. Bare `"status": "ok"` (without `--test`) is a false-positive trap — a stale cookie file passes the parse check.
2. `notebooklm list --json` → expect valid JSON (may be empty for new accounts).
3. **If auth fails or is missing → run `notebooklm login` first.** This is the primary auth path: opens a browser, the user signs in to Google once, and the resulting `storage_state.json` is reused on every subsequent run. Works on any environment with a display.
   - For headless contexts where opening a browser is not feasible, use `notebooklm login --browser-cookies <browser>` instead — extracts the user's already-logged-in cookies from Chrome/Firefox/etc. (requires the `[cookies]` extra; rookiepy may not install on Python 3.13+). Use `chrome::<profile-name-or-directory>` to target one Chromium user-profile, or `firefox::<container-name>` / `firefox::none` to target one Firefox container.
   - To survey signed-in Google accounts before picking one: `notebooklm auth inspect --browser <browser>` (read-only; pass `-v` to see which Chromium user-profile each account came from, or `--json` for tooling). Scoped forms such as `notebooklm auth inspect --browser 'chrome::Profile 1'` inspect only that browser profile.
   - Re-run step 1 after login to confirm.
4. **If auth was working but cookies went stale** (Google rotated SIDTS, or you signed in fresh in the browser) **→ refresh the active profile in place instead of full re-login:**
   - `notebooklm auth refresh` — server-side SIDTS refresh against the existing `storage_state.json`. Cheap and silent; safe to run on a schedule (cron / launchd / systemd) at 15–20 min cadence to keep an unattended profile warm.
   - `notebooklm auth refresh --browser-cookies <browser>` — re-extract cookies from a running browser and match them back to the profile's recorded email in `context.json`. Use when the on-disk `storage_state.json` is too stale for the server-side refresh path but you've just signed back into Google in the browser. For Chromium-family browsers with multiple user-profiles (Chrome's `Default`, `Profile 1`, …), refresh fans out across all profiles to find the email — same path as `auth inspect` (issue #571). Use `chrome::<profile-name-or-directory>` when you already know the exact browser profile.
   - Both forms preserve the same `--profile` (no new profile is created).

> **Note:** `notebooklm status` reports *context state* (selected notebook); do not use it to verify auth.
