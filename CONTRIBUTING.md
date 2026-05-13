# Contributing to notebooklm-py

## For Human Contributors

### Getting Started

```bash
# Canonical contributor install (respects uv.lock)
uv sync --frozen --extra browser --extra dev --extra markdown
source .venv/bin/activate
uv run playwright install chromium
pre-commit install

# Run the full pre-commit suite (matches what CI runs).
# IMPORTANT: use the broad `.` scope, not `src/ tests/` — the pre-commit hook
# in CI invokes ruff-format on the whole tree and is stricter than a narrow scope.
uv run ruff format --check . && \
    uv run ruff check . && \
    uv run mypy src/notebooklm --ignore-missing-imports && \
    uv run pytest
```

**No uv?** Plain pip works as a fallback (won't enforce the lockfile, so you may resolve newer dep versions than CI):
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"   # [all] = browser + dev + markdown (no cookies; see installation.md)
playwright install chromium
pre-commit install
```

For full prerequisites, headless setup, optional extras (`[cookies]`, `[markdown]`), and platform notes, see [docs/installation.md#e-contributor](docs/installation.md#e-contributor).

The `browser` extra is part of the contributor install because the default unit
suite imports and patches `playwright.sync_api`. The command
`uv sync --frozen --extra dev` is only the test/lint toolchain; it is not enough
for `uv run pytest`.

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

> **CI parity.** The local pre-commit one-liner above matches the CI **lint gate** (`uv run pre-commit run --all-files` in `.github/workflows/test.yml`). CI additionally runs the full test matrix on multiple Python versions (3.10–3.14) and asserts a 90% coverage floor (`pytest --cov=src/notebooklm --cov-fail-under=90`). The lint+test failure modes are caught locally; the multi-Python-version drift is not — `uv run pytest -q` here uses your local Python version only.

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
| Subfolder README | `README.md` | `docs/examples/README.md` |
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
├── installation.md        # Canonical install guide (personas, extras, platform notes)
├── cli-reference.md       # CLI command reference
├── python-api.md          # Python API reference
├── configuration.md       # Storage and settings
├── troubleshooting.md     # Common issues and solutions
├── stability.md           # API versioning policy
├── development.md         # Architecture and testing
├── releasing.md           # Release checklist
├── rpc-development.md     # RPC capture and debugging
├── rpc-reference.md       # RPC payload structures
└── examples/              # Runnable example scripts
```
