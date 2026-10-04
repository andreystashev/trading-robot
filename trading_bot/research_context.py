"""Read-only, reproducible comparison of fixed external-filter hypotheses."""

import argparse
import hashlib
import html
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import re

# Set before importing the SDK or any module that imports gRPC.
if sys.platform == "darwin":
    os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

from backtest import download, report_html, simulate
from config import load_settings
from log_setup import configure_logging
from market_context import market_filter
from research_io import atomic_text, read_bars, read_experiment, write_bars, write_json
from strategy import StrategyParams
from t_invest_client import SandboxBroker

ROOT = Path(__file__).parent / "reports"
VARIANTS = (
    ("Исходная", StrategyParams()),
    ("MA с трендом", StrategyParams(20, 60, 120)),
    ("Пробой", StrategyParams(30, 120, 120, kind="breakout")),
)
COST_SCENARIOS = (5, 10)


def load_reference(output: Path, ticker: str, start: datetime, end: datetime):
    """Cache by ticker and exact requested range, not approximate candle coverage."""
    identity = f"{ticker}:{start.isoformat()}:{end.isoformat()}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    cache_dir = output / "cache"
    cache_dir.mkdir(exist_ok=True)
    cache = cache_dir / f"{ticker}_{digest}.csv"
    if cache.exists():
        return read_bars(cache)
    with SandboxBroker(load_settings()) as broker:
        share = broker.find_share_by_ticker(ticker)
        reference = download(broker, share.uid, start, end)
    if not reference:
        raise ValueError(f"No reference candles for {ticker}.")
    write_bars(cache, reference)
    return read_bars(cache)


def comparison_html(rows: list[dict], ticker: str) -> str:
    table = []
    for row in rows:
        summary = row["summary"]
        values = [
            row["period"],
            row["strategy"],
            row["context"] or "Нет",
            row["cost_bps"],
            round(summary["net_pnl"], 2),
            summary["closed_trades"],
            round(summary["fees"], 2),
        ]
        table.append(
            "<tr>"
            + "".join(f"<td>{html.escape(str(value))}</td>" for value in values)
            + "</tr>"
        )
    return f"""<!doctype html><meta charset="utf-8"><title>Контекст рынка</title>
<style>body{{font:15px system-ui;background:#101319;color:#e9edf5;margin:30px}}td,th{{padding:12px;border-bottom:1px solid #444}}table{{border-collapse:collapse}}</style>
<h1>Проверка внешнего фильтра</h1><p>{html.escape(ticker)} — отдельная акция, не индекс рынка и не доказанный опережающий сигнал.
Вход разрешён выше средней 120 свечей и при росте за 4 свечи. Нет свежих данных — нет входа. Продажи не блокируются.</p>
<p>Фиксированные варианты; просмотренные периоды не являются слепым тестом. Комиссия и проскальзывание — каждый по указанному числу bps на каждую сторону. Новый капитал в каждом периоде, 1 лот.</p>
<table><tr><th>Период</th><th>Стратегия</th><th>Фильтр</th><th>Затраты bps</th><th>Чистый результат ₽</th><th>Сделки</th><th>Комиссия ₽</th></tr>{''.join(table)}</table>"""


def run(folders: list[str], reference_ticker: str = "LKOH") -> Path:
    ticker = reference_ticker.upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", ticker):
        raise ValueError("Reference must be an exchange ticker, e.g. LKOH.")
    if not folders:
        raise ValueError("At least one saved period is required.")
    experiments = [read_experiment(ROOT / name) for name in folders]
    start = min(bars[0].time for _, bars in experiments)
    end = max(bars[-1].time for _, bars in experiments) + timedelta(minutes=15)
    output = ROOT / "market_context"
    output.mkdir(exist_ok=True)
    reference = load_reference(output, ticker, start, end)
    # Separate runs cannot overwrite each other's per-period artifacts.
    run_dir = output / (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    )
    run_dir.mkdir()
    rows = []
    for index, (original, bars) in enumerate(experiments):
        context = market_filter(bars, reference)
        parameters = original["parameters"]
        for label, strategy in VARIANTS:
            for enabled in (False, True):
                for cost in COST_SCENARIOS:
                    result = simulate(
                        bars,
                        start=datetime.fromisoformat(parameters["start"]),
                        lot=parameters["lot"],
                        initial=original["summary"]["initial"],
                        params=strategy,
                        fee_bps=cost,
                        slippage_bps=cost,
                        entry_filter=context if enabled else None,
                    )
                    row = {
                        "period": folders[index],
                        "strategy": label,
                        "params": asdict(strategy),
                        "context": ticker if enabled else None,
                        "cost_bps": cost,
                        "summary": result["summary"],
                    }
                    rows.append(row)
                    folder = (
                        run_dir
                        / f"p{index}_{strategy.kind}_{strategy.fast}_{enabled}_{cost}"
                    )
                    folder.mkdir()
                    result["research"] = {
                        "reference": ticker if enabled else None,
                        "source": folders[index],
                    }
                    write_json(folder / "result.json", result)
                    atomic_text(folder / "report.html", report_html(result))
                    write_bars(folder / "candles.csv", bars)
                    print(
                        folders[index],
                        label,
                        "context",
                        enabled,
                        "cost",
                        cost,
                        "pnl",
                        round(result["summary"]["net_pnl"], 2),
                        "trades",
                        result["summary"]["closed_trades"],
                    )
    comparison = {"reference": ticker, "run_folder": run_dir.name, "rows": rows}
    document = comparison_html(rows, ticker)
    write_json(run_dir / "comparison.json", comparison)
    atomic_text(run_dir / "report.html", document)
    # Publish latest only when every scenario completed successfully.
    write_json(output / "comparison.json", comparison)
    atomic_text(output / "report.html", document)
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+")
    parser.add_argument("--reference", default="LKOH")
    args = parser.parse_args()
    configure_logging()
    print(run(args.folders, args.reference))


if __name__ == "__main__":
    main()
