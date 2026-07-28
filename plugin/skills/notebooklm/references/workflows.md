# Common Workflows

Part of the `notebooklm` skill. Step-by-step recipes for the most common
multi-command tasks, including subagent fire-and-forget patterns for
long-running operations. See [SKILL.md](../SKILL.md) for the "Workflow
scripts" summary and the CLI-not-on-PATH substitution rule.

## Research to Podcast

**Preferred: canned script.** `scripts/research_to_podcast.py` runs the whole
sequence below (create/reuse notebook → research → import sources → generate
audio → download) in one call:

```bash
uv run <skill-dir>/scripts/research_to_podcast.py "renewable energy trends 2024"
uv run <skill-dir>/scripts/research_to_podcast.py "AI safety" --mode deep --max-sources 15 --output podcast.mp3
```

It prints progress to stderr and a single JSON summary line to stdout (exit 0
success / 1 user or auth error / 2 timeout). Use the manual CLI sequences
below when you need to inspect intermediate state, use a non-default source
mix, or the script's assumptions don't fit.

### Manual: Research to Podcast (Interactive)
**Time:** 5-10 minutes total

1. `notebooklm create "Research: [topic]"` — *if fails: check auth with `notebooklm login`*
2. `notebooklm source add` for each URL/document — *if one fails: log warning, continue with others*
3. Wait for sources: `notebooklm source list --json` until all status=READY — *required before generation*
4. `notebooklm generate audio "Focus on [specific angle]"` (confirm when asked) — *if rate limited: wait 5 min, retry once*
5. Note the artifact ID returned
6. Check `notebooklm artifact list` later for status
7. `notebooklm download audio ./podcast.mp3` when complete (confirm when asked)

### Manual: Research to Podcast (Automated with Subagent)
**Time:** 5-10 minutes, but continues in background

When user wants full automation (generate and download when ready) and the
canned script isn't a fit:

1. Create notebook and add sources as usual
2. Wait for sources to be ready (use `source wait` or check `source list --json`)
3. Run `notebooklm generate audio "..." --json` → parse `task_id` from output
4. **Spawn a background agent** using Task tool:
   ```python
   Task(
     prompt="Wait for artifact {task_id} in notebook {notebook_id} to complete, then download.
             Use: notebooklm artifact wait {task_id} -n {notebook_id} --timeout 1200
             Then: notebooklm download audio ./podcast.mp3 -a {task_id} -n {notebook_id}",
     subagent_type="general-purpose"
   )
   ```
5. Main conversation continues while agent waits

**Error handling in subagent:**
- If `artifact wait` returns exit code 2 (timeout): Report timeout, suggest checking `artifact list`
- If download fails: Check if artifact status is COMPLETED first

**Benefits:** Non-blocking, user can do other work, automatic download on completion

## Document Analysis
**Time:** 1-2 minutes

1. `notebooklm create "Analysis: [project]"`
2. `notebooklm source add ./doc.pdf` (or URLs)
3. `notebooklm ask "Summarize the key points"`
4. `notebooklm ask "What are the main arguments?"`
5. Continue chatting as needed

## Bulk Import

**Preferred: canned script.** `scripts/bulk_import.py` adds every URL/file
given (or read from `--from-file`) to a notebook and, by default, waits for
all of them to finish processing:

```bash
uv run <skill-dir>/scripts/bulk_import.py "https://url1.com" "https://url2.com" ./local-file.pdf
uv run <skill-dir>/scripts/bulk_import.py --from-file sources.txt --title "Collection: [name]"
uv run <skill-dir>/scripts/bulk_import.py "https://url1.com" -n <existing_notebook_id> --no-wait
```

It prints progress to stderr and a single JSON summary line to stdout (exit 0
success / 1 user or auth error or all adds failed / 2 timeout waiting for
sources). Use the manual CLI sequences below for finer control (e.g.
per-source label assignment between adds).

### Manual: Bulk Import
**Time:** Varies by source count

1. `notebooklm create "Collection: [name]"`
2. Add multiple sources:
   ```bash
   notebooklm source add "https://url1.com"
   notebooklm source add "https://url2.com"
   notebooklm source add ./local-file.pdf
   ```
3. `notebooklm source list` to verify

**Source limits:** Varies by plan—Standard: 50, Plus: 100, Pro: 300, Ultra: 600 sources per notebook. See [NotebookLM plans](https://support.google.com/notebooklm/answer/16213268) for details. The CLI does not enforce these limits; they are applied by your NotebookLM account.
**Supported types:** PDFs, YouTube URLs, web URLs, Google Docs, text files, Markdown, Word docs, EPUB, audio files, video files, images

### Manual: Bulk Import with Source Waiting (Subagent Pattern)
**Time:** Varies by source count

When adding multiple sources and needing to wait for processing before chat/generation, and the canned script isn't a fit:

1. Add sources with `--json` to capture IDs (parse with `jq -r .source.id`):
   ```bash
   notebooklm source add "https://url1.com" --json  # → {"source": {"id": "abc...", ...}}
   notebooklm source add "https://url2.com" --json  # → {"source": {"id": "def...", ...}}
   ```
2. **Spawn a background agent** to wait for all sources:
   ```
   Task(
     prompt="Wait for sources {source_ids} in notebook {notebook_id} to be ready.
             For each: notebooklm source wait {id} -n {notebook_id} --timeout 600
             Report when all ready or if any fail.",
     subagent_type="general-purpose"
   )
   ```
3. Main conversation continues while agent waits
4. Once sources are ready, proceed with chat or generation

**Why wait for sources?** Sources must be indexed before chat or generation. Takes ~30 seconds to several minutes per source (see the processing-times table below).

## Deep Web Research (Subagent Pattern)
**Time:** 15-30+ minutes, runs in background

Deep research finds and analyzes web sources on a topic:

1. Create notebook: `notebooklm create "Research: [topic]"`
2. Start deep research (non-blocking):
   ```bash
   notebooklm source add-research "topic query" --mode deep --no-wait
   ```
3. **Spawn a background agent** to wait and import:
   ```
   Task(
     prompt="Wait for research in notebook {notebook_id} to complete and import sources.
             Use: notebooklm research wait -n {notebook_id} --import-all --timeout 1800
             Report how many sources were imported.",
     subagent_type="general-purpose"
   )
   ```
4. Main conversation continues while agent waits
5. When agent completes, sources are imported automatically

**Alternative (blocking):** For simple cases, omit `--no-wait`:
```bash
notebooklm source add-research "topic" --mode deep --import-all
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
- Do NOT poll or wait in main conversation - generation takes 5-45 minutes (see timing table below)
- User checks status manually, OR use subagent with `artifact wait`

## Processing Times

**Processing times vary significantly.** Use the subagent pattern (or the
canned scripts, which wait internally) for long operations:

| Operation | Typical time | Suggested timeout |
|-----------|--------------|-------------------|
| Source processing | 30s - 10 min | 600s |
| Research (fast) | 30s - 2 min | 180s |
| Research (deep) | 15 - 30+ min | 1800s |
| Notes | instant | n/a |
| Mind-map | instant (sync) | n/a |
| Quiz, flashcards | 5 - 15 min | 900s |
| Report, data-table | 5 - 15 min | 900s |
| Audio generation | 10 - 20 min | 1200s |
| Video generation | 15 - 45 min | 2700s |
