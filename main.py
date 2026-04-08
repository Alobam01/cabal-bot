import asyncio
import os
import json
import re
from sqlalchemy import text
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from db import init_db, AsyncSessionLocal
from models import UserConfig
from user_listener import start_user_listener
from price import get_sol_price_usd



API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

YOUR_SOL_WALLET = "6oU4uLAfavhXWoF68rDNcChs7tzfs4AQ6Dq3VwwjWCLJ"

tasks = {}  # user_id: task
login_tasks = {}  # user_id: task
DEFAULT_SOURCE_GROUPS = ["solearlytrending", "solwhaletrending"]

# Conversation states
PHONE, CODE = range(2)

async def is_subscribed(user):
    if user.subscription_status == "active" and user.subscription_expiry and user.subscription_expiry > datetime.utcnow():
        return True
    if user.subscription_status == "trial" and user.trial_start:
        if datetime.utcnow() - user.trial_start < timedelta(days=3):
            return True
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to **Cabal CA Filter SaaS**\n\n"
        "Commands:\n"
        "/login — Login (QR, recommended)\n"
        "/login_code — Login with your phone (less reliable)\n"
        "/plans — See pricing\n"
        "/subscribe — Get SOL payment details\n"
        "/addgroups — Set your 3 cabal groups (one per line)\n"
        "/settarget — Set your private group\n"
        "/train <CA or scanner link> — Add successful CA (build your dataset)\n"
        "/startlistening — Activate\n"
        "/stop — Stop listener\n"
        "/status — Check status\n"
        "/checkpayment <tx> — Submit payment tx"
    )

async def plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = await get_sol_price_usd()
    sol_amount = round(10 / price, 4)
    await update.message.reply_text(
        f"💰 Pricing\n\n"
        f"• **3 Days Free Trial**\n"
        f"• **$10 / month** ≈ **{sol_amount} SOL** (SOL ≈ ${price:.2f})\n\n"
        "Use /subscribe after trial."
    )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    price = await get_sol_price_usd()
    sol_amount = round(10 / price, 4)
    memo = f"cabal_saas_{user_id}_{int(datetime.utcnow().timestamp())}"
    
    await update.message.reply_text(
        f"💸 Monthly Subscription - $10\n\n"
        f"Send **{sol_amount} SOL** to:\n"
        f"`{YOUR_SOL_WALLET}`\n\n"
        f"**Memo** (include exactly):\n"
        f"`{memo}`\n\n"
        f"After sending, use:\n"
        f"`/checkpayment <transaction_signature>`"
    )

async def _finalize_login(user_id: int, session_string: str):
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if not user:
            user = UserConfig(telegram_id=user_id)
        user.session_string = session_string
        user.subscription_status = "trial"
        user.trial_start = datetime.utcnow()
        session.add(user)
        await session.commit()

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in login_tasks and not login_tasks[user_id].done():
        await update.message.reply_text("Login already in progress. Use the link I sent you.")
        return

    await update.message.reply_text(
        "Open the login link on the SAME phone where Telegram is installed, then approve the login.\n\n"
        "If the tg:// link doesn't open, use the https://t.me/login link instead."
    )

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    qr_login = await client.qr_login()

    url = qr_login.url
    await update.message.reply_text(url)
    if url.startswith("tg://login?token="):
        await update.message.reply_text(url.replace("tg://login?token=", "https://t.me/login?token="))

    async def waiter():
        nonlocal qr_login
        try:
            for _ in range(3):
                try:
                    await asyncio.wait_for(qr_login.wait(), timeout=180)
                    break
                except asyncio.TimeoutError:
                    qr_login = await client.qr_login()
                    url = qr_login.url
                    await update.message.reply_text("Login link expired. Here is a fresh one:")
                    await update.message.reply_text(url)
                    if url.startswith("tg://login?token="):
                        await update.message.reply_text(url.replace("tg://login?token=", "https://t.me/login?token="))
            else:
                await update.message.reply_text("Login failed: timed out waiting for approval.")
                return

            try:
                session_string = client.session.save()
                await _finalize_login(user_id, session_string)
                await update.message.reply_text("✅ Login successful! Session saved.\nNow set your groups with /addgroups")
            except SessionPasswordNeededError:
                password = os.getenv("TG_2FA_PASSWORD")
                if not password:
                    await update.message.reply_text(
                        "Login needs your Telegram 2FA password.\n\n"
                        "Set TG_2FA_PASSWORD in your environment (Render env vars) and run /login again."
                    )
                    return
                await client.sign_in(password=password)
                session_string = client.session.save()
                await _finalize_login(user_id, session_string)
                await update.message.reply_text("✅ Login successful! Session saved.\nNow set your groups with /addgroups")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await update.message.reply_text(f"Login failed ({type(e).__name__}): {e}")
        finally:
            try:
                await client.disconnect()
            finally:
                login_tasks.pop(user_id, None)

    login_tasks[user_id] = asyncio.create_task(waiter())

# Login Conversation
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send your phone number with country code (e.g. +234xxxxxxxxxx)")
    return PHONE

async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['phone'] = update.message.text.strip()
    await update.message.reply_text("🔄 Sending verification code... Please wait a moment.")
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(context.user_data['phone'])
        context.user_data['client'] = client
        context.user_data['phone_code_hash'] = sent.phone_code_hash
        await update.message.reply_text("✅ Code sent! Send the 5-digit code here.")
        return CODE
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return ConversationHandler.END

async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client = context.user_data.get('client')
    if not client:
        await update.message.reply_text("Session expired. Try /login again.")
        return ConversationHandler.END
    try:
        await client.sign_in(context.user_data['phone'], code, phone_code_hash=context.user_data['phone_code_hash'])
        session_string = client.session.save()

        await _finalize_login(update.effective_user.id, session_string)
        await update.message.reply_text("✅ Login successful! Session saved.\nNow set your groups with /addgroups")
        await client.disconnect()
    except SessionPasswordNeededError:
        await update.message.reply_text("2FA password required. This version doesn't support 2FA yet.")
    except Exception as e:
        await update.message.reply_text(f"Login error: {e}")
    return ConversationHandler.END

async def addgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    groups = update.message.text.split('\n')[1:]  # skip command
    groups = [g.strip() for g in groups if g.strip()][:3]
    
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if not user:
            await update.message.reply_text("Login first with /login")
            return
        user.source_groups = groups
        await session.commit()
    
    await update.message.reply_text(f"✅ 3 groups saved: {groups}")

async def settarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target = update.message.text.split(maxsplit=1)[1].strip() if len(update.message.text.split()) > 1 else None
    if not target:
        await update.message.reply_text("Usage: /settarget @yourprivategroup or -100xxxxxxxxx")
        return
    
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if not user:
            await update.message.reply_text("Login first")
            return
        user.target_group = target
        await session.commit()
    
    await update.message.reply_text(f"✅ Target group saved: {target}")

async def train(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    payload = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""
    if not payload:
        await update.message.reply_text("Usage: /train <CA or scanner link>")
        return

    scanner_match = re.search(r"(?:https?://)?t\.me/soul_scanner_bot\?start=([^\s&]+)", payload, re.IGNORECASE)
    if scanner_match:
        ca = scanner_match.group(1)
        if ca.lower().startswith("ets_"):
            ca = ca[len("ets_") :]
        candidates = [ca.strip()]
    else:
        candidates = [line.strip() for line in payload.splitlines() if line.strip()]
    candidates = [c for c in candidates if 8 <= len(c) <= 120]
    
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if not user:
            await update.message.reply_text("Login first")
            return
        if not user.training_examples:
            user.training_examples = []
        added = 0
        for ca in candidates:
            if ca not in user.training_examples:
                user.training_examples.append(ca)
                added += 1
        if len(user.training_examples) > 500:
            user.training_examples = user.training_examples[-500:]
        await session.commit()
    
    await update.message.reply_text(f"✅ Added {added} CA(s) ({len(user.training_examples)} total)")

async def startlistening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if not user or not user.session_string:
            await update.message.reply_text("Login first with /login")
            return
        if not await is_subscribed(user):
            await update.message.reply_text("Your trial expired or no active subscription. Use /subscribe")
            return
        if not user.target_group:
            await update.message.reply_text("Set your target with /settarget first")
            return
        if not user.training_examples:
            await update.message.reply_text("Add training CAs first with /train <CA or scanner link>")
            return
        
        if user.telegram_id in tasks and not tasks[user.telegram_id].done():
            await update.message.reply_text("Listener already running")
            return
        
        user.is_active = True
        await session.commit()
    
    task = asyncio.create_task(
        start_user_listener(user_id, user.session_string, user.source_groups or DEFAULT_SOURCE_GROUPS, 
                           user.target_group, user.training_examples or [], API_ID, API_HASH)
    )
    tasks[user_id] = task
    await update.message.reply_text("🚀 Listener started! High-quality CAs will be sent to your group.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in tasks and not tasks[user_id].done():
        tasks[user_id].cancel()
        del tasks[user_id]
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if user:
            user.is_active = False
            await session.commit()
    await update.message.reply_text("⛔ Listener stopped")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if not user:
            await update.message.reply_text("No account found. Use /login")
            return
        
        sub = "Active" if await is_subscribed(user) else "Expired/Trial ended"
        groups = len(user.source_groups) if user.source_groups else 0
        examples = len(user.training_examples) if user.training_examples else 0
        
        await update.message.reply_text(
            f"📊 Status\n\n"
            f"Subscription: {sub}\n"
            f"Groups: {groups}/3\n"
            f"Training examples: {examples}\n"
            f"Target: {user.target_group or 'Not set'}\n"
            f"Listener: {'Running' if user.is_active else 'Stopped'}"
        )

async def checkpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /checkpayment <tx_signature>")
        return
    tx = context.args[0]
    user_id = update.effective_user.id
    # Manual confirmation - you check on Solscan then run this or update DB
    async with AsyncSessionLocal() as session:
        user = await session.get(UserConfig, user_id)
        if user:
            user.subscription_status = "active"
            user.subscription_expiry = datetime.utcnow() + timedelta(days=30)
            user.last_payment_tx = tx
            await session.commit()
            await update.message.reply_text("✅ Payment recorded! Subscription activated for 30 days.")
        else:
            await update.message.reply_text("Account not found.")

def main_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('login_code', login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
        },
        fallbacks=[],
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("plans", plans))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("addgroups", addgroups))
    application.add_handler(CommandHandler("settarget", settarget))
    application.add_handler(CommandHandler("train", train))
    application.add_handler(CommandHandler("startlistening", startlistening))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("checkpayment", checkpayment))
    application.add_handler(conv_handler)
    
    print("🚀 Full Cabal SaaS Bot Started (with your SOL wallet)")
    print("Bot is now running... Test with /start in Telegram")
    
    application.run_polling()

if __name__ == '__main__':
    main_bot()
