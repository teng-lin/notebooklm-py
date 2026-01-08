# notebooklm-py

**Unofficial Python library, CLI, and agent skills for Google NotebookLM**

Automate Google NotebookLM programmatically. Create notebooks, add sources, chat with your content, and generate podcasts, videos, quizzes, and more.

[![PyPI version](https://badge.fury.io/py/notebooklm-py.svg)](https://badge.fury.io/py/notebooklm-py)
[![Python Version](https://img.shields.io/pypi/pyversions/notebooklm-py.svg)](https://pypi.org/project/notebooklm-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **⚠️ Unofficial API**: This library uses reverse-engineered Google APIs that can change without notice. Not affiliated with or endorsed by Google. See [Troubleshooting](docs/troubleshooting.md) if you encounter issues.

**Three ways to use:**
- **Python Library** - Async API for application integration
- **CLI** - Command-line tool for scripts and automation
- **Agent Skills** - Claude Code skill for natural language automation

## Installation

```bash
# Basic installation
pip install notebooklm-py

# With browser login support (required for first-time setup)
pip install "notebooklm-py[browser]"
playwright install chromium
```

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

### For Contributors

- **[Architecture](docs/contributing/architecture.md)** - Code structure
- **[Testing](docs/contributing/testing.md)** - Running and writing tests
- **[RPC Capture](docs/reference/internals/rpc-capture.md)** - Protocol reference and capture guides
- **[Debugging](docs/contributing/debugging.md)** - Network capture guide

## License

MIT License. See [LICENSE](LICENSE) for details.
