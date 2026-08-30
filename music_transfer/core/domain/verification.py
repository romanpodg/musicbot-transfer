"""Structured verification results.

Verification is deliberately separate from API acknowledgement (Invariant G):
a platform can answer ``200 OK`` and still not have the item, and it can return
an error after the write actually succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SequenceComparison:
    """Comparison of an expected sequence against the actual one."""

    expected_count: int = 0
    actual_count: int = 0
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    order_mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        """Return whether counts, membership, and order all agree."""

        return (
            self.expected_count == self.actual_count
            and not self.missing
            and not self.unexpected
            and not self.order_mismatches
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "order_mismatches": [dict(item) for item in self.order_mismatches],
            "matches": self.matches,
        }


@dataclass(slots=True)
class VerificationResult:
    """The structured outcome of verifying one destination container.

    Comparing only ``expected_count == actual_count`` can hide wrong content:
    a playlist with the right length can still hold the wrong tracks.  This
    result therefore reports membership *and* order separately.
    """

    success: bool = True
    expected_count: int = 0
    actual_count: int = 0
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    order_mismatches: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_comparison(
        cls, comparison: SequenceComparison, *, warnings: list[str] | None = None
    ) -> VerificationResult:
        """Build a result from a completed sequence comparison."""

        return cls(
            success=comparison.matches,
            expected_count=comparison.expected_count,
            actual_count=comparison.actual_count,
            missing=list(comparison.missing),
            unexpected=list(comparison.unexpected),
            order_mismatches=[dict(item) for item in comparison.order_mismatches],
            warnings=list(warnings or []),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values."""

        return {
            "success": self.success,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "order_mismatches": [dict(item) for item in self.order_mismatches],
            "warnings": list(self.warnings),
        }
