---
name: notebooklm
description: Complete API for Google NotebookLM - full programmatic access including features not in the web UI. Create notebooks, add sources, generate all artifact types, download in multiple formats. Activates on explicit /notebooklm or intent like "create a podcast about X"
---

# NotebookLM Automation

Complete programmatic access to Google NotebookLM—including capabilities not exposed in the web UI. Create notebooks, add sources (URLs, YouTube, PDFs, audio, video, images), chat with content, generate all artifact types, and download results in multiple formats.

## Installation

**From PyPI (Recommended for AI agents — Python-version-aware):**
```bash
pip install "notebooklm-py[browser]"   # mandatory; errors must propagate

# [cookies] (rookie-cookies) is optional and supported on Python 3.13+.
pip install "notebooklm-py[cookies]"   # errors propagate
```

> Full install matrix (extras, headless servers, contributor flow): [Installation guide on GitHub](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md).

**Android backend (opt-in):** install `pip install "notebooklm-py[android]"`,
then select it with `notebooklm --backend android ...` or
`NOTEBOOKLM_BACKEND=android`. In Python, pass `backend="android"` to
`NotebookLMClient.from_storage()` or the client constructor. Android requires
the selected profile's `master_token.json` and mints short-lived mobile bearer
tokens from it; `[all]` deliberately does not include the Android extra. All
public namespaces stay on the Android transport. Consult the
[Android backend guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/android/README.md)
for setup and security guidance.

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

**Skill install methods:**

- `notebooklm skill install` installs this skill into the supported local agent directories managed by the CLI.
- `npx skills add teng-lin/notebooklm-py` installs this skill from the GitHub repository into compatible agent skill directories.
- If you are already reading this file inside an agent skill directory, the skill is already installed. You only need the Python package and authentication below.

**CLI-managed install:**
```bash
notebooklm skill install
```

## Prerequisites

**IMPORTANT:** Before using any command, you MUST authenticate:

```bash
notebooklm login          # Opens browser for Google OAuth
notebooklm list           # Verify authentication works
```

If commands fail with authentication errors, re-run `notebooklm login`.

### CI/CD, Multiple Accounts, and Parallel Agents

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

### Sandboxed Agents (Claude Cowork / Headless)

Sandboxed, no-display agent environments — **Claude Cowork** (Anthropic's desktop agent for non-developers) and similar headless sandboxes — can't run `notebooklm login` (it needs a browser), and they reset between sessions. Everything else works with two adjustments:

1. **Bootstrap each session.** The sandbox resets, so install at the start of every session. You do **not** need `[browser]`/Playwright here — that extra exists only for the interactive `login` flow, which you run on a host machine, not in the sandbox. Chat, sources, generation, and download all run on the base install:
   ```bash
   pip install notebooklm-py   # no [browser] needed for queries/generation
   ```
   (This is the one place the mandatory `[browser]` install at the top of this file does not apply — you are reusing auth, not logging in here.)
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

If Cowork reads `~/.claude/skills/`, `notebooklm skill install` registers this skill there automatically; otherwise build the uploadable archive on the host with `notebooklm skill package` (writes `notebooklm-skill.zip`) and add it via **Claude Settings → Capabilities**. Full recipe (extras matrix, headless auth, CI env-var notes): [installation.md § AI Agent](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md#a-ai-agent-primary-persona).

## Agent Setup Verification

Before starting workflows, verify auth is in place. **Use `--test --json` (not bare `--json`)** — bare `--json` only proves the cookie file parses; `--test` makes a network call and proves the cookies still authenticate against Google.

1. `notebooklm auth check --test --json` → require BOTH `"status": "ok"` AND `"checks.token_fetch": true`. Bare `"status": "ok"` (without `--test`) is a false-positive trap — a stale cookie file passes the parse check.
2. `notebooklm list --json` → expect valid JSON (may be empty for new accounts).
3. **If auth fails or is missing → run `notebooklm login` first.** This is the primary auth path: opens a browser, the user signs in to Google once, and the resulting `storage_state.json` is reused on every subsequent run. Works on any environment with a display.
   - For headless contexts where opening a browser is not feasible, use `notebooklm login --browser-cookies <browser>` instead — extracts the user's already-logged-in cookies from Chrome/Firefox/etc. (requires the `[cookies]` extra, which installs `rookie-cookies`). Use `chrome::<profile-name-or-directory>` to target one Chromium user-profile, or `firefox::<container-name>` / `firefox::none` to target one Firefox container.
   - To survey signed-in Google accounts before picking one: `notebooklm auth inspect --browser <browser>` (read-only; pass `-v` to see which Chromium user-profile each account came from, or `--json` for tooling). Scoped forms such as `notebooklm auth inspect --browser 'chrome::Profile 1'` inspect only that browser profile.
   - Re-run step 1 after login to confirm.
4. **If auth was working but cookies went stale** (Google rotated SIDTS, or you signed in fresh in the browser) **→ refresh the active profile in place instead of full re-login:**
   - `notebooklm auth refresh` — server-side SIDTS refresh against the existing `storage_state.json`. Cheap and silent; safe to run on a schedule (cron / launchd / systemd) at 15–20 min cadence to keep an unattended profile warm.
   - `notebooklm auth refresh --browser-cookies <browser>` — re-extract cookies from a running browser and match them back to the profile's recorded email in `context.json`. Use when the on-disk `storage_state.json` is too stale for the server-side refresh path but you've just signed back into Google in the browser. For Chromium-family browsers with multiple user-profiles (Chrome's `Default`, `Profile 1`, …), refresh fans out across all profiles to find the email — same path as `auth inspect` (issue #571). Use `chrome::<profile-name-or-directory>` when you already know the exact browser profile.
   - Both forms preserve the same `--profile` (no new profile is created).

> **Note:** `notebooklm status` reports *context state* (selected notebook); do not use it to verify auth.

## When This Skill Activates

**Explicit:** User says "/notebooklm", "use notebooklm", or mentions the tool by name

**Intent detection:** Recognize requests like:
- "Create a podcast about [topic]"
- "Summarize these URLs/documents"
- "Generate a quiz from my research"
- "Turn this into an audio overview"
- "Create flashcards for studying"
- "Generate a video explainer"
- "Make an infographic"
- "Create a mind map of the concepts"
- "Download the quiz as markdown"
- "Add these sources to NotebookLM"

## Autonomy Rules

**Run automatically (no confirmation):**
- `notebooklm status` - check context
- `notebooklm auth check` - diagnose auth issues
- `notebooklm auth inspect` - list Google accounts visible to a browser (read-only)
- `notebooklm auth refresh` - server-side SIDTS refresh of the active profile (no new profile, no destructive writes)
- `notebooklm auth refresh --browser-cookies <browser>` - re-extract cookies from a browser into the active profile (rebuilds `storage_state.json` for the same `--profile`, not a new one)
- `notebooklm list` - list notebooks
- `notebooklm source list` - list sources
- `notebooklm artifact list` - list artifacts
- `notebooklm language list` - list supported languages
- `notebooklm language get` - get current language
- `notebooklm artifact wait` - wait for artifact completion (in supported background context)
- `notebooklm source wait` - wait for source processing (in supported background context)
- `notebooklm research status` - check research status
- `notebooklm research wait` - wait for research without `--import-all` (in supported background context)
- `notebooklm use <id>` - set context (⚠️ SINGLE-AGENT ONLY - use `-n` flag in parallel workflows)
- `notebooklm create` - create notebook
- `notebooklm ask "..."` - chat queries (without `--save-as-note`)
- `notebooklm suggest-prompts` - AI-suggested prompts for a notebook (read-only, no state change)
- `notebooklm history` - display conversation history (read-only)
- `notebooklm source add` - add sources
- `notebooklm profile list` - list profiles
- `notebooklm profile create` - create profile
- `notebooklm profile switch` - switch active profile
- `notebooklm doctor` - check environment health

**Ask before running:**
- `notebooklm language set` - changes the account-global output language (or local configuration with `--local`). Prefer a generation command's per-command `--language` override when available.
- `notebooklm delete`, `source delete`, `source delete-by-title`, `source clean`, `note delete`, `artifact delete`, `label delete`, `share remove`, `auth logout`, `clear`, `profile delete`, or `ask --new` - destructive or state-changing. Once approved, pass `--yes`/`-y` where the command supports it. Most destructive `--json` commands still require explicit `--yes` and otherwise return a structured confirmation error (`CONFIRM_REQUIRED` or `VALIDATION_ERROR`, depending on the command family); current exceptions include `share remove --json` and `ask --new --json`, which skip the prompt for non-interactive callers.
- `notebooklm generate *` - long-running, may fail
- `notebooklm download *` - writes to filesystem
- `notebooklm artifact wait` - long-running (when in main conversation)
- `notebooklm source wait` - long-running (when in main conversation)
- `notebooklm research wait` - long-running (when in main conversation)
- `notebooklm research wait --import-all` - state-changing; imports research sources into the notebook in any execution context
- `notebooklm research cancel <run_id>` - state-changing; cancels a running research job (an in-progress job transitions to FAILED). Fire-and-forget: it does not confirm success — re-check with `notebooklm research status`.
- `notebooklm ask "..." --save-as-note` - writes a note
- `notebooklm history --save` - writes a note

## Quick Reference

| Task | Command |
|------|---------|
| Authenticate | `notebooklm login` |
| Authenticate from browser cookies | `notebooklm login --browser-cookies <browser>` |
| Authenticate from one Chromium profile | `notebooklm login --browser-cookies 'chrome::Profile 1'` |
| Authenticate from one Firefox container | `notebooklm login --browser-cookies 'firefox::Work'` |
| Import every signed-in account into its own profile | `notebooklm login --browser-cookies <browser> --all-accounts` |
| Inspect signed-in accounts (read-only, by email) | `notebooklm auth inspect --browser <browser>` |
| Inspect one browser profile/container | `notebooklm auth inspect --browser 'chrome::Profile 1'` |
| Diagnose auth issues | `notebooklm auth check` |
| Diagnose auth (full) | `notebooklm auth check --test` |
| Refresh active profile in place (server-side) | `notebooklm auth refresh` |
| Refresh active profile from a re-signed-in browser | `notebooklm auth refresh --browser-cookies <browser>` |
| Refresh from one Chromium profile | `notebooklm auth refresh --browser-cookies 'chrome::Profile 1'` |
| One-shot cookie keepalive (for cron) | `notebooklm auth refresh --quiet` |
| List notebooks | `notebooklm list` |
| Create notebook | `notebooklm create "Title"` |
| Set context | `notebooklm use <notebook_id>` |
| Show context | `notebooklm status` |
| Add URL source | `notebooklm source add "https://..."` |
| Add file | `notebooklm source add ./file.pdf` |
| Add YouTube | `notebooklm source add "https://youtube.com/..."` |
| List sources | `notebooklm source list` |
| List sources in a label | `notebooklm source list --label <label_id_or_name>` |
| Delete source by ID | `notebooklm source delete <source_id>` |
| Delete source by exact title | `notebooklm source delete-by-title "Exact Title"` |
| Wait for source processing | `notebooklm source wait <source_id>` |
| List labels | `notebooklm label list` |
| Expand label to sources | `notebooklm label sources <label_id_or_name>` |
| Generate labels | `notebooklm label generate --scope unlabeled` |
| Create label | `notebooklm label create "Topic"` |
| Add sources to label | `notebooklm label add <label_id_or_name> <source_id>...` |
| Remove sources from label | `notebooklm label remove <label_id_or_name> <source_id>...` |
| Delete label | `notebooklm label delete <label_id_or_name> --yes` |
| Web research (fast) | `notebooklm source add-research "query"` |
| Web research (deep) | `notebooklm source add-research "query" --mode deep --no-wait` |
| Web research (query from file) | `notebooklm source add-research --prompt-file research_query.txt --mode deep` |
| Check research status | `notebooklm research status` |
| Wait for research | `notebooklm research wait --run-id <run_id>` |
| Wait and import research (confirm first) | `notebooklm research wait --run-id <run_id> --import-all` |
| Cancel research | `notebooklm research cancel <run_id>` (run_id = the `task_id` from `research status`) |
| Suggest questions to ask | `notebooklm suggest-prompts` |
| Chat | `notebooklm ask "question"` |
| Chat (long prompt from file) | `notebooklm ask --prompt-file question.txt` |
| Chat (specific sources) | `notebooklm ask "question" -s src_id1 -s src_id2` |
| Chat (with references) | `notebooklm ask "question" --json` |
| Chat (save answer as note) | `notebooklm ask "question" --save-as-note` |
| Chat (save with title) | `notebooklm ask "question" --save-as-note --note-title "Title"` |
| Show conversation history | `notebooklm history` |
| Save all history as note | `notebooklm history --save` |
| Continue specific conversation | `notebooklm ask "question" -c <conversation_id>` |
| Save history with title | `notebooklm history --save --note-title "My Research"` |
| Get source fulltext | `notebooklm source fulltext <source_id>` |
| Get source guide | `notebooklm source guide <source_id>` |
| Generate podcast | `notebooklm generate audio "instructions"` |
| Generate (long prompt from file) | `notebooklm generate audio --prompt-file instructions.txt` |
| Generate podcast (JSON) | `notebooklm generate audio --json` |
| Generate podcast (specific sources) | `notebooklm generate audio -s src_id1 -s src_id2` |
| Generate video | `notebooklm generate video "instructions"` |
| Generate report | `notebooklm generate report --format briefing-doc` |
| Generate report (append instructions) | `notebooklm generate report --format study-guide --append "Target audience: beginners"` |
| Generate quiz | `notebooklm generate quiz` |
| Revise a slide | `notebooklm generate revise-slide "prompt" --artifact <id> --slide 0` |
| Check artifact status | `notebooklm artifact list` |
| Wait for completion | `notebooklm artifact wait <artifact_id>` |
| Delete artifact | `notebooklm artifact delete <artifact_id> --yes` |
| Download audio | `notebooklm download audio ./output.m4a` |
| Download video | `notebooklm download video ./output.mp4` |
| Download cinematic video | `notebooklm download cinematic-video ./cinematic.mp4` (alias for `download video`) |
| Download infographic | `notebooklm download infographic ./infographic.png` |
| Download slide deck (PDF) | `notebooklm download slide-deck ./slides.pdf` |
| Download slide deck (PPTX) | `notebooklm download slide-deck ./slides.pptx --format pptx` |
| Download report | `notebooklm download report ./report.md` |
| Download mind map | `notebooklm download mind-map ./map.json` |
| Download data table | `notebooklm download data-table ./data.csv` |
| Download quiz | `notebooklm download quiz quiz.json` |
| Download quiz (markdown) | `notebooklm download quiz --format markdown quiz.md` |
| Download flashcards | `notebooklm download flashcards cards.json` |
| Download flashcards (markdown) | `notebooklm download flashcards --format markdown cards.md` |
| Delete notebook | `notebooklm delete -n <id>` (add `--yes` to skip the prompt non-interactively) |
| List languages | `notebooklm language list` |
| Get language | `notebooklm language get` |
| Set language | `notebooklm language set zh_Hans` |
| List profiles | `notebooklm profile list` |
| Create profile | `notebooklm profile create work` |
| Switch profile | `notebooklm profile switch work` |
| Delete profile | `notebooklm profile delete old --yes` (`-y`; `--confirm` is a deprecated alias) |
| Rename profile | `notebooklm profile rename old new` |
| Use profile (one-off) | `notebooklm -p work list` |
| Health check | `notebooklm doctor` |
| Health check (auto-fix) | `notebooklm doctor --fix` |

**Parallel safety:** Use explicit notebook IDs in parallel workflows. Notebook-scoped commands broadly support `-n/--notebook` (ask/history, source, artifact, generate, download, note, label, share, research, and notebook delete/rename/summary/metadata). Download commands also support `-a/--artifact`. For chat, use `-c <conversation_id>` to target a specific conversation.

**Partial IDs:** Use first 6+ characters of UUIDs. Must be unique prefix (fails if ambiguous). Works for ID-based commands such as `use`, `source delete`, and `wait`. For exact source-title deletion, use `source delete-by-title "Title"`. For automation, prefer full UUIDs to avoid ambiguity.

## Command Output Formats

Commands with `--json` return structured data for parsing:

**Create notebook:**
```bash
$ notebooklm create "Research" --json
{"notebook": {"id": "abc123de-...", "title": "Research", "created_at": null}}
# parse with: jq -r .notebook.id
```

**Add source:**
```bash
$ notebooklm source add "https://example.com" --json
{"source": {"id": "def456...", "title": "Example", "type": "web_page", "url": "https://example.com"}}
# parse with: jq -r .source.id
# Note: no `status` field on add — use `source list --json` or `source wait` to check processing state.
```

**Generate artifact:**
```bash
$ notebooklm generate audio "Focus on key points" --json
{"task_id": "xyz789...", "status": "pending"}
# When run with --wait, completed status also includes a `url` field.
```

**Chat with references:**
```bash
$ notebooklm ask "What is X?" --json
{"answer": "X is... [1] [2]", "conversation_id": "...", "turn_number": 1, "is_follow_up": false, "references": [{"source_id": "abc123...", "citation_number": 1, "cited_text": "Relevant passage from source..."}, {"source_id": "def456...", "citation_number": 2, "cited_text": "Another passage..."}]}
```

**Source fulltext (get indexed content):**
```bash
$ notebooklm source fulltext <source_id> --json
{"source_id": "...", "title": "...", "kind": "web_page", "content": "Full indexed text...", "url": null, "char_count": 12345}
```

**Understanding citations:** The `cited_text` in references is often a snippet or section header, not the full quoted passage. The `start_char`/`end_char` positions reference NotebookLM's internal chunked index, not the raw fulltext. Use `SourceFulltext.find_citation_context()` to locate citations:
```python
fulltext = await client.sources.get_fulltext(notebook_id, ref.source_id)
matches = fulltext.find_citation_context(ref.cited_text)  # Returns list[(context, position)]
if matches:
    context, pos = matches[0]  # First match; check len(matches) > 1 for duplicates
```

**Extract IDs:** Singular endpoints wrap their result in an envelope —
parse `.notebook.id` (from `create`), `.source.id` (from `source add`),
or `.task_id` (from `generate *`). The chat `--json` references list uses
`.references[].source_id`.

## Generation Types

Common generate options vary by subcommand:
- `-n, --notebook` targets the notebook.
- `-s, --source` limits generation to specific source(s) on content generators (not `revise-slide`).
- `--language` sets output language where supported (defaults to configured language or `en`).
- `--wait`, `--timeout`, and `--interval` are shared polling controls where waiting is supported.
- `--json` returns machine-readable output.
- `--retry N` automatically retries rate limits on supported subcommands (not `mind-map`).
- `--prompt-file PATH` reads description/query text from a file on `ask`, generation subcommands except `mind-map`, and `source add-research`.

| Type | Command | Options | Download |
|------|---------|---------|----------|
| Podcast | `generate audio` | `--format [deep-dive\|brief\|critique\|debate]`, `--length [short\|default\|long]` | .m4a |
| Video | `generate video` | `--format [explainer\|brief\|cinematic\|short]` (⁴), `--style [auto\|custom\|classic\|whiteboard\|kawaii\|anime\|watercolor\|retro-print\|heritage\|paper-craft]`, `--style-prompt` with `--style custom` | .mp4 |
| Slide Deck | `generate slide-deck` | `--format [detailed\|presenter]`, `--length [default\|short]` (²) | .pdf / .pptx |
| Slide Revision | `generate revise-slide "prompt" --artifact <id> --slide N` | `--wait`, `--notebook` | *(re-downloads parent deck)* |
| Infographic | `generate infographic` | `--orientation [landscape\|portrait\|square]`, `--detail [concise\|standard\|detailed]`, `--style [auto\|sketch-note\|professional\|bento-grid\|editorial\|instructional\|bricks\|clay\|anime\|kawaii\|scientific]` | .png |
| Report | `generate report` | `--format [briefing-doc\|study-guide\|blog-post\|custom]`, `--append "extra instructions"` (¹) | .md |
| Mind Map | `generate mind-map` | `--kind [interactive\|note-backed]` (³) *(default: interactive)* | .json |
| Data Table | `generate data-table` | description required | .csv |
| Quiz | `generate quiz` | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | .json/.md/.html |
| Flashcards | `generate flashcards` | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | .json/.md/.html |

¹ `--append` only customizes the built-in templates. With `--format custom`, pass the prompt as the positional `DESCRIPTION` argument (`notebooklm generate report "PROMPT" --format custom`); `--append` is silently ignored in that mode (the CLI prints a warning).

³ **Two kinds of mind map (issue #1256).** `generate mind-map --kind interactive` (the default) creates the **interactive** studio artifact (what the web app now makes); it is polled to completion. `generate mind-map --kind note-backed` creates the **note-backed** kind — a JSON node tree, generated synchronously. Both emit the same `{mind_map, note_id, kind}` JSON, list under `artifact list --type mind-map`, and export via `download mind-map`. `--instructions` applies only to the note-backed kind.

⁴ **Cinematic video (Veo 3).** `generate video --format cinematic` generates AI documentary footage via Veo 3; it **ignores `--style`**, takes ~30-40 min, and requires a Google AI Ultra subscription. Also exposed as the `generate cinematic-video` alias (which forces `--format cinematic` and a longer default timeout). Download with `download video` or the `download cinematic-video` alias.

² **Portrait / vertical slide decks via prompt.** Slide-deck has no `--orientation` flag (unlike infographic). Treat portrait decks as skill-level prompt guidance, not a typed CLI/API contract: NotebookLM currently honors orientation cues written into the `DESCRIPTION` positional argument. Including phrases like `"9:16 portrait"`, `"vertical layout"`, `"portrait mobile format"`, or `"vertical 9:16 layout"` can make NotebookLM render each slide as a 9:16 portrait image. Empirically:

- The `.pptx` canvas itself may stay 16:9, but each slide's embedded image can be rendered as 9:16 portrait — useful for vertical/mobile video material extracted via `python-pptx`.
- Orientation is steered once at generation time. `generate revise-slide` edits content within an existing slide but does not change its orientation; if a slide falls back to landscape (occasional inconsistency), regenerate the whole deck rather than revising the single page.
- Combine with an explicit page count in the prompt (e.g. `"Create exactly 8 pages, using a vertical 9:16 portrait layout"`) for the most predictable output.

```bash
# Skill prompt hint: ask NotebookLM to render each slide as a 9:16 portrait image
notebooklm generate slide-deck "Create an 8-page deck in 9:16 portrait orientation for mobile viewing" --length default
```

## Features Beyond the Web UI

These capabilities are available via CLI but not in NotebookLM's web interface:

| Feature | Command | Description |
|---------|---------|-------------|
| **Batch downloads** | `download <type> --all` | Download all artifacts of a type at once |
| **Quiz/Flashcard export** | `download quiz --format json` | Export as JSON, Markdown, or HTML (web UI only shows interactive view) |
| **Mind map extraction** | `download mind-map` | Export hierarchical JSON for visualization tools |
| **Data table export** | `download data-table` | Download structured tables as CSV |
| **Slide deck as PPTX** | `download slide-deck --format pptx` | Download slide deck as editable .pptx (web UI only offers PDF) |
| **Slide revision** | `generate revise-slide "prompt" --artifact <id> --slide N` | Modify individual slides with a natural-language prompt |
| **Report template append** | `generate report --format study-guide --append "..."` | Append custom instructions to built-in format templates without losing the format type |
| **Source fulltext** | `source fulltext <id>` | Retrieve the indexed text content of any source |
| **Save chat to note** | `ask "..." --save-as-note` / `history --save` | Save Q&A answers or conversation history as notebook notes |
| **Programmatic sharing** | `share` commands | Manage sharing permissions without the UI |

## Common Workflows

### Research to Podcast (Interactive)
**Time:** 5-10 minutes total

Keep every returned ID and pass it explicitly throughout this workflow; do not rely on the active notebook or the latest visible source/artifact.

1. Run `notebooklm create "Research: [topic]" --json` and capture `.notebook.id` as `{notebook_id}` — *if it fails, diagnose auth with `notebooklm auth check --test --json` and require `checks.token_fetch == true`*.
2. For each URL/document, run `notebooklm source add <source> -n {notebook_id} --json` and capture `.source.id`. Keep every successful ID in `{source_ids}`; if one add fails, diagnose it and continue only if the remaining sources satisfy the request.
3. After confirmation, wait for **every** captured source: `notebooklm source wait {source_id} -n {notebook_id} --timeout 600`. If polling `source list -n {notebook_id} --json` instead, JSON status values are lowercase; require `status == "ready"` and stop on `"error"`.
4. After confirmation, run `notebooklm generate audio "Focus on [specific angle]" -n {notebook_id} -s {source_id} --json` (repeat `-s` for each selected source) and capture `.task_id` as `{artifact_id}` — *if rate limited: wait 5 minutes, retry once*.
5. After confirmation, wait for that exact artifact: `notebooklm artifact wait {artifact_id} -n {notebook_id} --timeout 1200`.
6. After confirmation, download that exact artifact: `notebooklm download audio ./podcast.m4a -a {artifact_id} -n {notebook_id}`.

### Research to Podcast (Automated with Background Work)
**Time:** 5-10 minutes, but continues in background

When user wants full automation (generate and download when ready):

1. Perform interactive steps 1-3 above, retaining `{notebook_id}` and every `{source_id}`.
2. After confirmation, run `notebooklm generate audio "..." -n {notebook_id} -s {source_id} --json` (repeat `-s` as needed) and capture `.task_id` as `{artifact_id}`.
3. Keep `{artifact_id}` with `{notebook_id}`; do not resolve either from current context or the latest artifact.
4. If the host harness provides a supported background-agent or background-command facility, delegate these exact ID-pinned commands as one sequential job. Start the download only after `artifact wait` exits 0, and stop the job if the wait fails:
   ```bash
   notebooklm artifact wait {artifact_id} -n {notebook_id} --timeout 1200
   notebooklm download audio ./podcast.m4a -a {artifact_id} -n {notebook_id}
   ```
5. If background execution is unavailable, give the user the same commands with `{artifact_id}` and `{notebook_id}` replaced by the captured values, or run them in the foreground after confirmation. Never assume a particular background-agent API; use only facilities the host actually exposes.

**Error handling in background work:**
- If `artifact wait` returns exit code 2 (timeout): report the timeout and inspect `artifact list -n {notebook_id} --json` for that ID.
- If download fails: verify that the exact artifact's JSON status is `completed` first.

**Benefits:** Non-blocking, user can do other work, automatic download on completion

### Document Analysis
**Time:** 1-2 minutes

1. Run `notebooklm create "Analysis: [project]" --json` and capture `.notebook.id` as `{notebook_id}`.
2. Add each document or URL with `notebooklm source add <source> -n {notebook_id} --json`; capture every `.source.id`.
3. After confirmation, wait for **every** captured source with `notebooklm source wait {source_id} -n {notebook_id} --timeout 600`. Do not chat against a source while it is still indexing; stop and diagnose any source that reaches `error`.
4. `notebooklm ask "Summarize the key points" -n {notebook_id}`.
5. `notebooklm ask "What are the main arguments?" -n {notebook_id}`.
6. Continue chatting with `-n {notebook_id}` as needed.

### Bulk Import
**Time:** Varies by source count

1. Run `notebooklm create "Collection: [name]" --json` and capture `.notebook.id` as `{notebook_id}`.
2. Add multiple sources and capture each `.source.id`:
   ```bash
   notebooklm source add "https://url1.com" -n {notebook_id} --json
   notebooklm source add "https://url2.com" -n {notebook_id} --json
   notebooklm source add ./local-file.pdf -n {notebook_id} --json
   ```
3. Run `notebooklm source list -n {notebook_id} --json` to verify the captured IDs and statuses.

**Source limits:** Varies by plan—Standard: 50, Plus: 100, Pro: 300, Ultra: 600 sources per notebook. See [NotebookLM plans](https://support.google.com/notebooklm/answer/16213268) for details. The CLI does not enforce these limits; they are applied by your NotebookLM account.
**Supported types:** PDFs, YouTube URLs, web URLs, Google Docs, text files, Markdown, Word docs, EPUB, audio files, video files, images

### Bulk Import with Source Waiting (Background Pattern)
**Time:** Varies by source count

When adding multiple sources and needing to wait for processing before chat/generation:

1. Retain the explicit `{notebook_id}`, then add sources with `--json` to capture IDs (parse with `jq -r .source.id`):
   ```bash
   notebooklm source add "https://url1.com" -n {notebook_id} --json  # → {"source": {"id": "abc...", ...}}
   notebooklm source add "https://url2.com" -n {notebook_id} --json  # → {"source": {"id": "def...", ...}}
   ```
2. If the host harness supports background work, delegate one ID-pinned wait per captured source through that facility. For example:
   ```bash
   notebooklm source wait abc... -n {notebook_id} --timeout 600
   notebooklm source wait def... -n {notebook_id} --timeout 600
   ```
3. If background work is unavailable, give the user those exact commands with the captured notebook and source IDs filled in, or run them in the foreground after confirmation.
4. Proceed with chat or generation only after all waits succeed.

**Why wait for sources?** Sources must be indexed before chat or generation. Takes ~30 seconds to several minutes per source (see the processing-times table below).

### Deep Web Research (Background Pattern)
**Time:** 15-30+ minutes, runs in background

Deep research finds and analyzes web sources on a topic:

1. Create the notebook with `notebooklm create "Research: [topic]" --json`; capture `.notebook.id` as `{notebook_id}`.
2. Start deep research non-blocking, capture the JSON response, and retain `.poll_task_id // .task_id` as `{research_run_id}`:
   ```bash
   notebooklm source add-research "topic query" -n {notebook_id} --mode deep --no-wait --json
   ```
3. After confirmation to import the completed research sources, if the host harness supports background work, delegate this exact ID-pinned command through that facility:
   ```bash
   notebooklm research wait -n {notebook_id} --run-id {research_run_id} --import-all --timeout 1800 --json
   ```
4. If background work is unavailable, give the user that command with both captured IDs filled in, or run it in the foreground after confirmation. Do not omit `--run-id`: selecting the latest visible run is unsafe when research runs overlap.
5. When the wait succeeds, retain the returned imported source IDs for any later chat or generation workflow.

**Alternative (blocking):** After confirmation to import the research sources, omit `--no-wait` for simple cases:
```bash
notebooklm source add-research "topic" -n {notebook_id} --mode deep --import-all
# Blocks until research completes (deep mode: 15-30+ min)
```

**When to use each mode:**
- `--mode fast`: Specific topic, quick overview needed (5-10 sources, seconds)
- `--mode deep`: Broad topic, comprehensive analysis needed (20+ sources, 15-30+ min)

**Research sources:**
- `--from web`: Search the web (default)
- `--from drive`: Search Google Drive

## Output Style

**Progress updates:** Brief status for each step
- "Creating notebook 'Research: AI'..."
- "Adding source: https://example.com..."
- "Starting audio generation... (task ID: abc123)"

**Fire-and-forget for long operations:**
- Start generation, return artifact ID immediately
- Do NOT poll or wait in main conversation - generation takes 5-45 minutes (see timing table)
- Give the user an exact ID-pinned `artifact wait` command, or delegate it through a background facility the host actually supports

**JSON output:** Use `--json` flag for machine-readable output:
```bash
notebooklm list --json
notebooklm auth check --test --json   # use --test for network-validated auth (see § Agent Setup Verification)
notebooklm source list --json
notebooklm artifact list --json
```

**JSON schemas (key fields):**

`notebooklm list --json`:
```json
{"notebooks": [{"index": 1, "id": "...", "title": "...", "is_owner": true, "created_at": "..."}], "count": 1}
```

`notebooklm auth check --test --json` (use `--test` to drive the network token-fetch — bare `--json` would leave `"token_fetch": null`):
```json
{"status": "ok", "checks": {"storage_exists": true, "json_valid": true, "cookies_present": true, "sid_cookie": true, "token_fetch": true}, "details": {"storage_path": "...", "auth_source": "file", "cookies_found": ["SID", "HSID", "..."], "cookie_domains": [".google.com"]}}
```

`notebooklm source list --json`:
```json
{"notebook_id": "...", "notebook_title": "...", "sources": [{"index": 1, "id": "...", "title": "...", "type": "web_page", "url": "...", "status": "ready|processing|error", "status_id": 1, "created_at": "..."}], "count": 1}
```

`notebooklm artifact list --json`:
```json
{"notebook_id": "...", "notebook_title": "...", "artifacts": [{"index": 1, "id": "...", "title": "...", "type": "Audio", "type_id": 1, "status": "in_progress|pending|completed|unknown", "status_id": 1, "created_at": "..."}], "count": 1}
```

**Status values:**
- Sources: `processing` → `ready` (or `error`)
- Artifacts: `pending` or `in_progress` → `completed` (or `unknown`)

## Error Handling

**On failure, run safe read-only diagnosis first.** Run `notebooklm auth check --test --json` and require `checks.token_fetch == true`. Inspect the relevant explicit IDs with `notebooklm list --json`, `notebooklm source list -n {notebook_id} --json`, `notebooklm artifact list -n {notebook_id} --json`, or `notebooklm research status -n {notebook_id} --run-id {research_run_id} --json`; do not mutate state during diagnosis.

If read-only diagnosis does not identify a safe recovery, offer the user a choice:
1. Retry the operation
2. Skip and continue with something else
3. Escalate with the diagnostic findings

**Error decision tree:**

| Error | Cause | Action |
|-------|-------|--------|
| Auth/cookie error | Session expired | Run `notebooklm auth check --test --json`; if `checks.token_fetch` is not `true`, run `notebooklm login` |
| "No notebook context" | Context not set | Use `-n <id>` or `--notebook <id>` flag (parallel), or `notebooklm use <id>` (single-agent) |
| "No result found for RPC ID" | Rate limiting | Wait 5-10 min, retry |
| `GENERATION_FAILED` | Google rate limit | Wait and retry later |
| Download fails | Generation incomplete | Check `notebooklm artifact list -n {notebook_id} --json` for the exact artifact ID |
| Invalid notebook/source ID | Wrong ID | Run `notebooklm list --json` and `notebooklm source list -n {notebook_id} --json` to verify |
| RPC protocol error | Google changed APIs | May need CLI update |

## Exit Codes

All commands use consistent exit codes:

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Continue |
| 1 | Error (not found, processing failed) | Check stderr, see Error Handling |
| 2 | Timeout (wait commands only) | Extend timeout or check status manually |

**Examples:**
- `source wait` returns 1 if source not found or processing failed
- `artifact wait` returns 2 if timeout reached before completion
- `generate` returns 1 if rate limited (check stderr for details)

## Long Prompts

When a prompt or query exceeds shell command-line length limits, use `--prompt-file` to read it from a file:

```bash
notebooklm ask --prompt-file ./long_question.txt
notebooklm generate report --prompt-file ./custom_report_prompt.txt
notebooklm source add-research --prompt-file ./research_query.txt --mode deep
```

`--prompt-file` is mutually exclusive with the positional text argument. The file is read as UTF-8 with trailing whitespace stripped. Supported on: `ask`, all `generate` subcommands (except `mind-map`), and `source add-research`.

> **Note:** `--prompt-file` reads a *prompt/query text file*, not a source document. To upload a file as a notebook source, use `source add ./file.pdf`.

## Known Limitations

**Rate limiting:** Audio, video, quiz, flashcards, infographic, and slide deck generation may fail due to Google's rate limits. This is an API limitation, not a bug.

**Reliable operations:** These always work:
- Notebooks (list, create, delete, rename)
- Sources (add, list, delete)
- Chat/queries
- Mind-map, study-guide, report, data-table generation

**Unreliable operations:** These may fail with rate limiting:
- Audio (podcast) generation
- Video generation
- Quiz and flashcard generation
- Infographic and slide deck generation

**Workaround:** If generation fails:
1. Check status: `notebooklm artifact list`
2. Retry after 5-10 minutes
3. Use the NotebookLM web UI as fallback

**Processing times vary significantly.** Use supported background work for long operations when available:

| Operation | Typical time | Suggested timeout |
|-----------|--------------|-------------------|
| Source processing | 30s - 10 min | 600s |
| Research (fast) | 30s - 2 min | 180s |
| Research (deep) | 15 - 30+ min | 1800s |
| Notes | instant | n/a |
| Mind map (`--kind note-backed`) | instant; synchronous | n/a |
| Mind map (`--kind interactive`, default) | backend operation is asynchronous; the CLI polls it to completion | CLI-managed |
| Quiz, flashcards | 5 - 15 min | 900s |
| Report, data-table | 5 - 15 min | 900s |
| Audio generation | 10 - 20 min | 1200s |
| Video generation | 15 - 45 min | 2700s |

**Polling intervals:** When checking status manually, poll every 15-30 seconds to avoid excessive API calls.

## Language Configuration

Language setting controls the output language for generated artifacts (audio, video, etc.).

**Important:** Language is a **GLOBAL** setting that affects all notebooks in your account.

```bash
# List all 80+ supported languages with native names
notebooklm language list

# Show current language setting
notebooklm language get

# Set language for artifact generation
notebooklm language set zh_Hans  # Simplified Chinese
notebooklm language set ja       # Japanese
notebooklm language set en       # English (default)
```

**Common language codes:**
| Code | Language |
|------|----------|
| `en` | English |
| `zh_Hans` | 中文（简体） - Simplified Chinese |
| `zh_Hant` | 中文（繁體） - Traditional Chinese |
| `ja` | 日本語 - Japanese |
| `ko` | 한국어 - Korean |
| `es` | Español - Spanish |
| `fr` | Français - French |
| `de` | Deutsch - German |
| `pt_BR` | Português (Brasil) |

**Override per command:** Use `--language` flag on generate commands:
```bash
notebooklm generate audio --language ja   # Japanese podcast
notebooklm generate video --language zh_Hans  # Chinese video
```

**Offline mode:** Use `--local` flag to skip server sync:
```bash
notebooklm language set zh_Hans --local  # Save locally only
notebooklm language get --local  # Read local config only
```

## Troubleshooting

```bash
notebooklm --help              # Main commands
notebooklm auth check          # Diagnose auth issues
notebooklm auth check --test   # Full auth validation with network test
notebooklm source --help       # Source management
notebooklm research --help     # Research status/wait/cancel
notebooklm generate --help     # Content generation
notebooklm artifact --help     # Artifact management
notebooklm download --help     # Download content
notebooklm language --help     # Language settings
```

**Diagnose auth:** `notebooklm auth check` - shows cookie domains, storage path, validation status
**Re-authenticate:** `notebooklm login`
**Check version:** `notebooklm --version`
**Refresh a CLI-managed install:** `notebooklm skill install`
