"""Public OHLC candles (no API key): Coinbase Exchange, falling back to Kraken."""

from __future__ import annotations

import math
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class Candle:
    time: int  # unix seconds, candle open
    open: float
    high: float
    low: float
    close: float
    volume: float


_KRAKEN_PAIRS = {"BTC-USD": "XBTUSD"}
_KRAKEN_INTERVALS = {60: 1, 300: 5, 900: 15, 3600: 60, 14400: 240, 86400: 1440}


class MarketData:
    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()

    def candles(self, symbol: str, granularity: int = 3600, count: int = 200) -> list[Candle]:
        errors = []
        for source in (self._coinbase, self._kraken):
            try:
                data = source(symbol, granularity)
                if data:
                    return data[-count:]
            except (requests.RequestException, KeyError, ValueError, IndexError) as e:
                errors.append(f"{source.__name__}: {e}")
        raise RuntimeError(f"No candle source available for {symbol}: {errors}")

    def last_price(self, symbol: str) -> float:
        return self.candles(symbol, 60, 5)[-1].close

    def _coinbase(self, symbol: str, granularity: int) -> list[Candle]:
        r = self._session.get(f"https://api.exchange.coinbase.com/products/{symbol}/candles",
                              params={"granularity": granularity}, timeout=15)
        r.raise_for_status()
        # rows: [time, low, high, open, close, volume], newest first
        rows = sorted(r.json(), key=lambda row: row[0])
        return [Candle(int(t), float(o), float(h), float(lo), float(c), float(v)) for t, lo, h, o, c, v in rows]

    def _kraken(self, symbol: str, granularity: int) -> list[Candle]:
        pair = _KRAKEN_PAIRS.get(symbol, symbol.replace("-", ""))
        r = self._session.get("https://api.kraken.com/0/public/OHLC",
                              params={"pair": pair, "interval": _KRAKEN_INTERVALS[granularity]}, timeout=15)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise ValueError(body["error"])
        rows = next(v for k, v in body["result"].items() if k != "last")
        # rows: [time, open, high, low, close, vwap, volume, count]
        return [Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[6]))
                for row in rows]


class SyntheticMarketData:
    """Deterministic offline candles for tests and sandboxed dry runs. Not real prices."""

    _BASE = {"BTC-USD": 60000.0, "ETH-USD": 3000.0, "SOL-USD": 150.0}

    def __init__(self, drift: float = 0.0005):
        self._drift = drift

    def candles(self, symbol: str, granularity: int = 3600, count: int = 200) -> list[Candle]:
        base = self._BASE.get(symbol, 100.0)
        out, price = [], base
        for i in range(count):
            wave = 0.01 * math.sin(i / 9) + 0.004 * math.sin(i / 2.3)
            nxt = price * (1 + self._drift + wave / 10)
            hi, lo = max(price, nxt) * 1.003, min(price, nxt) * 0.997
            out.append(Candle(1_700_000_000 + i * granularity, price, hi, lo, nxt, 100 + 10 * math.cos(i)))
            price = nxt
        return out

    def last_price(self, symbol: str) -> float:
        return self.candles(symbol)[-1].close
