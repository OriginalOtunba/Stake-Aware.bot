# render_unified.py
import os
import hmac
import hashlib
import json
import time
import threading
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# === LOAD ENV ===
load_dotenv()
# Telegram / Backend
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ACCESS_BOT_TOKEN = os.getenv("ACCESS_BOT_TOKEN")
RESULTS_BOT_TOKEN = os.getenv("RESULTS_BOT_TOKEN")
ACCESS_BOT_USERNAME = os.getenv("ACCESS_BOT_USERNAME")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", 0))
DAILY_GROUP_ID = os.getenv("DAILY_GROUP_ID")
WEEKEND_GROUP_ID = os.getenv("WEEKEND_GROUP_ID")
DAILY_GROUP_LINK = os.getenv("DAILY_GROUP_LINK")
WEEKEND_GROUP_LINK = os.getenv("WEEKEND_GROUP_LINK")

# Paystack
PAYSTACK_WEBHOOK_SECRET = os.getenv("PAYSTACK_WEBHOOK_SECRET", "")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")

# Plans
DAILY_PLAN_AMOUNT = int(os.getenv("DAILY_PLAN_AMOUNT", 50000))
DAILY_PLAN_DURATION = int(os.getenv("DAILY_PLAN_DURATION", 30))
WEEKEND_PLAN_AMOUNT = int(os.getenv("WEEKEND_PLAN_AMOUNT", 20000))
WEEKEND_PLAN_DURATION = int(os.getenv("WEEKEND_PLAN_DURATION", 30))
EXPIRY_ALERT_DAYS = int(os.getenv("EXPIRY_ALERT_DAYS", 3))

# Backend / Flask
APP_PORT = int(os.getenv("FLASK_PORT", 5000))
USERS_FILE = Path("data/users.json")
USERS_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)

# === USERS STORAGE ===
def load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return {}

def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2))

# === FLASK APP ===
app = Flask(__name__)

def verify_signature(req):
    if not PAYSTACK_WEBHOOK_SECRET:
        return True
    sig = req.headers.get("x-paystack-signature", "")
    body = req.get_data()
    computed = hmac.new(PAYSTACK_WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(sig, computed)

def send_admin_message(text):
    if not MAIN_BOT_TOKEN or not ADMIN_TELEGRAM_ID:
        print("[admin msg]", text)
        return
    url = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": ADMIN_TELEGRAM_ID, "text": text}, timeout=10)
    except Exception as e:
        print("Failed admin message:", e)

def grant_or_renew(email, plan, reference):
    users = load_users()
    now = int(time.time())
    duration = DAILY_PLAN_DURATION if plan == "daily" else WEEKEND_PLAN_DURATION
    expires_at = now + duration * 24*3600

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

# === PAYSTACK WEBHOOK ===
@app.route("/webhook/paystack", methods=["POST"])
def webhook_paystack():
    if not verify_signature(request):
        return "Invalid signature", 400
    try:
        data = request.get_json(force=True)
    except:
        return jsonify({"error":"invalid json"}), 400

    event = data.get("event")
    if event != "charge.success":
        return {"status": "ignored"}, 200

    pdata = data.get("data", {})
    reference = pdata.get("reference")
    email = (pdata.get("customer") or {}).get("email") or pdata.get("customer_email")
    amount = int(pdata.get("amount", 0)) // 100

    if not reference or not email:
        return {"error": "missing reference/email"}, 400

    plan = "daily" if amount >= DAILY_PLAN_AMOUNT else "weekend"
    grant_or_renew(email, plan, reference)
    return {"status":"ok"}, 200

@app.route("/", methods=["GET"])
def index():
    return "StakeAware backend running", 200

# === EXPIRY CHECKER THREAD ===
def expiry_checker():
    while True:
        users = load_users()
        now = int(time.time())
        changed = False
        for email, u in users.items():
            exp = u.get("expires_at",0)
            if u.get("active") and exp:
                if 0 < exp-now <= EXPIRY_ALERT_DAYS*24*3600:
                    chat_id = u.get("chat_id")
                    if chat_id:
                        try:
                            requests.post(f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
                                          json={"chat_id":chat_id, "text": f"Reminder: your {u.get('plan')} subscription expires on {datetime.utcfromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S UTC')}"}, timeout=5)
                        except: pass
                if exp <= now:
                    u["active"] = False
                    users[email] = u
                    changed = True
                    send_admin_message(f"{email} subscription expired.")
        if changed:
            save_users(users)
        time.sleep(3600)

# === RESULTS BOT LOGIC ===
ADDING_GAME = range(1)
games = []

def main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton("➕ Add Game", callback_data="add_game")],
        [InlineKeyboardButton("📋 List Games", callback_data="list_games")],
        [InlineKeyboardButton("📤 Post Games", callback_data="post_games")],
        [InlineKeyboardButton("🗑️ Clear Games", callback_data="clear_games")]
    ]
    return InlineKeyboardMarkup(buttons)

def is_admin(user_id):
    return user_id == ADMIN_TELEGRAM_ID

def format_games_list():
    if not games:
        return "📭 No games added yet."
    msg = "🎯 *STAKEAWARE OFFICIAL PREDICTION FOR THE DAY*\n\n"
    total_odds = 1.0
    for i,g in enumerate(games):
        try: odds=float(g.split()[-2]); total_odds*=odds
        except: odds=None
        msg+=f"{i+1}. *{g}*\n"
    msg+=f"\n💰 *Total Odds:* {total_odds:.2f}" if total_odds!=1.0 else "\n💰 *Total Odds:* —"
    msg+="\n\n🔥 Play Responsibly 🔥"
    return msg

async def results_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Welcome! You will receive results in your groups.")
        return
    await update.message.reply_text("Welcome to StakeAware Results Bot.", reply_markup=main_menu_keyboard())

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
    if query.data=="add_game":
        await query.edit_message_text("Send game in format:\nTeamA vs TeamB GG - 1.55 odds", reply_markup=main_menu_keyboard())
        return ADDING_GAME
    elif query.data=="list_games":
        await query.edit_message_text(format_games_list(), parse_mode="Markdown", reply_markup=main_menu_keyboard())
    elif query.data=="post_games":
        day=datetime.now().weekday()
        targets=[DAILY_GROUP_ID]
        if day in [4,5,6]: targets.append(WEEKEND_GROUP_ID)
        for gid in targets:
            try: await context.bot.send_message(chat_id=gid, text=format_games_list(), parse_mode="Markdown")
            except: pass
        await query.edit_message_text(f"✅ Results posted to {len(targets)} group(s).", reply_markup=main_menu_keyboard())
        games.clear()
    elif query.data=="clear_games":
        games.clear()
        await query.edit_message_text("🗑️ All games cleared.", reply_markup=main_menu_keyboard())

async def add_game_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    game_text = update.message.text.strip()
    if not game_text: return ADDING_GAME
    games.append(game_text)
    await update.message.reply_text(f"✅ Game added:\n*{game_text}*", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ADDING_GAME

def start_results_bot():
    app = ApplicationBuilder().token(RESULTS_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", results_start))
    conv = ConversationHandler(entry_points=[CallbackQueryHandler(handle_menu)],
                               states={ADDING_GAME:[MessageHandler(filters.TEXT&~filters.COMMAND, add_game_message)]},
                               fallbacks=[], allow_reentry=True)
    app.add_handler(conv)
    print("✅ Results Bot running...")
    app.run_polling()

# === UNIFIED BOT STARTUP ===
def run_bots():
    # Results Bot
    threading.Thread(target=start_results_bot, daemon=True).start()
    # Expiry Checker
    threading.Thread(target=expiry_checker, daemon=True).start()

if __name__=="__main__":
    run_bots()
    print("Starting Flask backend on port", APP_PORT)
    app.run(host="0.0.0.0", port=APP_PORT)
