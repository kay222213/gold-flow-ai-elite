"""Fail-closed XAUUSD decision-support engine.

This engine emits a CANDIDATE only when every coded price-action gate passes.
It never places orders and never claims order-flow/fundamental confirmation without provider data.
MT5's final bar is normally still forming; this module explicitly excludes it.
"""
from __future__ import annotations
from math import floor, isfinite
import time
from typing import Any
from market_context import intraday_session_levels, structure_state, fair_value_gaps, previous_day_levels


def _valid_bars(bars: list[dict], minimum: int = 60) -> bool:
    if not isinstance(bars, list) or len(bars) < minimum:
        return False
    previous_time = None
    for b in bars:
        try:
            vals = [float(b[k]) for k in ("open", "high", "low", "close")]
            if not all(isfinite(v) for v in vals): return False
            o,h,l,c = vals
            if l <= 0 or h < max(o,c) or l > min(o,c): return False
            # Timestamp is mandatory: without it we cannot prove candle ordering/freshness.
            if "time" not in b: return False
            t = int(b["time"])
            if t <= 0: return False
            if previous_time is not None and t <= previous_time: return False
            previous_time = t
        except (KeyError, TypeError, ValueError):
            return False
    return True


def atr(bars: list[dict], period: int = 14) -> float:
    if period < 1 or len(bars) < period + 2:
        raise ValueError("Insufficient bars for ATR")
    # Use only completed bars; bars[-1] is treated as the potentially forming candle.
    closed = bars[:-1]
    trs = []
    for i in range(1, len(closed)):
        b, prev = closed[i], closed[i-1]
        trs.append(max(float(b["high"])-float(b["low"]),
                       abs(float(b["high"])-float(prev["close"])),
                       abs(float(b["low"])-float(prev["close"]))))
    if len(trs) < period: raise ValueError("Insufficient closed bars for ATR")
    return sum(trs[-period:]) / period


def fib_golden_zone(bars: list[dict], lookback: int = 50) -> dict:
    if len(bars) < lookback + 1: raise ValueError("Insufficient bars for Fibonacci swing")
    closed = bars[:-1][-lookback:]
    hi = max(float(b["high"]) for b in closed)
    lo = min(float(b["low"]) for b in closed)
    span = hi-lo
    if span <= 0: raise ValueError("Invalid swing range")
    return {
        "swing_high": hi, "swing_low": lo,
        "bullish_zone": [hi-span*0.618, hi-span*0.65],
        "bearish_zone": [lo+span*0.618, lo+span*0.65],
    }


def size_position(risk_money: float, entry: float, stop: float, tick_size: float,
                  tick_value_per_lot: float, volume_min: float, volume_max: float,
                  volume_step: float) -> dict:
    """tick_value_per_lot means account-currency loss per tick for one lot."""
    vals=(risk_money,entry,stop,tick_size,tick_value_per_lot,volume_min,volume_max,volume_step)
    if not all(isfinite(float(v)) for v in vals): raise ValueError("Non-finite sizing input")
    if risk_money <= 0 or entry <= 0 or stop <= 0 or entry == stop or min(tick_size,tick_value_per_lot,volume_min,volume_step) <= 0 or volume_max < volume_min:
        raise ValueError("Invalid risk or broker contract specification")
    distance=abs(entry-stop)
    loss_per_lot=(distance/tick_size)*tick_value_per_lot
    if loss_per_lot <= 0 or not isfinite(loss_per_lot): raise ValueError("Invalid loss per lot")
    raw=min(risk_money/loss_per_lot, volume_max)
    # Round down to volume step; never round up through risk budget.
    volume=floor((raw+1e-12)/volume_step)*volume_step
    if volume < volume_min:
        return {"volume":0.0,"risk_money":0.0,"min_volume_risk":round(loss_per_lot*volume_min,2),
                "reason":"Minimum broker volume exceeds permitted risk."}
    volume=round(volume,8)
    return {"volume":volume,"risk_money":round(loss_per_lot*volume,2),"reason":None}



def _round_to_tick(price: float, tick_size: float | None, mode: str = "nearest") -> float:
    """Round a price to a known tick. Without tick metadata, use 2dp for indicative XAUUSD only."""
    if tick_size is None or not isfinite(float(tick_size)) or float(tick_size) <= 0:
        return round(float(price), 2)
    ticks=float(price)/float(tick_size)
    if mode == "down": ticks=floor(ticks + 1e-12)
    elif mode == "up": ticks=-floor(-ticks + 1e-12)
    else: ticks=round(ticks)
    decimals=max(0,min(8, len(str(float(tick_size)).split(".")[-1].rstrip("0"))))
    return round(ticks*float(tick_size), decimals)


def build_trade_levels(side: str, entry: float, structural_stop: float, atr_value: float,
                       spread: float, tick_size: float | None, config: dict,
                       target_level: float | None = None) -> dict:
    """Build volatility-aware levels; risk percentage must never set price distances."""
    min_stop_price=float(config.get("min_stop_distance_price", 0.50))
    min_stop_atr=float(config.get("min_stop_atr_multiple", 1.0))
    stop_buffer_atr=float(config.get("stop_buffer_atr_multiple", 0.20))
    target_atr=float(config.get("target_atr_multiple", 1.50))
    min_rr=float(config.get("min_reward_risk", 2.0))
    if atr_value <= 0 or entry <= 0 or structural_stop <= 0:
        raise ValueError("Invalid entry, structural stop or ATR.")
    if side == "BUY":
        if structural_stop >= entry: raise ValueError("BUY structural stop must be below entry.")
        buffered_stop=structural_stop-max(atr_value*stop_buffer_atr, spread*1.5, (tick_size or 0)*2)
        min_distance=max(min_stop_price, atr_value*min_stop_atr, spread*2, (tick_size or 0)*5)
        stop=min(buffered_stop, entry-min_distance)
        risk_distance=entry-stop
        target_distance=max(min_rr*risk_distance, target_atr*atr_value)
        if target_level is not None:
            if target_level <= entry: raise ValueError("Nearest opposing liquidity objective is not above the BUY entry.")
            if target_level-entry+1e-9 < target_distance:
                raise ValueError("Nearest opposing liquidity objective cannot support the minimum reward:risk and ATR target; no trade.")
            target=target_level
        else:
            target=entry+target_distance
        stop=_round_to_tick(stop,tick_size,"down")
        target=_round_to_tick(target,tick_size,"down" if target_level is not None else "up")
        entry=_round_to_tick(entry,tick_size,"nearest")
    elif side == "SELL":
        if structural_stop <= entry: raise ValueError("SELL structural stop must be above entry.")
        buffered_stop=structural_stop+max(atr_value*stop_buffer_atr, spread*1.5, (tick_size or 0)*2)
        min_distance=max(min_stop_price, atr_value*min_stop_atr, spread*2, (tick_size or 0)*5)
        stop=max(buffered_stop, entry+min_distance)
        risk_distance=stop-entry
        target_distance=max(min_rr*risk_distance, target_atr*atr_value)
        if target_level is not None:
            if target_level >= entry: raise ValueError("Nearest opposing liquidity objective is not below the SELL entry.")
            if entry-target_level+1e-9 < target_distance:
                raise ValueError("Nearest opposing liquidity objective cannot support the minimum reward:risk and ATR target; no trade.")
            target=target_level
        else:
            target=entry-target_distance
        stop=_round_to_tick(stop,tick_size,"up")
        target=_round_to_tick(target,tick_size,"up" if target_level is not None else "down")
        entry=_round_to_tick(entry,tick_size,"nearest")
    else:
        raise ValueError("Side must be BUY or SELL.")
    risk_distance=abs(entry-stop)
    reward_distance=abs(target-entry)
    if risk_distance <= 0 or reward_distance <= 0:
        raise ValueError("Rounded trade levels are invalid.")
    return {"entry":entry,"stop_loss":stop,"take_profit":target,
            "risk_distance":round(risk_distance,8),"reward_distance":round(reward_distance,8),
            "reward_risk":round(reward_distance/risk_distance,2),
            "minimum_stop_distance":round(max(min_stop_price,atr_value*min_stop_atr,spread*2,(tick_size or 0)*5),8)}

def _bias(bars: list[dict], lookback: int) -> str:
    # Completed candles only, compare recent close to close N bars earlier.
    closed=bars[:-1]
    if len(closed) < lookback+1: return "unknown"
    a=float(closed[-1]["close"]); b=float(closed[-1-lookback]["close"])
    return "bullish" if a>b else "bearish" if a<b else "neutral"


def no_trade(reason: str, context: dict | None = None) -> dict:
    return {"status":"NO TRADE","side":None,"reason":reason,"reasons":[reason],"context":context or {}}


def _order_block_proxy(bars: list[dict], side: str, lookback: int=12):
    """Last opposite-colour candle before a directional impulse; heuristic zone, not verified institutional orders."""
    closed=bars[:-1]
    for b in reversed(closed[max(0,len(closed)-lookback-1):-1]):
        o,c=float(b["open"]),float(b["close"])
        if side=="bullish" and c<o: return {"low":float(b["low"]),"high":float(b["high"]),"time":b.get("time"),"type":"bullish_order_block_proxy"}
        if side=="bearish" and c>o: return {"low":float(b["low"]),"high":float(b["high"]),"time":b.get("time"),"type":"bearish_order_block_proxy"}
    return None

def _equal_liquidity_pools(bars: list[dict], tolerance: float):
    closed=bars[:-1]
    if len(closed)<10: return {"equal_high":None,"equal_low":None}
    sample=closed[-80:]; highs=[]; lows=[]
    for i in range(2,len(sample)-2):
        if float(sample[i]["high"])>=max(float(sample[j]["high"]) for j in (i-2,i-1,i+1,i+2)): highs.append(float(sample[i]["high"]))
        if float(sample[i]["low"])<=min(float(sample[j]["low"]) for j in (i-2,i-1,i+1,i+2)): lows.append(float(sample[i]["low"]))
    def pool(vals):
        for i in range(len(vals)-1,-1,-1):
            matches=[v for v in vals[:i] if abs(v-vals[i])<=tolerance]
            if matches: return round((sum(matches)+vals[i])/(len(matches)+1),5)
        return None
    return {"equal_high":pool(highs),"equal_low":pool(lows)}

def generate_signal(bars: dict[str,list[dict]], quote: dict, config: dict) -> dict[str,Any]:
    required=("M5","M15","H1","H4")
    if any(tf not in bars or not _valid_bars(bars[tf], 65) for tf in required):
        return no_trade("Insufficient or invalid timestamped M5/M15/H1/H4 candles.")
    # Fresh quotes do not make stale candles current. Check the latest completed bar per timeframe.
    now_epoch=time.time()
    max_candle_ages={"M5":float(config.get("max_m5_candle_age_seconds",600)),
                      "M15":float(config.get("max_m15_candle_age_seconds",1800)),
                      "H1":float(config.get("max_h1_candle_age_seconds",7200)),
                      "H4":float(config.get("max_h4_candle_age_seconds",28800))}
    for tf in required:
        completed_time=int(bars[tf][-2]["time"])
        candle_age=now_epoch-completed_time
        if candle_age < 0 or candle_age > max_candle_ages[tf]:
            return no_trade(f"Latest completed {tf} candle is stale ({candle_age:.0f}s old); waiting for fresh source candles.",
                             {"timeframe":tf,"candle_age_seconds":round(candle_age,1),"maximum_age_seconds":max_candle_ages[tf]})
    try:
        age=float(quote.get("age_seconds",999))
        bid=float(quote.get("bid",0)); ask=float(quote.get("ask",0))
    except (TypeError,ValueError):
        return no_trade("Quote fields are invalid.")
    max_age=float(config.get("max_quote_age_seconds",90))
    if not isfinite(age) or age < 0 or age > max_age: return no_trade("Market quote is stale.")
    if not (isfinite(bid) and isfinite(ask) and bid>0 and ask>=bid): return no_trade("Invalid bid/ask quote.")
    spread=ask-bid
    max_spread=float(config.get("max_spread_price", config.get("max_spread", 0.50)))
    if spread > max_spread: return no_trade("Spread exceeds configured maximum.")
    specs=("tick_size","tick_value","volume_min","volume_max","volume_step")
    try: spec_values=[float(quote.get(k,0) or 0) for k in specs]
    except (TypeError,ValueError): return no_trade("Instrument contract specifications are invalid.")
    sizing_available=all(isfinite(v) and v>0 for v in spec_values)
    # Price-only feeds can support a technical price plan, but not reliable monetary sizing.
    # Never invent tick value, volume step, account currency conversion, or executable spread.
    m5,m15,h1,h4=(bars[k] for k in required)
    try:
        a=atr(m5,14)
        fib=fib_golden_zone(m5,50)
    except ValueError as e: return no_trade(str(e))
    min_atr=float(config.get("min_atr_price",0.25))
    if a < min_atr:
        return no_trade(f"ATR(14) is abnormally small for XAUUSD ({a:.5f}); feed/scale validation failed. Minimum accepted ATR is {min_atr:.2f} price units.",
                         {"atr14":round(a,5),"minimum_atr_price":min_atr})
    # Ignore current forming candle; use latest completed M5 candle for trigger.
    closed=m5[:-1]
    trigger=closed[-1]; prior=closed[-2]
    swing=closed[-21:-1]
    swing_low=min(float(b["low"]) for b in swing)
    swing_high=max(float(b["high"]) for b in swing)
    h4_bias=_bias(h4,10); h1_bias=_bias(h1,20); m15_bias=_bias(m15,10)
    mid=(bid+ask)/2
    body=abs(float(trigger["close"])-float(trigger["open"]))
    displaced=body >= 0.8*a
    bullish= float(trigger["close"])>float(trigger["open"])
    bearish= float(trigger["close"])<float(trigger["open"])
    swept_low=float(trigger["low"])<swing_low and float(trigger["close"])>swing_low
    swept_high=float(trigger["high"])>swing_high and float(trigger["close"])<swing_high
    fib_bull=fib["bullish_zone"][1] <= mid <= fib["bullish_zone"][0]
    fib_bear=fib["bearish_zone"][0] <= mid <= fib["bearish_zone"][1]
    structure=structure_state(m5); fvg=fair_value_gaps(m5)
    levels=intraday_session_levels(m5)
    equal_pools=_equal_liquidity_pools(m5,max(0.05*a,0.01))
    bullish_ob=_order_block_proxy(m5,"bullish"); bearish_ob=_order_block_proxy(m5,"bearish")
    pd_levels=previous_day_levels(bars.get("D1",[]))
    buy_targets=[x for x in [swing_high,equal_pools.get("equal_high"),levels.get("asia_high"),levels.get("london_high"),levels.get("new_york_high"),pd_levels.get("high")] if x is not None and float(x)>ask]
    sell_targets=[x for x in [swing_low,equal_pools.get("equal_low"),levels.get("asia_low"),levels.get("london_low"),levels.get("new_york_low"),pd_levels.get("low")] if x is not None and float(x)<bid]
    buy_target=min((float(x) for x in buy_targets),default=None)
    sell_target=max((float(x) for x in sell_targets),default=None)
    context={"h4_bias":h4_bias,"h1_bias":h1_bias,"m15_bias":m15_bias,"atr14":round(a,5),"spread":None if quote.get("quote_type")=="indicative_price_only" else round(spread,5),
             "fib":fib,"swept_low":swept_low,"swept_high":swept_high,"displacement":displaced,
             "in_golden_zone":fib_bull or fib_bear,"order_flow_confirmed":False,
             "trigger_candle_time":trigger.get("time"),"closed_candle_only":True,"structure":structure,"fvg":fvg,
             "bullish_order_block_proxy":bullish_ob,"bearish_order_block_proxy":bearish_ob,"equal_liquidity":equal_pools,
             "previous_day_high":pd_levels.get("high"),"previous_day_low":pd_levels.get("low"),
             "nearest_buy_target":buy_target,"nearest_sell_target":sell_target,**levels}
    # Do not pretend an engulfing/sweep alone establishes inducement or a full reclaim-failure model.
    side=None; reasons=[]
    if h4_bias=="bullish" and h1_bias=="bullish" and m15_bias=="bullish" and swept_low and displaced and bullish and fib_bull:
        side="BUY"
        reasons=["H4, H1 and M15 closed-candle directional bias align bullish",
                 "Completed M5 candle swept a recent swing low and closed back above it",
                 "Completed M5 candle body exceeds 0.8 × ATR(14)",
                 "Mid-price is inside calculated 61.8–65% retracement zone"]
        entry=ask
        structural_stop=min(swing_low,float(trigger["low"]))
        if buy_target is None: return no_trade("No validated opposing liquidity target above entry; no trade.",context)
        try: levels=build_trade_levels("BUY",entry,structural_stop,a,spread,quote.get("tick_size"),config,buy_target)
        except ValueError as e: return no_trade(f"Trade-level validation failed: {e}",context)
        entry,stop,target=levels["entry"],levels["stop_loss"],levels["take_profit"]
    elif h4_bias=="bearish" and h1_bias=="bearish" and m15_bias=="bearish" and swept_high and displaced and bearish and fib_bear:
        side="SELL"
        reasons=["H4, H1 and M15 closed-candle directional bias align bearish",
                 "Completed M5 candle swept a recent swing high and closed back below it",
                 "Completed M5 candle body exceeds 0.8 × ATR(14)",
                 "Mid-price is inside calculated 61.8–65% retracement zone"]
        entry=bid
        structural_stop=max(swing_high,float(trigger["high"]))
        if sell_target is None: return no_trade("No validated opposing liquidity target below entry; no trade.",context)
        try: levels=build_trade_levels("SELL",entry,structural_stop,a,spread,quote.get("tick_size"),config,sell_target)
        except ValueError as e: return no_trade(f"Trade-level validation failed: {e}",context)
        entry,stop,target=levels["entry"],levels["stop_loss"],levels["take_profit"]
    else:
        missing=[]
        if len({h4_bias,h1_bias,m15_bias})!=1 or h4_bias not in ("bullish","bearish"): missing.append("H4/H1/M15 higher-timeframe bias not aligned")
        if not (swept_low or swept_high): missing.append("no completed-candle liquidity sweep/reclaim")
        if not displaced: missing.append("M5 displacement threshold not met")
        if not (fib_bull or fib_bear): missing.append("price outside Fibonacci golden zone")
        return no_trade("No complete setup: "+", ".join(missing)+". Order flow and inducement are not independently confirmed.",context)
    risk_pct=float(config.get("risk_percent",1)); equity=float(config.get("equity",0))
    daily_loss=float(config.get("max_daily_loss",0)); daily_target=float(config.get("daily_target",0)); daily_pnl=float(config.get("realized_pnl_today",0))
    if equity<=0 or risk_pct<=0 or risk_pct>5: return no_trade("Account equity or risk percentage is invalid.",context)
    if daily_loss>0 and daily_pnl <= -daily_loss: return no_trade("Manually reported daily loss limit reached; no new candidate.",context)
    if daily_target>0 and daily_pnl >= daily_target: return no_trade("Manually reported daily target reached; stop for the day.",context)
    risk_cash=equity*risk_pct/100
    if daily_loss>0: risk_cash=min(risk_cash,max(0,daily_loss+daily_pnl))
    if risk_cash<=0: return no_trade("No remaining daily risk budget.",context)
    if not sizing_available:
        return no_trade("Broker tick value, tick size, volume min/max/step are not verified. Configure the actual broker instrument specifications; no trade-ready signal is permitted.",context)
    sizing=None
    try: sizing=size_position(risk_cash,entry,stop,*spec_values)
    except ValueError as e: return no_trade(str(e),context)
    if sizing["volume"]<=0: return no_trade(sizing.get("reason") or "Position size invalid.",context)
    rr=abs(target-entry)/abs(entry-stop)
    min_rr=float(config.get("min_reward_risk",2))
    if rr+1e-9<min_rr: return no_trade("Setup fails minimum reward-to-risk after price-increment rounding.",context)
    # Never emit a tiny stop/target merely because a feed reports a near-zero ATR.
    min_stop=float(config.get("min_stop_distance_price",0.50))
    if abs(entry-stop)+1e-9 < min_stop:
        return no_trade(f"Stop distance {abs(entry-stop):.5f} is below the minimum accepted {min_stop:.2f} XAUUSD price units.",context)
    # Explicitly candidate only. Broker's tick value semantics and currency conversion must be verified.
    return {"status":"CANDIDATE","side":side,"symbol":quote.get("symbol","XAUUSD"),
            "entry":round(entry,5),"stop_loss":round(stop,5),"take_profit":round(target,5),
            "reward_risk":round(rr,2),"atr14":round(a,5),"spread":None if quote.get("quote_type")=="indicative_price_only" else round(spread,5),
            "risk_distance":round(abs(entry-stop),5),"reward_distance":round(abs(target-entry),5),
            "price_levels_are_indicative":quote.get("quote_type")=="indicative_price_only",
            "position_volume":sizing["volume"],
            "estimated_risk_account_currency":sizing["risk_money"],
            "stop_rationale":"Beyond the swept swing extreme plus ATR/spread buffer",
            "target_rationale":"Nearest validated opposing liquidity level, accepted only if it supports the configured minimum reward:risk and ATR distance",
            "reasons":reasons,"caveats":["Candidate only; no order is placed.",
            "This is a price reference, not an executable bid/ask quote, when the source is a chart/webhook feed.",
            "Position size and account-currency risk are calculated only from verified instrument tick/volume specifications in the account currency.",
            "Validate commissions, slippage, margin and broker stop rules before execution.",
            "Price levels use a minimum volatility-aware stop distance; this does not guarantee the stop survives market noise or slippage.",
            "Executable bid/ask and spread are unavailable on price-only feeds; re-check spread at the execution venue.",
            "Order book, footprint, CVD, news, inducement and reclaim failure are not fully verified by this price-action proxy."],
            "context":context}
