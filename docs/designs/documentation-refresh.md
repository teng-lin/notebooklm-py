# Documentation Refresh Design

**Status:** Approved (Revised)
**Date:** 2026-01-06
**Revised:** 2026-01-06 (incorporated Gemini critique)

## Problem Statement

Documentation has drifted significantly from the codebase after major refactoring:
- CLI split from monolithic `notebooklm_cli.py` into `cli/` package with 12 modules
- Service layer removed; API changed from `NotebookService(client)` to `client.notebooks.list()`
- Ghost methods documented (`add_pdf()`) that don't exist
- Stale file references, line numbers, and repository structure diagrams
- Inconsistent file naming conventions (PascalCase vs lowercase-kebab)

## Target Audiences

1. **LLM agents** using CLI for automation
2. **Python developers** building applications with the library
3. **Human CLI users** running commands directly
4. **Contributors** extending or debugging the library
5. **End users** who use LLMs to interact with NotebookLM

## Design Decisions

### Naming Conventions (Updated)

| Type | Format | Example |
|------|--------|---------|
| Root GitHub files | UPPERCASE.md | README.md, CONTRIBUTING.md |
| Agent files | UPPERCASE.md | CLAUDE.md, AGENTS.md |
| All docs/ files | lowercase-kebab.md | getting-started.md, cli-reference.md |
| Design docs | lowercase-kebab.md | documentation-refresh.md |
| Scratch files | YYYY-MM-DD-context.md | 2026-01-06-debug.md |

### Cleanup (Deletions)

| File | Reason |
|------|--------|
| `docs/FILE_UPLOAD_IMPLEMENTATION.md` | Implementation complete, info stale |
| `docs/designs/architecture-review.md` | Refactoring done, was planning doc |
| `docs/scratch/2026-01-05-e2e-test-analysis.md` | Info in KnownIssues |
| `docs/scratch/2026-01-05-extraction-verification.md` | Temporary work |
| `docs/scratch/2026-01-05-test-fix-summary.md` | Temporary work |
| `GEMINI.md` | Merged into AGENTS.md |
| `docs/API.md` | Replaced by python-api.md |
| `docs/EXAMPLES.md` | Merged into python-api.md |
| `docs/reference/KnownIssues.md` | Merged into troubleshooting.md |
| `docs/reference/RpcProtocol.md` | Moved to contributing/ |

### Consolidations

| From | To |
|------|-----|
| `docs/reference/internals/*.md` (5 files) | `docs/reference/internals/discovery.md` |
| `docs/API.md` + `docs/EXAMPLES.md` | `docs/python-api.md` |
| `docs/reference/KnownIssues.md` | `docs/troubleshooting.md` |
| `GEMINI.md` + `AGENTS.md` | `AGENTS.md` |

### New Files

| File | Purpose |
|------|---------|
| `docs/getting-started.md` | Install → login → first workflow |
| `docs/cli-reference.md` | Accurate CLI reference matching Click groups |
| `docs/python-api.md` | Full API reference + examples + migration guide |
| `docs/troubleshooting.md` | Errors, known issues, workarounds |
| `docs/configuration.md` | Storage, env vars, settings |
| `docs/examples/quickstart.py` | Runnable end-to-end example |
| `docs/examples/research-to-podcast.py` | Workflow script |
| `docs/examples/bulk-import.py` | Advanced usage script |
| `docs/contributing/architecture.md` | Code organization, layers |
| `docs/contributing/debugging.md` | Network capture, RPC tracing |
| `docs/contributing/testing.md` | Running tests, E2E auth |
| `docs/contributing/rpc-protocol.md` | Moved from reference/ |

### Final Structure

```
Root:
├── README.md              # Lean: pitch, install, quick start, links
├── CONTRIBUTING.md        # Human + agent rules, PR process
├── CLAUDE.md              # Claude Code behavioral hints
├── AGENTS.md              # Other LLMs (merged from GEMINI.md)
├── CHANGELOG.md           # Release history (unchanged)

docs/
├── getting-started.md     # Install → login → first workflow
├── cli-reference.md       # Accurate command reference + workflows
├── python-api.md          # Migration guide + API reference + examples
├── configuration.md       # Storage, env vars, settings
├── troubleshooting.md     # Errors, known issues, workarounds
├── examples/
│   ├── quickstart.py      # End-to-end runnable script
│   ├── research-to-podcast.py
│   └── bulk-import.py
├── contributing/
│   ├── architecture.md    # Code organization, layers
│   ├── debugging.md       # Network capture, RPC tracing
│   ├── testing.md         # Test running, E2E auth
│   └── rpc-protocol.md    # Deep dive (moved from reference/)
└── reference/
    └── internals/
        └── discovery.md   # Consolidated reverse-engineering notes
```

## Content Guidelines

### README.md (Lean)

- Project pitch (one paragraph)
- Installation commands
- Quick start (5 CLI commands showing core workflow)
- Links to detailed documentation
- License

### cli-reference.md (Accurate Command Reference)

**Section 1: Command Structure**
```markdown
All commands follow this pattern:
notebooklm [--storage PATH] <command> [OPTIONS] [ARGS]

Commands are organized into:
- Session commands (login, use, status, clear)
- Notebook commands (list, create, delete, rename, ...)
- Chat commands (ask, configure, history)
- Grouped commands (source, artifact, generate, download, note)
```

**Section 2: Quick Reference Tables**
```markdown
### Session Commands
| Command | Description | Example |
|---------|-------------|---------|
| `login` | Authenticate via browser | `notebooklm login` |
| `use <id>` | Set active notebook | `notebooklm use abc123` |

### Source Commands (`notebooklm source <cmd>`)
| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `add <content>` | URL/file/text | -- | `source add "https://..."` |
| `add-research <query>` | Search query | `--mode [fast|deep]` | `source add-research "AI" --mode deep` |
```

**Section 3: Full Command Details**
Each command group with all options, flags, and real examples.

**Section 4: Common Workflows**
```markdown
### Research → Podcast
# 1. Create notebook
notebooklm create "Climate Research"
# Output: Created notebook abc123

# 2. Set as active
notebooklm use abc123

# 3. Add sources
notebooklm source add "https://en.wikipedia.org/wiki/Climate_change"
notebooklm source add-research "climate policy" --mode deep

# 4. Generate podcast
notebooklm generate audio --format debate

# 5. Download
notebooklm download audio ./podcast.mp3
```

### python-api.md

**Section 1: Migration Guide (v0.x → v1.x)**
```markdown
## Breaking Changes

### Service Layer Removed
Before (v0.x):
```python
from notebooklm.services import NotebookService
service = NotebookService(client)
notebooks = await service.list()
```

After (v1.x):
```python
notebooks = await client.notebooks.list()
```
```

**Section 2: Quick Start**
5-10 line example to get started.

**Section 3: Core Concepts**
- Async patterns (`async with await`)
- Error handling (`RPCError` and common failures)
- Streaming responses

**Section 4: API Reference**
Every method, parameter, return type documented.

**Section 5: Enums & Constants**
All enums with values and descriptions.

### configuration.md

```markdown
## Storage Location
Default: `~/.notebooklm/storage_state.json`
Override: `--storage PATH` flag

## Browser Profile
Location: `~/.notebooklm/browser_profile/`
Purpose: Persistent Chromium profile to avoid Google bot detection

## Environment Variables
| Variable | Purpose | Default |
|----------|---------|---------|
| `NOTEBOOKLM_STORAGE` | Override storage path | `~/.notebooklm/storage_state.json` |
| `NOTEBOOKLM_DEBUG` | Enable debug logging | `0` |

## Storage File Format
```json
{
  "cookies": [...],
  "origins": [...]
}
```
```

### troubleshooting.md

- Common errors and fixes
- Known issues (migrated from KnownIssues.md)
- Google API changes and compatibility
- Browser login failures
- Rate limiting and quotas
- Network debugging tips

### CLAUDE.md

- Slim behavioral hints only
- Updated repository structure showing `cli/` package
- Fixed file references
- Guidance on CLI grouped commands
- When to suggest CLI vs Python API
- Link to CONTRIBUTING.md for shared rules

### Contributing Docs

- **architecture.md**: Three-layer design, file organization, `_*.py` naming convention
- **debugging.md**: Network capture in Chrome DevTools, decoding batchexecute, RPC tracing
- **testing.md**: pytest commands, E2E auth setup, fixtures, writing new tests
- **rpc-protocol.md**: Full protocol reference (moved from reference/)

### docs/examples/

Runnable Python scripts:
- **quickstart.py**: End-to-end workflow (create → add sources → generate → download)
- **research-to-podcast.py**: Deep research to podcast workflow
- **bulk-import.py**: Import multiple files/URLs

## Implementation Phases

### Phase 1: Cleanup & Structure
1. Delete obsolete files
2. Create new directories (`docs/contributing/`, `docs/examples/`)
3. Update naming convention guidelines in CONTRIBUTING.md and docs/README.md

### Phase 2: Core Documentation
4. Write `docs/getting-started.md`
5. Write `docs/cli-reference.md` (accurate command reference)
6. Write `docs/configuration.md`
7. Write `docs/python-api.md` (with migration guide)
8. Merge KnownIssues → `docs/troubleshooting.md`

### Phase 3: Consolidation & Migration
9. Consolidate internals → `docs/reference/internals/discovery.md`
10. Move `RpcProtocol.md` → `docs/contributing/rpc-protocol.md` (rename to lowercase)
11. Write contributor docs (architecture, debugging, testing)

### Phase 4: Root Files & Cleanup
12. Update `README.md` (slim down, add links)
13. Update `CLAUDE.md` (fix all stale references)
14. Merge `GEMINI.md` into `AGENTS.md`, delete `GEMINI.md`
15. Update `CONTRIBUTING.md` with enhanced contributor guide
16. Update `docs/README.md` to reflect new structure

### Phase 5: Examples & Validation
17. Write runnable example scripts in `docs/examples/`
18. Delete old files (`docs/API.md`, `docs/EXAMPLES.md`, etc.)
19. Verify all code examples run correctly

## Validation Checklist

- [ ] All code examples in docs are runnable
- [ ] CLI reference matches actual `--help` output
- [ ] No references to old file paths or removed methods
- [ ] Naming conventions consistent throughout
- [ ] All links between docs work
