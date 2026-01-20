# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`SourceType` enum** - New `str, Enum` for type-safe source identification:
  - `GOOGLE_DOCS`, `GOOGLE_SLIDES`, `GOOGLE_SPREADSHEET`, `PDF`, `PASTED_TEXT`, `WEB_PAGE`, `YOUTUBE`, `MARKDOWN`, `DOCX`, `CSV`, `IMAGE`, `MEDIA`, `UNKNOWN`
- **`ArtifactType` enum** - New `str, Enum` for type-safe artifact identification:
  - `AUDIO`, `VIDEO`, `REPORT`, `QUIZ`, `FLASHCARDS`, `MIND_MAP`, `INFOGRAPHIC`, `SLIDES`, `DATA_TABLE`, `UNKNOWN`
- **`.kind` property** - Unified type access across `Source`, `Artifact`, and `SourceFulltext`:
  ```python
  # Works with both enum and string comparison
  source.kind == SourceType.PDF        # True
  source.kind == "pdf"                 # Also True
  artifact.kind == ArtifactType.AUDIO  # True
  artifact.kind == "audio"             # Also True
  ```
- **`UnknownTypeWarning`** - Warning (deduplicated) when API returns unknown type codes
- **`SourceStatus.PREPARING`** - New status (5) for sources in upload/preparation phase
- **E2E test coverage** - Added file upload tests for CSV, MP3, MP4, DOCX, JPG, Markdown with type verification

### Changed
- **Source type detection** - Use API-provided type codes as source of truth instead of URL/extension heuristics
- **CLI file handling** - Simplified to always use `add_file()` for proper type detection
- **`StudioContentType`** - Now internal only (use `ArtifactType` for public API)

### Removed
- **`detect_source_type()`** - Obsolete heuristic function replaced by `Source.kind` property
- **`ARTIFACT_TYPE_DISPLAY`** - Unused constant replaced by `get_artifact_type_display()`

### Deprecated
- **`Source.source_type`** - Use `.kind` property instead (returns `SourceType` enum)
- **`Artifact.artifact_type`** - Use `.kind` property instead (returns `ArtifactType` enum)
- **`SourceFulltext.source_type`** - Use `.kind` property instead

## [0.2.1] - 2026-01-15

### Added
- **Authentication diagnostics** - New `notebooklm auth check` command for troubleshooting auth issues
  - Shows storage file location and validity
  - Lists cookies present and their domains
  - Detects `NOTEBOOKLM_AUTH_JSON` and `NOTEBOOKLM_HOME` usage
  - `--test` flag performs network validation
  - `--json` flag for machine-readable output (CI/CD friendly)
- **Structured logging** - Comprehensive DEBUG logging across library
  - `NOTEBOOKLM_LOG_LEVEL` environment variable (DEBUG, INFO, WARNING, ERROR)
  - RPC call timing and method tracking
  - Legacy `NOTEBOOKLM_DEBUG_RPC=1` still works
- **RPC health monitoring** - Automated nightly check for Google API changes
  - Detects RPC method ID mismatches before they cause failures
  - Auto-creates GitHub issues with `rpc-breakage` label on detection

### Fixed
- **Cookie domain priority** - Prioritize `.google.com` cookies over regional domains (e.g., `.google.co.uk`) for more reliable authentication
- **YouTube URL parsing** - Improved handling of edge cases in YouTube video URLs

### Documentation
- Added `auth check` to CLI reference and troubleshooting guide
- Consolidated CI/CD troubleshooting in development guide
- Added installation instructions to SKILL.md for Claude Code
- Clarified version numbering policy (PATCH vs MINOR)

## [0.2.0] - 2026-01-14

### Added
- **Source fulltext extraction** - Retrieve the complete indexed text content of any source
  - New `client.sources.get_fulltext(notebook_id, source_id)` Python API
  - New `source fulltext <source_id>` CLI command with `--json` and `-o` output options
  - Returns `SourceFulltext` dataclass with content, title, URL, and character count
- **Chat citation references** - Get detailed source references for chat answers
  - `AskResult.references` field contains list of `ChatReference` objects
  - Each reference includes `source_id`, `cited_text`, `start_char`, `end_char`, `chunk_id`
  - Use `notebooklm ask "question" --json` to see references in CLI output
- **Source status helper** - New `source_status_to_str()` function for consistent status display
- **Quiz and flashcard downloads** - Export interactive study materials in multiple formats
  - New `download quiz` and `download flashcards` CLI commands
  - Supports JSON, Markdown, and HTML output formats via `--format` flag
  - Python API: `client.artifacts.download_quiz()` and `client.artifacts.download_flashcards()`
- **Extended artifact downloads** - Download additional artifact types
  - New `download report` command (exports as Markdown)
  - New `download mind-map` command (exports as JSON)
  - New `download data-table` command (exports as CSV)
  - All download commands support `--all`, `--latest`, `--name`, and `--artifact` selection options

### Fixed
- **Regional Google domain authentication** - SID cookie extraction now works with regional Google domains (e.g., google.co.uk, google.de, google.cn) in addition to google.com
- **Artifact completion detection** - Media URL availability is now verified before reporting artifact as complete, preventing premature "ready" status
- **URL hostname validation** - Use proper URL parsing instead of string operations for security

### Changed
- **Pre-commit checks** - Added mypy type checking to required pre-commit workflow

## [0.1.4] - 2026-01-11

### Added
- **Source selection for chat and artifacts** - Select specific sources when using `ask` or `generate` commands
  - New `--sources` flag accepts comma-separated source IDs or partial matches
  - Works with all generation commands (audio, video, quiz, etc.) and chat
- **Research sources table** - `research status` now displays sources in a formatted table instead of just a count

### Fixed
- **JSON output broken in TTY terminals** - `--json` flag output was including ANSI color codes, breaking JSON parsing for commands like `notebooklm list --json`
- **Warning stacklevel** - `warnings.warn` calls now report correct source location

### Infrastructure
- **Windows CI testing** - Windows is now part of the nightly E2E test matrix
- **VCR.py integration** - Added recorded HTTP cassette support for faster, deterministic integration tests
- **Test coverage improvements** - Improved coverage for `_artifacts.py` (71% → 83%), `download.py`, and `session.py`

## [0.1.3] - 2026-01-10

### Fixed
- **PyPI README links** - Documentation links now work correctly on PyPI
  - Added `hatch-fancy-pypi-readme` plugin for build-time link transformation
  - Relative links (e.g., `docs/troubleshooting.md`) are converted to version-tagged GitHub URLs
  - PyPI users now see links pointing to the exact version they installed (e.g., `/blob/v0.1.3/docs/...`)
- **Development repository link** - Added prominent source link for PyPI users to find the GitHub repo

## [0.1.2] - 2026-01-10

### Added
- **Ruff linter/formatter** - Added to development workflow with pre-commit hooks and CI integration
- **Multi-version testing** - Docker-based test runner script for Python 3.10-3.14 (`/matrix` skill)
- **Artifact verification workflow** - New CI workflow runs 2 hours after nightly tests to verify generated artifacts

### Changed
- **Python version support** - Now supports Python 3.10-3.14 (dropped 3.9)
- **CI authentication** - Use `NOTEBOOKLM_AUTH_JSON` environment variable (inline JSON, no file writes)

### Fixed
- **E2E test cleanup** - Generation notebook fixture now only cleans artifacts once per session (was deleting artifacts between tests)
- **Nightly CI** - Fixed pytest marker from `-m e2e` to `-m "not variants"` (e2e marker didn't exist)
- macOS CI fix for Playwright version extraction (grep pattern anchoring)
- Python 3.10 test compatibility with mock.patch resolution

### Documentation
- Claude Code skill: parallel agent safety guidance
- Claude Code skill: timeout recommendations for all artifact types
- Claude Code skill: clarified `-n` vs `--notebook` flag availability

## [0.1.1] - 2026-01-08

### Added
- `NOTEBOOKLM_HOME` environment variable for custom storage location
- `NOTEBOOKLM_AUTH_JSON` environment variable for inline authentication (CI/CD friendly)
- Claude Code skill installation via `notebooklm skill install`

### Fixed
- Infographic generation parameter structure
- Mind map artifacts now persist as notes after generation
- Artifact export with proper ExportType enum handling
- Skill install path resolution for package data

### Documentation
- PyPI release checklist
- Streamlined README
- E2E test fixture documentation

## [0.1.0] - 2026-01-06

### Added
- Initial release of `notebooklm-py` - unofficial Python client for Google NotebookLM
- Full notebook CRUD operations (create, list, rename, delete)
- **Research polling CLI commands** for LLM agent workflows:
  - `notebooklm research status` - Check research progress (non-blocking)
  - `notebooklm research wait --import-all` - Wait for completion and import sources
  - `notebooklm source add-research --no-wait` - Start deep research without blocking
- **Multi-artifact downloads** with intelligent selection:
  - `download audio`, `download video`, `download infographic`, `download slide-deck`
  - Multiple artifact selection (--all flag)
  - Smart defaults and intelligent filtering (--latest, --earliest, --name, --artifact-id)
  - File/directory conflict handling (--force, --no-clobber, auto-rename)
  - Preview mode (--dry-run) and structured output (--json)
- Source management:
  - Add URL sources (with YouTube transcript support)
  - Add text sources
  - Add file sources (PDF, TXT, MD, DOCX) via native upload
  - Delete sources
  - Rename sources
- Studio artifact generation:
  - Audio overviews (podcasts) with 4 formats and 3 lengths
  - Video overviews with 9 visual styles
  - Quizzes and flashcards
  - Infographics, slide decks, and data tables
  - Study guides, briefing docs, and reports
- Query/chat interface with conversation history support
- Research agents (Fast and Deep modes)
- Artifact downloads (audio, video, infographics, slides)
- CLI with 27 commands
- Comprehensive documentation (API, RPC, examples)
- 96 unit tests (100% passing)
- E2E tests for all major features

### Fixed
- Audio overview instructions parameter now properly supported at RPC position [6][1][0]
- Quiz and flashcard distinction via title-based filtering
- Package renamed from `notebooklm-automation` to `notebooklm`
- CLI module renamed from `cli.py` to `notebooklm_cli.py`
- Removed orphaned `cli_query.py` file

### ⚠️ Beta Release Notice

This is the initial public release of `notebooklm-py`. While core functionality is tested and working, please note:

- **RPC Protocol Fragility**: This library uses undocumented Google APIs. Method IDs can change without notice, potentially breaking functionality. See [Troubleshooting](docs/troubleshooting.md) for debugging guidance.
- **Unofficial Status**: This is not affiliated with or endorsed by Google.
- **API Stability**: The Python API may change in future releases as we refine the interface.

### Known Issues

- **RPC method IDs may change**: Google can update their internal APIs at any time, breaking this library. Check the [RPC Development Guide](docs/rpc-development.md) for how to identify and update method IDs.
- **Rate limiting**: Heavy usage may trigger Google's rate limits. Add delays between bulk operations.
- **Authentication expiry**: CSRF tokens expire after some time. Re-run `notebooklm login` if you encounter auth errors.
- **Large file uploads**: Files over 50MB may fail or timeout. Split large documents if needed.

[Unreleased]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/teng-lin/notebooklm-py/releases/tag/v0.1.0
