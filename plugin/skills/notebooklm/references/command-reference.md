# Command Reference

Part of the `notebooklm` skill. Full command table, JSON output formats and
citation guidance, generation-type options, features beyond the web UI, long
prompts, and language configuration. See [SKILL.md](../SKILL.md) for the
condensed quick reference and the CLI-not-on-PATH substitution rule (`sh
<skill-dir>/scripts/nlm ...`) that applies to every command below.

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
| Wait for research | `notebooklm research wait --import-all` |
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
| Download audio | `notebooklm download audio ./output.mp3` |
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

**JSON output:** Use `--json` flag for machine-readable output:
```bash
notebooklm list --json
notebooklm auth check --test --json   # use --test for network-validated auth (see setup.md § Agent Setup Verification)
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
| Podcast | `generate audio` | `--format [deep-dive\|brief\|critique\|debate]`, `--length [short\|default\|long]` | .mp3 |
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

## Long Prompts

When a prompt or query exceeds shell command-line length limits, use `--prompt-file` to read it from a file:

```bash
notebooklm ask --prompt-file ./long_question.txt
notebooklm generate report --prompt-file ./custom_report_prompt.txt
notebooklm source add-research --prompt-file ./research_query.txt --mode deep
```

`--prompt-file` is mutually exclusive with the positional text argument. The file is read as UTF-8 with trailing whitespace stripped. Supported on: `ask`, all `generate` subcommands (except `mind-map`), and `source add-research`.

> **Note:** `--prompt-file` reads a *prompt/query text file*, not a source document. To upload a file as a notebook source, use `source add ./file.pdf`.

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
