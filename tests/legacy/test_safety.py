"""Verify confirmation gates and deterministic local behavior."""

from __future__ import annotations

import ast
import logging
import tempfile
import unittest
from pathlib import Path

from core.cleanup import CleanupPlan, CleanupScope, CleanupService
from core.models import AccountProfile, LibrarySnapshot
from core.sorting import SortOrder, sort_items
from core.state import SecretRedactionFilter, TransferState, TransferStateStore
from localization.manager import LocalizationManager, _leaf_keys
from ui.progress import Console
from ui.prompts import Prompts

from core.transfer import ConfirmationRequired, TransferOptions, TransferService


class FakeDestination:
    """A no-network destination with a visible mutation counter."""

    def __init__(self) -> None:
        self.favorite_calls: list[tuple[str, str]] = []
        self.removal_calls: list[tuple[str, str]] = []

    def add_favorite(self, category: str, item_id: str) -> None:
        self.favorite_calls.append((category, item_id))

    def remove_favorite(self, category: str, item_id: str) -> None:
        self.removal_calls.append((category, item_id))


def snapshot() -> LibrarySnapshot:
    """Build a minimal, complete snapshot for confirmation-gate tests."""

    return LibrarySnapshot(
        account=AccountProfile("1", "test"),
        tracks=[{"id": "track-1", "title": "Track"}],
    )


class SafetyTests(unittest.TestCase):
    """Proof that dangerous service operations cannot bypass confirmation."""

    def test_transfer_rejects_unconfirmed_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_store = TransferStateStore(Path(directory) / "transfer_state.json")
            service = TransferService(state_store, logging.getLogger("test.transfer"))
            destination = FakeDestination()

            with self.assertRaises(ConfirmationRequired):
                service.run(
                    destination,
                    TransferState.create("transfer", snapshot()),
                    TransferOptions(),
                    confirmed=False,
                )

            self.assertEqual(destination.favorite_calls, [])
            self.assertFalse(state_store.exists())

    def test_cleanup_rejects_unconfirmed_mutation(self) -> None:
        service = CleanupService(logging.getLogger("test.cleanup"))
        destination = FakeDestination()
        plan = CleanupPlan(
            scope=CleanupScope.TRACKS,
            snapshot=snapshot(),
            counts={"tracks": 1},
        )

        with self.assertRaises(ConfirmationRequired):
            service.execute(destination, plan, confirmed=False)

        self.assertEqual(destination.removal_calls, [])

    def test_sorting_is_stable_and_handles_missing_metadata(self) -> None:
        items = [
            {"id": "a", "title": "Zulu", "artist": "B"},
            {"id": "b", "title": "alpha", "artist": "A"},
            {"id": "c", "title": ""},
        ]
        ordered = sort_items(items, SortOrder.ALPHABETICAL)
        self.assertEqual([item["id"] for item in ordered], ["b", "a", "c"])

    def test_catalogs_have_matching_keys(self) -> None:
        localization_dir = Path(__file__).resolve().parents[2] / "tidal_manager" / "localization"
        manager = LocalizationManager(localization_dir)
        manager.validate_catalogs()
        self.assertEqual(set(manager.available_languages()), {"en", "ru"})

    def test_confirmed_transfer_records_progress_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_store = TransferStateStore(Path(directory) / "transfer_state.json")
            service = TransferService(state_store, logging.getLogger("test.transfer"))
            destination = FakeDestination()

            report = service.run(
                destination,
                TransferState.create("transfer", snapshot()),
                TransferOptions(),
                confirmed=True,
            )

            self.assertEqual(destination.favorite_calls, [("tracks", "track-1")])
            self.assertEqual(len(report.successful_items), 1)
            self.assertTrue(state_store.exists())

    def test_log_filter_redacts_token_shaped_values(self) -> None:
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "access_token=%s Authorization: Bearer value",
            ("top-secret",),
            None,
        )
        SecretRedactionFilter().filter(record)
        self.assertNotIn("top-secret", record.getMessage())
        self.assertNotIn("Bearer value", record.getMessage())

    def test_console_messages_reference_localization_keys(self) -> None:
        project_root = Path(__file__).resolve().parents[2] / "tidal_manager"
        manager = LocalizationManager(project_root / "localization")
        message_keys = _leaf_keys(manager._catalogs["en"])
        sources = [project_root / "main.py", *(project_root / "ui").glob("*.py")]
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "message" or not node.args:
                    continue
                first_argument = node.args[0]
                if isinstance(first_argument, ast.Constant) and isinstance(first_argument.value, str):
                    self.assertIn(first_argument.value, message_keys, path)

    def test_deletion_requires_yes_no_and_exact_delete_phrase(self) -> None:
        localization_dir = Path(__file__).resolve().parents[2] / "tidal_manager" / "localization"
        console = Console(LocalizationManager(localization_dir), output=lambda _: None)
        accepted_answers = iter(["y", "DELETE"])
        accepted = Prompts(console, input_function=lambda _: next(accepted_answers))
        self.assertTrue(accepted.confirm_deletion("test", "1 selected library object"))

        rejected_answers = iter(["y", "delete"])
        rejected = Prompts(console, input_function=lambda _: next(rejected_answers))
        self.assertFalse(rejected.confirm_deletion("test", "1 selected library object"))
