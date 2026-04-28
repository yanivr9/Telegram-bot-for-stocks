import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
import telebot
import os
import threading
from flask import Flask

# --- הגדרות אישיות ---
BOT_TOKEN = "8440114036:AAGCcg7BFOZ6tQNUq2u4mZ6K8vJ_qYF-6l0"
ALLOWED_CHAT_IDS = ["7353631352", "6054708220", "123456789"] # הוסף פה את ה-ID של החבר במקום 123456789

# --- הגדרות שרת (למניעת שינה) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- הגדרות אלגוריתם ---
TOLERANCE_PCT = 0.015  
BREAKOUT_PROXIMITY_PCT = 0.025 

bot = telebot.TeleBot(BOT_TOKEN)

# --- (כל הפונקציות שלך: calculate_atr, get_levels_with_hits, analyze_ticker_text נשארות בדיוק אותו דבר!) ---
# אני שם כאן רק את המבנה כדי לקצר, תדביק את הפונקציות המלאות מהקוד הקודם:

def calculate_atr(df, period=14):
    # ... התוכן מהקוד הקודם ...
    pass # (למחוק את ה-pass ולהדביק את הקוד)

def get_levels_with_hits(df):
    # ... התוכן מהקוד הקודם ...
    pass

def analyze_ticker_text(ticker):
    # ... התוכן מהקוד הקודם ...
    pass

# --- האזנה להודעות נכנסות ---
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if str(message.chat.id) not in ALLOWED_CHAT_IDS:
        bot.reply_to(message, "⛔ אין לך הרשאה להשתמש בבוט זה.")
        print(f"Access denied for user: {message.chat.id}", flush=True)
        return

    text = message.text.strip().upper()
    if text.startswith('/CHECK '):
        ticker = text.replace('/CHECK ', '').strip()
    elif text.startswith('/'):
        if text == '/START':
            bot.reply_to(message, "ברוך הבא! פשוט שלח לי סימול מניה (למשל AAPL) ואחזיר לך ניתוח מלא.")
        return
    else:
        ticker = text

    if not ticker:
        return

    print(f"Scanning ticker: {ticker}... (Requested by {message.chat.id})", flush=True)
    bot.reply_to(message, f"🔍 סורק את `{ticker}`, אנא המתן...")
    
    try:
        report = analyze_ticker_text(ticker)
        bot.reply_to(message, report, parse_mode='Markdown')
        print(f"Report for {ticker} sent successfully.", flush=True)
    except Exception as e:
        bot.reply_to(message, f"❌ אירעה שגיאה: {e}")
        print(f"Error scanning {ticker}: {e}", flush=True)

if __name__ == "__main__":
    # 1. הפעלת שרת ה-Web ברקע (ערוץ נפרד)
    server_thread = threading.Thread(target=run_server)
    server_thread.start()

    # 2. שליחת הודעת התעוררות
    startup_msg = "🤖 *סורק המניות חזר לאוויר משרת הענן!*\nפשוט שלח לי סימול מניה."
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            bot.send_message(chat_id, startup_msg, parse_mode='Markdown')
        except:
            pass

    print("Bot is running and listening...", flush=True)
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
