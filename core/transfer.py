"""Resumable, confirmation-gated library transfer and restore service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .auth import ItemUnavailableError, TidalClientError, TidalLibraryClient
from .models import LibrarySnapshot, TransferReport
from .sorting import SortOrder, sort_items
from .state import TransferState, TransferStateStore


ProgressCallback = Callable[[str, int, int], None]


class ConfirmationRequired(PermissionError):
    """Raised when a remote library mutation was not explicitly confirmed."""


@dataclass(frozen=True, slots=True)
class TransferOptions:
    """User-selected transfer behavior that remains stable across resumption."""

    sort_order: SortOrder = SortOrder.ORIGINAL


class TransferService:
    """Transfer a snapshot while saving state after every confirmed mutation."""

    _favorite_categories = ("tracks", "albums", "artists", "videos", "mixes")

    def __init__(self, state_store: TransferStateStore, logger: logging.Logger) -> None:
        self._state_store = state_store
        self._logger = logger

    def run(
        self,
        destination: TidalLibraryClient,
        state: TransferState,
        options: TransferOptions,
        *,
        confirmed: bool,
        progress: ProgressCallback | None = None,
    ) -> TransferReport:
        """Execute or resume a transfer only after explicit authorization.

        The passed state contains the original source snapshot.  A resumed run
        therefore never needs credentials for the source account and cannot
        accidentally transfer newly changed source-library content.
        """

        if not confirmed:
            raise ConfirmationRequired("transfer_confirmation_required")
        self._state_store.save(state)
        self._logger.info(
            "event=transfer_started operation=%s tracks=%d",
            state.operation,
            state.source_snapshot.counts()["tracks"],
        )
        report = TransferReport(operation=state.operation)
        report.source_counts = state.source_snapshot.counts()
        try:
            self._transfer_favorites(destination, state, options, report, progress)
            self._transfer_folders(destination, state, report, progress)
            self._transfer_playlists(destination, state, report, progress)
        except KeyboardInterrupt:
            self._state_store.save(state)
            self._logger.warning("event=transfer_interrupted operation=%s", state.operation)
            raise
        report.finish()
        self._logger.info(
            "event=transfer_finished operation=%s successful=%d failed=%d unavailable=%d",
            state.operation,
            len(report.successful_items),
            len(report.failed_items),
            len(report.unavailable_items),
        )
        return report

    def _transfer_favorites(
        self,
        destination: TidalLibraryClient,
        state: TransferState,
        options: TransferOptions,
        report: TransferReport,
        progress: ProgressCallback | None,
    ) -> None:
        snapshot = state.source_snapshot
        for category in self._favorite_categories:
            items = sort_items(getattr(snapshot, category), options.sort_order)
            for index, item in enumerate(items, start=1):
                item_id = str(item.get("id", ""))
                if not item_id:
                    self._record_failure(report, state, category, "missing", TidalClientError())
                    continue
                if state.is_completed(category, item_id):
                    report.add("skipped", category, item_id, "already_completed")
                elif state.is_ambiguous(category, item_id):
                    self._record_failure(
                        report, state, category, item_id, TidalClientError("ambiguous_remote_outcome")
                    )
                else:
                    state.current_category = category
                    self._state_store.save(state)
                    try:
                        destination.add_favorite(category, item_id)
                        state.mark_completed(category, item_id)
                        self._state_store.save(state)
                        report.add("successful", category, item_id)
                        self._logger.info(
                            "event=transfer_item_completed category=%s item_id=%s",
                            category,
                            item_id,
                        )
                    except Exception as error:
                        self._record_failure(report, state, category, item_id, error)
                if progress:
                    progress(category, index, len(items))

    def _transfer_folders(
        self,
        destination: TidalLibraryClient,
        state: TransferState,
        report: TransferReport,
        progress: ProgressCallback | None,
    ) -> None:
        folders = state.source_snapshot.folders
        for index, folder in enumerate(folders, start=1):
            folder_id = str(folder.get("id", ""))
            if not folder_id:
                self._record_failure(report, state, "folders", "missing", TidalClientError())
                continue
            if state.is_completed("folders", folder_id):
                report.add("skipped", "folders", folder_id, "already_completed")
            elif state.is_ambiguous("folders", folder_id):
                self._record_failure(
                    report, state, "folders", folder_id, TidalClientError("ambiguous_remote_outcome")
                )
            else:
                parent_source_id = str(folder.get("parent_id") or "root")
                parent_destination_id = state.destination_folders.get(parent_source_id)
                if parent_source_id == "root":
                    parent_destination_id = "root"
                if not parent_destination_id:
                    self._record_failure(
                        report, state, "folders", folder_id, TidalClientError("folder_parent_missing")
                    )
                else:
                    state.current_category = "folders"
                    self._state_store.save(state)
                    try:
                        destination_id = destination.create_folder(
                            str(folder.get("name", "")), parent_destination_id
                        )
                        state.destination_folders[folder_id] = destination_id
                        state.mark_completed("folders", folder_id)
                        self._state_store.save(state)
                        report.add("successful", "folders", folder_id)
                        self._logger.info(
                            "event=transfer_item_completed category=folders item_id=%s", folder_id
                        )
                    except Exception as error:
                        if _unknown_creation_outcome(error):
                            state.mark_ambiguous("folders", folder_id)
                        self._record_failure(report, state, "folders", folder_id, error)
            if progress:
                progress("folders", index, len(folders))

    def _transfer_playlists(
        self,
        destination: TidalLibraryClient,
        state: TransferState,
        report: TransferReport,
        progress: ProgressCallback | None,
    ) -> None:
        playlists = state.source_snapshot.playlists
        for index, playlist in enumerate(playlists, start=1):
            playlist_id = str(playlist.get("id", ""))
            if not playlist_id:
                self._record_failure(report, state, "playlists", "missing", TidalClientError())
                continue
            if state.is_completed("playlists", playlist_id):
                report.add("skipped", "playlists", playlist_id, "already_completed")
            elif state.is_ambiguous("playlists", playlist_id):
                self._record_failure(
                    report, state, "playlists", playlist_id, TidalClientError("ambiguous_remote_outcome")
                )
            else:
                self._transfer_playlist(destination, state, report, playlist, playlist_id)
            if progress:
                progress("playlists", index, len(playlists))

    def _transfer_playlist(
        self,
        destination: TidalLibraryClient,
        state: TransferState,
        report: TransferReport,
        playlist: dict[str, Any],
        playlist_id: str,
    ) -> None:
        parent_source_id = str(playlist.get("folder_id") or "root")
        parent_destination_id = state.destination_folders.get(parent_source_id, "root")
        if parent_source_id != "root" and parent_source_id not in state.destination_folders:
            self._record_failure(
                report, state, "playlists", playlist_id, TidalClientError("playlist_folder_missing")
            )
            return
        created_playlist = False
        try:
            if not bool(playlist.get("is_owned", False)):
                state.current_category = "playlists"
                self._state_store.save(state)
                destination.favorite_playlist(playlist_id, parent_destination_id)
                state.mark_completed("playlists", playlist_id)
                self._state_store.save(state)
                report.add("successful", "playlists", playlist_id)
                self._logger.info(
                    "event=transfer_item_completed category=playlists item_id=%s", playlist_id
                )
                return
            destination_id = state.destination_playlists.get(playlist_id)
            if destination_id is None:
                state.current_category = "playlists"
                self._state_store.save(state)
                created_playlist = True
                destination_id = destination.create_playlist(
                    str(playlist.get("name", "")),
                    str(playlist.get("description", "")),
                    parent_destination_id,
                )
                state.destination_playlists[playlist_id] = destination_id
                state.current_playlist = playlist_id
                state.current_position = 0
                self._state_store.save(state)
            self._transfer_playlist_items(
                destination, state, report, playlist_id, destination_id, playlist
            )
            state.mark_completed("playlists", playlist_id)
            state.current_playlist = None
            state.current_position = 0
            self._state_store.save(state)
            report.add("successful", "playlists", playlist_id)
            self._logger.info(
                "event=transfer_item_completed category=playlists item_id=%s", playlist_id
            )
        except Exception as error:
            if created_playlist and _unknown_creation_outcome(error):
                state.mark_ambiguous("playlists", playlist_id)
            self._record_failure(report, state, "playlists", playlist_id, error)

    def _transfer_playlist_items(
        self,
        destination: TidalLibraryClient,
        state: TransferState,
        report: TransferReport,
        source_playlist_id: str,
        destination_playlist_id: str,
        playlist: dict[str, Any],
    ) -> None:
        items = _playlist_items(playlist)
        saved_position = (
            state.current_position if state.current_playlist == source_playlist_id else 0
        )
        position = self._reconcile_playlist_position(
            destination, destination_playlist_id, items, saved_position
        )
        state.current_playlist = source_playlist_id
        state.current_position = position
        self._state_store.save(state)
        for current_position in range(position, len(items)):
            item = items[current_position]
            media_id = item["id"]
            report_id = f"{source_playlist_id}:{current_position}"
            state.current_position = current_position
            self._state_store.save(state)
            try:
                destination.add_playlist_item(destination_playlist_id, media_id)
                state.current_position = current_position + 1
                self._state_store.save(state)
                report.add("successful", "playlist_items", report_id)
            except Exception as error:
                self._record_failure(report, state, "playlist_items", report_id, error)
                raise

    @staticmethod
    def _reconcile_playlist_position(
        destination: TidalLibraryClient,
        destination_playlist_id: str,
        expected_items: list[dict[str, str]],
        saved_position: int,
    ) -> int:
        """Reconcile a crash between remote item creation and local checkpointing."""

        actual_items = destination.playlist_media_order(destination_playlist_id)
        actual_count = len(actual_items)
        if actual_count > len(expected_items) or actual_items != expected_items[:actual_count]:
            raise TidalClientError("playlist_resume_mismatch")
        return max(saved_position, actual_count)

    def _record_failure(
        self,
        report: TransferReport,
        state: TransferState,
        category: str,
        item_id: str,
        error: Exception,
    ) -> None:
        """Log only an exception class and write a safe report outcome."""

        status = "unavailable" if isinstance(error, ItemUnavailableError) else "failed"
        reason = "item_unavailable" if status == "unavailable" else _error_reason(error)
        report.add(status, category, item_id, reason)
        attempts = getattr(error, "attempts", 0)
        state.mark_failed(category, item_id, max(0, int(attempts) - 1))
        self._state_store.save(state)
        self._logger.error(
            "event=transfer_item_failed category=%s item_id=%s reason=%s retry_count=%d",
            category,
            item_id,
            reason,
            max(0, int(attempts) - 1),
        )


def _error_reason(error: Exception) -> str:
    """Return a safe provider error code, never a raw exception message."""

    if isinstance(error, TidalClientError):
        return error.reason
    return "provider_error"


def _unknown_creation_outcome(error: Exception) -> bool:
    """Identify failures where replaying a create could duplicate an object."""

    return _error_reason(error) in {
        "api_timeout",
        "network_error",
        "api_server_error",
        "rate_limited",
    }


def _playlist_items(playlist: dict[str, Any]) -> list[dict[str, str]]:
    """Read the exact interleaved order, including backups created before v1."""

    raw_order = playlist.get("item_order")
    if isinstance(raw_order, list):
        items = [
            {"kind": str(item.get("kind", "track")), "id": str(item.get("id", ""))}
            for item in raw_order
            if isinstance(item, dict) and str(item.get("id", ""))
        ]
        if items:
            return items
    tracks = playlist.get("track_order", [])
    videos = playlist.get("video_order", [])
    return [
        {"kind": "track", "id": str(item_id)}
        for item_id in tracks
        if str(item_id)
    ] + [
        {"kind": "video", "id": str(item_id)}
        for item_id in videos
        if str(item_id)
    ]
