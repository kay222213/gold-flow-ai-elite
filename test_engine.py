import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from engine import atr, fib_golden_zone, size_position, generate_signal
from market_context import session_name, high_low

def bars(n=110, base=2000.0, step=0.2, interval=300):
    out=[]
    start=int(time.time())-n*interval
    for i in range(n):
        c=base+i*step
        out.append({"time":start+i*interval,"open":c-step/2,"high":c+0.4,"low":c-0.4,"close":c,"tick_volume":100})
    return out

def test_atr_positive():
    assert atr(bars(),14)>0

def test_fib_zone_is_ordered():
    z=fib_golden_zone(bars())
    assert z["swing_high"]>z["swing_low"]
    assert z["bullish_zone"][0]>z["bullish_zone"][1]
    assert z["bearish_zone"][0]<z["bearish_zone"][1]

def test_size_position_never_rounds_up_risk():
    s=size_position(10,2000,1990,0.01,1,0.01,100,0.01)
    assert s["volume"]>0
    assert s["risk_money"]<=10

def test_missing_data_is_no_trade():
    s=generate_signal({}, {}, {"equity":1000})
    assert s["status"]=="NO TRADE"

def test_missing_contract_specs_never_invents_position_size():
    q={"bid":2000,"ask":2000.2,"age_seconds":0,"symbol":"XAUUSD"}
    s=generate_signal({"M5":bars(),"M15":bars(),"H1":bars()},q,{"equity":1000})
    # The fixture has no valid sweep setup, so it must remain NO TRADE; no fake volume/risk is invented.
    assert s["status"]=="NO TRADE"
    assert s.get("position_volume") is None

def test_stale_quote_is_no_trade():
    q={"bid":2000,"ask":2000.2,"age_seconds":20,"symbol":"XAUUSD",
       "tick_size":.01,"tick_value":1,"volume_min":.01,"volume_max":100,"volume_step":.01}
    b=bars()
    assert generate_signal({"M5":b,"M15":b,"H1":b},q,{"equity":1000,"max_quote_age_seconds":5})["status"]=="NO TRADE"

def test_excess_spread_is_no_trade():
    q={"bid":2000,"ask":2002,"age_seconds":0,"symbol":"XAUUSD",
       "tick_size":.01,"tick_value":1,"volume_min":.01,"volume_max":100,"volume_step":.01}
    b=bars()
    assert generate_signal({"M5":b,"M15":b,"H1":b},q,{"equity":1000,"max_spread_price":.5})["status"]=="NO TRADE"

def test_bad_ohlc_is_no_trade():
    b=bars(); b[-2]["low"]=3000
    q={"bid":2000,"ask":2000.1,"age_seconds":0,"symbol":"XAUUSD",
       "tick_size":.01,"tick_value":1,"volume_min":.01,"volume_max":100,"volume_step":.01}
    assert generate_signal({"M5":b,"M15":bars(),"H1":bars()},q,{"equity":1000})["status"]=="NO TRADE"

def test_session_name_is_string():
    assert isinstance(session_name(),str)

def test_high_low():
    h=high_low([{"high":5,"low":2},{"high":7,"low":3}])
    assert h=={"high":7.0,"low":2.0}


def test_timestamp_is_required_for_candles():
    b=bars()
    for candle in b:
        candle.pop("time", None)
    q={"bid":2000,"ask":2000.1,"age_seconds":0,"symbol":"XAUUSD"}
    s=generate_signal({"M5":b,"M15":bars(),"H1":bars(),"H4":bars()},q,{"equity":1000})
    assert s["status"] == "NO TRADE"


def test_future_quote_is_rejected():
    q={"bid":2000,"ask":2000.1,"age_seconds":-2,"symbol":"XAUUSD"}
    b=bars()
    s=generate_signal({"M5":b,"M15":bars(),"H1":bars(),"H4":bars()},q,{"equity":1000})
    assert s["status"] == "NO TRADE"


def test_trade_levels_reject_cent_stop_by_enforcing_atr_and_minimum_distance():
    from engine import build_trade_levels
    levels = build_trade_levels("BUY", 4015.70, 4015.69, 1.20, 0.0, 0.01,
                                {"min_stop_distance_price": 0.50,
                                 "min_stop_atr_multiple": 1.0,
                                 "stop_buffer_atr_multiple": 0.20,
                                 "target_atr_multiple": 1.50}, target_level=4019.00)
    assert levels["entry"] - levels["stop_loss"] >= 1.19
    assert levels["take_profit"] - levels["entry"] >= 2.0 * (levels["entry"] - levels["stop_loss"])
    assert levels["reward_risk"] >= 2.0


def test_sell_trade_levels_are_rounded_outward_and_keep_minimum_rr():
    from engine import build_trade_levels
    levels = build_trade_levels("SELL", 4015.70, 4015.71, 0.80, 0.0, 0.01,
                                {"min_stop_distance_price": 0.50,
                                 "min_stop_atr_multiple": 1.0,
                                 "stop_buffer_atr_multiple": 0.20,
                                 "target_atr_multiple": 1.50}, target_level=4012.00)
    assert levels["stop_loss"] - levels["entry"] >= 0.79
    assert levels["entry"] - levels["take_profit"] >= 2.0 * (levels["stop_loss"] - levels["entry"])
    assert levels["reward_risk"] >= 2.0


def test_risk_percent_does_not_change_price_levels():
    from engine import build_trade_levels
    a = build_trade_levels("BUY", 4015.70, 4014.90, 1.00, 0.10, 0.01,
                           {"risk_percent": 1, "min_stop_distance_price": 0.50}, target_level=4018.00)
    b = build_trade_levels("BUY", 4015.70, 4014.90, 1.00, 0.10, 0.01,
                           {"risk_percent": 3, "min_stop_distance_price": 0.50}, target_level=4018.00)
    assert (a["entry"], a["stop_loss"], a["take_profit"]) == (b["entry"], b["stop_loss"], b["take_profit"])


def test_stale_completed_candles_are_no_trade_even_with_fresh_quote():
    old = bars()
    for candle in old: candle["time"] -= 3600
    quote={"bid":2020,"ask":2020.1,"age_seconds":0,"symbol":"XAUUSD",
           "tick_size":0.01,"tick_value":1,"volume_min":0.01,"volume_max":100,"volume_step":0.01}
    result=generate_signal({"M5":old,"M15":bars(interval=900),"H1":bars(interval=3600),"H4":bars(interval=14400)},quote,
                           {"equity":1000,"max_quote_age_seconds":90})
    assert result["status"] == "NO TRADE"
    assert "stale" in result["reason"].lower()


def test_fresh_multitimeframe_data_builds_context_without_exception():
    m5=bars(base=2000,step=0.2,interval=300)
    m15=bars(base=2000,step=0.2,interval=900)
    h1=bars(base=2000,step=0.2,interval=3600)
    h4=bars(base=2000,step=0.2,interval=14400)
    q={"bid":2021.7,"ask":2021.8,"age_seconds":1,"symbol":"XAUUSD",
       "tick_size":0.01,"tick_value":1,"volume_min":0.01,"volume_max":100,"volume_step":0.01}
    result=generate_signal({"M5":m5,"M15":m15,"H1":h1,"H4":h4,"D1":bars(interval=86400)},q,
                           {"equity":1000,"risk_percent":1,"max_quote_age_seconds":90})
    assert result["status"] == "NO TRADE"
    assert "context" in result
