"""The TIDAL session wrapper: the only module that calls ``tidalapi``.

This is the migrated, proven ``tidal_manager.core.auth.TidalLibraryClient``.
Behaviour is preserved deliberately:

* every request gets a bounded timeout;
* transient failures are retried with exponential backoff, honouring
  ``Retry-After``;
* non-idempotent writes are **not** auto-retried, because a timeout does not
  prove the write failed;
* playlist appends use ``allow_duplicates=True`` so a source playlist with
  repeated entries is reproduced exactly;
* folder traversal detects cycles instead of recursing forever;
* an unreadable export section is recorded as incomplete rather than omitted.

What changed is the vocabulary, not the behaviour: results are universal domain
objects, and failures are core error types.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ...core.domain import (
    AccountProfile,
    Album,
    Artist,
    LibraryRecord,
    LibrarySnapshot,
    Playlist,
    PlaylistItem,
    Track,
)
from ...core.enums import Platform
from ...core.errors import (
    AuthenticationError,
    MusicTransferError,
    UnsupportedCapabilityError,
)
from ...infrastructure.http import (
    ProviderRequestError,
    RetryCallback,
    RetryEvent,
    RetryExecutor,
    RetryPolicy,
    install_request_timeout,
)
from .errors import (
    ItemUnavailableError,
    TidalClientError,
    looks_unavailable,
    translate_provider_error,
)
from .mapper import (
    album_from_tidal,
    artist_from_tidal,
    folder_record_from_tidal,
    playlist_from_tidal,
    playlist_item_from_tidal,
    record_from_tidal,
    track_from_tidal,
)
from .pagination import DEFAULT_POLICY, PLAYLIST_POLICY, PaginationPolicy, fetch_all

ProgressCallback = Callable[[str, int, int], None]

#: Favorites categories mapped to their ``tidalapi`` add methods.
_ADD_METHODS: dict[str, str] = {
    "tracks": "add_track",
    "albums": "add_album",
    "artists": "add_artist",
    "videos": "add_video",
    "mixes": "add_mixes",
}

#: Favorites categories mapped to their ``tidalapi`` remove methods.
_REMOVE_METHODS: dict[str, str] = {
    "tracks": "remove_track",
    "albums": "remove_album",
    "artists": "remove_artist",
    "videos": "remove_video",
    "mixes": "remove_mixes",
    "playlists": "remove_playlist",
}


class TidalLibraryClient:
    """Adapt ``tidalapi`` objects to portable library operations."""

    def __init__(
        self,
        session: Any,
        logger: logging.Logger,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._session = session
        self._logger = logger
        self._retry = RetryExecutor(logger, retry_policy)
        self._playlist_cache: dict[str, Any] = {}
        install_request_timeout(session, self._retry.policy)

    @property
    def session(self) -> Any:
        """Return the underlying ``tidalapi`` session.

        Exposed only for authentication and diagnostics; callers must not log
        it, because it carries tokens.
        """

        return self._session

    def set_retry_callback(self, callback: RetryCallback | None) -> None:
        """Expose sanitized retry events to a presentation layer."""

        self._retry.set_retry_callback(callback)

    # -- identity ----------------------------------------------------------

    def profile(self) -> AccountProfile:
        """Return safe account metadata for the connected session."""

        user = getattr(self._session, "user", None)
        account_id = getattr(user, "id", None)
        if account_id is None:
            raise TidalClientError("account_missing")
        display_name = getattr(user, "username", None)
        if not display_name:
            first_name = getattr(user, "first_name", None)
            last_name = getattr(user, "last_name", None)
            display_name = (
                " ".join(part for part in (first_name, last_name) if isinstance(part, str))
                or None
            )
        return AccountProfile(
            account_id=str(account_id),
            display_name=str(display_name) if display_name else None,
            platform=Platform.TIDAL,
        )

    # -- reads -------------------------------------------------------------

    KNOWN_SECTIONS: tuple[str, ...] = (
        "tracks",
        "albums",
        "artists",
        "videos",
        "mixes",
        "folders",
        "playlists",
    )

    def export_library(
        self,
        sections: tuple[str, ...] | list[str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> LibrarySnapshot:
        """Export supported sections, marking unreadable requested ones incomplete.

        When ``sections is None``, all supported sections are exported.
        When ``sections`` is explicitly specified, only the requested producers
        run.  Unrequested sections remain empty and are not marked incomplete.
        Unknown section names fail closed before executing any producer.
        """

        if sections is not None:
            for section in sections:
                if section not in self.KNOWN_SECTIONS:
                    raise UnsupportedCapabilityError(
                        "capability_unsupported", capability=section
                    )

        self._logger.info("event=library_export_started platform=tidal")
        snapshot = LibrarySnapshot(
            account=self.profile(),
            platform=Platform.TIDAL,
        )
        producers: tuple[tuple[str, Callable[[], list[Any]]], ...] = (
            ("tracks", lambda: self.liked_tracks(progress)),
            ("albums", lambda: self.saved_albums(progress)),
            ("artists", lambda: self.followed_artists(progress)),
            ("videos", lambda: self.videos(progress)),
            ("mixes", lambda: self.mixes(progress)),
            ("folders", lambda: self.folders(progress)),
            ("playlists", lambda: self.playlists(progress)),
        )
        if sections is None:
            selected = producers
        else:
            requested = set(sections)
            selected = tuple(p for p in producers if p[0] in requested)

        for section, producer in selected:
            try:
                if progress:
                    progress(section, 0, 0)
                values = producer()
                setattr(snapshot, section, values)
                if progress:
                    progress(section, len(values), len(values))
            except Exception as error:  # noqa: BLE001 - section isolation is the point
                snapshot.incomplete_sections.append(section)
                self._logger.error(
                    "event=library_export_section_failed section=%s error_type=%s",
                    section,
                    type(error).__name__,
                )
        self._logger.info(
            "event=library_export_completed partial=%s sections=%d",
            bool(snapshot.incomplete_sections),
            len(snapshot.incomplete_sections),
        )
        return snapshot

    def liked_tracks(self, progress: ProgressCallback | None = None) -> list[Track]:
        """Return every favorited track."""

        return self._collect(
            "export_tracks",
            lambda limit, offset: self._favorites().tracks(limit=limit, offset=offset),
            lambda value: track_from_tidal(value),
            progress,
            "tracks",
        )

    def saved_albums(self, progress: ProgressCallback | None = None) -> list[Album]:
        """Return every saved album."""

        return self._collect(
            "export_albums",
            lambda limit, offset: self._favorites().albums(limit=limit, offset=offset),
            album_from_tidal,
            progress,
            "albums",
        )

    def followed_artists(self, progress: ProgressCallback | None = None) -> list[Artist]:
        """Return every followed artist."""

        return self._collect(
            "export_artists",
            lambda limit, offset: self._favorites().artists(limit=limit, offset=offset),
            artist_from_tidal,
            progress,
            "artists",
        )

    def videos(self, progress: ProgressCallback | None = None) -> list[LibraryRecord]:
        """Return every saved video."""

        return self._collect(
            "export_videos",
            lambda limit, offset: self._favorites().videos(limit=limit, offset=offset),
            record_from_tidal,
            progress,
            "videos",
        )

    def mixes(self, progress: ProgressCallback | None = None) -> list[LibraryRecord]:
        """Return every saved mix or radio item."""

        return self._collect(
            "export_mixes",
            lambda limit, offset: self._favorites().mixes(limit=limit, offset=offset),
            record_from_tidal,
            progress,
            "mixes",
        )

    def folders(
        self, progress: ProgressCallback | None = None
    ) -> list[LibraryRecord]:
        """Return every playlist folder, depth-first, with cycle protection."""

        exported: list[LibraryRecord] = []
        visited: set[str] = set()
        membership: dict[str, str] = {}

        def visit(parent_id: str) -> None:
            for folder in fetch_all(
                lambda limit, offset: self._favorites().playlist_folders(
                    limit=limit, offset=offset, parent_folder_id=parent_id
                ),
                operation="export_folders",
                logger=self._logger,
            ):
                folder_id = _required_id(folder)
                if folder_id in visited:
                    self._logger.error("event=folder_cycle_detected folder_id=%s", folder_id)
                    continue
                visited.add(folder_id)
                exported.append(folder_record_from_tidal(folder, parent_id))
                self._emit(progress, "folders", len(exported))
                for playlist in fetch_all(
                    folder.items,
                    operation="export_folder_items",
                    logger=self._logger,
                ):
                    membership[_required_id(playlist)] = folder_id
                visit(folder_id)

        visit("root")
        self._folder_membership = membership
        return exported

    def playlists(self, progress: ProgressCallback | None = None) -> list[Playlist]:
        """Return every playlist with its ordered items."""

        user = self._session.user
        raw_playlists = fetch_all(
            user.playlist_and_favorite_playlists,
            operation="export_playlists",
            logger=self._logger,
        )
        profile = self.profile()
        membership = getattr(self, "_folder_membership", {})
        result: list[Playlist] = []
        for raw in raw_playlists:
            playlist_id = _required_id(raw)
            items = self.playlist_items(playlist_id, progress=progress)
            result.append(
                playlist_from_tidal(
                    raw,
                    items=items,
                    account_id=profile.account_id,
                    folder_id=membership.get(playlist_id, "root"),
                )
            )
            self._emit(progress, "playlists", len(result))
        return result

    def playlist_items(
        self, playlist_id: str, progress: ProgressCallback | None = None
    ) -> list[PlaylistItem]:
        """Return one playlist's items in exact order, duplicates preserved."""

        playlist = self._playlist(playlist_id)
        raw_items = fetch_all(
            playlist.items,
            policy=PLAYLIST_POLICY,
            operation="export_playlist_items",
            logger=self._logger,
        )
        items = [
            playlist_item_from_tidal(raw, position)
            for position, raw in enumerate(raw_items)
        ]
        self._emit(progress, "playlists", len(items))
        return items

    def playlist_media_order(self, playlist_id: str) -> list[dict[str, str]]:
        """Return a destination playlist's exact media order for safe resumption."""

        items = fetch_all(
            self._playlist(playlist_id).items,
            policy=PLAYLIST_POLICY,
            operation="playlist_order_read",
            logger=self._logger,
        )
        return [
            {
                "kind": "video" if "video" in type(item).__name__.lower() else "track",
                "id": _required_id(item),
            }
            for item in items
        ]

    def playlist_item_ids(self, playlist_id: str) -> list[str]:
        """Return the exact media id sequence of a playlist.

        Duplicate occurrences appear once each, which is what makes order
        verification and interrupted-write reconciliation correct.
        """

        return [entry["id"] for entry in self.playlist_media_order(playlist_id)]

    def search_tracks(self, query: str, limit: int = 5) -> list[Track]:
        """Search the TIDAL catalog for tracks."""

        results = self._invoke(
            "search_tracks",
            lambda: self._session.search(query, models=[_track_model()], limit=limit),
        )
        tracks = getattr(results, "tracks", None)
        return [track_from_tidal(item) for item in (tracks or [])]

    # -- writes ------------------------------------------------------------

    def add_favorite(self, category: str, item_id: str) -> None:
        """Add one library item to the connected account's favorites."""

        favorites = self._favorites()
        method_name = _ADD_METHODS.get(category)
        if method_name is None:
            raise TidalClientError("favorite_category_unsupported")
        self._call_mutation(
            f"favorite_add_{category}", getattr(favorites, method_name), item_id
        )

    def remove_favorite(self, category: str, item_id: str) -> None:
        """Remove one library item from the connected account's favorites."""

        favorites = self._favorites()
        method_name = _REMOVE_METHODS.get(category)
        if method_name is None:
            raise TidalClientError("favorite_category_unsupported")
        self._call_mutation(
            f"favorite_remove_{category}", getattr(favorites, method_name), item_id
        )

    def create_folder(self, title: str, parent_id: str) -> str:
        """Create a playlist folder and return its TIDAL identifier."""

        folder = self._invoke(
            "folder_create",
            lambda: self._session.user.create_folder(title, parent_id),
            retry_safe=False,
        )
        folder_id = getattr(folder, "id", None)
        if not folder_id:
            raise TidalClientError("folder_creation_failed")
        return str(folder_id)

    def create_playlist(self, title: str, description: str, parent_id: str = "root") -> str:
        """Create a playlist and return its TIDAL identifier."""

        playlist = self._invoke(
            "playlist_create",
            lambda: self._session.user.create_playlist(title, description, parent_id),
            retry_safe=False,
        )
        playlist_id = getattr(playlist, "id", None)
        if not playlist_id:
            raise TidalClientError("playlist_creation_failed")
        self._playlist_cache[str(playlist_id)] = playlist
        return str(playlist_id)

    def favorite_playlist(self, playlist_id: str, parent_id: str) -> None:
        """Add a public playlist to favorites in the given folder."""

        self._call_mutation(
            "playlist_favorite", self._favorites().add_playlist, playlist_id, parent_id
        )

    def add_playlist_item(self, playlist_id: str, media_id: str) -> None:
        """Append one media item, allowing duplicates for exact order restoration."""

        playlist = self._playlist(playlist_id)
        result = self._invoke(
            "playlist_item_add",
            lambda: playlist.add([str(media_id)], allow_duplicates=True),
            retry_safe=False,
        )
        if not result:
            raise ItemUnavailableError("playlist_item_not_added")

    # -- destructive operations (library management only) -------------------

    def delete_playlist(self, playlist_id: str) -> None:
        """Permanently delete a playlist owned by the connected account."""

        playlist = self._playlist_cache.get(playlist_id) or self._session.playlist(playlist_id)
        result = self._invoke("playlist_delete", playlist.delete)
        if result is False:
            raise TidalClientError("playlist_deletion_failed")
        self._playlist_cache.pop(playlist_id, None)

    def delete_folder(self, folder_id: str) -> None:
        """Remove an empty playlist folder."""

        self._call_mutation(
            "folder_delete",
            self._favorites().remove_folders_playlists,
            f"trn:folder:{folder_id}",
            "folder",
        )

    # -- internals ---------------------------------------------------------

    def _collect(
        self,
        operation: str,
        getter: Callable[..., Any],
        mapper: Callable[[Any], Any],
        progress: ProgressCallback | None,
        category: str,
        policy: PaginationPolicy = DEFAULT_POLICY,
    ) -> list[Any]:
        """Paginate a provider getter and map every item to a domain object."""

        values: list[Any] = []
        for raw in fetch_all(
            lambda limit, offset: self._invoke(operation, lambda: getter(limit=limit, offset=offset)),
            policy=policy,
            operation=operation,
            progress=lambda collected, _pages: self._emit(progress, category, collected),
            logger=self._logger,
        ):
            values.append(mapper(raw))
        return values

    def _favorites(self) -> Any:
        """Return the favorites helper of the connected user."""

        user = getattr(self._session, "user", None)
        favorites = getattr(user, "favorites", None)
        if favorites is None:
            raise TidalClientError("favorites_unavailable")
        return favorites

    def _playlist(self, playlist_id: str) -> Any:
        """Return a cached playlist object."""

        playlist = self._playlist_cache.get(playlist_id)
        if playlist is None:
            playlist = self._session.playlist(playlist_id)
            self._playlist_cache[playlist_id] = playlist
        return playlist

    @staticmethod
    def _emit(progress: ProgressCallback | None, category: str, current: int) -> None:
        if progress is not None:
            progress(category, current, 0)

    def _call_mutation(self, operation: str, method: Callable[..., Any], *args: Any) -> None:
        """Normalize failed provider mutation responses into typed errors."""

        try:
            result = self._invoke(operation, lambda: method(*args))
        except (TidalClientError, ItemUnavailableError, MusicTransferError):
            raise
        except Exception as error:  # noqa: BLE001 - classified immediately below
            if looks_unavailable(error):
                raise ItemUnavailableError("item_unavailable") from None
            raise TidalClientError("provider_mutation_failed") from None
        if result is False:
            raise TidalClientError("provider_mutation_rejected")

    def _invoke(
        self, operation: str, action: Callable[[], Any], *, retry_safe: bool = True
    ) -> Any:
        """Run an upstream call with bounded retries and translated failures."""

        try:
            return self._retry.call(operation, action, retry_safe=retry_safe)
        except ProviderRequestError as error:
            raise translate_provider_error(error, operation_is_write=not retry_safe) from None
        except AuthenticationError:
            raise


def _required_id(value: Any) -> str:
    """Extract an identifier or raise a normalized integration error."""

    identifier = getattr(value, "id", None)
    if identifier is None or not str(identifier):
        raise TidalClientError("provider_id_missing")
    return str(identifier)


def _track_model() -> Any:
    """Return the ``tidalapi`` track model, imported lazily."""

    import importlib

    tidalapi = importlib.import_module("tidalapi")
    return getattr(tidalapi, "Track", None) or tidalapi.media.Track


__all__ = [
    "ProgressCallback",
    "RetryEvent",
    "TidalLibraryClient",
]
