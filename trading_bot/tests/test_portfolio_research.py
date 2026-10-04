import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import Bar
from portfolio_research import targets, simulate_portfolio


class DailyPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.origin = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def bars(self, values):
        return [
            Bar(self.origin + timedelta(days=i), v, v, v, v, 100)
            for i, v in enumerate(values)
        ]

    def test_positive_momentum_rank_and_regime(self):
        history = {
            "A": list(range(100, 320)),
            "B": [100] * 220,
            "C": list(range(400, 180, -1)),
        }
        self.assertEqual(targets(history), {"A": 0.05})
        self.assertEqual(targets({"A": [100] * 100}), {})

    def test_next_open_no_future_close_leakage(self):
        values = list(range(100, 340))
        base = {"A": self.bars(values), "B": self.bars(values)}
        start = self.origin + timedelta(days=205)
        a = simulate_portfolio(base, {"A": 1, "B": 1}, start)
        altered = {"A": self.bars(values[:-1] + [1000]), "B": self.bars(values)}
        b = simulate_portfolio(altered, {"A": 1, "B": 1}, start)
        self.assertEqual(a["events"], b["events"])
        self.assertEqual(a["curve"][:-1], b["curve"][:-1])
        first = a["events"][0]
        self.assertEqual(first["date"], start.date().isoformat())
        self.assertAlmostEqual(first["price"], values[205] * 1.0005)

    def test_costs_and_risk_budget(self):
        data = {"A": self.bars([100] * 10), "B": self.bars([100] * 10)}
        r = simulate_portfolio(data, {"A": 1, "B": 1}, self.origin, passive=True)
        first = r["curve"][0]
        self.assertLessEqual(sum(first["positions"][t] * 100 for t in data), 10000)
        self.assertEqual(len(r["events"]), 2)
        self.assertLess(r["summary"]["net_pnl"], 0)
        self.assertAlmostEqual(r["summary"]["fees"], sum(e["fee"] for e in r["events"]))
        for q in first["positions"].values():
            self.assertGreaterEqual(q, 0)

    def test_missing_data_and_invalid_series_fail(self):
        data = {"A": self.bars([100] * 2), "B": self.bars([100] * 2)}
        with self.assertRaises(ValueError):
            simulate_portfolio(data, {"A": 1, "B": 0}, self.origin)
        with self.assertRaises(ValueError):
            simulate_portfolio(data, {"A": 1, "B": 1}, datetime(2025, 1, 1))
        data["B"] = list(reversed(data["B"]))
        with self.assertRaises(ValueError):
            simulate_portfolio(data, {"A": 1, "B": 1}, self.origin)

    def test_flat_market_is_cash_not_profitable_trades(self):
        data = {"A": self.bars([100] * 240), "B": self.bars([100] * 240)}
        r = simulate_portfolio(
            data, {"A": 1, "B": 1}, self.origin + timedelta(days=200)
        )
        self.assertEqual(r["summary"]["net_pnl"], 0)
        self.assertFalse(r["events"])
