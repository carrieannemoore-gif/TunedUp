"""Technical indicators in plain Python (no TA-lib / pandas dependency)."""

from __future__ import annotations

import math
import statistics

from .market_data import Candle


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):  # Wilder smoothing
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float] | None:
    if len(closes) < slow + signal:
        return None
    line = [f - s for f, s in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    return {"macd": line[-1], "signal": sig[-1], "histogram": line[-1] - sig[-1]}


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(candles, candles[1:])]
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def realized_vol_annualized(closes: list[float], granularity_s: int) -> float | None:
    if len(closes) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    periods_per_year = 365 * 24 * 3600 / granularity_s
    return statistics.stdev(rets) * math.sqrt(periods_per_year)


def pct_change(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback or closes[-1 - lookback] == 0:
        return None
    return (closes[-1] / closes[-1 - lookback] - 1) * 100


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def snapshot(candles: list[Candle], granularity_s: int) -> dict:
    """Compact indicator summary for one symbol, sent to Claude."""
    closes = [c.close for c in candles]
    per_day = max(1, round(86400 / granularity_s))
    last = closes[-1]
    ema20, ema50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    m = macd(closes)
    a = atr(candles)
    return {
        "last_close": _r(last, 6),
        "change_pct_1d": _r(pct_change(closes, per_day), 2),
        "change_pct_7d": _r(pct_change(closes, per_day * 7), 2),
        "ema20": _r(ema20, 6),
        "ema50": _r(ema50, 6),
        "trend": "up" if ema20 > ema50 else "down",
        "rsi14": _r(rsi(closes), 2),
        "macd": {k: _r(v, 6) for k, v in m.items()} if m else None,
        "atr14_pct_of_price": _r(a / last * 100 if a and last else None, 3),
        "realized_vol_annualized_pct": _r((realized_vol_annualized(closes, granularity_s) or 0) * 100, 1),
        "candles_used": len(candles),
        "candle_granularity_s": granularity_s,
    }
