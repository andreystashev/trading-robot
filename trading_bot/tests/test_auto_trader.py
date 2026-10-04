"""Offline scenario tests; no API requests or genuine credentials."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_trader import (
    BotState,
    load_state,
    reconcile_pending,
    save_state,
    select_action,
    state_lock,
    submit_intent,
    tick,
)
from config import Settings, ensure_auto_trading_allowed
from strategy import crossover_signal
from t_invest_client import ShareInfo
from t_tech.invest import MoneyValue, Quotation


def money(value):
    return MoneyValue(
        currency="rub", units=int(value), nano=round((value - int(value)) * 1e9)
    )


class BotScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.now = datetime.now(timezone.utc)
        self.share = ShareInfo("SBER", "Sber", "uid", "figi", 10, "rub", "TQBR")
        self.state = BotState("account", "uid")
        self.broker = MagicMock()
        self.broker.settings = Settings(
            "fake", enable_sandbox_orders=True, enable_auto_trading=True
        )
        self.broker.client.sandbox.get_sandbox_orders.return_value = NS(orders=[])
        self.broker.get_sandbox_positions.return_value = NS(
            securities=[], money=[money(100000)], blocked=[]
        )
        self.broker.get_sandbox_portfolio.return_value = NS(
            total_amount_portfolio=money(100000)
        )
        self.broker.trading_status.return_value = NS(
            api_trade_available_flag=True,
            market_order_available_flag=True,
            limit_order_available_flag=True,
            trading_status=NS(name="SESSION_OPEN"),
        )
        self.broker.client.instruments.share_by.return_value = NS(
            instrument=NS(min_price_increment=Quotation(units=0, nano=10000000))
        )
        self.broker.last_price.return_value = (100.0, self.now - timedelta(seconds=2))
        candles = [
            NS(
                close=Quotation(units=p, nano=0),
                time=self.now - timedelta(minutes=15 * (31 - i)),
                is_complete=True,
            )
            for i, p in enumerate([100] * 30 + [110])
        ]
        self.broker.client.market_data.get_candles.return_value = NS(candles=candles)

    def test_crossing_is_event_not_permanent_buy(self):
        self.assertEqual(crossover_signal([100] * 30 + [110]), "BUY")
        self.assertEqual(crossover_signal([100] * 30 + [90]), "SELL")
        self.assertEqual(crossover_signal([100] * 31), "HOLD")
        self.assertEqual(crossover_signal(list(range(31))), "HOLD")

    def test_automatic_execution_requires_both_flags(self):
        for settings in [
            Settings("x"),
            Settings("x", enable_sandbox_orders=True),
            Settings(
                "x", sandbox=False, enable_sandbox_orders=True, enable_auto_trading=True
            ),
        ]:
            with self.assertRaises(RuntimeError):
                ensure_auto_trading_allowed(settings)

    def test_dry_run_and_stale_price_send_nothing(self):
        self.assertTrue(
            tick(self.broker, self.share, self.state, self.path, False).startswith(
                "BUY"
            )
        )
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()
        self.broker.last_price.return_value = (100.0, self.now - timedelta(hours=1))
        self.assertIn(
            "stale", tick(self.broker, self.share, self.state, self.path, True)
        )
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_failed_submission_persists_intent_before_request(self):
        self.broker.client.sandbox.post_sandbox_order.side_effect = RuntimeError(
            "connection lost"
        )
        with self.assertRaises(RuntimeError):
            tick(self.broker, self.share, self.state, self.path, True)
        restarted = load_state(self.path, "account", "uid")
        self.assertIsNotNone(restarted.pending)
        self.assertEqual(restarted.entries, 1)
        self.broker.client.sandbox.get_sandbox_order_state.side_effect = RuntimeError(
            "not found"
        )
        with self.assertRaises(RuntimeError):
            tick(self.broker, self.share, restarted, self.path, True)
        self.assertEqual(self.broker.client.sandbox.post_sandbox_order.call_count, 1)

    def test_recover_filled_order_after_restart(self):
        self.state.pending = {
            "request_id": "request",
            "side": "BUY",
            "created": self.now.isoformat(),
        }
        save_state(self.state, self.path)
        restarted = load_state(self.path, "account", "uid")
        self.broker.client.sandbox.get_sandbox_order_state.return_value = NS(
            order_id="exchange",
            execution_report_status=NS(name="EXECUTION_REPORT_STATUS_FILL"),
            lots_executed=1,
            lots_requested=1,
            executed_commission=money(0.05),
            total_order_amount=money(100.55),
            stages=[NS(price=money(100.5), quantity=10)],
        )
        self.assertTrue(
            reconcile_pending(self.broker, restarted, self.path, True, self.now)
        )
        self.assertEqual(restarted.held_lots, 1)
        self.assertEqual(restarted.entry_price, 100.5)
        self.assertIsNone(load_state(self.path, "account", "uid").pending)

    def test_pending_order_wait_then_cancel_never_resubmit(self):
        self.state.pending = {
            "request_id": "request",
            "side": "BUY",
            "created": (self.now - timedelta(seconds=61)).isoformat(),
        }
        self.broker.client.sandbox.get_sandbox_order_state.return_value = NS(
            order_id="exchange",
            execution_report_status=NS(name="EXECUTION_REPORT_STATUS_NEW"),
            lots_executed=0,
            lots_requested=1,
        )
        self.assertFalse(
            reconcile_pending(self.broker, self.state, self.path, False, self.now)
        )
        self.broker.client.sandbox.cancel_sandbox_order.assert_not_called()
        self.assertFalse(
            reconcile_pending(self.broker, self.state, self.path, True, self.now)
        )
        self.broker.client.sandbox.cancel_sandbox_order.assert_called_once()
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_untracked_position_and_orders_block_trading(self):
        self.broker.get_sandbox_positions.return_value.securities = [
            NS(instrument_uid="uid", balance=10, blocked=0)
        ]
        with self.assertRaises(RuntimeError):
            tick(self.broker, self.share, self.state, self.path, True)
        self.broker.client.sandbox.get_sandbox_orders.return_value = NS(orders=[NS()])
        with self.assertRaises(RuntimeError):
            tick(self.broker, self.share, self.state, self.path, True)
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_stop_loss_works_without_candles(self):
        self.state.held_lots = 1
        self.state.entry_price = 100
        self.assertEqual(
            select_action([], 98, self.state), ("SELL", "software stop loss")
        )
        self.assertEqual(select_action([], 100, self.state)[0], "HOLD")

    def test_daily_loss_blocks_new_entries(self):
        tick(self.broker, self.share, self.state, self.path, False)
        self.broker.get_sandbox_portfolio.return_value.total_amount_portfolio = money(
            98000
        )
        tick(self.broker, self.share, self.state, self.path, True)
        self.assertTrue(self.state.halted)
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_risk_limits_cash_position_size_and_attempts(self):
        self.broker.get_sandbox_positions.return_value.money = [money(1)]
        self.assertIn(
            "limit", tick(self.broker, self.share, self.state, self.path, True)
        )
        self.broker.get_sandbox_positions.return_value.money = [money(100000)]
        self.share = ShareInfo("SBER", "Sber", "uid", "figi", 100, "rub", "TQBR")
        self.assertIn(
            "limit", tick(self.broker, self.share, self.state, self.path, True)
        )
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_entry_attempt_cap_survives_restart(self):
        tick(self.broker, self.share, self.state, self.path, False)
        self.state.entries = 2
        save_state(self.state, self.path)
        restarted = load_state(self.path, "account", "uid")
        self.assertIn(
            "limit", tick(self.broker, self.share, restarted, self.path, True)
        )
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_stop_loss_sells_without_new_candle_and_halts_entries(self):
        self.state.held_lots = 1
        self.state.entry_price = 110
        self.broker.get_sandbox_positions.return_value.securities = [
            NS(instrument_uid="uid", balance=10, blocked=0)
        ]
        self.broker.client.market_data.get_candles.return_value = NS(candles=[])
        self.broker.client.sandbox.post_sandbox_order.return_value = NS(
            order_id="exit",
            execution_report_status=NS(name="EXECUTION_REPORT_STATUS_FILL"),
            lots_executed=1,
            lots_requested=1,
        )
        self.assertIn(
            "software stop", tick(self.broker, self.share, self.state, self.path, True)
        )
        self.assertTrue(self.state.halted)
        kwargs = self.broker.client.sandbox.post_sandbox_order.call_args.kwargs
        self.assertEqual(kwargs["direction"].name, "ORDER_DIRECTION_SELL")
        self.assertEqual(kwargs["order_type"].name, "ORDER_TYPE_MARKET")

    def test_same_bar_does_not_reenter(self):
        self.state.last_bar = (
            self.broker.client.market_data.get_candles.return_value.candles[
                -1
            ].time.isoformat()
        )
        self.assertIn(
            "no new", tick(self.broker, self.share, self.state, self.path, True)
        )
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_buy_limit_price_caps_cost(self):
        self.broker.client.sandbox.post_sandbox_order.return_value = NS(
            order_id="exchange",
            execution_report_status=NS(name="EXECUTION_REPORT_STATUS_NEW"),
            lots_executed=0,
            lots_requested=1,
        )
        tick(self.broker, self.share, self.state, self.path, True)
        kwargs = self.broker.client.sandbox.post_sandbox_order.call_args.kwargs
        self.assertEqual(kwargs["order_type"].name, "ORDER_TYPE_LIMIT")
        self.assertEqual(
            (kwargs["price"].units, kwargs["price"].nano), (100, 500000000)
        )

    def test_no_short_or_pyramiding(self):
        with self.assertRaises(RuntimeError):
            submit_intent(
                self.broker, self.state, self.share, "SELL", self.now, self.path
            )
        self.state.held_lots = 1
        with self.assertRaises(RuntimeError):
            submit_intent(
                self.broker, self.state, self.share, "BUY", self.now, self.path
            )
        self.broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_state_binding_and_lock(self):
        save_state(self.state, self.path)
        with self.assertRaises(RuntimeError):
            load_state(self.path, "other", "uid")
        with state_lock(self.path):
            with self.assertRaises(RuntimeError):
                with state_lock(self.path):
                    pass


if __name__ == "__main__":
    unittest.main()
