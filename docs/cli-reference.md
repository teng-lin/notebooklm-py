# CLI Reference

**Status:** Active
**Last Updated:** 2026-06-11

Complete command reference for the `notebooklm` CLI—providing full programmatic access to all NotebookLM features, including capabilities not exposed in the web UI.

> **Exit codes:** every command follows the convention documented in [CLI Exit-Code Convention](cli-exit-codes.md) (`0` success, `1` user/app error, `2` system/unexpected, `130` SIGINT). Two commands intentionally deviate for shell control-flow use (`source stale --exit-on-stale` opts into an inverted predicate; `source wait` is three-way); see the doc for details.

## Command Structure

```bash
notebooklm [-p PROFILE] [--storage PATH] [--version] [-v|--quiet] <command> [OPTIONS] [ARGS]
```

### Global Options

- `-p, --profile NAME` - Use a named profile (overrides `NOTEBOOKLM_PROFILE` env var)
- `--storage PATH` - Override the default storage location
- `-v, --verbose` - Increase verbosity (`-v` for INFO, `-vv` for DEBUG)
- `--quiet` - Suppress status output and INFO/WARN log records (only errors survive). Structured `--json` payloads are still emitted. Mutually exclusive with `-v`/`-vv`; combining the two raises `UsageError` (exit `2`).
- `--version` - Show version and exit
- `--help` - Show help message

### Environment Variables

- `NOTEBOOKLM_HOME` - Base directory for all config files (default: `~/.notebooklm`)
- `NOTEBOOKLM_PROFILE` - Active profile name (default: `default`)
- `NOTEBOOKLM_NOTEBOOK` - Default notebook ID for any command exposing `-n/--notebook`. Resolution order: `-n/--notebook` flag > `NOTEBOOKLM_NOTEBOOK` env > active context (`notebooklm use`) > error. Empty/whitespace values are treated as unset.
- `NOTEBOOKLM_AUTH_JSON` - Inline authentication JSON (for CI/CD, no file writes needed)
- `NOTEBOOKLM_HL` - Default output language for artifact generation (overridden by `--language`)
- `NOTEBOOKLM_LOG_LEVEL` - Override the package logger level (e.g. `DEBUG`, `INFO`, `WARNING`, `ERROR`)
- `NOTEBOOKLM_DEBUG_RPC` - Enable RPC debug logging (`1` to enable)
- `NOTEBOOKLM_QUIET_DEPRECATIONS` - Suppress deprecation notices (`1` to enable)

See [Configuration](configuration.md) for full env-var precedence and CI/CD setup.

### Command Organization

- **Session commands** - Authentication and context management
- **Notebook commands** - CRUD operations on notebooks
- **Chat commands** - Querying and conversation management
- **Grouped commands** - `source`, `label`, `artifact`, `agent`, `generate`, `download`, `note`, `share`, `research`, `language`, `skill`, `auth`, `profile`, `mcp`
- **Utility commands** - `metadata`, `doctor`

---

## Quick Reference

### Session Commands

| Command | Description | Example |
|---------|-------------|---------|
| `login` | Authenticate via browser | `notebooklm login` / `notebooklm login --browser msedge` |
| `login --master-token` | Headless auth: mint cookies from a durable master token (`[headless]`) | `notebooklm login --master-token --account you@gmail.com` |
| `use <id>` | Set active notebook | `notebooklm use abc123` |
| `status` | Show current context | `notebooklm status` |
| `status --paths` | Show configuration paths | `notebooklm status --paths` |
| `status --json` | Output status as JSON | `notebooklm status --json` |
| `clear` | Clear current context | `notebooklm clear` |
| `auth check` | Diagnose authentication issues | `notebooklm auth check` |
| `auth check --test` | Validate with network test | `notebooklm auth check --test` |
| `auth check --json` | Output as JSON | `notebooklm auth check --json` |
| `auth inspect` | List Google accounts visible to a browser cookie store (read-only) | `notebooklm auth inspect --browser chrome` |
| `auth import-cookies` | Import auth cookies from a JSON file (or stdin) into the active profile | `notebooklm auth import-cookies cookies.json` |
| `auth logout` | Clear saved cookies and cached browser profile | `notebooklm auth logout` |
| `auth refresh` | One-shot SIDTS rotation poke (for OS schedulers) | `notebooklm auth refresh` |
| `auth refresh --quiet` | Refresh; suppress success output | `notebooklm auth refresh --quiet` |
| `doctor` | Check environment health | `notebooklm doctor` |
| `doctor --fix` | Auto-fix detected issues | `notebooklm doctor --fix` |
| `doctor --json` | Output diagnostics as JSON | `notebooklm doctor --json` |
| `completion <shell>` | Print shell completion script (`bash`/`zsh`/`fish`) | `notebooklm completion zsh > ~/.zfunc/_notebooklm` |

### Profile Commands (`notebooklm profile <cmd>`)

| Command | Description | Example |
|---------|-------------|---------|
| `list` | List all profiles | `notebooklm profile list` |
| `create <name>` | Create a new profile | `notebooklm profile create work` |
| `switch <name>` | Set the active profile | `notebooklm profile switch work` |
| `delete <name>` | Delete a profile | `notebooklm profile delete old` |
| `rename <old> <new>` | Rename a profile | `notebooklm profile rename old new` |

### Language Commands (`notebooklm language <cmd>`)

| Command | Description | Example |
|---------|-------------|---------|
| `list` | List all supported languages | `notebooklm language list` |
| `get` | Show current language setting | `notebooklm language get` |
| `get --local` | Show local config only (skip server sync) | `notebooklm language get --local` |
| `set <code>` | Set language for artifact generation | `notebooklm language set zh_Hans` |
| `set <code> --local` | Set local config only (skip server sync) | `notebooklm language set ja --local` |

**Note:** Language is a **GLOBAL** setting that affects all notebooks in your account.

### Notebook Commands

| Command | Description | Example |
|---------|-------------|---------|
| `list` | List all notebooks | `notebooklm list` |
| `list --json` | Output as JSON | `notebooklm list --json` |
| `list --limit N` | Show at most N notebooks (default: unlimited) | `notebooklm list --limit 10` |
| `list --no-truncate` | Do not truncate the Title column | `notebooklm list --no-truncate` |
| `use <id>` | Set active notebook (verifies existence by default) | `notebooklm use abc123` |
| `use <id> --force` | Skip the existence check (offline / debugging) | `notebooklm use abc123 --force` |
| `use <id> --json` | Emit `{active_notebook_id, success, verified, notebook}` envelope | `notebooklm use abc --json` |
| `create <title>` | Create notebook (does not change active context) | `notebooklm create "Research"` |
| `create <title> --use` | Create notebook and make it the active context | `notebooklm create "Research" --use` |
| `create <title> --json` | JSON envelope; with `--use` includes `active_notebook_id` | `notebooklm create "X" --use --json` |
| `delete -n <id>` | Delete notebook (uses current notebook if `-n` omitted) | `notebooklm delete -n abc123` |
| `delete -n <id> -y` | Skip confirmation | `notebooklm delete -n abc123 -y` |
| `delete -n <id> --json` | Emit `{notebook_id, success}` envelope (plus `context_cleared: true` when deleting the active notebook); requires `-y` (refuses to prompt in JSON mode) | `notebooklm delete -n abc123 -y --json` |
| `rename <title>` | Rename current notebook | `notebooklm rename "New Title"` |
| `summary` | Get AI summary | `notebooklm summary` |
| `summary --topics` | Include suggested topics | `notebooklm summary --topics` |

### Chat Commands

| Command | Description | Example |
|---------|-------------|---------|
| `ask <question>` | Ask a question (auto-resumes the last conversation) | `notebooklm ask "What is this about?"` |
| `ask -` | Read question from stdin (Unix `-` convention) | `echo "what is X?" \| notebooklm ask -` |
| `ask --prompt-file PATH` | Read question from file (use `-` for stdin) | `notebooklm ask --prompt-file q.txt` |
| `ask -c <id>` | Continue a specific conversation | `notebooklm ask -c conv_abc "..."` |
| `ask --new` | **DESTRUCTIVE.** Permanently delete the notebook's current server-side conversation (turns are not recoverable) and start fresh on the next `ask`. Prompts for confirmation with the conversation's short id; pass `--yes`/`-y` to skip the prompt. `--json` implies `--yes` so scripted callers don't hang on stdin. Closes the workaround documented in [#659](https://github.com/teng-lin/notebooklm-py/issues/659). | `notebooklm ask --new -y "start fresh"` |
| `ask --new --yes` (alias `-y`) | Skip the `--new` destructive-delete confirmation prompt. | `notebooklm ask --new --yes "..."` |
| `ask -s <id>` | Limit to specific source IDs (repeatable) | `notebooklm ask "Summarize" -s src1 -s src2` |
| `ask --json` | Get answer with source references | `notebooklm ask "Explain X" --json` |
| `ask --request-timeout N` | Per-invocation HTTP request/read timeout in seconds for chat. Defaults to the library chat timeout. `--timeout` is a back-compat alias for the same flag. | `notebooklm ask "long prompt" --request-timeout 120` |
| `ask --save-as-note` | Save response as a note. When the answer contains `[N]` citations, the saved note preserves interactive hover-anchored citation links matching the NotebookLM web UI's "Save to note" behavior ([issue #660](https://github.com/teng-lin/notebooklm-py/issues/660)). Answers without citations fall back to a plain-text note. | `notebooklm ask "Explain X" --save-as-note` |
| `ask --save-as-note --note-title` | Save response with custom note title. The NotebookLM server may apply smart-title generation for citation-rich saves and override the requested title; the success message reflects what the server actually stored. | `notebooklm ask "Explain X" --save-as-note --note-title "Title"` |
| `suggest-prompts` | Get AI-suggested prompts for the notebook (each a title plus a ready-to-send instruction for `ask`) | `notebooklm suggest-prompts` |
| `suggest-prompts --mode N` | Select the suggestion surface (1-10, default 4): 1=audio deep-dive, 2=audio brief, 3=video explainer, 4=chat questions, 5=audio critique, 6=audio debate, 8=quiz, 9=flashcards, 10=video short. Out-of-range exits 1. | `notebooklm suggest-prompts --mode 8` |
| `suggest-prompts --query TEXT` | Free-text steer for the kind of prompts to suggest | `notebooklm suggest-prompts --query "key risks"` |
| `suggest-prompts -s <id>` | Limit to specific source IDs (repeatable; defaults to all sources) | `notebooklm suggest-prompts -s src1 -s src2` |
| `suggest-prompts --json` | Machine-readable output (`{notebook_id, suggestions, count}`) | `notebooklm suggest-prompts --json` |
| `configure --mode` | Set predefined chat mode (`default`, `learning-guide`, `concise`, `detailed`) | `notebooklm configure --mode learning-guide` |
| `configure --persona` | Set custom persona prompt (up to 10,000 chars) | `notebooklm configure --persona "Act as a tutor"` |
| `configure --response-length` | Response verbosity (`default`, `longer`, `shorter`) | `notebooklm configure --response-length longer` |
| `configure --json` | Machine-readable output | `notebooklm configure --mode concise --json` |
| `history` | View conversation history | `notebooklm history` |
| `history -l N` | Maximum number of Q&A turns to show | `notebooklm history -l 5` |
| `history --json` | Machine-readable output | `notebooklm history --json` |
| `history --clear` | Clear local conversation cache | `notebooklm history --clear` |
| `history --save` | Save history as a note | `notebooklm history --save` |
| `history --save -t "Title"` | Save history with custom title | `notebooklm history --save -t "Summary"` |
| `history --show-all` | Show full Q&A content (not preview) | `notebooklm history --show-all` |
| `history --no-truncate` | Disable the 50-char preview cap on the Question/Answer columns in the table view (the existing `-l/--limit` flag is unchanged: it caps the number of Q&A turns fetched server-side) | `notebooklm history --no-truncate` |

Setting just one of `configure --persona` / `--response-length` **merges** with the current settings — the field you omit is preserved, not reset. A bare `configure` with no flags resets all custom chat settings to their defaults. (A `--mode` preset cannot be combined with `--persona`/`--response-length`.)

### Source Commands (`notebooklm source <cmd>`)

Supported source types: URLs, YouTube videos, files (PDF, text, Markdown, Word, audio, video, images), Google Drive documents, and pasted text.

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | `--json`, `--limit N`, `--no-truncate` | `source list --limit 20 --no-truncate` |
| `add <content>` | URL/file/text (use `-` for stdin) | `--title`, `--type`, `--timeout`, `--follow-symlinks`, `--allow-internal` (URL sources only), `--json` (file-source `--mime-type` overrides extension inference — see [detailed section](#source-add-mime-type-file-sources)) | `source add "https://..." --timeout 90` |
| `add-drive <id> <title>` | Drive file ID, title | `--mime-type [google-doc\|google-slides\|google-sheets\|pdf]`, `--json` | `source add-drive abc123 "Doc" --mime-type google-slides` |
| `add-research [query]` | Search query (or `--prompt-file -` for stdin) | `--mode [fast\|deep]`, `--from [web\|drive]`, `--import-all`, `--cited-only`, `--no-wait`, `--timeout`, `--prompt-file PATH` | `source add-research "AI" --mode deep --no-wait` |
| `get <id>` | Source ID | `--json` | `source get src123` |
| `fulltext <id>` | Source ID | `--json`, `-o FILE`, `--force`, `--no-clobber`, `-f [text\|markdown]` | `source fulltext src123 -f markdown -o out.md` (`-f markdown` requires the `markdown` extra: `pip install "notebooklm-py[markdown]"` — full extras matrix: [docs/installation.md#optional-extras-matrix](installation.md#optional-extras-matrix)) |
| `guide <id>` | Source ID | `--json` | `source guide src123` |
| `stale <id>` | Source ID | `--exit-on-stale`, `--json` | `source stale src123` (exit 0 on success; pass `--exit-on-stale` for the back-compat inverted predicate — see [exit codes](cli-exit-codes.md)) |
| `wait <id>` | Source ID | `--timeout`, `--interval`, `--json` | `source wait src123 --timeout 300 --interval 5` |
| `clean` | - | `--dry-run`, `-y/--yes`, `--json` | `source clean --dry-run` |
| `rename <id> <title>` | Source ID, new title | `--json` | `source rename src123 "New Name"` |
| `refresh <id>` | Source ID | `--json` | `source refresh src123` |
| `delete <id>` | Source ID | `-y/--yes`, `--json` | `source delete src123 -y` |
| `delete-by-title <title>` | Exact source title | `-y/--yes`, `--json` | `source delete-by-title "My Source"` |

All `source` subcommands also accept `-n/--notebook ID` (resolves via flag > `NOTEBOOKLM_NOTEBOOK` env > active context).

`source delete <id>` accepts only full source IDs or unique partial-ID prefixes. To delete by exact source title, use `source delete-by-title "<title>"`.

`source clean` automatically removes duplicate, error, and access-blocked sources; combine with `--dry-run` to preview the candidate set first.

`source fulltext -o FILE` auto-renames an existing output path by default (`FILE` -> `FILE (2)`, etc.) instead of overwriting it. Pass `--force` to overwrite intentionally, or `--no-clobber` to fail if the path exists.

`source stale` reports whether a URL/Drive source needs a refresh. By default it follows the standard CLI exit convention (`0` on success, `1` on error); branch on the JSON `stale`/`fresh` fields (or stdout text) for the freshness verdict. Pass `--exit-on-stale` to opt into the back-compat inverted predicate (`0` = stale, `1` = fresh) for shell idioms like `if notebooklm source stale --exit-on-stale ID; then refresh; fi`.

`source list` also accepts `--label <id|name>` to list only the sources in a given label (a saved selection). The selector resolves a label id (or partial prefix) **or** an exact label name; see [Label Commands](#label-commands-notebooklm-label-cmd).

### Label Commands (`notebooklm label <cmd>`)

Source labels group a notebook's sources into topic buckets. A `<id|name>` argument accepts a label id (or partial prefix) **or** an exact label name; an ambiguous name lists the matching ids so you can disambiguate.

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | `--json` | `label list -n nb123` |
| `sources <id\|name>` | Label id or name | `--json` | `label sources Papers` |
| `generate` | - | `--scope [all\|unlabeled]`, `-y/--yes`, `--json` | `label generate --scope all -y` |
| `create <name>` | Label name | `--emoji 📄`, `--json` | `label create "Papers" --emoji 📄` |
| `rename <id\|name> <new_name>` | Label ref, new name | `--json` | `label rename Papers "Research Papers"` |
| `emoji <id\|name> <emoji>` | Label ref, emoji | `--json` | `label emoji Papers 📚` |
| `add <id\|name> <source_id>...` | Label ref, one+ source ids | `--json` | `label add Papers src123 src456` |
| `remove <id\|name> <source_id>...` | Label ref, one+ source ids | `--json` | `label remove Papers src123` |
| `delete <id\|name>...` | One+ label refs | `-y/--yes`, `--json` | `label delete Papers -y` |

All `label` subcommands accept `-n/--notebook ID` (resolves via flag > `NOTEBOOKLM_NOTEBOOK` env > active context).

`label generate --scope all` (wipes and regenerates every label with new ids) and `label delete` are destructive and require `-y/--yes` to confirm (or an interactive prompt). `label generate --scope unlabeled` (the default) only labels currently-unlabeled sources and needs no confirmation. `label add` appends sources (existing members survive; labels may overlap) and `label remove` un-assigns sources from the label only — the sources stay in the notebook (and in any other label). `label delete` removes the label only — its sources become unlabeled, not deleted. `source_id` arguments to `label add`/`label remove` accept partial-prefix matching like every other source-id command.

### Research Commands (`notebooklm research <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `status` | - | `-n/--notebook`, `--json` | `research status` |
| `wait` | - | `-n/--notebook`, `--timeout`, `--interval`, `--import-all`, `--cited-only`, `--json` | `research wait --import-all --cited-only` |
| `cancel` | `RUN_ID` | `-n/--notebook`, `--json` | `research cancel <run_id>` |

### Generate Commands (`notebooklm generate <type>`)

All generate commands (except the synchronous `mind-map`) accept the same uniform polling-flag surface:
- `-n, --notebook ID` to target a specific notebook (also resolved from `NOTEBOOKLM_NOTEBOOK` / active context)
- `-s, --source ID` to select specific sources (repeatable)
- `--json` for machine-readable output (returns `task_id` and `status`)
- `--wait / --no-wait` to block until completion (default: `--no-wait` — returns immediately with a `task_id`)
- `--timeout SECONDS` to cap the `--wait` budget (defaults: 1200s for audio, 1800s for video, 3600s for cinematic-video, and 300s for quiz/flashcards/slide-deck/infographic/data-table/report/revise-slide). No-op without `--wait`.
- `--interval SECONDS` to tune polling cadence (default: 2). No-op without `--wait`.
- `--retry N` to automatically retry on rate limits with exponential backoff
- `--prompt-file PATH` to read the description/query from a file (or `-` for stdin) instead of the command line. Mutually exclusive with the positional argument; useful for long prompts that exceed shell length limits.

Language-aware generate commands (`audio`, `video`, `cinematic-video`, `report`, `infographic`, `slide-deck`, `data-table`, `mind-map`) also support:
- `--language LANG` to override output language (precedence: `--language` > `NOTEBOOKLM_HL` env > config > `'en'`)

`quiz`, `flashcards`, and `revise-slide` do not accept `--language`.

| Command | Arguments | Type-specific options | Example |
|---------|-----------|-----------------------|---------|
| `audio [description]` | Instructions | `--format [deep-dive\|brief\|critique\|debate]`, `--length [short\|default\|long]` | `generate audio "Focus on history"` |
| `video [description]` | Instructions | `--format [explainer\|brief\|cinematic]`, `--style [auto\|custom\|classic\|whiteboard\|kawaii\|anime\|watercolor\|retro-print\|heritage\|paper-craft]`, `--style-prompt TEXT` (required with `--style custom`; rejected with `--format cinematic`) | `generate video "Explainer for kids"` |
| `cinematic-video [description]` | Instructions | Alias for `video --format cinematic` | `generate cinematic-video "Documentary about quantum physics"` |
| `slide-deck [description]` | Instructions | `--format [detailed\|presenter]`, `--length [default\|short]` | `generate slide-deck` |
| `revise-slide <description>` | Revision instructions | `-a/--artifact <id>` (required), `--slide N` (required) | `generate revise-slide "Move title up" --artifact <id> --slide 0` |
| `quiz [description]` | Instructions | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | `generate quiz --difficulty hard` |
| `flashcards [description]` | Instructions | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | `generate flashcards` |
| `infographic [description]` | Instructions | `--orientation [landscape\|portrait\|square]`, `--detail [concise\|standard\|detailed]`, `--style [auto\|sketch-note\|professional\|bento-grid\|editorial\|instructional\|bricks\|clay\|anime\|kawaii\|scientific]` | `generate infographic` |
| `data-table <description>` | Instructions | (uniform options only) | `generate data-table "compare concepts"` |
| `mind-map` | - | `--kind [interactive\|note-backed]`, `--instructions TEXT` *(no `--wait` / `--timeout` / `--interval` / `--retry` / `--prompt-file`)* | `generate mind-map --kind interactive` |
| `report [description]` | Instructions | `--format [briefing-doc\|study-guide\|blog-post\|custom]`, `--append TEXT` (no effect with `--format custom`) | `generate report --format study-guide` |

> **Two kinds of mind map (issue #1256).** NotebookLM has two distinct mind-map objects. `generate mind-map --kind interactive` (the default) builds the **interactive** kind — a studio artifact (the one the web app now creates) that is polled to completion. `generate mind-map --kind note-backed` builds the **note-backed** kind — a JSON node tree stored as a note, generated synchronously. Both produce the same `{mind_map, note_id, kind}` output, appear in `artifact list --type mind-map`, and download via `download mind-map`. `--instructions` is a free-text prompt that steers generation: the interactive kind applies it reliably (it travels in the `CREATE_ARTIFACT` prompt slot the server honors and that `artifact get-prompt` reads back); the note-backed kind passes it through to `GENERATE_MIND_MAP`, but the server may not always act on it.

### Artifact Commands (`notebooklm artifact <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | `--type [all\|audio\|video\|slide-deck\|quiz\|flashcard\|infographic\|data-table\|mind-map\|report]`, `--limit N`, `--no-truncate`, `--json` | `artifact list --type audio --limit 5` |
| `get <id>` | Artifact ID | `--json` | `artifact get art123` |
| `get-prompt <id>` | Artifact ID | `--json` | `artifact get-prompt art123` |
| `rename <id> <title>` | Artifact ID, title | `--json` | `artifact rename art123 "Title"` |
| `delete <id>` | Artifact ID | `-y/--yes`, `--json` | `artifact delete art123 -y` |
| `export <id>` | Artifact ID | `--title TEXT` (required), `--type [docs\|sheets]`, `--json` | `artifact export art123 --title "My Doc" --type sheets` |
| `poll <task_id>` | Task ID (from `generate <type>`) | `--json` | `artifact poll task123` |
| `wait <id>` | Artifact ID (from `artifact list`) | `--timeout` (default: 300), `--interval` (default: 2), `--json` | `artifact wait art123 --timeout 600` |
| `retry <id>` | Artifact ID (from `artifact list`) | `--wait`, `--timeout` (default: 300), `--interval` (default: 2), `--json` | `artifact retry art123 --wait` |
| `suggestions` | - | `--json` | `artifact suggestions` |

All `artifact` subcommands also accept `-n/--notebook ID`.

> **Note:** `artifact delete` on a Mind Map clears its content rather than removing the artifact entry — Mind Maps are stored alongside notes and Google may garbage-collect cleared entries later. The CLI prints a `Cleared mind map:` message instead of `Deleted artifact:` when this happens.

> **Common confusion: `poll` vs `wait` ID kind.** Both commands accept the *same identifier* — the API returns one ID that serves as both the generation `task_id` (during creation) and the `artifact_id` (once the row appears in `artifact list`). The split is operational, not semantic:
>
> - **`artifact poll <task_id>`** — single non-blocking status check. Pass the raw `task_id` returned by `notebooklm generate <type>` straight through. `poll` does **not** prefix-match against `artifact list`, so it works *immediately* after a `generate` call returns, before the artifact has appeared in any list response.
> - **`artifact wait <artifact_id>`** — blocks (with exponential backoff) until the artifact is `completed`, `failed`, or `--timeout` elapses. Accepts a full UUID **or a unique prefix** that resolves against `artifact list` via the standard partial-ID resolver.
>
> Rule of thumb: **just generated something? use `poll`.** **Found it in `artifact list`? use `wait`.** You can pass the same string to both — the kind label is about lifecycle stage, not about a different identifier format.

> **`artifact retry <artifact_id>`** re-runs generation for a *failed* artifact in place (the web UI "Retry" button) — the artifact is not deleted first and the same ID is preserved. Accepts a full UUID or a unique prefix. By default it kicks off the retry and returns immediately (`Retry started: <id>`); pass `--wait` to block until the retried generation reaches a terminal state. A synchronous refusal (rate limit / quota / not-retryable) exits non-zero with a typed error rather than reporting a started task. A retry may itself fail again provider-side, in which case you can run `artifact retry` again against the same ID.

### Download Commands (`notebooklm download <type>`)

Every `download` subcommand accepts the same selection / safety / output flag set: `-n/--notebook ID`, `-a/--artifact ID`, `--all`, `--latest` (default), `--earliest`, `--name TEXT` (fuzzy title match), `--dry-run`, `--force`, `--no-clobber` (don't overwrite existing files: a single download fails, `--all` skips them; default is auto-rename), and `--json`.

| Command | Arguments | Type-specific options | Example |
|---------|-----------|-----------------------|---------|
| `audio [path]` | Output path | (none) | `download audio --all` |
| `video [path]` | Output path | (none) | `download video --latest` |
| `cinematic-video [path]` | Output path | Alias for `download video`; cinematic and standard videos share the same artifact type | `download cinematic-video ./documentary.mp4` |
| `slide-deck [path]` | Output path | `--format [pdf\|pptx]` (default: pdf) | `download slide-deck ./slides.pdf` |
| `infographic [path]` | Output path | (none) | `download infographic ./info.png` |
| `report [path]` | Output path | (none) | `download report ./report.md` |
| `mind-map [path]` | Output path | (none) | `download mind-map ./map.json` |
| `data-table [path]` | Output path | (none) | `download data-table ./data.csv` |
| `quiz [path]` | Output path | `--format [json\|markdown\|html]` (default: json) | `download quiz --format markdown quiz.md` |
| `flashcards [path]` | Output path | `--format [json\|markdown\|html]` (default: json) | `download flashcards cards.json` |

<!-- `download mind-map` handles both note-backed and interactive kinds; both export their JSON node tree. -->

### Note Commands (`notebooklm note <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | `--json` | `note list --json` |
| `create [content]` | Note content (or `-` for stdin) | `--content TEXT` (or `-` for stdin; mutex with positional), `-t/--title TEXT`, `--json` | `cat notes.md \| note create -` |
| `get <id>` | Note ID | `--json` | `note get note123` |
| `save <id>` | Note ID | `--title`, `--content`, `--json` | `note save note123 --title "Updated title"` |
| `rename <id> <title>` | Note ID, title | `--json` | `note rename note123 "Title"` |
| `delete <id>` | Note ID | `-y/--yes`, `--json` | `note delete note123 -y` |

All `note` subcommands also accept `-n/--notebook ID`.

> **`source get` / `artifact get` / `note get` exit `1` on not-found (BREAKING).** All three `get` commands now exit `1` when the requested ID does not resolve to an existing item, matching the rest of the CLI's user-error convention. Under `--json` the failure body is the standard typed error envelope (`{"error": true, "code": "NOT_FOUND", "message": "...", "id": "...", "notebook_id": "..."}`); without `--json` the message is written to stderr. The previous behavior was exit `0` with a "not found" line on stdout. The pre-existing "no partial-ID match" branch (raised by `_resolve_partial_id` as a `ClickException`) was already exit `1` and is unchanged. See [CLI Exit-Code Convention](cli-exit-codes.md#get-on-not-found-exits-1-was-0-landed) for migration guidance.

### Metadata Command

Export notebook metadata and a simplified source list.

```bash
notebooklm metadata [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Specify notebook (uses current if not set)
- `--json` - Output as JSON for scripts

**Examples:**
```bash
notebooklm metadata
notebooklm metadata -n abc123 --json
```

### Skill Commands (`notebooklm skill <cmd>`)

Manage NotebookLM agent skill integration.

| Command | Description | Example |
|---------|-------------|---------|
| `install` | Install/update the skill for `claude`, `.agents`, or both | `skill install --target all` |
| `status` | Check installed targets and version info | `skill status --scope project` |
| `uninstall` | Remove one or more installed targets | `skill uninstall --target agents` |
| `show` | Display the packaged skill or an installed target | `skill show --target source` |

Defaults:

- `skill install` uses `--scope user --target all`
- `claude` maps to `.claude/skills/notebooklm/SKILL.md`
- `agents` maps to `.agents/skills/notebooklm/SKILL.md`
- `show --target source` prints the canonical packaged skill file
- Project-scope installs support `--dry-run`, `--no-clobber`, and `--force`; these flags are rejected for user-scope installs.

The packaged wheel includes the repo-root `SKILL.md`, so the same skill content powers `notebooklm skill install`, GitHub discovery, and `npx skills add teng-lin/notebooklm-py`.

Codex does not use the `skill` subcommand. In this repository it reads the root [`AGENTS.md`](../AGENTS.md) file and invokes the `notebooklm` CLI or Python API directly.

### MCP Commands (`notebooklm mcp <cmd>`)

Install the NotebookLM MCP server block into supported MCP client configs.

| Command | Description | Example |
|---------|-------------|---------|
| `install <client>` | Configure `claude-desktop`, `claude-code`, `cursor`, or `windsurf` | `mcp install claude-desktop` |
| `install <client> --config-path PATH` | Write a non-standard config path | `mcp install cursor --config-path ./mcp.json` |

The installer writes a `notebooklm` server entry that launches `notebooklm-mcp`
through `uvx`. Re-running is idempotent and preserves unrelated servers in the
same config file. See [MCP server guide](mcp-guide.md).

### Agent Commands (`notebooklm agent <cmd>`)

Show bundled instructions for supported agent environments.

| Command | Description | Example |
|---------|-------------|---------|
| `show codex` | Print the Codex repository guidance | `agent show codex` |
| `show claude` | Print the bundled Claude Code skill template | `agent show claude` |

`agent show codex` prefers the root [`AGENTS.md`](../AGENTS.md) file when running from a source checkout, so the CLI mirrors the same instructions Codex sees in the repository.

### Features Beyond the Web UI

These CLI capabilities are not available in NotebookLM's web interface:

| Feature | Command | Description |
|---------|---------|-------------|
| **Batch downloads** | `download <type> --all` | Download all artifacts of a type at once |
| **Quiz/Flashcard export** | `download quiz --format json` | Export as JSON, Markdown, or HTML |
| **Mind map extraction** | `download mind-map` | Export hierarchical JSON for visualization tools |
| **Data table export** | `download data-table` | Download structured tables as CSV |
| **Slide deck as PPTX** | `download slide-deck --format pptx` | Download as editable .pptx (web UI only offers PDF) |
| **Slide revision** | `generate revise-slide "prompt" --artifact <id> --slide N` | Modify individual slides with a natural-language prompt |
| **Report template append** | `generate report --format study-guide --append "..."` | Append instructions to built-in templates |
| **Source fulltext** | `source fulltext <id>` | Retrieve the indexed text content of any source |
| **Save chat to note** | `ask "..." --save-as-note` / `history --save` | Save Q&A answers or full conversation as notebook notes |
| **Programmatic sharing** | `share` commands | Manage permissions without the UI |

---

## Detailed Command Reference

### Authentication: `login`

Authenticate with Google NotebookLM via browser.

> **Python equivalent:** load saved credentials with [`AuthTokens.from_storage()` / `NotebookLMClient.from_storage(...)`](python-api.md#authentication). The CLI's interactive browser-login flow has no Python counterpart — run `notebooklm login` once to seed `storage_state.json`, then drive the API from Python.

```bash
notebooklm login [OPTIONS]
```

By default, opens a Chromium browser with a persistent profile. Complete the Google login in the browser window — the CLI detects the redirect back to `notebooklm.google.com` and saves the session automatically (no terminal keystroke required). The wait window is 5 minutes; if login is not detected before then, the command exits with a retry hint. Use `--browser msedge` for Microsoft Edge, or `--browser-cookies <browser>` to import cookies from an already-logged-in browser without launching Playwright.

**Options:**
- `--storage PATH` - Where to save storage_state.json (default: `$NOTEBOOKLM_HOME/profiles/<profile>/storage_state.json`)
- `--browser [chromium|msedge|chrome]` - Browser to use for login (default: `chromium`). Use `chrome` for system Google Chrome (workaround when bundled Chromium crashes, e.g. macOS 15+); use `msedge` for Microsoft Edge. **Note:** only `chromium` is auto-installed by the CLI on first login (~170 MB Chromium download); `--browser msedge` and `--browser chrome` require the corresponding browser to be already installed on your system.
- `--browser-cookies <auto|chrome|edge|firefox|safari|brave|arc|...>` - Read cookies from an installed browser instead of launching Playwright. Pass an explicit browser name, or `auto` to let rookiepy auto-detect. For Chromium-family user profiles, use `chrome::<profile-name-or-directory>` (for example `chrome::Profile 1` or `brave::Work`) to extract from one profile explicitly. For Firefox Multi-Account Containers, use `firefox::<container-name>` to extract from a single container, or `firefox::none` for the no-container default — unscoped `firefox` merges every container's cookies (and emits a warning when that's happening). Requires `pip install "notebooklm-py[cookies]"` (full extras matrix: [docs/installation.md#optional-extras-matrix](installation.md#optional-extras-matrix)).
- `--account EMAIL` - Pick a signed-in Google account by email when several are present in the browser. Saves to the active profile by default; use `--profile-name` for a separate named profile or `--storage` for an exact path. Only valid with `--browser-cookies`.
- `--all-accounts` - Extract every Google account signed in to the browser into separate profiles named from each account email. Only valid with `--browser-cookies`.
- `--profile-name NAME` - Write a targeted `--account` import to this named profile instead of the active profile. Only valid with `--browser-cookies`.
- `--fresh` - Start with a clean browser session (deletes the cached browser profile). Use to switch Google accounts. Has no effect with `--browser-cookies`.
- `--include-domains LABEL[,LABEL...]` - Opt in to extracting sibling-product cookies (default: required Google auth/Drive cookies only). Supported labels: `youtube`, `docs`, `myaccount`, `mail`, `all`. Pass labels comma-separated or repeat the flag.

**Master-token (headless) options** — mint/refresh web cookies from a durable Google master token, no per-session browser. Requires `pip install "notebooklm-py[headless]"`. Full guide: [installation.md#d-headless-server-or-ci](installation.md#d-headless-server-or-ci).
- `--master-token` - Bootstrap headless auth. Requires `--account EMAIL`. A visible browser opens Google's EmbeddedSetup to capture the single-use `oauth_token` (needs `[browser]`), or pass it with `--oauth-token`. Exchanges it for a durable master token (saved `0600` at `master_token.json`), mints cookies into `storage_state.json`, and verifies by listing notebooks.
- `--master-token-refresh` - Re-mint cookies from the stored master token, no prompt (for recovery / cron). Replaces `storage_state.json` cookies, preserving CLI context.
- `--oauth-token VALUE` - Provide the single-use EmbeddedSetup `oauth_token` manually (headless boxes without `[browser]`).
- `--cdp-url URL` - Capture `oauth_token` by attaching to a running Chrome over CDP (e.g. `http://localhost:9222`) instead of launching a browser.
- `--android-id HEX` - Override the per-install Android id (default: generated and persisted with the token). ⚠️ The master token is a **full-account, durable** credential — use a dedicated/throwaway account only.
- `--force` - With `--master-token`, overwrite even if the target profile already holds a session for a **different** account. Without it, a mismatched `--account` is refused (use a dedicated `-p <profile>` instead) so account B's mint can't silently clobber account A's profile.

**Examples:**
```bash
# Default (Chromium)
notebooklm login

# Use Microsoft Edge (for orgs that require Edge for SSO)
notebooklm login --browser msedge

# Reuse cookies from your already-logged-in Chrome session
notebooklm login --browser-cookies chrome
notebooklm login --browser-cookies 'chrome::Profile 1'  # one Chromium profile

# Auto-detect any supported browser via rookiepy
notebooklm login --browser-cookies auto

# Firefox Multi-Account Containers: target one container
notebooklm login --browser-cookies 'firefox::Work'
notebooklm login --browser-cookies 'firefox::none'  # no-container default

# Populate a named profile via cookie import
notebooklm --profile work login --browser-cookies chrome

# Pick a specific browser account by email
notebooklm login --browser-cookies chrome --account alice@example.com

# Extract every signed-in browser account into separate profiles
notebooklm login --browser-cookies chrome --all-accounts

# Force a clean browser session before logging in
notebooklm login --fresh

# Headless master-token auth (one browser sign-in, then no per-session browser)
notebooklm login --master-token --account you@gmail.com
notebooklm login --master-token --account you@gmail.com --oauth-token "$OAUTH_TOKEN"  # headless box
notebooklm login --master-token-refresh   # re-mint cookies from the stored token
```

**Notes on `--browser-cookies`:**
- Honors `--profile` / `NOTEBOOKLM_PROFILE` and writes to that profile's `storage_state.json`.
- Without `--account` or `--all-accounts`, imports the selected browser/profile's default Google account into the target profile.
- For Chromium-family browsers, unscoped `chrome`, `brave`, `edge`, etc. fan out across populated user profiles when account selection is needed. Use `chrome::<profile-name-or-directory>` to read exactly one profile; directory names such as `Default` and `Profile 1` are stable across UI renames.
- Use `notebooklm auth inspect --browser <browser>` to see available account emails before a targeted import; pass `-v` to show the Chromium profile directory each account came from.

### Authentication: `auth import-cookies`

Import authentication cookies from a JSON file (or stdin) and persist them to the active profile's `storage_state.json` — a file-backed, persistent alternative to the env-var-based `NOTEBOOKLM_AUTH_JSON` for users who can obtain cookies as JSON but find the browser/Playwright login flow difficult.

> **⚠️ These are full-account credentials.** A JSON cookie export grants the same access as being logged in. Only import a file you exported yourself, keep it private, and delete it after import. Be especially cautious with third-party browser cookie-export extensions.

```bash
notebooklm auth import-cookies JSON_PATH [OPTIONS]
```

Accepts either a Playwright `storage_state` object (`{"cookies": [...]}`) or a bare JSON list of cookie objects (the shape most browser cookie-export tools produce). Use `-` to read JSON from stdin. Common export fields are normalized (e.g. `expirationDate` → `expires`), `__Secure-`/`__Host-` cookies are forced `Secure`, and any `storage_state` `origins` (localStorage/sessionStorage) are dropped.

Imported cookies are filtered through the **same domain allowlist** used by browser login, validated locally for the NotebookLM-required cookies (and a usable secondary binding — `OSID`, or `APISID`+`SAPISID`), and written atomically with private (`0o600`) permissions. Invalid input never overwrites an existing session, and an existing `storage_state.json` is first copied to `storage_state.json.bak` so a stale import can be rolled back (one step — each run overwrites the previous `.bak`).

Incompatible with `NOTEBOOKLM_AUTH_JSON`: unset that env var first, or the command exits with an error (it does not silently fall back to the env auth).

**Options:**
- `--include-domains LABEL` (repeatable) - Opt in to persisting sibling-product cookies (default: required Google auth/Drive/NotebookLM domains only). Pass labels comma-separated or repeat the flag (e.g. `--include-domains youtube,docs` or `--include-domains youtube --include-domains docs`). Same labels as `login`: `youtube`, `docs`, `myaccount`, `mail`, `all`.
- `--include-optional` - Persist all optional sibling-product cookie domains.
- `--json` - Emit a JSON result (`storage_path`, `cookie_count`, `backup_path`).
- `--quiet` - Suppress success output.

**Examples:**
```bash
# Import a bare cookie list exported by a browser tool
notebooklm auth import-cookies cookies.json

# Import a Playwright storage_state into a named profile
notebooklm -p work auth import-cookies playwright-storage-state.json

# Pipe JSON from stdin
cat cookies.json | notebooklm auth import-cookies -
```

### Session: `use`

Set the active notebook for subsequent commands.

```bash
notebooklm use [OPTIONS] <notebook_id>
```

By default, the notebook must exist on the server; a typo or unreachable backend results in a non-zero exit and the saved context is left untouched. Pass `--force` to bypass verification (offline / debugging).

Supports partial ID matching:
```bash
notebooklm use abc  # Matches abc123def456...
```

**Options:**
- `--force` - Skip the existence check and persist the notebook ID even if verification fails.
- `--json` - Emit `{"active_notebook_id": "<id>", "success": true, "verified": true|false, "notebook": {...}}`. `verified: false` covers the `--force` path.

When credentials are expired, `use` routes through the canonical "Not logged in" handler instead of a generic "Could not verify" message; under `--json` you get the standard `{"error": true, "code": "AUTH_REQUIRED", ...}` envelope.

### Session: `status`

Show current context (active notebook and conversation).

```bash
notebooklm status [OPTIONS]
```

**Options:**
- `--paths` - Show resolved configuration file paths
- `--json` - Output as JSON (useful for scripts)

**Examples:**
```bash
# Basic status
notebooklm status

# Show where config files are located
notebooklm status --paths
# Output shows home_dir, storage_path, context_path, browser_profile_dir

# JSON output for scripts
notebooklm status --json
```

**With `--paths`:**
```
                Configuration Paths
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ File            ┃ Path                         ┃ Source          ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ Home Directory  │ /home/user/.notebooklm      │ default         │
│ Storage State   │ .../storage_state.json      │                 │
│ Context         │ .../context.json            │                 │
│ Browser Profile │ .../browser_profile         │                 │
└─────────────────┴──────────────────────────────┴─────────────────┘
```

### Language: `list`, `get`, `set`

Manage the output language for artifact generation (audio, video, etc.).

**Important:** Language is a **GLOBAL** setting that affects all notebooks in your account.

```bash
# List all supported languages with native names
notebooklm language list

# Show current language setting (syncs from server)
notebooklm language get

# Set language to Simplified Chinese
notebooklm language set zh_Hans

# Set language to Japanese
notebooklm language set ja
```

**Options for `get`:**
- `--local` - Skip server sync, show local config only
- `--json` - Output as JSON

**Options for `set`:**
- `--local` - Save to local config only, skip server sync
- `--json` - Output as JSON

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
| `pt_BR` | Português (Brasil) - Brazilian Portuguese |

Run `notebooklm language list` for all 80+ supported languages.

### Share: `status`, `public`, `view-level`, `add`, `update`, `remove`

Manage notebook sharing settings and user permissions.

```bash
# Show current sharing status and shared users
notebooklm share status

# Enable public link sharing (anyone with link can view)
notebooklm share public --enable

# Disable public sharing
notebooklm share public --disable

# Set what viewers can access
notebooklm share view-level full   # Full notebook (chat, sources, notes)
notebooklm share view-level chat   # Chat interface only

# Share with specific users
notebooklm share add user@example.com                        # Add as viewer (default)
notebooklm share add user@example.com --permission editor    # Add as editor
notebooklm share add user@example.com -m "Check this out!"   # With message
notebooklm share add user@example.com --no-notify            # Skip email notification

# Update user permission
notebooklm share update user@example.com --permission editor

# Remove user access
notebooklm share remove user@example.com
notebooklm share remove user@example.com -y   # Skip confirmation
```

**Options (all commands):**
- `-n, --notebook ID` - Specify notebook (uses current if not set, supports partial IDs)
- `--json` - Output as JSON

**Permission levels:**
| Level | Access |
|-------|--------|
| `viewer` | Read-only access (default) |
| `editor` | Can edit notebook content |

**View levels:**
| Level | Viewers can see |
|-------|-----------------|
| `full` | Chat, sources, and notes |
| `chat` | Chat interface only |

### Label: `list`, `sources`, `generate`, `create`, `rename`, `emoji`, `add`, `remove`, `delete`

Manage source labels — AI-generated (or manually named) topic groupings of a notebook's sources.

```bash
# List labels (with member ids + resolved source titles)
notebooklm label list

# Expand a label to its source objects
notebooklm label sources Papers

# AI-group sources into topic labels (the UI's "Reorganize")
notebooklm label generate                       # --scope unlabeled (default, safe)
notebooklm label generate --scope all -y        # destructive: wipe + regenerate every label

# Create an empty, manually-named label
notebooklm label create "Papers" --emoji 📄

# Rename a label (preserves its emoji) / set its emoji
notebooklm label rename Papers "Research Papers"
notebooklm label emoji Papers 📚

# Add source(s) to a label (append; existing members preserved)
notebooklm label add Papers src123 src456

# Remove source(s) from a label (un-assign only; source stays in the notebook)
notebooklm label remove Papers src123

# Delete one or more labels (the label only, not its sources)
notebooklm label delete Papers -y
```

**Options (all commands):**
- `-n, --notebook ID` - Specify notebook (uses current if not set, supports partial IDs)
- `--json` - Output as JSON

**Confirmation gates:**
- `label generate --scope all` wipes and regenerates **every** label with new ids — requires `-y/--yes` (or an interactive prompt).
- `label delete` requires `-y/--yes` (or an interactive prompt).

**Name-or-ID resolution:** `<id|name>` arguments accept a label id (or partial prefix) or an exact label name. An ambiguous name lists the matching ids (with emoji + source count) so you can re-run with the id.

**Selecting sources by label:** `notebooklm source list --label <id|name>` lists only the sources in a label — a read-only saved-selection filter.

### Authentication: `auth check`

Diagnose authentication issues by validating storage file, cookies, and optionally testing token fetch.

```bash
notebooklm auth check [OPTIONS]
```

**Options:**
- `--test` - Also test token fetch from NotebookLM (makes network request)
- `--passive` - With `--test`, validate read-only: never run `NOTEBOOKLM_REFRESH_CMD`, rotate cookies, or write to disk. Use for unattended readiness/health checks that must not mutate state or race real work. No effect without `--test`.
- `--json` - Output as JSON (useful for scripts)

**Examples:**
```bash
# Quick local validation
notebooklm auth check

# Full validation with network test
notebooklm auth check --test

# Read-only readiness probe (no refresh cmd, no cookie write) for a health check
notebooklm auth check --test --passive

# JSON output for automation
notebooklm auth check --json
```

**Checks performed:**
1. Storage file exists and is readable
2. JSON structure is valid
3. Required cookies (`SID` + `__Secure-1PSIDTS`) are present (the Tier 1 `MINIMUM_REQUIRED_COOKIES` set; either `OSID` or the `APISID`+`SAPISID` pair is also needed for the secondary-binding check — see [auth-cookie-lifecycle.md](auth-cookie-lifecycle.md) §3.5)
4. Cookie domains are correct (.google.com vs regional)
5. (With `--test`) Token fetch succeeds

**Output shows:**
- Authentication source (file path or environment variable)
- Which cookies were found and from which domains
- Detailed cookie breakdown by domain (highlighting key auth cookies)
- Token lengths when using `--test`

**Use cases:**
- Debug "Not logged in" errors
- Verify auth setup in CI/CD environments
- Check if cookies are from correct domain (regional vs .google.com)
- Diagnose NOTEBOOKLM_AUTH_JSON environment variable issues

**Exit codes:** `auth check` exits `0` only when every *executed* check passes
and non-zero (`1`) when any executed check fails — in **both** text and `--json`
modes. A skipped check does not count as a failure: without `--test`, the token
fetch is skipped, so the exit status reflects only the local cookie checks.
Unattended monitors should therefore rely on the exit code (not on parsing the
table). To gate readiness on a real token fetch, run `notebooklm auth check
--test` and check the exit status. See [CLI Exit-Code Convention](cli-exit-codes.md).

### Authentication: `auth refresh`

One-shot keepalive: open a session, trigger the layer-1 SIDTS rotation poke against `accounts.google.com`, persist the rotated cookies to `storage_state.json`, and exit. When a file-backed Playwright storage state has cookies but lacks in-band `notebooklm.account` metadata, `auth refresh` also repairs that metadata if account discovery is unambiguous. It does not replace existing metadata; use `login --browser-cookies <browser> --account EMAIL` to re-bind a profile that already points at the wrong account. Designed to be invoked by the OS scheduler (launchd / systemd / cron / Task Scheduler / k8s CronJob) so an otherwise-idle profile does not stale out between user-driven calls.

```bash
notebooklm auth refresh [OPTIONS]
```

**Options:**
- `--browser-cookies <browser>`, `--browser-cookie <browser>` - Re-extract cookies from an installed browser and match the current profile's account from `context.json`. This repairs account routing when browser account order changes after another account logs out. Accepts the same scoped syntax as `login`: `chrome::<profile-name-or-directory>` for one Chromium profile, and `firefox::<container-name>` or `firefox::none` for one Firefox container.
- `--include-domains LABEL[,LABEL...]` - Forward to the browser-cookie reader (only meaningful with `--browser-cookies`). Same syntax as `notebooklm login --include-domains`.
- `--quiet`, `-q` - Suppress success output; print only on error (cron-friendly)
- `--verify` - After refreshing, run a read-only passive token fetch to confirm the resulting cookies actually authenticate; exit non-zero if they still fail. A successful refresh command alone does not prove the post-refresh cookies work — they may still redirect to sign-in. Especially valuable with `--browser-cookies`, which rewrites the cookie jar but does not otherwise verify it.

**Cadence:** 15-20 minutes is the recommended interval. Tighter is wasteful (the 60 s mtime guard would skip it anyway); significantly looser may cross the `__Secure-1PSIDTS` server-side validity window for your account/region.

**Requires file-backed authentication.** `auth refresh` refuses to run when `NOTEBOOKLM_AUTH_JSON` is set, because the inline-JSON auth mode has no writable backing store to persist rotated cookies into. Use a profile-backed `storage_state.json` (the default) or set `NOTEBOOKLM_HOME` / `--profile` to point at one.

**Exit codes:**
- `0` - the auth path completed without raising. The rotation POST is **best-effort**: exit 0 also covers (a) the 60 s mtime guard skipping the POST, (b) `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` being set, (c) another process holding the cross-process rotate lock, and (d) a transient `httpx` error during the POST being caught and logged at DEBUG. Treat exit 0 as "no error" rather than "rotation occurred." For verification, enable `NOTEBOOKLM_LOG_LEVEL=DEBUG` and check for the `RotateCookies` log line.
- `1` - a fatal error reached the CLI layer (e.g. `NOTEBOOKLM_AUTH_JSON` set, missing `storage_state.json`, invalid profile, `httpx.RequestError` not swallowed by the rotate guard), **or** `--verify` was passed and the post-refresh passive token fetch failed. The OS scheduler's next firing is the retry mechanism; this command does not retry in-process.

**Examples:**
```bash
# One-shot refresh against the default profile
notebooklm auth refresh

# Refresh a named profile (works with --profile / NOTEBOOKLM_PROFILE)
notebooklm --profile work auth refresh

# Re-extract from Chrome and repair account routing if browser account order changed
notebooklm --profile work auth refresh --browser-cookies chrome
notebooklm --profile work auth refresh --browser-cookies 'chrome::Profile 1'

# Quiet variant for cron / systemd
notebooklm --profile work auth refresh --quiet
```

**Pairs with:**
- `NOTEBOOKLM_REFRESH_CMD` for in-process auth-expiry recovery (covers the case where `auth refresh` itself fails because cookies have already expired beyond rotation)
- `keepalive=<seconds>` on `NotebookLMClient` for in-process long-lived workers (no OS scheduler needed)

See [Troubleshooting](troubleshooting.md) for full per-OS scheduler recipes (launchd plist, systemd user timer, cron, Task Scheduler, k8s CronJob).

### Authentication: `auth inspect`

List Google accounts visible to a browser's cookie store. **Read-only — never writes to disk.** Use this before `notebooklm login --browser-cookies <browser> --account <email>` to see which account emails are available. Chromium-family browsers fan out across populated user profiles by default; use `chrome::<profile-name-or-directory>` to inspect only one profile.

```bash
notebooklm auth inspect [OPTIONS]
```

**Options:**
- `--browser TEXT` - Browser to read cookies from (`chrome`, `firefox`, `brave`, `edge`, `safari`, `arc`, ...). `auto` picks the first one rookiepy can read. Use `chrome::<profile-name-or-directory>` for one Chromium profile, or `firefox::<container-name>` / `firefox::none` for one Firefox container. Requires `pip install "notebooklm-py[cookies]"`.
- `--include-domains LABEL[,LABEL...]` - Opt in to enumerating accounts via sibling-product cookies (same syntax as `notebooklm login --include-domains`). By default this command consults only required Google auth cookies, which is sufficient for account discovery on every tested path.
- `--json` - Output as JSON

**Examples:**
```bash
notebooklm auth inspect --browser chrome
notebooklm auth inspect --browser 'chrome::Profile 1' --json
notebooklm auth inspect --browser firefox --json
```

### Authentication: `auth logout`

Log out by clearing saved authentication. Removes both the saved cookie file (`storage_state.json`) and the cached browser profile. After logout, run `notebooklm login` to authenticate with a different Google account.

```bash
notebooklm auth logout
```

**Examples:**
```bash
notebooklm auth logout                        # Clear auth for active profile
notebooklm -p work auth logout                # Clear auth for 'work' profile
notebooklm --storage A.json auth logout       # Clear the override auth file
```

### Session: `completion`

Print the shell completion script for `bash`, `zsh`, or `fish`. Pipe the output into a file your shell sources at startup; once the script is loaded, Click handles the `_NOTEBOOKLM_COMPLETE` env-var protocol automatically.

```bash
notebooklm completion <bash|zsh|fish>
```

**Install (one-time):**

```bash
# zsh — drop the script anywhere on $fpath; the file MUST be named _notebooklm
notebooklm completion zsh > ~/.zfunc/_notebooklm
# (ensure ~/.zfunc is on $fpath in ~/.zshrc, then `autoload -Uz compinit && compinit`)

# bash — source from ~/.bashrc
notebooklm completion bash > ~/.notebooklm-complete.bash
echo 'source ~/.notebooklm-complete.bash' >> ~/.bashrc

# fish — drop into the system completions directory
notebooklm completion fish > ~/.config/fish/completions/notebooklm.fish
```

**ID-aware tab completion:** Once the script is installed, `-n/--notebook`, `-s/--source`, and `-a/--artifact` complete from the active profile's live IDs:

```bash
notebooklm ask -n <TAB>            # lists notebook IDs (filtered by what you've typed)
notebooklm ask -s <TAB>            # lists sources in the active notebook
notebooklm download audio -a <TAB> # lists artifacts in the active notebook
```

Note: commands that take a positional source / artifact / notebook ID (e.g. `source delete <id>`, `artifact get <id>`, `use <id>`) currently complete only after the option / argument is fully typed — Click's `shell_complete=` is attached to the flag-style `-n/--notebook`, `-s/--source`, `-a/--artifact` declarations enumerated below.

For `-s` and `-a` the active notebook is resolved with the same precedence the command body uses: `-n/--notebook` flag value already on the line > `NOTEBOOKLM_NOTEBOOK` env var > the persisted `notebooklm use` context. With no resolvable notebook (and on any auth / network failure), the completer returns no suggestions silently — it never prints a traceback into your terminal.

**Print-only by design:** the command never writes to your shell config; you decide where the script lands. This keeps the install path discoverable and avoids surprising shutdowns of existing completion setups.

### Source: `add` — `--follow-symlinks` security gate

File-source uploads reject symlinks by default. If the path you pass (or any ancestor directory) is a symbolic link, `source add` refuses the upload rather than silently following it — a workspace symlink could otherwise exfiltrate the file it points at (e.g. `~/Downloads/foo.pdf -> /etc/passwd`). Pass `--follow-symlinks` to opt in explicitly.

URL sources reject internal hosts (`localhost`, loopback, private IP ranges,
and link-local addresses) by default so the CLI cannot be used as an SSRF
trampoline. Pass `--allow-internal` only for a deliberate local NotebookLM test;
non-HTTP(S) schemes remain rejected even with the flag.

> **Python equivalent:** [`client.sources.add_file(nb_id, path, title=...)`](python-api.md#sourcesapi-clientsources). The symlink gate is a CLI-only safeguard; callers using the Python API are responsible for resolving symbolic links before passing the path.

```bash
# Default — symlink rejected with: "Path is a symlink; pass --follow-symlinks to follow it explicitly."
notebooklm source add ./link-to-doc.pdf --type file

# Opt-in: resolve the symlink, then upload the resolved target
notebooklm source add ./link-to-doc.pdf --type file --follow-symlinks
```

The same gate applies on the explicit `--type file` path (no auto-detect), so typing the source type as `file` does not bypass the check.

<a id="source-add-mime-type-file-sources"></a>

### Source: `add` `--mime-type` (file sources)

The `--mime-type` flag on `notebooklm source add` (file-source path) is a
**supported** parameter. It overrides the filename-extension inference and
sets the content-type header used for the resumable upload handshake. Omit it
to let the MIME type be inferred from the file extension.

```bash
# Inferred from the extension (typical case — no flag needed)
notebooklm source add ./report.pdf --type file

# Override inference (e.g. an extensionless export, or a misnamed file)
notebooklm source add ./report.bin --type file --mime-type application/pdf
```

> **Note:** The same `--mime-type` flag on `notebooklm source add-drive`
> (Google Drive sources) selects between `google-doc` / `google-slides` /
> `google-sheets` / `pdf` Drive document types.
>
> Historical: an earlier release treated the file-source `--mime-type` as a
> deprecated no-op. It was re-wired to set the upload content-type and is no
> longer deprecated; the `NOTEBOOKLM_QUIET_DEPRECATIONS` notice it used to
> emit is gone.

### Source: `add-research`

Perform AI-powered research and add discovered sources to the notebook.

> **Python equivalent:** [`client.research.start(nb_id, query, source="web", mode="deep")`](python-api.md#researchapi-clientresearch) followed by `client.research.wait_for_completion(...)` / `client.research.import_sources(...)`.

```bash
notebooklm source add-research [query] [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--mode [fast|deep]` - Research depth (default: fast)
- `--from [web|drive]` - Search source (default: web)
- `--import-all` - Automatically import all found sources (works with blocking mode)
- `--cited-only` - With `--import-all`, import only cited sources
- `--no-wait` - Start research and return immediately (non-blocking)
- `--timeout SECONDS` - Per-phase seconds budget for (a) the research-completion poll loop and (b) the `--import-all` retry loop (default: 1800). Each phase gets the full budget independently, so worst-case total wall time is up to 2× this value. Matches `research wait --timeout` semantics. Before 0.4.2 the in-line poll was hardcoded to 5 minutes, so deep research that ran longer was silently abandoned and left an "Add sources?" modal hanging in the NotebookLM web UI — bump `--timeout` for long deep-research runs.
- `--prompt-file PATH` - Read query from a file (or `-` for stdin) instead of the positional argument

> **Note:** `--mode deep` is only supported with `--from web` (the default). Combining `--mode deep --from drive` is rejected by the backend with `ValidationError("Deep Research only supports Web sources.")` — for Drive, stick with `--mode fast`.

**Examples:**
```bash
# Fast web research (blocking)
notebooklm source add-research "Quantum computing basics"

# Drive research (fast mode only — Drive does not support --mode deep)
notebooklm source add-research "Project Alpha" --from drive

# Non-blocking deep research for agent workflows (web only — see note above)
notebooklm source add-research "AI safety papers" --mode deep --no-wait

# Import only cited sources for tighter relevance
notebooklm source add-research "topic" --import-all --cited-only

# Bounded import-retry budget for large result sets
notebooklm source add-research "AI papers" --mode deep --import-all --timeout 3600

# Read long query from file (or stdin via '-')
notebooklm source add-research --prompt-file research_query.txt --mode deep
echo "very long query..." | notebooklm source add-research --prompt-file -
```

### Research: `status`

Check research status for the current notebook (non-blocking).

> **Python equivalent:** [`client.research.poll(nb_id)`](python-api.md#researchapi-clientresearch).

```bash
notebooklm research status [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--json` - Output as JSON

**Output states:**
- **No research running** - No active research session
- **Research in progress** - Deep research is still running
- **Research completed** - Shows query, found sources, and summary

**Examples:**
```bash
# Check status
notebooklm research status

# JSON output for scripts/agents
notebooklm research status --json
```

### Research: `wait`

Wait for research to complete (blocking).

> **Python equivalent:** [`client.research.wait_for_completion(nb_id)`](python-api.md#researchapi-clientresearch), then optionally call `client.research.import_sources(...)` (matches the CLI's `--import-all` / `--cited-only` behavior).

```bash
notebooklm research wait [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--timeout SECONDS` - Maximum seconds to wait (default: 300)
- `--interval SECONDS` - Seconds between status checks (default: 5)
- `--import-all` - Import all found sources when done
- `--cited-only` - With `--import-all`, import only cited sources
- `--json` - Output as JSON

**Examples:**
```bash
# Basic wait
notebooklm research wait

# Wait longer for deep research
notebooklm research wait --timeout 600

# Wait and auto-import sources
notebooklm research wait --import-all

# Import only cited sources
notebooklm research wait --import-all --cited-only

# JSON output for agent workflows
notebooklm research wait --json --import-all
```

**Use case:** Primarily for LLM agents that need to wait for non-blocking deep research started with `source add-research --no-wait`.

### Research: `cancel`

Cancel an in-flight research run.

> **Python equivalent:** [`client.research.cancel(nb_id, run_id)`](python-api.md#researchapi-clientresearch).

```bash
notebooklm research cancel RUN_ID [OPTIONS]
```

**Arguments:**
- `RUN_ID` - The run's poll-level id — the `task_id` shown by `research status`. For **deep** research this is the `report_id` returned by `source add-research`, **not** the deep start `task_id` (which is a sessionId and will not cancel anything).

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--json` - Output as JSON

**Examples:**
```bash
# Cancel a run (find the run id with `research status`)
notebooklm research cancel <run_id>

# JSON output for agent workflows
notebooklm research cancel <run_id> --json
```

> **Fire-and-forget:** the server reports neither success nor failure for a cancel and does not validate the run id, so this command cannot confirm the cancel took effect. Run `research status` afterward — a cancelled in-progress run shows as `failed`.

### Generate: `audio`

Generate an audio overview (podcast).

> **Python equivalent:** [`client.artifacts.generate_audio(nb_id, ...)`](python-api.md#generation-methods).

```bash
notebooklm generate audio [description] [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--format [deep-dive|brief|critique|debate]` - Podcast format (default: deep-dive)
- `--length [short|default|long]` - Duration (default: default)
- `--language LANG` - Output language (precedence: `--language` > `NOTEBOOKLM_HL` env > config > `'en'`)
- `-s, --source ID` - Limit to specific source IDs (repeatable, uses all if not specified)
- `--wait / --no-wait` - Wait for completion (default: `--no-wait`)
- `--timeout SECONDS` - Maximum seconds to wait (default: 1200; no-op without `--wait`)
- `--interval SECONDS` - Seconds between status checks (default: 2; no-op without `--wait`)
- `--retry N` - Retry N times with exponential backoff on rate limit
- `--json` - Output as JSON (returns `task_id` and `status`)
- `--prompt-file PATH` - Read description from a file (or `-` for stdin); mutually exclusive with positional argument

**Examples:**
```bash
# Basic podcast (starts async, returns immediately)
notebooklm generate audio

# Debate format with custom instructions
notebooklm generate audio "Compare the two main viewpoints" --format debate

# Generate and wait for completion
notebooklm generate audio "Focus on key points" --wait

# Generate using only specific sources
notebooklm generate audio -s src_abc -s src_def

# JSON output for scripting/automation
notebooklm generate audio --json
# Output: {"task_id": "abc123...", "status": "pending"}

# Read long instructions from a file
notebooklm generate audio --prompt-file instructions.txt --format debate
```

### Generate: `video`

Generate a video overview. Use `--format cinematic` for AI-generated documentary footage (Veo 3); cinematic videos ignore `--style` and take ~30-40 min (requires AI Ultra). For non-cinematic formats, see `generate cinematic-video` for the alias subcommand.

> **Python equivalent:** [`client.artifacts.generate_video(nb_id, ...)`](python-api.md#generation-methods).

```bash
notebooklm generate video [description] [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--format [explainer|brief|cinematic]` - Video format
- `--style [auto|custom|classic|whiteboard|kawaii|anime|watercolor|retro-print|heritage|paper-craft]` - Visual style
- `--style-prompt TEXT` - Custom visual style prompt (required when `--style custom`; rejected with `--format cinematic`)
- `--language LANG` - Output language (precedence: `--language` > `NOTEBOOKLM_HL` env > config > `'en'`)
- `-s, --source ID` - Limit to specific source IDs (repeatable, uses all if not specified)
- `--wait / --no-wait` - Wait for completion (default: `--no-wait`)
- `--timeout SECONDS` - Maximum seconds to wait (default: 1800; `--format cinematic` defaults to 3600; no-op without `--wait`)
- `--interval SECONDS` - Seconds between status checks (default: 2; no-op without `--wait`)
- `--retry N` - Retry N times with exponential backoff on rate limit
- `--json` - Output as JSON (returns `task_id` and `status`)
- `--prompt-file PATH` - Read description from a file (or `-` for stdin); mutually exclusive with positional argument

**Examples:**
```bash
# Kid-friendly explainer
notebooklm generate video "Explain for 5 year olds" --style kawaii

# Custom visual style
notebooklm generate video --style custom --style-prompt "hand-drawn diagrams"

# Cinematic format (Veo 3)
notebooklm generate video --format cinematic "documentary overview"

# Professional style
notebooklm generate video --style classic --wait

# Generate from specific sources only
notebooklm generate video -s src_123 -s src_456

# JSON output for scripting/automation
notebooklm generate video --json
```

**Note:** Passing a non-cinematic `--format` to the `cinematic-video` alias exits `2` with a `UsageError`. Use `generate video --format <other>` for non-cinematic formats.

### Generate: `revise-slide`

Revise an individual slide in an existing slide deck using a natural-language prompt.

> **Python equivalent:** [`client.artifacts.revise_slide(nb_id, artifact_id, slide_index, instructions)`](python-api.md#generation-methods).

```bash
notebooklm generate revise-slide [description] --artifact <id> --slide N [OPTIONS]
```

**Required Options:**
- `-a, --artifact ID` - The slide deck artifact ID to revise
- `--slide N` - Zero-based index of the slide to revise (0 = first slide)

**Optional:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--wait / --no-wait` - Wait for completion (default: `--no-wait`)
- `--timeout SECONDS` - Maximum seconds to wait (default: 300; no-op without `--wait`)
- `--interval SECONDS` - Seconds between status checks (default: 2; no-op without `--wait`)
- `--retry N` - Retry N times with exponential backoff on rate limit
- `--json` - Machine-readable output
- `--prompt-file PATH` - Read description from a file (or `-` for stdin); mutually exclusive with positional argument

**Examples:**
```bash
# Revise the first slide
notebooklm generate revise-slide "Move the title up" --artifact art123 --slide 0

# Revise the fourth slide and wait for completion
notebooklm generate revise-slide "Remove taxonomy table" --artifact art123 --slide 3 --wait
```

**Note:** The slide deck must already be fully generated before using `revise-slide`. Use `artifact list` to find the artifact ID.

---

### Generate: `report`

Generate a text report (briefing doc, study guide, blog post, or custom).

> **Python equivalent:** [`client.artifacts.generate_report(nb_id, report_format=..., ...)`](python-api.md#generation-methods).

```bash
notebooklm generate report [description] [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--format [briefing-doc|study-guide|blog-post|custom]` - Report format (default: briefing-doc)
- `--append TEXT` - Append extra instructions to the built-in prompt (no effect with `--format custom`)
- `--language LANG` - Output language (precedence: `--language` > `NOTEBOOKLM_HL` env > config > `'en'`)
- `-s, --source ID` - Limit to specific source IDs (repeatable, uses all if not specified)
- `--wait / --no-wait` - Wait for completion (default: `--no-wait`)
- `--timeout SECONDS` - Maximum seconds to wait (default: 300; no-op without `--wait`)
- `--interval SECONDS` - Seconds between status checks (default: 2; no-op without `--wait`)
- `--retry N` - Retry N times with exponential backoff on rate limit
- `--json` - Output as JSON
- `--prompt-file PATH` - Read description from a file (or `-` for stdin); mutually exclusive with positional argument

**Examples:**
```bash
notebooklm generate report --format study-guide
notebooklm generate report "Executive summary for stakeholders" --format briefing-doc

# Generate report from specific sources
notebooklm generate report --format study-guide -s src_001 -s src_002

# Custom report with description (auto-selects custom format)
notebooklm generate report "Create a white paper analyzing the key trends"

# Append instructions to a built-in format
notebooklm generate report --format study-guide --append "Target audience: beginners"
notebooklm generate report --format briefing-doc --append "Focus on AI trends, keep it under 2 pages"

# Read a long custom prompt from file (auto-selects custom format)
notebooklm generate report --prompt-file custom_report.txt
```

### Artifact: `list`, `get`, `get-prompt`, `rename`, `delete`, `export`, `poll`, `wait`, `retry`, `suggestions`

Manage existing artifacts (audio, video, slide decks, quizzes, reports, etc.). Every subcommand resolves the notebook via the standard precedence (`-n/--notebook` flag > `NOTEBOOKLM_NOTEBOOK` env > active context).

> **Python equivalent:** [`client.artifacts.list/get/get_prompt/rename/delete/poll_status/wait_for_completion/retry_failed/suggest_reports(...)`](python-api.md#artifactsapi-clientartifacts) for management; [`export_report` / `export_data_table` / `export(...)`](python-api.md#export-methods) for export.

```bash
notebooklm artifact <list|get|get-prompt|rename|delete|export|poll|wait|retry|suggestions> [OPTIONS]
```

**Common options (all subcommands):**
- `-n, --notebook ID` - Notebook ID (uses current if not set; supports partial IDs)

**Per-subcommand options:**

| Subcommand | Required arguments | Options |
|---|---|---|
| `list` | (none) | `--type [all\|audio\|video\|slide-deck\|quiz\|flashcard\|infographic\|data-table\|mind-map\|report]`, `--limit N` (default: unlimited), `--no-truncate`, `--json` |
| `get` | `ARTIFACT_ID` | `--json` |
| `get-prompt` | `ARTIFACT_ID` | `--json` |
| `rename` | `ARTIFACT_ID NEW_TITLE` | `--json` |
| `delete` | `ARTIFACT_ID` | `-y/--yes` (skip confirmation), `--json` |
| `export` | `ARTIFACT_ID` | `--title TEXT` (**required**), `--type [docs\|sheets]` (default: docs), `--json` |
| `poll` | `TASK_ID` (from `generate <type>`) | `--json` |
| `wait` | `ARTIFACT_ID` | `--timeout SECONDS` (default: 300), `--interval SECONDS` (default: 2), `--json` |
| `retry` | `ARTIFACT_ID` | `--wait` (block until terminal), `--timeout SECONDS` (default: 300), `--interval SECONDS` (default: 2), `--json` |
| `suggestions` | (none) | `--json` |

**Examples:**
```bash
# Filter artifact list to one notebook and one type, JSON for scripting
notebooklm artifact list --notebook nb_abc --type audio --json

# Inspect a single artifact (partial ID OK)
notebooklm artifact get art123 --json

# Show the prompt an artifact was generated from
notebooklm artifact get-prompt art123 --json

# Rename an artifact
notebooklm artifact rename art123 "Final cut"

# Delete without prompting
notebooklm artifact delete art123 -y --json

# Export a report to Google Docs (title is required)
notebooklm artifact export art123 --title "Climate briefing" --type docs

# Poll a task immediately after a generate call returns
notebooklm generate audio --json   # -> { "task_id": "task_abc...", "status": "..." }
notebooklm artifact poll task_abc

# Block until the artifact finishes (or 600s elapses)
notebooklm artifact wait art123 --timeout 600 --json

# Retry a failed artifact in place (UI "Retry"); --wait blocks until terminal
notebooklm artifact retry art123 --wait

# Get AI-suggested report topics for the active notebook
notebooklm artifact suggestions --json
```

> **`poll` vs `wait`:** The API returns one identifier that doubles as both `task_id` (right after `generate`) and `artifact_id` (once it appears in `artifact list`). `poll` is a one-shot status check that does not prefix-match against `artifact list` (use it immediately after `generate`); `wait` blocks until terminal status and accepts partial IDs that resolve against `artifact list`. See the quick-reference note above for full details.

### Download: `audio`, `video`, `slide-deck`, `infographic`, `report`, `mind-map`, `data-table`

Download generated artifacts to your local machine.

> **Python equivalent:** the per-type [`client.artifacts.download_audio/video/slide_deck/infographic/report/mind_map/data_table(nb_id, path, ...)`](python-api.md#downloading-artifacts) methods.

```bash
notebooklm download <type> [OUTPUT_PATH] [OPTIONS]
```

**Artifact Types and Output Formats:**

| Type | Default Extension | Description |
|------|-------------------|-------------|
| `audio` | `.mp3` | Audio overview (podcast) as MP3 |
| `video` | `.mp4` | Video overview |
| `slide-deck` | `.pdf` or `.pptx` | Slide deck as PDF (default) or PowerPoint |
| `infographic` | `.png` | Infographic image |
| `report` | `.md` | Report as Markdown (Briefing Doc, Study Guide, etc.) |
| `mind-map` | `.json` | Mind map as JSON tree structure |
| `data-table` | `.csv` | Data table as CSV (UTF-8 with BOM for Excel) |

**Options:**
- `--all` - Download all artifacts of this type
- `--latest` - Download only the most recent artifact (default if no ID/name provided)
- `--earliest` - Download only the oldest artifact
- `--name NAME` - Download artifact with matching title (supports partial matches)
- `-a, --artifact ID` - Select specific artifact by ID (supports partial IDs)
- `--dry-run` - Show what would be downloaded without actually downloading
- `--force` - Overwrite existing files
- `--no-clobber` - Don't overwrite existing files (a single download fails; `--all` skips them; default is auto-rename)
- `--format [pdf|pptx]` - Slide deck format (slide-deck command only, default: pdf)
- `--json` - Output result in JSON format

**Examples:**
```bash
# Download the latest podcast
notebooklm download audio ./podcast.mp3

# Download all infographics
notebooklm download infographic --all

# Download a specific slide deck by name
notebooklm download slide-deck --name "Final Presentation"

# Download slide deck as PPTX (editable PowerPoint)
notebooklm download slide-deck --format pptx

# Preview a batch download
notebooklm download audio --all --dry-run

# Download a report as markdown
notebooklm download report ./study-guide.md

# Download mind map as JSON
notebooklm download mind-map ./concept-map.json

# Download data table as CSV (opens in Excel)
notebooklm download data-table ./research-data.csv
```

### Download: `quiz`, `flashcards`

Download quiz questions or flashcard decks in various formats.

> **Python equivalent:** [`client.artifacts.download_quiz(nb_id, path, output_format="json|markdown|html")`](python-api.md#downloading-artifacts) and `download_flashcards(...)` with the same signature.

```bash
notebooklm download quiz [OUTPUT_PATH] [OPTIONS]
notebooklm download flashcards [OUTPUT_PATH] [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current context if not set)
- `--format FORMAT` - Output format: `json` (default), `markdown`, or `html`
- `-a, --artifact ID` - Select specific artifact by ID

**Output Formats:**
- **JSON** - Structured data preserving full API fields (answerOptions, rationale, isCorrect, hint)
- **Markdown** - Human-readable format with checkboxes for correct answers
- **HTML** - Raw HTML as returned from NotebookLM

**Examples:**
```bash
# Download quiz as JSON
notebooklm download quiz quiz.json

# Download quiz as markdown
notebooklm download quiz --format markdown quiz.md

# Download flashcards as JSON (normalizes f/b keys to front/back)
notebooklm download flashcards cards.json

# Download flashcards as markdown
notebooklm download flashcards --format markdown cards.md

# Download flashcards as raw HTML
notebooklm download flashcards --format html cards.html
```

### Note: `list`, `create`, `get`, `save`, `rename`, `delete`

Read, create, and update notebook notes (the "Notes" panel in the web UI). Mind-map artifacts surface as notes in the underlying API but are filtered out of `note list` — use `artifact list --type mind-map` for those.

> **Python equivalent:** [`client.notes.list/create/get/update/delete(...)`](python-api.md#notesapi-clientnotes). The CLI's `note save` and `note rename` both map to `client.notes.update(...)` (title and/or content). Mind-map helpers (`list_mind_maps`, `delete_mind_map`) live on the same API.

```bash
notebooklm note <list|create|get|save|rename|delete> [OPTIONS]
```

**Common options (all subcommands):**
- `-n, --notebook ID` - Notebook ID (uses current if not set; supports partial IDs)
- `--json` - Machine-readable output

**Per-subcommand options:**

| Subcommand | Required arguments | Type-specific options |
|---|---|---|
| `list` | (none) | — |
| `create` | `[CONTENT]` (positional or stdin via `-`) | `--content TEXT` (or `-` for stdin; mutually exclusive with positional), `-t/--title TEXT` (default: `"New Note"`) |
| `get` | `NOTE_ID` | — |
| `save` | `NOTE_ID` | `--title TEXT`, `--content TEXT` |
| `rename` | `NOTE_ID NEW_TITLE` | — |
| `delete` | `NOTE_ID` | `-y/--yes` (skip confirmation) |

**Examples:**
```bash
# List notes in the active notebook
notebooklm note list --json

# Create a titled note from a positional string
notebooklm note create "Quick thought" -t "Idea: research follow-up"

# Pipe markdown from a file or another command (use `-` to read stdin)
cat draft.md | notebooklm note create -

# Equivalent --content form
cat draft.md | notebooklm note create --content -

# Update an existing note (partial ID OK; only the fields you pass change)
notebooklm note save note_abc --title "Updated title"
notebooklm note save note_abc --content "New body"

# Rename without touching content
notebooklm note rename note_abc "Renamed"

# Delete without prompting
notebooklm note delete note_abc -y --json
```

### Profile: `list`, `create`, `switch`, `delete`, `rename`

Manage named profiles under `$NOTEBOOKLM_HOME/profiles/<name>/`. Each profile owns its own `storage_state.json`, `context.json`, and browser profile, so multiple Google accounts can coexist. The active profile is selected by `-p/--profile`, `NOTEBOOKLM_PROFILE`, or the `default_profile` field in `$NOTEBOOKLM_HOME/config.json` (in that precedence). See [configuration.md](configuration.md) for the full profile model.

```bash
notebooklm profile <list|create|switch|delete|rename> [OPTIONS]
```

**Per-subcommand options:**

| Subcommand | Required arguments | Options |
|---|---|---|
| `list` | (none) | `--json` |
| `create` | `NAME` | — |
| `switch` | `NAME` | — |
| `delete` | `NAME` | `--yes`/`-y` (skip prompt; `--confirm` is a deprecated alias; the active default profile cannot be deleted) |
| `rename` | `OLD_NAME NEW_NAME` | — |

**Examples:**
```bash
# Enumerate profiles (active flag included in JSON)
notebooklm profile list --json

# Create a profile shell, then authenticate into it
notebooklm profile create work
notebooklm -p work login

# Make 'work' the default for subsequent commands
notebooklm profile switch work

# Rename a profile (does not change `storage_state.json` contents)
notebooklm profile rename work work-old

# Delete a non-active profile without prompting
notebooklm profile delete old-account --yes
```

> **Note:** `profile delete` refuses to remove the currently active default profile. Switch to a different profile first (`notebooklm profile switch <other>`) and then delete.

### Skill: `install`, `status`, `uninstall`, `show`

Manage the bundled NotebookLM agent-skill template. The skill lives in `SKILL.md` at the repository root; installing it materializes a copy under one of:
- `.claude/skills/notebooklm/SKILL.md` (Claude Code, `--target claude`)
- `.agents/skills/notebooklm/SKILL.md` (universal agent skill directory, `--target agents`)

`--scope` selects whether to write into the **user's** home (`~/.claude/skills/...`, `~/.agents/skills/...`) or the **current project** (`./.claude/skills/...`, `./.agents/skills/...`).

```bash
notebooklm skill <install|status|uninstall|show> [OPTIONS]
```

**Options matrix** (defaults: `--scope user --target all`):

| Subcommand | `--scope` choices | `--target` choices | Default `--target` |
|---|---|---|---|
| `install` | `user`, `project` | `all`, `claude`, `agents` | `all` |
| `status` | `user`, `project` | `all`, `claude`, `agents` | `all` |
| `uninstall` | `user`, `project` | `all`, `claude`, `agents` | `all` |
| `show` | `user`, `project` | `source`, `claude`, `agents` | `source` |

`skill show --target source` prints the packaged `SKILL.md` straight out of the wheel (the canonical content); the other `show` targets read the materialized copy from disk.

Project-scope install hardening:

- `--dry-run` prints the target files and actions without writing.
- `--no-clobber` creates missing targets but skips differing existing files.
- `--force` overwrites differing project targets.
- These flags are project-scope only; `--scope user` preserves the historical always-overwrite behavior.

**Examples:**
```bash
# Install both targets for the current user (default scope+target)
notebooklm skill install

# Install only the Claude Code target into the current project
notebooklm skill install --scope project --target claude --dry-run
notebooklm skill install --scope project --target claude --force

# Inspect what's installed in the user-scope agents directory
notebooklm skill status --scope user --target agents

# Print the packaged skill source (e.g. for piping into another agent's loader)
notebooklm skill show --target source

# Print the installed Claude target verbatim
notebooklm skill show --scope project --target claude

# Remove all installed targets from the current project
notebooklm skill uninstall --scope project --target all
```

Codex does not consume the `skill` subcommand. In this repository it reads the root [`AGENTS.md`](../AGENTS.md) file and invokes the `notebooklm` CLI or Python API directly.

### Source: `add-drive`

Add a Google Drive document, slide deck, sheet, or PDF as a source. The Drive `--mime-type` selects which Drive document type to import (Google Doc / Slides / Sheets / PDF). This is distinct from the file-source `--mime-type` documented above, which sets the resumable-upload content-type for a locally-uploaded file.

> **Python equivalent:** [`client.sources.add_drive(nb_id, file_id, title, mime_type=...)`](python-api.md#sourcesapi-clientsources).

```bash
notebooklm source add-drive [OPTIONS] FILE_ID TITLE
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set; supports partial IDs)
- `--mime-type [google-doc|google-slides|google-sheets|pdf]` - Drive document type (default: `google-doc`)
- `--json` - Output as JSON

**Examples:**
```bash
# Default — treat the Drive file as a Google Doc
notebooklm source add-drive 1AbcD...XyZ "Project Brief"

# Import as Google Slides
notebooklm source add-drive 1AbcD...XyZ "Quarterly Deck" --mime-type google-slides

# Import a Drive-hosted PDF
notebooklm source add-drive 1AbcD...XyZ "Whitepaper" --mime-type pdf --json
```

### Source: `stale`, `clean`

`source stale` is a shell-friendly predicate that reports whether a URL/Drive source needs a refresh; `source clean` removes duplicate, error, or access-blocked sources in bulk.

> **Python equivalent:** [`client.sources.check_freshness(nb_id, source_id)`](python-api.md#sourcesapi-clientsources) returns the same staleness verdict. There is no single bulk "clean" call — combine `client.sources.list(...)` with a filter on duplicate/error/blocked status and call `client.sources.delete(...)` per match.

```bash
notebooklm source stale [OPTIONS] SOURCE_ID
notebooklm source clean [OPTIONS]
```

**`stale` exit codes (standard by default; opt-in inverted predicate via `--exit-on-stale`):**
- Default: `0` on success regardless of freshness, `1` on error. Branch on the JSON `stale`/`fresh` fields (or stdout text) for the verdict.
- With `--exit-on-stale`: `0` if stale (needs refresh), `1` if fresh — for back-compat with the `if … ; then refresh; fi` shell idiom.
- See [CLI Exit-Code Convention](cli-exit-codes.md) for details.

**`stale` options:** `-n/--notebook ID`, `--exit-on-stale`, `--json`.

**`clean` options:** `-n/--notebook ID`, `--dry-run` (preview the candidate set), `-y/--yes` (skip the confirmation prompt), `--json`.

**Examples:**
```bash
# Refresh a single stale URL source if needed (back-compat shell idiom)
if notebooklm source stale --exit-on-stale src_abc; then
  notebooklm source refresh src_abc
fi

# Default semantics: branch on JSON output instead
if [[ "$(notebooklm source stale src_abc --json | jq -r .stale)" == "true" ]]; then
  notebooklm source refresh src_abc
fi

# Preview what `clean` would remove
notebooklm source clean --dry-run

# Remove duplicates/errors/blocked sources without prompting
notebooklm source clean -y --json
```

---

## Common Workflows

### Research → Podcast

Find information on a topic and create a podcast about it.

```bash
# 1. Create a notebook for this research
notebooklm create "Climate Change Research" --use --json
# Output includes: {"active_notebook_id": "...", "notebook": {...}}

# 2. Add a starting source
notebooklm source add "https://en.wikipedia.org/wiki/Climate_change"

# 3. Research more sources automatically (blocking; --import-all retry budget defaults to 1800s)
notebooklm source add-research "climate change policy 2024" --mode deep --import-all

# 4. Generate a podcast
notebooklm generate audio "Focus on policy solutions and future outlook" --format debate --wait

# 5. Download the result
notebooklm download audio ./climate-podcast.mp3
```

### Research → Podcast (Non-blocking with Subagent)

For LLM agents, use non-blocking mode to avoid timeout:

```bash
# 1. Create notebook and set it active
notebooklm create "Climate Change Research" --use

# 2. Add initial source
notebooklm source add "https://en.wikipedia.org/wiki/Climate_change"

# 3. Start deep research (non-blocking)
notebooklm source add-research "climate change policy 2024" --mode deep --no-wait
# Returns immediately

# 4. In a subagent, wait for research and import
notebooklm research wait --import-all --timeout 300
# Blocks until complete, then imports sources

# 5. Continue with podcast generation...
```

**Research commands:**
- `research status` - Check if research is in progress, completed, or not running
- `research wait --import-all` - Block until research completes, then import sources

### Document Analysis → Study Materials

Upload documents and create study materials.

```bash
# 1. Create notebook
notebooklm create "Exam Prep"
notebooklm use <id>

# 2. Add your documents
notebooklm source add "./textbook-chapter.pdf"
notebooklm source add "./lecture-notes.pdf"

# 3. Get a summary
notebooklm summary

# 4. Generate study materials
notebooklm generate quiz --difficulty hard --wait
notebooklm generate flashcards --wait
notebooklm generate report --format study-guide --wait

# 5. Ask specific questions
notebooklm ask "Explain the key concepts in chapter 3"
notebooklm ask "What are the most likely exam topics?"
```

### YouTube → Quick Summary

Turn a YouTube video into notes.

```bash
# 1. Create notebook and add video
notebooklm create "Video Notes"
notebooklm use <id>
notebooklm source add "https://www.youtube.com/watch?v=VIDEO_ID"

# 2. Get summary
notebooklm summary

# 3. Ask questions
notebooklm ask "What are the main points?"
notebooklm ask "Create bullet point notes"

# 4. Generate a quick briefing doc
notebooklm generate report --format briefing-doc --wait
```

### Bulk Import

Add multiple sources at once.

```bash
# Set active notebook
notebooklm use <id>

# Add multiple URLs
notebooklm source add "https://example.com/article1"
notebooklm source add "https://example.com/article2"
notebooklm source add "https://example.com/article3"

# Add multiple local files (use a loop)
for f in ./papers/*.pdf; do
  notebooklm source add "$f"
done
```

---

### Doctor: `doctor`

Check profile setup, auth status, and migration.

Diagnoses common issues with profiles, authentication, and directory structure. Use `--fix` to automatically repair detected problems.

```bash
notebooklm doctor [OPTIONS]
```

**Options:**
- `--fix` - Attempt to fix detected issues (e.g. missing directories, broken configurations)
- `--json` - Output diagnostic results as a JSON structure for scripting/automation

**Examples:**
```bash
# Check profile and authentication health
notebooklm doctor

# Auto-repair environment issues
notebooklm doctor --fix

# Print diagnostics in machine-readable format
notebooklm doctor --json
```

---

### Agent: `agent show`

Show bundled instructions for supported agent environments.

This command displays tailored instructions for different LLM agents (Codex or Claude Code) to help them understand how to use this CLI programmatically.

```bash
notebooklm agent show [OPTIONS] {codex|claude}
```

**Examples:**
```bash
# Show instructions for Codex
notebooklm agent show codex

# Show instructions for Claude Code
notebooklm agent show claude
```

> **Note:** `agent show codex` prefers the root `AGENTS.md` file when running from a source checkout, so the CLI mirrors the same instructions Codex sees in the repository.

---

## Tips for LLM Agents

When using this CLI programmatically:

1. **Three ways to specify notebooks**: (a) `notebooklm use <id>` to set persistent context, (b) `NOTEBOOKLM_NOTEBOOK=<id>` env var for the calling shell, or (c) `-n <id>` directly on each command. Resolution precedence: `-n/--notebook` flag > `NOTEBOOKLM_NOTEBOOK` env > active context > error.

2. **Generation commands are async by default** (except mind-map):
   - `mind-map`: Synchronous, completes instantly (no `--wait` option)
   - All others: Return immediately with task ID (default: `--no-wait`)

   Avoid `--wait` for LLM agents—all async operations can take minutes to 30+ minutes. Use `artifact wait <id>` in a background task or inform the user to check back later. Long-waits (`generate <kind> --wait`, `artifact wait`, `source wait`) honor SIGINT cleanly: Ctrl-C exits `130` with a `"Resume with: notebooklm artifact poll <task_id>"` hint (or `notebooklm source wait <id>` on the source path) instead of a Python traceback. Under `--json` the cancellation surfaces as `{"error": true, "code": "CANCELLED", "resume_hint": "..."}`.

3. **Partial IDs work**: `notebooklm use abc` matches any notebook ID starting with "abc". The same prefix-match resolves `-n/--notebook`, `-s/--source`, and `-a/--artifact` arguments across the CLI.

4. **Check status**: Use `notebooklm status` to see the current active notebook and conversation.

5. **Auto-detection**: `source add` auto-detects content type:
   - URLs starting with `http` → web source
   - YouTube URLs → video transcript extraction
   - File paths → file upload (PDF, text, Markdown, Word, audio, video, images)
   - Path-shaped strings that don't exist on disk (e.g. `./missin.md`) still ingest as inline text but emit a stderr warning so a typo doesn't silently masquerade as a successful upload. Pass `--type text` to suppress the warning.

6. **Stdin pipelines**: Four surfaces accept the canonical Unix `-` placeholder for "read from stdin": `notebooklm ask -`, `notebooklm ask --prompt-file -`, `notebooklm note create -` (or `--content -`), and `notebooklm source add -` (forces text-source path; bypasses path-shaped detection). Same convention applies to the various `generate <kind> --prompt-file -` flags.

7. **Quiet output for CI/cron**: Pass `--quiet` (root-level) to suppress status output and INFO/WARN log records; errors still surface and structured `--json` payloads are still emitted. `--quiet` is mutually exclusive with `-v/-vv` (combining the two raises `UsageError` with exit `2`). For long-running keepalive loops, `notebooklm auth refresh --quiet` is the subcommand-scoped equivalent.

8. **Error handling**: Commands exit with non-zero status on failure (`1` for user/library errors, `2` for system/unexpected errors per [CLI Exit-Code Convention](cli-exit-codes.md)). With `--json`, failures surface as a typed envelope `{"error": true, "code": "<TYPED_CODE>", "message": "..."}` on stdout; without `--json`, error messages go to stderr.

9. **Deep research**: Use `--no-wait` with `source add-research --mode deep` to avoid blocking. Then use `research wait --import-all` in a subagent to wait for completion.

10. **Shell completion**: `notebooklm completion <bash|zsh|fish>` prints a completion script that enables ID-aware tab completion for `-n/--notebook`, `-s/--source`, and `-a/--artifact` from your active profile's live IDs. See the [`completion`](#session-completion) section for install snippets.
