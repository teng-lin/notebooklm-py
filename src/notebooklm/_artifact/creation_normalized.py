"""Closed normalized domain requests consumed by artifact protocol hooks.

Each numeric selection is an explicit client enum value, with defaults already
resolved. Keeping the numeric value preserves Web's historical acceptance of
compatible enum-like values; Android's stricter policy validates enum membership.
There is no backend discriminator or extensible options mapping in this seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class _NormalizedCreation:
    notebook_id: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedAudio(_NormalizedCreation):
    language: str
    instructions: str | None
    format_code: int
    length_code: int
    later: bool = False


@dataclass(frozen=True)
class NormalizedVideo(_NormalizedCreation):
    language: str
    instructions: str | None
    format_code: int
    style_code: int
    style_prompt: str | None


@dataclass(frozen=True)
class NormalizedCinematicVideo(_NormalizedCreation):
    language: str
    instructions: str | None


@dataclass(frozen=True)
class NormalizedReport(_NormalizedCreation):
    language: str
    title: str
    description: str
    directive: str


@dataclass(frozen=True)
class NormalizedQuiz(_NormalizedCreation):
    instructions: str | None
    quantity_code: int
    difficulty_code: int


@dataclass(frozen=True)
class NormalizedFlashcards(_NormalizedCreation):
    instructions: str | None
    quantity_code: int
    difficulty_code: int


@dataclass(frozen=True)
class NormalizedInfographic(_NormalizedCreation):
    language: str
    instructions: str | None
    orientation_code: int
    detail_code: int
    style_code: int


@dataclass(frozen=True)
class NormalizedSlideDeck(_NormalizedCreation):
    language: str
    instructions: str | None
    format_code: int
    length_code: int


@dataclass(frozen=True)
class NormalizedDataTable(_NormalizedCreation):
    language: str
    instructions: str | None


@dataclass(frozen=True)
class NormalizedInteractiveMindMap(_NormalizedCreation):
    language: str
    instructions: str | None


NormalizedArtifactCreationRequest: TypeAlias = (
    NormalizedAudio
    | NormalizedVideo
    | NormalizedCinematicVideo
    | NormalizedReport
    | NormalizedQuiz
    | NormalizedFlashcards
    | NormalizedInfographic
    | NormalizedSlideDeck
    | NormalizedDataTable
    | NormalizedInteractiveMindMap
)
