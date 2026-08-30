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
from .matching import MatchResult, ScoredCandidate
from .playlist import Playlist, PlaylistItem
from .track import Track
from .transfer import (
    TransferItem,
    TransferJob,
    TransferPlan,
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
    "Artist",
    "LibraryRecord",
    "LibrarySnapshot",
    "MatchResult",
    "Playlist",
    "PlaylistItem",
    "ScoredCandidate",
    "SequenceComparison",
    "Track",
    "TransferItem",
    "TransferJob",
    "TransferPlan",
    "TransferPlanSummary",
    "TransferProgress",
    "TransferReport",
    "TransferSettings",
    "VerificationResult",
    "artist_names",
    "new_identifier",
    "utc_now",
]
