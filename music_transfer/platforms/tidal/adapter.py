"""The TIDAL platform adapter.

TIDAL is the first real adapter and the reference implementation for future
ones.  It exposes the proven TIDAL client through the generic contract and
declares its capabilities honestly.

Notes on TIDAL-specific behaviour that the core needs to know, expressed as
*capabilities* rather than platform checks:

* TIDAL ids can be reused for TIDAL destinations, so a TIDAL -> TIDAL transfer
  needs no catalog search (``can_reuse_identifier``).
* TIDAL favorites display the most recently added item first, so
  ``insertion_behavior`` is ``PREPEND``.
* TIDAL does not let us set a historical "date added"; migration time becomes
  the effective date (``preserves_custom_added_date = False``).
* TIDAL playlist appends accept duplicates, so source playlists with repeated
  entries are reproduced exactly.
* TIDAL exposes ISRC values for most tracks, which makes ISRC matching possible.
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.domain import (
    Account,
    AccountProfile,
    Album,
    Artist,
    LibrarySnapshot,
    Playlist,
    PlaylistItem,
    Track,
)
from ...core.enums import EntityType, InsertionBehavior, Platform
from ...core.errors import (
    AuthenticationError,
    AuthorizationError,
    MusicTransferError,
    TransferConfigurationError,
    UnsupportedCapabilityError,
)
from ...core.ports import (
    DestinationState,
    LibraryMaintenanceAdapter,
    MusicPlatformAdapter,
    PlatformCapabilities,
)
from .client import TidalLibraryClient
from .errors import ItemUnavailableError, TidalClientError, translate_provider_error

_LOGGER = logging.getLogger("music_transfer.platforms.tidal")

#: Sections that map onto favorites categories for read/write dispatch.
_TRACK_CATEGORY = "tracks"
_ALBUM_CATEGORY = "albums"
_ARTIST_CATEGORY = "artists"


class TidalAdapter(MusicPlatformAdapter, LibraryMaintenanceAdapter):
    """Expose TIDAL through the generic platform contract."""

    #: TIDAL's declared capabilities.
    CAPABILITIES = PlatformCapabilities(
        platform=Platform.TIDAL,
        read_liked_tracks=True,
        read_saved_albums=True,
        read_followed_artists=True,
        read_playlists=True,
        read_videos=True,
        read_mixes=True,
        read_folders=True,
        write_liked_tracks=True,
        write_saved_albums=True,
        write_followed_artists=True,
        create_playlists=True,
        write_playlist_items=True,
        write_videos=True,
        write_mixes=True,
        create_folders=True,
        delete_liked_tracks=True,
        delete_saved_albums=True,
        delete_followed_artists=True,
        delete_playlists=True,
        delete_folders=True,
        search_tracks=True,
        search_albums=False,
        search_artists=False,
        preserves_custom_added_date=False,
        supports_playlist_duplicates=True,
        supports_already_exists_detection=True,
        supports_batch_playlist_writes=False,
        insertion_behavior=InsertionBehavior.PREPEND,
        distinguishes_region_availability=True,
        exposes_isrc=True,
    )

    def __init__(
        self,
        client: TidalLibraryClient,
        *,
        account: Account | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._account = account
        self._logger = logger or _LOGGER

    @property
    def client(self) -> TidalLibraryClient:
        """Return the underlying TIDAL client (authentication and diagnostics)."""

        return self._client

    @property
    def platform(self) -> Platform:
        return Platform.TIDAL

    @property
    def capabilities(self) -> PlatformCapabilities:
        return self.CAPABILITIES

    # -- identity ----------------------------------------------------------

    def get_profile(self) -> AccountProfile:
        return self._read("get_profile", self._client.profile)

    def as_account(self, *, owner_user_id: str | None = None) -> Account:
        """Build an :class:`Account` from the connected session.

        Only an ``auth_reference`` (a keyring pointer) is attached; the token
        itself never enters the account record.
        """

        profile = self.get_profile()
        return Account(
            id=f"tidal:{profile.account_id}",
            platform=Platform.TIDAL,
            platform_account_id=profile.account_id,
            display_name=profile.display_name,
            owner_user_id=owner_user_id,
            auth_reference=f"keyring:{profile.account_id}",
        )

    # -- reads -------------------------------------------------------------

    def export_library(
        self, sections: tuple[str, ...] | None = None, progress: Any = None
    ) -> LibrarySnapshot:
        return self._read(
            "export_library",
            self._client.export_library,
            sections=sections,
            progress=progress,
        )

    def get_liked_tracks(self, progress: Any = None) -> list[Track]:
        return self._read("get_liked_tracks", self._client.liked_tracks, progress)

    def get_saved_albums(self, progress: Any = None) -> list[Album]:
        return self._read("get_saved_albums", self._client.saved_albums, progress)

    def get_followed_artists(self, progress: Any = None) -> list[Artist]:
        return self._read("get_followed_artists", self._client.followed_artists, progress)

    def get_playlists(self, progress: Any = None) -> list[Playlist]:
        return self._read("get_playlists", self._client.playlists, progress)

    def get_playlist_items(self, playlist_id: str, progress: Any = None) -> list[PlaylistItem]:
        return self._read("get_playlist_items", self._client.playlist_items, playlist_id)

    def get_destination_state(self, sections: tuple[str, ...] | None = None) -> DestinationState:
        """Read what the destination already contains.

        When ``sections is None``, all canonical destination sections are read.
        When ``sections`` is explicitly provided (e.g. ``()`` or ``("tracks",)``),
        only the requested sections are read.
        Unrequested sections remain UNKNOWN (not in complete_sections, not in incomplete_sections).
        Non-fatal/transient section failures are added to incomplete_sections and omitted from complete_sections.
        Fatal errors (AuthenticationError, AuthorizationError, UnsupportedCapabilityError, TransferConfigurationError)
        propagate immediately out of get_destination_state.
        Unknown section names fail closed before any provider read is initiated.
        """

        from ...core.ports.platform import KNOWN_DESTINATION_SECTIONS

        if sections is not None:
            for section in sections:
                if section not in KNOWN_DESTINATION_SECTIONS:
                    raise UnsupportedCapabilityError(
                        "capability_unsupported", capability=section
                    )
            wanted = set(sections)
        else:
            wanted = set(KNOWN_DESTINATION_SECTIONS)

        state = DestinationState(platform=Platform.TIDAL)
        complete: set[str] = set()
        incomplete: list[str] = []

        if "tracks" in wanted:
            try:
                state.track_ids = self._read_destination_section(
                    "tracks",
                    lambda: frozenset(track.source_id for track in self._client.liked_tracks()),
                )
                complete.add("tracks")
            except (
                AuthenticationError,
                AuthorizationError,
                UnsupportedCapabilityError,
                TransferConfigurationError,
            ):
                raise
            except MusicTransferError:
                incomplete.append("tracks")

        if "albums" in wanted:
            try:
                state.album_ids = self._read_destination_section(
                    "albums",
                    lambda: frozenset(album.source_id for album in self._client.saved_albums()),
                )
                complete.add("albums")
            except (
                AuthenticationError,
                AuthorizationError,
                UnsupportedCapabilityError,
                TransferConfigurationError,
            ):
                raise
            except MusicTransferError:
                incomplete.append("albums")

        if "artists" in wanted:
            try:
                state.artist_ids = self._read_destination_section(
                    "artists",
                    lambda: frozenset(artist.source_id for artist in self._client.followed_artists()),
                )
                complete.add("artists")
            except (
                AuthenticationError,
                AuthorizationError,
                UnsupportedCapabilityError,
                TransferConfigurationError,
            ):
                raise
            except MusicTransferError:
                incomplete.append("artists")

        if "videos" in wanted:
            try:
                state.video_ids = self._read_destination_section(
                    "videos",
                    lambda: frozenset(record.source_id for record in self._client.videos()),
                )
                complete.add("videos")
            except (
                AuthenticationError,
                AuthorizationError,
                UnsupportedCapabilityError,
                TransferConfigurationError,
            ):
                raise
            except MusicTransferError:
                incomplete.append("videos")

        if "mixes" in wanted:
            try:
                state.mix_ids = self._read_destination_section(
                    "mixes",
                    lambda: frozenset(record.source_id for record in self._client.mixes()),
                )
                complete.add("mixes")
            except (
                AuthenticationError,
                AuthorizationError,
                UnsupportedCapabilityError,
                TransferConfigurationError,
            ):
                raise
            except MusicTransferError:
                incomplete.append("mixes")

        if "playlists" in wanted:
            try:
                state.playlist_ids = self._read_destination_section(
                    "playlists",
                    lambda: frozenset(playlist.source_id for playlist in self._client.playlists()),
                )
                complete.add("playlists")
            except (
                AuthenticationError,
                AuthorizationError,
                UnsupportedCapabilityError,
                TransferConfigurationError,
            ):
                raise
            except MusicTransferError:
                incomplete.append("playlists")

        state.complete_sections = frozenset(complete)
        state.incomplete_sections = tuple(incomplete)
        return state

    def _read_destination_section(
        self, section: str, reader: Any
    ) -> frozenset[str]:
        """Read a single destination section, translating provider errors.

        Fatal errors (AuthenticationError, AuthorizationError, etc.) propagate immediately.
        Non-fatal section failures are logged and raised as MusicTransferError for isolation.
        """
        try:
            return reader()
        except (
            AuthenticationError,
            AuthorizationError,
            UnsupportedCapabilityError,
            TransferConfigurationError,
        ) as error:
            self._log_failure(f"destination_state_{section}", error)
            raise
        except (TidalClientError, ItemUnavailableError) as error:
            translated = translate_provider_error(error, operation_is_write=False)
            if isinstance(
                translated,
                (
                    AuthenticationError,
                    AuthorizationError,
                    UnsupportedCapabilityError,
                    TransferConfigurationError,
                ),
            ):
                self._log_failure(f"destination_state_{section}", translated)
                raise translated from None
            self._warn(f"destination_state_{section}", translated)
            raise translated from None
        except Exception as error:  # noqa: BLE001 - classified through translate_provider_error
            translated = translate_provider_error(error, operation_is_write=False)
            if isinstance(
                translated,
                (
                    AuthenticationError,
                    AuthorizationError,
                    UnsupportedCapabilityError,
                    TransferConfigurationError,
                ),
            ):
                self._log_failure(f"destination_state_{section}", translated)
                raise translated from None
            self._warn(f"destination_state_{section}", translated)
            raise translated from None



    def playlist_item_ids(self, playlist_id: str) -> list[str]:
        return self._read("playlist_item_ids", self._client.playlist_item_ids, playlist_id)

    # -- search ------------------------------------------------------------

    def search_track(self, track: Track, limit: int = 5) -> list[Track]:
        from ...core.matching import normalize_track

        query = normalize_track(track).search_query or track.title
        return self._read("search_track", self._client.search_tracks, query, limit)

    # -- writes ------------------------------------------------------------

    def save_track(self, track_id: str) -> None:
        self._write("save_track", _TRACK_CATEGORY, track_id)

    def save_album(self, album_id: str) -> None:
        self._write("save_album", _ALBUM_CATEGORY, album_id)

    def follow_artist(self, artist_id: str) -> None:
        self._write("follow_artist", _ARTIST_CATEGORY, artist_id)

    def save_video(self, video_id: str) -> None:
        self._write("save_video", "videos", video_id)

    def save_mix(self, mix_id: str) -> None:
        self._write("save_mix", "mixes", mix_id)

    def create_playlist(self, playlist: Playlist) -> str:
        """Create a playlist, mapping an ambiguous failure to the core error.

        A timeout here is *not* proof that the playlist was not created, so the
        failure is reported as ambiguous; the executor reconciles against
        destination state instead of creating a duplicate.
        """

        try:
            return self._client.create_playlist(
                playlist.name, playlist.description or "", playlist.folder_id or "root"
            )
        except (TidalClientError, ItemUnavailableError) as error:
            raise translate_provider_error(error, operation_is_write=True) from None
        except Exception as error:  # noqa: BLE001 - classified immediately below
            raise translate_provider_error(error, operation_is_write=True) from None

    def add_playlist_item(self, playlist_id: str, track_id: str) -> None:
        """Append one item, allowing duplicates.

        Raises:
            AmbiguousOperationError: When the append may or may not have landed.
            UnavailableError: When the item is not available to this account.
        """

        try:
            self._client.add_playlist_item(playlist_id, track_id)
        except (TidalClientError, ItemUnavailableError) as error:
            raise translate_provider_error(error, operation_is_write=True) from None
        except Exception as error:  # noqa: BLE001 - classified immediately below
            raise translate_provider_error(error, operation_is_write=True) from None

    def create_folder(self, name: str, parent_id: str) -> str:
        try:
            return self._client.create_folder(name, parent_id)
        except (TidalClientError, ItemUnavailableError) as error:
            raise translate_provider_error(error, operation_is_write=True) from None

    def favorite_playlist(self, playlist_id: str, parent_id: str = "root") -> None:
        """Add a public playlist to favorites (used for non-owned playlists)."""

        self._write("favorite_playlist", "playlists", playlist_id, parent_id)

    # -- destructive operations --------------------------------------------

    def remove_track(self, track_id: str) -> None:
        self._write("remove_track", _TRACK_CATEGORY, track_id)

    def remove_album(self, album_id: str) -> None:
        self._write("remove_album", _ALBUM_CATEGORY, album_id)

    def unfollow_artist(self, artist_id: str) -> None:
        self._write("unfollow_artist", _ARTIST_CATEGORY, artist_id)

    def remove_video(self, video_id: str) -> None:
        self._write("remove_video", "videos", video_id)

    def remove_mix(self, mix_id: str) -> None:
        self._write("remove_mix", "mixes", mix_id)

    def delete_playlist(self, playlist_id: str) -> None:
        try:
            self._client.delete_playlist(playlist_id)
        except (TidalClientError, ItemUnavailableError) as error:
            raise translate_provider_error(error, operation_is_write=False) from None

    def delete_folder(self, folder_id: str) -> None:
        try:
            self._client.delete_folder(folder_id)
        except (TidalClientError, ItemUnavailableError) as error:
            raise translate_provider_error(error, operation_is_write=False) from None

    def unfavorite_playlist(self, playlist_id: str) -> None:
        """Remove a non-owned playlist from favorites instead of deleting it."""

        self._write("unfavorite_playlist", "playlists", playlist_id)

    # -- capability query --------------------------------------------------

    def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
        """Return whether a source id can be written without a catalog search.

        Tracks, albums, and artists live in TIDAL's shared catalogue, so an id
        read from one account is valid on any other: a TIDAL -> TIDAL transfer
        can save it directly with no search at all.

        A **playlist** is different.  Its id has the same shape, but the object
        belongs to an account and does not exist on the destination until it is
        created there.  Reporting a playlist id as reusable would let the
        executor skip ``create_playlist`` and then fail writing every entry.
        """

        if source is not Platform.TIDAL:
            return False
        return entity_type is not EntityType.PLAYLIST

    # -- internals ---------------------------------------------------------

    def _read(self, operation: str, method: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a read-only provider call and translate failures."""

        try:
            return method(*args, **kwargs)
        except (UnsupportedCapabilityError, TransferConfigurationError) as error:
            self._log_failure(operation, error)
            raise
        except (TidalClientError, ItemUnavailableError) as error:
            self._log_failure(operation, error)
            raise translate_provider_error(error, operation_is_write=False) from None
        except Exception as error:  # noqa: BLE001 - classified immediately below
            self._log_failure(operation, error)
            raise translate_provider_error(error, operation_is_write=False) from None

    def _write(self, operation: str, category: str, item_id: str, *extra: Any) -> None:
        """Run a mutating favorites call and translate failures."""

        try:
            if operation == "favorite_playlist":
                self._client.favorite_playlist(item_id, extra[0] if extra else "root")
                return
            if operation == "unfavorite_playlist":
                self._client.remove_favorite(category, item_id)
                return
            if operation.startswith("remove_") or operation.startswith("unfollow_"):
                self._client.remove_favorite(category, item_id)
                return
            self._client.add_favorite(category, item_id)
        except (TidalClientError, ItemUnavailableError) as error:
            self._log_failure(operation, error)
            raise translate_provider_error(error, operation_is_write=True) from None
        except Exception as error:  # noqa: BLE001 - classified immediately below
            self._log_failure(operation, error)
            raise translate_provider_error(error, operation_is_write=True) from None

    def _log_failure(self, operation: str, error: Exception) -> None:
        """Log a failure with context only; never the exception message."""

        self._logger.error(
            "event=tidal_call_failed platform=tidal operation=%s error_type=%s",
            operation,
            type(error).__name__,
        )

    def _warn(self, operation: str, error: Exception) -> None:
        """Log a non-fatal read failure."""

        self._logger.warning(
            "event=tidal_read_failed platform=tidal operation=%s error_type=%s",
            operation,
            type(error).__name__,
        )


def build_tidal_adapter(client: TidalLibraryClient, **kwargs: Any) -> TidalAdapter:
    """Factory used by the platform registry."""

    return TidalAdapter(client, **kwargs)


__all__ = ["TidalAdapter", "build_tidal_adapter"]
