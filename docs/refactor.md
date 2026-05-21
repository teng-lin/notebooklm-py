# Session Capability Refactor Proposal

**Status:** Implementation complete — pending release.
The capability-protocol refactor (ADR-013) landed across Phases 1-4
(`.sisyphus/plans/refactor-completion-plan.md`). The post-refactor
runtime shape is canonicalised in [`docs/architecture.md`](./architecture.md).
The document below is preserved as the proposal that drove the work.
**Last Updated:** 2026-05-21

This proposal captured the agreed direction for repairing the feature-facing
`Session` boundary and related note/mind-map service boundaries. It is kept
as a historical record of the design intent. For the post-refactor map see
[`docs/architecture.md`](./architecture.md).

## Problem

`_session_contracts.Session` has become a broad capability bag. It currently
exposes runtime details and feature-specific capabilities to every feature API:

- `auth`
- `kernel`
- `rpc_call(...)`
- `transport_post(...)`
- `next_reqid(...)`
- `assert_bound_loop()`
- `operation_scope(...)`
- `register_drain_hook(...)`

That violates the intent of
[`ADR-010`](adr/0010-session-kernel-split.md): feature APIs should not depend on
concrete `Session` internals. It also makes feature dependencies harder to read:
most features only need logical RPC calls, while chat, uploads, and artifact
polling need narrower specialized runtime slices.

The goal is to make capabilities composable and explicit without turning every
method into a globally promoted protocol.

## Scope

In scope: feature-facing protocol boundaries between `_session_contracts.Session`
and the public feature APIs (`NotebooksAPI`, `SourcesAPI`, `ChatAPI`,
`NotesAPI`, `ArtifactsAPI`, `ResearchAPI`, `SettingsAPI`, `SharingAPI`) plus
their helper modules (`_chat_transport.py`, `_artifact_polling.py`,
`_artifact_generation.py`, `_artifact_downloads.py`, `_source_upload.py`).

Out of scope: Session-internal collaborators (`_auth/session.py:43-44`,
`_session_lifecycle.py:243-244`, `_session_auth.py:197-240`) consume the
concrete `_session.Session`, not the feature-facing capability protocols. They
are not migrated by this refactor.

## Public API Stability (v0.4.1)

**Binding constraint:** the v0.4.1 public API surface must remain intact. This
refactor changes only private constructor signatures, private collaborator
wiring, and helper-module internals. Every method, property, and attribute
reachable through `NotebookLMClient` at v0.4.1 continues to exist and behave
the same way.

Preserved public surface:

- `NotebookLMClient.from_storage(...)`, `NotebookLMClient(...)`, `__aenter__`,
  `__aexit__`, `close()`, `refresh_auth()`, `client.auth`.

`client.kernel` is **not** a public attribute today: `NotebookLMClient` has no
`kernel` property. The concrete `_session.Session.kernel` and the `_core.py`
shim (see Non-Goals) are preserved, so the private path
`client._core.kernel` continues to work — but it is not elevated to public.
- `client.notebooks.*`, `client.sources.*`, `client.chat.*`, `client.notes.*`,
  `client.artifacts.*`, `client.research.*`, `client.settings.*`,
  `client.sharing.*` — every existing method retains its signature, defaults,
  and return type.
- `client.notes.list_mind_maps(...)` and `client.notes.delete_mind_map(...)`
  continue to work; their implementation moves from `MindMapService(session)`
  to `NoteBackedMindMapService`, but the public contract is unchanged.
- `client.notes.create_from_chat(...)` keeps working; it emits a
  `DeprecationWarning` and internally forwards to
  `client.chat.save_answer_as_note(...)`. Return type and side effects are
  identical to v0.4.1 behavior.

Additive (not breaking):

- `client.chat.save_answer_as_note(notebook_id, ask_result, title=...)` is a
  new method that exposes the chat-owned saved-note workflow directly. Adding
  a new method is not a breaking change.

The refactor does not:

- Rename any public method, property, attribute, or class.
- Change any public method's signature (positional args, kwargs, defaults,
  return type).
- Move any public class out of its current import path.
- Remove any deprecated symbol that was not already scheduled for removal in
  the v0.5.x deprecation cycle.

Constructor signatures of private classes (`ChatAPI(...)`, `NotesAPI(...)`,
`ArtifactsAPI(...)`, `SourcesAPI(...)`, `NotebooksAPI(...)`,
`ResearchAPI(...)`, `SettingsAPI(...)`, `SharingAPI(...)`) are **not part of
the public API**. Their modules are underscore-prefixed (`_chat.py`,
`_notes.py`, ...) and the classes are reached only through
`NotebookLMClient.<feature>` attributes. Constructor changes are internal.

## Design Rules

1. Promote a capability to `_session_contracts.py` only when it is shared by more
   than one feature or service.
2. Keep single-feature runtime needs local to the owning feature module.
3. Concrete `_session.Session` may still implement many methods and properties,
   but feature-facing protocols must not advertise unrelated capabilities.
4. Prefer feature-owned collaborators over widening shared session contracts.
5. Remove old `core` vocabulary from touched feature APIs.
6. Do not use mixins for dependency expression. Use protocols for required
   capabilities and collaborators/services for extracted behavior.

## Shared Capability Protocols

`src/notebooklm/_session_contracts.py` should contain only shared capability
protocols.

```python
class RpcCaller(Protocol):
    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any: ...


class LoopGuard(Protocol):
    def assert_bound_loop(self) -> None: ...


class OperationScopeProvider(Protocol):
    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]: ...


class AsyncWorkRuntime(LoopGuard, OperationScopeProvider, Protocol):
    """Runtime support for feature-owned async work."""
```

Keep the full `rpc_call(...)` signature for this migration. The `_is_retry`
parameter is not ideal, but removing it would mix boundary cleanup with raw-RPC
compatibility cleanup.

### Removed From Shared Contracts

These should not be globally promoted in this pass:

- `auth`
- `kernel`
- `transport_post(...)`
- `next_reqid(...)`
- `register_drain_hook(...)` — relocate to `_artifacts.py`. The broad `Session`
  protocol's `register_drain_hook` member at `_session_contracts.py:85-89` and
  the existing standalone `DrainHookRegistration` Protocol at
  `_session_contracts.py:92-99` both move to `_artifacts.py`.

`AuthMetadata` and `Kernel` may still exist as private upload dependencies, but
they should not be members of a general feature-facing `Session` protocol.

## Feature And Helper Runtime Mapping

This map includes helper modules and test fakes, not just public feature APIs.
Any module that holds a `Session` reference today must be retyped or rewired.

| Consumer | Target Dependency | Notes |
|---|---|---|
| `NotebooksAPI` | `RpcCaller` plus injected `sources_api: NotebookSourceLister` | Source-id resolution requires the existing `SourcesAPI` collaborator (`client.py:285`). `NotebooksAPI._rpc_call` shim at `_notebooks.py:170-188` must accept `operation_variant` before retyping. |
| `ResearchAPI` | `RpcCaller` | `poll(...)` is caller-driven today; no background poll task. |
| `SettingsAPI` | `RpcCaller` | Pure logical RPC facade. |
| `SharingAPI` | `RpcCaller` | Pure logical RPC facade. |
| `SourcesAPI` | `RpcCaller` plus injected `SourceUploadPipeline` | No fallback construction from `session.kernel`, `session.auth`, or `getattr(session, "record_upload_queue_wait", None)` (`_sources.py:88`). |
| `SourceUploadPipeline` (`_source_upload.py:25`) | Local `UploadRuntime(RpcCaller, OperationScopeProvider, Protocol)` + `kernel` + `auth` constructor args | Pipeline calls `self._session.operation_scope(...)` at `_source_upload.py:281` and `self._session.rpc_call(...)` at `_source_upload.py:372`. The existing local `RpcCaller` Protocol at `_source_upload.py:43-56` is a **callable callback** protocol (used as `register_file_source(rpc_call=...)` at `_source_upload.py:345`), structurally distinct from the shared object protocol — it must be **renamed** to `RpcCallback`, not deleted, to avoid name collision. |
| `ChatAPI` | Local `ChatRuntime` | Chat-specific `transport_post(...)` and `next_reqid(...)` remain local. |
| `chat_aware_authed_post` (`_chat_transport.py:35-40`) | `ChatRuntime` (replaces `Session`) | Helper currently takes `session: Session` positionally at `_chat_transport.py:36` and calls `session.transport_post(...)` at `_chat_transport.py:66`. Signature must migrate in the same change as `ChatAPI`. The call site at `_chat.py:306` (`chat_aware_authed_post(self._core, ...)`) must migrate too. |
| `ArtifactsAPI` | Local `ArtifactsRuntime` | Artifacts owns polling, drain-hook registration, and artifact RPC facade behavior. |
| `ArtifactPollingService` (`_artifact_polling.py:14`) | `AsyncWorkRuntime` | Needs loop guard and operation scope, not RPC. |
| `_artifact_generation` (helper module) | Receives `ArtifactsRuntime` through `ArtifactsAPI` | `_artifact_generation.py:515, :611, :661, :696` call `api._core.rpc_call(...)` for non-mind-map paths. After `ArtifactsAPI` retypes to `ArtifactsRuntime` (which extends `RpcCaller`), these helpers transitively use the runtime's RPC. `_artifact_generation.generate_mind_map` at `_artifact_generation.py:638` is rewired to `NoteService.create_note` (see Step 7). |
| `_artifact_downloads` (helper module) | Receives `ArtifactsRuntime` through `ArtifactsAPI` | `_artifact_downloads.download_mind_map` at `_artifact_downloads.py:339, :352` is rewired to `NoteBackedMindMapService.list_mind_maps` / `extract_content` (see Step 7). |
| `NotesAPI` | `RpcCaller` plus `NoteService` plus `NoteBackedMindMapService` | `NotesAPI` retains public `list_mind_maps` and `delete_mind_map` (at `_notes.py:215, :234`) which forward to the mind-map service. The `MindMapService(session)` fallback at `_notes.py:55-56` is replaced with a required collaborator. |
| `NoteBackedMindMapService` | `NoteService` | Current mind maps are note-row-backed; service exposes `list_mind_maps`, `extract_content`, and `delete_mind_map`. |
| `FakeSession` (`tests/_fixtures/fake_core.py:41-100`) | Capability-protocol shape | The class itself starts at line 41; the `defaults` dict mirroring the broad `Session` shape begins around line 91. Shrinks to match the new narrow contracts; updated alongside feature retyping. |
| `tests/unit/test_session_contracts.py:123` | Capability-protocol pins | Replaces the single broad `Session` protocol pin (`test_session_protocol_has_exactly_eight_members`) with per-capability pins. |

## Local Chat Runtime

`ChatAPI` should define its local runtime protocol in `_chat.py`, because these
capabilities are not shared today.

```python
class ChatRuntime(RpcCaller, LoopGuard, Protocol):
    async def transport_post(
        self,
        build_request: BuildRequest,
        parse_label: str,
        *,
        disable_internal_retries: bool = False,
    ) -> httpx.Response: ...

    async def next_reqid(self, step: int = 100000) -> int: ...
```

`next_reqid(...)` is currently used only by `ChatAPI.ask()` at `_chat.py:284`.
`transport_post(...)` is not called by `ChatAPI` directly; it is called by
`chat_aware_authed_post(...)` in `_chat_transport.py` (signature at
`_chat_transport.py:35-40`, `session.transport_post(...)` at line 66).

The helper's signature must migrate to `runtime: ChatRuntime` in the same
commit as `ChatAPI`, and the call site at `_chat.py:306`
(`chat_aware_authed_post(self._core, ...)`) must migrate to
`chat_aware_authed_post(self._runtime, ...)` in the same commit. Otherwise the
local runtime protocol does not actually slim the chat dependency surface.
Neither method should be globally promoted until another feature needs the same
capability.

`ChatAPI` should take one local runtime:

```python
class ChatAPI:
    def __init__(
        self,
        runtime: ChatRuntime,
        *,
        notebooks: NotebookSourceIdProvider,
        conversation_cache: ConversationCache | None = None,
    ) -> None:
        self._runtime = runtime
```

Remove the deprecated `core=` constructor alias.

## Local Artifacts Runtime

`DrainHookRegistration` should be removed from `_session_contracts.py` and kept
local to `_artifacts.py`.

```python
class DrainHookRegistration(Protocol):
    def register_drain_hook(
        self,
        name: str,
        hook: Callable[[], Awaitable[None]],
    ) -> None: ...


class ArtifactsRuntime(RpcCaller, AsyncWorkRuntime, DrainHookRegistration, Protocol):
    """Runtime capabilities required by the artifacts feature."""
```

This is intentionally local. Artifact polling is the only current behavior that
registers close-time feature cleanup. Deep Research currently exposes a
caller-driven `poll(...)` method and does not spawn shared poll tasks or require
close-time hook cleanup.

If Deep Research later adds artifact-style leader/follower polling with shared
background tasks, revisit whether drain-hook registration should become shared.

`ArtifactsAPI` should take one local runtime plus the two collaborator services
it needs for the mind-map paths:

```python
class ArtifactsAPI:
    def __init__(
        self,
        runtime: ArtifactsRuntime,
        *,
        notebooks: NotebookSourceIdProvider,
        mind_maps: NoteBackedMindMapService,
        note_service: NoteService,
        storage_path: Path | None = None,
    ) -> None:
        self._runtime = runtime
```

`mind_maps` covers `_artifact_downloads.download_mind_map`. `note_service`
covers `_artifact_generation.generate_mind_map`, which persists a generated
mind-map note row through `NoteService.create_note`. Both must be present
before the module-level `_mind_map` wrappers can be removed. The non-mind-map
`api._core.rpc_call(...)` sites in `_artifact_generation.py` continue to flow
through `ArtifactsRuntime` since the runtime extends `RpcCaller`.

`ArtifactPollingService` should still depend only on `AsyncWorkRuntime`:

```python
class ArtifactPollingService:
    def __init__(
        self,
        runtime: AsyncWorkRuntime,
        poll_registry: PollRegistry | None = None,
    ) -> None:
        self._runtime = runtime
```

## Sources And Upload

`SourcesAPI` should require an injected `SourceUploadPipeline`.

```python
class SourcesAPI:
    def __init__(
        self,
        rpc: RpcCaller,
        *,
        uploader: SourceUploadPipeline,
    ) -> None:
        self._rpc = rpc
        self._uploader = uploader
```

Remove the fallback that constructs `SourceUploadPipeline` by reading
`session.kernel`, `session.auth`, and the `getattr(session,
"record_upload_queue_wait", None)` callback at `_sources.py:88`. All three are
wired through `NotebookLMClient` explicitly.

### Upload pipeline runtime

`SourceUploadPipeline` should type its first constructor argument against a
feature-local protocol rather than the broad `Session`:

```python
class UploadRuntime(RpcCaller, OperationScopeProvider, Protocol):
    """Runtime capabilities required by source upload."""


class SourceUploadPipeline:
    def __init__(
        self,
        runtime: UploadRuntime,
        kernel: Kernel,
        auth: AuthMetadata,
        *,
        record_upload_queue_wait: Callable[..., None] | None = None,
        ...,
    ) -> None:
        self._runtime = runtime
```

The pipeline calls `self._session.operation_scope(...)` at
`_source_upload.py:281` and `self._session.rpc_call(...)` at
`_source_upload.py:372`. Typing the first arg as `UploadRuntime` slims the
boundary without losing those capabilities.

The existing **local** `RpcCaller` Protocol at `_source_upload.py:43-56` is
structurally different from the shared one: it is a **callable callback**
protocol (used as `register_file_source(rpc_call=...)` at
`_source_upload.py:345`), while the shared `_session_contracts.RpcCaller` is
an **object** protocol with a `.rpc_call(...)` method. They cannot substitute
for each other.

**Rename** the local protocol to `RpcCallback` in the same commit as the
`UploadRuntime` introduction. This avoids the name collision without breaking
the callback contract. Do **not** replace with a `Callable[..., Awaitable[Any]]`
alias — the structural Protocol form is intentional (keyword-only args, named
parameters, mypy-friendly errors at call sites).

The pipeline genuinely needs upload-specific auth routing, live cookies,
operation-scope drain accounting, and RPC registration/probing. Only
`NotebookLMClient` wires those internals; `SourcesAPI` does not see them.

## Note And Mind Map Service Split

The current `_mind_map.py` mixes note-row primitives, mind-map filtering, and
saved-from-chat note encoding. Three production consumers reach into it today:

- `NotesAPI` (notes CRUD + public mind-map list/delete)
- `_artifact_generation.generate_mind_map` at `_artifact_generation.py:638`
  (persists generated mind maps via module-level `_mind_map.create_note`)
- `_artifact_downloads.download_mind_map` at `_artifact_downloads.py:339,352`
  (reads mind-map rows via module-level `_mind_map.list_mind_maps` /
  `extract_content` through `_artifact_seams()`)

Any slim of `_mind_map.py` must rewire all three consumers in the same change.

### `_note_service.py`

Create `src/notebooklm/_note_service.py`.

```python
class NoteRowKind(Enum):
    NOTE = "note"
    SAVED_CHAT = "saved_chat"
    MIND_MAP = "mind_map"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class NoteService:
    async def fetch_note_rows(self, notebook_id: str) -> list[Any]: ...
    def classify_row(self, row: list[Any]) -> NoteRowKind: ...
    def extract_content(self, row: list[Any]) -> str | None: ...
    async def create_note(...): ...
    async def update_note(...): ...
    async def delete_note(...): ...
```

`NoteRowKind` stays private. It is an internal classification of rows returned by
the undocumented `GET_NOTES_AND_MIND_MAPS` RPC, not a public API type.

`SAVED_CHAT` is still a note. `NotesAPI.list(...)` should include both `NOTE`
and `SAVED_CHAT` rows. The distinction exists for backend classification and
tests, not for changing the public `Note` dataclass.

The classifier owns backend row interpretation:

- deleted rows: `["id", None, 2]`
- mind-map rows: content JSON with `"children"` or `"nodes"`
- saved-chat rows: note rows with saved-chat mode metadata when reliably present
- plain note rows: normal note content

This reflects observed wire behavior: NotebookLM returns notes and mind maps from
one mixed note-row collection.

`NoteService.create_note(...)` is the persistence path used by
`_artifact_generation.generate_mind_map` when it writes a generated mind-map
note row. `NoteService.delete_note(...)` is also the persistence path used by
`NoteBackedMindMapService.delete_mind_map`. `NoteService.extract_content(...)`
is reused by mind-map downloads through `NoteBackedMindMapService`.

### `_mind_map.py`

Keep `_mind_map.py` as the mind-map boundary, not the generic note service.

```python
class NoteBackedMindMapService:
    def __init__(self, notes: NoteService) -> None:
        self._notes = notes

    async def list_mind_maps(self, notebook_id: str) -> list[Any]: ...
    def extract_content(self, row: list[Any]) -> str | None: ...
    async def delete_mind_map(self, notebook_id: str, note_id: str) -> bool: ...
```

`list_mind_maps(...)` filters note rows down to mind-map rows.
`extract_content(...)` delegates to `NoteService.extract_content(...)` so the
download path does not need to know that mind maps share storage with notes.
`delete_mind_map(...)` delegates to `NoteService.delete_note(...)` and
**returns its `bool` result** so the v0.4.1 public contract
`NotesAPI.delete_mind_map(...) -> bool` (at `_notes.py:234`, backed by
`_mind_map.py:164` returning `True`) is preserved without making `NoteService`
aware of mind maps.

Only mind-map behavior belongs in `_mind_map.py`. If Google later moves mind
maps to a real artifact endpoint or explicit mind-map endpoint, replace this
adapter without polluting `NoteService`.

### Module-level wrapper removal order

`_mind_map.py` currently exports module-level `create_note(...)`,
`list_mind_maps(...)`, `extract_content(...)`, and a saved-chat helper
(`_mind_map.py:259-352`, `:485`, `:580-655`). These wrappers must remain until
all callers are migrated. Order within this refactor:

1. Add `NoteService` and `NoteBackedMindMapService` (in the same migration step,
   so Step 7 can rewire to both).
2. Rewire `_artifact_generation.generate_mind_map` to use
   `NoteService.create_note`.
3. Rewire `_artifact_downloads.download_mind_map` (and the `_artifact_seams()`
   indirection) to use `NoteBackedMindMapService.list_mind_maps` /
   `extract_content`.
4. Migrate saved-chat helper to `_chat_notes.py` (see next section).
5. Only then remove the module-level `_mind_map` wrappers.

## Saved Chat Answer As Note

`save_chat_answer_as_note` is chat-owned behavior, not generic note behavior.
It uses a special `CREATE_NOTE` request variant with chat citations and rich
source-passage anchors.

Target:

- `ChatAPI` owns the workflow.
- `_chat_notes.py` owns the wire encoder/helper.
- `NoteService` does not know about saved-from-chat encoding.
- `NotesAPI.create_from_chat(...)` remains temporarily as a deprecated
  forwarder.

```python
class ChatAPI:
    async def save_answer_as_note(
        self,
        notebook_id: str,
        ask_result: AskResult,
        *,
        title: str | None = None,
    ) -> Note:
        # Match v0.4.1 NotesAPI.create_from_chat semantics at _notes.py:164-180:
        # raise ValueError if references are empty; derive default title from
        # the answer text (NOT the question — AskResult has no `question` field).
        if not ask_result.references:
            raise ValueError(
                "save_answer_as_note requires AskResult.references to be "
                "non-empty; use notes.create() for plain-text notes."
            )
        resolved_title = (
            title
            if title is not None
            else f"Chat: {ask_result.answer[:50].strip().replace(chr(10), ' ')}"
        )
        ...
```

The keyword-only `*, title: str | None = None` and the title-derivation
expression match the v0.4.1 `NotesAPI.create_from_chat` shape exactly
(`_notes.py:125-130, :164-180`) so the deprecated forwarder is a pure
pass-through delegate. Title derivation lives in `ChatAPI.save_answer_as_note`
exclusively. Note: the v0.4.1 derivation uses `ask_result.answer`, not
`ask_result.question` — `AskResult` (`src/notebooklm/_types/chat.py:121-139`)
has no `question` field today.

```python
class NotesAPI:
    def __init__(
        self,
        rpc: RpcCaller,
        *,
        notes: NoteService,
        mind_maps: NoteBackedMindMapService,
        save_chat_answer: SaveChatAnswerCallback,  # required, no None default
    ) -> None:
        self._rpc = rpc
        self._notes = notes
        self._mind_maps = mind_maps
        self._save_chat_answer = save_chat_answer

    async def create_from_chat(
        self,
        notebook_id: str,
        ask_result: AskResult,
        *,
        title: str | None = None,  # preserves v0.4.1 keyword-only signature
    ) -> Note:
        warnings.warn(
            "NotesAPI.create_from_chat is deprecated; "
            "use ChatAPI.save_answer_as_note.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Forwarder is a pure delegate; title derivation and ValueError on
        # empty references live in ChatAPI.save_answer_as_note.
        return await self._save_chat_answer(
            notebook_id, ask_result, title=title
        )
```

`save_chat_answer` is **required** (no `None` default). The composition root
always wires `self.chat.save_answer_as_note`; tests that construct `NotesAPI`
directly must inject the callback explicitly. Use callable injection so
`NotesAPI` does not import `ChatAPI`.

## Constructor And Naming Rules

Use names that describe the capability slice:

- pure RPC features store `self._rpc`
- composite feature runtimes store `self._runtime`
- old `self._core` should be removed from touched feature modules

Pure RPC feature APIs may keep one positional constructor argument, but the
parameter should be named `rpc`:

```python
class ResearchAPI:
    def __init__(self, rpc: RpcCaller) -> None:
        self._rpc = rpc
```

For features with collaborators, keep extra dependencies keyword-only:

```python
SourcesAPI(rpc, *, uploader=source_uploader)
NotebooksAPI(rpc, *, sources_api=sources)
ChatAPI(runtime, *, notebooks=notebooks)
ArtifactsAPI(runtime, *, notebooks=notebooks, mind_maps=mind_maps, note_service=note_service)
NotesAPI(rpc, *, notes=note_service, mind_maps=mind_maps, save_chat_answer=...)
```

Remove compatibility aliases/fallbacks from every touched feature API:

- remove `core=` from `ChatAPI`
- remove `uploader=None` fallback from `SourcesAPI`
- remove `record_upload_queue_wait` `getattr` fallback from `SourcesAPI`
- remove `drain_hooks=None -> session` fallback from `ArtifactsAPI`
- remove `_mind_map.MindMapService(session)` fallback from `NotesAPI`
  (`_notes.py:55-56`) and `ArtifactsAPI` (`_artifacts.py:181-183`)

`NotebookLMClient._core` may remain as a temporary compatibility alias for
external/private callers and tests, but new internal wiring should use
`self._session`. The `_core.py` module shim (a `dir(_session)` re-export loop at
`_core.py:13-16`) is also left untouched: ~22 test files import private names
through `notebooklm._core`, and any `_session` rename cascades through the loop.

## Composition Root

`NotebookLMClient` remains the wiring root. The construction order must satisfy
two cross-feature dependencies:

1. `NotebooksAPI` needs `SourcesAPI` for source-id resolution.
2. `NotesAPI` needs `ChatAPI.save_answer_as_note` as a deprecated forwarder
   callback.

So `sources` → `notebooks` → `chat` → `notes` → `artifacts`, with the
remaining RPC-only features (`research`, `settings`, `sharing`) wired
anywhere after `notebooks`:

```python
self._session = Session(...)
self._core = self._session  # temporary compatibility alias

note_service = NoteService(self._session)
mind_maps = NoteBackedMindMapService(note_service)

source_uploader = SourceUploadPipeline(
    self._session,
    self._session.kernel,
    self._session.auth,
    record_upload_queue_wait=self._session.record_upload_queue_wait,
    ...
)

self.sources = SourcesAPI(self._session, uploader=source_uploader)
self.notebooks = NotebooksAPI(self._session, sources_api=self.sources)
self.chat = ChatAPI(self._session, notebooks=self.notebooks)
self.notes = NotesAPI(
    self._session,
    notes=note_service,
    mind_maps=mind_maps,
    save_chat_answer=self.chat.save_answer_as_note,
)
self.artifacts = ArtifactsAPI(
    self._session,
    notebooks=self.notebooks,
    mind_maps=mind_maps,
    note_service=note_service,
    storage_path=storage_path,
)
self.research = ResearchAPI(self._session)
self.settings = SettingsAPI(self._session)
self.sharing = SharingAPI(self._session)
```

`chat` is constructed before `notes` so that `self.chat.save_answer_as_note`
exists when `NotesAPI` is built. `artifacts` receives both `mind_maps` (for
download paths) and `note_service` (for the generated mind-map persistence
path) so that the module-level `_mind_map` wrappers can be removed.

## Documentation Updates

This refactor should update docs in the same change:

- `docs/architecture.md`: replace the broad `Session Protocol` section with the
  capability model.
- `docs/development.md`: change new-feature guidance from "type against
  `Session`" to "type against the narrowest shared capability or a local
  feature runtime."
- `docs/adr/0013-composable-session-capabilities.md`: new ADR ratifying the
  capability-composition model; ADR-010 is re-statused as `Superseded by
  ADR-013` in the same PR. (Both land before the 11-step migration begins;
  see ADR-013 §Status.)
- `docs/adr/0012-implementation-surface-convention.md`: amend the stale
  five-member-Session reference — performed in the ADR-013 ratification PR.
- `docs/rpc-reference.md` and `docs/python-api.md`: update note/mind-map and
  saved-chat note wording if public behavior or names change.

## Migration Plan

The plan is structured so each step keeps the build green: new protocols are
introduced additively, feature retyping is paired with test and `FakeSession`
updates **in the same commit**, fallback removals are paired with same-commit
direct-construction site updates, and the broad `Session` protocol is removed
last.

1. **Additive contracts.** Add new capability protocols (`RpcCaller`,
   `LoopGuard`, `OperationScopeProvider`, `AsyncWorkRuntime`) to
   `_session_contracts.py` alongside the existing broad `Session` protocol. Do
   not remove anything yet. Build stays green.
2. **Pure RPC features.** Retype `NotebooksAPI`, `ResearchAPI`, `SettingsAPI`,
   `SharingAPI` to `RpcCaller`. Rename `self._core` to `self._rpc`. Fix
   `NotebooksAPI._rpc_call` at `_notebooks.py:170-188` to accept the
   `operation_variant` kwarg required by `RpcCaller`. Update affected tests and
   the `FakeSession` fixture in the same commit.
3. **Chat runtime.** Add local `ChatRuntime` in `_chat.py`. Migrate
   `chat_aware_authed_post(...)` in `_chat_transport.py:35-40` to take
   `runtime: ChatRuntime` instead of `session: Session`, and migrate its call
   site at `_chat.py:306` to pass `self._runtime`. Remove `core=` alias from
   `ChatAPI`; rename internal storage to `self._runtime`. Update tests and
   `FakeSession` in the same commit.
4. **Artifacts runtime.** Add local `ArtifactsRuntime` and
   `DrainHookRegistration` in `_artifacts.py`. Remove the
   `drain_hooks=None -> session` fallback and the
   `_mind_map.MindMapService(session)` fallback at `_artifacts.py:181-183`,
   making the legacy `mind_map_service: MindMapService` parameter required
   (parameter name unchanged for now). In `NotebookLMClient`, instantiate
   `_mind_map.MindMapService(self._session)` and pass it explicitly as
   `mind_map_service=` to `ArtifactsAPI` — **this transitional wiring is
   replaced by `NoteBackedMindMapService` in Step 7**. Retype
   `ArtifactPollingService` to `AsyncWorkRuntime`. Update every direct
   `ArtifactsAPI(core)` test site and `FakeSession` in the same commit, so
   the fallback removal does not red-tree the build.
5. **Sources require uploader.** Define `UploadRuntime(RpcCaller,
   OperationScopeProvider, Protocol)` in `_source_upload.py` and retype
   `SourceUploadPipeline`'s first constructor arg from `Session` to
   `UploadRuntime`. **Rename** the existing local `RpcCaller` Protocol at
   `_source_upload.py:43-56` to `RpcCallback` (callable protocol; structurally
   distinct from the shared object protocol) in the same commit; update its
   call sites at `_source_upload.py:345` and any caller of
   `register_file_source(rpc_call=...)`. Require `SourceUploadPipeline` in
   `SourcesAPI`; remove the `session.kernel`, `session.auth`, and
   `getattr(session, "record_upload_queue_wait", None)` fallbacks at
   `_sources.py:88`. Wire all three through `NotebookLMClient` explicitly.
   Update all ~13 direct `SourcesAPI(core)` test sites in the same commit.
6. **NoteService and NoteBackedMindMapService.** Add `_note_service.py` with
   private `NoteRowKind` and `NoteService` (`create_note`, `update_note`,
   `delete_note`, `fetch_note_rows`, `classify_row`, `extract_content`). Add
   `NoteBackedMindMapService` to `_mind_map.py` (`list_mind_maps`,
   `extract_content`, `delete_mind_map`) using the existing module-level
   helpers internally. Both services exist alongside the old wrappers; nothing
   is removed yet.
7. **Rewire artifact generation/download + retype `ArtifactsAPI`.** Migrate
   `_artifact_generation.generate_mind_map` at `_artifact_generation.py:638`
   to use `NoteService.create_note`, and
   `_artifact_downloads.download_mind_map` at `_artifact_downloads.py:339, :352`
   (plus the `_artifact_seams()` indirection) to use
   `NoteBackedMindMapService.list_mind_maps` / `extract_content`. Rename
   `ArtifactsAPI`'s `mind_map_service: MindMapService` parameter to
   `mind_maps: NoteBackedMindMapService` and add the new keyword-only
   `note_service: NoteService` parameter. In `NotebookLMClient`, replace the
   transitional `MindMapService(self._session)` wiring (from Step 4) with
   `NoteBackedMindMapService(note_service)`, and pass both `mind_maps` and
   `note_service` to `ArtifactsAPI`. The non-mind-map
   `api._core.rpc_call(...)` paths at `_artifact_generation.py:515, :611,
   :661, :696` continue to flow through `ArtifactsRuntime` (no change needed).
   Update artifact tests that patch the `_mind_map` module-level seams to
   patch the new services instead, in the same commit. **NotesAPI is not
   retyped in this step**; that is Step 8.
8. **Saved-chat note + NotesAPI retype.** Move saved-from-chat encoding from
   `_mind_map.py:485, :580-655` to `_chat_notes.py`. Add
   `ChatAPI.save_answer_as_note(self, notebook_id, ask_result, *, title:
   str | None = None) -> Note`. Match v0.4.1
   `NotesAPI.create_from_chat` semantics at `_notes.py:125-130, :164-180`:
   raise `ValueError` when `ask_result.references` is empty, and derive
   `f"Chat: {ask_result.answer[:50].strip().replace(chr(10), ' ')}"` when
   `title is None`. The derivation uses `ask_result.answer`, **not**
   `ask_result.question` — `AskResult` has no `question` field today
   (`src/notebooklm/_types/chat.py:121-139`). Retype `NotesAPI` to
   `(rpc, *, notes: NoteService, mind_maps: NoteBackedMindMapService,
   save_chat_answer: SaveChatAnswerCallback)` — `save_chat_answer` is
   **required** (no `None` default). Forward
   `NotesAPI.list_mind_maps(self, notebook_id: str) -> builtins.list[Any]`
   and `NotesAPI.delete_mind_map(self, notebook_id, mind_map_id: str) -> bool`
   to `self._mind_maps`, preserving both signatures and return types from
   v0.4.1 (`_notes.py:215-232, :234-244`); the raw `list[Any]` row shape is
   the documented public contract — no typed `MindMap` class is introduced.
   Remove the `_mind_map.MindMapService(session)` fallback at
   `_notes.py:55-56` and rewire `_notes.py:174`
   (`_mind_map.save_chat_answer_as_note(self._core, ...)`) to
   `await self._save_chat_answer(notebook_id, ask_result, title=title)`.
   Convert `NotesAPI.create_from_chat(...)` to a deprecated callable-backed
   forwarder that **preserves the v0.4.1 signature exactly**, including the
   keyword-only marker: `create_from_chat(self, notebook_id, ask_result, *,
   title: str | None = None) -> Note`. The forwarder is a pure delegate
   (title derivation and `ValueError` raise live in
   `ChatAPI.save_answer_as_note`). In `NotebookLMClient`, wire
   `save_chat_answer=self.chat.save_answer_as_note` and pass
   `notes=note_service`, `mind_maps=mind_maps` to `NotesAPI`. Update direct
   `NotesAPI(core)` test sites in the same commit.
9. **Slim `_mind_map.py`.** Remove the module-level `create_note`,
   `list_mind_maps`, `extract_content`, and saved-chat wrappers now that all
   callers route through the new services. `NoteBackedMindMapService` remains.
   Update any remaining test patches.
10. **Final removal.** Delete the broad `Session` protocol from
    `_session_contracts.py` and the broad `FakeSession` defaults shape from
    `tests/_fixtures/fake_core.py:41-100`. Replace the broad protocol pin in
    `tests/unit/test_session_contracts.py:123`
    (`test_session_protocol_has_exactly_eight_members`) with per-capability
    pins. By this point no source-file import of `_session_contracts.Session`
    remains.
11. **Documentation.** Update architecture docs and ADR amendments.

`_core.py` (the module-level compatibility shim) and `NotebookLMClient._core`
(the attribute alias) are not touched at any step. The shim re-exports
`_session` via a `dir(_session)` loop, so any `_session` rename cascades to
~22 test imports; prefer adding new symbols over renaming existing ones during
this migration.

## Risks

- This is broad. Constructor changes will touch many tests; ~36 test files
  construct feature APIs directly and ~22 import private names through
  `notebooklm._core`.
- Removing compatibility fallbacks may expose hidden direct construction sites.
  Each fallback removal step (3, 4, 5, 8) must update direct-construction tests
  in the same commit.
- Note/mind-map split affects artifact generation, artifact download, and
  saved-chat note tests that patch old `_mind_map` module-level functions; all
  must migrate together with the wrapper removal (Step 7 before Step 9).
- `NoteRowKind.SAVED_CHAT` classification depends on reliably observed metadata.
  If the metadata is not present in list rows, classify those rows as `NOTE`.
- `NotebooksAPI._rpc_call` signature must accept `operation_variant` before
  retyping to `RpcCaller`, or Protocol conformance fails.
- The implementation must not widen `_session_contracts.py` again to avoid local
  constructor work.
- Two `RpcCaller` Protocols exist today (one shared in `_session_contracts.py`
  after Step 1, one local at `_source_upload.py:43-56`). The local one is a
  **callable callback** protocol (used as `register_file_source(rpc_call=...)`
  at `_source_upload.py:345`), structurally different from the shared object
  protocol. Step 5 must **rename** the local protocol to `RpcCallback` — not
  delete it — to avoid silent shadowing while preserving the callback
  contract.

## Non-Goals

- **Do not change the v0.4.1 public API surface** (see Public API Stability).
  Any change visible through `NotebookLMClient.<feature>.*` is out of scope
  unless explicitly listed as additive there.
- Do not remove concrete `_session.Session.auth` or `_session.Session.kernel` in
  this pass.
- Do not delete `NotebookLMClient._core` or the `_core.py` module shim.
- Do not redesign public note/artifact APIs. `NotesAPI.list_mind_maps` and
  `NotesAPI.delete_mind_map` are preserved as public surface; only their
  implementation moves to `NoteBackedMindMapService`.
- Do not introduce mixins.
- Do not make `NoteRowKind` public.
- Do not migrate Session-internal collaborators (`_auth/session.py:43-44`,
  `_session_lifecycle.py:243-244`, `_session_auth.py:197-240`). They consume
  the concrete `_session.Session`, not the feature-facing capability protocols.

## Review Checklist

- **Does every method, property, and attribute on `NotebookLMClient` at v0.4.1
  still work with the same signature?** (Run the v0.4.1 e2e suite against the
  refactored client; no diff in observable behavior except the new
  `client.chat.save_answer_as_note` and the `DeprecationWarning` from
  `client.notes.create_from_chat`.)
- Are constructor changes confined to underscore-prefixed (private) modules?
- Is `NotesAPI.create_from_chat(notebook_id, ask_result, *, title: str | None =
  None)` still callable with `title=None` (deprecated forwarder), and does
  it still produce the v0.4.1 derived default title (`f"Chat: ..."`)?
- Does `NotesAPI.delete_mind_map(...) -> bool` continue to return a `bool`
  (not `None`) after rewiring through `NoteBackedMindMapService`?
- Does `NotesAPI.list_mind_maps(...)` still return the same row shape?
- Is every promoted protocol shared by at least two consumers?
- Are single-feature capabilities local to the feature module?
- Does each feature constructor name the dependency by capability, not by
  implementation?
- Does `NotebookLMClient` remain the only place that wires concrete session
  internals such as `kernel` and `auth` into upload?
- Does `NoteService` own backend note-row classification without owning
  mind-map product behavior?
- Does `NoteBackedMindMapService` cover every public mind-map call
  (`list_mind_maps`, `extract_content`, `delete_mind_map`)?
- Does `ChatAPI` own saved-chat-note workflow without turning `NoteService` into
  a chat-aware service?
- Are compatibility removals intentional and covered by test updates in the
  same commit?
- Does each migration step keep the tree green, with tests and `FakeSession`
  updated in the same commit as the feature retyping?
- Is the local `RpcCaller` at `_source_upload.py:43-56` **renamed** to
  `RpcCallback` (callable protocol) — not deleted in favor of the shared
  object protocol — before the shared `RpcCaller` lands in any
  upload-touching commit?
- Does Step 4 explicitly wire `_mind_map.MindMapService(self._session)` in
  `NotebookLMClient` as the transitional `mind_map_service=` for
  `ArtifactsAPI`, with Step 7 later swapping it to
  `NoteBackedMindMapService`?

