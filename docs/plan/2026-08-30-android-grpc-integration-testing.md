# Android backend integration testing (gRPC capture) — plan

**Date:** 2026-08-30 · **Branch:** `test/android-grpc-integration` · **Status:** in progress

## Decision

Follow the reviewer recommendation: do **not** make a general-purpose gRPC
record/replay runtime (`grpcvcr`) a prerequisite for the Android backend. Test
NotebookLM at its own backend seam, test gRPC locally at the wire boundary, and
touch Google minimally from a scheduled canary.

| Layer | Purpose | Runs | State on `main` (d5df15e7) | This branch |
|---|---|---|---|---|
| 1. Semantic cassette seam | Deterministic replay of sanitized protobuf through the `AndroidSession` `grpc_loader` seam | every PR | **Exists** — `tests/_helpers/android_grpc_cassette.py`, one cassette (`GetProject`), `@pytest.mark.grpc_cassette` guards in `tests/integration/conftest.py` | Record-or-replay fixture; reserved placeholders + request-local numbering; per-RPC request normalizers; 26 recorded families (every `supported` RPC except `GenerateArtifact` retry, `DeriveArtifact`, `ExportToDrive` and deep research; artifact generation is covered by quiz, report, flashcards and audio overview) listed in `tests/integration/README.md` |
| 2. Local `grpc.aio` contract suite | Real stubs, wire encoding, metadata, status, streaming, error mapping — no Google | every PR | **Partial** — `tests/unit/android/test_chat_fake_server.py` covers chat only | `tests/unit/android/test_session_fake_server.py` covering the reviewer's checklist against `AndroidSession` directly |
| 3. Live canary | Auth, method existence, field-number and shape drift that recordings cannot see | nightly / manual | **Buried** — a `GetProject` preflight step inside the full Android E2E lane of `nightly.yml` | `scripts/android_grpc_canary.py` + a light `android-grpc-health` job mirroring `rpc-health.yml`; read-only; strict decode; diagnostic hashes; never updates fixtures |

## Layer 1 — cassette seam changes

The existing format (`notebooklm.android.grpc-cassette` v1, JSON, protobuf
bytes only, no metadata) is kept. Two problems block recording more than one
trivial interaction per cassette:

1. **Encounter-ordered placeholders.** `ProtoRedactor` numbers strings/UUIDs in
   the order it meets them across requests *and* responses. A replay test cannot
   know which placeholder a user-supplied input (notebook id, question text)
   received without replaying the whole recording history.
   → `ProtoRedactor.reserve(value)` pre-assigns placeholders through the normal
   counters *before* traffic. The fixture reserves the same inputs in the same
   order in both modes, so the placeholder sequence is deterministic
   (`00000000-0000-4000-8000-000000000001`, `SCRUBBED_STRING_0001`, …) and is
   what the replay test passes to the public API.
2. **Client nonces.** `_android/chat.py` mints a random turn id per `ask`;
   byte-exact request matching would never replay.
   → Per-RPC **request normalizers** (`tests/_helpers/android_grpc_normalizers.py`)
   clear known nonce fields on both the record and replay side. They are a
   small, auditable table keyed by full method path — not a field-policy engine.
   Shipped: chat `user_message_id` only. `_android/sources.py` also mints a
   correlation id, but on the `AddTentativeSources` mutation, which no read
   family records; a future upload family must extend the table first.

Both modes drive the **public** `NotebookLMClient(..., backend="android")`
so cassette tests exercise the same assembly path as users.

* **Replay** (default, CI): `ReplayGrpcModule` + `ReplayBearer`; synthetic
  `AuthTokens`; the conftest guard already refuses any unbound `grpc.aio`
  channel or `httpx` request.
* **Record** (`NOTEBOOKLM_ANDROID_GRPC_RECORD=1`, local only): a plain live
  client creates a disposable scratch notebook (text source + note) *outside*
  the recorder, then a second client with `RecordingGrpcModule` records one
  family per cassette, then the scratch notebook is deleted. Recording is never
  a CI path and there is no live fallback on a cassette miss.

Cassette files: one per family, `tests/cassettes/android/<family>_recorded.grpc.json`.

Scope decision (2026-08-30, final): record all 43 `supported` RPCs except
`GenerateArtifact` retry, `DeriveArtifact` slides, `ExportToDrive` and
`DiscoverSourcesAsync` (deep research) — each needs state or access the scratch
notebook cannot provide (a failed artifact, a slide deck, Drive, 10+ min runs)
for no additional transport coverage. `CreateArtifact` is covered by four
families: quiz, report, flashcards and one full **audio overview** run (the
initial cut had excluded audio/video for quota reasons; audio was added on
request the same day — 23 poll rounds over ~6.5 min live, instant on replay).
Video overviews remain excluded.

Deferred on purpose: a `descriptor_sha256` pin. Every proto regeneration would
invalidate every cassette even when wire-compatible; the pinned protobuf FQNs
plus strict decode (`DiscardUnknownFields` at record time, exact type match at
replay) already fail closed on a real schema change.

## Layer 2 — contract suite checklist

In-process `grpc.aio.server` with generic handlers built from the generated
`*_pb2` types, exercised through a real `AndroidSession`:

- exact method paths for every call site; request bytes deserialize on the server;
- `authorization: Bearer …` metadata present, and *only* that (no cookie leakage);
- unary-unary and unary-stream; empty stream; partial stream then failure;
- `UNAUTHENTICATED` → `AuthError` (one bearer refresh for `replay_safe` reads,
  none for mutations), `PERMISSION_DENIED` → `AuthError`, `UNAVAILABLE` →
  `ServerError` (retried once for reads), server-side `DEADLINE_EXCEEDED` and
  client deadline → `RPCTimeoutError`, `RESOURCE_EXHAUSTED` → `RateLimitError`;
- lazy stream consumption (no RPC until first `__anext__`);
- `aclose()` cancels the wire call; channel close on session close.

## Layer 3 — canary

`scripts/android_grpc_canary.py` (read-only; dedicated account):

1. bearer acquisition + one forced refresh;
2. one unary `GetProject` on `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`;
3. one more read-only unary call (`ListChatSessions`; the backend's only unary-stream RPC writes a chat turn, so the canary never streams);
4. strict decode: unknown-field count and response-shape fingerprint per RPC,
   printed as diagnostic hashes; non-zero exit on drift or transport error.

Wired as `android-grpc-health` in `.github/workflows/rpc-health.yml`
(schedule + `workflow_dispatch`), separate from the heavy E2E matrix. It never
rewrites cassettes.

## Out of scope (pilot)

Sync parity, client-streaming, bidi, call-object fidelity, Windows storage
semantics, `grpcvcr` integration (revisit only after aio unary/unary-stream is
stable there, pinned by commit, with five clean nightlies before replacing the
consumer-owned seam).
