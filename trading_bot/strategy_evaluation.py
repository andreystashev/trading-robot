"""Fixed hypotheses, chronological validation, cost stress; no orders or fitting."""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from backtest import simulate
from research_io import read_experiment, write_json, atomic_text
from strategy import StrategyParams

CANDIDATES = {
    "ma_10_30": StrategyParams(),
    "trend_20_60": StrategyParams(20, 60, 120),
    "breakout_30_120": StrategyParams(30, 120, 120, kind="breakout"),
    "reversion_unfiltered": StrategyParams(5, 40, 0, 30, 2, "reversion"),
    "reversion_trend": StrategyParams(5, 40, 200, 30, 2, "reversion"),
    "pullback_rsi2": StrategyParams(2, 5, 200, 30, 2, "pullback"),
}


def evaluate(folder):
    original, bars = read_experiment(folder)
    start = datetime.fromisoformat(original["parameters"]["start"])
    end = bars[-1].time + timedelta(minutes=15)
    if end - start < timedelta(days=180):
        raise ValueError("At least 180 calendar days required")
    split = end - timedelta(days=90)
    base = dict(
        initial=original["summary"]["initial"], lot=original["parameters"]["lot"]
    )
    fee = original["parameters"]["fee_bps_per_side"]
    slip = original["parameters"]["slippage_bps_per_side"]
    rows = []
    for name, params in CANDIDATES.items():

        def run(a, b, multiplier=1):
            return simulate(
                [v for v in bars if v.time < b],
                start=a,
                params=params,
                fee_bps=fee * multiplier,
                slippage_bps=slip * multiplier,
                **base,
            )["summary"]

        rows.append(
            dict(
                name=name,
                params=asdict(params),
                development=run(start, split),
                validation=run(split, end),
                stress=run(split, end, 2),
                quarters=[
                    run(a, min(a + timedelta(days=90), end))
                    for a in [
                        start + timedelta(days=i)
                        for i in range(0, (end - start).days, 90)
                    ]
                ],
            )
        )
    eligible = [
        r
        for r in rows
        if r["development"]["closed_trades"] >= 10 and r["development"]["net_pnl"] > 0
    ]
    selected = (
        max(eligible, key=lambda r: r["development"]["net_pnl"])["name"]
        if eligible
        else None
    )
    result = dict(
        source=str(folder),
        split=split.isoformat(),
        selected_by_development=selected,
        passed_validation=[
            r["name"]
            for r in rows
            if r["development"]["net_pnl"] > 0
            and r["validation"]["net_pnl"] > 0
            and r["stress"]["net_pnl"] > 0
            and r["validation"]["closed_trades"] >= 10
        ],
        assumptions=dict(**base, fee_bps=fee, slippage_bps=slip),
        note="Six hypotheses; RSI2 added after viewing the first five. Last 90 days are validation, not used for parameter selection. This history has been viewed before: not a blind test. Each segment starts flat; raw candles, no dividend adjustments. Costs constant, no financing or cash interest.",
        rows=rows,
    )
    write_json(folder / "strategy_evaluation.json", result)
    lines = [
        "# Сравнение алгоритмов",
        "",
        result["note"],
        "",
        f"Разделение: {split.isoformat()}. Выбор по разработке: {selected}.",
        f"Положительный результат разработки, проверки и стресс-теста: {result['passed_validation'] or 'нет'}. Это предварительный фильтр, не статистическое доказательство.",
        f"Капитал {base['initial']} ₽; акций в лоте {base['lot']} (из исходного отчёта); комиссия {fee} bps и проскальзывание {slip} bps на сторону.",
        "",
        "| Алгоритм | Разработка, ₽ | Проверка 90 дней, ₽ | Удвоенные затраты, ₽ | Сделки проверки | Квартальные результаты, ₽ |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['development']['net_pnl']:.2f} | {r['validation']['net_pnl']:.2f} | {r['stress']['net_pnl']:.2f} | {r['validation']['closed_trades']} | "
            + ", ".join(f"{q['net_pnl']:.2f}" for q in r["quarters"])
            + " |"
        )
    atomic_text(folder / "strategy_evaluation.md", "\n".join(lines) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    result = evaluate(parser.parse_args().folder)
    for r in result["rows"]:
        print(
            r["name"],
            "development",
            round(r["development"]["net_pnl"], 2),
            "validation",
            round(r["validation"]["net_pnl"], 2),
            "stress",
            round(r["stress"]["net_pnl"], 2),
            "trades",
            r["validation"]["closed_trades"],
        )
    print("Selected:", result["selected_by_development"])
