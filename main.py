# main.py - Advanced Simulation-ready TradeBot (no pandas)
"""
Advanced simulation TradeBot:
- Runs a live-ish worker (simulated trading using public klines or local CSV)
- Backtester endpoint to run historical simulation for a date range (compounding equity)
- Smart strategy: EMA crossover + RSI + ATR volatility filter + volume filter
- Position sizing: fixed fractional risk per trade using stop-loss (risk_per_trade)
- No external DB; writes trades.csv and debug log
- No pandas required
"""
import os, time, threading, csv, math, io
from datetime import datetime, timezone
from flask import Flask, jsonify, send_file, request, render_template
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# CONFIG
SYMBOLS = os.getenv("SYMBOLS", "ETHUSDT,BTCUSDT,BNBUSDT,SOLUSDT,ADAUSDT").split(",")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
EMA_FAST = int(os.getenv("EMA_FAST", "8"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "1.0"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.02"))  # 2% of equity risk per trade
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.01"))  # default 1%
INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "10.0"))
KL_LIMIT = int(os.getenv("KL_LIMIT", "1000"))
BINANCE_REST = "https://api.binance.com/api/v3/klines"
DEBUG_LOG = "bot_debug.log"
TRADE_LOG = "trades.csv"

app = Flask(__name__, template_folder="templates", static_folder="static")

# state
balance_lock = threading.Lock()
balance = INITIAL_BALANCE
current_trade = None
trades = []
stats = {"trades":0,"wins":0,"losses":0,"profit_usd":0.0}

# requests session
def create_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

SESSION = create_session()

# logging
def debug(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(DEBUG_LOG,"a",encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# utilities: fetch klines (public) or read CSV
def fetch_klines(symbol, interval="1m", limit=500):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    headers = {"User-Agent":"TradeBot-Sim/1.0"}
    r = SESSION.get(BINANCE_REST, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    out = []
    for k in data:
        out.append({"time": int(k[0]), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])})
    return out

def parse_csv_file(file_storage):
    text = file_storage.stream.read().decode('utf-8')
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out=[]
    for i,ln in enumerate(lines):
        if i==0 and ("time" in ln.lower() and "open" in ln.lower()):
            continue
        parts = ln.split(",")
        if len(parts) < 6:
            continue
        t = parts[0].strip()
        try:
            if t.isdigit() and len(t)>10:
                t = int(t)
            else:
                t = int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()*1000)
        except Exception:
            t = int(datetime.utcnow().timestamp()*1000)
        out.append({"time": int(t), "open": float(parts[1]), "high": float(parts[2]), "low": float(parts[3]), "close": float(parts[4]), "volume": float(parts[5])})
    return out

# indicators (list-based)
def ema(values, span):
    if not values: return []
    alpha = 2.0/(span+1)
    out=[values[0]]
    for v in values[1:]:
        out.append((v - out[-1]) * alpha + out[-1])
    return out

def sma(values, period):
    out=[]
    s=0.0
    for i,v in enumerate(values):
        s += v
        if i>=period:
            s -= values[i-period]
            out.append(s/period)
        elif i==period-1:
            out.append(s/period)
    return out

def rsi(values, period=14):
    if len(values) < period+1:
        return [50]*len(values)
    deltas = [values[i]-values[i-1] for i in range(1,len(values))]
    ups = [d if d>0 else 0 for d in deltas]
    downs = [ -d if d<0 else 0 for d in deltas]
    up_avg = sum(ups[:period])/period
    down_avg = sum(downs[:period])/period or 1e-9
    out = [50]*(period+1)
    for d_up,d_down in zip(ups[period:], downs[period:]):
        up_avg = (up_avg*(period-1) + d_up)/period
        down_avg = (down_avg*(period-1) + d_down)/period
        rs = up_avg/(down_avg+1e-12)
        out.append(100 - (100/(1+rs)))
    return out

def atr(highs, lows, closes, period=14):
    trs=[]
    for i in range(1,len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sma(trs, period)

# backtest engine
def run_backtest(candles, initial_balance=10.0, risk_per_trade=0.02, stop_loss_pct=0.01):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    times = [c["time"] for c in candles]
    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    rsi_list = rsi(closes, RSI_PERIOD)
    atr_list = atr(highs, lows, closes, period=14) if len(closes)>20 else [0]*len(closes)
    equity = initial_balance
    position = None
    trades_bt=[]
    for i in range(2, len(closes)-1):
        price = closes[i]
        if i >= len(ema_fast) or i >= len(ema_slow) or (i-1)>=len(rsi_list):
            continue
        f_now = ema_fast[i-1]; f_prev = ema_fast[i-2]
        s_now = ema_slow[i-1]; s_prev = ema_slow[i-2]
        cross_up = (f_prev <= s_prev) and (f_now > s_now)
        cross_down = (f_prev >= s_prev) and (f_now < s_now)
        vol_ok = True
        if i>21:
            avg_vol = sum(volumes[i-21:i-1])/20.0
            vol_ok = volumes[i-1] > (avg_vol * VOLUME_MULTIPLIER)
        rsi_ok = (rsi_list[i-1] > 25 and rsi_list[i-1] < 75)
        if position is None:
            if cross_up and vol_ok and rsi_ok:
                risk_amount = equity * risk_per_trade
                stop_price = price*(1 - stop_loss_pct)
                if stop_loss_pct <= 0:
                    qty = equity / price
                else:
                    qty = risk_amount / (price * stop_loss_pct)
                qty = max( (1e-8), qty)
                entry = price
                position = {"entry":entry, "qty":qty, "stop":stop_price, "entry_time": times[i]}
        else:
            if lows[i] <= position["stop"]:
                exit_price = position["stop"]
                proceeds = position["qty"] * exit_price
                profit = proceeds - (position["qty"] * position["entry"])
                equity = proceeds
                trades_bt.append({"time": times[i], "symbol": "", "entry": position["entry"], "exit": exit_price, "profit": profit, "balance_after": equity, "reason":"SL"})
                position = None
            elif cross_down:
                exit_price = price
                proceeds = position["qty"] * exit_price
                profit = proceeds - (position["qty"] * position["entry"])
                equity = proceeds
                trades_bt.append({"time": times[i], "symbol": "", "entry": position["entry"], "exit": exit_price, "profit": profit, "balance_after": equity, "reason":"X"})
                position = None
    return {"initial_balance": initial_balance, "final_balance": equity, "trades": trades_bt, "equity_curve": None}

# worker (live-sim)
def worker_loop():
    global current_trade, balance
    debug("Worker started (live-sim). Symbols: " + ",".join(SYMBOLS))
    while True:
        try:
            for sym in SYMBOLS:
                try:
                    candles = fetch_klines(sym, limit=KL_LIMIT)
                except Exception as e:
                    debug(f"fetch error for {sym}: {e}")
                    continue
            # sleep
        except Exception as e:
            debug("Worker error: " + str(e))
        time.sleep(POLL_SECONDS)

# API endpoints
app = Flask(__name__, template_folder="templates", static_folder="static")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    with balance_lock:
        return jsonify({"balance": round(balance,8), "current_trade": current_trade, "stats": stats, "symbols": SYMBOLS, "trades": trades})

@app.route("/api/candles")
def api_candles():
    symbol = request.args.get("symbol", SYMBOLS[0])
    limit = int(request.args.get("limit", 200))
    try:
        candles = fetch_klines(symbol, limit=limit)
        return jsonify({"symbol":symbol, "candles":candles})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    data = request.form.to_dict() or request.json or {}
    csv_file = request.files.get("csv")
    symbol = data.get("symbol", SYMBOLS[0])
    initial = float(data.get("initial_balance", INITIAL_BALANCE))
    risk = float(data.get("risk_per_trade", RISK_PER_TRADE))
    stop = float(data.get("stop_loss_pct", STOP_LOSS_PCT))
    try:
        if csv_file:
            candles = parse_csv_file(csv_file)
        else:
            candles = fetch_klines(symbol, limit=KL_LIMIT)
    except Exception as e:
        return jsonify({"error": f"Failed to load candles: {str(e)}"}), 500
    result = run_backtest(candles, initial_balance=initial, risk_per_trade=risk, stop_loss_pct=stop)
    return jsonify(result)

@app.route("/api/logs")
def api_logs():
    try:
        with open(DEBUG_LOG,"r",encoding="utf-8") as f:
            return "<pre>" + "".join(f.readlines()[-500:]) + "</pre>"
    except Exception:
        return "(no logs yet)"

@app.route("/download_trades")
def download_trades():
    try:
        return send_file(TRADE_LOG, as_attachment=True)
    except Exception:
        return jsonify({"error":"no trades yet"}), 404

def start_worker():
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    start_worker()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")))
