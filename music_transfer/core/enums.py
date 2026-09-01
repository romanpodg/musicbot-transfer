"""Enumerations shared by every layer of the music transfer core.

Keeping these values in one module means the core can compare platforms,
entity types, and statuses without string literals and without importing any
platform SDK.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """A music service that can act as a transfer source or destination.

    Only ``TIDAL`` has a working adapter in this phase.  The remaining members
    exist so that registry lookups, capability negotiation, and error messages
    can be written today without platform-name conditionals appearing later.
    """

    TIDAL = "tidal"
    SPOTIFY = "spotify"
    APPLE_MUSIC = "apple_music"
    DEEZER = "deezer"
    YOUTUBE_MUSIC = "youtube_music"


class EntityType(StrEnum):
    """The library object kinds that a transfer can carry."""

    TRACK = "track"
    ALBUM = "album"
    ARTIST = "artist"
    PLAYLIST = "playlist"
    PLAYLIST_ITEM = "playlist_item"
    VIDEO = "video"
    MIX = "mix"
    FOLDER = "folder"


class ContentType(StrEnum):
    """User-selectable library sections offered by the application layer.

    Distinct from :class:`EntityType` because one content type (a playlist)
    is materialised as several transfer items (a playlist plus its items).
    """

    LIKED_TRACKS = "liked_tracks"
    SAVED_ALBUMS = "saved_albums"
    FOLLOWED_ARTISTS = "followed_artists"
    PLAYLISTS = "playlists"
    VIDEOS = "videos"
    MIXES = "mixes"


class JobStatus(StrEnum):
    """The states of the transfer lifecycle.

    Valid transitions are declared in :mod:`music_transfer.core.transfer.lifecycle`.
    Do not compare these values ad hoc; use the state machine so that illegal
    transitions are rejected consistently.
    """

    CREATED = "created"
    AUTHENTICATING = "authenticating"
    EXPORTING = "exporting"
    NORMALIZING = "normalizing"
    MATCHING = "matching"
    PLANNING = "planning"
    WAITING_CONFIRMATION = "waiting_confirmation"
    IMPORTING = "importing"
    VERIFYING = "verifying"
    COMPLETED = "completed"

    # Terminal / exceptional states.
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationStatus(StrEnum):
    """The outcome of post-write destination verification.

    Distinct from :class:`JobStatus` (which tracks overall workflow progression):
    a job can reach ``COMPLETED`` status while verification status is ``FAILED``
    if execution finished but destination verification discovered discrepancies.
    """

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"


class ItemStatus(StrEnum):
    """The per-item lifecycle used for resumable, auditable transfers.

    Failure meanings stay explicit on purpose.  Collapsing them into a single
    ``ERROR`` state would make retry selection and user reporting impossible.
    """

    PENDING = "pending"
    MATCHED = "matched"
    TRANSFERRED = "transferred"

    ALREADY_EXISTS = "already_exists"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    SKIPPED = "skipped"

    FAILED = "failed"


#: Statuses that mean "this item is finished and must not be replayed".
TERMINAL_ITEM_STATUSES: frozenset[ItemStatus] = frozenset(
    {
        ItemStatus.TRANSFERRED,
        ItemStatus.ALREADY_EXISTS,
        ItemStatus.SKIPPED,
    }
)

#: Statuses that mean "this item may be selected again by a retry job".
RETRYABLE_ITEM_STATUSES: frozenset[ItemStatus] = frozenset(
    {
        ItemStatus.PENDING,
        ItemStatus.MATCHED,
        ItemStatus.FAILED,
        ItemStatus.NOT_FOUND,
        ItemStatus.AMBIGUOUS,
        ItemStatus.UNAVAILABLE,
    }
)


class MatchMethod(StrEnum):
    """How a destination candidate was identified."""

    ISRC = "isrc"
    DIRECT_ID = "direct_id"
    EXACT_METADATA = "exact_metadata"
    NORMALIZED_METADATA = "normalized_metadata"
    FUZZY_METADATA = "fuzzy_metadata"
    NONE = "none"


class MatchOutcome(StrEnum):
    """The coarse, UI-facing classification of a match attempt."""

    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class OrderingMode(StrEnum):
    """The logical order a user asks for, independent of any platform."""

    DATE_ADDED_NEWEST_FIRST = "date_added_newest_first"
    DATE_ADDED_OLDEST_FIRST = "date_added_oldest_first"
    ALPHABETICAL = "alphabetical"
    ARTIST = "artist"
    ALBUM = "album"
    SOURCE_ORDER = "source_order"


class InsertionBehavior(StrEnum):
    """How a destination library visually orders newly written items.

    ``APPEND``: the newest write appears at the end of the visible list.
    ``PREPEND``: the newest write appears at the top of the visible list
    (the behaviour of TIDAL favourites).

    The executor needs this to convert a *requested logical order* into the
    *actual write order*.
    """

    APPEND = "append"
    PREPEND = "prepend"


class OperationKind(StrEnum):
    """Safety classification of an adapter operation (see the read/write boundary)."""

    READ = "read"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


class TransferOperation(StrEnum):
    """The planned mutation action to be performed at the destination.

    Separates 'what happened to the item' (ItemStatus) from 'what mutation
    should happen next' (TransferOperation).
    """

    NONE = "none"
    SAVE_TRACK = "save_track"
    SAVE_ALBUM = "save_album"
    FOLLOW_ARTIST = "follow_artist"
    CREATE_PLAYLIST = "create_playlist"
    ADD_PLAYLIST_ITEM = "add_playlist_item"


class MutationState(StrEnum):
    """Durable intent tracking for non-idempotent destination operations.

    Recorded before an external mutating call so recovery can determine
    whether a write was in flight if the process crashed.
    """

    NONE = "none"
    IN_FLIGHT = "in_flight"


class PreconditionExpectation(StrEnum):
    """The expected presence of an entity at the destination before execution."""

    PRESENT = "present"
    ABSENT = "absent"


class DestinationPresence(StrEnum):
    """The observed presence of an entity at the destination (Phase 1.4C).

    Distinguishes definitively observed presence, definitively observed absence,
    and unknown absence/presence due to unread, partial, or unsupported state.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"



