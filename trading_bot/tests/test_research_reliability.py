import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import Bar
from journal import latest, record
from log_setup import MoscowFormatter
from research_context import comparison_html, load_reference, run
from research_io import atomic_text, read_bars, write_bars, write_json
from strategy import StrategyParams
from auto_trader import BotState, tick


class ResearchReliabilityTests(unittest.TestCase):
    def bars(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            Bar(start + timedelta(minutes=15 * i), 100, 101, 99, 100, 10)
            for i in range(2)
        ]

    def test_csv_rejects_empty_duplicate_and_invalid_candles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.csv"
            for bars in [
                [],
                self.bars() + self.bars(),
                [Bar(self.bars()[0].time, 100, 99, 101, 100, 0)],
            ]:
                write_bars(path, bars)
                with self.assertRaises(ValueError):
                    read_bars(path)
            write_bars(path, self.bars())
            self.assertEqual(read_bars(path), self.bars())

    def test_failed_publish_preserves_previous_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text("old")
            with patch("research_io.Path.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    atomic_text(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cache_uses_exact_requested_range(self):
        bars = self.bars()
        start = bars[0].time
        end = bars[-1].time + timedelta(minutes=15)
        with tempfile.TemporaryDirectory() as directory, patch(
            "research_context.SandboxBroker"
        ) as broker, patch("research_context.load_settings"), patch(
            "research_context.download", return_value=bars
        ) as download:
            output = Path(directory)
            self.assertEqual(load_reference(output, "LKOH", start, end), bars)
            self.assertEqual(load_reference(output, "LKOH", start, end), bars)
            self.assertEqual(download.call_count, 1)
            load_reference(output, "LKOH", start, end + timedelta(minutes=15))
            self.assertEqual(download.call_count, 2)

    def test_report_displays_actual_reference_and_escapes_html(self):
        document = comparison_html([], "<GMKN>")
        self.assertIn("&lt;GMKN&gt;", document)
        self.assertNotIn("LKOH", document)

    def test_journal_read_handles_special_uri_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal?#.sqlite3"
            record("PLAN", {"lots": 1}, path=path)
            self.assertEqual(latest(path=path)[0]["kind"], "PLAN")

    def test_exception_credentials_are_redacted(self):
        secret = "t." + "a" * 40
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            event = logging.LogRecord(
                "test", logging.ERROR, "", 0, "failed", (), sys.exc_info()
            )
        formatted = MoscowFormatter().format(event)
        self.assertNotIn(secret, formatted)
        self.assertIn("[REDACTED]", formatted)

    def test_journal_failure_does_not_hide_original_cycle_error(self):
        state = BotState("account", "uid")
        share = MagicMock(ticker="SBER")
        with patch("auto_trader._tick", side_effect=ValueError("original")), patch(
            "auto_trader.record", side_effect=OSError("journal")
        ), patch("auto_trader.logger"):
            with self.assertRaisesRegex(ValueError, "original"):
                tick(MagicMock(), share, state, Path("/tmp/state.json"), False)

    def test_runs_keep_separate_artifacts_and_publish_actual_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            bars = self.bars()
            write_bars(source / "candles.csv", bars)
            write_json(
                source / "result.json",
                {
                    "parameters": {
                        "start": bars[0].time.isoformat(),
                        "lot": 1,
                        "fee_bps_per_side": 5,
                        "slippage_bps_per_side": 5,
                    },
                    "summary": {"initial": 100000},
                },
            )
            with patch("research_context.ROOT", root), patch(
                "research_context.load_reference", return_value=bars
            ), patch(
                "research_context.VARIANTS", [("baseline", StrategyParams())]
            ), patch(
                "research_context.COST_SCENARIOS", [5]
            ), patch(
                "builtins.print"
            ):
                first = run(["source"], "LKOH")
                second = run(["source"], "GMKN")
            self.assertNotEqual(first, second)
            self.assertIn("LKOH", (first / "report.html").read_text())
            self.assertIn("GMKN", (root / "market_context" / "report.html").read_text())

    def test_invalid_ticker_and_empty_input_fail_before_network(self):
        with patch("research_context.load_reference") as load:
            with self.assertRaises(ValueError):
                run([], "LKOH")
            with self.assertRaises(ValueError):
                run(["source"], "../LKOH")
            load.assert_not_called()
