import os
import time
import html
import threading
import datetime
from collections import deque

import pytz
import telebot
import requests
import schedule
import feedparser
from flask import Flask

import screener
import paper
import weather

# ================= 1. FLASK SERVER & SELF-PING =================
server = Flask(__name__)


@server.route('/')
def home():
    return "Hermes AI & Market Engine Active 24/7!"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    server.run(host="0.0.0.0", port=port)


threading.Thread(target=run_flask, daemon=True).start()


def keep_alive_self_ping():
    """Render free tier ko jagaye rakhne ki koshish (asli bharosa UptimeRobot ka 5-min ping)."""
    time.sleep(30)
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    while True:
        try:
            if render_url:
                requests.get(render_url, timeout=10)
            else:
                port = int(os.environ.get("PORT", 10000))
                requests.get(f"http://127.0.0.1:{port}/", timeout=10)
        except Exception as e:
            print(f"[PING ERROR] {e}")
        time.sleep(600)


threading.Thread(target=keep_alive_self_ping, daemon=True).start()

# ================= 2. CONFIG =================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("LLM_API_KEY")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

bot = telebot.TeleBot(BOT_TOKEN)
IST = pytz.timezone("Asia/Kolkata")


def authorized(message):
    """Sirf aapka chat bot use kar sake."""
    return (not CHAT_ID) or str(message.chat.id) == str(CHAT_ID)


def send_long(chat_id, text, **kw):
    for i in range(0, len(text), 4000):
        bot.send_message(chat_id, text[i:i + 4000], **kw)


def reply_long(message, text):
    """AI ka plain-text jawab; lamba ho to tukdon me."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["(khali jawab)"]
    bot.reply_to(message, chunks[0])
    for c in chunks[1:]:
        bot.send_message(message.chat.id, c)


# ================= 3. NEWS (cache ke saath) =================
NEWS_FEEDS = {
    "📊 Indian Market News": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en",
    "🇮🇳 India Top News": "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-IN&gl=IN&ceid=IN:en",
    "🌍 World News": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
}
_NEWS_CACHE = {"time": 0, "data": {}}


def get_news_raw(max_age=0):
    """Har category ki 10 headlines. max_age second se purana cache ho to dobara fetch."""
    if max_age and _NEWS_CACHE["data"] and time.time() - _NEWS_CACHE["time"] < max_age:
        return _NEWS_CACHE["data"]
    data = {}
    for category, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            data[category] = [e.title.rsplit(" - ", 1)[0].strip() for e in feed.entries[:10]]
        except Exception as e:
            data[category] = [f"Error fetching news: {e}"]
    if any(data.values()):
        _NEWS_CACHE.update(time=time.time(), data=data)
    return data


def fetch_bulletin_news():
    sections = []
    for category, titles in get_news_raw().items():
        lines = [f"<b>{category}</b>"] + [f"{i}. {html.escape(t)}" for i, t in enumerate(titles, 1)]
        sections.append("\n".join(lines))
    return sections


def news_context():
    data = get_news_raw(max_age=1800)
    return "\n".join(f"{cat}: " + " | ".join(t[:110] for t in titles[:6]) for cat, titles in data.items())


def send_scheduled_news():
    if not CHAT_ID:
        return
    try:
        bot.send_message(CHAT_ID, "📰 <b>TAAZA NEWS BULLETIN (Har 3 Ghante)</b>", parse_mode="HTML")
        for sec in fetch_bulletin_news():
            send_long(CHAT_ID, sec, parse_mode="HTML")
            time.sleep(1)
    except Exception as e:
        print(f"News dispatch error: {e}")


# ================= 4. SCREENER =================
def run_screener_task(label="DAILY SCAN", chat_id=None):
    chat_id = chat_id or CHAT_ID
    if not chat_id:
        return
    now_ist = datetime.datetime.now(IST).strftime("%d-%m-%Y %H:%M")
    try:
        results, total = screener.run_screener(top_n=10)
        send_long(chat_id, screener.format_results(results, total, f"{label} | {now_ist} IST"),
                  parse_mode="HTML")
    except Exception as e:
        print(f"Screener error: {e}")
        try:
            bot.send_message(chat_id, f"⚠️ Screener error: {e}")
        except Exception:
            pass


# ================= 5. VIRTUAL (PAPER) TRADING JOBS =================
def _send_paper(text):
    if text and CHAT_ID:
        try:
            send_long(CHAT_ID, text, parse_mode="HTML")
        except Exception as e:
            print(f"Paper dispatch error: {e}")


def paper_open_job():
    try:
        _send_paper(paper.open_positions())
    except Exception as e:
        print(f"Paper open error: {e}")


def paper_check_job():
    try:
        for m in paper.check_positions():
            _send_paper(m)
    except Exception as e:
        print(f"Paper check error: {e}")


def paper_squareoff_job():
    try:
        _send_paper(paper.square_off())
    except Exception as e:
        print(f"Paper squareoff error: {e}")


# ================= 6. WEATHER JOB =================
def weather_job():
    if not CHAT_ID:
        return
    try:
        bot.send_message(CHAT_ID, "🌅 <b>SUBAH KA MAUSAM</b>\n" + weather.format_html(), parse_mode="HTML")
    except Exception as e:
        print(f"Weather dispatch error: {e}")


# ================= 7. HERMES AI (data + memory) =================
HISTORY = {}   # chat_id -> pichle messages (memory)
KW_NEWS = ("news", "khabar", "samachar", "headline", "market", "nifty", "sensex", "duniya", "world", "india")
KW_SCAN = ("scan", "screener", "stock", "share", "top", "kaun", "best", "condition", "score", "entry", "nifty")
KW_PAPER = ("trade", "portfolio", "virtual", "pnl", "profit", "loss", "position", "paper", "nuksan", "fayda")
KW_WEATHER = ("weather", "mausam", "barish", "baarish", "rain", "garmi", "sardi", "temperature", "temp", "dhoop")

SYSTEM_PROMPT = (
    "You are Hermes, a sharp trading and reasoning assistant inside a Telegram bot. "
    "Reply in simple Hinglish, crisp and structured. "
    "The user's message may include a [DATA] block with live bot data (news, screener results, "
    "virtual trades, weather). Use ONLY that data for prices, scores and facts; if the needed data "
    "is missing, say so and suggest the right command (/scan, /news, /pnl, /weather). "
    "Never invent prices or news. Never promise profits; you give analysis, not financial advice."
)


def build_context(text):
    """Sawal ke hisaab se sirf zaroori live data (token bachane ke liye)."""
    t = text.lower()
    parts = [f"Abhi ka time: {datetime.datetime.now(IST).strftime('%d-%m-%Y %H:%M')} IST"]
    blocks = [
        (KW_NEWS, "TAAZA NEWS", news_context),
        (KW_SCAN, "SCREENER (pichla scan)", screener.last_summary),
        (KW_PAPER, "VIRTUAL TRADING", paper.context_text),
        (KW_WEATHER, "MAUSAM", weather.context_text),
    ]
    for kws, title, fn in blocks:
        if any(k in t for k in kws):
            try:
                parts.append(f"{title}:\n{fn()}")
            except Exception as e:
                parts.append(f"{title}: data nahi mila ({e})")
    return "\n\n".join(parts)


def ask_groq(chat_id, user_text):
    hist = HISTORY.setdefault(chat_id, deque(maxlen=10))
    try:
        ctx = build_context(user_text)
    except Exception:
        ctx = ""
    messages = ([{"role": "system", "content": SYSTEM_PROMPT}] + list(hist) +
                [{"role": "user", "content": f"[DATA]\n{ctx}\n[/DATA]\n\nSawal: {user_text}"}])
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.4, "max_tokens": 800},
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            timeout=30)
        data = r.json()
        if data.get("choices"):
            reply = data["choices"][0]["message"]["content"]
            hist.append({"role": "user", "content": user_text})      # memory me sirf asli baat
            hist.append({"role": "assistant", "content": reply})
            return reply
        return f"Groq Error: {data.get('error', data)}"
    except Exception as e:
        return f"Groq Request Failed: {e}"


# ================= 8. SCHEDULER (IST timezone) =================
def setup_timings():
    schedule.every(3).hours.do(send_scheduled_news)
    schedule.every().day.at("07:00", "Asia/Kolkata").do(weather_job)
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        getattr(schedule.every(), day).at("09:00", "Asia/Kolkata").do(run_screener_task, label="MORNING SCAN")
        getattr(schedule.every(), day).at("15:35", "Asia/Kolkata").do(run_screener_task, label="EOD SCAN")
        getattr(schedule.every(), day).at("09:20", "Asia/Kolkata").do(paper_open_job)
        getattr(schedule.every(), day).at("15:15", "Asia/Kolkata").do(paper_squareoff_job)
    schedule.every(5).minutes.do(paper_check_job)  # market hours ke bahar khud skip
    while True:
        try:
            schedule.run_pending()
        except Exception as e:  # ek job fail hone par scheduler band na ho
            print(f"[SCHEDULER ERROR] {e}")
        time.sleep(30)


threading.Thread(target=setup_timings, daemon=True).start()


# ================= 9. TELEGRAM HANDLERS =================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not authorized(message):
        return
    bot.reply_to(
        message,
        "⚡ <b>Hermes AI Active!</b>\n\n"
        "• Market, strategy, news, screener, virtual trades ya mausam, kuch bhi puchein. "
        "Main live data dekh kar jawab dunga aur pichli baat yaad rakhunga.\n"
        "• Auto messages: news har 3 ghante, mausam roz 7 AM, screener 9:00 AM / 3:35 PM, "
        "virtual trade alerts market time mein.\n\n"
        "Commands:\n"
        "/news - abhi taaza news\n"
        "/scan - Top 10 stocks screener\n"
        "/check TCS - ek stock ki 9 conditions\n"
        "/weather [shehar] - mausam\n"
        "/portfolio - virtual positions\n"
        "/pnl - virtual trading report\n"
        "/resetpaper - virtual account reset\n"
        "/clear - Hermes ki memory saaf",
        parse_mode="HTML",
    )


@bot.message_handler(commands=['news'])
def manual_news(message):
    if not authorized(message):
        return
    bot.send_chat_action(message.chat.id, 'typing')
    for sec in fetch_bulletin_news():
        send_long(message.chat.id, sec, parse_mode="HTML")
        time.sleep(1)


@bot.message_handler(commands=['scan'])
def manual_scan(message):
    if not authorized(message):
        return
    bot.reply_to(message, "⏳ Scan chal raha hai, 1-2 minute lagenge...")
    run_screener_task(label="MANUAL SCAN", chat_id=message.chat.id)


@bot.message_handler(commands=['check'])
def check_stock(message):
    if not authorized(message):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Aise likhein: /check TCS")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        bot.reply_to(message, screener.check_symbol(parts[1]), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")


@bot.message_handler(commands=['weather'])
def cmd_weather(message):
    if not authorized(message):
        return
    city = " ".join(message.text.split()[1:]).strip() or None
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        bot.reply_to(message, weather.format_html(city), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Mausam nahi mil paya: {e}")


@bot.message_handler(commands=['portfolio'])
def cmd_portfolio(message):
    if authorized(message):
        bot.send_chat_action(message.chat.id, 'typing')
        send_long(message.chat.id, paper.portfolio(), parse_mode="HTML")


@bot.message_handler(commands=['pnl'])
def cmd_pnl(message):
    if authorized(message):
        send_long(message.chat.id, paper.report(), parse_mode="HTML")


@bot.message_handler(commands=['resetpaper'])
def cmd_resetpaper(message):
    if authorized(message):
        bot.reply_to(message, paper.reset())


@bot.message_handler(commands=['clear'])
def cmd_clear(message):
    if authorized(message):
        HISTORY.pop(message.chat.id, None)
        bot.reply_to(message, "🧹 Hermes ki memory saaf ho gayi.")


@bot.message_handler(func=lambda msg: True)
def handle_hermes_chat(message):
    if not authorized(message) or not message.text:
        return
    bot.send_chat_action(message.chat.id, 'typing')
    reply_long(message, ask_groq(message.chat.id, message.text))


# ================= 10. MAIN =================
if __name__ == "__main__":
    print("Hermes Engine Started...")
    bot.remove_webhook()
    if CHAT_ID:  # restart hone par pata chal jaye
        try:
            bot.send_message(CHAT_ID, "🟢 Hermes online hai. (/start se commands dekhein)")
        except Exception as e:
            print(f"Startup message error: {e}")
    bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
