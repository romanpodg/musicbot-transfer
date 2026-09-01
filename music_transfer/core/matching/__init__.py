"""Reusable matching subsystem.

Consumers: the transfer planner (to build a plan) and, later, a dedicated
matching service for cross-platform transfers.  Nothing here knows about any
specific music platform.
"""

from __future__ import annotations

from .matcher import AlbumMatcher, ArtistMatcher, MatchingPolicy, TrackMatcher
from .normalization import (
    NormalizedTrack,
    detect_explicit_flag,
    find_version_qualifiers,
    normalize_isrc,
    normalize_light,
    normalize_text,
    normalize_track,
    split_artists,
    strip_version_qualifiers,
)
from .scoring import (
    DURATION_EXACT_TOLERANCE_MS,
    DURATION_MISMATCH_MS,
    ScoreBreakdown,
    ScoreWeights,
    method_for,
    score_album,
    score_artists,
    score_candidate,
    score_duration,
    score_title,
    similarity,
)

__all__ = [
    "DURATION_EXACT_TOLERANCE_MS",
    "DURATION_MISMATCH_MS",
    "AlbumMatcher",
    "ArtistMatcher",
    "MatchingPolicy",
    "NormalizedTrack",
    "ScoreBreakdown",
    "ScoreWeights",
    "TrackMatcher",
    "detect_explicit_flag",
    "find_version_qualifiers",
    "method_for",
    "normalize_isrc",
    "normalize_light",
    "normalize_text",
    "normalize_track",
    "score_album",
    "score_artists",
    "score_candidate",
    "score_duration",
    "score_title",
    "similarity",
    "split_artists",
    "strip_version_qualifiers",
]
