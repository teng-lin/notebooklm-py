# VCR Cassettes

Recorded HTTP interactions used by the integration test tier for deterministic,
offline replay. See [docs/development.md](../../docs/development.md) for the
recording workflow and [tests/vcr_config.py](../vcr_config.py) for the VCR
setup (scrubbing, matchers, record modes).

## Layout

Cassettes are split by **client tier** — the wire protocol they were recorded
against. Each tier owns a top-level subdirectory; nothing lives loose in
`tests/cassettes/` itself.

```text
tests/cassettes/
├── README.md                               (this file)
├── web/                                    Web batchexecute RPC (YAML, vcrpy)
│   ├── <object>_<operation>.yaml           (recorded interactions or synthetic error cassettes)
│   ├── <object>_<operation>_<context>.yaml (recorded interactions with extra context)
│   ├── gzip_coverage/
│   │   └── *.yaml                          (derived replay fixtures for gzip coverage)
│   └── examples/
│       └── example_<description>.yaml      (illustrative fixtures, not recordings)
└── android/                                Android protocol captures
    ├── *.grpc.json                         sanitized protobuf gRPC interactions
    └── *_phenotype.yaml                    auxiliary protobuf-over-HTTP (VCR.py)
```

`web/` is the VCR `cassette_library_dir` (`tests/vcr_config.py`), so cassette
names in test code are **relative to `web/`** and carry no tier prefix:

```python
@notebooklm_vcr.use_cassette("notebooks_list.yaml")          # web/notebooks_list.yaml
@notebooklm_vcr.use_cassette("examples/example_scrubbed_cookies.yaml")
```

`android/` is not a vcrpy corpus at all — it replays through a consumer-owned
gRPC channel seam (`tests/unit/android/test_grpc_cassette.py`,
`tests/integration/test_android_grpc_cassette.py`). Those tests construct the
public client with `backend="android"`; the harness resolves cassette paths
itself. The narrowly scoped `*_phenotype.yaml` exception captures the separate
protobuf-over-HTTP call needed to obtain Play Books experiment metadata; it is
never used to capture gRPC.

## Naming convention

### Web cassettes — `web/<object>_<operation>[_<context>].yaml`

Recorded against the live NotebookLM API. Most live directly in `web/`. A few
`web/error_synthetic_*.yaml` files are synthetic error recordings used by
error-replay tests, and `web/gzip_coverage/` holds a derived replay cassette
for gzip decoding coverage.

`<object>` is the API surface — typically the `client.<area>` namespace name:

| Object         | Examples |
|----------------|----------|
| `notebooks`    | `notebooks_list.yaml`, `notebooks_create.yaml`, `notebooks_rename.yaml` |
| `sources`      | `sources_add_url.yaml`, `sources_add_drive.yaml`, `sources_get_guide.yaml` |
| `artifacts`    | `artifacts_list_quizzes.yaml`, `artifacts_generate_report.yaml` |
| `chat`         | `chat_ask.yaml`, `chat_ask_with_references.yaml` |
| `notes`        | `notes_create.yaml`, `notes_list_mind_maps.yaml` |
| `auth`         | `auth_rotate_cookies_refresh.yaml` |
| `cli`          | `cli_doctor.yaml`, `cli_auth.yaml`, `cli_login_browser_cookies_check.yaml` |

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

### Example cassettes — `web/examples/example_<description>.yaml`

Illustrative fixtures used by `tests/integration/test_vcr_example.py` to
demonstrate the cassette format, scrubbing pipeline, and `use_cassette`
decorator. They are **hand-crafted, not real recordings**, and target
`httpbin.org` rather than the live NotebookLM API.

Always live under `web/examples/`, always prefixed `example_`. Tests that
reference them use the `examples/`-relative subpath (the `web/` prefix is
implied by `cassette_library_dir`):

```python
@notebooklm_vcr.use_cassette("examples/example_scrubbed_cookies.yaml")
```

The subdirectory placement keeps illustrative fixtures out of the replay-time
real-cassette discovery in `tests/integration/conftest.py` (`_real_cassettes`),
which globs `web/*.yaml` non-recursively. Cleanliness and shape guards are
broader: CI runs `tests/scripts/check_cassettes_clean.py --strict --recursive`
over the whole tree, and golden decode coverage also scans recursively while
excluding `examples/`.

### Android cassettes — `android/*.grpc.json`

Sanitized protobuf captures replayed through the test-only gRPC channel seam
(`@pytest.mark.grpc_cassette`). They are JSON, not YAML, and are never loaded
by vcrpy. See [docs/development.md](../../docs/development.md) for the Android
recording workflow.

### Android auxiliary HTTP — `android/*_phenotype.yaml`

The Play Books write path obtains an experiment token through an HTTPS
protobuf request before invoking gRPC. Its integration test captures that one
HTTP exchange with VCR.py and protobuf-aware body redaction. Authorization and
token values are never committed; the corresponding gRPC cassette pins only
the required application metadata key names.

## When to add a cassette

- **New web cassette**: record against the live API with
  `NOTEBOOKLM_VCR_RECORD=1`. This uses VCR `new_episodes` mode: existing
  matching interactions replay, and only missing ones append. To fully
  re-record an existing cassette, delete or move it first. VCR writes it into
  `tests/cassettes/web/` automatically. The slug is `<object>_<operation>`
  plus an optional `_<context>` if the test parametrizes.
  Verify sensitive data is scrubbed
  (`uv run python tests/scripts/check_cassettes_clean.py --strict`)
  before committing.
  Mutation cassettes that return resource UUIDs and feed them into later
  requests should additionally use `ResourceIdCassetteScrubber` from
  `tests/vcr_config.py`; it preserves equality with deterministic reserved
  UUID placeholders without committing account-linkable notebook/source IDs.
- **New illustrative example**: hand-author the YAML under `web/examples/`
  with the `example_` prefix. Reference it from the test via the
  `examples/example_<description>.yaml` subpath.

## Related

- [tests/vcr_config.py](../vcr_config.py) — VCR configuration, scrubbers,
  matchers (`rpcids`, `freq`), and the `cassette_library_dir` that pins web
  cassette lookups to `web/`.
- [tests/cassette_patterns.py](../cassette_patterns.py) — canonical scrub
  pattern registry.
- [tests/scripts/check_cassettes_clean.py](../scripts/check_cassettes_clean.py)
  — CI/repo-lint guard that asserts no sensitive data slips into cassettes.
  Its no-argument scan of `tests/cassettes/` always recurses, so it covers
  every tier.
- [docs/development.md](../../docs/development.md) — recording workflow,
  test notebook IDs, scrubbing details.
