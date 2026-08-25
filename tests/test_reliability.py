"""Deterministic regression tests for retry, cleanup queue, and resume safety."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

import requests

from core.auth import TidalClientError
from core.cleanup import (
    CleanupManager,
    CleanupPlan,
    CleanupScope,
    DeleteQueueStore,
)
from core.models import AccountProfile, LibrarySnapshot
from core.retry import RetryExecutor, RetryPolicy, configure_tidal_session
from core.state import DeleteStateStore, TransferState, TransferStateStore, configure_logging
from core.transfer import TransferOptions, TransferService


def cleanup_snapshot(*identifiers: str) -> LibrarySnapshot:
    """Build a small complete track-only snapshot for local cleanup tests."""

    return LibrarySnapshot(
        account=AccountProfile("1", "test"),
        tracks=[
            {"id": identifier, "title": f"Track {identifier}", "artist": "Artist"}
            for identifier in identifiers
        ],
    )


class FakeCleanupClient:
    """A deterministic fake that can fail or interrupt one queued deletion."""

    def __init__(self, *, fail: set[str] | None = None, interrupt: str | None = None) -> None:
        self.fail = fail or set()
        self.interrupt = interrupt
        self.removed: list[tuple[str, str]] = []

    def remove_favorite(self, category: str, item_id: str) -> None:
        self.removed.append((category, item_id))
        if item_id == self.interrupt:
            raise KeyboardInterrupt
        if item_id in self.fail:
            raise TidalClientError("api_timeout", attempts=3)


class FailingDestination:
    """A transfer fake retaining the number of provider retries in state."""

    def add_favorite(self, category: str, item_id: str) -> None:
        raise TidalClientError("api_timeout", attempts=3)


class ReliabilityTests(unittest.TestCase):
    """Proof that long-running mutations cannot silently lose progress."""

    def _manager(self, root: Path) -> CleanupManager:
        return CleanupManager(
            logging.getLogger("test.cleanup.reliability"),
            DeleteStateStore(root / "delete_state.json"),
            DeleteQueueStore(root / "delete_queue.json"),
        )

    def test_cleanup_queue_records_failure_and_resumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self._manager(root)
            plan = CleanupPlan(
                CleanupScope.TRACKS,
                cleanup_snapshot("one", "two"),
                {"tracks": 2},
            )
            first_client = FakeCleanupClient(fail={"two"})

            first = manager.execute(first_client, plan, confirmed=True)

            self.assertEqual(first.completed, 1)
            self.assertEqual(len(first.failed), 1)
            state = DeleteStateStore(root / "delete_state.json").load()
            self.assertEqual((state.total, state.completed, state.failed, state.remaining), (2, 1, 1, 0))
            queue = DeleteQueueStore(root / "delete_queue.json").load()
            self.assertEqual([item["status"] for item in queue.items], ["completed", "failed"])

            second_client = FakeCleanupClient()
            second = manager.resume(second_client, confirmed=True)

            self.assertEqual(second.completed, 2)
            self.assertEqual(second.failed, [])
            self.assertEqual(second_client.removed, [("tracks", "two")])
            self.assertFalse((root / "delete_state.json").exists())
            self.assertFalse((root / "delete_queue.json").exists())

    def test_cleanup_interruption_preserves_processing_item_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self._manager(root)
            plan = CleanupPlan(
                CleanupScope.TRACKS,
                cleanup_snapshot("one", "two"),
                {"tracks": 2},
            )
            with self.assertRaises(KeyboardInterrupt):
                manager.execute(
                    FakeCleanupClient(interrupt="two"), plan, confirmed=True
                )

            state = DeleteStateStore(root / "delete_state.json").load()
            queue = DeleteQueueStore(root / "delete_queue.json").load()
            self.assertTrue(state.interrupted)
            self.assertEqual((state.completed, state.remaining), (1, 1))
            self.assertEqual(queue.items[1]["status"], "processing")

            resumed_client = FakeCleanupClient()
            manager.resume(resumed_client, confirmed=True)
            self.assertEqual(resumed_client.removed, [("tracks", "two")])
            self.assertFalse((root / "delete_state.json").exists())

    def test_cleanup_progress_reports_current_item_before_and_after_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            manager = self._manager(root)
            plan = CleanupPlan(CleanupScope.TRACKS, cleanup_snapshot("one"), {"tracks": 1})

            manager.execute(FakeCleanupClient(), plan, confirmed=True, progress=events.append)

            self.assertGreaterEqual(len(events), 2)
            self.assertEqual(events[0].label, "Artist - Track one")
            self.assertEqual((events[-1].current, events[-1].total, events[-1].failed), (1, 1, 0))

    def test_retry_uses_exponential_backoff_and_sanitized_events(self) -> None:
        sleeps: list[float] = []
        events = []
        attempts = 0

        def action() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise requests.Timeout("secret must not be exposed")
            return "ok"

        executor = RetryExecutor(
            logging.getLogger("test.retry"),
            RetryPolicy(max_attempts=3, initial_backoff_seconds=1, max_backoff_seconds=8, jitter_ratio=0),
            events.append,
            sleeps.append,
        )
        self.assertEqual(executor.call("test", action), "ok")
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual([event.reason for event in events], ["api_timeout", "api_timeout"])

    def test_tidal_session_timeout_is_installed_on_every_request(self) -> None:
        calls: list[dict[str, object]] = []

        class RequestSession:
            def request(self, *args: object, **kwargs: object) -> str:
                calls.append(kwargs)
                return "response"

        class Session:
            request_session = RequestSession()

        session = Session()
        configure_tidal_session(session, RetryPolicy(timeout_seconds=17))
        self.assertEqual(session.request_session.request("GET", "endpoint"), "response")
        self.assertEqual(calls, [{"timeout": 17}])

    def test_transfer_state_persists_failed_items_and_retry_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransferStateStore(Path(directory) / "transfer_state.json")
            snapshot = cleanup_snapshot("one")
            state = TransferState.create("transfer", snapshot)

            report = TransferService(store, logging.getLogger("test.transfer.reliability")).run(
                FailingDestination(), state, TransferOptions(), confirmed=True
            )

            self.assertEqual(report.failed_items[0]["reason"], "api_timeout")
            loaded = store.load()
            self.assertEqual(loaded.failed_items, {"tracks": ["one"]})
            self.assertEqual(loaded.retry_counts, {"tracks:one": 2})

    def test_central_log_is_created_and_scrubs_token_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "tidal_manager.log"
            logger = configure_logging(path)
            logger.info("Authorization: Bearer visible-token access_token=another-token")
            for handler in logger.handlers:
                handler.flush()

            content = path.read_text(encoding="utf-8")
            self.assertIn("event=logging_initialized", content)
            self.assertNotIn("visible-token", content)
            self.assertNotIn("another-token", content)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
