"""
סורק פריצות אוטומטי — רץ מ-GitHub Actions (ראו .github/workflows/breakout.yml)
קורא את רשימת המניות מ-watchlist.txt, בודק פריצת התנגדות,
ושולח התראה בטלגרם לכל הצ'אטים המורשים.
"""
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_IDS = [c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()]
TEST_MODE = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

TOLERANCE_PCT = 0.015
WATCHLIST_FILE = "watchlist.txt"
STATE_FILE = "alerted.json"   # זיכרון בין ריצות: התראה אחת לכל מניה ביום


def send_telegram(text):
    ok = True
    for chat_id in ALLOWED_CHAT_IDS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=30,
            )
            if not r.ok:
                print(f"Telegram error for chat {chat_id}: {r.text}")
                ok = False
        except Exception as e:
            print(f"Telegram send failed for chat {chat_id}: {e}")
            ok = False
    return ok


def load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return [line.strip().upper() for line in f
                    if line.strip() and not line.strip().startswith("#")]
    except FileNotFoundError:
        return []


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)

def get_levels_with_hits(df):
    try:
        if len(df) < 60:
            return []
        highs_series = df['High']
        lows_series = df['Low']
        maxima_idx = argrelextrema(highs_series.values, np.greater, order=20)[0]
        minima_idx = argrelextrema(lows_series.values, np.less, order=20)[0]
        raw_levels = np.sort(np.concatenate((highs_series.iloc[maxima_idx].values,
                                             lows_series.iloc[minima_idx].values)))

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
                if indices.empty:
                    return 0
                count = 1
                for i in range(1, len(indices)):
                    if (indices[i] - indices[i - 1]).days > 3:
                        count += 1
                return count

            final_hits_count = count_isolated_hits(hits_indices)
            if final_hits_count >= 2:
                levels_data.append({'price': round(float(level), 2), 'hits': final_hits_count})
        return levels_data
    except Exception:
        return []


def check_breakout(ticker):
    """מחזיר הודעת התראה אם המניה חצתה היום רמת התנגדות, אחרת None"""
    df = yf.download(ticker, period="2y", progress=False)
    if not df.empty and isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty or len(df) < 100:
        print(f"{ticker}: not enough data ({len(df)} rows)")
        return None

    curr_price = float(df['Close'].iloc[-1])
    prev_close = float(df['Close'].iloc[-2])
    curr_vol = float(df['Volume'].iloc[-1])
    avg_vol_20 = float(df['Volume'].iloc[-21:-1].mean())

    # פריצה = המחיר חצה היום רמת התנגדות שאתמול היה מתחתיה
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
    msg += f"פעילות: `{vol_ratio:.0f}%` | {vol_indicator}"
    return msg


def main():
    if not BOT_TOKEN or not ALLOWED_CHAT_IDS:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_IDS secrets")

    if TEST_MODE:
        now_il = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y %H:%M")
        send_telegram("🧪 *הודעת בדיקה מסורק הפריצות!*\n"
                      f"הסורק רץ בהצלחה מ-GitHub Actions ({now_il} שעון ישראל).\n"
                      "מעכשיו תקבלו כאן התראות אוטומטיות על פריצות. 🚨")
        print("Test message sent.")
        return

    watchlist = load_watchlist()
    if not watchlist:
        print("watchlist.txt is empty — nothing to scan")
        return

    state = load_state()
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    alerts = 0

    for ticker in watchlist:
        try:
            # התראה אחת לכל מניה ביום
            if state.get(ticker) == today:
                print(f"{ticker}: already alerted today")
                continue

            alert = check_breakout(ticker)
            if alert:
                if send_telegram(alert):
                    state[ticker] = today
                    alerts += 1
                    print(f"{ticker}: BREAKOUT alert sent")
            else:
                print(f"{ticker}: no breakout")
        except Exception as e:
            print(f"{ticker}: error {e}")

    save_state(state)
    print(f"Done. Scanned {len(watchlist)} tickers, sent {alerts} alerts.")


if __name__ == "__main__":
    main()
