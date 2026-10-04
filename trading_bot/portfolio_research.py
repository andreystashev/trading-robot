"""Daily long/cash momentum experiment. Read-only: never submits orders.

Not a deployable trading profile. Dividends/interest/taxes are excluded and the
present-day universe has selection bias; results are exploratory price returns.
"""

import os
import sys

if sys.platform == "darwin":
    os.environ.setdefault("GRPC_DNS_RESOLVER", "native")
import argparse
import html
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from uuid import uuid4
from zoneinfo import ZoneInfo
from backtest import Bar
from config import load_settings
from log_setup import configure_logging
from research_io import read_bars, write_bars, write_json, atomic_text
from t_invest_client import SandboxBroker, quotation_to_float
from t_tech.invest import CandleInterval
from t_tech.invest.schemas import CandleSource

ROOT = Path(__file__).parent / "reports" / "daily_portfolio"
MSK = ZoneInfo("Europe/Moscow")


def targets(history, *, lookback=63, trend=200, top=2):
    """Fixed rank using only completed previous-day closes, no fitting."""
    ranked = []
    for ticker, prices in history.items():
        if len(prices) < max(trend, lookback + 1):
            continue
        score = prices[-1] / prices[-lookback - 1] - 1
        if score > 0 and prices[-1] > mean(prices[-trend:]):
            ranked.append((score, ticker))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return {ticker: 0.05 for _, ticker in ranked[:top]}


def simulate_portfolio(
    data, lots, start, *, initial=100000, fee_bps=5, slip_bps=5, passive=False
):
    if not data or set(data) != set(lots) or start.tzinfo is None:
        raise ValueError("Missing universe, lots or timezone")
    if (
        not math.isfinite(initial)
        or initial <= 0
        or any(type(v) is not int or v < 1 for v in lots.values())
        or not all(math.isfinite(v) and 0 <= v <= 100 for v in [fee_bps, slip_bps])
    ):
        raise ValueError("Invalid capital, lot or costs")
    indexed = {}
    for ticker, bars in data.items():
        previous = None
        indexed[ticker] = {}
        for b in bars:
            if (
                b.time.tzinfo is None
                or previous is not None
                and b.time <= previous
                or not all(
                    math.isfinite(v) and v > 0 for v in [b.open, b.high, b.low, b.close]
                )
                or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
            ):
                raise ValueError("Invalid daily OHLC or ordering")
            previous = b.time
            day = b.time.astimezone(MSK).date()
            if day in indexed[ticker]:
                raise ValueError("Duplicate daily candle")
            indexed[ticker][day] = b
    days = sorted(set.intersection(*(set(v) for v in indexed.values())))
    history = {t: [] for t in data}
    held = {t: 0 for t in data}
    cash = initial
    fees = turnover = 0.0
    peak = initial
    dd = 0.0
    events = []
    curve = []
    week = None

    def trade(ticker, quantity, price, day):
        nonlocal cash, fees, turnover
        fill = price * (1 + slip_bps / 10000 if quantity > 0 else 1 - slip_bps / 10000)
        notional = abs(quantity) * fill
        commission = notional * fee_bps / 10000
        if quantity > 0 and notional + commission > cash + 1e-8:
            raise ValueError("Insufficient modeled cash")
        cash -= quantity * fill + commission
        held[ticker] += quantity
        fees += commission
        turnover += notional
        events.append(
            dict(
                date=day.isoformat(),
                ticker=ticker,
                shares=quantity,
                price=fill,
                fee=commission,
            )
        )

    for day in days:
        today = {t: indexed[t][day] for t in data}
        if day >= start.astimezone(MSK).date():
            current_week = day.isocalendar()[:2]
            if week != current_week and not (passive and curve):
                equity_open = cash + sum(held[t] * today[t].open for t in data)
                weights = (
                    {t: 0.10 / len(data) for t in data} if passive else targets(history)
                )
                desired = {
                    t: math.floor(
                        equity_open
                        * weights.get(t, 0)
                        / (
                            today[t].open
                            * (1 + slip_bps / 10000)
                            * (1 + fee_bps / 10000)
                            * lots[t]
                        )
                    )
                    * lots[t]
                    for t in data
                }
                for t in sorted(data):
                    if desired[t] < held[t]:
                        trade(t, desired[t] - held[t], today[t].open, day)
                for t in sorted(data):
                    if desired[t] > held[t]:
                        trade(t, desired[t] - held[t], today[t].open, day)
            week = current_week
            equity = cash + sum(held[t] * today[t].close for t in data)
            peak = max(peak, equity)
            dd = max(dd, (peak - equity) / peak * 100)
            curve.append(
                dict(
                    date=day.isoformat(), equity=equity, cash=cash, positions=dict(held)
                )
            )
        for t in data:
            history[t].append(today[t].close)
    if not curve:
        raise ValueError("No common test days")
    return dict(
        summary=dict(
            net_pnl=curve[-1]["equity"] - initial,
            return_pct=(curve[-1]["equity"] / initial - 1) * 100,
            max_drawdown_pct=dd,
            fees=fees,
            orders=len(events),
            turnover=turnover,
            days=len(curve),
            final_equity=curve[-1]["equity"],
        ),
        events=events,
        curve=curve,
    )


def load_daily(broker, ticker, start, end):
    share = broker.find_share_by_ticker(ticker)
    if share.currency.lower() != "rub":
        raise ValueError("RUB shares only")
    key = f"{ticker}_{start.date()}_{end.date()}"
    path = ROOT / "cache" / (key + ".csv")
    metadata = path.with_suffix(".json")
    if path.exists() and metadata.exists():
        import json

        meta = json.loads(metadata.read_text())
        if meta["uid"] == share.uid and meta["lot"] == share.lot:
            return read_bars(path), share.lot
    bars = {}
    cursor = start
    while cursor < end:
        until = min(cursor + timedelta(days=300), end)
        response = broker.client.market_data.get_candles(
            instrument_id=share.uid,
            from_=cursor,
            to=until,
            interval=CandleInterval.CANDLE_INTERVAL_DAY,
            candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE,
        )
        for c in response.candles:
            if c.is_complete and start <= c.time < end:
                bars[c.time] = Bar(
                    c.time,
                    *[
                        quotation_to_float(getattr(c, k))
                        for k in ["open", "high", "low", "close"]
                    ],
                    c.volume,
                )
        cursor = until
    if not bars:
        raise ValueError("No daily history: " + ticker)
    values = sorted(bars.values(), key=lambda b: b.time)
    write_bars(path, values)
    write_json(
        metadata,
        dict(
            ticker=ticker,
            uid=share.uid,
            lot=share.lot,
            interval="DAY",
            source="EXCHANGE",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    return values, share.lot


def run(tickers):
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=365)
    warmup = start - timedelta(days=400)
    split = end - timedelta(days=90)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "cache").mkdir(exist_ok=True)
    folder = ROOT / (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    )
    folder.mkdir()
    data = {}
    lots = {}
    with SandboxBroker(load_settings()) as broker:
        for ticker in tickers:
            data[ticker], lots[ticker] = load_daily(broker, ticker, warmup, end)
            write_bars(folder / (ticker + ".csv"), data[ticker])
    results = []
    for period, a, b in [("development", start, split), ("validation", split, end)]:
        subset = {t: [v for v in bars if v.time < b] for t, bars in data.items()}
        for label, passive, mult in [
            ("momentum", False, 1),
            ("momentum_stress", False, 2),
            ("passive", True, 1),
        ]:
            result = simulate_portfolio(
                subset, lots, a, passive=passive, fee_bps=5 * mult, slip_bps=5 * mult
            )
            result.update(period=period, strategy=label)
            results.append(result)
    output = dict(
        universe=tickers,
        lots=lots,
        start=start.isoformat(),
        split=split.isoformat(),
        end=end.isoformat(),
        rules="63-day positive momentum, above SMA200, top 2; weekly next-open rebalance; maximum 5% per asset, 10% total; no leverage or shorts",
        limitations="Current-day universe selection, one year, raw exchange daily candles; dividends, corporate actions, taxes, cash interest, order book and partial fills excluded. Not a blind test or production strategy. Each segment starts flat. Passive benchmark allocates 10% equally across the same universe and holds; no terminal forced liquidation.",
        results=results,
    )
    write_json(folder / "result.json", output)
    rows = "".join(
        "<tr>"
        + "".join(
            "<td>" + html.escape(str(v)) + "</td>"
            for v in [
                r["period"],
                r["strategy"],
                round(r["summary"]["net_pnl"], 2),
                round(r["summary"]["max_drawdown_pct"], 3),
                r["summary"]["orders"],
                round(r["summary"]["fees"], 2),
            ]
        )
        + "</tr>"
        for r in results
    )
    atomic_text(
        folder / "report.html",
        '<!doctype html><meta charset="utf-8"><title>Дневной momentum</title><style>body{font:16px system-ui;background:#101319;color:#e9edf5;margin:30px}td,th{padding:12px;border-bottom:1px solid #444}</style><h1>Дневной портфель: '
        + html.escape(", ".join(tickers))
        + "</h1><p>"
        + html.escape(output["rules"])
        + "</p><p>"
        + html.escape(output["limitations"])
        + "</p><p>Капитал 100000 ₽. Комиссия и проскальзывание по 5 bps на сторону; стресс по 10 bps.</p><table><tr><th>Период</th><th>Алгоритм</th><th>Результат ₽</th><th>Просадка %</th><th>Операции</th><th>Комиссия ₽</th></tr>"
        + rows
        + "</table>",
    )
    for r in results:
        print(r["period"], r["strategy"], r["summary"])
    print("Report:", folder / "report.html")
    return folder


if __name__ == "__main__":
    import re

    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="+", default=["SBER", "LKOH", "GAZP"])
    args = parser.parse_args()
    tickers = [t.strip().upper() for t in args.tickers]
    if (
        not 2 <= len(tickers) <= 10
        or len(set(tickers)) != len(tickers)
        or any(not re.fullmatch(r"[A-Z0-9]{1,12}", t) for t in tickers)
    ):
        parser.error("2..10 distinct exchange tickers required")
    configure_logging()
    run(tickers)
