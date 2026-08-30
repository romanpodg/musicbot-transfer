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
from collections.abc import Sequence
from typing import Any

from ..domain import SequenceComparison, TransferItem, TransferJob, VerificationResult
from ..enums import EntityType, ItemStatus
from ..errors import UnsupportedCapabilityError
from ..ports import MusicPlatformAdapter

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
    for position, (expected_id, actual_id) in enumerate(zip(expected, actual)):
        if len(comparison.order_mismatches) >= MAX_REPORTED_ORDER_MISMATCHES:
            break
        if expected_id != actual_id:
            comparison.order_mismatches.append(
                {"position": position, "expected": expected_id, "actual": actual_id}
            )
    return comparison


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

        try:
            state = self._destination.get_destination_state(("tracks",))
        except UnsupportedCapabilityError as error:
            return VerificationResult(
                success=False,
                expected_count=len(expected_ids),
                warnings=[f"verification_unsupported:{getattr(error, 'capability', 'unknown')}"],
            )
        if not state.is_trustworthy("tracks"):
            return VerificationResult(
                success=False,
                expected_count=len(expected_ids),
                actual_count=len(state.track_ids),
                warnings=["destination_state_incomplete"],
            )
        comparison = compare_sequences(expected_ids, sorted(state.track_ids))
        # Order is irrelevant for a set-like library section.
        comparison.order_mismatches = []
        return VerificationResult.from_comparison(comparison)

    def verify_job(
        self, job: TransferJob, items: list[TransferItem]
    ) -> dict[str, VerificationResult]:
        """Verify every playlist in a job plus the set-like sections.

        Returns a mapping keyed by ``playlist:<container id>`` for playlists and
        ``tracks``/``albums``/``artists`` for set-like sections.
        """

        results: dict[str, VerificationResult] = {}
        playlists: dict[str, list[str]] = {}
        for item in items:
            if item.entity_type is not EntityType.PLAYLIST_ITEM:
                continue
            if item.status is not ItemStatus.TRANSFERRED or not item.destination_id:
                continue
            container = item.container_destination_id or ""
            bucket = playlists.setdefault(container, [])
            bucket.append(item.destination_id)
        for container_id, expected in playlists.items():
            if not container_id:
                continue
            results[f"playlist:{container_id}"] = self.verify_playlist(container_id, expected)
        for entity_type, key in (
            (EntityType.TRACK, "tracks"),
            (EntityType.ALBUM, "albums"),
            (EntityType.ARTIST, "artists"),
        ):
            expected = [
                item.destination_id
                for item in items
                if item.entity_type is entity_type
                and item.status is ItemStatus.TRANSFERRED
                and item.destination_id
            ]
            if not expected:
                continue
            if entity_type is EntityType.TRACK:
                results[key] = self.verify_liked_tracks(expected)
            else:
                results[key] = self._verify_identifier_set(key, expected)
        self._logger.info(
            "event=verification_completed job_id=%s containers=%d failures=%d",
            job.id,
            len(results),
            sum(1 for result in results.values() if not result.success),
        )
        return results

    def _verify_identifier_set(self, section: str, expected: Sequence[str]) -> VerificationResult:
        """Verify membership for a non-track set-like section."""

        try:
            state = self._destination.get_destination_state((section,))
        except UnsupportedCapabilityError as error:
            return VerificationResult(
                success=False,
                expected_count=len(expected),
                warnings=[f"verification_unsupported:{getattr(error, 'capability', 'unknown')}"],
            )
        actual = (
            sorted(state.album_ids) if section == "albums" else sorted(state.artist_ids)
        )
        comparison = compare_sequences(expected, actual)
        comparison.order_mismatches = []
        return VerificationResult.from_comparison(comparison)

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
    def as_report(results: dict[str, VerificationResult]) -> dict[str, Any]:
        """Serialize verification results for a JSON report."""

        return {key: value.as_dict() for key, value in results.items()}
