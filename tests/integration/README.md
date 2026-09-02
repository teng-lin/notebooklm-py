# `tests/integration/` — recorded-seam rule

This directory holds the **integration tier** of the test pyramid. Anything
collected here exercises real or recorded-real NotebookLM traffic. Web
`batchexecute` calls use [VCR.py](https://github.com/kevin1024/vcrpy) HTTP
cassettes. Android calls use the test-only protobuf-aware gRPC channel seam in
`tests/_helpers/android_grpc_cassette.py`, because `grpc.aio` performs its I/O
in gRPC C-core and is not intercepted by vcrpy.

To keep the tier honest — i.e. to keep "integration" from quietly slipping
back into "unit with extra ceremony" — every test collected under
`tests/integration/` MUST satisfy one of these four rules. The
`pytest_collection_modifyitems` hook in `conftest.py` raises
`pytest.UsageError` at collection time if none of them holds, so a violation
fails CI immediately rather than degrading the tier silently.

## The rule

A `tests/integration/` test is accepted if **any** of the following is true:

1. **`@pytest.mark.vcr`** is applied (per-test decorator or module-level
   `pytestmark = [pytest.mark.vcr, ...]`).
2. **`@notebooklm_vcr.use_cassette("…")`** decorates the test function. The
   hook detects the VCR-wrapped function by walking the function's
   `wrapt.FunctionWrapper` chain and matching `CassetteContextDecorator` on
   the bound `_self_wrapper`.
3. **`@pytest.mark.grpc_cassette`** is applied to a test replaying an Android
   `.grpc.json` cassette through the custom channel adapter.
4. **`@pytest.mark.allow_no_vcr`** is applied as an explicit opt-out.

If none of the four is present, collection fails with a message naming the
violating node IDs.

## Android gRPC cassettes

Android cassettes live in `tests/cassettes/android/` and intentionally use the
`.grpc.json` suffix rather than vcrpy's YAML format. Each interaction pins the
full method path, unary-unary versus unary-stream shape, deterministic request
protobuf FQN/bytes, and deterministic response protobuf FQN/bytes. The model
can pin an allowlisted set of application metadata key names, but never stores
metadata values or bearer credentials. Recording requires an explicit
application-level sanitizer and then unconditionally runs the generalized
protobuf redactor as its final security boundary. The redactor
discards unknown fields, replaces every string and byte string, and maps
integers/floats to safe non-zero placeholders while preserving scalar presence,
message structure, booleans, and schema-defined enum values. Replay injects an
in-memory channel and a non-secret bearer provider; it must never construct a
live gRPC channel or mint OAuth credentials.

Set `NOTEBOOKLM_ANDROID_GRPC_RECORD=1` only for an explicitly reviewed recording
test. In that mode, `@pytest.mark.grpc_cassette` keeps the real profile home
available. Replay remains isolated from the developer's profile. The Play Books
family also records its auxiliary Phenotype protobuf-over-HTTP exchange through
VCR.py into `play_books_phenotype.yaml`; the HTTP cassette strips authorization
and redacts both protobuf bodies. Hand-built fixtures must be named
`*_synthetic.grpc.json`; do not call them recorded traffic.

### Recorded families

Every `@pytest.mark.grpc_cassette` test in `test_android_grpc_cassette.py`
binds one family through the `android_grpc_cassette` fixture and drives the
**public** `NotebookLMClient(..., backend="android")`. The same test body
records and replays (`tests/_helpers/android_grpc_harness.py`):

| Cassette | Public calls | Wire RPCs |
|---|---|---|
| `get_or_create_account` | `settings.get_user_settings()` | `GetOrCreateAccount` |
| `get_project_rich` | `notebooks.get()`, `sources.list()` | `GetProject` ×2 |
| `load_source` | `sources.get_fulltext()` | `GetProject` ×2, `LoadSource` |
| `retrieve_relevant_chunks` | `sources.search()`, `sources.search(..., source_ids=...)` | `GetProject`, `RetrieveRelevantChunks` ×2 |
| `list_artifacts_get_notes` | `artifacts.list()`, `notes.list()` | `ListArtifacts`, `GetNotes` ×2 |
| `get_labels` | `labels.list()`, `collections.create()`, `collections.list()`, `collections.delete()` | `GetLabels` ×5, `CreateLabel`, `DeleteLabels` |
| `list_discover_sources_job` | `research.poll()` | `ListDiscoverSourcesJob` |
| `research_discover` | `research.discover()` | `DiscoverSources` |
| `get_project_details` | `sharing.get_status()` | `GetProjectDetails` |
| `generate_free_form_streamed` | `chat.ask()` | `GetProject`, `ListChatSessions` ×2, `ListChatTurns`, `GenerateFreeFormStreamed (stream)` |
| `list_chat_sessions_turns` | `chat.ask()`, `chat.get_conversation_id()`, `chat.get_history()` | `GetProject`, `ListChatSessions` ×5, `ListChatTurns` ×2, `GenerateFreeFormStreamed (stream)` |
| `chat_session_control` | `chat.ask()`, `chat.session_status()`, `chat.cancel()` | `GetProject`, `ListChatSessions` ×2, `ListChatTurns`, `GenerateFreeFormStreamed (stream)`, `GetChatSessionStatus`, `CancelGeneration` |
| `notebook_lifecycle` | `notebooks.create/rename/set_emoji/list/copy/delete()` | `ListRecentlyViewedProjects` ×2, `CreateProject`, `MutateProject` ×2, `CopyProject`, `DeleteProjects` ×2 |
| `generate_notebook_guide` | `notebooks.get_description()`, `notebooks.get_summary()` | `GenerateNotebookGuide` ×2 |
| `source_lifecycle` | `sources.add_text/add_url/wait_until_ready/rename/get_guide/check_freshness/refresh/delete()` | `AddTentativeSources` ×2, `AddSources` ×2, `GetProject` ×10, `MutateSource`, `GenerateDocumentGuides`, `CheckSourceFreshness` ×2, `DeleteSources` ×2 |
| `note_lifecycle` | `notes.create/update/get/delete()` | `CreateNote`, `GetNotes` ×6, `MutateNote`, `DeleteNotes` |
| `label_lifecycle` | `labels.create/rename/set_emoji/add_sources/sources/remove_sources/generate/delete/list()` | `GetProject` ×2, `CreateLabel` ×2, `GetLabels` ×11, `MutateLabel` ×4, `DeleteLabels` |
| `collection_lifecycle` | `collections.create/rename/add_notebooks/notebooks/remove_notebooks/delete/get_or_none()` | `GetLabels` ×9, `CreateLabel`, `MutateLabel` ×3, `ListRecentlyViewedProjects`, `DeleteLabels` |
| `share_project` | `sharing.set_public()`, `sharing.get_status()` | `ShareProject` ×2, `GetProjectDetails` ×3 |
| `delete_chat_turns` | `chat.ask()`, `chat.delete_conversation()`, `chat.get_history()` | `GetProject`, `ListChatSessions` ×5, `ListChatTurns` ×2, `GenerateFreeFormStreamed (stream)`, `DeleteChatTurns` |
| `mutate_account` | `settings.get_output_language()`, `settings.set_output_language()` | `GetOrCreateAccount`, `MutateAccount` |
| `act_on_sources_mind_map` | `artifacts.generate_mind_map()`, `notes.delete()` | `GetProject`, `ActOnSources`, `CreateNote`, `GetNotes` ×2, `DeleteNotes` |
| `quiz_lifecycle` | `artifacts.generate_quiz/poll_status/get/rename/delete/get_or_none()` | `GetProject`, `CreateArtifact`, `ListArtifacts` ×8, `GetArtifact` ×3, `GetNotes` ×2, `UpdateArtifact`, `DeleteArtifact` |
| `generate_report_suggestions` | `artifacts.suggest_reports()` | `GenerateReportSuggestions` |
| `next_step_suggestions` | `notebooks.suggest_next_steps()`, `artifacts.get_customization_choices()` | `NextStepSuggestions` ×2, `GetArtifactCustomizationChoices` |
| `source_transfers` | `notebooks.create()`, `sources.add_urls_async/wait_until_ready/append_text/get_fulltext/copy()`, `notebooks.delete()` | `CreateProject`, `AddSourcesAsync`, `GetProject` ×N, `AppendSource`, `LoadSource` ×2, `CopySourcesAsync`, `DeleteProjects` |
| `play_books` | `sources.list_play_books()`, `sources.add_play_book()`, `sources.delete()` | `ListExpertIntelligenceContent` ×2, `AddTentativeSources`, `AddSources`, `GetProject` ×2, `DeleteSources` |
| `artifact_copy` | `artifacts.generate_flashcards/poll_status()`, `notebooks.create()`, `artifacts.copy()`, `artifacts.delete()`, `notebooks.delete()` | `GetProject`, `CreateArtifact`, `ListArtifacts` ×N, `CreateProject`, `CopyArtifactsAsync`, `DeleteArtifact`, `DeleteProjects` |
| `research_fast_cancel` | `research.start(mode="fast")`, `research.cancel()`, `research.poll()` | `DiscoverSourcesManifold`, `ListDiscoverSourcesJob` ×2, `CancelDiscoverSourcesJob` |
| `research_fast_import` | `research.start(mode="fast")`, `research.poll()`, `research.import_sources()` | `DiscoverSourcesManifold`, `ListDiscoverSourcesJob`, `FinishDiscoverSourcesRun` |
| `generate_report` | `artifacts.generate_report/poll_status/get/delete/get_or_none()` | `GetProject`, `CreateArtifact`, `ListArtifacts` ×5, `GetArtifact` ×2, `GetNotes` ×2, `DeleteArtifact` |
| `generate_flashcards` | `artifacts.generate_flashcards/poll_status/get/delete/get_or_none()` | `GetProject`, `CreateArtifact`, `ListArtifacts` ×6, `GetArtifact` ×3, `GetNotes` ×2, `DeleteArtifact` |
| `generate_audio` | `artifacts.generate_audio/poll_status/get/delete/get_or_none()` | `GetProject`, `CreateArtifact`, `ListArtifacts` ×23, `GetArtifact` ×20, `GetNotes` ×2, `DeleteArtifact` |

33 families, 281 interactions. Re-record everything (creates one disposable scratch notebook with a text
source and a note through an *unrecorded* client, records, then deletes it):

```bash
NOTEBOOKLM_ANDROID_GRPC_RECORD=1 NOTEBOOKLM_PROFILE=<profile> \
    uv run pytest tests/integration/test_android_grpc_cassette.py -p no:randomly
```

Some families are *account*-scoped rather than scratch-scoped:
`get_or_create_account` and `mutate_account` keep the recorder's real account
booleans (for example `accepted_tos`, `is_premium_user`); `get_labels` and
`collection_lifecycle` record the shape of the recorder's real collections
(count and member count); `notebook_lifecycle` and `collection_lifecycle`
record the recorder's whole `ListRecentlyViewedProjects` inventory — the
notebook *count* plus per-project role and premium-feature booleans. Every id
and title is a placeholder and nothing identifying survives, but re-recording
binds those attributes of a real account into git — record from an account
you are comfortable describing.

Add `-k <family>` to re-record one cassette. Every protobuf type a cassette
carries must be registered in `KNOWN_CASSETTE_PAYLOAD_TYPES`
(`tests/unit/android/test_grpc_cassette.py`) so the canonicality guard can
decode it and prove the redactor is a no-op on the committed bytes.

Two rules make byte-exact request matching survive the record→replay gap:

- **Reserved placeholders.** `ProtoRedactor.reserve()` assigns placeholders to
  the test inputs (notebook id, question) *before* any traffic, in the same
  order in both modes, so replay knows the exact placeholder each input became
  (`00000000-0000-4000-8000-000000000001`, `SCRUBBED_STRING_0001`). The
  fixture hands the test real values while recording and these placeholders on
  replay.
  The reservation sequence (notebook id, question, URL, research query, the
  correlation names the harness feeds `sources.py` in place of its random
  `nblm-…` nonces, then the free-text pool `values.texts`) is
  part of the cassette contract: changing it renumbers every later placeholder,
  so re-record all families after touching it.
- **Request-local placeholders.** Requests are sanitized in a per-request
  scope: values the redactor already knows (reservations, ids echoed by
  earlier responses) keep their global placeholder, while unknown request
  scalars — client constants such as action names, language codes, the
  pinned app version — become `SCRUBBED_REQUEST_NNNN` numbered from one
  within that request. Request bytes therefore never depend on how much
  response traffic preceded them, which is what makes byte-exact matching
  stable between recording and replay.
  The flip side: a value the client mints itself and later matches against a
  response echo (the `sources.py` correlation names) is request-local on the
  request side but global on the response side, so the echo match fails on
  replay unless the value is reserved. That is why the harness injects reserved
  correlation names into `sources.py`; any new echo-match in `_android/` needs
  the same treatment (the mismatch diff will show `SCRUBBED_REQUEST_…` against
  `SCRUBBED_STRING_…`).
- **Request normalizers.** `tests/_helpers/android_grpc_normalizers.py` clears
  the few client-minted nonce fields (chat `user_message_id`) on both sides. It
  is a per-method table, not a field-policy engine, and never touches responses.

One integer field is exempt from numeric redaction:
`ResearchJobInfo.status`, an enum-coded status the recovered proto declares as
`int32` (see `_PRESERVED_CODE_FIELDS` in the seam). Everything else numeric
still collapses to `1`.

Known limitation: values rendered from *index ranges* over scrubbed text (the
chat answer sliced out of `response_doc`) are not replayable, because numeric
redaction collapses the ranges and string redaction changes lengths. Cassettes
prove pairing, ordering, ids, and enum/boolean semantics; render assertions
belong to the fake-server suite (`tests/unit/android/test_session_fake_server.py`)
or E2E.

## When to use `allow_no_vcr`

`allow_no_vcr` exists for tests that legitimately live under
`tests/integration/` for tree-organization reasons but make no real (or
recorded) HTTP calls. The authoritative allowlists live in:

- `tests/_fixtures/integration_allow_no_vcr_files.txt`
- `tests/_fixtures/integration_allow_no_vcr_nodeids.txt`
- `tests/_fixtures/integration_vcr_allow_no_vcr_nodeids.txt` for the rare
  intentional VCR/allow-no-VCR overlap

Current categories include:

- `test_auto_refresh.py` — asserts that the refresh callback is *wired*;
  doesn't fire a real refresh.
- `test_session_integration.py` — `httpx.MockTransport` + `AsyncMock` exercising error
  paths; no real socket.
- `test_*_idempotency.py` — mock-transport regression tests for retry /
  idempotency behavior; no live or recorded HTTP.
- The whole `concurrency/` subtree — uses `httpx.MockTransport` to inject
  scheduler-controllable behavior into the core/upload/download paths
  (real HTTP would defeat the determinism these tests need).

Per the project's testing strategy, **new mock-only tests should land in
`tests/unit/`** (or `tests/unit/concurrency/`). `allow_no_vcr` is a
transitional marker for the legacy mock-tier files above. Adding more of
them under `tests/integration/` should be a conscious decision, with the
allowlist manifests updated in the same PR. Real cassettes live in
`tests/cassettes/`, not under `tests/integration/`.

`test_gzip_cassette_replay.py` is VCR-tier, not `allow_no_vcr`: it uses a scoped
VCR instance over a derived cassette in `tests/cassettes/web/gzip_coverage/`.

## When to use `@pytest.mark.vcr` vs `@notebooklm_vcr.use_cassette`

- Module-level `pytestmark = [pytest.mark.vcr, skip_no_cassettes]` is the
  baseline for files where every test is VCR-tier. It also wires
  `skip_no_cassettes` so the run is skipped (not failed) when no real
  cassettes are present on disk.
- `@notebooklm_vcr.use_cassette("cassette_name.yaml")` pins a specific
  cassette to a specific test. Always pair with `@pytest.mark.vcr` (a)
  for self-documentation and (b) so the
  `_disable_keepalive_poke_for_vcr` autouse fixture activates — that
  fixture reads the marker, not the wrapper.

## Reference

- Hook implementation: `tests/integration/conftest.py`
  (`pytest_collection_modifyitems` + `_has_use_cassette_decorator`)
- Marker registration: `pyproject.toml` `[tool.pytest.ini_options].markers`
- Regression test (committed, pytester-based):
  `tests/unit/test_tier_enforcement_hook.py`
- Taxonomy guard: `tests/_guardrails/test_integration_allow_no_vcr_allowlist.py`
- Replay network guard: `tests/integration/conftest.py` refuses live sockets when
  cassette replay should be deterministic.
