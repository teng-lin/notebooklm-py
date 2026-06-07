# Source Labels — Proposed API Design

**Status:** Proposed (design only — not implemented)
**Last Updated:** 2026-06-07 (**source removal added**: live capture confirmed `le8sX`
removes via the third fieldmask slot `sources_remove`, so the design now includes
`labels.remove_sources()` + a `label remove` CLI command + a `remove_sources`
idempotency variant `IDEMPOTENT_SET_OP`. Also confirmed **one source per le8sX
call** — `add_sources`/`remove_sources` loop per id, and the prior multi-id add
builder shape was corrected to singular. See `rpc.md` "Confirmed (2026-06-07)".)
**Earlier (2026-06-07):** (AI-grouping primitive named `generate(scope="all"|"unlabeled")` — the UI's "Reorganize" verb — replacing the earlier `auto_label`; safe default `scope="unlabeled"`. RPCMethod names follow the enum convention — singular mutations `CREATE_LABEL` / `UPDATE_LABEL` / `DELETE_LABEL` (the multi-mode `CREATE_LABEL`, id `agX4Bc`, backs both `generate` and `create`), plural `LIST_LABELS`. Oracle/momus fix pass folded in: root re-exports of `Label`/`LabelError`/`LabelNotFoundError` from `notebooklm/__init__.py`; `delete()` idempotent-no-op contract and `rename()` emoji-preservation contract clarified; CLI confirm standardized on `--yes/-y`; `safe_index` wording softened for the `NOTEBOOKLM_STRICT_DECODE=0` opt-out.)
**Wire source of truth:** [`rpc.md`](./rpc.md) (reverse-engineered RPC capture)
**Convention sources:** `docs/conventions.md`, `docs/python-api.md`,
`docs/rpc-development.md`, the ADRs in `docs/adr/`, and the existing `SharingAPI`
(`_sharing.py`) and Note/MindMap split (`_note_service.py` + `_notes.py` +
`_mind_maps_api.py`).
**Review:** Reviewed 2026-06-06 by three models (Claude / Gemini / Codex) against
the ADRs. This revision folds in their must-fixes (idempotency registration,
strict drift parsing, return-contract on `return_object=False`, fresh options
builder, complete export/test checklist) and adopts the **separate-facade**
shape (no `kind`, no dual-membership object).

This proposes source-labeling support for `notebooklm-py`, matching existing
resource-API conventions, and designs forward-compat for **artifact labels**
*without* changing the source-label public surface.

---

## 1. Goals & principles

1. **Mirror existing resource APIs.** `client.labels` shaped like `client.sharing`
   — an async `LabelsAPI` taking a `RpcCaller`, returning dataclasses, with
   `get`/`get_or_none`, `build_*_params` helpers, and a domain exception family.
2. **Separate facades per entity domain — never "both together."** Following the
   repo's Note/MindMap precedent (one wire backend, thin per-domain facades),
   source-labels and artifact-labels are *distinct* public surfaces. A
   source-`Label` carries **only** `source_ids`; there is **no** `kind`
   discriminator and **no** object holding both source and artifact members
   (§10).
3. **Ship minimal now; extract later.** Build `LabelsAPI` directly over
   `RpcCaller` (no premature shared core). The `LabelService` backend is
   extracted only when a second consumer (artifact labels) is real — per
   ADR-0013/0014 ("no capability promoted on speculation") (§10).
4. **Centralize wire-shape knowledge & fail loud.** Positional payload/response
   knowledge lives in `_label/params.py` and `_row_adapters/labels.py`; the row
   adapter is **strict by default** — descent via `safe_index` raises on schema
   drift (ADR-0019/0011) rather than collapsing to sentinels. Under the ADR-0011
   `NOTEBOOKLM_STRICT_DECODE=0` opt-out a drifted `sources` slot degrades to an
   empty label — acceptable, and identical to every existing row adapter; **name
   and id drift still raise** regardless of the opt-out.
5. **Honor observed semantics.** Source assignment is **append**, labels may
   **overlap** sources, and source **removal** is supported via the third
   `le8sX` fieldmask slot (`sources_remove`) even though the web UI never sends
   it — confirmed empirically 2026-06-07 (see `rpc.md`). Both add and remove are
   **one source per call** (the server honours only the first id), so the API
   loops per id. The API reflects exactly what the wire was proven to do and
   refuses to guess anything still uncaptured.

---

## 2. Wire → API mapping (source labels)

| Wire RPC (rpc.md) | RPCMethod (proposed) | Public method(s) |
|---------------------|----------------------|------------------|
| `agX4Bc` (scope `[]`/`[0]`) | `CREATE_LABEL` | `labels.generate(nb, scope=...)` |
| `agX4Bc` (slot `[5]` manual) | `CREATE_LABEL` (same id) | `labels.create(nb, name, emoji="")` |
| `I3xc3c` | `LIST_LABELS` | `labels.list(nb)`, `labels.get(nb, id)`, `labels.get_or_none(nb, id)` |
| `le8sX` (name_emoji group) | `UPDATE_LABEL` | `labels.rename(...)`, `labels.set_emoji(...)`, `labels.update(...)` |
| `le8sX` (`sources_add` group) | `UPDATE_LABEL` (same id, variant `add_sources`) | `labels.add_sources(...)` |
| `le8sX` (`sources_remove` group) | `UPDATE_LABEL` (same id, variant `remove_sources`) | `labels.remove_sources(...)` |
| `GyzE7e` | `DELETE_LABEL` | `labels.delete(nb, ids)` |

---

## 3. RPCMethod enum additions

`src/notebooklm/rpc/types.py`, new "Label operations" section:

```python
# Label operations (source labeling — see rpc.md)
CREATE_LABEL = "agX4Bc"  # Auto-generate label groupings AND manual create
LIST_LABELS = "I3xc3c"         # List labels for a notebook
UPDATE_LABEL = "le8sX"         # Rename / set emoji / add sources (unified fieldmask)
DELETE_LABEL = "GyzE7e"       # Batch-delete labels by id
```

No changes to `encoder.py`/`decoder.py`/`overrides.py` (members discovered
generically; the `at` CSRF token is injected by `encoder.py` for every write).

---

## 4. Idempotency registration (ADR-0005 — REQUIRED, hard CI gate)

`docs/adr/0005`: *"every active `RPCMethod` is registered in
`IDEMPOTENCY_REGISTRY`."* `tests/unit/test_idempotency_registry.py` iterates
`for method in RPCMethod` and fails any member left `UNCLASSIFIED`. **The 4 new
methods MUST be registered** in `src/notebooklm/_idempotency_policy.py`.

> `allow_null=True` (used on the `[]`-echo writes below) is **decode tolerance**
> only — it tells the decoder a null/empty body is acceptable. It is **not**
> idempotency. Retry-safety is the registry classification here.

```python
# in src/notebooklm/_idempotency_policy.py registration body:
registry.register(
    RPCMethod.LIST_LABELS, IdempotencyPolicy.IDEMPOTENT_SET_OP,
    notes="Read-only label list; safe to retry.",
)
registry.register(
    # CONSERVATIVE BY DEFAULT (ADR-0005): already-absent delete behavior is
    # unverified (rpc.md open item). Until a committed-then-retried delete
    # is proven to return success/no-op, classify NO_RETRY (fail loud) rather
    # than advertising retry-safety. Downgrade to IDEMPOTENT_SET_OP once verified
    # (matches DELETE_SOURCE / DELETE_NOTEBOOK, whose idempotency is known).
    RPCMethod.DELETE_LABEL, IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes="Batch delete-by-id; already-absent retry behavior unverified.",
)
# UPDATE_LABEL: rename/emoji are set-state (idempotent); add_sources APPENDS
# (re-adding on retry may double-insert — dedup-on-retry unverified);
# remove_sources is naturally idempotent (removing an absent member is a
# confirmed no-op, rpc.md 2026-06-07), so it is retry-safe.
registry.register(
    RPCMethod.UPDATE_LABEL, IdempotencyPolicy.IDEMPOTENT_SET_OP,
    notes="Default variant: rename / set-emoji are set-state.",
)
registry.register(
    RPCMethod.UPDATE_LABEL, IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    variant="add_sources",
    notes="Membership append; dedup-on-retry unverified (rpc.md).",
)
registry.register(
    RPCMethod.UPDATE_LABEL, IdempotencyPolicy.IDEMPOTENT_SET_OP,
    variant="remove_sources",
    notes="Membership remove; no-op on an absent member (confirmed), so retry-safe.",
)
# CREATE_LABEL: manual-create has no client dedupe key; scope='all'
# regenerates every label with NEW ids (destructive). Neither is retry-safe.
registry.register(
    RPCMethod.CREATE_LABEL, IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes="Manual-create (no dedupe key) and scope='all' regenerate (new ids).",
)
```

The `add_sources` method (§7) passes `operation_variant="add_sources"` so the
executor selects the `NON_IDEMPOTENT_NO_RETRY` variant policy; `remove_sources`
(§7) passes `operation_variant="remove_sources"` for its `IDEMPOTENT_SET_OP`
policy.

> **Variant threading (required):** once a method has *any* explicit variant row,
> `IdempotencyRegistry` raises `IdempotencyVariantError` for an *unknown* non-None
> variant. `rename`, `set_emoji`, and `update` therefore pass
> `operation_variant=None` (resolving to the `IDEMPOTENT_SET_OP` default) — do
> **not** invent a `"rename"`/`"emoji"` variant string. Only `add_sources` and
> `remove_sources` pass registered variant strings. (Mirrors `CREATE_NOTE`, which
> registers `None` + named variants explicitly.)

---

## 5. Param builders

`src/notebooklm/_label/params.py` (new). Two differences from source RPCs: the
request-options wrapper is slot `[0]`, and `notebook_id` is in params (slot `[1]`)
*in addition to* `source_path` on the URL. Builders return **fresh** structures
per call (no shared mutable wrapper — cf. `_settings.build_get_user_settings_params`).

```python
from typing import Any


def _opts() -> list[Any]:
    """Fresh request-options wrapper (arg[0] of every label RPC).

    Mirrors the [1, None*8, [1]] context block in _settings.py; returned fresh
    so callers never alias a shared mutable list.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def build_generate_labels_params(
    notebook_id: str, *, scope: Literal["all", "unlabeled"] = "unlabeled"
) -> list[Any]:
    """CREATE_LABEL (agX4Bc) — AI re-grouping ("Reorganize"). scope slot[4]:
    "all" -> [] (wipe + regenerate, destructive); "unlabeled" -> [0] (incremental)."""
    return [_opts(), notebook_id, None, None, ([] if scope == "all" else [0])]


def build_create_label_params(notebook_id: str, name: str, emoji: str = "") -> list[Any]:
    """CREATE_LABEL (agX4Bc) — manual create. scope None; slot[5] = labels."""
    return [_opts(), notebook_id, None, None, None, [[name, emoji]]]


def build_list_labels_params(notebook_id: str) -> list[Any]:
    """LIST_LABELS (I3xc3c)."""
    return [_opts(), notebook_id]


def build_update_label_params(
    notebook_id: str,
    label_id: str,
    *,
    name: str | None = None,
    emoji: str | None = None,
    add_source_id: str | None = None,
    remove_source_id: str | None = None,
) -> list[Any]:
    """UPDATE_LABEL (le8sX). Fieldmask slot[3] = [[ name_emoji, add, remove ]]:
      * name_emoji = [name, emoji] — positional. A rename sends a length-1
        [name] (matches the captured rename). Whether a length-1 name_emoji
        PRESERVES an existing emoji vs clears it is UNVERIFIED (§15) — the
        capture only proves rename and emoji-set in isolation. Until confirmed,
        treat name-only update as "name set; emoji effect unknown".
      * add        = [[source_id]] at slot[3][0][1] — APPENDS one source.
      * remove     = [[source_id]] at slot[3][0][2] — un-assigns one source
        (confirmed 2026-06-07); the source is NOT deleted from the notebook.

    SINGULAR by design: the server honours only the FIRST id of each group per
    call (confirmed 2026-06-07), so this builder takes one add id and/or one
    remove id; the API loops one call per source (§7). A combined add+remove in
    one call dropped the add on the wire — the API never sends both together.
    """
    name_emoji: Any = None
    if name is not None or emoji is not None:
        name_emoji = [name] if emoji is None else [name, emoji]
    group: list[Any] = [name_emoji]
    if remove_source_id is not None:
        group.append([[add_source_id]] if add_source_id is not None else None)  # slot[1]
        group.append([[remove_source_id]])                                      # slot[2]
    elif add_source_id is not None:
        group.append([[add_source_id]])                                         # slot[1]
    return [_opts(), notebook_id, label_id, [group]]


def build_delete_labels_params(notebook_id: str, label_ids: list[str]) -> list[Any]:
    """DELETE_LABEL (GyzE7e) — batch, array of ids."""
    return [_opts(), notebook_id, list(label_ids)]
```

---

## 6. Model

`src/notebooklm/_types/labels.py` (new), re-exported from `notebooklm/types.py`.
**No `kind`, no `artifact_ids`** — a source-`Label` describes source membership
only (§10).

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Label:
    """A NotebookLM source label (a topic grouping of sources).

    Notebook-scoped. Membership is many-to-many: a source may belong to multiple
    labels, and a label owns a list of source IDs (the source carries no
    back-reference). See rpc.md for the wire model.
    """

    id: str
    name: str
    notebook_id: str | None = None
    emoji: str | None = None
    # Source UUIDs in this label. Empty for a freshly-created (still empty) label.
    source_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_api_response(
        cls,
        data: list[Any],
        *,
        notebook_id: str | None = None,
        method_id: str | None = None,
    ) -> Label:
        """Parse one label 4-tuple [name, sources, label_id, emoji]."""
        from .._row_adapters.labels import LabelRow

        row = LabelRow.from_label_tuple(data, method_id=method_id)
        return cls(
            id=row.id,
            name=row.name,
            notebook_id=notebook_id,
            emoji=row.emoji or None,
            source_ids=list(row.source_ids),
        )
```

Row adapter `src/notebooklm/_row_adapters/labels.py` (new) — **strict by default**
per ADR-0019/0011: descent uses `safe_index` (raises `UnknownRPCMethodError` on
positional drift, like the mind-map accessors), and type drift raises too. A
*legitimately* empty label (`sources` slot is `None`) is the only tolerated
"absence" — it is not drift. Under the ADR-0011 `NOTEBOOKLM_STRICT_DECODE=0`
opt-out a drifted `sources` slot degrades to an empty label — acceptable, and
identical to every existing row adapter; **name and id drift still raise** even
under the opt-out.

```python
from dataclasses import dataclass
from typing import Any

from ..rpc import safe_index
from ..exceptions import UnknownRPCMethodError

_SRC = "_row_adapters.labels"


@dataclass(frozen=True)
class LabelRow:
    """Typed positional view over a raw label tuple [name, sources, id, emoji]."""

    name: str
    source_ids: tuple[str, ...]
    id: str
    emoji: str

    @classmethod
    def from_label_tuple(cls, data: list[Any], *, method_id: str | None = None) -> "LabelRow":
        # Required positions — safe_index raises UnknownRPCMethodError on drift.
        name = safe_index(data, 0, method_id=method_id, source=_SRC)
        sources = safe_index(data, 1, method_id=method_id, source=_SRC)  # list OR None
        label_id = safe_index(data, 2, method_id=method_id, source=_SRC)
        emoji = safe_index(data, 3, method_id=method_id, source=_SRC)
        # Type drift fails loud — do NOT collapse to sentinels (ADR-0019/0011).
        if not isinstance(name, str) or not isinstance(label_id, str):
            raise UnknownRPCMethodError(
                method_id=method_id, source=_SRC,
                message="label tuple name/id not strings",
            )
        if sources is None:
            source_ids: tuple[str, ...] = ()  # legitimate empty label
        elif isinstance(sources, list):
            ids: list[str] = []
            for s in sources:
                # Each member must be [source_id]. A malformed member is drift —
                # RAISE, never silently skip (ADR-0019/0011).
                if not (isinstance(s, list) and s and isinstance(s[0], str)):
                    raise UnknownRPCMethodError(
                        method_id=method_id, source=_SRC,
                        message="malformed label member row",
                    )
                ids.append(s[0])
            source_ids = tuple(ids)
        else:
            raise UnknownRPCMethodError(
                method_id=method_id, source=_SRC,
                message="label sources slot is neither None nor list",
            )
        # Non-string emoji is drift — raise, do not coerce to "".
        if not isinstance(emoji, str):
            raise UnknownRPCMethodError(
                method_id=method_id, source=_SRC,
                message="label emoji slot is not a string",
            )
        return cls(name=name, source_ids=source_ids, id=label_id, emoji=emoji)
```

> `UnknownRPCMethodError.__init__(message="", *, method_id=None, path=None,
> source=None, found_ids=None, ...)` — all keyword args optional, so the calls
> above are valid (verified against `exceptions.py`). Intent: positional drift via
> `safe_index`, type drift via explicit raise — never silent `""`/`[]`.

---

## 7. LabelsAPI surface (source labels)

`src/notebooklm/_labels.py` (new). `RpcCaller` imported from `._runtime.contracts`.
The base is pure-RPC like `SharingAPI`, **but** because `sources()` and the
`list`-with-titles join expand membership into `Source` objects, the API also
needs the source lister — so the constructor mirrors `NotebooksAPI`
(`NotebooksAPI(executor, sources_api=self.sources)`), taking a narrow
`list_sources` callable (not the whole `SourcesAPI`, to keep the dependency
minimal and fakeable). **No `LabelService`, no `kind` param, no artifact
concepts** — ship minimal (§10).

```python
import logging
from collections.abc import Awaitable, Callable
from ._runtime.contracts import RpcCaller
from ._lookup import unwrap_or_raise
from .rpc import RPCMethod
from .exceptions import LabelNotFoundError
from .types import Label, Source
from ._label.params import (
    build_generate_labels_params, build_create_label_params, build_list_labels_params,
    build_update_label_params, build_delete_labels_params,
)

logger = logging.getLogger(__name__)

# Narrow capability: just `sources.list(notebook_id) -> list[Source]`.
ListSources = Callable[[str], Awaitable[list[Source]]]


class LabelsAPI:
    """Operations on NotebookLM source labels (client.labels).

    Usage:
        async with NotebookLMClient.from_storage() as client:
            labels = await client.labels.generate(nb)             # AI grouping
            mine = await client.labels.create(nb, "Papers", "📄")    # manual
            await client.labels.add_sources(nb, mine.id, [src_id])
            members = await client.labels.sources(nb, mine.id)       # group -> Sources
            await client.labels.delete(nb, [mine.id])
    """

    def __init__(self, rpc: RpcCaller, *, list_sources: ListSources):
        """list_sources is `client.sources.list` (wired in client.py after the
        SourcesAPI is constructed) — needed for the membership→Source join in
        sources() and list(with_titles=...). Same client/bound loop, so no
        loop-affinity concern (ADR-0004)."""
        self._rpc = rpc
        self._list_sources = list_sources
```

### Read

```python
async def list(self, notebook_id: str) -> list[Label]:
    """List all labels in a notebook (LIST_LABELS), with source membership."""

async def get(self, notebook_id: str, label_id: str) -> Label:
    """Get a label by id; raises LabelNotFoundError on miss (ADR-0019)."""
    return unwrap_or_raise(
        await self.get_or_none(notebook_id, label_id),
        LabelNotFoundError(label_id, method_id=RPCMethod.LIST_LABELS.value),
    )

async def get_or_none(self, notebook_id: str, label_id: str) -> Label | None:
    """Get a label by id, returning None when absent (sanctioned None-on-miss)."""

async def sources(self, notebook_id: str, label_id: str) -> list[Source]:
    """Expand a label to its Source objects — the group-as-collection accessor.

    Read-only convenience: one get_or_none(label) + one self._list_sources(nb),
    joined client-side. Two reads, not N+1. Raises LabelNotFoundError if the
    label is absent. Order follows the label's source_ids (membership order),
    not notebook order. A member id missing from the source list (concurrent
    deletion between the two reads) is skipped, not raised — it is a benign race,
    not schema drift. (See §12 — the primitive agents reach for; by-label
    *mutations* stay explicit composition, not a method here.)"""
```

`get`/`get_or_none`/`sources` resolve over `list` (the wire has no single-label
read), matching `sources.get` → `unwrap_or_raise(... get_or_none ...)`. Pass
`method_id` to the error for transport-level (`except RPCError`) diagnostics.

> **Response envelopes differ** (decode accordingly): `LIST_LABELS` (I3xc3c)
> returns `[[label, ...]]` (single-element outer list); `CREATE_LABEL`
> (agX4Bc) returns `[None, [label, ...]]`. See rpc.md.

### Generate (AI grouping) / create

`generate` is the UI's "Reorganize → All | Unlabeled" verb (CREATE_LABEL;
rpc.md). The first-ever run and `scope="unlabeled"` produce the same result
when no labels exist (all sources are unlabeled), so the **default is the safe
incremental scope**; the destructive full re-label is opt-in via `scope="all"`.

```python
async def generate(
    self, notebook_id: str, *, scope: Literal["all", "unlabeled"] = "unlabeled"
) -> list[Label]:
    """AI-group sources into topic labels — the UI's "Auto-label" (first run) /
    "Reorganize" (re-run) action, wire `CREATE_LABEL`. scope='unlabeled' (default,
    safe) labels only currently-unlabeled sources, preserving existing labels;
    scope='all' WIPES + regenerates EVERY label with new ids (destructive — the
    CLI gates it behind --yes/-y). Returns the full post-op label set (agX4Bc
    echoes it)."""

async def create(self, notebook_id: str, name: str, emoji: str = "") -> Label:
    """Create an empty, manually-named label (CREATE_LABEL slot[5]).

    Locate the new label by ID-diff, NOT by name (names may collide, §15):
      1. before_ids = {l.id for l in await self.list(notebook_id)}
      2. fire CREATE_LABEL create; the echo is the full label set
      3. return the single label whose id is not in before_ids
    Raises LabelError if zero or more than one new id appears.

    This before/after id-diff mirrors the `ADD_SOURCE_FILE` baseline precedent
    (`_source/upload.py` register_file_source: capture a baseline source-id set
    before create, then accept only the id NOT in the baseline). As there, a
    concurrent create racing between the snapshot and the echo (>1 new id) raises
    `LabelError` rather than guessing — the ambiguity is **intentionally** loud,
    not silently resolved."""
```

### Mutate (all UPDATE_LABEL) — `return_object=False` still raises on a missing label

Per ADR-0019 (*"mutate existing target missing → raise `*NotFoundError`;
no-payload success → `None`"*), the existence preflight runs in **both** modes —
exactly as `_sources.rename` does (it fetches and raises before honoring
`return_object=False`).

> **`rename` preserves the emoji (preflight-derived).** `rename` already runs a
> `get_or_none` preflight for not-found detection; it **reuses that fetched
> label's current emoji** and sends `[[[new_name, current_emoji]]]` (a length-2
> `name_emoji`), so the emoji is never clobbered regardless of whether a name-only
> `[[[new_name]]]` would clear it on the wire. The name-only wire behavior is
> uncaptured (§5/§15) — verify it and simplify `rename` back to `[[[new_name]]]`
> only once name-only is proven to preserve the emoji. `update(name=...)` follows
> the same preflight-derived-emoji rule unless an explicit `emoji=` is passed.

```python
async def rename(self, notebook_id, label_id, name, *, return_object=True) -> Label | None: ...
async def set_emoji(self, notebook_id, label_id, emoji, *, return_object=True) -> Label | None: ...
async def update(self, notebook_id, label_id, *, name=None, emoji=None, return_object=True) -> Label | None:
    """Set name and/or emoji (UPDATE_LABEL). Raises ValueError if BOTH name and
    emoji are None (no-op fieldmask) BEFORE issuing any RPC. When only `name` is
    given, the current emoji is carried over from the get_or_none preflight (see
    the rename note above)."""

async def add_sources(self, notebook_id, label_id, source_ids, *, return_object=True) -> Label | None:
    """Add source(s) to a label (UPDATE_LABEL, variant 'add_sources').
    APPEND semantics: existing members preserved; pass only IDs to add. Does NOT
    remove the sources from any other label (labels may overlap).

    Raises ValueError on an empty `source_ids` BEFORE issuing any RPC — an empty
    add is a no-op fieldmask and must fail loud, never round-trip to the wire.

    ONE le8sX call PER source: the server honours only the first id of the add
    group per call (confirmed 2026-06-07), so a single multi-id call would
    silently drop all but the first. The method therefore loops `len(source_ids)`
    writes, then a contract-load-bearing preflight re-fetch (`get_or_none`) that
    backs the ADR-0019 return/not-found contract (`le8sX` echoes `[]`, carrying no
    label to return, and the existence check must raise on a missing label even
    when `return_object=False`). The final fetch is **NOT removable** — unlike
    `sources.rename`'s 1-RPC fast path, the label wire gives no return payload."""
    if not source_ids:
        raise ValueError("add_sources requires at least one source id")
    logger.debug("Adding %d source(s) to label %s", len(source_ids), label_id)
    for sid in source_ids:                     # one call per source (wire honours first id only)
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_label_params(notebook_id, label_id, add_source_id=sid),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,                   # decode tolerance for the [] echo
            operation_variant="add_sources",   # → NON_IDEMPOTENT_NO_RETRY (§4)
        )
    # le8sX echoes [] — re-fetch to honor the return contract. The existence
    # check runs even when return_object=False (raises on a missing label).
    label = await self.get_or_none(notebook_id, label_id)
    if label is None:
        raise LabelNotFoundError(label_id, method_id=RPCMethod.UPDATE_LABEL.value)
    return label if return_object else None

async def remove_sources(self, notebook_id, label_id, source_ids, *, return_object=True) -> Label | None:
    """Un-assign source(s) from a label (UPDATE_LABEL, variant 'remove_sources').

    Removes via the third fieldmask slot (`sources_remove`); the source is NOT
    deleted from the notebook (un-assign only) and removal is label-scoped — a
    source belonging to other labels stays in them (overlap preserved). Removing
    a source that is not a member is a confirmed no-op (idempotent). Removing the
    last member leaves the label present but empty.

    Mirrors `add_sources`: ValueError on empty `source_ids` before any RPC; ONE
    le8sX call PER source (first-id-only wire); a trailing `get_or_none` preflight
    backing the ADR-0019 return/not-found contract."""
    if not source_ids:
        raise ValueError("remove_sources requires at least one source id")
    logger.debug("Removing %d source(s) from label %s", len(source_ids), label_id)
    for sid in source_ids:                     # one call per source (wire honours first id only)
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_label_params(notebook_id, label_id, remove_source_id=sid),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,                   # decode tolerance for the [] echo
            operation_variant="remove_sources",  # → IDEMPOTENT_SET_OP (§4)
        )
    label = await self.get_or_none(notebook_id, label_id)
    if label is None:
        raise LabelNotFoundError(label_id, method_id=RPCMethod.UPDATE_LABEL.value)
    return label if return_object else None
```

### Delete

```python
async def delete(self, notebook_id: str, label_ids: str | list[str]) -> None:
    """Delete one or more labels (DELETE_LABEL, batch). Accepts a single id or
    a list. Deleting a label does NOT delete its sources (they become unlabeled).

    API contract: an absent target is an **idempotent no-op returning None** —
    consistent with `sources.delete`/`notebooks.delete` and ADR-0019's
    idempotent-delete-returns-None. This is a SEPARATE axis from the
    transport-retry idempotency CLASS: the registry keeps `DELETE_LABEL` at
    `NON_IDEMPOTENT_NO_RETRY` (conservative — already-absent *retry* behavior is
    wire-unverified, §15), so the executor will not auto-retry, even though the
    public method tolerates a missing target. The two are independent: the API
    contract governs the return value on a known-absent target; the idempotency
    class governs whether the transport may replay the RPC after a failure.
    §15 keeps the open-item to verify-then-maybe-downgrade the retry class."""
```

---

## 8. Exceptions

`src/notebooklm/exceptions.py`, new "Domain: Labels" section. Also add
`LabelNotFoundError` to the `NotFoundError` umbrella doc list and both classes to
`__all__`.

```python
class LabelError(NotebookLMError):
    """Base for label operations."""


class LabelNotFoundError(NotFoundError, RPCError, LabelError):
    """Label not found in notebook."""

    def __init__(self, label_id: str, *, method_id: str | None = None,
                 raw_response: str | None = None):
        self.label_id = label_id
        super().__init__(f"Label not found: {label_id}",
                         method_id=method_id, raw_response=raw_response)
```

---

## 9. Client wiring & exports (ADR-0012/0017 — complete the promotion)

- `src/notebooklm/client.py` — after `self.sources` is constructed (the
  `list_sources` join needs it), next to `self.sharing = SharingAPI(...)`:
  ```python
  self.labels = LabelsAPI(internals.executor, list_sources=self.sources.list)
  ```
  Add `labels` to the client class docstring's Attributes list. (Mirrors
  `NotebooksAPI(internals.executor, sources_api=self.sources)`.)
- `src/notebooklm/cli/__init__.py` + `cli/grouped.py` — register the `label`
  group: export it from `cli/__init__.py` (imported by `notebooklm_cli.py`) and
  add it to `SectionedGroup.command_groups` so the no-orphan help gate
  (`test_grouped.py`) passes.
- `src/notebooklm/_labels.py` stays **private**. Feature API classes are **not**
  re-exported at the package root — `src/notebooklm/__init__.py` `__all__` lists
  the client, types, exceptions, and enums, **not** `SourcesAPI`/`SharingAPI`.
  So do **not** add `LabelsAPI` to root `__all__`; the public surface is
  `NotebookLMClient.labels` (an instance attribute), nothing more.
- `src/notebooklm/__init__.py` — re-export the **dataclass and its exceptions** at
  the package root, mirroring how `Source`/`Note` dataclasses and their
  `*NotFoundError` exceptions are root-re-exported: add `Label`, `LabelError`,
  `LabelNotFoundError` to **both** the import block and root `__all__` (so
  `from notebooklm import Label, LabelError, LabelNotFoundError` works).
  `LabelsAPI` is the sole exception — it stays private (see the bullet above). If
  the public-surface manifest's `_DOCUMENTED_PUBLIC_IMPORTS`
  (`test_public_surface_manifest.py`) is edited, it must include these three too.
- `src/notebooklm/types.py` — import & re-export `Label`; rewrite `Label.__module__`
  to `notebooklm.types` (per the existing public-type pattern); add to `__all__`.
- `src/notebooklm/exceptions.py` — add `LabelError`, `LabelNotFoundError` to
  `__all__` and the `NotFoundError` doc list.
- `scripts/audit_public_api_compat.py` — add `"labels"` to the hardcoded
  `CLIENT_NAMESPACE_ATTRIBUTES` tuple, else method-level public-API drift on the
  new namespace is not captured by the compat audit.
- `docs/python-api.md` — document the `client.labels` namespace (as a
  `NotebookLMClient` namespace; do **not** imply a root `LabelsAPI` export).
- `docs/stability.md` — record the new public surface and its stability tier.
- `docs/rpc-reference.md` — add the 4 RPCs to the master table + payload sections.

---

## 10. Forward-compatibility: labeling artifacts (Note/MindMap precedent)

The product will likely extend labels to **artifacts**. The first question is the
*model*; we bet on **separate label entities per kind (Approach A)** — see the
analysis at the end of this section. The public design commits to A **cleanly**,
via the repo's existing Note/MindMap pattern, and ships nothing speculative.

### The precedent (real, shipped)

```
NoteService(executor)                  # private backend — owns the wire RPC primitives
   ├── client.notes      = NotesAPI(...)        # facade: user-note semantics
   └── client.mind_maps  = MindMapsAPI(...)     # facade: mind-map semantics, same backend
```
Principle: **wire machinery in a service; domain semantics in thin facades.**
`MindMapsAPI` even dispatches across two RPC families behind one surface.

### How labels adopt it

```
LabelService(executor)                       # private backend — agX4Bc/I3xc3c/le8sX/GyzE7e
   ├── client.labels          = LabelsAPI(...)          # SOURCE-label facade (ships now)
   └── client.artifact_labels = ArtifactLabelsAPI(...)  # ARTIFACT-label facade (FUTURE)
```

Each facade is single-domain, so **no object ever carries both source and
artifact members** — the "both together" shape is structurally impossible. A
source-`Label` has only `source_ids`; a future artifact-`Label` would have only
`artifact_ids`.

### Phasing (ADR-0013 §1 / ADR-0014 — no promotion on speculation)

Today there is exactly **one** consumer (source labels), so we do **not** build a
shared core yet:

1. **Now:** `LabelsAPI` directly over `RpcCaller` (SharingAPI-style). No
   `LabelService`, no `kind`, no `client.artifact_labels`, no `LabelKind`.
2. **When the artifact-label wire is captured:** extract `LabelService` out of
   `_labels.py` and add `ArtifactLabelsAPI` over it. At that point there are
   **two real consumers** → the extraction is exactly the Note/MindMap pattern,
   ADR-legitimate, not speculative. Because `LabelsAPI` is a thin facade, **the
   public `client.labels` surface is unchanged by the extraction** (internals
   move, surface doesn't).

### Stability under either outcome

`client.labels` (source-only, `source_ids` only, no `kind`) is identical whether
Google ships A (separate entities) or B (one unified label viewed per panel) — a
source-label facade showing source members is valid in both. The A/B question
only affects the *future* `ArtifactLabelsAPI` internals (separate entities vs.
another view of shared entities), decided behind that facade when the wire is
known. Adding it is purely additive (new class + namespace) — no breaking change.

### Why A (the bet), briefly

Migration-free rollout (A is additive; B retrofits every existing label);
`agX4Bc` is source-content clustering ML that doesn't transfer to artifacts (so
the artifact facade has **no `generate`**); the panels are already separate
subsystems; and the wire is source-scoped with spare slots (cheapest A path:
reuse the RPCs with a domain discriminator). The case for B (normalized
"polymorphic tag") is real but migration-heavy and less Labs-like. Either way,
per above, the source-label public surface does not move.

---

## 11. Known semantics the API encodes

- **`allow_null=True` ≠ idempotency.** It is decode tolerance for the `[]` echo;
  retry-safety is the §4 registry classification.
- **Append, not replace.** `add_sources` sends only the new IDs; existing members
  survive (confirmed). No `set_sources` (full-replace) until the wire supports it.
- **One source per le8sX call.** Both add and remove honour only the first id of
  their group per call (confirmed 2026-06-07), so `add_sources`/`remove_sources`
  loop one call per id. A combined add+remove in one call dropped the add — the
  API never sends both groups together.
- **Overlap allowed.** A source can be in multiple labels; `add_sources` never
  removes from other labels, and `remove_sources` only touches the target label.
- **Source removal IS supported (confirmed 2026-06-07).** `remove_sources` writes
  the third `le8sX` fieldmask slot (`sources_remove`) — un-assigns the source
  without deleting it (idempotent no-op on a non-member; emptying a label leaves
  it present). The web UI has no control for it. This is distinct from the
  source-row "Remove source" UI action, which deletes the source from the
  *notebook* (`sources.delete`, RPC `tGMBJ`).
- **`scope="all"` wipes labels** (regenerates with new ids). Surface the risk in
  docstring + a CLI `--yes/-y` confirm gate.
- **`at` CSRF token** auto-injected by `encoder.py`; no API concern.

---

## 12. CLI & agent consumption

A label is a **persistent named selection of sources** — a group/collection
*on top of* source operations. The CLI is shaped around that: the `label` group
manages the entity; the high-value agent affordances are **discovering** groups,
**expanding** a group to its sources, and **selecting** sources by label.

> Console script is **`notebooklm`** (`pyproject.toml [project.scripts]`); examples
> use it. ADR-0008: Click commands are thin shells — `parse → build_plan →
> execute_plan(plan, facade) → render`. The label join + resolver logic lives in
> a service module `src/notebooklm/cli/services/label_listing.py`, not in
> `label_cmd.py`.

### `label` command group

`src/notebooklm/cli/label_cmd.py`, exported from `cli/__init__.py`, binned in
`SectionedGroup.command_groups` (`cli/grouped.py`), `--json` throughout.

```
notebooklm label list    -n <nb> [--json]                       # incl. members + titles (see below)
notebooklm label sources -n <nb> <id|name> [--json]             # expand group -> its sources (read)
notebooklm label generate   -n <nb> [--scope all|unlabeled] [--yes]  # confirm on "all" (destructive)
notebooklm label create  -n <nb> <name> [--emoji 📄]
notebooklm label rename  -n <nb> <id|name> <new_name>
notebooklm label emoji   -n <nb> <id|name> <emoji>
notebooklm label add     -n <nb> <id|name> <source_id>...       # add_sources (append; one call/source)
notebooklm label remove  -n <nb> <id|name> <source_id>...       # remove_sources (un-assign; NOT delete source)
notebooklm label delete  -n <nb> <id|name>... [--yes]           # deletes the LABEL only, not its sources
```

`label remove` un-assigns sources from the label via `client.labels.remove_sources()`;
it is the inverse of `label add` and is **distinct from `label delete`** (which
deletes the label entity). It does **not** delete the sources from the notebook, so
it needs no `--yes` gate (non-destructive — the sources survive). Its `<source_id>...`
go through `resolve_source_ids` like `label add`.

`label sources` (CLI) **delegates to `client.labels.sources()`** (§7) — the
membership→Source join is single-sourced in the API, not re-implemented in the
CLI. `label add`'s `<source_id>...` go through `resolve_source_ids`
(`cli/resolve.py`) for partial-prefix support, like every other source-id command.

### Selector on `source` reads (decision 1)

A label is a saved filter, so source *reads* accept `--label`:

```
notebooklm source list --label <id|name> [--json]               # sources in the group
```

`source list` has **no post-fetch filter hook today** (`prepare_list` only slices
by `limit`). Implementation: add a `label_filter` field to `SourceListPlan`,
resolve it to `source_ids` in `execute_source_list`
(`cli/services/source_listing.py`), and intersect before render — **not** in the
renderer. **Read-only** selector; reuses `client.labels.sources()` for the
expansion.

### Name-or-id resolution (decision 2)

`<id|name>` and `--label` accept a label **id** (or partial prefix) **or** a
**name**. This is a **new composite resolver** (`resolve_label_id()` in
`cli/services/label_listing.py`) over `client.labels.list()` — it is **not** a
mirror of `resolve_source_id`/`resolve_notebook_id`, which are **id/prefix-only**
(names appear only in their diagnostics). It combines two existing precedents:
- the id/prefix half = `resolve_partial_id_in_items` (`cli/resolve.py`);
- the exact-name half = `resolve_source_by_exact_title`
  (`cli/services/source_mutations.py`), which raises `AMBIGUOUS_TITLE` listing
  candidates.

Resolution order: **try id/prefix match first** — full-id passthrough is **disabled**
(`allow_full_id_passthrough=False`), so even a full-UUID-shaped input is matched against
actual label ids, never blindly accepted; this guards a label *name* that looks like a
UUID — **then exact name** (a UUID-shaped *name* is found by the name pass after the id
pass finds nothing). On
a name matching **>1** label → **error listing candidates** (id + emoji + source
count), never guess. The error routes through the ADR-0015 typed `--json`
envelope (like `source_cmd`'s `_handle_source_mutation_error`); plain mode exits
non-zero.

### No by-label mutation in v1 (decision 3)

By-label *mutations* (e.g. "delete every source in Papers") are **not** provided.
They are not server operations — they would be an N-call client fan-out with no
atomicity, a read-then-write race, and partial-failure ambiguity, and they
dangerously conflate "delete the label" (organizational, above) with "delete the
sources" (content destruction). Agents compose instead, explicitly:

```bash
# delete the sources in a group (each deletion explicit + individually confirmed)
notebooklm label sources Papers --json | jq -r '.sources[].id' \
  | while read sid; do notebooklm source delete "$sid" --yes; done
```

If real demand appears later, add it as a **guarded selector on `source`**
(`notebooklm source delete --label X --dry-run | --yes`), reusing the existing
`SourceCleanResult`/failure-list rendering (`source_cmd.py`) for the
partial-failure report — never as a `label` verb. Purely additive; deferred.

### Agent-consumption notes (JSON-first)

- `notebooklm label list --json` returns the standard list envelope
  `{"labels": [...], "count": N}` (items_key `"labels"`, per `prepare_list`), each
  label with `source_ids` **and resolved source titles** so an agent sees every
  group and its contents in **one caller call**.
- That title join is **one extra `sources.list()` fetch** (`LIST_LABELS` +
  `GET_NOTEBOOK`), **not N+1**: build a `{source_id: title}` map from a single
  `sources.list()` and look up — never per-member fetches. Idiomatic but
  **scale-sensitive** on large notebooks (a 500+-source notebook adds that
  payload to `label list`); acceptable absent a batch-get-by-id endpoint.
- `label sources` / `source list --label` give the agent the group-as-collection
  primitive (both via `client.labels.sources()`); per-source ops compose from there.
- All `--json` output uses the `{<items_key>: [...], "count": N}` envelope and
  stable ids; destructive ops (`generate --scope all`, `delete`) require `--yes`/confirm.

---

## 13. Implementation checklist

1. `rpc/types.py` — add 4 `RPCMethod` members (§3).
2. `_idempotency_policy.py` — register all 4 (incl. `add_sources` + `remove_sources` variants) (§4). **[CI gate]**
3. `_types/labels.py` — `Label` model (§6); re-export via `types.py` (+`__module__`, `__all__`).
4. `_label/params.py` — builders w/ `_opts()` (§5).
5. `_row_adapters/labels.py` — strict `LabelRow` via `safe_index` (§6).
6. `_labels.py` — `LabelsAPI(rpc, *, list_sources)`, incl. read-only `sources()`,
   `add_sources`/`remove_sources` (both loop one call per source) (§7).
7. `exceptions.py` — `LabelError`, `LabelNotFoundError` + `__all__` + `NotFoundError` doc (§8).
8. `client.py` — `self.labels = LabelsAPI(internals.executor, list_sources=self.sources.list)`
   (after `self.sources`) + docstring (§9). (`_labels.py`/`LabelsAPI` stay private;
   **not** in root `__all__`.)
8b. `__init__.py` — root-re-export `Label`, `LabelError`, `LabelNotFoundError` in
   the import block **and** `__all__` (mirrors Source/Note dataclasses + their
   exceptions); also add the 3 to `_DOCUMENTED_PUBLIC_IMPORTS` if that manifest
   list is edited (§9, A1). (`LabelsAPI` excepted — stays private.)
9. `scripts/audit_public_api_compat.py` — add `"labels"` to `CLIENT_NAMESPACE_ATTRIBUTES` (§9).
10. `cli/services/label_listing.py` — the join (members→titles) + composite
    `resolve_label_id()` (id/prefix OR exact-name, ambiguity error) (§12, ADR-0008).
11. `cli/label_cmd.py` (thin shell; `sources` via `client.labels.sources()`;
    `label add`/`label remove` ids via `resolve_source_ids`); export from
    `cli/__init__.py`; bin in `cli/grouped.py` `command_groups`. Add `label_filter`
    to `SourceListPlan` + intersect in `cli/services/source_listing.py` for
    `source list --label` (§12).
12. `docs/rpc-reference.md`, `docs/python-api.md`, `docs/stability.md` updates (§9).
13. `CLAUDE.md` — **HARD gate** (`tests/unit/test_claude_md_freshness.py`): every
    new src file must appear in **both** CLAUDE.md's file-table **and** the
    repository-structure tree — `_labels.py`, `_label/params.py`,
    `_types/labels.py`, `_row_adapters/labels.py`, `cli/label_cmd.py`,
    `cli/services/label_listing.py` (plus the `_label/__init__.py` package). The
    freshness test fails if any is missing from either place.
14. Tests (§14).

## 14. Testing

- **Idempotency registry (ADR-0005 gate):** the 4 methods are classified —
  `test_idempotency_registry.py` passes. Add **explicit** assertions that
  `(UPDATE_LABEL, "add_sources") == NON_IDEMPOTENT_NO_RETRY` **and**
  `(UPDATE_LABEL, "remove_sources") == IDEMPOTENT_SET_OP` — the default-entry test
  won't catch a missing/wrong variant because lookup falls back to the default.
- **Public-API contract + behavior (ADR-0019):** register `labels` in **both**
  `test_public_api_contract.py` (static: `get → Label`, `get_or_none →
  Label | None`, `delete → None`) and `test_public_api_behavior.py` (behavioral:
  mutations raise `LabelNotFoundError` on a missing label even with
  `return_object=False`). Also add `"labels"` to `audit_public_api_compat.py`'s
  `CLIENT_NAMESPACE_ATTRIBUTES` so method-level drift is audited.
- **Unit (builders):** exact payloads incl. fresh `_opts()`, scope flags,
  single-source add fieldmask `[[None, [[id]]]]`, single-source remove fieldmask
  `[[None, None, [[id]]]]`, `[[id]]` wrapping.
- **Unit (row adapter):** canary tests for tuple positions; `sources is None`
  empty-label case; **drift raises** (missing/typed-wrong slots), not sentinels;
  separate envelope tests for `agX4Bc` (`[None,[...]]`) vs `I3xc3c` (`[[...]]`).
- **Integration:** mock `RpcCaller`; assert each method → right `RPCMethod` +
  params + `source_path` (+ `operation_variant="add_sources"` /
  `"remove_sources"`); assert `add_sources`/`remove_sources` issue **one
  `rpc_call` per source id** (multi-id → N calls, not 1); `labels.sources()`
  joins membership with the source list.
- **CLI/agent (§12):** `resolve_label_id` errors on an ambiguous name (lists
  candidates) and id-vs-name precedence (incl. a UUID-shaped name); `label sources`
  and `source list --label` (via `SourceListPlan.label_filter`) return the group's
  sources; `label add`/`label remove` round-trip through `client.labels`; `label
  list --json` uses the `{"labels":[...],"count":N}` envelope with member ids +
  titles from a single `sources.list()` (no N+1); grouped-help registration
  (`test_grouped.py` — no orphan command).
- **E2E:** read-only `list` on a fixture notebook; mutations manual/gated per
  `docs/rpc-development.md`.

## 15. Open questions (carry from rpc.md)

- **Delete already-absent behavior** — only successful deletes (echo `[]`) are
  captured. Two distinct axes: (1) the **API contract** already treats a missing
  target as an idempotent no-op returning `None` (§7 `delete`, matching
  `sources.delete`/`notebooks.delete` + ADR-0019) — settled, no open item; (2)
  the **transport-retry idempotency class** stays `NON_IDEMPOTENT_NO_RETRY` until
  a committed-then-retried delete is proven to no-op on the wire, then downgrade
  to `IDEMPOTENT_SET_OP`. This open item is axis (2) only.
- **`add_sources` dedup-on-retry** — unverified; hence `NON_IDEMPOTENT_NO_RETRY`.
- **Name-only update emoji effect** — whether a length-1 `name_emoji` (`[name]`)
  preserves vs clears an existing emoji is uncaptured (§5); verify before
  documenting `update(name=...)` as emoji-preserving.
- **`create` echo** — confirm locating the new label in the returned full set
  (match on the id not present pre-call rather than by name).
- ~~**Source removal from a label**~~ — **RESOLVED 2026-06-07.** `le8sX` removes
  via the third fieldmask slot (`sources_remove`); `remove_sources` ships it (§7,
  §11). Residual: combined add+remove in one call dropped the add (the API never
  sends both groups together), and the one-source-per-call wire limit means both
  add and remove loop per id — both reflected in the design, not open risks.
- **Artifact-label wire shape** — capture when the feature ships (§10); drives
  the `LabelService` extraction + `ArtifactLabelsAPI`.
