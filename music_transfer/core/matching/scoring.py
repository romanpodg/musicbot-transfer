"""Match scoring with explicit, explainable components.

The score is a weighted blend of independent signals.  Every component also
emits machine-readable ``reasons`` and user-relevant ``warnings`` so that:

* an ambiguous result can be explained rather than guessed at;
* a future review screen can show *why* two tracks were not merged;
* explicit/clean and version differences are never silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..enums import MatchMethod
from .normalization import NormalizedTrack


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """Relative importance of each comparison signal (weights need not sum to 1)."""

    title: float = 0.40
    artists: float = 0.28
    duration: float = 0.14
    album: float = 0.18

    def total(self) -> float:
        """Return the sum of all weights, used to scale the final score."""

        return self.title + self.artists + self.duration + self.album


#: Duration tolerances, in milliseconds.
DURATION_EXACT_TOLERANCE_MS = 2_000
DURATION_MISMATCH_MS = 15_000

#: Penalties applied to the blended score.
VERSION_MISMATCH_PENALTY = 0.20
EXPLICIT_MISMATCH_PENALTY = 0.15
EXPLICIT_TO_CLEAN_PENALTY = 0.35


@dataclass(slots=True)
class ScoreBreakdown:
    """One candidate's score plus the reasons behind it."""

    score: float = 0.0
    title_score: float = 0.0
    artist_score: float = 0.0
    duration_score: float = 0.0
    album_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible values."""

        return {
            "score": round(self.score, 4),
            "title_score": round(self.title_score, 4),
            "artist_score": round(self.artist_score, 4),
            "duration_score": round(self.duration_score, 4),
            "album_score": round(self.album_score, 4),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def similarity(left: str, right: str) -> float:
    """Return a fuzzy similarity ratio in ``[0.0, 1.0]``.

    Uses the standard-library ``difflib`` so the core keeps zero third-party
    dependencies.
    """

    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def score_title(source: NormalizedTrack, candidate: NormalizedTrack) -> float:
    """Compare titles from strictest to loosest."""

    if source.raw_title and candidate.raw_title and source.raw_title.casefold() == candidate.raw_title.casefold():
        return 1.0
    if source.title and source.title == candidate.title:
        return 1.0
    if source.base_title and source.base_title == candidate.base_title:
        return 0.96
    return similarity(source.base_title, candidate.base_title)


def score_artists(source: NormalizedTrack, candidate: NormalizedTrack) -> float:
    """Compare artist credits.

    Primary artists matter most.  A featured-artist difference is tolerated
    because platforms disagree about whether to repeat it in the artist field.
    """

    source_primary = set(source.primary_artists)
    candidate_primary = set(candidate.primary_artists)
    if source_primary and candidate_primary:
        if source_primary == candidate_primary:
            return 1.0
        if source_primary & candidate_primary:
            return 0.90
    source_all = set(source.all_artists)
    candidate_all = set(candidate.all_artists)
    if source_all and candidate_all:
        if source_all == candidate_all:
            return 0.95
        union = source_all | candidate_all
        overlap = len(source_all & candidate_all) / len(union)
        if overlap > 0:
            return min(0.85, 0.55 + overlap * 0.4)
    if source.primary_artists and candidate.primary_artists:
        return similarity(source.primary_artists[0], candidate.primary_artists[0]) * 0.9
    return 0.0


def score_duration(source: NormalizedTrack, candidate: NormalizedTrack) -> float:
    """Compare durations with a linear falloff between tolerance bounds."""

    source_ms = source.duration_ms
    candidate_ms = candidate.duration_ms
    if source_ms is None or candidate_ms is None:
        # Unknown duration is not evidence of a mismatch.
        return 0.6
    difference = abs(source_ms - candidate_ms)
    if difference <= DURATION_EXACT_TOLERANCE_MS:
        return 1.0
    if difference >= DURATION_MISMATCH_MS:
        return 0.0
    span = DURATION_MISMATCH_MS - DURATION_EXACT_TOLERANCE_MS
    return 1.0 - (difference - DURATION_EXACT_TOLERANCE_MS) / span


def score_album(source: NormalizedTrack, candidate: NormalizedTrack) -> float:
    """Compare album titles, tolerating a missing album on either side."""

    if not source.album_title or not candidate.album_title:
        return 0.6
    if source.album_title == candidate.album_title:
        return 1.0
    return similarity(source.album_title, candidate.album_title)


def score_candidate(
    source: NormalizedTrack,
    candidate: NormalizedTrack,
    *,
    weights: ScoreWeights | None = None,
) -> ScoreBreakdown:
    """Score one destination candidate against one source track.

    Args:
        source: The normalized source track.
        candidate: The normalized destination candidate.
        weights: Optional component weights.

    Returns:
        A :class:`ScoreBreakdown` whose ``score`` is in ``[0.0, 1.0]``.
    """

    weights = weights or ScoreWeights()
    breakdown = ScoreBreakdown(
        title_score=score_title(source, candidate),
        artist_score=score_artists(source, candidate),
        duration_score=score_duration(source, candidate),
        album_score=score_album(source, candidate),
    )
    total = weights.total() or 1.0
    blended = (
        breakdown.title_score * weights.title
        + breakdown.artist_score * weights.artists
        + breakdown.duration_score * weights.duration
        + breakdown.album_score * weights.album
    ) / total
    breakdown.score = max(0.0, min(1.0, blended))
    _collect_reasons(source, candidate, breakdown)
    _apply_penalties(source, candidate, breakdown)
    return breakdown


def _collect_reasons(
    source: NormalizedTrack,
    candidate: NormalizedTrack,
    breakdown: ScoreBreakdown,
) -> None:
    """Record short, machine-readable explanations for the score."""

    if candidate.isrc and source.isrc and candidate.isrc == source.isrc:
        breakdown.reasons.append("isrc_equal")
    if breakdown.title_score >= 1.0:
        breakdown.reasons.append("title_equal")
    elif breakdown.title_score >= 0.9:
        breakdown.reasons.append("title_normalized_equal")
    elif breakdown.title_score > 0.0:
        breakdown.reasons.append("title_fuzzy")
    else:
        breakdown.reasons.append("title_mismatch")
    if breakdown.artist_score >= 1.0:
        breakdown.reasons.append("artists_equal")
    elif breakdown.artist_score >= 0.8:
        breakdown.reasons.append("artists_partial")
    else:
        breakdown.reasons.append("artists_mismatch")
    if breakdown.duration_score >= 1.0:
        breakdown.reasons.append("duration_equal")
    elif breakdown.duration_score <= 0.0:
        breakdown.reasons.append("duration_mismatch")
    elif breakdown.duration_score < 0.6:
        breakdown.reasons.append("duration_unknown")
    if breakdown.album_score >= 1.0:
        breakdown.reasons.append("album_equal")
    elif breakdown.album_score < 0.6:
        breakdown.reasons.append("album_mismatch")


def _apply_penalties(
    source: NormalizedTrack,
    candidate: NormalizedTrack,
    breakdown: ScoreBreakdown,
) -> None:
    """Reduce confidence for version and explicit/clean differences."""

    source_versions = set(source.version_qualifiers) - {"explicit", "clean"}
    candidate_versions = set(candidate.version_qualifiers) - {"explicit", "clean"}
    if source_versions != candidate_versions:
        breakdown.score = max(0.0, breakdown.score - VERSION_MISMATCH_PENALTY)
        breakdown.warnings.append("version_mismatch")
        breakdown.reasons.append("version_mismatch")
    if (
        source.explicit is not None
        and candidate.explicit is not None
        and source.explicit != candidate.explicit
    ):
        if source.explicit and not candidate.explicit:
            # Replacing an explicit recording with a clean one is the most
            # damaging silent substitution, so it is penalized hardest.
            breakdown.score = max(
                0.0, breakdown.score - EXPLICIT_TO_CLEAN_PENALTY
            )
            breakdown.warnings.append("explicit_replaced_by_clean")
            breakdown.reasons.append("explicit_mismatch")
        else:
            breakdown.score = max(0.0, breakdown.score - EXPLICIT_MISMATCH_PENALTY)
            breakdown.warnings.append("clean_replaced_by_explicit")
            breakdown.reasons.append("explicit_mismatch")
    return None


def method_for(
    source: NormalizedTrack, candidate: NormalizedTrack, breakdown: ScoreBreakdown
) -> MatchMethod:
    """Return which strategy produced this score.

    Used for reporting and for the future "how was this matched?" screen.
    """

    if source.isrc and candidate.isrc and source.isrc == candidate.isrc:
        return MatchMethod.ISRC
    if (
        breakdown.title_score >= 1.0
        and breakdown.artist_score >= 1.0
        and breakdown.album_score >= 1.0
    ):
        return MatchMethod.EXACT_METADATA
    if breakdown.title_score >= 0.96 and breakdown.artist_score >= 0.9:
        return MatchMethod.NORMALIZED_METADATA
    return MatchMethod.FUZZY_METADATA
