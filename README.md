# notebooklm-py
<p align="left">
  <img src="https://raw.githubusercontent.com/teng-lin/notebooklm-py/main/notebooklm-py.png" alt="notebooklm-py logo" width="128">
</p>

**The missing API for Google NotebookLM.** Automate research workflows, generate podcasts from your documents, and integrate NotebookLM into AI agents—all from Python or the command line.

[![PyPI version](https://img.shields.io/pypi/v/notebooklm-py.svg)](https://pypi.org/project/notebooklm-py/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/notebooklm-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/teng-lin/notebooklm-py/actions/workflows/test.yml/badge.svg)](https://github.com/teng-lin/notebooklm-py/actions/workflows/test.yml)

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

🤖 **AI Agent Tools** - Integrate NotebookLM into Claude Code, or other LLM agents. Ships with [Claude Code skills](#agent-skills-claude-code) for natural language automation (`notebooklm skill install`), or build your own integrations with the async Python API.

📚 **Research Automation** - Bulk-import sources (URLs, PDFs, YouTube, Google Drive), run web research queries, and extract insights programmatically. Build repeatable research pipelines.

🎙️ **Content Generation** - Generate Audio Overviews (podcasts), videos, quizzes, flashcards, and study guides. Turn your sources into polished content with a single command.

## Three Ways to Use

| Method | Best For |
|--------|----------|
| **Python API** | Application integration, async workflows, custom pipelines |
| **CLI** | Shell scripts, quick tasks, CI/CD automation |
| **Agent Skills** | Claude Code, LLM agents, natural language automation |

## Installation

```bash
# Basic installation
pip install notebooklm-py

# With browser login support (required for first-time setup)
pip install "notebooklm-py[browser]"
playwright install chromium
```
See [Installation](#installation) for options or jump to [Quick Start](#quick-start).

## Quick Start

### CLI

```bash
# 1. Authenticate (opens browser)
notebooklm login

# 2. Create a notebook
notebooklm create "My Research"
notebooklm use <notebook_id>

# 3. Add sources
notebooklm source add "https://en.wikipedia.org/wiki/Artificial_intelligence"
notebooklm source add "./paper.pdf"

# 4. Chat
notebooklm ask "What are the key themes?"

# 5. Generate a podcast
notebooklm generate audio --wait
notebooklm download audio ./podcast.mp3
```

### Python API

```python
import asyncio
from notebooklm import NotebookLMClient

async def main():
    async with await NotebookLMClient.from_storage() as client:
        # List notebooks
        notebooks = await client.notebooks.list()

        # Create notebook and add source
        nb = await client.notebooks.create("Research")
        await client.sources.add_url(nb.id, "https://example.com")

        # Chat
        result = await client.chat.ask(nb.id, "Summarize this")
        print(result.answer)

        # Generate podcast
        status = await client.artifacts.generate_audio(nb.id)
        await client.artifacts.wait_for_completion(nb.id, status.task_id)

asyncio.run(main())
```

### Agent Skills (Claude Code)

```bash
# Install the skill
notebooklm skill install

# Then use natural language in Claude Code:
# "Create a podcast about quantum computing"
# "Summarize these URLs into a notebook"
# "/notebooklm generate video"
```

## Features

| Category | Capabilities |
|----------|--------------|
| **Notebooks** | Create, list, rename, delete, share |
| **Sources** | URLs, YouTube, files (PDF/TXT/MD/DOCX), Google Drive, pasted text |
| **Chat** | Questions, conversation history, custom personas |
| **Generation** | Audio podcasts, video, slides, quizzes, flashcards, reports, infographics, mind maps |
| **Research** | Web and Drive research agents with auto-import |
| **Downloads** | Audio, video, slides, infographics |
| **Agent Skills** | Claude Code skill for LLM-driven automation |

## Documentation

- **[Getting Started](docs/getting-started.md)** - Installation and first workflow
- **[CLI Reference](docs/cli-reference.md)** - Complete command documentation
- **[Python API](docs/python-api.md)** - Full API reference
- **[Configuration](docs/configuration.md)** - Storage and settings
- **[Troubleshooting](docs/troubleshooting.md)** - Common issues and solutions
- **[API Stability](docs/stability.md)** - Versioning policy and stability guarantees

### For Contributors

- **[Architecture](docs/contributing/architecture.md)** - Code structure
- **[Testing](docs/contributing/testing.md)** - Running and writing tests
- **[RPC Capture](docs/reference/internals/rpc-capture.md)** - Protocol reference and capture guides
- **[Debugging](docs/contributing/debugging.md)** - Network capture guide
- **[Changelog](CHANGELOG.md)** - Version history and release notes
- **[Security](SECURITY.md)** - Security policy and credential handling

## Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** | ✅ Tested | Primary development platform |
| **Linux** | ✅ Tested | Fully supported |
| **Windows** | ✅ Tested | Tested in CI |

## License

MIT License. See [LICENSE](LICENSE) for details.
