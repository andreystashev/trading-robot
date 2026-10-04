import unittest, sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import Bar, simulate
from market_context import market_filter
from strategy import StrategyParams


class ContextTests(unittest.TestCase):
    def bars(self, n):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            Bar(
                start + timedelta(minutes=15 * i),
                100 + i / 10,
                100 + i / 10,
                100 + i / 10,
                100 + i / 10,
                10,
            )
            for i in range(n)
        ]

    def test_future_bars_do_not_change_past_signal(self):
        reference = self.bars(150)
        target = reference[119:125]
        self.assertEqual(
            market_filter(target, reference), market_filter(target, reference[:125])
        )
        self.assertTrue(all(market_filter(target, reference).values()))

    def test_stale_missing_and_insufficient_context_block(self):
        r = self.bars(120)
        future = self.bars(121)[-1]
        self.assertFalse(market_filter([future], r)[future.time])
        self.assertFalse(market_filter([r[0]], r)[r[0].time])
        self.assertFalse(market_filter([r[-1]], [])[r[-1].time])

    def test_external_filter_blocks_buys(self):
        bars = self.bars(50)
        p = StrategyParams(10, 20, kind="breakout")
        unfiltered = simulate(bars, start=bars[0].time, params=p)
        filtered = simulate(bars, start=bars[0].time, params=p, entry_filter={})
        self.assertTrue(any(e["action"] == "BUY" for e in unfiltered["events"]))
        self.assertFalse(any(e["action"] == "BUY" for e in filtered["events"]))
        self.assertEqual(filtered["summary"]["net_pnl"], 0)

    def test_duplicate_reference_rejected(self):
        r = self.bars(2)
        with self.assertRaises(ValueError):
            market_filter(r, r + r)

    def test_missing_context_does_not_block_exit_of_open_position(self):
        bars = self.bars(40)
        for i in range(28, 40):
            price = 100 + (40 - i) / 10
            bars[i] = Bar(bars[i].time, price, price, price, price, 10)
        allowed = {b.time: True for b in bars[:26]}
        result = simulate(
            bars,
            start=bars[0].time,
            params=StrategyParams(10, 20, kind="breakout"),
            entry_filter=allowed,
        )
        actions = [e["action"] for e in result["events"]]
        self.assertIn("BUY", actions)
        self.assertIn("SELL", actions)
