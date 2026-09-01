"""The platform adapter contract and the capability model.

This module is the *only* place where the core describes what a music service
can do.  Adapters live under ``music_transfer/platforms/`` and implement these
interfaces; the transfer engine asks :class:`PlatformCapabilities` instead of
comparing platform names.

Synchronous vs asynchronous
---------------------------

Adapter methods are **synchronous**.  Every current adapter performs blocking
HTTP I/O through a provider SDK, and the proven TIDAL implementation is
synchronous; rewriting it to ``async`` in the same change that introduces the
abstraction would risk the pagination, retry, and resume behaviour that the
project depends on.

Future asynchronous interfaces (Telegram handlers, FastAPI workers) wrap an
adapter in :class:`AsyncPlatformAdapter`, which offloads each call to a worker
thread.  That keeps the event loop unblocked without forcing every adapter to
be rewritten, and it keeps the core free of any asyncio dependency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..domain import (
    AccountProfile,
    Album,
    Artist,
    LibrarySnapshot,
    Playlist,
    PlaylistItem,
    Track,
)
from ..enums import DestinationPresence, EntityType, InsertionBehavior, OperationKind, Platform
from ..errors import InvalidDestinationSectionError, UnsupportedCapabilityError

#: Progress callback signature: ``(section, current, total)``.
ProgressCallback = Any


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """What one platform adapter can actually do.

    The engine consults these flags instead of branching on platform names, and
    a future UI uses them to decide which options to offer.  Unsupported
    capabilities are represented explicitly: an adapter raises
    :class:`~music_transfer.core.errors.UnsupportedCapabilityError` when a
    disabled operation is called.
    """

    platform: Platform

    # --- reads -----------------------------------------------------------
    read_liked_tracks: bool = False
    read_saved_albums: bool = False
    read_followed_artists: bool = False
    read_playlists: bool = False
    read_videos: bool = False
    read_mixes: bool = False
    read_folders: bool = False

    # --- non-destructive writes ------------------------------------------
    write_liked_tracks: bool = False
    write_saved_albums: bool = False
    write_followed_artists: bool = False
    create_playlists: bool = False
    write_playlist_items: bool = False
    write_videos: bool = False
    write_mixes: bool = False
    create_folders: bool = False

    # --- destructive operations (library management only) -----------------
    delete_liked_tracks: bool = False
    delete_saved_albums: bool = False
    delete_followed_artists: bool = False
    delete_playlists: bool = False
    delete_folders: bool = False

    # --- search -----------------------------------------------------------
    search_tracks: bool = False
    search_albums: bool = False
    search_artists: bool = False

    # --- behavioural traits -----------------------------------------------
    #: Whether the platform lets us set an arbitrary historical "date added".
    preserves_custom_added_date: bool = False
    #: Whether a playlist may contain the same track more than once.
    supports_playlist_duplicates: bool = True
    #: Whether the adapter can tell us an item is already present.
    supports_already_exists_detection: bool = True
    #: Whether several items can be appended to a playlist in one request.
    supports_batch_playlist_writes: bool = False
    #: How the destination visually orders newly written items.
    insertion_behavior: InsertionBehavior = InsertionBehavior.APPEND
    #: Whether the catalog distinguishes "not in catalog" from "not available
    #: in this region", letting us report UNAVAILABLE instead of NOT_FOUND.
    distinguishes_region_availability: bool = False
    #: Whether the adapter can expose ISRC values for matching.
    exposes_isrc: bool = False

    def require(self, capability: str) -> None:
        """Raise when a capability is disabled, instead of silently skipping.

        Args:
            capability: The field name to check.

        Raises:
            UnsupportedCapabilityError: If the flag is false or unknown.
        """

        if not hasattr(self, capability):
            raise UnsupportedCapabilityError(
                "capability_unknown", capability=capability
            )
        if not getattr(self, capability):
            raise UnsupportedCapabilityError(
                "capability_unsupported", capability=capability
            )

    def supports(self, capability: str) -> bool:
        """Return whether a named capability is enabled."""

        return bool(getattr(self, capability, False))

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values (for logs and diagnostics)."""

        return {
            "platform": str(self.platform),
            "read_liked_tracks": self.read_liked_tracks,
            "read_saved_albums": self.read_saved_albums,
            "read_followed_artists": self.read_followed_artists,
            "read_playlists": self.read_playlists,
            "read_videos": self.read_videos,
            "read_mixes": self.read_mixes,
            "read_folders": self.read_folders,
            "write_liked_tracks": self.write_liked_tracks,
            "write_saved_albums": self.write_saved_albums,
            "write_followed_artists": self.write_followed_artists,
            "create_playlists": self.create_playlists,
            "write_playlist_items": self.write_playlist_items,
            "write_videos": self.write_videos,
            "write_mixes": self.write_mixes,
            "create_folders": self.create_folders,
            "delete_liked_tracks": self.delete_liked_tracks,
            "delete_saved_albums": self.delete_saved_albums,
            "delete_followed_artists": self.delete_followed_artists,
            "delete_playlists": self.delete_playlists,
            "delete_folders": self.delete_folders,
            "search_tracks": self.search_tracks,
            "search_albums": self.search_albums,
            "search_artists": self.search_artists,
            "preserves_custom_added_date": self.preserves_custom_added_date,
            "supports_playlist_duplicates": self.supports_playlist_duplicates,
            "supports_already_exists_detection": self.supports_already_exists_detection,
            "supports_batch_playlist_writes": self.supports_batch_playlist_writes,
            "insertion_behavior": str(self.insertion_behavior),
            "distinguishes_region_availability": self.distinguishes_region_availability,
            "exposes_isrc": self.exposes_isrc,
        }


# --------------------------------------------------------------------------
# Destination state
# --------------------------------------------------------------------------

KNOWN_DESTINATION_SECTIONS: frozenset[str] = frozenset(
    {"tracks", "albums", "artists", "videos", "mixes", "playlists"}
)

_ENTITY_TYPE_TO_SECTION: dict[EntityType, str] = {
    EntityType.TRACK: "tracks",
    EntityType.ALBUM: "albums",
    EntityType.ARTIST: "artists",
    EntityType.VIDEO: "videos",
    EntityType.MIX: "mixes",
    EntityType.PLAYLIST: "playlists",
}


def destination_section_for_entity(entity_type: EntityType) -> str:
    """Return the canonical destination section name for an EntityType.

    Raises:
        InvalidDestinationSectionError: If entity_type does not map to a destination section.
    """
    section = _ENTITY_TYPE_TO_SECTION.get(entity_type)
    if section is None:
        raise InvalidDestinationSectionError(
            f"unsupported_entity_type_for_destination_presence:{entity_type}",
            section=str(entity_type),
        )
    return section


@dataclass(slots=True)
class DestinationState:
    """A read-only view of what the destination already contains.

    Destination membership is tri-state: PRESENT, ABSENT, UNKNOWN (Phase 1.4C).
    ``complete_sections`` records which sections were successfully read to completion.
    Absence can only be proven if the relevant section is in ``complete_sections``.
    A default DestinationState has empty ``complete_sections`` and represents UNKNOWN
    for all sections (fail-closed).
    """

    platform: Platform
    track_ids: frozenset[str] = frozenset()
    album_ids: frozenset[str] = frozenset()
    artist_ids: frozenset[str] = frozenset()
    video_ids: frozenset[str] = frozenset()
    mix_ids: frozenset[str] = frozenset()
    playlist_ids: frozenset[str] = frozenset()
    complete_sections: frozenset[str] = frozenset()
    incomplete_sections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Coerce collections into frozensets/tuples without assuming completeness."""
        if not isinstance(self.track_ids, frozenset):
            object.__setattr__(self, "track_ids", frozenset(self.track_ids))
        if not isinstance(self.album_ids, frozenset):
            object.__setattr__(self, "album_ids", frozenset(self.album_ids))
        if not isinstance(self.artist_ids, frozenset):
            object.__setattr__(self, "artist_ids", frozenset(self.artist_ids))
        if not isinstance(self.video_ids, frozenset):
            object.__setattr__(self, "video_ids", frozenset(self.video_ids))
        if not isinstance(self.mix_ids, frozenset):
            object.__setattr__(self, "mix_ids", frozenset(self.mix_ids))
        if not isinstance(self.playlist_ids, frozenset):
            object.__setattr__(self, "playlist_ids", frozenset(self.playlist_ids))
        if not isinstance(self.complete_sections, frozenset):
            object.__setattr__(self, "complete_sections", frozenset(self.complete_sections))
        if not isinstance(self.incomplete_sections, tuple):
            object.__setattr__(self, "incomplete_sections", tuple(self.incomplete_sections))

    def presence(self, entity_type: EntityType, identifier: str) -> DestinationPresence:
        """Query observed presence for an entity type and identifier.

        Returns:
            PRESENT: The section is complete and identifier was observed.
            ABSENT: The section is complete and identifier was not observed.
            UNKNOWN: The section is not complete (unread, failed, or partial).

        Raises:
            InvalidDestinationSectionError: If entity_type does not map to a destination section.
        """
        section = destination_section_for_entity(entity_type)
        return self.presence_in_section(section, identifier)

    def identifiers_for_section(self, section: str) -> frozenset[str]:
        """Return the observed destination identifiers for a canonical section.

        Raises:
            InvalidDestinationSectionError: If section is not a known destination section.
        """
        if section not in KNOWN_DESTINATION_SECTIONS:
            raise InvalidDestinationSectionError(
                f"invalid_destination_section:{section}",
                section=section,
            )
        if section == "tracks":
            return self.track_ids
        if section == "albums":
            return self.album_ids
        if section == "artists":
            return self.artist_ids
        if section == "videos":
            return self.video_ids
        if section == "mixes":
            return self.mix_ids
        if section == "playlists":
            return self.playlist_ids
        return frozenset()

    def presence_in_section(self, section: str, identifier: str) -> DestinationPresence:
        """Query observed presence within a canonical destination section.

        Returns:
            PRESENT: The section is complete, not incomplete, and identifier was observed.
            ABSENT: The section is complete, not incomplete, and identifier was not observed.
            UNKNOWN: The section is not complete (unread, failed, contradictory, or partial).

        Raises:
            InvalidDestinationSectionError: If section is not a known destination section.
        """
        id_set = self.identifiers_for_section(section)
        if section in self.incomplete_sections:
            return DestinationPresence.UNKNOWN
        if section not in self.complete_sections:
            return DestinationPresence.UNKNOWN

        if bool(identifier) and identifier in id_set:
            return DestinationPresence.PRESENT
        return DestinationPresence.ABSENT

    def has_track(self, track_id: str | None) -> bool:
        """Return whether a track id is known to be present."""
        if not track_id:
            return False
        return (
            self.presence(EntityType.TRACK, track_id)
            is DestinationPresence.PRESENT
        )

    def is_trustworthy(self, section: str) -> bool:
        """Return whether a section was read completely and without incomplete evidence."""
        return (
            section in self.complete_sections
            and section not in self.incomplete_sections
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values for diagnostic plan metadata."""
        return {
            "platform": str(self.platform),
            "track_count": len(self.track_ids),
            "album_count": len(self.album_ids),
            "artist_count": len(self.artist_ids),
            "video_count": len(self.video_ids),
            "mix_count": len(self.mix_ids),
            "playlist_count": len(self.playlist_ids),
            "complete_sections": sorted(self.complete_sections),
            "incomplete_sections": list(self.incomplete_sections),
        }



# --------------------------------------------------------------------------
# The platform read port
# --------------------------------------------------------------------------


@runtime_checkable
class MusicPlatformReadPort(Protocol):
    """The minimal read-only platform contract required for transfer planning.

    Invariant B: Planning performs no destination mutation.  The planner
    depends strictly on this interface rather than the full write-capable
    :class:`MusicPlatformAdapter`.
    """

    @property
    def platform(self) -> Platform:
        """Return the platform identifier."""
        ...

    @property
    def capabilities(self) -> PlatformCapabilities:
        """Return the capability declaration."""
        ...

    def get_destination_state(
        self, sections: tuple[str, ...] | None = None
    ) -> DestinationState:
        """Return what the destination already contains (read-only)."""
        ...

    def search_track(self, track: Track, limit: int = 5) -> list[Track]:
        """Search the destination catalog for a track (read-only)."""
        ...

    def search_album(self, album: Album, limit: int = 5) -> list[Album]:
        """Search the destination catalog for an album (read-only)."""
        ...

    def search_artist(self, artist: Artist, limit: int = 5) -> list[Artist]:
        """Search the destination catalog for an artist (read-only)."""
        ...

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        """Return whether source ids can be written directly to this platform (read-only)."""
        ...


# --------------------------------------------------------------------------
# The adapter contract
# --------------------------------------------------------------------------


class MusicPlatformAdapter(ABC):
    """The contract every music service implements.

    Every method is optional in practice: the default implementation raises
    :class:`UnsupportedCapabilityError`, so an adapter only overrides what its
    platform genuinely supports.  That satisfies the rule that unsupported
    platforms must not be forced to implement meaningless operations, while
    keeping unsupported calls loud instead of silently successful.
    """

    #: Method names that query remote or local state without mutating it.
    READ_METHODS: frozenset[str] = frozenset(
        {
            "get_profile",
            "export_library",
            "get_liked_tracks",
            "get_saved_albums",
            "get_followed_artists",
            "get_playlists",
            "get_playlist_items",
            "get_destination_state",
            "playlist_item_ids",
            "search_track",
            "search_album",
            "search_artist",
            "can_reuse_identifier",
        }
    )

    #: Method names that change remote state (non-destructive writes).
    MUTATING_METHODS: frozenset[str] = frozenset(
        {
            "save_track",
            "save_album",
            "follow_artist",
            "save_video",
            "save_mix",
            "create_playlist",
            "add_playlist_item",
            "add_playlist_items",
            "create_folder",
        }
    )

    #: Method names that remove remote state.  Only the library-maintenance
    #: service may call these, and only after its own stronger confirmation.
    DESTRUCTIVE_METHODS: frozenset[str] = frozenset(
        {
            "remove_track",
            "remove_album",
            "unfollow_artist",
            "remove_video",
            "remove_mix",
            "delete_playlist",
            "delete_folder",
        }
    )

    @property
    @abstractmethod
    def platform(self) -> Platform:
        """Return the platform this adapter talks to."""

    @property
    @abstractmethod
    def capabilities(self) -> PlatformCapabilities:
        """Return the capability declaration for this adapter."""

    # --- identity ---------------------------------------------------------

    @abstractmethod
    def get_profile(self) -> AccountProfile:
        """Return safe, display-oriented account metadata."""

    # --- reads -----------------------------------------------------------

    def export_library(
        self, sections: tuple[str, ...] | None = None, progress: ProgressCallback = None
    ) -> LibrarySnapshot:
        """Export the whole library, marking unreadable sections incomplete."""

        raise UnsupportedCapabilityError("capability_unsupported", capability="export_library")

    def get_liked_tracks(self, progress: ProgressCallback = None) -> list[Track]:
        """Return every liked/saved track."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="read_liked_tracks"
        )

    def get_saved_albums(self, progress: ProgressCallback = None) -> list[Album]:
        """Return every saved album."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="read_saved_albums"
        )

    def get_followed_artists(self, progress: ProgressCallback = None) -> list[Artist]:
        """Return every followed artist."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="read_followed_artists"
        )

    def get_playlists(self, progress: ProgressCallback = None) -> list[Playlist]:
        """Return playlists and, where available, their ordered items."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="read_playlists"
        )

    def get_playlist_items(
        self, playlist_id: str, progress: ProgressCallback = None
    ) -> list[PlaylistItem]:
        """Return the ordered items of one playlist, duplicates preserved."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="read_playlists"
        )

    def get_destination_state(
        self, sections: tuple[str, ...] | None = None
    ) -> DestinationState:
        """Return what the destination already contains.

        Read-only.  Used for "already exists" detection and for reconciling an
        ambiguous mutation before any retry (Invariant F).
        """

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="supports_already_exists_detection"
        )

    def playlist_item_ids(self, playlist_id: str) -> list[str]:
        """Return the exact media id sequence of a destination playlist.

        Read-only.  Essential for resuming an interrupted playlist write and
        for order verification.  Duplicates appear once per occurrence.
        """

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="read_playlists"
        )

    # --- search -----------------------------------------------------------

    def search_track(self, track: Track, limit: int = 5) -> list[Track]:
        """Search the destination catalog for a track."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="search_tracks"
        )

    def search_album(self, album: Album, limit: int = 5) -> list[Album]:
        """Search the destination catalog for an album."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="search_albums"
        )

    def search_artist(self, artist: Artist, limit: int = 5) -> list[Artist]:
        """Search the destination catalog for an artist."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="search_artists"
        )

    # --- non-destructive writes ------------------------------------------

    def save_track(self, track_id: str) -> None:
        """Add one track to the destination's liked/saved tracks."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="write_liked_tracks"
        )

    def save_album(self, album_id: str) -> None:
        """Save one album at the destination."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="write_saved_albums"
        )

    def follow_artist(self, artist_id: str) -> None:
        """Follow one artist at the destination."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="write_followed_artists"
        )

    def save_video(self, video_id: str) -> None:
        """Save one video at the destination."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="write_videos"
        )

    def save_mix(self, mix_id: str) -> None:
        """Save one mix at the destination."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="write_mixes"
        )

    def create_playlist(self, playlist: Playlist) -> str:
        """Create a destination playlist and return its identifier.

        Raises:
            AmbiguousOperationError: When the request timed out or failed in a
                way that leaves the remote outcome unknown.  Callers must
                reconcile against destination state before retrying.
        """

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="create_playlists"
        )

    def add_playlist_item(self, playlist_id: str, track_id: str) -> None:
        """Append one item to a destination playlist.

        Implementations must allow duplicates so that a source playlist with
        repeated entries is reproduced exactly (Invariant D).
        """

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="write_playlist_items"
        )

    def add_playlist_items(self, playlist_id: str, track_ids: list[str]) -> int:
        """Append several items at once; return how many were accepted.

        Only meaningful when the adapter declares
        ``supports_batch_playlist_writes``.
        """

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="supports_batch_playlist_writes"
        )

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a playlist folder and return its identifier."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="create_folders"
        )

    # --- capability query -------------------------------------------------

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        """Return whether source ids can be written directly to this platform.

        TIDAL returns ``True`` for TIDAL sources, which is what makes a
        TIDAL -> TIDAL transfer a direct copy with no catalog search.  Keeping
        this a capability query means the core contains no ``if platform ==
        "tidal"`` branch.
        """

        return False


class LibraryMaintenanceAdapter:
    """Destructive library operations, deliberately kept out of transfers.

    A transfer never calls these.  They exist only for the separate library
    management / cleanup service, which applies stronger confirmation than a
    transfer does (Invariant C).
    """

    def remove_track(self, track_id: str) -> None:
        """Unfavorite/remove one track."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_liked_tracks"
        )

    def remove_album(self, album_id: str) -> None:
        """Remove one saved album."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_saved_albums"
        )

    def unfollow_artist(self, artist_id: str) -> None:
        """Unfollow one artist."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_followed_artists"
        )

    def remove_video(self, video_id: str) -> None:
        """Remove one saved video."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_liked_tracks"
        )

    def remove_mix(self, mix_id: str) -> None:
        """Remove one saved mix."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_liked_tracks"
        )

    def delete_playlist(self, playlist_id: str) -> None:
        """Permanently delete an owned playlist, or unfavorite a public one."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_playlists"
        )

    def delete_folder(self, folder_id: str) -> None:
        """Delete a playlist folder."""

        raise UnsupportedCapabilityError(
            "capability_unsupported", capability="delete_folders"
        )


# --------------------------------------------------------------------------
# Guards and async bridge
# --------------------------------------------------------------------------


def operation_kind(
    adapter: MusicPlatformAdapter | type[MusicPlatformAdapter], method_name: str
) -> OperationKind:
    """Classify an adapter method as read, mutating, or destructive.

    Raises:
        UnsupportedCapabilityError: If the method name is not recognized,
            ensuring unknown operations fail closed instead of being
            implicitly treated as safe reads.
    """

    if method_name in MusicPlatformAdapter.DESTRUCTIVE_METHODS:
        return OperationKind.DESTRUCTIVE
    if method_name in MusicPlatformAdapter.MUTATING_METHODS:
        return OperationKind.MUTATING
    if method_name in MusicPlatformAdapter.READ_METHODS:
        return OperationKind.READ
    raise UnsupportedCapabilityError("unknown_operation", capability=method_name)


class ReadOnlyAdapter:
    """An explicit fail-closed read-only facade for platform adapters.

    The transfer planner receives destination adapters through this facade so
    that Invariant B ("a transfer plan performs no destination mutations") is
    enforced at runtime by the object boundary itself.

    This class explicitly implements :class:`MusicPlatformReadPort` and forwards
    only declared planning read operations to the underlying adapter. It defines
    no generic attribute forwarding (__getattr__) and no public escape hatch to
    the underlying write adapter. Unknown or mutating methods are unreachable
    and fail closed.
    """

    def __init__(self, inner: MusicPlatformAdapter | MusicPlatformReadPort) -> None:
        if isinstance(inner, ReadOnlyAdapter):
            self._inner = inner._inner
        else:
            self._inner = inner

    @property
    def platform(self) -> Platform:
        """Return the platform this adapter talks to."""
        return self._inner.platform

    @property
    def capabilities(self) -> PlatformCapabilities:
        """Return the capability declaration."""
        return self._inner.capabilities

    def get_destination_state(
        self, sections: tuple[str, ...] | None = None
    ) -> DestinationState:
        """Return what the destination already contains (read-only)."""
        return self._inner.get_destination_state(sections=sections)

    def search_track(self, track: Track, limit: int = 5) -> list[Track]:
        """Search the destination catalog for a track (read-only)."""
        return self._inner.search_track(track, limit=limit)

    def search_album(self, album: Album, limit: int = 5) -> list[Album]:
        """Search the destination catalog for an album (read-only)."""
        return self._inner.search_album(album, limit=limit)

    def search_artist(self, artist: Artist, limit: int = 5) -> list[Artist]:
        """Search the destination catalog for an artist (read-only)."""
        return self._inner.search_artist(artist, limit=limit)

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        """Return whether source ids can be written directly to this platform (read-only)."""
        return self._inner.can_reuse_identifier(entity_type, source)


@runtime_checkable
class AsyncPlatformAdapter(Protocol):
    """The async shape a worker-facing adapter should expose.

    This protocol documents the future direction; it is intentionally not
    implemented by TIDAL yet.  :func:`to_async` provides the bridge so an async
    worker can use today's synchronous adapters without blocking its loop.
    """

    async def export_library(self, sections: tuple[str, ...] | None = None) -> LibrarySnapshot: ...
    async def get_liked_tracks(self) -> list[Track]: ...
    async def save_track(self, track_id: str) -> None: ...


def to_async(adapter: MusicPlatformAdapter) -> _AsyncBridge:
    """Wrap a synchronous adapter so ``await`` runs it in a worker thread.

    The bridge exposes the same method names as the underlying adapter, which
    keeps future async call sites readable while the adapters stay synchronous
    and proven.
    """

    return _AsyncBridge(adapter)


class _AsyncBridge:
    """Run synchronous adapter calls in threads (see :func:`to_async`)."""

    def __init__(self, adapter: MusicPlatformAdapter) -> None:
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        import asyncio

        attribute = getattr(self._adapter, name)
        if not callable(attribute):
            return attribute

        async def run(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attribute, *args, **kwargs)

        return run
