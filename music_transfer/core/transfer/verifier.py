"""Post-execution verification.

Verification is separate from API acknowledgement (Invariant G): a platform can
return success and still not have the item, and it can error after a write
actually landed.  Verification therefore re-reads the destination.

Counting is not enough.  A playlist with the correct length can still hold the
wrong tracks, so sequence comparison reports membership *and* order separately.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Collection, Sequence
from typing import Any

from ..domain import SequenceComparison, TransferItem, TransferJob, VerificationResult
from ..enums import EntityType, ItemStatus, VerificationStatus
from ..errors import UnsupportedCapabilityError
from ..ports import MusicPlatformAdapter, destination_section_for_entity

_LOGGER = logging.getLogger("music_transfer.verifier")

#: Upper bound on reported order mismatches, so a large library cannot flood a log.
MAX_REPORTED_ORDER_MISMATCHES = 50


def compare_sequences(
    expected: Sequence[str], actual: Sequence[str]
) -> SequenceComparison:
    """Compare an expected id sequence against the actual one.

    Duplicates are compared as multisets, so a playlist that legitimately
    contains a track twice is verified correctly.
    """

    expected_counter = Counter(expected)
    actual_counter = Counter(actual)
    comparison = SequenceComparison(
        expected_count=len(expected),
        actual_count=len(actual),
        missing=sorted((expected_counter - actual_counter).elements()),
        unexpected=sorted((actual_counter - expected_counter).elements()),
    )
    for position, (expected_id, actual_id) in enumerate(zip(expected, actual, strict=False)):
        if len(comparison.order_mismatches) >= MAX_REPORTED_ORDER_MISMATCHES:
            break
        if expected_id != actual_id:
            comparison.order_mismatches.append(
                {"position": position, "expected": expected_id, "actual": actual_id}
            )
    return comparison


def compare_expected_membership(
    expected: Sequence[str],
    actual: Collection[str],
    *,
    warnings: list[str] | None = None,
) -> VerificationResult:
    """Verify that every expected identifier is present in actual collection (expected ⊆ actual).

    Set-like sections (liked tracks, albums, artists, videos, mixes) are scoped to the
    job's expected IDs. Unrelated items in the destination library section are neither
    unexpected nor verification failures.
    """

    expected_set = set(expected)
    actual_set = set(actual)

    missing = expected_set - actual_set
    observed_expected = expected_set & actual_set

    return VerificationResult(
        success=not missing,
        expected_count=len(expected_set),
        actual_count=len(observed_expected),
        missing=sorted(missing),
        unexpected=[],
        order_mismatches=[],
        warnings=list(warnings or []),
    )


class TransferVerifier:
    """Verify that planned destination content now actually exists."""

    def __init__(
        self, destination: MusicPlatformAdapter, logger: logging.Logger | None = None
    ) -> None:
        self._destination = destination
        self._logger = logger or _LOGGER

    def verify_playlist(
        self, container_destination_id: str, expected_ids: Sequence[str]
    ) -> VerificationResult:
        """Verify one playlist's exact membership and order.

        Returns a result carrying ``warnings`` when the destination cannot be
        read, rather than pretending verification succeeded.
        """

        try:
            actual = list(self._destination.playlist_item_ids(container_destination_id))
        except UnsupportedCapabilityError as error:
            return VerificationResult(
                success=False,
                expected_count=len(expected_ids),
                warnings=[f"verification_unsupported:{getattr(error, 'capability', 'unknown')}"],
            )
        return VerificationResult.from_comparison(compare_sequences(expected_ids, actual))

    def verify_liked_tracks(self, expected_ids: Sequence[str]) -> VerificationResult:
        """Verify that expected liked tracks are present at the destination.

        Liked tracks are set-like, so only membership is checked; order is not
        part of the contract for favourites.
        """

        return self._verify_identifier_set("tracks", expected_ids)

    def verify_job(
        self, job: TransferJob, items: list[TransferItem]
    ) -> dict[str, VerificationResult]:
        """Verify every playlist in a job plus the set-like sections.

        Returns a mapping keyed by ``playlist:<container id>`` for playlists and
        ``tracks``/``albums``/``artists``/``videos``/``mixes`` for set-like sections.
        """

        results: dict[str, VerificationResult] = {}
        playlists: dict[str, list[str]] = {}
        playlist_items = [
            item
            for item in items
            if item.entity_type is EntityType.PLAYLIST_ITEM
            and item.status is ItemStatus.TRANSFERRED
            and item.destination_id
        ]
        # Sort by write_position (or original_position if write_position is None)
        playlist_items.sort(
            key=lambda item: (
                item.write_position is None,
                item.write_position if item.write_position is not None else item.original_position,
            )
        )
        for item in playlist_items:
            container = item.container_destination_id or ""
            bucket = playlists.setdefault(container, [])
            bucket.append(item.destination_id)
        for container_id, expected in playlists.items():
            if not container_id:
                continue
            results[f"playlist:{container_id}"] = self.verify_playlist(container_id, expected)

        # Verify folder hierarchy and playlist placement if any folders or playlists were transferred
        transferred_folders = [
            item
            for item in items
            if item.entity_type is EntityType.FOLDER
            and item.status is ItemStatus.TRANSFERRED
            and item.destination_id
        ]
        transferred_playlists = [
            item
            for item in items
            if item.entity_type is EntityType.PLAYLIST
            and item.status is ItemStatus.TRANSFERRED
            and item.destination_id
        ]
        if transferred_folders or transferred_playlists:
            try:
                dest_snap = self._destination.export_library(sections=("folders", "playlists"))
                if "folders" in dest_snap.incomplete_sections:
                    results["incomplete:folders"] = VerificationResult(
                        success=False,
                        warnings=["verification_section_incomplete:folders"],
                    )
                if "playlists" in dest_snap.incomplete_sections:
                    results["incomplete:playlists"] = VerificationResult(
                        success=False,
                        warnings=["verification_section_incomplete:playlists"],
                    )

                from .planner import folder_parent_source_id

                dest_folders_by_id = {f.source_id: f for f in dest_snap.folders}
                folder_items_by_src = {
                    it.source_id: it for it in items if it.entity_type is EntityType.FOLDER
                }

                for f_item in transferred_folders:
                    fid = str(f_item.destination_id)
                    if fid not in dest_folders_by_id:
                        results[f"folder:{fid}"] = VerificationResult(
                            success=False,
                            missing=[fid],
                            warnings=["folder_missing"],
                        )
                        continue

                    dest_folder = dest_folders_by_id[fid]
                    expected_title = (
                        f_item.source_metadata.get("name")
                        or f_item.source_metadata.get("title")
                        or f_item.source_id
                    )
                    parent_src = f_item.container_source_id
                    if parent_src is None:
                        expected_parent_dest = None
                    else:
                        parent_it = folder_items_by_src.get(parent_src)
                        expected_parent_dest = parent_it.destination_id if parent_it else None

                    actual_parent_dest = folder_parent_source_id(dest_folder)
                    norm_expected_parent = (
                        None
                        if expected_parent_dest in (None, "root", "")
                        else str(expected_parent_dest)
                    )
                    norm_actual_parent = (
                        None
                        if actual_parent_dest in (None, "root", "")
                        else str(actual_parent_dest)
                    )

                    if dest_folder.title != expected_title:
                        results[f"folder:{fid}"] = VerificationResult(
                            success=False,
                            missing=["title_mismatch"],
                            warnings=[f"folder_title_mismatch:{dest_folder.title}!={expected_title}"],
                        )
                    elif norm_actual_parent != norm_expected_parent:
                        results[f"folder:{fid}"] = VerificationResult(
                            success=False,
                            missing=["parent_mismatch"],
                            warnings=[f"folder_parent_mismatch:{norm_actual_parent}!={norm_expected_parent}"],
                        )
                    else:
                        results[f"folder:{fid}"] = VerificationResult(
                            success=True,
                            expected_count=1,
                            actual_count=1,
                        )

                dest_playlists_by_id = {p.source_id: p for p in dest_snap.playlists}
                for p_item in transferred_playlists:
                    pid = str(p_item.destination_id)
                    if pid not in dest_playlists_by_id:
                        results[f"playlist_placement:{pid}"] = VerificationResult(
                            success=False,
                            missing=[pid],
                            warnings=["playlist_missing"],
                        )
                        continue

                    dest_pl = dest_playlists_by_id[pid]
                    expected_folder_dest = p_item.container_destination_id
                    norm_expected_folder = (
                        None
                        if expected_folder_dest in (None, "root", "")
                        else str(expected_folder_dest)
                    )
                    actual_folder_dest = dest_pl.folder_id
                    norm_actual_folder = (
                        None
                        if actual_folder_dest in (None, "root", "")
                        else str(actual_folder_dest)
                    )

                    if norm_actual_folder != norm_expected_folder:
                        results[f"playlist_placement:{pid}"] = VerificationResult(
                            success=False,
                            missing=["playlist_folder_mismatch"],
                            warnings=["playlist_folder_mismatch"],
                        )
                    else:
                        results[f"playlist_placement:{pid}"] = VerificationResult(
                            success=True,
                            expected_count=1,
                            actual_count=1,
                        )
            except Exception as error:  # noqa: BLE001
                self._logger.warning("event=folder_verification_unsupported error=%s", error)
                results["folders"] = VerificationResult(
                    success=False,
                    warnings=[f"verification_unsupported:{error}"],
                )
        for entity_type in (
            EntityType.TRACK,
            EntityType.ALBUM,
            EntityType.ARTIST,
            EntityType.VIDEO,
            EntityType.MIX,
        ):
            key = destination_section_for_entity(entity_type)
            expected = [
                item.destination_id
                for item in items
                if item.entity_type is entity_type
                and item.status in (ItemStatus.TRANSFERRED, ItemStatus.ALREADY_EXISTS)
                and item.destination_id
            ]
            if not expected:
                continue
            results[key] = self._verify_identifier_set(key, expected)
        self._logger.info(
            "event=verification_completed job_id=%s containers=%d failures=%d",
            job.id,
            len(results),
            sum(1 for result in results.values() if not result.success),
        )
        return results

    def _verify_identifier_set(self, section: str, expected: Sequence[str]) -> VerificationResult:
        """Verify membership for a set-like section."""

        expected_set = set(expected)

        try:
            state = self._destination.get_destination_state((section,))
        except UnsupportedCapabilityError as error:
            return VerificationResult(
                success=False,
                expected_count=len(expected_set),
                warnings=[f"verification_unsupported:{getattr(error, 'capability', 'unknown')}"],
            )
        ids = state.identifiers_for_section(section)
        if not state.is_trustworthy(section):
            observed = expected_set & set(ids)
            return VerificationResult(
                success=False,
                expected_count=len(expected_set),
                actual_count=len(observed),
                warnings=["destination_state_incomplete"],
            )
        return compare_expected_membership(expected, ids)

    @staticmethod
    def aggregate(results: dict[str, VerificationResult]) -> VerificationResult:
        """Combine several container results into one job-level result."""

        combined = VerificationResult()
        for result in results.values():
            combined.expected_count += result.expected_count
            combined.actual_count += result.actual_count
            combined.missing.extend(result.missing)
            combined.unexpected.extend(result.unexpected)
            combined.order_mismatches.extend(result.order_mismatches)
            combined.warnings.extend(result.warnings)
            combined.success = combined.success and result.success
        return combined

    @staticmethod
    def aggregate_status(
        results: dict[str, VerificationResult],
        *,
        verification_attempted: bool = True,
    ) -> VerificationStatus:
        """Determine aggregate verification status across all verified containers."""

        return aggregate_verification_status(
            results, verification_attempted=verification_attempted
        )

    @staticmethod
    def as_report(results: dict[str, VerificationResult]) -> dict[str, Any]:
        """Serialize verification results for a JSON report."""

        return {key: value.as_dict() for key, value in results.items()}


def aggregate_verification_status(
    results: dict[str, VerificationResult],
    *,
    verification_attempted: bool = True,
) -> VerificationStatus:
    """Determine the aggregate verification status across all verified containers.

    Aggregation rules:
    - If results are empty:
      - If verification was NOT attempted (dry run, early fatal abort): NOT_RUN.
      - If verification WAS attempted but nothing was verifiable: PARTIAL.
    - FAILED: At least one section has a confirmed discrepancy (missing items,
      unexpected items, or order mismatches).
    - PARTIAL: No confirmed discrepancies, but one or more sections could not
      be verified (unsupported capability or incomplete destination state).
    - PASSED: All sections completed verification successfully with no discrepancies
      and no incomplete/unsupported warnings.
    """

    if not results:
        return VerificationStatus.PARTIAL if verification_attempted else VerificationStatus.NOT_RUN

    has_mismatch = False
    has_partial = False

    for result in results.values():
        if result.missing or result.unexpected or result.order_mismatches:
            has_mismatch = True
            break
        if not result.success or result.warnings:
            has_partial = True

    if has_mismatch:
        return VerificationStatus.FAILED
    if has_partial:
        return VerificationStatus.PARTIAL
    return VerificationStatus.PASSED
