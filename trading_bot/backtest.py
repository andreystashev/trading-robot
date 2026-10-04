"""Read-only historical simulation, with next-bar fills and explicit costs."""

import csv
import html
import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from strategy import StrategyParams, configured_signal
from uuid import uuid4
from zoneinfo import ZoneInfo

from t_tech.invest import CandleInterval
from t_tech.invest.schemas import CandleSource
from auto_trader import (
    BotState,
    MAX_DAILY_ATTEMPTS,
    MAX_DAILY_ENTRIES,
    MAX_DAILY_LOSS_FRACTION,
    MAX_POSITION_FRACTION,
    STOP_LOSS_FRACTION,
    select_action,
    state_lock,
)
from t_invest_client import SandboxBroker, quotation_to_float

ROOT = Path(__file__).parent / "reports"
logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def simulate(
    bars: list[Bar],
    *,
    start: datetime,
    initial: float = 100000,
    lot: int = 1,
    fee_bps: float = 5,
    slippage_bps: float = 5,
    params: StrategyParams | None = None,
    entry_filter: dict[datetime, bool] | None = None,
) -> dict:
    if (
        not math.isfinite(initial)
        or initial <= 0
        or lot < 1
        or not all(math.isfinite(v) and 0 <= v <= 100 for v in [fee_bps, slippage_bps])
        or start.tzinfo is None
    ):
        raise ValueError("Invalid simulation parameters.")
    customized = params is not None
    params = params or StrategyParams()
    history_size = max(params.slow + 1, params.trend)
    previous = None
    for bar in bars:
        if (
            bar.time.tzinfo is None
            or previous is not None
            and bar.time <= previous
            or not all(
                math.isfinite(p) and p > 0
                for p in [bar.open, bar.high, bar.low, bar.close]
            )
            or not bar.low
            <= min(bar.open, bar.close)
            <= max(bar.open, bar.close)
            <= bar.high
        ):
            raise ValueError(
                "Bars must be ordered, unique, timezone-aware with valid OHLC."
            )
        previous = bar.time
    cash = initial
    fees = realized = 0.0
    entry_cost = 0.0
    trades = []
    events = []
    curve = []
    prices = []
    state = BotState("historical", "simulation")
    pending = None
    peak = initial
    max_drawdown = 0.0
    fee, slip = fee_bps / 10000, slippage_bps / 10000

    def fill(side, price, when, reason):
        nonlocal cash, fees, realized, entry_cost
        gross = price * lot
        commission = gross * fee
        fees += commission
        if side == "BUY":
            cash -= gross + commission
            state.held_lots = 1
            state.entry_price = price
            entry_cost = gross + commission
        else:
            cash += gross - commission
            profit = gross - commission - entry_cost
            realized += profit
            trades.append(
                {
                    "entry_cost": entry_cost,
                    "exit_net": gross - commission,
                    "pnl": profit,
                    "exit_time": when.isoformat(),
                    "reason": reason,
                }
            )
            state.held_lots = 0
            state.entry_price = 0.0
            entry_cost = 0.0
        events.append(
            {
                "time": when.isoformat(),
                "action": side,
                "price": price,
                "shares": lot,
                "fee": commission,
                "cash": cash,
                "reason": reason,
            }
        )

    for bar in bars:
        if bar.time < start:
            prices.append(bar.close)
            prices = prices[-history_size:]
            continue
        day = bar.time.astimezone(MOSCOW).date().isoformat()
        equity_open = cash + state.held_lots * lot * bar.open
        if state.day != day:
            state.day, state.day_equity = day, equity_open
            state.entries = state.attempts = 0
            state.halted = False
        if equity_open <= state.day_equity * (1 - MAX_DAILY_LOSS_FRACTION):
            state.halted = True
        if pending:
            side, cap, reason, signal_time = pending
            pending = None
            # Signals may only fill after their candle closes. A buy limit is
            # conservatively modeled at next OPEN, with no assumed intrabar fills.
            if side == "BUY":
                price = bar.open * (1 + slip)
                if (
                    bar.time - signal_time == timedelta(minutes=15)
                    and price <= cap
                    and not state.halted
                    and state.entries < MAX_DAILY_ENTRIES
                    and state.attempts < MAX_DAILY_ATTEMPTS
                    and price * lot * (1 + fee)
                    <= min(cash, equity_open * MAX_POSITION_FRACTION)
                ):
                    state.entries += 1
                    state.attempts += 1
                    fill(side, price, bar.time, reason)
                else:
                    events.append(
                        {
                            "time": bar.time.isoformat(),
                            "action": "SKIP_BUY",
                            "reason": "gap / limit / budget / daily restriction",
                        }
                    )
            elif state.held_lots and state.attempts < MAX_DAILY_ATTEMPTS:
                state.attempts += 1
                fill("SELL", bar.open * (1 - slip), bar.time, reason)
        if state.held_lots:
            threshold = state.entry_price * (1 - params.stop_pct / 100)
            if bar.low <= threshold and state.attempts < MAX_DAILY_ATTEMPTS:
                state.attempts += 1
                # Gap-through-stop exits at open; never assume the stop price
                # can be achieved through a price gap.
                fill(
                    "SELL",
                    min(bar.open, threshold) * (1 - slip),
                    bar.time,
                    "intrabar stop model",
                )
                state.halted = True
        prices.append(bar.close)
        prices = prices[-history_size:]
        equity = cash + state.held_lots * lot * bar.close
        if equity <= state.day_equity * (1 - MAX_DAILY_LOSS_FRACTION):
            state.halted = True
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
        curve.append(
            {
                "time": bar.time.isoformat(),
                "equity": equity,
                "cash": cash,
                "lots": state.held_lots,
                "drawdown_pct": drawdown * 100,
            }
        )
        if customized:
            signal = configured_signal(prices, params)
            if state.held_lots and (state.halted or signal == "SELL"):
                action, reason = "SELL", (
                    "daily limit" if state.halted else f"{params.kind} exit signal"
                )
            elif not state.held_lots and not state.halted and signal == "BUY":
                action, reason = "BUY", f"{params.kind} entry signal"
            else:
                action, reason = "HOLD", "no actionable configured signal"
        else:
            action, reason = select_action(prices, bar.close, state)
        if (
            action == "BUY"
            and entry_filter is not None
            and not entry_filter.get(bar.time, False)
        ):
            action, reason = "HOLD", "external market filter: falling / missing / stale"
        events.append(
            {
                "time": bar.time.isoformat(),
                "action": "SIGNAL_" + action,
                "reason": reason,
                "equity": equity,
                "halted": state.halted,
            }
        )
        if action != "HOLD" and state.attempts < MAX_DAILY_ATTEMPTS:
            if action == "BUY" and state.entries >= MAX_DAILY_ENTRIES:
                continue
            pending = (action, bar.close * 1.005, reason, bar.time)
    if not curve:
        raise RuntimeError("No complete candles in selected test period.")
    final = curve[-1]["equity"]
    unrealized = (
        state.held_lots * lot * bars[-1].close - entry_cost if state.held_lots else 0
    )
    gross_pnl = final - initial + fees
    first_bar = next(b for b in bars if b.time >= start)
    # Comparable benchmark: buy exactly one lot, same costs, hold open at end.
    benchmark_entry = first_bar.open * (1 + slip) * lot
    benchmark = lot * bars[-1].close - benchmark_entry * (1 + fee)
    return {
        "summary": {
            "initial": initial,
            "final_equity": final,
            "net_pnl": final - initial,
            "return_pct": (final / initial - 1) * 100,
            "gross_pnl": gross_pnl,
            "fees": fees,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "max_drawdown_pct": max_drawdown * 100,
            "closed_trades": len(trades),
            "wins": sum(t["pnl"] > 0 for t in trades),
            "win_rate_pct": (
                100 * sum(t["pnl"] > 0 for t in trades) / len(trades) if trades else 0
            ),
            "average_trade_pnl": realized / len(trades) if trades else 0,
            "open_lots": state.held_lots,
            "buy_hold_one_lot_pnl": benchmark,
            "bars": len(curve),
        },
        "parameters": {
            "fee_bps_per_side": fee_bps,
            "slippage_bps_per_side": slippage_bps,
            "lot": lot,
            "start": start.isoformat(),
            "end": bars[-1].time.isoformat(),
            "strategy": asdict(params) if customized else None,
            "entry_filter": (
                {t.isoformat(): v for t, v in entry_filter.items()}
                if entry_filter is not None
                else None
            ),
        },
        "trades": trades,
        "events": events,
        "equity_curve": curve,
    }


def download(
    broker: SandboxBroker, uid: str, start: datetime, end: datetime
) -> list[Bar]:
    by_time = {}
    cursor = start
    while cursor < end:
        until = min(cursor + timedelta(days=3), end)
        response = broker.client.market_data.get_candles(
            instrument_id=uid,
            from_=cursor,
            to=until,
            interval=CandleInterval.CANDLE_INTERVAL_15_MIN,
            candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE,
        )
        for c in response.candles:
            if c.is_complete and start <= c.time < end:
                by_time[c.time] = Bar(
                    c.time,
                    quotation_to_float(c.open),
                    quotation_to_float(c.high),
                    quotation_to_float(c.low),
                    quotation_to_float(c.close),
                    c.volume,
                )
        logger.info(
            "History %s .. %s: %s candles",
            cursor.date(),
            until.date(),
            len(response.candles),
        )
        cursor = until
    return sorted(by_time.values(), key=lambda b: b.time)


def report_html(result: dict) -> str:
    summary = result["summary"]
    strategy = result["parameters"].get("strategy") or asdict(StrategyParams())
    labels = {
        "initial": "Начальный капитал, ₽",
        "final_equity": "Итоговая стоимость счёта, ₽",
        "net_pnl": "Результат после комиссий, ₽",
        "return_pct": "Доходность счёта, %",
        "gross_pnl": "Результат до комиссий (с проскальзыванием), ₽",
        "fees": "Комиссии, ₽",
        "realized_pnl": "Прибыль закрытых сделок, ₽",
        "unrealized_pnl": "Оценка открытой позиции, ₽",
        "max_drawdown_pct": "Максимальная просадка по закрытиям свечей, %",
        "closed_trades": "Закрытые сделки",
        "wins": "Прибыльные сделки",
        "win_rate_pct": "Доля прибыльных сделок, %",
        "average_trade_pnl": "Средняя прибыль сделки, ₽",
        "open_lots": "Открытые лоты в конце",
        "buy_hold_one_lot_pnl": "Купить один лот и держать: результат, ₽",
        "bars": "Свечи тестового периода",
    }
    rows = "".join(
        f"<tr><td>{labels.get(k,k)}</td><td>{v:.6f}</td></tr>"
        for k, v in summary.items()
    )
    curve = result["equity_curve"]
    values = [p["equity"] for p in curve]
    lo, hi = min(values), max(values)
    span = max(hi - lo, 0.01)
    points = " ".join(
        f"{20+960*i/max(1,len(values)-1):.2f},{240-220*(v-lo)/span:.2f}"
        for i, v in enumerate(values)
    )
    trades = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(t[k]))}</td>" for k in ["exit_time", "pnl", "reason"]
        )
        + "</tr>"
        for t in result["trades"]
    )
    ticker = html.escape(str(result.get("instrument", {}).get("ticker", "Инструмент")))
    return f"""<!doctype html><meta charset="utf-8"><title>{ticker} · Исторический тест</title>
<style>body{{font:15px/1.6 system-ui;background:#101319;color:#e9edf5;max-width:1050px;margin:40px auto;padding:20px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;border-bottom:1px solid #303846;padding:8px}}svg{{background:#191e27;width:100%;border-radius:12px}}p{{color:#aeb9cc}}</style>
<h1>{ticker} · Исторический тест {html.escape(strategy.get('kind','ma'))} {strategy['fast']}/{strategy['slow']}</h1><p>Период UTC: {html.escape(result['parameters']['start'])} — {html.escape(result['parameters']['end'])}.<br>Лот: {result['parameters']['lot']} акций. Комиссия: {result['parameters']['fee_bps_per_side']/100:.3f}% и проскальзывание: {result['parameters']['slippage_bps_per_side']/100:.3f}% на каждую сторону.</p>
<p>Симуляция по OHLC, не фактические сделки. Сигнал закрытой свечи исполняется на следующем открытии.
Комиссии и проскальзывание включены. Налоги, дивиденды и абонентская плата не включены.
Программный стоп моделируется внутри свечи: это не гарантия исполнения живого робота.</p>
<p>Параметры теста: {html.escape(strategy.get('kind','ma'))}, окна {strategy['fast']}/{strategy['slow']}; фильтр тренда MA{strategy['trend']} (0 = выключен); минимальный разрыв {strategy['entry_edge_bps']} bps; выход при снижении {strategy['stop_pct']}%.</p><h2>Стоимость счёта</h2><p>Минимум: {lo:.4f} ₽ · максимум: {hi:.4f} ₽. Горизонтальная ось — последовательность свечей.</p>
<svg viewBox="0 0 1000 270" role="img" aria-label="Стоимость счёта"><polyline points="{points}" fill="none" stroke="#65dfb2" stroke-width="2"/></svg>
<h2>Результаты</h2><table>{rows}</table><h2>Закрытые сделки</h2><table><tr><th>Время выхода UTC</th><th>Прибыль после комиссий, ₽</th><th>Причина</th></tr>{trades}</table>
<p>Рядом с отчётом: result.json, events.csv, candles.csv. Открытая позиция оценивается по последнему закрытию, не ликвидируется искусственно.</p>"""


def run_backtest(
    broker: SandboxBroker,
    days: int = 30,
    fee_bps: float = 5,
    slippage_bps: float = 5,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    params: StrategyParams | None = None,
    profile: dict | None = None,
) -> Path:
    if not 1 <= days <= 365:
        raise ValueError("Backtest days must be 1..365.")
    ROOT.mkdir(exist_ok=True)
    with state_lock(ROOT / ".backtest.json"):
        share = broker.find_share_by_ticker(broker.settings.ticker)
        if share.currency.lower() != "rub" or share.lot < 1:
            raise ValueError(
                "Historical tests require a RUB share with a valid lot size."
            )
        start, end = date_range(days, from_date, to_date)
        bars = download(broker, share.uid, start - timedelta(days=10), end)
        result = simulate(
            bars,
            start=start,
            initial=float(broker.settings.sandbox_initial_balance),
            lot=share.lot,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            params=params,
        )
        result["profile"] = profile
        result["instrument"] = {
            "uid": share.uid,
            "ticker": share.ticker,
            "source": "T-Invest Sandbox API, EXCHANGE candles",
        }
        folder = ROOT / (end.strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8])
        folder.mkdir()
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "report.html").write_text(report_html(result), encoding="utf-8")
        with (folder / "events.csv").open("w", newline="", encoding="utf-8") as f:
            fields = [
                "time",
                "action",
                "reason",
                "price",
                "shares",
                "fee",
                "cash",
                "equity",
                "halted",
            ]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result["events"])
        with (folder / "candles.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["time", "open", "high", "low", "close", "volume"]
            )
            writer.writeheader()
            writer.writerows([{**asdict(b), "time": b.time.isoformat()} for b in bars])
        tmp = ROOT / "latest.tmp"
        tmp.write_text(
            json.dumps(
                {
                    "folder": folder.name,
                    "summary": result["summary"],
                    "parameters": result["parameters"],
                    "profile": result["profile"],
                    "instrument": result["instrument"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(ROOT / "latest.json")
        logger.info("Backtest result: %s", result["summary"])
        logger.info("Report: %s", folder / "report.html")
        return folder


def replay(folder: Path) -> dict:
    """Repeat a saved experiment without a token or network access."""
    original = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    with (folder / "candles.csv").open(encoding="utf-8", newline="") as f:
        bars = [
            Bar(
                datetime.fromisoformat(row["time"]),
                *(float(row[key]) for key in ["open", "high", "low", "close"]),
                int(row["volume"]),
            )
            for row in csv.DictReader(f)
        ]
    p = original["parameters"]
    result = simulate(
        bars,
        start=datetime.fromisoformat(p["start"]),
        initial=original["summary"]["initial"],
        lot=p["lot"],
        fee_bps=p["fee_bps_per_side"],
        slippage_bps=p["slippage_bps_per_side"],
        params=StrategyParams(**p["strategy"]) if p.get("strategy") else None,
        entry_filter=(
            {datetime.fromisoformat(t): v for t, v in p["entry_filter"].items()}
            if p.get("entry_filter") is not None
            else None
        ),
    )
    if result["summary"] != original["summary"]:
        raise RuntimeError(
            "Replay differs from saved result; inspect data/code/parameters."
        )
    logger.info("Offline replay matches: %s", result["summary"])
    return result


def date_range(
    days: int, from_date: str | None = None, to_date: str | None = None
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if bool(from_date) != bool(to_date):
        raise ValueError("Specify both start and end dates.")
    if from_date:
        start = (
            datetime.strptime(from_date, "%Y-%m-%d")
            .replace(tzinfo=MOSCOW)
            .astimezone(timezone.utc)
        )
        end = (
            datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=MOSCOW)
            + timedelta(days=1)
        ).astimezone(timezone.utc)
        if (
            datetime.strptime(to_date, "%Y-%m-%d").date()
            > now.astimezone(MOSCOW).date()
        ):
            raise ValueError("End date cannot be in the future.")
        if start >= end or start >= now:
            raise ValueError("Invalid historical date range.")
        end = min(end, now)
        if end - start > timedelta(days=365):
            raise ValueError("Maximum historical range is 365 days.")
    else:
        if not 1 <= days <= 365:
            raise ValueError("Days must be 1..365.")
        end = now
        start = end - timedelta(days=days)
    return start, end
