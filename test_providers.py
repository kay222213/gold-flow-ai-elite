import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tradingview_webhook import TradingViewWebhookProvider

def test_tradingview_webhook_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv('TRADINGVIEW_WEBHOOK_TOKEN','x'*32)
    p=TradingViewWebhookProvider()
    try: p.accept({'token':'bad','timeframe':'5','time':1760000000000,'open':1,'high':2,'low':.5,'close':1.5})
    except PermissionError: pass
    else: raise AssertionError('bad webhook token accepted')

def test_tradingview_webhook_normalizes_milliseconds(monkeypatch):
    monkeypatch.setenv('TRADINGVIEW_WEBHOOK_TOKEN','x'*32)
    p=TradingViewWebhookProvider()
    p.accept({'token':'x'*32,'ticker':'OANDA:XAUUSD','timeframe':'5','time':1760000000000,'open':2000,'high':2002,'low':1999,'close':2001,'volume':10})
    assert p.bars['M5'][0]['time']==1760000000
    assert p.latest['price']==2001


def test_settings_endpoint_requires_admin_token(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    monkeypatch.setenv('MARKET_DATA_SOURCE','none')
    monkeypatch.setenv('ADMIN_API_TOKEN','z'*40)
    with TestClient(app.app) as c:
        assert c.post('/api/settings',json={'risk_percent':1}).status_code==401
        assert c.post('/api/settings',json={'risk_percent':1},headers={'X-Admin-Token':'z'*40}).status_code==200
