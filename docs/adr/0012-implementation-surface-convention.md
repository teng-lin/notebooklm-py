# ADR-012: Implementation surface convention (underscore-prefix policy)

## Status

Accepted (Tier 13 PR 13.9a).

## Context

`notebooklm-py` has accumulated a deeply-seamed internal structure: at
the time of this ADR, `src/notebooklm/` contains 13 public-named modules
(`auth.py`, `client.py`, `config.py`, `exceptions.py`, `io.py`, `log.py`,
`migration.py`, `notebooklm_cli.py`, `paths.py`, `research.py`,
`types.py`, `urls.py`, `utils.py`) and roughly 50 underscore-prefixed
seam modules (`_session.py`, `_session_contracts.py`, `_core.py`,
`_core_*.py`, `_artifacts.py`, `_artifact_*.py`, `_chat.py`,
`_chat_*.py`, `_middleware_*.py`, the `_auth/` subpackage, etc.).
The seam modules carry the bulk of the implementation; the
public-named modules are mostly thin re-export facades or lifecycle
entry points.

The convention emerged organically through the Tier-7 / Tier-8 /
Tier-11 / Tier-12 / Tier-13 remediations. Each tier extracted one or
more concerns out of `_core.py` (or out of a feature module like
`_artifacts.py`) into a new `_<scope>_<concern>.py` seam, with a
re-export from the parent module preserved for one cycle and then
removed. The seams that remain in `_core_*.py` are the load-bearing
ones: `_core_constants.py` (knobs), `_core_helpers.py` (pure
utilities), `_core_transport.py` (HTTP), `_core_rpc.py` (RPC
dispatch), and so on. The same pattern applies inside
`_artifact_*.py` and `_chat_*.py`.

Downstream code, however, sees no documented rule that distinguishes
"this module is the public API" from "this module is an implementation
seam I should not import from." The `__init__.py` re-exports declare
the *intended* public surface, but the seam modules are still
directly importable — a `from`-import targeting an underscore-prefixed
seam (e.g. importing `Session` directly from the `_session` seam)
succeeds at runtime, and nothing in the package layout signals which
imports are safe across releases.

Three facts shape this ADR:

1. **The underscore-prefix convention is already universal in this
   codebase.** Every module that is not a stable, downstream-visible
   surface starts with `_`. The convention is implicit in the file
   tree; this ADR makes it explicit and load-bearing.
2. **Re-exports are the migration mechanism.** When a seam needs to
   become public, it is re-exported from a non-underscore-prefixed
   module (typically `__init__.py`, `auth.py`, `exceptions.py`, or
   `types.py`); the underscore-prefixed source module stays internal.
   ADR-003 (auth facade write-through) and the `_artifacts.py` →
   `__init__.py` export pattern both rely on this rule.
3. **The seam churn is high.** Tier-7 through Tier-13 moved
   ~40 distinct seams; that motion will continue. Pinning down which
   imports survive cross-release lets the seam authors refactor
   freely behind the underscore-prefixed boundary without worrying
   about silent downstream breakage.

Two implementation details that shaped this ADR:

1. **`__all__` is the secondary fence.** Per the `__all__` audit
   landing in PR 13.9b (the t13-9 split-off), every public-named
   module declares `__all__` listing exactly what it exports. The
   underscore-prefix convention is the *primary* fence (a module's
   filename signals stability); `__all__` is the secondary fence
   inside each public module.
2. **The `_auth/` subpackage exists.** Auth has a richer internal
   structure than other features (`_auth/paths.py`, `_auth/cookies.py`,
   `_auth/refresh.py`, etc.). Wrapping it in an underscore-prefixed
   subpackage rather than scattering `_auth_*.py` files at the top
   level keeps the surface tidy and signals "the entire subpackage is
   internal — use `auth.py` (the facade)."

## Decision

Every Python module in `src/notebooklm/` belongs to exactly one of
three categories, signalled by its filename:

### 1. Public surface (no underscore prefix)

```text
src/notebooklm/
├── __init__.py                  # re-export hub; declares the stable surface
├── client.py                    # NotebookLMClient + lifecycle helpers
├── auth.py                      # auth facade (flat re-exports from _auth/*)
├── types.py                     # public dataclasses (Notebook, Source, ...)
├── exceptions.py                # public exception hierarchy
├── config.py                    # process-level configuration helpers
├── paths.py                     # filesystem path resolution helpers
├── research.py                  # public research helpers
├── log.py                       # logging configuration helpers
├── io.py                        # public I/O helpers
├── migration.py                 # on-disk-format migration helpers
├── notebooklm_cli.py            # Click CLI entry-point assembler
├── urls.py                      # URL helpers
└── utils.py                     # public utility re-exports
```

Public modules are subject to stability guarantees per `docs/stability.md`.
Each public module declares `__all__` listing exactly the symbols it
exports. Adding or removing a name from a public module's `__all__` is
a `MINOR` (or `MAJOR` post-1.0) version bump; renaming/removing a
public module itself is a `MAJOR` bump.

#### Historical-internal subpackages with public-looking names

Two top-level *subpackages* — `notebooklm.rpc/` and `notebooklm.cli/` —
have no underscore prefix but **are not** part of the public surface and
are not covered by stability guarantees. They predate this convention
and are documented as internal in `docs/stability.md` (RPC internals are
internal except the documented `notebooklm.rpc.RPCMethod` import path;
the CLI implementation modules are internal — only the `notebooklm`
console-script entry point is stable). Renaming them to `_rpc/` and
`_cli/` would break documented import paths used by power users and
shell scripts, so the names are preserved and their status is asserted
in this ADR. New subpackages with the same status (public-looking name
but internal contents) MUST be added to this list before merging.

### 2. Implementation seams (single underscore prefix)

```text
src/notebooklm/
├── _session.py                  # Session concrete orchestrator
├── _core.py                     # legacy compatibility shim
├── _core_*.py                   # per-concern core seams (transport, RPC, drain, etc.)
├── _session_contracts.py        # Session/Kernel Protocols (Tier-13)
├── _kernel.py                   # Kernel concrete (Tier-13)
├── _middleware_*.py             # middleware-chain modules
├── _artifacts.py                # ArtifactsAPI implementation
├── _artifact_*.py               # per-concern artifact seams
├── _chat.py                     # ChatAPI implementation
├── _chat_*.py                   # per-concern chat seams
├── _notebooks.py                # NotebooksAPI implementation
├── _sources.py, _notes.py, ...  # other feature implementations
├── _env.py                      # env-var resolvers (NOTEBOOKLM_*)
├── _backoff.py, _atomic_io.py   # narrow utility seams
└── _auth/                       # auth subpackage (entire tree internal)
    ├── __init__.py
    ├── paths.py
    ├── cookies.py
    ├── refresh.py
    └── …
```

Seam modules are **not** subject to stability guarantees. Their public
surface, internal layout, and module location can all change between
any two releases. Downstream code that imports from a seam module
(for example pulling `Session` out of the `_session` seam, or
`is_strict_decode_enabled` out of the `_env` seam) is using an internal
API and accepts the cost of tracking those moves across releases. The
library does not promise import-path stability for any name reachable
only through an underscore-prefixed module.

### 3. Test-only helpers (double underscore prefix or under `tests/`)

Modules whose only consumers are tests live under `tests/` (the
canonical home for test fixtures) or, when they must be shipped with
the library for typing reasons, are prefixed with `__` and explicitly
documented as test-only. No production code in `src/notebooklm/` is
permitted to import from a test-only module.

### Promotion rule

When a name needs to become public (a new dataclass, a new exception,
a new helper), it is **added to a public-named module's `__all__`**
(typically `__init__.py` for the package surface, or `auth.py` for an
auth-related helper, or `exceptions.py` for a new exception class).
The underscore-prefixed seam that *implements* the name stays
internal; the public module simply re-exports it:

```python
# src/notebooklm/__init__.py
from ._chat import ChatAPI  # implementation seam → public re-export

__all__ = [..., "ChatAPI", ...]
```

`ChatAPI` is now public; `_chat.ChatAPI` remains the canonical source
and is free to refactor internally as long as the re-exported symbol
keeps its name and signature.

The reverse demotion (a public name moving to internal) follows the
deprecation policy in `docs/stability.md`: one release of
`DeprecationWarning` from the public module, then removal in the next
minor (0.x) or major (post-1.0) bump.

## Consequences

**Wanted:**

- Downstream code has one rule to apply: "if the module starts with
  `_`, do not import from it." Anyone reviewing a downstream
  integration can check the import list against the
  filename-prefix rule without needing to consult `__all__` or the
  stability doc for every name.
- Seam authors have full freedom to move, split, merge, or rename
  internal modules between releases as long as the public-named
  re-export modules continue to expose the same `__all__`.
- New ADRs and refactor plans can reference the rule by name ("the
  underscore-prefix convention from ADR-012") instead of restating
  it each time, shortening planning artifacts and reducing ambiguity
  across the Tier-13+ refactor arc.
- Static analysers (linters, IDEs) can be configured to warn on
  imports from underscore-prefixed modules outside the package
  itself, giving downstream consumers an automated guardrail.

**Unwanted:**

- The convention is not enforced by Python itself. An adversarial
  consumer can still reach into the `_core` seam directly and the
  import succeeds. The convention is policy-level, not
  language-level. The lint helper planned for PR 13.9b will warn on
  external-callsite imports from underscore-prefixed `notebooklm`
  submodules but cannot prevent them.
- Some seams that *look* public-grade are not. For example,
  `_env.is_strict_decode_enabled` (the topic of the sibling
  ADR-011 flip) is a private seam that downstream code might
  reasonably want to call to mirror the library's strict-decode
  policy. The convention says: re-export it through a public module
  (`config.py` or `__init__.py`) if that need is real, do not
  encourage direct imports from the `_env` seam. PR 13.9b's audit
  will enumerate which seam-level names have legitimate downstream
  demand and promote those re-exports explicitly.
- The Tier-13 `_session.py` / `_kernel.py` / `_session_contracts.py`
  module triad is still pinned by ADR-010 with underscore prefixes
  even though `Session` and `Kernel` are arguably the most-load-bearing
  contracts in the post-Tier-13 architecture. That is consistent with
  this ADR: the contracts are stable across the migration, but the
  module *layout* (the split into three sibling files) is allowed to
  re-organise behind the underscore-prefixed wall as the Tier-13 work
  settles.

## Alternatives considered

**Drop the prefix convention and rely solely on `__all__`.** Rejected.
`__all__` controls only `from foo import *` behaviour; named imports
that reach directly into an underscore-prefixed seam (e.g. pulling
`Session` out of the `_session` module by name) bypass it entirely.
Using `__all__` as the sole stability fence would force consumers to
consult the docs for every name to learn whether it is stable, and
reviewers would have no filename-level signal during code review. The
prefix-plus-`__all__` combination is strictly more readable.

**Make seam modules truly private with package-import tricks (e.g.
`importlib` indirection, vendored namespace).** Rejected. Python's
import system does not have a real "module-private" concept; emulating
it via `importlib` indirection or vendored namespaces would add
runtime cost (loader hooks) and obscure stack traces (the
auto-generated indirection module shows up in tracebacks). The cost
exceeds the benefit for a single-process library.

**Put all seams under a single `notebooklm._internal` subpackage.**
Rejected. The seams are already organised by domain
(`_core_*`, `_artifact_*`, `_chat_*`, `_middleware_*`); collapsing
them under a single `_internal/` subpackage would either lose the
domain grouping (one flat `_internal/` directory with 50 modules) or
duplicate it (`_internal/core/`, `_internal/artifacts/`, ...) for no
gain over the existing flat structure. The single-underscore prefix
already accomplishes the "this is internal" signal without restructuring.

**Promote `_env.is_strict_decode_enabled` to a public name in this PR
(since ADR-011 lands the same release).** Rejected. ADR-011 pins the
*behavior* (strict by default) and the *env-var contract*
(`NOTEBOOKLM_STRICT_DECODE`). Downstream code controls behaviour by
setting the env var, not by importing the helper. Exposing the
helper as a public name would commit the library to a function-call
API surface that has no current downstream demand and would couple
two unrelated changes in the same PR. PR 13.9b's `__all__` audit is
the right place to revisit which seam-level names have real downstream
demand and merit promotion.

**Tie this ADR to ADR-010 (Session/Kernel split).** Rejected. ADR-010
pins a specific contract shape (the five-member Session Protocol, the
three-member Kernel Protocol). This ADR pins a *naming* convention
that applies across the entire `src/notebooklm/` tree. The two are
complementary: ADR-010 says *what* the load-bearing contracts are;
this ADR says *where* their implementation lives and how downstream
code should reference them.
