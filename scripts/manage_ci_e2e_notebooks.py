#!/usr/bin/env python3
"""Provision, validate, reconcile, and delete disposable CI E2E notebooks.

Notebook IDs are sensitive resource handles.  This command writes them only to
the local manifest and ``GITHUB_ENV``; human-facing diagnostics contain roles,
counts, and categorized errors but never IDs, titles, or response bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from _ci_e2e_notebooks import (
    ACCOUNT_SLOTS,
    BACKENDS,
    LANES,
    MANIFEST_REPOSITORY,
    MODE_ROLES,
    RESERVED_PREFIX,
    AtomicJSONStore,
    ManifestError,
    atomic_append_lines,
    build_title,
    github_env_lines,
    is_valid_notebook_id,
    new_copy_row,
    new_manifest,
    parse_title,
    validate_manifest,
)

from notebooklm import (
    AuthError,
    ChatError,
    MindMapKind,
    NetworkError,
    NotebookLMClient,
    NotebookNotFoundError,
    RateLimitError,
    ServerError,
    SharePermission,
)
from notebooklm._logging import scrub_secrets

TEMPLATE_ID_ENV = "NOTEBOOKLM_E2E_TEMPLATE_NOTEBOOK_ID"
DEFAULT_TEMPLATE_CONTRACT = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "e2e_template_contract.json"
)
DEFAULT_PREPARED_CONTRACT = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "e2e_prepared_role_contract.json"
)

T = TypeVar("T")

_WEB_CHAT_QUOTA_MARKERS = (
    "rate limit",
    "rate limited",
    "rate-limited",
    "rejected by the api",
    "429",
    "too many requests",
)


class LifecycleError(RuntimeError):
    """A categorized, summary-safe lifecycle failure."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class ContractError(LifecycleError):
    """The immutable or prepared-role contract is not satisfied."""

    def __init__(self, message: str) -> None:
        super().__init__("TEMPLATE_ACCESS", message)


class CopyUnresolvedError(LifecycleError):
    """One non-idempotent copy dispatch could not be safely resolved."""

    def __init__(self, message: str) -> None:
        super().__init__("COPY_UNRESOLVED", message)


class CleanupError(LifecycleError):
    """At least one copy could not be safely deleted."""

    def __init__(self, message: str) -> None:
        super().__init__("CLEANUP", message)


class PersistenceError(LifecycleError):
    """Durable lifecycle state could not be recorded."""

    def __init__(self, message: str) -> None:
        super().__init__("CONFIGURATION", message)


class ProbeState(Enum):
    VALID = "valid"
    MISSING = "missing"
    UNSAFE = "unsafe"
    UNREADABLE = "unreadable"
    DELETE_UNCONFIRMED = "delete_unconfirmed"


@dataclass(frozen=True)
class CandidateProbe:
    state: ProbeState
    notebook: Any | None = None


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 4.0

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("invalid retry policy")


@dataclass(frozen=True)
class PreparationPolicy:
    poll_interval: float = 30.0
    quiet_period: float = 90.0
    deadline: float = 300.0

    def __post_init__(self) -> None:
        if self.poll_interval <= 0 or self.quiet_period < 0 or self.deadline <= 0:
            raise ValueError("invalid preparation policy")


@dataclass(frozen=True)
class Inventory:
    artifacts: Mapping[str, Any]
    notes: Mapping[str, Any]
    mind_maps: Mapping[str, Any]

    @property
    def ids(self) -> set[str]:
        return set(self.artifacts) | set(self.notes) | set(self.mind_maps)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "artifacts": len(self.artifacts),
            "notes": len(self.notes),
            "mind_maps": len(self.mind_maps),
        }


@dataclass(frozen=True)
class SweepResult:
    eligible: int
    deleted: int
    skipped: int
    failed: int


def _safe_exception_name(exc: BaseException) -> str:
    """Return a scrubbed class name, never an exception body."""

    return scrub_secrets(type(exc).__name__).replace("\n", " ")[:100]


def _category_for(exc: BaseException) -> str:
    if isinstance(exc, LifecycleError):
        return exc.category
    if isinstance(exc, AuthError):
        return "AUTHENTICATION"
    if isinstance(exc, RateLimitError):
        return "QUOTA"
    if isinstance(exc, ChatError) and any(
        marker in str(exc).lower() for marker in _WEB_CHAT_QUOTA_MARKERS
    ):
        return "QUOTA"
    if isinstance(exc, (ManifestError, ValueError, OSError)):
        return "CONFIGURATION"
    if isinstance(exc, (NetworkError, ServerError)):
        return "REGRESSION"
    return "REGRESSION"


def _load_json(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} contract is unreadable") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ContractError(f"{label} contract has an unsupported version")
    for key in _walk_keys(value):
        if key in {"notebook_id", "account_email", "account_identity"}:
            raise ContractError(f"{label} contract contains forbidden identity data")
    return value, hashlib.sha256(raw).hexdigest()


def _walk_keys(value: object) -> Sequence[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_walk_keys(child))
    return keys


def load_template_contract(path: Path) -> tuple[dict[str, Any], str]:
    contract, fingerprint = _load_json(path, label="template")
    try:
        re.compile(contract["title_regex"])
        source = contract["sources"]
        artifacts = contract["artifacts"]
        content_policy = contract["content_policy"]
        required_families = {
            "audio",
            "video",
            "report",
            "quiz",
            "flashcards",
            "mind_map",
            "infographic",
            "slide_deck",
            "data_table",
        }
        if (
            set(contract)
            != {
                "version",
                "title_regex",
                "reserved_title_prefix",
                "sources",
                "artifacts",
                "content_policy",
            }
            or set(source)
            != {"minimum_ready", "minimum_distinct_titles", "require_text_addressable"}
            or set(artifacts) != {"required_completed_families", "require_interactive_mind_map"}
            or contract["reserved_title_prefix"] != RESERVED_PREFIX
            or not isinstance(source["minimum_ready"], int)
            or source["minimum_ready"] < 3
            or not isinstance(source["minimum_distinct_titles"], int)
            or source["minimum_distinct_titles"] < 3
            or source["require_text_addressable"] is not True
            or artifacts["require_interactive_mind_map"] is not True
            or not isinstance(artifacts["required_completed_families"], list)
            or not required_families.issubset(artifacts["required_completed_families"])
            or content_policy
            != {
                "private_personal_licensed_or_unstable_web_content_allowed": False,
                "copy_permission_required_for_all_ci_slots": True,
            }
        ):
            raise KeyError
    except (KeyError, TypeError, re.error) as exc:
        raise ContractError("template contract shape is invalid") from exc
    return contract, fingerprint


def load_prepared_contract(path: Path) -> dict[str, Any]:
    contract, _fingerprint = _load_json(path, label="prepared-role")
    try:
        reference = contract["reference"]
        clean = contract["clean_roles"]
        if (
            set(contract) != {"version", "reference", "clean_roles"}
            or set(reference)
            != {
                "note_title",
                "note_body",
                "question",
                "require_readable_note",
                "require_conversation_id",
                "require_nonempty_history_pair",
                "conversation_turn_limit",
            }
            or set(clean)
            != {
                "roles",
                "minimum_ready_sources",
                "disallowed",
                "empty_chat_roles",
                "poll_interval_seconds",
                "quiet_period_seconds",
                "preparation_deadline_seconds",
            }
            or not all(
                isinstance(reference[field], str) and reference[field]
                for field in ("note_title", "note_body", "question")
            )
            or reference["require_readable_note"] is not True
            or reference["require_conversation_id"] is not True
            or reference["require_nonempty_history_pair"] is not True
            or reference["conversation_turn_limit"] != 2
            or tuple(clean["roles"]) != ("generation", "multi-source", "rpc")
            or tuple(clean["disallowed"]) != ("artifacts", "notes", "mind_maps")
            or tuple(clean["empty_chat_roles"]) != ("multi-source", "rpc")
            or clean["minimum_ready_sources"] < 3
            or clean["poll_interval_seconds"] != 30
            or clean["quiet_period_seconds"] != 90
            or clean["preparation_deadline_seconds"] != 300
        ):
            raise KeyError
    except (KeyError, TypeError) as exc:
        raise ContractError("prepared-role contract shape is invalid") from exc
    return contract


async def retry_idempotent(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]],
) -> T:
    """Retry an idempotent read/delete a bounded number of times."""

    last_error: Exception | None = None
    for attempt in range(policy.attempts):
        try:
            return await operation()
        except AuthError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 == policy.attempts:
                raise
            delay = min(policy.base_delay * (2**attempt), policy.max_delay)
            await sleep(delay)
    assert last_error is not None
    raise last_error


def _artifact_completed(artifact: Any) -> bool:
    completed = getattr(artifact, "is_completed", None)
    if isinstance(completed, bool):
        return completed
    return getattr(artifact, "status_str", None) == "completed"


def _kind_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else "unknown"


def _turn_has_content(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    for field in ("query", "question", "answer", "text", "content", "user_query_text"):
        child = getattr(value, field, None)
        if isinstance(child, str) and child.strip():
            return True
        if isinstance(value, dict):
            child = value.get(field)
            if isinstance(child, str) and child.strip():
                return True
    if isinstance(value, (list, tuple)):
        return any(_turn_has_content(child) for child in value)
    chat_turns = getattr(value, "chat_turns", None)
    if chat_turns is not None and any(_turn_has_content(child) for child in chat_turns):
        return True
    act_on_sources = getattr(value, "act_on_sources_response", None)
    answer = getattr(act_on_sources, "response", None)
    generated = getattr(answer, "response", None)
    return isinstance(generated, str) and bool(generated.strip())


def _matching_history_pairs(history: object, question: str) -> int:
    if not isinstance(history, (list, tuple)):
        return 0
    return sum(
        1
        for row in history
        if isinstance(row, (tuple, list))
        and len(row) >= 2
        and isinstance(row[0], str)
        and row[0].strip() == question
        and isinstance(row[1], str)
        and bool(row[1].strip())
    )


class NotebookLifecycleManager:
    """Auditable public-API orchestration for one account/backend lane."""

    def __init__(
        self,
        client: Any,
        *,
        template_id: str,
        store: AtomicJSONStore,
        template_contract: Mapping[str, Any],
        prepared_contract: Mapping[str, Any],
        retry_policy: RetryPolicy = RetryPolicy(),
        reconcile_timeout: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        nonce: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        if not is_valid_notebook_id(template_id):
            raise ManifestError("template notebook ID is malformed")
        if reconcile_timeout < 0 or reconcile_timeout > 90:
            raise ValueError("reconciliation timeout must be between zero and 90 seconds")
        self.client = client
        self.template_id = template_id
        self.store = store
        self.template_contract = dict(template_contract)
        self.prepared_contract = dict(prepared_contract)
        self.retry_policy = retry_policy
        self.reconcile_timeout = reconcile_timeout
        self.clock = clock
        self.sleep = sleep
        self.nonce = nonce

    async def _read(self, operation: Callable[[], Awaitable[T]]) -> T:
        return await retry_idempotent(
            operation,
            policy=self.retry_policy,
            sleep=self.sleep,
        )

    async def _assert_chat_empty(self, notebook_id: str) -> None:
        """Confirm no readable turns without the Web history helper's soft fallback."""

        conversation_id = await self._read(
            lambda: self.client.chat.get_conversation_id(notebook_id)
        )
        if conversation_id is None:
            return
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ContractError("prepared clean role returned a malformed conversation ID")
        turns = await self._read(
            lambda: self.client.chat.get_conversation_turns(
                notebook_id,
                conversation_id,
                limit=2,
            )
        )
        if _turn_has_content(turns):
            raise ContractError("prepared clean role inherited conversation state")

    async def validate_template(self, *, include_title: bool = True) -> dict[str, int]:
        """Validate immutable copied state using public typed APIs only."""

        try:
            notebook = await self._read(lambda: self.client.notebooks.get(self.template_id))
        except NotebookNotFoundError as exc:
            # Only the top-level template lookup is an account-access contract.
            # A copied child disappearing remains a reconciliation regression.
            if include_title:
                raise ContractError("template is unavailable to the selected account") from exc
            raise
        if getattr(notebook, "id", None) != self.template_id:
            raise ContractError("template lookup returned the wrong notebook")
        if include_title:
            title = getattr(notebook, "title", None)
            if not isinstance(title, str) or title.startswith(RESERVED_PREFIX):
                raise ContractError("template title violates the immutable title contract")
            if re.fullmatch(str(self.template_contract["title_regex"]), title) is None:
                raise ContractError("template title version does not match the contract")
        sources = await self._read(lambda: self.client.sources.list(self.template_id, strict=True))
        source_contract = self.template_contract["sources"]
        ready_sources = [source for source in sources if getattr(source, "is_ready", False)]
        if source_contract["require_text_addressable"] is True:
            non_text_kinds = {"image", "media", "unknown"}
            text_ready = [
                source
                for source in ready_sources
                if _kind_value(getattr(source, "kind", None)) not in non_text_kinds
            ]
            if len(text_ready) < source_contract["minimum_ready"]:
                raise ContractError("template has too few text-addressable ready sources")
        distinct_titles = {
            str(getattr(source, "title", "")).strip().casefold()
            for source in ready_sources
            if str(getattr(source, "title", "")).strip()
        }
        if len(ready_sources) < source_contract["minimum_ready"]:
            raise ContractError("template has too few ready sources")
        if len(distinct_titles) < source_contract["minimum_distinct_titles"]:
            raise ContractError("template source topics are not sufficiently distinct")

        artifacts = await self._read(lambda: self.client.artifacts.list(self.template_id))
        completed = [artifact for artifact in artifacts if _artifact_completed(artifact)]
        families = {_kind_value(getattr(artifact, "kind", None)) for artifact in completed}
        required = set(self.template_contract["artifacts"]["required_completed_families"])
        missing = sorted(required - families)
        if missing:
            raise ContractError("template is missing completed families: " + ",".join(missing))
        if not any(getattr(artifact, "is_interactive_mind_map", False) for artifact in completed):
            raise ContractError("template is missing a completed interactive mind map")
        return {
            "ready_sources": len(ready_sources),
            "completed_artifacts": len(completed),
            "artifact_families": len(families),
        }

    async def _validate_copy_shape(self, notebook_id: str) -> dict[str, int]:
        original = self.template_id
        self.template_id = notebook_id
        try:
            return await self.validate_template(include_title=False)
        finally:
            self.template_id = original

    async def _inventory(self, notebook_id: str) -> Inventory:
        artifacts = await self._read(lambda: self.client.artifacts.list(notebook_id))
        notes = await self._read(lambda: self.client.notes.list(notebook_id))
        mind_maps = await self._read(lambda: self.client.mind_maps.list(notebook_id))

        def index(items: Sequence[Any], label: str) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for item in items:
                child_id = getattr(item, "id", None)
                if (
                    not isinstance(child_id, str)
                    or not child_id
                    or child_id.strip() != child_id
                    or any(ord(character) < 32 for character in child_id)
                ):
                    raise ContractError(f"{label} inventory contains a malformed child ID")
                if child_id in result:
                    raise ContractError(f"{label} inventory contains duplicate child IDs")
                result[child_id] = item
            return result

        return Inventory(
            artifacts=index(artifacts, "artifact"),
            notes=index(notes, "note"),
            mind_maps=index(mind_maps, "mind-map"),
        )

    async def _sources(self, notebook_id: str) -> list[Any]:
        return await self._read(lambda: self.client.sources.list(notebook_id, strict=True))

    async def _delete_child(self, operation: Callable[[], Awaitable[None]]) -> None:
        await retry_idempotent(operation, policy=self.retry_policy, sleep=self.sleep)

    async def _delete_inventory(self, notebook_id: str, inventory: Inventory) -> None:
        """Delete one fresh disallowed snapshot with backing-aware de-duplication."""

        # ``artifacts.list`` intentionally merges both mind-map backings into
        # its public inventory.  Dispatch every ID also present in
        # ``mind_maps.list`` through the backing-aware mind-map API exactly
        # once; sending a note-backed ID to ``artifacts.delete`` targets the
        # wrong RPC family.
        for child_id, mind_map in inventory.mind_maps.items():
            kind = getattr(mind_map, "kind", None)
            if kind not in {MindMapKind.NOTE_BACKED, MindMapKind.INTERACTIVE}:
                raise ContractError("copied mind-map backing is unknown")
            await self._delete_child(
                lambda child_id=child_id, kind=kind: self.client.mind_maps.delete(
                    notebook_id,
                    child_id,
                    kind=kind,
                )
            )
        for child_id in inventory.artifacts:
            if child_id in inventory.mind_maps:
                continue
            await self._delete_child(
                lambda child_id=child_id: self.client.artifacts.delete(notebook_id, child_id)
            )
        for child_id in inventory.notes:
            if child_id in inventory.mind_maps:
                continue
            await self._delete_child(
                lambda child_id=child_id: self.client.notes.delete(notebook_id, child_id)
            )

    async def prepare_clean_role(self, notebook_id: str, role: str) -> dict[str, int]:
        """Remove inherited children and prove a 90-second stable empty inventory."""

        clean = self.prepared_contract["clean_roles"]
        if role not in clean["roles"]:
            raise ContractError("role does not use clean preparation")
        policy = PreparationPolicy(
            poll_interval=float(clean["poll_interval_seconds"]),
            quiet_period=float(clean["quiet_period_seconds"]),
            deadline=float(clean["preparation_deadline_seconds"]),
        )
        started = self.clock()
        snapshot = await self._inventory(notebook_id)
        await self._delete_inventory(notebook_id, snapshot)

        last_nonempty = self.clock()
        consecutive_empty = 0
        deadline = started + policy.deadline
        while True:
            current = await self._inventory(notebook_id)
            sources = await self._sources(notebook_id)
            now = self.clock()
            if now > deadline:
                raise ContractError("role preparation exceeded its five-minute deadline")
            if current.ids:
                consecutive_empty = 0
                await self._delete_inventory(notebook_id, current)
                # The quiet interval begins only after deletion is complete;
                # network/delete latency cannot satisfy part of the 90-second
                # post-delete observation window.
                last_nonempty = self.clock()
            else:
                consecutive_empty += 1
            ready_count = sum(bool(getattr(source, "is_ready", False)) for source in sources)
            ready = ready_count >= clean["minimum_ready_sources"]
            quiet = now - last_nonempty >= policy.quiet_period
            if not current.ids and consecutive_empty >= 3 and quiet and ready:
                final_inventory = await self._inventory(notebook_id)
                final_sources = await self._sources(notebook_id)
                final_now = self.clock()
                if final_now > deadline:
                    raise ContractError("role preparation exceeded its five-minute deadline")
                if final_inventory.ids:
                    consecutive_empty = 0
                    await self._delete_inventory(notebook_id, final_inventory)
                    last_nonempty = self.clock()
                elif sum(
                    bool(getattr(source, "is_ready", False)) for source in final_sources
                ) >= clean["minimum_ready_sources"] and not (snapshot.ids & final_inventory.ids):
                    if role in clean["empty_chat_roles"]:
                        await self._assert_chat_empty(notebook_id)
                        if self.clock() > deadline:
                            raise ContractError(
                                "role preparation exceeded its five-minute deadline"
                            )
                    return {
                        "removed_artifacts": len(snapshot.artifacts),
                        "removed_notes": len(snapshot.notes),
                        "removed_mind_maps": len(snapshot.mind_maps),
                        "ready_sources": sum(
                            bool(getattr(source, "is_ready", False)) for source in final_sources
                        ),
                    }
            now = self.clock()
            if now >= deadline:
                raise ContractError("role preparation did not reach a stable clean state")
            await self.sleep(min(policy.poll_interval, deadline - now))

    async def prepare_reference(self, notebook_id: str) -> dict[str, int]:
        """Seed and validate deterministic disposable note/conversation state."""

        reference = self.prepared_contract["reference"]
        note = await self.client.notes.create(
            notebook_id,
            reference["note_title"],
            reference["note_body"],
        )
        note_id = getattr(note, "id", None)
        if not is_valid_notebook_id(note_id):
            raise ContractError("prepared reference note returned a malformed ID")
        await self.client.chat.ask(notebook_id, reference["question"])
        deadline = self.clock() + 90.0
        while True:
            notes = await self._read(lambda: self.client.notes.list(notebook_id))
            markers = [
                item
                for item in notes
                if getattr(item, "title", None) == reference["note_title"]
                and getattr(item, "content", None) == reference["note_body"]
            ]
            marker = markers[0] if len(markers) == 1 else None
            readable = None
            if marker is not None and getattr(marker, "id", None) == note_id:
                readable = await self._read(lambda: self.client.notes.get(notebook_id, note_id))
            conversation_id = await self._read(
                lambda: self.client.chat.get_conversation_id(notebook_id)
            )
            history = await self._read(lambda: self.client.chat.get_history(notebook_id))
            turns: object = []
            if conversation_id:
                turns = await self._read(
                    lambda conversation_id=conversation_id: self.client.chat.get_conversation_turns(
                        notebook_id,
                        conversation_id,
                        limit=int(reference["conversation_turn_limit"]),
                    )
                )
            history_pairs = _matching_history_pairs(history, reference["question"])
            readable_valid = (
                readable is not None
                and getattr(readable, "id", None) == note_id
                and getattr(readable, "title", None) == reference["note_title"]
                and getattr(readable, "content", None) == reference["note_body"]
            )
            now = self.clock()
            if now > deadline:
                raise ContractError("prepared reference state exceeded its deadline")
            if (
                readable_valid
                and isinstance(conversation_id, str)
                and bool(conversation_id.strip())
                and history_pairs
                and _turn_has_content(turns)
            ):
                return {"seeded_notes": 1, "history_pairs": history_pairs}
            if now >= deadline:
                raise ContractError("prepared reference state did not become readable")
            await self.sleep(min(2.0, deadline - now))

    async def validate_prepared_role(self, notebook_id: str, role: str) -> dict[str, int]:
        if role == "reference":
            reference = self.prepared_contract["reference"]
            notes = await self._read(lambda: self.client.notes.list(notebook_id))
            markers = [
                item
                for item in notes
                if getattr(item, "title", None) == reference["note_title"]
                and getattr(item, "content", None) == reference["note_body"]
            ]
            if len(markers) != 1 or not is_valid_notebook_id(getattr(markers[0], "id", None)):
                raise ContractError("prepared reference note is absent or ambiguous")
            marker = markers[0]
            readable = await self._read(lambda: self.client.notes.get(notebook_id, marker.id))
            if (
                getattr(readable, "id", None) != getattr(marker, "id", None)
                or getattr(readable, "title", None) != reference["note_title"]
                or getattr(readable, "content", None) != reference["note_body"]
            ):
                raise ContractError("prepared reference note readback does not match")
            conversation_id = await self._read(
                lambda: self.client.chat.get_conversation_id(notebook_id)
            )
            history = await self._read(lambda: self.client.chat.get_history(notebook_id))
            history_pairs = _matching_history_pairs(history, reference["question"])
            if (
                not isinstance(conversation_id, str)
                or not conversation_id.strip()
                or not history_pairs
            ):
                raise ContractError("prepared reference conversation is absent")
            turns = await self._read(
                lambda: self.client.chat.get_conversation_turns(
                    notebook_id,
                    conversation_id,
                    limit=int(reference["conversation_turn_limit"]),
                )
            )
            if not _turn_has_content(turns):
                raise ContractError("prepared reference conversation turns are absent")
            return {"seeded_notes": 1, "history_pairs": history_pairs}
        if role not in self.prepared_contract["clean_roles"]["roles"]:
            raise ContractError("prepared role is not allowlisted")
        inventory = await self._inventory(notebook_id)
        sources = await self._sources(notebook_id)
        minimum = self.prepared_contract["clean_roles"]["minimum_ready_sources"]
        if inventory.ids:
            raise ContractError("prepared clean role has residual children")
        ready_count = sum(bool(getattr(source, "is_ready", False)) for source in sources)
        if ready_count < minimum:
            raise ContractError("prepared clean role has unready sources")
        if role in self.prepared_contract["clean_roles"]["empty_chat_roles"]:
            await self._assert_chat_empty(notebook_id)
        return {"ready_sources": ready_count, **inventory.counts}

    def _persist_manifest(self, manifest: dict[str, Any]) -> None:
        try:
            self.store.write(manifest, template_id=self.template_id)
        except Exception as exc:
            raise PersistenceError("manifest update was not durable") from exc

    def _update_row(
        self,
        manifest: dict[str, Any],
        role: str,
        **changes: object,
    ) -> dict[str, Any]:
        candidate = dict(manifest)
        candidate["copies"] = [dict(row) for row in manifest["copies"]]
        row = next(item for item in candidate["copies"] if item["role"] == role)
        row.update(changes)
        self._persist_manifest(candidate)
        manifest.clear()
        manifest.update(candidate)
        return row

    async def _probe_candidate(self, notebook_id: str, title: str) -> CandidateProbe:
        if notebook_id == self.template_id or not is_valid_notebook_id(notebook_id):
            return CandidateProbe(ProbeState.UNSAFE)
        try:
            notebook = await self._read(lambda: self.client.notebooks.get(notebook_id))
        except NotebookNotFoundError:
            return CandidateProbe(ProbeState.MISSING)
        except Exception:
            return CandidateProbe(ProbeState.UNREADABLE)
        if (
            getattr(notebook, "id", None) != notebook_id
            or getattr(notebook, "title", None) != title
            or getattr(notebook, "role", None) is not SharePermission.OWNER
        ):
            return CandidateProbe(ProbeState.UNSAFE, notebook)
        return CandidateProbe(ProbeState.VALID, notebook)

    async def _exact_title_once(self, title: str) -> tuple[int, CandidateProbe]:
        try:
            notebooks = await self._read(lambda: self.client.notebooks.list())
        except Exception:
            return -1, CandidateProbe(ProbeState.UNREADABLE)
        matches = [notebook for notebook in notebooks if getattr(notebook, "title", None) == title]
        if len(matches) != 1:
            return len(matches), CandidateProbe(
                ProbeState.MISSING if not matches else ProbeState.UNSAFE
            )
        notebook_id = getattr(matches[0], "id", None)
        if not is_valid_notebook_id(notebook_id):
            return 1, CandidateProbe(ProbeState.UNSAFE)
        return 1, await self._probe_candidate(str(notebook_id), title)

    async def _reconcile_title(self, title: str) -> tuple[int, CandidateProbe]:
        deadline = self.clock() + self.reconcile_timeout
        delay = 0.5
        while True:
            cardinality, probe = await self._exact_title_once(title)
            if cardinality == 1 and probe.state is not ProbeState.MISSING:
                return cardinality, probe
            if cardinality > 1 or cardinality < 0:
                return cardinality, probe
            now = self.clock()
            if now >= deadline:
                return cardinality, probe
            await self.sleep(min(delay, deadline - now))
            delay = min(delay * 2, 8.0)

    async def _guarded_delete(self, notebook_id: str, title: str) -> ProbeState:
        probe = await self._probe_candidate(notebook_id, title)
        if probe.state is ProbeState.MISSING:
            return ProbeState.MISSING
        if probe.state is not ProbeState.VALID:
            return probe.state
        try:
            await retry_idempotent(
                lambda: self.client.notebooks.delete(notebook_id),
                policy=self.retry_policy,
                sleep=self.sleep,
            )
        except NotebookNotFoundError:
            return ProbeState.MISSING
        except Exception:
            return ProbeState.UNREADABLE
        delay = self.retry_policy.base_delay
        for attempt in range(self.retry_policy.attempts):
            confirmation = await self._probe_candidate(notebook_id, title)
            if confirmation.state is ProbeState.MISSING:
                # Preserve the existing outcome distinction: VALID means this
                # call issued the deletion, while MISSING means it was absent
                # before dispatch.
                return ProbeState.VALID
            if confirmation.state is not ProbeState.VALID:
                return confirmation.state
            if attempt + 1 == self.retry_policy.attempts:
                return ProbeState.DELETE_UNCONFIRMED
            await self.sleep(min(delay, self.retry_policy.max_delay))
            delay = min(delay * 2, self.retry_policy.max_delay)
        return ProbeState.DELETE_UNCONFIRMED

    async def _recover_dispatched_copy(
        self,
        manifest: dict[str, Any],
        role: str,
        title: str,
        candidate_id: str | None,
    ) -> tuple[dict[str, Any] | None, bool]:
        anomaly = False

        async def persist_reconciliation(notebook_id: str, *, unsafe: bool) -> dict[str, Any]:
            try:
                return self._update_row(
                    manifest,
                    role,
                    status="reconciled",
                    notebook_id=notebook_id,
                    last_error_category="COPY_UNRESOLVED" if unsafe else None,
                )
            except PersistenceError:
                # Once an owned exact-title copy is known, a failed durable
                # confirmation must trigger the same immediate best-effort
                # teardown as the normal response-confirmation path.  The
                # intent remains on disk for unconditional cleanup to retry.
                await self._guarded_delete(notebook_id, title)
                raise

        if candidate_id is not None:
            candidate_probe = await self._probe_candidate(candidate_id, title)
            if candidate_probe.state is ProbeState.VALID:
                row = await persist_reconciliation(candidate_id, unsafe=False)
                return row, False
            anomaly = candidate_probe.state in {ProbeState.UNSAFE, ProbeState.UNREADABLE}
        cardinality, title_probe = await self._reconcile_title(title)
        if cardinality == 1 and title_probe.state is ProbeState.VALID:
            notebook_id = str(title_probe.notebook.id)
            row = await persist_reconciliation(notebook_id, unsafe=anomaly)
            return row, anomaly
        self._update_row(
            manifest,
            role,
            last_error_category="COPY_UNRESOLVED",
        )
        return None, True

    async def copy_one(
        self,
        manifest: dict[str, Any],
        role: str,
    ) -> dict[str, Any]:
        """Dispatch exactly one copy after durable verified-empty intent."""

        title: str | None = None
        for _attempt in range(100):
            nonce = self.nonce()
            title = build_title(
                str(manifest["run_id"]),
                str(manifest["run_attempt"]),
                str(manifest["lane"]),
                role,
                nonce,
            )
            notebooks = await self._read(lambda: self.client.notebooks.list())
            if not any(getattr(notebook, "title", None) == title for notebook in notebooks):
                break
        else:
            raise CopyUnresolvedError("unable to allocate an unused cryptographic copy title")
        assert title is not None
        manifest["copies"].append(new_copy_row(role=role, title=title))
        self._persist_manifest(manifest)

        candidate_id: str | None = None
        try:
            copied = await self.client.notebooks.copy(self.template_id, title)
            raw_candidate = getattr(copied, "id", None)
            if not is_valid_notebook_id(raw_candidate) or raw_candidate == self.template_id:
                raise CopyUnresolvedError("copy response did not contain a safe candidate")
            candidate_id = str(raw_candidate)
            try:
                self._update_row(
                    manifest,
                    role,
                    candidate_notebook_id=candidate_id,
                )
            except PersistenceError:
                await self._guarded_delete(candidate_id, title)
                raise
            if getattr(copied, "title", None) != title:
                raise CopyUnresolvedError("copy response title did not match the intent")
            probe = await self._probe_candidate(candidate_id, title)
            if probe.state is not ProbeState.VALID:
                raise CopyUnresolvedError("copy candidate could not be explicitly confirmed")
            try:
                return self._update_row(
                    manifest,
                    role,
                    status="confirmed",
                    notebook_id=candidate_id,
                    last_error_category=None,
                )
            except PersistenceError:
                await self._guarded_delete(candidate_id, title)
                raise
        except PersistenceError:
            raise
        except BaseException as original:
            row, anomaly = await self._recover_dispatched_copy(
                manifest,
                role,
                title,
                candidate_id,
            )
            if not isinstance(original, Exception):
                raise
            if row is None or anomaly:
                raise CopyUnresolvedError("copy dispatch remains unresolved") from original
            print(f"::warning::copy outcome reconciled for role {role}")
            return row

    async def provision(
        self,
        *,
        run_id: str,
        run_attempt: str,
        lane: str,
        mode: str,
        account_slot: str,
        backend: str,
        template_fingerprint: str,
        github_env: Path,
        mask: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Validate, create, prepare, and publish the complete role set."""

        if self.store.path.exists():
            raise PersistenceError("manifest already exists; refusing to overwrite lifecycle state")
        await self.validate_template()
        manifest = new_manifest(
            run_id=run_id,
            run_attempt=run_attempt,
            lane=lane,
            mode=mode,
            account_slot=account_slot,
            backend=backend,
            template_fingerprint=template_fingerprint,
        )
        self._persist_manifest(manifest)
        for role in MODE_ROLES[mode]:
            row = await self.copy_one(manifest, role)
            notebook_id = str(row["notebook_id"])
            if mask is not None:
                mask(notebook_id)
            await self._validate_copy_shape(notebook_id)
            if role == "reference":
                await self.prepare_reference(notebook_id)
            else:
                await self.prepare_clean_role(notebook_id, role)
            await self.validate_prepared_role(notebook_id, role)
            self._update_row(manifest, role, prepared=True)

        validate_manifest(manifest, template_id=self.template_id)
        role_ids = [str(row["notebook_id"]) for row in manifest["copies"]]
        if self.template_id in role_ids or len(set(role_ids)) != len(role_ids):
            raise ManifestError(
                "prepared role IDs are not distinct from each other and the template"
            )
        lines = github_env_lines(mode, manifest["copies"])
        atomic_append_lines(github_env, lines)
        return manifest

    def _mark_cleanup_failure(self, manifest: dict[str, Any], role: str) -> None:
        self._update_row(
            manifest,
            role,
            status="delete_failed",
            last_error_category="CLEANUP",
        )

    async def _cleanup_trusted(
        self, manifest: dict[str, Any], row: dict[str, Any]
    ) -> ProbeState | None:
        role = str(row["role"])
        title = str(row["title"])
        notebook_id = str(row["notebook_id"])
        anomaly = False
        candidate_id = row.get("candidate_notebook_id")
        if isinstance(candidate_id, str) and candidate_id != notebook_id:
            candidate_probe = await self._probe_candidate(candidate_id, title)
            anomaly = candidate_probe.state is not ProbeState.MISSING
        outcome = await self._guarded_delete(notebook_id, title)
        if outcome not in {ProbeState.VALID, ProbeState.MISSING} or anomaly:
            self._mark_cleanup_failure(manifest, role)
            return None
        self._update_row(
            manifest,
            role,
            status="deleted",
            last_error_category=None,
        )
        return outcome

    async def _cleanup_intent(
        self, manifest: dict[str, Any], row: dict[str, Any]
    ) -> ProbeState | None:
        role = str(row["role"])
        title = str(row["title"])
        candidate_id = row.get("candidate_notebook_id")
        anomaly = False
        confirmed_id: str | None = None
        if isinstance(candidate_id, str):
            candidate_probe = await self._probe_candidate(candidate_id, title)
            if candidate_probe.state is ProbeState.VALID:
                confirmed_id = candidate_id
            else:
                anomaly = candidate_probe.state in {ProbeState.UNSAFE, ProbeState.UNREADABLE}
        if confirmed_id is None:
            cardinality, title_probe = await self._reconcile_title(title)
            if cardinality == 1 and title_probe.state is ProbeState.VALID:
                confirmed_id = str(title_probe.notebook.id)
            else:
                self._mark_cleanup_failure(manifest, role)
                return None
        self._update_row(
            manifest,
            role,
            status="reconciled",
            notebook_id=confirmed_id,
            last_error_category="CLEANUP" if anomaly else None,
        )
        outcome = await self._guarded_delete(confirmed_id, title)
        if outcome not in {ProbeState.VALID, ProbeState.MISSING} or anomaly:
            self._mark_cleanup_failure(manifest, role)
            return None
        self._update_row(
            manifest,
            role,
            status="deleted",
            last_error_category=None,
        )
        return outcome

    async def cleanup(self, *, expected_backend: str | None = None) -> dict[str, int]:
        """Reconcile and delete every row, aggregating failures in reverse order."""

        if not self.store.path.exists():
            return {"deleted": 0, "already_missing": 0, "failed": 0}
        expected = {"backend": expected_backend} if expected_backend is not None else None
        manifest = self.store.read(template_id=self.template_id, expected=expected)
        failed = 0
        deleted = 0
        already_missing = 0
        for row in reversed(manifest["copies"]):
            if row["status"] == "deleted":
                already_missing += 1
                continue
            before = row.get("notebook_id")
            try:
                if before is None or row["status"] == "intent":
                    outcome = await self._cleanup_intent(manifest, row)
                else:
                    outcome = await self._cleanup_trusted(manifest, row)
            except Exception:
                failed += 1
                try:
                    self._mark_cleanup_failure(manifest, str(row["role"]))
                except Exception:
                    pass
                continue
            if outcome is ProbeState.VALID:
                deleted += 1
            elif outcome is ProbeState.MISSING:
                already_missing += 1
            else:
                failed += 1
        if failed:
            raise CleanupError(f"cleanup left {failed} role(s) unresolved")
        return {"deleted": deleted, "already_missing": already_missing, "failed": 0}

    async def sweep(
        self,
        *,
        current_run_id: str | None,
        current_run_attempt: str | None,
        max_age: timedelta = timedelta(hours=24),
        deletion_cap: int = 20,
        now: datetime | None = None,
    ) -> SweepResult:
        """Delete only stale, normative, explicitly owned account-local copies."""

        if deletion_cap < 1 or deletion_cap > 20:
            raise ValueError("sweep deletion cap must be between one and 20")
        if max_age <= timedelta(0):
            raise ValueError("sweep maximum age must be positive")
        if (current_run_id is None) != (current_run_attempt is None):
            raise ValueError("current run ID and attempt must be supplied together")
        if current_run_id is not None and (
            re.fullmatch(r"[0-9]+", current_run_id) is None
            or re.fullmatch(r"[0-9]+", str(current_run_attempt)) is None
        ):
            raise ValueError("current run ID and attempt must be decimal strings")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("sweep clock must be timezone-aware")
        notebooks = await self._read(lambda: self.client.notebooks.list())
        eligible: list[tuple[str, str]] = []
        skipped = 0
        for listed in notebooks:
            title = getattr(listed, "title", None)
            if not isinstance(title, str) or not title.startswith(RESERVED_PREFIX):
                skipped += 1
                continue
            parsed = parse_title(title)
            notebook_id = getattr(listed, "id", None)
            if parsed is None or not is_valid_notebook_id(notebook_id):
                skipped += 1
                continue
            if notebook_id == self.template_id:
                skipped += 1
                continue
            if parsed.run_id == current_run_id and parsed.run_attempt == current_run_attempt:
                skipped += 1
                continue
            probe = await self._probe_candidate(str(notebook_id), title)
            if probe.state is not ProbeState.VALID:
                skipped += 1
                continue
            created_at = getattr(probe.notebook, "created_at", None)
            if (
                not isinstance(created_at, datetime)
                or created_at.tzinfo is None
                or created_at >= now - max_age
            ):
                skipped += 1
                continue
            eligible.append((str(notebook_id), title))
        if len(eligible) > deletion_cap:
            raise CleanupError("sweep deletion cap would be exceeded; no copies were deleted")
        deleted = 0
        failed = 0
        for notebook_id, title in eligible:
            outcome = await self._guarded_delete(notebook_id, title)
            if outcome in {ProbeState.VALID, ProbeState.MISSING}:
                deleted += 1
            else:
                failed += 1
        if failed:
            raise CleanupError(f"sweep could not delete {failed} eligible copy/copies")
        return SweepResult(
            eligible=len(eligible),
            deleted=deleted,
            skipped=skipped,
            failed=0,
        )


def _template_id_from_env(name: str) -> str:
    if name != TEMPLATE_ID_ENV:
        raise ManifestError("template ID environment name is not allowlisted")
    value = os.environ.get(name, "")
    if not is_valid_notebook_id(value):
        raise ManifestError("template notebook ID environment value is missing or malformed")
    return value


def _runner_temp() -> Path | None:
    value = os.environ.get("RUNNER_TEMP")
    return Path(value) if value else None


def _metadata() -> tuple[str, str, str]:
    """Read trusted lifecycle identity only from the GitHub runner environment."""

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if re.fullmatch(r"[0-9]+", run_id) is None or re.fullmatch(r"[0-9]+", run_attempt) is None:
        raise ManifestError("GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT must be decimal")
    if repository != MANIFEST_REPOSITORY:
        raise ManifestError("GITHUB_REPOSITORY does not match the canonical repository")
    return run_id, run_attempt, repository


def _mask_for_github(value: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(name: str, *, manifest: bool = True) -> argparse.ArgumentParser:
        child = subparsers.add_parser(name)
        child.add_argument("--backend", choices=BACKENDS, required=True)
        child.add_argument("--template-id-env", required=True)
        if manifest:
            child.add_argument("--manifest", type=Path, required=True)
        return child

    provision = common("provision")
    provision.add_argument("--contract", type=Path, default=DEFAULT_TEMPLATE_CONTRACT)
    provision.add_argument(
        "--prepared-contract",
        type=Path,
        default=DEFAULT_PREPARED_CONTRACT,
    )
    provision.add_argument("--mode", choices=tuple(MODE_ROLES), required=True)
    provision.add_argument("--lane", choices=LANES, required=True)
    provision.add_argument("--account-slot", choices=ACCOUNT_SLOTS, required=True)
    provision.add_argument("--github-env", type=Path, required=True)
    provision.add_argument("--reconcile-timeout", type=float, default=90.0)

    validate = common("validate", manifest=False)
    validate.add_argument("--contract", type=Path, default=DEFAULT_TEMPLATE_CONTRACT)
    validate.add_argument("--prepared-contract", type=Path, default=DEFAULT_PREPARED_CONTRACT)
    validate.add_argument("--manifest", type=Path)
    validate.add_argument("--role", choices=("reference", "generation", "multi-source", "rpc"))

    common("cleanup")

    sweep = common("sweep")
    sweep.add_argument("--max-age-hours", type=float, default=24.0)
    sweep.add_argument("--deletion-cap", type=int, default=20)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "cleanup" and not args.manifest.exists():
        if args.template_id_env != TEMPLATE_ID_ENV:
            raise ManifestError("template ID environment name is not allowlisted")
        print("nothing to clean; manifest is absent")
        return 0
    template_id = _template_id_from_env(args.template_id_env)
    template_contract: dict[str, Any] = {}
    prepared_contract: dict[str, Any] = {}
    fingerprint = ""
    if args.command == "provision" or (args.command == "validate" and args.role is None):
        template_contract, fingerprint = load_template_contract(
            getattr(args, "contract", DEFAULT_TEMPLATE_CONTRACT)
        )
    if args.command == "provision" or (args.command == "validate" and args.role is not None):
        prepared_contract = load_prepared_contract(
            getattr(args, "prepared_contract", DEFAULT_PREPARED_CONTRACT)
        )
    if args.command == "validate" and (args.role is None) != (args.manifest is None):
        raise ManifestError("prepared-role validation requires both --manifest and --role")
    metadata = _metadata() if args.command in {"provision", "sweep"} else None
    manifest_path = getattr(args, "manifest", None)
    store_path = manifest_path or Path(os.devnull)
    store = AtomicJSONStore(store_path, runner_temp=_runner_temp())
    async with NotebookLMClient.from_storage(
        backend=args.backend,
        rate_limit_max_retries=0,
        server_error_max_retries=0,
    ) as client:
        manager = NotebookLifecycleManager(
            client,
            template_id=template_id,
            store=store,
            template_contract=template_contract,
            prepared_contract=prepared_contract,
            reconcile_timeout=getattr(args, "reconcile_timeout", 90.0),
        )
        if args.command == "provision":
            assert metadata is not None
            run_id, run_attempt, _repository = metadata
            manifest = await manager.provision(
                run_id=run_id,
                run_attempt=run_attempt,
                lane=args.lane,
                mode=args.mode,
                account_slot=args.account_slot,
                backend=args.backend,
                template_fingerprint=fingerprint,
                github_env=args.github_env,
                mask=_mask_for_github,
            )
            print(
                f"provisioned {len(manifest['copies'])} prepared role(s); "
                f"mode={args.mode} backend={args.backend} slot={args.account_slot}"
            )
            return 0
        if args.command == "validate":
            if args.role is None:
                counts = await manager.validate_template()
                print(
                    "template contract valid; "
                    f"ready_sources={counts['ready_sources']} "
                    f"completed_artifacts={counts['completed_artifacts']} "
                    f"fingerprint={fingerprint}"
                )
                return 0
            if args.manifest is None:
                raise ManifestError("prepared-role validation requires --manifest")
            manifest = store.read(
                template_id=template_id,
                expected={"backend": args.backend},
            )
            row = next((row for row in manifest["copies"] if row["role"] == args.role), None)
            if row is None or row["notebook_id"] is None or row["prepared"] is not True:
                raise ManifestError("prepared role is absent from the manifest")
            counts = await manager.validate_prepared_role(str(row["notebook_id"]), args.role)
            print(f"prepared role valid; role={args.role} checks={len(counts)}")
            return 0
        if args.command == "cleanup":
            counts = await manager.cleanup(expected_backend=args.backend)
            print(
                f"cleanup complete; deleted={counts['deleted']} "
                f"already_missing={counts['already_missing']} failed={counts['failed']}"
            )
            return 0
        if args.command == "sweep":
            assert metadata is not None
            current_run_id, current_run_attempt, _repository = metadata
            if args.manifest.exists():
                store.read(
                    template_id=template_id,
                    expected={
                        "backend": args.backend,
                        "run_id": current_run_id,
                        "run_attempt": current_run_attempt,
                    },
                )
            result = await manager.sweep(
                current_run_id=current_run_id,
                current_run_attempt=current_run_attempt,
                max_age=timedelta(hours=args.max_age_hours),
                deletion_cap=args.deletion_cap,
            )
            print(
                f"sweep complete; eligible={result.eligible} deleted={result.deleted} "
                f"skipped={result.skipped} failed={result.failed}"
            )
            return 0
    raise AssertionError("unreachable command")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            category = "REGRESSION"
        else:
            category = _category_for(exc)
        print(
            f"ERROR[{category}]: lifecycle command failed ({_safe_exception_name(exc)})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
