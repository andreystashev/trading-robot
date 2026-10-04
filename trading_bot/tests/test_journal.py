import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from journal import record, latest, database


class JournalTests(unittest.TestCase):
    def test_persistence_order_and_plan_link(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.sqlite3"
            record(
                "PLAN",
                {"action": "BUY", "estimated_commission": 1},
                path=path,
                plan_id="plan",
            )
            record(
                "ORDER_PREPARED",
                {"lots": 1},
                path=path,
                plan_id="plan",
                request_id="request",
            )
            record(
                "ORDER_STATUS",
                {"commission": 0.8, "filled_lots": 1},
                path=path,
                plan_id="plan",
                request_id="request",
            )
            rows = latest(path=path)
            self.assertEqual(
                [r["kind"] for r in rows], ["ORDER_STATUS", "ORDER_PREPARED", "PLAN"]
            )
            self.assertTrue(all(r["plan_id"] == "plan" for r in rows))
            self.assertEqual(latest(1, path)[0]["payload"]["commission"], 0.8)

    def test_missing_read_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "absent.sqlite3"
            self.assertEqual(latest(path=path), [])
            self.assertFalse(path.exists())

    def test_custom_state_keeps_journal_in_same_directory(self):
        self.assertEqual(
            database(Path("/tmp/test/state.json")), Path("/tmp/test/journal.sqlite3")
        )
