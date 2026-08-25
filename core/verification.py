"""Post-operation comparison and JSON report generation."""

from __future__ import annotations

import logging
from pathlib import Path

from .auth import ProgressCallback, TidalLibraryClient
from .models import LibrarySnapshot, TransferReport
from .state import TransferState
from .state import atomic_write_json


class VerificationService:
    """Compare a source snapshot with a live destination library."""

    def __init__(self, report_path: Path, logger: logging.Logger | None = None) -> None:
        self.report_path = report_path
        self._logger = logger or logging.getLogger("tidal_manager")

    def verify_and_write(
        self,
        source: LibrarySnapshot,
        destination: TidalLibraryClient,
        report: TransferReport,
        state: TransferState | None = None,
        progress: ProgressCallback | None = None,
    ) -> TransferReport:
        """Capture destination counts, add count deltas, and write a report."""

        self._logger.info("event=verification_started")
        destination_snapshot = destination.export_library(progress)
        report.source_counts = source.counts()
        report.destination_counts = destination_snapshot.counts()
        payload = report.as_dict()
        verification = {
            "source_incomplete_sections": source.incomplete_sections,
            "destination_incomplete_sections": destination_snapshot.incomplete_sections,
            "count_deltas": {
                category: report.destination_counts.get(category, 0)
                - report.source_counts.get(category, 0)
                for category in report.source_counts
            },
        }
        verification["favorites"] = _verify_favorites(source, destination_snapshot, report)
        if state is not None:
            verification["playlists"] = _verify_playlists(source, destination_snapshot, state)
            verification["folders"] = _verify_folders(source, destination_snapshot, state)
        payload["verification"] = verification
        atomic_write_json(self.report_path, payload)
        self._logger.info("event=verification_completed path=%s", self.report_path.name)
        return report


def _verify_favorites(
    source: LibrarySnapshot, destination: LibrarySnapshot, report: TransferReport
) -> dict[str, dict[str, int]]:
    """Verify intended favourite IDs while tolerating pre-existing extras."""

    outcome_by_category: dict[str, set[str]] = {"unavailable": set(), "failed": set()}
    for status, events in (("unavailable", report.unavailable_items), ("failed", report.failed_items), ("failed", report.permanent_failure_items)):
        for event in events:
            outcome_by_category[status].add(f"{event['category']}:{event['id']}")
    result: dict[str, dict[str, int]] = {}
    for category in ("tracks", "albums", "artists", "videos", "mixes"):
        expected = {str(item.get("id", "")) for item in getattr(source, category)} - {""}
        actual = {str(item.get("id", "")) for item in getattr(destination, category)} - {""}
        unavailable = {item_id for item_id in expected if f"{category}:{item_id}" in outcome_by_category["unavailable"]}
        failed = {item_id for item_id in expected if f"{category}:{item_id}" in outcome_by_category["failed"]}
        intended = expected - unavailable - failed
        result[category] = {
            "expected_transferred": len(intended), "verified_present": len(intended & actual),
            "missing": len(intended - actual), "unavailable": len(unavailable),
            "failed": len(failed), "preexisting_or_extra": len(actual - expected),
        }
    return result


def _verify_playlists(
    source: LibrarySnapshot, destination: LibrarySnapshot, state: TransferState
) -> dict[str, object]:
    """Compare mapped playlists by exact ordered `(kind, id)` sequences."""

    destination_by_id = {str(item.get("id", "")): item for item in destination.playlists}
    verified = mismatched = item_verified = item_missing = order_mismatches = 0
    details: list[dict[str, object]] = []
    for playlist in source.playlists:
        source_id = str(playlist.get("id", ""))
        destination_id = state.destination_playlists.get(source_id)
        if not destination_id:
            continue
        expected = _item_order(playlist)
        actual_playlist = destination_by_id.get(destination_id)
        actual = _item_order(actual_playlist or {})
        if actual_playlist is None:
            mismatched += 1
            details.append({"source_id": source_id, "destination_id": destination_id, "reason": "missing_playlist"})
        elif actual == expected:
            verified += 1
            item_verified += len(expected)
        else:
            mismatched += 1
            item_missing += max(0, len(expected) - len(actual))
            if len(actual) == len(expected):
                order_mismatches += 1
            details.append({"source_id": source_id, "destination_id": destination_id, "reason": "sequence_mismatch", "expected_count": len(expected), "actual_count": len(actual)})
    return {"verified": verified, "mismatched": mismatched, "items_verified": item_verified, "items_missing": item_missing, "order_mismatches": order_mismatches, "details": details}


def _verify_folders(source: LibrarySnapshot, destination: LibrarySnapshot, state: TransferState) -> dict[str, int]:
    destination_by_id = {str(item.get("id", "")): item for item in destination.folders}
    verified = mismatched = 0
    for folder in source.folders:
        source_id = str(folder.get("id", ""))
        destination_id = state.destination_folders.get(source_id)
        if not destination_id:
            continue
        actual = destination_by_id.get(destination_id)
        expected_parent = str(folder.get("parent_id") or "root")
        mapped_parent = state.destination_folders.get(expected_parent, "root")
        if actual and str(actual.get("parent_id") or "root") == mapped_parent:
            verified += 1
        else:
            mismatched += 1
    return {"verified": verified, "mismatched": mismatched}


def _item_order(playlist: dict[str, object]) -> list[tuple[str, str]]:
    raw = playlist.get("item_order", [])
    if not isinstance(raw, list):
        return []
    return [(str(item.get("kind", "track")), str(item.get("id", ""))) for item in raw if isinstance(item, dict)]
