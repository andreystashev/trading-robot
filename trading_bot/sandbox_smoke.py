"""Explicit one-lot sandbox round trip, separate from strategy state."""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from grpc import StatusCode
from t_tech.invest import OrderDirection, OrderType
from t_tech.invest.exceptions import RequestError
from t_tech.invest.schemas import OrderIdType

from auto_trader import BotState, load_state, save_state, state_lock, TERMINAL
from config import ensure_sandbox_trading_allowed
from t_invest_client import SandboxBroker, quotation_to_float

logger = logging.getLogger(__name__)
PATH = Path(__file__).with_name(".smoke_state.json")


def wait_order(broker, state):
    pending = state.pending
    deadline = time.monotonic() + 20
    while True:
        result = broker.client.sandbox.get_sandbox_order_state(
            account_id=state.account_id,
            order_id=pending.get("order_id") or pending["request_id"],
            order_id_type=(
                OrderIdType.ORDER_ID_TYPE_EXCHANGE
                if pending.get("order_id")
                else OrderIdType.ORDER_ID_TYPE_REQUEST
            ),
        )
        logger.info(
            "Smoke order %s: %s; filled=%s/%s; stages=%s",
            result.order_id,
            result.execution_report_status.name,
            result.lots_executed,
            result.lots_requested,
            [(quotation_to_float(s.price), s.quantity) for s in result.stages],
        )
        if result.execution_report_status.name in TERMINAL:
            return result
        if time.monotonic() >= deadline:
            ensure_sandbox_trading_allowed(broker.settings)
            broker.client.sandbox.cancel_sandbox_order(
                account_id=state.account_id, order_id=result.order_id
            )
            raise RuntimeError(
                "Smoke order cancellation requested; pending intent preserved. Inspect status before rerunning."
            )
        time.sleep(2)


def send(broker, state, share, side):
    ensure_sandbox_trading_allowed(broker.settings)
    if not broker.settings.sandbox or share.ticker != "SBER":
        raise RuntimeError("Smoke test permits Sandbox SBER only.")
    if state.pending:
        raise RuntimeError("Unresolved smoke intent: no repeat submission.")
    if side == "SELL" and state.held_lots != 1:
        raise RuntimeError("No filled smoke lot to sell; short forbidden.")
    state.pending = {
        "request_id": str(uuid4()),
        "side": side,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state, PATH)
    logger.info(
        "Smoke probe: sandbox %s ONE lot, UUID=%s", side, state.pending["request_id"]
    )
    try:
        result = broker.client.sandbox.post_sandbox_order(
            account_id=state.account_id,
            instrument_id=share.uid,
            quantity=1,
            direction=(
                OrderDirection.ORDER_DIRECTION_BUY
                if side == "BUY"
                else OrderDirection.ORDER_DIRECTION_SELL
            ),
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=state.pending["request_id"],
        )
    except RequestError as exc:
        # This documented validation error rejects submission; transport failures
        # retain intent because they do not establish an execution outcome.
        if exc.code == StatusCode.INVALID_ARGUMENT and str(exc.details) == "30079":
            state.pending = None
            save_state(state, PATH)
            logger.error(
                "Smoke rejected: 30079, instrument unavailable for trading; no execution."
            )
        logger.error(
            "Smoke API response: code=%s details=%s; no retry",
            exc.code.name,
            str(exc.details).replace(broker.settings.token, "[REDACTED]"),
        )
        raise
    state.pending["order_id"] = result.order_id
    save_state(state, PATH)
    result = wait_order(broker, state)
    if result.lots_executed not in {0, 1}:
        raise RuntimeError("Unexpected smoke fill; inspect account.")
    if side == "BUY":
        state.held_lots = result.lots_executed
    else:
        state.held_lots -= result.lots_executed
    state.pending = None
    save_state(state, PATH)
    return result


def run_smoke(broker: SandboxBroker) -> None:
    ensure_sandbox_trading_allowed(broker.settings)
    with state_lock(PATH):
        share = broker.find_share_by_ticker("SBER")
        account = broker.get_or_create_sandbox_account("trading-bot-smoke")
        state = load_state(PATH, account, share.uid)
        if state.pending:
            side = state.pending["side"]
            logger.info(
                "Reconciling previous smoke request %s before any new order",
                state.pending["request_id"],
            )
            result = wait_order(broker, state)
            if result.lots_executed not in {0, 1}:
                raise RuntimeError("Unexpected recovered fill; inspect account.")
            if side == "BUY":
                state.held_lots = result.lots_executed
            else:
                state.held_lots -= result.lots_executed
            state.pending = None
            save_state(state, PATH)
            if not state.held_lots:
                logger.info(
                    "Previous smoke request resolved; account flat. No new order this run."
                )
                return
        positions = broker.get_sandbox_positions(account)
        held = sum(
            p.balance for p in positions.securities if p.instrument_uid == share.uid
        )
        if held != state.held_lots * share.lot or any(
            p.blocked for p in positions.securities
        ):
            raise RuntimeError("Smoke account position mismatch; no new order.")
        if broker.client.sandbox.get_sandbox_orders(account_id=account).orders:
            raise RuntimeError("Smoke account has active orders; no new order.")
        before = quotation_to_float(
            broker.get_sandbox_portfolio(account).total_amount_portfolio
        )
        status = broker.trading_status(share)
        logger.info(
            "Smoke account=%s; equity before=%.4f RUB; lot=%s; market status=%s",
            account,
            before,
            share.lot,
            status.trading_status.name,
        )
        # Explicit diagnostic command: probe server even when status disallows orders.
        # The automatic strategy retains its market-status and freshness guards.
        if state.held_lots == 0:
            price, _ = broker.last_price(share)
            cash = sum(
                quotation_to_float(m) for m in positions.money if m.currency == "rub"
            )
            blocked = sum(
                quotation_to_float(m) for m in positions.blocked if m.currency == "rub"
            )
            if price * share.lot * 1.02 > min(cash - blocked, before * 0.05):
                raise RuntimeError(
                    "Smoke one-lot budget exceeds available cash or 5% account cap."
                )
            buy = send(broker, state, share, "BUY")
            if not buy.lots_executed:
                logger.warning("Smoke BUY not executed; no SELL sent.")
                return
        sell = send(broker, state, share, "SELL")
        after = quotation_to_float(
            broker.get_sandbox_portfolio(account).total_amount_portfolio
        )
        logger.info(
            "Smoke result: sold=%s; remaining lots=%s; equity after=%.4f; change=%.4f RUB",
            sell.lots_executed,
            state.held_lots,
            after,
            after - before,
        )
