import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
import telebot
import os
import json
import time
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask

# --- הגדרות אישיות ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
allowed_chats_env = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = [chat_id.strip() for chat_id in allowed_chats_env.split(",") if chat_id.strip()]

# --- הגדרות הסורק האוטומטי ---
# רשימת מעקב התחלתית מגיעה ממשתנה סביבה, ואפשר לנהל אותה עם /watch /unwatch /list
WATCHLIST_ENV = os.environ.get("WATCHLIST", "")
WATCHLIST_FILE = "watchlist.json"
SCAN_INTERVAL_MIN = int(os.environ.get("SCAN_INTERVAL_MIN", "15"))     # כל כמה דקות לסרוק
ALERT_COOLDOWN_HOURS = float(os.environ.get("ALERT_COOLDOWN_HOURS", "12"))  # לא לשלוח שוב על אותה מניה
SCAN_ALWAYS = os.environ.get("SCAN_ALWAYS", "0") == "1"                # 1 = לסרוק גם כשהשוק סגור

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

TOLERANCE_PCT = 0.015  
BREAKOUT_PROXIMITY_PCT = 0.025 

bot = telebot.TeleBot(BOT_TOKEN)

# --- ניהול רשימת מעקב ---
watchlist_lock = threading.Lock()

def load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r") as f:
            saved = json.load(f)
            if isinstance(saved, list) and saved:
                return [t.upper() for t in saved]
    except Exception:
        pass
    return [t.strip().upper() for t in WATCHLIST_ENV.split(",") if t.strip()]

def save_watchlist(tickers):
    # שמירה לקובץ היא רק גיבוי — בשרתים עם דיסק זמני היא עלולה להימחק באתחול,
    # ואז הרשימה תיטען מחדש ממשתנה הסביבה WATCHLIST
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(tickers, f)
    except Exception:
        pass

WATCHLIST = load_watchlist()

# זיכרון התראות: לא שולחים שוב על אותה מניה בתוך חלון הצינון
last_alert_time = {}

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
                levels_data.append({'price': round(float(level), 2), 'hits': final_hits_count})
        return levels_data
    except Exception as e:
        return []

def make_yahoo_session():
    # התחפושת המושלמת של דפדפן כרום אמיתי
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,he;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    })
    return session

def fetch_history(ticker, session):
    # ניסיון ראשון
    stock = yf.Ticker(ticker, session=session)
    df = stock.history(period="2y")

    # תוכנית גיבוי: אם יאהו חסמו את השיטה הרגילה (0 שורות), נשתמש במנגנון ההורדה הישיר
    if df.empty or len(df) < 100:
        df = yf.download(ticker, period="2y", progress=False)
        # סידור הנתונים למקרה שההורדה מחזירה מבנה כפול
        if not df.empty and isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

    return stock, df

def analyze_ticker_text(ticker):
    try:
        ticker = ticker.upper()
        session = make_yahoo_session()
        stock, df = fetch_history(ticker, session)

        if df.empty or len(df) < 100:
            return f"❌ יאהו חוסם את הבקשה כרגע או שאין מספיק נתונים. (התקבלו {len(df)} שורות).\nנסה שוב בעוד כמה דקות."

        curr_price = float(df['Close'].iloc[-1])
        prev_price = float(df['Close'].iloc[-2])
        curr_vol = float(df['Volume'].iloc[-1])
        avg_vol_20 = float(df['Volume'].iloc[-21:-1].mean())
        
        extended_price = None
        extended_label = ""
        is_market_open = False
        
        try:
            info = stock.info
            market_state = info.get('marketState', '').upper()
            live_price = info.get('currentPrice')
            if live_price:
                curr_price = float(live_price)

            if market_state == 'REGULAR':
                is_market_open = True
            else:
                pre_market = info.get('preMarketPrice')
                post_market = info.get('postMarketPrice')
                if pre_market:
                    extended_price = float(pre_market)
                    extended_label = "טרום מסחר"
                elif post_market:
                    extended_price = float(post_market)
                    extended_label = "אחרי מסחר"
        except Exception:
            pass

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

        report = f"📊 *דוח מניית {ticker}*\n\n"
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

# --- הסורק האוטומטי: פריצות בזמן אמת ---

def is_us_market_open_now():
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:  # שבת/ראשון
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)

def check_breakout(ticker, session):
    """מחזיר הודעת התראה אם המניה חצתה היום רמת התנגדות, אחרת None"""
    stock, df = fetch_history(ticker, session)
    if df.empty or len(df) < 100:
        return None

    curr_price = float(df['Close'].iloc[-1])
    prev_close = float(df['Close'].iloc[-2])
    curr_vol = float(df['Volume'].iloc[-1])
    avg_vol_20 = float(df['Volume'].iloc[-21:-1].mean())

    try:
        live_price = stock.info.get('currentPrice')
        if live_price:
            curr_price = float(live_price)
    except Exception:
        pass

    # רמות מחושבות מההיסטוריה; פריצה = המחיר חצה היום רמה שאתמול היה מתחתיה
    all_levels = get_levels_with_hits(df)
    crossed = [l for l in all_levels if prev_close <= l['price'] < curr_price]
    if not crossed:
        return None

    broken = max(crossed, key=lambda x: x['price'])
    vol_ratio = (curr_vol / avg_vol_20) * 100 if avg_vol_20 else 0
    vol_indicator = "🔥 ווליום חריג" if vol_ratio > 150 else "📊 ווליום רגיל"
    change_pct = ((curr_price / prev_close) - 1) * 100

    msg = f"🚨 *התראת פריצה: {ticker}*\n\n"
    msg += f"🟢 המניה פרצה התנגדות של `${broken['price']:,.2f}` ({broken['hits']} נגיעות)\n"
    msg += f"מחיר נוכחי: `${curr_price:,.2f}` | שינוי יומי: `{change_pct:+.2f}%`\n"
    msg += f"פעילות: `{vol_ratio:.0f}%` | {vol_indicator}\n\n"
    msg += f"לניתוח מלא שלחו: `{ticker}`"
    return msg

def broadcast(text):
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            bot.send_message(chat_id, text, parse_mode='Markdown')
        except Exception:
            pass

def scan_watchlist(report_chat_id=None):
    """סריקה אחת של כל רשימת המעקב. report_chat_id = מי שביקש /scan ידני"""
    with watchlist_lock:
        tickers = list(WATCHLIST)

    if not tickers:
        if report_chat_id:
            bot.send_message(report_chat_id, "רשימת המעקב ריקה. הוסיפו מניה עם `/watch AAPL`", parse_mode='Markdown')
        return

    session = make_yahoo_session()
    alerts_sent = 0
    now = time.time()

    for ticker in tickers:
        try:
            # צינון: לא שולחים שוב על אותה מניה בתוך החלון שהוגדר
            last = last_alert_time.get(ticker, 0)
            if now - last < ALERT_COOLDOWN_HOURS * 3600:
                continue

            alert = check_breakout(ticker, session)
            if alert:
                broadcast(alert)
                last_alert_time[ticker] = now
                alerts_sent += 1

            time.sleep(2)  # לא להפציץ את יאהו
        except Exception:
            continue

    if report_chat_id:
        bot.send_message(report_chat_id,
                         f"✅ נסרקו {len(tickers)} מניות, נשלחו {alerts_sent} התראות.",
                         parse_mode='Markdown')

def scanner_loop():
    while True:
        try:
            if SCAN_ALWAYS or is_us_market_open_now():
                scan_watchlist()
        except Exception:
            pass
        time.sleep(SCAN_INTERVAL_MIN * 60)

# --- טיפול בהודעות ---

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if str(message.chat.id) not in ALLOWED_CHAT_IDS:
        bot.reply_to(message, "⛔ אין לך הרשאה להשתמש בבוט זה.")
        return

    text = message.text.strip().upper()

    if text.startswith('/'):
        if text == '/START':
            bot.reply_to(message,
                "ברוך הבא! פשוט שלח לי סימול מניה (למשל AAPL) ואחזיר לך ניתוח מלא.\n\n"
                "*הסורק האוטומטי:*\n"
                "`/watch AAPL` — הוספה לרשימת המעקב\n"
                "`/unwatch AAPL` — הסרה מהרשימה\n"
                "`/list` — הצגת רשימת המעקב\n"
                "`/scan` — סריקה ידנית עכשיו\n\n"
                f"אני סורק אוטומטית כל {SCAN_INTERVAL_MIN} דקות בשעות המסחר, "
                "ושולח התראה לכולם כשמניה פורצת התנגדות. 🚨",
                parse_mode='Markdown')
            return

        if text.startswith('/WATCH '):
            ticker = text.replace('/WATCH ', '').strip()
            if ticker:
                with watchlist_lock:
                    if ticker not in WATCHLIST:
                        WATCHLIST.append(ticker)
                        save_watchlist(WATCHLIST)
                bot.reply_to(message, f"✅ `{ticker}` נוספה לרשימת המעקב ({len(WATCHLIST)} מניות).", parse_mode='Markdown')
            return

        if text.startswith('/UNWATCH '):
            ticker = text.replace('/UNWATCH ', '').strip()
            with watchlist_lock:
                if ticker in WATCHLIST:
                    WATCHLIST.remove(ticker)
                    save_watchlist(WATCHLIST)
                    bot.reply_to(message, f"🗑️ `{ticker}` הוסרה מרשימת המעקב.", parse_mode='Markdown')
                else:
                    bot.reply_to(message, f"`{ticker}` לא נמצאת ברשימה.", parse_mode='Markdown')
            return

        if text == '/LIST':
            with watchlist_lock:
                if WATCHLIST:
                    bot.reply_to(message, "👁️ *רשימת המעקב:*\n" + ", ".join(f"`{t}`" for t in WATCHLIST), parse_mode='Markdown')
                else:
                    bot.reply_to(message, "רשימת המעקב ריקה. הוסיפו מניה עם `/watch AAPL`", parse_mode='Markdown')
            return

        if text == '/SCAN':
            bot.reply_to(message, "🔍 סורק את רשימת המעקב, אנא המתן...")
            threading.Thread(target=scan_watchlist, kwargs={'report_chat_id': message.chat.id}).start()
            return

        if text.startswith('/CHECK '):
            ticker = text.replace('/CHECK ', '').strip()
        else:
            return
    else:
        ticker = text

    if not ticker: return

    bot.reply_to(message, f"🔍 סורק את `{ticker}`, אנא המתן...")
    
    try:
        report = analyze_ticker_text(ticker)
        if report:
            bot.reply_to(message, report, parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ שגיאה: לא התקבל דוח מהפונקציה.")
    except Exception as e:
        bot.reply_to(message, f"❌ אירעה שגיאה בשליחה: {e}")

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server)
    server_thread.start()

    scanner_thread = threading.Thread(target=scanner_loop, daemon=True)
    scanner_thread.start()

    with watchlist_lock:
        watch_count = len(WATCHLIST)
    startup_msg = ("🤖 *סורק המניות חזר לאוויר משרת הענן!*\n"
                   f"עוקב אחרי {watch_count} מניות וסורק כל {SCAN_INTERVAL_MIN} דקות בשעות המסחר. 🚨\n"
                   "שלחו סימול מניה לניתוח, או `/start` לרשימת הפקודות.")
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            bot.send_message(chat_id, startup_msg, parse_mode='Markdown')
        except:
            pass

    bot.infinity_polling(timeout=10, long_polling_timeout=5)
