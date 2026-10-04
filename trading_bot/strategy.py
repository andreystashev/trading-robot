"""Demonstration signals only: this module never sends orders."""

from statistics import mean, pstdev
from dataclasses import dataclass
import math


def simple_signal(prices: list[float]) -> str:
    if len(prices) < 20:
        return "HOLD"
    ma5, ma20 = mean(prices[-5:]), mean(prices[-20:])
    if ma5 > ma20:
        return "BUY"
    if ma5 < ma20:
        return "SELL"
    return "HOLD"


def crossover_signal(prices: list[float]) -> str:
    """MA10/MA30 crossing using only closed 15-minute candles."""
    if len(prices) < 31:
        return "HOLD"
    previous = mean(prices[-11:-1]) - mean(prices[-31:-1])
    current = mean(prices[-10:]) - mean(prices[-30:])
    if previous <= 0 < current:
        return "BUY"
    if previous >= 0 > current:
        return "SELL"
    return "HOLD"


@dataclass(frozen=True)
class StrategyParams:
    fast: int = 10
    slow: int = 30
    trend: int = 0
    entry_edge_bps: float = 0
    stop_pct: float = 1
    kind: str = "ma"

    def __post_init__(self):
        if any(type(v) is not int for v in [self.fast, self.slow, self.trend]):
            raise ValueError("Strategy windows must be integers.")
        if self.kind not in {"ma", "breakout", "reversion", "pullback"}:
            raise ValueError("Unknown strategy kind.")
        if not 2 <= self.fast < self.slow <= 200 or not 0 <= self.trend <= 300:
            raise ValueError("MA: 2 <= fast < slow <= 200; trend: 0..300.")
        if self.kind in {"reversion", "pullback"} and self.trend == 1:
            raise ValueError("Pullback trend window must be 0 or at least 2.")
        if (
            not math.isfinite(self.entry_edge_bps)
            or not 0 <= self.entry_edge_bps <= 100
        ):
            raise ValueError("Entry edge must be 0..100 bps.")
        if not math.isfinite(self.stop_pct) or not 0.1 <= self.stop_pct <= 10:
            raise ValueError("Stop must be 0.1..10 percent.")


def configured_signal(prices: list[float], params: StrategyParams) -> str:
    if len(prices) < max(params.slow + 1, params.trend):
        return "HOLD"
    if params.kind in {"reversion", "pullback"}:
        # Buy an unusually deep pullback, sell at the reference mean. All
        # features use closed candles; the current close is excluded from bands.
        reference = prices[-params.slow - 1 : -1]
        center, width = mean(reference), pstdev(reference)
        if prices[-1] >= center:
            return "SELL"
        if params.kind == "reversion" and (
            width == 0 or prices[-1] > center - 2 * width
        ):
            return "HOLD"
        if (center / prices[-1] - 1) * 10000 < params.entry_edge_bps:
            return "HOLD"
        changes = [
            b - a for a, b in zip(prices[-params.fast - 1 : -1], prices[-params.fast :])
        ]
        gains = sum(max(v, 0) for v in changes)
        losses = sum(max(-v, 0) for v in changes)
        rsi = 100 * gains / (gains + losses) if gains + losses else 50
        if rsi > (10 if params.kind == "pullback" else 30):
            return "HOLD"
        if params.trend:
            window = prices[-params.trend :]
            half = len(window) // 2
            if prices[-1] <= mean(window) or mean(window[half:]) <= mean(window[:half]):
                return "HOLD"
        return "BUY"
    if params.kind == "breakout":
        # Today's close is excluded from the comparison channel.
        if prices[-1] < min(prices[-params.fast - 1 : -1]):
            return "SELL"
        ceiling = max(prices[-params.slow - 1 : -1])
        if (
            prices[-1] > ceiling
            and (prices[-1] / ceiling - 1) * 10000 >= params.entry_edge_bps
        ):
            if params.trend and prices[-1] <= mean(prices[-params.trend :]):
                return "HOLD"
            return "BUY"
        return "HOLD"
    previous = mean(prices[-params.fast - 1 : -1]) - mean(prices[-params.slow - 1 : -1])
    fast, slow = mean(prices[-params.fast :]), mean(prices[-params.slow :])
    if previous >= 0 and fast < slow:
        return "SELL"
    if previous <= 0 and fast > slow:
        if (fast / slow - 1) * 10000 < params.entry_edge_bps:
            return "HOLD"
        if params.trend and prices[-1] <= mean(prices[-params.trend :]):
            return "HOLD"
        return "BUY"
    return "HOLD"
