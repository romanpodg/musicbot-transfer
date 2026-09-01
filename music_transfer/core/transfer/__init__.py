"""The transfer engine: planning, execution, verification, and recovery.

The engine is platform-agnostic.  It reads capabilities from an adapter and
never branches on a platform name.
"""

from __future__ import annotations

from .executor import (
    CancellationToken,
    ExecutionOutcome,
    ExecutionResult,
    TransferExecutor,
    build_report,
    scrub_credentials,
    status_after_execution,
)
from .lifecycle import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    can_transition,
    is_terminal,
    resume_target,
    transition,
)
from .ordering import (
    apply_logical_order,
    restore_positions,
    sort_key_for_date_added,
    sort_key_for_text,
    to_write_order,
)
from .planner import (
    CONTENT_SPECS,
    ENGINE_TRANSFER_SPECS,
    PlannerResult,
    TransferContentSpec,
    TransferPlanner,
    require_transfer_content_spec,
    validate_transfer_content_support,
)
from .recovery import RecoveryService
from .verifier import TransferVerifier, aggregate_verification_status, compare_sequences

__all__ = [
    "CONTENT_SPECS",
    "ENGINE_TRANSFER_SPECS",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "CancellationToken",
    "ExecutionOutcome",
    "ExecutionResult",
    "PlannerResult",
    "RecoveryService",
    "TransferContentSpec",
    "TransferExecutor",
    "TransferPlanner",
    "TransferVerifier",
    "aggregate_verification_status",
    "apply_logical_order",
    "build_report",
    "can_transition",
    "compare_sequences",
    "is_terminal",
    "require_transfer_content_spec",
    "restore_positions",
    "resume_target",
    "scrub_credentials",
    "sort_key_for_date_added",
    "sort_key_for_text",
    "status_after_execution",
    "to_write_order",
    "transition",
    "validate_transfer_content_support",
]
