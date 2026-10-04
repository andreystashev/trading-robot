import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace as NS
from grpc import StatusCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_trader import BotState, load_state
from config import Settings
from log_setup import RedactTokens, MoscowFormatter
from sandbox_smoke import send, run_smoke
from t_invest_client import ShareInfo
from t_tech.invest.exceptions import RequestError
from t_tech.invest import MoneyValue


class DiagnosticsTests(unittest.TestCase):
    def test_credentials_redacted(self):
        record = logging.LogRecord(
            "test",
            logging.INFO,
            "",
            1,
            "Token %s; Bearer abc",
            ("t." + "x" * 50,),
            None,
        )
        RedactTokens().filter(record)
        self.assertNotIn("x" * 50, record.getMessage())
        self.assertNotIn("Bearer abc", record.getMessage())
        self.assertIn("MSK", MoscowFormatter().formatTime(record))

    def test_rejection_vs_uncertain_submission(self):
        for code, detail, preserved in [
            (StatusCode.INVALID_ARGUMENT, "30079", False),
            (StatusCode.UNAVAILABLE, "network lost", True),
        ]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                broker = MagicMock()
                broker.settings = Settings("fake", enable_sandbox_orders=True)
                broker.client.sandbox.post_sandbox_order.side_effect = RequestError(
                    code, detail, None
                )
                state = BotState("account", "uid")
                share = ShareInfo("SBER", "Sber", "uid", "figi", 1, "rub", "TQBR")
                with patch("sandbox_smoke.PATH", path), self.assertRaises(RequestError):
                    send(broker, state, share, "BUY")
                restored = load_state(path, "account", "uid")
                self.assertEqual(restored.pending is not None, preserved)
                self.assertEqual(broker.client.sandbox.post_sandbox_order.call_count, 1)

    def test_smoke_sell_without_buy_is_forbidden(self):
        broker = MagicMock()
        broker.settings = Settings("fake", enable_sandbox_orders=True)
        share = ShareInfo("SBER", "Sber", "uid", "figi", 1, "rub", "TQBR")
        with self.assertRaises(RuntimeError):
            send(broker, BotState("account", "uid"), share, "SELL")
        broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_round_trip_and_rejected_buy_without_sell(self):
        for filled in [0, 1]:
            with self.subTest(
                filled=filled
            ), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                broker = MagicMock()
                broker.settings = Settings("fake", enable_sandbox_orders=True)
                broker.find_share_by_ticker.return_value = ShareInfo(
                    "SBER", "Sber", "uid", "figi", 1, "rub", "TQBR"
                )
                broker.get_or_create_sandbox_account.return_value = "account"
                cash = MoneyValue(currency="rub", units=100000, nano=0)
                broker.get_sandbox_positions.return_value = NS(
                    securities=[], money=[cash], blocked=[]
                )
                broker.client.sandbox.get_sandbox_orders.return_value = NS(orders=[])
                broker.get_sandbox_portfolio.return_value = NS(
                    total_amount_portfolio=cash
                )
                broker.trading_status.return_value = NS(
                    trading_status=NS(name="SESSION_OPEN")
                )
                broker.last_price.return_value = (100.0, datetime.now(timezone.utc))
                broker.client.sandbox.post_sandbox_order.side_effect = [
                    NS(order_id="buy"),
                    NS(order_id="sell"),
                ]
                results = [
                    NS(order_id="buy", lots_executed=filled),
                    NS(order_id="sell", lots_executed=1),
                ]
                with patch("sandbox_smoke.PATH", path), patch(
                    "sandbox_smoke.wait_order", side_effect=results
                ):
                    run_smoke(broker)
                calls = broker.client.sandbox.post_sandbox_order.call_args_list
                self.assertEqual(len(calls), 2 if filled else 1)
                self.assertEqual(
                    calls[0].kwargs["direction"].name, "ORDER_DIRECTION_BUY"
                )
                if filled:
                    self.assertEqual(
                        calls[1].kwargs["direction"].name, "ORDER_DIRECTION_SELL"
                    )
                self.assertEqual(load_state(path, "account", "uid").held_lots, 0)
