# CLI Reference

**Status:** Active
**Last Updated:** 2026-01-10

Complete command reference for the `notebooklm` CLI.

## Command Structure

```
notebooklm [--storage PATH] [--version] <command> [OPTIONS] [ARGS]
```

**Global Options:**
- `--storage PATH` - Override the default storage location (`~/.notebooklm/storage_state.json`)
- `--version` - Show version and exit
- `--help` - Show help message

**Environment Variables:**
- `NOTEBOOKLM_HOME` - Base directory for all config files (default: `~/.notebooklm`)
- `NOTEBOOKLM_AUTH_JSON` - Inline authentication JSON (for CI/CD, no file writes needed)
- `NOTEBOOKLM_DEBUG_RPC` - Enable RPC debug logging (`1` to enable)

See [Configuration](configuration.md) for details on environment variables and CI/CD setup.

**Command Organization:**
- **Session commands** - Authentication and context management
- **Notebook commands** - CRUD operations on notebooks
- **Chat commands** - Querying and conversation management
- **Grouped commands** - `source`, `artifact`, `generate`, `download`, `note`

---

## Quick Reference

### Session Commands

| Command | Description | Example |
|---------|-------------|---------|
| `login` | Authenticate via browser | `notebooklm login` |
| `use <id>` | Set active notebook | `notebooklm use abc123` |
| `status` | Show current context | `notebooklm status` |
| `status --paths` | Show configuration paths | `notebooklm status --paths` |
| `status --json` | Output status as JSON | `notebooklm status --json` |
| `clear` | Clear current context | `notebooklm clear` |

### Notebook Commands

| Command | Description | Example |
|---------|-------------|---------|
| `list` | List all notebooks | `notebooklm list` |
| `create <title>` | Create notebook | `notebooklm create "Research"` |
| `delete <id>` | Delete notebook | `notebooklm delete abc123` |
| `rename <title>` | Rename current notebook | `notebooklm rename "New Title"` |
| `share` | Toggle notebook sharing | `notebooklm share` or `notebooklm share --revoke` |
| `summary` | Get AI summary | `notebooklm summary` |

### Chat Commands

| Command | Description | Example |
|---------|-------------|---------|
| `ask <question>` | Ask a question | `notebooklm ask "What is this about?"` |
| `ask -s <id>` | Ask using specific sources | `notebooklm ask "Summarize" -s src1 -s src2` |
| `ask --json` | Get answer with source references | `notebooklm ask "Explain X" --json` |
| `configure` | Set persona/mode | `notebooklm configure --mode learning-guide` |
| `history` | View/clear history | `notebooklm history --clear` |

### Source Commands (`notebooklm source <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | - | `source list` |
| `add <content>` | URL/file/text | - | `source add "https://..."` |
| `add-drive <id> <title>` | Drive file ID | - | `source add-drive abc123 "Doc"` |
| `add-research <query>` | Search query | `--mode [fast|deep]`, `--from [web|drive]`, `--import-all`, `--no-wait` | `source add-research "AI" --mode deep --no-wait` |
| `get <id>` | Source ID | - | `source get src123` |
| `fulltext <id>` | Source ID | `--json`, `-o FILE` | `source fulltext src123 -o content.txt` |
| `guide <id>` | Source ID | `--json` | `source guide src123` |
| `rename <id> <title>` | Source ID, new title | - | `source rename src123 "New Name"` |
| `refresh <id>` | Source ID | - | `source refresh src123` |
| `delete <id>` | Source ID | - | `source delete src123` |
| `wait <id>` | Source ID | `--timeout`, `--interval` | `source wait src123` |

### Research Commands (`notebooklm research <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `status` | - | `--json` | `research status` |
| `wait` | - | `--timeout`, `--interval`, `--import-all`, `--json` | `research wait --import-all` |

### Generate Commands (`notebooklm generate <type>`)

All generate commands support:
- `--source/-s` to select specific sources (repeatable)
- `--json` for machine-readable output (returns `task_id` and `status`)

| Command | Options | Example |
|---------|---------|---------|
| `audio [description]` | `--format`, `--length`, `-s/--source`, `--wait`, `--json` | `generate audio "Focus on history"` |
| `video [description]` | `--style`, `--format`, `-s/--source`, `--wait`, `--json` | `generate video "Explainer for kids"` |
| `slide-deck [description]` | `--format`, `--length`, `-s/--source`, `--wait`, `--json` | `generate slide-deck` |
| `quiz [description]` | `--difficulty`, `--quantity`, `-s/--source`, `--wait`, `--json` | `generate quiz --difficulty hard` |
| `flashcards [description]` | `--difficulty`, `--quantity`, `-s/--source`, `--wait`, `--json` | `generate flashcards` |
| `infographic [description]` | `--orientation`, `--detail`, `-s/--source`, `--wait`, `--json` | `generate infographic` |
| `data-table [description]` | `-s/--source`, `--wait`, `--json` | `generate data-table` |
| `mind-map` | `-s/--source`, `--json` *(sync, no wait needed)* | `generate mind-map` |
| `report [description]` | `--type`, `-s/--source`, `--wait`, `--json` | `generate report --type study-guide` |

### Artifact Commands (`notebooklm artifact <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | `--type` | `artifact list --type audio` |
| `get <id>` | Artifact ID | - | `artifact get art123` |
| `rename <id> <title>` | Artifact ID, title | - | `artifact rename art123 "Title"` |
| `delete <id>` | Artifact ID | - | `artifact delete art123` |
| `export <id>` | Artifact ID | `--type [docs|sheets]`, `--title` | `artifact export art123 --type sheets` |
| `poll <task_id>` | Task ID | - | `artifact poll task123` |
| `wait <id>` | Artifact ID | `--timeout`, `--interval` | `artifact wait art123` |
| `suggestions` | - | `-s/--source`, `--json` | `artifact suggestions` |

### Download Commands (`notebooklm download <type>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `audio [path]` | Output path | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download audio --all` |
| `video [path]` | Output path | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download video --latest` |
| `slide-deck [path]` | Output directory | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download slide-deck ./slides/` |
| `infographic [path]` | Output path | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download infographic ./info.png` |
| `report [path]` | Output path | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download report ./report.md` |
| `mind-map [path]` | Output path | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download mind-map ./map.json` |
| `data-table [path]` | Output path | `-a/--artifact`, `--all`, `--latest`, `--name`, `--force`, `--dry-run` | `download data-table ./data.csv` |

### Note Commands (`notebooklm note <cmd>`)

| Command | Arguments | Options | Example |
|---------|-----------|---------|---------|
| `list` | - | - | `note list` |
| `create <content>` | Note content | - | `note create "My notes..."` |
| `get <id>` | Note ID | - | `note get note123` |
| `save <id>` | Note ID | - | `note save note123` |
| `rename <id> <title>` | Note ID, title | - | `note rename note123 "Title"` |
| `delete <id>` | Note ID | - | `note delete note123` |

### Skill Commands (`notebooklm skill <cmd>`)

Manage Claude Code skill integration.

| Command | Description | Example |
|---------|-------------|---------|
| `install` | Install/update skill to ~/.claude/skills/ | `skill install` |
| `status` | Check installation and version | `skill status` |
| `uninstall` | Remove skill | `skill uninstall` |
| `show` | Display skill content | `skill show` |

After installation, Claude Code recognizes NotebookLM commands via `/notebooklm` or natural language like "create a podcast about X".

---

## Detailed Command Reference

### Session: `login`

Authenticate with Google NotebookLM via browser.

```bash
notebooklm login
```

Opens a Chromium browser with a persistent profile. Log in to your Google account, then press Enter in the terminal to save the session.

### Session: `use`

Set the active notebook for subsequent commands.

```bash
notebooklm use <notebook_id>
```

Supports partial ID matching:
```bash
notebooklm use abc  # Matches abc123def456...
```

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

### Source: `add-research`

Perform AI-powered research and add discovered sources to the notebook.

```bash
notebooklm source add-research <query> [OPTIONS]
```

**Options:**
- `--mode [fast|deep]` - Research depth (default: fast)
- `--from [web|drive]` - Search source (default: web)
- `--import-all` - Automatically import all found sources (works with blocking mode)
- `--no-wait` - Start research and return immediately (non-blocking)

**Examples:**
```bash
# Fast web research (blocking)
notebooklm source add-research "Quantum computing basics"

# Deep research into Google Drive
notebooklm source add-research "Project Alpha" --from drive --mode deep

# Non-blocking deep research for agent workflows
notebooklm source add-research "AI safety papers" --mode deep --no-wait
```

### Research: `status`

Check research status for the current notebook (non-blocking).

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

```bash
notebooklm research wait [OPTIONS]
```

**Options:**
- `-n, --notebook ID` - Notebook ID (uses current if not set)
- `--timeout SECONDS` - Maximum seconds to wait (default: 300)
- `--interval SECONDS` - Seconds between status checks (default: 5)
- `--import-all` - Import all found sources when done
- `--json` - Output as JSON

**Examples:**
```bash
# Basic wait
notebooklm research wait

# Wait longer for deep research
notebooklm research wait --timeout 600

# Wait and auto-import sources
notebooklm research wait --import-all

# JSON output for agent workflows
notebooklm research wait --json --import-all
```

**Use case:** Primarily for LLM agents that need to wait for non-blocking deep research started with `source add-research --no-wait`.

### Generate: `audio`

Generate an audio overview (podcast).

```bash
notebooklm generate audio [description] [OPTIONS]
```

**Options:**
- `--format [deep-dive|brief|critique|debate]` - Podcast format (default: deep-dive)
- `--length [short|default|long]` - Duration (default: default)
- `--language LANG` - Language code (default: en)
- `-s, --source ID` - Use specific source(s) (repeatable, uses all if not specified)
- `--wait` - Wait for generation to complete
- `--json` - Output as JSON (returns `task_id` and `status`)

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
```

### Generate: `video`

Generate a video overview.

```bash
notebooklm generate video [description] [OPTIONS]
```

**Options:**
- `--format [explainer|brief]` - Video format
- `--style [auto|classic|whiteboard|kawaii|anime|watercolor|retro|heritage|paper-craft]` - Visual style
- `--language LANG` - Language code
- `-s, --source ID` - Use specific source(s) (repeatable, uses all if not specified)
- `--wait` - Wait for generation to complete
- `--json` - Output as JSON (returns `task_id` and `status`)

**Examples:**
```bash
# Kid-friendly explainer
notebooklm generate video "Explain for 5 year olds" --style kawaii

# Professional style
notebooklm generate video --style classic --wait

# Generate from specific sources only
notebooklm generate video -s src_123 -s src_456

# JSON output for scripting/automation
notebooklm generate video --json
```

### Generate: `report`

Generate a text report (briefing doc, study guide, blog post, or custom).

```bash
notebooklm generate report [description] [OPTIONS]
```

**Options:**
- `--type [briefing-doc|study-guide|blog-post|custom]` - Report type
- `-s, --source ID` - Use specific source(s) (repeatable, uses all if not specified)

**Examples:**
```bash
notebooklm generate report --type study-guide
notebooklm generate report "Executive summary for stakeholders" --type briefing-doc

# Generate report from specific sources
notebooklm generate report --type study-guide -s src_001 -s src_002
```

### Download: `audio`, `video`, `slide-deck`, `infographic`, `report`, `mind-map`, `data-table`

Download generated artifacts to your local machine.

```bash
notebooklm download <type> [OUTPUT_PATH] [OPTIONS]
```

**Artifact Types and Output Formats:**

| Type | Default Extension | Description |
|------|-------------------|-------------|
| `audio` | `.mp4` | Audio overview (podcast) in MP4 container |
| `video` | `.mp4` | Video overview |
| `slide-deck` | `.pdf` | Slide deck as PDF |
| `infographic` | `.png` | Infographic image |
| `report` | `.md` | Report as Markdown (Briefing Doc, Study Guide, etc.) |
| `mind-map` | `.json` | Mind map as JSON tree structure |
| `data-table` | `.csv` | Data table as CSV (UTF-8 with BOM for Excel) |

**Options:**
- `--all` - Download all artifacts of this type
- `--latest` - Download only the most recent artifact (default if no ID/name provided)
- `--earliest` - Download only the oldest artifact
- `--name NAME` - Download artifact with matching title (supports partial matches)
- `-a, --artifact ID` - Select specific artifact by ID
- `--dry-run` - Show what would be downloaded without actually downloading
- `--force` - Overwrite existing files
- `--no-clobber` - Skip if file already exists (default)
- `--json` - Output result in JSON format

**Examples:**
```bash
# Download the latest podcast
notebooklm download audio ./podcast.mp3

# Download all infographics
notebooklm download infographic --all

# Download a specific slide deck by name
notebooklm download slide-deck --name "Final Presentation"

# Preview a batch download
notebooklm download audio --all --dry-run

# Download a report as markdown
notebooklm download report ./study-guide.md

# Download mind map as JSON
notebooklm download mind-map ./concept-map.json

# Download data table as CSV (opens in Excel)
notebooklm download data-table ./research-data.csv
```

---

## Common Workflows

### Research → Podcast

Find information on a topic and create a podcast about it.

```bash
# 1. Create a notebook for this research
notebooklm create "Climate Change Research"
# Output: Created notebook: abc123

# 2. Set as active
notebooklm use abc123

# 3. Add a starting source
notebooklm source add "https://en.wikipedia.org/wiki/Climate_change"

# 4. Research more sources automatically (blocking - waits up to 5 min)
notebooklm source add-research "climate change policy 2024" --mode deep --import-all

# 5. Generate a podcast
notebooklm generate audio "Focus on policy solutions and future outlook" --format debate --wait

# 6. Download the result
notebooklm download audio ./climate-podcast.mp3
```

### Research → Podcast (Non-blocking with Subagent)

For LLM agents, use non-blocking mode to avoid timeout:

```bash
# 1-3. Create notebook and add initial source (same as above)
notebooklm create "Climate Change Research"
notebooklm use abc123
notebooklm source add "https://en.wikipedia.org/wiki/Climate_change"

# 4. Start deep research (non-blocking)
notebooklm source add-research "climate change policy 2024" --mode deep --no-wait
# Returns immediately

# 5. In a subagent, wait for research and import
notebooklm research wait --import-all --timeout 300
# Blocks until complete, then imports sources

# 6. Continue with podcast generation...
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
notebooklm generate report --type study-guide --wait

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
notebooklm generate report --type briefing-doc --wait
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

## Tips for LLM Agents

When using this CLI programmatically:

1. **Two ways to specify notebooks**: Either use `notebooklm use <id>` to set context, OR pass `-n <id>` directly to commands. Most commands support `-n/--notebook` as an explicit override.

2. **Generation commands are async by default** (except mind-map):
   - `mind-map`: Synchronous, completes instantly (no `--wait` option)
   - All others: Return immediately with task ID (default: `--no-wait`)

   Avoid `--wait` for LLM agents—all async operations can take minutes to 30+ minutes. Use `artifact wait <id>` in a background task or inform the user to check back later.

3. **Partial IDs work**: `notebooklm use abc` matches any notebook ID starting with "abc".

4. **Check status**: Use `notebooklm status` to see the current active notebook and conversation.

5. **Auto-detection**: `source add` auto-detects content type:
   - URLs starting with `http` → web source
   - YouTube URLs → video transcript extraction
   - File paths → file upload

6. **Error handling**: Commands exit with non-zero status on failure. Check stderr for error messages.

7. **Deep research**: Use `--no-wait` with `source add-research --mode deep` to avoid blocking. Then use `research wait --import-all` in a subagent to wait for completion.
