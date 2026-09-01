"""Shared builders and fake adapters for the new-architecture tests.

The fakes here are *real* in-memory implementations, not stubs that pretend a
platform works: they store what is written and return it, so resume,
verification, and idempotency tests exercise the same code paths the TIDAL
adapter will.  Anything a fake cannot honestly do, it leaves unimplemented so
the adapter default raises ``UnsupportedCapabilityError``.
"""

from __future__ import annotations

from typing import Any

from music_transfer.core.domain import (
    AccountProfile,
    Album,
    Artist,
    LibrarySnapshot,
    Playlist,
    PlaylistItem,
    Track,
)
from music_transfer.core.enums import EntityType, Platform
from music_transfer.core.errors import UnsupportedCapabilityError
from music_transfer.core.ports import (
    DestinationState,
    MusicPlatformAdapter,
    PlatformCapabilities,
)


def artist(name: str, identifier: str | None = None) -> Artist:
    """Build an artist, defaulting the id to the lowercased name."""

    return Artist(
        source_platform=Platform.TIDAL,
        source_id=identifier or name.casefold(),
        name=name,
    )


def album(title: str, artists: tuple[Artist, ...] = (), identifier: str | None = None) -> Album:
    """Build an album with an optional artist tuple."""

    return Album(
        source_platform=Platform.TIDAL,
        source_id=identifier or title.casefold().replace(" ", "-"),
        title=title,
        artists=artists or (artist("Unknown"),),
    )


def track(
    title: str,
    artists: tuple[Artist, ...] | None = None,
    *,
    identifier: str | None = None,
    isrc: str | None = None,
    duration_ms: int | None = 200_000,
    explicit: bool = False,
    album: Album | None = None,
    version: str | None = None,
) -> Track:
    """Build a track with sensible defaults for matching tests."""

    return Track(
        source_platform=Platform.TIDAL,
        source_id=identifier or title.casefold().replace(" ", "-"),
        title=title,
        artists=artists if artists is not None else (artist("Test Artist"),),
        album=album,
        isrc=isrc,
        duration_ms=duration_ms,
        explicit=explicit,
        version=version,
    )


def playlist(
    title: str,
    items: list[Track],
    *,
    identifier: str | None = None,
    description: str | None = None,
) -> Playlist:
    """Build a playlist whose item positions follow the given order."""

    return Playlist(
        source_platform=Platform.TIDAL,
        source_id=identifier or title.casefold().replace(" ", "-"),
        name=title,
        description=description,
        tracks=[
            PlaylistItem(position=index, track=item)
            for index, item in enumerate(items, start=1)
        ],
    )


def snapshot(*, playlists: tuple[Playlist, ...] = (), tracks: tuple[Track, ...] = ()) -> LibrarySnapshot:
    """Build a minimal library snapshot for planning tests."""

    return LibrarySnapshot(
        account=AccountProfile("1", "test", Platform.TIDAL),
        platform=Platform.TIDAL,
        tracks=list(tracks),
        playlists=list(playlists),
    )


class FakePlatformAdapter(MusicPlatformAdapter):
    """An in-memory platform adapter used as a source or a destination.

    State is real: ``save_track`` appends to :attr:`saved_tracks`, and
    ``add_playlist_item`` appends to the playlist's items.  That makes it
    possible to assert on idempotency (no duplicate after a resume) and on
    ordering (items land in the requested write order).
    """

    CAPABILITIES = PlatformCapabilities(
        platform=Platform.TIDAL,
        read_liked_tracks=True,
        read_saved_albums=True,
        read_followed_artists=True,
        read_playlists=True,
        write_liked_tracks=True,
        write_saved_albums=True,
        write_followed_artists=True,
        create_playlists=True,
        write_playlist_items=True,
        search_tracks=True,
        supports_already_exists_detection=True,
        supports_playlist_duplicates=True,
        exposes_isrc=True,
    )

    def __init__(
        self,
        platform: Platform = Platform.TIDAL,
        *,
        display_name: str = "fake",
        account_id: str = "fake-1",
        tracks: list[Track] | None = None,
        playlists: list[Playlist] | None = None,
        fail_on: set[str] | None = None,
        error_factory: Any = None,
    ) -> None:
        self._platform = platform
        self._display_name = display_name
        self._account_id = account_id
        self.tracks = list(tracks or [])
        self.albums: list[Album] = []
        self.artists: list[Artist] = []
        self.playlists: list[Playlist] = list(playlists or [])
        self.saved_tracks: list[str] = []
        self.saved_albums: list[str] = []
        self.followed_artists: list[str] = []
        self.created_playlists: list[str] = []
        #: Method names that should raise, to exercise failure handling.
        self.fail_on = set(fail_on or ())
        self.error_factory = error_factory
        #: Counts every write, so a test can prove a plan made none.
        self.write_calls: list[tuple[str, tuple[Any, ...]]] = []

    # -- identity ----------------------------------------------------------

    @property
    def platform(self) -> Platform:
        """Return the platform this fake stands in for."""

        return self._platform

    @property
    def capabilities(self) -> PlatformCapabilities:
        """Return the capability declaration."""

        return self.CAPABILITIES

    def get_profile(self) -> AccountProfile:
        """Return a safe display profile."""

        return AccountProfile(self._account_id, self._display_name, self._platform)

    # -- reads -------------------------------------------------------------

    def get_liked_tracks(self, progress=None) -> list[Track]:
        """Return the configured tracks."""

        return list(self.tracks)

    def get_saved_albums(self, progress=None) -> list[Album]:
        """Return the configured albums."""

        return list(self.albums)

    def get_followed_artists(self, progress=None) -> list[Artist]:
        """Return the configured artists."""

        return list(self.artists)

    def get_playlists(self, progress=None) -> list[Playlist]:
        """Return the configured playlists."""

        return list(self.playlists)

    def get_playlist_items(self, playlist_id: str, progress=None) -> list[PlaylistItem]:
        """Return the items of one playlist."""

        for item in self.playlists:
            if item.source_id == playlist_id:
                return list(item.tracks)
        return []

    def playlist_item_ids(self, playlist_id: str) -> list[str]:
        """Return the destination track ids of one playlist, in order."""

        return [
            item.track.source_id
            for item in self.get_playlist_items(playlist_id)
            if item.track is not None
        ]

    def export_library(self, sections=None, progress=None) -> LibrarySnapshot:
        """Return the configured library."""

        return LibrarySnapshot(
            account=self.get_profile(),
            platform=self._platform,
            tracks=list(self.tracks),
            albums=list(self.albums),
            artists=list(self.artists),
            playlists=list(self.playlists),
        )

    def get_destination_state(self, sections=None) -> DestinationState:
        """Return the current contents, used for resume reconciliation."""
        if "get_destination_state" in self.fail_on:
            if self.error_factory is not None:
                raise self.error_factory()
            raise RuntimeError("simulated failure in get_destination_state")

        if sections is not None:
            for s in sections:
                if s not in ("tracks", "albums", "artists", "playlists"):
                    raise UnsupportedCapabilityError("capability_unsupported", capability=s)
            wanted = set(sections)
        else:
            wanted = {"tracks", "albums", "artists", "playlists"}

        complete: set[str] = set()
        track_ids: list[str] = []
        album_ids: list[str] = []
        artist_ids: list[str] = []
        playlist_ids: list[str] = []

        if "tracks" in wanted:
            track_ids = [item.source_id for item in self.tracks] + [
                identifier
                for identifier in self.saved_tracks
                if identifier not in {item.source_id for item in self.tracks}
            ]
            complete.add("tracks")
        if "albums" in wanted:
            album_ids = [item.source_id for item in self.albums]
            complete.add("albums")
        if "artists" in wanted:
            artist_ids = [item.source_id for item in self.artists]
            complete.add("artists")
        if "playlists" in wanted:
            playlist_ids = [item.source_id for item in self.playlists]
            complete.add("playlists")

        return DestinationState(
            platform=self._platform,
            track_ids=frozenset(track_ids),
            album_ids=frozenset(album_ids),
            artist_ids=frozenset(artist_ids),
            playlist_ids=frozenset(playlist_ids),
            complete_sections=frozenset(complete),
        )


    def search_track(self, query: Track, limit: int = 5) -> list[Track]:
        """Return candidates by exact title, then by normalized title."""

        from music_transfer.core.matching import normalize_text

        wanted = normalize_text(query.title)
        exact = [item for item in self.tracks if normalize_text(item.title) == wanted]
        return exact[:limit]

    # -- writes ------------------------------------------------------------

    def save_track(self, track_id: str) -> None:
        """Record a saved track id."""

        self._record("save_track", track_id)
        self.saved_tracks.append(track_id)

    def save_album(self, album_id: str) -> None:
        """Record a saved album id."""

        self._record("save_album", album_id)
        self.saved_albums.append(album_id)

    def follow_artist(self, artist_id: str) -> None:
        """Record a followed artist id."""

        self._record("follow_artist", artist_id)
        self.followed_artists.append(artist_id)

    def create_playlist(self, item: Playlist) -> str:
        """Create a playlist and return its id."""

        self._record("create_playlist", item.name)
        identifier = f"dst-{item.source_id}"
        self.playlists.append(
            Playlist(
                source_platform=self._platform,
                source_id=identifier,
                name=item.name,
                tracks=[],
            )
        )
        return identifier

    def add_playlist_item(self, playlist_id: str, track_id: str) -> None:
        """Append an item to a playlist, preserving duplicates."""

        self._record("add_playlist_item", playlist_id, track_id)
        for item in self.playlists:
            if item.source_id == playlist_id:
                item.tracks.append(
                    PlaylistItem(position=len(item.tracks) + 1, track=track(track_id, identifier=track_id))
                )
                return
        raise LookupError(f"unknown playlist: {playlist_id}")

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        """Return whether a source identifier is valid on this destination.

        Catalogue entities (tracks, albums, artists) are shared, so an id is
        portable.  A playlist belongs to an account and must be created first,
        so its id is never reusable even between two accounts on one platform.
        """

        if source is not self._platform:
            return False
        return entity_type is not EntityType.PLAYLIST

    # -- helpers -----------------------------------------------------------

    def _record(self, method: str, *args: Any) -> None:
        """Track the call and optionally fail, to exercise error handling."""

        self.write_calls.append((method, args))
        if method in self.fail_on:
            if self.error_factory is not None:
                raise self.error_factory()
            raise RuntimeError(f"simulated failure in {method}")


__all__ = [
    "FakePlatformAdapter",
    "album",
    "artist",
    "playlist",
    "snapshot",
    "track",
]
