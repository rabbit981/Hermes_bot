"""
Virtual (paper) intraday trading. Koi asli order nahi jata.
Screener ke top stocks par 9:20 AM par virtual BUY, SL/Target 5-min candles se check,
3:15 PM par square-off, aur stats se pata chalta hai konsi condition kaam kar rahi hai.
"""
import datetime
import html
import json
import os
import threading

import pandas as pd
import pytz
import yfinance as yf

import screener

IST = pytz.timezone("Asia/Kolkata")
STATE_FILE = os.environ.get("PAPER_STATE_FILE", "paper_state.json")
START_CAPITAL = float(os.environ.get("PAPER_CAPITAL", "100000"))
MAX_POSITIONS = int(os.environ.get("PAPER_MAX_POS", "5"))
MIN_SCORE = int(os.environ.get("PAPER_MIN_SCORE", "6"))      # 9 me se kam se kam itni conditions pass
COST_PCT = float(os.environ.get("PAPER_COST_PCT", "0.05")) / 100  # har side brokerage+slippage ka andaza

_lock = threading.RLock()


# ---------------- helpers ----------------
def _now():
    return datetime.datetime.now(IST)


def _fresh_state():
    return {"cash": START_CAPITAL, "open": [], "closed": []}


def _load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return _fresh_state()


def _save(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def market_open(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 30)


def _fetch(symbols):
    """Aaj ki 5-minute candles (IST tz ke saath)."""
    if not symbols:
        return {}
    tickers = [s + ".NS" for s in symbols]
    data = yf.download(tickers, period="1d", interval="5m", group_by="ticker",
                       auto_adjust=True, progress=False, threads=True)
    out = {}
    for s in symbols:
        try:
            df = data[s + ".NS"].dropna(subset=["Close", "High", "Low"])
        except Exception:
            continue
        if df.empty:
            continue
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(IST)
        out[s] = df
    return out


def _complete(df, now):
    """Sirf poori ho chuki 5-min candles."""
    return df[df.index + pd.Timedelta(minutes=5) <= now]


def _close(st, pos, price, reason, now):
    qty = pos["qty"]
    buy_cost = qty * pos["entry"] * (1 + COST_PCT)
    proceeds = qty * price * (1 - COST_PCT)
    pnl = proceeds - buy_cost
    st["cash"] += proceeds
    st["open"].remove(pos)
    st["closed"].append({
        **pos, "exit": round(price, 2), "reason": reason,
        "exit_time": now.isoformat(), "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / buy_cost * 100, 2),
    })
    icon = "✅" if pnl > 0 else "🔴"
    return (f"{icon} <b>EXIT {html.escape(pos['symbol'])}</b> ({reason})\n"
            f"Entry ₹{pos['entry']:.2f} → Exit ₹{price:.2f} | Qty {qty}\n"
            f"P&L: ₹{pnl:+.2f} ({pnl / buy_cost * 100:+.2f}%)")


# ---------------- main actions ----------------
def open_positions():
    """9:20 AM: screener ke top stocks me virtual BUY."""
    now = _now()
    with _lock:
        st = _load()
        if st["open"]:
            return "⚠️ Purani virtual positions khuli hain, naye entry skip."
        results, _ = screener.run_screener(top_n=MAX_POSITIONS * 3, drop_today=True)
        picks = [r for r in results if r["score"] >= MIN_SCORE][:MAX_POSITIONS]
        if not picks:
            return f"📭 Aaj koi stock {MIN_SCORE}/9 score par nahi mila. Virtual trade nahi liya."
        data = _fetch([p["symbol"] for p in picks])
        per_trade = st["cash"] / MAX_POSITIONS
        lines = ["🧪 <b>VIRTUAL ENTRIES</b> (paper trading)", "─────────────────────"]
        for p in picks:
            df = data.get(p["symbol"])
            if df is None:
                continue
            df = _complete(df, now)
            if df.empty or df.index[-1].date() != now.date():
                continue
            entry = float(df["Close"].iloc[-1])
            qty = int(per_trade // (entry * (1 + COST_PCT)))
            if qty < 1:
                continue
            risk = max(p["price"] - p["sl"], entry * 0.005)
            reward = max(p["target"] - p["price"], risk * 2)
            pos = {
                "symbol": p["symbol"], "qty": qty, "entry": round(entry, 2),
                "sl": round(entry - risk, 2), "target": round(entry + reward, 2),
                "entry_time": now.isoformat(),
                "last_checked": df.index[-1].isoformat(),
                "score": p["score"], "conds": p["conds"],
            }
            st["cash"] -= qty * entry * (1 + COST_PCT)
            st["open"].append(pos)
            lines.append(f"🟢 <b>BUY {html.escape(p['symbol'])}</b> ₹{entry:.2f} × {qty} | Score {p['score']}/9\n"
                         f"   SL ₹{pos['sl']:.2f} | Target ₹{pos['target']:.2f}")
        if not st["open"]:
            return "📭 Aaj market data nahi mila (holiday ya data delay). Virtual entry nahi hui."
        _save(st)
        return "\n".join(lines)


def check_positions():
    """Har 5 minute: SL/Target hit hua ya nahi. Messages ki list return karta hai."""
    now = _now()
    if not market_open(now):
        return []
    with _lock:
        st = _load()
        if not st["open"]:
            return []
        data = _fetch([p["symbol"] for p in st["open"]])
        msgs = []
        for pos in list(st["open"]):
            df = data.get(pos["symbol"])
            if df is None:
                continue
            df = _complete(df, now)
            new = df[df.index > pd.Timestamp(pos["last_checked"])]
            for ts, row in new.iterrows():
                if float(row["Low"]) <= pos["sl"]:          # SL pehle (conservative)
                    msgs.append(_close(st, pos, pos["sl"], "STOPLOSS", now))
                    break
                if float(row["High"]) >= pos["target"]:
                    msgs.append(_close(st, pos, pos["target"], "TARGET", now))
                    break
                pos["last_checked"] = ts.isoformat()
        _save(st)
        return msgs


def square_off():
    """3:15 PM: bachi hui saari virtual positions band."""
    now = _now()
    with _lock:
        st = _load()
        if not st["open"]:
            return None
        data = _fetch([p["symbol"] for p in st["open"]])
        msgs = ["⏰ <b>3:15 PM SQUARE-OFF</b>"]
        for pos in list(st["open"]):
            df = data.get(pos["symbol"])
            price = float(df["Close"].iloc[-1]) if df is not None else pos["entry"]
            msgs.append(_close(st, pos, price, "SQUAREOFF", now))
        _save(st)
        today = now.date().isoformat()
        day = [t for t in st["closed"] if t["exit_time"][:10] == today]
        total = sum(t["pnl"] for t in day)
        msgs.append(f"─────────────────────\n📅 Aaj ka virtual P&L: ₹{total:+.2f} ({len(day)} trades)")
        return "\n".join(msgs)


def portfolio():
    now = _now()
    with _lock:
        st = _load()
        realized = sum(t["pnl"] for t in st["closed"])
        lines = [f"💼 <b>VIRTUAL PORTFOLIO</b>",
                 f"Cash: ₹{st['cash']:.2f} | Realized P&L: ₹{realized:+.2f}",
                 "─────────────────────"]
        if not st["open"]:
            lines.append("Koi khuli position nahi.")
            return "\n".join(lines)
        data = _fetch([p["symbol"] for p in st["open"]])
        for p in st["open"]:
            df = data.get(p["symbol"])
            ltp = float(df["Close"].iloc[-1]) if df is not None else p["entry"]
            upnl = p["qty"] * (ltp - p["entry"])
            lines.append(f"<b>{html.escape(p['symbol'])}</b> ₹{p['entry']:.2f} → ₹{ltp:.2f} | "
                         f"P&L ₹{upnl:+.2f}\n   SL ₹{p['sl']:.2f} | Target ₹{p['target']:.2f}")
        return "\n".join(lines)


def report():
    """Overall stats + condition-wise win rate, taaki pata chale konsi condition kaam ki."""
    st = _load()
    closed = st["closed"]
    if not closed:
        return "Abhi koi band virtual trade nahi hai. Kuch din trading chalne dein."
    n = len(closed)
    wins = [t for t in closed if t["pnl"] > 0]
    total = sum(t["pnl"] for t in closed)
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in closed if t["pnl"] <= 0]
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    lines = [
        "📊 <b>VIRTUAL TRADING REPORT</b>",
        f"Trades: {n} | Win rate: {len(wins) / n * 100:.0f}%",
        f"Total P&L: ₹{total:+.2f}",
        f"Avg win ₹{avg_win:+.2f} | Avg loss ₹{avg_loss:+.2f}",
        "─────────────────────",
        "<b>Score ke hisaab se win rate</b>",
    ]
    by_score = {}
    for t in closed:
        by_score.setdefault(t["score"], []).append(t)
    for s in sorted(by_score, reverse=True):
        g = by_score[s]
        lines.append(f"Score {s}/9: {sum(x['pnl'] > 0 for x in g) / len(g) * 100:.0f}% ({len(g)} trades)")
    lines += ["─────────────────────", "<b>Condition pass hone par win rate</b>"]
    names = list(closed[0]["conds"].keys())
    for name in names:
        g = [t for t in closed if t["conds"].get(name)]
        if g:
            lines.append(f"{html.escape(name)}: {sum(x['pnl'] > 0 for x in g) / len(g) * 100:.0f}% ({len(g)})")
    if n < 30:
        lines.append(f"\n⚠️ Abhi sirf {n} trades hain. Kam se kam 30-50 trades ke baad hi natije par bharosa karein.")
    return "\n".join(lines)


def reset():
    with _lock:
        _save(_fresh_state())
    return f"♻️ Virtual account reset. Naya capital ₹{START_CAPITAL:.0f}."


def context_text():
    """Virtual trading ka chhota plain-text summary (Hermes AI ke context ke liye). Network use nahi karta."""
    st = _load()
    closed = st["closed"]
    lines = [f"Virtual cash Rs {st['cash']:.0f}"]
    if st["open"]:
        for p in st["open"]:
            lines.append(f"Khuli: {p['symbol']} entry {p['entry']} qty {p['qty']} SL {p['sl']} "
                         f"target {p['target']} score {p['score']}/9")
    else:
        lines.append("Abhi koi khuli virtual position nahi.")
    if closed:
        wins = sum(t["pnl"] > 0 for t in closed)
        lines.append(f"Band trades {len(closed)}, win rate {wins / len(closed) * 100:.0f}%, "
                     f"total P&L Rs {sum(t['pnl'] for t in closed):+.0f}")
        for t in closed[-5:]:
            lines.append(f"Pichli trade: {t['symbol']} {t['reason']} P&L Rs {t['pnl']:+.0f} score {t['score']}/9")
    else:
        lines.append("Abhi koi band virtual trade nahi.")
    return "\n".join(lines)
