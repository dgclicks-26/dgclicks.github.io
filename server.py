"""
server.py  —  Digicam Venture Backend
=======================================
This is the brain connecting your website, Telegram bot, and Excel file.

HOW TO RUN:
  1. Fill in your .env file
  2. Open PowerShell in this folder
  3. Run:  python server.py
  4. You should see: "Digicam backend running on port 5000"

ENDPOINTS:
  GET  /api/inventory       Website loads this to show live stock
  POST /api/sale            Website sends this when a buyer checks out
  POST /api/enquiry         Website sends this when a buyer submits a question
  POST /api/chat            Website sends this for the AI chat widget
  POST /api/stripe-webhook  Stripe sends this when a card payment succeeds
  GET  /api/health          Quick check that the server is running
"""

import os
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
import telegram
import stripe

import excel_manager as xl
import camera_ai as ai
from pb_instant_booking import pb_instant_bp

# Load credentials from .env file
load_dotenv()

BOT_TOKEN      = os.getenv("BOT_TOKEN")
_owner         = os.getenv("OWNER_TELEGRAM_ID")
OWNER_ID       = int(_owner) if _owner else None
if OWNER_ID is None:
    print("WARNING: OWNER_TELEGRAM_ID not set")
STRIPE_SECRET  = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PORT           = int(os.getenv("PORT", 5000))

stripe.api_key = STRIPE_SECRET

app = Flask(__name__)
CORS(app)  # Allows your website to talk to this server
app.register_blueprint(pb_instant_bp)

# ============================================================
#  ADDRESS AUTOCOMPLETE — OneMap (Singapore Land Authority)
#  Paste this block into server.py (anywhere after `app = Flask(...)`).
#
#  Requires:  requests   (add "requests" to requirements.txt if not there)
#  Uses:      app, request, jsonify  (already imported in your Flask app)
#             a logger called `log` — if you don't have one, replace
#             log.error(...) with print(...)
#
#  Set these as Environment Variables on Render (Settings -> Environment):
#     ONEMAP_EMAIL     = the email you registered at onemap.gov.sg/apidocs/register
#     ONEMAP_PASSWORD  = your OneMap account password
# ============================================================
import os, time, requests

_onemap = {"token": None, "expiry": 0}

def _onemap_token():
    """Return a valid OneMap access token, refreshing it when expired."""
    now = time.time()
    if _onemap["token"] and now < _onemap["expiry"] - 120:
        return _onemap["token"]
    email    = os.getenv("ONEMAP_EMAIL")
    password = os.getenv("ONEMAP_PASSWORD")
    if not email or not password:
        return None  # not configured — search will still try (some queries work unauthenticated)
    try:
        r = requests.post(
            "https://www.onemap.gov.sg/api/auth/post/getToken",
            json={"email": email, "password": password}, timeout=10)
        r.raise_for_status()
        d = r.json()
        _onemap["token"] = d.get("access_token")
        try:
            _onemap["expiry"] = int(d.get("expiry_timestamp", now + 3 * 24 * 3600))
        except (TypeError, ValueError):
            _onemap["expiry"] = now + 3 * 24 * 3600
        return _onemap["token"]
    except Exception as e:
        try: log.error(f"OneMap token error: {e}")
        except Exception: print(f"OneMap token error: {e}")
        return None


@app.route("/api/address-search")
def address_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"results": []})
    token   = _onemap_token()
    headers = {"Authorization": token} if token else {}
    try:
        r = requests.get(
            "https://www.onemap.gov.sg/api/common/elastic/search",
            params={"searchVal": q, "returnGeom": "N",
                    "getAddrDetails": "Y", "pageNum": 1},
            headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        try: log.error(f"OneMap search error: {e}")
        except Exception: print(f"OneMap search error: {e}")
        return jsonify({"results": []})

    out = []
    for it in (data.get("results") or [])[:8]:
        out.append({
            "address":  it.get("ADDRESS", ""),
            "building": it.get("BUILDING", ""),
            "road":     it.get("ROAD_NAME", ""),
            "postal":   it.get("POSTAL", ""),
        })
    return jsonify({"results": out})


# Set up logging so you can see what's happening
logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("server.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ==============================================================
# TELEGRAM HELPERS
# ==============================================================

def telegram_notify(message: str):
    """Send a private Telegram message to you (the owner)."""
    async def _send():
        bot = telegram.Bot(token=BOT_TOKEN)
        await bot.send_message(
            chat_id=OWNER_ID,
            text=message,
            parse_mode="Markdown",
        )
    try:
        asyncio.run(_send())
    except Exception as e:
        log.error(f"Telegram notify failed: {e}")


def telegram_channel_post(message: str):
    """Post a message to your public Telegram channel."""
    channel = os.getenv("TELEGRAM_CHANNEL", "")
    if not channel:
        return

    async def _post():
        bot = telegram.Bot(token=BOT_TOKEN)
        await bot.send_message(
            chat_id=channel,
            text=message,
            parse_mode="Markdown",
        )
    try:
        asyncio.run(_post())
    except Exception as e:
        log.error(f"Channel post failed: {e}")


# ==============================================================
# ENDPOINTS
# ==============================================================

@app.route("/api/health", methods=["GET"])
def health():
    """Quick check that the server is alive."""
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    """
    Returns all cameras from your Excel file.
    The website calls this every time it loads to show live stock.
    """
    try:
        cameras = xl.get_all_cameras()
        result  = []
        for c in cameras:
            if not c["camera_id"]:
                continue
            result.append({
                "id":        c["camera_id"],
                "brand":     c["brand"],
                "model":     c["model"],
                "year":      c["year"],
                "condition": c["condition"],
                "price":     c["asking_price"],
                "stock":     c["units_stock"] or 0,
                "status":    c["status"],
                "notes":     c["notes"] or "",
            })
        return jsonify({"cameras": result})
    except Exception as e:
        log.error(f"GET /api/inventory error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sale", methods=["POST"])
def record_sale():
    """
    Called by the website when a buyer completes checkout.
    Records the sale in Excel, alerts you on Telegram,
    posts a stock update to your channel, and syncs the website.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    # Check all required fields are present
    required = ["camera_id", "sale_price", "buyer_address", "buyer_phone"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing: {field}"}), 400

    camera_id     = data["camera_id"].upper()
    sale_price    = float(data["sale_price"])
    channel       = data.get("channel", "Website")
    buyer_contact = data.get("buyer_contact", "website")
    buyer_address = data["buyer_address"]
    buyer_phone   = data["buyer_phone"]

    # Check camera exists and is in stock
    camera = xl.get_camera_by_id(camera_id)
    if not camera:
        return jsonify({"error": f"Camera {camera_id} not found"}), 404
    if (camera["units_stock"] or 0) < 1:
        return jsonify({"error": "Camera is out of stock"}), 409

    # Record the sale in Excel
    try:
        sale_id, low_stock_alert = xl.record_sale(
            camera_id     = camera_id,
            sale_price    = sale_price,
            channel       = channel,
            buyer_contact = buyer_contact,
            buyer_address = buyer_address,
            buyer_phone   = buyer_phone,
        )
    except Exception as e:
        log.error(f"record_sale error: {e}")
        return jsonify({"error": str(e)}), 500

    model_name = f"{camera['brand']} {camera['model']}"
    new_stock  = max((camera["units_stock"] or 1) - 1, 0)

    # Notify you on Telegram
    telegram_notify(
        f"🛒 *New Website Sale!*\n\n"
        f"Order ID: `{sale_id}`\n"
        f"Camera: {model_name} (`{camera_id}`)\n"
        f"Amount: S${sale_price:,.0f}\n"
        f"Buyer phone: {buyer_phone}\n"
        f"Address: {buyer_address}\n\n"
        f"Type `/shipped {camera_id}` when you've dispatched it."
    )

    # Send low stock alert if needed
    if low_stock_alert:
        telegram_notify(
            f"⚠️ *Low Stock Alert*\n\n"
            f"*{low_stock_alert['model']}* (`{low_stock_alert['camera_id']}`)\n"
            f"Only *{low_stock_alert['new_stock']}* unit(s) left.\n"
            f"Reorder level is set to {low_stock_alert['reorder_level']}. Time to restock! 📦"
        )

    # Post stock update to your Telegram channel
    if new_stock == 0:
        telegram_channel_post(
            f"🔴 *SOLD OUT — {model_name}*\n\n"
            f"This one just went! Follow the channel to be notified when new stock arrives."
        )
    else:
        telegram_channel_post(
            f"🟡 *Stock update — {model_name}*\n"
            f"Only *{new_stock}* unit(s) left at S${sale_price:,.0f}!"
        )

    log.info(f"Sale recorded: {sale_id} — {model_name}")
    return jsonify({"success": True, "sale_id": sale_id})


@app.route("/api/enquiry", methods=["POST"])
def handle_enquiry():
    """
    Called by the website contact form.
    Forwards the buyer's question straight to your Telegram.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    name    = data.get("name", "Unknown")
    email   = data.get("email", "No email")
    camera  = data.get("camera", "General question")
    message = data.get("message", "")

    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400

    telegram_notify(
        f"📩 *New Website Enquiry*\n\n"
        f"*From:* {name}\n"
        f"*Email:* {email}\n"
        f"*Camera:* {camera or 'General question'}\n\n"
        f"*Question:*\n{message}\n\n"
        f"_Reply to them at: {email}_"
    )

    log.info(f"Enquiry from {name} ({email})")
    return jsonify({"success": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Powers the AI chat widget on the website.
    Takes the buyer's question, passes it to Claude AI,
    and returns a helpful answer.
    """
    data = request.get_json()
    if not data or not data.get("message"):
        return jsonify({"error": "No message provided"}), 400

    question  = data["message"]
    inventory = data.get("inventory", [])

    # Build context from current inventory so AI knows what's in stock
    inv_context = ""
    if inventory:
        lines = [
            f"- {c['brand']} {c['model']} ({c['condition']}) "
            f"S${c['price']} — {'In stock' if c['stock'] > 0 else 'Sold out'}"
            for c in inventory
        ]
        inv_context = "Current inventory:\n" + "\n".join(lines)

    try:
        reply = ai.answer_question(question, inv_context)
    except Exception as e:
        log.error(f"Chat AI error: {e}")
        reply = (
            "I'm having a bit of trouble right now. "
            "Please use the contact form below and we'll get back to you shortly!"
        )

    return jsonify({"reply": reply})


@app.route("/api/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """
    Stripe calls this automatically when a credit card payment succeeds.
    This triggers the full sale recording flow.
    NOTE: You'll need to configure this URL in your Stripe dashboard (Phase 5).
    """
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    # Verify the request actually came from Stripe
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK)
    except stripe.error.SignatureVerificationError:
        log.warning("Invalid Stripe webhook signature — request rejected")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        log.error(f"Stripe webhook error: {e}")
        return jsonify({"error": str(e)}), 400

    # Only handle successful payments
    if event["type"] == "payment_intent.succeeded":
        intent    = event["data"]["object"]
        metadata  = intent.get("metadata", {})
        camera_id = metadata.get("camera_id")
        amount    = intent["amount_received"] / 100  # Stripe stores in cents

        if camera_id:
            camera = xl.get_camera_by_id(camera_id)
            if camera and (camera["units_stock"] or 0) > 0:
                try:
                    sale_id, alert = xl.record_sale(
                        camera_id     = camera_id,
                        sale_price    = amount,
                        channel       = "Website (Stripe)",
                        buyer_contact = metadata.get("buyer_email", ""),
                        buyer_address = metadata.get("buyer_address", "Provided at checkout"),
                        buyer_phone   = metadata.get("buyer_phone", "Provided at checkout"),
                    )
                    model_name = f"{camera['brand']} {camera['model']}"
                    telegram_notify(
                        f"💳 *Stripe Payment Confirmed!*\n\n"
                        f"Order: `{sale_id}`\n"
                        f"Camera: {model_name}\n"
                        f"Amount: S${amount:,.2f}\n"
                        f"Buyer: {metadata.get('buyer_email', 'Unknown')}\n\n"
                        f"Type `/shipped {camera_id}` when dispatched."
                    )
                    if alert:
                        telegram_notify(
                            f"⚠️ *Low Stock Alert* — {alert['model']}: "
                            f"{alert['new_stock']} unit(s) left."
                        )
                    log.info(f"Stripe sale recorded: {sale_id}")
                except Exception as e:
                    log.error(f"Stripe sale recording failed: {e}")

    return jsonify({"received": True})


# ==============================================================
# PHOTOBOOTH CALENDAR ENDPOINTS
# ==============================================================

import json as _json

BLOCKED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'blocked_dates.json')
CONFIRMED_BOOKINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'confirmed_bookings.json')
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "dgclicks2025")  # set in Render env vars

def _load_json_file(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return _json.load(f)
    except Exception:
        pass
    return {}

def _save_json_file(path, data):
    try:
        with open(path, 'w') as f:
            _json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save {path}: {e}")


@app.route("/api/pb-bookings", methods=["GET"])
def get_pb_bookings():
    """
    Returns all confirmed bookings and blocked dates for the calendar.
    Format: { "bookings": { "2025-12-25": [{"start":"14:00","end":"17:00"}] } }
    """
    # Load confirmed bookings from pb_instant_booking module
    confirmed = {}
    try:
        from pb_instant_booking import _load_bookings
        raw = _load_bookings()
        for key, b in raw.items():
            date = b.get('date', '')
            if not date:
                continue
            start = b.get('time', '10:00')
            dur   = float(b.get('duration', 2.5))
            # Calculate end time
            h, m = map(int, start.split(':'))
            total_mins = h * 60 + m + int(dur * 60)
            end = f"{total_mins // 60:02d}:{total_mins % 60:02d}"
            if date not in confirmed:
                confirmed[date] = []
            confirmed[date].append({"start": start, "end": end})
    except Exception as e:
        log.error(f"Error loading confirmed bookings: {e}")

    # Merge with manually blocked dates
    blocked = _load_json_file(BLOCKED_FILE)
    for date, slots in blocked.items():
        if date not in confirmed:
            confirmed[date] = []
        confirmed[date].extend(slots)

    return jsonify({"bookings": confirmed})


@app.route("/api/pb-block", methods=["POST"])
def block_date():
    """Block or unblock a date. Requires admin password."""
    data = request.get_json()
    if not data or data.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorised"}), 401

    date   = data.get("date")
    action = data.get("action", "block")  # "block" or "unblock"

    if not date:
        return jsonify({"error": "Date required"}), 400

    blocked = _load_json_file(BLOCKED_FILE)

    if action == "block":
        # Block the full day (8am–11pm)
        blocked[date] = [{"start": "08:00", "end": "23:00"}]
        log.info(f"Date blocked: {date}")
    elif action == "unblock":
        blocked.pop(date, None)
        log.info(f"Date unblocked: {date}")

    _save_json_file(BLOCKED_FILE, blocked)
    return jsonify({"success": True, "action": action, "date": date})


@app.route("/api/pb-blocked", methods=["GET"])
def get_blocked_dates():
    """Returns all manually blocked dates."""
    blocked = _load_json_file(BLOCKED_FILE)
    return jsonify({"blocked": list(blocked.keys())})


# ==============================================================
# START SERVER
# ==============================================================

import json as _json

BLOCKED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'blocked_dates.json')

def _load_json_file(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return _json.load(f)
    except Exception:
        pass
    return {}

def _save_json_file(path, data):
    try:
        with open(path, 'w') as f:
            _json.dump(data, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save {path}: {e}")

if __name__ == "__main__":
    log.info(f"🚀 Digicam backend running on port {PORT}")
    log.info(f"   Excel path:   {os.getenv('EXCEL_PATH')}")
    log.info(f"   Website path: {os.getenv('WEBSITE_PATH')}")
    log.info(f"   Channel:      {os.getenv('TELEGRAM_CHANNEL')}")

    app.run(host="0.0.0.0", port=PORT, debug=False)
