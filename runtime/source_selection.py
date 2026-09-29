"""Pure, conservative source selection; never infer recording identity from duration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import math
import unicodedata


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class SourceCandidate:
    source: str
    identifier: str
    title: str
    artist: str
    album: str
    duration_s: float
    file_size_bytes: int | None


@dataclass(frozen=True)
class CandidateSummary:
    source: str
    identifier: str
    album: str
    duration_s: float


class SourceSelectionError(Exception):
    """A stable, caller-actionable failure with no credentials or stream URLs."""

    code = "source_selection_error"

    def __init__(self, message: str, candidates: tuple[CandidateSummary, ...] = ()) -> None:
        super().__init__(message)
        self.candidates = candidates


class SourceUnavailable(SourceSelectionError):
    """No candidate passed title, artist, full-length and byte-size checks."""

    code = "unavailable"


class SourceAmbiguous(SourceSelectionError):
    """Multiple distinct stable source IDs remain; user must choose explicitly."""

    code = "ambiguous"


class InvalidSourceSelection(SourceSelectionError):
    """Invalid request, conflicting metadata or unmatched explicit selector."""

    code = "invalid_selection"


def _summary(candidates: Iterable[SourceCandidate]) -> tuple[CandidateSummary, ...]:
    return tuple(
        CandidateSummary(c.source, c.identifier, c.album, c.duration_s)
        for c in candidates
    )


def select_source(
    candidates: Iterable[SourceCandidate],
    *,
    title: str,
    artist: str,
    max_seconds: float,
    max_bytes: int,
    min_seconds: float = 30,
    album: str | None = None,
    source_id: str | None = None,
    source: str | None = None,
) -> SourceCandidate:
    """Choose the sole eligible ID or a uniquely identified explicit release.

    Caller must supply *verified* source metadata including a positive byte size;
    this helper neither queries a provider nor proves that a recording is studio,
    downloadable, licensed, or actually full length. Revalidate downloaded bytes.
    Identical `(source, identifier)` entries are deduplicated; inconsistent metadata
    for the same stable ID is an error, not a first-result tie-breaker.
    """
    if not isinstance(title, str) or not _normalized(title) or not isinstance(artist, str) or not _normalized(artist):
        raise InvalidSourceSelection("Provide a nonempty exact title and artist")
    if (not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds)
            or not isinstance(min_seconds, (int, float)) or not math.isfinite(min_seconds)
            or min_seconds <= 0 or max_seconds < min_seconds
            or type(max_bytes) is not int or max_bytes <= 0):
        raise InvalidSourceSelection("Invalid duration or byte-size limits")
    for label, value in (("album", album), ("source_id", source_id), ("source", source)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InvalidSourceSelection(f"Invalid {label} selector")
    if source is not None and source_id is None:
        raise InvalidSourceSelection("A source selector requires a source_id")

    by_id: dict[tuple[str, str], SourceCandidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, SourceCandidate):
            raise InvalidSourceSelection("Expected SourceCandidate entries")
        if not isinstance(candidate.source, str) or not candidate.source.strip() or not isinstance(candidate.identifier, str) or not candidate.identifier.strip():
            # No stable ID means this result cannot be selected or deduplicated.
            continue
        key = (candidate.source, candidate.identifier)
        previous = by_id.get(key)
        if previous is not None and previous != candidate:
            raise InvalidSourceSelection(f"Conflicting metadata for source ID {key!r}")
        by_id[key] = candidate

    eligible: list[SourceCandidate] = []
    for candidate in by_id.values():
        if (not isinstance(candidate.title, str) or _normalized(candidate.title) != _normalized(title)
                or not isinstance(candidate.artist, str) or _normalized(candidate.artist) != _normalized(artist)
                or not isinstance(candidate.duration_s, (int, float)) or not math.isfinite(candidate.duration_s)
                or not min_seconds <= candidate.duration_s <= max_seconds
                or type(candidate.file_size_bytes) is not int
                or not 0 < candidate.file_size_bytes <= max_bytes):
            continue
        eligible.append(candidate)

    if not eligible:
        raise SourceUnavailable("No exact-title/artist candidate has verified duration and bounded size")
    choices = _summary(eligible)
    selected = eligible
    if source_id is not None:
        selected = [c for c in selected if c.identifier == source_id and (source is None or c.source == source)]
    if album is not None:
        selected = [c for c in selected if isinstance(c.album, str) and _normalized(c.album) == _normalized(album)]
    if not selected:
        raise InvalidSourceSelection("Explicit source ID/album does not match any eligible candidate", choices)
    if len(selected) > 1:
        raise SourceAmbiguous("Choose an exact source ID (and provider if needed) or an unambiguous album", _summary(selected))
    return selected[0]
