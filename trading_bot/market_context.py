"""Causal confirmation from another share; never submits orders."""

import math
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from statistics import mean

if TYPE_CHECKING:
    from backtest import Bar

TREND_WINDOW = 120
MOMENTUM_BARS = 4
MAX_CONTEXT_AGE = timedelta(minutes=15)


def market_filter(target: list["Bar"], reference: list["Bar"]) -> dict[datetime, bool]:
    """Use only completed 15min reference bars at or before each target bar.

    Missing/stale context blocks entry. This confirms contemporaneous movement;
    it is not a proven leading indicator. Session gaps are not filled.
    """
    times = [bar.time for bar in reference]
    if any(time.tzinfo is None for time in times) or times != sorted(set(times)):
        raise ValueError("Reference bars must be aware, ordered and unique.")
    if any(not math.isfinite(bar.close) or bar.close <= 0 for bar in reference):
        raise ValueError("Reference close prices must be finite and positive.")
    result = {}
    for bar in target:
        if bar.time.tzinfo is None:
            raise ValueError("Target candle timestamps must be timezone-aware.")
        count = bisect_right(times, bar.time)
        if count < TREND_WINDOW or bar.time - times[count - 1] >= MAX_CONTEXT_AGE:
            result[bar.time] = False
            continue
        latest = reference[count - 1].close
        average = mean(bar.close for bar in reference[count - TREND_WINDOW : count])
        result[bar.time] = (
            latest > average and latest > reference[count - 1 - MOMENTUM_BARS].close
        )
    return result
