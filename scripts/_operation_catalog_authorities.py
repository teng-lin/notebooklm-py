"""Exact transport authorities, discriminators, and recency contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from notebooklm._operations import Operation
from notebooklm.rpc import RPCMethod

if __package__:
    from ._operation_catalog_specs import OPERATION_SPECS, NativeKey, _b, _p
else:  # pragma: no cover - direct script execution
    from _operation_catalog_specs import OPERATION_SPECS, NativeKey, _b, _p


@dataclass(frozen=True, slots=True)
class AuthorityRule:
    """One exact current execution authority for a semantic operation."""

    site: str
    discriminator: str


def _rules(*rows: tuple[str, str]) -> tuple[AuthorityRule, ...]:
    return tuple(AuthorityRule(*row) for row in rows)


@dataclass(frozen=True, slots=True)
class AppAuthorityRule:
    """One application/public-helper execution authority."""

    site: str
    binding: str
    discriminator: str


@dataclass(frozen=True, slots=True)
class AppAuthoritySourceContract:
    """Fail-closed source evidence for a delegated application authority."""

    required_calls: tuple[tuple[str, ...], ...]
    internal_caller: str
    caller_target: tuple[str, ...]
    public_export: str


# A shared RPC method is not enough to identify an operation.  These rules
# allocate its direct transport callsites using the public intent/payload
# discriminator that makes the binding semantic.  Unshared bindings are
# derived directly; every shared operation/binding must appear here.
SHARED_RPC_AUTHORITY_RULES: dict[tuple[Operation, NativeKey], tuple[AuthorityRule, ...]] = {
    (Operation.SOURCE_ADD_URL, _b(RPCMethod.ADD_SOURCE, "url")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "web or YouTube URL allocation via the source.register leaf",
        ),
    ),
    (Operation.SOURCE_ADD_URL_BATCH, _b(RPCMethod.ADD_SOURCE, "url")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "one non-replayed batch payload via the source.register leaf",
        ),
    ),
    (Operation.SOURCE_REGISTER, _b(RPCMethod.ADD_SOURCE, "url")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "registration kind url selects the URL/YouTube payload",
        ),
    ),
    (Operation.SOURCE_REGISTER, _b(RPCMethod.ADD_SOURCE, "text")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "registration kind text selects the pasted-text payload",
        ),
    ),
    (Operation.SOURCE_REGISTER, _b(RPCMethod.ADD_SOURCE, "drive")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "registration kind drive selects the Drive-document payload",
        ),
    ),
    (Operation.SOURCE_ADD_TEXT, _b(RPCMethod.ADD_SOURCE, "text")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "pasted-text allocation via the source.register leaf",
        ),
    ),
    (Operation.SOURCE_ADD_DRIVE, _b(RPCMethod.ADD_SOURCE, "drive")): _rules(
        (
            "_web/bindings/primitives.py:SOURCE_REGISTER",
            "native Drive-document allocation via the source.register leaf",
        ),
    ),
    (Operation.NOTEBOOK_LIST, _b(RPCMethod.LIST_NOTEBOOKS)): _rules(
        ("_web/bindings/notebooks.py:NOTEBOOK_LIST", "public=notebooks.list")
    ),
    (Operation.NOTEBOOK_CREATE, _b(RPCMethod.LIST_NOTEBOOKS)): _rules(
        (
            "_web/bindings/notebooks.py:NOTEBOOK_LIST",
            "pre-create baseline/probe or quota verification",
        )
    ),
    (Operation.COLLECTION_NOTEBOOKS, _b(RPCMethod.LIST_NOTEBOOKS)): _rules(
        ("_web/bindings/notebooks.py:NOTEBOOK_LIST", "collection membership expansion")
    ),
    (Operation.NOTEBOOK_GET, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/notebooks.py:NOTEBOOK_GET",
            "typed notebook/source-id lookup plus the row's undecoded raw branch",
        ),
    ),
    (Operation.NOTEBOOK_UPDATE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/notebooks.py:NOTEBOOK_GET", "unconditional post-mutation read")
    ),
    (Operation.NOTEBOOK_METADATA, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/notebooks.py:NOTEBOOK_GET", "metadata notebook branch"),
        (
            "_web/bindings/sources.py:SOURCE_LIST",
            "metadata source branch",
        ),
    ),
    (Operation.NOTEBOOK_SUGGEST_PROMPTS, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/settings.py:NOTEBOOK_SUGGEST_PROMPTS", "source_ids is None")
    ),
    (Operation.SOURCE_LIST, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/sources.py:SOURCE_LIST", "public=sources.list")
    ),
    (Operation.SOURCE_GET, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/sources.py:SOURCE_GET", "select exact source id inside decode")
    ),
    (Operation.SOURCE_ADD_URL, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_LIST",
            "unconditional baseline plus ambiguity probes via source.list",
        ),
        (
            "_web/bindings/sources.py:SOURCE_GET",
            "null UPDATE_SOURCE echo only via source.get",
        ),
    ),
    (Operation.SOURCE_ADD_URL_BATCH, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_LIST",
            "one snapshot only for omitted-row reconciliation via source.list",
        )
    ),
    (Operation.SOURCE_ADD_DRIVE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_LIST",
            "unconditional baseline plus ambiguity probes via source.list",
        )
    ),
    (Operation.SOURCE_ADD_FILE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_ADD_FILE",
            "unconditional baseline plus registration probes",
        )
    ),
    (Operation.SOURCE_UPDATE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_GET",
            "null UPDATE_SOURCE echo only via source.get",
        )
    ),
    (Operation.SOURCE_WAIT, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_WAIT",
            "one readiness snapshot per tick; multi-source modes share it across inputs",
        )
    ),
    (Operation.CHAT_ASK, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/notebooks.py:NOTEBOOK_GET", "source_ids is None")
    ),
    (Operation.CHAT_CONFIGURE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        ("_web/bindings/chat.py:CHAT_CONFIGURE", "action=GET only")
    ),
    (Operation.RESEARCH_IMPORT_VERIFY, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_LIST",
            "pre-import baseline and verification probes",
        )
    ),
    (Operation.LABEL_SOURCES, _b(RPCMethod.GET_NOTEBOOK)): _rules(
        (
            "_web/bindings/sources.py:SOURCE_LIST",
            "resolve label source ids",
        )
    ),
}

_GENERATION_OPERATIONS = {
    Operation.ARTIFACT_GENERATE_AUDIO: "artifact_type=audio",
    Operation.ARTIFACT_GENERATE_VIDEO: "artifact_type=video|cinematic-video",
    Operation.ARTIFACT_GENERATE_REPORT: "artifact_type=report|study-guide",
    Operation.ARTIFACT_GENERATE_QUIZ: "artifact_type=quiz",
    Operation.ARTIFACT_GENERATE_FLASHCARDS: "artifact_type=flashcards",
    Operation.ARTIFACT_GENERATE_INFOGRAPHIC: "artifact_type=infographic",
    Operation.ARTIFACT_GENERATE_SLIDE_DECK: "artifact_type=slide-deck",
    Operation.ARTIFACT_GENERATE_DATA_TABLE: "artifact_type=data-table",
}
for _operation, _discriminator in _GENERATION_OPERATIONS.items():
    # P9.4b: every generate family is a ``CustomBinding`` row whose two specs
    # (``sources`` GET_NOTEBOOK, ``create`` CREATE_ARTIFACT) are the authorities.
    _create_site = f"_web/bindings/studio.py:{_operation.name}"
    _source_site = _create_site
    SHARED_RPC_AUTHORITY_RULES[(_operation, _b(RPCMethod.CREATE_ARTIFACT))] = _rules(
        (_create_site, _discriminator)
    )
    SHARED_RPC_AUTHORITY_RULES[(_operation, _b(RPCMethod.GET_NOTEBOOK))] = _rules(
        (_source_site, "source_ids is None")
    )

SHARED_RPC_AUTHORITY_RULES.update(
    {
        (Operation.MIND_MAP_GENERATE_INTERACTIVE, _b(RPCMethod.CREATE_ARTIFACT)): _rules(
            (
                "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_INTERACTIVE",
                "kind=INTERACTIVE",
            )
        ),
        (Operation.MIND_MAP_GENERATE_INTERACTIVE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
            (
                "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_INTERACTIVE",
                "kind=INTERACTIVE and source_ids is None",
            )
        ),
        (Operation.ARTIFACT_GENERATE_MIND_MAP, _b(RPCMethod.GENERATE_MIND_MAP)): _rules(
            ("_web/bindings/mind_maps.py:ARTIFACT_GENERATE_MIND_MAP", "semantic facade")
        ),
        (Operation.MIND_MAP_GENERATE_NOTE, _b(RPCMethod.GENERATE_MIND_MAP)): _rules(
            (
                "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_NOTE",
                "kind=NOTE_BACKED",
            )
        ),
        (Operation.ARTIFACT_GENERATE_MIND_MAP, _b(RPCMethod.CREATE_NOTE, "plain")): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_GENERATE_MIND_MAP",
                "persist generated JSON through the legacy note family",
            )
        ),
        (Operation.NOTE_CREATE, _b(RPCMethod.CREATE_NOTE, "plain")): _rules(
            ("_web/bindings/notes.py:NOTE_CREATE", "public=notes.create")
        ),
        (Operation.MIND_MAP_GENERATE_NOTE, _b(RPCMethod.CREATE_NOTE, "plain")): _rules(
            (
                "_web/bindings/notes.py:NOTE_CREATE",
                "kind=NOTE_BACKED persistence",
            )
        ),
        (Operation.MIND_MAP_GENERATE_NOTE, _b(RPCMethod.UPDATE_NOTE)): _rules(
            (
                "_web/bindings/notes.py:NOTE_UPDATE",
                "kind=NOTE_BACKED persistence finalize",
            )
        ),
        (Operation.ARTIFACT_GENERATE_MIND_MAP, _b(RPCMethod.GET_NOTEBOOK)): _rules(
            ("_web/bindings/mind_maps.py:ARTIFACT_GENERATE_MIND_MAP", "source_ids is None")
        ),
        (Operation.MIND_MAP_GENERATE_NOTE, _b(RPCMethod.GET_NOTEBOOK)): _rules(
            (
                "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_NOTE",
                "kind=NOTE_BACKED and source_ids is None",
            )
        ),
    }
)


NON_RPC_AUTHORITY_RULES: Mapping[Operation, tuple[tuple[str, str, str, str], ...]] = {
    Operation.CHAT_ASK: (
        (
            "stream",
            "streamed_query",
            "_web/transport.py:WebTransport.stream",
            "reached through the chat.stream_answer leaf; bytes are incrementally buffered",
        ),
    ),
    Operation.CHAT_STREAM_ANSWER: (
        (
            "stream",
            "streamed_query",
            "_web/transport.py:WebTransport.stream",
            "the streamed GenerateFreeFormStreamed POST; it is the row's only native",
        ),
    ),
    Operation.SOURCE_ADD_FILE: (
        (
            "download",
            "drive_https_download",
            "_source/drive_import.py:DriveFetcher._request",
            "sources.add_drive_file only: cookie-authenticated Drive GET before file upload",
        ),
        (
            "upload",
            "resumable_upload",
            "_source/upload.py:SourceUploadPipeline.start_resumable_upload",
            "create resumable upload session",
        ),
        (
            "upload",
            "resumable_upload",
            "_source/upload.py:SourceUploadPipeline.upload_file_streaming._do_finalize",
            "stream bytes and finalize session",
        ),
        (
            "upload",
            "resumable_upload",
            "_source/upload.py:SourceUploadPipeline.cancel_upload_session",
            "pre-finalize cancellation cleanup",
        ),
    ),
    Operation.ARTIFACT_DOWNLOAD: (
        (
            "download",
            "artifact_https_download",
            "_studio/downloads.py:StudioDownloadClient.download",
            "HTTPS media representation; inline/locally formatted representations skip this path",
        ),
    ),
}

# Every manually allocated non-RPC authority must contain these transport calls,
# and every contract row must be allocated to exactly one semantic operation.
NON_RPC_SOURCE_CONTRACTS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    # P10 R2.2: the streamed POST is the transport's ``stream`` verb outright —
    # the row declares a ``StreamNative`` and the transport owns the attempt.
    "_web/transport.py:WebTransport.stream": (("chat_aware_authed_post",),),
    "_source/drive_import.py:DriveFetcher._request": (("stream",),),
    "_source/upload.py:SourceUploadPipeline.start_resumable_upload": (("post",),),
    "_source/upload.py:SourceUploadPipeline.upload_file_streaming._do_finalize": (
        ("stream_upload",),
    ),
    "_source/upload.py:SourceUploadPipeline.cancel_upload_session": (("post",),),
    "_studio/downloads.py:StudioDownloadClient.download": (
        ("get_guarded",),
        ("stream",),
    ),
}


APP_OPERATION_AUTHORITIES: Mapping[Operation, tuple[AppAuthorityRule, ...]] = {
    Operation.ARTIFACT_DOWNLOAD: (
        AppAuthorityRule(
            "_app/download.py:execute_download",
            "public_facade",
            "selection/conflict/filesystem choreography; each facade call owns its own budget",
        ),
    ),
}


# Exported retry helpers are caller-owned compatibility utilities, not
# production application execution authorities. Application generation enters
# one private facade workflow and therefore needs no delegated-authority row.
APP_AUTHORITY_SOURCE_CONTRACTS: Mapping[str, AppAuthoritySourceContract] = {}


@dataclass(frozen=True, slots=True)
class RecencyRule:
    """Structured GET_NOTEBOOK side-effect contract for a public call case."""

    public_methods: tuple[str, ...]
    minimum_calls: int
    maximum_calls: int | None
    unit: str
    condition: str
    authority_sites: tuple[str, ...] = ()


_GET_TYPED = "_web/bindings/notebooks.py:NOTEBOOK_GET"
_UPDATE_TYPED = "_web/bindings/notebooks.py:NOTEBOOK_GET"
_GET_SOURCES = "_web/bindings/sources.py:SOURCE_LIST"
# P9.3: source list/get/wait reads dispatch through their own codec rows.
_GET_SOURCE_LIST = "_web/bindings/sources.py:SOURCE_LIST"
_GET_SOURCE = "_web/bindings/sources.py:SOURCE_GET"
_GET_SOURCE_WAIT = "_web/bindings/sources.py:SOURCE_WAIT"
_GET_PROMPT_SOURCES = "_web/bindings/settings.py:NOTEBOOK_SUGGEST_PROMPTS"


RECENCY_CONTRACTS: dict[Operation, tuple[RecencyRule, ...]] = {
    Operation.NOTEBOOK_GET: (
        RecencyRule(
            _p("notebooks", "get", "get_or_none", "get_raw", "get_source_ids"),
            1,
            1,
            "public_call",
            "always: one row call per public read, decoded or raw",
            (_GET_TYPED,),
        ),
    ),
    Operation.NOTEBOOK_UPDATE: (
        RecencyRule(
            _p("notebooks", "update", "rename", "set_emoji"),
            1,
            1,
            "public_call",
            "always after a successful mutation",
            (_UPDATE_TYPED,),
        ),
    ),
    Operation.NOTEBOOK_METADATA: (
        RecencyRule(
            _p("notebooks", "get_metadata"),
            2,
            2,
            "public_call",
            "always: concurrent notebook.get plus source listing",
            (_GET_TYPED, _GET_SOURCES),
        ),
    ),
    Operation.NOTEBOOK_SUGGEST_PROMPTS: (
        RecencyRule(
            _p("notebooks", "suggest_prompts"),
            0,
            1,
            "public_call",
            "one only when source_ids is omitted",
            (_GET_PROMPT_SOURCES,),
        ),
    ),
    Operation.SOURCE_LIST: (
        RecencyRule(_p("sources", "list"), 1, 1, "public_call", "always", (_GET_SOURCE_LIST,)),
    ),
    Operation.SOURCE_GET: (
        RecencyRule(
            _p("sources", "get", "get_or_none"),
            1,
            1,
            "public_call",
            "always",
            (_GET_SOURCE,),
        ),
    ),
    Operation.SOURCE_ADD_TEXT: (
        RecencyRule(
            _p("sources", "add_text"),
            0,
            0,
            "public_call",
            "the create operation never reads; wait=True composes source.wait",
        ),
    ),
    Operation.SOURCE_ADD_URL_BATCH: (
        RecencyRule(
            (),
            0,
            1,
            "private_app_call",
            "one snapshot only when ADD_SOURCE omits positional outcomes",
            (_GET_SOURCE_LIST,),
        ),
    ),
    Operation.SOURCE_ADD_URL: (
        RecencyRule(
            _p("sources", "add_url"),
            1,
            None,
            "public_call",
            "one baseline plus ambiguity probes and, when wait=True, one snapshot per "
            "facade-owned readiness poll tick",
            (_GET_SOURCE_LIST, _GET_SOURCE),
        ),
    ),
    Operation.SOURCE_ADD_DRIVE: (
        RecencyRule(
            _p("sources", "add_drive"),
            1,
            None,
            "public_call",
            "one baseline plus ambiguity probes and, when wait=True, one snapshot per "
            "facade-owned readiness poll tick",
            (_GET_SOURCE_LIST,),
        ),
    ),
    Operation.SOURCE_ADD_FILE: (
        RecencyRule(
            _p("sources", "add_file", "add_drive_file"),
            1,
            None,
            "public_call",
            "one baseline plus registration/reconciliation probes; a custom title may add "
            "registration ticks even when wait=False, and wait=True adds facade-owned "
            "readiness ticks",
            ("_web/bindings/sources.py:SOURCE_ADD_FILE",),
        ),
    ),
    Operation.SOURCE_UPDATE: (
        RecencyRule(
            _p("sources", "rename"),
            0,
            1,
            "public_call",
            "one exact-id source read only when UPDATE_SOURCE returns a null echo",
            (_GET_SOURCE,),
        ),
    ),
    Operation.SOURCE_WAIT: (
        RecencyRule(
            _p(
                "sources",
                "wait_until_ready",
                "wait_until_registered",
            ),
            1,
            1,
            "waiter_poll_tick",
            "one snapshot per single-source waiter tick",
            (_GET_SOURCE_WAIT,),
        ),
        RecencyRule(
            _p("sources", "wait_all_until_ready"),
            1,
            1,
            "aggregate_poll_tick",
            "one shared snapshot per tick regardless of source count",
            (_GET_SOURCE_WAIT,),
        ),
        RecencyRule(
            _p("sources", "wait_for_sources"),
            1,
            1,
            "aggregate_poll_tick",
            "one shared snapshot per tick regardless of source count",
            (_GET_SOURCE_WAIT,),
        ),
        RecencyRule(
            _p(
                "sources",
                "add_url",
                "add_text",
                "add_file",
                "add_drive",
                "add_drive_file",
            ),
            1,
            1,
            "optional_add_poll_tick",
            "one source.wait snapshot per tick only when wait=True",
            (_GET_SOURCE_WAIT,),
        ),
    ),
    Operation.CHAT_ASK: (
        RecencyRule(
            _p("chat", "ask"),
            0,
            1,
            "public_call",
            "one only when source_ids is omitted",
            (_GET_TYPED,),
        ),
    ),
    Operation.CHAT_CONFIGURE: (
        RecencyRule(
            _p("chat", "get_settings"),
            1,
            1,
            "public_call",
            "always for get_settings",
            ("_web/bindings/chat.py:CHAT_CONFIGURE",),
        ),
        RecencyRule(
            _p("chat", "configure", "set_mode"),
            0,
            0,
            "public_call",
            "configure/set_mode mutate embedded settings without reading the notebook",
        ),
    ),
}

for _operation in (*_GENERATION_OPERATIONS, Operation.ARTIFACT_GENERATE_MIND_MAP):
    # P9.4b: every generate row reads default sources through its own declared spec.
    _recency_site = (
        "_web/bindings/mind_maps.py:ARTIFACT_GENERATE_MIND_MAP"
        if _operation is Operation.ARTIFACT_GENERATE_MIND_MAP
        else f"_web/bindings/studio.py:{_operation.name}"
    )
    RECENCY_CONTRACTS[_operation] = (
        RecencyRule(
            next(spec.public_methods for spec in OPERATION_SPECS if spec.operation is _operation),
            0,
            1,
            "public_call",
            "one only when source_ids is omitted",
            (_recency_site,),
        ),
    )
for _operation, _kind in (
    (Operation.MIND_MAP_GENERATE_NOTE, "NOTE_BACKED"),
    (Operation.MIND_MAP_GENERATE_INTERACTIVE, "INTERACTIVE"),
):
    _recency_site = (
        "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_NOTE"
        if _operation is Operation.MIND_MAP_GENERATE_NOTE
        else "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_INTERACTIVE"
    )
    RECENCY_CONTRACTS[_operation] = (
        RecencyRule(
            _p("mind_maps", "generate"),
            0,
            1,
            "public_call",
            f"one only when kind={_kind} and source_ids is omitted",
            (_recency_site,),
        ),
    )

SHARED_RPC_AUTHORITY_RULES.update(
    {
        (Operation.ARTIFACT_LIST, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            ("_web/bindings/mind_maps.py:ARTIFACT_LIST", "heterogeneous list")
        ),
        (Operation.ARTIFACT_GET, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_GET",
                "select artifact for get/get_or_none/get_prompt",
            ),
        ),
        (Operation.ARTIFACT_LIST, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_LIST",
                "merge note-backed mind maps",
            )
        ),
        (Operation.ARTIFACT_GET, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_GET",
                "select note-backed mind map",
            )
        ),
        (Operation.ARTIFACT_DOWNLOAD, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            (
                "_web/bindings/studio.py:ARTIFACT_DOWNLOAD",
                "action=mind_maps note-backed mind-map representation read",
            )
        ),
        (Operation.ARTIFACT_RENAME, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            ("_web/bindings/studio.py:ARTIFACT_CATALOG", "post-mutation readback via leaf")
        ),
        (Operation.ARTIFACT_CATALOG, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            ("_web/bindings/studio.py:ARTIFACT_CATALOG", "plain Studio catalog read")
        ),
        (Operation.ARTIFACT_DOWNLOAD, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            (
                "_web/bindings/studio.py:ARTIFACT_DOWNLOAD",
                "action=catalog representation catalog read",
            ),
        ),
        (Operation.ARTIFACT_WAIT, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            (
                "_web/bindings/studio.py:ARTIFACT_WAIT",
                "one catalog read per poll tick",
            )
        ),
        (Operation.MIND_MAP_LIST, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_LIST",
                "filter interactive maps",
            )
        ),
        (Operation.MIND_MAP_GET, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_LIST",
                "auto-detect/select interactive id",
            )
        ),
        (Operation.MIND_MAP_GENERATE_INTERACTIVE, _b(RPCMethod.LIST_ARTIFACTS)): _rules(
            (
                "_web/bindings/mind_maps.py:ARTIFACT_LIST",
                "post-create settle/id match",
            )
        ),
        (Operation.LABEL_LIST, _b(RPCMethod.LIST_LABELS)): _rules(
            ("_web/bindings/labels.py:LABEL_LIST", "label_type=source")
        ),
        (Operation.LABEL_GET, _b(RPCMethod.LIST_LABELS)): _rules(
            ("_web/bindings/labels.py:LABEL_GET", "select source-label id")
        ),
        (Operation.LABEL_SOURCES, _b(RPCMethod.LIST_LABELS)): _rules(
            (
                "_web/bindings/labels.py:LABEL_LIST",
                "resolve source-label membership via label.list",
            )
        ),
        (Operation.COLLECTION_LIST, _b(RPCMethod.LIST_LABELS)): _rules(
            ("_web/bindings/labels.py:COLLECTION_LIST", "label_type=collection")
        ),
        (Operation.COLLECTION_GET, _b(RPCMethod.LIST_LABELS)): _rules(
            ("_web/bindings/labels.py:COLLECTION_GET", "select collection id")
        ),
        (Operation.LABEL_CREATE, _b(RPCMethod.LIST_LABELS)): _rules(
            ("_web/bindings/labels.py:LABEL_LIST", "pre-create identity baseline via label.list")
        ),
        # P9.2-2: label.update is service-owned; its reads run through the
        # label.get leaf row and its writes through the label.mutate primitive.
        (Operation.LABEL_UPDATE, _b(RPCMethod.LIST_LABELS)): _rules(
            ("_web/bindings/labels.py:LABEL_GET", "update preflight/readback via label.get")
        ),
        (Operation.COLLECTION_CREATE, _b(RPCMethod.LIST_LABELS)): _rules(
            (
                "_web/bindings/labels.py:COLLECTION_LIST",
                "baseline/create readback via collection.list",
            )
        ),
        (Operation.COLLECTION_UPDATE, _b(RPCMethod.LIST_LABELS)): _rules(
            (
                "_web/bindings/labels.py:COLLECTION_GET",
                "update preflight/readback via collection.get",
            )
        ),
        (Operation.COLLECTION_NOTEBOOKS, _b(RPCMethod.LIST_LABELS)): _rules(
            (
                "_web/bindings/labels.py:COLLECTION_LIST",
                "resolve collection membership via collection.list",
            )
        ),
        (Operation.RESEARCH_POLL, _b(RPCMethod.POLL_RESEARCH)): _rules(
            ("_web/bindings/research.py:RESEARCH_POLL", "single public poll")
        ),
        (Operation.RESEARCH_WAIT, _b(RPCMethod.POLL_RESEARCH)): _rules(
            ("_web/bindings/research.py:RESEARCH_POLL", "one read per wait poll tick")
        ),
        (Operation.ARTIFACT_RENAME, _b(RPCMethod.RENAME_ARTIFACT)): _rules(
            (
                "_web/bindings/studio.py:ARTIFACT_PATCH_TITLE",
                "public=artifacts.rename via leaf",
            )
        ),
        (Operation.ARTIFACT_PATCH_TITLE, _b(RPCMethod.RENAME_ARTIFACT)): _rules(
            ("_web/bindings/studio.py:ARTIFACT_PATCH_TITLE", "one title set-op")
        ),
        (Operation.MIND_MAP_UPDATE, _b(RPCMethod.RENAME_ARTIFACT)): _rules(
            ("_web/bindings/mind_maps.py:MIND_MAP_UPDATE", "kind=INTERACTIVE")
        ),
        (Operation.NOTEBOOK_UPDATE, _b(RPCMethod.RENAME_NOTEBOOK)): _rules(
            ("_web/bindings/notebooks.py:NOTEBOOK_PATCH", "title|emoji mutation via leaf")
        ),
        (Operation.NOTEBOOK_PATCH, _b(RPCMethod.RENAME_NOTEBOOK)): _rules(
            ("_web/bindings/notebooks.py:NOTEBOOK_PATCH", "title|emoji property mask")
        ),
        (Operation.CHAT_CONFIGURE, _b(RPCMethod.RENAME_NOTEBOOK)): _rules(
            ("_web/bindings/chat.py:CHAT_CONFIGURE", "action=SET")
        ),
        (Operation.SHARING_SET_VIEW_LEVEL, _b(RPCMethod.RENAME_NOTEBOOK)): _rules(
            (
                "_web/bindings/primitives.py:SHARING_PATCH_VIEW_LEVEL",
                "share-view-level payload via sharing.patch_view_level",
            )
        ),
        (Operation.SHARING_PATCH_VIEW_LEVEL, _b(RPCMethod.RENAME_NOTEBOOK)): _rules(
            (
                "_web/bindings/primitives.py:SHARING_PATCH_VIEW_LEVEL",
                "share-view-level payload",
            )
        ),
        (Operation.SHARING_SET_PUBLIC, _b(RPCMethod.SHARE_NOTEBOOK)): _rules(
            (
                "_web/bindings/primitives.py:SHARING_MUTATE",
                "visibility entry via sharing.mutate",
            )
        ),
        (Operation.SHARING_UPDATE_USERS, _b(RPCMethod.SHARE_NOTEBOOK)): _rules(
            (
                "_web/bindings/primitives.py:SHARING_MUTATE",
                "user grant/upsert and removal entries via sharing.mutate",
            ),
        ),
        (Operation.SHARING_MUTATE, _b(RPCMethod.SHARE_NOTEBOOK)): _rules(
            ("_web/bindings/primitives.py:SHARING_MUTATE", "visibility or grant envelope")
        ),
        (Operation.NOTEBOOK_SUMMARIZE, _b(RPCMethod.SUMMARIZE)): _rules(
            ("_web/bindings/notebooks.py:NOTEBOOK_SUMMARIZE", "summary projection")
        ),
        (Operation.NOTEBOOK_DESCRIBE, _b(RPCMethod.SUMMARIZE)): _rules(
            ("_web/bindings/notebooks.py:NOTEBOOK_DESCRIBE", "description/topics projection")
        ),
        (Operation.LABEL_UPDATE, _b(RPCMethod.UPDATE_LABEL)): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "label field-mask mutation via leaf")
        ),
        (Operation.COLLECTION_UPDATE, _b(RPCMethod.UPDATE_LABEL)): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "collection name mutation via leaf")
        ),
        (Operation.LABEL_UPDATE, _b(RPCMethod.UPDATE_LABEL, "add_sources")): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "one source append per call via leaf")
        ),
        (Operation.LABEL_UPDATE, _b(RPCMethod.UPDATE_LABEL, "remove_sources")): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "one source removal per call via leaf")
        ),
        (Operation.COLLECTION_UPDATE, _b(RPCMethod.UPDATE_LABEL, "add_notebooks")): _rules(
            (
                "_web/bindings/primitives.py:LABEL_MUTATE",
                "one notebook append per call via leaf",
            )
        ),
        (Operation.COLLECTION_UPDATE, _b(RPCMethod.UPDATE_LABEL, "remove_notebooks")): _rules(
            (
                "_web/bindings/primitives.py:LABEL_MUTATE",
                "one notebook removal per call via leaf",
            )
        ),
        # P9.2 primitive: the row selects the variant from the request kind and form.
        (Operation.LABEL_MUTATE, _b(RPCMethod.UPDATE_LABEL)): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "form=field")
        ),
        (Operation.LABEL_MUTATE, _b(RPCMethod.UPDATE_LABEL, "add_sources")): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "kind=source_label form=add")
        ),
        (Operation.LABEL_MUTATE, _b(RPCMethod.UPDATE_LABEL, "remove_sources")): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "kind=source_label form=remove")
        ),
        (Operation.LABEL_MUTATE, _b(RPCMethod.UPDATE_LABEL, "add_notebooks")): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "kind=collection form=add")
        ),
        (Operation.LABEL_MUTATE, _b(RPCMethod.UPDATE_LABEL, "remove_notebooks")): _rules(
            ("_web/bindings/primitives.py:LABEL_MUTATE", "kind=collection form=remove")
        ),
        (Operation.NOTE_UPDATE, _b(RPCMethod.UPDATE_NOTE)): _rules(
            ("_web/bindings/notes.py:NOTE_UPDATE", "public=notes.update")
        ),
        (Operation.NOTE_CREATE, _b(RPCMethod.UPDATE_NOTE)): _rules(
            ("_web/bindings/notes.py:NOTE_UPDATE", "notes.create finalize")
        ),
        (Operation.ARTIFACT_GENERATE_MIND_MAP, _b(RPCMethod.UPDATE_NOTE)): _rules(
            (
                "_note_service.py:LegacyNoteBackedService.update_note",
                "persist generated JSON and title",
            )
        ),
        (Operation.MIND_MAP_UPDATE, _b(RPCMethod.UPDATE_NOTE)): _rules(
            ("_web/bindings/notes.py:NOTE_UPDATE", "kind=NOTE_BACKED")
        ),
        (Operation.SOURCE_ADD_URL, _b(RPCMethod.UPDATE_SOURCE)): _rules(
            (
                "_web/bindings/primitives.py:SOURCE_PATCH_TITLE",
                "optional post-create title via source.patch_title",
            )
        ),
        (Operation.SOURCE_ADD_DRIVE, _b(RPCMethod.UPDATE_SOURCE)): _rules(
            (
                "_web/bindings/primitives.py:SOURCE_PATCH_TITLE",
                "optional post-create title via source.patch_title",
            )
        ),
        (Operation.SOURCE_ADD_FILE, _b(RPCMethod.UPDATE_SOURCE)): _rules(
            (
                "_web/bindings/sources.py:SOURCE_ADD_FILE",
                "optional post-upload title",
            )
        ),
        (Operation.SOURCE_UPDATE, _b(RPCMethod.UPDATE_SOURCE)): _rules(
            (
                "_web/bindings/primitives.py:SOURCE_PATCH_TITLE",
                "public=sources.rename via source.patch_title",
            )
        ),
        (Operation.SOURCE_PATCH_TITLE, _b(RPCMethod.UPDATE_SOURCE)): _rules(
            ("_web/bindings/primitives.py:SOURCE_PATCH_TITLE", "one title set-op")
        ),
    }
)

SHARED_RPC_AUTHORITY_RULES.update(
    {
        (Operation.NOTEBOOK_CREATE, _b(RPCMethod.CREATE_NOTEBOOK)): _rules(
            ("_web/bindings/notebooks.py:NOTEBOOK_ALLOCATE", "guarded allocation attempts")
        ),
        (Operation.NOTEBOOK_ALLOCATE, _b(RPCMethod.CREATE_NOTEBOOK)): _rules(
            ("_web/bindings/notebooks.py:NOTEBOOK_ALLOCATE", "one guarded allocation attempt")
        ),
        (Operation.LABEL_GENERATE, _b(RPCMethod.CREATE_LABEL)): _rules(
            ("_web/bindings/labels.py:LABEL_GENERATE", "label_mode=auto-group")
        ),
        (Operation.LABEL_CREATE, _b(RPCMethod.CREATE_LABEL)): _rules(
            (
                "_web/bindings/primitives.py:LABEL_ALLOCATE",
                "manual source-label allocation via label.allocate",
            )
        ),
        (Operation.COLLECTION_CREATE, _b(RPCMethod.CREATE_LABEL)): _rules(
            (
                "_web/bindings/primitives.py:LABEL_ALLOCATE",
                "manual collection allocation via label.allocate",
            )
        ),
        (Operation.LABEL_ALLOCATE, _b(RPCMethod.CREATE_LABEL)): _rules(
            ("_web/bindings/primitives.py:LABEL_ALLOCATE", "manual allocation, either dialect")
        ),
        (Operation.ARTIFACT_DELETE, _b(RPCMethod.DELETE_ARTIFACT)): _rules(
            ("_web/bindings/studio.py:ARTIFACT_DELETE", "public=artifacts.delete")
        ),
        (Operation.MIND_MAP_DELETE, _b(RPCMethod.DELETE_ARTIFACT)): _rules(
            ("_web/bindings/mind_maps.py:MIND_MAP_DELETE", "kind=INTERACTIVE")
        ),
        (Operation.NOTE_DELETE, _b(RPCMethod.DELETE_NOTE)): _rules(
            ("_web/bindings/notes.py:NOTE_DELETE", "public=notes.delete")
        ),
        (Operation.NOTE_CREATE, _b(RPCMethod.DELETE_NOTE)): _rules(
            ("_web/bindings/notes.py:NOTE_DELETE", "cancelled create orphan cleanup")
        ),
        (Operation.ARTIFACT_GENERATE_MIND_MAP, _b(RPCMethod.DELETE_NOTE)): _rules(
            (
                "_note_service.py:LegacyNoteBackedService.delete_note",
                "cancelled generated-note cleanup",
            )
        ),
        (Operation.MIND_MAP_DELETE, _b(RPCMethod.DELETE_NOTE)): _rules(
            ("_web/bindings/notes.py:NOTE_DELETE", "kind=NOTE_BACKED")
        ),
        (Operation.LABEL_DELETE, _b(RPCMethod.DELETE_LABEL)): _rules(
            ("_web/bindings/labels.py:LABEL_DELETE", "label_type=source")
        ),
        (Operation.COLLECTION_DELETE, _b(RPCMethod.DELETE_LABEL)): _rules(
            ("_web/bindings/labels.py:COLLECTION_DELETE", "label_type=collection")
        ),
        (Operation.ARTIFACT_DOWNLOAD, _b(RPCMethod.GET_INTERACTIVE_HTML)): _rules(
            (
                "_web/bindings/studio.py:ARTIFACT_DOWNLOAD",
                "action=interactive_html|mind_map_tree interactive representation",
            ),
        ),
        (Operation.MIND_MAP_GET, _b(RPCMethod.GET_INTERACTIVE_HTML)): _rules(
            ("_web/bindings/mind_maps.py:MIND_MAP_GET", "kind=INTERACTIVE")
        ),
        (Operation.MIND_MAP_GENERATE_INTERACTIVE, _b(RPCMethod.GET_INTERACTIVE_HTML)): _rules(
            ("_web/bindings/mind_maps.py:MIND_MAP_GET", "wait=True post-generation tree")
        ),
        (Operation.CHAT_ASK, _b(RPCMethod.GET_LAST_CONVERSATION_ID)): _rules(
            (
                "_web/bindings/chat.py:CHAT_GET_CONVERSATION",
                "post-stream resolution via chat.get_conversation when no id is already known",
            )
        ),
        (Operation.CHAT_GET_CONVERSATION, _b(RPCMethod.GET_LAST_CONVERSATION_ID)): _rules(
            (
                "_web/bindings/chat.py:CHAT_GET_CONVERSATION",
                "public=chat.get_conversation_id",
            )
        ),
        (Operation.NOTE_LIST, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            ("_web/bindings/notes.py:NOTE_LIST", "filter kind=NOTE")
        ),
        (Operation.NOTE_GET, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            ("_web/bindings/notes.py:NOTE_GET", "select note id")
        ),
        (Operation.MIND_MAP_LIST, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            (
                "_web/bindings/mind_maps.py:MIND_MAP_LIST",
                "filter kind=NOTE_BACKED",
            )
        ),
        (Operation.MIND_MAP_GET, _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)): _rules(
            (
                "_web/bindings/mind_maps.py:MIND_MAP_LIST",
                "auto-detect/select note-backed id",
            )
        ),
        (Operation.SHARING_GET, _b(RPCMethod.GET_SHARE_STATUS)): _rules(
            ("_web/bindings/sharing.py:SHARING_GET", "public=sharing.get_status")
        ),
        (Operation.SHARING_SET_PUBLIC, _b(RPCMethod.GET_SHARE_STATUS)): _rules(
            (
                "_web/bindings/sharing.py:SHARING_GET",
                "post-public-mutation read via sharing.get",
            )
        ),
        (Operation.SHARING_SET_VIEW_LEVEL, _b(RPCMethod.GET_SHARE_STATUS)): _rules(
            (
                "_web/bindings/sharing.py:SHARING_GET",
                "post-view-level-mutation read via sharing.get",
            )
        ),
        (Operation.SHARING_UPDATE_USERS, _b(RPCMethod.GET_SHARE_STATUS)): _rules(
            (
                "_web/bindings/sharing.py:SHARING_GET",
                "post-user-grant mutation read via sharing.get",
            )
        ),
        (Operation.NOTEBOOK_CREATE, _b(RPCMethod.GET_USER_SETTINGS)): _rules(
            ("_web/bindings/settings.py:SETTINGS_GET_LIMITS", "quota-error diagnosis only")
        ),
        (Operation.SOURCE_ADD_FILE, _b(RPCMethod.GET_USER_SETTINGS)): _rules(
            (
                "_web/bindings/sources.py:SOURCE_ADD_FILE",
                "invalid-argument diagnosis only",
            )
        ),
        (Operation.SETTINGS_GET, _b(RPCMethod.GET_USER_SETTINGS)): _rules(
            ("_web/bindings/settings.py:SETTINGS_GET", "settings row projection")
        ),
        (Operation.SETTINGS_GET_LIMITS, _b(RPCMethod.GET_USER_SETTINGS)): _rules(
            ("_web/bindings/settings.py:SETTINGS_GET_LIMITS", "account-limit projection")
        ),
        (Operation.RESEARCH_IMPORT, _b(RPCMethod.IMPORT_RESEARCH)): _rules(
            ("_web/bindings/research.py:RESEARCH_IMPORT", "single import attempt")
        ),
        (Operation.RESEARCH_IMPORT_VERIFY, _b(RPCMethod.IMPORT_RESEARCH)): _rules(
            ("_web/bindings/research.py:RESEARCH_IMPORT", "verified import attempt")
        ),
    }
)
