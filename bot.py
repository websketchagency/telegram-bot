import os
import logging
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ChatMemberHandler
from telegram.constants import ParseMode
from apscheduler.schedulers.background import BackgroundScheduler
import requests

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FREE_GROUP_CHAT_ID = int(os.getenv("FREE_GROUP_CHAT_ID", "0"))
PREMIUM_GROUP_CHAT_ID = int(os.getenv("PREMIUM_GROUP_CHAT_ID", "0"))
PREMIUM_GROUP_LINK = os.getenv("PREMIUM_GROUP_LINK", "")
FREE_GROUP_LINK = os.getenv("FREE_GROUP_LINK", "")
PAYPAL_PAYMENT_LINK = os.getenv("PAYPAL_PAYMENT_LINK", "")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
ADMIN_USER_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "0").split(",") if x.strip()]
SUBSCRIPTION_PRICE = 5.0
SUBSCRIPTION_CURRENCY = "EUR"
SUBSCRIPTION_DAYS = 30

if not all([TELEGRAM_BOT_TOKEN, PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET]):
    raise ValueError("❌ MISSING CONFIG")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = "subscriptions.db"

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, joined_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (id INTEGER PRIMARY KEY, user_id INTEGER, start_date TEXT, end_date TEXT, status TEXT, payment_status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments
                 (id INTEGER PRIMARY KEY, user_id INTEGER, transaction_id TEXT, amount REAL, status TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_members
                 (id INTEGER PRIMARY KEY, user_id INTEGER, group_id INTEGER, joined_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

def add_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?)", 
              (user_id, username, first_name, last_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def add_group_member(user_id, group_id):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO group_members (user_id, group_id, joined_at) VALUES (?, ?, ?)",
              (user_id, group_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_subscription_status(user_id):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT status, end_date FROM subscriptions WHERE user_id = ? ORDER BY end_date DESC LIMIT 1", (user_id,))
    result = c.fetchone()
    conn.close()
    if not result:
        return "none"
    status, end_date = result
    if status == "active" and datetime.fromisoformat(end_date) > datetime.now():
        return "active"
    return "expired"

def get_days_remaining(user_id):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT end_date FROM subscriptions WHERE user_id = ? AND status = 'active' ORDER BY end_date DESC LIMIT 1", (user_id,))
    result = c.fetchone()
    conn.close()
    if not result:
        return 0
    end_date = datetime.fromisoformat(result[0])
    days = (end_date - datetime.now()).days
    return max(0, days)

def add_subscription(user_id, days):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    start = datetime.now()
    end = start + timedelta(days=days)
    c.execute("INSERT INTO subscriptions (user_id, start_date, end_date, status, payment_status) VALUES (?, ?, ?, ?, ?)",
              (user_id, start.isoformat(), end.isoformat(), "active", "completed"))
    conn.commit()
    conn.close()

def add_payment(user_id, transaction_id, amount):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, transaction_id, amount, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, transaction_id, amount, "completed", datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_premium_members():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM group_members WHERE group_id = ?", (PREMIUM_GROUP_CHAT_ID,))
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result

PAYPAL_BASE_URL = "https://api.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api.paypal.com"

def get_paypal_token():
    try:
        url = f"{PAYPAL_BASE_URL}/v1/oauth2/token"
        auth = (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
        data = {"grant_type": "client_credentials"}
        resp = requests.post(url, auth=auth, data=data, timeout=10)
        if resp.status_code == 200:
            return resp.json()["access_token"]
    except Exception as e:
        logger.error(f"Token error: {e}")
    return None

def verify_transaction(transaction_id):
    try:
        token = get_paypal_token()
        if not token:
            return False
        url = f"{PAYPAL_BASE_URL}/v1/reporting/transactions"
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "start_date": (datetime.now() - timedelta(days=1)).isoformat(),
            "end_date": datetime.now().isoformat(),
            "transaction_id": transaction_id
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("transaction_details") and data["transaction_details"][0].get("status") == "S":
                return True
    except Exception as e:
        logger.error(f"Verify error: {e}")
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    add_user(user_id, user.username or "N/A", user.first_name or "User", user.last_name or "")
    status = get_subscription_status(user_id)
    if status == "active":
        days = get_days_remaining(user_id)
        await update.message.reply_text(f"✅ Active subscription! ({days} days left)\n\n🔗 {PREMIUM_GROUP_LINK}")
    else:
        await update.message.reply_text(
            f"💎 Premium: {SUBSCRIPTION_PRICE} EUR/month, {SUBSCRIPTION_DAYS} days\n\nClick below:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay on PayPal", url=PAYPAL_PAYMENT_LINK)]])
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Buy subscription\n"
        "/status - Check status\n"
        "/info - Premium info\n"
        "/add_me - Register for cleanup tracking\n"
        "/cleanup - Admin: cleanup"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status = get_subscription_status(user_id)
    if status == "active":
        days = get_days_remaining(user_id)
        await update.message.reply_text(f"✅ ACTIVE ({days} days)")
    else:
        await update.message.reply_text("❌ No subscription")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💰 {SUBSCRIPTION_PRICE} EUR/month\n⏰ {SUBSCRIPTION_DAYS} days")

async def add_me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User registers themselves for cleanup tracking"""
    user = update.effective_user
    user_id = user.id
    add_user(user_id, user.username or "N/A", user.first_name or "User", user.last_name or "")
    add_group_member(user_id, PREMIUM_GROUP_CHAT_ID)
    await update.message.reply_text("✅ You're registered! You'll be checked in /cleanup")

async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.chat_member.new_chat_member.status == "member":
        user = update.chat_member.new_chat_member.user
        user_id = user.id
        add_user(user_id, user.username or "N/A", user.first_name or "User", user.last_name or "")
        add_group_member(user_id, update.effective_chat.id)
        if update.effective_chat.id == FREE_GROUP_CHAT_ID:
            await context.bot.send_message(update.effective_chat.id, f"👋 Welcome {user.mention_html()}!", parse_mode=ParseMode.HTML)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if context.user_data.get('awaiting_tx_id') == user_id:
        if verify_transaction(text):
            add_subscription(user_id, SUBSCRIPTION_DAYS)
            add_payment(user_id, text, SUBSCRIPTION_PRICE)
            try:
                await context.bot.restrict_chat_member(PREMIUM_GROUP_CHAT_ID, user_id, ChatPermissions(can_send_messages=True))
            except:
                pass
            await update.message.reply_text(f"✅ Payment confirmed!\n💎 Access {SUBSCRIPTION_DAYS} days\n{PREMIUM_GROUP_LINK}")
            context.user_data.pop('awaiting_tx_id', None)
        else:
            await update.message.reply_text("❌ Invalid Transaction ID")

async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin cleanup - restrict users without subscription + send DM"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ No permission")
        return
    
    await update.message.reply_text("⏳ Scanning PREMIUM members from database...")
    
    try:
        members = get_premium_members()
        
        if not members:
            await update.message.reply_text(
                "⚠️ No members in database!\n\n"
                "Members get added when they:\n"
                "1. Join PREMIUM group via /start → PayPal payment\n"
                "2. Press /add_me to register\n\n"
                "Current PREMIUM group members: 0"
            )
            return
        
        restricted_count = 0
        notified_count = 0
        
        for member_id in members:
            status = get_subscription_status(member_id)
            
            if status != "active":
                try:
                    await context.bot.restrict_chat_member(
                        PREMIUM_GROUP_CHAT_ID, member_id,
                        ChatPermissions(can_send_messages=False)
                    )
                    restricted_count += 1
                    logger.info(f"✅ Restricted user {member_id}")
                    
                except Exception as e:
                    logger.warning(f"Could not restrict {member_id}: {e}")
                
                try:
                    await context.bot.send_message(
                        member_id,
                        f"🚨 **SUBSCRIPTION EXPIRED**\n\n"
                        f"Your access to PREMIUM group has been revoked.\n\n"
                        f"**OPTIONS:**\n"
                        f"1️⃣ Join FREE group (free content)\n"
                        f"2️⃣ Renew PREMIUM (5 EUR/month)\n\n"
                        f"Sorry! 🙏",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🆓 JOIN FREE", url=FREE_GROUP_LINK)],
                            [InlineKeyboardButton("💳 RENEW NOW", url=PAYPAL_PAYMENT_LINK)]
                        ])
                    )
                    notified_count += 1
                    logger.info(f"✅ DM sent to user {member_id}")
                    
                except Exception as e:
                    logger.warning(f"Could not send DM to {member_id}: {e}")
        
        await update.message.reply_text(
            f"✅ **CLEANUP COMPLETED**\n\n"
            f"👥 Members scanned: {len(members)}\n"
            f"❌ Restricted: {restricted_count}\n"
            f"📢 Notified: {notified_count}\n\n"
            f"Expired users moved to FREE group with renewal link.",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"cleanup_cmd error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def send_reminders(app):
    subs = []
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT user_id, end_date FROM subscriptions WHERE status = 'active'")
    subs = c.fetchall()
    conn.close()
    now = datetime.now()
    for user_id, end_date_str in subs:
        end_date = datetime.fromisoformat(end_date_str)
        days = (end_date - now).days
        if days == 5:
            try:
                await app.bot.send_message(user_id, "⏰ 5 days left! /start to renew")
            except:
                pass
        elif days == 0:
            try:
                await app.bot.send_message(user_id, "🚨 LAST DAY! /start to renew NOW")
            except:
                pass
        elif days < 0:
            try:
                await app.bot.restrict_chat_member(PREMIUM_GROUP_CHAT_ID, user_id, ChatPermissions(can_send_messages=False))
                await app.bot.send_message(user_id, f"❌ Expired!\n\nOptions:\n🆓 FREE: {FREE_GROUP_LINK}\n💳 RENEW: {PAYPAL_PAYMENT_LINK}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("FREE", url=FREE_GROUP_LINK)], [InlineKeyboardButton("RENEW", url=PAYPAL_PAYMENT_LINK)]]))
            except:
                pass

def main():
    logger.info("🚀 Bot starting...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("add_me", add_me_cmd))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd))
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_reminders, 'interval', hours=1, args=[app])
    scheduler.start()
    logger.info("✅ Bot online!")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    main()
