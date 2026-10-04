"""Validated input and atomic artifacts shared by offline experiments."""

import csv
import json
import math
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from backtest import Bar

CANDLE_FIELDS = ("time", "open", "high", "low", "close", "volume")


def read_bars(path: Path) -> list[Bar]:
    bars = []
    previous = None
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not set(CANDLE_FIELDS) <= set(reader.fieldnames):
            raise ValueError(f"{path}: missing candle columns")
        for line, row in enumerate(reader, start=2):
            try:
                bar = Bar(
                    datetime.fromisoformat(row["time"]),
                    *(float(row[key]) for key in CANDLE_FIELDS[1:5]),
                    int(row["volume"]),
                )
                if (
                    bar.time.tzinfo is None
                    or previous is not None
                    and bar.time <= previous
                    or bar.volume < 0
                    or not all(
                        math.isfinite(v) and v > 0
                        for v in [bar.open, bar.high, bar.low, bar.close]
                    )
                    or not bar.low
                    <= min(bar.open, bar.close)
                    <= max(bar.open, bar.close)
                    <= bar.high
                ):
                    raise ValueError("invalid or unordered candle")
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"{path}: invalid candle at line {line}") from exc
            bars.append(bar)
            previous = bar.time
    if not bars:
        raise ValueError(f"{path}: no candles")
    return bars


def atomic_text(path: Path, content: str) -> None:
    """Publish complete UTF-8 artifacts; a failed write preserves the old file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def write_bars(path: Path, bars: list[Bar]) -> None:
    import io

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CANDLE_FIELDS)
    writer.writeheader()
    writer.writerows({**asdict(bar), "time": bar.time.isoformat()} for bar in bars)
    atomic_text(path, stream.getvalue())


def read_experiment(folder: Path) -> tuple[dict, list[Bar]]:
    original = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    try:
        parameters = original["parameters"]
        start = datetime.fromisoformat(parameters["start"])
        initial = float(original["summary"]["initial"])
        lot = parameters["lot"]
        costs = [
            float(parameters[key])
            for key in ["fee_bps_per_side", "slippage_bps_per_side"]
        ]
        if (
            start.tzinfo is None
            or not math.isfinite(initial)
            or initial <= 0
            or type(lot) is not int
            or lot < 1
            or not all(math.isfinite(v) and 0 <= v <= 100 for v in costs)
        ):
            raise ValueError("invalid parameters")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{folder}: invalid experiment metadata") from exc
    bars = read_bars(folder / "candles.csv")
    if not any(bar.time >= start for bar in bars):
        raise ValueError(f"{folder}: no candles in experiment period")
    return original, bars
