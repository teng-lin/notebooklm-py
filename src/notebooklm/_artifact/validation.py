"""Input-validation helpers for the artifacts facade.

These small guards live in a roomy sibling module so the ``_artifacts.py``
facade can call them in a line or two without growing past the module-size
ratchet (ADR-0008). They raise :class:`~notebooklm.exceptions.ValidationError`
with actionable messages for the two footguns tracked in #1874:

* ``coerce_report_format`` — ``generate_report`` puts ``report_format`` in the
  second positional slot while every sibling ``generate_*`` puts ``source_ids``
  there, so ``generate_report(nb, ["s1", "s2"])`` used to blow up deep inside
  the report-config lookup with an opaque ``TypeError``.
* ``check_exactly_one_export_target`` — ``export`` accepts ``artifact_id`` and
  ``content`` but exactly one must be supplied; neither is a silent no-op RPC
  and both is ambiguous.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import ValidationError
from ..rpc import ReportFormat


def coerce_report_format(report_format: Any) -> ReportFormat:
    """Coerce ``report_format`` to a :class:`ReportFormat`, else raise.

    Idempotent for enum members, coerces valid format strings (``ReportFormat``
    is a ``str`` enum), and rejects lists / bad strings with a message that
    points the caller at the ``source_ids=`` keyword — the common mistake is
    calling ``generate_report(nb, ["s1", "s2"])`` expecting the second
    positional to be ``source_ids`` as it is on every sibling ``generate_*``.
    """
    try:
        return ReportFormat(report_format)
    except ValueError as exc:
        raise ValidationError(
            f"report_format must be a ReportFormat (or valid format string), "
            f"got {report_format!r}. If you meant source_ids, use source_ids=[...]."
        ) from exc


def check_exactly_one_export_target(artifact_id: str | None, content: str | None) -> None:
    """Require exactly one of ``artifact_id`` / ``content`` for ``export``.

    Both ``None`` would send a no-op export RPC; both set is ambiguous.
    """
    if (artifact_id is None) == (content is None):
        which = "neither" if artifact_id is None else "both"
        raise ValidationError(
            f"export() requires exactly one of artifact_id= or content= (got {which})."
        )
