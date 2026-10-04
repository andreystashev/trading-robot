import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import date_range
from strategy import StrategyParams, configured_signal, crossover_signal
from dashboard import Dashboard


class ParameterTests(unittest.TestCase):
    def test_moscow_dates_are_inclusive(self):
        start, end = date_range(30, "2026-09-01", "2026-09-03")
        self.assertEqual(start.isoformat(), "2026-08-31T21:00:00+00:00")
        self.assertEqual(end - start, timedelta(days=3))

    def test_invalid_ranges(self):
        for first, last in [
            ("2026-09-01", None),
            ("2026-09-05", "2026-09-01"),
            ("bad", "2026-09-03"),
            ("2026-09-01", "2099-01-01"),
        ]:
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                date_range(30, first, last)

    def test_default_signal_is_unchanged(self):
        for prices in [
            [100] * 30 + [110],
            [100] * 30 + [90],
            [100] * 31,
            list(range(1, 50)),
        ]:
            self.assertEqual(
                configured_signal(prices, StrategyParams()), crossover_signal(prices)
            )

    def test_filters_and_parameter_validation(self):
        self.assertEqual(
            configured_signal([100] * 30 + [100.01], StrategyParams(entry_edge_bps=5)),
            "HOLD",
        )
        self.assertEqual(
            configured_signal(
                [200] * 90 + [100] * 30 + [110], StrategyParams(trend=120)
            ),
            "HOLD",
        )
        for kwargs in [
            {"fast": 30, "slow": 10},
            {"stop_pct": 0},
            {"trend": 301},
            {"entry_edge_bps": float("nan")},
        ]:
            with self.assertRaises(ValueError):
                StrategyParams(**kwargs)

    def test_breakout_excludes_current_close_and_exits_channel(self):
        p = StrategyParams(10, 20, 0, kind="breakout")
        self.assertEqual(configured_signal([100] * 20 + [101], p), "BUY")
        self.assertEqual(configured_signal([100] * 20 + [99], p), "SELL")
        self.assertEqual(configured_signal([100] * 21, p), "HOLD")
        self.assertEqual(configured_signal([100] * 20, p), "HOLD")

    def test_breakout_trend_filter_and_validation(self):
        p = StrategyParams(10, 20, 40, kind="breakout")
        self.assertEqual(configured_signal([200] * 20 + [100] * 20 + [101], p), "HOLD")
        with self.assertRaises(ValueError):
            StrategyParams(kind="unknown")

    def test_dashboard_passes_dates_and_parameters_as_arguments(self):
        app = Dashboard()
        with patch("dashboard.subprocess.Popen") as spawn:
            spawn.return_value.poll.return_value = None
            job = app.start_backtest(
                {
                    "from_date": "2026-09-01",
                    "to_date": "2026-09-03",
                    "fast": 20,
                    "slow": 60,
                    "trend": 120,
                    "fee_bps": 7,
                }
            )
            args = spawn.call_args.args[0]
            self.assertIn("2026-09-01", args)
            self.assertIn("--trend", args)
            self.assertEqual(len(job), 8)
            with self.assertRaises(RuntimeError):
                app.start_backtest({"days": 30})
            self.assertEqual(spawn.call_count, 1)

    def test_bad_parameters_never_start_process(self):
        with patch("dashboard.subprocess.Popen") as spawn:
            with self.assertRaises(ValueError):
                Dashboard().start_backtest({"fast": 60, "slow": 20})
            spawn.assert_not_called()
