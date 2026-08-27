# Web RPC ⇄ mobile gRPC audit — gaps and bugs (2026-08-07)

**Question:** does `notebooklm-py`'s web `batchexecute` client discard information the backend
actually sends, and does it misread any field?

**Method:** a five-agent parallel audit (enums, sources, notebooks/sharing, artifacts, chat/research)
diffing our client against ground truth recovered from Google's official NotebookLM Android app,
then **confirming every claim live against the real backend on both transports**. Ground truth:
`docs/mobile/schema.proto`, the blutter analysis build (object pool + `BuilderInfo` disassembly —
see [pr-2102 review §4.10](pr-2102-quota-source-state-review-2026-08-07.md)), and live probing.

**Why both transports.** Web `batchexecute` is positional JSON — index `i` carries protobuf tag
`i + 1`, and nothing on the wire says what a field means. Mobile gRPC returns the *same backend
messages* tag-addressed. So gRPC is an oracle that removes positional guesswork, and any
web-vs-gRPC disagreement is a real finding.

> **Precondition every cross-transport finding here rests on: same account on both transports —
> which means the same profile, and "both unset" does NOT give you that.** The two credential stores
> resolve an unspecified profile *differently*: `NotebookLMClient.from_storage()` honours
> `~/.notebooklm/config.json`'s `default_profile`, while a probe harness
> defaulting to the literal `profiles/default/` directory selects a **different account** on this
> machine. That mismatch produces `PERMISSION_DENIED` and a web-created notebook missing from the
> gRPC listing — which reads exactly like a transport difference and briefly produced a wrong
> "the accounts differ" conclusion during this audit. Within a profile, `master_token.json` (gRPC)
> and `storage_state.json` (web) do match. **Set the profile explicitly on both sides and verify by
> comparing the two emails before trusting any cross-transport claim.** The findings below were
> confirmed under a matched profile.

**Evidence labels:** **CONFIRMED-LIVE** (observed against the live backend), **CONFIRMED-STATIC**
(disassembly/schema only), **PLAUSIBLE**, **REFUTED**.

---

## 1. Confirmed bugs

### 1.1 CRITICAL — `is_owner` is an inverted public-visibility flag

`_types/notebooks.py:145-151` computes `is_owner = (meta[1] is False)`. Proto tag 2 is **not**
ownership.

> **Corrected 2026-08-07 by a live two-account probe.** Tag 2 does not track *public* — it tracks
> **"this notebook has any sharing at all."** A scratch notebook that was never public (tag 13 stayed
> `false` throughout) flipped tag 2 `false → true` the moment a collaborator was added, and back on
> revoke. This *subsumes* the earlier `set_public()` observation: making a notebook public is one way
> to share it, which is why tag 2 and tag 13 moved together in that test.
>
> So `is_owner` actually evaluates to **`not (shared with anyone)`**, and the headline repro is much
> more common than the public-link case: **an owner shares their notebook with a colleague, and their
> own CLI then labels it "Shared."**

Verified on three independent protocols. gRPC, tag-addressed, same account:

```
PRIVATE  "Heavy Chain Engineering"   tag1 userRole=1  tag2=0  tag13 isPublic=0
PUBLIC   "JSON: The Standard…"       tag1 userRole=1  tag2=1  tag13 isPublic=1
```

`userRole` stays `OWNER` for both; tag 2 tracks public exactly. A web causal test agrees —
`set_public(True)` flipped `meta[1]` and `meta[12]` together and reverted on `set_public(False)`,
with `meta[0]` pinned at `1` throughout.

Independently re-verified by the lead before acceptance — our own two APIs contradict each other on
the same live notebook:

```
notebooks.list()     → is_owner=False
sharing.get_status() → is_public=True,
                       shared_users=[<account-email>, permission=OWNER]
```

**Impact:** every notebook a user owns *and has shared* is mislabelled (CLI renders "Shared").
`_notebooks.py:653` `owned_count` undercounts by exactly the number the user has shared, biasing
`NotebookLimitError`. **Fix:** read `meta[0] == 1` (`userRole == OWNER`).

**Live two-account matrix** (account A owns; B is the collaborator; `userRole` agreed between web
`meta[0]` and gRPC tag 1 at every stage):

| stage | A `userRole` | A tag 2 | A `is_owner` → CLI | B `userRole` | B `is_owner` → CLI |
|---|---|---|---|---|---|
| created, unshared | 1 OWNER | false | True → "Owner" | (no access) | — |
| shared → B **editor** | 1 OWNER | **true** | **False → "Shared"** | **2 WRITER** | False → "Shared" |
| shared → B **viewer** | 1 OWNER | true | False → "Shared" | **3 READER** | False → "Shared" |
| revoked | 1 OWNER | false | True → "Owner" | `PERMISSION_DENIED` | error |

**Three distinct roles collapse onto one string.** An owner-who-shared, a WRITER, and a READER are
all rendered "Shared" and all report `is_owner=False` — so the boolean cannot be repaired by
inverting it; it has to become the role. `WRITER=2` / `READER=3` are now **observed live**, closing a
gap §5 previously recorded as unreachable. `SharePermission` (OWNER=1/EDITOR=2/VIEWER=3) is
value-identical to `ProjectRole` — the same wire enum under two names.

**The correct data is already parsed elsewhere.** `sharing.get_status()` returned the exact
permission from B's side; it does not contradict `notebooks.list()`, it simply carries strictly more
information that `list()` discards. `userRole` is on every `GET_NOTEBOOK` **and** `LIST_NOTEBOOKS`
row, so the fix costs no extra RPC.

Of all `ProjectMetadata` tags, **only tag 1 (userRole) and tag 2 (has-sharing) varied with permission
state** across both accounts and every stage — so there is no second candidate field and no ambiguity
about the fix. `NOT_SHARED=4` is still unobserved: revocation removes access outright rather than
reporting role 4, so it presumably belongs to the access-request flow.

Related: **`isPublic` (tag 13) is 100% populated on every row and never read anywhere in
`src/notebooklm`** — we derive the wrong thing from a neighbouring slot while the correct field sits
two positions away. *(CONFIRMED-LIVE)*

### 1.2 CRITICAL — flashcards send quantity and difficulty transposed

Two sibling call sites disagree with each other:

```
_artifact/payloads.py:289  quiz:       [quantity_code, difficulty_code]   ← correct
_artifact/payloads.py:331  flashcards: [difficulty_code, quantity_code]   ← reversed
```

The backend's `FlashcardsGenerationOptions` is `{1: cardQuantity, 2: flashcardsDifficulty}` (from the
Dart `BuilderInfo`). Live scratch test: sent `quantity=FEWER(1)`, `difficulty=HARD(3)`; the server
echoed `[3, 1]` — i.e. it stored `cardQuantity=MORE(3)`, `difficulty=EASY(1)`. **A user asking for
the fewest, hardest cards gets the most, easiest.** The identical rig run against quiz echoed `[1,3]`
correctly, so this is flashcards-specific, not a systematic misunderstanding.

Verified independently in-code by the lead: the transposition between the two call sites is plain in
the source, and the live control establishes which one is right. *(CONFIRMED-LIVE + code-verified)*

### 1.3 CRITICAL — `QuizQuantity.MORE = 2` is wrong; the backend uses 3

`rpc/types.py:288-290`:

```python
FEWER = 1
STANDARD = 2
MORE = 2  # Alias for STANDARD - API limitation
```

The docstring's claim that "Google's API only distinguishes FEWER and STANDARD" is **refuted live** —
the backend accepted and persisted `cardQuantity=3`. Our enum can never emit 3, so `--quantity more`
silently produces a standard-sized set. Affects quiz and flashcards across CLI, MCP and REST.
*(CONFIRMED-LIVE + code-verified)*

### 1.4 HIGH — `ArtifactStatus` 1/2 are swapped, and three values are missing entirely

The backend enum (merged `pp.txt` + `objs.txt`) versus ours (`rpc/types.py:204-213`):

| code | backend | ours |
|---|---|---|
| 0 | `ARTIFACT_STATUS_UNKNOWN` | **absent** → `"unknown"` |
| 1 | `ARTIFACT_STATUS_INITIALIZED` | `PROCESSING` → `"in_progress"` ❌ |
| 2 | `ARTIFACT_STATUS_PROCESSING` | `PENDING` → `"pending"` ❌ |
| 3 | `ARTIFACT_STATUS_READY` | `COMPLETED` ✓ |
| 4 | `ARTIFACT_STATUS_FAILED` | `FAILED` ✓ |
| 5 | `ARTIFACT_STATUS_SUGGESTED` | **absent** → `"unknown"` |
| 6 | `ARTIFACT_PENDING_REVIEW` | **absent** → `"unknown"` |

**The two transitional states are inverted.** A live trace of a fresh artifact showed **t+4s → code
2**, t+20s → code 3 — so code 2 is what an artifact reports *while generating*. We report
`"in_progress"` for a merely-initialized artifact and `"pending"` for one actively generating;
`Artifact.is_processing` and `is_pending` are backwards with them. Terminal states 3/4 are correct,
which is why this went unnoticed.

**`6 = ARTIFACT_PENDING_REVIEW` is a state nobody knew existed** — note it breaks the naming pattern
(no `ARTIFACT_STATUS_` prefix), which is why it appears only in `objs.txt` and was missed on the
first pass. Callers keyed on "done vs still working" would misclassify it entirely.

**The mislabel is visible at our own public API.** A live generation returned
`GenerationStatus(status='pending')` while the backend artifact was at `PROCESSING` 180 ms later. A
caller polling "queued vs running" gets the wrong answer from the first response onward, and
`Artifact.is_pending` returns `True` for an artifact that is mid-generation while `is_processing`
returns `False`.

Code 2 = actively generating is **CONFIRMED-LIVE twice, independently** (two separate traces, both
`2 → 3`). Code 1 was chased with 250 ms sampling starting 180 ms after `CreateArtifact` returned and
never caught — it is already past by then.

Code 1 is nonetheless **real**: across 301 cassette artifact rows there are **25 at status 1 and zero
at status 2** — the exact inverse of the live runs. Both codes exist and are distinct; they occupy
very short, differently-sampled windows, consistent with `1 = INITIALIZED` meaning "row created,
worker not yet started". Its *semantics* stay CONFIRMED-STATIC (name from the merged dump, existence
from recorded traffic) rather than claiming a live confirmation nobody obtained.

Independent corroboration that this is the right enum: our own `_artifact/listing.py:99` already
sends the literal string `'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"'`.

**Code 6 `PENDING_REVIEW` was searched in three places and found in none** — 42 live artifacts across
16 notebooks, 301 cassette rows, and a fresh generation. It exists in `objs.txt` alone. Best read is a
review/moderation flow this account never triggers (plausibly Workspace/EDU or shared-notebook
publishing), but that is a guess and is left flagged as an unknown state our enum cannot name.

*(1/2 swap: CONFIRMED-LIVE. Codes 0/5/6 and the semantics of 1: CONFIRMED-STATIC.)*

### 1.5 MEDIUM — `ArtifactTypeCode` has two wrong comments and two missing types

Backend: `0=UNKNOWN, 1=AUDIO_OVERVIEW, 2=TAILORED_REPORT, 3=EXPLAINER_VIDEO, 4=APP, 5=MINDMAP,
6=FANTASY_MAP, 7=INFOGRAPHIC, 8=SLIDES, 9=TABLE, 10=FILE`.

- `rpc/types.py:189` says `# Note: Type 6 appears unused in current API`. It is
  `ARTIFACT_TYPE_FANTASY_MAP` — a named backend type, not an unused code.
- The `ArtifactTypeCode` docstring calls `MIND_MAP = 5` "the library's synthetic code".
  `5 = ARTIFACT_TYPE_MINDMAP` is a genuine backend code, so the "synthetic" framing is wrong even
  though our note-backed handling is legitimately separate.
- **`10 = ARTIFACT_TYPE_FILE` is absent from both our enums** (`ArtifactTypeCode` and the public
  `ArtifactType`, `_types/artifacts.py:41-50`). The proto backs it: `Artifact.file = 25` →
  `FileArtifact{fileName, mimeType, filePreviewUrl, fileDownloadUrl}`. **A whole artifact type we
  cannot represent or download.** Type 6 is likewise absent.

*(CONFIRMED-STATIC — no type-6 or type-10 artifact existed among the 42 swept.)*

### 1.6 CRITICAL — `userDriveSourceStatus` is populated and silently dropped

`SourceSettings.userDriveSourceStatus` (tag 4 → settings index 3) is read by nothing in our client;
`_web/rows/sources.py:152-153` reads index 1 only, and no `Source` field could hold it.

Across 409 live source rows:

```
settings shapes:  402x [null,2]  ·  4x [null,2,null,3]  ·  3x [null,2,[null,null,null,[]]]
[3][3]  proto 4>4  n=4  null=0  int: 3   (= DRIVE_SOURCE_STATUS_ACTIVE)
```

All four populated rows are Drive-backed and all parse to `status=READY, is_ready=True, url=None`.

**Impact:** the enum's other values are `INACCESSIBLE`, `SYNCING`, `DELETED`,
`GEN_AI_ACCESS_DENIED`. When a Drive file is unshared, deleted, or still syncing, index 3 changes
while index 1 stays `COMPLETE(2)` — because *ingestion* did complete. So `is_ready` stays `True`,
`wait_until_ready` returns instantly, and chat is grounded on a stale snapshot with no signal
anywhere. *(Population CONFIRMED-LIVE; degraded-value behaviour inferred from the enum — not staged,
as that would mutate real user data.)*

### 1.7 HIGH — `modified_at` is really *last-viewed*, and our own reads mutate it

`meta[5]` (tag 6) is `lastViewedTime`, not a modification time, and it is the sort key for
`ListRecentlyViewedProjects`. Three pure reads with no mutations advanced it
(`1786105463 → 467 → 471`), and a single bare `GET_NOTEBOOK` moved that notebook to index 0 of the
recency list.

**Impact:** `modified_at` is misnamed and misleading, and merely listing/reading notebooks through
this client silently reorders the user's "recent" ordering in the real NotebookLM UI.

The other half of the same original probe note is correct: `created_at` = tag 9, pinned across
create/share/rename/read and byte-identical over gRPC. *(CONFIRMED-LIVE)*

### 1.8 HIGH — `Source` tags 6/7/8 dropped on 41 of 409 rows

`_web/rows/sources.py:149-153` reads indices 0–3 only. Mobile's `BuilderInfo` registers four
named fields then six `addUnused()` slots — which are populated live:

```
Source[5] tag6 = "https://contribution.usercontent.google.com/download?c=…&filename=…"
Source[6] tag7 = "https://drive.google.com/viewer/upload?ds=…"
Source[7] tag8 = ["/contrib_service/blobrefs/notebooklm/nos_files/MediaDataBlobref/global::…",
                  null, "text/markdown", [["AIP70BkAAAGk…"]]]
```

**Impact:** we cannot offer download of the user's originally-uploaded file, and we lose the true
content MIME at `Source[7][2]` — independent of the Drive-only MIME slot we do read. Largest single
discarded block found.

Independently re-verified by the lead on a separate sample: 27 of 305 rows across 12 notebooks carry
all three tags — the same ~9% rate, with the same download URL / viewer URL / blobref+MIME shapes.
*(CONFIRMED-LIVE, two independent samples)*

### 1.9 HIGH — `add_drive` idempotency probe can never match

`_source/add.py:347-368` probes by URL, but all four live Drive sources parse to `url=None`
(`metadata[7]` null; `[0]`/`[9]` are lists). With `disable_internal_retries=True`, a 5xx after commit
means the probe returns `None`, the create is re-issued, and the user gets **two copies** of the file.

Our own `_types/sources.py:83-86` already documents "Drive sources carry no URL", contradicting the
probe docstring at `add.py:268-271`. The usable key is on the wire and unread:
`metadata[0][0]` / `metadata[9][0]` = Drive `documentId` (live values match the requested file ids
exactly). *(CONFIRMED-LIVE)*

### 1.10 MEDIUM — five `SourceMetadata` slots populated on all 409 rows, none read

`metadata[1]` (tag 2, size metric), `[3]` (tag 4, `[uuid,[s,ns]]` revision handle), `[8]` (tag 9,
second size metric), `[14]` (tag 15, timestamp — **409/409**, likely last-modified; we expose only
`created_at`), plus `[11]` (tag 12, second title string, 8 rows) and `metadata[5][1]/[5][2]`
(YouTube video id + channel name, 3 rows).

**We read 3 of the 9–11 populated slots.** Mobile marks these `addUnused()`, so population counts are
CONFIRMED-LIVE but the *names* are unrecovered and the semantics are PLAUSIBLE.

### 1.11 MEDIUM — `GET_SHARE_STATUS` drops 4 of 6 populated fields

Unread: `maxIndividualsShareLimit` (1000), `isPublicSharingAllowed` (true), tags 7 and 8. If
`isPublicSharingAllowed` is ever `false`, a "make public" call would presumably no-op silently while
we report success — but that branch could not be exercised (no tenant with public sharing disabled).
*(Population CONFIRMED-LIVE; silent-no-op PLAUSIBLE.)*

### 1.12 MEDIUM — `LoadSource` discards all document structure

`extract_all_text` throws away 100% of the `TailwindDoc` tree — `Citation`,
`AnnotationMapEntry{objectId,startIndex,endIndex}`, headings, lists — so **citation anchors are
lost**. Note the fix is "parse the doc", not "request a different field": `plainText` and
`markdownString` are null under all five selectors on web. *(CONFIRMED-LIVE)*

### 1.13 HIGH — every no-answer response is misclassified and emits a false "API changed" warning

The backend's normal no-answer response is a canned prose apology with **no `TailwindDoc`**, so
`AnswerRow.is_answer` evaluates `False`, and the parser falls through to its emergency path:

```
AnswerRow.is_answer -> False
WARNING "No marked answer found; falling back to longest unmarked text (159 chars).
         The API response format may have changed."
```

The format has **not** changed — this is the backend's standard no-answer shape. Callers still get
the apology text (not an empty string), so this is misclassification plus a misleading diagnostic,
not data loss. *(CONFIRMED-LIVE)*

### 1.14 HIGH — `NextStepSuggestions` dropped, and cassettes could never have revealed it

Populated on **every** live answer, e.g.
`[5]=[[["What happens if the ratio is used above 18 degrees?", 9], …]]` where `9` =
`MagicArtifactType.CONVERSATIONAL_TEXT_CHIP`. We discard it entirely.

Notably **0 of 26 cassette frames carry it** — only live probing surfaced it. This is the clearest
demonstration in the audit that cassette replay cannot answer "what does the backend actually send".
*(CONFIRMED-LIVE)*

### 1.15 MEDIUM — chat and research metadata dropped

- `isFinalResponse` (inner[4]) is `true` on exactly the last chunk of every stream; we ignore it and
  use a longest-wins heuristic instead.
- `ConversationTurnKey[1]` (conversationId) and `[2]` (conversationTurnId) populated in all 9 live
  asks, never read.
- Research drops five always-populated paths: `DiscoveryMode` (task_info[2]), created and updated
  timestamps (task[2]/[3], 8s apart on a live run), account id, and `DiscoveredSource.hint`.
*(CONFIRMED-LIVE)*

### 1.16 HIGH — the citation→answer linkage is discarded, and the offsets we substitute are wrong

The complete citation→answer linkage appears to be present in every live response and discarded.
`inner[2]` is an annotation map of `[[Range{start,end},[citation ordinals]], …]`, live:
`[[[null,112,112],[0]], [[null,231,231],[1]]]` — zero-width anchors at exactly the `[1]`/`[2]` marker
insertion points, matched by doc-body entries like `[107,112,["3:2:2",[true]]]`. That the map exists
and is unread is **CONFIRMED-LIVE**.

Two further claims were **challenged by the lead and then substantiated with raw wire values.** Both
were reframed in the process — the original descriptions were right about impact and wrong about
mechanism.

**(a) `Citation` tag 4 is the fragment's source-side *union* range, not an answer-text range.**
Our index is correct; the meaning we document is not. Raw, from a live `must_cite` capture where the
answer is **256 chars**:

```
cite_inner[3]  (tag 4 → answer_start/end_char) = [[null, 0, 561]]
cite_inner[4]  (tag 5 = TailwindDocFragment)   → 7 elements spanning [0,43] … [473,561]
union of element ranges = [0, 561]   ==  cite_inner[3][0]   → True
exceeds the 256-char answer?                                 → True
```

So `_types/chat.py:83-88` documenting `answer_start_char`/`answer_end_char` as *answer* positions is
wrong — they are source-side. This also dissolves the coincidence that made the claim look shaky:
`answer_start_char == start_char` because element 0 begins the fragment (both are 0), while the *end*
values genuinely differ (43 vs 561). Two independent paths over the **same coordinate space**.

The real answer-side data is `inner[2]`, the annotation map (above) — which we don't read at all.

**(b) `cited_text`/`end_char` return only the first fragment element — because the descent stops one
level short.** `CitationDetail.passages` returns `cite_inner[4]`, which has **length 1**: it is the
fragment *message*, not a list of passages. So the `extract_text_passages` loop runs exactly once. It
is not skipping elements and `PassageRow.is_well_formed` is not rejecting them.

> **The fix must be two-level, or it regresses.** If `CitationDetail.passages` alone is changed to
> descend to `cite_inner[4][0]` (the 7 elements) while `PassageRow` is left as-is, `PassageRow`
> unwraps `element[0]` again — which is an **int** (`0`, `43`, `129`…), not a list — so
> `is_well_formed` becomes `False` for *every* element, all are skipped, and `cited_text` becomes
> `None`. Verified on elements 0/1/2. Both `CitationDetail.passages` (descend to `[4][0]`) and
> `PassageRow` (stop unwrapping `[0]`; treat each element as `[start, end, paragraph]`) must change
> together.

Correct output for this citation would be `start=0, end=561`, 7 elements, `cited_text` **560 chars** —
against the 42 chars we return today.

*(Both CONFIRMED-LIVE from raw wire values, not derived output.)*

### 1.17 HIGH — we invented two phantom artifact error fields, and the tests encode the fiction

`_web/rows/artifacts.py:117,119` documents index 3 as "failed-artifact plain error text" and
index 5 as "failed-artifact nested error payload". **Both are wrong.** Index 3 is `Artifact.sources`
(tag 4, repeated `ArtifactSource`) and index 5 is `isPubliclyReadable` (tag 6) — confirmed by the
mobile proto and by gRPC (`tag 4: <42 sources>`, each `{1:{1:'<uuid>'}}`).

Run against three **real** failed artifacts, `ArtifactRow.failed_error_text` (`:553`) returns `None`
every time, with `raw[3]` holding the source-id list and `raw[5]` null. It is dead code that can
never fire, and it is wired into `_artifact/polling.py:490` — so every failed generation surfaces
`None` where the API promises an explanation. It is saved from emitting a source UUID *as* an error
message only because index 3 happens to be a list rather than a string.

Its unit tests (`tests/unit/test_row_adapters.py:393-404`) construct synthetic rows with strings at
index 3 — **they pass while encoding a schema that does not exist.**

Lead verification: the docstring and the `_ERROR_TEXT_POS=3` / `_ERROR_PAYLOAD_POS=5` constants are
confirmed in-source, and they contradict the mobile tag map directly. *(CONFIRMED-LIVE by the
artifact sweep; static contradiction independently verified.)*

**Recommendation:** delete `failed_error_text` and both docstring lines. If a failure reason is
wanted, it must be captured from the `CreateArtifact` RPC status at generation time —
`QuotaFailure`/`ErrorInfo`/`PreconditionFailure` exist in the binary as standard `google.rpc.Status`
detail types, delivered on the RPC, not persisted on the artifact resource. `Rytqqe`
(RETRY_ARTIFACT) existing fits that: retry is offered precisely because the resource remembers
nothing.

### 1.18 MEDIUM — `is_answer` reads a negative index into a positional wire message

`_web/rows/chat.py:682,739-751` sets `_ANSWER_MARKER_POS = -1`, reading `first[4][-1]`.
`first[4]` is `AnswerResponse.responseDoc` = `TailwindDoc`, whose `BuilderInfo` is `body=1`,
(2,3 unused), `objects=4`, `type=5`. So `[-1]` lands on `TailwindDoc.type` **only because tag 5
happens to be the last registered field today.**

The moment the backend adds tag 6 — this repo's documented #1 breakage class — `[-1]` silently reads
the new field, `is_answer` goes false for every chunk, and the parser falls through to its
"No marked answer found" path. Should be an explicit `4` with a length guard. *(CONFIRMED-STATIC)*

### 1.19 MEDIUM — `get_source_ids` cries schema-drift on every empty notebook

`_notebooks.py:328-333`, observed live on a freshly created, genuinely empty notebook:

```
WARNING get_source_ids: notebook_info[1] not list for b6e4e936-… (schema drift?). len=11
```

An empty notebook **elides** the sources slot (`None`, not `[]`). The sibling path documents and
handles exactly this (`_source/listing.py:134-140`: *"a valid empty state, NOT a malformed
response"*); `get_source_ids` lacks the carve-out. `len=11` shows the envelope is perfectly healthy.

Because genuine RPC drift **is** this project's top breakage class, a false positive that fires
routinely on that exact log string trains operators to ignore the real signal. *(CONFIRMED-LIVE)*

### 1.20 MEDIUM — chat persona/config returned free and discarded

`Project` tag 8 (`[7]`) = `[[1, 'You are a helpful science tutor'], [1]]` — the chat config / custom
persona, written by `ChatAPI.configure` via `MutateProject`. `GET_NOTEBOOK` returns it for free and we
never read it back. Also `Project` tag 9 (`[8]`) = `[False]`, populated on 14/42 frames, unread.
*(CONFIRMED-LIVE — only visible after the frame-splitter fix.)*

### 1.21 HIGH — `artifactUserState` carries the user's study/playback progress, read by nothing

`Artifact` index 17 = `artifactUserState`. **Null in every frozen cassette, populated live** — which
is why it was initially ranked LOW on static evidence and then self-corrected upward.

```
audio:      [[[872, 489796000]]]   = audioOverviewState.playbackPosition   (6/9 audio artifacts)
flashcards: appArtifactState.appState with
            cardAcquisitionsMapping {"0":"acquired", "2":"not_acquired"},
            currentCardIndex: 11, hiddenCardIndices: […]
```

This is the user's resume position and their per-card study progress, returned on the **primary
listing RPC**, and nothing in our client reads it. Recall that `GetArtifactUserState` /
`UpsertArtifactUserState` are mobile-only in the method cross-reference — so this is the web surface
already handing us the same state for free. *(CONFIRMED-LIVE; invisible in cassettes.)*

### 1.22 MEDIUM — artifact content fields dropped across every row

All CONFIRMED-LIVE, from a 267-row artifact sweep:

- **`duration`** on audio/video (872s–3436s), present on every row, unread.
- **3 of 4 media URLs** dropped (HLS + DASH variants unreachable through our API).
- **Slide and infographic alt-text and full text transcripts** — 131/131 live slides, 14/14 live
  infographics. A caller wanting slide text must currently parse the exported PDF.
- **Report kind** at `[7][1][0]` — live values include `'Concept Explanation'`, which is **not in our
  `ReportFormat` enum**.
- `sources`, `lastModifiedTimestamp`, `etag` populated on 112/112 rows, unread.

### 1.23 Lower

*(The `SourceStatus` fallback was promoted out of this list — see §1.24.)*
- `Project.emoji` (tag 4, live `📖`/`🧬`) is unparsed and unsettable.
- `premiumFeatureInfo` is `[true,true,false]` on free vs `[true,true,true]` on Pro, so
  `canViewAnalytics` is real tier-dependent state we drop.
- `chatSessions` (tag 12) appears **only** in the CREATE response, never in `GET_NOTEBOOK` — we
  discard it there and then spend an `hPTbtc` round-trip to re-fetch it later.
- `notebooks.get_or_none()` raises `ClientError` (rpc_code=5) instead of returning `None`, breaking
  the ADR-0019 contract; the docstring at `_notebooks.py:684-687` is now false. *(CONFIRMED-LIVE,
  out of original scope.)*
- `add_url` never sends `WebContent.sourceName` *(PLAUSIBLE — may be intentional)*.

---

### 1.24 HIGH — an unmapped `SourceStatus` doesn't degrade to "unknown", it asserts *healthy*

`_web/rows/sources.py:662-673` catches `ValueError` from `SourceStatus(code)` and returns
**`SourceStatus.READY`**. Verified by executing our own adapter directly:

```
raw status 0  -> SourceStatus.READY
raw status 4  -> SourceStatus.READY
raw status 99 -> SourceStatus.READY
```

Our enum lacks `0 = UNSPECIFIED` and `4 = PENDING_DELETION` (§1.6's sibling gap), so both become
`READY` and `is_ready` reports `True`. **A source queued for deletion reads as ready to use.**

This is the failure mode worth naming precisely: not "we're missing an enum value" but "an unmapped
value silently lands in a *wrong* branch instead of an explicit unknown". The docstring at `:657-660`
defends the design as automatically accepting "any new values added to `SourceStatus`" — but the
fallback is `READY`, so every future backend state is asserted healthy by default. The safe default is
the opposite. *(CONFIRMED by direct execution; lead-verified independently.)*

Not observable live: the delete path goes `status 2 → GONE` inside a 0.6s poll interval, so
`PENDING_DELETION` has no client-visible window on this transport. The degrade consequence above is
proven regardless, and is the part that matters.

---

## 2. Refuted — claims that did not survive live testing

Recorded so they are not re-raised.

| Claim | Verdict |
|---|---|
| `metadata[0][1]` is a `mimeType` | **REFUTED.** Live it is an opaque token; the cassette's `SCRUBBED_AONS` was a VCR scrubber artifact. `metadata[0][0]` *is* the Drive documentId — that half stands. |
| Markdown slot is wrong | **REFUTED.** Live `[3],[3]` returns len=5 with `markdownString` null and HTML at `result[4][1]` — exactly where we read it. **Our code is correct.** |
| `TierLimits` null because notebook is freshly created | **REFUTED.** Not an age effect — it is per-RPC. `CCqFvf` (create) has tags 10/11 null, but the *first* `GET_NOTEBOOK` already carries them, unchanged at T+0/3/10/30s. No fallback needed. |
| `AccountLimits.tier` is dead code | **REFUTED.** Live returns 5 elements with `tier=1` extracting correctly and agreeing with `premiumUserInfo[3]`. |
| Mobile gRPC endpoint has drifted | **REFUTED.** The endpoint is unchanged; the failure was a `content-type` bug in the probe harness. See pr-2102 §4.10. |
| Junk-token rate ~17.7% | **REVISED.** 17.7% on a nav-heavy recorded page, **0.0%** (0/447) on a live prose page. Data-dependent, not universal. |
| `EmptyAnswerReason` is populated and we swallow it | **REFUTED on both transports.** The enum is fully defined in the merged dump (`0 UNKNOWN, 1 UNANSWERABLE, 2 FILTERED`) and we genuinely have no reader for it — but the backend never emits it. **12 live probes across two transports and two accounts with tag 4 never populated**: 9 web asks (off-topic, nonsense-topic, wholly empty notebook, PII, mild-abusive, control) all null/absent, plus 3 gRPC `ActOnSources` where tag 4 is read *directly* and only tags 1 and 3 are ever set; plus 72 cassette rows → zero. The "can't answer" path returns *prose*. Ranked CRITICAL twice, by two agents on two routes, before live probing killed it. |
| Artifacts carry a failure reason we discard | **REFUTED.** 42 artifacts across 16 notebooks (3 genuinely FAILED), read on both transports: tag sets are identical and contain **no** reason/detail/message field. The per-type sub-message on a failed artifact holds only `generationOptions` — the request echo. Our model handles FAILED correctly (`status=4`, `is_failed=True`). |
| Citation→source linkage is lost on the chat path | **REFUTED.** `_CITATIONS_POS=3` indexes *inside* `first[4]`, not top-level, so it correctly reads `TailwindDoc.objects` (tag 4) — populated in 54/72 rows and parsed into `ChatReference`. What *is* lost is citation-to-**fulltext alignment** (§1.12), a different and narrower problem. |
| SourceType 14 mislabels content as spreadsheet | **DOWNGRADED** HIGH → MEDIUM/latent. Mechanism confirmed, but all 3 live type-14 sources labelled correctly across 65 notebooks; predicted harm did not occur. |

---

## 3. Verified clean

Checked and correct — do not re-audit: `_META_YOUTUBE_POS=5`; `_META_MIME_POS=19` (no collision with
`expertIntelligence` at 18); `metadata[9]` descriptor; `type_code==14` disambiguation (both
directions); `_HTML_BLOCK_POS=4`; all six source request builders match their mobile messages; the
full `RPCMethod` surface (47 rpcids, every captured id maps to a known method, every method exercised
by ≥1 cassette, zero orphans).

**Cross-transport confirmation of the index↔tag rule**, from a `Source` decoded off mobile gRPC:
`[3.3]`↔`metadata[2]`, `[3.5]`↔`metadata[4]`, `[3.15]`↔`metadata[14]`, `[4.2]`↔`settings[1]`.

---

## 4. Method notes and one tooling defect

**`pp.txt` alone is an incomplete enum source.** The object pool yields **74 enums / 273 values**;
merging it with `objs.txt` yields **77 enums / ~1900 values**. Auditing from `pp.txt` alone
manufactures false positives (it would wrongly flag `SourceType` 16/17 as invented) and hides real
values (`SlideDeckLength.MEDIUM=3`). The lead's original guidance named only `pp.txt` and was wrong;
use the merged dump. Extraction script and merged output live in the session scratchpad.

**Two rpcids were mis-briefed by the lead.** `cFji9` is `GET_NOTES_AND_MIND_MAPS` and `I3xc3c` is
`LIST_LABELS` — neither is chat, despite being handed to the chat audit as chat rpcids. Only `khqZz`
(`GET_CONVERSATION_TURNS`) is. More importantly, **chat answers never traverse batchexecute at all** —
they arrive on the streamed `GenerateFreeFormStreamed` endpoint, so cassette-based field coverage
cannot see them under any circumstances and a separate stream-frame walker is required.

**"Same account" holds only within a profile.** `master_token.json` (gRPC) and `storage_state.json`
(web) match inside each profile, but differ across them — `default`/`ng-master` are one account,
a second profile another. A cross-profile comparison yields `PERMISSION_DENIED` and looks like a
transport difference. Any cross-transport claim must hold the profile fixed.

**The gRPC probe harness silently hid every error.** `httpx` cannot read HTTP/2 **trailers**, and ESF
returns `grpc-status` there — so a failed call arrives as HTTP 200, zero-length body, no status, which
is indistinguishable from an empty result. Reproduced directly:

```
GetProject(empty)                -> (None, None, None)   # actually INVALID_ARGUMENT
ListRecentlyViewed(empty)        -> (None, None, None)   # actually an error
ListRecentlyViewed({2:1, 3:1})   -> 48 projects          # success
```

The lead's earlier "`ListRecentlyViewedProjects`/`GetLabels` return empty — possibly empty accounts"
report to the team was therefore **wrong**: those were hidden errors, and the real fix was the missing
`includeOwnProjects` (tag 2). Harness patched to return an explicit `INDETERMINATE`; a `grpcio`-based
caller gives true statuses. **Never read "empty" from an httpx gRPC probe as a real empty result.**

**`addUnused()` does not mean "not sent".** `ActOnSourcesResponse` populates tags 2, 4 and 7 which
mobile's own `BuilderInfo` marks unused — the first-party client ignores fields its own backend
sends. This qualifies every "mobile doesn't model it" inference in this audit.

**A byte/char bug in the audit tooling produced false negatives.** The cassette frame-splitter treated
batchexecute's length prefix as a character count when it is a **byte** count, so any response
containing non-ASCII was silently dropped. `rLM1Ne` reported 6 frames when it has **42**; `hizoJc`
decoded as empty when it was not. Fixed mid-audit, but it means **any "field is never populated"
conclusion drawn before the fix is unsafe** — free-text-heavy responses (chat answers, citations,
artifact titles with emoji) were the most affected. Re-run before trusting a negative.

**`ProjectMetadata` field names cannot be recovered, and were not invented.** gRPC confirms the tag
set exactly matches the web mapping (1,2,3,6,7,8,9,13,14,15,18,19,23), but a wire dump carries no
names, and mobile's `BuilderInfo` names only four (`userRole`, `createTime`, `isPublic`,
`audioOverviewArtifactIds`) — the rest are `addUnused()`.

> **Derivation corrected 2026-08-07.** An earlier version claimed that counting `addUnused()` calls
> "reconstructs the tag space and correctly predicts that tags 4 and 5 do not exist." It proves less
> than that. `addUnused()` reserves one **field slot**, not one **tag number**, and the reserved slots
> take the next real tags — which are not consecutive. Proof: `ProjectMetadata` has `userRole` = 1,
> then five `addUnused()`, then `createTime` = **9**. Naive counting predicts tag 7; the true set is
> {2,3,6,7,8}. So counting yields *how many* tags live in a gap, not *which* — it is a cardinality
> argument. The conclusion (tags 4 and 5 are absent) still holds, but it is the **observed web tag
> set** that establishes it, not the count.

Of the unnamed tags, **19 and 20 vary
with notebook state** (real discarded state); 3, 7, 8, 14, 15, 16, 18, 23 were constant across 16
notebooks, two accounts, all cassettes and every transition — almost certainly feature flags.

---

## 5. Coverage gaps

Stated explicitly rather than omitted.

- **`EmptyAnswerReason.FILTERED` (value 2) untested — deliberately.** Inducing it requires authoring a
  prompt engineered to trip the safety filter, which both auditors declined and the lead endorses
  declining. Residual PLAUSIBLE risk: if `FILTERED` ever fires, a caller gets a bare empty string with
  no reason. Reading `first[3]` is a cheap defensive fix regardless, and is worth doing on its own
  merits — but it is not a confirmed live bug and should be ranked below the confirmed ones.
- `ArtifactStatus` 1/2/5 not observed live — the swap is CONFIRMED-STATIC from two independent
  derivations (the mobile enum, plus our own `_artifact/listing.py:99` already sending the literal
  string `ARTIFACT_STATUS_SUGGESTED`, which proves we speak the same enum). Confirming live costs one
  generation.
- Enum values never observed live, so CONFIRMED-STATIC only: `SourceType` 2,6,7,10,11,12,15,18,19,20;
  `SourceStatus` 0,1,4,5; `ArtifactStatus` 1,5,6; `ArtifactType` 5,6,10; `AppType` 3,5;
  `SlideDeckLength` MEDIUM/LONG; `InfographicStyle` ACADEMIC; `VideoFormat` WHITEBOARD_ANIMATION;
  `DiscoveryMode` 2,3,4,6.
- ~~gRPC `GetProject` / `LoadSource` return a 0-byte payload for every request shape tried.~~
  **RESOLVED — the finding was an artifact of the broken harness.** Re-run with a `grpcio` caller that
  reads trailers: `GetProject {}` was `INVALID_ARGUMENT`, `LoadSource {1: "<uuid>"}` likewise.
  `RequestContext` was never the culprit and is optional — `LoadSourceRequest.sourceId` is a
  `SourceId` **message**, so tag 1 must be `{1: {1: uuid}}`. Both RPCs work.
  **And the open question is answered:** across **17 sources spanning 10 `OriginalSourceContentType`
  values**, `LoadSourceResponse` carried tags `[1, 4]` every time — `plainText` (tag 2) and
  `markdownString` (tag 3) absent on 17/17, `tailwindDoc` present on 17/17. So §1.12's "the fix is
  parse the doc, not request another field" is confirmed on **both** transports; the first-party
  Android client parses the same tree we do.
  Same re-check refuted the "`GetLabels` returns empty" reading (it was `INVALID_ARGUMENT`; with
  `{2: projectId}` it returns 20 labels across the account) and independently corroborated §1.21's
  `artifactUserState` on the tag-addressed transport (tag 18 populated on 7/42 artifacts, e.g.
  `playbackPosition` 1412.562 s).
- Not observable with available credentials: `userRole ∈ {WRITER, READER, NOT_SHARED}` (no
  shared-with-me notebook), `PremiumTier` 2–6 (both accounts free tier), a tenant with public sharing
  disabled, `SourceStatus` 0/4, and degraded `UserDriveSourceStatus` values.
- `src/notebooklm/_source/_upload_decode.py` (525 lines) not deeply audited.
- `MutateSource` / `RefreshSource` / `CheckSourceFreshness` are absent from the mobile binary, so they
  are web-only and have no gRPC cross-check.
