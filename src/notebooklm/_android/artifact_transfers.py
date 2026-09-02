"""Android gRPC artifact transfers: CopyArtifactsAsync and the customization table.

Live-validated over native Android gRPC on 2026-09-01
(``docs/android/copy-append-suggestion-evidence.md``). ``CopyArtifactsAsync`` is
web-derived (the app never calls it); ``GetArtifactCustomizationChoices`` is
compiled into the app with exact request/response FQNs, and the live reply
carries two families (audio #1, video #2) the APK schema does not declare.

Kept as a mixin so ``_android/artifacts.py`` stays under the ADR-0008
module-size budget; :class:`AndroidArtifactsAPI` inherits it and supplies
``_transport``.
"""

from __future__ import annotations

import builtins
import logging
from typing import Any

from ..exceptions import ArtifactNotFoundError, DecodingError, ValidationError
from ..types import (
    ArtifactCustomizationChoices,
    CopiedArtifact,
    CustomizationChoice,
    ReportPreset,
)
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .codecs.artifacts import decode_artifact
from .session import AndroidSession
from .write_safety import call_unconfirmed_on_transport_loss

logger = logging.getLogger(__name__)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
COPY_ARTIFACTS_ASYNC_METHOD = f"/{_SERVICE}/CopyArtifactsAsync"
GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD = f"/{_SERVICE}/GetArtifactCustomizationChoices"


def _request_context() -> Any:
    from .upload import android_request_context

    return android_request_context()


def _format_choices(message: Any) -> tuple[CustomizationChoice, ...]:
    return tuple(
        CustomizationChoice(code=int(row.format), title=row.title, description=row.description)
        for row in message.choices
        if row.title
    )


class AndroidArtifactTransferMixin:
    """``CopyArtifactsAsync`` / ``GetArtifactCustomizationChoices`` over gRPC."""

    _transport: AndroidSession

    async def copy(
        self,
        notebook_id: str,
        artifact_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> builtins.list[CopiedArtifact]:
        """Copy ``artifact_ids`` into ``target_notebook_id`` (``CopyArtifactsAsync``).

        Request: context #1, bare-string artifact ids #2, target project #3
        (a target at #4 draws ``INVALID_ARGUMENT``). The reply maps each
        original id (#1) to the full new ``Artifact`` (#2); unknown ids are
        echoed under a separate field with no new row rather than ``NOT_FOUND``,
        so an empty mapping raises :class:`ArtifactNotFoundError`.
        """
        del notebook_id  # The route is addressed by artifact ids + target alone.
        if not artifact_ids:
            raise ValidationError("artifact_ids must not be empty")
        if any(not artifact_id for artifact_id in artifact_ids):
            raise ValidationError("artifact_ids must not contain empty entries")
        if not target_notebook_id:
            raise ValidationError("target_notebook_id must not be empty")
        request = _PROTO.CopyArtifactsAsyncRequest(
            request_context=_request_context(),
            artifact_ids=list(artifact_ids),
            target_project_id=target_notebook_id,
        )
        async with self._transport.operation_scope("artifacts.copy") as lease:
            response = await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    COPY_ARTIFACTS_ASYNC_METHOD,
                    request,
                    replay_safe=False,
                    response_type=_PROTO.CopyArtifactsAsyncResponse,
                    expected_epoch=lease.epoch,
                )
            )
        # Malformed entries are skipped, not fatal: the well-formed ones are the
        # only proof of copies that have already committed.
        copied: builtins.list[CopiedArtifact] = []
        malformed = 0
        for entry in response.copied_artifacts:
            artifact = (
                decode_artifact(entry.artifact, method_id=COPY_ARTIFACTS_ASYNC_METHOD)
                if entry.HasField("artifact")
                else None
            )
            if not entry.source_artifact_id or artifact is None or not artifact.id:
                malformed += 1
                logger.warning("CopyArtifactsAsync returned a malformed mapping entry")
                continue
            copied.append(CopiedArtifact(original_id=entry.source_artifact_id, artifact=artifact))
        if not copied:
            if malformed:
                raise DecodingError(
                    "CopyArtifactsAsync returned only malformed mapping entries",
                    method_id=COPY_ARTIFACTS_ASYNC_METHOD,
                )
            raise ArtifactNotFoundError(
                ", ".join(artifact_ids), method_id=COPY_ARTIFACTS_ASYNC_METHOD
            )
        missing = set(artifact_ids) - {item.original_id for item in copied}
        if missing:
            logger.warning(
                "CopyArtifactsAsync copied %d of %d artifact(s) into %s; not copied: %s",
                len(copied),
                len(artifact_ids),
                target_notebook_id,
                ", ".join(sorted(missing)),
            )
        return copied

    async def get_customization_choices(
        self, notebook_id: str | None = None
    ) -> ArtifactCustomizationChoices:
        """Return the Studio option tables (``GetArtifactCustomizationChoices``).

        Account-level: an empty request, a bogus project id and every
        ``artifact_type`` returned the identical 3238-byte table live, so only
        the request context is required; ``project_id`` is sent when given to
        mirror the app's exact request shape.
        """
        request = _PROTO.GetArtifactCustomizationChoicesRequest(request_context=_request_context())
        if notebook_id:
            request.project_id = notebook_id
        response = await self._transport.unary(
            GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD,
            request,
            replay_safe=True,
            response_type=_PROTO.GetArtifactCustomizationChoicesResponse,
        )
        choices = response.artifact_customization_choices
        return ArtifactCustomizationChoices(
            audio=_format_choices(choices.audio_overview_choices),
            video=_format_choices(choices.video_overview_choices),
            slide_deck=tuple(
                CustomizationChoice(
                    code=int(row.deck_type), title=row.title, description=row.description
                )
                for row in choices.slides_customization_choices.types
                if row.title
            ),
            reports=tuple(
                ReportPreset(
                    report_type=row.report_type,
                    description=row.report_description,
                    directive=row.report_directive,
                )
                for row in choices.tailored_report_customization_choices.report_type_options
                if row.report_type and row.report_directive
            ),
        )


__all__ = [
    "COPY_ARTIFACTS_ASYNC_METHOD",
    "GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD",
    "AndroidArtifactTransferMixin",
]
