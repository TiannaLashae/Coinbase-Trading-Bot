import os
import uuid
import time
import secrets
import traceback
import requests
import jwt

from flask import Flask, request, jsonify
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization

app = Flask(__name__)

# ── ENV VARS ────────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
COINBASE_API_KEY = os.environ.get("COINBASE_API_KEY", "").strip()

raw_key = os.environ.get("COINBASE_PRIVATE_KEY", "")
COINBASE_PRIVATE_KEY = raw_key.replace("\\n", "\n").strip()

# ── SETTINGS ────────────────────────────────────────────────────────────────
API_HOST = "api.coinbase.com"
PRODUCT_ID = "XRP-USD"
RISK_PERCENT = 0.01
REWARD_RATIO = 2.0
MAX_BALANCE_USE = 0.95

# ── POSITION TRACKER ────────────────────────────────────────────────────────
open_positions = {}

# ── STARTUP LOGS ────────────────────────────────────────────────────────────
def startup_log():
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

startup_log()

# ── HELPERS ─────────────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def verify_webhook(req):
    secret = req.args.get("secret", "").strip()
    return secret == WEBHOOK_SECRET

# ── COINBASE AUTH ───────────────────────────────────────────────────────────
def build_jwt(method, path):
    if not COINBASE_API_KEY or not COINBASE_PRIVATE_KEY:
        raise ValueError("Missing Coinbase API credentials")

    private_key = serialization.load_pem_private_key(
        COINBASE_PRIVATE_KEY.encode("utf-8"),
        password=None
    )

    uri = f"{method.upper()} {API_HOST}{path}"

    payload = {
        "sub": COINBASE_API_KEY,
        "iss": "cdp",
        "nbf": int(time.time()),
        "exp": int(time.time()) + 120,
        "uri": uri,
    }

    token = jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={
            "kid": COINBASE_API_KEY,
            "nonce": secrets.token_hex(),
        },
    )
    return token

def coinbase_request(method, path, body=None):
    try:
        token = build_jwt(method, path)
        url = f"https://{API_HOST}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=body,
            timeout=20
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw_text": response.text}

        print(f"[COINBASE] {method.upper()} {path} | Status: {response.status_code} | Response: {data}")

        return {
            "ok": response.ok,
            "status_code": response.status_code,
            "data": data
        }

    except Exception as e:
        print(f"[COINBASE ERROR] {e}")
        print(traceback.format_exc())
        return {
            "ok": False,
            "status_code": 500,
            "data": {"error": str(e)}
        }

# ── ACCOUNT HELPERS ─────────────────────────────────────────────────────────
def get_usd_balance():
    result = coinbase_request("GET", "/api/v3/brokerage/accounts")
    if not result["ok"]:
        return 0.0

    data = result["data"]
    for acct in data.get("accounts", []):
        if acct.get("currency") == "USD":
            return safe_float(acct.get("available_balance", {}).get("value"), 0.0)

    return 0.0

def get_xrp_balance():
    result = coinbase_request("GET", "/api/v3/brokerage/accounts")
    if not result["ok"]:
        return 0.0

    data = result["data"]
    for acct in data.get("accounts", []):
        if acct.get("currency") == "XRP":
            return safe_float(acct.get("available_balance", {}).get("value"), 0.0)

    return 0.0

def get_xrp_price():
    result = coinbase_request("GET", f"/api/v3/brokerage/best_bid_ask?product_ids={PRODUCT_ID}")
    if not result["ok"]:
        return None

    data = result["data"]
    pricebooks = data.get("pricebooks", [])
    if not pricebooks:
        return None

    bids = pricebooks[0].get("bids", [])
    asks = pricebooks[0].get("asks", [])

    if not bids or not asks:
        return None

    best_bid = safe_float(bids[0].get("price"))
    best_ask = safe_float(asks[0].get("price"))

    if best_bid <= 0 or best_ask <= 0:
        return None

    return (best_bid + best_ask) / 2

# ── ORDER HELPERS ───────────────────────────────────────────────────────────
def extract_order_success(result):
    if not result["ok"]:
        return False

    data = result["data"]

    if data.get("success") is True:
        return True

    if data.get("success_response"):
        return True

    if data.get("order_id"):
        return True

    if isinstance(data.get("success_response"), dict) and data["success_response"].get("order_id"):
        return True

    return False

def extract_order_id(result):
    data = result.get("data", {})

    if data.get("order_id"):
        return data.get("order_id")

    success_response = data.get("success_response", {})
    if isinstance(success_response, dict):
        return success_response.get("order_id")

    return None

def place_market_order(side, size, size_type="quote"):
    side = side.upper()

    if side not in ("BUY", "SELL"):
        return {"ok": False, "status_code": 400, "data": {"error": "Invalid side"}}

    if size <= 0:
        return {"ok": False, "status_code": 400, "data": {"error": "Size must be > 0"}}

    if size_type == "quote":
        order_config = {
            "market_market_ioc": {
                "quote_size": str(round(size, 2))
            }
        }
    elif size_type == "base":
        order_config = {
            "market_market_ioc": {
                "base_size": str(round(size, 6))
            }
        }
    else:
        return {"ok": False, "status_code": 400, "data": {"error": "Invalid size_type"}}

    body = {
        "client_order_id": f"xrp-bot-{uuid.uuid4()}",
        "product_id": PRODUCT_ID,
        "side": side,
        "order_configuration": order_config,
    }

    print(f"[ORDER REQUEST] Side: {side} | Size: {size} | Type: {size_type} | Body: {body}")
    return coinbase_request("POST", "/api/v3/brokerage/orders", body)

def close_position(strategy_id):
    pos = open_positions.get(strategy_id)
    if not pos:
        print(f"[CLOSE] No open position for {strategy_id}")
        return {"ok": False, "status_code": 400, "data": {"error": "No open position"}}

    if pos["side"] != "BUY":
        return {"ok": False, "status_code": 400, "data": {"error": "Only long spot positions supported"}}

    # Use live XRP balance to close, not just stored base size
    xrp_balance = get_xrp_balance()
    if xrp_balance <= 0:
        print(f"[CLOSE ERROR] No XRP balance available to close for {strategy_id}")
        return {"ok": False, "status_code": 400, "data": {"error": "No XRP balance available to close"}}

    result = place_market_order("SELL", xrp_balance, size_type="base")

    if extract_order_success(result):
        print(f"[CLOSE] Closed {strategy_id}")
        del open_positions[strategy_id]
    else:
        print(f"[CLOSE ERROR] Failed to close {strategy_id}: {result}")

    return result

# ── SL / TP MONITOR ─────────────────────────────────────────────────────────
def check_sl_tp():
    if not open_positions:
        return

    price = get_xrp_price()
    if not price:
        print("[SL/TP] Could not fetch current price, skipping check")
        return

    for strategy_id, pos in list(open_positions.items()):
        side = pos["side"]

        if side == "BUY":
            hit_sl = price <= pos["sl"]
            hit_tp = price >= pos["tp"]
        else:
            hit_sl = price >= pos["sl"]
            hit_tp = price <= pos["tp"]

        if hit_sl:
            print(f"[SL HIT] {strategy_id} | Current: {price} | SL: {pos['sl']}")
            close_position(strategy_id)
        elif hit_tp:
            print(f"[TP HIT] {strategy_id} | Current: {price} | TP: {pos['tp']}")
            close_position(strategy_id)

# ── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "product": PRODUCT_ID,
        "open_positions": open_positions,
        "time": now_iso(),
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not verify_webhook(request):
            print("[AUTH] Unauthorized webhook attempt")
            return jsonify({"error": "Unauthorized"}), 403

        try:
            data = request.get_json(force=True) or {}
        except Exception as e:
            print(f"[WEBHOOK ERROR] Invalid JSON: {e}")
            return jsonify({"error": f"Invalid JSON: {e}"}), 400

        print(f"[WEBHOOK] Received: {data}")

        action = str(data.get("action", "")).upper()
        strategy_id = str(data.get("strategy", "strategy_1"))
        atr = safe_float(data.get("atr", 0), 0)

        if action not in ("BUY", "SELL", "CLOSE"):
            return jsonify({"error": "Invalid action"}), 400

        # Check any existing position for SL/TP first
        check_sl_tp()

        # SELL / CLOSE should close an existing long spot position
        if action in ("SELL", "CLOSE"):
            if strategy_id not in open_positions:
                print(f"[SELL/CLOSE] No open position found for {strategy_id}")
                return jsonify({
                    "status": "skipped",
                    "reason": "no open position to close"
                }), 200

            result = close_position(strategy_id)

            if extract_order_success(result):
                return jsonify({
                    "status": "closed",
                    "strategy": strategy_id,
                    "result": result["data"]
                }), 200

            return jsonify({
                "error": "Close failed",
                "details": result["data"]
            }), result.get("status_code", 500)

        # If BUY comes in while already in a trade, skip it
        if strategy_id in open_positions:
            print(f"[SKIP] Already in position for {strategy_id}")
            return jsonify({
                "status": "skipped",
                "reason": "position already open"
            }), 200

        balance = get_usd_balance()

        # ── USE PRICE FROM WEBHOOK PAYLOAD FIRST ──────────────────────────
        price = safe_float(data.get("price", 0), 0)
        if price > 0:
            print(f"[PRICE] Using price from webhook payload: {price}")
        else:
            print(f"[PRICE] No price in payload, fetching from Coinbase...")
            price = get_xrp_price()

        if not price:
            return jsonify({"error": "Could not fetch XRP price"}), 500

        if balance <= 0:
            return jsonify({"error": "USD balance is zero or unavailable"}), 500

        sl_from_payload = safe_float(data.get("sl", 0), 0)
        tp_from_payload = safe_float(data.get("tp", 0), 0)

        if sl_from_payload > 0 and tp_from_payload > 0:
            sl_price = round(sl_from_payload, 6)
            tp_price = round(tp_from_payload, 6)
            sl_distance = abs(price - sl_price)
            print(f"[SL/TP] Using payload values | SL: {sl_price} | TP: {tp_price}")
        else:
            if atr > 0:
                sl_distance = atr * 1.5
            else:
                sl_distance = price * 0.01

            tp_distance = sl_distance * REWARD_RATIO

            sl_price = round(price - sl_distance, 6)
            tp_price = round(price + tp_distance, 6)

            print(f"[SL/TP] Calculated from ATR/fallback | SL: {sl_price} | TP: {tp_price}")

        if sl_distance <= 0:
            return jsonify({"error": "Invalid stop-loss distance"}), 500

        risk_usd = balance * RISK_PERCENT
        usd_size = round(risk_usd / (sl_distance / price), 2)
        usd_cap = round(balance * MAX_BALANCE_USE, 2)
        usd_size = min(usd_size, usd_cap)

        if usd_size <= 0:
            return jsonify({"error": "Calculated usd_size <= 0"}), 500

        base_size = round(usd_size / price, 6)

        print(
            f"[TRADE] Action: {action} | Strategy: {strategy_id} | "
            f"Balance: {balance} | Price: {price} | RiskUSD: {risk_usd} | "
            f"USD Size: {usd_size} | Base Size: {base_size} | "
            f"SL: {sl_price} | TP: {tp_price}"
        )

        result = place_market_order("BUY", usd_size, size_type="quote")

        if extract_order_success(result):
            order_id = extract_order_id(result)

            open_positions[strategy_id] = {
                "side": "BUY",
                "entry": price,
                "sl": sl_price,
                "tp": tp_price,
                "usd_size": usd_size,
                "base_size": base_size,
                "order_id": order_id,
                "time": now_iso(),
            }

            print(f"[ORDER SUCCESS] Strategy: {strategy_id} | Order ID: {order_id}")

            return jsonify({
                "status": "order placed",
                "strategy": strategy_id,
                "order_id": order_id,
                "action": "BUY",
                "entry": price,
                "sl": sl_price,
                "tp": tp_price,
                "usd_size": usd_size,
                "base_size": base_size,
            }), 200

        print(f"[ORDER FAILED] {result}")
        return jsonify({
            "error": "Order failed",
            "details": result["data"]
        }), result.get("status_code", 500)

    except Exception as e:
        print(f"[FATAL WEBHOOK ERROR] {e}")
        print(traceback.format_exc())
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
