"""TradingView Pine alert ingestion. Requires a configured high-entropy shared token."""
from __future__ import annotations
from datetime import datetime, timezone
import os, threading
from market_data import Quote

class TradingViewWebhookProvider:
    def __init__(self):
        self.token=os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", "").strip()
        if len(self.token)<24: raise RuntimeError("Set TRADINGVIEW_WEBHOOK_TOKEN to a random secret of at least 24 characters.")
        self.lock=threading.RLock(); self.bars={}; self.latest=None
    def accept(self, payload: dict):
        if payload.get("token") != self.token: raise PermissionError("Invalid TradingView webhook token")
        tf=str(payload.get("timeframe", "")).upper()
        if tf not in ("5", "5M", "15", "15M", "60", "1H", "240", "4H", "D", "1D"):
            raise ValueError("Unsupported timeframe; send M5, M15, H1 or H4 from Pine alert")
        tfmap={"5":"M5","5M":"M5","15":"M15","15M":"M15","60":"H1","1H":"H1","240":"H4","4H":"H4","D":"D1","1D":"D1"}
        tf=tfmap[tf]
        t=int(payload.get("time",0)); t=(t//1000 if t>10**12 else t); vals={k:float(payload[k]) for k in ("open","high","low","close")}
        if t<=0 or vals["low"]<=0 or vals["high"]<max(vals["open"],vals["close"]) or vals["low"]>min(vals["open"],vals["close"]): raise ValueError("Invalid timestamp or OHLC data")
        bar={"time":t,**vals,"tick_volume":int(float(payload.get("volume",0) or 0))}
        with self.lock:
            arr=self.bars.setdefault(tf,[])
            arr[:]=[x for x in arr if x["time"]!=t]
            arr.append(bar); arr.sort(key=lambda x:x["time"]); self.bars[tf]=arr[-1000:]
            self.latest={"price":vals["close"],"time":t,"symbol":str(payload.get("ticker","XAUUSD")),"source":"TradingView Pine webhook · close-only"}
    def get_quote(self, symbol: str) -> Quote:
        with self.lock: q=dict(self.latest) if self.latest else None
        if not q: raise RuntimeError("No TradingView webhook alerts received yet. Configure one alert for each required timeframe.")
        return Quote(symbol,float(q["price"]),float(q["price"]),datetime.fromtimestamp(q["time"],timezone.utc),q["source"])
    def get_bars(self, symbol: str, timeframe: str, count: int=300):
        with self.lock: arr=list(self.bars.get(timeframe,[]))
        if len(arr)<65: raise RuntimeError(f"TradingView webhook has {len(arr)} {timeframe} bars; at least 65 are required. Historical warm-up is not available from alerts alone.")
        return arr[-count:]
