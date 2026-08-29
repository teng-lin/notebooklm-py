"""Pinned values from the reviewed Android evidence profile."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AndroidEvidenceProfile:
    """Values which must move together when a later capture is reviewed."""

    app_version: str
    app_user_agent: str
    finalize_user_agent: str


ANDROID_EVIDENCE_PROFILE = AndroidEvidenceProfile(
    app_version="1.46.7.940945420",
    app_user_agent="NotebookLM/1.46.7.940945420 (Android 16; sdk_gphone64_arm64)",
    finalize_user_agent="Dart/3.13 (dart:io)",
)


__all__ = ["ANDROID_EVIDENCE_PROFILE", "AndroidEvidenceProfile"]
