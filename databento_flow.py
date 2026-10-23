"""Optional Databento live futures-trade flow. Gold futures (GC) are a proxy, not OTC spot XAUUSD flow."""
from __future__ import annotations
import os, threading, time, logging
from collections import deque
from datetime import datetime, timezone
log=logging.getLogger("goldflow.orderflow")
active_flow_provider=None

class DatabentoGoldFlow:
    def __init__(self):
        global active_flow_provider
        self.key=os.getenv("DATABENTO_API_KEY","").strip()
        if not self.key: raise RuntimeError("DATABENTO_API_KEY is not configured")
        self.dataset=os.getenv("DATABENTO_DATASET","GLBX.MDP3")
        self.symbol=os.getenv("DATABENTO_GOLD_SYMBOL","GC.FUT")
        self.lock=threading.RLock(); self.events=deque(maxlen=200000); self.error=None; self.client=None; self.thread=None
        active_flow_provider=self
    def start(self):
        try: import databento as db
        except ImportError as e: raise RuntimeError("Install optional databento package to use the live CME futures order-flow connector") from e
        self.client=db.Live(key=self.key)
        self.client.subscribe(dataset=self.dataset,schema="trades",stype_in="parent",symbols=self.symbol)
        self.client.add_callback(self._on_record, self._on_error)
        self.thread=threading.Thread(target=self._run,name="databento-gold-flow",daemon=True); self.thread.start()
    def _run(self):
        try: self.client.start(); self.client.block_for_close()
        except Exception as e: self._on_error(e)
    def _on_error(self, exc):
        self.error=str(exc); log.error("Databento order-flow error: %s",exc)
    def _on_record(self, record):
        try:
            side=str(getattr(record,"side", "")).upper()
            size=float(getattr(record,"size",0) or 0)
            ts_ns=int(getattr(record,"ts_event",0) or 0)
            if size<=0 or ts_ns<=0: return
            # Databento: B is buyer-initiated; A is seller-initiated; N/unknown is excluded from signed delta.
            signed=size if side.endswith("BID") or side=="B" else -size if side.endswith("ASK") or side=="A" else 0.0
            with self.lock: self.events.append((ts_ns/1e9,size,signed,side))
        except Exception as e: log.debug("Ignoring unparseable flow record: %s",e)
    def snapshot(self, symbol="XAUUSD"):
        now=time.time(); cutoff=now-float(os.getenv("ORDER_FLOW_WINDOW_SECONDS","300"))
        with self.lock: ev=[x for x in self.events if x[0]>=cutoff]
        if not ev: return {"connected":False,"provider":"Databento · CME GC futures trades","reason":self.error or "Waiting for live futures trades.","proxy_for":"XAUUSD spot; not the same market"}
        buy=sum(x[1] for x in ev if x[3].endswith("BID") or x[3]=="B")
        sell=sum(x[1] for x in ev if x[3].endswith("ASK") or x[3]=="A")
        unknown=sum(x[1] for x in ev if not (x[3].endswith("BID") or x[3] in ("B",) or x[3].endswith("ASK") or x[3]=="A"))
        delta=buy-sell; total=buy+sell
        return {"connected":True,"provider":"Databento · CME GC futures trades","timestamp":datetime.fromtimestamp(ev[-1][0],timezone.utc).isoformat(),
                "symbol":self.symbol,"window_seconds":int(now-cutoff),"trade_count":len(ev),"buy_aggressor_volume":buy,
                "sell_aggressor_volume":sell,"cvd_delta":delta,"delta_percent":round(100*delta/total,2) if total else 0,
                "unknown_side_volume":unknown,"proxy_for":"XAUUSD spot; futures basis/venue mismatch must be considered",
                "caveat":"Trade-level aggressor delta only. This is not full depth-of-market, footprint by price, or OTC spot order flow."}
    def stop(self):
        if self.client:
            try: self.client.stop()
            except Exception: pass
