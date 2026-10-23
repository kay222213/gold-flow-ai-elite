"""Server-side macro/news connectors. Missing credentials are reported, never simulated."""
from __future__ import annotations
import os, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

TIMEOUT = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "8"))

def _get(url, params=None, headers=None):
    r=requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status(); return r

def get_fred_macro():
    key=os.getenv("FRED_API_KEY", "").strip()
    if not key: return {"connected":False,"provider":"FRED","reason":"FRED_API_KEY not configured","series":[]}
    # Daily data series: 10Y Treasury, broad trade-weighted USD, 10Y real yield.
    series=[("DGS10","US 10Y Treasury yield"),("DTWEXBGS","Broad trade-weighted USD"),("DFII10","10Y TIPS real yield")]
    results=[]
    for sid,label in series:
        try:
            data=_get("https://api.stlouisfed.org/fred/series/observations",{"series_id":sid,"api_key":key,"file_type":"json","sort_order":"desc","limit":5}).json()
            obs=[x for x in data.get("observations",[]) if x.get("value") not in (None,".")]
            if obs:
                current=float(obs[0]["value"]); previous=float(obs[1]["value"]) if len(obs)>1 else None
                results.append({"id":sid,"label":label,"date":obs[0]["date"],"value":current,"previous_date":obs[1]["date"] if len(obs)>1 else None,
                    "change":round(current-previous,5) if previous is not None else None,"unit":"percent" if sid in ("DGS10","DFII10") else "index","source":"FRED","url":f"https://fred.stlouisfed.org/series/{sid}"})
        except Exception as e: results.append({"id":sid,"label":label,"error":str(e)})
    score=0; drivers=[]
    for x in results:
        delta=x.get("change")
        if delta is None: continue
        # A falling dollar / yields is typically supportive for gold; this is a regime heuristic, not a price forecast.
        direction=-1 if delta>0 else 1 if delta<0 else 0
        if x["id"] in ("DTWEXBGS","DGS10","DFII10"):
            score+=direction; drivers.append({"series":x["id"],"change":delta,"gold_context":"supportive" if direction>0 else "headwind" if direction<0 else "neutral"})
    bias="supportive" if score>=2 else "headwind" if score<=-2 else "mixed/unclear"
    return {"connected":bool(results),"provider":"FRED API","series":results,"gold_context_score":score,"gold_context_bias":bias,"drivers":drivers,
            "frequency_note":"Mostly daily, not tick-level; never interpret as intraday real-time."}

def get_calendar():
    key=os.getenv("TRADING_ECONOMICS_API_KEY", "").strip()
    if not key: return {"connected":False,"provider":"Trading Economics","reason":"TRADING_ECONOMICS_API_KEY not configured; calendar requires an authenticated plan","events":[]}
    now=datetime.now(timezone.utc); start=now.strftime("%Y-%m-%d"); end=(now+timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        data=_get(f"https://api.tradingeconomics.com/calendar/country/united%20states/{start}/{end}",{"f":"json"},{"Authorization":key}).json()
        events=[]
        for x in data if isinstance(data,list) else []:
            events.append({"time":x.get("Date"),"event":x.get("Event"),"country":x.get("Country"),"impact":x.get("Importance"),"actual":x.get("Actual"),"forecast":x.get("Forecast"),"previous":x.get("Previous"),"unit":x.get("Unit"),"source":"Trading Economics","url":"https://tradingeconomics.com/calendar"})
        return {"connected":True,"provider":"Trading Economics API","events":events}
    except Exception as e: return {"connected":False,"provider":"Trading Economics API","reason":str(e),"events":[]}

def get_news():
    # GDELT DOC API is open access, but its coverage/ranking is not a substitute for licensed terminal news.
    query='(gold OR XAUUSD OR "US dollar" OR Treasury OR Federal Reserve OR tariff OR war OR sanctions)'
    try:
        data=_get("https://api.gdeltproject.org/api/v2/doc/doc",{"query":query,"mode":"ArtList","format":"json","sort":"DateDesc","maxrecords":20,"timespan":"24h"}).json()
        articles=[]
        for x in data.get("articles",[]):
            articles.append({"title":x.get("title"),"url":x.get("url"),"source":x.get("domain"),"published":x.get("seendate"),"language":x.get("language"),"provider":"GDELT"})
        return {"connected":True,"provider":"GDELT DOC API","articles":articles,"caveat":"Headlines are unverified context. They are not proof of causal impact or direction."}
    except Exception as e: return {"connected":False,"provider":"GDELT DOC API","reason":str(e),"articles":[]}

def get_orderflow_status():
    if os.getenv("DATABENTO_API_KEY", "").strip():
        try:
            from databento_flow import active_flow_provider
            if active_flow_provider is not None: return active_flow_provider.snapshot(os.getenv("SYMBOL", "XAUUSD"))
        except Exception as e: return {"connected":False,"provider":"Databento","reason":str(e)}
    provider=os.getenv("ORDER_FLOW_PROVIDER", "none").strip().lower()
    endpoint=os.getenv("ORDER_FLOW_API_URL", "").strip(); token=os.getenv("ORDER_FLOW_API_TOKEN", "").strip()
    if provider=="rest" and endpoint and token:
        try:
            r=_get(endpoint,{"symbol":os.getenv("ORDER_FLOW_SYMBOL","GC")},{"Authorization":f"Bearer {token}"})
            data=r.json()
            # Validate a minimal expected schema; don't infer real flow from price candles.
            required=("timestamp","symbol")
            if not all(k in data for k in required): raise ValueError("Order-flow response missing required fields: timestamp, symbol")
            if data.get("delta_percent") is None and not (data.get("buy_volume") is not None and data.get("sell_volume") is not None):
                raise ValueError("Order-flow response must include delta_percent or classified buy_volume and sell_volume; CVD alone is not a valid delta percentage.")
            return {"connected":True,"provider":"Configured REST order-flow feed","snapshot":data,"caveat":"Confirm entitlement, exchange, contract mapping and data timestamp before use."}
        except Exception as e: return {"connected":False,"provider":"Configured REST order-flow feed","reason":str(e)}
    return {"connected":False,"provider":"none","reason":"No licensed trade-level/market-depth provider configured. CVD, footprint and DOM must remain unavailable until a real feed is supplied."}

def summarize_macro(macro, calendar, news, flow):
    bits=[]
    if macro.get("connected"):
        bits.append(f"FRED macro context: {macro.get('gold_context_bias','mixed/unclear')} (heuristic score {macro.get('gold_context_score',0)}; daily/lagged, not a trade trigger).")
        for s in macro.get("series",[]):
            if "value" in s: bits.append(f"{s['label']}: {s['value']} ({s['date']}, change {s.get('change')}; {s['source']}; daily/lagged context).")
    else: bits.append("Macro series unavailable: configure FRED_API_KEY.")
    if calendar.get("connected"): bits.append(f"Economic calendar connected: {len(calendar.get('events',[]))} US events returned.")
    else: bits.append("Economic calendar unavailable: configure Trading Economics API credentials.")
    if news.get("connected"): bits.append(f"GDELT headlines: {len(news.get('articles',[]))} articles retrieved; each requires source verification.")
    else: bits.append("News connector unavailable.")
    if not flow.get("connected"): bits.append("Order flow UNAVAILABLE; no order-flow confirmation is permitted.")
    return " ".join(bits)
