---
name: notebooklm
description: Complete API for Google NotebookLM - full programmatic access including features not in the web UI. Create notebooks, add sources, generate all artifact types, download in multiple formats. Activates on explicit /notebooklm or intent like "create a podcast about X"
---

# NotebookLM Automation

Complete programmatic access to Google NotebookLM—including capabilities not exposed in the web UI. Create notebooks, add sources (URLs, YouTube, PDFs, audio, video, images), chat with content, generate all artifact types, and download results in multiple formats.

This skill is **CLI-first**: every example below runs `notebooklm ...` directly.

> **If `notebooklm` is not on PATH,** substitute `sh <skill-dir>/scripts/nlm` for `notebooklm` in every command in this skill (`<skill-dir>` is the directory containing this SKILL.md; in Claude Code specifically that's `${CLAUDE_SKILL_DIR}`, but don't bake the variable into command lines — other harnesses don't set it). Example: `notebooklm list` becomes `sh <skill-dir>/scripts/nlm list`.
> **Windows fallback:** run `uvx --from "notebooklm-py[browser]" notebooklm ...` directly instead of `sh scripts/nlm` (no POSIX shell required).

## Getting the CLI

```bash
pip install "notebooklm-py[browser]"   # mandatory; errors must propagate

# [cookies] (rookiepy) is optional and known to FAIL TO BUILD on Python 3.13+.
if python -c "import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)"; then
    pip install "notebooklm-py[cookies]"   # errors propagate
else
    echo "Skipping [cookies] on Python 3.13+ (rookiepy unavailable). Use 'notebooklm login' interactively."
fi
```

Full install matrix (extras, GitHub-tag install, contributor flow): [docs/installation.md](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md). Skill-install methods, prerequisites, CI/CD & multi-account env vars, and sandboxed agents (Claude Cowork/headless): [references/setup.md](references/setup.md).

⚠️ **DO NOT install from the `main` branch** (`pip install git+https://...notebooklm-py`) — it may contain unreleased/unstable changes. Use PyPI or a release tag.

## Auth Gate (run before anything else)

1. `notebooklm auth check --test --json` → require BOTH `"status": "ok"` AND `"checks.token_fetch": true`. Bare `--json` (no `--test`) only proves the cookie file parses — a stale cookie file still reports `"status": "ok"`, a false-positive trap.
2. If it fails → `notebooklm login` (opens a browser) or, headless, `notebooklm login --browser-cookies <browser>` (requires the `[cookies]` extra).
3. Cookies stale but previously working → `notebooklm auth refresh` (cheap, no new profile) before a full re-login.
4. `notebooklm list --json` → expect valid JSON (may be empty for new accounts) as a second confirmation.

> `notebooklm status` reports *context state* (selected notebook) — it is **not** an auth check. Always use `auth check` to verify authentication.

Full walkthrough (browser-cookie targeting, `auth inspect` account survey, refresh semantics): [references/setup.md](references/setup.md).

**CI/CD, multi-account, parallel agents:** three env vars control config location/profile/inline-auth for automation — `NOTEBOOKLM_HOME` (config dir), `NOTEBOOKLM_PROFILE` (active profile), `NOTEBOOKLM_AUTH_JSON` (inline `storage_state.json`, no file writes). Full setup and isolation strategies: [references/setup.md](references/setup.md).

## When This Skill Activates

**Explicit:** User says "/notebooklm", "use notebooklm", or mentions the tool by name.

**Intent detection:** Recognize requests like:
- "Create a podcast about [topic]"
- "Summarize these URLs/documents"
- "Generate a quiz from my research"
- "Turn this into an audio overview"
- "Create flashcards for studying"
- "Generate a video explainer" / "Make an infographic" / "Create a mind map"
- "Download the quiz as markdown"
- "Add these sources to NotebookLM"

## Autonomy Rules

**Run automatically (no confirmation):**
- `notebooklm status` — check context (⚠️ context only, not an auth check)
- `notebooklm auth check[ --test]` / `auth inspect` / `auth refresh[ --browser-cookies <browser>]` — diagnose or non-destructively refresh auth
- `notebooklm list` / `source list` / `artifact list` / `label list` / `profile list` — list operations
- `notebooklm language list` / `get` / `set` — global setting, but not destructive
- `notebooklm suggest-prompts` / `history` (without `--save`) — read-only
- `notebooklm doctor[ --fix]` — environment health check
- `notebooklm use <id>` — set context (⚠️ single-agent only — use `-n <id>` in parallel workflows)
- `notebooklm create` — create notebook
- `notebooklm ask "..."` (without `--save-as-note`) — chat queries
- `notebooklm source add` — add sources
- `notebooklm profile create` / `switch` — profile management
- Wait/poll commands (`artifact wait`, `source wait`, `research wait`, `research status`) — **only when running inside a subagent**

**Ask before running:**
- Destructive: `delete`, `source delete`/`delete-by-title`/`clean`, `note delete`, `artifact delete`, `label delete`, `share remove`, `auth logout`, `clear`, `profile delete`, `ask --new` — once approved, pass `--yes`/`-y` where supported (some `--json` destructive commands require it explicitly and otherwise return a structured confirmation error)
- `notebooklm research cancel <run_id>` — fire-and-forget; an in-progress run transitions to FAILED; re-check with `research status`
- `notebooklm generate *` — long-running, may fail on rate limits
- `notebooklm download *` — writes to the filesystem
- `artifact wait` / `source wait` / `research wait` — long-running **only when in the main conversation** (fine inside a subagent — see above)
- `notebooklm ask "..." --save-as-note` / `history --save` — writes a note

Full per-command rationale: [references/setup.md](references/setup.md) and [references/workflows.md](references/workflows.md).

## Quick Reference

| Task | Command |
|------|---------|
| Authenticate | `notebooklm login` |
| Diagnose auth (network-validated) | `notebooklm auth check --test --json` |
| List notebooks | `notebooklm list` |
| Create notebook | `notebooklm create "Title"` |
| Set context (single-agent only) | `notebooklm use <notebook_id>` |
| Add a source | `notebooklm source add "https://..."` |
| List sources | `notebooklm source list` |
| Chat | `notebooklm ask "question"` |
| Generate podcast | `notebooklm generate audio "instructions"` |
| List artifacts / check status | `notebooklm artifact list` |
| Wait for artifact | `notebooklm artifact wait <artifact_id>` |
| Download audio | `notebooklm download audio ./output.mp3` |
| Download report | `notebooklm download report ./report.md` |
| Set language | `notebooklm language set zh_Hans` |
| Health check | `notebooklm doctor` |

Full table (~90 rows: labels, research, sharing, profiles, all download formats): [references/command-reference.md](references/command-reference.md).

**Beyond the web UI:** this CLI also exposes batch downloads (`download <type> --all`), quiz/flashcard export as JSON/Markdown/HTML, slide decks as editable PPTX, mind-map JSON export, data-table CSV export, source fulltext retrieval, saving chat answers/history as notes, and programmatic `share` management — none of which the NotebookLM web app offers. Full list: [references/command-reference.md](references/command-reference.md).

**Extract IDs from `--json` output:** `.notebook.id` (from `create`), `.source.id` (from `source add`), `.task_id` (from `generate *`) — e.g. `notebooklm create "Title" --json | jq -r .notebook.id`. Full output shapes and JSON schemas: [references/command-reference.md](references/command-reference.md).

**Parallel safety:** pass `-n <id>` / `--notebook <id>` explicitly on notebook-scoped commands instead of `use` when multiple agents share a profile — concurrent `use` calls overwrite each other's context.

**Partial IDs:** first 6+ characters of a UUID work for ID-based commands (fails if ambiguous); prefer full UUIDs in automation.

**Language is global, not per-notebook:** `notebooklm language set <code>` changes the artifact-generation language for the whole account; override a single generation with `--language` on `generate *` instead of changing the global setting. Language list/details: [references/command-reference.md](references/command-reference.md).

**Long prompts:** shell command-line length limits can truncate a long `ask`/`generate`/`add-research` query — use `--prompt-file path/to/prompt.txt` instead of the positional text argument (mutually exclusive with it). Not supported on `generate mind-map`.

## Workflow Scripts

Self-contained PEP 723 scripts under `scripts/` for the most common multi-step
sequences. Each talks to the Python API directly (only the `notebooklm-py`
package is required, not the `notebooklm` binary):

| Script | Purpose | Invocation |
|--------|---------|------------|
| `scripts/research_to_podcast.py` | Research a topic end-to-end into a downloaded podcast | `uv run <skill-dir>/scripts/research_to_podcast.py "TOPIC" [--mode fast\|deep]` |
| `scripts/bulk_import.py` | Add many URLs/files to a notebook and wait for them to be ready | `uv run <skill-dir>/scripts/bulk_import.py SOURCE... [--from-file list.txt]` |
| `scripts/generate_artifact.py` | Generate any artifact type, wait for it, and download it | `uv run <skill-dir>/scripts/generate_artifact.py TYPE -n ID [PROMPT]` |

Each prints progress to stderr and a one-line JSON summary to stdout (exit 0
success / 1 user error / 2 timeout). Full recipes and manual CLI fallbacks:
[references/workflows.md](references/workflows.md).

## Common Workflows (at a glance)

- **Research → Podcast:** research a topic, import discovered sources, generate + download audio. Use `scripts/research_to_podcast.py`, or do it manually/with a subagent — see [references/workflows.md](references/workflows.md).
- **Document Analysis:** create a notebook, add doc(s)/URL(s), then `ask` questions interactively.
- **Bulk Import:** add many URLs/files to one notebook and wait for them to be ready. Use `scripts/bulk_import.py`. Per-notebook source limits vary by plan (Standard 50 / Plus 100 / Pro 300 / Ultra 600); the CLI does not enforce them.
- **Deep Web Research:** `notebooklm source add-research "query" --mode deep --no-wait`, then a subagent runs `research wait --import-all` in the background.

**Rate-limit-prone generation:** audio, video, quiz, flashcards, infographic, and slide-deck generation can fail due to Google-side rate limits (not a bug) — retry after 5-10 minutes or check `artifact list`. Mind-map, study-guide, report, and data-table generation are reliable. Details: [references/troubleshooting.md](references/troubleshooting.md).

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (not found, validation, auth, rate limit) — check stderr |
| 2 | Timeout (`wait` commands) or unexpected/system error |
| 130 | Cancelled by user (Ctrl-C / SIGINT) |

Full exception → exit-code mapping: [docs/cli-exit-codes.md](https://github.com/teng-lin/notebooklm-py/blob/main/docs/cli-exit-codes.md). Condensed decision tree: [references/troubleshooting.md](references/troubleshooting.md).

## Reference Index (progressive disclosure)

Read these only when you need the detail — don't preload them:

- [references/setup.md](references/setup.md) — install detail beyond the two `pip install` lines above, skill-install methods, CI/CD & multi-account & parallel-agent env vars, and the full agent auth-verification walkthrough.
- [references/command-reference.md](references/command-reference.md) — the full command table, `--json` output formats and citation guidance, JSON schemas, all Generation Types options/footnotes, features beyond the web UI, long-prompt handling, and language configuration.
- [references/workflows.md](references/workflows.md) — the common multi-step workflows (research-to-podcast, document analysis, bulk import, deep research), subagent `Task(...)` patterns for long-running work, and the processing-time/timeout table.
- [references/troubleshooting.md](references/troubleshooting.md) — the error decision tree, expanded exit codes, known limitations (which generation types are rate-limit-prone), and the `--help` map.
