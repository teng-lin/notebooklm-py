"""Stable request payload builders for the source-label RPCs.

Two differences from the source RPCs: the request-options wrapper is slot ``[0]``,
and ``notebook_id`` rides in the params (slot ``[1]``) in addition to the
``source-path`` query arg. Every builder returns a **fresh** structure per call
so callers never alias a shared mutable wrapper (cf.
``_settings.build_get_user_settings_params``). See docs/design/source-labels/.
"""

from __future__ import annotations

from typing import Any, Literal


def _opts() -> list[Any]:
    """Fresh request-options wrapper (arg ``[0]`` of every label RPC).

    Mirrors the ``[1, None*8, [1]]`` context block in ``_settings.py``; returned
    fresh so callers never alias a shared mutable list.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def build_generate_labels_params(
    notebook_id: str, *, scope: Literal["all", "unlabeled"] = "unlabeled"
) -> list[Any]:
    """CREATE_LABEL (agX4Bc) — AI grouping. ``scope`` selects slot ``[4]``:

    ``"all"`` -> ``[]`` (wipe + regenerate every label, destructive, new ids);
    ``"unlabeled"`` -> ``[0]`` (incremental — only currently-unlabeled sources).
    """
    return [_opts(), notebook_id, None, None, ([] if scope == "all" else [0])]


def build_create_label_params(notebook_id: str, name: str, emoji: str = "") -> list[Any]:
    """CREATE_LABEL (agX4Bc) — manual create. Scope slot ``[4]`` is ``None``;
    slot ``[5]`` carries the labels to create."""
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
    add_source_ids: list[str] | None = None,
) -> list[Any]:
    """UPDATE_LABEL (le8sX). Fieldmask slot ``[3]`` = ``[[ name_emoji, sources ]]``:

    * ``name_emoji`` = ``[name, emoji]`` (positional). A rename sends a length-1
      ``[name]``. Whether a length-1 ``name_emoji`` PRESERVES an existing emoji
      or clears it is unverified on the wire (rpc.md open item) — the API layer
      preserves the current emoji by passing it explicitly.
    * ``sources`` = ``[[source_id], ...]`` — APPENDS; omitted leaves unchanged.
    """
    group: list[Any] = []
    if name is not None or emoji is not None:
        group.append([name] if emoji is None else [name, emoji])
    else:
        group.append(None)
    if add_source_ids:
        group.append([[sid] for sid in add_source_ids])  # each id in its own 1-list
    return [_opts(), notebook_id, label_id, [group]]


def build_delete_labels_params(notebook_id: str, label_ids: list[str]) -> list[Any]:
    """DELETE_LABEL (GyzE7e) — batch, array of ids."""
    return [_opts(), notebook_id, list(label_ids)]
