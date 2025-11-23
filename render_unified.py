# render_unified.py
import os
import math
import json
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# -------------------------
# Configuration / env
# -------------------------
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # e.g. https://stake-aware-bot.onrender.com
PORT = int(os.getenv("PORT", 10000))

MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ACCESS_BOT_TOKEN = os.getenv("ACCESS_BOT_TOKEN")
RESULTS_BOT_TOKEN = os.getenv("RESULTS_BOT_TOKEN")

PAYSTACK_DAILY = os.getenv("PAYSTACK_DAILY_LINK", "")
PAYSTACK_WEEKEND = os.getenv("PAYSTACK_WEEKEND_LINK", "")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "")
ACCESS_BOT_USERNAME = os.getenv("ACCESS_BOT_USERNAME", "")

DAILY_GROUP_ID = int(os.getenv("DAILY_GROUP_ID", "0"))
WEEKEND_GROUP_ID = int(os.getenv("WEEKEND_GROUP_ID", "0"))
DAILY_GROUP_LINK = os.getenv("DAILY_GROUP_LINK", "")
WEEKEND_GROUP_LINK = os.getenv("WEEKEND_GROUP_LINK", "")

# Admin IDs: CSV or single
ADMIN_IDS_RAW = os.getenv("ADMIN_TELEGRAM_IDS") or os.getenv("ADMIN_TELEGRAM_ID") or ""
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

# Files
USERS_FILE = os.path.join("data", "users.json")
os.makedirs("data", exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stakeaware")

# sanity checks
if not PUBLIC_URL:
    log.warning("PUBLIC_URL not set — webhooks won't register correctly until PUBLIC_URL is provided.")

# -------------------------
# Utilities
# -------------------------
def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# -------------------------
# Flask app (async view support)
# -------------------------
app = Flask("stakeaware_backend")

@app.get("/")
def index():
    return jsonify({"status": "StakeAware unified bot backend"}), 200

@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

# We'll add webhook routes later (after apps built)


# -------------------------
# Build PTB Application objects
# -------------------------
# We'll build three app objects: main_app, access_app, results_app
main_app = None
access_app = None
results_app = None

# Results in-memory store (cleared after posting)
games = []

# Conversation state for adding games
ADDING_GAME = 0

# -------------------------
# Handlers: MAIN BOT
# -------------------------
async def main_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Stake Aware provides daily 3-odds tickets based on deep analysis of sports trends and statistics.\n\n"
        "Subscribe for ₦50,000/month to receive daily predictions or ₦20,000/month for Weekend games only directly here in Telegram.\n\n"
        "💡 We study matches, form, and trends so you do not have to.\n\n"
        "Here is what you get as a Premium Subscriber 👇\n"
        "✅ Daily 3+ Odds Predictions carefully analyzed by our team.\n"
        "✅ Expert insights designed to maximize profits and minimize risks.\n"
        "✅ Consistent, data-backed selections that help you stay ahead of the betting market.\n"
        "✅ 24/7 access to exclusive tips — no guesswork, just strategy and precision!\n\n"
        "💰 In this group, we don’t chase luck — we create winning moments.\n"
        "Prepare to level up your betting game and start winning like a pro!\n\n"
        "Welcome once again — your journey to beating the bookies begins NOW! 🏆\n"
        "Choose your subscription plan below. After payment, click the link to automatically verify your Telegram account."
    )

    buttons = [
        [InlineKeyboardButton("💎 Daily 3-Odds — ₦50,000", url=PAYSTACK_DAILY)],
        [InlineKeyboardButton("🎯 Weekend 3-Odds — ₦20,000", url=PAYSTACK_WEEKEND)],
        [InlineKeyboardButton("✅ Verify Access", url=f"https://t.me/{ACCESS_BOT_USERNAME}")]
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(text, reply_markup=keyboard)

# -------------------------
# Handlers: ACCESS BOT
# -------------------------
async def access_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args  # deep-link param
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ Check Status", callback_data="status")]])

    if args:
        ref = args[0]
        try:
            url = f"{BACKEND_BASE_URL}/link_telegram"
            resp = requests.post(url, json={"reference": ref, "chat_id": chat_id}, timeout=8)
            if resp.status_code == 200:
                await update.message.reply_text("✅ Payment reference linked. You now have access if the payment is valid.", reply_markup=kb)
                return
            else:
                await update.message.reply_text(f"❌ Could not link reference: {resp.text}", reply_markup=kb)
                return
        except Exception as e:
            await update.message.reply_text(f"❌ Error connecting to backend: {e}", reply_markup=kb)
            return

    await update.message.reply_text(
        "Welcome to StakeAware Access Bot.\n\nIf you completed payment, open the verification link from the payment page (it should open this bot with a reference). Use the button to check /status.",
        reply_markup=kb
    )

async def access_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    try:
        headers = {}
        admin_key = os.getenv("BACKEND_ADMIN_KEY") or os.getenv("JWT_SECRET")
        if admin_key:
            headers["x-admin-key"] = admin_key
        resp = requests.get(f"{BACKEND_BASE_URL}/admin/users", headers=headers, timeout=8)
        if resp.status_code != 200:
            await update.message.reply_text("Could not fetch status from backend.")
            return
        users = resp.json()
        for email, u in users.items():
            if int(u.get("chat_id", 0)) == cid:
                exp = u.get("expires_at")
                exp_str = datetime.utcfromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S") if exp else "unknown"
                await update.message.reply_text(f"✅ Active plan: {u.get('plan')} | Expires at (UTC): {exp_str}")
                return
        await update.message.reply_text("❌ No active subscription found for this account.")
    except Exception as e:
        await update.message.reply_text(f"Error fetching status: {e}")

# -------------------------
# Handlers: RESULTS BOT
# -------------------------
def results_main_menu_kb():
    kb = [
        [InlineKeyboardButton("➕ Add Game", callback_data="add_game")],
        [InlineKeyboardButton("📋 List Games", callback_data="list_games")],
        [InlineKeyboardButton("📤 Post Games", callback_data="post_games")],
        [InlineKeyboardButton("🗑️ Clear Games", callback_data="clear_games")],
    ]
    return InlineKeyboardMarkup(kb)

def format_games_list_text():
    if not games:
        return "📭 No games added yet."
    lines = ["🎯 *STAKEAWARE OFFICIAL PREDICTION FOR THE DAY*\n"]
    total = 1.0
    any_odds = False
    for i, g in enumerate(games, start=1):
        toks = g.strip().split()
        odds = None
        for t in reversed(toks):
            try:
                odds = float(t.replace(",", "."))
                break
            except Exception:
                continue
        if odds:
            total *= odds
            any_odds = True
        lines.append(f"{i}. *{g}*")
    total_text = f"{total:.2f}" if any_odds else "—"
    lines.append(f"\n💰 *Total Odds:* {total_text}")
    lines.append("\n🔥 Play Responsibly 🔥")
    return "\n".join(lines)

async def results_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Welcome — you will receive results in your groups.")
        return
    await update.message.reply_text("StakeAware Results Bot.\nUse the menu below to manage results.", reply_markup=results_main_menu_kb())

async def results_handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        await query.answer("❌ Not authorized", show_alert=True)
        return

    data = query.data
    if data == "add_game":
        await query.message.edit_text("Send the game in this format:\nTeamA vs TeamB TYPE - 1.55 odds\n\nReply with the game text (just send the text).", reply_markup=results_main_menu_kb())
        # no explicit conversation object — we'll handle next messages by message handler that checks last prompt
        return

    if data == "list_games":
        await query.message.edit_text(format_games_list_text(), parse_mode="Markdown", reply_markup=results_main_menu_kb())
        return

    if data == "clear_games":
        games.clear()
        await query.message.edit_text("🗑️ All added games cleared.", reply_markup=results_main_menu_kb())
        return

    if data == "post_games":
        if not games:
            await query.answer("No games to post.", show_alert=True)
            return
        text = format_games_list_text()
        weekday = datetime.utcnow().weekday()  # Mon=0
        targets = [DAILY_GROUP_ID]
        if weekday in [4,5,6]:  # Fri-Sun -> 4,5,6
            targets.append(WEEKEND_GROUP_ID)
        sent = 0
        for gid in targets:
            try:
                await context.bot.send_message(chat_id=gid, text=text, parse_mode="Markdown")
                sent += 1
            except Exception as e:
                log.exception("Error posting to %s: %s", gid, e)
        games.clear()
        await query.message.edit_text(f"✅ Results posted to {sent} group(s).", reply_markup=results_main_menu_kb())
        return

async def results_add_game_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Invalid game text.")
        return
    games.append(text)
    await update.message.reply_text(f"✅ Game added:\n*{text}*", parse_mode="Markdown", reply_markup=results_main_menu_kb())

# -------------------------
# Register handlers into Applications
# -------------------------
async def build_and_register():
    global main_app, access_app, results_app

    # Build Apps
    if MAIN_BOT_TOKEN:
        main_app = ApplicationBuilder().token(MAIN_BOT_TOKEN).build()
        main_app.add_handler(CommandHandler("start", main_start))
    else:
        log.warning("MAIN_BOT_TOKEN missing — main_app not built.")

    if ACCESS_BOT_TOKEN:
        access_app = ApplicationBuilder().token(ACCESS_BOT_TOKEN).build()
        access_app.add_handler(CommandHandler("start", access_start))
        access_app.add_handler(CommandHandler("status", access_status))
        access_app.add_handler(CallbackQueryHandler(lambda u, c: access_status(u, c), pattern="status"))
    else:
        log.warning("ACCESS_BOT_TOKEN missing — access_app not built.")

    if RESULTS_BOT_TOKEN:
        results_app = ApplicationBuilder().token(RESULTS_BOT_TOKEN).build()
        results_app.add_handler(CommandHandler("start", results_start))
        results_app.add_handler(CallbackQueryHandler(results_handle_callback))
        # message handler for adding games (admin replies)
        results_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, results_add_game_message))
    else:
        log.warning("RESULTS_BOT_TOKEN missing — results_app not built.")

    # Initialize apps (prepare internal resources)
    inits = []
    for obj, name in [(main_app, "main"), (access_app, "access"), (results_app, "results")]:
        if obj:
            inits.append(obj.initialize())
    if inits:
        await asyncio.gather(*inits)
    log.info("All applications initialized.")

    # set webhooks (if PUBLIC_URL provided)
    if PUBLIC_URL:
        hooks = []
        if main_app:
            hooks.append(main_app.bot.set_webhook(f"{PUBLIC_URL}/webhook-main"))
        if access_app:
            hooks.append(access_app.bot.set_webhook(f"{PUBLIC_URL}/webhook-access"))
        if results_app:
            hooks.append(results_app.bot.set_webhook(f"{PUBLIC_URL}/webhook-results"))
        if hooks:
            await asyncio.gather(*hooks)
            log.info("Webhooks registered at PUBLIC_URL.")

# -------------------------
# Webhook endpoints
# -------------------------
# These are async endpoints — Flask supports async view functions.
@app.post("/webhook-main")
async def webhook_main():
    if not main_app:
        return "main bot not configured", 503
    data = await request.get_json(force=True)
    update = Update.de_json(data, main_app.bot)
    await main_app.process_update(update)
    return "ok", 200

@app.post("/webhook-access")
async def webhook_access():
    if not access_app:
        return "access bot not configured", 503
    data = await request.get_json(force=True)
    update = Update.de_json(data, access_app.bot)
    await access_app.process_update(update)
    return "ok", 200

@app.post("/webhook-results")
async def webhook_results():
    if not results_app:
        return "results bot not configured", 503
    data = await request.get_json(force=True)
    update = Update.de_json(data, results_app.bot)
    await results_app.process_update(update)
    return "ok", 200

# -------------------------
# Startup + serve
# -------------------------
async def main():
    log.info("Starting stakeaware unified backend...")
    # create data/users.json if missing
    try:
        if not os.path.exists(USERS_FILE):
            save_users({})
    except Exception:
        log.exception("Failed creating users file.")

    await build_and_register()
    # start Hypercorn to serve the Flask app asynchronously (avoids loop conflicts)
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    log.info("Ready to serve on port %s", PORT)
    await serve(app, config)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
    except Exception:
        log.exception("Fatal startup error.")
