import os
import re
import csv
import json
import time
import copy
import html as htmllib
import datetime
import threading
import functools
import unicodedata
import concurrent.futures
import pytz
import telebot
import requests
import feedparser
import yfinance as yf
from flask import Flask

# =====================================================================
#  HERMES ALL-IN-ONE BOT
#  Chat AI | Live Market | Nifty 200 | Paper Trading | News | Weather
#  Cricket | Football | Backtest | Uptime | Cloud-save
# =====================================================================

# ================= 1. CONFIGURATION =================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("LLM_API_KEY")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")      # optional (data restart pe safe rahe)
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")  # optional
REMOTE_KEY = os.environ.get("STATE_KEY", "hermes_state_v1")
STATE_FILE = "hermes_state.json"

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN set nahi hai. Render Environment mein add karein.")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
IST = pytz.timezone("Asia/Kolkata")
START_TIME = datetime.datetime.now(IST)
ALERTS_ACTIVE = True
LOCK = threading.RLock()

# ---- Trading rules (paper) ----
MARKET_OPEN = datetime.time(9, 20)
LAST_ENTRY = datetime.time(14, 45)      # iske baad naya trade nahi
SQUARE_OFF = datetime.time(15, 15)
PER_TRADE_AMOUNT = 25000
MAX_POSITIONS = 3
MAX_TRADES_PER_DAY = 12
DAILY_MAX_LOSS = 2000.0                 # din ka max loss (Rs) - iske baad naye trade band
COOLDOWN_MIN = 45                       # ek stock band hone ke baad dobara entry ke liye wait
SLIPPAGE = 0.0005                       # 0.05% slippage har order pe
TARGET_PCT = 0.01
SL_PCT = 0.005

# ---- NSE holidays 2026 (weekday wale) ----
NSE_HOLIDAYS = {
    "2026-01-26": "Republic Day", "2026-03-03": "Holi", "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti", "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti", "2026-05-01": "Maharashtra Day", "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram", "2026-09-14": "Ganesh Chaturthi", "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra", "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti", "2026-12-25": "Christmas",
}

# ================= 2. FLASK SERVER + KEEP ALIVE =================
server = Flask(__name__)

@server.route("/")
def home():
    return "Hermes Engine is Running 24/7!"

@server.route("/health")
def health():
    return {"status": "healthy", "time": datetime.datetime.now(IST).strftime("%H:%M:%S")}

def run_web_server():
    server.run(host="0.0.0.0", port=PORT)

def keep_alive_loop():
    """Render free tier so na jaye isliye khud ko ping karta hai."""
    while True:
        time.sleep(600)
        if RENDER_URL:
            try:
                requests.get(RENDER_URL + "/health", timeout=15)
            except Exception:
                pass

# ================= 3. STATE (file + optional cloud) =================
DEFAULT_STATE = {
    "account": {"capital": 100000.0, "available_cash": 100000.0, "realized_pnl": 0.0,
                "charges_total": 0.0, "trades_count": 0, "wins": 0, "positions": {}},
    "watchlist": ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"],
    "trade_history": [],
    "price_alerts": [],
    "alerts_active": True,
    "city": "Delhi",
    "last_heartbeat": None,
    "jobs_done": {},
    "extra_holidays": [],
    "last_exit": {},
    "last_news": 0,
    "day_stats": {"day": "", "opened": 0, "pnl": 0.0, "halted": False},
}
state = copy.deepcopy(DEFAULT_STATE)
PERSIST_MODE = "Local file (Render restart pe reset ho sakta hai)"
_DIRTY = threading.Event()

def merge_defaults(loaded, default):
    for k, v in default.items():
        if k not in loaded:
            loaded[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(loaded[k], dict):
            merge_defaults(loaded[k], v)
    return loaded

def remote_cmd(cmd):
    r = requests.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                      json=cmd, timeout=10)
    return r.json()

def load_state():
    global state, ALERTS_ACTIVE, PERSIST_MODE
    loaded = None
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            res = remote_cmd(["GET", REMOTE_KEY])
            if res.get("result"):
                loaded = json.loads(res["result"])
            PERSIST_MODE = "Cloud (Upstash) ✅ restart pe data safe"
        except Exception as e:
            print(f"Cloud load error: {e}")
            PERSIST_MODE = "Cloud connect nahi hua ⚠️ (local file use ho rahi hai)"
    if loaded is None:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    loaded = json.load(f)
        except Exception as e:
            print(f"State load error: {e}")
    if isinstance(loaded, dict):
        state = merge_defaults(loaded, DEFAULT_STATE)
    ALERTS_ACTIVE = state.get("alerts_active", True)

def save_state():
    try:
        with LOCK:
            state["alerts_active"] = ALERTS_ACTIVE
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        _DIRTY.set()
    except Exception as e:
        print(f"State save error: {e}")

def remote_sync_loop():
    """Cloud mein data har 20 sec mein (agar badla ho) save karta hai."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return
    while True:
        time.sleep(20)
        if _DIRTY.is_set():
            _DIRTY.clear()
            try:
                with LOCK:
                    snap = json.dumps(state)
                remote_cmd(["SET", REMOTE_KEY, snap])
            except Exception as e:
                print(f"Cloud save error: {e}")
                _DIRTY.set()

load_state()

# Restart hone par pata chale bot kitni der band tha
DOWNTIME_NOTE = ""
try:
    _ph = state.get("last_heartbeat")
    if _ph:
        _gap = (datetime.datetime.now(IST) - datetime.datetime.fromisoformat(_ph)).total_seconds()
        if _gap > 240:
            DOWNTIME_NOTE = (f"⚠️ Bot pichli baar ~{int(_gap // 3600)} ghante "
                             f"{int(_gap % 3600 // 60)} minute band raha.")
except Exception:
    pass

def heartbeat_loop():
    while True:
        with LOCK:
            state["last_heartbeat"] = now_ist().isoformat()
        save_state()
        time.sleep(60)

# ================= 4. HELPERS =================
def now_ist():
    return datetime.datetime.now(IST)

def esc(x):
    return htmllib.escape(str(x), quote=False)

def norm(s):
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()

MARKET_CONFIRMED_DATE = None
AUTO_HOLIDAY_DATE = None

def holiday_name(d=None):
    d = d or now_ist().date()
    key = d.isoformat()
    if key in NSE_HOLIDAYS:
        return NSE_HOLIDAYS[key]
    with LOCK:
        if key in state.get("extra_holidays", []):
            return "Custom holiday"
    if AUTO_HOLIDAY_DATE == key:
        return "Market band (data nahi aaya)"
    return None

def is_trading_day(d=None):
    d = d or now_ist().date()
    return d.weekday() < 5 and holiday_name(d) is None

def is_market_hours():
    n = now_ist()
    return is_trading_day(n.date()) and MARKET_OPEN <= n.time() < SQUARE_OFF

def chunk_text(text, limit=3800):
    chunks, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    chunks = [c.rstrip("\n") for c in chunks if c.strip()]
    return chunks or [""]

def send(chat_id, text, html=False):
    """Lambe message ko tukdon mein bhejta hai. HTML fail ho to plain text bhejta hai."""
    for p in chunk_text(text):
        try:
            bot.send_message(chat_id, p, parse_mode="HTML" if html else None)
        except Exception as e:
            print(f"Send error: {e}")
            if html:
                try:
                    bot.send_message(chat_id, htmllib.unescape(re.sub(r"<[^>]+>", "", p)))
                except Exception as e2:
                    print(f"Plain send error: {e2}")

def notify(text, html=True):
    if CHAT_ID and ALERTS_ACTIVE:
        send(CHAT_ID, text, html)

def is_owner(message):
    if not CHAT_ID:
        return True
    return str(message.chat.id) == str(CHAT_ID)

def yf_symbol(sym):
    sym = sym.upper().strip()
    special = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN"}
    if sym in special:
        return special[sym]
    if sym.startswith("^") or "." in sym:
        return sym
    return sym + ".NS"

def fmt_uptime():
    secs = int((now_ist() - START_TIME).total_seconds())
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    txt = (f"{d} din " if d else "") + f"{h} ghante {m} minute"
    return txt, secs / 3600

# ================= 5. REAL MARKET DATA =================
_cache = {}

def get_market_data(symbol):
    """Real 5-min data: price, EMA9/21, VWAP, volume ratio. 60 sec cache."""
    symbol = symbol.upper()
    hit = _cache.get(symbol)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        df = yf.Ticker(yf_symbol(symbol)).history(period="5d", interval="5m")
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close"])
        if df.empty:
            return None
        if df.index.tz is not None:
            df.index = df.index.tz_convert(IST)
        close = df["Close"]
        ema9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = close.ewm(span=21, adjust=False).mean().iloc[-1]

        last_day = df.index[-1].date()
        today = df[df.index.date == last_day]
        prev = df[df.index.date < last_day]
        tp = (today["High"] + today["Low"] + today["Close"]) / 3
        vol_sum = today["Volume"].sum()
        vwap = (tp * today["Volume"]).sum() / vol_sum if vol_sum > 0 else today["Close"].mean()

        vols = df["Volume"].tail(21)
        avg_vol = vols.iloc[:-1].mean() if len(vols) > 1 else 0
        vol_ratio = (vols.iloc[-1] / avg_vol) if avg_vol and avg_vol > 0 else 1.0

        price = float(close.iloc[-1])
        prev_close = float(prev["Close"].iloc[-1]) if not prev.empty else float(today["Open"].iloc[0])
        age_min = (now_ist() - df.index[-1]).total_seconds() / 60

        data = {
            "symbol": symbol,
            "price": round(price, 2),
            "ema9": round(float(ema9), 2),
            "ema21": round(float(ema21), 2),
            "vwap": round(float(vwap), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "day_high": round(float(today["High"].max()), 2),
            "day_low": round(float(today["Low"].min()), 2),
            "change_pct": round((price - prev_close) / prev_close * 100, 2),
            "fresh": age_min < 20,
            "last_candle": df.index[-1].strftime("%d-%b %H:%M"),
            "last_date": last_day.isoformat(),
        }
        _cache[symbol] = (time.time(), data)
        return data
    except Exception as e:
        print(f"Data error {symbol}: {e}")
        return None

def get_signal(d):
    if d["ema9"] > d["ema21"] and d["price"] > d["vwap"] and d["vol_ratio"] > 1.25:
        return "BUY"
    if d["ema9"] < d["ema21"] and d["price"] < d["vwap"] and d["vol_ratio"] > 1.25:
        return "SELL"
    return "NEUTRAL"

# ================= 6. GROQ AI (normal chat + trading expert) =================
chat_memory = {}

def system_prompt():
    return (
        "You are Hermes, a friendly, smart all-purpose AI assistant on Telegram. "
        "Answer ANY question normally (general knowledge, coding, health, career, study, daily life, etc.) "
        "like a helpful friend. Reply in the same language the user writes in; default to simple Hinglish. "
        "You are also an expert in Indian stock markets and algorithmic trading, but only go technical "
        "when the user asks about markets. Keep answers clear and reasonably short. "
        "You do NOT have live data inside normal chat: for live prices, weather, news, or sports scores, "
        "tell the user to use the bot commands (/price, /weather, /news, /score) instead of guessing. "
        "For trading, never promise profits and remind that it is not financial advice when giving views. "
        f"Current date/time: {now_ist().strftime('%d %b %Y, %H:%M')} IST."
    )

def ask_groq(chat_id, user_prompt, remember=True):
    if not GROQ_API_KEY:
        return "⚠️ AI key (LLM_API_KEY) set nahi hai."
    hist = chat_memory.get(chat_id, [])[-10:] if remember else []
    messages = [{"role": "system", "content": system_prompt()}] + hist + [{"role": "user", "content": user_prompt}]
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.5},
            timeout=40,
        )
        data = r.json()
        if "choices" in data and data["choices"]:
            reply = data["choices"][0]["message"]["content"]
            if remember:
                h = chat_memory.setdefault(chat_id, [])
                h.append({"role": "user", "content": user_prompt})
                h.append({"role": "assistant", "content": reply})
                chat_memory[chat_id] = h[-20:]
            return reply
        return f"⚠️ AI Error: {data.get('error', data)}"
    except Exception as e:
        return f"⚠️ Request failed: {e}"

# ================= 7. SCREENER =================
def run_screener_task(label="DAILY SCAN", chat_id=None):
    target = chat_id or CHAT_ID
    if not target:
        return
    if not ALERTS_ACTIVE and label != "MANUAL SCAN":
        return
    lines = [f"🎯 <b>STOCK SCREENER [{esc(label)}]</b>",
             f"🕒 {now_ist().strftime('%d-%m-%Y %H:%M')} IST",
             "─────────────────────"]
    with LOCK:
        wl = list(state["watchlist"])
    for sym in wl:
        d = get_market_data(sym)
        if not d:
            lines.append(f"• {esc(sym)}: data nahi mila")
            continue
        sig = get_signal(d)
        icon = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}[sig]
        lines.append(
            f"{icon} <b>{esc(sym)}</b> ₹{d['price']} ({d['change_pct']:+.2f}%)\n"
            f"   EMA9/21: {d['ema9']}/{d['ema21']} | VWAP: {d['vwap']} | Vol: {d['vol_ratio']}x → <b>{sig}</b>"
        )
    lines.append("─────────────────────")
    if not is_trading_day():
        lines.append("ℹ️ Aaj market band hai, ye pichla data hai.")
    lines.append("⚠️ Sirf paper/educational signal hai, financial advice nahi.")
    send(target, "\n".join(lines), html=True)

# ================= 8. PAPER TRADING ENGINE (charges + slippage + limits) =================
def calc_charges(buy_value, sell_value):
    """Approx Indian intraday charges: brokerage, STT, exchange, SEBI, GST, stamp duty."""
    brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    stt = 0.00025 * sell_value
    txn = 0.0000307 * (buy_value + sell_value)
    sebi = 0.000001 * (buy_value + sell_value)
    gst = 0.18 * (brokerage + txn + sebi)
    stamp = 0.00003 * buy_value
    return round(brokerage + stt + txn + sebi + gst + stamp, 2)

def entry_fill(side, price):
    return round(price * (1 + SLIPPAGE), 2) if side == "BUY" else round(price * (1 - SLIPPAGE), 2)

def exit_fill(side, price):
    return round(price * (1 - SLIPPAGE), 2) if side == "BUY" else round(price * (1 + SLIPPAGE), 2)

def trade_pnl(side, entry, exit_, qty):
    gross = (exit_ - entry) * qty if side == "BUY" else (entry - exit_) * qty
    buy_v, sell_v = (entry * qty, exit_ * qty) if side == "BUY" else (exit_ * qty, entry * qty)
    ch = calc_charges(buy_v, sell_v)
    return round(gross, 2), ch, round(gross - ch, 2)

def roll_day():
    today = now_ist().strftime("%Y-%m-%d")
    with LOCK:
        if state["day_stats"].get("day") != today:
            state["day_stats"] = {"day": today, "opened": 0, "pnl": 0.0, "halted": False}
            save_state()

def open_position(symbol, side, signal_price, qty):
    entry = entry_fill(side, signal_price)
    with LOCK:
        acc = state["account"]
        blocked = entry * qty
        if blocked > acc["available_cash"] or symbol in acc["positions"]:
            return
        acc["available_cash"] -= blocked
        target = round(entry * (1 + TARGET_PCT), 2) if side == "BUY" else round(entry * (1 - TARGET_PCT), 2)
        sl = round(entry * (1 - SL_PCT), 2) if side == "BUY" else round(entry * (1 + SL_PCT), 2)
        t = now_ist().strftime("%H:%M:%S")
        acc["positions"][symbol] = {"side": side, "entry_price": entry, "qty": qty, "target": target,
                                    "sl": sl, "time": t, "day": now_ist().strftime("%Y-%m-%d")}
        state["day_stats"]["opened"] += 1
        save_state()
    notify(
        f"🟢 <b>[PAPER TRADE: {side}] {esc(symbol)}</b>\n─────────────────────\n"
        f"• Entry: ₹{entry} (slippage ke saath)\n• Qty: {qty}\n"
        f"• Target: ₹{target} (1.0%)\n• Stop Loss: ₹{sl} (0.5%)\n• Time: {t} IST"
    )

def close_position(symbol, raw_price, reason):
    with LOCK:
        acc = state["account"]
        pos = acc["positions"].pop(symbol, None)
        if not pos:
            return
        side, qty, entry = pos["side"], pos["qty"], pos["entry_price"]
        exit_p = exit_fill(side, raw_price)
        gross, charges, net = trade_pnl(side, entry, exit_p, qty)
        acc["available_cash"] += entry * qty + net
        acc["realized_pnl"] += net
        acc["charges_total"] = acc.get("charges_total", 0.0) + charges
        acc["trades_count"] += 1
        if net > 0:
            acc["wins"] = acc.get("wins", 0) + 1
        state["day_stats"]["pnl"] = round(state["day_stats"].get("pnl", 0.0) + net, 2)
        state["last_exit"][symbol] = time.time()
        state["trade_history"].append({
            "symbol": symbol, "side": side, "entry": entry, "exit": exit_p, "qty": qty,
            "gross": gross, "charges": charges, "pnl": net, "reason": reason,
            "date": now_ist().strftime("%d-%m %H:%M"), "day": now_ist().strftime("%Y-%m-%d"),
        })
        state["trade_history"] = state["trade_history"][-100:]
        total, day_pnl = acc["realized_pnl"], state["day_stats"]["pnl"]
        save_state()
    icon = "💰" if net >= 0 else "🛑"
    notify(
        f"{icon} <b>[CLOSED: {esc(symbol)}] ({esc(reason)})</b>\n─────────────────────\n"
        f"• Entry: ₹{entry} ➔ Exit: ₹{exit_p}\n• Gross: ₹{gross:+.2f} | Charges: ₹{charges:.2f}\n"
        f"• Net P&L: <b>₹{net:+.2f}</b>\n• Aaj ka P&L: ₹{day_pnl:+.2f}\n• Total Realized: ₹{total:+.2f}"
    )

def confirm_market_open():
    """Data se confirm karta hai ki aaj market sach mein khula hai (holiday calendar ka backup)."""
    global MARKET_CONFIRMED_DATE, AUTO_HOLIDAY_DATE
    today = now_ist().date().isoformat()
    if MARKET_CONFIRMED_DATE == today:
        return True
    if AUTO_HOLIDAY_DATE == today:
        return False
    d = get_market_data("NIFTY")
    if d and d["last_date"] == today:
        MARKET_CONFIRMED_DATE = today
        return True
    if d and now_ist().time() >= datetime.time(9, 45):
        AUTO_HOLIDAY_DATE = today
        notify("🏖️ Aaj market band lag raha hai (naya data nahi aaya). Trading aaj skip hogi.")
    return False

def trading_loop():
    time.sleep(15)
    while True:
        try:
            if is_trading_day():
                roll_day()
                now = now_ist()
                t, today = now.time(), now.strftime("%Y-%m-%d")
                with LOCK:
                    open_syms = dict(state["account"]["positions"])
                    wl = list(state["watchlist"])

                # Purane din ki bachi position turant band
                for sym, pos in open_syms.items():
                    if pos.get("day") and pos["day"] != today and t >= MARKET_OPEN:
                        d = get_market_data(sym)
                        if d and d["fresh"]:
                            close_position(sym, d["price"], "PREVIOUS DAY CARRY EXIT")

                if t >= SQUARE_OFF and open_syms:
                    for sym in list(open_syms.keys()):
                        d = get_market_data(sym)
                        if d:
                            close_position(sym, d["price"], "3:15 PM AUTO EXIT")

                elif MARKET_OPEN <= t < SQUARE_OFF and confirm_market_open():
                    for symbol in wl:
                        d = get_market_data(symbol)
                        if not d or not d["fresh"]:
                            continue
                        price = d["price"]
                        with LOCK:
                            pos = state["account"]["positions"].get(symbol)
                            npos = len(state["account"]["positions"])
                            ds = dict(state["day_stats"])
                            last_exit = state["last_exit"].get(symbol, 0)

                        if pos:
                            if pos["side"] == "BUY":
                                if price >= pos["target"]:
                                    close_position(symbol, price, "TARGET HIT")
                                elif price <= pos["sl"]:
                                    close_position(symbol, price, "STOP-LOSS HIT")
                            else:
                                if price <= pos["target"]:
                                    close_position(symbol, price, "TARGET HIT")
                                elif price >= pos["sl"]:
                                    close_position(symbol, price, "STOP-LOSS HIT")
                            continue

                        # ---- naye entry ke risk checks ----
                        if t >= LAST_ENTRY or npos >= MAX_POSITIONS:
                            continue
                        if ds["opened"] >= MAX_TRADES_PER_DAY:
                            continue
                        if ds["pnl"] <= -DAILY_MAX_LOSS:
                            if not ds.get("halted"):
                                with LOCK:
                                    state["day_stats"]["halted"] = True
                                    save_state()
                                notify(f"🚫 Daily loss limit (₹{DAILY_MAX_LOSS:,.0f}) hit. "
                                       f"Aaj naye trade band.")
                            continue
                        if time.time() - last_exit < COOLDOWN_MIN * 60:
                            continue
                        sig = get_signal(d)
                        if sig in ("BUY", "SELL"):
                            qty = max(1, int(PER_TRADE_AMOUNT / price))
                            open_position(symbol, sig, price, qty)
        except Exception as e:
            print(f"Trading loop error: {e}")
        time.sleep(60)

# ================= 9. PRICE ALERTS =================
def price_alert_loop():
    time.sleep(20)
    while True:
        try:
            with LOCK:
                alerts = list(state["price_alerts"])
            for a in alerts:
                d = get_market_data(a["symbol"])
                if not d:
                    continue
                hit = (a["direction"] == "above" and d["price"] >= a["price"]) or \
                      (a["direction"] == "below" and d["price"] <= a["price"])
                if hit:
                    with LOCK:
                        if a in state["price_alerts"]:
                            state["price_alerts"].remove(a)
                            save_state()
                    if CHAT_ID:
                        send(CHAT_ID, f"🔔 PRICE ALERT: {a['symbol']} ₹{d['price']} "
                                      f"({a['direction']} ₹{a['price']} hit hua)")
        except Exception as e:
            print(f"Alert loop error: {e}")
        time.sleep(120)

# ================= 10. NEWS =================
FEEDS = {
    "📊 Indian Market News": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en",
    "🇮🇳 India Top News": "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-IN&gl=IN&ceid=IN:en",
    "🌍 World News": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
}

def fetch_bulletin_news(count=10):
    sections, raw = [], []
    for category, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                sections.append(f"<b>{esc(category)}</b>\nNews abhi nahi mili")
                continue
            lines = [f"<b>{esc(category)}</b>"]
            for i, e in enumerate(feed.entries[:count], start=1):
                title = e.title.split(" - ")[0].strip()
                lines.append(f"{i}. {esc(title)}")
                raw.append(title)
            sections.append("\n".join(lines))
        except Exception:
            sections.append(f"<b>{esc(category)}</b>\nNews fetch error")
    return sections, raw

def send_news(chat_id=None):
    target = chat_id or CHAT_ID
    if not target:
        return
    sections, _ = fetch_bulletin_news()
    send(target, "📰 <b>TAAZA NEWS BULLETIN</b>", html=True)
    for s in sections:
        send(target, s, html=True)
        time.sleep(1)

# ================= 11. SCHEDULER (IST, restart-safe) =================
def run_job_once(name, hhmm, fn, trading_only=True, window_min=20):
    """Job sirf apne time ke window (0-20 min) mein chalta hai, aur din mein ek hi baar."""
    now = now_ist()
    today = now.strftime("%Y-%m-%d")
    if trading_only and not is_trading_day():
        return
    h, m = map(int, hhmm.split(":"))
    sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (now - sched).total_seconds() / 60
    with LOCK:
        done = state["jobs_done"].get(name) == today
    if 0 <= delta <= window_min and not done:
        with LOCK:
            state["jobs_done"][name] = today
            save_state()
        fn()

def holiday_note():
    n = now_ist()
    name = holiday_name(n.date())
    if n.weekday() < 5 and name:
        notify(f"🏖️ <b>Aaj NSE holiday hai:</b> {esc(name)}\nMarket band, scan aur paper trading aaj nahi hongi.")

def daily_report():
    with LOCK:
        acc, ds = state["account"], state["day_stats"]
        text = (f"📈 <b>DAILY REPORT</b>\n─────────────────────\n"
                f"• Aaj ke trades: {ds.get('opened', 0)}\n• Aaj ka Net P&L: ₹{ds.get('pnl', 0):+,.2f}\n"
                f"• Total Realized: ₹{acc['realized_pnl']:+,.2f}\n"
                f"• Total Charges: ₹{acc.get('charges_total', 0):,.2f}\n"
                f"• Cash: ₹{acc['available_cash']:,.2f}")
    notify(text)

def scheduler_loop():
    time.sleep(10)
    while True:
        try:
            if ALERTS_ACTIVE:
                run_job_once("holiday", "09:00", holiday_note, trading_only=False, window_min=60)
                run_job_once("morning", "09:00", lambda: run_screener_task("MORNING SCAN"))
                run_job_once("eod", "15:25", lambda: run_screener_task("EOD SCAN"))
                run_job_once("report", "15:35", daily_report)
                with LOCK:
                    last_news = state.get("last_news", 0)
                if time.time() - last_news >= 3 * 3600:
                    with LOCK:
                        state["last_news"] = time.time()
                        save_state()
                    send_news()
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(30)

# ================= 12. NIFTY 200 + MARKET REPORT =================
NIFTY200_FALLBACK = """RELIANCE TCS HDFCBANK ICICIBANK INFY BHARTIARTL SBIN LT ITC HINDUNILVR KOTAKBANK AXISBANK
BAJFINANCE MARUTI SUNPHARMA ASIANPAINT HCLTECH M&M NTPC TITAN ULTRACEMCO POWERGRID TATAMOTORS TATASTEEL
ADANIENT ADANIPORTS ONGC COALINDIA JSWSTEEL BAJAJFINSV NESTLEIND WIPRO TECHM INDUSINDBK HINDALCO GRASIM
CIPLA DRREDDY EICHERMOT APOLLOHOSP BPCL TATACONSUM BRITANNIA HEROMOTOCO DIVISLAB SBILIFE HDFCLIFE BAJAJ-AUTO
SHRIRAMFIN LTIM ADANIPOWER ADANIGREEN AMBUJACEM BANKBARODA PNB CANBK UNIONBANK IOC GAIL VEDL DLF HAL BEL
ETERNAL TRENT JIOFIN IRCTC INDIGO PIDILITIND SIEMENS ABB HAVELLS DABUR GODREJCP MARICO COLPAL TORNTPHARM
LUPIN AUROPHARMA ZYDUSLIFE MAXHEALTH BIOCON MUTHOOTFIN CHOLAFIN PFC RECLTD IRFC SAIL NMDC JINDALSTEL
TVSMOTOR ASHOKLEY BOSCHLTD MRF BHARATFORG PERSISTENT COFORGE MPHASIS OFSS NAUKRI POLICYBZR ICICIPRULI
ICICIGI SBICARD BAJAJHLDNG LODHA GODREJPROP OBEROIRLTY PRESTIGE PHOENIXLTD TATAPOWER NHPC SJVN TORNTPOWER
JSWENERGY INDUSTOWER IDEA YESBANK IDFCFIRSTB FEDERALBNK AUBANK BANDHANBNK MOTHERSON CUMMINSIND POLYCAB
DIXON VOLTAS PAGEIND BERGEPAINT UPL SRF DEEPAKNTR TATACHEM LICI NYKAA DMART ABCAPITAL LICHSGFIN MANKIND ALKEM""".split()

NIFTY200 = list(NIFTY200_FALLBACK)
NIFTY200_SOURCE = "built-in list"

def load_nifty200():
    """NSE se official Nifty 200 list laane ki koshish. Na mile to built-in list use hoti hai."""
    global NIFTY200, NIFTY200_SOURCE
    try:
        h = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
        sess = requests.Session()
        sess.get("https://www.nseindia.com", headers=h, timeout=10)
        r = sess.get("https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
                     headers=h, timeout=20)
        if r.status_code == 200:
            rows = list(csv.reader(r.text.splitlines()))[1:]
            syms = [row[2].strip() for row in rows if len(row) > 2 and row[2].strip()]
            if len(syms) >= 150:
                NIFTY200 = syms
                NIFTY200_SOURCE = "NSE official"
    except Exception as e:
        print(f"Nifty200 list error: {e}")

_n200_cache = {"ts": 0, "rows": []}

def nifty200_snapshot():
    """Saare Nifty 200 stocks ka latest price + day change (5 min cache)."""
    if time.time() - _n200_cache["ts"] < 300 and _n200_cache["rows"]:
        return _n200_cache["rows"]
    rows = []
    try:
        syms = list(NIFTY200)
        tickers = [yf_symbol(x) for x in syms]
        df = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                         threads=True, progress=False, auto_adjust=False)
        for x, t in zip(syms, tickers):
            try:
                c = df[t]["Close"].dropna()
                if len(c) < 2:
                    continue
                price, prev = float(c.iloc[-1]), float(c.iloc[-2])
                rows.append({"symbol": x, "price": round(price, 2),
                             "chg": round((price - prev) / prev * 100, 2)})
            except Exception:
                continue
    except Exception as e:
        print(f"Nifty200 snapshot error: {e}")
    if rows:
        _n200_cache.update({"ts": time.time(), "rows": rows})
    return rows

def market_report(chat_id, top_n=5):
    hol = holiday_name()
    status = "🟢 Open" if is_market_hours() else ("🏖️ Holiday: " + hol if hol else "🔴 Closed (last data)")
    lines = ["🏦 <b>MARKET REPORT</b>",
             f"🕒 {now_ist().strftime('%d-%m-%Y %H:%M')} IST | Market: {esc(status)}",
             "─────────────────────"]
    ai_data = {"indices": {}, "breadth": {}}
    for name in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        d = get_market_data(name)
        if d:
            arrow = "🟢" if d["change_pct"] >= 0 else "🔴"
            lines.append(f"{arrow} <b>{name}</b>: {d['price']} ({d['change_pct']:+.2f}%)")
            ai_data["indices"][name] = {"price": d["price"], "chg%": d["change_pct"],
                                        "ema9": d["ema9"], "ema21": d["ema21"], "vwap": d["vwap"]}
    rows = nifty200_snapshot()
    if rows:
        adv = sum(1 for r in rows if r["chg"] > 0)
        dec = sum(1 for r in rows if r["chg"] < 0)
        srt = sorted(rows, key=lambda r: r["chg"], reverse=True)
        gain, lose = srt[:top_n], srt[-top_n:][::-1]
        lines.append("─────────────────────")
        lines.append(f"📊 <b>Nifty 200 ({len(rows)} stocks)</b>: 🟢 {adv} up | 🔴 {dec} down")
        lines.append("🚀 <b>Top Gainers</b>")
        lines += [f"  • {esc(r['symbol'])} ₹{r['price']} ({r['chg']:+.2f}%)" for r in gain]
        lines.append("📉 <b>Top Losers</b>")
        lines += [f"  • {esc(r['symbol'])} ₹{r['price']} ({r['chg']:+.2f}%)" for r in lose]
        ai_data["breadth"] = {"advancing": adv, "declining": dec,
                              "top_gainers": [(r["symbol"], r["chg"]) for r in gain],
                              "top_losers": [(r["symbol"], r["chg"]) for r in lose]}
    else:
        lines.append("⚠️ Nifty 200 data abhi nahi mila.")
    send(chat_id, "\n".join(lines), html=True)

    prompt = ("Ye live Indian market data hai. Simple Hinglish mein market ka analysis do: overall mood "
              "(bullish/bearish/sideways), breadth kya bata rahi hai, kaunse stocks strong/weak, aage kya "
              "dekhna chahiye, aur risk. Chhota aur clear rakho. Data: " + json.dumps(ai_data))
    send(chat_id, "🧠 Hermes Analysis:\n\n" + ask_groq(chat_id, prompt, remember=False))

def do_analysis(chat_id, sym):
    d = get_market_data(sym)
    if not d:
        send(chat_id, f"❌ {sym} ka data nahi mila.")
        return
    send(chat_id, (f"📌 {sym}: ₹{d['price']} ({d['change_pct']:+.2f}%) | High/Low {d['day_high']}/{d['day_low']}\n"
                   f"EMA9/21: {d['ema9']}/{d['ema21']} | VWAP {d['vwap']} | Vol {d['vol_ratio']}x | "
                   f"Signal: {get_signal(d)}"))
    prompt = (f"Is real data ka short analysis do (trend, support/resistance idea, risk, kya dekhna chahiye). "
              f"Financial advice mat do. Data: {json.dumps(d)}")
    send(chat_id, ask_groq(chat_id, prompt, remember=False))

# ================= 13. WEATHER (Open-Meteo, free) =================
WMO = {0: "☀️ Saaf aasmaan", 1: "🌤️ Mostly saaf", 2: "⛅ Partly cloudy", 3: "☁️ Badal",
       45: "🌫️ Kohra", 48: "🌫️ Kohra", 51: "🌦️ Halki phuhar", 53: "🌦️ Phuhar", 55: "🌧️ Tez phuhar",
       61: "🌧️ Halki barish", 63: "🌧️ Barish", 65: "🌧️ Tez barish", 71: "❄️ Halki barfbari",
       73: "❄️ Barfbari", 75: "❄️ Tez barfbari", 80: "🌦️ Baarish ke jhonke", 81: "🌧️ Tez jhonke",
       82: "⛈️ Bahut tez barish", 95: "⛈️ Aandhi-toofan", 96: "⛈️ Toofan + ole", 99: "⛈️ Toofan + ole"}

def get_weather(city):
    try:
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1, "language": "en"}, timeout=12).json()
        if not g.get("results"):
            return f"❌ '{esc(city)}' shehar nahi mila. Spelling check karein."
        loc = g["results"][0]
        f = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto", "forecast_days": 3}, timeout=12).json()
        c, dly = f["current"], f["daily"]
        lines = [f"🌍 <b>{esc(loc['name'])}, {esc(loc.get('country', ''))}</b>",
                 f"{WMO.get(c['weather_code'], '🌡️')} | 🌡️ {c['temperature_2m']}°C "
                 f"(feels {c['apparent_temperature']}°C)",
                 f"💧 Humidity: {c['relative_humidity_2m']}% | 💨 Hawa: {c['wind_speed_10m']} km/h",
                 "─────────────────────", "<b>Agle 3 din:</b>"]
        for i in range(len(dly["time"])):
            lines.append(f"• {dly['time'][i]}: {dly['temperature_2m_min'][i]}°–{dly['temperature_2m_max'][i]}°C, "
                         f"barish chance {dly['precipitation_probability_max'][i]}% "
                         f"{WMO.get(dly['weather_code'][i], '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Weather fetch error: {esc(e)}"

WEATHER_STOP = set("""weather mausam ka ki ke ko aaj kal today tomorrow kaisa kaisi kaise hai hain me mein in of the
batao bata do de dena temperature barish rain forecast kya hoga hogi kitna kitni abhi ab please plz city bhai
yaar tell me what is whats how garmi sardi""".split())

def extract_city(text):
    words = [w for w in re.findall(r"[A-Za-z]+", text) if w.lower() not in WEATHER_STOP]
    if words:
        return " ".join(words[:3])
    with LOCK:
        return state.get("city", "Delhi")

# ================= 14. SPORTS: CRICKET + FOOTBALL =================
SPORT_STOP = set("""cricket football soccer score scores live match matches ka ki ke ko aaj today abhi update updates
batao bata do de dena kya hai hain hua hui chal raha rahi kaisa kaisi result results status of the in me mein
please plz bhai yaar tell me what is whats how current now latest game games vs versus v dikhao dikha
chalu chal""".split())

CRIC_ALIASES = {"ipl": "indian premier league", "wpl": "women's premier league", "bbl": "big bash",
                "psl": "pakistan super league", "cpl": "caribbean premier league"}
FB_ALIASES = {"barca": "barcelona", "man utd": "manchester united", "man united": "manchester united",
              "man city": "manchester city", "spurs": "tottenham", "psg": "paris saint-germain",
              "atletico": "atletico madrid", "bayern": "bayern munich", "juve": "juventus",
              "epl": "premier league"}

def sport_query(text, aliases):
    t = norm(text)
    for k, v in aliases.items():
        t = re.sub(rf"\b{re.escape(k)}\b", v, t)
    toks = [w for w in re.findall(r"[a-z0-9'\.\-]+", t) if w not in SPORT_STOP]
    return toks

def to_ist_str(iso):
    try:
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(IST).strftime("%d-%b %H:%M IST")
    except Exception:
        return ""

# ---------- Cricket (ESPNcricinfo) ----------
CRIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.espncricinfo.com",
    "Referer": "https://www.espncricinfo.com/",
}
_cric_cache = {"ts": 0, "matches": [], "ok": False}

def parse_cricket_match(m):
    teams = []
    for tm in (m.get("teams") or []):
        team = tm.get("team") or {}
        name = team.get("longName") or team.get("name") or team.get("abbreviation") or "?"
        teams.append({"name": name, "abbr": team.get("abbreviation") or "",
                      "score": tm.get("score") or "", "info": tm.get("scoreInfo") or ""})
    series = m.get("series") or {}
    state_ = str(m.get("state") or "").upper()
    status = str(m.get("status") or "").lower()
    stage = str(m.get("stage") or "").upper()
    live = state_ == "LIVE" or stage == "RUNNING" or status == "live"
    done = state_ == "POST" or stage == "FINISHED" or status in ("result", "complete", "completed")
    return {
        "id": m.get("objectId") or m.get("id"),
        "series": series.get("longName") or series.get("name") or "",
        "title": m.get("title") or "",
        "format": m.get("format") or "",
        "ground": (m.get("ground") or {}).get("name") or "",
        "status_text": m.get("statusText") or "",
        "start": m.get("startTime") or "",
        "teams": teams, "live": live, "done": done and not live,
    }

def fetch_cricket_matches():
    if time.time() - _cric_cache["ts"] < 30 and _cric_cache["matches"]:
        return _cric_cache["matches"], True
    urls = ["https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en&latest=true",
            "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/live?lang=en"]
    seen, out, ok = set(), [], False
    for u in urls:
        try:
            r = requests.get(u, headers=CRIC_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            ms = data.get("matches") or (data.get("content") or {}).get("matches") or []
            ok = True
            for m in ms:
                pm = parse_cricket_match(m)
                key = pm["id"] or (pm["series"], pm["title"], tuple(t["name"] for t in pm["teams"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(pm)
        except Exception as e:
            print(f"Cricket fetch error: {e}")
    if out:
        _cric_cache.update({"ts": time.time(), "matches": out})
    return out, ok

def fmt_cricket(m):
    tag = "🟢 LIVE" if m["live"] else ("✅ Result" if m["done"] else "🕒 Upcoming")
    head = f"🏏 <b>{esc(m['series'])}</b>" + (f" — {esc(m['title'])}" if m["title"] else "")
    lines = [head, tag + (f" ({esc(m['format'])})" if m["format"] else "")]
    if not m["live"] and not m["done"] and m["start"]:
        st = to_ist_str(m["start"])
        if st:
            lines.append(f"  ⏰ {st}")
    for t in m["teams"]:
        lines.append(f"  {esc(t['name'])}: <b>{esc(t['score'] or '-')}</b> {esc(t['info'])}".rstrip())
    if m["status_text"]:
        lines.append(f"  ℹ️ {esc(m['status_text'])}")
    if m["ground"]:
        lines.append(f"  📍 {esc(m['ground'])}")
    return "\n".join(lines)

def cricket_report(text=""):
    matches, ok = fetch_cricket_matches()
    if not matches:
        return ("🏏 Cricket data abhi nahi mila (source down ya koi match nahi)." if ok else
                "⚠️ Cricket score source abhi reachable nahi hai. Thodi der baad try karein.")
    toks = sport_query(text, CRIC_ALIASES)
    if toks:
        def hay(m):
            return norm(" ".join([m["series"], m["title"], m["format"]] +
                                 [t["name"] + " " + t["abbr"] for t in m["teams"]]))
        sel = [m for m in matches if all(tk in hay(m) for tk in toks)]
        if not sel:
            return f"🏏 '{esc(' '.join(toks))}' ka koi cricket match abhi list mein nahi mila."
        sel.sort(key=lambda m: (not m["live"], m["done"]))
        return "\n\n".join(fmt_cricket(m) for m in sel[:6])
    live = [m for m in matches if m["live"]]
    if live:
        return "\n\n".join(fmt_cricket(m) for m in live[:8])
    done = [m for m in matches if m["done"]][:4]
    upc = [m for m in matches if not m["live"] and not m["done"]][:3]
    parts = ["🏏 Abhi koi live match nahi hai."]
    if done:
        parts.append("<b>Haal ke results:</b>\n\n" + "\n\n".join(fmt_cricket(m) for m in done))
    if upc:
        parts.append("<b>Aane wale match:</b>\n\n" + "\n\n".join(fmt_cricket(m) for m in upc))
    return "\n\n".join(parts)

# ---------- Football (ESPN public scoreboard) ----------
FB_LEAGUES = ["eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa", "uefa.europa.conf",
              "eng.2", "eng.fa", "eng.league_cup", "esp.copa_del_rey", "ned.1", "por.1", "tur.1", "sco.1",
              "bra.1", "arg.1", "usa.1", "mex.1", "ind.1", "sau.1", "fifa.world", "fifa.friendly",
              "uefa.nations", "uefa.euro", "conmebol.libertadores", "conmebol.america", "concacaf.gold",
              "afc.asian.cup", "caf.nations", "fifa.worldq.uefa", "fifa.worldq.conmebol", "fifa.worldq.afc",
              "fifa.cwc", "uefa.super_cup", "ger.2", "ita.2", "fra.2", "esp.2", "bel.1", "aus.1", "jpn.1"]
_fb_cache = {"ts": 0, "matches": [], "ok": 0}

def parse_espn_events(data, slug):
    out = []
    try:
        lg = ((data.get("leagues") or [{}])[0].get("name")) or slug
    except Exception:
        lg = slug
    for ev in data.get("events") or []:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            comps = comp.get("competitors") or []
            home = next((c for c in comps if c.get("homeAway") == "home"), comps[0] if comps else None)
            away = next((c for c in comps if c.get("homeAway") == "away"), comps[1] if len(comps) > 1 else None)
            if not home or not away:
                continue
            st = ev.get("status") or comp.get("status") or {}
            typ = st.get("type") or {}
            state_ = typ.get("state", "pre")
            detail = typ.get("shortDetail") or typ.get("detail") or ""
            clock = st.get("displayClock") or ""
            ht = (home.get("team") or {})
            at = (away.get("team") or {})
            out.append({
                "league": lg, "state": state_, "detail": detail, "clock": clock,
                "home": ht.get("displayName") or ht.get("name") or "?", "home_abbr": ht.get("abbreviation") or "",
                "away": at.get("displayName") or at.get("name") or "?", "away_abbr": at.get("abbreviation") or "",
                "hs": home.get("score", "0"), "as": away.get("score", "0"),
                "date": ev.get("date") or "",
            })
        except Exception:
            continue
    return out

def fetch_one_league(slug):
    try:
        r = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
                         params={"limit": 200}, timeout=8)
        if r.status_code != 200:
            return None
        return parse_espn_events(r.json(), slug)
    except Exception:
        return None

def fetch_football_matches():
    if time.time() - _fb_cache["ts"] < 45 and _fb_cache["matches"]:
        return _fb_cache["matches"], _fb_cache["ok"]
    allm, okc = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(fetch_one_league, FB_LEAGUES):
            if res is not None:
                okc += 1
                allm.extend(res)
    if allm:
        _fb_cache.update({"ts": time.time(), "matches": allm, "ok": okc})
    return allm, okc

def fmt_fb_line(m):
    if m["state"] == "in":
        return f"🟢 {esc(m['detail'] or m['clock'])}  {esc(m['home'])} <b>{esc(m['hs'])}-{esc(m['as'])}</b> {esc(m['away'])}"
    if m["state"] == "post":
        return f"✅ {esc(m['detail'] or 'FT')}  {esc(m['home'])} <b>{esc(m['hs'])}-{esc(m['as'])}</b> {esc(m['away'])}"
    return f"🕒 {esc(to_ist_str(m['date']))}  {esc(m['home'])} vs {esc(m['away'])}"

def group_fb(matches, limit=25):
    order = {"in": 0, "pre": 1, "post": 2}
    matches = sorted(matches, key=lambda m: (order.get(m["state"], 3), m["league"], m["date"]))[:limit]
    lines, cur = [], None
    for m in matches:
        if m["league"] != cur:
            cur = m["league"]
            lines.append(f"\n⚽ <b>{esc(cur)}</b>")
        lines.append(fmt_fb_line(m))
    return "\n".join(lines).strip()

def football_report(text=""):
    matches, okc = fetch_football_matches()
    if not matches:
        return ("⚽ Aaj koi football match list mein nahi mila." if okc else
                "⚠️ Football score source abhi reachable nahi hai. Thodi der baad try karein.")
    toks = sport_query(text, FB_ALIASES)
    if toks:
        def hay(m):
            return norm(" ".join([m["league"], m["home"], m["home_abbr"], m["away"], m["away_abbr"]]))
        sel = [m for m in matches if all(tk in hay(m) for tk in toks)]
        if not sel:
            return f"⚽ '{esc(' '.join(toks))}' ka aaj koi match nahi mila (sirf aaj ke matches dikhte hain)."
        return group_fb(sel, 15)
    live = [m for m in matches if m["state"] == "in"]
    if live:
        return "🔴 <b>LIVE FOOTBALL</b>\n" + group_fb(live, 30)
    return "⚽ Abhi koi live match nahi. Aaj ke matches:\n" + group_fb(matches, 25)

def send_sport(chat_id, kind, text=""):
    if kind == "cricket":
        send(chat_id, cricket_report(text), html=True)
    elif kind == "football":
        send(chat_id, football_report(text), html=True)
    else:
        toks = sport_query(text, {**CRIC_ALIASES, **FB_ALIASES})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fc = ex.submit(cricket_report, text)
            ff = ex.submit(football_report, text)
            c_txt, f_txt = fc.result(), ff.result()
        if toks:
            hits = [x for x in (c_txt, f_txt) if not x.startswith(("🏏 '", "⚽ '", "⚠️"))]
            send(chat_id, "\n\n".join(hits) if hits else
                 f"❌ '{esc(' '.join(toks))}' ka koi cricket ya football match abhi nahi mila.", html=True)
        else:
            send(chat_id, c_txt, html=True)
            send(chat_id, f_txt, html=True)

# ================= 15. BACKTEST =================
def backtest_symbol(symbol):
    df = yf.Ticker(yf_symbol(symbol)).history(period="1mo", interval="5m")
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["Close"])
    if len(df) < 60:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_convert(IST)
    df = df.copy()
    df["ema9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["day"] = df.index.date
    df["pv"] = (df["High"] + df["Low"] + df["Close"]) / 3 * df["Volume"]
    cum_pv = df.groupby("day")["pv"].cumsum()
    cum_v = df.groupby("day")["Volume"].cumsum().replace(0, float("nan"))
    df["vwap"] = (cum_pv / cum_v).fillna(df["Close"])
    avgv = df["Volume"].rolling(20).mean().shift(1)
    df["volr"] = (df["Volume"] / avgv).replace([float("inf"), -float("inf")], float("nan")).fillna(1.0)

    rows = list(zip(df.index, df["Close"], df["ema9"], df["ema21"], df["vwap"], df["volr"]))
    pos, last_exit_i = None, -999
    trades, day_pnl, day_cnt = [], {}, {}
    cooldown_candles = max(1, COOLDOWN_MIN // 5)

    def finish(p, c, ts):
        ex = exit_fill(p["side"], c)
        gross, ch, net = trade_pnl(p["side"], p["entry"], ex, p["qty"])
        trades.append({"net": net, "gross": gross, "charges": ch})
        day_pnl[p["day"]] = day_pnl.get(p["day"], 0.0) + net

    for i, (ts, c, e9, e21, vw, vr) in enumerate(rows):
        tm, day = ts.time(), ts.date()
        if pos:
            reason = None
            if pos["side"] == "BUY":
                reason = "T" if c >= pos["target"] else ("SL" if c <= pos["sl"] else None)
            else:
                reason = "T" if c <= pos["target"] else ("SL" if c >= pos["sl"] else None)
            if reason is None and (tm >= SQUARE_OFF or day != pos["day"]):
                reason = "EOD"
            if reason:
                finish(pos, c, ts)
                pos, last_exit_i = None, i
            continue
        if not (MARKET_OPEN <= tm < LAST_ENTRY) or i - last_exit_i < cooldown_candles:
            continue
        if day_cnt.get(day, 0) >= MAX_TRADES_PER_DAY or day_pnl.get(day, 0.0) <= -DAILY_MAX_LOSS:
            continue
        sig = None
        if e9 > e21 and c > vw and vr > 1.25:
            sig = "BUY"
        elif e9 < e21 and c < vw and vr > 1.25:
            sig = "SELL"
        if sig:
            entry = entry_fill(sig, c)
            qty = max(1, int(PER_TRADE_AMOUNT / c))
            tgt = entry * (1 + TARGET_PCT) if sig == "BUY" else entry * (1 - TARGET_PCT)
            slv = entry * (1 - SL_PCT) if sig == "BUY" else entry * (1 + SL_PCT)
            pos = {"side": sig, "entry": entry, "qty": qty, "target": tgt, "sl": slv, "day": day}
            day_cnt[day] = day_cnt.get(day, 0) + 1
    if pos:
        finish(pos, rows[-1][1], rows[-1][0])

    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for t in trades:
        cum += t["net"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return {
        "symbol": symbol.upper(), "days": len(set(df["day"])), "trades": n, "wins": len(wins),
        "win_rate": (len(wins) / n * 100) if n else 0.0,
        "net": round(sum(t["net"] for t in trades), 2),
        "gross": round(sum(t["gross"] for t in trades), 2),
        "charges": round(sum(t["charges"] for t in trades), 2),
        "avg_win": round(sum(t["net"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["net"] for t in losses) / len(losses), 2) if losses else 0.0,
        "max_dd": round(mdd, 2),
    }

def backtest_text(results):
    lines = ["🧪 <b>BACKTEST (pichhle ~1 mahine, 5-min candles)</b>",
             "Charges + slippage shamil. Sirf history hai, future ki guarantee nahi.",
             "─────────────────────"]
    tot_net = tot_tr = tot_w = 0
    for r in results:
        lines.append(f"• <b>{esc(r['symbol'])}</b>: {r['trades']} trades | Win {r['win_rate']:.0f}% | "
                     f"Net ₹{r['net']:+,.0f} | MaxDD ₹{r['max_dd']:,.0f}")
        tot_net += r["net"]
        tot_tr += r["trades"]
        tot_w += r["wins"]
    if len(results) > 1:
        wr = tot_w / tot_tr * 100 if tot_tr else 0
        lines.append("─────────────────────")
        lines.append(f"📊 <b>Total:</b> {tot_tr} trades | Win {wr:.0f}% | Net ₹{tot_net:+,.0f}")
    if len(results) == 1:
        r = results[0]
        lines.append(f"Gross ₹{r['gross']:+,.0f} | Charges ₹{r['charges']:,.0f}")
        lines.append(f"Avg win ₹{r['avg_win']:+,.0f} | Avg loss ₹{r['avg_loss']:+,.0f} | Din: {r['days']}")
    lines.append("💡 Net negative ho to strategy paper pe bhi kamzor hai, asli paise mat lagayein.")
    return "\n".join(lines)

# ================= 16. STATUS TEXT =================
def status_text():
    n = now_ist()
    up, hrs = fmt_uptime()
    with LOCK:
        npos = len(state["account"]["positions"])
        nwl = len(state["watchlist"])
        nal = len(state["price_alerts"])
        ds = dict(state["day_stats"])
    hol = holiday_name()
    market = "🟢 Open" if is_market_hours() else (f"🏖️ Holiday ({hol})" if hol else "🔴 Closed")
    txt = ("🟢 <b>BOT ONLINE HAI ✅</b>\n─────────────────────\n"
           f"⏱️ <b>Chal raha hai:</b> {up} (~{hrs:.1f} ghante)\n"
           f"🚀 <b>Start hua:</b> {START_TIME.strftime('%d-%b %H:%M')} IST\n"
           f"🕒 <b>Abhi IST:</b> {n.strftime('%H:%M:%S')}\n"
           f"🏦 <b>Market:</b> {esc(market)}\n"
           f"🔔 <b>Auto-Alerts:</b> {'🟢 Active' if ALERTS_ACTIVE else '🔴 Paused'}\n"
           f"🧠 <b>AI:</b> {esc(GROQ_MODEL)} ({'key set ✅' if GROQ_API_KEY else 'key missing ❌'})\n"
           f"📡 <b>Data:</b> Yahoo Finance | Nifty200: {len(NIFTY200)} ({esc(NIFTY200_SOURCE)})\n"
           f"💾 <b>Save:</b> {esc(PERSIST_MODE)}\n"
           f"💼 Positions: {npos} | 👀 Watchlist: {nwl} | 🔔 Alerts: {nal}\n"
           f"📅 Aaj: {ds.get('opened', 0)} trades | Net P&L ₹{ds.get('pnl', 0):+,.2f}")
    if DOWNTIME_NOTE:
        txt += "\n" + esc(DOWNTIME_NOTE)
    return txt

def holidays_text():
    today = now_ist().date().isoformat()
    upcoming = [(d, n) for d, n in sorted(NSE_HOLIDAYS.items()) if d >= today]
    with LOCK:
        extras = [d for d in state.get("extra_holidays", []) if d >= today]
    lines = ["🏖️ <b>Aane wali NSE holidays</b>"]
    lines += [f"• {d} — {esc(n)}" for d, n in upcoming[:8]] or ["• (2026 ki koi aur nahi bachi)"]
    if extras:
        lines.append("<b>Aapki jodi hui:</b> " + ", ".join(extras))
    lines.append("Weekend pe market waise bhi band rehta hai.")
    return "\n".join(lines)

# ================= 17. SMART ROUTING (normal text se sab kuch) =================
def has_any(t, words):
    return any(w in t for w in words)

CRICKET_WORDS = ["cricket", "ipl", "t20", "odi", "test match", "bbl", "psl", "wpl", "cpl", "ashes", "bcci"]
FOOTBALL_WORDS = ["football", "soccer", "premier league", "la liga", "bundesliga", "serie a", "ligue 1",
                  "champions league", "uefa", "fifa", "europa league", "isl "]
MARKET_PHRASES = ["nifty 200", "nifty200", "market kaisa", "market kaisi", "market ka", "market today",
                  "aaj market", "market analysis", "market report", "market update", "market ke bare",
                  "market ke baare", "market khula", "market status", "stock market", "share bazaar",
                  "share market", "bazaar", "bazar", "sensex", "nifty", "banknifty", "bank nifty",
                  "gainer", "loser"]

def route_intent(message):
    """True return karta hai agar message ko koi feature handle kar chuka."""
    text = message.text.strip()
    t = text.lower()
    cid = message.chat.id

    # 1. Bot status / uptime
    if "uptime" in t or (has_any(t, ["bot", "hermes"]) and
                         has_any(t, ["chal", "start", "online", "alive", "zinda", "run", "ghante", "hours",
                                     "kitne der", "status", "band", "on hai"])):
        send(cid, status_text(), html=True)
        return True

    # 2. Weather
    if has_any(t, ["weather", "mausam", "barish", "temperature", "garmi", "sardi"]):
        send(cid, get_weather(extract_city(text)), html=True)
        return True

    # 3. News
    if has_any(t, ["news", "khabar", "samachar", "headline"]):
        if has_any(t, ["summary", "analysis", "samjha", "asar"]):
            _, raw = fetch_bulletin_news(8)
            send(cid, ask_groq(cid, "In headlines ka simple Hinglish mein 6-8 bullet summary do aur "
                                    "market par asar batao:\n" + "\n".join(raw), remember=False))
        else:
            send_news(cid)
        return True

    # 4. Sports (cricket / football)
    is_cric = has_any(t, CRICKET_WORDS)
    is_fb = has_any(t, FOOTBALL_WORDS)
    generic_score = "score" in t or (has_any(t, ["match", "matches"]) and
                                     has_any(t, ["live", "aaj", "today", "kaisa", "update", "result", "chal"]))
    if is_cric or is_fb or generic_score:
        if is_cric and not is_fb:
            send_sport(cid, "cricket", text)
        elif is_fb and not is_cric:
            send_sport(cid, "football", text)
        else:
            send_sport(cid, "both", text)
        return True

    # 5. Holiday
    if has_any(t, ["holiday", "chhutti", "chutti"]):
        send(cid, holidays_text(), html=True)
        return True

    # 6. Specific stock / index analysis
    n200 = set(NIFTY200)
    tokens = re.findall(r"[A-Za-z&\-]+", text.upper())
    matched = [x for x in tokens if x in n200 or x in ("NIFTY", "BANKNIFTY", "SENSEX")]
    stock_words = ["price", "analysis", "analyze", "analyse", "stock", "share", "bhav", "rate", "kaisa",
                   "kaisi", "technical", "view", "target", "buy", "sell", "chart"]
    if matched and has_any(t, stock_words) and not has_any(t, ["nifty 200", "nifty200"]):
        do_analysis(cid, matched[0])
        return True

    # 7. Market / Nifty 200 overview
    if has_any(t, MARKET_PHRASES) or ("market" in t and len(t.split()) <= 4):
        market_report(cid)
        return True

    return False

# ================= 18. TELEGRAM COMMANDS =================
def owner_only(func):
    @functools.wraps(func)
    def wrapper(message):
        if not is_owner(message):
            bot.reply_to(message, "⛔ Ye private bot hai.")
            return
        try:
            return func(message)
        except Exception as e:
            print(f"Handler error in {func.__name__}: {e}")
            try:
                bot.reply_to(message, f"⚠️ Kuch gadbad hui: {e}")
            except Exception:
                pass
    return wrapper

def arg_of(message):
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""

@bot.message_handler(commands=['myid'])
def my_id(message):
    bot.reply_to(message, f"Aapka Chat ID: {message.chat.id}\n"
                          f"Isko Render mein TELEGRAM_CHAT_ID ki tarah daal dein.")

@bot.message_handler(commands=['start', 'help'])
@owner_only
def send_welcome(message):
    bot.reply_to(message, (
        "⚡ <b>Hermes All-in-One Bot</b>\n\n"
        "<b>💬 Chat:</b> Koi bhi sawaal seedha likhein. <code>/clear</code> - memory reset\n\n"
        "<b>📈 Market:</b>\n"
        "<code>/price TCS</code>, <code>/analyze TCS</code>, <code>/scan</code>\n"
        "<code>/market</code>, <code>/nifty200</code>\n"
        "<code>/watchlist</code>, <code>/add SYM</code>, <code>/remove SYM</code>\n"
        "<code>/alert TCS above 4300</code>, <code>/alerts</code>, <code>/delalerts</code>\n"
        "<code>/holidays</code>, <code>/addholiday 2026-12-31</code>\n\n"
        "<b>💼 Paper Trading:</b>\n"
        "<code>/positions</code>, <code>/ledger</code>, <code>/trades</code>\n"
        "<code>/backtest</code> ya <code>/backtest TCS</code> - strategy test\n"
        "<code>/resetaccount</code>\n\n"
        "<b>🏏⚽ Sports:</b>\n"
        "<code>/score</code>, <code>/cricket</code>, <code>/cricket India</code>, "
        "<code>/football</code>, <code>/football Real Madrid</code>\n\n"
        "<b>🌦️ Extra:</b> <code>/weather Mumbai</code>, <code>/setcity Delhi</code>, <code>/news</code>, "
        "<code>/summary</code>\n\n"
        "<b>⚙️ System:</b> <code>/status</code> (bot on/uptime), <code>/pause</code>, "
        "<code>/resume</code>, <code>/myid</code>\n\n"
        "💡 Commands ki zaroorat nahi, seedha likhein: <i>\"bot chal raha hai?\"</i>, "
        "<i>\"Mumbai ka mausam\"</i>, <i>\"market kaisa hai\"</i>, <i>\"TCS analysis\"</i>, "
        "<i>\"news\"</i>, <i>\"IPL score\"</i>, <i>\"Barcelona match\"</i>"
    ), parse_mode="HTML")

@bot.message_handler(commands=['scan'])
@owner_only
def manual_scan(message):
    bot.send_chat_action(message.chat.id, 'typing')
    run_screener_task("MANUAL SCAN", chat_id=message.chat.id)

@bot.message_handler(commands=['price'])
@owner_only
def price_cmd(message):
    sym = arg_of(message).upper().split()[0] if arg_of(message) else ""
    if not sym:
        bot.reply_to(message, "Use: /price TCS")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    d = get_market_data(sym)
    if not d:
        bot.reply_to(message, f"❌ {sym} ka data nahi mila. Symbol check karein.")
        return
    stale = "" if d["fresh"] else "\n⚠️ Market band hai, last available data dikha raha hoon."
    bot.reply_to(message, (
        f"📌 <b>{esc(sym)}</b>: ₹{d['price']} ({d['change_pct']:+.2f}%)\n"
        f"High/Low: {d['day_high']} / {d['day_low']}\n"
        f"EMA9/21: {d['ema9']} / {d['ema21']}\nVWAP: {d['vwap']}\n"
        f"Volume ratio: {d['vol_ratio']}x\nSignal: <b>{get_signal(d)}</b>\n"
        f"Last candle: {d['last_candle']}{stale}"
    ), parse_mode="HTML")

@bot.message_handler(commands=['analyze'])
@owner_only
def analyze_cmd(message):
    a = arg_of(message)
    if not a:
        bot.reply_to(message, "Use: /analyze TCS")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    do_analysis(message.chat.id, a.upper().split()[0])

@bot.message_handler(commands=['watchlist'])
@owner_only
def watchlist_cmd(message):
    with LOCK:
        wl = ", ".join(state["watchlist"])
    bot.reply_to(message, f"👀 Watchlist: {wl}")

@bot.message_handler(commands=['add'])
@owner_only
def add_cmd(message):
    a = arg_of(message)
    if not a:
        bot.reply_to(message, "Use: /add SBIN")
        return
    sym = a.upper().split()[0]
    if not get_market_data(sym):
        bot.reply_to(message, f"❌ {sym} valid nahi lag raha.")
        return
    with LOCK:
        if sym not in state["watchlist"]:
            state["watchlist"].append(sym)
            save_state()
    bot.reply_to(message, f"✅ {sym} watchlist mein add ho gaya.")

@bot.message_handler(commands=['remove'])
@owner_only
def remove_cmd(message):
    a = arg_of(message)
    if not a:
        bot.reply_to(message, "Use: /remove SBIN")
        return
    sym = a.upper().split()[0]
    with LOCK:
        if sym in state["watchlist"]:
            state["watchlist"].remove(sym)
            save_state()
            bot.reply_to(message, f"🗑️ {sym} hata diya.")
        else:
            bot.reply_to(message, "Ye watchlist mein nahi hai.")

@bot.message_handler(commands=['alert'])
@owner_only
def alert_cmd(message):
    parts = message.text.split()
    try:
        sym, direction, price = parts[1].upper(), parts[2].lower(), float(parts[3])
        assert direction in ("above", "below")
    except Exception:
        bot.reply_to(message, "Use: /alert TCS above 4300  ya  /alert TCS below 4100")
        return
    with LOCK:
        state["price_alerts"].append({"symbol": sym, "direction": direction, "price": price})
        save_state()
    bot.reply_to(message, f"🔔 Alert set: {sym} {direction} ₹{price}")

@bot.message_handler(commands=['alerts'])
@owner_only
def alerts_cmd(message):
    with LOCK:
        al = state["price_alerts"]
        txt = "\n".join(f"• {a['symbol']} {a['direction']} ₹{a['price']}" for a in al) or "Koi alert nahi."
    bot.reply_to(message, "🔔 Price Alerts:\n" + txt)

@bot.message_handler(commands=['delalerts'])
@owner_only
def delalerts_cmd(message):
    with LOCK:
        state["price_alerts"] = []
        save_state()
    bot.reply_to(message, "🗑️ Saare price alerts hata diye.")

@bot.message_handler(commands=['positions'])
@owner_only
def show_positions(message):
    with LOCK:
        pos = dict(state["account"]["positions"])
    if not pos:
        bot.reply_to(message, "ℹ️ Abhi koi open position nahi hai.")
        return
    lines = ["📌 <b>ACTIVE POSITIONS</b>\n"]
    for sym, p in pos.items():
        d = get_market_data(sym)
        live = ""
        if d:
            pnl = (d["price"] - p["entry_price"]) * p["qty"] * (1 if p["side"] == "BUY" else -1)
            live = f"\n  Live: ₹{d['price']} | Gross P&L: ₹{pnl:+.2f}"
        lines.append(f"• <b>{esc(sym)}</b> ({p['side']}) Qty {p['qty']}\n"
                     f"  Entry: ₹{p['entry_price']} | Target: ₹{p['target']} | SL: ₹{p['sl']}{live}\n")
    send(message.chat.id, "\n".join(lines), html=True)

@bot.message_handler(commands=['ledger'])
@owner_only
def show_ledger(message):
    with LOCK:
        a, ds = state["account"], state["day_stats"]
        wr = (a.get("wins", 0) / a["trades_count"] * 100) if a["trades_count"] else 0
        msg = ("📊 <b>PAPER TRADING LEDGER</b>\n─────────────────────\n"
               f"• Capital: ₹{a['capital']:,.2f}\n• Available Cash: ₹{a['available_cash']:,.2f}\n"
               f"• Trades: {a['trades_count']} (Win rate: {wr:.0f}%)\n"
               f"• Net Realized P&L: <b>₹{a['realized_pnl']:+,.2f}</b>\n"
               f"• Total Charges: ₹{a.get('charges_total', 0):,.2f}\n"
               f"• Open Positions: {len(a['positions'])}\n"
               f"─────────────────────\n"
               f"📅 Aaj: {ds.get('opened', 0)} trades | P&L ₹{ds.get('pnl', 0):+,.2f}\n"
               f"🛡️ Risk: daily loss limit ₹{DAILY_MAX_LOSS:,.0f}, max {MAX_TRADES_PER_DAY} trades/din, "
               f"{MAX_POSITIONS} positions")
    bot.reply_to(message, msg, parse_mode="HTML")

@bot.message_handler(commands=['trades'])
@owner_only
def trades_cmd(message):
    with LOCK:
        h = state["trade_history"][-10:]
    if not h:
        bot.reply_to(message, "Abhi tak koi closed trade nahi.")
        return
    lines = ["🧾 Last trades (net, charges ke baad):"] + [
        f"• {t['date']} {t['symbol']} {t['side']} ₹{t['entry']}→₹{t['exit']} Net ₹{t['pnl']:+.2f}" for t in h]
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=['resetaccount'])
@owner_only
def reset_account(message):
    with LOCK:
        state["account"] = copy.deepcopy(DEFAULT_STATE["account"])
        state["trade_history"] = []
        state["last_exit"] = {}
        state["day_stats"] = copy.deepcopy(DEFAULT_STATE["day_stats"])
        save_state()
    bot.reply_to(message, "♻️ Virtual account reset ho gaya (₹1,00,000).")

@bot.message_handler(commands=['backtest'])
@owner_only
def backtest_cmd(message):
    a = arg_of(message)
    syms = [a.upper().split()[0]] if a else None
    if syms is None:
        with LOCK:
            syms = list(state["watchlist"])[:5]
    bot.reply_to(message, f"🧪 Backtest chal raha hai ({', '.join(syms)})... thoda time lagega.")
    bot.send_chat_action(message.chat.id, 'typing')
    results = []
    for s in syms:
        try:
            r = backtest_symbol(s)
            if r:
                results.append(r)
        except Exception as e:
            print(f"Backtest error {s}: {e}")
    if not results:
        send(message.chat.id, "❌ Backtest ke liye data nahi mila.")
        return
    send(message.chat.id, backtest_text(results), html=True)

@bot.message_handler(commands=['news'])
@owner_only
def manual_news(message):
    bot.send_chat_action(message.chat.id, 'typing')
    send_news(message.chat.id)

@bot.message_handler(commands=['summary'])
@owner_only
def news_summary(message):
    bot.send_chat_action(message.chat.id, 'typing')
    _, raw = fetch_bulletin_news(8)
    prompt = ("In headlines ka simple Hinglish mein 6-8 bullet summary do, aur market par kya asar pad sakta "
              "hai wo bhi batao:\n" + "\n".join(raw))
    send(message.chat.id, ask_groq(message.chat.id, prompt, remember=False))

@bot.message_handler(commands=['ping', 'status', 'uptime'])
@owner_only
def check_status(message):
    bot.reply_to(message, status_text(), parse_mode="HTML")

@bot.message_handler(commands=['weather'])
@owner_only
def weather_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    city = arg_of(message) or state.get("city", "Delhi")
    send(message.chat.id, get_weather(city), html=True)

@bot.message_handler(commands=['setcity'])
@owner_only
def setcity_cmd(message):
    a = arg_of(message)
    if not a:
        bot.reply_to(message, "Use: /setcity Mumbai")
        return
    with LOCK:
        state["city"] = a
        save_state()
    bot.reply_to(message, f"✅ Default city: {a}")

@bot.message_handler(commands=['market'])
@owner_only
def market_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    market_report(message.chat.id)

@bot.message_handler(commands=['nifty200'])
@owner_only
def nifty200_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    market_report(message.chat.id, top_n=10)

@bot.message_handler(commands=['cricket'])
@owner_only
def cricket_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    send_sport(message.chat.id, "cricket", arg_of(message))

@bot.message_handler(commands=['football', 'soccer'])
@owner_only
def football_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    send_sport(message.chat.id, "football", arg_of(message))

@bot.message_handler(commands=['score', 'scores'])
@owner_only
def score_cmd(message):
    bot.send_chat_action(message.chat.id, 'typing')
    send_sport(message.chat.id, "both", arg_of(message))

@bot.message_handler(commands=['holidays'])
@owner_only
def holidays_cmd(message):
    send(message.chat.id, holidays_text(), html=True)

@bot.message_handler(commands=['addholiday'])
@owner_only
def addholiday_cmd(message):
    a = arg_of(message)
    try:
        datetime.date.fromisoformat(a)
    except Exception:
        bot.reply_to(message, "Use: /addholiday 2026-12-31 (YYYY-MM-DD)")
        return
    with LOCK:
        if a not in state["extra_holidays"]:
            state["extra_holidays"].append(a)
            save_state()
    bot.reply_to(message, f"✅ {a} holiday mein add ho gaya.")

@bot.message_handler(commands=['pause'])
@owner_only
def pause_alerts(message):
    global ALERTS_ACTIVE
    ALERTS_ACTIVE = False
    save_state()
    bot.reply_to(message, "⏸️ Auto alerts pause (paper trading engine background mein chalta rahega).")

@bot.message_handler(commands=['resume'])
@owner_only
def resume_alerts(message):
    global ALERTS_ACTIVE
    ALERTS_ACTIVE = True
    save_state()
    bot.reply_to(message, "▶️ Auto alerts resume.")

@bot.message_handler(commands=['clear'])
@owner_only
def clear_chat(message):
    chat_memory.pop(message.chat.id, None)
    bot.reply_to(message, "🧹 Chat memory clear ho gayi.")

@bot.message_handler(func=lambda m: True)
@owner_only
def handle_chat(message):
    if not message.text:
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        if route_intent(message):
            return
    except Exception as e:
        print(f"Route error: {e}")
    send(message.chat.id, ask_groq(message.chat.id, message.text))

# ================= 19. STARTUP =================
def start_background():
    for target in (run_web_server, keep_alive_loop, remote_sync_loop, heartbeat_loop, trading_loop,
                   price_alert_loop, scheduler_loop, load_nifty200):
        threading.Thread(target=target, daemon=True).start()

if __name__ == "__main__":
    print("Hermes All-in-One Started...")
    start_background()
    if CHAT_ID:
        send(CHAT_ID, "🚀 Hermes Engine ONLINE ✅\n"
                      f"Start time: {START_TIME.strftime('%d-%b-%Y %H:%M')} IST\n"
                      + (DOWNTIME_NOTE + "\n" if DOWNTIME_NOTE else "")
                      + f"Save mode: {PERSIST_MODE}\n"
                      + "\nHelp ke liye /help likhein. Kuch bhi pooch sakte hain 🙂")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=30)
        except Exception as err:
            print(f"Polling error: {err}")
            time.sleep(5)
