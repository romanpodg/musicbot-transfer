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

from ..domain import (
    Album,
    AlbumMatchResult,
    Artist,
    ArtistMatchResult,
    MatchResult,
    Track,
)
from ..enums import MatchMethod, MatchOutcome
from .normalization import (
    NormalizedTrack,
    normalize_text,
    normalize_track,
    strip_version_qualifiers,
)
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
    return not (source.base_title and candidate.base_title and source.base_title == candidate.base_title)


class AlbumMatcher:
    """Match source albums against destination candidates."""

    def __init__(self, policy: MatchingPolicy | None = None) -> None:
        self._policy = policy or MatchingPolicy()

    @property
    def policy(self) -> MatchingPolicy:
        """Return the immutable policy in use."""

        return self._policy

    def match_by_direct_identifier(
        self, source: Album, destination_id: str | None
    ) -> AlbumMatchResult | None:
        """Strategy 1: direct identifier reuse."""

        if not destination_id:
            return None
        from dataclasses import replace

        return AlbumMatchResult(
            source=source,
            destination=replace(source, source_id=destination_id),
            score=1.0,
            method=MatchMethod.DIRECT_ID,
            outcome=MatchOutcome.MATCHED,
            reasons=("direct_identifier_reused",),
        )

    def match_by_upc(
        self, source: Album, candidates: list[Album]
    ) -> AlbumMatchResult | None:
        """Strategy 2: strong portable identifier (UPC)."""

        if not source.upc:
            return None
        clean_source_upc = str(source.upc).strip()
        if not clean_source_upc:
            return None
        upc_matches = [
            c for c in candidates if c.upc and str(c.upc).strip() == clean_source_upc
        ]
        if not upc_matches:
            return None
        if len(upc_matches) == 1:
            return AlbumMatchResult(
                source=source,
                destination=upc_matches[0],
                score=1.0,
                method=MatchMethod.EXACT_METADATA,
                outcome=MatchOutcome.MATCHED,
                reasons=("upc_equal",),
                candidates=tuple(upc_matches),
            )
        source_title_norm = normalize_text(source.title)
        exact_title_matches = [
            c for c in upc_matches if normalize_text(c.title) == source_title_norm
        ]
        if len(exact_title_matches) == 1:
            return AlbumMatchResult(
                source=source,
                destination=exact_title_matches[0],
                score=1.0,
                method=MatchMethod.EXACT_METADATA,
                outcome=MatchOutcome.MATCHED,
                reasons=("upc_equal", "exact_title"),
                candidates=tuple(upc_matches),
            )
        return AlbumMatchResult(
            source=source,
            destination=None,
            score=1.0,
            method=MatchMethod.EXACT_METADATA,
            outcome=MatchOutcome.AMBIGUOUS,
            reasons=("upc_equal", "multiple_exact_candidates"),
            candidates=tuple(upc_matches),
        )

    def match_by_metadata(
        self, source: Album, candidates: list[Album]
    ) -> AlbumMatchResult:
        """Strategies 3-4: exact, then normalized title and artist comparison."""

        if not candidates:
            return AlbumMatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("no_candidates",),
            )

        source_norm_title = normalize_text(source.title)
        source_base_title = strip_version_qualifiers(source.title)
        source_artists = [normalize_text(name) for name in source.artist_names if name]
        source_primary = source_artists[0] if source_artists else ""

        scored: list[tuple[Album, float, MatchMethod, list[str]]] = []
        for candidate in candidates[: self._policy.max_candidates]:
            cand_norm_title = normalize_text(candidate.title)
            cand_base_title = strip_version_qualifiers(candidate.title)
            cand_artists = [normalize_text(name) for name in candidate.artist_names if name]
            cand_primary = cand_artists[0] if cand_artists else ""

            # Classify artist evidence:
            # 1. Confirmed agreement (both sides have artist data and match)
            # 2. Confirmed disagreement (both sides have artist data and do NOT match)
            # 3. Missing evidence (at least one side lacks artist metadata)
            has_source_artists = bool(source_artists)
            has_cand_artists = bool(cand_artists)

            artists_exact = False
            artist_overlap = False
            missing_artist_evidence = False

            if has_source_artists and has_cand_artists:
                if source_primary == cand_primary:
                    artists_exact = True
                elif any(a in cand_artists for a in source_artists) or any(
                    a in source_artists for a in cand_artists
                ):
                    artist_overlap = True
                else:
                    # Confirmed artist disagreement -> reject candidate entirely
                    continue
            else:
                missing_artist_evidence = True

            if source_norm_title and cand_norm_title and source_norm_title == cand_norm_title:
                if artists_exact:
                    scored.append(
                        (candidate, 1.0, MatchMethod.EXACT_METADATA, ["exact_title_and_artist"])
                    )
                elif artist_overlap:
                    scored.append(
                        (
                            candidate,
                            0.95,
                            MatchMethod.NORMALIZED_METADATA,
                            ["exact_title_artist_overlap"],
                        )
                    )
                elif missing_artist_evidence:
                    # Title matches but artist evidence is missing -> cannot prove identity.
                    # Score 0.70 falls in ambiguous band (0.62 <= score < 0.88), outcome AMBIGUOUS.
                    scored.append(
                        (
                            candidate,
                            0.70,
                            MatchMethod.NORMALIZED_METADATA,
                            ["exact_title_missing_artist_evidence"],
                        )
                    )
            elif (
                source_base_title
                and cand_base_title
                and source_base_title == cand_base_title
            ):
                if artists_exact:
                    scored.append(
                        (
                            candidate,
                            0.90,
                            MatchMethod.NORMALIZED_METADATA,
                            ["base_title_and_artist"],
                        )
                    )
                elif artist_overlap:
                    scored.append(
                        (
                            candidate,
                            0.85,
                            MatchMethod.NORMALIZED_METADATA,
                            ["base_title_artist_overlap"],
                        )
                    )
                elif missing_artist_evidence:
                    # Base title without positive artist evidence is never enough for MATCHED.
                    pass

        if not scored:
            return AlbumMatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("no_matching_candidates",),
            )

        scored.sort(key=lambda item: item[1], reverse=True)
        best_album, best_score, best_method, best_reasons = scored[0]
        outcome = self._policy.outcome_for(best_score)
        chosen = best_album if outcome is MatchOutcome.MATCHED else None

        if len(scored) > 1:
            runner_up_score = scored[1][1]
            if outcome is MatchOutcome.MATCHED and best_score - runner_up_score < 0.02:
                outcome = MatchOutcome.AMBIGUOUS
                chosen = None
                best_reasons = list(best_reasons) + ["ambiguous_candidates"]

        return AlbumMatchResult(
            source=source,
            destination=chosen,
            score=best_score,
            method=best_method,
            outcome=outcome,
            reasons=tuple(best_reasons),
            candidates=tuple(album for album, _, _, _ in scored[:3]),
        )

    def match(
        self,
        source: Album,
        candidates: list[Album],
        *,
        reusable_destination_id: str | None = None,
    ) -> AlbumMatchResult:
        """Match one source album trying strategies in order of confidence."""

        direct = self.match_by_direct_identifier(source, reusable_destination_id)
        if direct is not None:
            return direct
        by_upc = self.match_by_upc(source, candidates)
        if by_upc is not None:
            return by_upc
        return self.match_by_metadata(source, candidates)


class ArtistMatcher:
    """Match source artists against destination candidates."""

    def __init__(self, policy: MatchingPolicy | None = None) -> None:
        self._policy = policy or MatchingPolicy()

    @property
    def policy(self) -> MatchingPolicy:
        """Return the immutable policy in use."""

        return self._policy

    def match_by_direct_identifier(
        self, source: Artist, destination_id: str | None
    ) -> ArtistMatchResult | None:
        """Strategy 1: direct identifier reuse."""

        if not destination_id:
            return None
        from dataclasses import replace

        return ArtistMatchResult(
            source=source,
            destination=replace(source, source_id=destination_id),
            score=1.0,
            method=MatchMethod.DIRECT_ID,
            outcome=MatchOutcome.MATCHED,
            reasons=("direct_identifier_reused",),
        )

    def match_by_name(
        self, source: Artist, candidates: list[Artist]
    ) -> ArtistMatchResult:
        """Strategy 2: exact normalized artist name comparison."""

        if not candidates:
            return ArtistMatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("no_candidates",),
            )

        source_norm = normalize_text(source.name)
        if not source_norm:
            return ArtistMatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("empty_source_name",),
            )

        exact_matches: list[Artist] = []
        for candidate in candidates[: self._policy.max_candidates]:
            cand_norm = normalize_text(candidate.name)
            if cand_norm == source_norm:
                exact_matches.append(candidate)

        if not exact_matches:
            return ArtistMatchResult(
                source=source,
                outcome=MatchOutcome.NOT_FOUND,
                method=MatchMethod.NONE,
                reasons=("name_mismatch",),
            )

        if len(exact_matches) == 1:
            return ArtistMatchResult(
                source=source,
                destination=exact_matches[0],
                score=1.0,
                method=MatchMethod.EXACT_METADATA,
                outcome=MatchOutcome.MATCHED,
                reasons=("exact_name",),
                candidates=tuple(exact_matches),
            )

        # Name collision: multiple destination candidates share the same normalized name
        return ArtistMatchResult(
            source=source,
            destination=None,
            score=1.0,
            method=MatchMethod.EXACT_METADATA,
            outcome=MatchOutcome.AMBIGUOUS,
            reasons=("multiple_exact_name_candidates",),
            candidates=tuple(exact_matches),
        )

    def match(
        self,
        source: Artist,
        candidates: list[Artist],
        *,
        reusable_destination_id: str | None = None,
    ) -> ArtistMatchResult:
        """Match one source artist trying strategies in order of confidence."""

        direct = self.match_by_direct_identifier(source, reusable_destination_id)
        if direct is not None:
            return direct
        return self.match_by_name(source, candidates)
