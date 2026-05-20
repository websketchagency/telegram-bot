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
    raise ValueError("❌ CONFIG LIPSĂ")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logger.info(f"✅ ADMIN_USER_IDS: {ADMIN_USER_IDS}")

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

PAYPAL_BASE_URL = "https://api.paypal.com" if PAYPAL_MODE == "live" else "https://api.sandbox.paypal.com"

def get_paypal_token():
    try:
        url = f"{PAYPAL_BASE_URL}/v1/oauth2/token"
        auth = (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
        data = {"grant_type": "client_credentials"}
        resp = requests.post(url, auth=auth, data=data, timeout=10)
        if resp.status_code == 200:
            return resp.json()["access_token"]
    except Exception as e:
        logger.error(f"Eroare token: {e}")
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
        logger.error(f"Eroare verificare: {e}")
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    add_user(user_id, user.username or "N/A", user.first_name or "Utilizator", user.last_name or "")
    status = get_subscription_status(user_id)
    if status == "active":
        days = get_days_remaining(user_id)
        await update.message.reply_text(f"✅ Abonament activ! ({days} zile rămase)\n\n🔗 {PREMIUM_GROUP_LINK}")
    else:
        await update.message.reply_text(
            f"💎 PREMIUM: {SUBSCRIPTION_PRICE} EUR/lună\n⏰ Durată: {SUBSCRIPTION_DAYS} zile\n\nApasă butonul de mai jos:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Plătește pe PayPal", url=PAYPAL_PAYMENT_LINK)]])
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Cumpără abonament\n"
        "/status - Verifică status\n"
        "/info - Informații PREMIUM\n"
        "/add_me - Înregistrează-te pentru cleanup\n"
        "/cleanup - Cleanup manual\n"
        "/invite_all - Trimite mesaj de tracking"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status = get_subscription_status(user_id)
    if status == "active":
        days = get_days_remaining(user_id)
        await update.message.reply_text(f"✅ ACTIV ({days} zile rămase)")
    else:
        await update.message.reply_text("❌ Fără abonament")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"💰 {SUBSCRIPTION_PRICE} EUR/lună\n⏰ {SUBSCRIPTION_DAYS} zile de acces")

async def add_me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    add_user(user_id, user.username or "N/A", user.first_name or "Utilizator", user.last_name or "")
    add_group_member(user_id, PREMIUM_GROUP_CHAT_ID)
    await update.message.reply_text("✅ Te-ai înregistrat! Ești urmărit pentru cleanup automat.")

async def invite_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"🔔 /invite_all apelat de: {update.effective_user.id}")
    
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        msg = f"❌ NU EȘTI ADMIN. ID-ul tău: {user_id}"
        logger.warning(msg)
        await update.message.reply_text(msg)
        return
    
    await update.message.reply_text("📢 Se trimite mesaj în grupul PREMIUM...")
    
    try:
        await context.bot.send_message(
            PREMIUM_GROUP_CHAT_ID,
            "🔔 **IMPORTANT - PĂSTREAZĂ ACCESUL**\n\n"
            "Pentru a menține accesul la grupul PREMIUM, trebuie să:\n\n"
            "1️⃣ Apasă /add_me în DM-ul botului\n"
            "2️⃣ Asta te înregistrează pentru auto-cleanup\n"
            "3️⃣ Fără asta, vei fi restricționat\n\n"
            "👉 /start pentru a deschide botul\n"
            "👉 /add_me pentru a te înregistra",
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text("✅ Mesajul a fost trimis!")
        logger.info("✅ Mesaj de invitație trimis în grupul PREMIUM")
    except Exception as e:
        logger.error(f"Eroare: {e}")
        await update.message.reply_text(f"❌ Eroare: {e}")

async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestionează membri noi în grup - MESAJ PUBLIC ÎN GRUP"""
    if update.chat_member.new_chat_member.status == "member":
        user = update.chat_member.new_chat_member.user
        user_id = user.id
        first_name = user.first_name or "Prieten"
        username = user.username or ""
        
        add_user(user_id, username, first_name, user.last_name or "")
        add_group_member(user_id, update.effective_chat.id)
        
        logger.info(f"✅ Membru nou: {first_name} ({user_id}) a intrat în grup")
        
        # MESAJ PUBLIC ÎN GRUP PENTRU FIECARE MEMBRU NOU
        if update.effective_chat.id == FREE_GROUP_CHAT_ID:
            mention = f"@{username}" if username else first_name
            
            welcome_msg = (
                f"👋 **Bine ai venit, {mention}!**\n\n"
                f"📌 Ești în grupul **FREE** - conținut gratuit zilnic\n\n"
                f"💎 **VREI PREMIUM?**\n"
                f"✨ Acces complet la conținut exclusiv\n"
                f"💰 Preț: **{SUBSCRIPTION_PRICE} EUR/lună**\n"
                f"⏰ Durată: **{SUBSCRIPTION_DAYS} zile**\n\n"
                f"🚀 **Cum să obții PREMIUM:**\n"
                f"1️⃣ Apasă /start în bot\n"
                f"2️⃣ Alege \"💳 Plătește pe PayPal\"\n"
                f"3️⃣ Efectuează plata\n"
                f"4️⃣ Primești acces instant!\n\n"
                f"Enjoy! 🎉"
            )
            
            try:
                await context.bot.send_message(
                    FREE_GROUP_CHAT_ID,
                    welcome_msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Cumpără PREMIUM", url=PAYPAL_PAYMENT_LINK)]
                    ])
                )
                logger.info(f"✅ Mesaj de bun venit PUBLIC trimis pentru {first_name}")
            except Exception as e:
                logger.warning(f"Nu s-a putut trimite mesajul public în grup: {e}")

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
            await update.message.reply_text(f"✅ Plata confirmată!\n💎 Acces {SUBSCRIPTION_DAYS} zile activat!")
            context.user_data.pop('awaiting_tx_id', None)
        else:
            await update.message.reply_text("❌ ID tranzacție invalid!")

async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Acces interzis")
        return
    await update.message.reply_text("⏳ Cleanup inițiat...")

async def run_cleanup(bot):
    try:
        members = get_premium_members()
        if not members:
            logger.warning("Niciun membru pentru cleanup")
            return
        for member_id in members:
            status = get_subscription_status(member_id)
            if status != "active":
                try:
                    await bot.restrict_chat_member(PREMIUM_GROUP_CHAT_ID, member_id, ChatPermissions(can_send_messages=False))
                    await bot.send_message(member_id, "🚨 Abonamentul a expirat!")
                except:
                    pass
    except Exception as e:
        logger.error(f"Eroare cleanup: {e}")

async def send_reminders(app):
    pass

def main():
    logger.info("🚀 Bot pornit...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("add_me", add_me_cmd))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd))
    app.add_handler(CommandHandler("invite_all", invite_all_cmd))
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("✅ Bot online!")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])

if __name__ == "__main__":
    main()
