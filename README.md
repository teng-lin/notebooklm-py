# notebooklm-py
<p align="left">
  <img src="https://raw.githubusercontent.com/teng-lin/notebooklm-py/main/notebooklm-py.png" alt="notebooklm-py logo" width="128">
</p>

**A Comprehensive NotebookLM Skill & Unofficial Python API.** Full programmatic access to NotebookLM's features—including capabilities the web UI doesn't expose—via Python, CLI, and AI agents like Claude Code, Codex, and OpenClaw.

[![PyPI version](https://img.shields.io/pypi/v/notebooklm-py.svg)](https://pypi.org/project/notebooklm-py/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/notebooklm-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/teng-lin/notebooklm-py/actions/workflows/test.yml/badge.svg)](https://github.com/teng-lin/notebooklm-py/actions/workflows/test.yml)
<p>
  <a href="https://trendshift.io/repositories/19116" target="_blank"><img src="https://trendshift.io/api/badge/repositories/19116" alt="teng-lin%2Fnotebooklm-py | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
</p>

**Source & Development**: <https://github.com/teng-lin/notebooklm-py>

> **⚠️ Unofficial Library - Use at Your Own Risk**
>
> This library uses **undocumented Google APIs** that can change without notice.
>
> - **Not affiliated with Google** - This is a community project
> - **APIs may break** - Google can change internal endpoints anytime
> - **Rate limits apply** - Heavy usage may be throttled
>
> Best for prototypes, research, and personal projects. See [Troubleshooting](docs/troubleshooting.md) for debugging tips.

## What You Can Build

🤖 **AI Agent Tools** - Integrate NotebookLM into Claude Code, Codex, and other LLM agents. Ships with a root [NotebookLM skill](SKILL.md) for GitHub and `npx skills add` discovery, local `notebooklm skill install` support for Claude Code and `.agents` skill directories, and repo-level Codex guidance in [`AGENTS.md`](AGENTS.md).

📚 **Research Automation** - Bulk-import sources (URLs, PDFs, YouTube, Google Drive), run web/Drive research queries with auto-import, and extract insights programmatically. Build repeatable research pipelines.

🎙️ **Content Generation** - Generate Audio Overviews (podcasts), videos, slide decks, quizzes, flashcards, infographics, data tables, mind maps, and study guides. Full control over formats, styles, and output.

📥 **Downloads & Export** - Download all generated artifacts locally (MP3, MP4, PDF, PNG, CSV, JSON, Markdown). Export to Google Docs/Sheets. **Features the web UI doesn't offer**: batch downloads, quiz/flashcard export in multiple formats, mind map JSON extraction.

## Use Cases & Recipes

NotebookLM is a **grounded** engine: Gemini does the heavy reading and answers from *your* sources with citations. The winning pattern is to let it do the expensive analysis while your agent (Claude Code, Codex, …) orchestrates and handles the final mile — using NotebookLM as a **zero-token synthesis + memory layer an agent drives in a loop**, and pulling structured artifacts **out** in bulk and in richer, scriptable formats. Recipes people build on top of this library, grouped by what they use NotebookLM *as*:

**Spend fewer tokens** — let NotebookLM do the expensive thinking:

- **🪙 Zero-token research offload** — Throw 30 documents into a notebook, let Gemini do the heavy analysis, and have your agent spend tokens only on the final polish. The agent just orchestrates (`create` → `source add` → `ask`); the reasoning happens server-side. *In the wild: [a four-workflow guide to stop Claude Code burning tokens on NotebookLM](https://x.com/hooeem/status/2042293751805329445).*
- **🧠 Knowledge distillation → a permanent skill** — Run [Deep Research](docs/cli-reference.md#source-add-research) (`source add-research "your topic" --mode deep`) or load a doc corpus, let NotebookLM's Gemini condense it, and bake the result into a `SKILL.md` your agent loads at startup — **build once, reuse with zero runtime tokens or network calls**, git-versioned and immune to UI drift. A packaged domain expert without hand-curating sources. (Dumping raw docs into a skill flattens the hierarchy; NotebookLM condensing first is what makes it work.)
- **✅ Self-validating skills** — Have NotebookLM generate the *eval set* — a quiz straight from your sources — to grade an agent skill against ground truth instead of test questions you'd bias yourself. Build the skill, run it against the NotebookLM-authored evals, iterate to a pass. *In the wild: [a skill that scored 4/10 on the first pass and 10/10 after one iteration, graded by a NotebookLM-generated quiz](https://x.com/nurijanian/status/2037136490157986277).*

**Give your agent memory** — persistent, grounded recall:

- **💾 Persistent cross-session memory** — Keep a "Master Brain" notebook; a wrap-up step appends each session's decisions and fixes as notes (`note create` / `ask --save-as-note`), and a line in your `CLAUDE.md` queries it (`ask`) at the start of the next session. Storage and recall live on Google's infrastructure.
- **🧩 Grounded memory for coding agents** — Expose a notebook of your internal docs/RFCs/architecture over the [MCP server](docs/mcp-guide.md) (or plain `ask`) so an agent answers from *your* code with citations rather than plausible-sounding guesses — a zero-infra alternative to standing up your own vector DB and embedding pipeline. *In the wild: [turning a notebook into the source-grounded "project brain" a coding agent consults before it writes code](https://medium.com/@pradeep00271/every-software-project-needs-a-project-brain-5cbc33917160).*
- **🪞 Query your own notes / journal** — Load years of daily notes, meeting logs, or a journal and `ask` for **cited** answers *across your own history* — surfacing long-term patterns a keyword search can't (e.g. a weekly summary synthesized from 282 daily notes, every claim linked back to the entry it came from). *In the wild: [chatting with a year of daily notes as a cited knowledge base](https://artemxtech.substack.com/p/notebooklm-has-a-knowledge-graph).*

**Turn your sources into answers & artifacts** — cited responses, generated media, and exports:

- **📞 Grounded knowledge base / troubleshooting oracle (RAG)** — Load product docs, FAQs, RFCs, and past tickets, then `ask --json` for **source-grounded, cited** answers for support, on-call, or internal Q&A. Or have an agent point it at an entire fast-moving tool's docs — more than the agent can hold in context — as a **troubleshooting oracle** it queries the moment it hits an error. *In the wild: [OpenClaw drove the library to scrape all 524 pages of `docs.openclaw.ai`, dedupe the duplicate translations, and audit it down to 269 clean sources (missing/extra/duplicate = 0)](https://x.com/onenewbite/status/2024819940327379286).*
- **🔁 Multi-format content repurposing** — One source set, every format: `generate audio` (podcast), `generate video`, `generate slide-deck`, plus a `generate report` blog draft, `generate quiz`, and `generate flashcards` — fan a single notebook out across channels.
- **📤 Bulk, scriptable exports** — Pull mind maps as JSON, flashcards/quizzes as JSON/Markdown/HTML, data tables as CSV, and reports as Markdown — **in bulk, to local files, straight into Anki, your mind-mapping tool, or a repo** (`download <type>` / `download <type> --all`). The programmatic "get data *out*" half of the library, not just "put sources in."
- **🕸️ Obsidian / knowledge-graph sync** — Run the CLI from your vault root so downloaded artifacts (reports, mind-map JSON, transcripts) land as files in your knowledge graph; community skills built on this library even resolve NotebookLM's citation markers into Obsidian `[[wikilinks]]`. Pair with a podcast overview for an audio digest of your notes. *In the wild: ["Claude Code + NotebookLM + Obsidian = GOD MODE"](https://www.youtube.com/watch?v=kU3qYQ7ACMA).*

**Run it unattended, at scale, or on the go** — scheduled, headless, and remote:

- **🚨 Incident runbook generator** — On an alert, spin up a notebook of the relevant docs, ask targeted diagnostic questions, and generate a briefing-doc report (`generate report --format briefing-doc --wait`, then `download report`) as an automated runbook.
- **📚 Curriculum / study-set builder** — Scrape a syllabus or developer roadmap, create one notebook per topic (with deliberate pacing to dodge rate limits), and bulk-generate podcasts, quizzes, and flashcards for each.
- **📰 Scheduled audio briefings** — Pair `auth refresh --quiet` (cron/launchd/systemd) with `generate audio` to publish a fresh personalized briefing to a podcast feed on a schedule.
- **📱 NotebookLM from your phone, agent-driven** — Self-host the [remote MCP connector](docs/mcp-guide.md#remote-deployment-docker--a-tunnel) behind a Cloudflare/Tailscale tunnel and add it as a custom connector **on the web** (claude.ai Connectors, or ChatGPT with Developer Mode). Then drive the full toolset — deep research, source ingestion, studio generation, cited Q&A — from the **claude.ai mobile app** on the go (ChatGPT's MCP connectors are web-only), chained with your other MCP tools, instead of app-hopping.

These combine ordinary library primitives — see the [CLI Reference](docs/cli-reference.md) and [Python API](docs/python-api.md). The agent-side glue (skills, scheduling, vault layout) lives in your own setup, not this package. Per-notebook source counts depend on your Google account tier — split across notebooks if you hit a cap.

**New here?** Start with a walkthrough: [Claude Code + NotebookLM = CHEAT CODE (video)](https://www.youtube.com/watch?v=usTeU4Uh0iM) · [5 demos + 50 use cases, with prompts](https://aiblewmymind.substack.com/p/notebooklm-claude-code-use-cases).

## Ways to Use

| Method | Best For |
|--------|----------|
| **Python API** | Application integration, async workflows, custom pipelines |
| **CLI** | Shell scripts, quick tasks, CI/CD automation |
| **MCP Server** | Claude Desktop/Code, Codex, etc. — locally via stdio, or as a self-hosted remote connector (behind a Cloudflare/Tailscale tunnel) reachable from claude.ai and ChatGPT, mobile included. |
| **REST Server** | Local automation over guarded HTTP routes without spawning a CLI process per call |
| **Agent Integration** | Claude Code, Codex, LLM agents, natural language automation |

## Features

### Complete NotebookLM Coverage

| Category | Capabilities |
|----------|--------------|
| **Notebooks** | Create, list, rename, delete |
| **Sources** | URLs, YouTube, files (PDF, text, Markdown, Word, EPUB, audio, video, images), Google Drive, pasted text; refresh, get guide/fulltext |
| **Chat** | Questions, conversation history, custom personas, suggested starter prompts |
| **Notes** | Create, list, rename, delete, save chat answers, save conversation history |
| **Source Labels** | AI-generated or manual topic labels; add/remove source membership; filter sources by label |
| **Research** | Web and Drive research agents (fast/deep modes) with auto-import |
| **Sharing** | Public/private links, user permissions (viewer/editor), view level control |

### Content Generation (All Artifact Types)

| Type | Options | Download Format |
|------|---------|-----------------|
| **Audio Overview** | 4 formats (deep-dive, brief, critique, debate), 3 lengths, 50+ languages | MP3 |
| **Video Overview** | 4 formats (explainer, brief, cinematic, short), 8 visual styles (+ auto/custom), plus a dedicated `cinematic-video` CLI alias | MP4 |
| **Slide Deck** | Detailed or presenter format, adjustable length; individual slide revision | PDF, PPTX |
| **Infographic** | 3 orientations, 3 detail levels | PNG |
| **Quiz** | Configurable quantity and difficulty | JSON, Markdown, HTML |
| **Flashcards** | Configurable quantity and difficulty | JSON, Markdown, HTML |
| **Report** | Briefing doc, study guide, blog post, or custom prompt | Markdown |
| **Data Table** | Custom structure via natural language | CSV |
| **Mind Map** | Hierarchical node tree — **two kinds**: note-backed JSON or the newer interactive studio map (`--kind` / `MindMapKind`) | JSON |

### Beyond the Web UI

Programmatic, batch, and local-file capabilities the API/CLI make easy — several in richer formats, or at a scale, than clicking through the web app:

- **Batch downloads** - Download all artifacts of a type at once
- **Quiz/Flashcard export** - Get structured JSON, Markdown, or HTML files
- **Mind map data extraction** - Export hierarchical JSON for visualization tools
- **Data table CSV export** - Download structured tables as spreadsheets
- **Slide deck as PPTX or PDF** - Download editable PowerPoint or PDF files
- **Slide revision** - Modify individual slides with natural-language prompts
- **Report template customization** - Append extra instructions to built-in format templates
- **Save chat history to notes** - Persist a whole Q&A conversation (not just a single answer) as a notebook note
- **Source fulltext access** - Retrieve the indexed text content of any source
- **Programmatic sharing** - Manage permissions without the UI

## Installation

The full install guide — six personas (agent, end-user, library, headless, contributor, power-user), optional extras matrix, platform notes — lives in **[docs/installation.md](docs/installation.md)**.

**Quickest start** (CLI users and AI agents) — install the CLI with `uv tool` (recommended) or `pipx`:

```bash
uv tool install "notebooklm-py[browser]"   # or: pipx install "notebooklm-py[browser]"
notebooklm login                           # first run auto-downloads Chromium (~170 MB), then Google sign-in
notebooklm auth check --test --json        # verify: expect "status": "ok"
```

**Why `uv tool` / `pipx`?** They install the CLI into its own isolated environment and put `notebooklm` on your `PATH` — no dependency clashes with other tools, a one-line upgrade (`uv tool upgrade notebooklm-py`) or uninstall, and, crucially, they work on modern macOS (Homebrew Python) and Debian/Ubuntu where a system-wide `pip install` is blocked with `error: externally-managed-environment` ([PEP 668](https://peps.python.org/pep-0668/)). No `uv` yet? `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv` / `winget install astral-sh.uv`).

**Prefer plain `pip`?** It works the same **inside a virtualenv** (and directly on Windows, where Python isn't externally-managed):

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install "notebooklm-py[browser]"
```

**As a library** (embedded in your app — no Playwright, no Chromium):

```bash
uv add notebooklm-py                    # or, inside a virtualenv: pip install notebooklm-py
```

If `playwright install chromium` fails on Linux with `TypeError: onExit is not a function`, see the [Linux workaround](docs/troubleshooting.md#linux). **Contributors:** see [CONTRIBUTING.md](CONTRIBUTING.md).


### Authentication & Access

Flexible auth for local dev, headless servers, and multi-tenant setups:

- **Three ways to get cookies** - Interactive Playwright login (default), import from an already-signed-in browser (`login --browser-cookies chrome`, no Playwright), or a durable **master token**.
- **Master-token auth** - Mints fresh web cookies **on demand** with no per-session browser (`login --master-token --account you@example.com`), so it self-heals expired sessions unattended — the auth model for servers, CI, and the remote MCP connector (claude.ai / ChatGPT).
- **Multi-account profiles** - Switch between Google accounts without re-authenticating.

### Agent Setup

**Option 1 — CLI install**:

```bash
notebooklm skill install
```

Installs the skill into `~/.claude/skills/notebooklm` and `~/.agents/skills/notebooklm`.

**Option 2 — `npx` install** (via the open skills ecosystem):

```bash
npx skills add teng-lin/notebooklm-py
```

Fetches the canonical [SKILL.md](SKILL.md) directly from GitHub.

## Quick Start

<p align="center">
  <a href="https://asciinema.org/a/767284" target="_blank"><img src="https://asciinema.org/a/767284.svg" width="600" /></a>
  <br>
  <em>16-minute session compressed to 30 seconds</em>
</p>

### CLI

```bash
# 1. Authenticate (opens browser)
notebooklm login
# Or use Microsoft Edge (for orgs that require Edge for SSO)
# notebooklm login --browser msedge
# Or reuse cookies from an already-logged-in browser session
# notebooklm login --browser-cookies chrome
# notebooklm login --browser-cookies 'chrome::Profile 1'  # one Chromium profile
# (combine with --profile to populate a specific profile;
#  use --account / --all-accounts after auth inspect when several
#  Google accounts are signed in)

# 2. Create a notebook and add sources
notebooklm create "My Research"
notebooklm use <notebook_id>
notebooklm source add "https://en.wikipedia.org/wiki/Artificial_intelligence"
notebooklm source add "./paper.pdf"

# 3. Chat with your sources
notebooklm ask "What are the key themes?"
notebooklm ask --prompt-file ./long_question.txt  # Read question from file

# 4. Generate content (use --prompt-file for long prompts)
notebooklm generate audio "make it engaging" --wait
notebooklm generate video --style whiteboard --wait
notebooklm generate cinematic-video "documentary-style summary" --wait
notebooklm generate quiz --difficulty hard
notebooklm generate flashcards --quantity more
notebooklm generate slide-deck
notebooklm generate infographic --orientation portrait
notebooklm generate mind-map                       # interactive studio map (default); --kind note-backed for the JSON tree
notebooklm generate data-table "compare key concepts"

# 5. Download artifacts
notebooklm download audio ./podcast.mp3
notebooklm download video ./overview.mp4
notebooklm download cinematic-video ./documentary.mp4
notebooklm download quiz --format markdown ./quiz.md
notebooklm download flashcards --format json ./cards.json
notebooklm download slide-deck ./slides.pdf
notebooklm download infographic ./infographic.png
notebooklm download mind-map ./mindmap.json
notebooklm download data-table ./data.csv
```

Other useful CLI commands:

```bash
notebooklm auth check --test         # Diagnose auth/cookie issues
notebooklm auth refresh --quiet      # One-shot cookie keepalive (for cron / launchd / systemd)
notebooklm auth refresh --browser-cookies chrome  # Re-extract and repair account routing
notebooklm auth inspect --browser 'chrome::Profile 1'  # Preview one Chromium profile
notebooklm agent show codex          # Print bundled Codex instructions
notebooklm agent show claude         # Print bundled Claude Code skill template
notebooklm language list             # List supported output languages
notebooklm metadata --json           # Export notebook metadata and sources
notebooklm share status              # Inspect sharing state
notebooklm source add-research "AI" --import-all  # web research + import found sources
notebooklm skill status              # Check local agent skill installation
notebooklm profile list              # List all Google account profiles
notebooklm profile switch work       # Switch active account profile
```

Use `--prompt-file PATH` with `ask`, prompt-based `generate` commands, and `source add-research` when the text is too long for the shell command line. This reads prompt/query text from a file and is separate from `source add ./file.pdf`, which still uploads that file as a NotebookLM source.

### Python API

```python
import asyncio
from notebooklm import NotebookLMClient, MindMapKind

async def main():
    async with NotebookLMClient.from_storage() as client:
        # Create notebook and add sources
        nb = await client.notebooks.create("Research")
        await client.sources.add_url(nb.id, "https://example.com", wait=True)

        # Chat with your sources
        result = await client.chat.ask(nb.id, "Summarize this")
        print(result.answer)

        # Generate content (podcast, video, quiz, etc.)
        status = await client.artifacts.generate_audio(nb.id, instructions="make it fun")
        await client.artifacts.wait_for_completion(nb.id, status.task_id)
        await client.artifacts.download_audio(nb.id, "podcast.mp3")

        # Generate quiz and download as JSON
        status = await client.artifacts.generate_quiz(nb.id)
        await client.artifacts.wait_for_completion(nb.id, status.task_id)
        await client.artifacts.download_quiz(nb.id, "quiz.json", output_format="json")

        # Generate a mind map via the unified client.mind_maps API (issue #1256) —
        # two kinds: the newer MindMapKind.INTERACTIVE studio map (shown; polled to
        # completion by default) or MindMapKind.NOTE_BACKED JSON. Both export via:
        mm = await client.mind_maps.generate(nb.id, kind=MindMapKind.INTERACTIVE)
        await client.artifacts.download_mind_map(nb.id, "mindmap.json", mm.id)

asyncio.run(main())
```

## Documentation

- **[CLI Reference](docs/cli-reference.md)** - Complete command documentation
- **[Python API](docs/python-api.md)** - Full API reference
- **[MCP Guide](docs/mcp-guide.md)** - MCP server setup, transports, and tool reference
- **[REST API Server](docs/installation.md#rest-api-server)** - Experimental localhost FastAPI server
- **[Configuration](docs/configuration.md)** - Storage and settings
- **[Release Guide](docs/releasing.md)** - Release checklist and packaging verification
- **[Troubleshooting](docs/troubleshooting.md)** - Common issues and solutions
- **[API Stability](docs/stability.md)** - Versioning policy and stability guarantees
- **[Upgrading to v0.8.0](docs/upgrading-to-0.8.0.md)** - Breaking-change migration guide for the v0.8.0 error-and-return contract

### For Contributors

- **[Architecture](docs/architecture.md)** - Architectural overview and design principles
- **[Development Guide](docs/development.md)** - Architecture, testing, and releasing
- **[RPC Development](docs/rpc-development.md)** - Protocol capture and debugging
- **[RPC Reference](docs/rpc-reference.md)** - Payload structures
- **[Changelog](CHANGELOG.md)** - Version history and release notes
- **[Security](SECURITY.md)** - Security policy and credential handling

## Star History

[![Star History Chart](https://api.star-history.com/image?repos=teng-lin/notebooklm-py&type=timeline&legend=top-left)](https://www.star-history.com/?repos=teng-lin%2Fnotebooklm-py&type=timeline&legend=top-left)

## License

MIT License. See [LICENSE](LICENSE) for details.
