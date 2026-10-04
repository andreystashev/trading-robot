import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy import StrategyParams, configured_signal
from strategy_catalog import get_profile


class ReversionTests(unittest.TestCase):
    def test_deep_oversold_pullback_and_mean_exit(self):
        p = StrategyParams(2, 20, 0, 30, 2, "reversion")
        prices = [99, 101] * 10 + [96]
        self.assertEqual(configured_signal(prices, p), "BUY")
        self.assertEqual(configured_signal([99, 101] * 10 + [101], p), "SELL")
        self.assertEqual(configured_signal([100] * 21, p), "SELL")
        self.assertEqual(configured_signal(prices[:10], p), "HOLD")

    def test_trend_filter_blocks_falling_market(self):
        p = StrategyParams(2, 20, 40, 30, 2, "reversion")
        self.assertEqual(
            configured_signal([120] * 20 + [99, 101] * 10 + [96], p), "HOLD"
        )

    def test_rsi2_pullback_requires_oversold_edge_and_rising_regime(self):
        prices = [90] * 200 + [100] * 30 + [99, 98]
        p = StrategyParams(2, 5, 200, 30, 2, "pullback")
        self.assertEqual(configured_signal(prices, p), "BUY")
        self.assertEqual(
            configured_signal([110] * 200 + [100] * 30 + [99, 98], p), "HOLD"
        )
        self.assertEqual(
            configured_signal([100] * 200 + [100, 99.99, 99.98], p), "HOLD"
        )

    def test_profiles_have_supported_shared_algorithms(self):
        for name in ["reversion_sber", "pullback_sber"]:
            p = get_profile(name)
            self.assertEqual(p.params.trend, 200)
            self.assertEqual(p.params.stop_pct, 2)
            self.assertGreaterEqual(p.params.entry_edge_bps, 30)
