# Contributing to notebooklm-py

## For Human Contributors

### Getting Started

```bash
# Install in development mode
pip install -e ".[all]"
playwright install chromium

# Run tests
pytest

# Run linter
ruff check src/ tests/

# Run formatter
ruff format src/ tests/
```

### Code Quality

This project uses **ruff** for linting and formatting:

```bash
# Check for lint issues
ruff check src/ tests/

# Auto-fix lint issues
ruff check --fix src/ tests/

# Check formatting
ruff format --check src/ tests/

# Apply formatting
ruff format src/ tests/
```

**Pre-commit hooks** (optional but recommended):
```bash
pip install pre-commit
pre-commit install
```

### Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with clear commit messages
3. Ensure tests pass: `pytest`
4. Ensure lint passes: `ruff check src/ tests/`
5. Ensure formatting: `ruff format --check src/ tests/`
6. Submit a PR with a description of changes

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
