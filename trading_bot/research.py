"""Small fixed comparison, selection on the earlier period only."""

import html
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from backtest import simulate
from research_io import read_experiment, write_json, atomic_text
from strategy import StrategyParams


def compare(folder: Path) -> dict:
    original, bars = read_experiment(folder)
    start = datetime.fromisoformat(original["parameters"]["start"])
    end = bars[-1].time
    if end - start < timedelta(days=60):
        raise ValueError("Comparison needs at least 60 calendar days.")
    split = end - timedelta(days=30)
    variants = [
        StrategyParams(),
        StrategyParams(20, 60),
        StrategyParams(30, 90),
        StrategyParams(10, 60, 120),
        StrategyParams(20, 60, 120),
        StrategyParams(20, 60, 120, 5),
        StrategyParams(20, 60, 120, 0, 2),
        StrategyParams(30, 90, 120, 0, 2),
        StrategyParams(30, 120, 120, kind="breakout"),
    ]
    rows = []
    for p in variants:
        kwargs = {
            "lot": original["parameters"]["lot"],
            "initial": original["summary"]["initial"],
            "fee_bps": original["parameters"]["fee_bps_per_side"],
            "slippage_bps": original["parameters"]["slippage_bps_per_side"],
            "params": p,
        }
        train = simulate([b for b in bars if b.time < split], start=start, **kwargs)[
            "summary"
        ]
        check = simulate(bars, start=split, **kwargs)["summary"]
        rows.append({"params": asdict(p), "earlier": train, "later": check})
    candidates = [r for r in rows if r["earlier"]["closed_trades"] >= 5]
    selected = (
        max(candidates, key=lambda r: r["earlier"]["net_pnl"]) if candidates else None
    )
    result = {
        "selection": "Best earlier net pnl, minimum five closed trades; later results not used for selection",
        "split": split.isoformat(),
        "source_folder": str(folder),
        "selected_params": selected["params"] if selected else None,
        "variants": rows,
    }
    write_json(folder / "comparison.json", result)
    table = ""
    for r in rows:
        table += (
            "<tr><td>"
            + html.escape(str(r["params"]))
            + "</td>"
            + "".join(
                f"<td>{r[part]['net_pnl']:.2f} ₽ / {r[part]['closed_trades']} сделок</td>"
                for part in ["earlier", "later"]
            )
            + "</tr>"
        )
    atomic_text(
        folder / "comparison.html",
        """<!doctype html><meta charset="utf-8"><title>Сравнение стратегии</title>
<style>body{font:15px/1.6 system-ui;margin:40px;background:#101319;color:#e9edf5}td,th{padding:12px;border-bottom:1px solid #303846;text-align:left}table{border-collapse:collapse}</style>
<h1>Сравнение фиксированных вариантов MA и пробоя</h1><p>Выбор по раннему периоду, минимум 5 закрытых сделок. Последние 30 дней проверяются отдельно с новым капиталом и без переноса позиций. Это ограниченная проверка, не доказательство прибыльности.</p>
<p>Исходная стратегия на последних 30 днях уже изучалась ранее; этот период нельзя считать совершенно новым слепым тестом.</p>
<p>Выбранные по раннему периоду параметры: """
        + html.escape(str(result["selected_params"]))
        + """</p>
<table><tr><th>Параметры</th><th>Ранний период: результат / сделки</th><th>Последние 30 дней: результат / сделки</th></tr>"""
        + table
        + "</table>",
    )
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    result = compare(parser.parse_args().folder)
    for r in result["variants"]:
        print(
            r["params"],
            "earlier:",
            round(r["earlier"]["net_pnl"], 2),
            "later:",
            round(r["later"]["net_pnl"], 2),
            "trades:",
            r["earlier"]["closed_trades"],
            r["later"]["closed_trades"],
        )
    print("Selected by earlier period:", result["selected_params"])
