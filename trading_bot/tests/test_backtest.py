import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import Bar, simulate


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)

    def bars(self, values):
        return [
            Bar(self.start + timedelta(minutes=15 * i), v, v, v, v, 100)
            for i, v in enumerate(values)
        ]

    def test_next_bar_accounting_commission_both_sides(self):
        with patch(
            "backtest.select_action",
            side_effect=[("BUY", "test"), ("SELL", "test"), ("HOLD", "test")],
        ):
            r = simulate(
                self.bars([100, 100, 102]), start=self.start, fee_bps=10, slippage_bps=0
            )
        s = r["summary"]
        self.assertAlmostEqual(s["fees"], 0.202)
        self.assertAlmostEqual(s["net_pnl"], 1.798)
        self.assertAlmostEqual(s["realized_pnl"], 1.798)
        self.assertEqual(s["closed_trades"], 1)
        buy = next(e for e in r["events"] if e["action"] == "BUY")
        self.assertEqual(buy["time"], (self.start + timedelta(minutes=15)).isoformat())

    def test_slippage_lowers_profit_and_does_not_fill_above_limit(self):
        def run(slip):
            with patch(
                "backtest.select_action",
                side_effect=[("BUY", "test"), ("SELL", "test"), ("HOLD", "test")],
            ):
                return simulate(
                    self.bars([100, 100, 102]),
                    start=self.start,
                    fee_bps=0,
                    slippage_bps=slip,
                )

        self.assertLess(run(5)["summary"]["net_pnl"], run(0)["summary"]["net_pnl"])
        with patch("backtest.select_action", return_value=("BUY", "test")):
            r = simulate(self.bars([100, 110]), start=self.start)
        self.assertEqual(r["summary"]["open_lots"], 0)
        self.assertTrue(any(e["action"] == "SKIP_BUY" for e in r["events"]))

    def test_stop_gap_uses_worse_open_not_impossible_stop_price(self):
        with patch(
            "backtest.select_action",
            side_effect=[("BUY", "test"), ("HOLD", "test"), ("HOLD", "test")],
        ):
            r = simulate(
                self.bars([100, 100, 95]), start=self.start, fee_bps=0, slippage_bps=0
            )
        sell = next(e for e in r["events"] if e["action"] == "SELL")
        self.assertEqual(sell["price"], 95)
        self.assertEqual(r["summary"]["net_pnl"], -5)
        self.assertAlmostEqual(r["summary"]["max_drawdown_pct"], 0.005)

    def test_open_position_mark_to_market_not_fake_liquidation(self):
        with patch(
            "backtest.select_action", side_effect=[("BUY", "test"), ("HOLD", "test")]
        ):
            r = simulate(
                self.bars([100, 100]), start=self.start, fee_bps=10, slippage_bps=0
            )
        self.assertEqual(r["summary"]["open_lots"], 1)
        self.assertEqual(r["summary"]["closed_trades"], 0)
        self.assertAlmostEqual(r["summary"]["net_pnl"], r["summary"]["unrealized_pnl"])
        self.assertAlmostEqual(r["summary"]["net_pnl"], -0.1)

    def test_warmup_has_no_trades_and_small_budget_blocks_buy(self):
        with patch("backtest.select_action", return_value=("BUY", "test")):
            r = simulate(
                self.bars([100] * 5),
                start=self.start + timedelta(minutes=30),
                initial=1000,
            )
        self.assertEqual(r["summary"]["bars"], 3)
        self.assertEqual(r["summary"]["fees"], 0)

    def test_future_data_cannot_change_earlier_fills(self):
        with patch(
            "backtest.select_action",
            side_effect=lambda p, price, s: (
                ("BUY", "test") if not s.held_lots else ("HOLD", "test")
            ),
        ):
            a = simulate(self.bars([100, 100, 101]), start=self.start)
            b = simulate(self.bars([100, 100, 200]), start=self.start)
        self.assertEqual(a["events"][:3], b["events"][:3])

    def test_invalid_bars_and_parameters_fail(self):
        bars = self.bars([100, 100])
        for invalid in [[bars[1], bars[0]], [bars[0], bars[0]]]:
            with self.assertRaises(ValueError):
                simulate(invalid, start=self.start)
        with self.assertRaises(ValueError):
            simulate(bars, start=self.start, fee_bps=float("nan"))
        with self.assertRaises(RuntimeError):
            simulate([], start=self.start)

    def test_daily_entry_limit(self):
        with patch(
            "backtest.select_action",
            side_effect=lambda p, price, s: (
                ("BUY", "test") if not s.held_lots else ("SELL", "test")
            ),
        ):
            r = simulate(
                self.bars([100] * 10), start=self.start, fee_bps=0, slippage_bps=0
            )
        self.assertEqual(sum(e["action"] == "BUY" for e in r["events"]), 2)
        self.assertEqual(sum(e["action"] == "SELL" for e in r["events"]), 2)
