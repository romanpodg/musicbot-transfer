"""Structured matching results.

The matcher never decides what to show a user; it produces a score, a method,
reasons, and warnings.  A configurable threshold in
:class:`~music_transfer.core.matching.matcher.MatchingPolicy` turns the score
into an outcome, and the *application* layer decides whether an ambiguous match
needs a review screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import MatchMethod, MatchOutcome
from .album import Album
from .artist import Artist
from .track import Track


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The outcome of matching one source track against a destination catalog.

    Attributes:
        source: The source track.  Normalization never mutates it (Invariant L).
        destination: The chosen destination track, or ``None``.
        score: Confidence in ``[0.0, 1.0]``.
        method: How the match was established.
        outcome: Coarse classification derived from the score and policy.
        reasons: Short, machine-oriented explanations (``isrc_equal``,
            ``duration_mismatch``).  Suitable for logs and debug UIs.
        warnings: Human-relevant cautions (``explicit_vs_clean``,
            ``version_mismatch``) that a UI may surface to the user.
        candidates: Alternative high-scoring matches when the outcome is
            ambiguous, so a future review screen can offer choices.
    """

    source: Track
    destination: Track | None = None
    score: float = 0.0
    method: MatchMethod = MatchMethod.NONE
    outcome: MatchOutcome = MatchOutcome.NOT_FOUND
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    candidates: tuple[Track, ...] = ()

    @property
    def matched(self) -> bool:
        """Return whether a usable destination was found."""

        return self.destination is not None and self.outcome is MatchOutcome.MATCHED

    @property
    def destination_id(self) -> str | None:
        """Return the chosen destination identifier, if any."""

        return self.destination.source_id if self.destination is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "source_id": self.source.source_id,
            "destination_id": self.destination_id,
            "score": round(self.score, 4),
            "method": str(self.method),
            "outcome": str(self.outcome),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "candidate_ids": [track.source_id for track in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A destination candidate together with its match score and reasons."""

    track: Track
    score: float
    method: MatchMethod
    reasons: tuple[str, ...] = field(default=())
    warnings: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "track_id": self.track.source_id,
            "score": round(self.score, 4),
            "method": str(self.method),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class AlbumMatchResult:
    """The outcome of matching one source album against a destination catalog."""

    source: Album
    destination: Album | None = None
    score: float = 0.0
    method: MatchMethod = MatchMethod.NONE
    outcome: MatchOutcome = MatchOutcome.NOT_FOUND
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    candidates: tuple[Album, ...] = ()

    @property
    def matched(self) -> bool:
        """Return whether a usable destination album was found."""

        return self.destination is not None and self.outcome is MatchOutcome.MATCHED

    @property
    def destination_id(self) -> str | None:
        """Return the chosen destination identifier, if any."""

        return self.destination.source_id if self.destination is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "source_id": self.source.source_id,
            "destination_id": self.destination_id,
            "score": round(self.score, 4),
            "method": str(self.method),
            "outcome": str(self.outcome),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "candidate_ids": [album.source_id for album in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class ArtistMatchResult:
    """The outcome of matching one source artist against a destination catalog."""

    source: Artist
    destination: Artist | None = None
    score: float = 0.0
    method: MatchMethod = MatchMethod.NONE
    outcome: MatchOutcome = MatchOutcome.NOT_FOUND
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    candidates: tuple[Artist, ...] = ()

    @property
    def matched(self) -> bool:
        """Return whether a usable destination artist was found."""

        return self.destination is not None and self.outcome is MatchOutcome.MATCHED

    @property
    def destination_id(self) -> str | None:
        """Return the chosen destination identifier, if any."""

        return self.destination.source_id if self.destination is not None else None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "source_id": self.source.source_id,
            "destination_id": self.destination_id,
            "score": round(self.score, 4),
            "method": str(self.method),
            "outcome": str(self.outcome),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "candidate_ids": [artist.source_id for artist in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class IdentifierResolution:
    """The resolved destination identifier and match outcome for a single transfer entity."""

    destination_id: str | None
    match_method: MatchMethod
    match_score: float
    outcome: MatchOutcome
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_match(
        cls, match: MatchResult | AlbumMatchResult | ArtistMatchResult
    ) -> IdentifierResolution:
        """Construct a resolution result from any entity match result."""

        return cls(
            destination_id=match.destination_id,
            match_method=match.method,
            match_score=match.score,
            outcome=match.outcome,
            reasons=match.reasons,
            warnings=match.warnings,
        )
