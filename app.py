from __future__ import annotations
import asyncio, os, logging, secrets, hmac, re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from market_data import UnconfiguredProvider
from engine import generate_signal
from intelligence import get_fred_macro, get_calendar, get_news, get_orderflow_status, summarize_macro

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO")); log=logging.getLogger("goldflow")
SYMBOL=os.getenv("SYMBOL","XAUUSD")
POLL_SECONDS=max(1,float(os.getenv("POLL_SECONDS","5")))
MAX_AGE=max(5,float(os.getenv("MAX_QUOTE_AGE_SECONDS","90")))
CONFIG={"equity":float(os.getenv("ACCOUNT_EQUITY","1000")),"risk_percent":float(os.getenv("RISK_PERCENT","1")),
        "max_daily_loss":float(os.getenv("MAX_DAILY_LOSS","200")),"daily_target":float(os.getenv("DAILY_TARGET","2000")),
        "realized_pnl_today":float(os.getenv("REALIZED_PNL_TODAY","0")),
        "min_reward_risk":float(os.getenv("MIN_REWARD_RISK","2")),"max_spread_price":float(os.getenv("MAX_SPREAD_PRICE","0.50")),
        "min_atr_price":float(os.getenv("MIN_ATR_PRICE","0.25")),
        "min_stop_distance_price":float(os.getenv("MIN_STOP_DISTANCE_PRICE","0.50")),
        "min_stop_atr_multiple":float(os.getenv("MIN_STOP_ATR_MULTIPLE","1.0")),
        "stop_buffer_atr_multiple":float(os.getenv("STOP_BUFFER_ATR_MULTIPLE","0.20")),
        "target_atr_multiple":float(os.getenv("TARGET_ATR_MULTIPLE","1.50")),
        "max_m5_candle_age_seconds":float(os.getenv("MAX_M5_CANDLE_AGE_SECONDS","600")),
        "max_m15_candle_age_seconds":float(os.getenv("MAX_M15_CANDLE_AGE_SECONDS","1800")),
        "max_h1_candle_age_seconds":float(os.getenv("MAX_H1_CANDLE_AGE_SECONDS","7200")),
        "max_h4_candle_age_seconds":float(os.getenv("MAX_H4_CANDLE_AGE_SECONDS","28800")),
        "max_quote_age_seconds":MAX_AGE}
provider=UnconfiguredProvider(); tv_bridge=None; poll_task=None; intel_task=None; flow_service=None

def safe_error(exc: Exception) -> str:
    """Avoid returning provider URLs/API query strings that may contain credentials."""
    message=str(exc)
    message=re.sub(r'(?i)(apikey|api_key|token|key)=([^&\s]+)', r'\1=[REDACTED]', message)
    message=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*', r'\1[REDACTED]', message)
    return message[:500]

latest={"status":"unavailable","quote":None,"signal":{"status":"NO TRADE","side":None,"reason":"No live market-data provider configured."},
        "updated_at":None,"source":None,"error":None,"calendar_status":"PROVIDER NOT CONNECTED",
        "fundamental_summary":"No source-backed macro provider configured.","intelligence":{"macro":{},"calendar":{"events":[]},"news":{"articles":[]},"order_flow":{"connected":False}},
        "providers":{"market":{"connected":False,"provider":"none"},"calendar":{"connected":False,"provider":"none"},
                     "news":{"connected":False,"provider":"none"},"macro":{"connected":False,"provider":"none"},"order_flow":{"connected":False,"provider":"none"}}}

async def intelligence_loop():
    while True:
        try:
            macro,cal,news,flow=await asyncio.gather(asyncio.to_thread(get_fred_macro),asyncio.to_thread(get_calendar),asyncio.to_thread(get_news),asyncio.to_thread(get_orderflow_status))
            latest["intelligence"]={"macro":macro,"calendar":cal,"news":news,"order_flow":flow,"updated_at":datetime.now(timezone.utc).isoformat()}
            latest["calendar_status"]="CONNECTED" if cal.get("connected") else "PROVIDER NOT CONNECTED"
            latest["fundamental_summary"]=summarize_macro(macro,cal,news,flow)
            latest["providers"]={**latest["providers"],"macro":{"connected":macro.get("connected",False),"provider":macro.get("provider","FRED")},
                "calendar":{"connected":cal.get("connected",False),"provider":cal.get("provider","none")},
                "news":{"connected":news.get("connected",False),"provider":news.get("provider","none")},
                "order_flow":{"connected":flow.get("connected",False),"provider":flow.get("provider","none"),"reason":flow.get("reason")}}
        except Exception as e: log.warning("Intelligence refresh failed: %s",e)
        await asyncio.sleep(max(60,float(os.getenv("INTELLIGENCE_REFRESH_SECONDS","300"))))

@asynccontextmanager
async def lifespan(app: FastAPI):
    global provider,poll_task,intel_task,tv_bridge,flow_service
    source=os.getenv("MARKET_DATA_SOURCE","twelvedata" if os.getenv("TWELVEDATA_API_KEY") else "none").lower()
    try:
        if source=="twelvedata":
            from twelvedata_adapter import TwelveDataProvider
            provider=TwelveDataProvider()
        elif source=="tradingview_webhook":
            from tradingview_webhook import TradingViewWebhookProvider
            tv_bridge=TradingViewWebhookProvider(); provider=tv_bridge
        elif source=="mt5":
            from mt5_adapter import MT5Provider
            provider=MT5Provider()
        else: provider=UnconfiguredProvider()
    except Exception as exc:
        provider=UnconfiguredProvider(); latest["error"]=safe_error(exc)
        log.warning("Market provider not initialized; app remains available in NO TRADE mode: %s",exc)
    latest["providers"]["market"]={"connected":not isinstance(provider,UnconfiguredProvider),"provider":source,
        "reason":latest.get("error") if isinstance(provider,UnconfiguredProvider) else None}
    if os.getenv("DATABENTO_API_KEY","").strip():
        try:
            from databento_flow import DatabentoGoldFlow
            flow_service=DatabentoGoldFlow(); flow_service.start()
        except Exception as exc: log.warning("Order-flow provider could not start: %s",exc)
    poll_task=asyncio.create_task(poll_loop()); intel_task=asyncio.create_task(intelligence_loop())
    yield
    if flow_service:
        try: flow_service.stop()
        except Exception: pass
    for task in (poll_task,intel_task):
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass

app=FastAPI(title="Gold Flow AI API",version="3.0.0",lifespan=lifespan)
origins=[x.strip() for x in os.getenv("CORS_ORIGINS","http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500").split(",") if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=["GET","POST"],allow_headers=["*"])

async def poll_loop():
    while True:
        stamp=datetime.now(timezone.utc).isoformat()
        try:
            q=await asyncio.to_thread(provider.get_quote,SYMBOL)
            bars={tf:await asyncio.to_thread(provider.get_bars,SYMBOL,tf,350) for tf in ("M5","M15","H1","H4","D1")}
            # Price-only feeds do not know the user's broker contract specs. Optional values can be
            # configured server-side from the broker's instrument specification without MT5 access.
            tick_size=q.tick_size if q.tick_size is not None else (float(os.getenv("BROKER_TICK_SIZE")) if os.getenv("BROKER_TICK_SIZE","").strip() else None)
            tick_value=q.tick_value if q.tick_value is not None else (float(os.getenv("BROKER_TICK_VALUE_ACCOUNT_CCY")) if os.getenv("BROKER_TICK_VALUE_ACCOUNT_CCY","").strip() else None)
            volume_min=q.volume_min if q.volume_min is not None else (float(os.getenv("BROKER_VOLUME_MIN")) if os.getenv("BROKER_VOLUME_MIN","").strip() else None)
            volume_max=q.volume_max if q.volume_max is not None else (float(os.getenv("BROKER_VOLUME_MAX")) if os.getenv("BROKER_VOLUME_MAX","").strip() else None)
            volume_step=q.volume_step if q.volume_step is not None else (float(os.getenv("BROKER_VOLUME_STEP")) if os.getenv("BROKER_VOLUME_STEP","").strip() else None)
            account_currency=q.account_currency or os.getenv("BROKER_ACCOUNT_CURRENCY","").strip() or None
            quote={"symbol":q.symbol,"bid":q.bid,"ask":q.ask,"mid":q.mid,"spread":q.spread,"timestamp":q.timestamp.isoformat(),
                   "age_seconds":q.age_seconds(),"source":q.source,"tick_size":tick_size,"tick_value":tick_value,
                   "contract_size":q.contract_size,"volume_min":volume_min,"volume_max":volume_max,"volume_step":volume_step,
                   "account_currency":account_currency,"quote_type":"executable" if q.bid!=q.ask and q.tick_value else "indicative_price_only"}
            cfg={**CONFIG,"max_quote_age_seconds":MAX_AGE}
            signal=generate_signal(bars,quote,cfg)
            # Order-flow is a hard confirmation gate by default. CME GC futures flow is explicitly a proxy for OTC XAUUSD.
            flow_snapshot=None
            try:
                from databento_flow import active_flow_provider
                if active_flow_provider is not None: flow_snapshot=active_flow_provider.snapshot(SYMBOL)
                elif os.getenv("ORDER_FLOW_PROVIDER", "none").lower()=="rest": flow_snapshot=await asyncio.to_thread(get_orderflow_status)
            except Exception as flow_exc: log.warning("Order-flow snapshot unavailable: %s",flow_exc)
            flow_required=os.getenv("ORDER_FLOW_REQUIRED","true").lower() in ("1","true","yes")
            flow_ok=False
            if flow_snapshot and flow_snapshot.get("connected") and signal.get("side"):
                flow_values=flow_snapshot.get("snapshot",flow_snapshot)
                # Flow must be source-timestamped and recent; a stale snapshot cannot confirm a trade.
                raw_flow_time=flow_values.get("timestamp")
                try:
                    if not raw_flow_time: raise ValueError("missing timestamp")
                    flow_time=datetime.fromisoformat(str(raw_flow_time).replace("Z","+00:00"))
                    if flow_time.tzinfo is None: flow_time=flow_time.replace(tzinfo=timezone.utc)
                    flow_age=(datetime.now(timezone.utc)-flow_time.astimezone(timezone.utc)).total_seconds()
                    if flow_age < 0 or flow_age > max(30,2*int(os.getenv("ORDER_FLOW_WINDOW_SECONDS","300"))):
                        raise ValueError(f"stale order-flow timestamp ({flow_age:.1f}s old)")
                except Exception as flow_time_error:
                    signal={"status":"NO TRADE","side":None,"reason":f"Order-flow confirmation failed: {flow_time_error}.","reasons":["Order flow must be timestamped and fresh."],"context":signal.get("context",{})}
                    flow_values=None
                if flow_values is None:
                    pass
                elif flow_values.get("delta_percent") is not None:
                    dp=float(flow_values.get("delta_percent"))
                elif flow_values is not None and flow_values.get("buy_volume") is not None and flow_values.get("sell_volume") is not None:
                    buy_vol=float(flow_values.get("buy_volume") or 0)
                    sell_vol=float(flow_values.get("sell_volume") or 0)
                    total=buy_vol+sell_vol
                    if total <= 0: raise ValueError("Order-flow window has no classified buy/sell volume.")
                    dp=(buy_vol-sell_vol)/total*100
                elif flow_values is not None:
                    # Cumulative CVD divided by volume is not a valid aggressor-delta percentage.
                    signal={"status":"NO TRADE","side":None,"reason":"Order-flow provider must supply delta_percent or classified buy_volume and sell_volume; cumulative CVD alone is insufficient.","reasons":["Ambiguous flow schema cannot validate a trade."],"context":signal.get("context",{})}
                if flow_values is not None and signal.get("side"):
                    if not (-100.0 <= dp <= 100.0): raise ValueError("Order-flow delta percentage is outside the valid -100..100 range.")
                    threshold=float(os.getenv("ORDER_FLOW_MIN_DELTA_PERCENT","5"))
                    flow_ok=(dp>=threshold if signal["side"]=="BUY" else dp<=-threshold)
                    signal.setdefault("context",{})["order_flow_confirmed"]=flow_ok
                    signal["context"]["order_flow_delta_percent"]=dp
                    signal["context"]["order_flow_provider"]=flow_snapshot.get("provider")
                    if not flow_ok:
                        signal={"status":"NO TRADE","side":None,"reason":f"Order-flow confirmation failed: GC futures aggressor delta does not confirm the price-action side (delta {dp:.1f}%).","reasons":["Order flow is a required gate; do not treat spot candles as flow."],"context":signal.get("context",{})}
            elif signal.get("status")=="CANDIDATE" and flow_required:
                signal={"status":"NO TRADE","side":None,"reason":"Order-flow feed is required but unavailable. No trade candidate until real trade-level flow is connected.","reasons":["CVD/footprint/depth must not be inferred from ordinary OHLC candles."],"context":signal.get("context",{})}
            # Event-risk blackout: block candidates around source-timestamped high-impact US events.
            if signal.get("status")=="CANDIDATE":
                events=(latest.get("intelligence",{}).get("calendar",{}).get("events",[]) or [])
                now=datetime.now(timezone.utc)
                for ev in events:
                    impact=str(ev.get("impact","")).lower()
                    raw=ev.get("time")
                    if not raw or not any(tag in impact for tag in ("high","3","major")): continue
                    try:
                        event_time=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
                        if event_time.tzinfo is None: event_time=event_time.replace(tzinfo=timezone.utc)
                        if abs((event_time-now).total_seconds())<=int(os.getenv("NEWS_BLACKOUT_MINUTES","15"))*60:
                            signal={"status":"NO TRADE","side":None,"reason":f"High-impact event blackout: {ev.get('event','scheduled US release')} is within the configured blackout window.","reasons":["Avoid new candidates around high-impact releases; spreads and slippage can expand."],"context":signal.get("context",{})}; break
                    except Exception: continue
            status="connected" if 0 <= q.age_seconds() <= MAX_AGE else "stale"
            latest.update({"status":status,"quote":quote,"signal":signal,"updated_at":stamp,"source":q.source,"error":None,
                "providers":{**latest["providers"],"market":{"connected":status=="connected","provider":q.source,"quote_age_seconds":round(q.age_seconds(),2)}}})
        except Exception as exc:
            latest.update({"status":"unavailable","quote":None,"signal":{"status":"NO TRADE","side":None,
                "reason":"Live data or calculations unavailable; no trade candidate is permitted.","reasons":[safe_error(exc)]},
                "error":safe_error(exc),"updated_at":stamp,"providers":{**latest["providers"],"market":{"connected":False,"provider":os.getenv("MARKET_DATA_SOURCE","none"),"reason":safe_error(exc)}}})
            log.warning("Market polling failed: %s",exc)
        await asyncio.sleep(POLL_SECONDS)

@app.get("/api/health")
def health():
    return {"app":"Gold Flow AI","version":"3.0.0","status":latest["status"],"source":latest["source"],"error":latest["error"],
            "poll_seconds":POLL_SECONDS,"max_quote_age_seconds":MAX_AGE,"signal_status":latest["signal"]["status"],"providers":latest["providers"]}

@app.get("/api/state")
def state(): return latest

@app.get("/api/intelligence")
def intelligence(): return latest["intelligence"]

@app.get("/api/config")
def config():
    return {"symbol":SYMBOL,"poll_seconds":POLL_SECONDS,"max_quote_age_seconds":MAX_AGE,"risk_percent":CONFIG["risk_percent"],
            "min_reward_risk":CONFIG["min_reward_risk"],"max_spread_price":CONFIG["max_spread_price"],
            "min_atr_price":CONFIG["min_atr_price"],"min_stop_distance_price":CONFIG["min_stop_distance_price"],
            "min_stop_atr_multiple":CONFIG["min_stop_atr_multiple"],"stop_buffer_atr_multiple":CONFIG["stop_buffer_atr_multiple"],
            "target_atr_multiple":CONFIG["target_atr_multiple"]}

@app.post("/api/settings")
def update_settings(payload: dict, request: Request):
    expected=os.getenv("ADMIN_API_TOKEN", "").strip()
    supplied=request.headers.get("x-admin-token", "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401,detail="Settings endpoint requires a valid X-Admin-Token configured in the backend environment.")
    allowed={"equity","risk_percent","max_daily_loss","daily_target","realized_pnl_today","min_reward_risk","max_spread_price"}; updates={}
    for key in allowed:
        if key in payload:
            try: value=float(payload[key])
            except (TypeError,ValueError): raise HTTPException(status_code=400,detail=f"Invalid numeric setting: {key}")
            if value<0 and key!="realized_pnl_today": raise HTTPException(status_code=400,detail=f"Setting cannot be negative: {key}")
            updates[key]=value
    if "equity" in updates and updates["equity"]<=0: raise HTTPException(status_code=400,detail="Equity must be positive")
    if "risk_percent" in updates and not 0<updates["risk_percent"]<=5: raise HTTPException(status_code=400,detail="Risk percentage must be above 0 and at most 5")
    if "min_reward_risk" in updates and updates["min_reward_risk"]<1: raise HTTPException(status_code=400,detail="Minimum reward:risk must be at least 1")
    if "max_spread_price" in updates and updates["max_spread_price"]<0: raise HTTPException(status_code=400,detail="Maximum spread cannot be negative")
    CONFIG.update(updates); return {"saved":True,"settings":{k:CONFIG[k] for k in allowed}}

@app.post("/api/webhook/tradingview")
async def tradingview_webhook(request: Request):
    global tv_bridge
    secret=os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", "").strip()
    if len(secret)<24: raise HTTPException(status_code=503,detail="TradingView webhook is disabled until a server-side shared secret of 24+ characters is configured.")
    try: payload=await request.json()
    except Exception: raise HTTPException(status_code=400,detail="Expected JSON alert payload.")
    # Create a bridge for ingestion even if Twelve Data is the selected live provider.
    if tv_bridge is None:
        from tradingview_webhook import TradingViewWebhookProvider
        try: tv_bridge=TradingViewWebhookProvider()
        except Exception as e: raise HTTPException(status_code=503,detail=str(e))
    try: tv_bridge.accept(payload)
    except PermissionError: raise HTTPException(status_code=401,detail="Invalid webhook token.")
    except Exception as e: raise HTTPException(status_code=400,detail=str(e))
    return {"accepted":True,"timeframe":payload.get("timeframe"),"note":"Confirmed bar received; this is not an order execution or full order-flow feed."}

@app.get("/api/tradingview/bridge-status")
def tradingview_bridge_status():
    if tv_bridge is None: return {"connected":False,"reason":"No TradingView alerts received / bridge not initialized."}
    return {"connected":bool(tv_bridge.latest),"timeframes":{tf:len(bars) for tf,bars in tv_bridge.bars.items()},"source":"TradingView Pine webhook · confirmed OHLC only"}

@app.websocket("/api/stream")
async def stream(ws:WebSocket):
    await ws.accept()
    try:
        last_stamp=None
        while True:
            stamp=latest.get("updated_at")
            if stamp!=last_stamp: await ws.send_json(latest); last_stamp=stamp
            await asyncio.sleep(min(POLL_SECONDS,1))
    except WebSocketDisconnect: return
