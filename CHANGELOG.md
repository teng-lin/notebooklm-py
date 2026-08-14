# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Existing MCP/REST URL-batch endpoints now use the batch-capable
  `ADD_SOURCE` wire shape.** Valid URLs share one write RPC; successful rows
  and silently omitted failures are reconciled back into the existing ordered,
  per-item result arrays, including partial and all-failed batches. Transport-
  uncertain writes are never replayed blindly, and a partially admitted group
  of exact duplicate URLs fails closed because the response carries no request
  positions. On REST/MCP, that uncertainty deliberately overrides the original
  transport label: even a typed `RateLimitError` projects as a non-retriable,
  `unconfirmed` RPC failure (REST 502 rather than 429), preventing downstream
  retry middleware from duplicating an already-committed subset. Python callers
  still receive the original typed exception. The public single-item
  `sources.add_url()` path and its precise
  probe-then-create recovery remain unchanged. Its two ambiguity messages now
  front-load the manual source-list instruction so MCP/REST's real 300-character
  truncation cannot cut away the only actionable guidance.
  ([#2115](https://github.com/teng-lin/notebooklm-py/issues/2115),
  [#2238](https://github.com/teng-lin/notebooklm-py/issues/2238))

- **Source listings retain original-file and revision metadata already present
  on the wire.** `Source` now exposes `download_url`, `viewer_url`, and
  `content_mime` for uploaded files, plus `word_count`, `revision_id`,
  `revision_timestamp`, and `last_modified_at`. All fields are optional so
  source kinds and response shapes that omit the corresponding slots continue
  to decode cleanly. The MIME from the original-content descriptor also joins
  the existing Drive-only MIME as a type-disambiguation signal. The recovered
  mobile schema leaves these slots unnamed; names for the size/revision/update
  metadata are explicitly evidence-based, backed by live shapes rather than
  presented as recovered proto names. ([#2112](https://github.com/teng-lin/notebooklm-py/issues/2112),
  [#2114](https://github.com/teng-lin/notebooklm-py/issues/2114))

- **Complete artifact listing metadata and user state**
  ([#2136](https://github.com/teng-lin/notebooklm-py/issues/2136),
  [#2135](https://github.com/teng-lin/notebooklm-py/issues/2135),
  [#2129](https://github.com/teng-lin/notebooklm-py/issues/2129)). `Artifact`
  now retains every audio/video media URL and duration, slide and infographic
  text, report kind, source IDs, last-modified time, etag, audio resume
  position, and flashcard study progress returned by `LIST_ARTIFACTS`. The
  content and per-user state are exposed as typed frozen records; unknown
  media/state variants and report labels remain observable. The genuine
  backend artifact codes 6 (`FANTASY_MAP`) and 10 (`FILE`) are now represented
  by both `ArtifactTypeCode` and public `ArtifactType` members.

- **Notebook Project metadata and live chat follow-up suggestions are now
  retained instead of discarded.** `Notebook` exposes its `emoji`, typed
  `premium_features`, create-only `chat_sessions`, and current `chat_settings`;
  `notebooks.set_emoji()` / `notebooks.update()` make the emoji writable. The
  initial `ChatSession` returned by `CREATE_NOTEBOOK` is consumed on the first
  ask, avoiding the redundant `hPTbtc` lookup that previously fetched the same
  id again. `AskResult.next_steps` now carries the backend's suggested
  follow-up chips as typed `NextStepSuggestion` values, preserving both their
  question and raw `MagicArtifactType` code (including unknown future codes).
  The richer notebook fields are Python-only so existing CLI/MCP/REST notebook
  JSON key sets stay stable; chat next steps are intentionally additive on MCP
  and REST ask responses. ([#2133](https://github.com/teng-lin/notebooklm-py/issues/2133),
  [#2123](https://github.com/teng-lin/notebooklm-py/issues/2123),
  [#2119](https://github.com/teng-lin/notebooklm-py/issues/2119))

- **A readable rendering of a source, and citation resolution that reads
  offsets instead of searching for text.** `SourceFulltext` gains
  `rendered_content`, and `StructuredDocument` gains `render(start, end)` and
  `extent`. This library renders a source three ways, and until now two of them
  were missing the point:

  | Surface | What it answers |
  |---|---|
  | `SourceFulltext.content` | what this client has always returned — unchanged, byte for byte |
  | `SourceFulltext.document` | *where* text sits: the offset-faithful parse citations index |
  | `SourceFulltext.rendered_content` | what the source *says*, laid out to be read |

  `content` joins every text **run** with `"\n"`, and a run is a sub-paragraph
  fragment — so a paragraph the backend split into three runs arrives as three
  lines. That was a consequence of flattening a tree nobody had parsed, not a
  rendering anyone chose: on the captured source in
  `tests/unit/fixtures/source_fulltext_tailwind_doc.json` it turns 13 blocks
  into 17 lines, and live on a 382-block Wikipedia source it turns those into
  1518. `rendered_content` renders from the parsed tree instead — runs joined
  *within* a block, blocks separated, blocks with nothing to read omitted — so
  the same paragraph comes back whole. It is marker-free by design (list
  glyphs and heading levels stay on `blocks`), derived, costs no extra request,
  and leaves `content` and `char_count` exactly where they were.

  **`resolve_chat_reference_passage()` now resolves by offset.** A citation
  carries `start_char` / `end_char` into the source document, so the helper
  reads the passage straight out of that coordinate space and returns it
  rendered; the value-based `find_citation_context` search over `content`
  becomes the fallback for a reference with no usable range or a source whose
  document did not decode. That search compares `cited_text` against a string
  that joins runs with `"\n"` while `cited_text` uses no separator at all, so
  a key spanning a block boundary cannot match anywhere — #2210 could only
  bound the key to keep it inside one block. Reading the range removes the
  failure mode instead of bounding it, and picks the *cited* occurrence in a
  source that says the same thing twice, which no search can do.

  Four guards keep the rewrite honest. A reference carrying neither a range nor
  `cited_text` still raises without issuing a request — fetching first would
  turn a structural anchor into an RPC the helper used to avoid. The range must
  **fit** the document (`end <= document.extent`), must **render something on
  its own** before any context window is added — so a citation covering only an
  image is not handed its neighbours' prose — and, when the reference also
  carries `cited_text`, the two must **agree**: a bounded prefix of
  `cited_text` has to appear in `document.slice(start, end)`.

  That last one is the case an extent check cannot see. Checking the bound only
  catches a source re-indexed *shorter*; re-index one longer, or move text
  within it, and the stale range still fits and still resolves — to the wrong
  passage, silently. The value search this replaced detected that for free by
  failing to match, so `cited_text` is kept as a **cross-check** (never as a
  locator): the offsets decide where the passage is, and a disagreement stands
  them down in favour of the search.

  Offsets are UTF-16 code units throughout — windows, clips and the extent
  check all count in them, so a source containing an emoji resolves to the
  characters the backend meant rather than to its neighbours.
  `rendered_content` is, like `content`, deliberately **not**
  offset-addressable: its separators are its own. The CLI `--json` / MCP /
  REST fulltext payloads stay pinned to their existing key sets.

  One known limitation, pinned by a test rather than fixed here: a **table**
  renders as one line with its cell text running together, because the parse
  flattens rows and cells into the block's spans and a cell is several runs.
  Separating spans would split cells as often as it separated them, so giving
  tables their boundaries back is a decoder change
  ([#2230](https://github.com/teng-lin/notebooklm-py/issues/2230), fixed below).
  ([#2211](https://github.com/teng-lin/notebooklm-py/issues/2211))

- **`notebooklm source list --status <state>`** filters the listing to one
  ingestion status — `ready`, `processing`, `error`, `preparing`, or `unknown`
  — bringing the CLI to parity with the MCP `source_list` tool, which has had
  the filter (including `preparing`) all along. The choices derive from
  `SourceStatus` via the new `SOURCE_STATUS_LABELS`, so they cannot drift from
  the labels the Status column renders, and the filter runs inside the fetch so
  `--json`'s `count` always matches the rows shown. It composes with `--label`.

  **`--status preparing` is the reconciliation query for orphaned sources.** A
  file add that fails after its source row is registered leaves that row in
  place on purpose — it is the evidence, and it still consumes notebook quota —
  but the row sits at `preparing`, not `error`, so a caller looking under
  `error` finds nothing. There was previously no supported way to reach those
  rows from the CLI at all. Rows genuinely mid-upload report `preparing` too, so
  the filter cannot by itself tell "abandoned" from "in flight"; re-run it and
  act only on rows that persist. Nothing is auto-deleted — that posture is
  deliberate ([#2110](https://github.com/teng-lin/notebooklm-py/issues/2110)).
  ([#2138](https://github.com/teng-lin/notebooklm-py/issues/2138))

- **Notebook sharing now reports the collaborator cap and the public-sharing
  policy gate.** `GET_SHARE_STATUS` returns an eight-slot response carrying six
  known response fields — five of them non-null on every row observed, the sixth
  (`publicSettings`) null unless the notebook is actually public — and
  `ShareStatus.from_api_response` read two of them; the parser's own docstring
  described slot 2 as the bare literal `1000` without naming it.
  `ShareStatus.max_individuals_share_limit` (`maxIndividualsShareLimit`, proto
  tag 3) is the per-notebook collaborator cap the backend enforces — without it
  a bulk-share caller discovers the ceiling only as a failed RPC — and
  `ShareStatus.is_public_sharing_allowed` (`isPublicSharingAllowed`, tag 4) is
  the tenant/policy gate on making a notebook public. Both reach the MCP and
  REST share views and `notebooklm share status` (`--json` and the human table,
  which now warns when policy forbids public sharing).

  **Both are tri-state on purpose.** `None` means "the response made no claim"
  and is deliberately not collapsed into `False` / `0`: a cap of `0` reads as
  "you may add no collaborators" and a `False` reads as a policy denial, so
  fabricating either from silence would be worse than reporting nothing.
  Callers must test `is_public_sharing_allowed is False` for the deny case.
  Drifted types fail closed the same way — a `bool` in the cap slot does not
  decode as a limit of 1 (`bool` is an `int` subclass), and a truthy non-bool in
  the policy slot does not coerce into a verdict.

  **`set_public` deliberately does not consult the gate.** Pre-checking it would
  add an RPC round-trip to every call and change a public method's failure mode
  on the strength of a consequence that was **not** observed — the audit records
  the silent-no-op as *plausible*, and no tenant with public sharing disabled
  was available to exercise it. The field is surfaced so callers can decide with
  the evidence in hand.

  **Tags 7 and 8 stay unread.** They are populated on every live row
  (`[3, true, true]` and `false`), but the recovered mobile
  `GetProjectDetailsResponse` declares only tags 2-4, so nothing names them.
  Surfacing them would mean inventing a field name — the exact defect class
  `tests/_guardrails/_wire_contract.py` exists to catch — so they are recorded
  there as deliberately-undecoded instead. That file now also scans
  `_types/sharing.py`, which used bare integer literals and therefore made no
  checkable positional claim at all; its four slot reads are now named constants
  validated against the proto.

  **Evidence:** live-confirmed on 10/10 notebooks in a 2026-08 sweep, every one
  returning `1000` and `true` with an identical 8-slot shape. The `False` branch
  of `is_public_sharing_allowed` has never been observed on the wire; only its
  decoding is pinned by tests.
  ([#2130](https://github.com/teng-lin/notebooklm-py/issues/2130))
- **`NotebookLMClient(..., import_research_timeout=)`** gives the
  `IMPORT_RESEARCH` RPC the dedicated read-window knob `chat_timeout` already
  had ([#2205](https://github.com/teng-lin/notebooklm-py/issues/2205)). Unset it
  keeps the batch-scaled window (60 s + 3 s per requested source, capped at
  240 s) floored at the client's `timeout=`; a number replaces both; `None`
  inherits `timeout=` verbatim — the same three-way reading as `chat_timeout`.
- **Chat answers are now selected by the server's own end-of-stream marker, and
  carry the turn's identifier.** `GenerateFreeFormStreamedResponse.isFinalResponse`
  (proto tag 5 → `inner_data[4]`) is `true` on exactly the last chunk of every
  observed stream and was read nowhere; the parser instead picked the answer
  with a **longest-wins heuristic** over the chunks. That heuristic is correct
  for the streams observed but is an inference standing in for a boolean the
  server already sends, and it fails **silently** whenever the final chunk is
  not the longest — a truncated or corrected final chunk, a stream ending on a
  short closing statement — returning a plausible mid-stream chunk as the
  answer. A marked answer chunk that also carries `isFinalResponse` now wins
  outright; longest-wins is demoted to a fallback that logs a `WARNING` when it
  fires. The answer marker still decides what counts as an answer at all, so
  the unmarked-text fallback and its drift diagnostics are unchanged.

  `AnswerResponse.conversationTurnKey` (tag 3 → `answer_row[2]`) is populated
  on every chunk of every ask and was likewise unread — which is the reason
  `SubmitFeedback` (whose request takes exactly this key) could not be built
  from a chat result without a separate round trip. It is now decoded onto the
  new `AskResult.turn_key` (`ConversationTurnKey | None`), visible through the
  Python API, `ask --json`, the MCP `ask` tool and `POST /v1/notebooks/{id}/chat`.

  **`turn_key.session_id` is not a conversation id.** It is the same wire slot
  issue #659 established is a *per-stream* identifier, and the evidence about
  what it now holds is mixed: a live two-turn probe saw the `hPTbtc`-resolved
  conversation id there, while this repo's own recorded cassettes show it
  differing from the recorded `hPTbtc` id in 4/4 chat captures. So it is
  exposed under its **proto** name with nothing claimed for it, and `ask` still
  resolves the `conversation_id` it returns through `hPTbtc`. `turn_id` (tag 2)
  conversely does *not* take its proto name `conversationId`, which contradicts
  every observation — it changes on each turn of one conversation. `turn_code`
  (tag 3) is carried verbatim and not interpreted. The wire↔attribute mapping
  is recorded in `docs/rpc-reference.md` and pinned in
  `tests/_guardrails/_wire_contract.py`.
  ([#2122](https://github.com/teng-lin/notebooklm-py/issues/2122))

- **Research runs now report their mode, timings and discovered-source hints.**
  Five always-populated `POLL_RESEARCH` slots were dropped. `ResearchTask`
  gains `discovery_mode` (`task_info[2]`, the new public `DiscoveryMode` enum —
  the same enum the start params carry, so a poll can confirm a run is deep
  rather than the caller having to remember), `created_at` / `updated_at`
  (`task[3]` / `task[2]`) with a derived `duration`, and `account_id`
  (`task[4]`). `ResearchSource` gains `hint` (`DiscoveredSource.hint`,
  `src[2]`) — the backend's own one-line explanation of why it surfaced a
  source. Also adds `notebooklm.types.discovery_mode_to_str`.

  **The issue's create/update labels were backwards, and the wire settled it.**
  #2122 records `createTime task[2]` / `updateTime task[3]`. Polling one live
  run twice, 7.6s apart, showed `task[2]` advancing while `task[3]` held the
  value both slots shared on the first poll — reproduced on two accounts, and
  corroborated by 9/9 task rows across the repo's own cassettes. This client
  decodes `task[3]` as created and `task[2]` as updated.

  **`account_id` is account-scoped, and that took two accounts to establish.**
  Every cassette in this repo carries the same value, which cannot distinguish
  "account id" from "constant". Two live accounts produced two distinct values,
  each constant across every task and poll of that account. What is *not*
  established is whether it names the run's starter or the notebook's owner —
  both were the same account in both probes, and the docstring says so.

  The task-level fields stay off `ResearchTask.to_public_dict()` so the CLI
  `--json` shape remains byte-stable (matching `status_code` / `source_type`);
  the MCP `research_status` tool and `GET /v1/notebooks/{id}/research/{run_id}`
  surface them as `discovery_mode` (a string label) / `created_at` /
  `updated_at` / `duration_seconds`. The per-source `hint` rides the existing
  conditional-key convention on `ResearchSource.to_public_dict()`, so it
  reaches the CLI, MCP and REST source payloads alike.
  ([#2122](https://github.com/teng-lin/notebooklm-py/issues/2122))

- **`notebooklm research import` — importing research sources no longer requires
  a blocking wait** ([#2206](https://github.com/teng-lin/notebooklm-py/issues/2206)).
  The MCP tool (`research_import`) and the REST route
  (`POST /v1/research/{run_id}/import`) had exposed the standalone import for a
  while; the CLI's only door was `research wait --import-all`, and `--no-wait`
  with `--import-all` was rejected outright — so the one route out of blocking
  pointed at another waiting command. The new command imports an
  **already-completed** run and **fails fast** (exit 1, with the reason) when the
  run is still in flight, which is the whole point of it: a caller who wants to
  own their polling cadence can now express the flow end to end.

  ```bash
  notebooklm source add-research "AI safety" --mode deep --no-wait
  notebooklm research status      # your loop, your interval
  notebooklm research import      # imports, returns; never waits for the run
  ```

  It also closes the idempotency gap the CLI's bespoke import path had: a repeat
  import now reports `0 new, N already present` (each with the *existing*
  source's `{id, title, url}`) instead of looking like a no-op, matching what the
  MCP tool already returned. The dedupe is by URL against a pre-import snapshot,
  so a deep run's report row (no URL) is re-imported each time and a failed
  snapshot skips the filter — both pre-existing properties of
  `import_sources_with_verification`, now stated in `--help` and the CLI
  reference rather than left implicit. (Not REST: that route deliberately stays on the
  one-shot `import_sources` so a web request cannot block on a multi-minute
  reconcile loop, and returns no `already_present` split. The *importable-state
  ladder* is shared by all three adapters; the idempotent importer is shared by
  MCP and the CLI.) Options: `--run-id` (default: the notebook's
  single unambiguous run — with several runs in flight it errors rather than
  guessing, so pin the id), `--cited-only`, `--max-sources`, `--allow-duplicate`, `--timeout`,
  `--json`. The import RPC itself is retried with reconciliation (it commonly
  outlives one client timeout on deep payloads); `--timeout` bounds that, using
  the same vocabulary as `research wait --timeout`.
  No new core logic — it drives the same `_app` importable-state ladder all three
  adapters drive (so the guard cannot fork a third time) and the same idempotent
  importer the MCP tool drives.

- **Citations can now be aligned to both the answer and the source.** The two
  halves of that mapping were each missing a piece, and neither was usable
  without the other.

  On the **source** side ([#2128](https://github.com/teng-lin/notebooklm-py/issues/2128)),
  `GET_SOURCE` returns a structured `TailwindDoc` tree and this client kept only
  the strings in it — discarding headings, list structure, and every character
  offset, then joining the remainder with `"\n"` separators the backend's
  offsets never accounted for. `SourceFulltext.document` is now that tree
  parsed: `StructuredDocument` with typed `DocumentBlock` / `TextSpan` /
  `DocumentAnnotation` nodes, heading levels, ordered/unordered list info with
  glyphs and ordinals, per-run bold/italic/underline/link styling, and the
  absolute character ranges everything indexes. Table cell text is walked too,
  so an infobox no longer leaves a hole in the coordinate space.
  `SourceFulltext.content` is untouched, byte for byte, and no CLI / MCP / REST
  payload changes shape. There was no cheaper route: a `grpcio` sweep across 17
  sources and 10 content types found `plainText` and `markdownString` absent
  17/17 on the mobile transport too, so the first-party Android client parses
  this same tree.

  On the **answer** side ([#2120](https://github.com/teng-lin/notebooklm-py/issues/2120)),
  the answer's own `responseDoc` carries an annotation map — `Body`'s
  `inlineObjectLocations` — pairing each citation's object id with the range of
  the answer it supports. Nothing read it. `AskResult.answer_document` now
  exposes that document, and each `ChatReference` carries
  `answer_anchor_start` / `answer_anchor_end`, joined **by object id** so a
  skipped malformed citation cannot shift an anchor onto its neighbour. They
  resolve against `answer_document`, not `AskResult.answer` — the answer string
  additionally carries markdown emphasis and the inline `[N]` markers the
  document does not — and they are named "anchor" because the backend commonly
  anchors a citation at its marker's zero-width insertion point rather than over
  a span. `answer_document` is omitted from the MCP / REST / CLI `--json`
  envelopes, which would otherwise roughly double in size; the per-reference
  offsets survive the trim.

  `StructuredDocument.text` is laid out at the backend's offsets, so
  `slice(n, m)` is by construction what the backend meant by `[n, m)`.
  Positions the document occupies but whose text this client cannot render — an
  image, a rule, and for now a code block — are filled with `"\ufffc"` rather
  than collapsed, because collapsing them is exactly how the flat rendering lost
  the coordinate space in the first place.

  Those offsets are **UTF-16 code units**, the JavaScript convention, not Python
  code points: 22 of 22 spans in the captured answer document match their
  declared width in UTF-16 units where only 21 match in characters, the
  divergence being the span that opens with an emoji. `slice()` translates;
  `notebooklm.types.utf16_len()` is exported for callers doing their own offset
  arithmetic. Indexing `text` directly is off by one position per astral
  character, which is why `slice()` is the sanctioned accessor.

  A repo-wide sweep for the same defect class — every `len(` in
  `src/notebooklm` co-occurring with an offset concept — found no further
  instances: the remaining hits are list-length bounds checks on raw wire
  arrays, list pagination, and one self-consistent local string manipulation.
  The one thing it did surface is not a defect in either component but a
  composition hazard between them, filed as
  [#2211](https://github.com/teng-lin/notebooklm-py/issues/2211): `source
  read --offset` windows `SourceFulltext.content` in Python characters, so it
  is not interchangeable with a citation's `start_char` — different string,
  different unit. The affected docstrings now say so explicitly.

  Both surfaces are additive: new fields at the end of their dataclasses, empty
  (never `None`) when a response carries no decodable document.

- **The stored quiz/flashcards option pair can now be read back off the wire**
  ([#2195](https://github.com/teng-lin/notebooklm-py/issues/2195)).
  `ArtifactRow.quiz_options` / `ArtifactRow.flashcards_options` decode the
  `[quantity, difficulty]` pair the backend echoes at `data[9][1][7]` /
  `data[9][1][6]`, returning a named `QuizOptionPair` rather than a positional
  tuple — a bare 2-tuple would reintroduce on the decode side exactly the
  ambiguity that produced the transposition on the encode side.

  This closes the structural gap behind [#2116](https://github.com/teng-lin/notebooklm-py/issues/2116)
  and [#2117](https://github.com/teng-lin/notebooklm-py/issues/2117): nothing in
  the client read these options back, so the only thing between a wrong pair and
  a user was a fixture written from the same belief as the code. The e2e
  generation tests now assert the server's echo against what they sent, and
  every option pair in that tier is asymmetric — `MORE` and `HARD` are both `3`,
  so `more` + `hard` is as blind as `standard` + `medium`.

### Changed

- **`resolve_chat_reference_passage()` returns a rendered passage on the
  offset path.** For a source whose document decoded, the passage is now a
  window of the readable rendering (blocks separated, runs joined) rather than
  a window of the flat `content` (every run on its own line) — same text, laid
  out to be read, and located exactly instead of by prefix search. A source
  with no decodable document keeps the previous output byte for byte, since
  the search fallback is unchanged. Callers matching on the returned string's
  exact line breaks should switch to `document.slice(ref.start_char,
  ref.end_char)`, which returns the range and nothing else. The
  `ChatResponseParseError` message also changed: it now names only what was
  actually attempted, so it distinguishes a range that no longer fits its
  source from a citation that never carried one.
  ([#2211](https://github.com/teng-lin/notebooklm-py/issues/2211))
- **Passing a non-enum `quantity` / `difficulty` to `generate_quiz()` /
  `generate_flashcards()` now raises `ValidationError`**
  ([#2196](https://github.com/teng-lin/notebooklm-py/issues/2196)). Previously a
  bare `int` reached `.value` and produced `AttributeError: 'int' object has no
  attribute 'value'`. The check also rejects the *other* option enum:
  `quantity=QuizDifficulty.HARD` used to encode silently as `MORE`, since both
  are `3`. No working call changes — neither ints nor strings were ever accepted.

  The same issue asked whether `None` should mean "let the server choose". It
  does not, and now says so: `None` resolves to `STANDARD` / `MEDIUM` and is
  sent **explicitly**. Omission *is* accepted by the backend (live-probed:
  generation completes normally), but the stored options then echo back as
  `null`, so what the server picked is invisible to the caller and to the new
  read-back above. That trade — a value we can name, echo and assert against one
  we cannot see — is now a documented decision with a test, not an inline `else`.

- **The CLI's `--quantity` / `--difficulty` choice lists are derived from the
  enum maps** instead of hardcoded
  ([#2197](https://github.com/teng-lin/notebooklm-py/issues/2197)). The MCP and
  REST copies were already pinned to `_QUIZ_QUANTITY_MAP` / `_QUIZ_DIFFICULTY_MAP`
  by tests; the CLI had no such binding, so a new `QuizQuantity` member would
  have become reachable via MCP and REST while staying silently absent from the
  CLI with nothing red. Same choices, same order, no user-visible change today.

- **`ChatReference.cited_text` now returns a different value for the same
  citation.** This is the point of the change rather than a side effect, and it
  is worth stating on its own because **no signature-level compatibility audit
  can see it**: the field's type, name and position are unchanged, and only its
  contents move. Previously it held the cited fragment's *first block*; it now
  holds the whole fragment — 42 characters became 560 on the live capture that
  motivated the fix, and 16 became 3,927 on one citation in this repo's own
  `chat_ask_with_references` cassette. Anything that stores, hashes, diffs,
  de-duplicates or persists `cited_text` as a key will see different data from
  identical input, and any golden file recording it needs refreshing.

  Two related facts a caller should not have to rediscover:

  - **The offsets are UTF-16 code units**, the backend's own unit, not Python
    characters. They differ on any text outside the Basic Multilingual Plane —
    one emoji costs one position, and every offset after it shifts.
    `notebooklm.types.utf16_len()` is exported for callers doing their own
    offset arithmetic, and `document.slice()` does the conversion.
  - **`len(cited_text)` is not a reliable width.** It usually equals
    `end_char - start_char` and is not guaranteed to: it counts Python
    characters against a UTF-16 range, and it omits positions this client does
    not render as text. Take the width from `end_char - start_char`, or measure
    with `utf16_len()`.

  `ChatReference.answer_start_char` / `answer_end_char` keep returning exactly
  the values they always did — see **Fixed** below for why their *names* were
  wrong — so that pair is unaffected by this.

### Fixed

- **A missing master-token dependency no longer masquerades as revoked
  credentials.** When `gpsoauth` is absent, the master-token recovery rung now
  raises `MissingDependencyError` with the existing
  `pip install "notebooklm-py[headless]"` remedy. It bypasses the private mint,
  bootstrap, and `MasterTokenError` translations. MCP/REST callers reach the
  shared `DEPENDENCY` classification, while the CLI reports its actionable
  configuration error instead of a generic unauthenticated failure. An
  installed dependency whose exchange rejects a token still follows the
  unchanged `MasterTokenError`/rung-declined path.
  ([#2239](https://github.com/teng-lin/notebooklm-py/issues/2239))

- **Interrupted legacy-account promotion now self-heals, including its privacy
  scrub.** The detached migration remains single-flight while a worker is
  active, keeping the 90-second storage lock and 10-second `context.json` lock
  off the caller and event-loop threads. Once a worker settles, however, its
  path is retryable on a later read instead of being poisoned for the rest of
  the process. Reads also detect an already in-band account with a stale legacy
  sibling — the state left by a process dying between the durable write and
  scrub — and schedule cleanup without overwriting the in-band winner. The
  bounded exit drain remains diagnostic rather than pretending its 30-second
  default can cover 90 seconds of legitimate lock contention; an unfinished or
  failed attempt is retried on the next read/run.
  ([#2228](https://github.com/teng-lin/notebooklm-py/issues/2228))

- **`notebooks.create()` could hand back a notebook it did not create.** All
  four probe-then-create paths snapshot the resource list *before* the create so
  their probe can tell the row they just made from one that was already there.
  Three of them degrade a *failed* snapshot to `None` — "baseline unavailable" —
  and refuse to attribute any match after that. `notebooks.create` degraded to
  an empty `set()` instead, which reads as *"nothing existed"* and disables the
  filter outright: with a failed baseline and a single pre-existing notebook of
  the same title, that notebook passed the "is it new?" test and was returned as
  freshly created. Titles are not unique on this backend — two `create()` calls
  with one title yield two distinct notebooks — so nothing else could catch it,
  and the ambiguity guard only fires above one match. The caller then held an id
  it believed was a new empty notebook, and every later `sources.add_*` /
  `chat.ask` in that session wrote into someone else's.

  It was documented in place as an accepted limitation, written before the
  sentinel the source paths adopted in
  [#2113](https://github.com/teng-lin/notebooklm-py/issues/2113) /
  [#2204](https://github.com/teng-lin/notebooklm-py/issues/2204), which left
  `notebooks.create` the only one of the four still guessing. It now uses the
  same sentinel and raises `RPCError` — marked `unconfirmed`, naming the
  ambiguous rows — on any match it cannot attribute. The gate is on *matches*,
  not on the sentinel itself: a probe that lists cleanly and finds no notebook
  with the title has answered the question, so an unavailable baseline still
  never fails a create that would otherwise succeed.

  The trade is deliberate and is the same one #2204 made: refusing to attribute
  can cost a duplicate create, which is loud, visible in the notebook list, and
  diagnosable, where the silent wrong identity it replaces gave the caller
  nothing to act on and no reason to look. Reaching either outcome still takes a
  failed baseline `list()`, a commit-lost create, and a working probe `list()`
  at once. ADR-0005 named this as the one gap
  [#2220](https://github.com/teng-lin/notebooklm-py/issues/2220) left open and
  now records the closed rule.
  ([#2232](https://github.com/teng-lin/notebooklm-py/issues/2232))
- **A table in the readable rendering no longer runs its cells together.**
  `StructuredDocument.render()` — and through it
  `SourceFulltext.rendered_content` and the passage
  `resolve_chat_reference_passage()` returns — rendered a table as one line
  with its cell values adjacent: `"Operating systemAndroid 10+, iOS 17+
  [a]Included withGemini…"` on this repo's own captured infobox. A table now
  renders as **one line per row, cells tab-separated**.

  The boundary was being *computed and discarded*. The parse walks rows, then
  cells, then the blocks inside each cell, and flattens every run it finds into
  one span sequence — which is what the offset-faithful layout needs, and which
  loses the one thing a reader needs. Nothing downstream could recover it: a
  single cell is several runs (`"Android 10"`, `"+, "`, `"iOS 17"`, `"+ "`,
  `"[a]"` are one cell on that capture), so separating *runs* would have split
  cells as often as it separated them. `DocumentBlock` now carries the cell
  ranges the walk already computed, as the new `table_rows` (a tuple of rows of
  `TableCell`), and only `render()` reads them.

  **No offset moves.** The boundaries are carried as *offsets, never as
  characters*: `StructuredDocument.text` — the coordinate space every citation
  offset indexes — is byte-identical, and so are `DocumentBlock.text`,
  `ChatReference.cited_text`, `slice()`, `extent`, and the legacy
  `SourceFulltext.content`. The tab exists in the readable rendering alone,
  alongside the newline that rendering already inserted between blocks, and
  that rendering was never offset-addressable. Both invariants are pinned by
  tests against the captured fixture.
  ([#2230](https://github.com/teng-lin/notebooklm-py/issues/2230))

- **A file whose processing fails after a clean upload no longer reports a
  timeout.** When bytes transfer fine and NotebookLM's server-side processing
  *then* fails, the source settles at `status=ERROR`. For audio and
  still-unclassified sources (`type_code` 10 / 0 / `None`) a brief ERROR is
  treated as transient — the poll waits it out rather than failing fast, which
  is right, because those types really do report ERROR mid-transcription. But
  the tolerance had no bound other than the deadline: `wait_until_ready` kept
  polling to the timeout and raised `SourceTimeoutError`, so a genuine terminal
  failure was reported as *"still working, ask again later"* and nothing
  anywhere named it. Live-reproduced with a `.wav` upload (WAV is not a
  supported audio container; `.mp3` and `.m4a` are), which is exactly the shape
  that hits it.

  Tolerating a transient error is a hypothesis, and *sustained* ERROR right up
  to the deadline is what disproves it: the source did not fail to answer — it
  answered ERROR, over and over, until we gave up. `wait_until_ready`,
  `wait_until_registered`, and `wait_all_until_ready` now resolve that case as
  `SourceProcessingError`
  instead, naming the elapsed budget so *"ERROR throughout a 120s poll"* and
  *"ERROR on the last tick of a 2s poll"* stay distinguishable. A source still
  legitimately `PROCESSING` at the deadline is unchanged — still a timeout, so
  slow sources are not relabelled as broken — and neither is a source seen in
  ERROR only *once*. The conversion requires two consecutive ERROR observations
  ending at the deadline, because a `timeout` is caller-chosen: a short one can
  buy a single look at a source that is legitimately mid-transcription, and
  calling that terminal would be a fabricated verdict, the mirror image of the
  bug being fixed. The streak resets on any non-ERROR look, so a source that
  polled PROCESSING and flipped to ERROR on the final tick still times out.
  Both types are `SourceError` and
  every consumer already handles them side by side (`_app`'s wait outcomes, the
  MCP `source_wait` buckets, `notebooklm source wait`'s exit codes), so the
  `.wav` case moves from the "timed out, retry" bucket to "failed".
  ([#2138](https://github.com/teng-lin/notebooklm-py/issues/2138))

- **An idempotency probe that cannot answer no longer retries the create.**
  All four `PROBE_THEN_CREATE` paths — `notebooks.create`, `sources.add_url`,
  `sources.add_drive`, and `register_file_source` — ran their probe's `list()`
  inside a `try`, propagated transport failures, and swallowed everything else
  with a log and a `return None`. But `None` means "no match", which
  `idempotent_create` reads as *evidence the create did not land* and acts on by
  re-issuing it. A drifted `GET_NOTEBOOK` / `LIST_NOTEBOOKS` makes the strict
  decoder raise `RPCError`, which is indistinguishable from "not committed" — so
  the create was repeated on no evidence, and these variants run with
  `disable_internal_retries=True` precisely because a blind re-POST can
  duplicate. The probe failing is the moment its protection matters most.

  A probe now returns `None` only when it has affirmatively established that no
  matching resource exists, and raises when it cannot: `SourceAddError` on the
  three source paths, `RPCError` on `notebooks.create` — in both cases the type
  that path's existing "cannot disambiguate" branch already raises, so no
  caller's `except` clause changes meaning. The decode failure is retained as
  `cause` / `__cause__` and the transport failure that triggered the probe
  further up the `__context__` chain, so both halves stay in the traceback.

  The error also carries an **`unconfirmed`** marker attribute — read it with
  `getattr(exc, "unconfirmed", False)`. It is the supported way to tell "the
  write may have committed" from "the write was rejected", and it is load-bearing
  rather than decorative: without it, `_app.errors` classified these as
  `SOURCE_ADD` ("invalid input, fix and retry", REST 422, and **non-fatal** in a
  batch add, so a drifted backend turned one unconfirmed write into one per
  item) — or, when the probe's own failure happened to carry a 5xx/gRPC-14
  `rpc_code`, as the *retriable* `SERVER` with the hint "retry after a short
  delay", advertising exactly the retry the message forbids. They now classify as
  `RPC`: fatal in a batch, not retriable, no contradicting hint. The MCP
  browser-upload route no longer reports "Your file uploaded" for one either —
  nothing had been uploaded. Messages are also front-loaded with the action,
  because MCP and REST truncate at 300 characters and were cutting it off.

  **New on the surfaces you read.** Classification alone did not reach them, and
  each was still printing the opposite advice:

  - **CLI** — `handle_errors` dispatches on exception *type*, and a probe
    re-raises its transport/auth failure unchanged, so a marked `RateLimitError`
    was rendered as `Error: Rate limited. Retry after 30s.` with
    `"code": "RATE_LIMITED"` and `"retry_after": 30`. For an unconfirmed create
    the code is now **`UNCONFIRMED_WRITE`**, the message is replaced rather than
    annotated (the branch's own text *is* the retry instruction), `retry_after`
    is dropped, and a "check before retrying" note is added. Scripts keying on
    `RATE_LIMITED` no longer back off and retry a write that may already exist.
  - **MCP and REST** — both JSON payloads now carry **`"unconfirmed": true`**
    and an overriding `hint`. Without it these arrived as an opaque message
    (often a bare connection error) plus `retriable: false`, with nothing
    indicating a source may already exist.

  The wording is deliberately *"no further attempt was made"*, not "it was not
  retried": on the two-attempt path a first probe may have returned "no match"
  and let one retry through before a second probe failed, so an operator
  reconciling on the stronger claim would look for one row and stop.

  **This is a deliberate trade, not a free win.** A decode blip on a create that
  never landed now surfaces as a hard failure the caller must retry by hand,
  where before it recovered silently. It is accepted because the outcomes are
  not symmetric in *detectability*: a caller told "unresolved, go look" can act,
  while a caller handed a silent duplicate — or, on `add_url`, the wrong source
  id — has nothing to act on and no reason to look. Swallowing also converted a
  `PROBE_THEN_CREATE` operation into an at-least-once one at the moment its
  guarantee was load-bearing, and this codebase makes at-least-once an explicit
  opt-in (`AT_LEAST_ONCE_ACCEPTED`, which emits a rate-limited WARN so the
  trade-off stays visible). The rule and its rationale are now in ADR-0005.
  ([#2220](https://github.com/teng-lin/notebooklm-py/issues/2220))

  The *pre-create baseline* read deliberately still degrades rather than
  failing — it runs before anything is written, so proceeding is safe and
  refusing would break adds that would otherwise succeed. What changed there in
  this release is only the log level: `register_file_source` and
  `notebooks.create` swallowed at `DEBUG`, and the `notebooklm` logger defaults
  to `WARNING`, so the record was discarded before any handler saw it and the
  call ran with a degraded probe in silence. Both now log at `WARNING` and name
  the failure type, matching what `add_url` / `add_drive` got in
  [#2204](https://github.com/teng-lin/notebooklm-py/issues/2204). What the
  degraded baseline degrades *to* is a separate question, settled for
  `notebooks.create` by
  [#2232](https://github.com/teng-lin/notebooklm-py/issues/2232) above.

- **A configured `timeout=` was silently overridden for chat and
  IMPORT_RESEARCH.** ([#2205](https://github.com/teng-lin/notebooklm-py/issues/2205))
  Both RPCs carry a longer-than-default built-in read window — 180 s for chat,
  and the batch-scaled 60 s + 3 s/source (capped at 240 s) added for
  IMPORT_RESEARCH in [#2187](https://github.com/teng-lin/notebooklm-py/issues/2187)
  — and each *replaced* the client's `timeout=` outright rather than composing
  with it. `NotebookLMClient(auth, timeout=600)` therefore still got 180 s on
  chat and at most 240 s on IMPORT_RESEARCH, with no opt-out short of
  monkeypatching internals. Those built-ins are now defaults rather than caps:
  they only ever lengthen the configured budget, so that client gets its 600 s
  everywhere. Batch scaling is unchanged for a default client. An explicitly
  passed `chat_timeout=` still wins outright — including a value *below*
  `timeout=`, so deliberately fast failure stays expressible — which is why the
  kwarg now defaults to an internal sentinel instead of the literal `180.0`;
  every existing call passing a float or `None` behaves exactly as before. A
  new `import_research_timeout=` kwarg gives IMPORT_RESEARCH the same dedicated
  knob `chat_timeout` already had. Separately, an IMPORT_RESEARCH attempt made
  by `import_sources_with_verification` is now clamped to what remains of that
  call's own `max_elapsed` budget, so a late retry can no longer be granted
  minutes of window against a budget with seconds left, and the loop stops
  rather than sending an attempt too short to observe its own result. The clamp
  bounds what an attempt is given, not the wall-clock duration of one already in
  flight — the read slot is an inactivity limit, not a request deadline.

- **A mistyped `.pptx` / `.ppt` filename was ingested as pasted text with no
  warning** ([#2202](https://github.com/teng-lin/notebooklm-py/issues/2202)).
  `source add` warns when an argument looks like a file but does not exist —
  otherwise `source add dekc.pptx` silently creates a source whose entire content
  is the string `dekc.pptx`. That check ran against a hand-maintained extension
  list which PowerPoint never joined, even after it became a first-class decoded
  type in [#2137](https://github.com/teng-lin/notebooklm-py/issues/2137) /
  [#2191](https://github.com/teng-lin/notebooklm-py/issues/2191), so a typo'd
  deck was one of the cases that got no warning.

  *Correcting the issue report:* #2202 states that a bare `deck.pptx` was not
  uploaded at all. It was — auto-detection checks `Path(content).exists()`
  **before** the extension list, so an existing file has always been uploaded
  whatever its name (live-verified on `main`: a bare `deck.pptx` reached READY
  and decoded as `powerpoint`). The extension list only governs the
  does-not-exist warning. The consolidation below stands; the user-visible
  symptom was the smaller one.

  Adding two extensions would only have deferred the drift, so the list is gone:
  the path heuristic, the Drive download-and-upload router, and the upload
  HTML-reject gate now all derive from **one** declaration that sits beside the
  source type-code map, and a guardrail pins the derivation. Two real drifts fell
  out of consolidating: `.pptx` is now accepted by the **Drive
  download-and-upload router** (which had refused it outright), and
  `.xhtml`/`.xht` now earn the same missing-file warning.

  `.ppt` is deliberately *not* claimed uploadable. The two sets have asymmetric
  failure modes — a wrong entry in the path heuristic costs a spurious warning,
  a wrong entry in the Drive router turns a fast client-side refusal into a full
  download plus a murky server-side failure — so an extension has to earn the
  upload set. The live evidence covers `.pptx` (OOXML) only; legacy `.ppt` is a
  different container and was never put on the wire, and renaming a `.pptx`
  would prove nothing. It therefore sits in a third, explicitly-labelled
  "file-shaped, not upload-verified" set: a mistyped `deck.ppt` still gets its
  warning, and the Drive router still refuses it up front.

- **MCP `source_add` went silent about a title miss whenever you passed
  `wait=true`** ([#1989](https://github.com/teng-lin/notebooklm-py/issues/1989)).
  An explicit `title` for a url/youtube/drive add is honored by a best-effort
  post-add rename, and the immediate response flags a rename that did not stick
  with `title_override_applied: false` plus a warning. The `wait=true` tail
  returned the `source_wait` aggregate and dropped the signal entirely — the
  same add reported the miss or not depending only on whether you waited. It now
  carries the same pair, checked against the **final** title from the
  post-processing re-read (so a title the backend reverts after the add is
  caught too, not just a rename RPC that failed). Only READY outcomes are
  judged; a timed-out or failed source has no final title to judge. Both tails
  now share one predicate rather than two copies.

- **`SourceFulltext.find_citation_context()` could not locate a multi-block
  citation.** Its search key is a prefix of `cited_text`, and once `cited_text`
  spanned a whole fragment (below) that key crossed block boundaries — where
  `content` has a `"\n"` the citation does not, so the key could not occur in
  the string being searched. For a source whose first block is a short heading
  the key crossed the very first boundary, so `resolve_chat_reference_passage`
  raised `ChatResponseParseError` for a perfectly valid citation. The key is now
  capped at the first block boundary as well as at 40 characters, restoring the
  property that held before — when `cited_text` was first-block-only, the key
  could not straddle a join. `cited_text` itself is unchanged: making its
  *value* wrong to satisfy a value-based consumer would reintroduce exactly the
  separators #2128 removed. Resolving by offset against `document` needs no
  search at all and is tracked as
  [#2211](https://github.com/teng-lin/notebooklm-py/issues/2211).

- **Saved-from-chat notes miscounted their citation offsets on non-ASCII
  answers.** `build_save_chat_as_note_params` sized its `TailwindDoc` ranges —
  the cited passage's local span, the cleaned-answer span, and every `[N]`
  marker anchor — with `len()`, i.e. Python characters, where the backend
  counts UTF-16 code units. One emoji anywhere in an answer ended each
  subsequent range a unit short, misaligning the saved note's hover anchoring.
  Latent before this change and newly reachable after it, because `cited_text`
  now spans the whole cited fragment rather than its first block.

- **The fixture secrets scan now covers `tests/unit/fixtures` too.** CI swept
  `tests/fixtures` with `check_cassettes_clean.py --secrets-only` but not the
  sibling directory, so a live-captured payload committed there could carry
  credential-shaped material unchallenged — which is exactly what the first
  such payload in this change did, until it was scrubbed. Both directories are
  now scanned, in CI and in the `pytest` mirror of that step.

- **`ChatReference.cited_text` was truncated to the first block of each
  citation.** `Citation.fragment` is a *message* whose `elements` list sits one
  level below it, and the decoder stopped at the message — so the passage loop
  iterated a single wrapper and returned whatever the fragment's first block
  held. On the live capture that motivated the fix, 37 characters of an
  available 556; on this repo's own `chat_ask_with_references` cassette, one
  citation went from 16 characters of a 3,927-character range to the whole of
  it. `cited_text` is now the fragment's text verbatim and `start_char` /
  `end_char` span the whole fragment. (Verbatim, not padded: where a length
  guarantee matters, `document.slice(start_char, end_char)` gives one.)

  The fix had to be two-level. Descending to the elements while still unwrapping
  each one's `[0]` — the pre-fix `PassageRow` behaviour — reads an element's
  `startIndex`, an `int` where a nested record is expected; every element then
  fails well-formedness and `cited_text` becomes `None` for *every* citation,
  strictly worse than the truncation. `tests/unit/test_citation_alignment.py`
  pins that trap against the real fragment. The per-passage adapters
  (`PassageRow`, `TextLeafRow`) are retired in favour of the shared document
  adapters, which decode the same `StructuralElement` rows the source path uses.
  ([#2120](https://github.com/teng-lin/notebooklm-py/issues/2120))

- **`ChatReference.answer_start_char` / `answer_end_char` were documented as
  answer-text positions and are not.** They are the cited fragment's
  **source-side** range, the union of the fragment's own element ranges. A live
  capture reported `[1130, 1695]` for the third citation of a 536-character
  answer, so following the docstring and slicing the answer indexed off the end.
  (The slot is `Citation`'s protobuf tag 4, which the recovered schema does not
  name; the meaning comes from live capture.) The fields are renamed to `fragment_start_char` /
  `fragment_end_char`; the old names remain as deprecated aliases through v1.0,
  returning exactly the values they always did (see
  [`docs/deprecations.md`](docs/deprecations.md)). The genuinely answer-side
  range is the new `answer_anchor_*` pair above.
  ([#2120](https://github.com/teng-lin/notebooklm-py/issues/2120))
- **A rejected artifact generation now reports the server's reason instead of
  "generation is unavailable".** A live probe (2026-08-13) showed
  `CREATE_ARTIFACT` answering an Audio Overview request on a source-less
  notebook with `[["wrb.fr","R7cb6c",null,null,null,[3],"generic"]]` — an
  explicit `google.rpc.Status` of `INVALID_ARGUMENT`. The `allow_null=True`
  decode returned `None` *before* consulting that status, so the client
  discarded what the server said and substituted its own guess,
  `ArtifactFeatureUnavailableError("Audio generation is unavailable")`. Callers
  can now pass `raise_on_null_status=True` (as `CREATE_ARTIFACT`,
  `RETRY_ARTIFACT` and `REVISE_SLIDE` do — all three live-verified: retry and
  revise answer `[5]` NOT_FOUND for an unknown artifact id) to have a
  status-tagged null raise with the server's code and label. It is opt-in per
  call site rather than a blanket change because three *other* RPCs are
  recorded doing the same thing on flows this client reports as successful —
  `SHARE_NOTEBOOK` and `SHARE_ARTIFACT` answer `[3]`, `REMOVE_RECENTLY_VIEWED`
  answers `[13]` — and only the last of those has ever been reasoned about.
  Whether the two share rejections are benign is an open question this change
  deliberately does not answer; a swallowed status now logs at DEBUG so they
  are findable. A generation rejected with no status at all still raises
  `ArtifactFeatureUnavailableError`.
  ([#2188](https://github.com/teng-lin/notebooklm-py/issues/2188))
- **`google.rpc.Status.message` is no longer discarded.** Index 1 of the
  rejection envelope is the only slot in which the *server* can state a reason;
  nothing in this client read it, on either the `batchexecute` or the streamed-chat
  path. It is now read (defensively — a non-empty string only) and surfaced on
  both. Only the rate-limit message *leads* with it; the decoder's bare-status
  path and both streamed-chat sites APPEND it and keep the client-authored
  guidance, because nobody has seen the slot populated and so there is no
  evidence a server sentence would be as actionable as the advice it displaced. **No captured response has
  ever populated it**, so in practice users still see the client-authored
  wording; this changes what happens if the server ever does explain itself, and
  it records on the wire-contract registry that the slot is unobserved rather
  than unknown. Every other rejection sentence this client prints is
  client-authored, including "API rate limit or quota exceeded" — see
  `docs/rpc-reference.md`.
  ([#2188](https://github.com/teng-lin/notebooklm-py/issues/2188))
- **#239's quota-failure guarantee has a regression test again.** #2134 removed
  the phantom `failed_error_text` read and, with it, the tests that were the
  only guard on "a quota failure surfaces a helpful message" — tests that had
  asserted against a row shape the backend has never emitted. The replacement
  drives the **real** decoder: a recorded `UserDisplayableError` rejection and
  the verbatim live `INVALID_ARGUMENT` body are decoded unmocked and asserted at
  the `generate_audio` boundary, and the surviving behaviour — a late FAILED
  artifact carries no reason, so the user gets the generic
  `"{Type} generation failed"` fallback — is pinned to that fallback.
  ([#2193](https://github.com/teng-lin/notebooklm-py/issues/2193))

### Changed

- **`client.sources.list()` now owns source status/type filtering and exact
  counts.** The existing method accepts keyword-only `statuses` and `types`
  collections (OR within one axis, AND across axes) without an extra RPC or a
  parallel inventory surface. `strict=True` now rejects malformed/id-less rows,
  incomplete type/status discriminants, and conflicting duplicate IDs, so
  `len(await client.sources.list(..., strict=True))` is the exact count of
  unique addressable matches; the default remains tolerant of row-level
  anomalies. Research-import reconciliation explicitly keeps that tolerant
  row mode so a known duplicate-ID collision cannot trigger a non-idempotent
  retry.

- **`notebooklm research wait --timeout` now defaults to 1800 seconds, up from
  300.** Every observed deep run exceeded the old cap (374s live, 358s in the
  `research_deep_poll_long` cassette), so an unattended wait reported a timeout
  on work the backend went on to finish; fast runs settle in seconds, so the
  raised ceiling costs them nothing. The value is a cap, not a wait, and it now
  matches `source add-research`. With `--import-all` it is also the
  import-retry budget, so that budget grows with it.
  ([#2142](https://github.com/teng-lin/notebooklm-py/issues/2142))

### Fixed

- **`sources.add_url` no longer reports success for a create that never
  landed.** Its idempotency probe matched `source.url == url` against the whole
  notebook with no baseline filter, so after a transport failure it could adopt
  a source that was already there and hand the caller that id. A URL is not
  unique within a notebook — the backend accepts the same URL twice and
  `SourceLister.list` dedupes by source id, not by URL — and a live probe on
  this account reproduced the worst shape of it: an `add_url` whose create
  **did** commit (as `df618843-…`) returned the pre-existing `0d2c15a1-…`
  instead, leaving the caller holding the wrong source id *and* an unreported
  duplicate. With `wait=True` the mask is total, since the adopted source is
  already `READY`. Probe matches are now filtered against a snapshot of source
  ids taken before the create — the same shape `add_file` has always had and
  `add_drive` adopted in [#2113](https://github.com/teng-lin/notebooklm-py/issues/2113)
  — and a match the snapshot cannot vouch for, or an ambiguous multi-match,
  raises `SourceAddError` rather than guessing. An unavailable snapshot on its
  own does not: with no matching source there is nothing to be ambiguous about,
  so the call still retries and still ends in the underlying transport error. A probed-but-fresh result now also
  honors a requested `title`, which the pre-fix code skipped because a probe
  match could not be proven to be the caller's own. The probe's non-transport
  failure branch moved from `DEBUG` to `WARNING`: with internal retries
  disabled on this variant it is the last net against a duplicate.
  ([#2204](https://github.com/teng-lin/notebooklm-py/issues/2204))

  **Behaviour change:** `add_url` previously listed sources only inside its
  retry probe (i.e. only after a transport failure); it now takes that snapshot
  on *every* call. That is one extra `GET_NOTEBOOK` per `add_url`, and because
  the backend writes `lastViewedTime` when answering one
  ([#2126](https://github.com/teng-lin/notebooklm-py/issues/2126)), every
  `add_url` now bumps the notebook's position in the web UI's *Recent* list.
  `ADD_SOURCE` alone does not — the same live probe held `last_viewed_at`
  pinned across an `add_url` — so on this, the highest-traffic add path, the
  bump is genuinely new rather than already paid. It is accepted because no
  cheaper probe exists (source ids are published only inside the
  `GET_NOTEBOOK` payload, and `LIST_NOTEBOOKS`, which does not bump, does not
  carry them) and because capturing the baseline lazily on the retry path is
  not merely cheaper but wrong: the probe runs *after* the create, so a list
  taken there already contains the just-created source. The bump does not
  compound across a bulk import, and `wait=True` callers were paying it already
  via `wait_until_ready`'s polling. A caller looping over single-item `add_url`
  still pays one baseline read per item. The REST/MCP URL-batch endpoints used
  that same sequential path in this release; #2115 later moved those already
  batch-shaped endpoints to one true-batch write plus optional reconciliation,
  without changing single-item `add_url`.

- **A failed idempotency baseline is no longer invisible.** `add_url` and
  `add_drive` swallow a failure to snapshot source ids before the create and
  carry on with the probe disabled. That swallow logged at `DEBUG`, and the
  `notebooklm` logger defaults to `WARNING`, so the record was discarded before
  any handler saw it — the call ran without the protection and said nothing.
  (Found by review, and not hypothetical: five of the repo's own VCR tests were
  replaying a cassette that predates the baseline read and silently exercising
  the disabled path.) Both now log at `WARNING` and name the failure type, and
  `add_url`'s resulting ambiguity error carries the underlying exception as its
  `cause` and names the source ids it could not disambiguate, so a caller told
  to "check the notebook source list" knows which rows to look at.
  ([#2204](https://github.com/teng-lin/notebooklm-py/issues/2204))

- **`docs/rpc-reference.md` no longer misstates the research path.** Four
  corrections, each against a captured payload: `START_FAST_RESEARCH` returns a
  one-element response (only deep carries `report_id`); the `1` / `5` start
  params are `DiscoveryMode`, echoed back at `task_info[2]`; the `POLL_RESEARCH`
  sketch now documents the slots it omitted (`task[2]`/`[3]`/`[4]`,
  `task_info[2]`, the deep-only `task_info[5]`, and the deep source row's
  populated `[5]`/`[6]`/`[8]`); and status code `6` is recorded as never
  observed — all three completed runs, deep included, report `2`. The `6 →
  completed` coarsening is kept as forward-compat, and the unit fixtures that
  used `6` to stand for a completed deep run now use the captured `2`.
  ([#2143](https://github.com/teng-lin/notebooklm-py/issues/2143))
### Added

- **The CLI now reports the Drive axis that MCP/REST already had.**
  `Source.drive_document_id` (#2113) and `Source.drive_status` /
  `Source.is_drive_degraded` (#2111) reached the MCP and REST surfaces
  automatically — those serialize the whole dataclass — but `source list` and
  `source get --json` hand-compose their row from the deliberately narrow
  `{"id", "title", "type", "url"}` summary, so both fields were invisible
  there. Both commands now emit `drive_document_id`, `drive_status` (string
  label) and `is_drive_degraded` on **every** row — `null` / `false` on
  non-Drive sources, so a `jq` filter needs no `// empty` guard.

  No raw Drive status code is shipped beside the label, unlike the ingestion
  axis's `status` / `status_id`: it would carry no information (an unmappable
  wire code is replaced by the `UNKNOWN` client sentinel before it could reach
  output) and its code space collides adversarially with `status_id`'s — `2` /
  `3` are `ready` / `error` for ingestion but `syncing` / `active` for Drive.
  That matches the "labels, not raw codes" rule the MCP compact roster already
  applies. MCP/REST keep their `drive_status` (code) + `drive_status_label`
  (string) spelling; both resolve through `drive_source_status_to_str`.

  `source add` / `source add-drive` / `source add-drive-file` JSON envelopes
  are **unchanged**: the Drive keys are composed into the list/get row rather
  than added to the shared summary those envelopes publish. `source list` and
  `source get --json` now build that row from one shared serializer, so the two
  cannot drift apart again — which is how this gap opened.

  Human output gains no column. `source list` appends `(drive: deleted)` to the
  Status cell for any row the backend reports as degraded (`is_drive_degraded`),
  whatever its ingestion status — an unreadable code is not flagged, since it is
  not evidence of degradation. The
  Status column now folds rather than ellipsizes so the longest label survives
  a narrow terminal. `source get` prints `Drive File ID:` / `Drive Status:`
  lines only for a source that carries the corresponding field — the two are
  gated independently, because the only Drive row this project has captured on
  the wire carries a file id with no health slot at all.
  ([#2113](https://github.com/teng-lin/notebooklm-py/issues/2113),
  [#2111](https://github.com/teng-lin/notebooklm-py/issues/2111))

- **Drive-backed sources now report Drive-side health.**
  `SourceSettings.userDriveSourceStatus` (proto tag 4 → `source[3][3]`) is
  populated by the backend on Drive-backed sources and was read nowhere in this
  client, so a source whose Drive file had been deleted, unshared, or was
  mid-sync still reported `is_ready=True` with no signal anywhere in the API —
  `status` (tag 2) describes NotebookLM's own ingestion pipeline, which
  genuinely completes and stays complete after the Drive file goes away. The
  slot is now decoded into the new `Source.drive_status`
  (`DriveSourceStatus | None`), with `Source.is_drive_degraded` as the explicit
  check; the MCP/REST source view gains both a `drive_status_label` string and
  the `is_drive_degraded` verdict, and the `source_list(detail="compact")`
  roster gains the label. `None` means the row made no Drive claim (every
  non-Drive source, plus the backend's `UNSPECIFIED`, which means the same
  thing and is normalized rather than modelled); a code this client cannot map
  decodes to
  `DriveSourceStatus.UNKNOWN` — deliberately distinct from `None` — and warns
  once. Also adds the public `DriveSourceStatus` enum and
  `notebooklm.types.drive_source_status_to_str`.

  **`is_ready` is deliberately unchanged.** Folding Drive health into it would
  flip a public predicate to `False` for sources that report ready today, and
  would turn `wait_until_ready` into a guaranteed timeout for a Drive file that
  can never come back — a worse failure than the silence it replaces. The two
  axes stay separate: `is_ready` answers "has NotebookLM finished ingesting",
  `is_drive_degraded` answers "did the backend explicitly report a degraded
  Drive state". A `False` from it means "nothing degraded was reported" — for a
  non-Drive source, for `ACTIVE`, and for an unreadable code alike — not "the
  Drive file is confirmed present and readable". `is_ready`'s docstring and
  `docs/python-api.md` now carry the caveat.

  **Evidence and its limits:** the slot's population is live-confirmed — 4 of
  409 real source rows across 25 notebooks carry it, all Drive-backed, all
  `DRIVE_SOURCE_STATUS_ACTIVE`. `INACCESSIBLE` / `SYNCING` / `DELETED` /
  `GEN_AI_ACCESS_DENIED` are read off the backend `UserDriveSourceStatus` enum
  recovered from the official Android app and are **not** live-observed;
  producing them means deliberately breaking access to a real Drive file, which
  was not done. The decode of those values is pinned by tests and by
  `tests/_guardrails/_wire_contract.py`; their occurrence in practice is not.
  ([#2111](https://github.com/teng-lin/notebooklm-py/issues/2111))

- **`notebooklm.types.share_permission_to_str`** — the single source of truth
  for permission/role code to string (`"owner"` / `"editor"` / `"viewer"`,
  `"unknown"` for unmapped codes), joining the existing `source_status_to_str`
  and `artifact_status_to_str` helpers.
  ([#2125](https://github.com/teng-lin/notebooklm-py/issues/2125))

### Changed

- **Failed artifact polling no longer derives `GenerationStatus.error` from
  nonexistent artifact-row fields.** `poll_status()` treated row index 3 as
  "failure text" and index 5 as a "nested error payload"; those slots are
  `Artifact.sources` and `Artifact.isPubliclyReadable`, so the read could only
  ever produce a wrong answer. For schema-conformant rows the observed output is
  unchanged — #2134 reports `error=None` for 3/3 real failed artifacts — but a
  nonconforming row carrying a string in either slot no longer populates the
  public field. `wait_for_completion()` still sets its own client-authored
  message for `REMOVED`, and `generate_*(retry=...)` still synthesizes
  `error=str(exc)` for retry callbacks; neither reads the wire for it. Capturing
  a genuine provider failure reason (delivered on the `CreateArtifact` RPC
  status, not persisted on the artifact) is not implemented.
  ([#2134](https://github.com/teng-lin/notebooklm-py/issues/2134))

### Fixed

- **PowerPoint sources now decode as their own type.** A source row with backend
  content type `6` returns the new `SourceType.POWERPOINT` (`kind="powerpoint"`,
  legacy compatibility label `text_file`) instead of `UNKNOWN`, and no longer emits
  the misleading "Consider updating notebooklm-py" `UnknownTypeWarning` — no released
  version mapped the code. The CLI's source-type display gains a matching label.
  Refs [#2137](https://github.com/teng-lin/notebooklm-py/issues/2137).
- **Deep-research imports no longer crash on a known backend retry rejection.**
  `IMPORT_RESEARCH` gets a batch-size-scaled read timeout (60-240s, scaled by
  requested source count) instead of the shared 30s default, since the server
  fetches/parses/embeds every entry before responding and routinely exceeds it
  on large batches. `ResearchAPI.import_sources_with_verification`'s
  timeout-retry loop also now recovers from the documented gRPC 9
  (`FAILED_PRECONDITION`) the server returns when a retry lands against a
  task it already partially committed — verified via `sources.list` the same
  way a lost-response timeout is, but only a fully-verified success is
  accepted (a partial/no match surfaces the error immediately rather than
  retrying against the already-rejected task). Fixes
  [#2187](https://github.com/teng-lin/notebooklm-py/issues/2187).
- **`Notebook.modified_at` was never a modification time — it is
  `lastViewedTime`, and reading a notebook advances it.** The field is now
  `Notebook.last_viewed_at` / `NotebookMetadata.last_viewed_at`, with
  `modified_at` kept as a deprecated alias through v1.0 (see
  [`docs/deprecations.md`](docs/deprecations.md)). A re-confirmation probe on a
  pair of throwaway notebooks reproduced both halves: three pure reads with no
  mutation of any kind advanced the slot, and one bare `GET_NOTEBOOK` moved a
  notebook from index 1 to index 0 of the recency list. Because that slot is the
  sort key behind the NotebookLM web UI's "Recent" ordering, **every**
  `notebooks.get()` this client issues — including internal reads and every
  source-readiness poll iteration — reshuffles that ordering as a side effect of
  observation. `notebooks.list()` does not: a third probe held the value pinned
  across repeated `LIST_NOTEBOOKS`, so listing reads the ordering without
  touching it. `docs/python-api.md` now documents the semantics and carries an
  inventory of the internal call paths that bump recency — `chat.ask()` without
  explicit `source_ids` is the most frequent, and `sources.add_file()` carries an
  unconditional pre-create baseline read;
  `client.notebooks.remove_from_recent()` is the only way to undo it. The
  backend exposes no lighter-weight existence/status probe than `GET_NOTEBOOK`,
  so no call path could be narrowed. `Notebook.modified_at` stays a dataclass
  field (silent, docs-only deprecation — a warning there would also fire from
  `repr` / `__eq__` / `dataclasses.replace` / serialization), while the
  `NotebookMetadata.modified_at` *property* emits a gated `DeprecationWarning`.
  Every JSON surface (CLI `--json`, MCP, REST) emits `last_viewed_at` alongside
  the unchanged `modified_at` key.
  ([#2126](https://github.com/teng-lin/notebooklm-py/issues/2126))
- **`Notebook.is_owner` no longer inverts on shared notebooks, and the notebook's
  role is now surfaced.** `is_owner` was decoded from `meta[1]` (proto tag 2) on
  the belief that the slot flagged ownership. A two-account live probe showed
  tag 2 actually tracks *"this notebook has any sharing at all"*, so the flag
  evaluated to `not (shared with anyone)`: **every notebook you own and have
  shared reported `is_owner=False`** and rendered as "Shared" in your own CLI.
  The same undercount reached `NotebookLimitError`, whose owned-notebook quota
  check silently dropped each shared notebook.

  The flag now derives from `meta[0]` (tag 1, `userRole`), which is present on
  every `LIST_NOTEBOOKS` and `GET_NOTEBOOK` row, so the fix costs no extra RPC.

  **Migration.** `is_owner` keeps working and keeps its type; it simply returns
  the right answer now, so callers relying on the inverted value should drop
  their workaround. Because one boolean cannot separate an owner-who-shared from
  an editor or a viewer — all three previously collapsed onto `False` — a new
  `Notebook.role` field carries the real level as a `SharePermission`
  (`OWNER` / `EDITOR` / `VIEWER`, or `None` when the row states no level), and
  `is_owner` is now the `role is SharePermission.OWNER` shorthand. Prefer `role`
  when read-only access must be distinguished from edit access:

  ```python
  from notebooklm.types import SharePermission

  for nb in await client.notebooks.list():
      if nb.role is SharePermission.VIEWER:
          print(f"{nb.title}: read-only")
  ```

  The CLI's `list` / `use` "Owner" column is renamed **"Access"** and now shows
  Owner / Editor / Viewer, `metadata` and `status` follow, and `list --json`
  gains a `role` string alongside `is_owner`. MCP and REST notebook payloads
  gain `role` (the raw code) plus `role_label`, mirroring how sources already
  ship `status` plus `status_label`.
  ([#2125](https://github.com/teng-lin/notebooklm-py/issues/2125))

- **`ArtifactStatus` codes 1 and 2 are no longer transposed, and codes 0/5/6 are
  now modeled**
  ([#2127](https://github.com/teng-lin/notebooklm-py/issues/2127)). The client
  read wire code 1 as `PROCESSING` and code 2 as `PENDING`, the inverse of the
  backend enum. Two independent live traces of a generating artifact showed
  `2 → 3`, so code 2 is what an artifact reports *while generating*; every
  `CREATE_ARTIFACT` / `RETRY_ARTIFACT` row recorded on the **web** transport
  starts at code 1 (`ARTIFACT_STATUS_INITIALIZED` — "row created, worker not
  started"), a window short enough that live sampling regularly misses it and
  catches code 2 on the very first poll instead.
  Consequently `Artifact.is_pending` returned `True` for an artifact that was
  mid-generation while `Artifact.is_processing` returned `False` for it, and any
  caller distinguishing "queued" from "running" — progress UIs, timeout
  heuristics, retry logic — read the two states backwards. Codes 0
  (`ARTIFACT_STATUS_UNKNOWN`), 5 (`ARTIFACT_STATUS_SUGGESTED`, the state
  `LIST_ARTIFACTS` already filters on server-side) and 6
  (`ARTIFACT_PENDING_REVIEW`) were unmodeled and all collapsed into `"unknown"`;
  they are now distinct members, with `GenerationState.SUGGESTED` /
  `GenerationState.PENDING_REVIEW` added so a caller can detect them. Code 6's
  semantics are unconfirmed (it appears in the backend enum dump but was
  observed in none of 42 live artifacts, 301 recorded rows, or a fresh
  generation) — it is modeled for detectability only.

  **⚠ BREAKING for callers that compare `Artifact.status` / `ArtifactStatus`
  members to raw integers, or that catch one artifact timeout exception but not
  its sibling.** `notebooklm.types.ArtifactStatus` is a public name and its
  member integers changed. This is filed under *Fixed* because the old integers
  were simply wrong about the wire, but the migration below is required.

  **User-visible changes.** No existing `ArtifactStatus` member name, and no
  existing `status_str` / `GenerationState` value, was renamed or removed —
  `PENDING` still means "queued" and `PROCESSING` still means "generating".
  What changed is which wire code produces which string, plus two *additions*
  to the vocabulary (`SUGGESTED` / `"suggested"` and `PENDING_REVIEW` /
  `"pending_review"`, for codes 5 and 6, which previously had no members and
  decoded to `"unknown"`). So an exhaustive `if`/`elif` or `match` over the
  status strings needs a new arm, while every existing arm keeps its meaning:
  - wire code 1 now renders `"pending"` (was `"in_progress"`), and code 2 now
    renders `"in_progress"` (was `"pending"`). This affects `Artifact.status_str`,
    `GenerationStatus.status`, the CLI's `artifact poll` / `--json` output, the
    MCP status payloads, and `GET /v1/notebooks/{id}/artifacts/{task_id}`;
  - the two artifact timeout exceptions swap with the codes, in **both**
    directions: a task that times out while the backend reports code 2 now
    raises `ArtifactInProgressTimeoutError` (was `ArtifactPendingTimeoutError`),
    and — the more common case, since it is the state every `CREATE_ARTIFACT` /
    `RETRY_ARTIFACT` row starts in — a task stuck at code 1 now raises
    `ArtifactPendingTimeoutError` (was `ArtifactInProgressTimeoutError`). Both
    are separately catchable and both are reachable via the
    `ArtifactTimeoutError` / `WaitTimeoutError` umbrellas, so `except
    ArtifactInProgressTimeoutError` callers should add the sibling. The
    `stalled_phase` attribute and the CLI's `--json` error envelope follow;
  - codes 5 and 6 now render `"suggested"` / `"pending_review"` instead of
    `"unknown"`. All three remain *still-running* for the poll loop and the
    REST poll route (`GenerationState.UNKNOWN` is now classified there
    explicitly rather than falling through the terminal branch), exactly as
    `"unknown"` behaved for the client loop before;
  - because code 0 is now a modeled member, `ArtifactStatus(artifact.status)`
    on a **truncated or malformed** artifact row no longer raises `ValueError`
    — the row adapter synthesizes `0` for a short row or a non-int status leaf,
    which now resolves to `ArtifactStatus.UNKNOWN` instead. Code reading that
    `ValueError` as a decode-failure signal needs another check.

  New: `GenerationState.is_terminal` is now the single authority for
  "generation ended" (`COMPLETED` / `FAILED` / `REMOVED`); everything else,
  including the two new states, means keep waiting. Prefer it over enumerating
  members. A state added to the enum later is non-terminal by default, so a
  waiter can no longer silently abandon a running task.

  Callers that hard-coded the raw integers (`artifact.status == 1` to mean
  "generating") must flip them; callers using `.is_pending` / `.is_processing` /
  `.status_str` / `GenerationState` now get the correct answer with no change.

- **Flashcard generation no longer swaps `quantity` and `difficulty`.** The
  flashcards payload builder emitted the option pair as
  `[difficulty, quantity]` while the backend's `FlashcardsGenerationOptions` —
  and the quiz builder next to it — expect `[quantity, difficulty]`. Asking for
  the fewest, hardest cards silently produced the most, easiest ones, with no
  error at any layer (CLI, MCP, REST). The regression test meant to pin this
  ordering used symmetric fixtures (`[2, 2]` and `[1, 1]`) and so passed under
  either ordering; it is now asymmetric (`FEWER` + `HARD` → `[1, 3]`), and a new
  test asserts the quiz and flashcards builders agree side by side
  ([#2116](https://github.com/teng-lin/notebooklm-py/issues/2116)).
- **`QuizQuantity.MORE` now emits its own wire value.** It was defined as a
  value-alias of `STANDARD` (`2`), on the documented but incorrect belief that
  Google's API could not distinguish the two, so `--quantity more` produced a
  standard-sized quiz or flashcard set with no warning. The backend accepts and
  persists `cardQuantity = 3`, matching both
  `QuizGenerationOptions.QuestionQuantity` and
  `FlashcardsGenerationOptions.CardQuantity`, so `MORE = 3`. **Behavior change
  for existing callers:** `QuizQuantity.MORE == QuizQuantity.STANDARD` was
  `True` and is now `False`, and unchanged code passing `--quantity more` /
  `quantity=QuizQuantity.MORE` now receives a genuinely larger quiz or flashcard
  set rather than the standard-sized one it silently got before — which is what
  those callers were asking for all along
  ([#2117](https://github.com/teng-lin/notebooklm-py/issues/2117)).

- **`sources.add_drive` no longer duplicates a Drive source when a create has to
  be retried.** Its idempotency probe matched an existing source by looking for
  a `/d/<file_id>` segment inside `source.url`, but Drive-backed sources carry no
  URL at all, so the probe could never match. Since this variant runs with
  internal retries disabled, a 5xx or network failure *after* the server had
  committed the create left the probe reporting "not committed" and the add was
  re-issued, producing two copies of the same Drive file
  ([#2113](https://github.com/teng-lin/notebooklm-py/issues/2113)). The probe now
  matches on the Drive `documentId` the backend echoes back — exposed as the new
  `Source.drive_document_id` and decoded from either
  `SourceMetadata.googleDocsMetadata` (Docs/Slides) or
  `googleDriveSourceMetadata` (Drive-hosted files). Because a `documentId` is not
  unique within a notebook, matches are filtered against a snapshot of source ids
  taken before the create, so a pre-existing copy of the same file is never
  handed back as if it were the one just created; an unavailable snapshot or an
  ambiguous multi-match raises `SourceAddError` rather than guessing. `add_drive`
  also now rejects a blank `file_id` with `ValidationError` instead of writing an
  unmatchable source.

  **Behaviour change:** `add_drive` previously listed sources only inside its
  retry probe (i.e. only after a transport failure); it now takes that snapshot
  on *every* call, matching the shape `add_file` has always had. That is one
  extra `GET_NOTEBOOK` per `add_drive`, and because the backend writes
  `lastViewedTime` when answering one
  ([#2126](https://github.com/teng-lin/notebooklm-py/issues/2126)), every
  `add_drive` now bumps the notebook's position in the web UI's *Recent* list.
  No cheaper probe exists — source ids are published only inside the
  `GET_NOTEBOOK` payload — and the trade is deliberate: reporting a create that
  never happened is far worse than a reordered Recent list.

- **RPC bundle monitoring no longer reports authentication/access failures as
  protocol drift.** The live registry capture now classifies login,
  CookieMismatch, region/anti-abuse, HTTP, and CDN failures as exit code 2 and
  explicitly states that no drift conclusion was possible. The nightly workflow
  opens its RPC/studio-drift issue only for the script's explicit `drift`
  outcome; exit 2 and unclassified runner failures are routed into the existing
  authentication/infrastructure report instead. The maintainer live-auth matrix
  now runs the real RPC canary; always exercises fallback-disabled storage-only
  mid-session recovery (#2161); tests a sibling-process re-mint under four
  simultaneous RPCs and actual mid-session master-token fallback; drives both
  REST (live recovery plus stale-start lazy rebind) and MCP (tool call
  before/after live-jar invalidation) through their adapter lifespans; and
  regression-tests the access-gate routing that caused
  [#2174](https://github.com/teng-lin/notebooklm-py/issues/2174) alongside
  [#2175](https://github.com/teng-lin/notebooklm-py/issues/2175). Its new opt-in
  `--include-interactive` lane also covers ordinary headed Playwright login,
  initial headed master-token bootstrap, and loopback CDP-attached master-token
  capture without changing the unattended default matrix. Its configurable
  interaction deadline is forwarded into the actual Playwright/CDP wait rather
  than acting only as an outer subprocess timeout.
- **Long-lived MCP and REST servers now keep cookie sessions alive and recover
  from sibling profile refreshes.** Both server adapters enable the client's
  600-second background `RotateCookies` loop for their process-lifetime client.
  If a live request is nevertheless rejected, mid-session recovery now first
  re-reads a different valid `storage_state.json` into the live jar before
  invoking the opt-in refresh command, browser, or master-token rungs. Cookies
  and the file's in-band account route are installed from the same profile
  generation, so a sibling login that changes accounts also reroutes the
  immediate retry and later REST/MCP requests. An in-band clear marker prevents
  a just-cleared/default profile from briefly inheriting stale legacy
  `context.json` routing before sibling cleanup completes. If the live jar changes around
  that read, recovery preserves one untried
  authentication-bearing live candidate, then performs one final bounded disk
  sample if that candidate is also rejected. The REST adapter also retries its
  single client bind on demand after a stale-auth startup, so both live and
  initially degraded servers can consume cookies refreshed by a CLI or sibling
  process without a restart. Failed request-time binds are coalesced and
  rate-limited before another full bootstrap is attempted
  ([#2161](https://github.com/teng-lin/notebooklm-py/issues/2161)).

### Changed

- **BREAKING: `QuizQuantity.MORE` no longer compares equal to
  `QuizQuantity.STANDARD`.** `MORE` was a value-alias of `STANDARD` (`2`) and is
  now its own wire value (`3`), so the identity and equality between the two
  members no longer hold. `QuizQuantity` is part of the documented-stable public
  surface (`docs/stability.md`), so this is an intentional, allowlisted
  wire-value correction rather than an incidental change — see
  `scripts/api-compat-allowlist.json`. **Migration:** callers that branched on
  `MORE == STANDARD` must drop that assumption; callers passing `--quantity
  more` / `quantity=QuizQuantity.MORE` now receive a genuinely larger quiz or
  flashcard set instead of a standard-sized one, which is what they were asking
  for all along. Nothing needs to change to *get* the old output — pass
  `standard` explicitly. See the `### Fixed` entry above for the live evidence
  ([#2117](https://github.com/teng-lin/notebooklm-py/issues/2117)).
- **First-party profile replacements now use native typed results.** Browser capture consumes
  `ProfileStore.replace_from_remint()` directly, while cookie import/login/refresh use a narrow
  path-shaped login operation with primitive account modes. Existing v0.x storage wrapper
  signatures, results, facade identities, and explicit `cookie_saver=` behavior are unchanged;
  native-to-legacy projections now live only in the compatibility owner.
- **Reading a profile's account binding no longer writes to disk.** On a
  pre-v0.5.0 two-file profile (account metadata in a sibling `context.json`),
  every read of the account binding — including the one that runs on **every
  RPC** to pick the `authuser` a request routes to — used to perform the
  one-shot migration inline, taking the storage write lock. It now derives the
  same record read-only and returns immediately; the durable migration runs
  once per profile in the background. The values callers see are unchanged
  (`notebooklm profile list`, `auth check`, `client.get_account_email` and the
  request routing all report exactly what they did before, and exactly what the
  migrated file will contain). Two things are observable: the legacy
  `context.json[account]` key is scrubbed a moment *after* the first read
  rather than during it, and a profile whose migration keeps failing (read-only
  profile directory, full disk) now logs a plain, default-visible warning
  instead of one gated behind a per-path throttle. That throttle existed only
  because promotion used to run on the per-RPC read path and would otherwise
  warn on every request; the one-shot is single-flight per profile and does not
  retry in-process, so the warning fires at most once per profile per run and
  cannot flood the log. No configuration changes.
- **Cold-start recovery now runs `NOTEBOOKLM_REFRESH_CMD` before the re-mint
  rungs.** On a dead-cookie cold start, the external refresh command (rung
  "L2.5") previously ran only *after* headless re-auth (L3) and master-token
  re-mint (L4) had both failed. It now runs first, matching the order
  mid-session recovery has always used and the order
  [ADR-0030](docs/adr/0030-one-recovery-ladder.md) documents; cold start had
  never matched its own ADR. Operators with a configured command will see it
  invoked earlier. A **failing** command no longer ends the ladder — L3 and L4
  still run after it, so a broken or timing-out refresh command cannot mask the
  re-mint rungs — and the command is invoked at most once per recovery. When the
  whole ladder is exhausted the error is unchanged: the command's actionable
  exit-code message is preserved rather than being replaced by a generic
  "Authentication expired". Where no command is configured, nothing changes.

### Added

- **`SharingAPI.set_users()` sets several users' permissions in one request.**
  The client sends one `SHARE_NOTEBOOK` RPC for all email/permission pairs,
  then refreshes the returned sharing status once. Notification and welcome
  message settings apply to the whole call, not per grant; batching collapses N
  RPC + status-refresh round trips into one, which is not the same as sending
  fewer notification emails. The operation is an **upsert** — a live probe
  confirmed the backend adds an absent email and replaces an existing one, in
  both the plural and singular forms — so `add_user()` and `update_user()` are
  now intent wrappers over it rather than distinct operations, differing only in
  their default `notify`. Two backend preconditions the probe exposed are
  encoded here: a batch naming one email twice returns success while silently
  leaving that user unchanged, so duplicates now raise `ValueError` before the
  request is issued (exact match — case variants are passed through, since
  RFC 5321 keeps the local part case-sensitive); and a batch removal silently drops the
  *whole* request if any target is already absent, so plural removal is
  deliberately not offered — it needs a share-status preflight and
  post-verification rather than a wider entry list. Both are recorded in
  [`docs/rpc-reference.md`](docs/rpc-reference.md)
  ([#2000](https://github.com/teng-lin/notebooklm-py/issues/2000)).
- **Deep-research sources expose the backend's per-task source ordinal.**
  `ResearchSource.source_ordinal` and its serializers preserve an integer
  `src[8]` when the row carries one — in the captures a 1-based bijection over
  a task's discovered sources, which the client previously decoded and threw
  away. It is **not** established to resolve the report's own citation
  markers; see the field docs before using it that way
  ([#2141](https://github.com/teng-lin/notebooklm-py/issues/2141)).
- **Mid-session `NOTEBOOKLM_REFRESH_CMD` (opt-in for one release).** The external
  refresh command (the L2.5 rung of the unified recovery ladder) previously fired
  only at cold start; it can now also run **mid-session** — e.g. inside a
  long-lived server that has been running past cookie expiry — when you set
  `NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1` (literal `1`). It is **off by default for
  one release** so operators whose commands assume cold-start-only invocation are
  not surprised, then flips to default-on a later release. Because promoting the
  rung into long-lived servers widens exposure of whatever the command prints,
  the default DEBUG log line now records only the command basename, exit code,
  and byte counts; set `NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT=1` to route the
  command's captured `stdout`/`stderr` into the redacting DEBUG logger. The
  refresh subprocess environment also now scrubs **all** first-party secret vars
  (`NOTEBOOKLM_AUTH_JSON`, `NOTEBOOKLM_SERVER_TOKEN`,
  `NOTEBOOKLM_SERVER_TOKEN_FILE`, `NOTEBOOKLM_MCP_TOKEN`,
  `NOTEBOOKLM_MCP_OAUTH_PASSWORD`, `NOTEBOOKLM_MCP_OAUTH_STATE_PATH`), not just
  `NOTEBOOKLM_AUTH_JSON`. See the new
  [ADR-0030](docs/adr/0030-one-recovery-ladder.md) ("one recovery ladder") and
  `docs/configuration.md`.

### Changed

- **One RotateCookies wire contract (ADR-0031 Stage 0).** The POST to
  `accounts.google.com/RotateCookies` was independently assembled at four
  sites — the keepalive poke, the file-based and in-memory PSIDTS recoveries,
  and the master-token mint's completing leg — and the mint leg alone omitted
  `raise_for_status`, so a 429/5xx there passed silently. All four now route
  through a single wire implementation in `notebooklm._auth.keepalive`
  (guardrail-enforced). Two observable deltas, both on the mint leg only: a
  rejected rotation is now logged and skipped instead of silently ignored, and
  the leg uses the canonical 15 s rotation timeout instead of inheriting the
  mint client's 30 s. See the new
  [ADR-0031](docs/adr/0031-credential-tier-auth-model.md) for the
  credential-tier model this is the first stage of.

- **`notebooklm.auth.__all__` is now exactly the documented public surface (38 →
  6 names).** `__all__` had been doing double duty: the CLI boundary lint forbids
  `cli/` from importing `notebooklm._*`, so every auth helper the CLI needed was
  reached through the `notebooklm.auth` facade — and the external-imports audit
  then *forced* that name into `__all__`. "The CLI needs it" silently became "it
  is public API", and the published surface grew to 38 names against the 6 that
  docs/stability.md ever promised (`AuthTokens`,
  `convert_rookiepy_cookies_to_storage_state`, the three cookie-domain constants,
  and `LockUnavailableError`).

  The other 32 are **de-advertised, not removed** — the #1592 mechanism:
  `notebooklm.auth.<name>` keeps resolving exactly as before, with one reviewed
  `removed-export` allowance each in `scripts/api-compat-allowlist.json`. No
  runtime behaviour changes and no import breaks; only `from notebooklm.auth
  import *` (which no supported usage relies on) and static-analysis views of the
  advertised surface are affected. The 30 names first-party code genuinely needs
  across the boundary are now tracked by their own list,
  `AUTH_CROSS_BOUNDARY_NAMES` in `tests/_guardrails/test_public_surface.py`, which
  grants importability *without* publishing — so a future CLI need can no longer
  re-inflate the public API. Two names (`KEEP_ACCOUNT`, `LoginWriteOutcome`) had
  no importer at all and are simply unblessed. Publishing a name now requires
  changing `__all__`, the docs, and the manifest together:
  `test_auth_all_matches_documented_public_surface` fails the build if the three
  disagree.

- **The API-compat audit now compares the VALUE of designated public constants.**
  `scripts/audit_public_api_compat.py` reasoned about shape only — a module
  constant contributed just its `kind`, so rebinding one compared as "str vs str"
  and passed. The Gemini Notebook rebrand went through that blind spot: the audit
  stayed green while `config.DEFAULT_BASE_URL` and `config.PERSONAL_BASE_HOST`
  changed host and `auth.REQUIRED_COOKIE_DOMAINS` gained members. Constants listed
  in the new `VALUE_TRACKED_CONSTANTS` (those five, minus the unchanged optional
  tiers) now carry an order-insensitive fingerprint — sorted so a `frozenset`
  cannot fingerprint differently under a different `PYTHONHASHSEED` — and a change
  surfaces as a `changed-constant-value` break requiring an allowlist entry. The
  three real changes above are recorded there now rather than being invisible.

- **`NOTEBOOKLM_AUTH_JSON` is now read through a single helper.** The env var was
  checked at ~7 auth-layer call sites that had drifted on
  presence-vs-truthiness (the #2057 / #2083 class of bug, where a *set-but-empty*
  value fell through to a profile file at some sites and raised at others). All
  auth-layer reads now route through `notebooklm._auth.paths.resolve_auth_json_env`:
  an **unset** variable falls through to profile-file auth, and a **set** value —
  even empty — selects inline env auth (never a silent fall-through to a file),
  with the one payload-parsing consumer (`_load_storage_state`) raising the
  "set but empty" configuration error. Behaviour is preserved (the tokens.py
  truthiness outlier had already been corrected in #2083); this consolidation
  makes the contract un-driftable. See [ADR-0030](docs/adr/0030-one-recovery-ladder.md).
  The `notebooklm.auth` browser-cookie filter's dropped-cookie / malformed-row
  warnings now also log to the documented `notebooklm.auth` logger rather than a
  private child logger.

- **`notebooklm.io.atomic_write_json` now rejects `storage_state.json` paths.**
  As the final step of routing every `storage_state.json` mutation through the
  single canonical `_auth.storage` (ADR-0029), the public
  `atomic_write_json` helper now raises `ValueError` when handed a
  `storage_state.json` path — the same guard `atomic_update_json` has enforced
  since #1215. A bare atomic write skips the canonical dotted
  `.storage_state.json.lock` sentinel and would re-open the lost-update race, so
  it is refused; cookie/account writers must go through the dedicated
  `notebooklm._auth.storage` intents. Callers writing other JSON files
  (`context.json`, `config.json`, OAuth tokens, …) are unaffected. The canonical
  writer uses a module-private bypass internally. This is a documented
  public-surface change (precedent #1215).
- The CLI `login --browser-cookies`, `auth refresh --browser-cookies`, and
  `auth import-cookies` writers now persist through the canonical
  `storage.replace_from_login` (write-time domain filter, post-filter
  required-cookie revalidation, in-band account metadata, and — for import — the
  `.bak` backup all happen under one storage lock). Additive `notebooklm.auth`
  re-exports (`replace_from_login`, `LoginWriteOutcome`, `AccountRecord`,
  `KEEP_ACCOUNT`, `CLEAR_ACCOUNT`, `drop_legacy_account_key`) expose the writer to
  the CLI boundary. Behaviour is preserved; the failure surface
  (`COOKIE_VALIDATION_FAILED` on dropped required cookies, exit 1, nothing
  written) is unchanged.
- **CLI, MCP, and REST downloads now derive from one artifact-format registry.**
  `_app.download_specs` owns each type's client binding and each representation's
  extension/MIME pair; adapter registries, MCP schema enums, and the MIME lookup
  are projections of that table. Adding a type or format no longer requires
  updating three copies, while the CLI keeps its help prose and public legacy
  `slide_format` parameter as explicit adapter-local residue
  ([#2056](https://github.com/teng-lin/notebooklm-py/issues/2056)).

- **The default base host is now `notebook.google.com`.** Google rebranded
  NotebookLM to Gemini Notebook and serves the personal app from both
  `notebooklm.google.com` and `notebook.google.com`; `batchexecute` is
  dual-served on both (ADR-0028). The client now defaults to the rebrand host
  instead of waiting for the legacy one to fail, which would have converted a
  migration into an incident
  ([#2067](https://github.com/teng-lin/notebooklm-py/issues/2067)).

  **What changes for you:**

  - **Share URLs.** `share_url`, `get_share_url()` and artifact deep-links now
    read `https://notebook.google.com/notebook/<id>`. Previously-issued links
    keep working — Google serves and redirects between both hosts — but any
    test or snapshot pinning the old string needs updating.
  - **`config.DEFAULT_BASE_URL` and `config.PERSONAL_BASE_HOST` change value.**
    Both remain public and keep their names and types, so the API-compat gate
    sees no break; the *values* moved. Code comparing against them is fine;
    code comparing against a hardcoded `"notebooklm.google.com"` is not.
  - **Rolling back is normally just the env var.** Set
    `NOTEBOOKLM_BASE_URL=https://notebooklm.google.com` to return to the
    pre-rebrand host — it is still served and is the documented rollback lever.
    Existing profiles usually keep working across the switch. The host-scoped
    `OSID` does not survive it, but it is not the only binding path: `APISID` +
    `SAPISID` together with bare `LSID` also satisfies the check, and a profile
    captured by `notebooklm login` normally carries all three. The `LSID`
    conjunct is required — `APISID` + `SAPISID` alone fail (#1977).

    If authentication *does* fail after switching, the profile was relying on
    the host-scoped `OSID`, which was minted on the host you just left and is
    never sent to the other one. Recover with `notebooklm login --fresh`. Use
    `--fresh` specifically: a plain `notebooklm login` can report "Already
    logged in" and re-mint nothing, because the login accept-set matches either
    personal host.

### Removed

- **The pre-v0.5.0 legacy account-metadata read fallback.** `read_account_metadata`
  no longer returns a raw pass-through of the sibling `context.json[account]`
  key when `storage_state.json` has no in-band record. That standing fallback was
  a silent wrong-account hazard: a profile whose `authuser` lived only in the
  legacy sibling would keep being re-derived from that file forever — a missed
  or stale value routes requests to a *different* signed-in Google account (an
  issue-#2103-class bug) with no failure to notice. In its place,
  `read_account_metadata` calls a one-shot `promote_legacy_account` migration on
  every read where in-band is absent, embedding the legacy record in-band
  (durably, once) and scrubbing the legacy key — so no existing user loses their
  account binding, and the result is always genuinely in-band truth rather than
  a value re-derived from an unmigrated file each call. A transient promotion
  failure (disk full, permission error, lock timeout) falls back to the legacy
  record already read rather than to "no account" — the failure is logged at
  WARNING (default-visible), since a persistent cause would otherwise silently
  reintroduce the same hazard via a different trigger. The startup profiles
  migration promotes proactively too, as a completeness nicety.
  `drop_legacy_account_key` (whose two remaining call sites in the CLI login
  writers are gone — the scrub now lives inside `replace_from_login` itself) is
  de-blessed rather than removed: still importable from `notebooklm.auth` for
  back-compat.

### Deprecated

- **The `AuthTokens` cookie compatibility views now have an explicit v1
  runway.** Direct `flat_cookies` access emits one caller-attributed
  `DeprecationWarning`; use `jar` for bootstrap-cookie questions and managed
  client APIs for requests. `cookies` and `cookie_jar` are docs-only deprecated
  so construction, repr, equality, and `dataclasses.replace()` remain quiet.
  `jar` is the transitional shape for v1's immutable `initial_cookies` field;
  `cookie_header` and `cookie_header_for(url)` are scheduled for v1 deletion
  and remain warning-free through v0.x. `CookieJar` stays an ordered sequence,
  never a Mapping or live transport jar. Suppress the direct-access warning
  temporarily with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`.
- **`AuthTokens.from_storage(...)` is deprecated in favor of the managed client
  lifecycle.** It remains available throughout v0.x, but now emits
  `DeprecationWarning` and is scheduled for removal in v1.0. Migrate to
  `async with NotebookLMClient.from_storage(...) as client:` and use
  `client.auth` while the client is open.
- **Implicit synchronous storage loading during `AuthTokens` construction is
  deprecated.** `AuthTokens(..., storage_path=..., cookie_jar=None)` still
  builds its cookie jar for v0.x compatibility, but now emits
  `DeprecationWarning` and is scheduled for removal in v1.0. Prefer the managed
  client lifecycle above; callers constructing tokens directly should supply
  `cookie_jar=` explicitly. Set `NOTEBOOKLM_QUIET_DEPRECATIONS=1` to suppress
  either warning temporarily while migrating.
- **The pre-profiles home-root layout** (`~/.notebooklm/storage_state.json` /
  `context.json` / `browser_profile/` read directly, outside `profiles/<name>/`)
  now emits a `DeprecationWarning` on each read. It is reached only when the
  profile-dir path doesn't exist and the resolved profile is `"default"`; running
  any `notebooklm` command once triggers the existing crash-safe migration and
  the fallback is never hit again. Scheduled for removal in v1.0. Suppress with
  `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. See [docs/deprecations.md](docs/deprecations.md).

### Fixed

- **Unknown source status codes no longer masquerade as ready.** Missing,
  malformed, and unmapped wire statuses now resolve to `SourceStatus.UNKNOWN`
  with `is_ready=False`; unmapped integers also emit a drift warning (#2124).
- **An empty notebook no longer logs `schema drift?` on every
  `get_source_ids` call.** A genuinely empty notebook returns a healthy
  envelope whose sources slot is present but explicitly null — the backend
  sends `None` there, not `[]` — and the guard tested only
  `isinstance(sources, list)`, so a valid empty state landed in the branch
  reserved for a malformed response. Obfuscated method ids and positional
  payload shapes changing underneath us is this project's #1 breakage class
  and `schema drift?` is how an operator finds out, so a warning that fires on
  every empty notebook erodes the one signal that has to stay trustworthy. The
  walk now separates the three shapes the single `isinstance` test conflated:
  an envelope too short to carry a sources slot still warns (a truncated shape,
  as before), a present-and-null slot returns quietly, and a present-but-wrong
  type still warns. That is the same split the sibling walk over this slot
  already makes in `_source/listing.py`
  ([#2131](https://github.com/teng-lin/notebooklm-py/issues/2131)).
- **Expected no-answer chat responses no longer emit a wire-drift warning.**
  Responses without a TailwindDoc answer now return the backend apology quietly,
  while present-but-unmarked response documents still warn
  ([#2118](https://github.com/teng-lin/notebooklm-py/issues/2118)).
- **Chat turn numbers now come from server history instead of the client-local
  cache.** Stateless remote MCP requests create a fresh client for each call, so
  a real continuation could previously report the contradictory pair
  `is_follow_up=True, turn_number=1`. `chat.ask()` now counts complete
  newest-first server history by user-question rows under the conversation lock,
  uses that count to classify implicit continuations, and assigns the new answer
  the next ordinal. Explicit-conversation asks retain explicit follow-up intent
  while using the same server count for their ordinal, and stale local cache
  entries no longer control the result
  ([#1976](https://github.com/teng-lin/notebooklm-py/issues/1976)).
- **Deep-research reports are identified by their typed content block instead
  of row order.** Report markdown is extracted only from a kind-`3` content
  block, so reordered web snippets can no longer be mistaken for the report
  ([#2140](https://github.com/teng-lin/notebooklm-py/issues/2140)).
- **Research that finds nothing is no longer an undifferentiated `failed`.** A
  Google Drive research run whose query matched no file came back as
  `status: failed` with no sources, no code, no message and no remediation —
  indistinguishable from a genuine error, so a caller could not tell "refine the
  query" from "fix permissions" or "back off" (issue #1964). The backend status
  codes were live-captured against the serving API and are now documented in
  `docs/rpc-reference.md`: `1` in-flight, `2` completed, `3` no matches (observed
  only on Drive), `4` cancelled, `6` completed (deep). `ResearchTask` gains a
  `termination_reason` (`no_results` / `cancelled` / `completed` / `in_progress`
  / `unknown`) plus a `reason_message` and a source-specific `hint` — an empty
  Drive search now suggests the exact filename, document URL, or document id,
  while an empty web search suggests broadening the query. The coarse `status`
  field is **unchanged**, so existing `status == "failed"` checks keep working;
  an unrecognised terminal code maps to `unknown` rather than being guessed at.
  The MCP `research_status` tool surfaces all three fields (and now reports a
  cancelled run from the wire code alone, so a cancel from another process — or
  from before a server restart — is still reported honestly), and  `research_import`'s refusal message no longer tells you to "start a new
  research session" when your query simply matched nothing. The CLI
  (`research status` / `research wait` / `source add-research`) and the REST
  `GET .../research/{run_id}` route report the same reason, so no surface is
  left showing a bare `failed`. `research status --json` is deliberately
  unchanged — it emits the byte-stable public dict, as `status_code` did.

  `ResearchTask` also gains `source_type` (the search source echoed by the
  backend) and `is_drive_search` / `is_web_search`. Both `source_type` and the
  existing `status_code` are ordinary dataclass fields, so they participate in
  `ResearchTask.__eq__` / `__hash__` / `__repr__`: a parsed task no longer
  compares equal to one hand-built without them. That is deliberate — the
  reason, message and hint all derive from those two fields, so excluding them
  would let two "equal" tasks carry different explanations.
- **`login --master-token` now honors `--storage` for `master_token.json` too
  (#2103).** The login writer resolved the storage path from `--storage` but the
  master-token path from the profile dir, so under a `--storage` override the
  durable token landed where no reader ever looks — the L4 master-token recovery
  rung silently reported "no token", `auth check` reported `present: False`
  immediately after a successful login, and the full-account credential was
  written into the default profile dir the user had explicitly redirected away
  from. The writer now derives the token path as a sibling of the storage path,
  matching every reader (`_auth/recovery.py`, `_app/auth_check.py`,
  `cli/services/auth_refresh.py`). Behavior without `--storage` is unchanged.
  The same fix covers `login --master-token-refresh --storage …`, and the
  account-ownership guard now checks the token that actually sits beside the
  target storage instead of the active profile's.

- **A symlinked or relative `--storage` alias now selects the same
  `master_token.json` the recovery ladder reads (#2104 review).** `--storage`
  was used verbatim when deriving the token sibling, so an alias — a relative
  path, a `~` prefix, or a symlinked directory — wrote the token beside the
  *alias* while the L4 master-token recovery rung, which canonicalizes through
  `canonical_storage_key` (`expanduser().resolve()`) before taking the parent
  directory, looked beside the resolved target and found nothing. The login
  driver now canonicalizes an explicit `--storage` the same way the
  `cli.services.auth_source` resolver already does, so every syntactic spelling
  of one storage file collapses to one token location. Profile-derived paths
  were already absolute and are unaffected.

- **`_app/auth_check.py`'s `auth check` and `cli/services/auth_refresh.py`'s
  missing-storage bootstrap now also canonicalize the `master_token.json`
  sibling for a symlinked or relative `--storage` (#2103 structural
  follow-up, PR-1).** #2104/#2105 fixed this for the CLI login writer only,
  one of four sites that each derived the sibling independently: the L4
  recovery rung resolved via `canonical_storage_key`, `auth check` used an
  unresolved `with_name`, and the missing-storage bootstrap used a raw
  `.parent` join — three different policies that could each derive a
  different sibling for the same alias. All four (plus `get_master_token_path`,
  previously derived from the profile directory directly rather than from
  `get_storage_path`, so it disagreed with every other reader for a legacy
  home-root profile) now call the new
  `notebooklm.paths.master_token_path_for(storage_path)`, the sole derivation
  site, guarded by a lint against a second one reappearing. `auth check`
  reporting `present: false`/`false` account presence for such a profile
  under `--storage`, and the missing-storage bootstrap failing to find a
  token beside a symlinked/relative `--storage`, are the two behavior changes
  this fixes; ordinary absolute-path profiles are unaffected.

- **A stale or circular `--storage`/profile symlink no longer crashes `auth
  check` or the cold-start bootstrap (#2103 PR-1 review).** The new
  `master_token_path_for` canonicalizes via `Path.resolve()`, which raises
  `RuntimeError` (not `OSError`) on a symlink loop on Python 3.10-3.12 — a
  CPython pathlib behavior fixed upstream in 3.13, where the same call
  degrades silently instead. `master_token_path_for` now catches both
  exception types on every supported Python version and falls back to a
  best-effort, non-canonicalized path rather than propagating.

- **The master-token transaction no longer lets a mint silently overwrite a
  different account's session (#2103 structural follow-up, PR-2).** Before
  this, `notebooklm._auth.storage_writer.persist_minted_jar` — the function
  every master-token mint (bootstrap, L4 recovery, operator refresh, and the
  documented low-level recipe that calls it directly) ultimately writes
  through — applied no account check at all: a wrong `--account`, a stale
  profile alias, or a hand-rolled `mint_cookies` + `persist_minted_jar` call
  could clobber an existing, different account's cookies *and* durable master
  token with no warning. It now refuses (raising `MasterTokenError`, or
  returning a typed `False` on the L4 ladder) when existing storage is bound
  to a recorded account different from the one being minted, unless `force` —
  enforced under the storage-write lock itself, closing both the
  check-before-mint TOCTOU race and the bypass a direct low-level call sat
  outside of. This closes the #2104 review thread
  (`discussion_r3731673393`) that bound ADR-0023 to fixing the L4 read-side
  cross-account re-mint.

- **`notebooklm auth refresh`'s missing-storage bootstrap no longer conflates
  four different outcomes into one boolean internally (#2103 structural
  follow-up, PR-2).** The flock/shield/recheck machinery moved from
  `cli/services/auth_refresh.py` into `notebooklm._auth.master_token` as
  `bootstrap_storage_from_master_token`, returning an explicit
  `BootstrapOutcome` (`MINTED` / `PRESENT_AFTER_WAIT` / `PRESENT_ON_ENTRY` /
  `NO_TOKEN`) instead of a bool that could not distinguish "this call minted
  it" from "a concurrent leader already had", nor "nothing to do because
  storage already existed" from "nothing to do because there's no token" —
  each outcome is now logged at DEBUG. The CLI reaches the collapsed
  boolean directly via `bootstrap_missing_storage_from_master_token`
  (auth cross-boundary ledger shrink: `BootstrapOutcome` never needed to
  cross the CLI boundary, since the CLI always collapsed it immediately).
  External behavior for `notebooklm auth refresh` is unchanged.

### Changed

- **The CLI no longer assembles the master-token transaction from minting
  primitives (#2103 structural follow-up, PR-2).** `bootstrap` (oauth_token →
  durable token → minted session) and `refresh` (no-prompt re-mint), formerly
  in `cli/services/login/master_token.py` and `cli/services/auth_refresh.py`,
  moved into `notebooklm._auth.master_token` as
  `bootstrap_from_oauth_token`/`remint_from_stored_token`, exposed via the
  `notebooklm.auth` facade as `master_token_bootstrap`/`master_token_remint`.
  The CLI now invokes these whole, audited transactions instead of composing
  `exchange_master_token` + `mint_cookies` + `persist_minted_jar` +
  `write_master_token` + `generate_android_id` itself; those five primitives
  remain importable from `notebooklm.auth` (de-blessed, not removed) for the
  documented low-level recipe, but the CLI no longer imports them. The L4
  recovery rung (`_auth/recovery.py`) and the operator refresh path
  previously assembled the same read→mint→persist sequence independently and
  disagreed on error handling and reload; both now call the shared kernel,
  `remint_from_stored_token`. `android_id` resolution (explicit → stored →
  generated) moved from the CLI driver into `bootstrap_from_oauth_token`
  itself; the CLI keeps only its cheap pre-capture `read_master_token` probe
  and the inherently-interactive browser `oauth_token` capture.

- **A new lint (#2103 structural follow-up, PR-3) locks in the above:** no
  module under `cli/` may import or attribute-access `exchange_master_token`,
  `mint_cookies`, `persist_minted_jar`, or `write_master_token` from the
  `notebooklm.auth` facade, by name or via a module-alias attribute call
  (`tests/_guardrails/test_master_token_minting_denylist.py`). `cli/`'s
  broader access to `notebooklm.auth` for every other name is unaffected.

- **`persist_minted_jar` gained a `force` keyword (default `False`) and, on
  `notebooklm._auth.storage_writer.persist_minted_jar`, a
  `refuse_unknown_owner` keyword (default `True`).** Existing callers that
  always mint into the same account they already control are unaffected;
  callers assembling a custom transaction that mints across accounts should
  pass `force=True` explicitly.

- **`NOTEBOOKLM_AUTH_JSON` now beats a profile everywhere, as documented.** The
  precedence `--storage` > `NOTEBOOKLM_AUTH_JSON` > profile file is stated in
  `docs/configuration.md`, drawn in `docs/architecture.md`, and implemented by
  `cli.services.auth_source.AuthSource` — but three library call sites spelled
  the predicate themselves and two of them ranked the profile *above* the env
  var. Running any command with both an active profile and inline env auth
  produced a client assembled from **two different accounts**: the CLI resolved
  the storage path through `AuthSource` (`None`, meaning env auth) and passed
  the profile name alongside, so `fetch_tokens_with_domains` re-resolved to the
  profile's `storage_state.json` and minted CSRF/session tokens from it while
  the cookie jar was loaded from the env var
  ([#2083](https://github.com/teng-lin/notebooklm-py/issues/2083)).

  The predicate now lives in one place, `_auth.cookies.resolve_auth_storage_path`,
  so the three sites cannot drift apart again. It also tests *presence* rather
  than truthiness, matching the loader: an empty `NOTEBOOKLM_AUTH_JSON` is a
  configuration error reported by name, not a silent fall-through to a file.

  Relatedly, `NOTEBOOKLM_REFRESH_CMD` no longer fires for env-only auth. It had
  been falling back to `get_storage_path(profile=...)`, so it would lock,
  rewrite and then read a profile file the caller had bypassed — silently
  converting env auth into file auth and mutating a profile that may belong to
  another account. It could not have helped anyway: `NOTEBOOKLM_AUTH_JSON` is
  scrubbed from the refresh subprocess's environment, so the command cannot
  re-mint the credential actually in use. The original auth error is preserved
  and the env-only caller stays side-effect free.

  **What changes for you:** if you set `NOTEBOOKLM_AUTH_JSON` *and* select a
  profile (via `--profile`, `-p`, or `NOTEBOOKLM_PROFILE`), the env var now wins
  consistently instead of half-winning. Use `--storage PATH` to authenticate
  from a file while the env var is set — it still overrides both.

- **A present-but-unusable `__Secure-1PSIDTS` now triggers recovery instead of
  failing on the first RPC.** The cookie-load preflight checked required cookie
  *names* and nothing else, and PSIDTS recovery only ever runs from the `except`
  arm around that preflight — so a `__Secure-1PSIDTS` that was present but
  expired, or scoped to a domain that never routes to `accounts.google.com`,
  satisfied the check, skipped recovery, and surfaced later as an opaque auth
  failure. The loaders now apply the same RFC 6265 routing predicate the recovery
  gate uses, so the two conditions cannot drift apart
  ([#2061](https://github.com/teng-lin/notebooklm-py/issues/2061)).

  Cookie rows also go through one shared shape/expiry normalizer before any
  `http.cookiejar.Cookie` is built, so a row whose `expires` is `""`, `"never"`,
  `nan`, `inf`, or a non-numeric type is skipped with a value-free diagnostic
  rather than raising a bare `float()` coercion error out of a recovery handler
  — including at capture time, so such a row is no longer persisted at all.
  Storage-shape problems (missing file, malformed JSON, no `cookies` list) now
  raise a distinct `StorageStateValidationError` so a configuration failure can
  never be mistaken for a cookie-validation failure and fire a network POST.

  **What changes for you:** on a profile in one of those states, the client now
  makes a `RotateCookies` POST and a storage write during startup where it
  previously went straight to a failing RPC. Nothing that loaded before stops
  loading: a routed, live PSIDTS short-circuits before any POST, and if the heal
  cannot run or does not succeed the load continues exactly as it did before.
  The routing condition is only raised where a recovery attempt follows it, so
  artifact downloads and the read-only `fetch_tokens_passive` probe are
  unchanged — a PSIDTS scoped to the app host is unrotatable, not unusable.
  `auth inspect` likewise keeps probing before validating, so a network outage
  is still reported as a network outage rather than as a bad cookie set.

- **Authentication docs now match the executable secondary-binding rule.** The
  `APISID`/`SAPISID` path also requires bare `LSID` when `OSID` is absent;
  `__Host-1PLSID` and `__Host-3PLSID` do not substitute for it. A documentation
  guard now checks the published predicate against runtime across every cookie
  subset and pins the Tier 1 cookie set
  ([#2074](https://github.com/teng-lin/notebooklm-py/issues/2074)).

- **The rebrand-host health lane now acknowledges the availability transitions
  it already reported.** The authenticated nightly run at commit `36221e0`
  reached `notebook.google.com` with HTTP 200 for both `LIST_NOTEBOOKS`
  (`batchexecute`, with `wXbhsf` echoed) and a parsed
  `GenerateFreeFormStreamed` response. Its checked-in fallback still said both
  capabilities were `ABSENT`, so losing the run-to-run cache could re-file the
  already handled transitions. The fallback now records the last acknowledged
  status per capability (`PRESENT` for both observations); a later contrary
  observation remains a reportable transition. This changes only advisory
  reporting state: the lane still carries no exit code, makes no default-host
  decision, and continues to report upload as `NOT_PROBED` because the workflow
  issues no `/upload/_/` POST
  ([#2077](https://github.com/teng-lin/notebooklm-py/issues/2077),
  [#2078](https://github.com/teng-lin/notebooklm-py/issues/2078)).

- **The pinned frontend build label is current again, and can no longer rot
  unwatched.** `_env.DEFAULT_BL` — the `bl` value sent on the chat streaming
  endpoint — had been pinned to `boq_labs-tailwind-frontend_20260301.03_p0` since
  the day it was introduced, while Google was serving `…_20260802.02_p0`: 154
  label-days of drift that nothing in the repo could have noticed, because
  cassettes replay the recorded value and the offline suite passes regardless.
  The constant is bumped to the served label (re-captured live from the app shell,
  identical on both personal hosts), and the nightly canary gains a **build-label
  lane** that fetches the shell, extracts the served label, and compares. Ordinary
  week-to-week drift passes; only a pin more than `BUILD_LABEL_STALE_AFTER_DAYS`
  (90) behind exits `5` and files a deduped "Pinned frontend build label is stale"
  issue, which clears itself on the next bump. The verdict compares label *dates*,
  never the wall clock, so a delayed run cannot age into an alarm, and exit 5
  ranks below every live-breakage code so a stale pin can never mask an outage.
  Live A/B on the streaming endpoint (2026-08-04) found the server does **not**
  validate this value — the pinned label, the served label, and a fabricated
  `…_19700101.00_p0` each returned a complete, cited answer — so this is hygiene
  against a silent trap, not a fix for a live failure
  ([#2073](https://github.com/teng-lin/notebooklm-py/issues/2073)).

- **A signed-out nightly run no longer reports the rebrand host's endpoints as
  absent.** `is_login_redirect` recognized `accounts.google.com/ServiceLogin` and
  friends but not the app's own bounce, `notebook.google.com/login?continue=…`,
  which is what an unauthenticated request actually receives. That redirect
  scored `ABSENT` — "the endpoint is gone" — on a lane whose stated invariant
  ([#2062](https://github.com/teng-lin/notebooklm-py/issues/2062)) is that a
  signed-out run must draw no conclusion and must not overwrite recorded state.
  The `login` path marker (which subsumes the old `servicelogin` entry) closes it
  ([#2073](https://github.com/teng-lin/notebooklm-py/issues/2073)).

- **RPC errors now name the host, not just the method.** Google serves the
  personal app from two hosts — `notebooklm.google.com` and, since the Gemini
  Notebook rebrand, `notebook.google.com` (ADR-0028) — and
  `NOTEBOOKLM_BASE_URL` selects between them. A wrong-host session previously
  surfaced as `Client error 404 calling LIST_NOTEBOOKS: Not Found`, which is
  indistinguishable from "this account cannot see that notebook", so the one
  recovery lever available was one the user could not tell they needed. Every
  HTTP-status message now reads `… calling LIST_NOTEBOOKS on
  notebook.google.com: …` — 4xx (including the 401/403 fallback), 5xx, and
  429 on both the mapper and the retry-exhausted transport path, which is the
  one a real 429 actually reaches. The host is read from the request that
  failed rather than re-read from `NOTEBOOKLM_BASE_URL`, so a re-point while an
  RPC is in flight cannot make the message name a host that failure never
  touched ([#2067](https://github.com/teng-lin/notebooklm-py/issues/2067)).

- **File-backed clients now recover fully expired cookies during cold start.**
  The same opt-in layer-3 browser recovery and automatic layer-4 master-token
  re-mint used by live clients now run behind the shared initial token fetch,
  with same-loop recovery coalescing and post-recovery cookie/account reloads.
  `AuthTokens.from_storage()` and `NotebookLMClient.from_storage()` gain the
  keyword-only `allow_headless=False` opt-in. `notebooklm auth refresh` gains
  `--allow-headless`, honors root `--storage`, and can mint missing storage from
  a sibling master token before one passive validation. The forced
  `login --master-token-refresh` route remains compatible but is labeled legacy
  in favor of conditional `auth refresh`
  ([#2068](https://github.com/teng-lin/notebooklm-py/issues/2068)).

- **Corrected the last cookie-domain tier claim that still said the ranking is
  decisive.** #2057 rewrote the `_auth_domain_priority` docstring and its two
  consumers, but the tier comment added by
  [#2020](https://github.com/teng-lin/notebooklm-py/pull/2020) still said the
  canonical app host "still wins when both hosts carry" a cookie name. It does
  not: `.notebooklm.google.com` and `.notebook.google.com` are both tier 3, and
  their bare variants both tier 2, so those pairs tie and the winner falls to
  `storage_state` iteration order. Also renames `test_priority_strict_ordering`
  — which asserted "no ties between named tiers" over a sample holding exactly
  one domain per tier, and so passed while four tiers were shared — and adds a
  test pinning the real tie structure. Comments and tests only; no behaviour
  change ([#2054](https://github.com/teng-lin/notebooklm-py/issues/2054)).

### Added

- **Host-contract tests for the RPC path.** `get_batchexecute_url()` /
  `get_query_url()` / `get_upload_url()` are now asserted against a bare host
  literal under the *default* environment — previously covered only under the
  enterprise override, so a change to the default was invisible — and the
  streamed-chat builder is pinned to send no origin-bound header. The latter
  matters because such headers are host-bound and appear in no VCR `match_on`,
  so one naming the wrong host would pass every offline test and fail every
  live call ([#2067](https://github.com/teng-lin/notebooklm-py/issues/2067)).

- **`notebook.google.com` is now an accepted `NOTEBOOKLM_BASE_URL` value.**
  Google serves the personal app from this host after the "Gemini Notebook"
  rebrand, and a live probe on 2026-08-04 reached `batchexecute` on **both**
  personal hosts — the endpoint is dual-served. The base-URL validator therefore
  accepts either, via a new internal `notebooklm._env.PERSONAL_APP_HOSTS` that
  gives the two personal hosts one named home; `_url_utils`' app-host set is
  derived from it instead of repeating the literals. **The default host is
  unchanged** (`https://notebooklm.google.com`), and the rebrand host is
  deliberately *not* documented as a supported value in `docs/configuration.md`:
  it is accepted so that authentication against it is not hard-blocked, but no
  `batchexecute` request to it has ever been captured in `tests/cassettes/` — a
  coverage gap on our side, since the validator rejected the host until now
  ([#1977](https://github.com/teng-lin/notebooklm-py/issues/1977),
  [#2013](https://github.com/teng-lin/notebooklm-py/issues/2013)).

- **The nightly RPC-health check probes the rebrand host in its own reporting
  lane.** `scripts/check_rpc_health.py` runs a `batchexecute` and a streamed-chat
  probe against `notebook.google.com` last in the run, and reports a *state
  change* (`ABSENT->PRESENT`) under its own issue title rather than a recurring
  error. The lane carries **no exit code**, deliberately: the existing
  "Non-transient ERROR detected" issue dedups by title alone, so a rebrand probe
  that legitimately failed every night would file one issue and then suppress
  every later main-lane degradation issue. `UNKNOWN` (transport failure, 429, 5xx)
  carries the previous state forward instead of manufacturing a transition. Two
  new flags support it: `--base-url` (point a whole manual run at a specific
  personal app host) and `--rebrand-state-file` (where the lane reads and writes
  its previous state; omitted means baseline-only, no write)
  ([#1977](https://github.com/teng-lin/notebooklm-py/issues/1977)).

- **`AuthTokens.cookie_header_for(url)` — a domain-correct `Cookie:` header.**
  Selects cookies per RFC 6265 for a specific URL via the session's
  `cookie_jar`, so a cookie scoped to one host is never sent to another. Use it
  instead of `AuthTokens.cookie_header`, which collapses every domain into one
  `name -> value` slot and therefore has to pick an arbitrary winner when the
  same name exists on two hosts. `url` is required — a default would
  reintroduce the fabricated target the method exists to remove — and a
  non-`https` URL raises rather than returning a quietly truncated header
  ([#2054](https://github.com/teng-lin/notebooklm-py/issues/2054)).

### Fixed

- **Browser login now accepts both personal hosts, whichever one is selected.**
  `accepted_login_hosts()` keyed its accept set on the legacy host by name, so
  selecting `notebook.google.com` narrowed the set to that host alone — and
  because Google's login flow can land on either host regardless of which one we
  navigated to, a perfectly good landing was rejected and the login wait ran to
  its timeout. Selecting either personal host now accepts both (the selected one
  first, so the DEBUG line names the host we navigated to). Enterprise has no
  such alias and still accepts only itself
  ([#2013](https://github.com/teng-lin/notebooklm-py/issues/2013)).

- **Partial file-upload failures now retain their recovery context.** If upload
  session start or finalization fails after source registration, the retained
  source row is no longer silently orphaned: the failure carries `source_id` and
  `stage` attributes naming that row (read with
  `getattr(exc, "source_id", None)`), and `notebooklm source add` prints the
  `source delete` needed to retry cleanly (`--json` adds `source_id` / `stage`
  beside the existing code / `retry_after`). The exception **type is unchanged**
  — `AuthError`, `RateLimitError`, `ServerError`, `NetworkError`,
  `ValidationError`, or a bare `SourceAddError` still propagate as themselves, so
  every existing `except` clause around `add_file()` keeps matching. A raw
  `httpx.RequestError` is normalised to `NetworkError` first (httpx exception on
  `.original_error`, `__cause__` unchanged) so a dropped connection projects as
  retryable infrastructure rather than a 4xx input rejection. Wire source type
  code `0` now maps silently to `UNKNOWN` instead of emitting a drift warning
  (#2138).
- **Resumable-upload URL trust is now host-relative, and `Origin`/`Referer`
  derive from the validated upload URL.** Google's Scotty frontend picks which
  personal host it names in the `X-Goog-Upload-URL` response header, so an
  equality check against the configured host would reject a legitimate upload
  once either host can be configured; a personal client now accepts either
  personal host. Any other configured host — enterprise included — stays pinned
  to **exactly itself**, so an enterprise client still rejects a consumer-host
  upload URL rather than streaming enterprise file bytes to the consumer service
  on the say-so of a response header. The upload and cancel requests now send
  `Origin`/`Referer` derived from the *validated* upload URL rather than the
  configured base URL: once the two hosts stand in for each other the two can
  legitimately diverge, and Google's origin-bound auth checks reject a POST to
  host B carrying `Origin: https://hostA`. The headers are built below the
  validation call, so a server-named host can never reach an outbound header
  unvalidated ([#2013](https://github.com/teng-lin/notebooklm-py/issues/2013)).

- **Cookie-scope recovery guidance no longer points at a host the client may not
  be using.** The stale-cookie advice from browser-cookie account discovery and
  the region / anti-abuse gate message both hardcoded
  `https://notebooklm.google.com`. Both now name the **configured** host — the
  one actually probed — because refreshing cookies against, or reproducing a
  per-request gate on, a host this client never calls proves nothing. The
  stale-cookie advice also carries a shared both-personal-hosts scope caveat
  (`app_host_scope_note`, one copy in `_auth/cookie_policy.py`, re-exported
  through `_auth/browser_capture.py` for the CLI boundary), which covers the
  other reading of that failure: the binding cookie exists, but on the sibling
  personal host, so the probe was rejected for scope rather than staleness
  ([#2013](https://github.com/teng-lin/notebooklm-py/issues/2013)).

- **The nightly RPC-health probes no longer send one host's cookies to
  another.** All three authenticated probes in `scripts/check_rpc_health.py`
  — two `batchexecute` calls and one streamed-chat call —
  hand-built their `Cookie:` header from the flat `AuthTokens.cookie_header`
  projection, so the `accounts.google.com`-scoped `LSID` was sent to the app
  host on **every** request, and where a name existed on more than one host
  (`OSID` and `__Secure-OSID` legitimately do, post-rebrand, with different
  values per host) the surviving value was arbitrary — decided by
  `storage_state` iteration order, because the domain-priority tiers are not
  all distinct. The probes now share one client carrying the domain-scoped jar.
  This is the same defect class as [#2019](https://github.com/teng-lin/notebooklm-py/issues/2019)
  / [#2018](https://github.com/teng-lin/notebooklm-py/issues/2018), which fixed
  the script's *auth* path and left its *request* path untouched
  ([#2054](https://github.com/teng-lin/notebooklm-py/issues/2054)).

- **`notebooks.get_or_none()` returns `None` again for a notebook that does
  not exist.** ADR-0019 makes it *the* sanctioned `None`-on-miss lookup, but it
  raised `ClientError` on exactly that input, so every caller of the safe
  lookup was holding an unsafe one — visible only against the live backend,
  not in the docstring or the type signature. The cause sits one layer up:
  `get()` documents that it raises `NotebookNotFoundError` on a miss, and its
  post-validation rests on the premise that the backend answers an unknown id
  with a degenerate payload rather than a proper RPC error. It now answers with
  gRPC status 5, which the decoder surfaces as `ClientError` — a *sibling* of
  `NotebookNotFoundError` under `RPCError`, not an ancestor, so
  `except NotebookNotFoundError` never saw it. `get()` now translates a
  status-5 rejection into `NotebookNotFoundError` and keeps the
  degenerate-payload post-validation as the belt-and-braces path. The match is
  deliberately narrow to status 5: the decoder routes status 7
  (`PERMISSION_DENIED`) through the same `ClientError` branch, and a notebook
  the caller may not read is not a notebook that does not exist. Callers
  wanting the untranslated failure still have `get_raw()`
  ([#2132](https://github.com/teng-lin/notebooklm-py/issues/2132)).

  The **whole diagnostic survives the translation**, not just the chained
  cause: status 5 also covers "the notebook belongs to a different signed-in
  Google account", and `server/_errors.py` documents that the 404 body
  preserves that account-routing guidance verbatim. Since every adapter renders
  `str(exc)` and never `__cause__`, `NotebookNotFoundError` gained optional
  `detail` / `rpc_code` / `found_ids` arguments (all keyword-only with
  defaults, so existing construction is unaffected) and the translation passes
  the decoder's message through. A plain absence with no diagnostic still reads
  exactly `Notebook not found: <id>`. `get_or_none()` folds both meanings of
  status 5 into `None` by construction — its docstring now says so, and points
  at `get()` for callers who need to tell them apart.

- **Canonical gRPC status codes replace scattered magic numbers.** The wire
  statuses at index 5 of a `wrb.fr` entry were spelled as bare literals at
  three layers — the decoder's `(5, 7)` routing, the neutral error classifier's
  `== 5`, and the new notebook not-found translation — with nothing naming the
  namespace, while `RPCErrorCode.NOT_FOUND` is `404` in a *different*
  (HTTP-style) namespace that shares member names. `GrpcStatusCode` in
  `rpc/types.py` is now the single source of truth for those numbers, paired
  with two coercion helpers for the `str | int | None` shape `rpc_code` takes:
  `normalize_rpc_code` (to `int`, keeping HTTP 5xx intact for the transient
  check) and `normalize_grpc_status` (to the enum, for semantic comparisons).
  Both reject `bool` explicitly, since `True` would otherwise coerce to
  `CANCELLED`. `RPCErrorCode` is unchanged.

- **`scripts/diagnose_get_notebook.py` can authenticate again, and honours
  `NOTEBOOKLM_BASE_URL`.** Two independent defects in the same ~40 lines: it
  built its `Cookie:` header by joining `AuthTokens.cookies`, whose keys became
  `(name, domain, path)` tuples in #369 — emitting a syntactically malformed
  `('SID', '.google.com', '/')=…` header, so the script has been unable to
  authenticate for anyone since [#369](https://github.com/teng-lin/notebooklm-py/issues/369) —
  and it read the eager module-level
  `BATCHEXECUTE_URL` constant, ignoring `NOTEBOOKLM_BASE_URL` and so pointing
  enterprise users at the consumer host. Both are fixed together, and the
  script now loads auth domain-preserving (`extract_cookies_with_domains`)
  rather than through the flat map, so its jar routes per host instead of
  broadcasting
  ([#2054](https://github.com/teng-lin/notebooklm-py/issues/2054)).

- **The `__Secure-1PSIDTS` recovery gate now decides by RFC 6265 routing instead
  of a domain-priority ranking.** Whether to fire the healing `RotateCookies`
  POST was decided from a domain-blind "global winner" projection that ranked
  duplicate cookie names by domain tier and read that one winner's `expires` —
  while the POST it gates is routed per RFC 6265. The two could therefore answer
  different questions about the same cookie set. The ranking was also inverted
  relative to the action: `accounts.google.com`, the exact host the POST targets,
  sat in the *lowest* tier, so a host-scoped `__Secure-1PSIDTS` that does route
  was outranked by app-host cookies that do not. And because the tiers are not
  all distinct, the winner within a shared tier depended on `storage_state`
  ordering. The gate now asks whether the `Cookie:` header this jar would send to
  the rotate URL actually carries `__Secure-1PSIDTS`; the separate "did the heal
  land?" check stays domain-blind, because it must predict the retrying
  preflight. No divergence had been observed on measured profiles — this was a
  latent defect whose failure mode is a silent wrong gate decision surfacing as a
  spurious missing-cookie error
  ([#2057](https://github.com/teng-lin/notebooklm-py/issues/2057)).

- **An empty `NOTEBOOKLM_AUTH_JSON` no longer redirects recovery at a profile
  file.** The cookie loader tests the variable's *presence* and fails fast with
  "set but empty", but the recovery path tested its *truthiness* — so an empty
  value fell through to the default profile, where recovery could fire a
  `RotateCookies` POST and persist rotated cookies to a profile the caller had
  deliberately bypassed by setting the variable at all. Both now test presence
  ([#2057](https://github.com/teng-lin/notebooklm-py/issues/2057)).

- **A cookie row with an uncoercible `expires` no longer takes a whole profile
  down.** `http.cookiejar.Cookie.__init__` coerces via `int(float(expires))`, so
  a row whose `expires` is `""`, `"never"`, `nan`, `inf`, or a list raised
  `ValueError: could not convert string to float` out of every cookie loader —
  and on the recovery paths it was raised from *inside* the caller's own
  `except ValueError:` handler, replacing the actionable "Missing required
  cookies … Run `notebooklm login`" message with a converter traceback. One
  malformed sibling row was enough; it did not have to be the PSIDTS row. Rows
  that cannot be converted are now skipped in `build_httpx_cookies_from_storage`,
  `load_httpx_cookies` and the recovery jars alike — a row we cannot convert is
  a row we could never have sent, so dropping it lets the loaders' own
  validation produce the actionable message instead
  ([#2057](https://github.com/teng-lin/notebooklm-py/issues/2057)).

- **`doctor` no longer warns Windows users about permissions the client
  deliberately never sets.** `notebooklm doctor` compared the profile
  directory's POSIX mode against `0o700` on every platform, but the writer
  (`paths._ensure_dir`) intentionally skips `mode=` / `chmod` on Windows —
  pre-3.13 CPython ignores `mode=` in `mkdir()`, and 3.13+ turns it into ACLs
  restrictive enough to block other same-user processes — so the directory is
  left inheriting the parent's ACLs. Every Windows install therefore reported a
  permanent `profile_dir` warn (`permissions: 0o777, expected: 0o700`)
  describing the exact state the client creates on purpose, and `--fix` could
  not clear it because `mkdir(mode=…)` / `chmod` do not move those bits on
  Windows either. The check is now platform-aware: on Windows it passes with a
  detail naming inherited NTFS ACLs (evaluated and not applicable, not silently
  hidden), a missing directory is still a hard `fail`, and `--fix` no longer
  claims a permission change it cannot make
  ([#2046](https://github.com/teng-lin/notebooklm-py/issues/2046)).

  POSIX reporting is unchanged, with one deliberate exception: `doctor --fix`
  used to hard-code the `profile_dir` row to `pass` after a successful
  migration, which reported a green permissions row for a directory that could
  still be world-readable. It now re-runs the check, so a wide-mode directory is
  actually repaired (and reported) instead of being silently greenlit.

- Cookie extraction now requests `notebook.google.com` (the Gemini Notebook rebrand
  host) and the runtime loader ranks its per-product binding cookies
  (`OSID` / `__Secure-OSID`) at the same priority tier as the legacy
  `notebooklm.google.com` host. Before this fix, `notebooklm login --browser-cookies`
  never asked the browser for `notebook.google.com` cookies, and a user whose browser
  only populated `OSID` on the rebrand host could fail the Tier-2 secondary-binding
  check ([#2020](https://github.com/teng-lin/notebooklm-py/pull/2020)).
- **`notebooklm -vv login` now explains the five-minute wait instead of going
  silent.** The interactive login wait emitted no DEBUG output at all, so a
  login that never landed was indistinguishable from a user who walked away —
  the reason the `notebook.google.com` rebrand needed manual triage across
  #2017 / #2022 / #2023 / #2025 / #2028 / #2030 / #2032. `-vv` now logs the
  host(s) that would end the wait plus the starting URL before blocking, one
  line per main-frame navigation observed during it, and the final URL on
  timeout. Every logged URL is reduced to scheme + host — path, query,
  fragment, and userinfo are all dropped, so a federated SSO redirect cannot
  put a one-time assertion into output users paste into public bug reports —
  and the tracing attaches no listener at all unless DEBUG is enabled, so
  behaviour with logging off is unchanged
  ([#2046](https://github.com/teng-lin/notebooklm-py/issues/2046)).
- **Audio overviews now download as `.m4a`, not `.mp3`.** The Audio Overview
  bytes have always been AAC in an ISO-BMFF/MP4 container — the artifact
  metadata itself advertises them as `audio/mp4` — so the `.mp3` name was a
  mislabel that broke players, transcoders, and MIME-sniffing upload endpoints
  in ways that were hard to trace back to the extension. Fixed on all three
  surfaces (CLI `download audio`, MCP `studio_download`, REST
  `POST /v1/notebooks/{id}/artifacts/download`), and the advertised MIME type is
  now `audio/mp4` instead of `audio/mpeg`
  ([#2034](https://github.com/teng-lin/notebooklm-py/issues/2034)).

  > **⚠ Migration.** Only the *derived* filename changed — an explicit output
  > path is still honoured verbatim, so `notebooklm download audio out.mp3`
  > keeps writing `out.mp3` (with the same, still-AAC bytes). What changes is
  > the default name when you pass no path, the per-file names under `--all`,
  > and the `filename` / `mime_type` the MCP and REST surfaces advertise.
  > Scripts that glob `*.mp3` in a download directory should glob `*.m4a`
  > (or `*.m4a` and `*.mp3` during a transition). Note that re-running
  > `download audio --all <dir>` after upgrading writes new `.m4a` files
  > *alongside* any `.mp3` files a previous run left there — the names no
  > longer collide, so nothing is overwritten or auto-renamed.

- **REST `POST /v1/notebooks/{id}/artifacts/download` no longer mislabels a
  non-default `output_format`.** The route named its spool file from the type's
  *default* extension, then served that name as the download filename and
  derived `Content-Type` from it — so a `pptx` slide deck arrived as
  `artifact.pdf` / `application/pdf`, and a `markdown` quiz as `artifact.json`.
  Same defect class as the audio fix above, on the format axis instead of the
  type axis. The MCP `/files/dl` route already resolved both format-aware and
  was unaffected
  ([#2034](https://github.com/teng-lin/notebooklm-py/issues/2034)).
- **Nightly RPC-health and bundle-drift checks no longer report "Authentication
  expired" against valid credentials.** `scripts/check_rpc_health.py` and
  `scripts/capture_rpc_registry.py` loaded auth through the flat
  `load_auth_from_storage()` mapping, which drops every cookie's domain, so
  host-scoped cookies (`OSID` on the NotebookLM host, `LSID` on
  `accounts.google.com`) were broadcast to every `*.google.com` host. Once
  Google's `notebook.google.com` cutover landed, the stale broadcast `OSID`
  collided with the freshly minted host cookie and dead-ended the sign-in chain
  on `accounts.google.com/CookieMismatch`. Flattening also **lost** data
  outright, which is the more serious half: a flat map has one slot per name,
  so where `OSID` legitimately existed on both the app host and
  `accounts.google.com` with different values, the duplicate-name resolution
  silently discarded one of them before any request was built — the sign-in
  host then received a value that was never its own. The health check lost the
  domains only under `NOTEBOOKLM_AUTH_JSON`, where there is no storage file to
  rebuild the jar from; the drift monitor lost them unconditionally, because a
  plain mapping handed to `httpx.get(cookies=...)` carries no domain at all.
  Both now use the domain-preserving stored-auth and cookie-jar loaders shared
  by the managed client lifecycle and CLI, and a
  unit guardrail pins — for both the env-var and the profile-file
  configuration — that the jar mirrors the `storage_state` domains, that a
  host-scoped cookie value never reaches a host it was not scoped to, and that
  the same-named sibling on the other host survives
  ([#2019](https://github.com/teng-lin/notebooklm-py/issues/2019),
  [#2018](https://github.com/teng-lin/notebooklm-py/issues/2018)).

### Changed

- **`scripts/check_rpc_health.py` reports what it authenticated with.** The
  report now names the auth source (`NOTEBOOKLM_AUTH_JSON` vs the resolved
  profile path), the base host, the account route, and the cookie jar's
  `name@domain` scopes — names and domains only, never values. #2019 was a
  cookie-*scoping* failure that surfaced as a generic "authentication expired"
  line, leaving the nightly log with nothing to diagnose it from
  ([#2019](https://github.com/teng-lin/notebooklm-py/issues/2019)).
- **`notebooklm login` now actually auto-installs Chromium**
  ([#2031](https://github.com/teng-lin/notebooklm-py/issues/2031)). The
  pre-flight in `cli/services/playwright_login.py` scraped
  `playwright install --dry-run chromium` for a `"will download"` marker no
  Playwright release emits, so it always returned early and the user hit a raw
  `Executable doesn't exist` error at launch. `--dry-run` cannot answer the
  question in either direction — it prints the same `Install location:` block
  whether or not the browser is on disk — so the probe now resolves
  Playwright's own `chromium.executable_path` (in an isolated, timeout-bounded
  interpreter) and tests that path.
- **Token-extraction failures no longer misreport a wrong page as an expired
  login.** When `SNlM0e` / `FdrFJe` could not be extracted, `extract_csrf_from_html`
  and `extract_session_id_from_html` fell back to scanning the page *body* for any
  `accounts.google.com` URL and, on a hit, raised `Authentication expired or
  invalid. Run 'notebooklm login' to re-authenticate.` — with no URL attached.
  Practically every Google-served page carries such a link, so a valid session that
  simply landed on the wrong page was reported as expired. The scheduled
  `rpc-health` workflow failed this way every day from 2026-07-28 to 2026-08-03
  while the credentials were provably valid, and three users piled onto the
  auto-filed issue diagnosing an unrelated login bug
  ([#2038](https://github.com/teng-lin/notebooklm-py/issues/2038),
  [#2019](https://github.com/teng-lin/notebooklm-py/issues/2019)).

  The body scan is gone; classification is now driven by the authoritative final
  URL, and **every** failure message carries it. Four conditions are now
  distinguishable: the region/anti-abuse gate, a cookie mismatch, a genuine auth
  redirect, and a token-missing response — the last split into "we never reached a
  NotebookLM app host" (a redirect/environment problem) versus "the app answered
  but the token moved" (a real page-structure change worth filing). The
  `"CSRF token not found"` / `"Session ID not found"` / `"Authentication expired"`
  message substrings and the `ValueError` type are unchanged.

- **A `accounts.google.com/CookieMismatch` hop is now its own diagnostic.** It
  means the cookies were sent to a host they were not scoped for — commonly a
  `storage_state.json` whose per-cookie domains were flattened — which needs a
  different fix than re-authenticating. Because the interstitial redirects onward
  to a `support.google.com` help article, it is invisible to the final URL alone,
  so `_fetch_tokens_with_jar` now threads the redirect history into the extractors
  via a new optional keyword-only `redirect_urls` argument on
  `extract_csrf_from_html` / `extract_session_id_from_html` (additive; existing
  positional calls are unaffected)
  ([#2038](https://github.com/teng-lin/notebooklm-py/issues/2038)).

- **`notebook.google.com` is now recognised as a NotebookLM app host** when
  deciding whether a token-less response actually reached the app. Google serves
  the personal app from this alias after the "Gemini Notebook" rebrand, and
  `_auth/browser_capture.url_matches_base_host` already honoured it; without the
  matching entry a genuine app response would have been reported as "the request
  never reached the app". The alias is deliberately *not* added to
  `_ALLOWED_BASE_HOSTS` — that set governs where credentials may be sent, which
  is a narrower question than where a response may have come from
  ([#2038](https://github.com/teng-lin/notebooklm-py/issues/2038)).
- **`login` now explains bundled-Chromium launch failures instead of asking for a bug report.**
  The friendly launch-failure branch was gated on `CHANNEL_BROWSERS` (`chrome` / `msedge`), which
  excludes the *default* bundled Chromium — so every bundled launch failure fell through to a bare
  re-raise and surfaced as `Unexpected error: … This may be a bug` with exit 2. It is now classified:
  a missing download points at `python -m playwright install chromium`, and Windows'
  `spawn UNKNOWN` is named as an execution veto (AppLocker / WDAC / Software Restriction Policies,
  or Defender blocking `%LOCALAPPDATA%\ms-playwright`) with the working alternatives — a system
  browser under `Program Files`, or signing in elsewhere and shipping `storage_state.json` — plus
  an explicit note that `--headless` does not help. `spawn UNKNOWN` on `--browser chrome`/`msedge`
  is covered too, and so is `login --master-token`, whose one-time bootstrap spawns a headed
  browser and previously hit the same raw-traceback dead end. The veto itself is an environment
  policy this client cannot bypass ([#2004](https://github.com/teng-lin/notebooklm-py/issues/2004)).

### Documentation

- `CLAUDE.md`: the VCR match tuple was described as `rpcids` plus decoded body
  shape. It is the full tuple
  `["method", "scheme", "host", "port", "path", "rpcids", "freq"]`
  (`tests/vcr_config.py`) — `host` is part of the match, so replay is pinned to
  the host a cassette was recorded against, which is exactly why rebrand-host
  coverage cannot be inherited from existing cassettes
  ([#1977](https://github.com/teng-lin/notebooklm-py/issues/1977)).
- `docs/adr/0028-gemini-notebook-rename.md`: records the 2026-08-04 probe
  showing `batchexecute` **dual-served** on both personal hosts, replacing the
  earlier claim that the rebrand host was unverified, and separates that from
  this project's own (real) cassette coverage gap
  ([#1977](https://github.com/teng-lin/notebooklm-py/issues/1977)).
- `docs/troubleshooting.md`: the base-URL section now agrees with ADR-0028 — the
  rebrand host is experimental and not the default, rather than an endpoint
  users should expect not to work
  ([#1977](https://github.com/teng-lin/notebooklm-py/issues/1977)).
- `docs/rpc-development.md`: documents the rebrand-host reporting lane — its
  exit-code exclusion and the dedup reasoning behind it, its two flags, and what
  it does not answer (whether the rebrand host serves Scotty `/upload/_/`)
  ([#1977](https://github.com/teng-lin/notebooklm-py/issues/1977)).
- `docs/troubleshooting.md`: new Windows `spawn UNKNOWN` section (why libuv reports `UNKNOWN`
  rather than `ENOENT`/`EACCES`, why headless does not help, and the ship-`storage_state.json`
  path for locked-down hosts), and a correction to the master-token note — the one-time
  `login --master-token` bootstrap **does** launch a browser, so only `--oauth-token` (manual
  paste) and `--cdp-url` (attach) avoid spawning one.

## [0.8.0] - 2026-08-03

The headline of 0.8.0 is **integrations**: NotebookLM is now reachable from AI
agents and HTTP clients through two new adapters built over the shared `_app/`
core (ADR-0021) — an **MCP server** and an experimental **single-tenant REST API
server** — plus a **remote MCP connector** you can self-host for **claude.ai**
(and Claude Desktop / Code / Cursor) behind a Cloudflare Tunnel or Tailscale
Funnel, gated by a single password.

0.8.0 also lands the **breaking half** of the ADR-0019 error contract (the
[#1346](https://github.com/teng-lin/notebooklm-py/issues/1346) umbrella):
"absence and refusal **raise**; only success and async-lifecycle state are
returned." Every flip previewed under `NOTEBOOKLM_FUTURE_ERRORS` in v0.7.0 is
now the default, and the preview flag — together with the dict-subscript /
get-returns-None / kwarg-alias deprecation machinery — has been **removed**
(#1365). See the [Upgrading to v0.8.0](docs/upgrading-to-0.8.0.md) guide and the
**Breaking** section below.

> **⚠ `NOTEBOOKLM_FUTURE_ERRORS` is gone.** It was the v0.7.0 forward-compat
> preview gate; its target behavior is now unconditional, so the flag is a no-op
> (setting it changes nothing). Remove it from your environment / CI config.

### Added

- **MCP file upload now records and can verify a SHA-256 of the uploaded bytes.**
  The `/files/ul` route computes the digest of exactly the received bytes and
  stores it in the completion record (`{source_id, name, size, mime, sha256}`),
  which `await_upload` surfaces. An optional `?sha256=<hex>` lets a client assert
  transit integrity — a mismatch returns a clean `400` before the source is added
  (the single-use token rolls back and stays retryable)
  ([#1889](https://github.com/teng-lin/notebooklm-py/issues/1889)).
- **MCP `await_upload` emits progress keepalives while it polls**, so an MCP host
  no longer treats a long upload wait as a stalled call (`ctx.report_progress` at
  start and on each ~2s tick; best-effort, and a no-op without a client progress
  token) ([#1889](https://github.com/teng-lin/notebooklm-py/issues/1889)).
- **MCP remote `studio_download` returns the report / data-table body inline**
  (bounded, alongside the signed link) so a remote host can read a generated
  report without a second fetch
  ([#1911](https://github.com/teng-lin/notebooklm-py/issues/1911)).
- **`studio_status` reports `media_ready`, `studio_download` reports
  `size_bytes`, and `studio_retry` raises an actionable error** when an artifact
  can't be retried, instead of a bare failure
  ([#1924](https://github.com/teng-lin/notebooklm-py/issues/1924)).
- **Deploy quick start for end users**: the shipped Compose defaults to the
  `:latest` image, and each release now attaches a version-baked
  `docker-compose.yml` + `env.example`
  ([#1904](https://github.com/teng-lin/notebooklm-py/issues/1904)).
- **`await_upload` — confirm a brokered file upload landed.** A new MCP tool that
  waits for a source created via the remote signed-URL upload flow to finish,
  returning the added source (`source_id` / name / size / mime) as soon as the
  browser POST completes — so an agent that brokered an upload can confirm the add
  without polling `source_list`. Accepts the upload URL, the `/u/<shortid>`
  short-link, or the bare token.
  ([#1889](https://github.com/teng-lin/notebooklm-py/issues/1889))
- **Experimental in-app upload widget (opt-in).** With
  `NOTEBOOKLM_MCP_UPLOAD_WIDGET=1` on a remote HTTP deployment, `source_add_widget`
  renders a file picker *inline* in MCP-Apps hosts (claude.ai, ChatGPT) so a mobile
  user can pick and upload **one or more files** (up to 10) without leaving the
  chat — no browser round-trip. Enabling it auto-enables stateless HTTP (which
  MCP-Apps hosts require to fetch the `ui://` widget resource). It stays off the
  default tool surface / budgets unless enabled; the portable signed-link flow
  remains the fallback. See [ADR-0027](docs/adr/0027-mcp-app-upload-widget.md).
  ([#1891](https://github.com/teng-lin/notebooklm-py/issues/1891),
  [#1894](https://github.com/teng-lin/notebooklm-py/issues/1894))
- **`studio_download` `download_ready` now carries `filename` / `mime` /
  `size_bytes`.** The MCP download-ready payload previously returned only the
  local path, so an agent had to stat the file to learn what it got; the
  artifact's server filename, content type, and byte size now travel in the
  envelope. ([#1835](https://github.com/teng-lin/notebooklm-py/issues/1835))
- **Explicit per-state counts in the `source_wait` aggregate.** The wait
  aggregate now reports how many sources finished `ready` vs `error` vs still
  `pending` (rather than a single rolled-up status), so a caller waiting on a
  batch can see partial progress and act on the failures.
  ([#1834](https://github.com/teng-lin/notebooklm-py/issues/1834))
- **`notebooklm skill package` — Claude-uploadable skill archive** (#1856 item 2;
  the docs half landed in #1857). Builds a deterministic ZIP whose root contains
  the `notebooklm/SKILL.md` skill folder (version-stamped, frontmatter-first),
  ready for upload via Claude **Settings → Capabilities** in chat and **Claude
  Cowork** — the supported hand-off for sandboxed agent environments that
  cannot run `skill install` against a local directory. Supports
  `-o/--output` (file or directory), `--force` overwrite protection, and
  `--json` (success payload `{"path", "version", "entries", "size_bytes"}`;
  failures use the standard error envelope with codes `SKILL_SOURCE_MISSING` /
  `OUTPUT_EXISTS` / `WRITE_FAILED`). Release workflow now also attaches `notebooklm-skill.zip`
  to GitHub releases alongside the `.mcpb` bundle.
- **`AccountLimits.tier` — a correct account-tier signal, sourced from the
  authoritative quota block.** The v0.8.0 removal ([#1738]) dropped a tier that came
  from `GET_USER_TIER` (`FetchRecommendations`, a **promotions** endpoint) which could
  not tell free from paid. This adds `tier` read from `GET_USER_SETTINGS` limits[4] —
  the *same* block as `notebook_limit` / `source_limit`, so no extra RPC. It is an
  opaque enum key (not an ordinal): `1`=Free, `2`=Pro, `4`=Plus, `3`/`6`=Ultra
  (20 TB / 30 TB), `5`=Expanded (legacy); only `1` and `2` are live-confirmed. The
  MCP **and** REST `server_info(include_account=True)` account blocks now include a
  `tier` key. The removed `plan_name` label and the promotions RPC/`AccountTier` type
  stay gone. ([#1738](https://github.com/teng-lin/notebooklm-py/issues/1738))

- **Short-form video format** across the client, CLI, MCP, and REST
  ([#1805](https://github.com/teng-lin/notebooklm-py/issues/1805)). Video
  generation now takes a format selector so you can request a **short** video in
  addition to the default full-length one. The server ignores a custom style on
  the short format, so passing one is rejected up front rather than silently
  dropped ([#1805](https://github.com/teng-lin/notebooklm-py/issues/1805)
  follow-up).

- **MCP `source_add` gains in-channel bytes and add-and-wait modes**
  ([#1803](https://github.com/teng-lin/notebooklm-py/issues/1803),
  [#1807](https://github.com/teng-lin/notebooklm-py/issues/1807),
  [#1890](https://github.com/teng-lin/notebooklm-py/issues/1890)). Rather than
  separate `source_upload_bytes` / `source_add_and_wait` tools, both fold into
  `source_add`: `source_add(source_type="file", bytes_base64=…, filename=…)` adds a
  small file by sending its bytes directly through the tool call (for network-boxed
  agents that cannot reach the signed browser-upload URL), and
  `source_add(..., wait=True, timeout=…, interval=…)` composes the add with
  `source_wait` for single-source adds — returning the `source_wait` aggregate plus a
  top-level `source_id`. This keeps the MCP surface leaner (ADR-0025): one `source_add`
  verb with input modes, not three near-duplicate add tools (MCP tool surface 36 → 34,
  schema-char budget ratcheted down).

- **Compact roster mode for `source_list` and `studio_list`**
  ([#1806](https://github.com/teng-lin/notebooklm-py/issues/1806)) — a terser
  listing shape that fits more entries into an agent's context window.

- **MCP strict IDs-only mode + canonical-id echoes**
  ([#1808](https://github.com/teng-lin/notebooklm-py/issues/1808)). Opt into
  rejecting name-based refs and having tools echo the canonical id they resolved,
  so an agent can pin every subsequent call to an unambiguous id.

- **Near-miss candidates on failed name lookups**
  ([#1787](https://github.com/teng-lin/notebooklm-py/issues/1787)). When a
  notebook/source/note/artifact name doesn't resolve, the error now lists the
  closest matches instead of a bare not-found.

- **Experimental: MCP server** (#1484, opt-in via the `mcp` extra). A
  [Model Context Protocol](https://modelcontextprotocol.io) server exposing
  NotebookLM to MCP clients (Claude Desktop / Code, Cursor, Windsurf) as 34 tools
  across notebooks, sources, chat, notes, studio artifacts, and research — built
  as a transport-neutral sibling adapter over the `_app/` layer (ADR-0021), so it
  behaves identically to the equivalent `notebooklm` CLI command. Run it with the
  `notebooklm-mcp` console script (stdio by default, or loopback HTTP via
  `--transport http`); wire it into a client with `notebooklm mcp install
  <client>` or the one-click `.mcpb` desktop bundle. Notebook- and source-scoped
  tool arguments accept a **name or an id**; destructive tools require
  `confirm=true` (returning a `needs_confirmation` preview otherwise); long-running
  generation is non-blocking (`*_generate` returns a `task_id` to poll via
  `*_status`, then download). **The MCP tool surface is experimental and not
  covered by the library's semver guarantees** — names, parameters, and output
  shapes may change between releases. `pip install notebooklm-py` is unaffected:
  the server and its dependencies (`fastmcp`) arrive only with the `mcp` extra.
  See [docs/mcp-guide.md](docs/mcp-guide.md).

- **Experimental: remote MCP connector — self-host NotebookLM for claude.ai,
  Claude Desktop, and Cursor** (#1645, #1647). The MCP server now also runs as a
  **remote HTTP connector**, not just a local stdio process: `notebooklm-mcp --transport http`
  serves the same tools over HTTP behind a bearer token, and `deploy/` ships a
  one-command Docker stack (`make dev`) that exposes it through a **Cloudflare
  Tunnel** or a **Tailscale Funnel** — no public IP, no open port, no TLS cert to
  manage. For **claude.ai** (whose connector UI is OAuth-only and has no bearer
  field), the server can run its own tiny **OAuth authorization server gated by a
  single password** (`NOTEBOOKLM_MCP_OAUTH_PASSWORD`) — no external IdP, no JWT
  template; registered clients and tokens persist across restarts. Opt-in and
  additive: leave the OAuth vars unset to stay bearer-only (Claude Code / Desktop
  unaffected). The server **fail-closed** refuses to start on a non-loopback bind
  with no auth, or on partial / weak / non-HTTPS OAuth config. Single-tenant,
  self-hosted, one container per Google account. **The remote-deployment surface
  is experimental and may change between releases.** See
  [deploy/README.md](deploy/README.md) and
  [docs/mcp-guide.md](docs/mcp-guide.md).

- **Experimental: single-tenant REST API server** (#1538, opt-in via the `server`
  extra; console script `notebooklm-server`). A FastAPI app exposing guarded
  `/v1` routes — notebooks, sources, chat, notes, studio artifacts, sharing, and
  research — over the same transport-neutral `_app/` core the CLI and MCP server
  use (ADR-0021), so behavior matches the equivalent CLI command. Loopback-bound
  by default, requires `NOTEBOOKLM_SERVER_TOKEN`, and refuses an unauthenticated
  non-loopback bind. Native multipart source upload and `FileResponse` artifact
  download (no signed-URL broker needed). Follow-up work closed the remaining
  REST↔MCP capability and hardening gaps (#1620 notes CRUD, #1767). **The REST
  surface is experimental and not covered by the library's semver guarantees.**
  `pip install notebooklm-py` is unaffected — FastAPI / uvicorn arrive only with
  the `server` extra. See
  [docs/installation.md#rest-api-server](docs/installation.md#rest-api-server).

- **Master-token headless auth** (#1638, #1640; ADR-0023; opt-in via the new
  `headless` extra). NotebookLM's browser cookies are short-lived, and until now
  the only way to re-acquire them was a human re-running `notebooklm login` in a
  browser — fatal for long-lived headless / CI / server use. A durable Google
  **master token** (minted once via `notebooklm login --master-token`, then stored
  `0600` beside the profile) can now **re-mint fresh web cookies on demand with no
  per-session browser**. Recovery is automatic: when a `master_token.json` is
  present, an expired session re-mints in-process as the final layer of the
  refresh ladder (single-flight, so concurrent RPCs coalesce one re-mint) instead
  of raising "run 'notebooklm login'". `notebooklm auth check` surfaces the
  master-token identity and `--master-token-refresh` re-mints on demand. This is
  the foundation that makes the remote MCP connector and unattended deployments
  viable. **Security:** the master token is a **full-account, long-lived**
  credential — store it like a password and prefer a dedicated / throwaway account
  for servers. It and its dependency (`gpsoauth`) arrive only with the `headless`
  extra. See [docs/installation.md](docs/installation.md) and
  [docs/auth-cookie-lifecycle.md](docs/auth-cookie-lifecycle.md).

- **Source labels** (#1474). A new `client.labels` namespace and a `label` CLI
  command group bring NotebookLM's source-label (tag) feature to the client:
  list / create / rename / delete labels and assign or clear them on sources, then
  narrow a listing with `source list --label <name>` (the MCP `source_list` tool
  also gained a label filter). Additive new public surface across the Python API
  and CLI.

- **Suggested prompts** (#1612, #1616). New `client.chat.suggest_prompts(...)`
  returns model-generated starter prompts for a notebook (backed by the
  `GeneratePromptSuggestions` RPC), surfaced as a `notebooklm suggest-prompts` CLI
  command and, later, an MCP `suggest_prompts` tool (#1726). A `mode` selector
  targets the surface the suggestions are for (ask / audio / video / quiz /
  flashcards / …). Additive new public surface.

- **Opt-in `curl_cffi` browser-impersonation transport** (#1632). An alternative
  HTTP backend that mimics a real browser's TLS fingerprint, selected via
  `NOTEBOOKLM_TRANSPORT=curl_cffi` (requires the `curl_cffi` package), for
  environments where the default `httpx` transport is fingerprint-blocked. It sits
  behind the existing `async_client_factory` seam; the default transport is
  unchanged.

- **`--json` on every local CLI command** (#1643). The remaining local commands
  gained a `--json` flag — now enforced by a coverage guardrail — so any
  `notebooklm` command can emit machine-readable output for scripting and
  automation.

- **MCP soft-404 body-pattern detection.** The `source_wait` content-sanity check
  (#1698) now also flags a READY web page that sails past the thin-text threshold
  but whose short body matches a dead-link / error-page boilerplate phrase (e.g.
  "Whoops! broken link") — a soft-404 that ingests as a full-bodied 200. Body-only
  (titles are never scanned), gated to sub-2000-char bodies, advisory-only (never
  blocks), zero extra RPC (the body is already fetched). The batch
  `source_add(urls=[...])` now surfaces the same warning per synchronously-ready
  web-page item.
- **MCP artifact rename & delete tools.** Two new MCP tools close the artifact
  CRUD gap (the server previously exposed create + read only): `artifact_rename`
  (title-only update) and `artifact_delete` (destructive, two-step `confirm`).
  Both accept a notebook/artifact name **or** ID, cover every studio artifact
  type including both mind-map kinds — note-backed maps route through the note
  system, interactive maps and regular artifacts through the artifact RPC — over
  the shared kind-aware `_app.artifacts` cores the CLI already uses. The MCP tool
  surface is now **28 tools**.
- **Live-API e2e coverage for the MCP server and the CLI binary.** The nightly
  E2E job now installs `--extra mcp`, so the MCP/CLI layers run against the real
  NotebookLM API once per release instead of being silently `importorskip`-ped.
  New suites: per-domain MCP tool round-trips plus a 28-tool→test matrix
  (`tests/e2e/test_mcp.py`); the HTTP transport, bearer gate, `.well-known`
  discovery, and signed-URL upload/download routes driven in-process over
  `httpx.ASGITransport` (`tests/e2e/test_mcp_http.py`); live-only contract
  checks — the `source_ids` omitted-`==`-`[]`-`==`-all collapse (#1652),
  not-found resolution, destructive confirm-gating, and the MCP error shape
  (`tests/e2e/test_mcp_contracts.py`); and a CLI-binary `--json` smoke including
  stdout-purity on a live failure (`tests/e2e/test_cli_live.py`). A standalone
  `scripts/mcp_live_smoke.py` runs the upload+download round-trip against a
  deployed server (PASS/FAIL) to bootstrap the new per-release manual
  "MCP connector smoke" checklist in `docs/releasing.md`.
- **Remote MCP file upload & download** (ADR-0024). Over the remote (HTTP)
  connector — where the claude.ai browser can't carry the MCP credential and the
  JSON-RPC channel can't carry bytes — `source_add type=file` and
  `artifact_download` now broker a **short-lived HMAC-signed URL** served by the
  same container, and the browser does the byte transfer out-of-band (the
  established remote-MCP pattern; MCP has no native upload primitive and its
  native binary-Resource download is capped far below a podcast/video). Upload
  accepts a raw body over `POST`/`PUT` (a browser file-picker page **or** a
  code-execution-sandbox `curl`), bounded by a 200 MiB per-request cap and an
  in-flight-upload limit; download returns a clickable `resource_link`. Enabled
  by `NOTEBOOKLM_MCP_PUBLIC_URL` (falls back to `NOTEBOOKLM_MCP_OAUTH_BASE_URL`);
  unset → the two tools return a clear "not configured" error and the server
  still starts. **stdio (local) installs are unchanged** — they keep reading and
  writing real local paths. The REST `server` extra already supports native
  multipart upload + `FileResponse` download and is unaffected. See
  [docs/mcp-guide.md](docs/mcp-guide.md#file-upload--download-remote).
- **Retrieve the generation prompt behind an artifact** (#1571). New
  `client.artifacts.get_prompt(notebook_id, artifact_id)` returns the free-text
  prompt an artifact was generated from, and a matching `artifact get-prompt`
  CLI command prints it (with `--json`). Works for every studio artifact type —
  audio, report, video, quiz, flashcards, interactive mind map, infographic,
  slide deck, and data table — by reading the prompt already present in the
  `LIST_ARTIFACTS` response through the new `ArtifactRow.generation_prompt`
  accessor (no new RPC). Returns `None` for an artifact with no stored prompt
  (e.g. a note-backed mind map) and raises `ArtifactNotFoundError` for an
  unknown id. The transport-neutral `_app.artifacts.get_artifact_prompt` exposes
  the same behaviour to the MCP/HTTP adapters.
- **Custom prompt for interactive mind maps.** `instructions` is now sent for
  interactive (studio-artifact) mind maps, not just note-backed ones. The
  interactive `CREATE_ARTIFACT` payload carries the free-text prompt at the
  `[9][1][2]` slot of its options block — the same slot quizzes and flashcards
  use — and the NotebookLM server honors it for variant 4 (verified live: the
  prompt steers the generated node tree, and reads back via
  `artifacts.get_prompt`). Previously `client.mind_maps.generate(...,
  kind=INTERACTIVE, instructions=...)` and `notebooklm generate mind-map --kind
  interactive --instructions ...` silently dropped the prompt with a warning;
  both now apply it. The no-prompt request shape is unchanged. (Note-backed maps
  still pass `instructions` through `GENERATE_MIND_MAP`, but the server does not
  reliably act on them.)

- **Passive auth validation for unattended monitors** (#1569). New
  `notebooklm.auth.fetch_tokens_passive(...)` validates the cookies on disk with
  a strictly read-only token fetch — it never runs `NOTEBOOKLM_REFRESH_CMD`,
  never fires the layer-1 keepalive rotation poke, and never writes
  `storage_state.json` (additive public symbol; the active
  `fetch_tokens_with_domains` is unchanged). `notebooklm auth check --test
  --passive` routes the token probe through it, so a systemd/cron health check
  can answer "do the cookies currently authenticate?" without mutating state,
  spawning a subprocess, or racing real work. The transport-neutral
  `run_auth_check(AuthCheckPlan(..., passive=True))` exposes the same probe to
  the MCP/HTTP adapters.

- **`notebooklm auth refresh --verify`** (#1569). After a refresh completes,
  runs the passive token probe to confirm the resulting cookies actually
  authenticate, exiting non-zero if they still redirect to sign-in. A successful
  refresh command alone does not prove the post-refresh cookies work — this is
  especially valuable with `--browser-cookies`, which rewrites the cookie jar
  but does not otherwise verify it.

- **macOS login recovery hint.** When the bundled-Chromium interactive login
  times out (`Login not detected within 5 minutes`), the message now suggests
  retrying with `notebooklm login --browser chrome` to reuse an already
  signed-in system Chrome session — which often detects immediately and sidesteps
  bundled-Chromium issues on macOS.

- **Layer-3 headless re-auth: `client.refresh_auth(allow_headless=True)`** (#1525,
  P2; P1 was #1512). When NotebookLM's first-party cookies are fully dead — the
  homepage GET 302s to the Google login page and neither token refresh (L1) nor
  `RotateCookies` rotation (L2) can help — a persisted browser profile may still
  hold a live Google SSO session that outlives `storage_state.json`. The new
  opt-in re-auth layer drives an unattended **headless** browser against that
  profile to silently re-mint cookies, then retries. It is **explicit by
  default**: `refresh_auth(allow_headless=True)` (additive keyword-only,
  defaults to `False`) triggers it on demand, and a *mid-RPC* auto-fire happens
  only when `NOTEBOOKLM_HEADLESS_REAUTH=1` is set — L3 **never** fires by
  default, so behavior with no opt-in and no profile is unchanged. Outcomes are
  typed and honest (re-minted / profile-session-also-dead / unavailable) and it
  never reports success on dead tokens. **SECURITY:** the persistent profile is
  an account-equivalent credential; L3 is **local-unattended-only** and must not
  be the auth path for a remote / hosted MCP server. It reuses the existing
  cookie-domain allowlist and never logs a captured cookie value.

- **`client.mind_maps.list_note_backed(notebook_id)`** — typed list of only
  the **note-backed** mind maps (every `kind` is `NOTE_BACKED`, `tree`
  populated, deleted rows excluded) via a single `GET_NOTES_AND_MIND_MAPS`
  RPC — no `LIST_ARTIFACTS`. Factored out of `mind_maps.list()` (which now
  builds on it) and used by the CLI `artifact delete` carve-out probe so the
  note-backed membership check is fully typed while keeping the historical
  single-RPC call set (recorded cassettes replay unchanged).

- **Schema-drift observability: `rpc_decode_errors` counter + chat drift canary**
  (#1492). Wire-schema drift is the stated #1 breakage class, but
  decode/drift failures (`DecodingError` / `UnknownRPCMethodError`) were
  invisible to metrics — they did not even reach the transport-leg
  `rpc_calls_failed` counter (the middleware chain wraps only the transport
  leg; decode happens after). `ClientMetricsSnapshot` now exposes a dedicated
  `rpc_decode_errors` counter (additive, defaults to `0`, appended at the end
  of the dataclass so existing positional construction is unaffected),
  incremented at the executor's response-decode boundary whenever a decoded
  response envelope is rejected as drift — both the wrapped shape-drift case
  (bad JSON / missing key-or-index) and a surfaced `DecodingError` /
  `UnknownRPCMethodError` from the envelope decoder. A decoded *semantic* error
  (rate-limit, not-found, auth) is not drift and does not bump the counter; a
  drift error recovered by refresh-and-retry is not counted. (Positional drift
  raised later by feature-layer `safe_index` navigation, after `rpc_call`
  returns, is not yet routed through this counter — a tracked follow-up.)
  Operators can now alert on "Google reshaped a response" distinctly from
  ordinary 5xx / network failures. Separately,
  `scripts/check_rpc_health.py` now probes the streamed-chat orchestration RPC
  `GenerateFreeFormStreamed` — a `PATH_NOT_METHOD` (`v1` URL) endpoint with no
  obfuscated method ID — by asserting a 200 plus a recognizable stream frame,
  closing the gap where the chat surface escaped the daily drift canary.

### Documentation

- **Per-tier quota & limit reference (`docs/quota-limits.md`).** A new reference
  documenting NotebookLM's published static plan limits — notebooks, sources,
  chats, and studio artifacts across consumer (5 tiers), Workspace (5 levels), and
  Enterprise — keyed to the `AccountLimits.tier` int, with evidence classes for the
  features Google leaves unquantified and a note that live per-account
  remaining/reset counters are not API-exposed. Distilled from the research in
  [#1825](https://github.com/teng-lin/notebooklm-py/issues/1825); cross-linked from
  the `tier` field, `rpc-reference.md`, `mcp-guide.md`, and `python-api.md`.
- **Sandboxed agents (Claude Cowork / headless).** Documented running
  `notebooklm-py` from Claude Cowork and similar no-display sandboxes in
  `SKILL.md` and `docs/installation.md`: per-session `pip install` bootstrap (no
  `[browser]` needed for queries — that extra is only for the interactive
  `login`, which runs on a host machine), and reusing a host-generated
  `storage_state.json` via the root `--storage` flag or `NOTEBOOKLM_AUTH_JSON`.
  ([#1856](https://github.com/teng-lin/notebooklm-py/issues/1856))


### Changed

- **MCP: the standalone `studio_get_prompt` tool was folded into `studio_list`**
  (net −1 tool, ADR-0025 tool-surface consolidation). Each artifact's
  `generation_prompt` (the free-text prompt it was generated from, `null` when it
  records none) now rides the default `studio_list` summary listing, and
  `studio_list(item=<artifact>)` returns it for a single artifact resolved by id or
  exact title (the unified cross-type Studio resolver, as `studio_delete` /
  `studio_rename` use — not the old artifact-scoped title-prefix resolver). The
  prompt is decoded from the same `LIST_ARTIFACTS` row the listing already fetches,
  so this adds no extra request. The CLI `notebooklm artifact get-prompt` command
  is unchanged.
  ([#1896](https://github.com/teng-lin/notebooklm-py/issues/1896))
- **The in-app upload widget's token pool gets a longer (1h) TTL.** The widget
  mints one single-use token per file up front but uploads them sequentially, so
  a later file in a large batch could hit an expired token mid-upload; the pool
  now uses `WIDGET_UPLOAD_TTL` instead of the 15-minute default. The single-use /
  signed-token model is unchanged
  ([#1894](https://github.com/teng-lin/notebooklm-py/issues/1894)).
- **MCP name refs resolve by unique title prefix** (#1786). Notebook, source,
  note, and artifact name arguments now match on a unique title *prefix*, not
  just an exact title, so an agent can address an entity by the shortest
  unambiguous fragment; an ambiguous prefix is a resolution error listing the
  candidates.

- **MCP research tools now accept the poll id under one name, `poll_task_id`.**
  `research_status`, `research_import`, and `research_cancel` previously took the
  value that `research_start` / `research_status` surface as `poll_task_id` under
  mismatched parameter names (`task_id` on status/import, `run_id` on cancel),
  forcing docstring warnings about which id goes where and inviting copy-paste
  mistakes. All three now accept it as `poll_task_id`, so the value pastes
  verbatim from one tool's output into the next. The old `task_id` / `run_id`
  names still work as **deprecated aliases** (removed in v0.9.0): passing one
  emits a `DeprecationWarning` and adds a `deprecation` note to the result;
  passing both names with different values is a validation error.
  ([#1789](https://github.com/teng-lin/notebooklm-py/issues/1789))

- **MCP `source_wait` now returns one unified per-source aggregate** (#1669).
  Both modes — waiting for a single `source` or for every source in the notebook
  — return the same shape: `{notebook_id, ok, ready, timed_out, failed,
  not_found}`. `ready` carries the sources that reached READY (with
  `kind`/`status_label` labels); the three error buckets carry
  `{source_id, error}` entries; `ok` is `true` iff all error buckets are empty.
  Previously the single-source mode returned `{status: ready|not_found|failed|
  timeout}` while the all-sources mode **threw on the first failure and discarded
  every source that had already become ready** — the all-sources mode now reports
  **partial progress** instead. (A `source` reference that does not resolve still
  raises NOT_FOUND before the wait — an input error, distinct from a resolved
  source the backend reports missing/failed/slow.)
- **Regenerable test baselines (Phase 1; contributor-facing, no public API
  change).** Frozen public-surface snapshots that were hand-typed copies of
  values the code already derives — `_FROZEN_TYPES_ALL` and
  `_UNGATED_PUBLIC_ALL_SNAPSHOT` in
  `tests/_guardrails/test_public_surface_manifest.py` — are now *derived*
  baselines committed under `tests/fixtures/baselines/` (`types_all.json`,
  `ungated_surface.json`) and registered in `tests/_baselines/registry.py`
  alongside the existing CLI contract. A single freeze test diffs each committed
  file against `derive()`; a dev-only `--update-baselines` pytest flag (wrapped by
  `python scripts/regen_baselines.py`) regenerates them. Adding a public symbol is
  now one regen command plus a reviewed diff instead of hand-editing several
  literals. CI never regenerates — it only diffs (the regen flag is refused under
  `CI`). The authored `_DOCUMENTED_PUBLIC_IMPORTS` (promised-import *intent*) and
  `_TOP_LEVEL_TYPE_EXPORTS` (fuzzy derivation) stay hand-curated. See
  [ADR-0022](docs/adr/0022-regenerable-baselines.md).
- **`notebooklm.rpc` public surface narrowed to the two documented power-user
  imports** (#1589). `notebooklm.rpc.__all__` now lists only `RPCMethod` and
  `resolve_rpc_id`; the ~47 other names it used to advertise (the batchexecute
  wire helpers `encode_rpc_request` / `decode_response` / `extract_rpc_result` /
  …, the endpoint URL constants and helpers, `safe_index`, and the enum /
  exception **re-exports** that remain public under their canonical names —
  most enums as `notebooklm.<X>` / `notebooklm.types.<X>`, the exceptions as
  `notebooklm.<X>` / `notebooklm.exceptions.<X>`, with `ArtifactStatus` /
  `artifact_status_to_str` `notebooklm.types`-only and `ArtifactTypeCode` having
  no public alias) are no longer part of the
  blessed, compat-gated public surface. This aligns the audited surface with
  `docs/stability.md`, which has always marked `notebooklm.rpc.*` internal. **Not
  a removal:** every name stays importable as `notebooklm.rpc.<name>` for
  back-compat — only the public-API *advertisement* shrank. New code should
  import the canonical public name (or `RPCMethod` / `resolve_rpc_id` for
  raw-RPC power use). See [docs/deprecations.md](docs/deprecations.md).

- **`notebooklm.auth` public surface narrowed (PR-1)** (#1592). `auth.__all__`
  drops 23 internal re-exports that only first-party `src`/tests imported (the
  cookie-snapshot/storage helpers `save_cookies_to_storage` / `snapshot_cookie_jar`
  / `CookieSnapshot*` / `CookieSaveResult` / `advance_cookie_snapshot_after_save`,
  the WIZ-extraction helpers `extract_csrf_from_html` / `extract_session_id_from_html`
  / `extract_wiz_field`, `authuser_query` / `format_authuser_value`,
  `load_httpx_cookies` / `normalize_cookie_map`, `ALLOWED_COOKIE_DOMAINS` /
  `MINIMUM_REQUIRED_COOKIES`, the env/URL constants `KEEPALIVE_ROTATE_URL` /
  `NOTEBOOKLM_REFRESH_CMD_ENV` / `NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV` /
  `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV`, `load_auth_from_storage`, `fetch_tokens`,
  and `recover_psidts_in_memory`). These were migration leftovers from the
  `_auth/*` extraction (ADR-0003 → ADR-0014); `docs/stability.md` has always marked
  `notebooklm.auth.*` internal. **Not a removal:** every name stays importable as
  `notebooklm.auth.<name>` for back-compat — first-party code now imports them from
  their `notebooklm._auth.<sub>` home. The documented imports (`AuthTokens`,
  `convert_rookiepy_cookies_to_storage_state`, the cookie-domain constants) and the
  cohesive operations (`enumerate_accounts`, `fetch_tokens_with_domains`,
  `fetch_tokens_passive`, …) are unchanged. See
  [docs/deprecations.md](docs/deprecations.md).

### Removed

- **`SettingsAPI.get_account_tier()` and the `AccountTier` type** (BREAKING).
  `client.settings.get_account_tier()`, `notebooklm.AccountTier` /
  `notebooklm.types.AccountTier`, and the underlying `GET_USER_TIER` RPC are
  removed. The tier came from `FetchRecommendations`, a **promotions** endpoint,
  and was a promotion-eligibility signal that could not distinguish free from
  paid — both a free and a Pro account reported
  `NOTEBOOKLM_TIER_PRO_CONSUMER_USER`. Use `client.settings.get_account_limits()`
  (`AccountLimits.notebook_limit` / `source_limit`) for quota decisions instead.
  The MCP `server_info(include_account=True)` account block drops its `tier` and
  `plan_name` keys (now `{email, authuser, available, notebook_limit,
  source_limit, output_language}`). See
  [docs/deprecations.md](docs/deprecations.md).

### Fixed

- `notebooklm login --storage <path>` now gives each custom storage file an
  isolated persistent browser profile, with login, logout, `status --paths`,
  doctor's L3 readiness row, and L3 headless re-auth resolving the same
  storage-specific path. New explicit-storage browser directories carry an
  ownership marker: `login --fresh` refuses to recursively delete unowned
  sidecars and logout leaves them intact (and reports the preserved path).
  Canonical legacy and named-profile layouts remain managed even when selected
  through `--storage`. Very long storage filenames use a stable canonical-path hash to
  stay within filesystem component limits. Existing custom-storage browser
  sessions are not auto-migrated because the old shared profile cannot be safely
  attributed to a storage file or account. (#2026)
- `notebooklm login --master-token` now starts Playwright with the required Windows
  event-loop policy, avoiding a pre-browser `NotImplementedError` (#2011).
- `notebooklm login` now recognizes `notebook.google.com` as the personal app landing
  host after the Gemini Notebook rebrand, preventing successful browser sign-ins from
  timing out after five minutes. Enterprise and RPC base-host validation remain
  unchanged. (#2013)
- `server_info(include_account=True)` (MCP tool and the REST `GET /v1/server/info`
  route) now adds an `output_language_is_default` boolean to the account block. When
  the account has never set an explicit output language, `output_language` is `null`
  **and** `output_language_is_default` is `true`, signalling that the account uses
  NotebookLM's default language rather than a bare null that reads as
  missing/broken. A genuinely unparseable settings response still degrades to
  `available: false` (so it stays distinguishable from the unset-uses-default case).
- **Self-hosted MCP OAuth: switching the served account no longer resets the client
  registry.** OAuth-registered clients + issued tokens (`oauth_state.json`) were stored
  under the *served account's profile dir*, so pointing the server at a different profile
  silently orphaned every connected client (ChatGPT / claude.ai → "Client ID not found").
  The OAuth state is now **deployment-scoped**, keyed on `NOTEBOOKLM_MCP_OAUTH_BASE_URL`
  (the OAuth issuer) at `<home>/oauth/<slug>.json` — independent of the served profile,
  and distinct per issuer so two servers on one host stay isolated. New
  `NOTEBOOKLM_MCP_OAUTH_STATE_PATH` overrides the path; the Docker deploy mounts a
  dedicated `/data/oauth` volume (`NOTEBOOKLM_OAUTH_STATE_DIR`). Existing installs are
  migrated once on startup (the legacy profile-dir `oauth_state.json` is copied over, then
  renamed `.migrated` so it is never re-read). This deliberately reverses the profile-dir
  coupling introduced in #1765, and applies the deployment-vs-data separation that the
  proposed multi-profile server
  ([#1901](https://github.com/teng-lin/notebooklm-py/issues/1901)) builds on — without
  implementing it.
- **`source_add` now honors an explicit `title` for YouTube, Google Drive, and
  web-page imports.** These source types re-derive the display title server-side
  (from the video / Drive / page metadata), so a caller-supplied `title` was
  silently dropped. `SourcesAPI.add_url` (new optional `title=`) and
  `add_drive` now apply the requested title via a best-effort post-add
  `rename` — a rename failure is non-fatal (the added source is kept with its
  upstream title and a warning is logged). The MCP `source_add` tool flags a miss
  with `title_override_applied: false` + a `warning`
  ([#1960](https://github.com/teng-lin/notebooklm-py/issues/1960)).
- **Fresh conversations are no longer reported as follow-ups.** A null-conversation
  ask now checks whether the server-assigned current conversation has any turns
  before setting `AskResult.is_follow_up` ([#1973](https://github.com/teng-lin/notebooklm-py/issues/1973)).
- **MCP `source_add` bytes upload no longer defaults to a generic
  `upload.bin`.** Passing `bytes_base64` without an explicit `filename` used to
  spool the file as `upload.bin`, which NotebookLM rejected with an HTTP 400
  ("file type unsupported") even for plain text. The filename/extension is now
  seeded from the `title` and/or `mime_type` (falling back to `upload.bin` only
  when neither yields one), so a bytes upload succeeds without a caller-supplied
  filename ([#1955](https://github.com/teng-lin/notebooklm-py/issues/1955)).
- **Research import is now idempotent.** Re-importing a completed research task
  no longer duplicates its sources: `import_sources_with_verification` (and the
  MCP `research_import` tool that drives it) pre-filters requested sources whose
  URL already exists in the notebook on *every* attempt, not only on the
  timeout-retry path. A repeat import adds nothing and reports the skipped
  sources — the MCP result gains `newly_imported` / `already_present` (with the
  existing source ids) and a `status` of `already_imported`. Pass
  `allow_duplicate=True` (MCP) to force a re-add. Report / pasted-text entries
  have no dedupable URL and are unaffected
  ([#1961](https://github.com/teng-lin/notebooklm-py/issues/1961)).
- `chat.ask()` (and the `chat_ask` MCP tool / `AskResult`) now report
  `is_follow_up=True` when an implicit ask (no `conversation_id`) continues the
  notebook's existing current conversation, instead of always reporting `False`
  on the implicit path. A genuinely new (first-ever) conversation still reports
  `is_follow_up=False`
  ([#1965](https://github.com/teng-lin/notebooklm-py/issues/1965)).
- **A missing optional-dependency error now points at the right fix.** Reading a
  source with `output_format="markdown"` without the `markdownify` extra
  installed used to report the wrong remediation ("Check the auth profile /
  storage configuration.") because the missing-extra error was classified as a
  configuration error. It now raises a dedicated `MissingDependencyError`
  (classified as a new `DEPENDENCY` category) whose hint names the install
  command — e.g. `pip install 'notebooklm-py[markdown]'` — across the MCP
  (`DEPENDENCY` code) and REST (`dependency` / HTTP 500) surfaces
  ([#1959](https://github.com/teng-lin/notebooklm-py/issues/1959)).
- **MCP `notebook_describe(include_metadata=true)` no longer reports a source
  count that disagrees with its own `sources` list.** The exposed
  `metadata.notebook.sources_count` is now re-projected to the enumerated
  (id-bearing, deduped) source count instead of the raw unfiltered
  `GET_NOTEBOOK` row count, which could inflate it (e.g. 168 vs 50) and mislead
  autonomous agents on quota / completeness. Source enumeration
  (`source_list` / `metadata.sources`) also dedups duplicate id-bearing rows
  (research re-imports, ghost/probe echoes), keeping the first occurrence
  ([#1919](https://github.com/teng-lin/notebooklm-py/issues/1919)).
- **MCP `research_import` is safe for unattended use.** The tool now imports via
  the timeout-tolerant `import_sources_with_verification` (as the CLI does), so a
  deep-research import that times out is reconciled against what the server
  actually committed instead of failing as if nothing imported (hiding a large
  partial import). It also gained optional `cited_only` (import only the sources
  the report cites) and `max_sources` (cap the count imported, applied after
  `cited_only`) so one call can't silently blow a notebook to its source cap.
  ([#1920](https://github.com/teng-lin/notebooklm-py/issues/1920))
- **`source_add` batch mode isolates a per-URL failure** instead of aborting the
  whole batch: a bad URL now yields a per-item `SourceAddError` while the other
  sources in the same call still add, matching the tool's documented behavior
  ([#1905](https://github.com/teng-lin/notebooklm-py/issues/1905)).
- **MCP `studio_generate` for interactive mind maps** now normalizes its payload
  to the bare node tree and returns a terminal poll shape, fixing malformed
  generate requests and non-terminal status reports
  ([#1908](https://github.com/teng-lin/notebooklm-py/issues/1908),
  [#1914](https://github.com/teng-lin/notebooklm-py/issues/1914)).
- **MCP `research_cancel` distinguishes an already-cancelled run** and surfaces
  the raw status code instead of a generic failure
  ([#1922](https://github.com/teng-lin/notebooklm-py/issues/1922)).
- **Content-sanity flags bot-challenge / WAF interstitial pages**, so a source
  that fetched a challenge page instead of real content is reported rather than
  silently ingested as thin content
  ([#1929](https://github.com/teng-lin/notebooklm-py/issues/1929)).
- **`suggest_prompts` strips a leading list marker** from returned prompts, and
  `research_start` drops redundant ids from its result
  ([#1909](https://github.com/teng-lin/notebooklm-py/issues/1909)).
- **The MCP server warns at startup when the upload widget is enabled but
  stateless HTTP is disabled** (`FASTMCP_STATELESS_HTTP` explicitly falsey),
  which would silently stop the widget from rendering
  ([#1915](https://github.com/teng-lin/notebooklm-py/issues/1915)).
- **RPC failures no longer leak the obfuscated method id or raw status code into
  the client-facing message.** A null-result RPC error used to read
  `RPC LBwxtb returned null result with status code 9 (Failed precondition).
  Found IDs: [...]` — surfacing the obfuscated method id (the #1 breakage class,
  which changes whenever Google re-obfuscates a method) and the raw numeric gRPC
  code to agents/users through MCP, REST, and the CLI. The message is now a
  stable, human-readable `The server rejected this request (failed precondition).`
  (or `The server returned an empty result …` when no status is attached); the
  method id, status code, and found-id list remain on the exception attributes
  (`method_id` / `rpc_code` / `found_ids`) for logging and debugging.
  ([#1921](https://github.com/teng-lin/notebooklm-py/issues/1921),
  [#1931](https://github.com/teng-lin/notebooklm-py/issues/1931))
- **A blank `FASTMCP_STATELESS_HTTP` no longer crash-loops the remote MCP server.**
  The deploy compose passed the variable through as `${FASTMCP_STATELESS_HTTP:-}`,
  which injects an **empty string** when unset — and FastMCP's settings bool-parse
  it at import and raise, so a docker deploy that didn't set the var crash-looped.
  The compose passthrough is removed (the upload widget auto-enables stateless in
  code; a non-widget deploy stays stateful), and an import-time guard treats an
  empty/whitespace-only value as unset so a blank value from any source can't crash
  the server. ([#1898](https://github.com/teng-lin/notebooklm-py/issues/1898))
- **Upstream upload errors surface as a clean, redacted 4xx — not an opaque 500.**
  An unsupported file type (or other upstream rejection) during a remote
  `/files/ul` upload used to leak a raw `httpx.HTTPStatusError` as an unclassified
  500 with no CORS header — which a browser upload widget could only report as
  "Failed to fetch". Upload HTTP errors are now classified at the client layer
  (429 → rate-limit, 5xx → server, 401/403/3xx → auth, other 4xx → validation)
  into a redacted, CORS-carrying response, so the user sees the real reason. Fixes
  the REST/CLI `add_file` path too.
  ([#1892](https://github.com/teng-lin/notebooklm-py/issues/1892))
- **Upload-only Drive file types auto-route to the file-upload path.** Adding a
  Drive file whose type NotebookLM only accepts as an upload (not an add-by-URL)
  used to fail at the RPC; the add-from-Drive path now detects those types and
  routes them through the upload flow, and when a Drive add can't be recovered the
  error now names the file-upload path as the remedy instead of a generic failure.
  ([#1887](https://github.com/teng-lin/notebooklm-py/issues/1887),
  [#1885](https://github.com/teng-lin/notebooklm-py/issues/1885))
- **Loopback MCP HTTP transport rejects non-loopback `Host` headers.** The
  loopback-bound HTTP transport now refuses requests whose `Host` header is not a
  loopback name, closing a DNS-rebinding vector against a locally bound MCP server.
  ([#1876](https://github.com/teng-lin/notebooklm-py/issues/1876))
- **Null-conversation chat asks are serialized with explicit follow-ups.**
  Consecutive asks against a fresh (null) conversation could race the
  conversation-id assignment; they are now serialized and threaded as explicit
  follow-ups so each ask lands on the intended conversation.
  ([#1880](https://github.com/teng-lin/notebooklm-py/issues/1880))
- **Multi-source `source_wait` no longer polls O(N²).** Waiting on N sources took
  one snapshot per source per tick; the wait now reads a single notebook snapshot
  per tick and derives every source's state from it, and the overall wait duration
  is bounded. ([#1882](https://github.com/teng-lin/notebooklm-py/issues/1882))
- **Raised the streamed chat-response size cap.** Long answers were truncated at
  the previous streamed-response ceiling; the cap is raised so full responses come
  through. ([#1852](https://github.com/teng-lin/notebooklm-py/issues/1852))
- **Aggregate retry deadline threaded through the RPC path; OAuth `fsync`
  offloaded.** The aggregate retry now honors a single deadline across attempts
  instead of resetting per call, and the OAuth token `fsync` is moved off the event
  loop so it can't stall concurrent requests.
  ([#1881](https://github.com/teng-lin/notebooklm-py/issues/1881))
- **`studio_download` rejects an unknown `output_format` self-documentingly.** An
  invalid `output_format` now returns an error that lists the accepted formats
  instead of an opaque rejection.
  ([#1833](https://github.com/teng-lin/notebooklm-py/issues/1833))
- **MCP source waits stay JSON-first.** `source_wait` output is emitted as
  structured JSON rather than prose, matching the rest of the MCP source surface.
  ([#1830](https://github.com/teng-lin/notebooklm-py/issues/1830))
- **Deep-research null-start no longer leaks an internal method id.** A failed
  deep-research start surfaced the obfuscated RPC method id in the error; it is now
  hidden. ([#1851](https://github.com/teng-lin/notebooklm-py/issues/1851))
- **REST server resource-limit hardening.** Several bounds were added to the
  experimental REST server so a single client can't exhaust it: per-route JSON
  request-body size caps ([#1846](https://github.com/teng-lin/notebooklm-py/issues/1846)),
  route-group backpressure to cap concurrent in-flight requests
  ([#1847](https://github.com/teng-lin/notebooklm-py/issues/1847)), a bounded fanout
  for multi-source waits ([#1844](https://github.com/teng-lin/notebooklm-py/issues/1844)),
  a shared suggest-surface map so the suggest routes don't each rebuild it
  ([#1843](https://github.com/teng-lin/notebooklm-py/issues/1843)), and
  `PendingRegistry.drop()` now removes the stale FIFO tuple so the pending queue
  can't leak entries ([#1879](https://github.com/teng-lin/notebooklm-py/issues/1879)).
  Diagnostics endpoints also stay live when auth is stale instead of failing with
  the guarded routes ([#1845](https://github.com/teng-lin/notebooklm-py/issues/1845)),
  and the account block is fetched with a single `GET_USER_SETTINGS` call instead of
  two ([#1862](https://github.com/teng-lin/notebooklm-py/issues/1862)).
- **Artifact-generation validation footguns** ([#1874]). Three input-validation
  hardenings on the artifact surface: (A) the REST `POST /artifacts`
  (`ArtifactGenerate`) body now rejects unknown fields (`extra="forbid"`) with a
  422 instead of silently ignoring a typo (`{"stylee": …}`) and starting a default
  artifact; (B) `generate_report` now coerces/validates `report_format` at the
  boundary, raising a clear `ValidationError` (that names `source_ids`) instead of
  an opaque `TypeError` deep in the payload builder when a caller misplaces a
  positional `source_ids` list; (C) `export()` enforces exactly-one-of
  (`artifact_id`, `content`) instead of firing a meaningless no-op RPC when both
  default to `None`. **Breaking:** `export()`'s `content` is now **keyword-only** so
  its positional slots match `export_report` / `export_data_table` (title in slot
  3) — this fixes a silent title→content misbind; pass `content=…` explicitly.
  ([#1874](https://github.com/teng-lin/notebooklm-py/issues/1874))

- **MCP source tools no longer swallow fatal errors or fan out unbounded.** The MCP
  `source_add` batch and `source_wait` had drifted from the REST route's policy: a
  batch `source_add` caught *every* per-URL exception — so an expired auth / rate
  limit / upstream 5xx was reported as a per-item "error" inside a **success**
  envelope, hiding it from the agent; and multi-source `source_wait` spawned one
  poller per source with no concurrency limit. The shared policy (the batch/wait
  caps, the fatal-vs-isolate classifier, and the bounded multi-source wait pool) now
  lives in the transport-neutral `_app` core (`_app.source_batch` / `_app.source_wait`)
  and is used by **both** adapters, with a parity guardrail (`tests/_guardrails/
  test_source_policy_parity.py`) that prevents future drift. Behavior change: a fatal
  batch item now aborts the whole `source_add` call (so an agent can re-auth/retry),
  only per-URL 4xx-input failures isolate; `source_wait` now bounds concurrency at 8,
  caps the source count at 100 on **both** the explicit-subset and the omitted-`sources`
  wait-all path (enforced at one shared chokepoint so the adapters can't drift), caps
  timeout at 3600s, and rejects non-finite (`NaN`/`Infinity`) timeout/interval — all via
  a shared `_app` validator used by the REST route too. Per-URL SSRF/validation isolation
  is unchanged.
  ([#1871](https://github.com/teng-lin/notebooklm-py/issues/1871))
- **Direct-PDF-URL sources no longer show the raw URL as their title.** Adding a
  source whose URL points straight at a `.pdf` left the full request URL in the
  title slot (the server extracts `<title>` for HTML pages but not for a direct
  PDF link), while HTML sources got proper titles. A PDF source whose title is
  **exactly the source URL** (the server degradation — not a user-set title that
  merely resembles a URL) now falls back to the URL path basename — e.g.
  `https://host/papers/SomePaper.pdf` → `SomePaper` (query and fragment ignored;
  percent-encoding decoded; a URL whose basename has no `.pdf` extension, such as
  `…/download?file=x.pdf`, keeps the raw URL rather than a misleading `download`).
  Applied consistently across `source list`/get (`Source.from_row`) **and**
  `source fulltext` (`SourceContentRenderer`). No network I/O and no PDF
  dependency; PDF `/Title` metadata extraction is out of scope. Because the fix
  lives in the shared read paths it applies **retroactively** — a source added
  before this release displays the derived title on the next read, so callers
  that relied on `source delete-by-title "<the raw URL>"` should use the new
  basename (URL-add idempotency is unaffected — it keys off the source `url`, not
  title). ([#1850](https://github.com/teng-lin/notebooklm-py/issues/1850))
- **Drive-hosted PDFs no longer list as `google_spreadsheet`.** The backend
  returns type code `14` for both native Google Sheets and Drive-hosted PDFs,
  and Drive sources carry no URL, so the read paths (`source_list` /
  `GET_NOTEBOOK` **and** `source fulltext` / `source_read` / `GET_SOURCE`)
  mislabeled Drive PDFs as `kind="google_spreadsheet"`. `kind` now disambiguates
  code `14` by the source MIME (`metadata[19]` / `metadata[9][2]` from a live
  capture): `application/pdf` → `pdf`, while real Google Sheets
  (`application/vnd.google-apps.spreadsheet`) are unchanged. This completes the
  read-path half of the fix started in #1831 (the add path).
  ([#1832](https://github.com/teng-lin/notebooklm-py/issues/1832))

- **Remote MCP connector: pin `fastmcp==3.4.2` so claude.ai can connect.**
  fastmcp 3.4.3 regressed the RFC 9728 protected-resource-metadata route
  (`/.well-known/oauth-protected-resource/mcp` started returning **404**), which
  dead-ends the claude.ai remote-connector OAuth discovery — the connector fails
  with *"Couldn't connect to the server."* Because the `mcp` extra previously
  allowed `fastmcp>=3.0,<4`, the published `0.8.0a2` Docker image floated to the
  broken 3.4.3 at build time even though the client code was unchanged. The
  `mcp` extra is now pinned to the last-good `fastmcp==3.4.2`; the pin will be
  lifted once a fixed fastmcp ships.

- **First-class human/mobile upload path in `upload_required`**
  ([#1801](https://github.com/teng-lin/notebooklm-py/issues/1801)). When a source
  add needs a browser upload, the `upload_required` response now surfaces the
  human/mobile hand-off path as a first-class option rather than burying it.

- **`server_info` and the profile probe report the resolved profile, not the
  raw active one** ([#1790](https://github.com/teng-lin/notebooklm-py/issues/1790)).
  The REST server's `server_info` and its startup profile probe now bind and
  report the profile that was actually resolved for the request.

- **The `.mcpb` desktop bundle now ships for pre-releases and launches the
  pinned pre-release.** The bundled launcher (`run_server.py`) always ran
  `uvx --from "notebooklm-py[mcp]"`, which resolves the latest *stable* server —
  so a pre-release bundle would have run the stable release, not the
  pre-release (which is why `publish-mcpb.yml` skipped pre-releases). The
  launcher now reads its version from the bundled `manifest.json` and, for a
  `vX.Y.ZaN` bundle, pins the exact version (`uvx --from
  "notebooklm-py[mcp]==X.Y.ZaN"` — the explicit `==` pin resolves the pre-release
  under uv's default resolver, without loosening transitive dependency
  resolution); stable bundles stay unpinned and track the latest stable server.
  `publish-mcpb.yml` now ships a bundle for pre-releases too.

- **`notebooklm doctor` no longer greenlights a session that is missing
  `__Secure-1PSIDTS`** (#1753). The auth check previously passed on `SID`
  presence alone, so a login that captured only half the Tier-1 cookie set — a
  common outcome on Windows, where Chrome 127+ App-Bound Encryption blocks
  `--browser-cookies` decryption and an automation-detected Playwright login can
  be served a session without the rotating token-binding cookie — reported "All
  checks passed" even though every real RPC then failed with `Missing required
  cookies: __Secure-1PSIDTS`. Doctor now emits a **warn** row (not a hard fail —
  the cookie legitimately rotates and can be re-minted at runtime, so a static
  offline probe must not false-negative a recoverable session) pointing at the
  Firefox `--browser-cookies` and `--master-token` workarounds. `auth check
  --test` already reported the real error and is unchanged. New
  [troubleshooting.md](docs/troubleshooting.md) section documents the Windows
  App-Bound Encryption limitation and its workarounds.

- **`notebooklm auth check` text mode now exits non-zero when an executed check
  fails, matching `--json` mode** (#1569). Previously the Rich-table renderer
  printed the failed checks but always exited `0`, so an unattended health check
  using `auth check --test` (without `--json`) silently treated expired auth as
  healthy — text and JSON modes had different process contracts. Both modes now
  exit `0` only when every *executed* check passes (skipped checks — e.g. the
  token fetch without `--test` — do not count as failures) and non-zero (`1`)
  otherwise. Behavioral fix to the CLI exit-code contract; no API change.

- **`Notebook.created_at` now reflects the true creation time instead of the
  last-modified time; `Notebook.modified_at` is newly exposed.** The notebook
  metadata block carries two timestamps — the creation instant at
  `data[5][8][0]` (pinned across edits) and the last-modified instant at
  `data[5][5][0]` (advances on every modification). `Notebook.from_api_response`
  read the *modified* slot (`data[5][5]`) and labeled it `created_at`, so the
  field — and everything built on it (`--json` output, `metadata` export, the
  `Created` table column) — silently reported the last edit time. `created_at`
  now reads the creation slot (`data[5][8]`), and the last-modified time is
  surfaced additively as `Notebook.modified_at` (new field, defaults to `None`,
  appended at the end so positional construction is unaffected; also added to
  `NotebookMetadata.to_dict()` and the notebook `--json` envelopes). This
  applies to both `notebooks.get()` and `notebooks.list()` (the homepage/recent
  feed), which share the decode path. No signature change — `created_at` keeps
  its type, only its source value is corrected — so this is a behavioral fix,
  not an API-compat break, and `modified_at` is purely additive.
- **Decoded `created_at` timestamps are now tz-aware UTC instead of naive
  host-local time** (#1519). The shared decoder `_datetime_from_timestamp`
  (backing `Notebook`/`Source`/`Artifact`/`MindMap.created_at`) called
  `datetime.fromtimestamp(value)` with no `tz`, producing a **naive** datetime in
  the host's local zone — so the same epoch surfaced as a different wall-time
  string per machine, and `created_at.isoformat()` / `--json` output, the
  `strftime` table cells, etc. mis-stated the absolute instant (a notebook
  created `13:40:05` UTC rendered as the offset-less `08:40:05` under
  `America/New_York`). It now returns `datetime.fromtimestamp(value, tz=utc)`:
  the value is tz-aware UTC and host-independent. `.timestamp()` round-trips
  unchanged, so internal sort/dedup/download ordering is provably unaffected —
  only the rendered string changes (now offset-aware, identical everywhere).
  This is the production sibling of the timezone slip pinned out of the #1511
  golden VCR test.
- **Artifact downloads re-validate every redirect hop against the trusted-host
  allowlist (SSRF-adjacent)** (#1521). Both download clients
  (`download_url` single + `download_urls_batch`) use
  `follow_redirects=True`, but the host-allowlist + HTTPS gate validated only
  the *initial* URL. A trusted Google URL whose `Location` pointed
  off-allowlist — a non-HTTPS hop, or a private/link-local host such as
  `169.254.169.254` / `localhost` — was followed and its body written to the
  caller's `output_path`, defeating the explicit allowlist. (Google session
  cookies were already not leaked to a non-Google redirect host — the stdlib
  cookie policy is domain-scoped — so this never exposed credentials; the
  residual harm was attacker-influenced bytes landing on the filesystem.) Both
  clients now attach an httpx `request` event hook that re-checks every hop's
  host + scheme **before the request is sent**, raising `ArtifactDownloadError`
  on the first off-allowlist or non-HTTPS hop so the untrusted host never
  receives a connection. Legitimate trusted→trusted redirects (Google
  signed-URL CDNs already on the allowlist) are unaffected. The host-allowlist
  check (`_is_trusted_download_host`) also no longer percent-decodes the host
  before matching: decoding created a parser differential where
  `evil%2egoogleapis.com` (`%2e`→`.`) was judged trusted while httpx connected
  to the raw, non-Google host — the guard now matches the exact host httpx
  connects to and rejects any host containing `%`.

- **`source delete` now honors exact-id-wins over prefix matching, in lockstep
  with `source get` / `rename` / `refresh`** (#1522). The delete-path resolver
  (`_app/source_mutations.resolve_source_for_delete`) built its prefix-match
  list and branched solely on `len(matches)` with no exact-id short-circuit, so
  `source delete abc` raised `AMBIGUOUS_ID` when a notebook held both `abc` and
  `abcdef` — even though the shared resolvers (`cli.resolve.resolve_partial_id_in_items`
  and its `_app` twin `_app.resolve.resolve_ref`) both return on an exact
  (case-insensitive) id match before evaluating prefixes, so the other verbs
  resolved the same input. The delete resolver now mirrors that Rule 3: a
  source whose id equals the input (case-insensitively) wins over any
  longer-id prefix match (and is not treated as a partial expansion, so no
  "Matched:" prose is emitted). Genuine prefix ambiguity (two strict prefixes,
  no exact match) and the not-found / title-instead-of-id paths are unchanged.
- **`Note.created_at` and note-backed `MindMap.created_at` are now populated**
  (#1529). Both fields were declared `datetime | None` but never filled in,
  even though the raw `GET_NOTES_AND_MIND_MAPS` / `CREATE_NOTE` responses carry
  the creation timestamp in the note metadata envelope at `row[1][2][2][0]` —
  the same slot the artifact path already decodes. `NoteRow` gains a
  `created_at_raw` / `created_at` property pair (mirroring `ArtifactRow`) that
  centralizes the descent behind named position constants, and every
  note-creation surface now reads it: `notes.list` / `notes.get`,
  `notes.create` (both the wrapped `[[id, …]]` and the flat
  `[id, …]` CREATE_NOTE response shapes), `chat.save_answer_as_note`, and the
  note-backed `mind_maps.list_note_backed` / `mind_maps.generate`.
  `Artifact.from_mind_map` was lifted to reuse the shared `NoteRow` extraction
  so the position knowledge lives in one place. Additive: absent / legacy
  rows still yield `None`; no signature change.
- **`profile` filesystem commands now surface a friendly error + exit 1
  instead of a raw traceback** (#1520). The `profile delete` (`shutil.rmtree`),
  `profile create` (directory materialization), `profile rename` (`os.rename`),
  and text-mode `profile list` paths performed their pure-filesystem operations
  unguarded, so an `OSError` — a half-deleted or locked profile directory, a
  read-only mount, or a browser-profile file held by AV/the browser on Windows
  — escaped `SectionedGroup.main` (which only catches `ClickException`/`Abort`)
  and printed a Python traceback. Each now mirrors `profile switch`'s existing
  `except OSError -> click.ClickException` idiom, yielding the documented
  friendly-message + exit-1 CLI contract. The `--json profile list` path keeps
  its existing `handle_errors` envelope (an unexpected `OSError` there stays the
  `UNEXPECTED_ERROR` / exit-2 contract automation relies on).
- **Runtime secret redaction now derives from one canonical registry, closing
  several credential-disclosure gaps** (#1517, #1518). The logging redaction
  cookie-name alternation in `_logging.py` was hand-enumerated and had drifted
  from the project's own cassette sanitizer: the session cookies `NID`,
  `LSOLH`, and `__Host-GAPS` (classified must-scrub by
  `tests/cassette_patterns.py`) were absent, so a bare `NID=g.a000-…` token —
  exactly what `_auth/refresh.py` logs at DEBUG from refresh-command
  stdout/stderr through the redacting logger — passed through `scrub_secrets`
  verbatim (#1517). Google API keys (`AIza…`) and any future `__Secure-*` /
  `__Host-*` cookie carrying an opaque (non-token-shaped) value were also not
  redacted at runtime. Separately, `UnknownRPCMethodError.data_at_failure` was
  spliced unscrubbed with `!r` into `__str__` / `__repr__` / tracebacks (a
  string splice that bypasses the logging `RedactingFilter`), unlike the
  sibling `raw_response` which was already scrubbed, so a credential-shaped
  indexed value leaked through every rendering regardless of `NOTEBOOKLM_DEBUG`
  (#1518). All are fixed by a single canonical runtime registry
  (`notebooklm._secrets`): `RUNTIME_SESSION_COOKIES` (the bare must-scrub cookie
  names the redaction alternation now derives from), `SECURE_HOST_UMBRELLA_PATTERNS`
  (`__Secure-*` / `__Host-*` prefix umbrellas whose name class spans the full
  RFC 6265 `token` charset — any future secure/host cookie name fails closed by
  construction), and `AUTH_TOKEN_SHAPE_PATTERNS` (carrier-agnostic
  `g.a000-` / `sidts-` / `ya29.` token catch-alls plus the `AIza…` Google
  API-key shape, ported from the cassette registry as defense in depth so a
  secret under an unknown carrier name still fails closed). `data_at_failure`
  (and the already-scrubbed `AuthExtractionError.payload_preview`) are routed
  through `scrub_secrets`, so all three additions cover the exception surfaces
  too. Every name-anchored value pattern (cookies, the `__Secure-*`/`__Host-*`
  umbrellas, and the `at=`/`csrf=`/`f.sid=`/`upload_id=`/OAuth/`Bearer` query +
  header forms) now also redacts an RFC 6265 / JSON **double-quoted** value
  (`SID="opaque"`, `f.sid="opaque"`): the value class excluded `"`, so a quoted
  value made the whole pattern miss and leaked verbatim — the optional
  surrounding quotes redact the inner value while preserving the quotes. A
  parity guardrail
  (`tests/_guardrails/test_runtime_secret_registry_parity.py`) asserts the
  runtime registry stays in lockstep with the cassette sanitizer on every axis:
  bare-cookie superset, secure/host umbrella coverage by construction, and
  regex-string equality of the credential-shape set — so a new must-scrub shape
  added to the cassette registry forces the runtime registry to keep up.
- **Playwright login: closing the browser during the final storage-state
  capture now shows the browser-closed help instead of a bug-report prompt**
  (#1514, deferred from the #1512 review). Every in-flow Playwright call in
  the login flow (page recovery, the navigation retry loop, the login wait,
  cookie-forcing) already mapped `TargetClosedError` to the friendly
  `BROWSER_CLOSED_HELP` text + exit 1, but a closure in the narrow window
  during the final `context.storage_state()` capture fell through the outer
  handler's bare `raise` and exited 2 ("Unexpected error … please report a
  bug"). The outer handler in `run_browser_capture` now recognizes
  TargetClosed and surfaces the same help + exit 1; every other unexpected
  failure keeps the exit-2 bug-report contract.
- **Playwright storage-state filter hardened against malformed cookie rows
  and exact-duplicate identities** (#1513, deferred from the #1512 review).
  `filter_storage_state_cookies_by_domain_policy` no longer crashes the whole
  persist when rookiepy / Playwright emits a malformed row: non-dict entries,
  cookies whose `domain` is not a str, and cookies whose `name` is not a
  non-empty str are skipped with one bounded `logger.warning` per row
  (`reprlib` preview) instead of raising in `.get` / `.lstrip`. It also
  dedups rows sharing an exact RFC 6265 identity `(name, domain, path)`
  (path normalized via `or "/"`, matching every loader): the last occurrence
  in capture order wins whole (fields are never merged), mirroring the
  persistence-merge rule in `save_cookies_to_storage` where the newer
  observation overwrites the stored row for the same key. Same-name rows on
  *different* domains or paths are all kept — cross-domain same-name
  resolution remains a load-time concern (the flat loaders rank by
  `_auth_domain_priority`); deduping by bare name at write time would starve
  the `(name, domain, path)`-keyed runtime loader
  (`build_httpx_cookies_from_storage`), which legitimately holds e.g. the
  per-product `OSID` cookie on `notebooklm.google.com` and
  `myaccount.google.com` as distinct jar entries.

- **Split-state PSIDTS recovery no longer writes a duplicate
  `__Secure-3PSIDTS` row to `storage_state.json`** (#1523). On the
  `--browser-cookies` path, when `__Secure-1PSIDTS` is missing/expired (so
  recovery fires) but a fresh `__Secure-3PSIDTS` is already in the source jar,
  Google's `RotateCookies` POST returns both rotated SIDTS cookies and the
  in-memory recovery append loop emitted a second `__Secure-3PSIDTS` (and a
  stale `__Secure-1PSIDTS` twin) entry with no analog in any real browser jar.
  Auth still worked (the row is deduped on load), but the on-disk artifact
  diverged from the true cookie set. `recover_psidts_in_memory` now keys the
  source jar by RFC 6265 identity `(name, domain, path)` (path normalized via
  `or "/"`, matching every loader) and overwrites the existing row in place
  with the rotated occurrence instead of appending — exactly one row per key,
  carrying the fresh value, mirroring the last-occurrence-wins dedup added to
  `filter_storage_state_cookies_by_domain_policy` in #1513.

- **`sources.add_text` no longer swallows typed transport errors into
  `SourceAddError`.** Its bare `except RPCError` wrapped *everything* —
  including the `RPCError` subclasses `RateLimitError`, `AuthError`, and
  `ServerError` — so callers could not catch a rate-limited `add_text` to
  back off via `retry_after` (or re-login on `AuthError`). It now re-raises
  the narrow transport types unwrapped before wrapping only the residual
  broad `RPCError`, matching the ADR-0019 catch ordering its siblings
  `add_url`/`add_drive` already follow. The rule is now *enforced*, not just
  documented: a new AST guardrail
  (`tests/_guardrails/test_error_contract_catch_ordering.py`) fails any
  `except RPCError` clause that wrap-and-raises a different exception class
  without a preceding narrow-transport re-raise clause in the same `try`
  (scope: `src/notebooklm/**` minus the `rpc/` protocol layer, where the
  transport subtree originates).

- **`notebooklm note create --json` no longer reports failure on every
  successful create.** It previously emitted `{"id": null, "created": false,
  "error": "Creation may have failed"}` for every note it successfully
  created: a leftover raw-shape decoder in the `_app` layer went dead when
  `notes.create` was typed to return a `Note` (it expected the retired
  raw-list RPC shape and yielded `None` for a typed `Note`). The bug was
  masked in the unit suite by stale raw-list mocks of `notes.create`. The CLI
  now emits the real note id with `"created": true`; facade failures
  propagate as exceptions through the standard CLI error handler instead of
  a soft-failure envelope.

- **14 positional-decode sites no longer fabricate wrong-but-valid values
  silently on wire drift.** Guarded single-level reads of decoded
  `batchexecute` payloads could swallow a Google reshape into a plausible
  default — an empty notebook / mind-map id, an empty share email, a deleted
  mind map leaking as live, a silently-empty chat history, a `LIST_NOTEBOOKS`
  wrapper mis-dispatch feeding garbage rows, an unvalidated source type code,
  and a note lookup flipping found → not-found. Per the #1485
  absence-vs-malformed policy, genuine absence (short rows, `None` slots,
  legitimately-empty containers) keeps its soft degrade, while
  present-but-malformed data is now loud: the chat conversation-history walk
  moved behind a new `ConversationTurnRow` adapter and raises
  `UnknownRPCMethodError` on a truthy non-list payload or turns container
  (malformed individual turn rows and unrecognized role codes are skipped
  with a DEBUG diagnostic); `notebooks.list()` raises `DecodingError` on an
  unrecognized payload shape; mind-map rows are decoded through `NoteRow`
  and WARN when a null content slot lacks the soft-delete sentinel;
  notebook-id, share-email, and source-type-code slots WARN with a bounded
  payload preview when present-but-wrong-type (keeping list parsing alive);
  and `notes.get_or_none` id matching reads through `NoteRow.id`. One
  behavior nuance: `SharedUser.email` is now always a `str` — a `None` email
  slot normalizes to `""` instead of leaking `None` through the `str`-typed
  field.
- **Chat citation-structure drift is no longer swallowed at DEBUG** (#1505
  continuity — the last named survivor of that drift-swallow class). A Google
  reshape of the streamed-chat citation structure previously degraded to
  "answers with no citations" via a blanket `except → logger.debug → []` in
  `parse_citations` — invisible, and it also discarded already-parsed
  citations. Per the absence-vs-malformed policy: genuine absence (no type
  block, short type block, `None`/empty citation slot — the routine "answer
  without citations" shapes on real traffic) stays completely silent; a
  truthy non-list where the citation *container* belongs (`first[4][3]`) is
  structural wire drift and raises `UnknownRPCMethodError` (matching the
  parser's existing `inner_data[0]` raise and `unwrap_conversation_turns`);
  a present-but-unusable individual citation row now logs at least one
  bounded WARNING and is skipped, so a good answer keeps its surviving
  citations. Surviving citations keep their **raw wire ordinal** as
  `citation_number` (a skipped row leaves a hole; with nothing skipped this
  equals the dense numbering always produced), so the answer's literal `[N]`
  markers never shift onto a different citation. Correspondingly,
  save-as-note's positional marker fallback (`references[N-1]`) now applies
  only when that positional reference carries no `citation_number`: a holed
  marker drops its anchor with a warning instead of anchoring the wrong
  chunk.

### Security

- **The loopback DNS-rebinding `Host` guard is no longer bypassed on a tokenless
  local bind.** The guard is skipped only when an external bind is opted into
  **and** authentication (bearer or OAuth) is configured — the
  `ALLOW_EXTERNAL_BIND` flag alone (e.g. set while `--host` stays at loopback) no
  longer disables it, closing a rebinding gap on an unauthenticated local server
  ([#1935](https://github.com/teng-lin/notebooklm-py/issues/1935)).

### Breaking

- **`sources` / `artifacts` / `notes` / `mind_maps` `.get()` raise on a miss**
  (#1247). A genuine miss now raises the matching `*NotFoundError`
  (`SourceNotFoundError` / `ArtifactNotFoundError` / `NoteNotFoundError` /
  `MindMapNotFoundError`) instead of returning `None` (and the v0.7.0
  `DeprecationWarning` is gone), matching `notebooks.get`. Return annotations
  narrow from `X | None` to `X`. Use the unchanged, warning-free `get_or_none()`
  for the sanctioned `None`-on-miss lookup, or wrap in `try/except *NotFoundError`.
- **Typed research / mind-map / guide returns are attribute-only** (#1251). The
  `MappingCompatMixin` dict-subscript bridge is removed from `ResearchTask` /
  `ResearchStart` / `MindMapResult` / `SourceGuide` / `ResearchSource`:
  `result["key"]` raises `TypeError`; `result.get(...)` / `.keys()` / `.items()` /
  `.values()` raise `AttributeError`; `"k" in result` / `iter(result)` /
  `len(result)` raise `TypeError`. Only attribute access (`result.status`,
  `guide.keywords`, …) and `to_public_dict()` survive. `ResearchStatus` stays a
  `str`-enum, so `status == "completed"` keeps working.
- **`research.wait_for_completion(interval=...)` removed** (#1254). The deprecated
  `interval=` keyword alias is gone (its v0.7.0 `DeprecationWarning` cycle is
  complete); passing it now raises the standard `TypeError` for an unexpected
  keyword. Use `initial_interval=` (same poll cadence).
- **`generate mind-map` defaults to interactive** (#1272). The CLI
  `notebooklm generate mind-map <nb>` (and `artifact`/`download` mind-map paths)
  now default `--kind` to `interactive` instead of the note-backed JSON map. Pass
  `--kind note-backed` to keep the note-backed behavior.
- **`sources.refresh()` / `chat.delete_conversation()` return `None`** (#1290).
  Both previously returned `True` on success (uninformative — any failure raised
  first); they now return `None` and their annotations change from `-> bool` to
  `-> None`. `chat.clear_cache(...)` is deliberately unchanged and stays `-> bool`
  (its bool is meaningful).
- **Synchronous generation-kickoff refusals raise** (#1342). `artifacts.generate_*`
  and `revise_slide` no longer swallow a `USER_DISPLAYABLE_ERROR` refusal into a
  `GenerationStatus(status="failed")` — they re-raise the underlying
  `RateLimitError` / `RPCError`. `_parse_generation_result` raises
  `ArtifactFeatureUnavailableError` / `DecodingError` on a missing artifact id.
  `research.start` raises `DecodingError` on an empty / non-list payload or a
  falsey `task_id` (return type narrows from `ResearchStart | None` to
  `ResearchStart`). The public `artifacts.with_rate_limit_retry` helper retries
  only on a *raised* `RateLimitError` and re-raises on budget exhaustion (a
  returned rate-limited status is no longer a retry signal).
- **Derived-read / lister drift raises `DecodingError`** (#1344). A
  structurally-unrecognized RPC payload that previously collapsed to an empty
  value now raises `DecodingError`, so callers can distinguish a genuine miss from
  server-side shape drift: `sources.check_freshness()`, the note lister, and the
  artifact raw lister reject malformed-but-truthy payloads. Legitimate
  empty / stale shapes are unchanged.
- **Mutate-existing ops fail loud on a missing target** (#1362). `notes.update`
  preflights existence and raises `NoteNotFoundError` before firing the update
  RPC; `sources.rename(..., return_object=False)` and
  `artifacts.rename(..., return_object=False)` run the existence preflight on the
  `False` path and raise `SourceNotFoundError` / `ArtifactNotFoundError` on a miss.
  `return_object=False` still returns `None` on success.
- **`NotebooksAPI.share()` removed + research poll/wait raise on ambiguity**
  (#1363). The deprecated `client.notebooks.share()` is gone — use
  `client.sharing.set_public(...)` + `client.notebooks.get_share_url(...)`.
  `research.poll(task_id=None)` / `wait_for_completion(task_id=None)` now raise the
  new `AmbiguousResearchTaskError` when two or more tasks are in flight (instead of
  warning and guessing); with a single in-flight task they resolve it silently.
- **Removed `NOTEBOOKLM_FUTURE_ERRORS` and the deprecation machinery** (#1365).
  The forward-compat preview gate and the `warn_get_returns_none` /
  `deprecated_kwarg` / `MappingCompatMixin` deprecation helpers are deleted now
  that every break they previewed is the default. `warn_deprecated` and
  `NOTEBOOKLM_QUIET_DEPRECATIONS` remain for future one-off deprecations.

## [0.7.3] - 2026-06-29

Maintenance patch on the 0.7.x line. Backports fixes from `main`
(cherry-picked ahead of the v0.8.0 breaking release).

### Fixed

- **Markdown (`.md`) uploads no longer fail on Python 3.10** (backport of
  #1628; fixes #1627). `mimetypes.guess_type` returns `None` for `.md` on
  Python 3.10 and on hosts without a populated `/etc/mime.types`, so the upload
  fell back to `application/octet-stream`; NotebookLM could not infer a parser
  and processing failed server-side (status=ERROR), surfacing as
  "Error uploading source" / `SourceProcessingError`. `.md`/`.markdown` are now
  pinned to `text/markdown` before the opaque fallback.
- **Clearer error when NotebookLM redirects to its region / anti-abuse access
  gate** (backport of #1630). A redirect to `notebooklm.google` is now
  classified and surfaced with an actionable message instead of an opaque
  failure.
- **Interactive `notebooklm login` no longer hangs 5 minutes and silently
  fails to save auth** (backport of #1700; fixes #1697). The login-success
  detector used Playwright's default `wait_until="load"`, but
  `notebooklm.google.com` is a streaming SPA that never fires the `load` event
  (`readyState` stays `interactive`), so the wait blocked until timeout even
  though sign-in had succeeded — printing "Login not detected within 5 minutes"
  and never writing `storage_state.json`. The initial navigation and the
  success detector now pass `wait_until="commit"` (the same level the existing
  cookie-forcing navigations already use), so login is detected as soon as the
  authenticated host is reached.

## [0.7.2] - 2026-06-18

Maintenance patch on the 0.7.x line. Backports fixes from `main`
(cherry-picked ahead of the v0.8.0 breaking release).

### Fixed

- **Video Overview visual-style values corrected to match the live Web UI**
  (#1594; backport of #1597). `VideoStyle` used stale numeric wire values, so
  several named styles (Whiteboard, Kawaii, Anime, Watercolor, Heritage,
  Paper-craft, Classic, Custom) serialized as the *wrong* style on the wire.
  Values now match live Web UI captures, and `CUSTOM` is encoded the Web-UI way
  (the style enum slot is omitted/defaulted and the custom visual-style prompt
  is appended). **Note:** this changes the integer values of `VideoStyle`
  members — code that passed the raw ints must update; passing the enum members
  (`VideoStyle.WHITEBOARD`, …) is unaffected.

- **Video (and other artifact) generation no longer fails immediately on
  cohorts that reject the legacy client-options shape** (#1594; backport of
  #1583). `CREATE_ARTIFACT` sent a minimal param-0 of `[2]`; the live web
  client sends the fuller capability envelope
  (`[2, null, null, [1, …, [1]], [[1, 4, 8, 2, 3, 6]]]`). On some accounts the
  backend began rejecting the short form for video generation specifically —
  the artifact was created and then immediately marked failed, while audio and
  infographic (which the backend still accepted) kept working. All artifact
  builders (audio, video, cinematic video, report, quiz, flashcards) now send
  the full envelope, matching the browser. The VCR body matcher normalizes the
  old and new client-options shapes so existing generation cassettes still
  replay.

- **`notebooks.create()` and `sources.add_url()` no longer fail on
  already-migrated cohorts** (#1548; backport of #1546). Google migrated the
  `CREATE_NOTEBOOK` (CCqFvf) and URL `ADD_SOURCE` (izAoDd) payloads to a nested
  wire shape during the gradual rollout: the flat trailing template block
  (`[2],[1]` for create; `[2],None,None` for url-add) collapsed into a nested
  `[2, None, None, [1, …, [1]]]` block, and the URL source spec gained a
  trailing `1`. Backends on already-migrated accounts began rejecting the old
  flat shape (`status=3` for create, `5`/`9` for add-source), while read paths
  were unaffected. Both payload builders now emit the nested shape, which is
  forward-compatible across migrated and un-migrated cohorts (verified live).
  Scope is limited to the create + add-url RPCs with exact Web-UI captures; the
  youtube/text/drive/file `ADD_SOURCE` variants are unchanged.

- **Empty notebook summary no longer raises `UnknownRPCMethodError`** (#1485).
  A brand-new, source-less notebook has no summary yet, so the `SUMMARIZE` RPC
  returns an absent/`None` payload. `notebooks.get_summary()` and
  `notebooks.get_description()` now treat that routine "no summary yet" state as
  an empty summary (`""`) instead of mis-classifying it as wire-schema drift.
  Genuinely-malformed payloads (a present-but-non-list `result[0]`, a scalar, or
  a string where a nested list is expected) still raise. `get_summary` now shares
  the `_extract_summary` descent with `get_description`, so both agree on every
  shape. As part of the fix, `safe_index` rejects a `str`/`bytes` value at an
  intermediate descent hop (it is indexable but never a valid container, so
  descending it would smuggle a single character past drift detection).
- **`download <type>` no longer exits 1 with no file written** (#1488). The
  download path listed artifacts twice — the executor listed to select the
  target, then each per-type download re-listed to re-find it by id — so the
  second `LIST_ARTIFACTS` could not replay against a single-interaction VCR
  cassette and aborted the download. The executor now lists once and threads
  the already-fetched rows into the download method (which skips its redundant
  second list); studio downloads also no longer trigger the note-backed
  mind-map sub-fetch they never needed. Live behavior is unchanged for direct
  `client.artifacts.download_*()` calls.

## [0.7.1] - 2026-06-06

Maintenance patch on the 0.7.x line. Backports two fixes from `main`
(cherry-picked ahead of the v0.8.0 breaking release).

### Fixed

- **Chat reads no longer time out on slow shared-notebook streams** (#1466,
  #1468). `client.chat.ask` now uses a chat-specific per-read HTTP timeout
  instead of inheriting the general client `timeout`, so shared notebooks that
  are slow to emit the first streamed byte stop raising spurious timeouts. A new
  optional `chat_timeout` keyword on `NotebookLMClient(...)` /
  `NotebookLMClient.from_storage(...)` (and a matching `chat ask` CLI flag)
  tunes the window; it defaults to 180 s. **Behavior note:** chat reads now wait
  up to 180 s by default rather than inheriting the prior client timeout — pass
  `chat_timeout=None` to restore the old inherit-`timeout` behavior.
- **Dropped a misleading byte-count-mismatch WARNING from the chunked RPC
  parser** (#1469). The decoder no longer logs a spurious size-mismatch warning
  during normal chunked parsing.

### Added

- `chat_timeout` keyword argument on the client constructors and a corresponding
  `chat ask` CLI option (additive; defaults preserve existing call sites).

## [0.7.0] - 2026-06-04

### Highlights

- **v0.8.0 error-contract runway.** This release lands the *additive half* of a
  cross-SDK convergence on "absence and refusal **raise**; only success and
  async-lifecycle state are returned." You can adopt the forward-compatible form
  today and run on both 0.7.0 and 0.8.0 with no flag day:
  - **Test your code against 0.8.0 today** — set `NOTEBOOKLM_FUTURE_ERRORS=1` to
    opt your process into the v0.8.0 error contract (`get()` raises
    `*NotFoundError` on a miss, all dict-style access on the typed returns
    (`[...]`, `.get()`, `in`, `.keys()`, …) raises, and
    the deprecated `wait_for_completion(interval=...)` alias raises) **without
    changing default behavior**. Run your test suite with it on to find breakage
    before you upgrade. This is the "test-before-you-migrate" mechanism paired
    with the [Upgrading to v0.8.0](docs/upgrading-to-0.8.0.md) guide.
  - **`get_or_none()`** — a new, **silent** optional lookup on
    `sources` / `artifacts` / `notes` / `mind_maps` that returns the object or
    `None` and never warns. It is the sanctioned replacement for the now-soft
    `get()`-returns-`None` pattern.
  - **`get()` now warns on a miss** (still returns `None` this release) and will
    **raise** the typed `*NotFoundError` for its domain in v0.8.0 (#1247).
  - **Typed `*NotFoundError` per domain** — `NoteNotFoundError` /
    `MindMapNotFoundError` join the existing source / artifact / notebook errors,
    all catchable via the `NotFoundError` umbrella.
- **Breaking: `rename()` returns the renamed object; `delete()` returns `None`.**
  `rename()` now re-fetches and returns the live object (raising `*NotFoundError`
  on a missing target), and `delete()` returns `None` and is idempotent on an
  already-absent target. See **Breaking changes** below before upgrading.
- **Typed dataclass returns** for `research.poll` / `start` /
  `wait_for_completion`, `artifacts.generate_mind_map`, and `sources.get_guide`
  (`ResearchStatus`, `ResearchTask`, `ResearchSource`, `ResearchStart`,
  `MindMapResult`, `SourceGuide`) — attribute access instead of untyped dicts,
  with a backward-compatible read-only mapping bridge.
- **Unified `client.mind_maps` surface** over both backends (note-backed +
  interactive), plus **`client.artifacts.retry_failed()`** to retry a failed
  Studio artifact in place (and a matching `notebooklm artifact retry` command).

### Breaking changes

> **⚠ BREAKING — `rename()` returns the renamed object; `delete()` returns `None`.**
>
> These return-type changes ship **now, as a clean break with no deprecation
> runway**, because the old returns were never usable contracts a caller could
> depend on in good faith. (Contrast `get()`'s `None`-on-miss, which **is** a
> real, documented contract and keeps its full deprecation runway to v0.8.0 —
> see issue #1247. The coherent story: **reads/renames are missing-strict;
> deletes are absence-idempotent.**)
>
> **`rename()` → returns the renamed object, raises `*NotFoundError` on a
> missing target** (issues #1255, #1256):
>
> - `artifacts.rename` previously returned `None` **even on success** (an
>   unusable return); it now re-fetches and returns the renamed `Artifact`,
>   raising `ArtifactNotFoundError` when the target is absent.
> - `sources.rename` previously **fabricated** an unverified
>   `Source(id, title)` when the RPC echoed nothing (a silent-false-success
>   bug); it now prefers the `UPDATE_SOURCE` echo, falls back to an internal
>   fetch, returns the real `Source`, and raises `SourceNotFoundError` when the
>   target is absent. **The fabrication is gone.**
> - `notebooks.rename` already returned the re-fetched `Notebook` (the
>   reference behavior) — unchanged.
> - `mind_maps.rename` (both note-backed and interactive backings) now returns
>   the renamed `MindMap` and raises on a missing target.
> - **Error taxonomy:** only *genuine absence* (empty-payload / absent-from-list,
>   detected via a content/list lookup — **not** a transport 404) maps to a
>   `*NotFoundError`. Transport / `429` / `5xx` / auth errors propagate **as
>   themselves** and are never laundered into a synthetic `*NotFoundError`.
> - **Bulk opt-out:** every `rename()` accepts `return_object: bool = True`.
>   Pass `return_object=False` to skip the hydrate re-fetch and return `None`
>   (artifacts' re-fetch is a full `LIST_ARTIFACTS`, so bulk renamers that
>   ignore the return should opt out to avoid N extra list calls).
>
> **`delete()` → returns `None`, idempotent on a missing target** (issue #1211):
>
> - `notebooks` / `sources` / `artifacts` / `notes.delete` and
>   `notes.delete_mind_map` (and `mind_maps.delete`) previously returned a
>   hardcoded `True`; they now return `None`. The old `True` was a tautology
>   (never `False`), but `True → None` is a *real, observable* flip from truthy
>   to falsy:
>   ```python
>   # BEFORE (entered the block; delete always returned True)
>   if await client.sources.delete(nb_id, src_id):
>       ...  # this branch no longer runs — delete() now returns None (falsy)
>   ```
>   Drop the `if`; call `delete()` for its effect. Use `get()` first if you
>   need to assert existence.
> - **Idempotent:** deleting an already-absent target **succeeds** (returns
>   `None`); it does **not** raise `*NotFoundError`. This matches HTTP `DELETE`
>   idempotency and keeps retry/teardown loops clean. (The one exception is
>   `mind_maps.delete` *without* an explicit `kind`, which must list to pick
>   the right RPC family and so raises `ValueError` for an unknown id; pass
>   `kind=` to delete idempotently.)
> - **Real failures still raise:** `allow_null=True` tolerates only a null
>   *result*, not an RPC/HTTP error — a `403` / `5xx` / auth / transport
>   failure on delete still propagates. "Idempotent on missing" is not "swallow
>   all errors."

> **⚠ BREAKING — lapsed v0.6.0-targeted deprecations removed.**
>
> These deprecation shims advertised removal in v0.6.0, which has shipped, so
> they have now been removed. This is a pre-1.0 breaking change. See
> [`docs/deprecations.md`](docs/deprecations.md) "Removed in v0.7.0".
>
> - **Positional `wait` / `wait_timeout` on `SourcesAPI.add_url` / `add_text` /
>   `add_file` / `add_drive`** — these parameters are now **keyword-only**.
>   Passing them positionally raises `TypeError`.
>   ```python
>   # BEFORE (deprecated, emitted DeprecationWarning)
>   await client.sources.add_url(nb_id, url, True, 45.0)
>   # AFTER
>   await client.sources.add_url(nb_id, url, wait=True, wait_timeout=45.0)
>   ```
> - **`ArtifactsAPI.wait_for_completion(poll_interval=...)`** — the deprecated
>   `poll_interval` alias was removed; use `initial_interval=...` (same
>   cadence). Passing `poll_interval` raises `TypeError`.
>   ```python
>   # BEFORE
>   await client.artifacts.wait_for_completion(nb_id, task_id, poll_interval=5.0)
>   # AFTER
>   await client.artifacts.wait_for_completion(nb_id, task_id, initial_interval=5.0)
>   ```
> - **`NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out** — removed. Strict
>   decoding is now the only mode: schema-drift helpers (notably `safe_index`)
>   always raise `UnknownRPCMethodError` on shape drift instead of
>   warn-and-returning `None` / `[]`. The env var is now ignored (no-op).
>   Callers that previously relied on the soft fallback should handle
>   `UnknownRPCMethodError` (a subclass of `RPCError` / `DecodingError`).
> - **`NotesAPI.create_from_chat(...)`** — removed (deprecated since v0.5.0,
>   two MINOR cycles of warnings served; the documented removal target was
>   v0.7.0). It was a pure forwarder. Use `ChatAPI.save_answer_as_note(...)`,
>   the canonical citation-rich saved-from-chat method and data owner
>   (ADR-0013): `await client.chat.save_answer_as_note(nb_id, ask_result)`.
>   The now-unused `save_chat_answer` injection plumbing on `NotesAPI` was
>   removed with it.
>
> **Not removed:** `SourcesAPI.add_file(mime_type=...)` and
> `notebooklm source add --mime-type` (file sources) were **reassessed and
> kept** — `mime_type` was re-wired to set the resumable-upload content-type
> header (overriding filename-extension inference), so it is a supported
> parameter, not a dead shim. Its stale `DeprecationWarning` had already been
> removed; the documentation now reflects this.
>
> **Not removed:** awaiting `NotebookLMClient.from_storage(...)` still works —
> its deprecation targets v1.0, not v0.6.0.

### Added

- `get_or_none()` — the sanctioned **silent** optional lookup, added to
  `client.sources` / `client.artifacts` / `client.notes` / `client.mind_maps`.
  It returns the entity (`Source` / `Artifact` / `Note` / `MindMap`) or `None`
  for a genuine absence and **never warns**, making it the drop-in migration
  target for the now-deprecated `get()`-returns-`None` pattern (see
  **Deprecated** below; issue #1247). Unlike `get()`, it does **not** swallow
  transport, auth, or decode faults — only a real "not found" yields `None`.
  ```python
  # Silent optional lookup (no DeprecationWarning):
  src = await client.sources.get_or_none(nb_id, source_id)
  if src is None:
      ...
  ```
  Additive (ADR-0019; issue #1247).
- `NOTEBOOKLM_FUTURE_ERRORS` opt-in preview flag — run the **v0.8.0 error
  contract** early to test forward-compatibility before the breaking flips ship
  (ADR-0019 / umbrella #1346). Default-off and **byte-identical** to current
  v0.7.0 behavior; when truthy (`1`/`true`/`yes`/`on`) the three warn-runways
  adopt their v0.8.0 raise-target: `sources.get` / `artifacts.get` /
  `notes.get` / `mind_maps.get` raise the matching `*NotFoundError` on a miss
  (#1247), the **whole** `MappingCompatMixin` mapping surface — `[...]`
  subscript plus the silent `get` / `keys` / `items` / `values` / `len` / `in` /
  `iter` shims — raises the exact error a bare dataclass would (#1251), and
  the deprecated `ResearchAPI.wait_for_completion(interval=...)` alias raises
  `TypeError` (#1254). Takes precedence over `NOTEBOOKLM_QUIET_DEPRECATIONS`
  (a runway raises regardless of quiet). The four `get()` methods are now routed
  through a single `_lookup.resolve_get` bridge, eliminating the hand-duplicated
  warn-on-miss pattern. Helper: `notebooklm._deprecation.future_errors_enabled`.
  The flag now **also** previews the purely-behavioral v0.8.0 changes that have
  no warn-runway (#1405): the uninformative `bool` returns of `sources.refresh`
  and `chat.delete_conversation` become `None` (#1290); a synchronous generation
  refusal **raises** the decoder's `RateLimitError` / `RPCError` /
  `DecodingError` / `ArtifactFeatureUnavailableError` instead of being swallowed
  into `GenerationStatus(status="failed")` / returned `None` — across
  `_call_generate`, `revise_slide`, `_parse_generation_result`, and
  `research.start` (#1342); and the mutate-existing ops `notes.update` and
  `sources`/`artifacts` `rename(return_object=False)` fail loud with a
  `*NotFoundError` on a missing target (#1362). These previews are runtime-only —
  **no public return annotation changes** until the v0.8.0 flip — so default-off
  stays byte-identical. Does **not** close #1247/#1251/#1254/#1290/#1342/#1362 —
  the runways and current behavior remain until the v0.8.0 flip. See
  `docs/deprecations.md`. Additive (issues #1346, #1405).
- `client.artifacts.retry_failed(notebook_id, artifact_id)` — retry a failed
  Studio artifact in place (the web UI "Retry" action), via the new
  `RETRY_ARTIFACT` (`Rytqqe`) RPC. The artifact is not deleted first and the
  same `artifact_id` is preserved, so existing `poll_status()` /
  `wait_for_completion()` flows keep working. Follows the ADR-0019 "async
  kickoff" contract: an accepted retry returns
  `GenerationStatus(status="in_progress")`, while a synchronous refusal
  (`USER_DISPLAYABLE_ERROR` — rate limit / quota / not-retryable) **raises** the
  underlying `RateLimitError` / `RPCError` rather than returning a
  `status="failed"` handle. New `notebooklm artifact retry <artifact_id>
  [--wait] [--json]` CLI command. Additive (issues #1319, #1346).
- `notebooklm.artifacts.with_rate_limit_retry` now also retries when the
  wrapped callable **raises** `RateLimitError` (backing off and re-raising once
  the retry budget is exhausted), so it can wrap the new `retry_failed`. The
  existing returned-rate-limited-`GenerationStatus` path (used by `generate_*`)
  is unchanged — this is a backward-compatible addition (issue #1319).
- New public exception types for the note and mind-map domains, mirroring the
  existing `SourceError` / `SourceNotFoundError` shape: `NoteError` +
  `NoteNotFoundError` and `MindMapError` + `MindMapNotFoundError`. Each
  `*NotFoundError` is a triple-base `(NotFoundError, RPCError, <Domain>Error)`,
  so it is catchable via the cross-domain `NotFoundError` umbrella, at
  transport-level `except RPCError` call sites, and at domain-level
  `except NoteError` / `except MindMapError` call sites. These are the
  prerequisite for the mind-map not-found work (ADR-0019; issues #1291, #1346).
  `MindMapNotFoundError` is now raised by the `mind_maps` mutation paths (see
  *Changed* below); `NoteNotFoundError` is not raised by any method yet.
- `ResearchStatus.NOT_FOUND` — a typed lifecycle sentinel for the
  poll-observed absence of a *specific* requested research task, distinct from
  `NO_RESEARCH` ("nothing in flight"). `research.poll(notebook_id, task_id=...)`
  now returns `ResearchTask.not_found(task_id)` (status `NOT_FOUND`, carrying
  the requested id) when a non-empty pinned `task_id` matches no in-flight task;
  the unfiltered `task_id=None` empty poll still returns `NO_RESEARCH`
  unchanged. Additive and non-breaking — the poll never raises for an absent
  task (ADR-0019 Rule 4; issues #1344, #1346).
- **Typed return values for the research / mind-map / source-guide methods.**
  `research.poll` / `research.start` / `research.wait_for_completion`,
  `artifacts.generate_mind_map`, and `sources.get_guide` now return typed
  dataclasses instead of untyped `dict[str, Any]`, with a new
  `ResearchStatus` str-enum for the status field. The new public types are
  exported from `notebooklm` and `notebooklm.types`:
  `ResearchStatus`, `ResearchTask`, `ResearchSource`, `ResearchStart`,
  `MindMapResult`, and `SourceGuide`.
  ```python
  from notebooklm import ResearchStatus

  result = await client.research.poll(nb_id)
  if result.status == ResearchStatus.COMPLETED:   # also == "completed"
      for source in result.sources:
          print(source.title, source.url)

  guide = await client.sources.get_guide(nb_id, src_id)
  print(guide.summary, guide.keywords)

  mind_map = await client.artifacts.generate_mind_map(nb_id)
  print(mind_map.note_id, mind_map.mind_map)
  ```
  This is **backward-compatible**: `ResearchStatus` is a `str` enum (so
  `status == "completed"` still holds), and the returned dataclasses keep
  working as read-only mappings — `result["status"]` / `result.get("status")`
  / `result.keys()` / `"status" in result` all still work (subscript emits a
  `DeprecationWarning`; see **Deprecated** below). The dict-subscript bridge is
  removed in v0.8.0.
- **`WaitTimeoutError` — one catchable base for every wait/poll timeout.** A
  new public exception (`notebooklm.WaitTimeoutError`) is the common base of
  `SourceTimeoutError`, `ArtifactTimeoutError` (and its
  `ArtifactPendingTimeoutError` / `ArtifactInProgressTimeoutError` subclasses),
  and the new `ResearchTimeoutError`, so a single `except WaitTimeoutError`
  clause catches a wait timeout from any domain. It mixes in the built-in
  `TimeoutError`, so this is **fully backward-compatible**: existing
  `except TimeoutError` clauses keep catching every wait timeout unchanged.
  ```python
  from notebooklm import WaitTimeoutError
  try:
      await client.sources.wait_until_ready(nb_id, src_id)
      await client.artifacts.wait_for_completion(nb_id, task_id)
      await client.research.wait_for_completion(nb_id, research_task_id)
  except WaitTimeoutError:   # was three separate / inconsistent timeout types
      ...
  ```
- **`ResearchError` / `ResearchTimeoutError`.** The research domain gained a
  catchable base (`ResearchError`, mirroring `SourceError` / `ArtifactError`)
  and a domain timeout (`ResearchTimeoutError`). `ResearchAPI.wait_for_completion`
  previously raised the bare built-in `TimeoutError`; it now raises
  `ResearchTimeoutError`, a `WaitTimeoutError` (and therefore still a
  `TimeoutError`), exposing `notebook_id` / `task_id` / `timeout` /
  `timeout_seconds` / `last_status`. (`ResearchTaskMismatchError` stays a
  `ValidationError` — it is caller-input validation, not a wait timeout.)

### Changed / Deprecated

- `ArtifactTimeoutError` now declares its bases umbrella-first
  (`WaitTimeoutError, ArtifactError`), matching `SourceTimeoutError` and
  `ResearchTimeoutError`. This is a cosmetic reorder with no behavior change:
  `isinstance`/`except` against either base is unaffected.
- `client.mind_maps` mutation sites now raise `MindMapNotFoundError` instead of
  a bare `ValueError` on a missing target, so callers can `except NotFoundError`
  (or `except MindMapError`) uniformly across namespaces. `rename` (and the
  underlying note-backed `rename_mind_map`) raise it; `MindMapNotFoundError`
  multi-inherits `ValueError`'s sibling `NotFoundError`, **not** `ValueError`
  itself, so existing `except ValueError` rename callers must switch to
  `except NotFoundError` / `except MindMapNotFoundError`. `delete(kind=None)` is
  now **idempotent** — deleting an already-absent mind map returns `None` rather
  than raising (matching `sources`/`artifacts`/`notes` delete, and the
  `kind`-supplied path). `get_tree` returns `None` for a missing mind map (it is
  a derived read that does not police parent existence) — previously `kind=None`
  raised on an unknown id. Shape-drift in the interactive payload still raises
  `UnknownRPCMethodError` (ADR-0019; issues #1291, #1346).
- `client.mind_maps.generate(kind=MindMapKind.INTERACTIVE)` now raises
  `ArtifactFeatureUnavailableError` (instead of a bare `ArtifactError`) when the
  `CREATE_ARTIFACT` call returns no artifact id — no generation task was
  created. **Non-breaking for `except ArtifactError`**:
  `ArtifactFeatureUnavailableError` is a subclass of `ArtifactError`, so that
  catch still works. (It also multi-inherits `RPCError`, so a handler that does
  `except RPCError` *before* `except ArtifactError` will now take the `RPCError`
  branch — the same MRO the sibling `generate_*` / `retry_failed` null-create
  paths already produce.) This aligns the interactive async kickoff with that
  sibling null-create contract (ADR-0019 "async kickoff"; issue #1359).
- Documented two pre-existing `client.mind_maps` read semantics (docs-only, no
  behavior change): `list()` populates `MindMap.tree` only for note-backed
  entries — interactive entries carry `tree=None` ("not fetched", not "empty";
  call `get_tree(..., kind=MindMapKind.INTERACTIVE)` to fetch one); and the explicit
  `get_tree(..., kind=MindMapKind.INTERACTIVE)` path delegates absence detection to the RPC,
  so a missing id's value is server-dependent (returns `None` today) rather than
  enforced client-side (issues #1355, #1359).
- **`ResearchAPI.wait_for_completion(interval=...)` → `initial_interval=...`.**
  The research waiter's poll-cadence keyword is now `initial_interval`,
  matching `SourcesAPI.wait_until_ready` and
  `ArtifactsAPI.wait_for_completion`. The old `interval=` keyword still works
  as a **deprecated alias** (warns in 0.7.0, removed in v0.8.0): passing a
  non-default value emits a `DeprecationWarning` (suppressible with
  `NOTEBOOKLM_QUIET_DEPRECATIONS=1`), and passing both `interval` and
  `initial_interval` raises `TypeError`. Default-shape calls stay silent and
  the signature is otherwise unchanged, so the public-API compatibility audit
  stays clean. See [`docs/deprecations.md`](docs/deprecations.md) for the
  migration.
  > **Decision — `wait_timeout` kept.** The `wait_timeout` keyword on the
  > `SourcesAPI.add_*` family was deliberately **not** renamed to `timeout`:
  > on those add methods `timeout` would be ambiguous with a per-request HTTP
  > timeout, whereas the dedicated waiter methods already spell their budget
  > `timeout`. The research `interval` → `initial_interval` rename was the only
  > standardization with a clear, unambiguous win.

### Deprecated

> Every deprecation below is on a compatibility runway to **v0.8.0**. The
> consolidated [Upgrading to v0.8.0](docs/upgrading-to-0.8.0.md) guide is the
> single reference for moving your code across the boundary; set
> `NOTEBOOKLM_FUTURE_ERRORS=1` to exercise the v0.8.0 behavior in your tests
> today.

- **`client.mind_maps.get()` returning `None` for a missing mind map is now
  deprecated**, closing the runway gap that left `mind_maps` as the only
  #1247-cohort namespace without one. It now emits a `DeprecationWarning` on a
  miss while **still returning `None`** (behavior unchanged this release),
  matching `sources.get()` / `artifacts.get()` / `notes.get()`. In **v0.8.0** it
  will instead **raise** `MindMapNotFoundError`. Use `get_or_none()` for the
  sanctioned optional lookup (it stays silent), or migrate the `None`-check to a
  `try/except MindMapNotFoundError`. The warning fires only on a miss; suppress
  it with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. Tracking issue: #1247 (gap: #1358).
  See [`docs/deprecations.md`](docs/deprecations.md).
- **`sources.get()` / `artifacts.get()` / `notes.get()` returning `None` for a
  missing entity is deprecated.** These three methods now emit a
  `DeprecationWarning` on a miss while **still returning `None`** (behavior is
  unchanged this release). In **v0.8.0** they will instead **raise** the
  matching `*NotFoundError` (`SourceNotFoundError` / `ArtifactNotFoundError` /
  `NoteNotFoundError`), unifying the not-found contract with `notebooks.get()`,
  which already raises `NotebookNotFoundError`. Tracking issue: #1247.
  ```python
  # Migrate the None-check to a try/except before v0.8.0:
  # BEFORE (deprecated)
  src = await client.sources.get(nb_id, source_id)
  if src is None:
      ...
  # AFTER
  try:
      src = await client.sources.get(nb_id, source_id)
  except SourceNotFoundError:
      ...
  ```
  The warning fires only on a miss; successful lookups stay silent. Suppress it
  with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. See
  [`docs/deprecations.md`](docs/deprecations.md).
- **Dict-subscript access on the new typed research / mind-map / guide
  returns is deprecated.** Now that `research.poll` / `research.start` /
  `research.wait_for_completion`, `artifacts.generate_mind_map`, and
  `sources.get_guide` return typed dataclasses (see **Added**), the legacy
  `result["status"]` dict-subscript access emits a `DeprecationWarning` and
  will be **removed in v0.8.0**. Migrate to attribute access (`result.status`).
  The silent `result.get(...)` / `result.keys()` / `"x" in result` mapping
  shims also disappear in v0.8.0. Suppress the warning with
  `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. See
  [`docs/deprecations.md`](docs/deprecations.md).
  ```python
  # BEFORE (still works in 0.7.0, warns on subscript)
  if result["status"] == "completed":
      sources = result["sources"]
  # AFTER
  if result.status == "completed":
      sources = result.sources
  ```

### Fixed

- **CLI now emits the `NOT_FOUND` error envelope for the `*NotFoundError`
  family from the centralized handler, instead of the generic
  `NOTEBOOKLM_ERROR`.** Any `NotebookNotFoundError` / `SourceNotFoundError` /
  `ArtifactNotFoundError` / `NoteNotFoundError` / `MindMapNotFoundError` that
  reaches `cli/error_handler.py` (e.g. `notebooks.get()` on a missing notebook,
  or a `rename` whose target was deleted mid-operation) now exits `1` with the
  typed `{"error": true, "code": "NOT_FOUND", ...}` JSON envelope carrying the
  missing resource id — matching the per-command `source` / `artifact` /
  `note get` convention (the documented CLI not-found contract since v0.5.0).
  The per-command `get` paths already used `get_or_none` and are unaffected.
  This also makes the `NOTEBOOKLM_FUTURE_ERRORS=1` preview faithful at the CLI
  boundary, pre-positioning it for the v0.8.0 `get()` → raise / mutate-existing
  fail-loud flips (issues #1364, #1247, #1362).
- **`Source.from_api_response` now reports the real processing `status`.** The
  `ADD_SOURCE` / rename parsing path previously never read the status block and
  always fell back to `SourceStatus.READY`, while `client.sources.list()` /
  `get()` and the source poller read the decoded status. Both parsers now
  funnel through a single `Source.from_row` construction site, so a `Source`
  produced from an add/rename response carries the same `status` (and `url` /
  `created_at`) as the listing path. The `Source.status` field annotation was
  also corrected from `int` to `SourceStatus` (still an `int`-compatible enum).

## [0.6.0] - 2026-05-29

### Breaking changes

> **⚠ BREAKING — exception hierarchy symmetry restored.**
>
> `SourceNotFoundError` and `ArtifactNotFoundError` now inherit from `RPCError`
> in addition to their respective domain bases (`SourceError`,
> `ArtifactError`), restoring symmetry with `NotebookNotFoundError` which has
> mixed in `RPCError` since the 0.5.x series. Combined with the new
> `NotFoundError` umbrella (see **Added** below), the class declarations are
> now:
>
> ```python
> class NotebookNotFoundError(NotFoundError, RPCError, NotebookError): ...
> class SourceNotFoundError(NotFoundError, RPCError, SourceError): ...        # new RPCError mixin in 0.6.0
> class ArtifactNotFoundError(NotFoundError, RPCError, ArtifactError): ...    # new RPCError mixin in 0.6.0
> ```
>
> **Migration.** Code that catches the broad `RPCError` *before* a more
> specific `SourceNotFoundError` / `ArtifactNotFoundError` clause now routes
> "not found" through the broad branch instead of falling through to the
> specific one. Reorder your `except` clauses so the more specific exceptions
> come first.
>
> The example below uses `client.sources.get_fulltext(...)`, which raises
> `SourceNotFoundError` for a missing source. (`client.sources.get(...)`
> returns `None` and does not raise, so it doesn't demonstrate the change.)
>
> ```python
> # BEFORE — in 0.5.x this layout worked: SourceNotFoundError was NOT an
> # RPCError, so it fell through the broad `except RPCError` to the specific
> # handler. In 0.6.0 the broad handler catches it first, leaving the
> # specific `except SourceNotFoundError` clause unreachable.
> try:
>     fulltext = await client.sources.get_fulltext(notebook_id, source_id)
> except RPCError as e:        # ← in 0.6.0 this also catches SourceNotFoundError
>     handle_rpc_failure(e)
> except SourceNotFoundError:  # ← in 0.6.0 this branch becomes unreachable
>     handle_missing_source()
>
> # AFTER — put the specific exception first so the broad branch only sees
> # other RPC failures.
> try:
>     fulltext = await client.sources.get_fulltext(notebook_id, source_id)
> except SourceNotFoundError:
>     handle_missing_source()
> except RPCError as e:
>     handle_rpc_failure(e)
> ```
>
> Code that catches `SourceNotFoundError` / `ArtifactNotFoundError` directly,
> or catches via the domain bases (`SourceError`, `ArtifactError`), or via the
> shared `NotebookLMError` base, continues to behave exactly as before. Only
> the `RPCError`-before-specific ordering is affected.
>
> `SourceNotFoundError.__init__` and `ArtifactNotFoundError.__init__` also
> now accept keyword-only `method_id` / `raw_response` parameters (forwarded
> to the `RPCError` parent), matching the `NotebookNotFoundError` signature.
> All positional call sites remain source-compatible.

- **`notebooklm source stale <ID>` now follows the standard CLI exit-code convention by default.** Exit `0` indicates the freshness check succeeded (regardless of whether the source is fresh or stale); exit `1` indicates an error. Previously the command used an inverted predicate (`0` = stale, `1` = fresh) so the shell idiom `if notebooklm source stale ID; then refresh; fi` worked naturally. **Migration:** scripts that depended on the inverted predicate can opt back into the legacy semantics with the new `--exit-on-stale` flag (`if notebooklm source stale --exit-on-stale ID; then refresh; fi`). Scripts written for the new default should branch on the JSON `stale`/`fresh` fields or stdout text. See [`docs/cli-exit-codes.md`](docs/cli-exit-codes.md#source-stale-exit-on-stale) for the full rationale + the new `Exit code semantics` summary.
- **`NotebookLMClient.rpc_call(...)` no longer accepts `source_path`, `_is_retry`, or `operation_variant`** — the three kwargs deprecated in v0.5.0 (`docs/deprecations.md`) were removed after one MINOR cycle. The public escape hatch's primary contract (`client.rpc_call(method, params)`) is unchanged and the default-shape call keeps working with no migration. Migration:
  - **Keyword callers**: drop the removed kwarg from the call. The previous default-shape behavior (`source_path="/"`, `_is_retry=False`, `operation_variant=None`) is now what every call gets unconditionally — `source_path` was a leaky internal seam, `_is_retry` was an internal retry-loop flag, and `operation_variant` is part of the mutating-RPC idempotency registry. Calls that genuinely needed a non-`"/"` `source_path` or a specific `operation_variant` were already on the wrong layer; build a typed method on a sub-client instead, or open an issue describing the workflow.
  - **Positional callers** (rare): the positional order of the remaining parameters is `(method, params, allow_null, *, disable_internal_retries=...)`, so a previously-positional `source_path` / `_is_retry` argument now binds to a different parameter slot. A pre-cut `client.rpc_call(method, params, "/", True)` (which passed `source_path="/"`, `allow_null=True`) becomes `client.rpc_call(method, params, allow_null=True)` after the cut — switch to keyword arguments for `allow_null` to avoid this footgun.
  - There is no public replacement for the removed internal-only kwargs (`_is_retry`, `operation_variant`); they were never part of the supported surface in the first place.
- **`source add --url` rejects internal hosts by default (SSRF guard).** `localhost`, `127.0.0.1`, RFC-1918, and link-local URLs — and any non-`http(s)` scheme — are now refused before ingestion. **Migration:** pass the new `--allow-internal` flag to ingest an internal `http(s)` URL intentionally (the scheme allowlist still applies). Full detail in **Security** below ([#1114](https://github.com/teng-lin/notebooklm-py/pull/1114)).
- **`source` CLI `--json` output shape changed.** `source get --json` now emits the bare kind value (`"type": "url"`) instead of the leaked Python enum repr (`"type": "SourceType.URL"`), and `source fulltext --json` emits a fixed `{source_id, title, kind, content, url, char_count}` payload instead of a raw `asdict(SourceFulltext)` dump. **Migration:** `--json` consumers parsing `source get`'s `type` field, or relying on extra `fulltext` keys, must update. Full detail in **Fixed** below ([#1129](https://github.com/teng-lin/notebooklm-py/pull/1129)).
- **Post-parse CLI validation errors exit `1` (was `2`) and print a JSON envelope on stdout under `--json`.** For `download` flag conflicts, `generate` validation, `research wait --cited-only`, and `ask --new` + `--conversation-id`, a `--json` invocation now emits `{"error": true, "code": "VALIDATION_ERROR", ...}` on stdout and exits `1` instead of Click's stderr usage text + exit `2`. Text-mode behavior is unchanged. **Migration:** automation parsing these `--json` failures should branch on exit `1` + the JSON body. Full detail in **Changed** below (ADR-0015; [#1112](https://github.com/teng-lin/notebooklm-py/pull/1112), [#1115](https://github.com/teng-lin/notebooklm-py/pull/1115), [#1117](https://github.com/teng-lin/notebooklm-py/pull/1117)).

### Added

- **`notebooklm source stale --exit-on-stale` flag** — opt-in back-compat for the legacy inverted-predicate exit codes (`0` = stale, `1` = fresh). The default behavior is now the standard CLI convention (see **Breaking changes** above); pass `--exit-on-stale` to keep `if notebooklm source stale --exit-on-stale ID; then refresh; fi` shell idioms working.
- **`Exit code semantics` summary section in [`docs/cli-exit-codes.md`](docs/cli-exit-codes.md#exit-code-semantics).** A normative one-line table — `0` = succeeded as documented, `1` = failed or queried target not found, `2` = Click parser-time error — backing the convention every command obeys outside the documented intentional exceptions. Cross-references the existing tables and [ADR-0015](docs/adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md).
- **`NotFoundError` cross-domain umbrella exception.** Catch `NotFoundError` to handle any "resource not found" case across notebooks, sources, and artifacts in one `except` clause — replacing `except (NotebookNotFoundError, SourceNotFoundError, ArtifactNotFoundError):`. `NotebookNotFoundError`, `SourceNotFoundError`, and `ArtifactNotFoundError` all inherit from `NotFoundError`. The umbrella itself is additive; the asymmetric inheritance noted on its original introduction has been resolved in the same release — all three subclasses also mix in `RPCError` (see **Breaking changes** above for the `except`-ordering migration).
- **`notebooklm notebook delete --json`** ([#1167](https://github.com/teng-lin/notebooklm-py/issues/1167)). `notebook delete` was the last delete command (and the only `list` / `create` / `metadata` sibling) without a JSON envelope — passing `--json` crashed with `No such option`. It now emits the typed success/cancel envelope, refuses to prompt in `--json` mode (requiring `--yes`, else a `VALIDATION_ERROR` envelope + exit `1`), and surfaces `context_cleared: true` when the deleted notebook was the active context ([#1193](https://github.com/teng-lin/notebooklm-py/pull/1193)).
- **`notebooklm skill install --dry-run` / `--no-clobber` / `--force`** ([#1109](https://github.com/teng-lin/notebooklm-py/pull/1109)). Project-scope installs now classify each target as create / up-to-date / overwrite. A target that would be overwritten with *different* content exits `1` and lists the conflicts unless `--force` (overwrite) or `--no-clobber` (skip differing, still create missing) is passed; `--dry-run` previews intended writes without touching disk. Writes go through an atomic temp-file + `os.replace` so a crash can't leave a partial `SKILL.md`. User scope keeps the historical always-overwrite behavior (the new flags error when paired with `--scope user`).
- **`GenerationStatus.is_removed` + `status="removed"`** ([#1168](https://github.com/teng-lin/notebooklm-py/issues/1168)). A delisted or quota-removed artifact now reports `status="removed"` (`is_removed=True`) instead of a synthesized `"failed"`, so callers can distinguish a transient list omission from a server-marked FAILED artifact. `is_failed` stays `False` for a removal; `is_rate_limited` still treats a quota-worded removal as retryable, and CLI exit behavior is unchanged ([#1195](https://github.com/teng-lin/notebooklm-py/pull/1195)).
- **Structured media-timeout diagnostics.** When an *accepted* media task (audio / video / cinematic-video / infographic / slide-deck) stays queued or running past the `--wait` / `wait_for_completion` budget, the artifact APIs now raise a typed timeout exception that preserves the last poll-status transition and media-not-ready metadata (also surfaced in `--json`) instead of a bare timeout ([#1094](https://github.com/teng-lin/notebooklm-py/issues/1094)).

### Changed

- **Media `--wait` default timeouts raised.** `generate audio --wait` now defaults to 1200 s ([#1140](https://github.com/teng-lin/notebooklm-py/pull/1140)) and the video / cinematic-video wait defaults were increased to match empirical generation durations ([#1088](https://github.com/teng-lin/notebooklm-py/pull/1088), [#1094](https://github.com/teng-lin/notebooklm-py/issues/1094)), so long generations no longer time out before the artifact is ready under default settings. `docs/` now documents the media wait budgets and the manual `artifact wait` recovery path.
- **`notebooklm doctor` exits `1` when any check fails** ([#1160](https://github.com/teng-lin/notebooklm-py/issues/1160)). It previously built `status: "fail"` entries but always exited `0`, so CI health checks, `set -e` scripts, and monitoring probes read a broken install as green. Overall health is now computed from the final check states (after any `--fix`) and the process exits `1` if any check still fails (warnings stay non-fatal). The exit happens after the payload/table is emitted, so machine-readable `--json` output is unaffected; `doctor` profile JSON errors are also now wrapped in the typed envelope ([#1179](https://github.com/teng-lin/notebooklm-py/pull/1179), [#1146](https://github.com/teng-lin/notebooklm-py/pull/1146)).
- **Post-parse CLI validation errors emit the typed JSON envelope under `--json`** ([ADR-0015](docs/adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md)). `download` flag conflicts (`--force` + `--no-clobber`, `--latest` + `--earliest`, `--all` + `--artifact`), `generate` validation (cinematic `--format` / `--style` conflicts, invalid `--language` / `NOTEBOOKLM_HL`), `research wait --cited-only` without `--import-all`, and `ask --new` + `--conversation-id` now route through `{"error": true, "code": "VALIDATION_ERROR", ...}` on stdout and exit `1` under `--json`, instead of Click's parser bypassing the envelope to exit `2` with usage text on stderr. Text-mode behavior (usage text, exit `2`) is unchanged. Flagged under **Breaking changes** above for `--json` automation ([#1112](https://github.com/teng-lin/notebooklm-py/pull/1112), [#1115](https://github.com/teng-lin/notebooklm-py/pull/1115), [#1117](https://github.com/teng-lin/notebooklm-py/pull/1117)).

### Fixed

- **`notebooklm artifact delete <id> --json` now requires `--yes` before deleting** ([#1197](https://github.com/teng-lin/notebooklm-py/issues/1197)). Without `--yes`, the command emits the typed `VALIDATION_ERROR` envelope, includes `"deleted": false`, exits `1`, and leaves the artifact untouched, matching the other destructive delete commands.
- **HTML file uploads now fail client-side with a clear validation error** ([#1127](https://github.com/teng-lin/notebooklm-py/issues/1127)). `notebooklm source add ./article.html` and `client.sources.add_file(..., "article.html")` previously reached NotebookLM's upload endpoint as `text/html` and surfaced a cryptic upstream `400 Bad Request`. The upload pipeline now rejects `.html` / `.htm` / `.xhtml` / `.xht` / HTML MIME uploads before registering a source, with guidance to convert the page to `.txt`, `.md`, or `.pdf`.
- **`notebooklm source fulltext -o FILE` no longer silently overwrites existing files** ([#1173](https://github.com/teng-lin/notebooklm-py/issues/1173)). Existing output paths now auto-rename by default (`FILE` -> `FILE (2)`, etc.); pass `--force` to overwrite intentionally or `--no-clobber` to fail when the path already exists.
- **`sources.list()` raises on a malformed `GET_NOTEBOOK` response under strict-decode (the default)** ([#1159](https://github.com/teng-lin/notebooklm-py/issues/1159)). A drifted or error-enveloped response was previously folded into an empty list, so a sync script could conclude every source had vanished and re-add them all. The hand-rolled list-shape checks now honor `NOTEBOOKLM_STRICT_DECODE` (logging the drift warning, then raising `RPCError`); a genuinely empty notebook (a `None` sources slot) still returns `[]`. Set `NOTEBOOKLM_STRICT_DECODE=0` for the legacy warn-and-return-`[]` fallback ([#1178](https://github.com/teng-lin/notebooklm-py/pull/1178)).
- **`client.rpc_call(..., allow_null=True)` raises on method-ID drift and anti-bot walls** ([#1158](https://github.com/teng-lin/notebooklm-py/issues/1158)). The decoder gated its entire null-handling block behind `not allow_null`, so opt-in null callers (`CREATE_ARTIFACT`, `GENERATE_MIND_MAP`, `DELETE_SOURCE`, `GET_SUGGESTED_REPORTS`, …) silently received `None` when Google rotated a method ID or served a redirect / anti-bot page. An absent RPC ID (drift) and a body with no RPC frames (anti-bot wall) now always raise; only a present-but-`null` `wrb.fr` frame returns `None`. Null-result error messages now embed the discovered `found_ids` ([#1176](https://github.com/teng-lin/notebooklm-py/pull/1176)).
- **Auth-refresh replay no longer re-issues non-idempotent writes** ([#1157](https://github.com/teng-lin/notebooklm-py/issues/1157)). After a mid-flight auth error (HTTP 401/403, or an auth-shaped decoded `RPCError`) on a probe-then-create method (`CREATE_NOTEBOOK`, `CREATE_ARTIFACT`, `CREATE_NOTE`, `ADD_SOURCE`, `SHARE_NOTEBOOK`, `GENERATE_MIND_MAP`), the refresh-and-retry path could duplicate the resource, invite email, or generation quota when the error landed *after* the server committed the write. Both replay paths (the `AuthRefreshMiddleware` 401/403 leg and the `RpcExecutor` decode-time leg) now honor the effective `disable_internal_retries` classification and propagate the original auth error so the caller's probe-then-create wrapper can disambiguate a commit-lost write ([#1177](https://github.com/teng-lin/notebooklm-py/pull/1177)).
- **`client.notes.create` raises when `CREATE_NOTE` returns no usable note id** ([#1162](https://github.com/teng-lin/notebooklm-py/issues/1162)). It previously fell through to a success-shaped `Note(id="")` that was never finalized via `UPDATE_NOTE` or persisted server-side, so any later operation keyed on the empty id silently misbehaved. It now raises `RPCError`, matching the sibling `add_source` / `notebooks.create` paths ([#1186](https://github.com/teng-lin/notebooklm-py/pull/1186)).
- **Stale authed-POST envelope rebuilt after a `401 → refresh → 429 → retry` flow** ([#1096](https://github.com/teng-lin/notebooklm-py/pull/1096)). The terminal freshness guard's snapshot-equality short-circuit could POST the pre-refresh URL / headers / body against the refreshed cookie jar; the envelope is now rebuilt from a freshly captured auth snapshot on every terminal attempt (byte-identical on the happy path, load-bearing on the post-refresh retry).
- **`NotebookLMClient.close(drain=True)` no longer hangs on in-flight artifact polls** ([#1161](https://github.com/teng-lin/notebooklm-py/issues/1161)). Registered drain hooks (which cancel polls parked in `operation_scope`) now fire before the drain wait, so `close()` short-circuits a pending poll instead of blocking up to the poll's own 300 s timeout ([#1182](https://github.com/teng-lin/notebooklm-py/pull/1182)).
- **`Kernel.open()` closes the httpx client if the open-time cookie snapshot raises** ([#1163](https://github.com/teng-lin/notebooklm-py/issues/1163)). A failure while capturing the open snapshot previously propagated with a live, never-closed client (Python skips `__aexit__` after a failed `__aenter__`), leaking the connection pool. `open()` now `aclose()`s the partial client and resets it so a retry rebuilds cleanly ([#1187](https://github.com/teng-lin/notebooklm-py/pull/1187)).
- **RPC concurrency semaphore gains the loop-affinity guard + close→reopen reset its siblings already have** ([#1169](https://github.com/teng-lin/notebooklm-py/issues/1169)). The per-client `max_concurrent_rpcs` semaphore was the only loop-bound primitive without an affinity guard or reset, so reopening a capped client on a different event loop reused the stale semaphore and could raise "bound to a different event loop" or mispark waiters on Python 3.10/3.11 (masked on 3.12+). It is now guarded by the bound-loop assertion and discarded on any bound-loop change ([#1196](https://github.com/teng-lin/notebooklm-py/pull/1196)).
- **New conversations are serialized per notebook** ([#1144](https://github.com/teng-lin/notebooklm-py/pull/1144)). Concurrent `chat.ask()` calls with no `conversation_id` against the same notebook are serialized so they no longer race to create duplicate server-side conversations.
- **Auth-refresh lock released if the lock-wait metric raises** ([#1164](https://github.com/teng-lin/notebooklm-py/issues/1164)). `await_refresh` recorded the lock-wait metric between `acquire()` and the `try`, so a metric-side exception left the auth-refresh lock held forever, deadlocking every subsequent refresh. The metric call moved inside the `try` / `finally: release()`, matching the sibling `update_auth_tokens` ([#1188](https://github.com/teng-lin/notebooklm-py/pull/1188)).
- **Source-upload registration fails closed on an unparseable source id** ([#1143](https://github.com/teng-lin/notebooklm-py/pull/1143)). The resumable-upload path now raises instead of silently accepting a response it can't parse a source id from, while still tolerating the legacy filename-first row shapes.
- **Artifact-generation defaults and null responses hardened** ([#1063](https://github.com/teng-lin/notebooklm-py/issues/1063), [#1088](https://github.com/teng-lin/notebooklm-py/pull/1088)). Omitting infographic options on the Python `client.artifacts.generate_*` calls now sends concrete visual defaults (matching the CLI) instead of producing a null `CREATE_ARTIFACT` result, and a null artifact-generation response is now classified as `ArtifactFeatureUnavailableError`.
- **`source` command `--json` output shape corrected and stabilized** ([#1129](https://github.com/teng-lin/notebooklm-py/pull/1129)). `source get --json` previously leaked the Python enum repr (`"type": "SourceType.URL"`) and now emits the bare kind value (`"type": "url"`); `source fulltext --json` now emits a fixed `{source_id, title, kind, content, url, char_count}` payload instead of a raw `asdict(SourceFulltext)` dump, and its `-o` envelope gains a `kind` field. **`--json` consumers that parsed `source get`'s `type` field or relied on extra `fulltext` keys must update** (flagged under **Breaking changes** above). Shared serializers keep the shape consistent across the source subcommands going forward.
- **`notebooklm source add -` (stdin) rejects a non-text `--type`.** Piping content from stdin with an explicit non-text source type now fails with a clear validation error instead of mis-routing the content.
- **`notebooklm agent show` routes errors to stderr** ([#1175](https://github.com/teng-lin/notebooklm-py/issues/1175)) so they no longer pollute stdout.
- **Auth-error classification hardened** ([#1142](https://github.com/teng-lin/notebooklm-py/pull/1142)) — empty RPC code labels no longer slip past the auth-error matcher.
- **Malformed `batchexecute` chunk records are now counted** ([#1141](https://github.com/teng-lin/notebooklm-py/pull/1141)) rather than silently dropped, so the `client.metrics` surface reflects partial-response drift.

### Removed

- `NotebookLMClient.rpc_call(source_path=...)`, `NotebookLMClient.rpc_call(_is_retry=...)`, `NotebookLMClient.rpc_call(operation_variant=...)` — see Breaking changes above. The corresponding `DeprecationWarning` emitters in `client.py` and the `tests/unit/test_rpc_call_public_surface.py` warning-surface tests were retired in the same change.

### Security

- **SSRF guard on `source add --url`** ([#1114](https://github.com/teng-lin/notebooklm-py/pull/1114)). The prefix-only `startswith(("http://", "https://"))` check was replaced with a structural `urlsplit` parse + scheme allowlist (`http` / `https` only) plus a private / loopback / link-local IP guard and a `localhost`-literal guard. **Behavior change:** `http://localhost`, `http://127.0.0.1`, RFC-1918 hosts, and `http://169.254.169.254` are now rejected by default — pass the new `--allow-internal` flag to ingest an internal URL intentionally (the scheme allowlist still applies). DNS is never resolved at validation time. Flagged under **Breaking changes** above.
- **Resumable upload URLs validated and redacted** ([#1130](https://github.com/teng-lin/notebooklm-py/pull/1130)). The server-returned upload session / cancel URLs are validated before use and redacted in error and log output so a credentialed upload URL can't leak.
- **Artifact download allowlist validated by hostname** ([#1172](https://github.com/teng-lin/notebooklm-py/issues/1172)). Download host-allowlisting now parses the URL hostname structurally instead of matching a string prefix, closing a bypass where a crafted URL (including encoded-slash hosts, hardened further in [#1199](https://github.com/teng-lin/notebooklm-py/pull/1199)) could satisfy a prefix check while pointing at an untrusted host.
- **`httpx` / `urllib3` logs redacted for library consumers** ([#1166](https://github.com/teng-lin/notebooklm-py/issues/1166)). `configure_logging()` now attaches a logger-level `RedactingFilter` to the `httpx` and `urllib3` loggers at import, so a consumer who enables httpx DEBUG (e.g. `logging.basicConfig(level=logging.DEBUG)`) no longer sees the session id in `?f.sid=...` request lines. Pure defense-in-depth — no handler is added, so consumers who never enable those loggers see no behavior change ([#1191](https://github.com/teng-lin/notebooklm-py/pull/1191)).
- **Bare CSRF / session-id token values redacted in logs** ([#1165](https://github.com/teng-lin/notebooklm-py/issues/1165)). The scrubber now redacts bare `SNlM0e` (CSRF) and `FdrFJe` (session-id) `WIZ_global_data` markers, the `csrf=` form alias, and standalone `AF1_QpN-` CSRF tokens — credential-equivalent shapes that previously passed through `scrub_secrets()` unredacted ([#1189](https://github.com/teng-lin/notebooklm-py/pull/1189)).
- **Playwright login subprocess output sanitized** ([#1111](https://github.com/teng-lin/notebooklm-py/pull/1111)). `ensure_chromium_installed` now strips ANSI control sequences and redacts inherited environment-variable secret values (including JSON-nested leaves such as `NOTEBOOKLM_AUTH_JSON`) from captured subprocess stderr/stdout before surfacing install diagnostics (meta-audit G4).

## [0.5.0] - 2026-05-23

The first release after the v0.4.x auth cookie lifecycle series. Headline user-facing work: a top-to-bottom CLI UX overhaul (uniform `--json`, exit-code policy, shell completion, stdin pipes, SIGINT-resume), auth and cookie reliability hardening (inline PSIDTS cold-start recovery, fail-closed `notebooklm use`, concurrent-upload safety), and the v0.3-era deprecation removal cycle. **Read Breaking changes below before upgrading.**

### Breaking changes

Items that need attention when upgrading from 0.4.x. Full migration prose lives in the natural sections below.

- **`NOTEBOOKLM_STRICT_DECODE` now defaults to `1`** — RPC shape drift raises `UnknownRPCMethodError` (subclass of `RPCError`) at the decoder boundary instead of warning and returning `None`. Set `=0` to opt back into the legacy behavior for one release window (the soft-mode fallback itself now emits `DeprecationWarning` and is scheduled for removal in v0.6.0).
- **`rate_limit_max_retries` default raised from `0` to `3`** with exponential-backoff fallback. Programmatic users now inherit smart-retry behavior matching the CLI. Pass `rate_limit_max_retries=0` to restore the previous immediate-`RateLimitError` behavior. Mutating create RPCs already opt out via `disable_internal_retries=True`.
- **`server_error_max_retries` default raised from `0` to `3`** with the same exponential-backoff fallback, covering HTTP 5xx + retryable network errors (#629). Pass `server_error_max_retries=0` to restore immediate failure on 5xx.
- **`max_concurrent_rpcs` semaphore added with default `16`** (#630). High-fan-out callers (e.g. `asyncio.gather` over 100 RPCs) are now throttled by default instead of saturating the connection pool. Pass `max_concurrent_rpcs=None` to restore unbounded fan-out. Must satisfy `max_concurrent_rpcs <= ConnectionLimits.max_connections`.
- **`notebooklm use <id>` fails closed when the notebook doesn't exist.** `use` now verifies the id with `NotebooksAPI.get(id)` before persisting and exits `1` without writing to `context.json` on a missing notebook / wire failure / auth-expiry. Pass `--force` to bypass verification. `NotebookNotFoundError` now inherits from both `RPCError` and `NotebookError`.
- **`source get` / `artifact get` / `note get` exit `1` on not-found (was `0`).** Matches the rest of the CLI's user-error convention so scripts can branch on the exit code. `--json` failure body uses the standard `{"error": true, "code": "NOT_FOUND", ...}` envelope.
- **`generate cinematic-video --format <non-cinematic>` exits `2` with a UsageError** instead of silently overriding the conflict. Drop the conflicting flag, or use `generate video --format <value>` if a non-cinematic format was intended.
- **`NOTEBOOKLM_REFRESH_CMD` defaults to `shell=False`** (security hardening for the shell-injection footgun when the env var is sourced from CI configs). Now parsed with `shlex.split` and invoked with `subprocess.run(argv, shell=False, ...)`. Set `NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1` (literal `"1"` only) to opt back into the legacy `shell=True`.
- **`source add` no longer follows symlinks by default.** A workspace symlink like `~/Downloads/foo.pdf → /etc/passwd` previously resolved and uploaded the target with no warning. The path now refuses symlink traversal with a `ClickException` (exit `1`) unless `--follow-symlinks` is explicit. Scripts that point at symlink-resolved paths must add the flag (#476).
- **YouTube cookies no longer scraped or trusted by default at login / refresh.** The cookie-domain allowlist split into REQUIRED (NotebookLM + Drive + RotateCookies) and OPTIONAL (YouTube / Docs / Mail / myaccount). Pass `--include-domains=youtube` (or `=all`) on `login` / `auth refresh --browser-cookies <browser>` / `auth inspect` to opt YouTube back in; pass `=docs`/`=mail`/`=myaccount` to opt those sibling domains in explicitly (#483).
- **Artifact generation without `language=` now honors the configured language.** The Python `client.artifacts.generate_*` methods now resolve omitted `language` via `NOTEBOOKLM_HL` / global config / `"en"` instead of hard-coding `"en"` at the signature. Pass `language="en"` for a fixed English payload.
- **`--storage <path>` no longer shares the default profile's notebook context.** A previously-run `notebooklm use <id>` against the default profile is invisible to a later `notebooklm --storage X.json <cmd>` (and vice versa) because `--storage` now derives a sibling `<path>.context.json`. Set the active notebook explicitly via `notebooklm --storage <path> use <id>`, `-n/--notebook`, or `NOTEBOOKLM_NOTEBOOK` env var (#467).
- **`login --browser-cookies --account EMAIL` now writes the active/default profile by default** instead of creating a profile from the email local-part. Use `--profile-name NAME` to write a separate named profile, or `--storage PATH` for an exact file. Existing profile auth for a different or unknown account prompts before overwrite (#987).
- **v0.3-era deprecated APIs removed** — `Source.source_type`, `Artifact.artifact_type`, `Artifact.variant`, `SourceFulltext.source_type`, `StudioContentType`, `DEFAULT_STORAGE_PATH`, `notebooklm.cli.language.save_config`. Migrate to the `.kind` property and `notebooklm.paths.get_storage_path()`. See **Removed** below.
- **Cookie identity widened to `(name, domain, path)`** per RFC 6265 §5.3. Writes remain backward-compatible (flat dicts / legacy 2-tuples still accepted); reads of `auth.cookies` with the old 2-tuple key now raise `KeyError`. Use `auth.cookies[("SID", ".google.com", "/")]`, `auth.flat_cookies["SID"]`, or `auth.cookie_header`.

### Added

#### Auth and reliability
- **Inline `__Secure-1PSIDTS` cold-start recovery.** When a storage file has `__Secure-1PSID` but no `__Secure-1PSIDTS`, a preflight POST to `accounts.google.com/RotateCookies` mints a fresh token before any RPC traffic, so cold-start workers no longer fail on the first call. Cross-process flock serializes concurrent cold starts; respects `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` ([#865](https://github.com/teng-lin/notebooklm-py/issues/865), [#872](https://github.com/teng-lin/notebooklm-py/pull/872)).
- **`NOTEBOOKLM_BASE_URL` env var for enterprise NotebookLM deployments** (#402). Routes RPC + auth traffic through a non-`google.com` base URL; cookie-domain allowlist auto-extends to the enterprise host. Previously enterprise users had to monkey-patch internals.
- **`NOTEBOOKLM_RPC_OVERRIDES` env-var escape hatch.** When Google rotates a `batchexecute` method ID, set e.g. `NOTEBOOKLM_RPC_OVERRIDES='{"LIST_NOTEBOOKS": "newId123"}'` to keep working until a patch ships. Overrides are gated to `notebooklm.google.com` / `accounts.google.com` base hosts so a redirected base can't pivot them (#486).
- **`ConnectionLimits` dataclass for httpx pool tuning.** Pass `ConnectionLimits(max_connections=200, ...)` to `NotebookLMClient(...)` for long-running agents and high-fan-out workers — no more monkey-patching internals (#527).
- **`max_concurrent_rpcs` constructor arg** (default `16`, #630). Bounds simultaneous in-flight RPCs to protect the connection pool under fan-out. `None` opts out — see **Breaking changes** for the default-shift note.
- **`--include-domains` flag on `login` / `auth refresh --browser-cookies <browser>` / `auth inspect`.** Backs the REQUIRED/OPTIONAL cookie-domain split described in **Breaking changes** — passing `=youtube`/`=docs`/`=mail`/`=myaccount` (or `=all`) opts those OPTIONAL domains back in. Accepts repeated-flag or comma-separated syntax (#483).
- **In-memory `__Secure-1PSIDTS` recovery during `--browser-cookies` extraction** ([#990](https://github.com/teng-lin/notebooklm-py/pull/990), [#991](https://github.com/teng-lin/notebooklm-py/pull/991)). When `rookiepy` returns a partial cookie set (most often when the browser hasn't rotated `__Secure-1PSIDTS` yet), a single `RotateCookies` POST against the live browser cookies mints the missing token before persistence. Recovery declines surface scenario-specific hints (`No SID → "You are not signed in to Google in <browser>"`, `PSIDTS missing + secondary binding intact → "RotateCookies recovery did not succeed. Open https://notebooklm.google.com in <browser>"`) instead of the previous generic "No valid Google authentication cookies found".

#### Chat
- **`client.chat.delete_conversation(notebook_id, conversation_id)` + `notebooklm ask --new` is now genuinely destructive** (#824). Captures the web UI's "Delete history" action (`J7Gthc` RPC) so callers can force-end a server-side conversation; the next `ask()` with no `conversation_id` starts a brand-new thread. **⚠ Deleted turns are not recoverable.** CLI prompts for confirmation; `--json` implies `--yes`.
- **`notebooklm ask --new` flag** (previously promised in the docstring but undeclared) — starts a fresh conversation, mutually exclusive with `--conversation-id`.
- **`notebooklm ask --timeout`** per-invocation HTTP timeout, mirroring `source add --timeout`.
- **`ChatReference.answer_range` + `.score`** (#686). Every reference now exposes the answer-text span it grounds (start/end char positions) and the model's relevance score — useful for highlighting cited passages and ranking sources.
- **`chat save` preserves inline citation hover anchors** (closes #660, [#675](https://github.com/teng-lin/notebooklm-py/pull/675)). Saved notes retain `[citation]`-style anchors so users can hover-preview the source passage that grounded each claim in the NotebookLM web UI.

#### CLI ergonomics
- **Uniform `--json` envelopes** on every detail and mutating command: `artifact get/rename/delete/poll/export`, eight `source` subcommands (`delete/rename/refresh/clean/get/delete-by-title/add-drive/stale`), `note get/save/create/delete/rename`, `notebooklm configure`, and `notebook use`. Detail commands mirror the underlying dataclasses; mutating commands emit `{"id": ..., "renamed|deleted|exported": true, ...}`.
- **Standard download flag set on `download quiz` / `download flashcards`** — `--all`, `--latest`, `--earliest`, `--name`, `--dry-run`, `--force`, `--no-clobber`, `--json` — so one wrapper script works across every artifact type.
- **Uniform `--timeout` / `--interval`** on `generate <kind> --wait`, `artifact wait`, and `source wait`.
- **`--limit=N` and `--no-truncate` on every `list` command, plus `--no-truncate` on `chat history`.** `chat history --no-truncate` lifts the hardcoded 50-char preview on Question/Answer columns.
- **Shell completion + ID-aware completers.** `notebooklm completion <bash|zsh|fish>` prints a completion script; once sourced, `-n/--notebook`, `-s/--source`, and `-a/--artifact` TAB-complete live IDs from the active profile.
- **SIGINT resume hint on long-running `--wait` ops.** Ctrl-C exits 130 with `Cancelled. Resume with: notebooklm artifact poll <task_id>` (or the parallel `source wait <source_id>`) instead of dumping a `KeyboardInterrupt` traceback. Under `--json`: `{"error": true, "code": "CANCELLED", "resume_hint": "..."}`.
- **Unix `-` stdin convention on `ask`, `note create`, `source add`, and `--prompt-file`.** `echo "what is X?" | notebooklm ask -` and similar pipelines now compose without temp files.
- **`NOTEBOOKLM_NOTEBOOK` env var + global `--quiet` flag.** `NOTEBOOKLM_NOTEBOOK=<id> notebooklm ask "..."` works without `-n/--notebook` or a prior `notebooklm use`. `--quiet` suppresses status output, raises the package logger floor to ERROR, and remains mutually exclusive with `-v/-vv`.
- **`source add` warns when a path-shaped argument doesn't exist.** A typo like `./missin.md` previously fell through to inline-text ingestion silently; an advisory stderr warning now fires before the source is added.
- **`--follow-symlinks` opt-in on `source add`.** See **Breaking changes** above; scripts that point at symlink-resolved paths must add the flag to keep working (#476).
- **`source clean` command** (#261). Bulk-delete failed/stale sources in a notebook; pairs with `source stale` for inspection. Supports `--all`, `--latest`, `--earliest`, `--dry-run`, and `--json`.
- **`notebooklm create --use` flag** (#220, #413). `create --use "title"` makes the new notebook the active context in one step. (Plain `create` no longer auto-switches the context — `--use` is the explicit opt-in.)
- **Chromium-profile selectors on `login --browser-cookies chromium:<profile>`** (#648). Pick a specific Chrome user profile (e.g. `chromium:Profile_1`) instead of always defaulting to the first profile. Useful for users with multiple Google accounts in one browser install.
- **`auth login --update` on `--all-accounts`** (#594). Replaces the stored state for an already-logged-in account instead of refusing on conflict.

#### Python API
- **Source fulltext markdown format.** `client.sources.get_fulltext(..., output_format="markdown")` and `source fulltext -f markdown` (closes #222). Requires the optional `markdownify` extra (`pip install "notebooklm-py[markdown]"`).
- **Public `client.rpc_call(method_id, params)`** (#646). A documented escape hatch for invoking any `batchexecute` RPC method directly when no high-level API wraps it yet. Pairs with `NOTEBOOKLM_RPC_OVERRIDES` for community self-patching while waiting on a fix.
- **Observability hooks + drain API on `NotebookLMClient`** (#643). New `on_rpc_event` callback (per-call timing + status), `client.metrics` snapshot, and `await client.drain()` for graceful shutdown. Designed for long-running agents needing visibility without monkey-patching.
- **Correlation IDs + categorized logging** (#430, #431). Every RPC carries an `X-Correlation-ID` (also surfaced on log records); log records are categorized (`rpc.call`, `rpc.retry`, `auth.refresh`, `upload.chunk`, …) for filtering. Credential redaction now covers every log surface by default.
- **Per-call upload timeouts on `sources.add_file` / `add_drive`** (#618). New `upload_timeout` / `chunk_timeout` keyword args for tuning large-file uploads against slow networks.
- **`ResearchAPI.wait_for_completion(notebook_id, task_id=None, *, timeout=1800, interval=5)`** ([#970](https://github.com/teng-lin/notebooklm-py/pull/970)). Polls until research reaches a terminal state (`completed` / `failed`) or the timeout fires; passes through `task_id` on subsequent polls once the backend assigns one to prevent a later concurrent task from substituting its sources/report. Surfaces a new terminal `failed` status so wait loops no longer spin until timeout after the backend rejects a task.
- **`notebooklm.artifacts.with_rate_limit_retry(callable, *, max_retries=3, ...)`** ([#969](https://github.com/teng-lin/notebooklm-py/pull/969)). Shared retry helper for the `client.artifacts.generate_*` family — catches generation-time `RateLimitError`, honors `retry_after`, and falls back to exponential backoff. Replaces the per-caller try/except/sleep boilerplate previously suggested in `docs/python-api.md`.
- **`__all__` declared on `notebooklm.paths`, `notebooklm.migration`, and `notebooklm.notebooklm_cli`** ([#958](https://github.com/teng-lin/notebooklm-py/pull/958)). ADR-0012 marks all three as public modules; `__all__` now pins the exported surface (12 names on `paths`, 3 on `migration`, `cli` + `main` on the CLI entry point) so `from notebooklm.paths import *` is well-defined and the public API compatibility audit can lock it.

### Changed
- **Custom `--storage` downloads now use the selected auth file** ([#838](https://github.com/teng-lin/notebooklm-py/issues/838), [#888](https://github.com/teng-lin/notebooklm-py/pull/888)). `ArtifactDownloadService` previously snapshotted the session's storage path at construction time, so `--storage` overrides applied after construction were silently ignored on download. CLI `--storage` flag and mid-process profile switches are now inherited reliably.
- **`--storage <path>` derives a sibling `<path>.context.json` per file** (#467). Two `--storage` invocations against different files no longer leak notebook state through the default profile. Precedence: explicit `--storage` > profile > legacy home-root. (See **Breaking changes** for the script-impact note.)
- **Conversation IDs are now server-assigned** (#659, #667). `ChatAPI.ask()` returns whatever the server creates instead of minting a local UUID. Previously-saved conversation IDs from a v0.4.x session remain valid against the server.
- **Cross-event-loop reuse fails fast with `RuntimeError`** (#633). One `NotebookLMClient` instance is bound to its `open()`-time event loop; reusing it from a different loop (common in hot-reload servers, worker pools) now raises on the first authed POST instead of failing with cryptic httpx errors.
- **`notebook use` surfaces the typed auth-aware error on expired credentials.** Text mode shows the canonical "Not logged in" walkthrough with the `notebooklm login` remediation; `--json` emits the standard `AUTH_REQUIRED` envelope.
- **`download <type>` exception paths route through the typed error handler.** `--json` is honored on the exception path; `RateLimitError.retry_after` surfaces as both a JSON field and a "Retry after Ns" text line; `AuthError` shows the canonical re-auth hint.
- **`notebooklm login` and `notebooklm auth refresh` no longer leak Python tracebacks on unexpected failures.** Unexpected exceptions become a single friendly line + bug-report URL with exit code `2`; original traceback remains available at `-vv`.
- **`--wait` paths show a transient spinner with elapsed timer** and an empirical typical-duration hint where known (e.g. `typically 30-40 min` for cinematic-video). No-op under `--json`.
- **CLI group docstrings synced with the live registered subcommand set.** `source`, `download`, `artifact`, and `note` group `--help` blocks now enumerate every registered subcommand (previously missed `add-drive`, `add-research`, `clean`, `wait`, `cinematic-video`, `quiz`, `flashcards`, `suggestions`, `rename`).
- **`notebooklm --help` bins five previously-orphaned top-level commands** into primary sections: `auth` → **Session**; `metadata` → **Notebooks**; `agent` / `skill` / `language` → **Command Groups**.
- **`artifact poll` vs `artifact wait` `--help` clarified on ID kind.** `poll <task_id>` straight from `generate`; `wait <artifact_id>` resolved against `artifact list`.
- **First-run profile migration no longer races concurrent invocations** (#478). Previously two `notebooklm` invocations starting under a fresh home (container start-up races, parallel test runs, MCP worker pools) could both run the copy-and-delete migration. Lock waits past 30 s raise a domain-specific `MigrationLockTimeoutError(RuntimeError)`.
- **`RPCError.raw_response` previews capped at 80 chars; `NOTEBOOKLM_DEBUG=1` opts into full body.** Previously embedded a 500-char preview of the upstream response — noisy in CI and capable of leaking large server payloads (#479).
- **`RPCError.rpc_id` and `RPCError.code` deprecations revoked.** Both are now permanent aliases for `method_id` / `rpc_code` — removing exception diagnostic aliases can mask the original exception inside `except` handlers.
- **BREAKING: `note delete --json` without `--yes` and `note rename` lose-the-race now exit `1` (was `0`).** Two parallel surgical fixes to `cli/note.py` matching the broader `--json` exit-code convention (audit P1.T5). `notebooklm note delete <id> --json` without `--yes` now emits `{"error": true, "code": "VALIDATION_ERROR", "message": "Pass --yes to confirm deletion in --json mode", "id": ..., "notebook_id": ...}` + exit `1` (was the same payload as `{deleted: false, error: ...}` + exit `0`). `notebooklm note rename <id> "new"` when the note vanishes between the partial-ID resolve and the underlying `get` (e.g. a concurrent `note delete`) now emits the standard `{"error": true, "code": "NOT_FOUND", "message": "Note not found", "id": ..., "notebook_id": ...}` envelope + exit `1` (was `{renamed: false, error: ...}` + exit `0`). **Migration:** scripts branching on the exit code now correctly catch both misconfigurations; scripts parsing the JSON body must switch from `data["deleted"] == false` / `data["renamed"] == false` checks to `data["error"] == true` (or branch on `data["code"]`).

### Deprecated
- **`await NotebookLMClient.from_storage(...)` form.** `from_storage` now returns an awaitable async-context-manager wrapper that supports both the legacy `async with await NotebookLMClient.from_storage(...) as client:` pattern (and bare `await NotebookLMClient.from_storage(...)`) and the new canonical `async with NotebookLMClient.from_storage(...) as client:` pattern. Awaiting the call emits `DeprecationWarning`; the await form will be removed in v1.0. Migration: drop the `await` keyword from `async with await NotebookLMClient.from_storage(...) as client:` call sites.
- **`NotebookLMClient.rpc_call` kwargs `_is_retry`, `source_path`, `operation_variant`.** Emit `DeprecationWarning`; removal targets v0.6.0.
- **`NotesAPI.create_from_chat`.** Use `ChatAPI.save_answer_as_note`; removal targets v0.6.0.
- **Positional `wait` / `wait_timeout` on `SourcesAPI.add_url` / `add_text` / `add_file` / `add_drive`.** Calls like `client.sources.add_url(nb_id, url, True)` still work in v0.5.0 but emit `DeprecationWarning`; pass `wait=True` / `wait_timeout=...` as keywords. Removal targets v0.6.0. CLI is unaffected.
- **`SourcesAPI.add_file` `mime_type` parameter.** Never wired into the resumable-upload RPC — the server derives MIME from the filename extension. Passing a non-`None` value now emits `DeprecationWarning`; removal targets v0.6.0. The separate `add_drive(..., mime_type=...)` parameter is unaffected.
- **`notebooklm source add --mime-type` on the file-source path.** A no-op when the resolved source type is `file`; using it now emits a stderr deprecation note (suppress via `NOTEBOOKLM_QUIET_DEPRECATIONS=1`). Removal targets v0.6.0. The same flag on `source add-drive` is unaffected.
- **`ArtifactsAPI.wait_for_completion(poll_interval=...)`.** Use `initial_interval=...`; `poll_interval` remains accepted until v0.6.0.
- **`NotebooksAPI.share()`.** Use `client.sharing.set_public()`. Scheduled for removal in a future major release.
- **`NOTEBOOKLM_STRICT_DECODE=0` soft-mode fallback.** Each use emits `DeprecationWarning` naming the decoder source; the soft-mode path is scheduled for removal in v0.6.0.
- **`ResearchAPI.poll(task_id=None)` default on multi-task notebooks.** When multiple research tasks are in flight, `poll()` with no `task_id` now emits `DeprecationWarning` (single-task notebooks: no warning, current behavior preserved). Scheduled for removal in a future major release.

### Removed
- **v0.3-era deprecation cycle complete.** Removed `Source.source_type`, `SourceFulltext.source_type`, `Artifact.artifact_type` (use `.kind`); `Artifact.variant` (use `.kind`, `.is_quiz`, `.is_flashcards`); `notebooklm.StudioContentType` (use `ArtifactType`); `notebooklm.DEFAULT_STORAGE_PATH` (use `notebooklm.paths.get_storage_path()`); `notebooklm.cli.language.save_config` (now private).
- **RPC raw-code `StudioContentType` aliases.** `notebooklm.rpc.types.StudioContentType` and `notebooklm.rpc.StudioContentType` removed; use `ArtifactType` for public code and `ArtifactTypeCode` only for low-level RPC internals.
- **`RPCMethod.DISCOVER_SOURCES` and `RPCMethod.QUERY_ENDPOINT`.** `DISCOVER_SOURCES` was an unused enum entry never exercised by any `client.*` API. `QUERY_ENDPOINT` was an endpoint URL path, not a batchexecute RPC method; use `notebooklm.rpc.get_query_url()` for the configured streamed-chat endpoint.

### Fixed
- **Artifact generation language compatibility restored.** Omitting `language` on public `client.artifacts.generate_*` calls again defaults artifact output to `"en"`; pass `language=None` to opt in to `NOTEBOOKLM_HL` default-language resolution.
- **Source upload auth/MIME routing** ([#984](https://github.com/teng-lin/notebooklm-py/pull/984)). The resumable-upload path skipped a redundant env-auth lookup and now classifies media MIME types case-insensitively; `application/mp4` is included in the media-MIME set so `.mp4` uploads route through the media upload path instead of the generic file path.
- **Source upload rejection with status `3` now hints at the per-notebook source cap** ([#977](https://github.com/teng-lin/notebooklm-py/pull/977)). Previously surfaced as a bare `RPCError`; the error message now suggests checking the notebook source count when the server returns the cap-rejection code.
- **Windows atomic-replace races on cookie/profile writes** ([#983](https://github.com/teng-lin/notebooklm-py/pull/983)). `os.replace` on Windows can transiently fail with `ERROR_ACCESS_DENIED` (5) or `ERROR_SHARING_VIOLATION` (32) when the destination is briefly held open by AV scanners or backup software. Bounded retry with backoff handles the transient cases; persistent failures still surface.
- **IO event-loop blocking and chunked-download throughput** ([#981](https://github.com/teng-lin/notebooklm-py/pull/981)). Sync `Path.resolve()` / `open()` / `os.fstat()` on the upload path are now wrapped in `asyncio.to_thread`, keeping the loop responsive under the upload semaphore on slow filesystems. Chunked downloads use a single dedicated writer thread fed by a bounded `queue.Queue` (≈512 KiB buffered) instead of spawning one `to_thread` call per 64 KiB chunk. A bug where `ArtifactDownloadError` (raised by `download_urls_batch()` for invalid scheme / untrusted host / auth failure / HTML payload) aborted the entire batch instead of landing in `DownloadResult.failed` is also fixed.
- **`notebooklm login --browser-cookies` hardening** ([#974](https://github.com/teng-lin/notebooklm-py/pull/974)). Tightened Chromium account enumeration, cookie-jar normalization, and refresh writes so partial extractions surface a clear error instead of silently writing an incomplete `storage_state.json`. Pairs with the in-memory `__Secure-1PSIDTS` recovery shipped in [#990](https://github.com/teng-lin/notebooklm-py/pull/990) / [#991](https://github.com/teng-lin/notebooklm-py/pull/991).
- **`notebooklm login --browser-cookies` Playwright account metadata** ([#989](https://github.com/teng-lin/notebooklm-py/pull/989)). The Playwright login path now writes account metadata to the profile and validates it on subsequent refresh (rejecting bool-shaped corruption from earlier buggy writes), so `notebooklm auth refresh --all-accounts` and `--account EMAIL` can target the right profile without manual cleanup.
- **Playwright account metadata repair runs after the sync context exits** ([#1000](https://github.com/teng-lin/notebooklm-py/issues/1000), [#1002](https://github.com/teng-lin/notebooklm-py/pull/1002)). `notebooklm login` previously invoked `repair_playwright_account_metadata()` while `sync_playwright()`'s event loop was still active, which raised on `run_async()`. The repair is now deferred until after the Playwright context closes, using the captured page HTML and saved storage path.
- **`source add-research --wait` timeout path** ([#971](https://github.com/teng-lin/notebooklm-py/pull/971)). The CLI service now wraps the research wait in a typed timeout error and surfaces a resumable hint (`notebooklm research poll <task_id>`) instead of hanging until the global request timeout.
- **`notebooklm auth refresh --all-accounts` language sync runs once** ([#976](https://github.com/teng-lin/notebooklm-py/pull/976)). Previously re-issued the `notebooklm.SetLanguage` RPC once per account; now coalesces to a single sync at the end of the multi-account loop.
- **Loop-affinity guard on `sources.add_file` and `client.drain()` admission** ([#952](https://github.com/teng-lin/notebooklm-py/pull/952)). Cross-event-loop reuse already failed fast on authed RPC POSTs (#633); upload admission and drain admission now also raise `RuntimeError` instead of silently mis-binding to the wrong loop.
- **`NotebookLMClient.close()` no longer leaks the httpx pool if cancelled mid-drain** ([#950](https://github.com/teng-lin/notebooklm-py/pull/950)). A `CancelledError` raised during drain previously skipped `httpx.AsyncClient.aclose()`; close now shields the transport cleanup so the connection pool is released on every cancellation path.
- **Deep-research source import no longer requires leaving the "Add sources?" modal** ([#315](https://github.com/teng-lin/notebooklm-py/issues/315), [#882](https://github.com/teng-lin/notebooklm-py/pull/882)). The deep-research flow used to discover sources but skip the modal-confirm step, leaving sources pending until a separate UI action committed them. The CLI / `ResearchAPI.import_sources` now commits directly.
- **`DELETE_NOTE` no longer races shielded `UPDATE_NOTE` at cancel time** ([#876](https://github.com/teng-lin/notebooklm-py/pull/876)). Cancellation during an in-flight `NotesAPI.update(...)` could land a delete before the shielded write completed, then have the update resurrect the note. Cancel-time cleanup is now ordered so `DELETE_NOTE` waits for any shielded `UPDATE_NOTE` to settle.
- **Client close preserves the original exception** (#526). `NotebookLMClient.__aexit__` previously masked the original body exception when `aclose()` itself raised. Body exceptions are now preserved (chained via `__cause__`) while close-time failures still propagate; an inner shield guarantees the underlying httpx client is closed on every path.
- **Unique temp file per concurrent artifact download** (#523). Two parallel `download_*` calls against the same artifact used to share `<dest>.tmp` and clobber each other's bytes. Each invocation now allocates a unique temp file (PID + uuid suffix) and atomically renames into place.
- **`add_file` TOCTOU fix + `max_concurrent_uploads` knob** (#595). `SourcesAPI.add_file` used to open the source file twice — a path swap between the two opens could substitute a different file into a successful upload. The file is now opened once; the FD is held across size check + registration + upload. New `max_concurrent_uploads: int | None = 4` on `NotebookLMClient` caps simultaneous in-flight uploads (doubles as an FD-exhaustion guard for `asyncio.gather` fan-outs).
- **Research `task_id` cross-wire on concurrent in-flight tasks** (#619). Two research sessions in flight on the same notebook could let `ResearchAPI.poll(notebook_id)` silently return the latest task, mis-attributing source provenance to the caller's task. `poll()` gains an optional `task_id` discriminator; `import_sources()` raises the new `ResearchTaskMismatchError` (subclass of `ValidationError`) when a `research_task_id` on any source disagrees with the caller's `task_id`.
- **`RPCHealth` surfaces `httpx` exception class name on empty error messages** ([#874](https://github.com/teng-lin/notebooklm-py/pull/874)). Some `httpx` exception classes raise with empty `str(exc)`, which previously surfaced as a blank line. Health output now prefixes the class name (e.g. `ConnectTimeout:`).
- **`notebooklm login` install hint stripped the `[browser]` extra** (#416). Rich interpreted `[browser]` as a style tag, so the "Playwright not installed" message rendered as `pip install "notebooklm-py"` with no extras. Fixed by `markup=False`; also corrected the package name from `notebooklm` to `notebooklm-py`.
- **Per-create-RPC idempotency hardening** ([#801](https://github.com/teng-lin/notebooklm-py/pull/801), [#806](https://github.com/teng-lin/notebooklm-py/pull/806), [#808](https://github.com/teng-lin/notebooklm-py/pull/808), [#809](https://github.com/teng-lin/notebooklm-py/pull/809), [#813](https://github.com/teng-lin/notebooklm-py/pull/813)). Six-policy idempotency registry with probe-then-retry semantics for `ADD_SOURCE`, `ADD_SOURCE_FILE`, `CREATE_NOTE`, `CREATE_ARTIFACT`, `GENERATE_MIND_MAP`, and `START_RESEARCH` / `IMPORT_SOURCES`. Resolves duplicate-create on transient retries while still raising clear errors for genuine probe failures.

### Security
- **Comprehensive secret-leak audit closed across logging, auth, and URL handling** ([#746](https://github.com/teng-lin/notebooklm-py/pull/746), [#803](https://github.com/teng-lin/notebooklm-py/pull/803), [#903](https://github.com/teng-lin/notebooklm-py/pull/903)). A multi-iteration sweep tightening every surface that could leak credentials or grant codes:
  - `payload_preview`, `final_url`, and share-URL IDs scrubbed in error paths (#746).
  - `repr()` redaction on auth objects, `NOTEBOOKLM_REFRESH_CMD` stdout/stderr redaction, Playwright cookie-jar domain filter, atomic profile-state writes (#803).
  - Standalone `__Secure-1PSIDTS` / `__Secure-3PSIDTS` / `__Secure-1PAPISID` / `__Secure-3PAPISID` cookie redaction in `_logging.py` (previously only caught inside `Cookie:` / `Set-Cookie:` header values); `_safe_url` redacts the URL **path** with `/<redacted>` on Google OAuth hosts (`accounts.google.com`, `oauth2.googleapis.com`, `oauth2.googleusercontent.com`) and subdomains, so opaque grant codes in paths like `/o/oauth2/auth/<token>` no longer leak through `ValueError` interpolations or CSRF / session-id drift surfaces (#903).

## [0.4.1] - 2026-05-11

> **Compatibility note.** Despite a few additive items (`notebooklm auth refresh` CLI, `keepalive=` constructor argument on `NotebookLMClient`, `NOTEBOOKLM_REFRESH_CMD` env var, two new dataclass fields), 0.4.1 is shipped as a patch release because the dominant work — and the reason to ship now — is auth/cookie stability remediation. Bumping to v0.5.0 would force the long-deferred removal of v0.3-era deprecated APIs (see [Stability](docs/stability.md)) earlier than scheduled; we'd rather keep that change isolated from the auth cookie lifecycle work. All additive items are backward compatible — existing code keeps working without changes.

### Added
- **`notebooklm auth refresh` CLI command** - One-shot keepalive that opens a session, triggers the layer-1 SIDTS rotation poke against `accounts.google.com`, persists the rotated cookies to `storage_state.json`, and exits. Designed to be scheduled by the OS (launchd / systemd / cron / Task Scheduler / k8s CronJob) to keep an idle profile from staling out between user-driven calls. Pairs naturally with `--quiet` for log-only-on-error cron output. Requires file/profile-backed authentication — explicitly refuses to run when `NOTEBOOKLM_AUTH_JSON` is set (no writable backing store). See `docs/troubleshooting.md` for per-OS scheduler recipes (#336).
- **Periodic keepalive task on `NotebookLMClient`** - Long-lived clients (agents, workers, multi-hour `async with` blocks) can opt into a background task that periodically POSTs `RotateCookies` to drive `__Secure-1PSIDTS` rotation, then persists rotated cookies to `storage_state.json` immediately so a crash doesn't lose the freshness. Disabled by default — pass `keepalive=<seconds>` to `NotebookLMClient(...)` or `NotebookLMClient.from_storage(...)` to enable. Values below `keepalive_min_interval` (default 60 s) are clamped up to that floor. The loop swallows transient errors at DEBUG and continues; cancellation on `__aexit__` is clean. Persistence runs off-loop via `asyncio.to_thread` so the loop never blocks on disk I/O. Closes the gap left by the per-call layer-1 poke for clients that never re-call `fetch_tokens` (#297, #312, #341).
- **Auto-refresh on auth expiry** - `fetch_tokens` now optionally runs a user-provided shell command when a Google session cookie has expired, reloads cookies from the same storage path, and retries once. Opt in by setting the `NOTEBOOKLM_REFRESH_CMD` environment variable to a command that rewrites `storage_state.json` (e.g. a sync script reading from a cookie vault). Refresh commands receive `NOTEBOOKLM_REFRESH_STORAGE_PATH` and `NOTEBOOKLM_REFRESH_PROFILE` so profile-aware scripts can target the active auth file. Covers every CLI entry point without changing the public API. Retry guards prevent refresh loops (#336).
- **`examples/refresh_browser_cookies.py`** - Sample `NOTEBOOKLM_REFRESH_CMD` script that re-extracts cookies from a live local browser via `notebooklm login --browser-cookies`. Provides a recovery path for unattended automation when the in-process keepalive isn't enough (idle gaps, force-logout, password change).
- **`Source.created_at` and `GenerationStatus.url` public dataclass fields** - `Source.created_at` is now populated for both nested and deeply-nested response paths. `GenerationStatus.url` is now populated by `poll_status` for media artifact types (audio, video, infographic, slide-deck PDF) so callers can stream the asset as soon as the status flips to ready (#349, #356).
- **`ALLOWED_COOKIE_DOMAINS` extended for sibling Google products** - The browser-cookie import path now accepts cookies from Google's sibling product domains, restoring `--browser-cookies` flows for users whose active Google session lives on a sibling surface rather than `notebooklm.google.com` directly (#362).

### Fixed
- **Cookies could silently stale out under sustained use** - `fetch_tokens` now POSTs to `https://accounts.google.com/RotateCookies` (Chrome's dedicated unsigned rotation endpoint) before hitting `notebooklm.google.com` to drive `__Secure-1PSIDTS` / `__Secure-3PSIDTS` rotation. Empirically validated against both DBSC-bound (Playwright-minted) and unbound (Firefox-imported) profiles. RPC traffic against `notebooklm.google.com` alone does not appear to trigger rotation, so a keepalive that hit NotebookLM alone could silently stale out. The rotated `Set-Cookie` lands in the live `httpx` jar and is persisted via `save_cookies_to_storage()` along the `fetch_tokens_with_domains` / `AuthTokens.from_storage` paths. A 60 s mtime guard rate-limits the layer-1 poke — the POST is skipped when storage was recently rotated. Failures log at DEBUG and never abort token fetch. Disable with `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1` (e.g. networks that block `accounts.google.com`). Closes #312 (#345, #346).
- **Concurrent `RotateCookies` poke stampede** - The 60 s mtime guard only debounces *sequential* invocations; under `asyncio.gather` fan-out, parallel CLI loops, or MCP worker pools, all callers see the same stale `storage_state.json` mtime and stampede the POST. Three layered protections inside `_poke_session`: a per-event-loop, per-storage-path async lock registry plus a sync state lock for in-process dedup (an `asyncio.gather` of 10 fires exactly one POST), a non-blocking `LOCK_EX | LOCK_NB` flock on the new `.storage_state.json.rotate.lock` sentinel for cross-process dedup (parallel CLI loops / MCP workers skip silently when another process is rotating), and a failure-stampede protection where the timestamp updates regardless of POST outcome — so a 15 s timeout against a hung `accounts.google.com` doesn't let 10 fanned-out callers each wait the full timeout. The layer-2 keepalive loop now calls the bare `_rotate_cookies` directly (it's already self-paced via `keepalive_min_interval`) and `NOTEBOOKLM_DISABLE_KEEPALIVE_POKE` continues to disable both layers (#347, #348).
- **`Notebook.sources_count` parsed but never surfaced** - The `sources_count` field on the public `Notebook` dataclass is now populated from `data[1]` on both LIST and GET notebook shapes; previously it always read as `0` regardless of actual source count (#350).
- **`Artifact.url` unpopulated for media artifacts** - The `url` field on the public `Artifact` dataclass is now populated for media types (audio, video, infographic; slide-deck exposes the PDF URL — use `download_slide_deck(output_format="pptx")` for PPTX) so callers no longer need to drop down to `download_*` to obtain the asset URL (#349, #356).
- **Cross-process and refresh-path save races** - Close lifecycle and refresh-path saves now serialize correctly with the keepalive writer; concurrent writers no longer overwrite each other's rotated cookies (#344).
- **Keepalive ↔ close serialization; stop mutating caller `Auth`** - The keepalive task no longer races with `__aexit__`, and no longer mutates the `Auth` instance the caller passed in. Callers that share an `Auth` across multiple clients now get the isolation the API documented (#343).
- **Snapshot keepalive cookie jar; normalize explicit `storage_path`** - The keepalive task now snapshots the live `httpx` jar before writing (avoiding torn writes when an RPC is mid-flight); an explicit `storage_path=` argument to `NotebookLMClient` is normalized onto the `Auth` instance so the keepalive task writes to the file the caller actually pointed at (#342).
- **Per-domain cookie scoping on file upload** - File-upload requests now send only cookies whose `Domain` attribute applies to the upload host, instead of the full jar. Prevents upload rejection when the jar mixes cookies for `google.com`, `notebooklm.google.com`, and `googleusercontent.com` (#373, #374).
- **Two-tier cookie validation pre-flight** - Auth loaders now distinguish "missing-but-recoverable" from "fatal" cookie states before attempting an RPC, surfacing clearer errors and avoiding doomed requests against Google's identity surface (#372).
- **Preserve cookie attributes on load** - `Domain`, `Path`, `Secure`, `HttpOnly`, and `SameSite` attributes round-trip through storage load, restoring behaviors that depended on cross-host scoping (#365, #368).
- **Unify flat-cookie selection across loaders** - Legacy flat-cookie and modern Playwright storage shapes now share a single selection contract; subtle mismatches between the two paths are eliminated (#375, #376).
- **Tolerate non-numeric / out-of-range timestamp values on dataclasses** - `Notebook.created_at`, `Source.created_at`, and `Artifact.created_at` now catch `TypeError`, `ValueError`, `OSError`, and `OverflowError` from `datetime.fromtimestamp` and resolve to `None` instead of raising on edge-case server responses (#357).
- **`examples/refresh_browser_cookies.py` `--profile` placement** - The example invoked `... login --browser-cookies <b> --profile <p>` but `--profile` is a top-level Click option and was rejected after `login` (`Error: No such option: --profile`). Now invokes `... --profile <p> login --browser-cookies <b>` and works end-to-end against profile-backed storage.

### Infrastructure
- **Consolidated URL extraction** - `_extract_artifact_url`, per-type extractors (audio/video/infographic/slide-deck), and `_is_valid_artifact_url` moved to `types.py`. Readiness checks, `Artifact.url`, `GenerationStatus.url`, and the download paths now share one URL-selection contract: `mp4` quality-4 > any `mp4` > first valid URL for video. `SourcesAPI.get_fulltext` fixed for YouTube fulltext URLs at `metadata[5][0]` along the way (#349, #356).
- **Removed redundant `ArtifactsAPI` URL helpers** - Private `_is_valid_media_url` and `_find_infographic_url` shim methods removed; tests now exercise the canonical `types.py` helpers (#358).
- **E2E `--profile` pytest flag** - `pytest --profile <name>` scopes the E2E notebook ID cache to a named profile, so parallel multi-profile test runs don't collide on the cached notebook fixture (#340).

## [0.4.0] - 2026-05-09

### Added
- **Multi-account profiles** - Switch between Google accounts without re-authenticating (#227)
  - `notebooklm profile create/list/switch/rename/delete` commands
  - Global `--profile` / `-p` flag and `NOTEBOOKLM_PROFILE` environment variable to scope any command to a profile
  - Per-profile storage paths under `~/.notebooklm/profiles/<name>/`
  - Implicit default profile preserved for backward compatibility; existing `~/.notebooklm/storage_state.json` is auto-detected as the default profile (no manual migration needed)
- **`notebooklm doctor` diagnostic command** - `notebooklm doctor [--fix] [--json]` checks profile setup, auth, and migration status; reports actionable issues
- **Microsoft Edge SSO login** - `notebooklm login --browser msedge` for organizations that require Edge for SSO (#204)
- **Browser cookie import** - Reuse cookies from your existing browser session without driving Playwright
  - `notebooklm login --browser-cookies <browser>` (chrome, edge, firefox, safari, etc.)
  - New `convert_rookiepy_cookies_to_storage_state()` Python helper
  - Optional `[cookies]` extra installs `rookiepy` (`pip install "notebooklm-py[cookies]"`)
  - Honors the active profile: `notebooklm --profile <name> login --browser-cookies <browser>` writes to that profile's `storage_state.json`. Note that cookie extraction always pulls the source browser's currently-active Google account for `google.com` / `notebooklm.google.com` — to populate multiple profiles from the same browser, switch the active Google account in the browser between runs (or use a separate browser per profile).
- **EPUB source type** - Upload `.epub` files as notebook sources (#231)
- **Agent skill installation** - Install the bundled NotebookLM skill into local AI agents (#206, #207)
  - `notebooklm skill install` - Install into `~/.claude/skills/notebooklm` and `~/.agents/skills/notebooklm`
  - `notebooklm skill status` - Check installation state
  - `notebooklm agent show codex` / `notebooklm agent show claude` - Print bundled agent templates
- **Mind map customization** - `client.artifacts.generate_mind_map()` now accepts `language` and `instructions` parameters (#252)
- **`note list --json`** - Machine-readable note listings (#259)
- **Bare status codes in decoder errors** - Decoder surfaces server status codes on null RPC results for clearer diagnostics (#114, #294)

### Fixed
- **Cross-domain cookie preservation** - Login storage state retains cookies across `google.com` and `notebooklm.google.com` subdomains, restoring sessions for regional domains
- **NotebookLM subdomain cookies** - Subdomain cookies are no longer dropped during login (#334)
- **Video artifact detection** - Correctly detect completed video media URLs in polling responses (#333)
- **Research import on unavailable snapshots** - CLI gracefully handles missing source snapshots during research import (#335)
- **Source import retry** - Filtered partial-import retry payloads and tightened verification to avoid false positives (#321, #327)
- **Server-state verification on timeout** - Prevents duplicate inflation when source imports time out (#319)
- **Playwright navigation interruption** - Handles updated Playwright behavior on already-authenticated sessions (#214, #322)
- **Login subprocess on Windows** - Use `sys.executable` for Playwright subprocess calls (#279)
- **Legacy Windows Unicode output** - Sanitized output streams for legacy Windows consoles (#324)
- **Settings quota errors** - Use account limits when reporting create-quota failures (#328)
- **Chat references** - Emit references only from the winning chunk to avoid >600-element duplication (#300, #310)
- **Login retry mechanism** - Resolved race conditions and improved error handling on retry (#243)
- **Quota detection during polling** - Detect quota / daily-limit failures during artifact polling (#240)
- **Google account switching** - Fixed switching between Google accounts at login time (#246)
- **YouTube URL extraction** - Extract YouTube URLs at deeply-nested response positions (#265)
- **Bare-HTTP URL fallback** - Disabled brittle bare-HTTP fallback in `sources.list()` (#294)
- **Logout context cleanup** - Clear the active notebook context on `notebooklm logout`
- **Infographic URL extraction** - Aligned with download-path logic; added regression test (#229)
- **Custom storage path for downloads** - Artifact downloads now respect custom auth storage paths (#235)
- **Windows file permissions** - Skip Unix-only `0o600` calls on Windows and rely on Python 3.13+ ACL behavior (#225)
- **TOCTOU protection** - Hardened directory creation in `session.py` (#225)

### Changed
- **`rookiepy` is an optional `[cookies]` extra** - Excluded from `[all]` to avoid Python 3.13+ install issues; install with `pip install "notebooklm-py[cookies]"`
- **Login error detection** - Improved detection of missing browser binaries (e.g., `msedge` not installed)
- **Skill installation paths** - Hardened to handle alternative `~/.claude` and `~/.agents` layouts
- **Deprecation removal deferred to v0.5.0** - The deprecated APIs originally scheduled for removal in v0.4.0 — `StudioContentType`, `Source.source_type`, `SourceFulltext.source_type`, `Artifact.artifact_type`, `Artifact.variant`, and `DEFAULT_STORAGE_PATH` — continue to work and emit `DeprecationWarning`. Removal is now planned for v0.5.0 to give downstream users an extra release to migrate.

### Infrastructure
- Pinned `ruff==0.8.6` in dev deps to match pre-commit configuration
- Bumped `python-dotenv` (#299)
- Bumped `pytest` in the `uv` group
- Added contribution templates and PR quality guidelines for issues and PRs

## [0.3.4] - 2026-03-12

### Added
- **Notebook metadata export** - Added notebook metadata APIs and CLI export with a simplified sources list
  - New `notebooklm metadata` command with human-readable and `--json` output
  - New `NotebookMetadata` and `SourceSummary` public types
  - New `client.notebooks.get_metadata()` helper
- **Cinematic Video Overview support** - Added cinematic generation and download flows
  - `notebooklm generate video --format cinematic`
- **Infographic styles** - Added CLI support for selecting infographic visual styles
- **`source delete-by-title`** - Added explicit exact-title deletion command for sources

### Fixed
- **Research imports on timeout** - CLI research imports now retry on timeout with backoff
- **Metadata command behavior** - Aligned metadata output and implementation with current CLI patterns
- **Regional login cookies** - Improved browser login handling for regional Google domains
- **Notebook summary parsing** - Fixed notebook summary response parsing
- **Source delete UX** - Improved source delete resolution, ambiguity handling, and title-vs-ID errors
- **Empty downloads** - Raise an error instead of producing zero-byte files
- **Module execution** - Added `python -m notebooklm` support

### Changed
- **Documentation refresh** - Updated release, development, CLI, README, and Python API docs for current commands, APIs, and `uv` workflows
- **Public API surface** - Exported `NotebookMetadata`, `SourceSummary`, and `InfographicStyle`

## [0.3.3] - 2026-03-03

### Added
- **`ask --save-as-note`** - Save chat answers as notebook notes directly from the CLI (#135)
  - `notebooklm ask "question" --save-as-note` - Save response as a note
  - `notebooklm ask "question" --save-as-note --note-title "Title"` - Save with custom title
- **`history --save`** - Save full conversation history as a notebook note (#135)
  - `notebooklm history --save` - Save history with default title
  - `notebooklm history --save --note-title "Title"` - Save with custom title
  - `notebooklm history --show-all` - Show full Q&A content instead of preview
- **`generate report --append`** - Append custom instructions to built-in report format templates (#134)
  - Works with `briefing-doc`, `study-guide`, and `blog-post` formats (no effect on `custom`)
  - Example: `notebooklm generate report --format study-guide --append "Target audience: beginners"`
- **`generate revise-slide`** - Revise individual slides in an existing slide deck (#129)
  - `notebooklm generate revise-slide "prompt" --artifact <id> --slide 0`
- **PPTX download for slide decks** - Download slide decks as editable PowerPoint files (#129)
  - `notebooklm download slide-deck --format pptx` (web UI only offers PDF)

### Fixed
- **Partial artifact ID in download commands** - Download commands now support partial artifact IDs (#130)
- **Chat empty answer** - Fixed `ask` returning empty answer when API response marker changes (#123)
- **X.com/Twitter content parsing** - Fixed parsing of X.com/Twitter source content (#119)
- **Language sync on login** - Syncs server language setting to local config after `notebooklm login` (#124)
- **Python version check** - Added runtime check with clear error message for Python < 3.10 (#125)
- **RPC error diagnostics** - Improved error reporting for GET_NOTEBOOK and auth health check failures (#126, #127)
- **Conversation persistence** - Chat conversations now persist server-side; conversation ID shown in `history` output (#138)
- **History Q&A previews** - Fixed populating Q&A previews using conversation turns API (#136)
- **`generate report --language`** - Fixed missing `--language` option for report generation (#109)

### Changed
- **Chat history API** - Simplified history retrieval; removed `exchange_id`, improved conversation grouping with parallel fetching (#140, #141)
- **Conversation ID tracking** - Server-side conversation lookup via new `hPTbtc` RPC (`GET_LAST_CONVERSATION_ID`) replaces local exchange ID tracking
- **History Q&A population** - Now uses `khqZz` RPC (`GET_CONVERSATION_TURNS`) to fetch full Q&A turns with accurate previews (#136)

### Infrastructure
- Bumped `actions/upload-artifact` from v6 to v7 (#131)

## [0.3.2] - 2026-01-26

### Fixed
- **CLI conversation reset** - Fixed conversation ID not resetting when switching notebooks (#97)
- **UTF-8 file encoding** - Added explicit UTF-8 encoding to all file I/O operations (#93)
- **Windows Playwright login** - Restored ProactorEventLoop for Playwright login on Windows (#91)

### Infrastructure
- Fixed E2E test teardown hook for pytest 8.x compatibility (#101)
- Added 15-second delay between E2E generation tests to avoid rate limits (#95)

## [0.3.1] - 2026-01-23

### Fixed
- **Windows CLI hanging** - Fixed asyncio ProactorEventLoop incompatibility causing CLI to hang on Windows (#79)
- **Unicode encoding errors** - Fixed encoding issues on non-English Windows systems (#80)
- **Streaming downloads** - Downloads now use streaming with temp files to prevent corrupted partial downloads (#82)
- **Partial ID resolution** - All CLI commands now support partial ID matching for notebooks, sources, and artifacts (#84)
- **Source operations** - Fixed empty array handling and `add_drive` nesting (#73)
- **Guide response parsing** - Fixed 3-level nesting in `get_guide` responses (#72)
- **RPC health check** - Handle null response in health check scripts (#71)
- **Script cleanup** - Ensure temp notebook cleanup on failure or interrupt

### Infrastructure
- Added develop branch to nightly E2E tests with staggered schedule
- Added custom branch support to nightly E2E workflow for release testing

## [0.3.0] - 2026-01-21

### Added
- **Language settings** - Configure output language for artifact generation (audio, video, etc.)
  - New `notebooklm language list` - List all 80+ supported languages with native names
  - New `notebooklm language get` - Show current language setting
  - New `notebooklm language set <code>` - Set language (e.g., `zh_Hans`, `ja`, `es`)
  - Language is a **global** setting affecting all notebooks in your account
  - `--local` flag for offline-only operations (skip server sync)
  - `--language` flag on generate commands for per-command override
- **Sharing API** - Programmatic notebook sharing management
  - New `client.sharing.get_status(notebook_id)` - Get current sharing configuration
  - New `client.sharing.set_public(notebook_id, True/False)` - Enable/disable public link
  - New `client.sharing.set_view_level(notebook_id, level)` - Set viewer access (FULL_NOTEBOOK or CHAT_ONLY)
  - New `client.sharing.add_user(notebook_id, email, permission)` - Share with specific users
  - New `client.sharing.update_user(notebook_id, email, permission)` - Update user permissions
  - New `client.sharing.remove_user(notebook_id, email)` - Remove user access
  - New `ShareStatus`, `SharedUser` dataclasses for structured sharing data
  - New `ShareAccess`, `SharePermission`, `ShareViewLevel` enums
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
- **`--retry` flag for generation commands** - Automatic retry with exponential backoff on rate limits
  - `notebooklm generate audio --retry 3` - Retry up to 3 times on rate limit errors
  - Works with all generate commands (audio, video, quiz, etc.)
- **`ArtifactStatus.FAILED`** - New status (code 4) for artifact generation failures
- **Centralized exception hierarchy** - All errors now inherit from `NotebookLMError` base class
  - New `SourceAddError` with detailed failure messages for source operations
  - Granular exception types for better error handling in automation
- **CLI `share` command group** - Notebook sharing management from command line
  - `notebooklm share` - Enable public sharing
  - `notebooklm share --revoke` - Disable public sharing
- **Partial UUID matching for note commands** - `note get`, `note delete`, etc. now support partial IDs

### Fixed
- **Silent failures in CLI** - Commands now properly report errors instead of failing silently
- **Source type emoji display** - Improved consistency in `source list` output

### Changed
- **Source type detection** - Use API-provided type codes as source of truth instead of URL/extension heuristics
- **CLI file handling** - Simplified to always use `add_file()` for proper type detection

### Removed
- **`detect_source_type()`** - Obsolete heuristic function replaced by `Source.kind` property
- **`ARTIFACT_TYPE_DISPLAY`** - Unused constant replaced by `get_artifact_type_display()`

### Deprecated
The following emit `DeprecationWarning` when accessed and were originally scheduled for removal in v0.4.0.
See [Migration Guide](docs/stability.md#migrating-from-v02x-to-v030) for upgrade instructions.

> **Note:** Removal was subsequently deferred one release; see the [0.4.0] entry above. These names will now be removed in v0.5.0.

- **`Source.source_type`** - Use `.kind` property instead (returns `SourceType` str enum)
- **`Artifact.artifact_type`** - Use `.kind` property instead (returns `ArtifactType` str enum)
- **`Artifact.variant`** - Use `.kind`, `.is_quiz`, or `.is_flashcards` instead
- **`SourceFulltext.source_type`** - Use `.kind` property instead
- **`StudioContentType`** - Use `ArtifactType` (str enum) for user-facing code

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

[Unreleased]: https://github.com/teng-lin/notebooklm-py/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.4...v0.4.0
[0.3.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/teng-lin/notebooklm-py/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/teng-lin/notebooklm-py/releases/tag/v0.1.0
