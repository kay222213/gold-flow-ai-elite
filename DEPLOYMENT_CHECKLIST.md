# Deployment and activation checklist

This repository is designed to run safely without secrets configured: feeds that cannot authenticate remain disconnected and the decision engine fails closed. **No software bundle can activate paid provider accounts or deploy itself into your hosting account without authorization.**

## 1. Run locally

- Install Python 3.11 or 3.12.
- Run `start_local_windows.bat` on Windows, or `./start_local.sh` on macOS/Linux.
- The first run creates `backend/.env.example` as `backend/.env` and exits so you can configure keys.
- Add keys only in `backend/.env`; never commit it.
- Start again. Open `http://127.0.0.1:8000/api/health` and serve `frontend/` on port 5500 in a second terminal with `python -m http.server 5500` from the repository root.
- Open `http://127.0.0.1:5500/frontend/` and set the API URL to `http://127.0.0.1:8000`.

## 2. Required providers

- `TWELVEDATA_API_KEY`: XAU/USD quotes and historical candles.
- `FRED_API_KEY`: Treasury-yield and broad-dollar macro context.
- `TRADING_ECONOMICS_API_KEY`: economic calendar.
- `DATABENTO_API_KEY`: live CME GC trade flow, with the correct dataset and entitlement. GC is a futures proxy for OTC spot gold, not the same market.
- `BROKER_TICK_SIZE`, `BROKER_TICK_VALUE_ACCOUNT_CCY`, `BROKER_VOLUME_MIN`, `BROKER_VOLUME_MAX`, `BROKER_VOLUME_STEP`, `BROKER_ACCOUNT_CURRENCY`: exact XAUUSD contract specifications from your broker, used for monetary risk and lot sizing. If missing, trade-ready candidates are blocked.
- `TRADINGVIEW_WEBHOOK_TOKEN`: use the generated server secret in Pine alert configuration.
- `ADMIN_API_TOKEN`: private risk-settings writes. Keep this private; do not put it into a public shared screenshot or source file.

GDELT news retrieval is attempted without a key. Provider availability and data plans can change; check the health endpoint and provider's own dashboard.

## 3. Deploy backend

- Push the repository to GitHub.
- Create a Render Web Service from this repository using `render.yaml`.
- Add provider keys in Render's environment-variable dashboard. Do not commit secrets.
- Set `CORS_ORIGINS` to the exact origin of the frontend host, for example `https://YOUR_GITHUB_USERNAME.github.io` (origin only, no repository path).
- Verify `https://YOUR_BACKEND_HOST/api/health`.

## 4. Deploy frontend

- Publish the `frontend/` directory with GitHub Pages or another static host.
- Open the site, select the Data/Connections panel, enter the deployed backend HTTPS URL and save it.
- Check the market provider status, quote timestamp, macro/calendar/news statuses, and order-flow timestamp.

## 5. Go/no-go verification

- Compare XAU/USD quote and bars to the intended TradingView feed; venue prices can differ.
- Check that timestamps advance and stale quotes cause `NO TRADE`.
- Verify Databento trade side/delta against its own records; CME GC flow is a futures proxy, not OTC spot order flow. Stale or ambiguously classified flow must block a candidate.
- Validate sizing with the actual broker's contract specs, account currency, commissions, margin and execution venue. The app intentionally withholds monetary sizing when specs are unknown.
- Run out-of-sample and walk-forward backtests with realistic spread/slippage/fees, then demo forward-test.
- Do not use a `CANDIDATE` as a guaranteed or automated order. The app never sends broker orders.
