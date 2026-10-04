import sys, tempfile, unittest, json
from pathlib import Path
from types import SimpleNamespace as NS
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from grpc import StatusCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rpc_limits import DeadlineInterceptor
from auto_trader import BotState, run_bot, save_state, load_state, reconcile_pending
from t_tech.invest.exceptions import RequestError
from t_tech.invest import MoneyValue


class StabilityTests(unittest.TestCase):
    def test_rpc_deadline_preserves_smaller_existing_timeout_and_calls_once(self):
        interceptor = DeadlineInterceptor(15)
        for timeout, expected in [(None, 15), (3, 3), (60, 15)]:
            continuation = MagicMock()
            details = NS(
                method="/PostSandboxOrder",
                timeout=timeout,
                metadata=(),
                credentials=None,
            )
            interceptor.intercept_unary_unary(continuation, details, "request")
            continuation.assert_called_once()
            self.assertEqual(continuation.call_args.args[0].timeout, expected)

    def test_transient_loop_error_recovers_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as folder:
            broker = MagicMock()
            broker.settings.ticker = "SBER"
            broker.get_or_create_sandbox_account.return_value = "account"
            broker.find_share_by_ticker.return_value.uid = "uid"
            error = RequestError(StatusCode.UNAVAILABLE, "offline", None)
            with patch(
                "auto_trader.tick", side_effect=[error, "HOLD", KeyboardInterrupt]
            ), patch("auto_trader.time.sleep") as sleep:
                with self.assertRaises(KeyboardInterrupt):
                    run_bot(broker, loop=True, path=Path(folder) / "state.json")
                self.assertEqual(sleep.call_count, 2)
            broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_nontransient_error_stops_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            broker = MagicMock()
            broker.settings.ticker = "SBER"
            broker.get_or_create_sandbox_account.return_value = "account"
            broker.find_share_by_ticker.return_value.uid = "uid"
            with patch(
                "auto_trader.tick",
                side_effect=RequestError(StatusCode.INVALID_ARGUMENT, "invalid", None),
            ), patch("auto_trader.time.sleep") as sleep:
                with self.assertRaises(RequestError):
                    run_bot(broker, loop=True, path=Path(folder) / "state.json")
                sleep.assert_not_called()

    def test_corrupted_pending_and_numeric_types_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            for change in [
                {"pending": []},
                {"pending": {"side": "BUY", "request_id": "x", "created": "invalid"}},
                {"entry_price": "100"},
                {"held_lots": True},
            ]:
                state = BotState("account", "uid")
                data = state.__dict__ | change
                path.write_text(json.dumps(data))
                with self.assertRaises(RuntimeError):
                    load_state(path, "account", "uid")

    def test_cancel_fill_race_adopts_fill_and_never_resubmits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = BotState(
                "account",
                "uid",
                pending={
                    "side": "BUY",
                    "request_id": "request",
                    "created": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
                },
            )
            broker = MagicMock()
            broker.settings.enable_sandbox_orders = True
            broker.settings.enable_auto_trading = True
            broker.settings.sandbox = True
            pending = NS(
                order_id="order",
                execution_report_status=NS(name="EXECUTION_REPORT_STATUS_NEW"),
                lots_executed=0,
                lots_requested=1,
            )
            filled = NS(
                order_id="order",
                execution_report_status=NS(name="EXECUTION_REPORT_STATUS_FILL"),
                lots_executed=1,
                lots_requested=1,
                executed_commission=MoneyValue(units=0, nano=0, currency="rub"),
                total_order_amount=MoneyValue(units=100, nano=0, currency="rub"),
                stages=[
                    NS(quantity=10, price=MoneyValue(units=100, nano=0, currency="rub"))
                ],
            )
            broker.client.sandbox.get_sandbox_order_state.side_effect = [
                pending,
                filled,
            ]
            now = datetime.now(timezone.utc)
            self.assertFalse(reconcile_pending(broker, state, path, True, now))
            self.assertTrue(reconcile_pending(broker, state, path, True, now))
            self.assertEqual(state.held_lots, 1)
            self.assertIsNone(state.pending)
            broker.client.sandbox.cancel_sandbox_order.assert_called_once()
            broker.client.sandbox.post_sandbox_order.assert_not_called()

    def test_repeated_transient_errors_stop_after_five_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            broker = MagicMock()
            broker.settings.ticker = "SBER"
            broker.get_or_create_sandbox_account.return_value = "account"
            broker.find_share_by_ticker.return_value.uid = "uid"
            with patch(
                "auto_trader.tick",
                side_effect=RequestError(StatusCode.UNAVAILABLE, "offline", None),
            ) as tick, patch("auto_trader.time.sleep") as sleep:
                with self.assertRaises(RequestError):
                    run_bot(broker, loop=True, path=Path(folder) / "state.json")
                self.assertEqual(tick.call_count, 5)
                self.assertEqual(
                    [call.args[0] for call in sleep.call_args_list], [30, 60, 120, 120]
                )
