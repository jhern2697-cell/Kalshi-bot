from flask import Flask, request, jsonify, render_template_string
import requests
import os
import json
from datetime import datetime, timezone
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
DEFAULT_QUANTITY = int(os.getenv("DEFAULT_QUANTITY", "25"))

# ─── YOUR APPROVED SCHEDULE ───────────────────────────────
# Based on your 7-week backtest analysis
APPROVED_HOURS = {
    "Monday":    [7, 9, 10, 12, 17, 18, 19, 22],
    "Tuesday":   [8, 9, 10, 17, 19, 22],
    "Wednesday": [7, 8, 9, 10, 17, 18, 19],
    "Thursday":  [7, 9, 19, 22],
    "Friday":    [7, 8, 9, 10, 12, 13, 17, 19, 22],
    "Saturday":  [9, 10, 12, 17, 18, 19, 22],
    "Sunday":    [9, 10, 12, 13, 17, 18, 19, 22],
}

# ─── BID FORMULA (your exact logic) ───────────────────────
def calculate_bid(engine_a: float, engine_b: float, vol_ratio: float) -> float:
    avg = (engine_a + engine_b) / 2.0

    # Your margin subtraction scale
    if avg < 35:
        margin = 15
    elif avg < 45:
        margin = 17
    elif avg < 55:
        margin = 19
    elif avg < 65:
        margin = 21
    else:
        margin = 23

    raw_bid = avg - margin

    # Vol ratio adjustment
    vol_adjusted = raw_bid / vol_ratio if vol_ratio > 0 else raw_bid
    final_bid = min(vol_adjusted, raw_bid)

    # Clamp to your 20c-60c range
    final_bid = max(20.0, min(60.0, final_bid))
    return round(final_bid) / 100.0  # return as decimal e.g. 0.37

# ─── SCHEDULE CHECK ───────────────────────────────────────
def is_approved_hour(day_name: str, hour: int) -> bool:
    approved = APPROVED_HOURS.get(day_name, [])
    return hour in approved

# ─── KALSHI API ───────────────────────────────────────────
def get_kalshi_headers():
    return {
        "Authorization": f"Bearer {KALSHI_API_KEY}",
        "Content-Type": "application/json",
    }

def find_market(ticker_base: str, target_price: float, side: str, current_hour: int):
    """Find relevant Kalshi markets for this hour"""
    try:
        # Search for markets matching this asset and hour
        url = f"{KALSHI_BASE_URL}/markets"
        params = {
            "tickers": ticker_base,
            "status": "open",
            "limit": 20
        }
        resp = requests.get(url, headers=get_kalshi_headers(), params=params, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Market search failed: {resp.text}")
            return []
        
        markets = resp.json().get("markets", [])
        logger.info(f"Found {len(markets)} markets for {ticker_base}")
        return markets
    except Exception as e:
        logger.error(f"find_market error: {e}")
        return []

def place_order(ticker: str, side: str, bid_price: float, quantity: int):
    """Place a limit order on Kalshi"""
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

# ─── WEBHOOK ENDPOINT ─────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        logger.info(f"Webhook received: {data}")

        # Validate secret
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        # Extract values from TradingView alert
        engine_a  = float(data.get("engineA", 0))
        engine_b  = float(data.get("engineB", 0))
        vol_ratio = float(data.get("volRatio", 1.0))
        asset     = data.get("asset", "BTC").upper()  # "BTC" or "ETH"
        price     = float(data.get("price", 0))

        # Get current day/hour in EST
        now      = datetime.now()
        day_name = now.strftime("%A")
        hour     = now.hour

        # Check schedule
        if not is_approved_hour(day_name, hour):
            msg = f"⏭ Skipped {day_name} {hour:02d}:00 — not in approved schedule"
            logger.info(msg)
            log_trade({"status": "skipped", "reason": f"{day_name} {hour:02d}:00 not approved", "asset": asset})
            return jsonify({"status": "skipped", "reason": msg})

        # Calculate bid
        bid = calculate_bid(engine_a, engine_b, vol_ratio)

        # Check bid is in valid range
        if bid < 0.20 or bid > 0.60:
            msg = f"⏭ Bid {bid:.2f} out of range"
            logger.info(msg)
            log_trade({"status": "skipped", "reason": f"bid {bid:.2f} out of range", "asset": asset})
            return jsonify({"status": "skipped", "reason": msg})

        logger.info(f"📊 {asset} | EngA:{engine_a}% EngB:{engine_b}% | Bid:{bid:.2f} | {day_name} {hour:02d}:00")

        # Determine ticker base
        ticker_base = "KXBTCD" if asset == "BTC" else "KXETHD"

        # Find available markets
        markets = find_market(ticker_base, price, "yes", hour)

        orders_placed = []

        if not markets:
            # Log attempt even if no markets found
            log_trade({
                "status": "no_markets",
                "asset": asset,
                "engineA": engine_a,
                "engineB": engine_b,
                "bid": f"{bid:.2f}",
                "day": day_name,
                "hour": f"{hour:02d}:00"
            })
            return jsonify({"status": "no_markets_found", "bid": bid})

        # Place YES and NO bracket orders on up to 3 markets each side
        yes_markets = [m for m in markets if float(m.get("yes_ask", 1)) <= bid + 0.05][:3]
        no_markets  = [m for m in markets if float(m.get("no_ask", 1)) <= bid + 0.05][:3]

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
            "hour": f"{hour:02d}:00",
            "orders": len(orders_placed)
        })

        return jsonify({
            "status": "success",
            "bid": bid,
            "orders_placed": len(orders_placed),
            "details": orders_placed
        })

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
