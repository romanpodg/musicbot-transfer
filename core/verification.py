"""Post-operation comparison and JSON report generation."""

from __future__ import annotations

import logging
from pathlib import Path

from .auth import ProgressCallback, TidalLibraryClient
from .models import LibrarySnapshot, TransferReport
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
        progress: ProgressCallback | None = None,
    ) -> TransferReport:
        """Capture destination counts, add count deltas, and write a report."""

        self._logger.info("event=verification_started")
        destination_snapshot = destination.export_library(progress)
        report.source_counts = source.counts()
        report.destination_counts = destination_snapshot.counts()
        payload = report.as_dict()
        payload["verification"] = {
            "source_incomplete_sections": source.incomplete_sections,
            "destination_incomplete_sections": destination_snapshot.incomplete_sections,
            "count_deltas": {
                category: report.destination_counts.get(category, 0)
                - report.source_counts.get(category, 0)
                for category in report.source_counts
            },
        }
        atomic_write_json(self.report_path, payload)
        self._logger.info("event=verification_completed path=%s", self.report_path.name)
        return report
