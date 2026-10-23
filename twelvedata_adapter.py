"""Twelve Data spot-gold OHLC/price adapter. API key must remain server-side.
Price-only feeds are indicative: they do not supply executable bid/ask or broker contract specs.
"""
from __future__ import annotations
import os, requests, time, threading
from datetime import datetime, timezone
from market_data import Quote

BASE = "https://api.twelvedata.com"

class TwelveDataProvider:
    def __init__(self):
        self.key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.symbol = os.getenv("TWELVEDATA_SYMBOL", "XAU/USD").strip()
        if not self.key:
            raise RuntimeError("TWELVEDATA_API_KEY is missing. Configure it server-side; never put it in frontend code.")
        self.timeout = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "8"))
        self.session = requests.Session(); self.lock=threading.RLock(); self.cache={}
        self.quote_ttl=float(os.getenv("TWELVEDATA_QUOTE_CACHE_SECONDS","10"))
        self.bar_ttls={"M5":float(os.getenv("TWELVEDATA_M5_CACHE_SECONDS","30")),"M15":float(os.getenv("TWELVEDATA_M15_CACHE_SECONDS","120")),"H1":float(os.getenv("TWELVEDATA_H1_CACHE_SECONDS","300")),"H4":300.0,"D1":1800.0}

    def _get(self, endpoint, params):
        p = {**params, "apikey": self.key}
        r = self.session.get(f"{BASE}/{endpoint}", params=p, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError("Twelve Data API error: " + str(data.get("message", "unknown error")))
        return data

    def get_quote(self, symbol: str) -> Quote:
        now=time.monotonic()
        with self.lock:
            cached=self.cache.get("quote")
            if cached and now-cached[0] < self.quote_ttl: return cached[1]
        data = self._get("quote", {"symbol": self.symbol, "interval": "1min", "dp": 5})
        price = data.get("close") or data.get("price")
        if price is None:
            raise RuntimeError("Twelve Data quote did not return a usable price.")
        stamp = data.get("datetime") or data.get("timestamp")
        if isinstance(stamp, (int, float)):
            ts = datetime.fromtimestamp(float(stamp), timezone.utc)
        elif stamp:
            try: ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError: ts = datetime.now(timezone.utc)
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        else:
            # Quote endpoint provides no event timestamp; do not claim a verified live timestamp.
            raise RuntimeError("Quote has no source timestamp; refusing to treat request time as market time.")
        px = float(price)
        if px <= 0: raise RuntimeError("Provider returned a non-positive price.")
        quote=Quote(symbol, px, px, ts.astimezone(timezone.utc), "Twelve Data · indicative XAU/USD")
        with self.lock: self.cache["quote"]=(time.monotonic(),quote)
        return quote

    def get_bars(self, symbol: str, timeframe: str, count: int = 300) -> list[dict]:
        intervals = {"M5":"5min", "M15":"15min", "H1":"1h", "H4":"4h", "D1":"1day"}
        if timeframe not in intervals: raise ValueError(f"Unsupported timeframe {timeframe}")
        now=time.monotonic()
        with self.lock:
            cached=self.cache.get("bars:"+timeframe)
            if cached and now-cached[0] < self.bar_ttls.get(timeframe,300): return cached[1][-count:]
        data = self._get("time_series", {"symbol": self.symbol, "interval": intervals[timeframe], "outputsize": min(count, 5000), "timezone":"UTC", "dp":5})
        values = data.get("values")
        if not isinstance(values, list) or len(values) < 65:
            raise RuntimeError(f"Insufficient {timeframe} candles from Twelve Data.")
        out=[]
        for x in reversed(values):
            stamp = datetime.fromisoformat(str(x["datetime"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            out.append({"time":int(stamp.timestamp()), "open":float(x["open"]), "high":float(x["high"]), "low":float(x["low"]), "close":float(x["close"]), "tick_volume":int(float(x.get("volume") or 0))})
        with self.lock: self.cache["bars:"+timeframe]=(time.monotonic(),out)
        return out
