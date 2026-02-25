from flask import Flask, request, jsonify, render_template_string
import requests
import os
import time
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
import logging

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────
KALSHI_API_KEY   = os.getenv("KALSHI_API_KEY", "")
KALSHI_BASE_URL  = "https://api.elections.kalshi.com/trade-api/v2"
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "kalshi_bot_secret")
DEFAULT_QUANTITY = int(os.getenv("DEFAULT_QUANTITY", "1"))

# ─── APPROVED SCHEDULE (EST) ──────────────────────────────
APPROVED_HOURS = {
    "Monday":    [7, 9, 10, 12, 17, 18, 19, 22],
    "Tuesday":   [8, 9, 10, 17, 19, 22],
    "Wednesday": [7, 8, 9, 10, 17, 18, 19],
    "Thursday":  [7, 9, 19, 22],
    "Friday":    [7, 8, 9, 10, 12, 13, 17, 19, 22],
    "Saturday":  [9, 10, 12, 17, 18, 19, 22],
    "Sunday":    [9, 10, 12, 13, 17, 18, 19, 22],
}

# ─── BID FORMULA ──────────────────────────────────────────
def calculate_bid(engine_a: float, engine_b: float, vol_ratio: float) -> float:
    avg = (engine_a + engine_b) / 2.0
    if avg <= 30:
        margin = 15
    elif avg <= 40:
        margin = 17
    elif avg <= 50:
        margin = 19
    elif avg <= 60:
        margin = 21
    else:
        margin = 23
    raw_bid = avg - margin
    vol_adjusted = raw_bid / vol_ratio if vol_ratio > 0 else raw_bid
    final_bid = min(vol_adjusted, raw_bid)
    final_bid = max(10.0, min(60.0, final_bid))
    return round(final_bid) / 100.0

# ─── SCHEDULE CHECK (EST) ─────────────────────────────────
def is_approved_hour(day_name: str, hour_est: int) -> bool:
    return hour_est in APPROVED_HOURS.get(day_name, [])

# ─── KALSHI API ───────────────────────────────────────────
def get_kalshi_headers():
    return {
        "Authorization": f"Bearer {KALSHI_API_KEY}",
        "Content-Type": "application/json",
    }

def find_markets(ticker_base: str):
    try:
        url = f"{KALSHI_BASE_URL}/markets"
        params = {"tickers": ticker_base, "status": "open", "limit": 50}
        resp = requests.get(url, headers=get_kalshi_headers(), params=params, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Market search failed: {resp.status_code} {resp.text}")
            return []
        markets = resp.json().get("markets", [])
        logger.info(f"Found {len(markets)} markets for {ticker_base}")
        if markets:
            logger.info(f"Sample market keys: {list(markets[0].keys())}")
            logger.info(f"Sample market: {markets[0]}")
        return markets
    except Exception as e:
        logger.error(f"find_markets error: {e}")
        return []

def place_order(ticker: str, side: str, bid_price: float, quantity: int):
    try:
        url = f"{KALSHI_BASE_URL}/portfolio/orders"
        payload = {
            "ticker": ticker,
            "client_order_id": f"bot_{ticker}_{int(datetime.now().timestamp())}",
            "type": "limit",
            "action": "buy",
            "side": side,
            "count": quantity,
            "yes_price": int(bid_price * 100) if side == "yes" else int((1 - bid_price) * 100),
            "no_price":  int((1 - bid_price) * 100) if side == "yes" else int(bid_price * 100),
        }
        resp = requests.post(url, headers=get_kalshi_headers(), json=payload, timeout=10)
        if resp.status_code in [200, 201]:
            logger.info(f"✅ Order placed: {ticker} {side} @ {bid_price}")
            return {"success": True, "order": resp.json()}
        else:
            logger.error(f"❌ Order failed: {resp.status_code} {resp.text}")
            return {"success": False, "error": resp.text}
    except Exception as e:
        logger.error(f"place_order error: {e}")
        return {"success": False, "error": str(e)}

# ─── TRADE LOG ────────────────────────────────────────────
trade_log = []

def log_trade(data):
    trade_log.insert(0, {**data, "time": datetime.now().strftime("%m/%d %H:%M")})
    if len(trade_log) > 100:
        trade_log.pop()

# ─── BACKGROUND TRADE EXECUTION ───────────────────────────
def execute_trade(asset, price, bid, day_name, est_hour, engine_a, engine_b, vol_ratio):
    """Runs in background thread — retries market search up to 3 times"""
    ticker_base = "KXBTCD" if asset == "BTC" else "KXETHD"
    markets = []

    for attempt in range(3):
        markets = find_markets(ticker_base)
        if markets:
            break
        logger.info(f"No markets yet for {asset}, retry {attempt + 1}/3 in 2 minutes...")
        time.sleep(120)

    if not markets:
        logger.info(f"No markets found for {asset} after 3 attempts")
        log_trade({"status": "no_markets", "asset": asset, "bid": f"${bid:.2f}", "day": day_name, "hour": f"{est_hour:02d}:00"})
        return

    # YES = strikes BELOW current price
    yes_markets = sorted(
        [m for m in markets if float(m.get("strike_value", m.get("cap_strike", m.get("floor_strike", 0)))) < price
         and float(m.get("yes_ask", 100)) / 100.0 <= bid + 0.05],
        key=lambda m: float(m.get("strike_value", m.get("cap_strike", m.get("floor_strike", 0)))),
        reverse=True
    )[:3]

    # NO = strikes ABOVE current price
    no_markets = sorted(
        [m for m in markets if float(m.get("strike_value", m.get("cap_strike", m.get("floor_strike", 0)))) > price
         and float(m.get("no_ask", 100)) / 100.0 <= bid + 0.05],
        key=lambda m: float(m.get("strike_value", m.get("cap_strike", m.get("floor_strike", 0))))
    )[:3]

    logger.info(f"YES markets: {len(yes_markets)} | NO markets: {len(no_markets)}")

    orders_placed = []
    for m in yes_markets:
        result = place_order(m["ticker"], "yes", bid, DEFAULT_QUANTITY)
        orders_placed.append({"ticker": m["ticker"], "side": "yes", "bid": bid, "result": result})

    for m in no_markets:
        result = place_order(m["ticker"], "no", bid, DEFAULT_QUANTITY)
        orders_placed.append({"ticker": m["ticker"], "side": "no", "bid": bid, "result": result})

    log_trade({
        "status": "orders_placed",
        "asset": asset,
        "engineA": engine_a,
        "engineB": engine_b,
        "volRatio": vol_ratio,
        "bid": f"${bid:.2f}",
        "day": day_name,
        "hour": f"{est_hour:02d}:00",
        "orders": len(orders_placed)
    })

# ─── WEBHOOK ENDPOINT ─────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        logger.info(f"Webhook received: {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        engine_a  = float(data.get("engineA", 0))
        engine_b  = float(data.get("engineB", 0))
        vol_ratio = float(data.get("volRatio", 1.0))
        asset     = data.get("asset", "BTC").upper()
        price     = float(data.get("price", 0))

        # Convert UTC to EST
        now_utc  = datetime.utcnow()
        est_hour = (now_utc.hour - 5) % 24
        if now_utc.hour < 5:
            est_dt   = now_utc - timedelta(hours=5)
            day_name = est_dt.strftime("%A")
        else:
            day_name = now_utc.strftime("%A")

        logger.info(f"EST time: {day_name} {est_hour:02d}:00")

        # Check schedule
        if not is_approved_hour(day_name, est_hour):
            msg = f"⏭ Skipped {day_name} {est_hour:02d}:00 EST — not approved"
            logger.info(msg)
            log_trade({"status": "skipped", "reason": msg, "asset": asset})
            return jsonify({"status": "skipped", "reason": msg})

        # Calculate bid
        bid = calculate_bid(engine_a, engine_b, vol_ratio)
        logger.info(f"📊 {asset} | EngA:{engine_a}% EngB:{engine_b}% | Bid:${bid:.2f} | {day_name} {est_hour:02d}:00 EST")

        # Fire and forget — background thread handles retries
        t = threading.Thread(target=execute_trade, args=(asset, price, bid, day_name, est_hour, engine_a, engine_b, vol_ratio))
        t.daemon = True
        t.start()

        return jsonify({"status": "processing", "bid": bid, "asset": asset})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ─── DASHBOARD ────────────────────────────────────────────
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Bot</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&display=swap');
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#08080f; color:#fff; font-family:'DM Mono',monospace; padding:16px; }
  .title { font-family:'Bebas Neue',sans-serif; font-size:28px; color:#00e87a; letter-spacing:3px; text-align:center; padding:16px 0 4px; }
  .sub { text-align:center; font-size:10px; color:#333355; letter-spacing:2px; margin-bottom:16px; }
  .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:16px; }
  .stat { background:#0e0e1a; border:1px solid #1a1a2e; border-radius:12px; padding:12px 8px; text-align:center; }
  .stat-n { font-family:'Bebas Neue',sans-serif; font-size:22px; color:#00e87a; }
  .stat-l { font-size:9px; color:#333355; letter-spacing:1px; margin-top:2px; }
  .section { font-size:9px; color:#222244; letter-spacing:2px; margin-bottom:8px; }
  .log-item { background:#0e0e1a; border:1px solid #1a1a2e; border-radius:10px; padding:12px 14px; margin-bottom:8px; }
  .log-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; }
  .log-asset { font-family:'Bebas Neue',sans-serif; font-size:18px; color:#fff; letter-spacing:2px; }
  .log-time { font-size:10px; color:#333355; }
  .log-detail { font-size:11px; color:#555577; }
  .badge { font-size:9px; padding:3px 8px; border-radius:8px; }
  .badge-placed { background:rgba(0,232,122,0.1); border:1px solid rgba(0,232,122,0.3); color:#00e87a; }
  .badge-skipped { background:rgba(255,150,0,0.1); border:1px solid rgba(255,150,0,0.3); color:#ff9600; }
  .badge-error { background:rgba(255,50,50,0.1); border:1px solid rgba(255,50,50,0.3); color:#ff3232; }
  .refresh { text-align:center; margin-top:16px; }
  .refresh a { color:#00e87a; font-size:11px; text-decoration:none; letter-spacing:1px; }
  .status-dot { width:8px; height:8px; border-radius:50%; background:#00e87a; display:inline-block; margin-right:6px; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
</style>
</head>
<body>
<div class="title">KALSHI BOT</div>
<div class="sub"><span class="status-dot"></span>LIVE · AUTO-TRADING ACTIVE</div>
<div class="stats">
  <div class="stat"><div class="stat-n">{{ total_orders }}</div><div class="stat-l">ORDERS TODAY</div></div>
  <div class="stat"><div class="stat-n">{{ total_skipped }}</div><div class="stat-l">SKIPPED</div></div>
  <div class="stat"><div class="stat-n">{{ quantity }}</div><div class="stat-l">CONTRACTS</div></div>
</div>
<div class="section">RECENT ACTIVITY</div>
{% for trade in trades %}
<div class="log-item">
  <div class="log-header">
    <div style="display:flex;align-items:center;gap:8px">
      <div class="log-asset">{{ trade.get('asset','—') }}</div>
      <span class="badge {{ 'badge-placed' if trade.status == 'orders_placed' else ('badge-skipped' if trade.status == 'skipped' else 'badge-error') }}">
        {{ 'PLACED' if trade.status == 'orders_placed' else ('SKIPPED' if trade.status == 'skipped' else trade.status.upper()) }}
      </span>
    </div>
    <div class="log-time">{{ trade.time }}</div>
  </div>
  <div class="log-detail">
    {% if trade.status == 'orders_placed' %}
      Bid: {{ trade.bid }} · EngA: {{ trade.engineA }}% · EngB: {{ trade.engineB }}% · {{ trade.get('orders',0) }} orders · {{ trade.day }} {{ trade.hour }}
    {% else %}
      {{ trade.get('reason', trade.status) }}
    {% endif %}
  </div>
</div>
{% endfor %}
{% if not trades %}
<div class="log-item"><div class="log-detail" style="text-align:center;padding:8px 0;">No activity yet — waiting for TradingView alerts</div></div>
{% endif %}
<div class="refresh"><a href="/">↻ REFRESH</a></div>
</body>
</html>
"""

@app.route("/")
def dashboard():
    total_orders  = sum(1 for t in trade_log if t.get("status") == "orders_placed")
    total_skipped = sum(1 for t in trade_log if t.get("status") == "skipped")
    return render_template_string(DASHBOARD_HTML,
        trades=trade_log[:20],
        total_orders=total_orders,
        total_skipped=total_skipped,
        quantity=DEFAULT_QUANTITY
    )

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
