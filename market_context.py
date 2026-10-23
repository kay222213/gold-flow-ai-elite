"""Pure helpers for session levels and SAST session labels."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

SAST=ZoneInfo("Africa/Johannesburg")

def session_name(now: datetime | None=None) -> str:
    now=(now or datetime.now(SAST)).astimezone(SAST)
    t=now.time()
    # Approximate SAST session labels; actual London/NY overlap shifts with DST.
    if time(0,0) <= t < time(8,0): return "Asia (approx.)"
    if time(8,0) <= t < time(14,0): return "London (approx.)"
    if time(14,0) <= t < time(17,0): return "London / New York overlap (DST-sensitive)"
    if time(17,0) <= t < time(22,0): return "New York (approx.)"
    return "Rollover / quiet period (approx.)"

def high_low(bars: list[dict]) -> dict:
    if not bars: return {"high":None,"low":None}
    return {"high":max(float(x["high"]) for x in bars),"low":min(float(x["low"]) for x in bars)}

def previous_day_levels(d1_bars: list[dict]) -> dict:
    # D1 MT5 bar index -1 is the forming day; -2 is the last completed day.
    if len(d1_bars)<3: return {"high":None,"low":None}
    b=d1_bars[-2]
    return {"high":float(b["high"]),"low":float(b["low"])}

def intraday_session_levels(bars: list[dict], now: datetime | None=None) -> dict:
    """Approximate SAST session highs/lows from timestamped M5 candles; boundaries are configurable in code."""
    now=(now or datetime.now(SAST)).astimezone(SAST); today=now.date()
    sessions={"asia":(time(0,0),time(8,0)),"london":(time(8,0),time(14,0)),"new_york":(time(14,0),time(22,0))}
    out={}
    for name,(start,end) in sessions.items():
        selected=[]
        for b in bars:
            try: dt=datetime.fromtimestamp(int(b["time"]),tz=ZoneInfo("UTC")).astimezone(SAST)
            except Exception: continue
            if dt.date()==today and start<=dt.time()<end: selected.append(b)
        levels=high_low(selected)
        out[f"{name}_high"]=levels["high"]; out[f"{name}_low"]=levels["low"]
    return out

def structure_state(bars: list[dict], lookback: int=30) -> dict:
    closed=bars[:-1] if len(bars)>1 else bars
    if len(closed)<lookback: return {"state":"unknown","swing_high":None,"swing_low":None,"bos_up":False,"bos_down":False}
    sample=closed[-lookback:]; prev=sample[:-1]; last=sample[-1]
    hi=max(float(x["high"]) for x in prev); lo=min(float(x["low"]) for x in prev)
    return {"state":"bullish_break" if float(last["close"])>hi else "bearish_break" if float(last["close"])<lo else "range/inside",
            "swing_high":hi,"swing_low":lo,"bos_up":float(last["close"])>hi,"bos_down":float(last["close"])<lo}

def fair_value_gaps(bars: list[dict]) -> dict:
    closed=bars[:-1]
    if len(closed)<3: return {"bullish":None,"bearish":None}
    # Three-candle imbalance proxy: candle 1 and candle 3 do not overlap.
    for i in range(len(closed)-1,1,-1):
        a,b,c=closed[i-2],closed[i-1],closed[i]
        if float(c["low"])>float(a["high"]): return {"bullish":[float(a["high"]),float(c["low"])],"bearish":None}
        if float(c["high"])<float(a["low"]): return {"bullish":None,"bearish":[float(c["high"]),float(a["low"])]}
    return {"bullish":None,"bearish":None}
