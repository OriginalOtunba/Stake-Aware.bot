# render_unified.py
import os
import logging
import time
import hmac
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

load_dotenv()

# ==== CONFIG ====
BOT_TOKEN = os.environ.get("ACCESS_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.environ.get("ADMIN_TELEGRAM_ID", "0").split(",")))
DAILY_GROUP_ID = os.environ.get("DAILY_GROUP_ID")
WEEKEND_GROUP_ID = os.environ.get("WEEKEND_GROUP_ID")
USERS_FILE = Path("data/users.json")
USERS_FILE.parent.mkdir(parents=True, exist_ok=True)

# Paystack config
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET", "")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
ACCESS_BOT_USERNAME = os.environ.get("ACCESS_BOT_USERNAME", "StakeAwareAccessBot")
DAILY_PLAN_AMOUNT = int(os.environ.get("DAILY_PLAN_AMOUNT", "50000"))
WEEKEND_PLAN_AMOUNT = int(os.environ.get("WEEKEND_PLAN_AMOUNT", "20000"))
DAILY_PLAN_DURATION = int(os.environ.get("DAILY_PLAN_DURATION", "30"))
WEEKEND_PLAN_DURATION = int(os.environ.get("WEEKEND_PLAN_DURATION", "30"))
EXPIRY_ALERT_DAYS = int(os.environ.get("EXPIRY_ALERT_DAYS", "3"))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==== STATES ====
ADDING_GAME = range(1)
games = []

# ==== HELPERS ====
def load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except:
        return {}

def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2))

def is_admin(user_id):
    return user_id in ADMIN_IDS

def format_games_list():
    if not games:
        return "📭 No games added yet."
    msg = "🎯 *STAKEAWARE OFFICIAL PREDICTION FOR THE DAY*\n\n"
    total_odds = 1.0
    for i, g in enumerate(games):
        try:
            odds = float(g.split()[-2])
            total_odds *= odds
        except:
            odds = None
        msg += f"{i+1}. *{g}*\n"
    msg += f"\n💰 *Total Odds:* {total_odds:.2f}" if total_odds != 1.0 else "\n💰 *Total Odds:* —"
    msg += "\n\n🔥 Play Responsibly 🔥"
    return msg

def send_admin_message(text):
    for admin_id in ADMIN_IDS:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", 
                          json={"chat_id": admin_id, "text": text}, timeout=8)
        except:
            pass

def grant_or_renew(email, plan, reference):
    users = load_users()
    now = int(time.time())
    duration_days = DAILY_PLAN_DURATION if plan == "daily" else WEEKEND_PLAN_DURATION
    expires_at = now + duration_days * 24 * 3600

    prev = users.get(email)
    if prev and prev.get("expires_at", 0) > now:
        new_expiry = max(prev["expires_at"], expires_at)
        prev.update({
            "plan": plan,
            "paystack_reference": reference,
            "expires_at": new_expiry,
            "active": True
        })
        users[email] = prev
        action = "renewed"
    else:
        users[email] = {
            "email": email,
            "plan": plan,
            "paystack_reference": reference,
            "expires_at": expires_at,
            "active": True,
            "chat_id": None
        }
        action = "activated"
    save_users(users)
    deep_link = f"https://t.me/{ACCESS_BOT_USERNAME}?start={reference}"
    send_admin_message(f"{email} {action} ({plan}). Paystack ref: {reference}\nDeep-link: {deep_link}")
    return users[email]

def verify_paystack_signature(req):
    if not PAYSTACK_WEBHOOK_SECRET:
        return True
    sig = req.headers.get("x-paystack-signature", "")
    body = req.get_data()
    computed = hmac.new(PAYSTACK_WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(sig, computed)

# ==== TELEGRAM BOT HANDLERS ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("Welcome Admin! Use buttons to manage games.",
                                        reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("Welcome! You will receive results in your groups.")

def main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton("➕ Add Game", callback_data="add_game")],
        [InlineKeyboardButton("📋 List Games", callback_data="list_games")],
        [InlineKeyboardButton("📤 Post Games", callback_data="post_games")],
        [InlineKeyboardButton("🗑️ Clear Games", callback_data="clear_games")]
    ]
    return InlineKeyboardMarkup(buttons)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Not authorized", show_alert=True)
        return

    if query.data == "add_game":
        await query.edit_message_text("Send the game in this format:\nTeamA vs TeamB GG - 1.55 odds",
                                      reply_markup=main_menu_keyboard())
        return ADDING_GAME
    elif query.data == "list_games":
        await query.edit_message_text(format_games_list(), parse_mode="Markdown", reply_markup=main_menu_keyboard())
    elif query.data == "post_games":
        await post_games(update, context)
        await query.edit_message_text("Menu:", reply_markup=main_menu_keyboard())
    elif query.data == "clear_games":
        games.clear()
        await query.edit_message_text("🗑️ All added games cleared", reply_markup=main_menu_keyboard())

async def add_game_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Invalid input")
        return ADDING_GAME
    games.append(text)
    await update.message.reply_text(f"✅ Game added:\n*{text}*", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ADDING_GAME

async def post_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not games:
        await update.callback_query.message.reply_text("📭 No games to post.")
        return
    msg = format_games_list()
    day = datetime.now().weekday()
    targets = [DAILY_GROUP_ID]
    if day in [4,5,6]:
        targets.append(WEEKEND_GROUP_ID)
    for gid in targets:
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Failed to post to {gid}: {e}")
    games.clear()
    await update.callback_query.message.reply_text(f"✅ Posted to {len(targets)} group(s).", reply_markup=main_menu_keyboard())

# ==== FLASK APP ====
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "StakeAware backend running", 200

@app.route("/webhook/paystack", methods=["POST"])
def webhook_paystack():
    if not verify_paystack_signature(request):
        return "Invalid signature", 400
    try:
        event_json = request.get_json(force=True)
    except:
        return jsonify({"error": "invalid json"}), 400
    event_type = event_json.get("event")
    if event_type != "charge.success":
        return jsonify({"status": "ignored"}), 200
    data = event_json.get("data") or {}
    reference = data.get("reference")
    email = (data.get("customer") or {}).get("email") or data.get("customer_email")
    amount = int(data.get("amount", 0)) // 100
    if not reference or not email:
        return jsonify({"error": "missing reference/email"}), 400
    plan = "daily" if amount >= DAILY_PLAN_AMOUNT else "weekend"
    grant_or_renew(email, plan, reference)
    return jsonify({"status": "ok"}), 200

@app.route("/link_telegram", methods=["POST"])
def link_telegram():
    try:
        data = request.get_json(force=True)
    except:
        return jsonify({"error": "invalid json"}), 400
    chat_id = data.get("chat_id") or data.get("telegram_id")
    reference = data.get("reference") or data.get("paystack_reference")
    if not chat_id or not reference:
        return jsonify({"error": "chat_id and reference required"}), 400
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
    return jsonify({"status": "linked", "email": email}), 200

# ==== EXPIRY CHECKER ====
def expiry_checker():
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
                            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                          json={"chat_id": u["chat_id"],
                                                "text": f"Reminder: your {u.get('plan')} subscription expires on {datetime.utcfromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S UTC')}"})
                        except:
                            pass
                    else:
                        send_admin_message(f"User {email} ({u.get('plan')}) expires soon. Deep-link: https://t.me/{ACCESS_BOT_USERNAME}?start={u.get('paystack_reference')}")
                if exp <= now:
                    u["active"] = False
                    users[email] = u
                    changed = True
                    send_admin_message(f"{email} subscription expired.")
        if changed:
            save_users(users)
        time.sleep(3600)

# ==== START BOT THREAD ====
def start_telegram_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_menu)],
        states={ADDING_GAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_game_message)]},
        fallbacks=[],
        allow_reentry=True
    )
    application.add_handler(conv_handler)
    print("✅ Unified Telegram Bot running...")
    application.run_polling()

threading.Thread(target=start_telegram_bot, daemon=True).start()
threading.Thread(target=expiry_checker, daemon=True).start()

# ==== START FLASK ====
if __name__ == "__main__":
    print("Starting Flask server for webhooks...")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
