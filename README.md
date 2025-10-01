TradeBot - Advanced Simulation package (ready)

This package contains a ready-to-deploy simulation-focused trading bot that:
- Runs live-sim monitoring (public klines) and a background worker.
- Provides a backtester endpoint (/api/backtest) to run historical simulations with compounding.
- Uses no pandas; pure Python indicators (EMA, RSI, ATR approximation).
- UI included (templates + static) to run backtests and view results.

Deployment:
1. Upload repository to GitHub.
2. Create a Render Web Service (Python 3.11), Build: pip install -r requirements.txt, Start: gunicorn main:app --bind 0.0.0.0:$PORT
3. If Render cannot fetch Binance (451), upload CSV historical data via the backtest upload form (or use a VPS).

Backtest usage:
POST /api/backtest with JSON body or form-data fields:
- symbol (optional)
- initial_balance
- risk_per_trade
- stop_loss_pct
- csv file with form key 'csv' (optional)

Example:
curl -X POST https://your-service.onrender.com/api/backtest -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT","initial_balance":10,"risk_per_trade":0.02}'
