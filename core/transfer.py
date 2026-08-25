"""Resumable, confirmation-gated library transfer and restore service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .auth import ItemUnavailableError, TidalClientError, TidalLibraryClient, UnsupportedOperationError
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
        # Restore is intentionally exempt: restoring a backup onto the account
        # which made it is a legitimate recovery workflow.
        if state.operation == "transfer":
            profile = getattr(destination, "profile", None)
            destination_profile = profile() if callable(profile) else None
            if (
                destination_profile is not None
                and destination_profile.account_id == state.source_snapshot.account.account_id
            ):
                raise TidalClientError("same_source_destination_account")
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
            existing_ids = _existing_favorite_ids(destination, category)
            for index, item in enumerate(items, start=1):
                item_id = str(item.get("id", ""))
                if not item_id:
                    self._record_failure(report, state, category, "missing", TidalClientError())
                    continue
                persisted_status = state.status_of(category, item_id)
                if persisted_status in {"unavailable", "unsupported", "failed_permanent"}:
                    report.add(persisted_status if persisted_status != "failed_permanent" else "failed_permanent", category, item_id, "previous_terminal_outcome")
                elif persisted_status in {"already_present", "completed"}:
                    report.add("skipped", category, item_id, persisted_status)
                elif item_id in existing_ids:
                    state.mark_terminal(category, item_id, "already_present")
                    report.add("skipped", category, item_id, "already_present")
                elif state.is_completed(category, item_id):
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
            elif state.is_ambiguous("folders", folder_id) or state.status_of("folders", folder_id) == "creating":
                if self._recover_ambiguous_folder(destination, state, folder):
                    report.add("successful", "folders", folder_id, "reconciled_created")
                else:
                    report.add("ambiguous", "folders", folder_id, "creation_conflict")
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
                    name = str(folder.get("name", ""))
                    state.mark_create_intent("folders", folder_id, {
                        "name": name, "parent_id": parent_destination_id,
                        "baseline_ids": sorted(destination.creation_candidate_ids("folders", name, parent_destination_id)),
                    })
                    self._state_store.save(state)
                    try:
                        destination_id = destination.create_folder(
                            name, parent_destination_id
                        )
                        state.destination_folders[folder_id] = destination_id
                        state.mark_created("folders", folder_id, destination_id)
                        state.mark_completed("folders", folder_id)
                        self._state_store.save(state)
                        report.add("successful", "folders", folder_id)
                        self._logger.info(
                            "event=transfer_item_completed category=folders item_id=%s", folder_id
                        )
                    except Exception as error:
                        if _unknown_creation_outcome(error):
                            state.mark_ambiguous("folders", folder_id)
                            self._state_store.save(state)
                            report.add("ambiguous", "folders", folder_id, "ambiguous_remote_outcome")
                        else:
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
            elif state.is_ambiguous("playlists", playlist_id) or state.status_of("playlists", playlist_id) == "creating":
                if self._recover_ambiguous_playlist(destination, state, playlist):
                    self._transfer_playlist(destination, state, report, playlist, playlist_id)
                else:
                    report.add("ambiguous", "playlists", playlist_id, "creation_conflict")
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
                # Followed playlists retain their TIDAL UUID; persist that
                # identity so verification does not silently skip them.
                state.destination_playlists[playlist_id] = playlist_id
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
                created_playlist = True
                name = str(playlist.get("name", ""))
                description = str(playlist.get("description", ""))
                state.mark_create_intent("playlists", playlist_id, {
                    "name": name, "description": description, "parent_id": parent_destination_id,
                    "baseline_ids": sorted(destination.creation_candidate_ids("playlists", name, parent_destination_id, description)),
                })
                self._state_store.save(state)
                destination_id = destination.create_playlist(
                    name, description,
                    parent_destination_id,
                )
                state.destination_playlists[playlist_id] = destination_id
                state.mark_created("playlists", playlist_id, destination_id)
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
            if state.is_ambiguous("playlists", playlist_id):
                self._state_store.save(state)
                report.add("ambiguous", "playlists", playlist_id, "ambiguous_remote_outcome")
            else:
                self._record_failure(report, state, "playlists", playlist_id, error)

    def _recover_ambiguous_folder(
        self, destination: TidalLibraryClient, state: TransferState, folder: dict[str, Any]
    ) -> bool:
        parent = str(folder.get("parent_id") or "root")
        parent = state.destination_folders.get(parent, parent)
        candidates = destination.creation_candidates("folders", str(folder.get("name", "")), parent)
        intent = state.create_intents.get(f"folders:{folder.get('id')}", {})
        baseline = {str(value) for value in intent.get("baseline_ids", [])}
        candidates = [candidate for candidate in candidates if str(candidate.get("id")) not in baseline]
        if len(candidates) != 1:
            self._logger.error("event=folder_creation_conflict source_id=%s candidates=%d", folder.get("id"), len(candidates))
            return False
        source_id = str(folder["id"])
        state.destination_folders[source_id] = str(candidates[0]["id"])
        state.mark_completed("folders", source_id)
        self._state_store.save(state)
        return True

    def _recover_ambiguous_playlist(
        self, destination: TidalLibraryClient, state: TransferState, playlist: dict[str, Any]
    ) -> bool:
        source_id = str(playlist["id"])
        parent = str(playlist.get("folder_id") or "root")
        parent = state.destination_folders.get(parent, parent)
        candidates = destination.creation_candidates("playlists", str(playlist.get("name", "")), parent, str(playlist.get("description", "")))
        intent = state.create_intents.get(f"playlists:{source_id}", {})
        baseline = {str(value) for value in intent.get("baseline_ids", [])}
        candidates = [candidate for candidate in candidates if str(candidate.get("id")) not in baseline]
        if len(candidates) != 1:
            self._logger.error("event=playlist_creation_conflict source_id=%s candidates=%d", source_id, len(candidates))
            return False
        state.destination_playlists[source_id] = str(candidates[0]["id"])
        state.mark_pending("playlists", source_id)
        self._state_store.save(state)
        return True

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
            destination, destination_playlist_id, source_playlist_id, items, saved_position, state
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
                destination.add_playlist_item(destination_playlist_id, item["kind"], media_id)
                state.current_position = current_position + 1
                self._state_store.save(state)
                report.add("successful", "playlist_items", report_id)
            except UnsupportedOperationError as error:
                # Video writes are not implemented by tidalapi 0.8.x.  This is
                # terminal, but must not prevent subsequent track items from
                # retaining their order.
                state.mark_terminal("playlist_items", report_id, "unsupported")
                state.current_position = current_position + 1
                self._state_store.save(state)
                report.add("unsupported", "playlist_items", report_id, error.reason)
                self._logger.warning(
                    "event=playlist_item_unsupported playlist_id=%s position=%d total=%d kind=%s item_id=%s retry=false",
                    source_playlist_id, current_position + 1, len(items), item["kind"], media_id,
                )
            except ItemUnavailableError as error:
                state.mark_terminal("playlist_items", report_id, "unavailable")
                state.current_position = current_position + 1
                self._state_store.save(state)
                report.add("unavailable", "playlist_items", report_id, error.reason)
                self._logger.warning(
                    "event=playlist_item_unavailable playlist_id=%s position=%d total=%d kind=%s item_id=%s retry=false",
                    source_playlist_id, current_position + 1, len(items), item["kind"], media_id,
                )
            except Exception as error:
                self._record_failure(report, state, "playlist_items", report_id, error)
                raise

    @staticmethod
    def _reconcile_playlist_position(
        destination: TidalLibraryClient,
        destination_playlist_id: str,
        source_playlist_id: str,
        expected_items: list[dict[str, str]],
        saved_position: int,
        state: TransferState | None = None,
    ) -> int:
        """Reconcile remote indices to source positions, including skipped media."""

        actual_items = destination.playlist_media_order(destination_playlist_id)
        remote_position = 0
        source_position = 0
        terminal = {"unsupported", "unavailable", "failed_permanent"}
        while source_position < len(expected_items):
            report_id = f"{source_playlist_id}:{source_position}"
            status = state.status_of("playlist_items", report_id) if state else None
            if status in terminal:
                source_position += 1
                continue
            if remote_position == len(actual_items):
                # Advance over any terminal source positions after the last
                # remote item; the returned value is always a source index.
                while source_position < len(expected_items):
                    next_status = state.status_of("playlist_items", f"{source_playlist_id}:{source_position}") if state else None
                    if next_status not in terminal:
                        break
                    source_position += 1
                return source_position
            if actual_items[remote_position] != expected_items[source_position]:
                raise TidalClientError("playlist_resume_mismatch")
            # A remote item proves completion even if the last local checkpoint
            # was lost after the mutation response.
            if state and status != "completed":
                state.mark_completed("playlist_items", report_id)
            remote_position += 1
            source_position += 1
        if remote_position != len(actual_items):
            raise TidalClientError("playlist_resume_destination_longer")
        return source_position

    def _record_failure(
        self,
        report: TransferReport,
        state: TransferState,
        category: str,
        item_id: str,
        error: Exception,
    ) -> None:
        """Log only an exception class and write a safe report outcome."""

        if isinstance(error, ItemUnavailableError):
            status, state_status, reason = "unavailable", "unavailable", "item_unavailable"
        else:
            reason = _error_reason(error)
            retryable = getattr(error, "retryable", reason in {"api_timeout", "network_error", "api_server_error", "rate_limited"})
            status = "failed" if retryable else "failed_permanent"
            state_status = "failed_retryable" if retryable else "failed_permanent"
        report.add(status, category, item_id, reason)
        attempts = getattr(error, "attempts", 0)
        if state_status == "failed_retryable":
            state.mark_failed(category, item_id, max(0, int(attempts) - 1))
        else:
            state.mark_terminal(category, item_id, state_status)
        self._state_store.save(state)
        self._logger.error(
            "event=transfer_item_failed category=%s item_id=%s reason=%s retry_count=%d",
            category,
            item_id,
            reason,
            max(0, int(attempts) - 1),
        )


def _existing_favorite_ids(destination: TidalLibraryClient, category: str) -> set[str]:
    """Use a fresh destination view when available; keep test doubles simple."""

    getter = getattr(destination, "favorite_ids", None)
    if not callable(getter):
        return set()
    return {str(item_id) for item_id in getter(category)}


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
