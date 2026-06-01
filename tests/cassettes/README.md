# VCR Cassettes

Recorded HTTP interactions used by the integration test tier for deterministic,
offline replay. See [docs/development.md](../../docs/development.md) for the
recording workflow and [tests/vcr_config.py](../vcr_config.py) for the VCR
setup (scrubbing, matchers, record modes).

## Layout

```text
tests/cassettes/
├── README.md                           (this file)
├── <object>_<operation>.yaml           (real recorded interactions)
├── <object>_<operation>_<context>.yaml (real, with extra context)
└── examples/
    └── example_<description>.yaml      (illustrative fixtures, not recordings)
```

## Naming convention

### Real cassettes — `<object>_<operation>[_<context>].yaml`

Recorded against the live NotebookLM API. Live in `tests/cassettes/` (top
level — no subdirectory).

`<object>` is the API surface — typically the `client.<area>` namespace name:

| Object         | Examples |
|----------------|----------|
| `notebooks`    | `notebooks_list.yaml`, `notebooks_create.yaml`, `notebooks_rename.yaml` |
| `sources`      | `sources_add_url.yaml`, `sources_add_drive.yaml`, `sources_get_guide.yaml` |
| `artifacts`    | `artifacts_list_quizzes.yaml`, `artifacts_generate_report.yaml` |
| `chat`         | `chat_ask.yaml`, `chat_ask_with_references.yaml` |
| `notes`        | `notes_create.yaml`, `notes_list_mind_maps.yaml` |
| `auth`         | `auth_rotate_cookies_refresh.yaml` |
| `cli`          | `cli_doctor.yaml`, `cli_login_browser_cookies_check.yaml` |

`<operation>` is the method or CLI verb being exercised (`list`, `add`,
`download`, `generate`, `rename`, etc.).

`<context>` (optional) disambiguates parametrized variants of the same
operation. Use it when one operation has several recordings:

- Source kind: `sources_add_url.yaml`, `sources_add_text.yaml`,
  `sources_add_drive.yaml`, `sources_add_file.yaml`
- Artifact kind: `artifacts_list_video.yaml`, `artifacts_list_quizzes.yaml`
- Output format: `artifacts_download_quiz_markdown.yaml`

Keep the slug **lowercase, words separated by `_`** to match the basename
literals in the repair allowlist and shape-lint xfail lists.

### Example cassettes — `examples/example_<description>.yaml`

Illustrative fixtures used by `tests/integration/test_vcr_example.py` to
demonstrate the cassette format, scrubbing pipeline, and `use_cassette`
decorator. They are **hand-crafted, not real recordings**, and target
`httpbin.org` rather than the live NotebookLM API.

Always live under the `examples/` subdirectory, always prefixed `example_`.
Tests that reference them must use the subpath:

```python
@notebooklm_vcr.use_cassette("examples/example_scrubbed_cookies.yaml")
```

The subdirectory placement keeps illustrative fixtures out of the cassette
loops in `tests/integration/conftest.py` (`_real_cassettes`),
`tests/unit/test_cassette_shapes.py`, and
`tests/scripts/check_cassettes_clean.py`, all of which scan the top level
of `tests/cassettes/` non-recursively. Adding a new example only needs to
land in `examples/` — no allowlist or filter edits required.

## When to add a cassette

- **New real cassette**: record against the live API with
  `NOTEBOOKLM_VCR_RECORD=1`. The slug is `<object>_<operation>` plus an
  optional `_<context>` if the test parametrizes. Verify sensitive data is
  scrubbed (`uv run python tests/scripts/check_cassettes_clean.py`) before
  committing.
- **New illustrative example**: hand-author the YAML under `examples/`
  with the `example_` prefix. Reference it from the test via the
  `examples/example_<description>.yaml` subpath.

## Related

- [tests/vcr_config.py](../vcr_config.py) — VCR configuration, scrubbers,
  matchers (`rpcids`, `freq`).
- [tests/cassette_patterns.py](../cassette_patterns.py) — canonical scrub
  pattern registry.
- [tests/scripts/check_cassettes_clean.py](../scripts/check_cassettes_clean.py)
  — pre-commit guard that asserts no sensitive data slips into a cassette.
- [docs/development.md](../../docs/development.md) — recording workflow,
  test notebook IDs, scrubbing details.
