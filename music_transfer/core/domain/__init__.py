"""Universal domain models shared by every layer.

These modules must not import Telegram, FastAPI, Redis, or any music-platform
SDK.  Platform-specific data belongs in ``metadata`` dictionaries, never in
dedicated core fields.
"""

from __future__ import annotations

from .account import Account, AccountProfile
from .album import Album
from .artist import Artist, artist_names
from .library import LibraryRecord, LibrarySnapshot
from .matching import (
    AlbumMatchResult,
    ArtistMatchResult,
    IdentifierResolution,
    MatchResult,
    ScoredCandidate,
)
from .playlist import Playlist, PlaylistItem, PlaylistMediaRef
from .track import Track
from .transfer import (
    PlanPrecondition,
    TransferItem,
    TransferJob,
    TransferPlan,
    TransferPlanItem,
    TransferPlanSummary,
    TransferProgress,
    TransferReport,
    TransferSettings,
    new_identifier,
    utc_now,
)
from .verification import SequenceComparison, VerificationResult

__all__ = [
    "Account",
    "AccountProfile",
    "Album",
    "AlbumMatchResult",
    "Artist",
    "ArtistMatchResult",
    "IdentifierResolution",
    "LibraryRecord",
    "LibrarySnapshot",
    "MatchResult",
    "PlanPrecondition",
    "Playlist",
    "PlaylistItem",
    "PlaylistMediaRef",
    "ScoredCandidate",
    "SequenceComparison",
    "Track",
    "TransferItem",
    "TransferJob",
    "TransferPlan",
    "TransferPlanItem",
    "TransferPlanSummary",
    "TransferProgress",
    "TransferReport",
    "TransferSettings",
    "VerificationResult",
    "artist_names",
    "new_identifier",
    "utc_now",
]
