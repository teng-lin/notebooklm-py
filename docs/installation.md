# Installation

**Last Updated:** 2026-05-14

This is the canonical installation guide for `notebooklm-py`. The README has a quickstart; everything else lives here.

**Contents**

- [Prerequisites](#prerequisites)
- [Quick install (TL;DR by persona)](#quick-install-tldr-by-persona)
- [Choose your install path](#choose-your-install-path)
  - [A. AI Agent (primary persona)](#a-ai-agent-primary-persona)
  - [B. End user](#b-end-user)
  - [C. Library user](#c-library-user)
  - [D. Headless server or CI](#d-headless-server-or-ci)
  - [E. Contributor](#e-contributor)
  - [F. Power user](#f-power-user)
- [Optional extras matrix](#optional-extras-matrix)
- [Post-install steps](#post-install-steps)
- [Verifying your install](#verifying-your-install)
- [Platform notes](#platform-notes)
- [Upgrading and uninstalling](#upgrading-and-uninstalling)
- [Common gotchas (appendix)](#common-gotchas-appendix)
  - [All vs All-Extras](#all-vs-all-extras)
  - [`uv pip install` vs `uv sync`](#uv-pip-install-vs-uv-sync)

---

## Prerequisites

- **Python 3.10 or later.** Tested and classified for 3.10, 3.11, 3.12, 3.13, 3.14. The CLI hard-fails with a clear error on older versions (see `_version_check.py`).
- **Operating systems.** macOS (primary development platform), Linux (Debian/Ubuntu, Fedora), Windows 10/11, WSL.
- **`uv` (optional but recommended for contributors).** Install with `curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv` / `winget install astral-sh.uv`. End users can use plain `pip` or `pipx`.
- **Disk and bandwidth.** Base install is small (~10 MB). The first `notebooklm login` downloads Chromium (~170 MB; 30–90 s; **no progress bar** — be patient).

---

## Quick install (TL;DR by persona)

> **Installing the CLI on macOS / Linux — use an isolated installer.** Plain
> `pip install` into the *system* interpreter fails on modern macOS (Homebrew
> Python) and Debian/Ubuntu with `error: externally-managed-environment`
> ([PEP 668](https://peps.python.org/pep-0668/)). For CLI/app use, prefer
> **`uv tool install`** or **`pipx install`** — they put `notebooklm` (and
> `notebooklm-mcp` / `notebooklm-server`) on your PATH in a dedicated environment
> without touching system Python. Plain `pip` still works **inside a virtualenv**
> and on **Windows** (python.org's Python is not externally-managed). Library
> users install into their own project's venv (`uv add` / `pip install`), so
> PEP 668 never applies.

| Persona | Install command |
|---|---|
| **A — AI Agent** | `pip install "notebooklm-py[browser]"` in the user's active env (fall back to `uv tool install` / `pipx install` on an *externally-managed-environment* error) |
| **B — End user** | `uv tool install "notebooklm-py[browser]"` or `pipx install "notebooklm-py[browser]"` (isolated; avoids the PEP 668 error) |
| **C — Library user** | `uv add notebooklm-py` (or `pip install notebooklm-py` inside your project venv) |
| **D — Headless server / CI** | `pip install notebooklm-py` inside a venv/container; ship a `storage_state.json` (no Playwright) |
| **E — Contributor** | `uv sync --frozen --extra browser --extra dev --extra markdown && uv run playwright install chromium && uv run pre-commit install` |
| **F — Power user** | `uv tool install --python 3.12 "notebooklm-py[browser,cookies,markdown]"` (the `cookies` extra needs Python ≤ 3.12; `--python 3.12` makes uv provision a matching interpreter even if your default is 3.13+) |

---

## Choose your install path

### A. AI Agent (primary persona)

For Claude Code, Codex, and similar agent harnesses.

The project ships `notebooklm skill install`, [SKILL.md](../SKILL.md), and [AGENTS.md](../AGENTS.md). Agents run install on the user's behalf in the user's existing environment — no new venv. They typically can't *interact* with a browser, but most agent harnesses (Claude Code, Codex) *can* shell out to Playwright when a user is present, and `[cookies]` is a preferred optimization for reusing the user's already-logged-in browser cookies.

> **Note on agent harness coverage:** `notebooklm skill install` empirically writes to `~/.claude/skills/notebooklm/SKILL.md` and `~/.agents/skills/notebooklm/SKILL.md`. Cursor and other harnesses with bespoke skill formats are not auto-targeted; they fall back to `pip install` + manual skill registration.

**Recommended install (Python-version-aware; surfaces real errors instead of swallowing them):**

<!-- not mirrored: end-user install path (Persona A); CONTRIBUTING.md tracks the in-repo `uv sync` flow only -->
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

> If `pip install` errors with `externally-managed-environment` (modern macOS / Debian system Python, [PEP 668](https://peps.python.org/pep-0668/)), retry with `uv tool install "notebooklm-py[browser]"` or `pipx install "notebooklm-py[browser]"` — isolated installs that don't touch system Python. Inside an active virtualenv, `pip` works as-is.

**Why two separate calls (not `[browser,cookies]`):** the combined form is atomic — if `rookiepy` fails to compile, the whole install fails and the user gets **nothing**. Splitting means `[browser]` always succeeds; `[cookies]` is recoverable.

**Skill install (separate from the Python package):**

<!-- not mirrored: agent skill registration; not part of the contributor install flow -->
```bash
notebooklm skill install              # writes to ~/.claude/skills/, ~/.agents/skills/
# OR (alternative ecosystem):
npx skills add teng-lin/notebooklm-py
```

If the agent is reading `SKILL.md` from inside an already-installed location (e.g. `~/.claude/skills/notebooklm/SKILL.md`), the skill is already present — you only need the Python package install + auth.

**Authentication — `notebooklm login` is the primary path:**

<!-- not mirrored: end-user auth setup; contributors usually source storage_state from a personal account -->
```bash
notebooklm login                       # primary: opens browser, user signs in to Google once
```

After login, `storage_state.json` persists at `~/.notebooklm/profiles/default/storage_state.json` and is reused on every subsequent run. **Verify with `notebooklm auth check --test --json`** (require `"status": "ok"` AND `"checks.token_fetch": true` — bare `auth check --json` only proves the file parses, not that the cookies still authenticate against Google).

**Headless / sandboxed agent contexts** (no display, can't open a browser): use the cookie-extraction path instead, requires the `[cookies]` extra installed in step 2:

<!-- not mirrored: headless-agent auth path; out of scope for the contributor README -->
```bash
notebooklm login --browser-cookies auto    # rookiepy autodetects an installed browser
```

If the agent is in a no-display sandbox AND `[cookies]` isn't installed (Python 3.13+ skipped it), ask the user to run `notebooklm login` on a workstation and copy the resulting `~/.notebooklm/profiles/default/storage_state.json` to the agent's environment (or set `NOTEBOOKLM_AUTH_JSON`).

**Verification (machine-parseable):**

<!-- not mirrored: agent-targeted verification; CONTRIBUTING.md uses the test+lint suite as its smoke check -->
```bash
notebooklm --version                    # text version
notebooklm auth check --json            # JSON: {"status": "ok", "checks": {...}}
notebooklm auth check --test --json     # same + network token-fetch validation
notebooklm list --json                  # JSON list (may be empty for new accounts)
```

> **Important:** `notebooklm status` is *context state* (selected notebook), **NOT auth**. Do not grep its output for auth signals.

**Error strings the agent should grep:**

- `"Playwright not installed"` → install `[browser]`
- `"rookiepy"` (in stderr of `pip install`) → expected on Python 3.13+; skip `[cookies]` and use interactive `notebooklm login`
- `"status": "ok"` (in `auth check --json`) → auth file present and parses; pair with `--test` for network validation

### B. End user

Occasional CLI use.

**Prerequisites:** Python 3.10+ already installed.

**Recommended — isolated install (macOS / Linux / Windows):**

<!-- not mirrored: end-user isolated install (pipx / uv tool); CONTRIBUTING.md targets in-repo contributors -->
```bash
uv tool install "notebooklm-py[browser]"
# OR, with pipx:
pipx install "notebooklm-py[browser]"
```

Both put `notebooklm` on your PATH in a dedicated environment, so they work even where the system Python is locked down — modern macOS (Homebrew) and Debian/Ubuntu reject a plain `pip install` into it with `error: externally-managed-environment` ([PEP 668](https://peps.python.org/pep-0668/)). (If you don't have `uv` yet: <https://docs.astral.sh/uv/getting-started/installation/>.)

Plain `pip` is fine **inside a virtualenv**, or on Windows (python.org's Python is not externally-managed):

<!-- not mirrored: end-user pip install in a venv (Persona B); CONTRIBUTING.md tracks the in-repo `uv sync` flow only -->
```bash
pip install "notebooklm-py[browser]"
```

**Post-install:** Run `notebooklm login` once. The CLI auto-installs Chromium on first run (~170 MB, 30–90 s, **no progress bar — be patient**).

**Verify:**

<!-- not mirrored: end-user post-install verify (Persona B); contributors run the test suite instead -->
```bash
notebooklm --version
notebooklm login                  # opens Chromium for Google sign-in
notebooklm auth check --test      # confirms auth roundtrip, with explicit success message
```

### C. Library user

Embedding `notebooklm-py` in a Python application.

**Recommended:** `pip install notebooklm-py` (in your app's venv).

**Post-install:** None for runtime use. To programmatically run interactive login from your app, add `[browser]` and run `playwright install chromium`.

**Why no extras by default:** all RPC traffic uses `httpx`; auth is cookie-based (`src/notebooklm/auth.py`). Apps can ship a pre-generated `storage_state.json` and never touch Playwright.

**Verify:**

```python
import notebooklm
print(notebooklm.__version__)
```

> **Production deployment patterns (tracked in [#417](https://github.com/teng-lin/notebooklm-py/issues/417)).** Production-grade FastAPI/Django integration — client lifetime in a `lifespan` handler, httpx pool sizing, behavior under concurrent CSRF refresh, multi-tenant `storage_state.json` rotation, a service-shaped Dockerfile, and structured rate-limit/backoff patterns — is not yet covered in `docs/python-api.md`. These were intentionally deferred from the install-docs consolidation (PR #416) to keep its scope focused. See [#417](https://github.com/teng-lin/notebooklm-py/issues/417) for the gap inventory and acceptance criteria.

### D. Headless server or CI

**Recommended:** `pip install notebooklm-py`

**Post-install (3-step recipe — Playwright is *not* required on the server):**

1. **On a workstation with a display**, install with `[browser]` and log in once:
   <!-- not mirrored: headless-server bootstrap step 1 (Persona D); not part of contributor flow -->
   ```bash
   pip install "notebooklm-py[browser]"
   playwright install chromium
   notebooklm login   # writes ~/.notebooklm/profiles/default/storage_state.json
   ```
2. **Move the auth file to the server.** Either ship it as a file:
   <!-- not mirrored: headless-server bootstrap step 2a (scp); not part of contributor flow -->
   ```bash
   scp ~/.notebooklm/profiles/default/storage_state.json \
       user@server:~/.notebooklm/profiles/default/storage_state.json
   ```
   or stuff the contents into a CI / deployment env var (preferred for ephemeral runners):
   <!-- not mirrored: headless-server bootstrap step 2b (env var); not part of contributor flow -->
   ```bash
   export NOTEBOOKLM_AUTH_JSON="$(cat ~/.notebooklm/profiles/default/storage_state.json)"
   ```

   > **CI env-var notes:**
   > - `storage_state.json` is typically 4–15 KB — well under GitHub Actions' 48 KB single-secret cap.
   > - Watch for trailing newlines: pipe with `tr -d '\n'` if your secret-set tool adds one (`cat ... | tr -d '\n' | gh secret set NOTEBOOKLM_AUTH_JSON`).
   > - For **ephemeral runners** (GitHub Actions, GitLab CI — no persistent disk between runs), the layer-5 in-process refresh from [troubleshooting.md](troubleshooting.md#authentication-errors) cannot persist rotated cookies. Run `notebooklm auth refresh` periodically on a workstation cron and push the refreshed file with `gh secret set NOTEBOOKLM_AUTH_JSON < ~/.notebooklm/profiles/default/storage_state.json`.
3. **On the server**, run any non-`login` command:
   <!-- not mirrored: headless-server smoke test; not part of contributor flow -->
   ```bash
   notebooklm list
   notebooklm auth check --test    # verifies the cookies still authenticate against Google
   ```

**Why no extras:** reduces the install surface to 4 deps (`httpx`, `click`, `rich`, `filelock`); avoids 200+ MB Chromium download in CI images.

For runtime configuration (env vars, profiles, parallel agents), see [configuration.md#headless-servers--containers](configuration.md#headless-servers--containers).

#### Alternative: master-token auth (no cookie file to ship, survives expiry)

The cookie-copy recipe above ships a `storage_state.json` that eventually
expires (cookies are short-lived; ephemeral CI runners can't persist rotations).
The **master-token** path instead holds one durable Google master token and
**mints fresh web cookies from it on demand** — no browser per session, and an
expired session **re-mints automatically** (no manual re-login). One browser
sign-in, then headless forever.

<!-- not mirrored: master-token headless bootstrap (Persona D); not part of contributor flow -->
```bash
pip install "notebooklm-py[headless]"        # adds gpsoauth (pure-Python)

# One-time bootstrap (a visible browser opens Google's EmbeddedSetup; sign in
# with a DEDICATED/throwaway account, and the single-use oauth_token is captured
# automatically). Add [browser] for the auto-capture, or paste it with
# --oauth-token <value> on a headless box.
notebooklm login --master-token --account you@gmail.com

# Ship master_token.json to the server instead of storage_state.json:
scp ~/.notebooklm/profiles/default/master_token.json \
    user@server:~/.notebooklm/profiles/default/master_token.json

# On the server, just run commands — cookies are minted/refreshed as needed:
notebooklm list
# Force a re-mint by hand (or from cron) any time:
notebooklm login --master-token-refresh
```

When a `master_token.json` sits beside a profile's `storage_state.json`, an
expired session is recovered by re-minting from the master token in-process
(after the normal homepage/RotateCookies/headless ladder is exhausted) — so
long-lived headless workers self-heal.

> ⚠️ **Security:** the master token is **full-account, durable, and
> infostealer-grade** — a materially larger blast radius than an expiring
> `storage_state.json` (it survives password changes until explicitly revoked).
> Use a **dedicated/throwaway Google account only**, store it `0600` (the CLI
> does), and never commit or log it. This path uses Google's Android auth flow
> (`gpsoauth`) and is unofficial/ToS-grey, like the rest of this client. See
> [ADR-0023](adr/0023-master-token-headless-auth.md) for the design and rationale.

### E. Contributor

Working on this repo.

**Recommended (respects the checked-in `uv.lock`):**

<!-- not mirrored: contributor bootstrap with git clone + cd; CONTRIBUTING.md picks up after the clone with the canonical `uv sync --frozen --extra browser --extra dev --extra markdown` command (enforced verbatim by scripts/check_ci_install_parity.py). -->
```bash
git clone https://github.com/teng-lin/notebooklm-py.git
cd notebooklm-py
uv sync --frozen --extra browser --extra dev --extra markdown
source .venv/bin/activate
uv run playwright install chromium
pre-commit install
```

**Why `uv sync --frozen` and not `uv pip install -e ".[all]"`:** the repo has a checked-in `uv.lock`. `uv sync --frozen` enforces the lockfile and fails fast on drift; `uv pip install` ignores the lockfile and re-resolves transitively (will silently get newer versions of `playwright`, `ruff`, etc.).

**Why three extras and not `[all]`:** `[all]` is `pip` extras semantics. `uv sync --extra X` is the `uv` equivalent. The three extras here are the contributor subset of `[all]` = `[browser, dev, markdown, mcp, server]`. `cookies` is intentionally excluded (`rookiepy` build issues on Python 3.13+), and `mcp` / `server` are omitted from the default contributor flow because those adapters are not needed for the standard local suite; opt in via `--extra cookies` / `--extra mcp` / `--extra server` if needed.

**Why `browser` is part of the contributor install:** the default local test suite includes unit tests that import and patch `playwright.sync_api`, even though they do not launch a real browser. `uv sync --frozen --extra dev` installs pytest/ruff/mypy but not Playwright, so `uv run pytest` will fail with `ModuleNotFoundError: No module named 'playwright'`. Use the full contributor command above before running the default test suite.

**Linux only:** `uv run playwright install-deps chromium` (scoped form, matches `test.yml`).

**Pre-commit checklist (run before every commit):**

```bash
uv run ruff format --check . && \
    uv run ruff check . && \
    uv run mypy src/notebooklm --ignore-missing-imports && \
    uv run pytest --cov=src/notebooklm --cov-report=term-missing --cov-fail-under=90
```

**Verify:**

<!-- not mirrored: contributor verify block; CONTRIBUTING.md mirrors only the pre-commit checklist (the more frequent, per-commit version) -->
```bash
notebooklm --version
uv run pytest --cov=src/notebooklm --cov-report=term-missing --cov-fail-under=90
uv run pre-commit run --all-files
```

### F. Power user

Non-default browsers, cookie extraction, markdown source dumps.

> **Why this section uses the combined `[browser,cookies]` form** — unlike Persona A, which uses two separate `pip install` calls so a `rookiepy` build failure doesn't leave the user with nothing: power users explicitly opted in, know what `rookiepy` is, and prefer the all-or-nothing tradeoff (single command, no wrapping logic).

> ⚠️  **Don't use `[all]` for power-user setups.** `[all]` deliberately *excludes* `cookies` (see [§ All vs All-Extras](#all-vs-all-extras)). If you `pip install "notebooklm-py[all]"` and then try `--browser-cookies`, you'll get an opaque `rookiepy` import error. For everything-and-the-kitchen-sink, use `pip install "notebooklm-py[browser,cookies,markdown]"` explicitly (Python ≤ 3.12 only).

- **`--browser-cookies` (no Playwright login):** `pip install "notebooklm-py[browser,cookies]"`. **Caveat:** `rookiepy` may fail to install on Python 3.13/3.14; use Python 3.12 or accept the risk. See [cli-reference.md#authentication-login](cli-reference.md#authentication-login) for the full `--browser-cookies` syntax, including `chrome::<profile-name-or-directory>` for one Chromium user-profile and `firefox::<container>` for Firefox Multi-Account Containers (on every OS — not just macOS). Use `notebooklm auth inspect --browser <browser>` for previewing available accounts before import.
- **Markdown source dumps:** `pip install "notebooklm-py[markdown]"` for `notebooklm source fulltext -f markdown`.
- **Edge instead of Chromium:** install Microsoft Edge from [microsoft.com/edge](https://www.microsoft.com/edge) first — `--browser msedge` does NOT auto-install Edge (only `--browser chromium` auto-installs). Then `notebooklm login --browser msedge`.
- **Multi-account (personal + work):** see [configuration.md#multiple-accounts](configuration.md#multiple-accounts). Common power-user flow: `notebooklm profile create work && notebooklm -p work login --browser-cookies edge --account work@corp.com`. Use `--all-accounts` to bootstrap profiles for every signed-in Google account in one command.

---

## Optional extras matrix

Source of truth: `pyproject.toml` `[project.optional-dependencies]`.

| Extra | What it adds | When you need it | pip command | uv (in your project) |
|---|---|---|---|---|
| (none) | `httpx`, `click`, `rich`, `filelock` | All RPC operations, all CLI commands except `login`. Suffices when you ship a `storage_state.json`. | `pip install notebooklm-py` | `uv add notebooklm-py` |
| `browser` | `playwright>=1.40.0` | `notebooklm login` (interactive). | `pip install "notebooklm-py[browser]"` | `uv add "notebooklm-py[browser]"` |
| `cookies` | `rookiepy>=0.1.0` | `notebooklm login --browser-cookies <browser>`, `notebooklm auth inspect`. | `pip install "notebooklm-py[cookies]"` | `uv add "notebooklm-py[cookies]"` |
| `headless` | `gpsoauth>=1.1.0` | `notebooklm login --master-token` — headless auth that mints/refreshes web cookies from a durable master token, no per-session browser. Pure-Python (in `all`). See [§ D](#d-headless-server-or-ci). | `pip install "notebooklm-py[headless]"` | `uv add "notebooklm-py[headless]"` |
| `impersonate` | `curl_cffi>=0.11` | **Experimental.** Browser TLS/JA3 impersonation transport — set `NOTEBOOKLM_TRANSPORT=curl_cffi` to route the authenticated API surface through a Chrome-fingerprinted connection (insurance vs TLS fingerprint-gating); override the profile with `NOTEBOOKLM_IMPERSONATE` (default `chrome`, e.g. `safari`, `chrome131`). Native wheels. | `pip install "notebooklm-py[impersonate]"` | `uv add "notebooklm-py[impersonate]"` |
| `markdown` | `markdownify>=0.14.1` | `notebooklm source fulltext -f markdown`. | `pip install "notebooklm-py[markdown]"` | `uv add "notebooklm-py[markdown]"` |
| `mcp` | `fastmcp>=2.14` | Run the MCP server (`notebooklm-mcp`) so an MCP client/agent can drive NotebookLM as tools. | `pip install "notebooklm-py[mcp]"` | `uv add "notebooklm-py[mcp]"` |
| `server` | `fastapi`, `uvicorn[standard]`, `python-multipart` | The localhost REST API server (`notebooklm-server`, experimental). See [§ REST API server](#rest-api-server). | `pip install "notebooklm-py[server]"` | `uv add "notebooklm-py[server]"` |
| `dev` | pytest stack, mypy, ruff (`==0.15.15` exact pin), pre-commit (`>=4.5.1`), vcrpy | Contributor tooling only. Not sufficient for this repo's default `uv run pytest`; add `browser` too because some unit tests import Playwright. | `pip install "notebooklm-py[dev]"` | `uv add "notebooklm-py[dev]"` (in your project) — but contributors *to this repo* use the [Persona E](#e-contributor) `uv sync` flow instead |
| `all` | Resolves to `browser` + `dev` + `headless` + `markdown` + `mcp` + `server` (**not `cookies`**) | Contributors who do not need `rookiepy`. | `pip install "notebooklm-py[all]"` | `uv add "notebooklm-py[all]"` (in your project) — see [All vs All-Extras](#all-vs-all-extras) |

> **Note on `uv` columns:** the `uv (in your project)` column is for users adding `notebooklm-py` as a dependency in **their own** project (requires a `pyproject.toml` in that project). Contributors working inside *this* repo use the Persona E flow (`uv sync --frozen --extra ...`), governed by this repo's `uv.lock`. Do not run `uv sync` outside a project — it errors with `No pyproject.toml found`.

---

## REST API server

> **⚠️ Experimental.** Like the MCP adapter, the REST server is experimental: the `/v1` surface and behavior may change in a minor release, and it is excluded from the public-API compatibility gate. Pin a version before relying on it for automation. The server also logs an experimental warning on every startup.

A single-tenant, localhost REST API over the same transport-neutral core as the CLI — the natural shape for scripting and agent automation (feed a notebook, generate an artifact, pull it down) without spawning a CLI process per call.

<!-- not mirrored: the server extra is end-user/automation tooling, not part of the contributor `uv sync` flow; CONTRIBUTING.md tracks only browser/dev/markdown. -->
```bash
uv tool install "notebooklm-py[server]"    # fastapi + uvicorn + python-multipart
# OR, with pipx:  pipx install "notebooklm-py[server]"   (or plain pip inside a venv)
```

**Prerequisite:** a provisioned account (`storage_state.json`) from `notebooklm login`. The server holds one account for the process; it does not run browser login itself.

**Launch:**

<!-- not mirrored: REST-server launch (end-user/automation tooling); CONTRIBUTING.md tracks only the contributor `uv sync` flow. -->
```bash
export NOTEBOOKLM_SERVER_TOKEN="$(openssl rand -hex 32)"   # REQUIRED — the server refuses to start without it
notebooklm-server --host 127.0.0.1 --port 8000            # loopback-only by default
```

Configuration is read from `NOTEBOOKLM_SERVER_*` env vars (overridable by the matching flags):

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOTEBOOKLM_SERVER_TOKEN` | *(unset)* | Bearer token every request must present. **Required** — fail-closed if unset. |
| `NOTEBOOKLM_SERVER_HOST` | `127.0.0.1` | Bind host. Non-loopback is refused unless the elevated-risk override below is set. |
| `NOTEBOOKLM_SERVER_PORT` | `8000` | Bind port. |
| `NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND` | *(unset)* | ⚠️ Set to `1` to bind a non-loopback interface. Only behind a trusted reverse proxy — this exposes account-fronting credentials to the network. |

**Surface:** every route is under `/v1` and requires `Authorization: Bearer <token>` plus a loopback `Host` header (a DNS-rebinding guard). `/healthz` is the one public, token-less route. The auto-generated `/docs` / `/openapi.json` schema UI is disabled (it would otherwise be reachable token-less).

<!-- not mirrored: REST-server curl examples (end-user/automation tooling); not part of the contributor install flow. -->
```bash
TOKEN=$NOTEBOOKLM_SERVER_TOKEN
BASE=http://127.0.0.1:8000

curl $BASE/healthz                                                    # {"ok": true}  (no token)
curl -H "Authorization: Bearer $TOKEN" $BASE/v1/notebooks             # list notebooks
curl -H "Authorization: Bearer $TOKEN" -d '{"title":"My NB"}' \
     -H 'Content-Type: application/json' $BASE/v1/notebooks           # create
curl -H "Authorization: Bearer $TOKEN" -d '{"url":"https://example.com"}' \
     -H 'Content-Type: application/json' $BASE/v1/notebooks/<id>/sources/url
curl -H "Authorization: Bearer $TOKEN" -d '{"question":"Summarize"}' \
     -H 'Content-Type: application/json' $BASE/v1/notebooks/<id>/chat # blocking answer
curl -H "Authorization: Bearer $TOKEN" $BASE/v1/notebooks/<id>/share # sharing status
```

Endpoints: `/v1/notebooks` (list/get/create/delete); `/v1/notebooks/{id}/sources` (list/get/add via `url`·`text`·`file`/delete); `/v1/notebooks/{id}/notes` (list/get/create/update via `PUT`/delete); `/v1/notebooks/{id}/chat` (blocking ask, no streaming); `/v1/notebooks/{id}/artifacts` (list / generate / poll / download); `/v1/notebooks/{id}/share` (status / public link / users / view level). Long-running work (source ingest, artifact generation) is **poll-the-resource**: the create call returns immediately and the matching `GET` reports `pending` until the resource is ready (`200`), `404` for an id the server never created, `409`/`410` for a failed/removed artifact.

**Artifacts & uploads:**

<!-- not mirrored: REST-server artifact/upload curl examples (end-user/automation tooling); not part of the contributor install flow. -->
```bash
# Generate (non-blocking → 202 + task_id). Omit source_ids to use ALL sources
# (like the CLI); pass them to scope. Some types (quiz/flashcards) need at least one source.
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"type":"quiz"}' $BASE/v1/notebooks/<id>/artifacts        # → {"task_id": ...}
curl -H "Authorization: Bearer $TOKEN" $BASE/v1/notebooks/<id>/artifacts/<task_id>  # poll
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"type":"audio"}' $BASE/v1/notebooks/<id>/artifacts/download -o out.mp3     # download
# File upload is multipart (the original filename + content-type are preserved):
curl -H "Authorization: Bearer $TOKEN" -F 'file=@./notes.pdf' \
     $BASE/v1/notebooks/<id>/sources/file
```

**Error envelope:** every failure is `{"error": {"category": "...", "message": "..."}}` with a category-derived HTTP status — `not_found`→404, `validation`→400/422, `auth`→401/403, `rate_limited`→429, `notebook_limit`→409, server/network→502, timeouts→504. The category is classified once by `_app.errors.classify`, shared with the CLI.

---

## Post-install steps

### `playwright install chromium` — when required, when auto-installed

- **Required**: when you'll use `notebooklm login` (the interactive Playwright flow), unless the CLI auto-installs Chromium for you (it does — see `ensure_chromium_installed()` in `cli/services/playwright_login.py`, which runs `python -m playwright install chromium` on first login if Chromium is missing).
- **Not required**: for headless servers (Persona D), library use (Persona C), or `--browser-cookies`-based auth (Persona A/F with `[cookies]`).

### `playwright install-deps chromium` — Linux system libraries

On Debian/Ubuntu, Playwright needs system libs for Chromium. Run after `playwright install chromium`:

<!-- not mirrored: Linux-specific Playwright system-library install; CI runs `uv run playwright install-deps chromium` directly in test.yml -->
```bash
playwright install-deps chromium       # scoped to chromium; matches CI
```

Works without `sudo` if you're root or have passwordless sudo. Otherwise `sudo playwright install-deps chromium`.

### First-time `notebooklm login`

<!-- not mirrored: end-user first-login walkthrough; contributors typically reuse an existing storage_state.json -->
```bash
notebooklm login                       # opens Chromium for Google sign-in
notebooklm auth check --test           # verify
```

The login command:
- Auto-installs Chromium if missing (Persona A/B/E).
- Saves cookies to `~/.notebooklm/profiles/<profile>/storage_state.json`.
- Uses a *persistent* browser profile so subsequent logins are faster.

### `notebooklm skill install` — for AI agents (Persona A)

Registers the skill into local agent skill directories:

<!-- not mirrored: agent skill directory registration; out of scope for the contributor README -->
```bash
notebooklm skill install               # writes ~/.claude/skills/notebooklm/, ~/.agents/skills/notebooklm/
```

Optional — only needed if your agent harness reads from those directories and the skill isn't already present.

### Running the MCP server (`mcp` extra)

The MCP server ships behind the optional `mcp` extra (see the extras matrix above) and exposes the same `_app/` business logic over the Model Context Protocol.

<!-- not mirrored: end-user MCP run/config pointer; out of scope for the contributor README -->
```bash
notebooklm-mcp                                         # installed console script (stdio transport)
uvx --from "notebooklm-py[mcp]" notebooklm-mcp         # no install — run straight from PyPI
```

Wire it into an MCP client with either:
- `notebooklm mcp install <client>` — auto-writes the server config for `claude-desktop`, `claude-code`, `cursor`, or `windsurf`; or
- the one-click `.mcpb` desktop bundle built from `desktop-extension/` (Claude Desktop's "Install Extension").

Full usage walkthrough (auth, transports, the 35 tools, workflows, troubleshooting): **[mcp-guide.md](mcp-guide.md)**.

---

## Verifying your install

| Command | What it checks | Use when |
|---|---|---|
| `notebooklm --version` | Package installed correctly. | Always. |
| `notebooklm auth check --json` | Auth file parses; `SID` cookie present. Returns `{"status": "ok"\|"error", "checks": {...}}`. | Agents (machine-parseable). |
| `notebooklm auth check --test` | Same + network token-fetch validates that cookies still authenticate against Google. | End users (after login). |
| `notebooklm auth check --test --json` | Both. | Agents that need to confirm the cookies aren't stale. |
| `notebooklm list` | Package + auth + RPC roundtrip all work. | After login, as a smoke test. |

> **Important:** `notebooklm status` reports *context state* (which notebook is selected). It is **not** an auth check. See [Common gotchas](#common-gotchas-appendix).

**Your first end-to-end run:**

<!-- not mirrored: end-user smoke test; contributors run `uv run pytest` instead -->
```bash
notebooklm create "My First Notebook"
notebooklm source add 'https://en.wikipedia.org/wiki/Python_(programming_language)'
notebooklm ask "Summarize the sources in three sentences"
```

For the full CLI surface, see [cli-reference.md](cli-reference.md).

---

## Platform notes

| Platform | Install-time notes | Diagnostic detail |
|---|---|---|
| **macOS** | Chromium auto-downloads on first login. `--browser-cookies` from Chrome/Edge/Brave/Opera may prompt for Keychain access. | [troubleshooting.md#macos](troubleshooting.md#macos) |
| **Linux** | (a) `playwright install-deps chromium` for system libs (Debian/Ubuntu). (b) **Known bug:** `playwright > 1.57` may fail with `TypeError: onExit is not a function` — pin `playwright==1.57.0`. | [troubleshooting.md#linux](troubleshooting.md#linux) |
| **Windows** | The library auto-configures `WindowsSelectorEventLoopPolicy` and `PYTHONUTF8=1`. Prefer plain `pip install` (uv/pipx less common on Windows). | [troubleshooting.md#windows](troubleshooting.md#windows) |
| **WSL** | The browser opens in the Windows host (expected); `storage_state.json` lives in the WSL filesystem. | [troubleshooting.md#wsl](troubleshooting.md#wsl) |

---

## Upgrading and uninstalling

<!-- not mirrored: end-user upgrade commands; contributors `git pull && uv sync --frozen ...` instead -->
```bash
pip install --upgrade notebooklm-py            # latest patch
pip install --upgrade "notebooklm-py[browser]"  # preserves your extras
```

For pinning patterns and version-stability guarantees, see [stability.md](stability.md).

To uninstall:

<!-- not mirrored: end-user uninstall; contributors `git clean -fdx` and remove the worktree instead -->
```bash
pip uninstall notebooklm-py
rm -rf ~/.notebooklm                          # optional: remove auth state
```

---

## Common gotchas (appendix)

### All vs All-Extras

> ⚠️  **`pip install ".[all]"` and `uv sync --all-extras` are not equivalent.**
>
> - `pyproject.toml` defines: `all = ["notebooklm-py[browser,dev,markdown,mcp,server]"]` — a self-referential extras string that resolves to **browser + dev + markdown + mcp + server only**. It deliberately excludes `cookies` because `rookiepy` has install issues on Python 3.13+ ([CHANGELOG `[0.4.1]`](../CHANGELOG.md)).
> - `uv sync --all-extras` installs **every** extra including `cookies`, and may fail on Python 3.13/3.14.
> - In this repo, prefer `uv sync --frozen --extra browser --extra dev --extra markdown`.

### `uv pip install` vs `uv sync`

- `uv pip install -e ".[all]"` ignores the checked-in `uv.lock` — it re-resolves dependencies and may pull newer versions of `playwright`, `ruff`, etc. than the lock specifies.
- `uv sync --frozen` enforces the lockfile and fails fast on drift. **This is what contributors should use.**
- `uv sync` (no `--frozen`) silently updates `uv.lock` if `pyproject.toml` has changed. Use only when intentionally bumping deps.

### `notebooklm status` ≠ auth

`notebooklm status` reports the *currently selected notebook* (context). It does NOT report whether you are authenticated. For auth, use `notebooklm auth check` (or `--json` / `--test --json` for machine output and network validation).
