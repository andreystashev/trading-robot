import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy import StrategyParams, configured_signal
from strategy_catalog import Profile, parse_profile, import_profile, profiles
from paper_trader import PaperSession
from auto_trader import BotState, select_action


class ProfileTests(unittest.TestCase):
    def test_import_is_validated_isolated_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "strategy_catalog.CATALOG", Path(tmp)
        ):
            p = import_profile(
                {
                    "id": "lkoh_test",
                    "name": "LKOH",
                    "ticker": "lkoh",
                    "params": {"fast": 2, "slow": 3},
                }
            )
            self.assertEqual(p.ticker, "LKOH")
            self.assertEqual(profiles()[p.id], p)
            with self.assertRaises(ValueError):
                import_profile(p.public())
            for data in [
                {"id": "../escape", "name": "x", "ticker": "SBER"},
                {"name": "x", "ticker": "SBER", "python": "evil"},
                {"name": "x", "ticker": "SBER", "params": {"fast": 2.5, "slow": 3}},
            ]:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    parse_profile(data)

    def test_custom_strategy_used_by_sandbox_decision(self):
        params = StrategyParams(fast=2, slow=3, stop_pct=2)
        state = BotState("test", "LKOH")
        self.assertEqual(
            select_action([100, 100, 100, 110], 110, state, params)[0], "BUY"
        )
        state.held_lots = 1
        state.entry_price = 100
        self.assertEqual(select_action([], 98, state, params)[0], "SELL")
        self.assertEqual(select_action([], 98.5, state, params)[0], "HOLD")


class ProfileReportTests(unittest.TestCase):
    def test_report_keeps_selected_instrument_and_profile(self):
        from backtest import Bar, run_backtest

        start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        bars = [
            Bar(start + timedelta(minutes=15 * i), 100, 100, 100, 100, 10)
            for i in range(4)
        ]
        broker = SimpleNamespace(
            settings=SimpleNamespace(ticker="LKOH", sandbox_initial_balance=200000),
            find_share_by_ticker=lambda ticker: SimpleNamespace(
                currency="rub", lot=1, uid="lkoh", ticker=ticker
            ),
        )
        profile = Profile("lkoh_test", "LKOH test", "LKOH").public()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "backtest.ROOT", Path(tmp)
        ), patch(
            "backtest.date_range", return_value=(start, start + timedelta(days=1))
        ), patch(
            "backtest.download", return_value=bars
        ):
            folder = run_backtest(broker, profile=profile)
            latest = json.loads((Path(tmp) / "latest.json").read_text())
            self.assertEqual(latest["profile"], profile)
            self.assertEqual(latest["instrument"]["ticker"], "LKOH")
            self.assertIn("LKOH", (folder / "report.html").read_text())


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch("strategy_catalog.RUNTIME", Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.profile = Profile("test", "Test", "LKOH", StrategyParams(fast=2, slow=3))
        self.paper = PaperSession(self.profile)
        self.origin = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)

    def data(self, minutes, price=110, prices=None):
        now = self.origin + timedelta(minutes=minutes)
        seq = prices or [100, 100, 100, 110]
        return {
            "connected": True,
            "ticker": "LKOH",
            "currency": "rub",
            "lot": 10,
            "price": price,
            "quote_age": 0,
            "quote_time": now.isoformat(),
            "updated": now.isoformat(),
            "market": {"api": True, "market": True},
            "series": [
                {
                    "time": (now - timedelta(minutes=15 * (len(seq) - i))).isoformat(),
                    "close": v,
                }
                for i, v in enumerate(seq)
            ],
        }

    def test_current_quotes_fills_costs_stop_and_recovery(self):
        self.paper.start(self.data(0))
        self.paper.update(self.data(0))
        self.assertEqual(
            self.paper.state["lots"], 0
        )  # warm-up cannot retroactively buy
        self.paper.update(self.data(15))
        buy = 110 * 1.0005 * 10
        fee = buy * 0.0005
        self.assertEqual(self.paper.state["lots"], 1)
        self.assertAlmostEqual(self.paper.snapshot(10)["net_pnl"], 1100 - buy - fee)
        self.paper.update(self.data(15))
        self.assertEqual(len(self.paper.state["events"]), 1)
        self.paper.update(self.data(16, 100))
        sell = 100 * 0.9995 * 10
        sell_fee = sell * 0.0005
        self.assertAlmostEqual(
            self.paper.snapshot(10)["net_pnl"], sell - sell_fee - buy - fee
        )
        self.assertAlmostEqual(
            self.paper.state["realized"], self.paper.snapshot(10)["net_pnl"]
        )
        recovered = PaperSession(self.profile)
        self.assertFalse(recovered.running)
        self.assertEqual(recovered.state, self.paper.state)
        other = PaperSession(Profile("other", "Other", "SBER"))
        self.assertEqual(other.state["cash"], 100000)

    def test_stale_unavailable_market_and_lot_risk_block_trades(self):
        self.paper.start(self.data(0))
        d = self.data(15)
        d["quote_age"] = 121
        self.paper.update(d)
        self.assertFalse(self.paper.state["events"])
        d = self.data(16)
        d["market"]["market"] = False
        self.paper.update(d)
        self.assertFalse(self.paper.state["events"])
        d = self.data(30, 110, [100, 100, 100, 110])
        d["lot"] = 1000
        self.paper.update(d)
        self.assertEqual(self.paper.state["lots"], 0)
        self.assertIn("5%", self.paper.state["decision"])

    def test_pause_and_parameter_tampering(self):
        self.paper.start(self.data(0))
        self.paper.update(self.data(15))
        self.paper.stop()
        before = self.paper.snapshot(10)
        self.paper.update(self.data(30, 100))
        self.assertEqual(before, self.paper.snapshot(10))
        with self.assertRaises(ValueError):
            PaperSession(
                Profile("test", "Changed", "LKOH", StrategyParams(fast=2, slow=3))
            )

    def test_quote_before_candle_close_cannot_use_signal(self):
        self.paper.start(self.data(0))
        d = self.data(15)
        d["quote_time"] = (self.origin + timedelta(minutes=1)).isoformat()
        self.paper.update(d)
        self.assertEqual(self.paper.state["lots"], 0)

    def test_corrupt_saved_portfolio_fails_closed(self):
        self.paper.start(self.data(0))
        self.paper.update(self.data(15))
        good = dict(self.paper.state)
        for changes in [
            {"realized": float("nan")},
            {"lot_size": -1},
            {"last_quote": "invalid"},
            {"last_quote": "2026-01-01T00:00:00"},
            {"curve": [{"time": "invalid", "equity": 100}]},
            {"events": "invalid"},
            {"lots": True},
        ]:
            with self.subTest(changes=changes):
                self.paper.path.write_text(json.dumps({**good, **changes}))
                with self.assertRaises(ValueError):
                    PaperSession(self.profile)


if __name__ == "__main__":
    unittest.main()
