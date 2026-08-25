"""OAuth authentication and the isolated ``tidalapi`` integration adapter.

The adapter is the only module that imports and calls ``tidalapi``.  It keeps
provider-version details out of backups, transfer logic, and the user interface.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from .models import AccountProfile, LibrarySnapshot
from .retry import (
    ProviderRequestError,
    RetryCallback,
    RetryExecutor,
    RetryPolicy,
    configure_tidal_session,
)


ProgressCallback = Callable[[str, int, int], None]
MessageCallback = Callable[..., None]


class AccountRole(StrEnum):
    """The two deliberately separate OAuth identities used by the application."""

    SOURCE = "source"
    DESTINATION = "destination"


class AuthenticationError(RuntimeError):
    """Raised when an OAuth session cannot be established safely."""


class TidalClientError(RuntimeError):
    """Raised for normalized, non-secret TIDAL integration failures."""

    def __init__(self, reason: str = "provider_error", attempts: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class ItemUnavailableError(TidalClientError):
    """Raised when an item is no longer available to the destination account."""


class CredentialStore:
    """Store OAuth credentials in the OS keyring, never in project files."""

    _service_name = "tidal-library-manager"

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def load(self, role: AccountRole) -> dict[str, str | None] | None:
        """Load a role-specific OAuth credential from the configured keyring."""

        keyring = self._keyring_module()
        if keyring is None:
            return None
        try:
            raw = keyring.get_password(self._service_name, role.value)
            if not raw:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("credential_shape_invalid")
            required = ("token_type", "access_token")
            if not all(isinstance(value.get(key), str) for key in required):
                raise ValueError("credential_fields_invalid")
            return {
                "token_type": str(value["token_type"]),
                "access_token": str(value["access_token"]),
                "refresh_token": _optional_string(value.get("refresh_token")),
                "expiry_time": _optional_string(value.get("expiry_time")),
            }
        except Exception as error:
            self._logger.warning(
                "event=credential_load_failed role=%s error_type=%s",
                role.value,
                type(error).__name__,
            )
            self.delete(role)
            return None

    def save(self, role: AccountRole, session: Any) -> bool:
        """Persist OAuth tokens in the OS keyring when a secure backend is available."""

        keyring = self._keyring_module()
        if keyring is None:
            return False
        expiry = getattr(session, "expiry_time", None)
        payload = {
            "token_type": str(getattr(session, "token_type", "")),
            "access_token": str(getattr(session, "access_token", "")),
            "refresh_token": _optional_string(getattr(session, "refresh_token", None)),
            "expiry_time": expiry.isoformat() if hasattr(expiry, "isoformat") else None,
        }
        if not payload["token_type"] or not payload["access_token"]:
            self._logger.warning("event=credential_save_skipped role=%s", role.value)
            return False
        try:
            keyring.set_password(self._service_name, role.value, json.dumps(payload))
            return True
        except Exception as error:
            self._logger.warning(
                "event=credential_save_failed role=%s error_type=%s",
                role.value,
                type(error).__name__,
            )
            return False

    def delete(self, role: AccountRole) -> None:
        """Remove a role's OAuth credential from the OS keyring."""

        keyring = self._keyring_module()
        if keyring is None:
            return
        try:
            keyring.delete_password(self._service_name, role.value)
        except Exception:
            return

    def _keyring_module(self) -> Any | None:
        """Return keyring only when it has a usable non-fail backend."""

        try:
            keyring = importlib.import_module("keyring")
            backend = keyring.get_keyring()
            if "fail" in type(backend).__module__.lower():
                return None
            return keyring
        except Exception:
            return None


class TidalAuthenticator:
    """Establish independent ``tidalapi`` OAuth sessions for each account role."""

    def __init__(
        self,
        credentials: CredentialStore,
        logger: logging.Logger,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._credentials = credentials
        self._logger = logger
        self._retry = RetryExecutor(logger, retry_policy)

    def connect(self, role: AccountRole, emit: MessageCallback) -> TidalLibraryClient:
        """Restore or start a browser/device OAuth flow for one account role."""

        session = self._new_session()
        restored = self._restore_session(session, role)
        if not restored:
            self._login_with_oauth(session, emit, role)
        try:
            if not self._retry.call("authentication_check", session.check_login):
                raise AuthenticationError("session_not_valid")
        except AuthenticationError:
            raise
        except Exception as error:
            self._logger.warning(
                "event=authentication_validation_failed role=%s error_type=%s",
                role.value,
                type(error).__name__,
            )
            raise AuthenticationError("authentication_failed") from None
        if not self._credentials.save(role, session):
            emit("auth.session_not_saved")
        self._logger.info("event=authentication_completed role=%s", role.value)
        return TidalLibraryClient(session, self._logger, self._retry.policy)

    def forget(self, role: AccountRole) -> None:
        """Forget a previously saved OAuth session for one role."""

        self._credentials.delete(role)
        self._logger.info("event=authentication_forgotten role=%s", role.value)

    def _new_session(self) -> Any:
        try:
            tidalapi = importlib.import_module("tidalapi")
            session = tidalapi.Session()
            configure_tidal_session(session, self._retry.policy)
            return session
        except ImportError as error:
            raise AuthenticationError("tidalapi_missing") from error
        except Exception as error:
            self._logger.error("event=session_initialization_failed error_type=%s", type(error).__name__)
            raise AuthenticationError("session_initialization_failed") from None

    def _restore_session(self, session: Any, role: AccountRole) -> bool:
        credential = self._credentials.load(role)
        if credential is None:
            return False
        expiry = _parse_expiry(credential["expiry_time"])
        try:
            loaded = self._retry.call(
                "authentication_restore",
                lambda: session.load_oauth_session(
                    credential["token_type"],
                    credential["access_token"],
                    credential["refresh_token"],
                    expiry,
                ),
            )
            if loaded and self._retry.call("authentication_restore_check", session.check_login):
                self._logger.info("event=authentication_restored role=%s", role.value)
                return True
        except Exception as error:
            self._logger.info(
                "event=authentication_restore_rejected role=%s error_type=%s",
                role.value,
                type(error).__name__,
            )
        self._credentials.delete(role)
        return False

    def _login_with_oauth(self, session: Any, emit: MessageCallback, role: AccountRole) -> None:
        try:
            login, future = self._retry.call("oauth_start", session.login_oauth)
            url = getattr(login, "verification_uri_complete", None)
            if not isinstance(url, str) or not url:
                raise AuthenticationError("oauth_url_missing")
            emit("auth.open_url", url=url)
            future.result()
            self._logger.info("event=oauth_login_completed role=%s", role.value)
        except AuthenticationError:
            raise
        except Exception as error:
            self._logger.warning(
                "event=oauth_login_failed role=%s error_type=%s",
                role.value,
                type(error).__name__,
            )
            raise AuthenticationError("oauth_login_failed") from None


class TidalLibraryClient:
    """Adapt current ``tidalapi`` objects to portable library operations."""

    def __init__(
        self,
        session: Any,
        logger: logging.Logger,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._session = session
        self._logger = logger
        self._playlist_cache: dict[str, Any] = {}
        self._retry = RetryExecutor(logger, retry_policy)
        configure_tidal_session(session, self._retry.policy)

    def set_retry_callback(self, callback: RetryCallback | None) -> None:
        """Expose sanitized retry events to the localized presentation layer."""

        self._retry.set_retry_callback(callback)

    def profile(self) -> AccountProfile:
        """Return safe account metadata for a connected session."""

        user = getattr(self._session, "user", None)
        account_id = getattr(user, "id", None)
        if account_id is None:
            raise TidalClientError("account_missing")
        display_name = getattr(user, "username", None)
        if not display_name:
            first_name = getattr(user, "first_name", None)
            last_name = getattr(user, "last_name", None)
            display_name = " ".join(
                part for part in (first_name, last_name) if isinstance(part, str)
            ) or None
        return AccountProfile(account_id=str(account_id), display_name=display_name)

    def export_library(self, progress: ProgressCallback | None = None) -> LibrarySnapshot:
        """Export all supported library sections without credentials.

        A section that the current upstream API cannot read is explicitly marked
        incomplete instead of being silently omitted from a backup.
        """

        self._logger.info("event=library_export_started")
        snapshot = LibrarySnapshot(account=self.profile())
        producers: tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...] = (
            ("tracks", lambda: self._export_tracks(progress)),
            ("albums", lambda: self._export_albums(progress)),
            ("artists", lambda: self._export_artists(progress)),
            ("videos", lambda: self._export_videos(progress)),
            ("mixes", lambda: self._export_mixes(progress)),
            ("folders", lambda: self._export_folders(progress)),
            ("playlists", lambda: self._export_playlists(progress)),
        )
        for section, producer in producers:
            try:
                if progress:
                    progress(section, 0, 0)
                values = producer()
                setattr(snapshot, section, values)
                if progress:
                    progress(section, len(values), len(values))
            except Exception as error:
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

    def add_favorite(self, category: str, item_id: str) -> None:
        """Add a library item to the connected account's favorites."""

        favorites = self._favorites()
        methods = {
            "tracks": "add_track",
            "albums": "add_album",
            "artists": "add_artist",
            "videos": "add_video",
            "mixes": "add_mixes",
        }
        method_name = methods.get(category)
        if method_name is None:
            raise TidalClientError("favorite_category_unsupported")
        self._call_mutation(
            f"favorite_add_{category}", getattr(favorites, method_name), item_id
        )

    def remove_favorite(self, category: str, item_id: str) -> None:
        """Remove a library item from the connected account's favorites."""

        favorites = self._favorites()
        methods = {
            "tracks": "remove_track",
            "albums": "remove_album",
            "artists": "remove_artist",
            "videos": "remove_video",
            "mixes": "remove_mixes",
            "playlists": "remove_playlist",
        }
        method_name = methods.get(category)
        if method_name is None:
            raise TidalClientError("favorite_category_unsupported")
        self._call_mutation(
            f"favorite_remove_{category}", getattr(favorites, method_name), item_id
        )

    def create_folder(self, title: str, parent_id: str) -> str:
        """Create a destination playlist folder and return its TIDAL ID."""

        folder = self._invoke(
            "folder_create",
            lambda: self._session.user.create_folder(title, parent_id),
            retry_safe=False,
        )
        folder_id = getattr(folder, "id", None)
        if not folder_id:
            raise TidalClientError("folder_creation_failed")
        return str(folder_id)

    def create_playlist(self, title: str, description: str, parent_id: str) -> str:
        """Create a destination playlist and return its TIDAL ID."""

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
        """Add a public source playlist to destination favorites in its folder."""

        self._call_mutation(
            "playlist_favorite", self._favorites().add_playlist, playlist_id, parent_id
        )

    def add_playlist_item(self, playlist_id: str, media_id: str) -> None:
        """Append one media item, allowing duplicates for exact order restoration."""

        playlist = self._playlist_cache.get(playlist_id)
        if playlist is None:
            playlist = self._session.playlist(playlist_id)
            self._playlist_cache[playlist_id] = playlist
        result = self._invoke(
            "playlist_item_add",
            lambda: playlist.add([str(media_id)], allow_duplicates=True),
            retry_safe=False,
        )
        if not result:
            raise ItemUnavailableError("playlist_item_not_added")

    def playlist_media_order(self, playlist_id: str) -> list[dict[str, str]]:
        """Return a destination playlist's exact media order for safe resumption."""

        playlist = self._playlist_cache.get(playlist_id)
        if playlist is None:
            playlist = self._session.playlist(playlist_id)
            self._playlist_cache[playlist_id] = playlist
        return [
            {
                "kind": "video" if "video" in type(item).__name__.lower() else "track",
                "id": _required_id(item),
            }
            for item in self._paginate(
                playlist.items, operation="playlist_order_read", page_size=100
            )
        ]

    def delete_playlist(self, playlist_id: str) -> None:
        """Permanently delete a playlist owned by the connected account."""

        playlist = self._playlist_cache.get(playlist_id) or self._session.playlist(playlist_id)
        result = self._invoke("playlist_delete", playlist.delete)
        if result is False:
            raise TidalClientError("playlist_deletion_failed")
        self._playlist_cache.pop(playlist_id, None)

    def delete_folder(self, folder_id: str) -> None:
        """Remove an empty playlist folder from the connected account."""

        trn = f"trn:folder:{folder_id}"
        self._call_mutation(
            "folder_delete", self._favorites().remove_folders_playlists, trn, "folder"
        )

    def _favorites(self) -> Any:
        user = getattr(self._session, "user", None)
        favorites = getattr(user, "favorites", None)
        if favorites is None:
            raise TidalClientError("favorites_unavailable")
        return favorites

    def _export_tracks(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        return [
            self._track_record(track)
            for track in self._paginate(
                self._favorites().tracks, operation="export_tracks", progress=progress, category="tracks"
            )
        ]

    def _export_albums(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        return [
            self._album_record(album)
            for album in self._paginate(
                self._favorites().albums, operation="export_albums", progress=progress, category="albums"
            )
        ]

    def _export_artists(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        return [
            {"id": _required_id(artist), "name": _text_value(artist, "name")}
            for artist in self._paginate(
                self._favorites().artists, operation="export_artists", progress=progress, category="artists"
            )
        ]

    def _export_videos(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        return [
            {"id": _required_id(video), "title": _text_value(video, "name", "title")}
            for video in self._paginate(
                self._favorites().videos, operation="export_videos", progress=progress, category="videos"
            )
        ]

    def _export_mixes(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        return [
            {"id": _required_id(mix), "title": _text_value(mix, "title", "name")}
            for mix in self._paginate(
                self._favorites().mixes, operation="export_mixes", progress=progress, category="mixes"
            )
        ]

    def _export_folders(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        self._folder_membership: dict[str, str] = {}
        exported: list[dict[str, Any]] = []
        visited: set[str] = set()

        def visit(parent_id: str) -> None:
            for folder in self._paginate(
                lambda limit, offset: self._favorites().playlist_folders(
                    limit=limit, offset=offset, parent_folder_id=parent_id
                ),
                operation="export_folders",
                progress=progress,
                category="folders",
            ):
                folder_id = _required_id(folder)
                if folder_id in visited:
                    self._logger.error("event=folder_cycle_detected folder_id=%s", folder_id)
                    continue
                visited.add(folder_id)
                exported.append(
                    {
                        "id": folder_id,
                        "name": _text_value(folder, "name"),
                        "parent_id": parent_id,
                        "created_at": _date_value(folder, "created"),
                    }
                )
                for playlist in self._paginate(
                    folder.items,
                    operation="export_folder_items",
                    progress=progress,
                    category="folders",
                ):
                    self._folder_membership[_required_id(playlist)] = folder_id
                visit(folder_id)

        visit("root")
        return exported

    def _export_playlists(self, progress: ProgressCallback | None) -> list[dict[str, Any]]:
        user = self._session.user
        playlists = self._paginate(
            user.playlist_and_favorite_playlists,
            operation="export_playlists",
            progress=progress,
            category="playlists",
        )
        profile = self.profile()
        membership = getattr(self, "_folder_membership", {})
        return [
            self._playlist_record(
                playlist,
                profile.account_id,
                membership.get(_required_id(playlist), "root"),
                progress,
            )
            for playlist in playlists
        ]

    def _playlist_record(
        self, playlist: Any, account_id: str, folder_id: str, progress: ProgressCallback | None
    ) -> dict[str, Any]:
        items = self._paginate(
            playlist.items,
            operation="export_playlist_items",
            page_size=100,
            progress=progress,
            category="playlists",
        )
        track_order: list[str] = []
        video_order: list[str] = []
        item_order: list[dict[str, str]] = []
        for item in items:
            media_id = _required_id(item)
            category = "video" if "video" in type(item).__name__.lower() else "track"
            if category == "video":
                video_order.append(media_id)
            else:
                track_order.append(media_id)
            item_order.append({"kind": category, "id": media_id})
        creator = getattr(playlist, "creator", None)
        creator_id = str(getattr(creator, "id", ""))
        owned = "userplaylist" in type(playlist).__name__.lower() or creator_id == account_id
        return {
            "id": _required_id(playlist),
            "name": _text_value(playlist, "name"),
            "description": _text_value(playlist, "description"),
            "folder_id": folder_id,
            "created_at": _date_value(playlist, "created"),
            "is_owned": owned,
            "track_order": track_order,
            "video_order": video_order,
            "item_order": item_order,
        }

    def _track_record(self, track: Any) -> dict[str, Any]:
        album = getattr(track, "album", None)
        return {
            "id": _required_id(track),
            "title": _text_value(track, "name", "title"),
            "artist": _artist_name(track),
            "album": _text_value(album, "name", "title"),
            "duration": _number_value(track, "duration"),
            "isrc": _optional_string(getattr(track, "isrc", None)),
            "added_at": _date_value(track, "user_date_added"),
        }

    def _album_record(self, album: Any) -> dict[str, Any]:
        return {
            "id": _required_id(album),
            "title": _text_value(album, "name", "title"),
            "artist": _artist_name(album),
            "release_date": _date_value(album, "available_release_date", "release_date"),
        }

    def _paginate(
        self,
        getter: Callable[..., list[Any]],
        page_size: int = 50,
        *,
        operation: str,
        progress: ProgressCallback | None = None,
        category: str | None = None,
    ) -> list[Any]:
        """Retrieve a paginated tidalapi collection without an arbitrary limit."""

        values: list[Any] = []
        offset = 0
        while True:
            page = self._invoke(
                operation,
                lambda: getter(limit=page_size, offset=offset),
            )
            if not page:
                return values
            values.extend(page)
            if progress and category:
                progress(category, len(values), 0)
            if len(page) < page_size:
                return values
            offset += len(page)

    def _call_mutation(
        self, operation: str, method: Callable[..., Any], *args: Any
    ) -> None:
        """Normalize failed provider mutation responses into typed errors."""

        try:
            result = self._invoke(operation, lambda: method(*args))
        except TidalClientError:
            raise
        except Exception as error:
            if _looks_unavailable(error):
                raise ItemUnavailableError("item_unavailable") from None
            raise TidalClientError("provider_mutation_failed") from None
        if result is False:
            raise TidalClientError("provider_mutation_rejected")

    def _invoke(
        self, operation: str, action: Callable[[], Any], *, retry_safe: bool = True
    ) -> Any:
        """Run an upstream call with bounded retries and normalized failures."""

        try:
            return self._retry.call(operation, action, retry_safe=retry_safe)
        except ProviderRequestError as error:
            if error.reason == "item_unavailable":
                raise ItemUnavailableError(error.reason, error.attempts) from None
            raise TidalClientError(error.reason, error.attempts) from None


def _optional_string(value: Any) -> str | None:
    """Convert a non-empty value to a string or return ``None``."""

    return str(value) if value is not None and str(value) else None


def _parse_expiry(value: str | None) -> datetime | None:
    """Deserialize a keyring expiry value for ``load_oauth_session``."""

    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _required_id(value: Any) -> str:
    """Extract an item identifier or raise a normalized integration error."""

    item_id = getattr(value, "id", None)
    if item_id is None or not str(item_id):
        raise TidalClientError("provider_id_missing")
    return str(item_id)


def _text_value(value: Any, *attributes: str) -> str:
    """Read the first meaningful provider string attribute."""

    for attribute in attributes:
        current = getattr(value, attribute, None)
        if current is not None and str(current):
            return str(current)
    return ""


def _number_value(value: Any, attribute: str) -> int | None:
    """Read an optional integer provider attribute safely."""

    current = getattr(value, attribute, None)
    return int(current) if isinstance(current, (int, float)) else None


def _date_value(value: Any, *attributes: str) -> str | None:
    """Serialize the first available provider date without locale conversion."""

    for attribute in attributes:
        current = getattr(value, attribute, None)
        if hasattr(current, "isoformat"):
            return current.isoformat()
        if isinstance(current, str) and current:
            return current
    return None


def _artist_name(value: Any) -> str:
    """Extract a primary artist name from either compact or full objects."""

    artist = getattr(value, "artist", None)
    name = _text_value(artist, "name")
    if name:
        return name
    artists = getattr(value, "artists", None)
    if isinstance(artists, list):
        names = [_text_value(item, "name") for item in artists]
        return ", ".join(name for name in names if name)
    return ""


def _looks_unavailable(error: Exception) -> bool:
    """Classify only normalized provider availability failures, never raw details."""

    name = type(error).__name__.lower()
    return "notfound" in name or "unavailable" in name
