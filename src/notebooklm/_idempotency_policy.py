from __future__ import annotations

from ._idempotency import IdempotencyPolicy, IdempotencyRegistry
from .rpc.types import RPCMethod

__all__ = ["register_default_policies"]


def register_default_policies(registry: IdempotencyRegistry) -> None:
    """Register every production idempotency classification on ``registry``.

    This is the declarative classification data extracted from
    ``_idempotency.py`` (issue #1331). It is applied to the module-level
    ``IDEMPOTENCY_REGISTRY`` singleton at ``_idempotency`` import time via a
    bottom-of-module call. The two-pass shape is load-bearing: some
    ``register`` calls run *before* :meth:`IdempotencyRegistry._seed_defaults`
    (so the seeder skips them), the seeder then fills ``UNCLASSIFIED`` for
    every remaining method, and the rest register *after* the seed (replacing
    the placeholders). See ADR-0005 for the taxonomy rationale.
    """
    _START_RESEARCH_NOT_IDEMPOTENT_NOTE = (
        "research start: no client-token slot in params and ResearchAPI.poll "
        "keyed by (notebook_id, query) is ambiguous when peer tasks exist with "
        "the same query — surface the first failure and let the caller poll to "
        "decide whether the write landed"
    )
    _IMPORT_RESEARCH_NOT_IDEMPOTENT_NOTE = (
        "research import: no client-token slot in params; source rows are not "
        "granular per-task on the wire so a post-commit-lost SourcesAPI.list "
        "probe cannot bind URL-matched rows to this specific import batch "
        "(collides with prior workflows that imported the same URLs) — surface "
        "the failure and let the caller list-and-disambiguate"
    )
    _CREATE_NOTE_NOT_IDEMPOTENT_NOTE = (
        "CREATE_NOTE has no client-token slot and no client-visible note_id on "
        "commit-lost; title-based probes break under server-side smart-title "
        "generation (saved_from_chat variant). Caller must list notes and "
        "disambiguate on failure"
    )

    registry.register(
        RPCMethod.START_FAST_RESEARCH,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_START_RESEARCH_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.START_DEEP_RESEARCH,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_START_RESEARCH_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.IMPORT_RESEARCH,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_IMPORT_RESEARCH_NOT_IDEMPOTENT_NOTE,
    )
    # CANCEL_RESEARCH drives an in-flight run to its terminal (FAILED) state. The
    # server returns [] unconditionally and never validates the run id, so a
    # blind transport retry is safe: re-cancelling a now-terminal (or unknown)
    # run is a no-op with the same observable result. Retry-safe set-op
    # semantics, like DELETE_SOURCE / DELETE_ARTIFACT.
    registry.register(
        RPCMethod.CANCEL_RESEARCH,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes=(
            "research run cancel is idempotent — final-state set-op; server "
            "returns [] unconditionally (no id validation), so a retry is a no-op"
        ),
    )

    # CREATE_NOTE has two operation variants on the wire:
    #   * ``"plain"`` — 5-element params from ``NoteService.create_note``
    #     (default for ``notes.create()`` and mind-map row creation). The
    #     ``(CREATE_NOTE, None)`` default mirrors the same policy so callers
    #     that omit ``operation_variant`` still get NON_IDEMPOTENT_NO_RETRY.
    #   * ``"saved_from_chat"`` — 7-element params from
    #     ``_chat.notes.save_chat_answer_as_note`` (issue #660). Used by
    #     ``ChatAPI.save_answer_as_note``.
    # Both variants share the policy; explicit registration documents the
    # two distinct param shapes for future-classification work.
    registry.register(
        RPCMethod.CREATE_NOTE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.CREATE_NOTE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="plain",
        notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.CREATE_NOTE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="saved_from_chat",
        notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
    )

    # Default-fill every remaining method with an UNCLASSIFIED placeholder. The
    # explicit registrations below must replace every placeholder before tests pass.
    # Methods classified above are skipped by the absence check inside
    # ``_seed_defaults``.
    registry._seed_defaults()

    # ---------------------------------------------------------------------------
    # Active classifications — artifact and generation create patterns
    # ---------------------------------------------------------------------------
    #
    # CREATE_ARTIFACT — mutating create. Params are nested positional lists
    # shaped like ``[client_options, notebook_id, [None, None, type_code,
    # source_ids_triple, ..., config]]`` for every artifact variant (audio,
    # video, report, quiz, etc.; see the ``generate_*`` methods and the
    # ``_artifact.payloads.build_*`` helpers). Every position is structural —
    # there is no caller-supplied client-token slot. The server allocates the
    # artifact_id in the response
    # (``ArtifactsAPI._parse_generation_result`` reads ``result[0][0]`` — see
    # ``_artifacts.py``), so a token-dedupe strategy is impossible.
    #
    # PROBE_THEN_CREATE forces ``effective_disable_internal_retries=True``,
    # which suppresses the retry middleware inside
    # ``RuntimeTransport.perform_authed_post``. Without
    # this, a 5xx between server-side commit and client-side response would
    # trigger a naive re-POST and duplicate the artifact (the original
    # audit finding). Callers can layer a list-based probe + retry on top of
    # this foundation via ``idempotent_create`` in a follow-up; for B-generation
    # the classification alone removes the duplicate-write risk.
    registry.register(
        RPCMethod.CREATE_ARTIFACT,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        notes=(
            "P0-3: mutating create with no caller-supplied client-token slot. "
            "Server allocates artifact_id in the response. PROBE_THEN_CREATE "
            "forces the inner retry loop off to prevent duplicate-write on 5xx; "
            "a list-based probe wrapper can be layered via idempotent_create "
            "in a follow-up."
        ),
    )

    # GENERATE_MIND_MAP (live method ``ActOnSources``, a generic source-action
    # op we drive to generate a mind map) — generation RPC with no client-token
    # slot.
    # Params are ``[source_ids_nested, None, None, None, None,
    # ["interactive_mindmap", [["[CONTEXT]", instructions]], language], None,
    # [2, None, [1]]]`` (see ``ArtifactsAPI.generate_mind_map`` in
    # ``_artifacts.py`` and ``_artifact.payloads.build_mind_map_params``).
    # Every slot is structural (sources, content config, language, mode
    # triple). The response carries the mind-map JSON directly
    # (``generate_mind_map`` reads ``result[0][0]``) — there is no task_id to
    # probe with after the fact, so token-dedupe is impossible here too.
    #
    # Note: ``GENERATE_MIND_MAP`` itself does NOT persist the note server-side
    # (see ``tests/integration/test_mind_map_chain_vcr.py`` header). The actual
    # persistence is the subsequent ``CREATE_NOTE`` + ``UPDATE_NOTE`` chain in
    # ``NoteService.create_note``. PROBE_THEN_CREATE here suppresses the inner retry loop on
    # the *generation* RPC for two reasons: (a) a blind re-POST wastes the
    # expensive LLM inference, and (b) LLM nondeterminism means a retried
    # generation may return a *different* mind-map JSON, which would
    # silently mismatch what the client saw on the first commit before the
    # response was lost. The persisted-write side is classified too:
    # ``CREATE_NOTE`` is ``NON_IDEMPOTENT_NO_RETRY`` for both note-create
    # variants, while ``UPDATE_NOTE`` is an idempotent set op.
    registry.register(
        RPCMethod.GENERATE_MIND_MAP,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        notes=(
            "P0-3: generation RPC with no caller-supplied client-token slot. "
            "Response carries the mind-map JSON directly. PROBE_THEN_CREATE "
            "forces the inner retry loop off so a 5xx after server-side "
            "generation does not trigger a fresh LLM inference whose result "
            "may diverge from the first (lost) response. The persisted-note "
            "side of the mind-map chain is classified separately: CREATE_NOTE "
            "is NON_IDEMPOTENT_NO_RETRY and UPDATE_NOTE is an idempotent set op."
        ),
    )

    # ----------------------------------------------------------------------------
    # Active classifications — side effects and notebooks
    # ----------------------------------------------------------------------------
    #
    # These entries replace the UNCLASSIFIED placeholders for mutating RPCs whose
    # side-effect semantics are well-understood and stable. The full
    # audit decision matrix lives in ADR-0005
    # (``docs/adr/0005-idempotency-taxonomy.md``); the short version follows.
    #
    # CREATE_NOTEBOOK
    #   Mutating create with an executable wrapper in ``NotebooksAPI.create``:
    #   the caller captures a title/baseline probe before issuing the RPC and
    #   retries only after probing for a committed notebook. Classification:
    #   ``PROBE_THEN_CREATE`` so raw ``rpc_call(CREATE_NOTEBOOK, ...)`` disables
    #   blind transport retries too.
    #
    # DELETE_NOTEBOOK / DELETE_SOURCE / DELETE_ARTIFACT
    #   Server-side delete is idempotent: replaying the request after a 5xx /
    #   network failure yields the same final state (the resource is gone).
    #   Classification: ``IDEMPOTENT_SET_OP``. The transport retry loop keeps
    #   running unchanged — today's behavior is preserved, the registry simply
    #   documents *why* it is safe.
    #
    # REFRESH_SOURCE
    #   Refresh kicks off a server-side fetch job. A duplicate refresh job is
    #   harmless (extra bandwidth, same eventual content) but observable, so
    #   the caller has accepted at-least-once semantics. Classification:
    #   ``AT_LEAST_ONCE_ACCEPTED``. The transport may retry; the registry
    #   emits a rate-limited WARN so operators can see the trade-off when it
    #   actually fires.
    #
    # SHARE_NOTEBOOK
    #   Mutates the shared-users / public-access ACL. A blind retry after a
    #   network blip can re-send invitation emails (with ``notify=True``) or
    #   flip access between RESTRICTED / ANYONE-WITH-LINK twice. The codebase
    #   does expose a server-side probe RPC (``GET_SHARE_STATUS``) that can
    #   list the current ACL, so the *correct* policy is ``PROBE_THEN_CREATE``
    #   — the transport must NOT retry blindly, and a future wrapper can
    #   ``get_status()`` to decide whether the prior call landed before
    #   re-issuing. Today only the classification is in place (which suppresses
    #   the blind retry); the caller-side probe-then-create wrapper is a
    #   follow-up.
    registry.register(
        RPCMethod.CREATE_NOTEBOOK,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        notes=(
            "notebook create has an executable title/baseline probe wrapper in "
            "NotebooksAPI.create; raw rpc_call paths must also suppress blind "
            "transport retries to avoid duplicate notebooks on commit-lost errors"
        ),
    )
    registry.register(
        RPCMethod.DELETE_NOTEBOOK,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes="server-side delete is idempotent (set-op semantics)",
    )
    registry.register(
        RPCMethod.DELETE_SOURCE,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes="server-side delete is idempotent (set-op semantics)",
    )
    registry.register(
        RPCMethod.DELETE_ARTIFACT,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes="server-side delete is idempotent (set-op semantics)",
    )
    registry.register(
        RPCMethod.REFRESH_SOURCE,
        IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED,
        notes="duplicate refresh jobs are acceptable cost (extra fetch, same content)",
    )
    registry.register(
        RPCMethod.SHARE_NOTEBOOK,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        notes=(
            "mutates ACL; blind retry can re-send invite emails or double-flip access. "
            "GET_SHARE_STATUS exposes the server-side ACL for a future probe-then-create "
            "wrapper; today's classification suppresses the inner retry loop."
        ),
    )

    # ----------------------------------------------------------------------------
    # Active classifications — ADD_SOURCE + ADD_SOURCE_FILE
    # ----------------------------------------------------------------------------
    #
    # ADD_SOURCE is variant-shaped: the call site distinguishes ``"url"`` (web /
    # YouTube), ``"drive"`` (Google Drive document), and ``"text"`` (pasted
    # content). Each variant has a different retry-safety profile because the
    # server-side dedupe key differs:
    #
    # * ``"url"`` — probe by ``source.url == url`` on a notebook list. The probe
    #   is a single GET_NOTEBOOK; the wrapper retries the create once if the
    #   probe finds nothing. PROBE_THEN_CREATE.
    # * ``"drive"`` — probe by ``file_id in source.url`` (Drive URLs embed the
    #   file_id). Same wrapper as ``"url"``. PROBE_THEN_CREATE.
    # * ``"text"`` — no reliable dedupe key (titles non-unique, body not
    #   exposed in the source list). NON_IDEMPOTENT_NO_RETRY: force-disable the
    #   inner transport retries and let the first failure surface so the caller
    #   can decide. See the ``add_text`` rationale in
    #   ``tests/integration/concurrency/test_idempotency_create.py:17-19``.
    #
    # ADD_SOURCE_FILE is single-shape: it registers a file source by name.
    # Filenames are NOT identity-bearing (two uploads of ``report.pdf`` are
    # legitimately two distinct sources), so the per-API wrapper captures a
    # baseline of source IDs *before* the create attempt and filters probe
    # matches to "new since the create started" sources only. Ambiguous
    # matches (>1 new source with the same filename) raise rather than guess.
    # PROBE_THEN_CREATE.
    #
    # These entries force-disable blind transport retries via
    # ``resolve_effective_disable_internal_retries``. The per-API call sites in
    # ``_source/add.py`` / ``_source/upload.py`` own the executable probe loop for
    # the URL, Drive, and file variants.

    _RAW_ADD_SOURCE_NOT_IDEMPOTENT_NOTE = (
        "raw ADD_SOURCE without an operation_variant has no proven dedupe/probe "
        "key. Public call sites must pass 'url', 'drive', or 'text'; direct "
        "rpc_call users get first-failure surfacing rather than blind retry"
    )

    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_RAW_ADD_SOURCE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        variant="url",
        notes="probe by source.url == url on notebook list (web + YouTube)",
    )
    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        variant="drive",
        notes="probe by /d/<file_id> URL segment marker on notebook list",
    )
    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="text",
        notes="no reliable dedupe key — titles non-unique, body not exposed",
    )
    registry.register(
        RPCMethod.ADD_SOURCE_FILE,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        notes=(
            "baseline-diff probe by source.title == filename — filenames are not "
            "identity-bearing, so the wrapper captures source-id baseline before "
            "the create and filters probe matches to new sources only"
        ),
    )

    # ----------------------------------------------------------------------------
    # Complete coverage — read-only / idempotent set-state RPCs
    # ----------------------------------------------------------------------------
    #
    # ``IDEMPOTENT_SET_OP`` is the retry-safe bucket for operations where replay
    # cannot create an additional server resource. This includes side-effect-free
    # reads and "set this state to X" mutations; both preserve the public retry
    # default because transport retries remain enabled.

    _IDEMPOTENT_READ_OR_SET_OP_NOTES: dict[RPCMethod, str] = {
        # Live method ListRecentlyViewedProjects: a read-only recents list.
        RPCMethod.LIST_NOTEBOOKS: "read-only recents list; replay does not mutate notebook state",
        RPCMethod.GET_NOTEBOOK: "read-only notebook fetch; replay does not mutate notebook state",
        # Live method MutateProject (generic notebook mutator; covers title plus
        # chat-config and view-level via different params).
        RPCMethod.RENAME_NOTEBOOK: (
            "set notebook title/settings to caller-supplied values; replay leaves the same state"
        ),
        RPCMethod.GET_SOURCE: "read-only source content fetch; replay does not mutate source state",
        RPCMethod.CHECK_SOURCE_FRESHNESS: (
            "read-only freshness check; replay does not start a refresh job"
        ),
        RPCMethod.UPDATE_SOURCE: (
            "set source metadata/title to caller-supplied values; replay leaves the same state"
        ),
        # Live method GenerateNotebookGuide: generates the notebook guide
        # (summary + suggested questions); response-only, persists nothing.
        RPCMethod.SUMMARIZE: (
            "response-only notebook guide generation; no persisted resource is created by replay"
        ),
        RPCMethod.GET_SOURCE_GUIDE: (
            "response-only source guide fetch/generation; no persisted resource is created by replay"
        ),
        RPCMethod.GET_SUGGESTED_REPORTS: (
            "response-only report suggestion generation; no persisted resource is created by replay"
        ),
        RPCMethod.LIST_ARTIFACTS: "read-only artifact list; replay does not mutate artifact state",
        RPCMethod.RENAME_ARTIFACT: (
            "set artifact title to a caller-supplied value; replay leaves the same state"
        ),
        RPCMethod.SHARE_ARTIFACT: (
            "legacy public share-link state update; replay leaves the same share state"
        ),
        # Live method GetArtifact: a generic single-artifact getter (we use it
        # to fetch interactive-artifact HTML / mind-map tree). Read-only.
        RPCMethod.GET_INTERACTIVE_HTML: (
            "read-only artifact fetch; replay does not mutate artifact state"
        ),
        # Live method ListDiscoverSourcesJob (the research family is the
        # DiscoverSources pipeline).
        RPCMethod.POLL_RESEARCH: "read-only research task poll; replay does not start a task",
        # Live method GetNotes: mind maps come back as JSON-bodied notes, so a
        # single GetNotes read returns both.
        RPCMethod.GET_NOTES_AND_MIND_MAPS: (
            "read-only notes/mind-maps list; replay does not mutate note state"
        ),
        RPCMethod.UPDATE_NOTE: (
            "set note content/title to caller-supplied values; replay leaves the same state"
        ),
        RPCMethod.DELETE_NOTE: "server-side note delete is idempotent (set-op semantics)",
        RPCMethod.GET_LAST_CONVERSATION_ID: (
            "read-only conversation id fetch; replay does not mutate chat state"
        ),
        RPCMethod.GET_CONVERSATION_TURNS: (
            "read-only conversation history fetch; replay does not mutate chat state"
        ),
        # Live method DeleteChatTurns: deletes the conversation's chat turns
        # (the web UI "Delete history" action), idempotent set-op.
        RPCMethod.DELETE_CONVERSATION: (
            "server-side chat-turn delete is idempotent (set-op semantics)"
        ),
        # Live method GeneratePromptSuggestions: response-only prompt-suggestion
        # generation (same family as GET_SUGGESTED_REPORTS); persists nothing.
        RPCMethod.SUGGEST_PROMPTS: (
            "response-only prompt suggestion generation; no persisted resource is created by replay"
        ),
        RPCMethod.GET_SHARE_STATUS: "read-only share status fetch; replay does not mutate ACL state",
        RPCMethod.REMOVE_RECENTLY_VIEWED: (
            "remove notebook from recents is idempotent; replay leaves it absent"
        ),
        # Live method GetOrCreateAccount: the first call may create the account
        # server-side, but every call (including replay) converges to the same
        # account state, so replay creates no *additional* resource.
        RPCMethod.GET_USER_SETTINGS: (
            "GetOrCreateAccount settings fetch; replay converges to the same account state"
        ),
        # Live method MutateAccount (generic account mutator; we use it only for
        # the output-language setting).
        RPCMethod.SET_USER_SETTINGS: (
            "set user settings to caller-supplied values; replay leaves the same state"
        ),
        RPCMethod.LIST_LABELS: "read-only label list; replay does not mutate label state",
        RPCMethod.UPDATE_LABEL: (
            "default (rename / set-emoji) sets label fields to caller-supplied values; "
            "replay leaves the same state. The add_sources variant is classified "
            "separately as NON_IDEMPOTENT_NO_RETRY"
        ),
    }

    for _method, _notes in _IDEMPOTENT_READ_OR_SET_OP_NOTES.items():
        registry.register(
            _method,
            IdempotencyPolicy.IDEMPOTENT_SET_OP,
            notes=_notes,
        )

    # ----------------------------------------------------------------------------
    # Complete coverage — non-idempotent methods with no reliable probe/token
    # ----------------------------------------------------------------------------

    registry.register(
        RPCMethod.EXPORT_ARTIFACT,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "exports create an external Docs/Sheets artifact and return its URL; "
            "there is no client-token slot or reliable post-failure probe to bind "
            "a commit-lost export to this call"
        ),
    )
    registry.register(
        RPCMethod.REVISE_SLIDE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "slide revision starts a prompt-driven generation/update with no "
            "client-token slot or probe; a blind retry may create a second, "
            "divergent revision"
        ),
    )
    registry.register(
        RPCMethod.RETRY_ARTIFACT,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "in-place retry kicks off a fresh generation for an already-failed "
            "artifact; the artifact_id is fixed and re-used, but the RPC has no "
            "client-token slot and the response carries the same id whether or "
            "not the kickoff committed, so a blind transport retry could re-launch "
            "generation twice. Surface the first failure and let the caller decide "
            "whether to re-invoke (issue #1319)"
        ),
    )

    # ----------------------------------------------------------------------------
    # Source labels (multi-mode CREATE_LABEL / batch DELETE_LABEL / fieldmask
    # UPDATE_LABEL add_sources). LIST_LABELS and the default rename/emoji
    # UPDATE_LABEL are idempotent set-ops registered above; the writes below have
    # no caller-supplied client-token slot.
    # ----------------------------------------------------------------------------
    registry.register(
        RPCMethod.CREATE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "multi-mode label create/auto-group (agX4Bc) with no client-token slot; "
            "the server allocates label ids and echoes the full set, so a blind retry "
            "on commit-lost could create a duplicate manual label or regenerate every "
            "label with fresh ids — surface the first failure"
        ),
    )
    registry.register(
        RPCMethod.DELETE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "batch label delete with no client-token slot; conservative until the "
            "already-absent wire behavior is captured (rpc.md open item) — no blind "
            "retry until a committed-then-retried delete is proven to no-op, then "
            "downgrade to IDEMPOTENT_SET_OP like DELETE_SOURCE/DELETE_ARTIFACT"
        ),
    )
    registry.register(
        RPCMethod.UPDATE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="add_sources",
        notes=(
            "add_sources APPENDS source ids via the fieldmask, with no client-token "
            "slot; whether a blind retry that lands twice dedupes server-side is "
            "unverified (rpc.md), so surface the first failure rather than risk a "
            "double-append"
        ),
    )
    registry.register(
        RPCMethod.UPDATE_LABEL,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        variant="remove_sources",
        notes=(
            "remove_sources UN-ASSIGNS a source via the sources_remove fieldmask slot; "
            "removing an already-absent member is a confirmed silent no-op (rpc.md "
            "2026-06-07), so a blind transport retry that lands twice leaves the same "
            "final state — retry-safe set-op semantics like DELETE_SOURCE"
        ),
    )
