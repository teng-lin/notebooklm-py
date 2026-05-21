# Contributing to notebooklm-py

## For Human Contributors

### Getting Started

```bash
# Canonical contributor install (respects uv.lock)
uv sync --frozen --extra browser --extra dev --extra markdown
source .venv/bin/activate
uv run playwright install chromium
pre-commit install
```

**Run the full pre-commit suite** (matches what CI runs). IMPORTANT: use the
broad `.` scope, not `src/ tests/` — the pre-commit hook in CI invokes
ruff-format on the whole tree and is stricter than a narrow scope.

```bash
uv run ruff format --check . && \
    uv run ruff check . && \
    uv run mypy src/notebooklm --ignore-missing-imports && \
    uv run pytest --cov=src/notebooklm --cov-report=term-missing --cov-fail-under=90
```

**No uv?** Plain pip works as a fallback (won't enforce the lockfile, so you may resolve newer dep versions than CI):
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"   # [all] = browser + dev + markdown (no cookies; see installation.md)
playwright install chromium
pre-commit install
```

For full prerequisites, headless setup, optional extras (`[cookies]`, `[markdown]`), and platform notes, see [docs/installation.md#e-contributor](docs/installation.md#e-contributor).

> **Install-doc parity.** `docs/installation.md` is the canonical install guide; this file mirrors a small contributor-focused subset. Every fenced ``bash`` block in `installation.md` must EITHER appear verbatim in `CONTRIBUTING.md`, OR be marked with `<!-- not mirrored: <reason> -->` on the line directly before its opening fence. CI enforces this via `scripts/check_ci_install_parity.py` so a stale block can't drift in unnoticed. When you edit `installation.md`, decide on the spot whether the new content also belongs in this file.

The `browser` extra is part of the contributor install because the default unit
suite imports and patches `playwright.sync_api`. The command
`uv sync --frozen --extra dev` is only the test/lint toolchain; it is not enough
for `uv run pytest`.

> **Architecture & testing context.** Once installed, read [docs/development.md](docs/development.md) for the layered RPC/Core/Client/CLI design, test-tree layout, and release workflow before touching `src/notebooklm/`.

### Code Quality

This project uses **ruff** for linting and formatting:

```bash
# Check for lint issues
ruff check .

# Auto-fix lint issues
ruff check --fix .

# Check formatting
ruff format --check .

# Apply formatting
ruff format .
```

**Pre-commit hooks** (included in the `[dev]` extra; install once after the canonical setup):
```bash
pre-commit install                              # one-time, after the canonical install
pre-commit run --all-files                      # manual run on the whole tree (matches the CI lint gate)
```

> **Caveat:** if `pre-commit install` errors with `Cowardly refusing to install hooks with core.hooksPath set`, your git is configured to use a custom hooks directory (common with Husky / nx / shared dev configs). Workaround: `git config --unset core.hooksPath` then re-run `pre-commit install`, or run `pre-commit run --all-files` manually before each commit. CI runs the same hook either way, so a clean local hook is convenience, not correctness.

> **CI parity.** The local pre-commit one-liner above matches the CI **lint gate** (`uv run pre-commit run --all-files` in `.github/workflows/test.yml`). CI additionally runs the full test matrix on multiple Python versions (3.10–3.14) and asserts a 90% coverage floor (`pytest --cov=src/notebooklm --cov-report=term-missing --cov-fail-under=90`). The lint+test failure modes are caught locally; the multi-Python-version drift is not — `uv run pytest --cov=src/notebooklm --cov-report=term-missing --cov-fail-under=90` here uses your local Python version only.

### Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with clear commit messages
3. Ensure tests pass: `pytest`
4. Ensure lint passes: `ruff check .`
5. Ensure formatting: `ruff format --check .`
6. Submit a PR with a description of changes

### Pull Request Quality Expectations

- **Reference an issue**: PRs should link to an existing issue or clearly describe the problem being solved. If no issue exists, open one first for discussion.
- **AI-assisted contributions**: Welcome, but the submitter must review, understand, and test the code before submitting. PRs that appear to be unreviewed AI output will be closed.
- **No duplicates**: Check existing open PRs before submitting. Duplicate PRs for the same issue will be closed in favor of the first or best submission.
- **Accurate severity**: Claims of "critical" bugs must include evidence (stack trace, reproduction steps, affected users). Routine edge cases are not critical.
- **Tested locally**: All PRs must include evidence of local testing. The PR template includes a checklist for this.

### Dependency upper bounds

Every runtime and `[project.optional-dependencies]` entry in `pyproject.toml` must have an upper bound — typically `<currentmajor + 1` (or `<currentminor + 1` for pre-1.0 packages like `httpx`). The bound protects downstream installs from a breaking new release that lands before we have time to test it.

When you bump a cap (e.g. moving `pytest>=8.0,<10` to `pytest>=8.0,<11`):

1. Run `uv lock --refresh` and `uv sync --frozen --extra browser --extra dev --extra markdown` locally.
2. Run the full pre-commit one-liner above.
3. Mention the upgrade rationale in the PR description.

The `dependency-audit` workflow (`.github/workflows/dependency-audit.yml`) runs `pip-audit --strict` against the locked env on every push to `main` and nightly. It is currently in soft-launch mode (`continue-on-error: true`) and will be flipped to a hard merge gate after the first release cycle. New deps should still pass `pip-audit` cleanly when introduced.

### Test tiers

The test suite is split into three tiers by network/auth dependency. Place new tests in the tier that matches their isolation profile — a tier-enforcement hook will eventually fail PRs that mis-tier a test.

| Tier | Location | What lives here | Network | Auth |
|------|----------|-----------------|---------|------|
| Unit | `tests/unit/` | Pure-Python tests + `pytest_httpx` (`httpx_mock`) request-level mocks. Encoder/decoder, dataclasses, helpers, CLI boundary, and httpx_mock-driven API tests. | None (mocked) | None |
| Integration | `tests/integration/` | VCR cassette replay only — `@pytest.mark.vcr` / `notebooklm_vcr.use_cassette(...)` against recorded fixtures in `tests/cassettes/`. | None (replayed) | None |
| E2E | `tests/e2e/` | Real NotebookLM API. Marked `@pytest.mark.e2e`; excluded from the default `pytest` run via `addopts = --ignore=tests/e2e`. | Real | Required (`notebooklm login`) |

Run a tier explicitly:

```bash
uv run pytest tests/unit
uv run pytest tests/integration
uv run pytest tests/e2e -m e2e        # requires auth
```

Quick guidance:

- Reach for `httpx_mock` when you need to assert on outgoing request shape (headers, body, cookies, URL) or stub a small response — put the test under `tests/unit/`.
- Reach for VCR when you want to replay a recorded server response end-to-end against the real RPC decoder — put the test under `tests/integration/` and record the cassette per `docs/rpc-development.md`.
- Reach for E2E only when you need to validate a live round-trip against Google's servers — put the test under `tests/e2e/` and mark it `@pytest.mark.e2e`.

---

## Documentation Rules for AI Agents

**IMPORTANT:** All AI agents (Claude, Gemini, etc.) must follow these rules when working in this repository.

### File Creation Rules

1. **No Root Rule** - Never create `.md` files in the repository root unless explicitly instructed by the user.

2. **Modify, Don't Fork** - Edit existing files; never create `FILE_v2.md`, `FILE_REFERENCE.md`, or `FILE_updated.md` duplicates.

3. **Scratchpad Protocol** - All analysis, investigation logs, and intermediate work go in `docs/scratch/` with date prefix: `YYYY-MM-DD-<context>.md`

4. **Consolidation First** - Before creating new docs, search for existing related docs and update them instead.

### Protected Sections

Some sections within files are critical and must not be modified without explicit user approval.

**Inline markers** (source of truth):
```markdown
<!-- PROTECTED: Do not modify without approval -->
## Critical Section Title
Content that should not be changed by agents...
<!-- END PROTECTED -->
```

For code files:
```python
# PROTECTED: Do not modify without approval
class RPCMethod(Enum):
    ...
# END PROTECTED
```

**Rule:** Never modify content between `PROTECTED` and `END PROTECTED` markers unless explicitly instructed by the user.

### Design Decision Lifecycle

Design decisions should be captured where they're most useful, not in separate documents that become stale.

| When | Where | What to Include |
|------|-------|-----------------|
| **Feature work** | PR description | Design rationale, edge cases, alternatives considered |
| **Specific decisions** | Commit message | Why this approach was chosen |
| **Large discussions** | GitHub Issue | Link from PR, spans multiple changes |
| **Investigation/debugging** | `docs/scratch/` | Temporary work, delete when done |

**Why not design docs?** Separate design documents accumulate and become stale. PR descriptions stay attached to the code changes, are searchable in GitHub, and don't clutter the repository.

**Scratch files** (`docs/scratch/`) - Temporary investigation logs and intermediate work. Format: `YYYY-MM-DD-<context>.md`. Periodically cleaned up.

### Naming Conventions

| Type | Format | Example |
|------|--------|---------|
| Root GitHub files | `UPPERCASE.md` | `README.md`, `CONTRIBUTING.md` |
| Agent files | `UPPERCASE.md` | `CLAUDE.md`, `AGENTS.md` |
| Subfolder README | `README.md` | `docs/adr/README.md` |
| All other docs/ files | `lowercase-kebab.md` | `cli-reference.md`, `contributing.md` |
| Scratch files | `YYYY-MM-DD-context.md` | `2026-01-06-debug-auth.md` |

### Status Headers

All documentation files should include status metadata:

```markdown
**Status:** Active | Deprecated
**Last Updated:** YYYY-MM-DD
```

Agents should ignore files marked `Deprecated`.

### Information Management

1. **Link, Don't Copy** - Reference README.md sections instead of repeating commands. Prevents drift between docs.

2. **Scoped Instructions** - Subfolders like `docs/examples/` may have their own README.md with folder-specific rules.

---

## Documentation Structure

```
docs/
├── adr/                   # Architectural Decision Records (ADRs)
├── architecture.md        # Monolithic Session to composable capabilities architecture map
├── auth-cookie-lifecycle.md      # Cookie expiration mitigation strategies and keepalive loops
├── cli-exit-codes.md      # CLI exit-code convention (binding contract for scripts/CI)
├── cli-reference.md       # CLI command reference
├── configuration.md       # Storage, profiles, and settings
├── deprecations.md        # Staged API deprecations tracker
├── development.md         # Architecture, testing, and VCR cassette practices
├── installation.md        # Canonical install guide (personas, extras, platform notes)
├── python-api.md          # Python API reference
├── refactor-history.md    # Historical record of the Tier 12/13 refactor + downstream migration tables
├── releasing.md           # Release checklist
├── rpc-development.md     # RPC capture and debugging
├── rpc-reference.md       # RPC payload structures and Content Type Codes
├── stability.md           # API versioning and stability policy
├── troubleshooting.md     # Common issues and solutions
└── examples/              # Runnable example scripts
```

> When adding or modifying a CLI command, follow the [CLI Exit-Code Convention](docs/cli-exit-codes.md) — the policy table and the two intentional exceptions (`source stale`, `source wait`) are binding.
