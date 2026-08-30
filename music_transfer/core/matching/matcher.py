"""The matching subsystem: strategies, thresholds, and results.

Strategies are applied most-trustworthy first:

1. **ISRC** - an identical valid ISRC identifies the same recording, so the
   score is maximal and no metadata comparison can lower it.
2. **DIRECT_ID** - a source identifier the destination can reuse verbatim
   (TIDAL -> TIDAL).  This is a capability query on the adapter, not a
   platform-name check in the core.
3. **Exact metadata** - raw title/artist/album agreement before normalization.
4. **Normalized metadata** - agreement after qualifier and separator handling.
5. **Fuzzy metadata** - similarity only, used when nothing else is conclusive.

The matcher produces :class:`MatchResult` values.  It never decides what a UI
should do; :class:`MatchingPolicy` only maps scores to outcomes, and the
application layer chooses what to show.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import MatchResult, Track
from ..enums import MatchMethod, MatchOutcome
from .normalization import NormalizedTrack, normalize_track
from .scoring import ScoreBreakdown, ScoreWeights, method_for, score_candidate


@dataclass(frozen=True, slots=True)
class MatchingPolicy:
    """Configurable thresholds and switches for matching.

    The defaults encode the intended product behaviour:

    * ``>= high_confidence`` (0.88) - transfer automatically;
    * ``>= ambiguous_threshold`` (0.62) - flag for user review;
    * below that - report as not found.
    """

    high_confidence: float = 0.88
    ambiguous_threshold: float = 0.62
    fuzzy_enabled: bool = True
    fuzzy_floor: float = 0.55
    allow_explicit_to_clean_fallback: bool = False
    max_candidates: int = 5
    weights: ScoreWeights = ScoreWeights()

    def outcome_for(self, score: float) -> MatchOutcome:
        """Map a score onto the coarse outcome used by planners and UIs."""

        if score >= self.high_confidence:
            return MatchOutcome.MATCHED
        if score >= self.ambiguous_threshold:
            return MatchOutcome.AMBIGUOUS
        return MatchOutcome.NOT_FOUND


class TrackMatcher:
    """Match source tracks against destination candidates."""

    def __init__(self, policy: MatchingPolicy | None = None) -> None:
        self._policy = policy or MatchingPolicy()

    @property
    def policy(self) -> MatchingPolicy:
        """Return the immutable policy in use."""

        return self._policy

    # -- individual strategies --------------------------------------------

    def match_by_isrc(self, source: Track, candidates: list[Track]) -> MatchResult | None:
        """Strategy 1: identical valid ISRC on both sides."""

        source_isrc = normalize_isrc_or_none(source)
        if source_isrc is None:
            return None
        for candidate in candidates:
            if normalize_isrc_or_none(candidate) == source_isrc:
                return MatchResult(
                    source=source,
                    destination=candidate,
                    score=1.0,
                    method=MatchMethod.ISRC,
                    outcome=MatchOutcome.MATCHED,
                    reasons=("isrc_equal",),
                )
        return None

    def match_by_direct_identifier(
        self, source: Track, destination_id: str | None
    ) -> MatchResult | None:
        """Strategy 2: the destination can reuse the source id verbatim."""

        if not destination_id:
            return None
        return MatchResult(
            source=source,
            destination=_with_destination_id(source, destination_id),
            score=1.0,
            method=MatchMethod.DIRECT_ID,
            outcome=MatchOutcome.MATCHED,
            reasons=("direct_identifier_reused",),
        )

    def match_by_metadata(
        self, source: Track, candidates: list[Track]
    ) -> MatchResult:
        """Strategies 3-5: exact, normalized, then fuzzy metadata comparison."""

        if not candidates:
            return MatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("no_candidates",),
            )
        source_normalized = normalize_track(source)
        scored: list[tuple[Track, ScoreBreakdown]] = []
        for candidate in candidates[: self._policy.max_candidates]:
            candidate_normalized = normalize_track(candidate)
            breakdown = score_candidate(
                source_normalized, candidate_normalized, weights=self._policy.weights
            )
            if not self._policy.fuzzy_enabled and _is_fuzzy_only(
                source_normalized, candidate_normalized
            ):
                continue
            if self._policy.fuzzy_enabled and breakdown.title_score < self._policy.fuzzy_floor:
                continue
            scored.append((candidate, breakdown))
        if not scored:
            return MatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("candidates_below_floor",),
            )
        scored.sort(key=lambda pair: pair[1].score, reverse=True)
        best_track, best_breakdown = scored[0]
        best_normalized = normalize_track(best_track)
        score = best_breakdown.score
        if (
            not self._policy.allow_explicit_to_clean_fallback
            and "explicit_replaced_by_clean" in best_breakdown.warnings
        ):
            score = min(score, self._policy.ambiguous_threshold)
        outcome = self._policy.outcome_for(score)
        chosen = best_track if outcome is MatchOutcome.MATCHED else None
        runner_up = scored[1][1].score if len(scored) > 1 else 0.0
        if outcome is MatchOutcome.MATCHED and score - runner_up < 0.02 and len(scored) > 1:
            # Two candidates are effectively tied; a wrong guess here is worse
            # than asking the user, so the result is downgraded to ambiguous.
            outcome = MatchOutcome.AMBIGUOUS
            chosen = None
        return MatchResult(
            source=source,
            destination=chosen,
            score=score,
            method=method_for(source_normalized, best_normalized, best_breakdown),
            outcome=outcome,
            reasons=tuple(best_breakdown.reasons),
            warnings=tuple(best_breakdown.warnings),
            candidates=tuple(track for track, _ in scored[:3]),
        )

    # -- combined entry point ---------------------------------------------

    def match(
        self,
        source: Track,
        candidates: list[Track],
        *,
        reusable_destination_id: str | None = None,
    ) -> MatchResult:
        """Match one source track, trying strategies in order of trustworthiness.

        Args:
            source: The source track.
            candidates: Destination catalog candidates (may be empty).
            reusable_destination_id: Set when the destination adapter confirmed
                it can reuse the source identifier directly.
        """

        direct = self.match_by_direct_identifier(source, reusable_destination_id)
        if direct is not None:
            return direct
        by_isrc = self.match_by_isrc(source, candidates)
        if by_isrc is not None:
            return by_isrc
        return self.match_by_metadata(source, candidates)


def normalize_isrc_or_none(track: Track) -> str | None:
    """Return a track's comparable ISRC, or ``None`` when unusable."""

    from .normalization import normalize_isrc

    return normalize_isrc(track.isrc)


def _with_destination_id(source: Track, destination_id: str) -> Track:
    """Return a copy of ``source`` carrying the destination identifier.

    Used by the direct-identifier strategy so downstream code can read a
    destination id from the matched track without a second lookup.
    """

    from dataclasses import replace

    return replace(source, source_id=destination_id)


def _is_fuzzy_only(source: NormalizedTrack, candidate: NormalizedTrack) -> bool:
    """Return whether a pair only agrees after fuzzy comparison."""

    if source.title and candidate.title and source.title == candidate.title:
        return False
    if source.base_title and candidate.base_title and source.base_title == candidate.base_title:
        return False
    return True
