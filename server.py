import os
import hmac
import time
import jwt
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone

app = Flask(__name__)

# ── ENV VARS ────────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
COINBASE_API_KEY = os.environ.get("COINBASE_API_KEY", "").strip()

raw_key = os.environ.get("COINBASE_PRIVATE_KEY", "")
COINBASE_PRIVATE_KEY = raw_key.replace("\\n", "\n").strip()

# Startup debug
if not WEBHOOK_SECRET:
    print("[STARTUP ERROR] Missing WEBHOOK_SECRET")
else:
    print("[STARTUP OK] WEBHOOK_SECRET loaded")

if not COINBASE_API_KEY:
    print("[STARTUP ERROR] Missing COINBASE_API_KEY")
else:
    print("[STARTUP OK] COINBASE_API_KEY loaded")

if not COINBASE_PRIVATE_KEY:
    print("[STARTUP ERROR] Missing COINBASE_PRIVATE_KEY")
elif "BEGIN EC PRIVATE KEY" not in COINBASE_PRIVATE_KEY and "BEGIN PRIVATE KEY" not in COINBASE_PRIVATE_KEY:
    print("[STARTUP ERROR] COINBASE_PRIVATE_KEY format looks wrong")
else:
    print("[STARTUP OK] COINBASE_PRIVATE_KEY loaded")


# ── SETTINGS ─────────────────────────────────────────────────────────────────
PRODUCT_ID = "XRP-USD"   # Coinbase product ID
RISK_PERCENT = 0.01      # 1% equity risk per trade
REWARD_RATIO = 2.0       # 2:1 reward-to-risk


# ── POSITION TRACKER ─────────────────────────────────────────────────────────
open_positions = {}
# Format:
# {
#   "strategy_1": {
#       "side": "BUY",
#       "entry": 0.50,
#       "sl": 0.48,
#       "tp": 0.54,
#       "size": 100
#   }
# }


# ── COINBASE AUTH ────────────────────────────────────────────────────────────
def build_jwt(method, path):
    """Generate a short-lived JWT for Coinbase Advanced Trade API."""
    if not COINBASE_API_KEY or not COINBASE_PRIVATE_KEY:
        raise ValueError("Missing Coinbase API credentials")

    uri = f"{method} api.coinbase.com{path}"
    payload = {
        "sub": COINBASE_API_KEY,
        "iss": "cdp",
        "nbf": int(time.time()),
        "exp": int(time.time()) + 120,
        "uri": uri,
    }

    token = jwt.encode(
        payload,
        COINBASE_PRIVATE_KEY,
        algorithm="ES256",
        headers={
            "kid": COINBASE_API_KEY,
            "nonce": str(int(time.time() * 1000))
        },
    )
    return token


def coinbase_request(method, path, body=None):
    """Make an authenticated request to the Coinbase Advanced Trade API."""
    try:
        token = build_jwt(method, path)
        url = f"https://api.coinbase.com{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        resp = requests.request(method, url, headers=headers, json=body, timeout=20)

        try:
            return resp.json()
        except Exception:
            return {
                "success": False,
                "status_code": resp.status_code,
                "text": resp.text
            }
    except Exception as e:
        print(f"[COINBASE ERROR] {e}")
        return {"success": False, "error": str(e)}


# ── ACCOUNT HELPERS ──────────────────────────────────────────────────────────
def get_usd_balance():
    """Return available USD balance."""
    data = coinbase_request("GET", "/api/v3/brokerage/accounts")
    for acct in data.get("accounts", []):
        if acct.get("currency") == "USD":
            return float(acct["available_balance"]["value"])
    return 0.0


def get_xrp_price():
    """Return current XRP-USD mid price."""
    data = coinbase_request("GET", f"/api/v3/brokerage/best_bid_ask?product_ids={PRODUCT_ID}")
    pricebooks = data.get("pricebooks", [])
    if pricebooks and pricebooks[0].get("bids") and pricebooks[0].get("asks"):
        best_bid = float(pricebooks[0]["bids"][0]["price"])
        best_ask = float(pricebooks[0]["asks"][0]["price"])
        return (best_bid + best_ask) / 2
    return None


# ── ORDER EXECUTION ──────────────────────────────────────────────────────────
def place_market_order(side, base_size):
    """Place a market buy or sell order."""
    if side == "BUY":
        order_config = {
            "market_market_ioc": {
                "quote_size": str(round(base_size, 2))  # USD amount for buys
            }
        }
    else:
        order_config = {
            "market_market_ioc": {
                "base_size": str(round(base_size, 2))   # XRP quantity for sells
            }
        }

    body = {
        "client_order_id": f"xrp-bot-{int(time.time() * 1000)}",
        "product_id": PRODUCT_ID,
        "side": side,
        "order_configuration": order_config,
    }

    result = coinbase_request("POST", "/api/v3/brokerage/orders", body)
    print(f"[ORDER] {side} | Result: {result}")
    return result


def close_position(strategy_id):
    """Close an open position by selling/buying back the held size."""
    pos = open_positions.get(strategy_id)
    if not pos:
        print(f"[CLOSE] No open position for {strategy_id}")
        return {"success": False, "error": "No open position"}

    close_side = "SELL" if pos["side"] == "BUY" else "BUY"
    result = place_market_order(close_side, pos["size"])

    if result.get("success"):
        print(f"[CLOSE] Closed {strategy_id} position")
        del open_positions[strategy_id]

    return result


# ── SL/TP MONITOR ────────────────────────────────────────────────────────────
def check_sl_tp():
    """
    Called on every incoming webhook tick.
    Checks all open positions against current price and closes if SL or TP hit.
    """
    if not open_positions:
        return

    price = get_xrp_price()
    if not price:
        return

    for strat_id, pos in list(open_positions.items()):
        side = pos["side"]
        hit_sl = (side == "BUY" and price <= pos["sl"]) or (side == "SELL" and price >= pos["sl"])
        hit_tp = (side == "BUY" and price >= pos["tp"]) or (side == "SELL" and price <= pos["tp"])

        if hit_sl:
            print(f"[SL HIT] {strat_id} | Price: {price} | SL: {pos['sl']}")
            close_position(strat_id)
        elif hit_tp:
            print(f"[TP HIT] {strat_id} | Price: {price} | TP: {pos['tp']}")
            close_position(strat_id)


# ── WEBHOOK AUTH ─────────────────────────────────────────────────────────────
def verify_webhook(req):
    """Verify the webhook secret passed as a query param."""
    secret = req.args.get("secret", "").strip()
    return hmac.compare_digest(secret, WEBHOOK_SECRET)


# ── MAIN WEBHOOK ROUTE ───────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    # 1. Authenticate
    if not verify_webhook(request):
        print("[AUTH] Unauthorized webhook attempt")
        return jsonify({"error": "Unauthorized"}), 403

    # 2. Parse alert payload
    try:
        data = request.get_json(force=True) or {}
    except Exception as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400

    print(f"[WEBHOOK] Received: {data}")

    action = str(data.get("action", "")).upper()
    strategy_id = str(data.get("strategy", "strategy_1"))

    try:
        atr = float(data.get("atr", 0) or 0)
    except Exception:
        atr = 0

    if action not in ("BUY", "SELL", "CLOSE"):
        return jsonify({"error": "Invalid action"}), 400

    # 3. Check SL/TP on every tick
    check_sl_tp()

    # 4. Handle CLOSE signal
    if action == "CLOSE":
        result = close_position(strategy_id)
        return jsonify({
            "status": "closed",
            "strategy": strategy_id,
            "result": result
        })

    # 5. Skip if already in a position for this strategy
    if strategy_id in open_positions:
        print(f"[SKIP] Already in position for {strategy_id}")
        return jsonify({"status": "skipped", "reason": "position already open"})

    # 6. Calculate position size
    balance = get_usd_balance()
    price = get_xrp_price()

    if not price:
        return jsonify({"error": "Could not fetch price"}), 500

    if balance <= 0:
        return jsonify({"error": "USD balance is zero or unavailable"}), 500

    risk_usd = balance * RISK_PERCENT

    # Use ATR for SL distance if provided, else default to 1%
    if atr > 0:
        sl_distance = atr * 1.5
    else:
        sl_distance = price * 0.01

    tp_distance = sl_distance * REWARD_RATIO

    if action == "BUY":
        sl_price = round(price - sl_distance, 5)
        tp_price = round(price + tp_distance, 5)
    else:
        sl_price = round(price + sl_distance, 5)
        tp_price = round(price - tp_distance, 5)

    # USD amount to spend
    if sl_distance <= 0:
        return jsonify({"error": "Invalid stop-loss distance"}), 500

    usd_size = round(risk_usd / (sl_distance / price), 2)
    usd_size = min(usd_size, round(balance * 0.95, 2))  # never use more than 95% of balance

    print(
        f"[TRADE] {action} | Strategy: {strategy_id} | "
        f"Price: {price} | SL: {sl_price} | TP: {tp_price} | Size: ${usd_size}"
    )

    # 7. Place order
    result = place_market_order(action, usd_size)

    if result.get("success"):
        open_positions[strategy_id] = {
            "side": action,
            "entry": price,
            "sl": sl_price,
            "tp": tp_price,
            "size": usd_size,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        return jsonify({
            "status": "order placed",
            "strategy": strategy_id,
            "action": action,
            "entry": price,
            "sl": sl_price,
            "tp": tp_price,
            "size_usd": usd_size,
        })
    else:
        return jsonify({"error": "Order failed", "details": result}), 500


# ── HEALTH CHECK ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "product": PRODUCT_ID,
        "open_positions": open_positions,
        "time": datetime.now(timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
