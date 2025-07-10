#!/usr/bin/env python3
"""
bot.py - Telegram bot to fetch top 7 tennis matches (next 3 days),
set thresholds by player surname, list/remove thresholds, and notify on drops.

Setup:
1. Python 3.8+
2. pip3 install python-telegram-bot requests
3. python3 bot.py
"""
import sys
import logging
import requests
import re
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import json
from pathlib import Path
import subprocess
import time
import asyncio

# File to persist thresholds
THRESHOLDS_FILE = Path(__file__).parent / 'thresholds.json'

def load_thresholds():
    global thresholds
    try:
        with open(THRESHOLDS_FILE, 'r') as f:
            thresholds = json.load(f)
    except FileNotFoundError:
        thresholds = {}
    except json.JSONDecodeError:
        thresholds = {}

def save_thresholds():
    THRESHOLDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(THRESHOLDS_FILE, 'w') as f:
        json.dump(thresholds, f)
    # commit the updated thresholds file and push
    commit_and_push()

def commit_and_push():
    try:
        cwd = Path(__file__).parent
        subprocess.run(["git", "add", str(THRESHOLDS_FILE.name)], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-m", "Persist thresholds update"], cwd=cwd, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=cwd, check=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Git push failed: {e}")

# ======== Config ========
TELEGRAM_TOKEN = '7495330094:AAF1-3HvNMyYft2jI1d0QZL3tTiDyQ0cx1c'
ODDS_API_KEY   = 'fd52b739736aa01f79326410472dbf4b'
SPORT_KEY      = 'tennis'

# Validate credentials
if not TELEGRAM_TOKEN or ' ' in TELEGRAM_TOKEN:
    print("Invalid TELEGRAM_TOKEN")
    sys.exit(1)
if not ODDS_API_KEY:
    print("Invalid ODDS_API_KEY")
    sys.exit(1)

# Logging setup
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory thresholds: chat_id -> list of {'surname': str, 'threshold': float}
thresholds = {}

# Helper: 'F. Lastname'
def format_name(full_name: str) -> str:
    parts = full_name.split()
    return f"{parts[0][0]}. {parts[-1]}" if parts else full_name

# Fetch raw market data
def fetch_markets():
    url = f'https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds/'
    params = {'regions': 'uk,us,eu,au', 'markets': 'h2h', 'apiKey': ODDS_API_KEY}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json() or []

# Get top 7 markets within next 3 days
def get_top7_markets():
    data = fetch_markets()
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc + timedelta(days=3)
    upcoming = []
    for m in data:
        try:
            dt_utc = datetime.fromisoformat(m['commence_time'].replace('Z', '+00:00')).astimezone(timezone.utc)
            if now_utc <= dt_utc <= cutoff:
                upcoming.append((m, dt_utc))
        except:
            continue
    top7 = sorted(
        upcoming,
        key=lambda x: (-x[0].get('total_matched', x[0].get('totalMatched', 0)), x[1])
    )[:7]
    return top7

async def check_single_threshold(chat: int, surname: str, thr_price: float, send_func):
    top7 = get_top7_markets()
    surname_lc = surname.lower()
    for mkt, _ in top7:
        for o in mkt['bookmakers'][0]['markets'][0]['outcomes']:
            if o['name'].lower().split()[-1] == surname_lc and o['price'] <= thr_price:
                await send_func(chat, surname, o['price'], thr_price)
                return True
    return False

async def send_threshold_alert(chat, surname, price, thr_price):
    text = f"⚠️ *{surname}* odds dropped to {price} (≤ {thr_price})"
    await app.bot.send_message(chat_id=chat, text=text, parse_mode='Markdown')

# Handler: /t10t - list matches and check thresholds
async def handle_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    await update.message.reply_text("Fetching top 7 tennis matches (next 3 days)...")

    # Build a map of current thresholds for quick lookup
    user_thresholds = thresholds.get(chat, [])
    thr_map = {thr['surname'].lower(): thr['threshold'] for thr in user_thresholds}

    top7 = get_top7_markets()

    # List matches
    for idx, (mkt, dt_utc) in enumerate(top7, start=1):
        home_full = mkt.get('home_team', 'Unknown')
        away_full = mkt.get('away_team', 'Unknown')
        home = format_name(home_full)
        away = format_name(away_full)
        dt_local = dt_utc.astimezone()
        # Mark live matches (commenced in the past)
        now_utc = datetime.now(timezone.utc)
        live_flag = " 🔴 LIVE" if dt_utc <= now_utc else ""

        time_str = dt_local.strftime('%A, %H%M GMT')

        outcomes = mkt['bookmakers'][0]['markets'][0]['outcomes']
        home_price = next((o['price'] for o in outcomes if o['name'] == home_full), 'N/A')
        away_price = next((o['price'] for o in outcomes if o['name'] == away_full), 'N/A')

        # Check if there's a threshold set for these players
        home_surname = home_full.split()[-1].lower()
        away_surname = away_full.split()[-1].lower()
        home_thr = thr_map.get(home_surname)
        away_thr = thr_map.get(away_surname)

        # Annotate if threshold exists
        home_annotation = f" (watch <{home_thr})" if home_thr is not None else ""
        away_annotation = f" (watch <{away_thr})" if away_thr is not None else ""

        # Build and send message for each match
        text = (
            f"{idx}. *{home} vs {away}*{live_flag} — {time_str}\n"
            f"   • {home}: {home_price}{home_annotation}\n"
            f"   • {away}: {away_price}{away_annotation}"
        )
        await update.message.reply_text(text, parse_mode='Markdown')

# Handler: /setthreshold Surname Price
async def setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /setthreshold <surname> <price> (e.g. /setthreshold Fritz 3.10)"
        )
        return
    surname = args[0]
    try:
        price = float(args[1])
    except ValueError:
        await update.message.reply_text("Invalid price. Use a number like 3.10")
        return
    thresholds.setdefault(chat, []).append({'surname': surname, 'threshold': price})
    save_thresholds()
    await update.message.reply_text(
        f"Threshold set: *{surname}* < {price}", parse_mode='Markdown'
    )
    await update.message.reply_text(
        "Got it! I'll monitor that player's lay odds and notify you if it dips below your threshold."
    )
    # Immediately check this threshold now
    breached = await check_single_threshold(chat, surname, price, send_threshold_alert)
    if breached:
        # If it was already breached, remove it
        thresholds[chat] = [thr for thr in thresholds[chat] if thr['surname'].lower() != surname.lower()]
        save_thresholds()

# Handler: show all thresholds with /thresholds
async def list_thresholds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    user_th = thresholds.get(chat, [])
    if not user_th:
        await update.message.reply_text("You have no thresholds set.")
        return
    lines = [f"*{thr['surname']}* < {thr['threshold']}" for thr in user_th]
    text = "Your thresholds:\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode='Markdown')

# Handler: /remove Surname
async def remove_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /remove <surname>")
        return
    surname = args[0]
    user_th = thresholds.get(chat, [])
    new_list = [thr for thr in user_th if thr['surname'].lower() != surname.lower()]
    if len(new_list) == len(user_th):
        await update.message.reply_text(f"No threshold found for {surname}.")
    else:
        thresholds[chat] = new_list
        save_thresholds()
        await update.message.reply_text(f"Removed threshold for {surname}.")

# Handler: clear all thresholds with 'remove all'
async def remove_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    thresholds.pop(chat, None)
    save_thresholds()
    await update.message.reply_text("All thresholds have been removed.")

# Handler: plain text "Surname Price"
async def text_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 2:
        return
    surname, price_str = parts
    try:
        price = float(price_str)
    except ValueError:
        return
    thresholds.setdefault(chat, []).append({'surname': surname, 'threshold': price})
    save_thresholds()
    await update.message.reply_text(
        f"Threshold set: *{surname}* < {price}", parse_mode='Markdown'
    )
    await update.message.reply_text(
        "Got it! I'll monitor that player's lay odds and notify you if it dips below your threshold."
    )
    # Immediately check this threshold now
    breached = await check_single_threshold(chat, surname, price, send_threshold_alert)
    if breached:
        thresholds[chat] = [thr for thr in thresholds[chat] if thr['surname'].lower() != surname.lower()]
        save_thresholds()

import threading

def threshold_watcher():
    while True:
        try:
            top7 = get_top7_markets()
        except requests.exceptions.HTTPError as e:
            logger.error(f"Error fetching markets in watcher: {e}")
            time.sleep(10)
            continue

        for chat, user_th in list(thresholds.items()):
            for thr in list(user_th):
                surname = thr['surname'].lower()
                thr_price = thr['threshold']
                for mkt, _ in top7:
                    for o in mkt['bookmakers'][0]['markets'][0]['outcomes']:
                        if o['name'].lower().split()[-1] == surname and o['price'] <= thr_price:
                            try:
                                app.bot.send_message(
                                    chat_id=chat,
                                    text=f"⚠️ *{thr['surname']}* odds dropped to {o['price']} (≤ {thr_price})",
                                    parse_mode='Markdown'
                                )
                            except Exception as e:
                                logger.error(f"Threshold alert error: {e}")
                            thresholds[chat].remove(thr)
                            save_thresholds()
                            break
        time.sleep(10)

# Main entry
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # support both /t10t shortcut and the original /top10tennis command
    app.add_handler(CommandHandler(['t10t', 'top10tennis'], handle_top))
    app.add_handler(CommandHandler('setthreshold', setthreshold))
    app.add_handler(CommandHandler('thresholds', list_thresholds))
    app.add_handler(CommandHandler('remove', remove_threshold))
    # plain text handlers
    app.add_handler(MessageHandler(filters.Regex(r'^[A-Za-z]+ \d+(?:\.\d+)?$'), text_threshold))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^remove all$'), remove_all))
    load_thresholds()
    # Start background watcher thread for thresholds
    threading.Thread(target=threshold_watcher, daemon=True).start()
    logger.info("Bot started and polling...")
    app.run_polling()