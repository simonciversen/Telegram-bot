#!/usr/bin/env python3
"""bot.py - Telegram bot to fetch top 7 tennis matches (next 3 days),
set thresholds by player surname, list/remove thresholds, and notify on drops.

Setup:
1. Python 3.8+
2. pip3 install python-telegram-bot requests
3. python3 bot.py
"""
# Helper: Get matched volume
def get_matched_volume(mkt: dict) -> float:
    # Use the top-level "total_matched" or "totalMatched" field
    return mkt.get('total_matched', mkt.get('totalMatched', 0))

# Helper: Get play count (number of distinct betting markets for the chosen popular bookmaker, e.g., Bet365)
def get_play_count(mkt: dict) -> int:
    """
    Returns the number of distinct betting markets for the chosen popular bookmaker (Bet365),
    or falls back to the bookmaker with the most markets.
    """
    bookmakers = mkt.get('bookmakers', [])
    # Try to find the popular bookmaker “bet365”
    for bk in bookmakers:
        if 'bet365' in bk.get('key', '').lower():
            return len(bk.get('markets', []))
    # Fallback: choose the bookmaker with the most markets (proxy for bet variety)
    if not bookmakers:
        return 0
    best = max(bookmakers, key=lambda b: len(b.get('markets', [])))
    return len(best.get('markets', []))
import sys
import logging
import requests
from requests.exceptions import RequestException
import re
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackContext
from telegram import Bot as SyncBot
import json
from pathlib import Path
import subprocess
import time
import asyncio

# Fixed GMT+2 timezone (no DST)
GMT_PLUS_2 = timezone(timedelta(hours=2))

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
ODDS_API_KEY   = '260567c3535bb5e28f0243d42a7396f6'
SPORT_KEY      = 'tennis'

# synchronous bot used inside threads for alerts
sync_bot = SyncBot(token=TELEGRAM_TOKEN)

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
    """
    Pulls all tennis-related markets by first getting every sport_key
    starting with 'tennis' (so ATP, WTA, Slams, etc.), then fetching
    odds for each and flattening the results.
    """
    # 1) get all sport keys
    url_sports = 'https://api.the-odds-api.com/v4/sports'
    params = {'apiKey': ODDS_API_KEY}
    r = requests.get(url_sports, params=params, timeout=10)
    r.raise_for_status()
    all_sports = r.json()  # list of { key, title, ... }

    # 2) pick only those whose key starts with "tennis"
    tennis_keys = [s['key'] for s in all_sports if s['key'].lower().startswith('tennis')]
    if not tennis_keys:
        logger.warning("No tennis sport keys found in sports list.")
        return []

    # 3) for each tennis key, fetch its markets
    all_markets = []
    for sk in tennis_keys:
        url_odds = f'https://api.the-odds-api.com/v4/sports/{sk}/odds/'
        try:
            r2 = requests.get(url_odds, params={
                'regions': 'uk,us,eu,au',
                'markets': 'h2h',
                'apiKey': ODDS_API_KEY
            }, timeout=10)
            if r2.status_code == 401:
                logger.error(f"Unauthorized for sport key {sk}.")
                continue
            if r2.status_code == 422:
                logger.error(f"Unprocessable for sport key {sk}.")
                continue
            r2.raise_for_status()
            data = r2.json() or []
            logger.info(f"Fetched {len(data)} markets for {sk}")
            all_markets.extend(data)
        except RequestException as e:
            logger.error(f"Error fetching odds for {sk}: {e}")
            continue

    if not all_markets:
        logger.warning("No tennis markets returned across all tennis keys.")
    return all_markets

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
        key=lambda x: (-get_play_count(x[0]), x[1])
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

    # fetch markets, but catch authorization or other HTTP errors gracefully
    try:
        top7 = get_top7_markets()
    except RequestException as e:
        await update.message.reply_text(f"⚠️ Could not retrieve matches: {e}")
        return

    # if no markets returned, let the user know
    if not top7:
        await update.message.reply_text("⚠️ No upcoming matches found (or you're not authorized to view them).")
        return

    # List matches
    for idx, (mkt, dt_utc) in enumerate(top7, start=1):
        home_full = mkt.get('home_team', 'Unknown')
        away_full = mkt.get('away_team', 'Unknown')
        home = format_name(home_full)
        away = format_name(away_full)
        dt_local = dt_utc.astimezone(GMT_PLUS_2)
        # Mark live matches (commenced in the past)
        now_utc = datetime.now(timezone.utc)
        live_flag = " 🔴 LIVE" if dt_utc <= now_utc else ""

        # Display “Today” or “Tomorrow” for very near dates
        today_local = datetime.now(dt_local.tzinfo).date()
        match_date = dt_local.date()
        if match_date == today_local:
            day_str = "Today"
        elif match_date == today_local + timedelta(days=1):
            day_str = "Tomorrow"
        else:
            day_str = dt_local.strftime('%A')
        time_str = f"{day_str}, {dt_local.strftime('%H:%M')}"

        outcomes = mkt['bookmakers'][0]['markets'][0]['outcomes']
        home_price = next((o['price'] for o in outcomes if o['name'] == home_full), 'N/A')
        away_price = next((o['price'] for o in outcomes if o['name'] == away_full), 'N/A')

        # Find exchange odds (lay) from any bookmaker whose key contains "exchange"
        ex_bk = next((b for b in mkt.get('bookmakers', []) if 'exchange' in b.get('key', '').lower()), None)
        if ex_bk and ex_bk.get('markets'):
            ex_outcomes = ex_bk['markets'][0]['outcomes']
            home_lay = next((o['price'] for o in ex_outcomes if o['name'] == home_full), 'N/A')
            away_lay = next((o['price'] for o in ex_outcomes if o['name'] == away_full), 'N/A')
        else:
            home_lay = away_lay = 'N/A'

        # Count play count for this market
        play_count = get_play_count(mkt)

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

async def list_thresholds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    user_th = thresholds.get(chat, [])
    if not user_th:
        await update.message.reply_text("You have no thresholds set.")
        return

    # Aggregate thresholds per player surname (case-insensitive)
    agg: dict[str, list[float]] = {}
    for thr in user_th:
        surname_lc = thr['surname'].lower()
        agg.setdefault(surname_lc, []).append(thr['threshold'])

    # Build a single line per player, capitalizing surname and showing all thresholds
    lines = []
    for surname_lc, prices in agg.items():
        surname_cap = surname_lc.capitalize()
        unique_prices = sorted(set(prices))
        # Join multiple thresholds with " & " and display just the numbers
        price_str = ' & '.join(str(p) for p in unique_prices)
        lines.append(f"*{surname_cap}* {price_str}")

    text = "Your thresholds:\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode='Markdown')

# Handler: /remove Surname
async def remove_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat.id
    # Determine surname either from slash-command args or plain-text "remove X"
    text = update.message.text.strip()
    m = re.match(r'(?i)^remove\s+([A-Za-z]+)$', text)
    if m:
        surname = m.group(1)
    else:
        args = context.args
        if len(args) != 1:
            await update.message.reply_text("Usage: /remove <surname> or /removeall to clear all thresholds")
            return
        surname = args[0]

    # support "/remove all" to clear everything
    if surname.lower() == 'all':
        return await remove_all(update, context)

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
            try:
                top7 = get_top7_markets()
            except RequestException as e:
                logger.error(f"Error fetching markets in watcher: {e}")
                # alert each chat of the problem
                for chat in thresholds:
                    try:
                        sync_bot.send_message(
                            chat_id=chat,
                            text=f"⚠️ Error retrieving markets: {e}",
                            parse_mode='Markdown'
                        )
                    except Exception as send_exc:
                        logger.error(f"Failed to send HTTPError alert to chat {chat}: {send_exc}")
                time.sleep(60)
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
        except Exception as e:
            logger.error(f"Threshold watcher encountered error: {e}", exc_info=e)
        time.sleep(10)

# Main entry
if __name__ == '__main__':
    # CLI test mode: print top 7 matches and exit
    if len(sys.argv) > 1 and sys.argv[1] == '--print':
        top7 = get_top7_markets()
        print("Top 7 Tennis Matches (Next 3 Days):")
        for idx, (mkt, dt_utc) in enumerate(top7, start=1):
            outcomes = mkt['bookmakers'][0]['markets'][0]['outcomes']
            home = format_name(mkt.get('home_team', 'Unknown'))
            away = format_name(mkt.get('away_team', 'Unknown'))
            dt_local = dt_utc.astimezone(GMT_PLUS_2)
            time_str = dt_local.strftime('%H:%M')
            print(f"{idx}. {home} vs {away} — {time_str}")
            print(f"   • {home}: {next((o['price'] for o in outcomes if o['name']==mkt.get('home_team', 'Unknown')), 'N/A')}")
            print(f"   • {away}: {next((o['price'] for o in outcomes if o['name']==mkt.get('away_team', 'Unknown')), 'N/A')}")
        sys.exit(0)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # Register /t10t and /top10tennis commands separately
    app.add_handler(CommandHandler('t10t', handle_top))
    app.add_handler(CommandHandler('top10tennis', handle_top))
    app.add_handler(CommandHandler('setthreshold', setthreshold))
    app.add_handler(CommandHandler('thresholds', list_thresholds))
    app.add_handler(CommandHandler('remove', remove_threshold))
    app.add_handler(CommandHandler('removeall', remove_all))
    # plain text handler to remove a single player threshold without slash
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^remove\s+[A-Za-z]+$'), remove_threshold))
    # plain text handlers
    app.add_handler(MessageHandler(filters.Regex(r'^[A-Za-z]+ \d+(?:\.\d+)?$'), text_threshold))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^remove all$'), remove_all))
    load_thresholds()
    # Start background watcher thread for thresholds
    threading.Thread(target=threshold_watcher, daemon=True).start()
    # Global error handler to catch uncaught exceptions in handlers
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"Exception while handling update {update}: {context.error}", exc_info=context.error)

    app.add_error_handler(error_handler)
    logger.info("Bot started and polling...")
    app.run_polling()

