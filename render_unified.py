# render_unified.py
"""
Unified Flask + Telegram runner for StakeAware (Main, Access, Results bots)
- Uses python-telegram-bot v20+ (async)
- Runs all bots in one asyncio loop (no thread event-loop problems)
- Provides polling mode (default) or webhook mode (if WEBHOOK_BASE_URL + RUN_MODE=webhook)
- Includes Flask backend for Paystack webhooks, paystack_redirect, link_telegram, admin/users
"""

import os
import hmac
import hashlib
import json
import time
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify, redirect, Response

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters
)

# -------------------------
# Load environment & config
# -------------------------
load_dotenv()

# Flask / backend config
APP_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5000)))
PAYSTACK_WEBHOOK_SECRET = os.getenv("PAYSTACK_WEBHOOK_SECRET", "")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
ACCESS_BOT_USERNAME = os.getenv("ACCESS_BOT_USERNAME", "StakeAwareAccessBot")
ACCESS_BOT_TOKEN = os.getenv("ACCESS_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or 0)
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", f"http://127.0.0.1:{APP_PORT}")

# Bot tokens
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN", "")
RESULTS_BOT_TOKEN = os.getenv("RESULTS_BOT_TOKEN", "")
# ACCESS_BOT_TOKEN already loaded above

# Payment links, group links & ids
PAYSTACK_DAILY_LINK = os.getenv("PAYSTACK_DAILY_LINK", "")
PAYSTACK_WEEKEND_LINK = os.getenv("PAYSTACK_WEEKEND_LINK", "")
DAILY_GROUP_ID = int(os.getenv("DAILY_GROUP_ID", "0") or 0)
WEEKEND_GROUP_ID = int(os.getenv("WEEKEND_GROUP_ID", "0") or 0)
DAILY_GROUP_LINK = os.getenv("DAILY_GROUP_LINK", "")
WEEKEND_GROUP_LINK = os.getenv("WEEKEND_GROUP_LINK", "")

# Plans / alerts
DAILY_PLAN_AMOUNT = int(os.getenv("DAILY_PLAN_AMOUNT", "50000"))
WEEKEND_PLAN_AMOUNT = int(os.getenv("WEEKEND_PLAN_AMOUNT", "20000"))
DAILY_PLAN_DURATION = int(os.getenv("DAILY_PLAN_DURATION", "30"))
WEEKEND_PLAN_DURATION = int(os.getenv("WEEKEND_PLAN_DURATION", "30"))
EXPIRY_ALERT_DAYS = int(os.getenv("EXPIRY_ALERT_DAYS", "3"))

# Admin list for results bot (comma separated)
ADMIN_TELEGRAM_IDS = [int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", str(ADMIN_TELEGRAM_ID)).split(",") if x.strip()]

# Mode: polling (default) or webhook
RUN_MODE = os.getenv("RUN_MODE", "polling").lower()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "")  # e.g. https://stakeawarebot.onrender.com
WEBHOOK_PATH_PREFIX = os.getenv("WEBHOOK_PATH_PREFIX", "/bot")  # optional prefix for endpoints

# Files
DATA_DIR = Path("data")
USERS_FILE = DATA_DIR / "users.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("render_unified")

# Flask app
flask_app = Flask("stakeaware_backend")

# Helper: send message via access bot token (raw Telegram API) for backend -> DM
BOT_API_SEND = f"https://api.telegram.org/bot{ACCESS_BOT_TOKEN}/sendMessage" if ACCESS_BOT_TOKEN else None

# -------------------------
# Persistence helpers
# -------------------------
def load_users() -> Dict[str, Any]:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception as e:
        log.error("Failed loading users.json: %s", e)
        return {}

def save_users(users: Dict[str, Any]) -> None:
    USERS_FILE.write_text(json.dumps(users, indent=2))

# -------------------------
# Paystack helpers & routes
# -------------------------
def verify_signature(req) -> bool:
    if not PAYSTACK_WEBHOOK_SECRET:
        return True
    sig = req.headers.get("x-paystack-signature", "")
    body = req.get_data()
    computed = hmac.new(PAYSTACK_WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(sig, computed)

def send_admin_message(text: str) -> None:
    if not BOT_API_SEND or not ADMIN_TELEGRAM_ID:
        log.info("[admin msg] %s", text)
        return
    try:
        requests.post(BOT_API_SEND, json={"chat_id": ADMIN_TELEGRAM_ID, "text": text}, timeout=8)
    except Exception as e:
        log.warning("Failed to send admin message: %s", e)

def grant_or_renew(email: str, plan: str, reference: str) -> Dict[str, Any]:
    users = load_users()
    now = int(time.time())
    duration_days = DAILY_PLAN_DURATION if plan == "daily" else WEEKEND_PLAN_DURATION
    expires_at = now + duration_days * 24 * 3600

    prev = users.get(email)
    if prev and prev.get("expires_at", 0) > now:
        new_expiry = max(prev["expires_at"], expires_at)
        prev.update({"plan": plan, "paystack_reference": reference, "expires_at": new_expiry, "active": True})
        users[email] = prev
        action = "renewed"
    else:
        users[email] = {"email": email, "plan": plan, "paystack_reference": reference, "expires_at": expires_at, "active": True, "chat_id": None}
        action = "activated"

    save_users(users)
    deep_link = f"https://t.me/{ACCESS_BOT_USERNAME}?start={reference}"
    send_admin_message(f"{email} {action} ({plan}). Paystack ref: {reference}\nDeep-link: {deep_link}")
    return users[email]

def handle_paystack_event(event_json: Dict[str, Any]) -> (Dict[str, Any], int):
    if not event_json:
        return {"error": "empty payload"}, 400
    etype = event_json.get("event")
    if etype != "charge.success":
        return {"status": "ignored"}, 200

    data = event_json.get("data", {}) or {}
    reference = data.get("reference")
    email = (data.get("customer") or {}).get("email") or data.get("customer_email")
    amount = int(data.get("amount", 0)) // 100

    if not reference or not email:
        return {"error": "missing reference or email"}, 400

    if PAYSTACK_SECRET_KEY:
        try:
            verify_url = f"https://api.paystack.co/transaction/verify/{reference}"
            headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
            r = requests.get(verify_url, headers=headers, timeout=10)
            jr = r.json()
            if not jr.get("status") or jr.get("data", {}).get("status") != "success":
                return {"error": "verification failed"}, 400
            verified_data = jr.get("data", {})
            email = (verified_data.get("customer") or {}).get("email") or verified_data.get("customer_email") or email
            amount = int(verified_data.get("amount", amount * 100)) // 100
        except Exception as e:
            log.error("Error verifying with Paystack: %s", e)
            return {"error": "verification error"}, 400

    md = data.get("metadata") or {}
    plan = md.get("plan_type") if isinstance(md, dict) else None
    if not plan:
        plan = "daily" if amount >= DAILY_PLAN_AMOUNT else "weekend"

    grant_or_renew(email, plan, reference)
    return {"status": "ok", "email": email}, 200

@flask_app.route("/webhook/paystack", methods=["POST"])
def webhook_paystack():
    if not verify_signature(request):
        return "Invalid signature", 400
    try:
        event_json = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400
    result, code = handle_paystack_event(event_json)
    return jsonify(result), code

@flask_app.route("/stakeaware_secure_test_2025", methods=["POST"])
def legacy_webhook():
    try:
        event_json = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400
    if PAYSTACK_WEBHOOK_SECRET and not verify_signature(request):
        return "Invalid signature", 400
    result, code = handle_paystack_event(event_json)
    return jsonify(result), code

@flask_app.route("/paystack_redirect", methods=["GET"])
def paystack_redirect():
    ref = request.args.get("reference")
    if not ref:
        return "Missing reference", 400

    if not PAYSTACK_SECRET_KEY:
        tg_url = f"https://t.me/{ACCESS_BOT_USERNAME}?start={ref}"
        return f"<html><body>Payment processed. <a href='{tg_url}'>Open Access Bot</a></body></html>", 200

    try:
        verify_url = f"https://api.paystack.co/transaction/verify/{ref}"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        r = requests.get(verify_url, headers=headers, timeout=10)
        jr = r.json()
    except Exception as e:
        return f"Error contacting Paystack: {e}", 500

    if not jr.get("status") or jr.get("data", {}).get("status") != "success":
        return "Payment verification failed", 400

    verified = jr.get("data", {})
    email = (verified.get("customer") or {}).get("email") or verified.get("customer_email")
    amount = int(verified.get("amount", 0)) // 100

    if not email:
        return "Could not determine customer email from payment", 400

    plan = "daily" if amount >= DAILY_PLAN_AMOUNT else "weekend"
    grant_or_renew(email, plan, ref)

    # optional hit admin users internally (local)
    try:
        admin_headers = {"x-admin-key": JWT_SECRET} if JWT_SECRET else {}
        admin_url = f"http://127.0.0.1:{APP_PORT}/admin/users"
        admin_res = requests.get(admin_url, headers=admin_headers, timeout=6)
        if admin_res.status_code == 200:
            log.info("✅ Admin backend /users hit successfully.")
        else:
            log.warning("⚠️ Admin backend returned %s: %s", admin_res.status_code, admin_res.text)
    except Exception as e:
        log.debug("Internal admin/users hit error: %s", e)

    tg_url = f"https://t.me/{ACCESS_BOT_USERNAME}?start={ref}"
    html = f"""<html><head><meta http-equiv="refresh" content="0; url={tg_url}" /></head>
    <body><p>Payment successful. Redirecting to Telegram... <a href="{tg_url}">Click here if not redirected</a></p></body></html>"""
    return html, 200

@flask_app.route("/link_telegram", methods=["POST"])
def link_telegram():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    chat_id = data.get("chat_id") or data.get("telegram_id")
    reference = data.get("reference") or data.get("paystack_reference")
    if not chat_id or not reference:
        return jsonify({"error": "chat_id and reference are required"}), 400

    users = load_users()
    found = None
    for email, u in users.items():
        if u.get("paystack_reference") == reference:
            found = (email, u)
            break

    if not found:
        return jsonify({"error": "user not found"}), 404

    email, u = found
    u["chat_id"] = int(chat_id)
    u["active"] = True
    users[email] = u
    save_users(users)

    group_link = DAILY_GROUP_LINK if u.get("plan") == "daily" else WEEKEND_GROUP_LINK

    # DM user with inline join button (no raw URL displayed)
    if BOT_API_SEND and group_link:
        try:
            requests.post(BOT_API_SEND, json={
                "chat_id": chat_id,
                "text": f"Payment verified! You now have {u.get('plan')} access.",
                "reply_markup": {"inline_keyboard": [[{"text": "Join Group", "url": group_link}]]}
            }, timeout=8)
        except Exception as e:
            log.warning("Failed to DM user: %s", e)

    return jsonify({"status": "linked", "email": email}), 200

@flask_app.route("/admin/users", methods=["GET"])
def admin_users():
    key = request.headers.get("x-admin-key", "")
    if JWT_SECRET and key != JWT_SECRET:
        return "unauthorized", 401
    return jsonify(load_users()), 200

# Expiry checker (background)
def _expiry_checker_loop():
    while True:
        users = load_users()
        now = int(time.time())
        changed = False
        for email, u in list(users.items()):
            exp = u.get("expires_at", 0)
            if u.get("active") and exp:
                if 0 < exp - now <= EXPIRY_ALERT_DAYS * 24 * 3600:
                    if u.get("chat_id"):
                        try:
                            requests.post(BOT_API_SEND, json={"chat_id": u["chat_id"],
                                                             "text": f"Reminder: your {u.get('plan')} subscription expires on {datetime.utcfromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S UTC')}"}, timeout=8)
                        except Exception:
                            pass
                    else:
                        send_admin_message(f"User {email} ({u.get('plan')}) expires soon but has no chat_id. Deep-link: https://t.me/{ACCESS_BOT_USERNAME}?start={u.get('paystack_reference')}")
                if exp <= now:
                    u["active"] = False
                    users[email] = u
                    changed = True
                    send_admin_message(f"{email} subscription expired.")
        if changed:
            save_users(users)
        time.sleep(3600)

# -------------------------
# Telegram bot handlers
# -------------------------
# Shared small utilities
def build_keyboard(buttons):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text=b[0], url=b[1]) if len(b) > 1 else InlineKeyboardButton(text=b[0], callback_data=b[1]) for b in row] for row in buttons])

# -------------------------
# MAIN BOT (public / marketing)
# -------------------------
MAIN_WELCOME_TEXT = (
    "Stake Aware provides daily 3-odds tickets based on deep analysis of sports trends and statistics.\n\n"
    "Subscribe for ₦50,000/month to receive daily predictions or ₦20,000/month for Weekend games only directly here in Telegram.\n\n"
    "Prepare to level up your betting game and start winning like a pro! 🏆\n"
    "Choose your subscription plan below. After payment, click the verification link to automatically verify your Telegram account."
)

async def main_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [("💎 Daily 3-Odds — ₦50,000", PAYSTACK_DAILY_LINK)],
        [("🎯 Weekend 3-Odds — ₦20,000", PAYSTACK_WEEKEND_LINK)],
        [("✅ Verify Access", f"https://t.me/{ACCESS_BOT_USERNAME}")]
    ]
    await update.effective_message.reply_text(MAIN_WELCOME_TEXT, reply_markup=build_keyboard(keyboard))

# -------------------------
# ACCESS BOT (linking deep-link to backend)
# -------------------------
async def access_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = ctx.args or []
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Check Status", callback_data="status")]])
    if args:
        ref = args[0]
        try:
            resp = requests.post(f"{BACKEND_BASE_URL}/link_telegram", json={"reference": ref, "chat_id": chat_id}, timeout=8)
            if resp.status_code == 200:
                await update.message.reply_text("✅ Payment reference linked. You now have access if the payment is valid.", reply_markup=kb)
                return
            else:
                await update.message.reply_text(f"❌ Could not link reference: {resp.text}", reply_markup=kb)
                return
        except Exception as e:
            await update.message.reply_text(f"❌ Error connecting to backend: {e}", reply_markup=kb)
            return

    await update.message.reply_text("Welcome to StakeAware Access Bot.\nIf you completed payment, open the verification link from the payment page (it should open this bot). Use the button to check status.", reply_markup=kb)

async def access_status_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _send_status_for_chat(q.message.chat_id, q.message)

async def access_status_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_status_for_chat(update.effective_chat.id, update.message)

async def _send_status_for_chat(chat_id: int, reply_target):
    try:
        headers = {}
        if JWT_SECRET:
            headers["x-admin-key"] = JWT_SECRET
        resp = requests.get(f"{BACKEND_BASE_URL}/admin/users", headers=headers, timeout=8)
        if resp.status_code != 200:
            await reply_target.reply_text("Could not fetch status from backend.")
            return
        users = resp.json()
        for email, u in users.items():
            if int(u.get("chat_id", 0)) == chat_id:
                exp = u.get("expires_at")
                exp_str = datetime.utcfromtimestamp(int(exp)).strftime("%Y-%m-%d %H:%M:%S UTC") if exp else "unknown"
                await reply_target.reply_text(f"✅ Active plan: {u.get('plan')} | Expires at (UTC): {exp_str}")
                return
        await reply_target.reply_text("❌ No active subscription found for this account.")
    except Exception as e:
        await reply_target.reply_text(f"Error fetching status: {e}")

# -------------------------
# RESULTS BOT (admin only)
# -------------------------
# In-memory games store and admin state
RESULTS_GAMES: list[str] = []
ADMIN_AWAITING_ADD: Dict[int, bool] = {}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_TELEGRAM_IDS

def make_results_menu():
    buttons = [
        [InlineKeyboardButton("➕ Add Game", callback_data="add_game")],
        [InlineKeyboardButton("📋 List Games", callback_data="list_games"),
         InlineKeyboardButton("🗑️ Clear Games", callback_data="clear_games")],
        [InlineKeyboardButton("📤 Post Games", callback_data="post_games")]
    ]
    return InlineKeyboardMarkup(buttons)

def format_results_text() -> str:
    if not RESULTS_GAMES:
        return "📭 No games added yet."
    lines = ["🎯 *STAKEAWARE OFFICIAL PREDICTION FOR THE DAY*\n"]
    total = 1.0
    parsed_any = False
    for idx, g in enumerate(RESULTS_GAMES, 1):
        # pretty line: bold teams, keep odds at end if present
        toks = g.strip().split()
        odds = None
        # attempt to parse last token or last numeric token as odds
        for t in reversed(toks):
            try:
                odds = float(t.replace(",", "."))
                parsed_any = True
                break
            except Exception:
                continue
        lines.append(f"{idx}. *{g}*")
        if odds:
            total *= odds
    total_text = f"{total:.2f}" if parsed_any and total != 1.0 else "—"
    lines.append(f"\n💰 *Total Odds:* {total_text}")
    lines.append("\n🔥 Play Responsibly 🔥")
    return "\n".join(lines)

async def results_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Welcome! You will receive results in your groups.")
        return
    await update.message.reply_text("StakeAware Results Bot — Admin Menu", reply_markup=make_results_menu())

# Callback query handler for results
async def results_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("❌ Not authorized", show_alert=True)
        return

    if q.data == "add_game":
        ADMIN_AWAITING_ADD[uid] = True
        # Send prompt as reply (so next message from this admin is treated as game)
        await q.message.reply_text("Send the game text (example):\nReal Madrid vs Arsenal GG - 1.55\n\nReply here with the full game line.")
        return

    if q.data == "list_games":
        await q.message.reply_text(format_results_text(), parse_mode="Markdown")
        return

    if q.data == "clear_games":
        RESULTS_GAMES.clear()
        await q.message.reply_text("🗑️ All added games cleared.")
        return

    if q.data == "post_games":
        if not RESULTS_GAMES:
            await q.message.reply_text("📭 No games to post.")
            return

        text = format_results_text()
        weekday = datetime.utcnow().weekday()  # Monday=0 ... Sunday=6
        targets = [DAILY_GROUP_ID]
        if weekday in (4, 5, 6):  # Fri-Sun -> include weekend
            if WEEKEND_GROUP_ID:
                targets.append(WEEKEND_GROUP_ID)

        sent = 0
        for gid in targets:
            try:
                await ctx.bot.send_message(chat_id=gid, text=text, parse_mode="Markdown")
                sent += 1
            except Exception as e:
                log.warning("Failed to post to %s: %s", gid, e)

        RESULTS_GAMES.clear()
        await q.message.reply_text(f"✅ Results posted to {sent} group(s).")
        return

# Message handler to capture game lines when admin is awaiting add
async def results_message_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    if ADMIN_AWAITING_ADD.pop(uid, False):
        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("❌ Invalid game text — try again.")
            return
        RESULTS_GAMES.append(text)
        await update.message.reply_text(f"✅ Game added:\n*{text}*", parse_mode="Markdown")
        # keep admin in menu
        await update.message.reply_text("Back to menu:", reply_markup=make_results_menu())

# -------------------------
# Start/stop utilities
# -------------------------
async def register_and_start_bots():
    # Build application instances
    apps = {}

    if MAIN_BOT_TOKEN:
        main_app = ApplicationBuilder().token(MAIN_BOT_TOKEN).build()
        main_app.add_handler(CommandHandler("start", main_start))
        apps["main"] = {"app": main_app, "poll": True, "path": "/main_bot"}
    else:
        log.warning("MAIN_BOT_TOKEN missing — main bot disabled")

    if ACCESS_BOT_TOKEN:
        access_app = ApplicationBuilder().token(ACCESS_BOT_TOKEN).build()
        access_app.add_handler(CommandHandler("start", access_start))
        access_app.add_handler(CallbackQueryHandler(access_status_callback, pattern="status"))
        access_app.add_handler(CommandHandler("status", access_status_command))
        apps["access"] = {"app": access_app, "poll": True, "path": "/access_bot"}
    else:
        log.warning("ACCESS_BOT_TOKEN missing — access bot disabled")

    if RESULTS_BOT_TOKEN:
        results_app = ApplicationBuilder().token(RESULTS_BOT_TOKEN).build()
        results_app.add_handler(CommandHandler("start", results_start))
        results_app.add_handler(CallbackQueryHandler(results_callback))
        results_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, results_message_listener))
        apps["results"] = {"app": results_app, "poll": True, "path": "/results_bot"}
    else:
        log.warning("RESULTS_BOT_TOKEN missing — results bot disabled")

    # If webhook mode requested and base URL provided, register webhooks on Telegram AND also expose Flask endpoints
    use_webhook_mode = RUN_MODE == "webhook" and WEBHOOK_BASE_URL
    if use_webhook_mode:
        log.info("Starting in WEBHOOK mode; registering webhook URLs & wiring Flask endpoints.")
        for name, info in apps.items():
            app = info["app"]
            path = info["path"]
            webhook_url = WEBHOOK_BASE_URL.rstrip("/") + path
            try:
                # set webhook on Telegram side
                await app.bot.set_webhook(url=webhook_url)
                log.info("Registered webhook for %s -> %s", name, webhook_url)
                # expose flask route that forwards updates into app.update_queue
                def make_route(app_ref):
                    async def _forward():
                        try:
                            js = request.get_json(force=True)
                        except Exception:
                            return "invalid json", 400
                        try:
                            update = Update.de_json(js, app_ref.bot)
                            # queue the update to the application
                            app_ref.update_queue.put_nowait(update)
                            return "ok", 200
                        except Exception as e:
                            log.exception("Failed to forward update to app: %s", e)
                            return "error", 500
                    return _forward
                route_path = path
                flask_app.add_url_rule(route_path, endpoint=f"hook_{name}", view_func=make_route(app), methods=["POST"])
                info["poll"] = False
            except Exception as e:
                log.exception("Failed to register webhook for %s: %s", name, e)
                # fallback to polling
                info["poll"] = True

    # Start the apps as tasks (polling or initialize)
    tasks = []
    for name, info in apps.items():
        app_obj = info["app"]
        if info.get("poll", True):
            log.info("Starting polling for bot: %s", name)
            tasks.append(app_obj.run_polling())
        else:
            # initialize the application so it can process queued updates
            log.info("Initializing application (webhook receive-only): %s", name)
            await app_obj.initialize()
            await app_obj.start()
            # do not add run_polling task
    # Await all polling bots (if any)
    if tasks:
        await asyncio.gather(*tasks)
    else:
        # If no polling tasks, keep the main loop alive (webhook-only mode)
        while True:
            await asyncio.sleep(3600)

# -------------------------
# Entrypoint
# -------------------------
def start_backend_thread():
    # expiry checker runs in background thread (blocking)
    import threading
    t = threading.Thread(target=_expiry_checker_loop, daemon=True)
    t.start()
    log.info("Started expiry checker thread.")

def run_flask():
    log.info("Starting Flask backend on port %s", APP_PORT)
    # Use host 0.0.0.0 so Render can expose it
    flask_app.run(host="0.0.0.0", port=APP_PORT)

if __name__ == "__main__":
    # Basic sanity checks
    if not (MAIN_BOT_TOKEN or ACCESS_BOT_TOKEN or RESULTS_BOT_TOKEN):
        log.warning("No bot tokens provided. At least one bot token required to run bots.")

    # Start expiry checker thread
    start_backend_thread()

    # Run Flask in a thread so asyncio loop can run in main thread (PTB requires main async loop)
    import threading
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Run all bots in main asyncio loop
    try:
        asyncio.run(register_and_start_bots())
    except KeyboardInterrupt:
        log.info("Shutting down (KeyboardInterrupt)")
    except Exception as e:
        log.exception("Fatal error in bot runner: %s", e)
