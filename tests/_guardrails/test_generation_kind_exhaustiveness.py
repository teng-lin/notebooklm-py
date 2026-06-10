"""Gate: the generation-kind axis stays in lockstep across its parallel tables.

The kind/type axis (audio, video, cinematic-video, slide-deck, revise-slide,
quiz, flashcards, infographic, data-table, mind-map, report) is spread across
parallel tables that nothing previously tied together: the ``GenerationKind``
Literal (the single source of truth, ``_app/generate_plans.py``), the per-kind
plan builders (``_BUILDERS``), the display-name map (``_DISPLAY_NAME``), the
executor dispatch table (``_KIND_TO_METHOD``), the spinner duration hints
(``_TYPICAL_DURATIONS``), the hand-written ``generate <kind>`` Click leaves,
the ``DOWNLOAD_SPECS`` registry (which itself derives the ``download <kind>``
leaves), the ``ArtifactsAPI`` ``generate_*`` / ``download_*`` facade method
sets, the per-kind RPC payload builders (``_artifact/payloads.py``), and the
type-decode chain (``ArtifactType`` -> ``ArtifactTypeCode`` ->
``_ARTIFACT_TYPE_CODE_MAP`` -> the ``artifact list --type`` filter choices).

Adding a kind was a multi-file memory exercise; a missed table failed
*silently* (no duration hint, no CLI leaf, a kind that lists but cannot
download, ...). "Enforce, don't document": this gate derives the axis from
``typing.get_args(GenerationKind)`` and asserts per-table parity.

The parity rules are NOT naive full equality — some tables legitimately cover
a subset. Every legitimate gap is a **documented exception** carrying a
one-line reason; the detector is **self-draining** (an exception whose kind
shows up in the table, or that names a retired kind, fails loudly so the
exception sets can only shrink). Cross-axis facts baked in as exceptions:

* ``cinematic-video`` is a generation kind but not an artifact type — it
  shares the VIDEO wire type / download path (the CLI ``download
  cinematic-video`` leaf is a pure Click alias for ``download video``).
* ``revise-slide`` is an operation on an existing SLIDE_DECK artifact, never
  a listable/downloadable type.
* ``mind-map`` generation returns ``MindMapResult`` (rendered synchronously),
  not a ``GenerationStatus`` wait loop, and has TWO first-class backings
  (note-backed + interactive studio artifact) and therefore two payload
  builders — neither is deprecated.
* ``quiz`` / ``flashcards`` share wire type-code 4, distinguished by variant,
  so they are absent from the code->type map and FLASHCARDS has no own
  ``ArtifactTypeCode`` member.
* ``report`` display names are per-format (``_REPORT_DISPLAY``), so the kind
  has no row in ``_DISPLAY_NAME``.

KNOWN PARITY BUG (baselined, not fixed here — see
:func:`test_duration_hint_behavior_baseline_known_bug`):
``_TYPICAL_DURATIONS`` is keyed by *kind* names but the runtime lookup
(``_format_status_message`` via ``handle_generation_result``) receives the
plan's *display name* ("slide deck", "data table", "briefing document", ...).
Five keys are therefore unreachable today and their hints never render;
cinematic-video waits show the standard-video hint. The baseline is
**behavioral**: it drives the REAL ``execute_generation`` end-to-end (real
``build_generation_plan`` plan, stub client) and pins the spinner message the
executor actually emits per kind, so it self-drains on ANY fix path —
re-keying the table by display names, changing the lookup inside
``_format_status_message``, OR switching the executor call-site argument to
``plan.kind``.

Anti-vacuity: the axis floor (>= 11 kinds) is asserted so a broken derivation
cannot pass silently, and pure-detector self-checks prove a planted
missing-kind / unknown-key / stale-exception is actually caught.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import typing
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from notebooklm import _artifact
from notebooklm._app.generate import _KIND_TO_METHOD, execute_generation
from notebooklm._app.generate_plans import (
    _BUILDERS,
    _DISPLAY_NAME,
    _REPORT_DISPLAY,
    GenerationKind,
    build_generation_plan,
)
from notebooklm._app.generate_retry import _TYPICAL_DURATIONS, _format_status_message
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._types.artifacts import _ARTIFACT_TYPE_CODE_MAP
from notebooklm.cli import artifact_cmd
from notebooklm.cli._download_specs import DOWNLOAD_SPECS
from notebooklm.cli.download_cmd import download as download_group
from notebooklm.cli.generate_cmd import generate as generate_group
from notebooklm.cli.rendering import cli_name_to_artifact_type
from notebooklm.rpc.types import ArtifactTypeCode
from notebooklm.types import ArtifactType, GenerationStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"

#: The single source of truth for the axis.
KINDS: frozenset[str] = frozenset(typing.get_args(GenerationKind))

#: Anti-vacuity floor: the axis has 11 kinds today. If the derivation above
#: ever returns fewer, the discovery is broken (or a kind was deliberately
#: retired — then lower this floor in the same commit, on purpose).
KNOWN_KIND_FLOOR = 11


# --- file:line locators --------------------------------------------------------
# Failure messages cite the exact table to update. Line numbers are resolved at
# test time (never hardcoded, so they cannot rot); a locator whose pattern no
# longer matches fails test_locators_resolve so the pointer gets re-anchored.


def _loc(rel: str, pattern: str) -> str:
    """Return ``src/notebooklm/<rel>:<line>`` of the first line matching ``pattern``."""
    path = SRC_ROOT / rel
    if not path.is_file():
        return f"src/notebooklm/{rel} (MODULE NOT FOUND — re-anchor this locator)"
    rx = re.compile(pattern)
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if rx.search(line):
            return f"src/notebooklm/{rel}:{lineno}"
    return f"src/notebooklm/{rel} (PATTERN {pattern!r} NOT FOUND — re-anchor this locator)"


LOC: Mapping[str, str] = {
    "GenerationKind": _loc("_app/generate_plans.py", r"^GenerationKind = Literal"),
    "_BUILDERS": _loc("_app/generate_plans.py", r"^_BUILDERS"),
    "_DISPLAY_NAME": _loc("_app/generate_plans.py", r"^_DISPLAY_NAME"),
    "_KIND_TO_METHOD": _loc("_app/generate.py", r"^_KIND_TO_METHOD"),
    "_TYPICAL_DURATIONS": _loc("_app/generate_retry.py", r"^_TYPICAL_DURATIONS"),
    "generate group": _loc("cli/generate_cmd.py", r"^def generate\("),
    "download registration": _loc("cli/download_cmd.py", r"^for _spec in DOWNLOAD_SPECS"),
    "DOWNLOAD_SPECS": _loc("cli/_download_specs.py", r"^DOWNLOAD_SPECS"),
    "ArtifactsAPI": _loc("_artifacts.py", r"^class ArtifactsAPI"),
    "ArtifactType": _loc("_types/artifacts.py", r"^class ArtifactType\("),
    "ArtifactTypeCode": _loc("rpc/types.py", r"^class ArtifactTypeCode\("),
    "_ARTIFACT_TYPE_CODE_MAP": _loc("_types/artifacts.py", r"^_ARTIFACT_TYPE_CODE_MAP"),
    "artifact list --type": _loc("cli/artifact_cmd.py", r'^\s*"--type",'),
    "payloads module": _loc("_artifact/payloads.py", r"^def build_audio_artifact_params"),
}


# --- pure parity detector ------------------------------------------------------
# Pure on its inputs so the self-checks below can plant drifted tables and
# prove the detector bites, exercising the SAME logic the live gates run.


def parity_failures(
    axis: frozenset[str],
    keys: Iterable[str],
    *,
    table: str,
    axis_name: str,
    axis_location: str,
    exceptions: Mapping[str, str] | None = None,
    extras: Mapping[str, str] | None = None,
) -> list[str]:
    """Compare a table's key set against the axis; return teaching messages.

    ``exceptions`` are axis members the table legitimately does NOT cover
    (each with a one-line reason); ``extras`` are keys the table legitimately
    carries beyond the axis. Both are self-draining: a stale entry (exception
    now covered / extra now absent / extra now an axis member / either naming
    a retired member) is itself a failure, so the documented sets can only
    shrink.
    """
    exceptions = exceptions or {}
    extras = extras or {}
    key_set = set(keys)
    failures: list[str] = []

    for kind in sorted(axis - key_set - set(exceptions)):
        failures.append(
            f"{axis_name} {kind!r} is missing from {table}. Add the kind's row there, "
            f"or — if the table legitimately must not cover it — add a documented "
            f"exception (with a one-line reason) to this guardrail."
        )
    for kind in sorted(set(exceptions) & key_set):
        failures.append(
            f"Documented exception for {kind!r} in {table} is stale: the kind is now "
            f"covered by the table. Drain the exception from this guardrail."
        )
    for kind in sorted(set(exceptions) - axis):
        failures.append(
            f"Documented exception for {kind!r} in {table} names a member that no "
            f"longer exists in {axis_name} ({axis_location}). Drain it."
        )
    for key in sorted(key_set - axis - set(extras)):
        failures.append(
            f"{table} carries key {key!r} which is not a {axis_name} member "
            f"({axis_location}) nor a documented extra. Add the kind to {axis_name} "
            f"(and every parallel table this gate checks) or remove the row."
        )
    for key in sorted(set(extras) & axis):
        failures.append(
            f"Documented extra {key!r} for {table} has JOINED {axis_name} "
            f"({axis_location}) — it is no longer an extra beyond the axis. Drain it "
            f"from the extras set in this guardrail."
        )
    for key in sorted(set(extras) - key_set):
        failures.append(
            f"Documented extra {key!r} for {table} is no longer present in the table. "
            f"Drain it from this guardrail."
        )
    return failures


def _assert_parity(failures: list[str]) -> None:
    assert failures == [], "\n".join(failures)


def _check_kind_table(
    keys: Iterable[str],
    *,
    table: str,
    exceptions: Mapping[str, str] | None = None,
    extras: Mapping[str, str] | None = None,
) -> list[str]:
    """Run :func:`parity_failures` against the ``GenerationKind`` axis."""
    return parity_failures(
        KINDS,
        keys,
        table=table,
        axis_name="GenerationKind",
        axis_location=LOC["GenerationKind"],
        exceptions=exceptions,
        extras=extras,
    )


# --- documented exception / extra sets ------------------------------------------
# Every entry carries a one-line reason; test_every_documented_reason_is_nonempty
# audits them, and the detector self-drains any entry that goes stale.

DISPLAY_NAME_EXCEPTIONS: Mapping[str, str] = {
    "report": "report display names are per-format (_REPORT_DISPLAY: briefing document / "
    "study guide / blog post / custom report), so the kind has no single row here",
}

TYPICAL_DURATION_EXCEPTIONS: Mapping[str, str] = {
    "revise-slide": "no empirical duration hint recorded; the spinner falls back "
    "gracefully to kind + elapsed seconds (missing keys are tolerated by design)",
}

DOWNLOAD_SPEC_EXCEPTIONS: Mapping[str, str] = {
    "cinematic-video": "downloads as a video (shared VIDEO artifact type); the CLI "
    "'download cinematic-video' leaf is a pure Click alias registered in download_cmd.py, "
    "deliberately not a DOWNLOAD_SPECS row",
    "revise-slide": "an operation on an existing SLIDE_DECK artifact, not a "
    "downloadable artifact type",
}

KIND_TO_ARTIFACT_TYPE_EXCEPTIONS: Mapping[str, str] = {
    "cinematic-video": "a video *format*, not a wire artifact type — shares "
    "ArtifactType.VIDEO with the 'video' kind",
    "revise-slide": "an operation on an existing SLIDE_DECK artifact, not an "
    "artifact type of its own",
}

ARTIFACT_TYPE_EXTRAS: Mapping[str, str] = {
    "UNKNOWN": "decoder fallback for unrecognized wire types; not a generable kind",
}

ARTIFACT_TYPE_CODE_EXCEPTIONS: Mapping[str, str] = {
    "FLASHCARDS": "shares wire code 4 with QUIZ; distinguished by FLASHCARDS_VARIANT "
    "at artifact_data[9][1][0], so it has no ArtifactTypeCode member of its own",
}

ARTIFACT_TYPE_CODE_EXTRAS: Mapping[str, str] = {
    "QUIZ_FLASHCARD": "backward-compatibility alias for QUIZ (= 4), kept for callers "
    "that imported the old name",
}

CODE_MAP_EXCEPTIONS: Mapping[str, str] = {
    "QUIZ": "type-4 family is resolved by variant in _map_artifact_kind, deliberately "
    "absent from the plain code->type map",
    "FLASHCARDS": "type-4 family is resolved by variant in _map_artifact_kind, "
    "deliberately absent from the plain code->type map",
}

FACADE_GENERATE_EXTRAS: Mapping[str, str] = {
    "generate_study_guide": "convenience wrapper delegating to "
    "generate_report(ReportFormat.STUDY_GUIDE); not a GenerationKind of its own",
}

#: Per-kind RPC payload builders in ``_artifact/payloads.py``. mind-map has TWO
#: first-class backings (note-backed GENERATE_MIND_MAP + interactive studio
#: CREATE_ARTIFACT variant) — both stay, neither is deprecated. revise-slide's
#: builder has no "_artifact_" infix because it revises rather than creates.
KIND_TO_PAYLOAD_BUILDERS: Mapping[str, tuple[str, ...]] = {
    "audio": ("build_audio_artifact_params",),
    "video": ("build_video_artifact_params",),
    "cinematic-video": ("build_cinematic_video_artifact_params",),
    "slide-deck": ("build_slide_deck_artifact_params",),
    "revise-slide": ("build_revise_slide_params",),
    "quiz": ("build_quiz_artifact_params",),
    "flashcards": ("build_flashcards_artifact_params",),
    "infographic": ("build_infographic_artifact_params",),
    "data-table": ("build_data_table_artifact_params",),
    "mind-map": ("build_mind_map_params", "build_interactive_mind_map_artifact_params"),
    "report": ("build_report_artifact_params",),
}

PAYLOAD_BUILDER_EXTRAS: Mapping[str, str] = {
    "build_retry_artifact_params": "retries an existing artifact by id; not kind-keyed",
    "build_suggest_reports_params": "AI report-topic suggestions; not a generation kind",
}

#: KNOWN BUG BASELINE (FIXME — see module docstring): the per-kind duration-hint
#: behavior OBSERVED by running the REAL ``execute_generation`` end-to-end
#: (real ``build_generation_plan`` plan, stub client) and parsing the spinner
#: message it emits into the injected ``wait_context`` — classified against the
#: hint ``_TYPICAL_DURATIONS`` *intends* for that kind:
#:
#: * ``correct``          — the intended hint renders.
#: * ``missing``          — an intended hint never renders (FIXME: the kind-named
#:                          key can't match the display-name lookup).
#: * ``wrong-hint``       — a different kind's hint renders (FIXME:
#:                          cinematic-video displays as "video" and gets the
#:                          standard-video estimate).
#: * ``no-hint-intended`` — no hint recorded; graceful fallback (revise-slide).
#: * ``never-waits``      — the kind never enters the wait loop (mind-map renders
#:                          synchronously; FIXME: its hint key is dead weight).
#:
#: Behavioral on purpose: ANY fix path — re-keying ``_TYPICAL_DURATIONS`` by
#: display names, fixing the lookup inside ``_format_status_message``, or
#: passing ``plan.kind`` at the executor call site — changes the emitted
#: message, flips outcomes to ``correct``, and fails this pin, forcing the
#: baseline to drain.
EXPECTED_DURATION_HINT_BEHAVIOR: Mapping[str, str] = {
    "audio": "correct",
    "video": "correct",
    "cinematic-video": "wrong-hint",  # FIXME: shows the standard-video hint
    "slide-deck": "missing",  # FIXME: key "slide-deck" vs display "slide deck"
    "revise-slide": "no-hint-intended",
    "quiz": "correct",
    "flashcards": "correct",
    "infographic": "correct",
    "data-table": "missing",  # FIXME: key "data-table" vs display "data table"
    "mind-map": "never-waits",  # FIXME: hint key exists but the kind never waits
    "report": "missing",  # FIXME: key "report" vs per-format displays
}

ALL_DOCUMENTED_REASON_TABLES: Mapping[str, Mapping[str, str]] = {
    "DISPLAY_NAME_EXCEPTIONS": DISPLAY_NAME_EXCEPTIONS,
    "TYPICAL_DURATION_EXCEPTIONS": TYPICAL_DURATION_EXCEPTIONS,
    "DOWNLOAD_SPEC_EXCEPTIONS": DOWNLOAD_SPEC_EXCEPTIONS,
    "KIND_TO_ARTIFACT_TYPE_EXCEPTIONS": KIND_TO_ARTIFACT_TYPE_EXCEPTIONS,
    "ARTIFACT_TYPE_EXTRAS": ARTIFACT_TYPE_EXTRAS,
    "ARTIFACT_TYPE_CODE_EXCEPTIONS": ARTIFACT_TYPE_CODE_EXCEPTIONS,
    "ARTIFACT_TYPE_CODE_EXTRAS": ARTIFACT_TYPE_CODE_EXTRAS,
    "CODE_MAP_EXCEPTIONS": CODE_MAP_EXCEPTIONS,
    "FACADE_GENERATE_EXTRAS": FACADE_GENERATE_EXTRAS,
    "PAYLOAD_BUILDER_EXTRAS": PAYLOAD_BUILDER_EXTRAS,
}


# --- axis sanity ----------------------------------------------------------------


def test_axis_floor_holds() -> None:
    """The derived axis has at least the known 11 kinds (anti-vacuity).

    If ``typing.get_args(GenerationKind)`` ever returns fewer, the derivation
    (or the Literal itself) broke and every downstream parity check would be
    comparing against a phantom axis — fail here first, loudly. Retiring a
    kind on purpose means lowering KNOWN_KIND_FLOOR in the same commit.
    """
    assert len(KINDS) >= KNOWN_KIND_FLOOR, (
        f"GenerationKind ({LOC['GenerationKind']}) yielded only {len(KINDS)} kinds "
        f"(< floor {KNOWN_KIND_FLOOR}): {sorted(KINDS)}. The axis derivation is broken, "
        f"or a kind was retired — if deliberate, lower KNOWN_KIND_FLOOR here on purpose."
    )
    assert all(isinstance(k, str) and k for k in KINDS)


# --- per-table parity gates -------------------------------------------------------


def test_plan_builders_cover_every_kind() -> None:
    """``_BUILDERS`` (plan construction) covers the axis exactly — no exceptions.

    ``build_generation_plan`` raises ``Unknown generation kind`` for any kind
    missing here, so a gap is an immediate runtime break for that kind.
    """
    _assert_parity(_check_kind_table(_BUILDERS, table=f"_BUILDERS ({LOC['_BUILDERS']})"))


def test_display_names_cover_every_kind_except_report() -> None:
    """``_DISPLAY_NAME`` covers the axis minus the per-format ``report`` kind."""
    _assert_parity(
        _check_kind_table(
            _DISPLAY_NAME,
            table=f"_DISPLAY_NAME ({LOC['_DISPLAY_NAME']})",
            exceptions=DISPLAY_NAME_EXCEPTIONS,
        )
    )


def test_executor_dispatch_covers_every_kind() -> None:
    """``_KIND_TO_METHOD`` covers the axis exactly AND every value is a facade method.

    The executor does an unguarded ``_KIND_TO_METHOD[plan.kind]`` then
    ``getattr(client.artifacts, name)`` — a missing key or a dangling method
    name is a guaranteed KeyError/AttributeError for that kind.
    """
    table = f"_KIND_TO_METHOD ({LOC['_KIND_TO_METHOD']})"
    _assert_parity(_check_kind_table(_KIND_TO_METHOD, table=table))

    dangling = sorted(
        f"{kind!r} -> {method!r}"
        for kind, method in _KIND_TO_METHOD.items()
        if not inspect.iscoroutinefunction(getattr(ArtifactsAPI, method, None))
    )
    assert dangling == [], (
        f"{table} maps kind(s) to names that are not async methods on ArtifactsAPI "
        f"({LOC['ArtifactsAPI']}): " + ", ".join(dangling)
    )


def test_duration_hints_cover_every_kind_except_revise_slide() -> None:
    """``_TYPICAL_DURATIONS`` covers the axis minus ``revise-slide`` (graceful fallback)."""
    _assert_parity(
        _check_kind_table(
            _TYPICAL_DURATIONS,
            table=f"_TYPICAL_DURATIONS ({LOC['_TYPICAL_DURATIONS']})",
            exceptions=TYPICAL_DURATION_EXCEPTIONS,
        )
    )


class _StubArtifactsAPI:
    """Stub ``client.artifacts``: every dispatch target starts a pending task.

    Method names are derived from the REAL ``_KIND_TO_METHOD`` so a future
    kind is stubbed automatically; ``wait_for_completion`` resolves the task
    so ``handle_generation_result``'s wait branch runs to completion.
    """

    def __init__(self) -> None:
        async def _start(*_args: Any, **_kwargs: Any) -> GenerationStatus:
            return GenerationStatus(task_id="task-1", status="pending")

        for method_name in set(_KIND_TO_METHOD.values()):
            setattr(self, method_name, _start)

    async def wait_for_completion(
        self, _notebook_id: str, task_id: str, **_kwargs: Any
    ) -> GenerationStatus:
        return GenerationStatus(task_id=task_id, status="completed")


class _StubMindMapsAPI:
    async def generate(self, *_args: Any, **_kwargs: Any) -> object:
        return object()


class _StubClient:
    def __init__(self) -> None:
        self.artifacts = _StubArtifactsAPI()
        self.mind_maps = _StubMindMapsAPI()


#: Kind-specific raw args ``build_generation_plan`` requires (the values the
#: Click layer would default to). Kinds absent here need only the common keys.
_EXECUTOR_RAW_ARGS: Mapping[str, dict[str, Any]] = {
    "audio": {"audio_format": "deep-dive", "audio_length": "default"},
    "slide-deck": {"deck_format": "detailed", "deck_length": "default"},
    "revise-slide": {"artifact_id": "artifact-1", "slide_index": 1},
    "quiz": {"quantity": "standard", "difficulty": "medium"},
    "flashcards": {"quantity": "standard", "difficulty": "medium"},
    "infographic": {"orientation": "landscape", "detail": "standard", "style": "auto"},
}


def _plan_variants(kind: str) -> list[dict[str, Any]]:
    """Raw-arg variants to drive per kind — report fans out over its formats."""
    if kind != "report":
        return [dict(_EXECUTOR_RAW_ARGS.get(kind, {}))]
    variants: list[dict[str, Any]] = [
        {"report_format": fmt} for fmt in _REPORT_DISPLAY if fmt != "custom"
    ]
    variants.append({"description": "custom report prompt"})  # smart-custom path
    return variants


def _executor_wait_message(kind: str, extra_args: dict[str, Any]) -> str | None:
    """Run the REAL executor for ``kind``; return the spinner message it emits.

    Builds the plan via the real ``build_generation_plan`` and awaits the real
    ``execute_generation`` against a stub client, capturing the first argument
    of the injected ``wait_context`` — exactly the string a user's spinner
    shows. ``None`` means the kind never entered the wait loop.
    """
    raw_args: dict[str, Any] = {"notebook_id": "nb-1", "wait": True, **extra_args}
    plan = build_generation_plan(kind, raw_args)
    captured: list[str] = []

    def wait_context(message: str, _resume_hint: str) -> contextlib.nullcontext[None]:
        captured.append(message)
        return contextlib.nullcontext()

    async def _resolve_notebook(_client: Any, notebook_id: str, **_kw: Any) -> str:
        return notebook_id

    async def _resolve_sources(_client: Any, _nb: str, source_ids: Any, **_kw: Any) -> Any:
        return list(source_ids) or None

    asyncio.run(
        execute_generation(
            plan,
            _StubClient(),  # type: ignore[arg-type]
            notebook_resolver=_resolve_notebook,
            source_resolver=_resolve_sources,
            wait_context=wait_context,
            mind_map_context=contextlib.nullcontext,
        )
    )
    return captured[0] if captured else None


def _hint_from_message(message: str) -> str | None:
    """Parse the parenthesized duration hint out of a spinner status message."""
    match = re.search(r" generation \((.+)\)\.\.\.$", message)
    return match.group(1) if match else None


def _classify_hint(intended: str | None, observed: str | None) -> str:
    """Classify one kind's hint behavior: intended (per ``_TYPICAL_DURATIONS``) vs observed."""
    if intended is None:
        return "no-hint-intended" if observed is None else "unintended-hint"
    if observed is None:
        return "missing"
    return "correct" if observed == intended else "wrong-hint"


def _hint_behavior(kind: str) -> str:
    """Observed duration-hint behavior for ``kind`` through the production chain.

    Runs the real executor for every plan variant of the kind and classifies
    the emitted spinner message(s). ``never-waits`` means no variant entered
    the wait loop; ``inconsistent`` (never expected) means variants disagreed.
    """
    messages = [_executor_wait_message(kind, extra) for extra in _plan_variants(kind)]
    if all(m is None for m in messages):
        return "never-waits"
    if any(m is None for m in messages):
        return "inconsistent"
    observed = {_hint_from_message(m) for m in messages if m is not None}
    if len(observed) != 1:
        return "inconsistent"
    return _classify_hint(_TYPICAL_DURATIONS.get(kind), observed.pop())


def test_duration_hint_behavior_baseline_known_bug() -> None:
    """FIXME baseline: per-kind hint behavior is pinned BEHAVIORALLY (keying bug).

    ``_format_status_message`` looks hints up by the plan's *display name*
    (``execute_generation`` passes ``plan.display_name`` into
    ``handle_generation_result``), but ``_TYPICAL_DURATIONS`` is keyed by
    *kind* names. So slide-deck/data-table/report waits render no hint and
    cinematic-video renders the standard-video hint. This is REAL,
    pre-existing drift — baselined here (guardrail-only change), not silently
    fixed.

    The pin is computed by running the REAL ``execute_generation`` end-to-end
    (real plan builder, stub client) and parsing the spinner message it
    actually emits, so it self-drains on ANY fix path: re-keying
    ``_TYPICAL_DURATIONS`` by display names, fixing the lookup inside
    ``_format_status_message``, or passing ``plan.kind`` at the executor call
    site — each changes the emitted message, flips outcomes to "correct", and
    fails this pin; update EXPECTED_DURATION_HINT_BEHAVIOR (drain the FIXMEs)
    in the fix commit. A NEW "missing"/"wrong-hint" means a new hint shipped
    with the same bug — fix it instead of baselining more debt.
    """
    # The pin itself is axis-keyed: a new kind must take an explicit position.
    _assert_parity(
        _check_kind_table(
            EXPECTED_DURATION_HINT_BEHAVIOR,
            table="EXPECTED_DURATION_HINT_BEHAVIOR (this guardrail)",
        )
    )
    observed = {kind: _hint_behavior(kind) for kind in sorted(KINDS)}
    assert observed == dict(EXPECTED_DURATION_HINT_BEHAVIOR), (
        f"Per-kind duration-hint behavior changed (_TYPICAL_DURATIONS at "
        f"{LOC['_TYPICAL_DURATIONS']}, lookup in _format_status_message).\n"
        f"  baselined: {dict(sorted(EXPECTED_DURATION_HINT_BEHAVIOR.items()))}\n"
        f"  observed:  {observed}\n"
        "missing/wrong-hint -> correct: the keying bug was (partly) FIXED — drain "
        "those FIXME entries from EXPECTED_DURATION_HINT_BEHAVIOR.\n"
        "correct -> missing/wrong-hint: a hint regressed — key the entry so the "
        "production lookup (display name today) actually reaches it.\n"
        "Fix paths that drain this baseline: re-key _TYPICAL_DURATIONS by display "
        "names, or pass plan.kind through to the lookup instead of plan.display_name."
    )


def test_cli_generate_group_has_a_leaf_per_kind() -> None:
    """The hand-written ``generate <kind>`` Click leaves cover the axis exactly.

    ``cinematic-video`` is a real leaf (an alias command that re-dispatches via
    ``ctx.info_name``), so no exceptions apply: a new kind must ship its leaf.
    """
    _assert_parity(
        _check_kind_table(
            generate_group.commands,
            table=f"the 'generate' Click group ({LOC['generate group']})",
        )
    )


def test_download_specs_cover_every_downloadable_kind() -> None:
    """``DOWNLOAD_SPECS`` rows cover the axis minus the two non-downloadable kinds."""
    _assert_parity(
        _check_kind_table(
            {spec.name for spec in DOWNLOAD_SPECS},
            table=f"DOWNLOAD_SPECS ({LOC['DOWNLOAD_SPECS']})",
            exceptions=DOWNLOAD_SPEC_EXCEPTIONS,
        )
    )


def test_download_specs_are_internally_sound() -> None:
    """Each spec row binds a real facade coroutine and a distinct ArtifactType.

    The spec ``kind`` set must equal ArtifactType minus UNKNOWN — i.e. every
    listable artifact type is downloadable. A duplicate name/kind or a
    dangling ``download_attr`` is registry drift.
    """
    table = f"DOWNLOAD_SPECS ({LOC['DOWNLOAD_SPECS']})"
    names = [spec.name for spec in DOWNLOAD_SPECS]
    assert len(names) == len(set(names)), f"duplicate spec names in {table}: {sorted(names)}"
    # Kind uniqueness must be asserted directly: the parity check below
    # set-ifies ``spec.kind``, so two rows pointing at the same ArtifactType
    # would otherwise pass as long as every type appears at least once.
    kinds = [spec.kind.name for spec in DOWNLOAD_SPECS]
    assert len(kinds) == len(set(kinds)), f"duplicate spec kinds in {table}: {sorted(kinds)}"

    dangling = sorted(
        f"{spec.name!r} -> {spec.download_attr!r}"
        for spec in DOWNLOAD_SPECS
        if not inspect.iscoroutinefunction(getattr(ArtifactsAPI, spec.download_attr, None))
    )
    assert dangling == [], (
        f"{table} binds download_attr(s) that are not async methods on ArtifactsAPI "
        f"({LOC['ArtifactsAPI']}): " + ", ".join(dangling)
    )

    _assert_parity(
        parity_failures(
            frozenset(ArtifactType.__members__),
            {spec.kind.name for spec in DOWNLOAD_SPECS},
            table=f"{table} 'kind' column",
            axis_name="ArtifactType",
            axis_location=LOC["ArtifactType"],
            exceptions=ARTIFACT_TYPE_EXTRAS,  # UNKNOWN is not downloadable
        )
    )


def test_cli_download_group_matches_specs_plus_alias() -> None:
    """The ``download`` Click leaves are the spec rows plus the cinematic-video alias.

    Leaves are generated from DOWNLOAD_SPECS, so the live risk is the alias
    wiring and any future hand-added leaf bypassing the registry.
    """
    expected = {spec.name for spec in DOWNLOAD_SPECS} | {"cinematic-video"}
    _assert_parity(
        parity_failures(
            frozenset(expected),
            download_group.commands,
            table=f"the 'download' Click group ({LOC['download registration']})",
            axis_name="DOWNLOAD_SPECS names + the cinematic-video alias",
            axis_location=LOC["DOWNLOAD_SPECS"],
        )
    )


def test_artifact_type_enum_matches_kind_axis() -> None:
    """``ArtifactType`` members are exactly the wire-typed kinds (+ UNKNOWN).

    Derived by the naming convention ``kind.replace('-', '_').upper()`` over
    the axis minus the two kinds that aren't artifact types of their own.
    """
    derived = frozenset(
        kind.replace("-", "_").upper() for kind in KINDS - set(KIND_TO_ARTIFACT_TYPE_EXCEPTIONS)
    )
    _assert_parity(
        parity_failures(
            derived,
            set(ArtifactType.__members__),
            table=f"ArtifactType ({LOC['ArtifactType']})",
            axis_name="GenerationKind-derived artifact-type names",
            axis_location=LOC["GenerationKind"],
            extras=ARTIFACT_TYPE_EXTRAS,
        )
    )
    # Keep the derivation's own exception set honest against the axis.
    _assert_parity(
        _check_kind_table(
            KINDS - set(KIND_TO_ARTIFACT_TYPE_EXCEPTIONS),
            table=f"the kind->ArtifactType derivation (this guardrail; enum at "
            f"{LOC['ArtifactType']})",
            exceptions=KIND_TO_ARTIFACT_TYPE_EXCEPTIONS,
        )
    )


def test_artifact_type_code_enum_parity() -> None:
    """``ArtifactTypeCode`` names mirror ArtifactType minus FLASHCARDS/UNKNOWN.

    FLASHCARDS shares wire code 4 with QUIZ (variant-resolved); QUIZ_FLASHCARD
    is a kept back-compat alias.
    """
    _assert_parity(
        parity_failures(
            frozenset(ArtifactType.__members__) - set(ARTIFACT_TYPE_EXTRAS),
            set(ArtifactTypeCode.__members__),
            table=f"ArtifactTypeCode ({LOC['ArtifactTypeCode']})",
            axis_name="ArtifactType (minus UNKNOWN)",
            axis_location=LOC["ArtifactType"],
            exceptions=ARTIFACT_TYPE_CODE_EXCEPTIONS,
            extras=ARTIFACT_TYPE_CODE_EXTRAS,
        )
    )


def test_artifact_type_code_map_parity() -> None:
    """``_ARTIFACT_TYPE_CODE_MAP`` decodes every wire type except the variant family.

    Checks all three dimensions: the mapped ArtifactType VALUES (axis parity),
    the integer KEYS (exactly the matching ``ArtifactTypeCode`` wire values,
    minus the variant-resolved code 4), and the per-entry PAIRING (each code
    decodes to the same-named type) — so a stale or transposed integer key
    cannot hide behind a correct-looking value set.
    """
    table = f"_ARTIFACT_TYPE_CODE_MAP ({LOC['_ARTIFACT_TYPE_CODE_MAP']})"
    _assert_parity(
        parity_failures(
            frozenset(ArtifactType.__members__) - set(ARTIFACT_TYPE_EXTRAS),
            {member.name for member in _ARTIFACT_TYPE_CODE_MAP.values()},
            table=table,
            axis_name="ArtifactType (minus UNKNOWN)",
            axis_location=LOC["ArtifactType"],
            exceptions=CODE_MAP_EXCEPTIONS,
        )
    )

    # Key parity: the wire code 4 family (QUIZ + its QUIZ_FLASHCARD alias) is
    # variant-resolved in _map_artifact_kind (per CODE_MAP_EXCEPTIONS); every
    # other ArtifactTypeCode value must appear as a key, and nothing else may.
    variant_family = {"QUIZ", "QUIZ_FLASHCARD"}
    expected_codes = {
        member.value: name
        for name, member in ArtifactTypeCode.__members__.items()
        if name not in variant_family
    }
    assert set(_ARTIFACT_TYPE_CODE_MAP) == set(expected_codes), (
        f"{table} integer keys drifted from ArtifactTypeCode ({LOC['ArtifactTypeCode']}) "
        f"values (minus the variant-resolved code 4 family).\n"
        f"  missing key(s): {sorted(set(expected_codes) - set(_ARTIFACT_TYPE_CODE_MAP))}\n"
        f"  stale key(s):   {sorted(set(_ARTIFACT_TYPE_CODE_MAP) - set(expected_codes))}\n"
        "Update the map's keys (or ArtifactTypeCode) so the wire codes agree."
    )
    mispaired = sorted(
        f"{code} -> {_ARTIFACT_TYPE_CODE_MAP[code].name!r} (ArtifactTypeCode.{name} = {code})"
        for code, name in expected_codes.items()
        if _ARTIFACT_TYPE_CODE_MAP[code].name != name
    )
    assert mispaired == [], (
        f"{table} pairs wire code(s) with the WRONG ArtifactType — the key set looks "
        f"right but an entry is transposed:\n" + "\n".join(f"  {m}" for m in mispaired)
    )


def test_artifact_list_type_filter_covers_every_artifact_type() -> None:
    """``artifact list --type`` choices map onto every ArtifactType (minus UNKNOWN).

    Choices route through ``cli_name_to_artifact_type`` (which owns the
    singular 'flashcard' alias); 'all' is the no-filter sentinel. A dead
    choice (mapping to None) or an unfilterable type is drift.
    """
    table = f"the 'artifact list --type' Choice ({LOC['artifact list --type']})"
    list_cmd = artifact_cmd.artifact.commands["list"]
    (type_param,) = [p for p in list_cmd.params if p.name == "artifact_type"]
    choices = set(type_param.type.choices)  # type: ignore[attr-defined]
    assert "all" in choices, f"{table} lost its 'all' (no-filter) sentinel"

    mapped: dict[str, ArtifactType | None] = {
        c: cli_name_to_artifact_type(c) for c in choices - {"all"}
    }
    dead = sorted(c for c, t in mapped.items() if t is None)
    assert dead == [], (
        f"{table} offers choice(s) that cli_name_to_artifact_type cannot map to an "
        f"ArtifactType ({LOC['ArtifactType']}): {dead}. Fix the choice name or add "
        f"the alias in cli/rendering.py."
    )
    _assert_parity(
        parity_failures(
            frozenset(ArtifactType.__members__) - set(ARTIFACT_TYPE_EXTRAS),
            {t.name for t in mapped.values() if t is not None},
            table=table,
            axis_name="ArtifactType (minus UNKNOWN)",
            axis_location=LOC["ArtifactType"],
        )
    )


def test_payload_builders_exist_per_kind() -> None:
    """Every kind has its RPC payload builder(s) in ``_artifact/payloads.py``.

    The KIND_TO_PAYLOAD_BUILDERS registry above is keyed by the axis (so a new
    kind fails here until its builder is named) and every named builder must
    exist; conversely every ``build_*`` function in the module must be claimed
    by a kind or documented as an extra — a new builder with no kind is drift.
    """
    payloads = _artifact.payloads
    table = f"KIND_TO_PAYLOAD_BUILDERS (this guardrail; builders in {LOC['payloads module']})"
    _assert_parity(_check_kind_table(KIND_TO_PAYLOAD_BUILDERS, table=table))

    missing = sorted(
        f"{kind!r} -> {name!r}"
        for kind, names in KIND_TO_PAYLOAD_BUILDERS.items()
        for name in names
        if not callable(getattr(payloads, name, None))
    )
    assert missing == [], (
        f"{table} names builder(s) absent from src/notebooklm/_artifact/payloads.py: "
        + ", ".join(missing)
    )

    claimed = {name for names in KIND_TO_PAYLOAD_BUILDERS.values() for name in names}
    actual = {n for n in dir(payloads) if n.startswith("build_") and callable(getattr(payloads, n))}
    _assert_parity(
        parity_failures(
            frozenset(claimed),
            actual,
            table="build_* functions in src/notebooklm/_artifact/payloads.py",
            axis_name="kind-claimed payload builders",
            axis_location="KIND_TO_PAYLOAD_BUILDERS in this guardrail",
            extras=PAYLOAD_BUILDER_EXTRAS,
        )
    )


def test_facade_generate_method_set_matches_dispatch() -> None:
    """Public ``ArtifactsAPI.generate_*`` methods == executor targets + documented extras.

    The reverse direction of test_executor_dispatch_covers_every_kind: a NEW
    ``generate_<x>`` facade method that no GenerationKind dispatches to fails
    here until the kind (and its tables) exist or the method is documented as
    a deliberate convenience wrapper. (``revise_slide`` is dispatch-checked
    too; it just doesn't carry the ``generate_`` prefix.)
    """
    expected = {m for m in _KIND_TO_METHOD.values() if m.startswith("generate_")}
    actual = {
        name
        for name, _ in inspect.getmembers(ArtifactsAPI, inspect.iscoroutinefunction)
        if name.startswith("generate_")
    }
    _assert_parity(
        parity_failures(
            frozenset(expected),
            actual,
            table=f"public generate_* methods on ArtifactsAPI ({LOC['ArtifactsAPI']})",
            axis_name="_KIND_TO_METHOD generate_* targets",
            axis_location=LOC["_KIND_TO_METHOD"],
            extras=FACADE_GENERATE_EXTRAS,
        )
    )


def test_facade_download_method_set_matches_specs() -> None:
    """Public ``ArtifactsAPI.download_*`` methods == the DOWNLOAD_SPECS bindings.

    The reverse direction of test_download_specs_are_internally_sound: a NEW
    ``download_<x>`` facade coroutine with no spec row would ship an API the
    CLI cannot reach — add the registry row (one edit) or document why not.
    """
    expected = {spec.download_attr for spec in DOWNLOAD_SPECS}
    actual = {
        name
        for name, _ in inspect.getmembers(ArtifactsAPI, inspect.iscoroutinefunction)
        if name.startswith("download_")
    }
    _assert_parity(
        parity_failures(
            frozenset(expected),
            actual,
            table=f"public download_* methods on ArtifactsAPI ({LOC['ArtifactsAPI']})",
            axis_name="DOWNLOAD_SPECS download_attr bindings",
            axis_location=LOC["DOWNLOAD_SPECS"],
        )
    )


# --- guardrail self-maintenance ---------------------------------------------------


def test_locators_resolve() -> None:
    """Every file:line pointer used in failure messages still anchors to real code."""
    broken = sorted(f"{name}: {loc}" for name, loc in LOC.items() if "NOT FOUND" in loc)
    assert broken == [], (
        "Locator pattern(s) no longer match their source — re-anchor them so failure "
        "messages keep pointing at the right table:\n" + "\n".join(f"  {b}" for b in broken)
    )


def test_every_documented_reason_is_nonempty() -> None:
    """Every documented exception/extra carries a real one-line reason.

    A blank reason is an undocumented exception in disguise; the sets exist to
    make every legitimate gap auditable.
    """
    blank = sorted(
        f"{table}[{key!r}]"
        for table, mapping in ALL_DOCUMENTED_REASON_TABLES.items()
        for key, reason in mapping.items()
        if not (isinstance(reason, str) and reason.strip())
    )
    assert blank == [], "Documented exception(s)/extra(s) with empty reasons:\n" + "\n".join(
        f"  {b}" for b in blank
    )


# --- self-checks: prove the detector bites -----------------------------------------


def test_detector_catches_a_planted_missing_kind() -> None:
    """A real table with one kind deleted is caught, naming the kind AND the table.

    Drives the SAME detector the live gates run over a copy of the real
    executor dispatch table with 'data-table' removed — the exact drift shape
    this gate exists to prevent.
    """
    planted = dict(_KIND_TO_METHOD)
    planted.pop("data-table", None)
    table = f"_KIND_TO_METHOD ({LOC['_KIND_TO_METHOD']})"
    failures = _check_kind_table(planted, table=table)
    assert any("'data-table'" in f and "_KIND_TO_METHOD" in f for f in failures), failures
    # The teaching message points at the real file:line to update.
    assert any("_app/generate.py:" in f for f in failures), failures


def test_detector_catches_an_unknown_key() -> None:
    """A table key that is not an axis member (nor a documented extra) is caught."""
    planted = {*KINDS, "holographic-sandwich"}
    failures = _check_kind_table(planted, table="planted table")
    assert any("'holographic-sandwich'" in f and "not a GenerationKind" in f for f in failures), (
        failures
    )


def test_detector_flags_stale_exception_and_extra() -> None:
    """Self-draining proof: a covered exception and an absent extra both fail.

    An exception whose kind IS now in the table must be drained; so must an
    extra that vanished and an exception naming a retired kind. This is what
    keeps the documented sets shrink-only.
    """
    covered = _check_kind_table(
        KINDS,  # full coverage, yet 'report' is excepted -> stale
        table="planted table",
        exceptions={"report": "stale reason"},
    )
    assert any("stale" in f and "'report'" in f for f in covered), covered

    retired = _check_kind_table(
        KINDS,
        table="planted table",
        exceptions={"retired-kind": "kind no longer exists"},
    )
    assert any("no longer exists" in f and "'retired-kind'" in f for f in retired), retired

    gone_extra = parity_failures(
        frozenset({"a"}),
        {"a"},
        table="planted table",
        axis_name="axis",
        axis_location="here",
        extras={"vanished": "was once allowed"},
    )
    assert any("'vanished'" in f and "no longer present" in f for f in gone_extra), gone_extra


def test_detector_flags_extra_that_joined_the_axis() -> None:
    """Self-draining proof: a documented extra that became an axis member fails.

    The shape this catches live: if ``generate_study_guide`` ever became a real
    ``_KIND_TO_METHOD`` target (i.e. joined the axis the facade set is checked
    against), the FACADE_GENERATE_EXTRAS entry would be stale — the detector
    must say so instead of silently passing while the entry rots.
    """
    failures = parity_failures(
        frozenset({"a", "b"}),
        {"a", "b"},
        table="planted table",
        axis_name="axis",
        axis_location="here",
        extras={"b": "documented as an extra, but now an axis member"},
    )
    assert any("'b'" in f and "JOINED" in f for f in failures), failures
    # ...and the same entry while still genuinely extra raises nothing.
    assert (
        parity_failures(
            frozenset({"a"}),
            {"a", "b"},
            table="planted table",
            axis_name="axis",
            axis_location="here",
            extras={"b": "genuinely beyond the axis"},
        )
        == []
    )


def test_hint_classifier_covers_all_categories() -> None:
    """The behavioral-baseline classifier distinguishes every category it pins.

    Pure-input proof that the categories EXPECTED_DURATION_HINT_BEHAVIOR pins
    are actually computable and distinct — including 'unintended-hint', the
    never-expected shape that would mean a hint renders with no intended entry.
    """
    assert _classify_hint(None, None) == "no-hint-intended"
    assert _classify_hint(None, "typically 1 min") == "unintended-hint"
    assert _classify_hint("typically 1 min", None) == "missing"
    assert _classify_hint("typically 1 min", "typically 1 min") == "correct"
    assert _classify_hint("typically 1 min", "typically 9 min") == "wrong-hint"
    # The extractor reads the REAL formatter output shapes: hint and no-hint.
    assert _hint_from_message(_format_status_message("audio")) == _TYPICAL_DURATIONS["audio"]
    assert _hint_from_message(_format_status_message("slide deck")) is None


def test_detector_accepts_a_clean_table() -> None:
    """A table in exact parity (with honest exceptions/extras) yields no failures."""
    assert (
        parity_failures(
            frozenset({"a", "b", "c"}),
            {"a", "b", "x"},
            table="clean planted table",
            axis_name="axis",
            axis_location="here",
            exceptions={"c": "legitimately uncovered"},
            extras={"x": "legitimately extra"},
        )
        == []
    )
