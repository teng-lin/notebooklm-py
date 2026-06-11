---
name: Bug Report
about: Report a bug in notebooklm-py
title: ""
labels: bug
assignees: ""
---

## Description

A clear description of the bug.

## Steps to Reproduce

1. ...
2. ...
3. ...

## Expected Behavior

What you expected to happen.

## Actual Behavior

What actually happened. Include the full error message or traceback if applicable.

```text
Paste error output here
```

## Environment

- OS: (e.g., macOS 15, Ubuntu 24.04, Windows 11)
- Python version: (e.g., 3.12)
- notebooklm-py version: (run `notebooklm --version`)
- Install method: (pip, uv, pipx)
- Surface: (CLI, Python API, MCP, REST server, desktop extension, docs)

## Debug Output

If applicable, run the failing command with `-vv` for verbose logging and paste the relevant output:

```bash
notebooklm -vv <your-command-here>
```

For auth/context issues, also include these outputs with cookies, emails, notebook
titles, and paths redacted as needed:

```bash
notebooklm doctor --json
notebooklm status --paths --json
notebooklm auth check --test --json
```

## Checklist

- [ ] I verified this bug exists on the latest version of notebooklm-py
- [ ] I searched existing issues and this is not a duplicate
