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
    LibraryRecord,
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


def record(
    identifier: str,
    title: str = "",
    platform: Platform = Platform.TIDAL,
    metadata: dict[str, Any] | None = None,
) -> LibraryRecord:
    """Build a library record for videos, mixes, or folders."""

    return LibraryRecord(
        source_platform=platform,
        source_id=identifier,
        title=title,
        metadata=dict(metadata or {}),
    )


def artist(
    name: str, identifier: str | None = None, platform: Platform = Platform.TIDAL
) -> Artist:
    """Build an artist, defaulting the id to the lowercased name."""

    return Artist(
        source_platform=platform,
        source_id=identifier or name.casefold(),
        name=name,
    )


def album(
    title: str,
    artists: tuple[Artist, ...] | list[Artist] | list[str] | None = None,
    identifier: str | None = None,
    platform: Platform = Platform.TIDAL,
    upc: str | None = None,
) -> Album:
    """Build an album with an optional artist list or tuple."""

    if artists is None:
        artist_objs = (artist("Unknown", platform=platform),)
    elif artists and isinstance(artists[0], str):
        artist_objs = tuple(artist(a, platform=platform) for a in artists)
    else:
        artist_objs = tuple(artists)

    return Album(
        source_platform=platform,
        source_id=identifier or title.casefold().replace(" ", "-"),
        title=title,
        artists=artist_objs,
        upc=upc,
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
    folder_id: str | None = None,
) -> Playlist:
    """Build a playlist whose item positions follow the given order."""

    return Playlist(
        source_platform=Platform.TIDAL,
        source_id=identifier or title.casefold().replace(" ", "-"),
        name=title,
        description=description,
        folder_id=folder_id,
        tracks=[
            PlaylistItem(position=index, track=item)
            for index, item in enumerate(items, start=1)
        ],
    )


def snapshot(
    *,
    playlists: tuple[Playlist, ...] = (),
    tracks: tuple[Track, ...] = (),
    albums: tuple[Album, ...] = (),
    artists: tuple[Artist, ...] = (),
    videos: tuple[LibraryRecord, ...] = (),
    mixes: tuple[LibraryRecord, ...] = (),
    folders: tuple[LibraryRecord, ...] = (),
    incomplete_sections: list[str] | None = None,
) -> LibrarySnapshot:
    """Build a minimal library snapshot for planning tests."""

    return LibrarySnapshot(
        account=AccountProfile("1", "test", Platform.TIDAL),
        platform=Platform.TIDAL,
        tracks=list(tracks),
        albums=list(albums),
        artists=list(artists),
        playlists=list(playlists),
        videos=list(videos),
        mixes=list(mixes),
        folders=list(folders),
        incomplete_sections=list(incomplete_sections or []),
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
        read_videos=True,
        read_mixes=True,
        write_liked_tracks=True,
        write_saved_albums=True,
        write_followed_artists=True,
        write_videos=True,
        write_mixes=True,
        create_playlists=True,
        write_playlist_items=True,
        read_folders=True,
        create_folders=True,
        search_tracks=True,
        search_albums=True,
        search_artists=True,
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
        albums: list[Album] | None = None,
        artists: list[Artist] | None = None,
        playlists: list[Playlist] | None = None,
        folders: list[LibraryRecord] | None = None,
        videos: list[LibraryRecord] | None = None,
        mixes: list[LibraryRecord] | None = None,
        catalog_tracks: list[Track] | None = None,
        catalog_albums: list[Album] | None = None,
        catalog_artists: list[Artist] | None = None,
        capabilities: PlatformCapabilities | None = None,
        fail_on: set[str] | None = None,
        error_factory: Any = None,
    ) -> None:
        self._platform = platform
        self._display_name = display_name
        self._account_id = account_id
        self._capabilities = capabilities or self.CAPABILITIES
        self.tracks = list(tracks or [])
        self.albums: list[Album] = list(albums or [])
        self.artists: list[Artist] = list(artists or [])
        self.playlists: list[Playlist] = list(playlists or [])
        self.folders: list[LibraryRecord] = list(folders or [])
        self.videos: list[LibraryRecord] = list(videos or [])
        self.mixes: list[LibraryRecord] = list(mixes or [])
        self.catalog_tracks = list(catalog_tracks) if catalog_tracks is not None else None
        self.catalog_albums = list(catalog_albums) if catalog_albums is not None else None
        self.catalog_artists = list(catalog_artists) if catalog_artists is not None else None
        self.saved_tracks: list[str] = []
        self.saved_albums: list[str] = []
        self.followed_artists: list[str] = []
        self.saved_videos: list[str] = []
        self.saved_mixes: list[str] = []
        self.created_playlists: list[str] = []
        self.created_folders: list[tuple[str, str | None]] = []
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

        return self._capabilities

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

        sec_set = set(sections) if sections is not None else None
        return LibrarySnapshot(
            account=self.get_profile(),
            platform=self._platform,
            tracks=list(self.tracks) if sec_set is None or "tracks" in sec_set else [],
            albums=list(self.albums) if sec_set is None or "albums" in sec_set else [],
            artists=list(self.artists) if sec_set is None or "artists" in sec_set else [],
            playlists=list(self.playlists) if sec_set is None or "playlists" in sec_set else [],
            folders=list(self.folders) if sec_set is None or "folders" in sec_set else [],
            videos=list(self.videos) if sec_set is None or "videos" in sec_set else [],
            mixes=list(self.mixes) if sec_set is None or "mixes" in sec_set else [],
        )

    def get_destination_state(self, sections=None) -> DestinationState:
        """Return the current contents, used for resume reconciliation."""
        if "get_destination_state" in self.fail_on:
            if self.error_factory is not None:
                raise self.error_factory()
            raise RuntimeError("simulated failure in get_destination_state")

        if sections is not None:
            for s in sections:
                if s not in ("tracks", "albums", "artists", "videos", "mixes", "playlists"):
                    raise UnsupportedCapabilityError("capability_unsupported", capability=s)
            wanted = set(sections)
        else:
            wanted = {"tracks", "albums", "artists", "videos", "mixes", "playlists"}

        complete: set[str] = set()
        track_ids: list[str] = []
        album_ids: list[str] = []
        artist_ids: list[str] = []
        video_ids: list[str] = []
        mix_ids: list[str] = []
        playlist_ids: list[str] = []

        if "tracks" in wanted:
            track_ids = [item.source_id for item in self.tracks] + [
                identifier
                for identifier in self.saved_tracks
                if identifier not in {item.source_id for item in self.tracks}
            ]
            complete.add("tracks")
        if "albums" in wanted:
            album_ids = [item.source_id for item in self.albums] + [
                identifier
                for identifier in self.saved_albums
                if identifier not in {item.source_id for item in self.albums}
            ]
            complete.add("albums")
        if "artists" in wanted:
            artist_ids = [item.source_id for item in self.artists] + [
                identifier
                for identifier in self.followed_artists
                if identifier not in {item.source_id for item in self.artists}
            ]
            complete.add("artists")
        if "videos" in wanted:
            video_ids = [item.source_id for item in self.videos] + [
                identifier
                for identifier in self.saved_videos
                if identifier not in {item.source_id for item in self.videos}
            ]
            complete.add("videos")
        if "mixes" in wanted:
            mix_ids = [item.source_id for item in self.mixes] + [
                identifier
                for identifier in self.saved_mixes
                if identifier not in {item.source_id for item in self.mixes}
            ]
            complete.add("mixes")
        if "playlists" in wanted:
            playlist_ids = [item.source_id for item in self.playlists]
            complete.add("playlists")

        return DestinationState(
            platform=self._platform,
            track_ids=frozenset(track_ids),
            album_ids=frozenset(album_ids),
            artist_ids=frozenset(artist_ids),
            video_ids=frozenset(video_ids),
            mix_ids=frozenset(mix_ids),
            playlist_ids=frozenset(playlist_ids),
            complete_sections=frozenset(complete),
        )

    def search_track(self, query: Track, limit: int = 5) -> list[Track]:
        """Return candidates by exact title, then by normalized title."""

        from music_transfer.core.matching import normalize_text

        pool = self.catalog_tracks if self.catalog_tracks is not None else self.tracks
        wanted = normalize_text(query.title)
        exact = [item for item in pool if normalize_text(item.title) == wanted]
        return exact[:limit]

    def search_album(self, query: Album, limit: int = 5) -> list[Album]:
        """Return album candidates by UPC or exact/normalized title."""

        from music_transfer.core.matching import normalize_text

        pool = self.catalog_albums if self.catalog_albums is not None else self.albums
        if query.upc:
            clean_upc = str(query.upc).strip()
            upc_matches = [
                item for item in pool if item.upc and str(item.upc).strip() == clean_upc
            ]
            if upc_matches:
                return upc_matches[:limit]

        wanted = normalize_text(query.title)
        exact = [item for item in pool if normalize_text(item.title) == wanted]
        return exact[:limit]

    def search_artist(self, query: Artist, limit: int = 5) -> list[Artist]:
        """Return artist candidates by exact normalized name."""

        from music_transfer.core.matching import normalize_text

        pool = self.catalog_artists if self.catalog_artists is not None else self.artists
        wanted = normalize_text(query.name)
        exact = [item for item in pool if normalize_text(item.name) == wanted]
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

    def save_video(self, video_id: str) -> None:
        """Record a saved video id."""

        self._record("save_video", video_id)
        self.saved_videos.append(video_id)

    def save_mix(self, mix_id: str) -> None:
        """Record a saved mix id."""

        self._record("save_mix", mix_id)
        self.saved_mixes.append(mix_id)

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
                folder_id=item.folder_id,
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

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Create a playlist folder and return its identifier."""

        self._record("create_folder", name, parent_id)
        identifier = f"dst-fld-{len(self.folders) + 1}"
        self.folders.append(
            LibraryRecord(
                source_platform=self._platform,
                source_id=identifier,
                title=name,
                metadata={"parent_source_id": parent_id, "parent_id": parent_id},
            )
        )
        self.created_folders.append((name, parent_id))
        return identifier

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        """Return whether a source identifier is valid on this destination.

        Catalogue entities (tracks, albums, artists, videos, mixes) are shared.
        Playlists, playlist items, and folders belong to an account and are not portable.
        """

        if source is not self._platform:
            return False
        return entity_type in {
            EntityType.TRACK,
            EntityType.ALBUM,
            EntityType.ARTIST,
            EntityType.VIDEO,
            EntityType.MIX,
        }

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
    "record",
    "snapshot",
    "track",
]
