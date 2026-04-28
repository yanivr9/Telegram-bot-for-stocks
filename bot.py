import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
import telebot
import os
import threading
import requests
from flask import Flask

# --- הגדרות אישיות (נמשכות כמשתני סביבה לאבטחה) ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
allowed_chats_env = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [chat_id.strip() for chat_id in allowed_chats_env.split(",") if chat_id.strip()]

if not BOT_TOKEN:
    print("⚠️ חסר משתנה סביבה: TELEGRAM_BOT_TOKEN")

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

# --- פונקציות ליבה ---
def calculate_atr(df, period=14):
    try:
        high_low = df['High'] - df['Low']
        high_close = np.abs(df['High'] - df['Close'].shift())
        low_close = np.abs(df['Low'] - df['Close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        atr = true_range.rolling(window=period).mean()
        return atr.iloc[-1]
    except Exception as e:
        return None

def get_levels_with_hits(df):
    try:
        if len(df) < 60: return []
        
        highs_series = df['High']
        lows_series = df['Low']
        maxima_idx = argrelextrema(highs_series.values, np.greater, order=20)[0]
        minima_idx = argrelextrema(lows_series.values, np.less, order=20)[0]
        
        raw_levels = np.sort(np.concatenate((highs_series.iloc[maxima_idx].values, lows_series.iloc[minima_idx].values)))
        
        cleaned_levels = []
        if len(raw_levels) > 0:
            cleaned_levels.append(raw_levels[0])
            for i in range(1, len(raw_levels)):
                if raw_levels[i] > cleaned_levels[-1] * 1.02:
                    cleaned_levels.append(raw_levels[i])
        
        levels_data = []
        for level in cleaned_levels:
            lower_bound = level * (1 - TOLERANCE_PCT)
            upper_bound = level * (1 + TOLERANCE_PCT)
            mask = (df['High'] >= lower_bound) & (df['Low'] <= upper_bound)
            hits_indices = df[mask].index
            
            def count_isolated_hits(indices):
                if indices.empty: return 0
                count = 1
                for i in range(1, len(indices)):
                    if (indices[i] - indices[i-1]).days > 3:
                        count += 1
                return count
            
            final_hits_count = count_isolated_hits(hits_indices)
            if final_hits_count >= 2:
                levels_data.append({
                    'price': round(float(level), 2),
                    'hits': final_hits_count
                })
        return levels_data
    except Exception as e:
        return []

def analyze_ticker_text(ticker):
    try:
        # יצירת "תחפושת" לדפדפן כדי ש-Yahoo לא יחסום את שרת הענן
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        })
        
        stock = yf.Ticker(ticker.upper(), session=session)
        df = stock.history(period="2y")
        
        if len(df) < 100:
            return f"❌ אין מספיק נתונים היסטוריים לניתוח מניה זו.\n(התקבלו {len(df)} שורות מ-Yahoo)"

        curr_price = df['Close'].iloc[-1]
        prev_price = df['Close'].iloc[-2]
        curr_vol = df['Volume'].iloc[-1]
        avg_vol_20 = df['Volume'].iloc[-21:-1].mean()
        
        # --- בדיקת סטטוס שוק ומחירים מחוץ לשעות ---
        extended_price = None
        extended_label = ""
        is_market_open = False
        
        try:
            info = stock.info
            market_state = info.get('marketState', '').upper()
            
            live_price = info.get('currentPrice')
            if live_price:
                curr_price = live_price

            if market_state == 'REGULAR':
                is_market_open = True
            else:
                pre_market = info.get('preMarketPrice')
                post_market = info.get('postMarketPrice')
                
                if pre_market:
                    extended_price = pre_market
                    extended_label = "טרום מסחר"
                elif post_market:
                    extended_price = post_market
                    extended_label = "אחרי מסחר"
        except Exception:
            pass
        # ------------------------------------------------

        all_levels = get_levels_with_hits(df)
        resistances = sorted([l for l in all_levels if l['price'] > curr_price], key=lambda x: x['price'])
        supports = sorted([l for l in all_levels if l['price'] < curr_price], key=lambda x: x['price'], reverse=True)
        
        r1 = resistances[0] if resistances else None
        s1 = supports[0] if len(supports) >= 1 else None
        s2 = supports[1] if len(supports) >= 2 else None
        
        atr_val = calculate_atr(df)
        atr_pct = (atr_val / curr_price) * 100 if atr_val else 0

        breakout_status = "סטטוס רגיל"
        if r1:
            dist_to_res_pct = ((r1['price'] - curr_price) / curr_price) * 100
            if dist_to_res_pct <= 0:
                breakout_status = "🟢 פריצה בפועל"
            elif dist_to_res_pct <= (BREAKOUT_PROXIMITY_PCT * 100):
                breakout_status = f"🟡 קרובה לפריצה מרחק {dist_to_res_pct:.1f}%"
                
        vol_ratio = (curr_vol / avg_vol_20) * 100
        vol_indicator = "🔥 ווליום חריג" if vol_ratio > 150 else "📊 ווליום רגיל"

        # --- הרכבת ההודעה ---
        report = f"📊 *דוח מניית {ticker.upper()}*\n\n"
        
        report += "🔹 *נתונים כלליים*\n"
        
        if is_market_open:
            report += f"מחיר נוכחי (שוק פתוח): `${curr_price:,.2f}`\n"
        else:
            report += f"מחיר סגירה: `${curr_price:,.2f}`\n"
            if extended_price and extended_price != curr_price:
                ext_change = ((extended_price / curr_price) - 1) * 100
                report += f"מחוץ לשעות: `${extended_price:,.2f}` | `{ext_change:+.2f}%` ({extended_label})\n"
                
        report += f"שינוי יומי: `{((curr_price/prev_price)-1)*100:+.2f}%`\n"
        report += f"פעילות: `{vol_ratio:.0f}%` | {vol_indicator}\n"
        report += f"תנודתיות ATR: `${atr_val:,.2f}` | `{atr_pct:.1f}%`\n\n"
        
        report += "🎯 *התנגדות ופריצה*\n"
        if r1:
            report += f"התנגדות קרובה: `${r1['price']:,.2f}` | {r1['hits']} נגיעות\n"
        else:
            report += "התנגדות קרובה: לא זוהתה\n"
        report += f"מצב: {breakout_status}\n\n"
        
        report += "🛡️ *רמות תמיכה*\n"
        if s1:
            dist_to_s1 = ((curr_price - s1['price']) / curr_price) * 100
            report += f"תמיכה קרובה: `${s1['price']:,.2f}` | {s1['hits']} נגיעות | מרחק -{dist_to_s1:.1f}%\n"
        if s2:
            dist_to_s2 = ((curr_price - s2['price']) / curr_price) * 100
            report += f"תמיכה נוספת: `${s2['price']:,.2f}` | {s2['hits']} נגיעות | מרחק -{dist_to_s2:.1f}%\n"
            
        if not s1 and not s2:
            report += "לא זוהו רמות תמיכה ברורות."
            
        return report

    except Exception as e:
        return f"❌ שגיאה בניתוח המניה {ticker}: {e}"

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
        if report:
            bot.reply_to(message, report, parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ שגיאה: לא התקבל דוח מהפונקציה.")
        print(f"Report for {ticker} sent successfully.", flush=True)
    except Exception as e:
        bot.reply_to(message, f"❌ אירעה שגיאה בשליחה: {e}")
        print(f"Error scanning {ticker}: {e}", flush=True)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server)
    server_thread.start()

    startup_msg = "🤖 *סורק המניות חזר לאוויר משרת הענן!*\nפשוט שלח לי סימול מניה."
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            bot.send_message(chat_id, startup_msg, parse_mode='Markdown')
        except:
            pass

    print("Bot is running and listening...", flush=True)
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
