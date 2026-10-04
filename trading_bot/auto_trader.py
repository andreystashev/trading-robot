"""Sandbox-only MA experiment with durable intent and position reconciliation."""

import json
import logging
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from uuid import uuid4
from zoneinfo import ZoneInfo

from t_tech.invest import (
    CandleInterval,
    InstrumentIdType,
    OrderDirection,
    OrderType,
    Quotation,
)
from t_tech.invest.schemas import OrderIdType, OrderState
from t_tech.invest.exceptions import RequestError
from grpc import StatusCode

from config import ensure_auto_trading_allowed
from strategy import crossover_signal, configured_signal, StrategyParams
from journal import record, database
from t_invest_client import SandboxBroker, ShareInfo, quotation_to_float

logger = logging.getLogger(__name__)
ACCOUNT_NAME = "trading-bot-auto"
STATE_PATH = Path(__file__).with_name(".bot_state.json")
MOSCOW = ZoneInfo("Europe/Moscow")
MAX_POSITION_FRACTION = 0.05
STOP_LOSS_FRACTION = 0.01
MAX_DAILY_LOSS_FRACTION = 0.01
MAX_DAILY_ENTRIES = 2
MAX_DAILY_ATTEMPTS = 4
MAX_QUOTE_AGE_SECONDS = 120
TERMINAL = {
    "EXECUTION_REPORT_STATUS_FILL",
    "EXECUTION_REPORT_STATUS_REJECTED",
    "EXECUTION_REPORT_STATUS_CANCELLED",
}


@dataclass
class BotState:
    account_id: str
    instrument_uid: str
    day: str = ""
    day_equity: float = 0.0
    entries: int = 0
    attempts: int = 0
    halted: bool = False
    last_bar: str = ""
    held_lots: int = 0
    entry_price: float = 0.0
    pending: dict[str, str] | None = None


def save_state(state: BotState, path: Path) -> None:
    """Write intent before network submission; replace atomically on same volume."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(asdict(state), file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def load_state(path: Path, account_id: str, uid: str) -> BotState:
    if not path.exists():
        return BotState(account_id, uid)
    try:
        state = BotState(**json.loads(path.read_text(encoding="utf-8")))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Bot state is damaged; inspect it before continuing."
        ) from exc
    if state.account_id != account_id or state.instrument_uid != uid:
        raise RuntimeError(
            "Bot state belongs to another account/instrument. Do not delete "
            "it before checking old positions and pending orders."
        )
    if (
        type(state.held_lots) is not int
        or state.held_lots not in {0, 1}
        or type(state.entries) is not int
        or state.entries < 0
        or type(state.attempts) is not int
        or state.attempts < 0
        or type(state.halted) is not bool
        or not all(
            type(value) in {int, float} and math.isfinite(value) and value >= 0
            for value in [state.entry_price, state.day_equity]
        )
    ):
        raise RuntimeError("Invalid bot state; manual reconciliation required.")
    if state.pending is not None:
        try:
            pending = state.pending
            if (
                not isinstance(pending, dict)
                or pending.get("side") not in {"BUY", "SELL"}
                or not isinstance(pending.get("request_id"), str)
                or not pending["request_id"]
                or datetime.fromisoformat(pending["created"]).tzinfo is None
            ):
                raise ValueError("Invalid pending intent")
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(
                "Pending intent is damaged; inspect broker state before continuing."
            ) from exc
    return state


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """OS lock released on process exit, including crashes."""
    with path.with_suffix(".lock").open("a+b") as file:
        if os.name == "nt":
            import msvcrt

            file.seek(0)
            file.write(b"0")
            file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another bot process holds the state lock.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another bot process holds the state lock.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)


def order_state(broker: SandboxBroker, state: BotState) -> OrderState:
    pending = state.pending
    if pending is None:
        raise RuntimeError("No pending intent.")
    return broker.client.sandbox.get_sandbox_order_state(
        account_id=state.account_id,
        order_id=pending.get("order_id") or pending["request_id"],
        order_id_type=(
            OrderIdType.ORDER_ID_TYPE_EXCHANGE
            if pending.get("order_id")
            else OrderIdType.ORDER_ID_TYPE_REQUEST
        ),
    )


def reconcile_pending(
    broker: SandboxBroker, state: BotState, path: Path, execute: bool, now: datetime
) -> bool:
    """Resolve saved intent by ID; never resubmit after ambiguous failure."""
    if state.pending is None:
        return True
    result = order_state(broker, state)
    status = result.execution_report_status.name
    record(
        "ORDER_STATUS",
        {
            "status": status,
            "order_id": result.order_id,
            "filled_lots": result.lots_executed,
            "requested_lots": result.lots_requested,
            "commission": (
                quotation_to_float(result.executed_commission)
                if status in TERMINAL
                else None
            ),
            "total_order_amount": (
                quotation_to_float(result.total_order_amount)
                if status in TERMINAL
                else None
            ),
            "side": state.pending["side"],
            "account_id": state.account_id,
        },
        path=database(path),
        plan_id=state.pending.get("plan_id"),
        request_id=state.pending["request_id"],
    )
    logger.info(
        "Pending %s: %s, filled %s/%s",
        result.order_id,
        status,
        result.lots_executed,
        result.lots_requested,
    )
    if status not in TERMINAL:
        age = (now - datetime.fromisoformat(state.pending["created"])).total_seconds()
        if execute and age >= 60:
            ensure_auto_trading_allowed(broker.settings)
            broker.client.sandbox.cancel_sandbox_order(
                account_id=state.account_id,
                order_id=result.order_id,
            )
            logger.info("Requested cancellation; will recheck next tick.")
        return False
    logger.info(
        "Final order accounting: id=%s side=%s lots=%s commission=%.6f %s "
        "total=%.6f %s",
        result.order_id,
        state.pending["side"],
        result.lots_executed,
        quotation_to_float(result.executed_commission),
        result.executed_commission.currency,
        quotation_to_float(result.total_order_amount),
        result.total_order_amount.currency,
    )
    filled = result.lots_executed
    if filled not in {0, 1}:
        raise RuntimeError(
            "Unexpected fill quantity; stop and inspect sandbox account."
        )
    if filled:
        if state.pending["side"] == "BUY":
            # Portfolio average cost is not reliable in Sandbox: use order fills.
            stages = result.stages
            quantity = sum(stage.quantity for stage in stages)
            if not quantity or any(
                type(s.quantity) is not int
                or s.quantity <= 0
                or not math.isfinite(quotation_to_float(s.price))
                or quotation_to_float(s.price) <= 0
                for s in stages
            ):
                raise RuntimeError(
                    "Fill prices unavailable; pending intent preserved for inspection."
                )
            state.entry_price = (
                sum(quotation_to_float(s.price) * s.quantity for s in stages) / quantity
            )
            state.held_lots = filled
        else:
            if filled > state.held_lots:
                raise RuntimeError(
                    "Sell fill exceeds robot position; manual reconciliation required."
                )
            state.held_lots -= filled
            if state.held_lots < 0:
                raise RuntimeError(
                    "Unexpected sell fill; manual reconciliation required."
                )
            if state.held_lots == 0:
                state.entry_price = 0.0
    state.pending = None
    save_state(state, path)
    return True


def select_action(
    prices: list[float],
    price: float,
    state: BotState,
    params: StrategyParams | None = None,
) -> tuple[str, str]:
    params = params or StrategyParams()
    signal = configured_signal(prices, params)
    if state.held_lots:
        if price <= state.entry_price * (1 - params.stop_pct / 100):
            return "SELL", "software stop loss"
        if state.halted:
            return "SELL", "daily loss limit"
        if signal == "SELL":
            return "SELL", f"{params.kind} exit signal"
    elif not state.halted and signal == "BUY":
        return "BUY", f"{params.kind} entry signal"
    return "HOLD", "no actionable crossing"


def submit_intent(
    broker: SandboxBroker,
    state: BotState,
    share: ShareInfo,
    side: str,
    now: datetime,
    path: Path,
    reference_price: float | None = None,
    plan_id: str | None = None,
) -> None:
    ensure_auto_trading_allowed(broker.settings)
    if (
        share.ticker != broker.settings.ticker
        or share.class_code != "TQBR"
        or share.currency.lower() != "rub"
    ):
        raise RuntimeError("Automatic trading requires the selected RUB share on TQBR.")
    if side not in {"BUY", "SELL"} or state.pending is not None:
        raise RuntimeError("Invalid action or unresolved order intent.")
    if side == "BUY" and state.held_lots != 0:
        raise RuntimeError("Pyramiding is disabled.")
    if side == "SELL" and state.held_lots != 1:
        raise RuntimeError("Short selling is disabled.")
    limit_price = None
    if side == "BUY":
        if (
            reference_price is None
            or not math.isfinite(reference_price)
            or reference_price <= 0
        ):
            raise RuntimeError("A valid buy reference price is required.")
        instrument = broker.client.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
            id=share.uid,
        ).instrument
        increment = instrument.min_price_increment
        step = Decimal(increment.units) + Decimal(increment.nano) / Decimal(
            1_000_000_000
        )
        if step <= 0:
            raise RuntimeError("Missing minimum price increment.")
        # A limit caps purchase price; the cash check reserves a further fee buffer.
        cap = Decimal(str(reference_price)) * Decimal("1.005")
        cap = (cap / step).to_integral_value(rounding=ROUND_DOWN) * step
        units = int(cap)
        limit_price = Quotation(units=units, nano=int((cap - units) * 1_000_000_000))
    state.attempts += 1
    if side == "BUY":
        state.entries += 1
    state.pending = {
        "request_id": str(uuid4()),
        "side": side,
        "created": now.isoformat(),
    }
    if plan_id:
        state.pending["plan_id"] = plan_id
    save_state(state, path)
    record(
        "ORDER_PREPARED",
        {
            "side": side,
            "lots": 1,
            "account_id": state.account_id,
            "instrument_uid": share.uid,
            "reference_price": reference_price,
        },
        path=database(path),
        plan_id=plan_id,
        request_id=state.pending["request_id"],
    )
    logger.info(
        "ONE sandbox %s 1 lot; request UUID %s", side, state.pending["request_id"]
    )
    result = broker.client.sandbox.post_sandbox_order(
        account_id=state.account_id,
        instrument_id=share.uid,
        quantity=1,
        direction=(
            OrderDirection.ORDER_DIRECTION_BUY
            if side == "BUY"
            else OrderDirection.ORDER_DIRECTION_SELL
        ),
        order_type=(
            OrderType.ORDER_TYPE_LIMIT if side == "BUY" else OrderType.ORDER_TYPE_MARKET
        ),
        price=limit_price,
        order_id=state.pending["request_id"],
    )
    state.pending["order_id"] = result.order_id
    save_state(state, path)
    record(
        "ORDER_SENT",
        {"order_id": result.order_id, "status": result.execution_report_status.name},
        path=database(path),
        plan_id=plan_id,
        request_id=state.pending["request_id"],
    )
    logger.info(
        "Order %s: %s, filled %s/%s",
        result.order_id,
        result.execution_report_status.name,
        result.lots_executed,
        result.lots_requested,
    )
    # Persisted intent remains until GetSandboxOrderState supplies final fill data.


def _tick(
    broker: SandboxBroker,
    share: ShareInfo,
    state: BotState,
    path: Path,
    execute: bool,
    plan_id: str,
    params: StrategyParams | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    if not reconcile_pending(broker, state, path, execute, now):
        return "HOLD: pending order"
    active = broker.client.sandbox.get_sandbox_orders(
        account_id=state.account_id
    ).orders
    if active:
        raise RuntimeError(
            "Untracked active orders in auto account; inspect before continuing."
        )
    positions = broker.get_sandbox_positions(state.account_id)
    securities = [p for p in positions.securities if p.instrument_uid == share.uid]
    if any(
        p.instrument_uid != share.uid and (p.balance or p.blocked)
        for p in positions.securities
    ):
        raise RuntimeError("Foreign securities found in dedicated auto account.")
    shares = sum(p.balance for p in securities)
    if any(p.blocked for p in securities) or shares != state.held_lots * share.lot:
        raise RuntimeError("Position differs from saved bot state; no orders sent.")
    if state.held_lots and state.entry_price <= 0:
        raise RuntimeError(
            "Missing actual entry price; manual reconciliation required."
        )
    portfolio = broker.get_sandbox_portfolio(state.account_id)
    total = portfolio.total_amount_portfolio
    equity = quotation_to_float(total)
    if total.currency.lower() != "rub" or not math.isfinite(equity) or equity <= 0:
        raise RuntimeError("Invalid RUB portfolio valuation.")
    day = now.astimezone(MOSCOW).date().isoformat()
    if state.day != day:
        state.day, state.day_equity = day, equity
        state.entries = state.attempts = 0
        state.halted = False
    if equity <= state.day_equity * (1 - MAX_DAILY_LOSS_FRACTION):
        state.halted = True
    save_state(state, path)
    logger.info(
        "Account snapshot: equity=%.2f RUB; lots=%s; entry=%.4f; "
        "entries=%s/%s; attempts=%s/%s; halted=%s",
        equity,
        state.held_lots,
        state.entry_price,
        state.entries,
        MAX_DAILY_ENTRIES,
        state.attempts,
        MAX_DAILY_ATTEMPTS,
        state.halted,
    )
    market = broker.trading_status(share)
    if not (market.api_trade_available_flag and market.market_order_available_flag):
        return (
            f"HOLD: market orders unavailable; status={market.trading_status.name}; "
            f"api={market.api_trade_available_flag}; "
            f"market={market.market_order_available_flag}; limit={market.limit_order_available_flag}"
        )
    price, timestamp = broker.last_price(share)
    age = (now - timestamp).total_seconds()
    if not math.isfinite(price) or price <= 0 or not 0 <= age <= MAX_QUOTE_AGE_SECONDS:
        return f"HOLD: stale/invalid last price; quote={timestamp.isoformat()}; age={age:.1f}s"
    candles = broker.client.market_data.get_candles(
        instrument_id=share.uid,
        from_=now - timedelta(days=10),
        to=now,
        interval=CandleInterval.CANDLE_INTERVAL_15_MIN,
    ).candles
    completed = sorted((c for c in candles if c.is_complete), key=lambda c: c.time)
    fresh = bool(completed and 0 <= (now - completed[-1].time).total_seconds() <= 1800)
    prices = [quotation_to_float(c.close) for c in completed] if fresh else []
    if any(not math.isfinite(p) or p <= 0 for p in prices):
        return "HOLD: invalid candle data"
    strategy = params or StrategyParams()
    required = max(strategy.slow + 1, strategy.trend)
    logger.info(
        "Signal snapshot: strategy=%s; windows=%s/%s; candles=%s/%s; signal=%s",
        strategy.kind,
        strategy.fast,
        strategy.slow,
        len(prices),
        required,
        configured_signal(prices, strategy),
    )
    action, reason = select_action(prices, price, state, params)
    bar = completed[-1].time.isoformat() if completed else ""
    record(
        "SIGNAL",
        {
            "action": action,
            "reason": reason,
            "candle": bar,
            "ticker": share.ticker,
            "reference_price": price,
            "held_lots": state.held_lots,
            "equity": equity,
        },
        path=database(path),
        plan_id=plan_id,
    )
    risk_exit = reason in {"software stop loss", "daily loss limit"}
    if action != "HOLD" and not risk_exit and (not fresh or bar == state.last_bar):
        return "HOLD: no new completed candle"
    if action == "BUY":
        cash = sum(
            quotation_to_float(m)
            for m in positions.money
            if m.currency.lower() == "rub"
        )
        blocked = sum(
            quotation_to_float(m)
            for m in positions.blocked
            if m.currency.lower() == "rub"
        )
        cost_with_buffer = price * share.lot * 1.01
        if (
            state.entries >= MAX_DAILY_ENTRIES
            or state.attempts >= MAX_DAILY_ATTEMPTS
            or cost_with_buffer > equity * MAX_POSITION_FRACTION
            or cost_with_buffer > cash - blocked
        ):
            return "HOLD: entry risk/cash limit"
    if action == "SELL" and state.attempts >= MAX_DAILY_ATTEMPTS:
        return "HOLD: attempt cap reached; position needs inspection"
    message = f"{action}: {reason}; price={price:.2f}; equity={equity:.2f}; lots={state.held_lots}"
    record(
        "PLAN",
        {
            "action": action,
            "reason": reason,
            "mode": "execute" if execute else "observe",
            "ticker": share.ticker,
            "account_id": state.account_id,
            "candle": bar,
            "reference_price": price,
            "planned_lots": 1 if action != "HOLD" else 0,
            "lot_size": share.lot,
            "notional": price * share.lot if action != "HOLD" else 0,
            "estimated_commission": (
                price * share.lot * 0.0005 if action != "HOLD" else 0
            ),
            "commission_note": "Estimate 5 bps; actual commission comes from broker",
            "equity": equity,
            "held_lots": state.held_lots,
        },
        path=database(path),
        plan_id=plan_id,
    )
    if action != "HOLD" and execute:
        if risk_exit:
            state.halted = True
        state.last_bar = bar
        submit_intent(broker, state, share, action, now, path, price, plan_id=plan_id)
    return message + (" [execute]" if execute else " [dry-run]")


def tick(
    broker: SandboxBroker,
    share: ShareInfo,
    state: BotState,
    path: Path,
    execute: bool,
    params: StrategyParams | None = None,
) -> str:
    plan_id = str(uuid4())
    try:
        result = _tick(broker, share, state, path, execute, plan_id, params)
    except Exception as exc:
        try:
            record(
                "CYCLE_ERROR",
                {"error_type": type(exc).__name__, "ticker": share.ticker},
                path=database(path),
                plan_id=(
                    state.pending.get("plan_id", plan_id) if state.pending else plan_id
                ),
                request_id=state.pending.get("request_id") if state.pending else None,
            )
        except Exception:
            logger.exception(
                "Could not persist cycle error; original error is preserved"
            )
        raise
    record(
        "DECISION",
        {
            "decision": result,
            "ticker": share.ticker,
            "mode": "execute" if execute else "observe",
        },
        path=database(path),
        plan_id=plan_id,
    )
    return result


def run_bot(
    broker: SandboxBroker,
    *,
    execute: bool = False,
    loop: bool = False,
    path: Path = STATE_PATH,
    params: StrategyParams | None = None,
    account_name: str = ACCOUNT_NAME,
) -> None:
    if execute:
        ensure_auto_trading_allowed(broker.settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with state_lock(path):
        share = broker.find_share_by_ticker(broker.settings.ticker)
        account_id = broker.get_or_create_sandbox_account(account_name)
        state = load_state(path, account_id, share.uid)
        logger.info("Auto sandbox account: %s; execute=%s", account_id, execute)
        failures = 0
        while True:
            try:
                logger.info("%s", tick(broker, share, state, path, execute, params))
                failures = 0
            except RequestError as exc:
                if not loop or exc.code not in {
                    StatusCode.UNAVAILABLE,
                    StatusCode.DEADLINE_EXCEEDED,
                    StatusCode.RESOURCE_EXHAUSTED,
                }:
                    raise
                failures += 1
                if failures >= 5:
                    raise
                delay = min(30 * 2 ** (failures - 1), 120)
                logger.warning(
                    "Transient broker error %s; wait %ss, then reconcile saved intent. No order resubmission.",
                    exc.code.name,
                    delay,
                )
                time.sleep(delay)
                continue
            if not loop:
                return
            time.sleep(30)
