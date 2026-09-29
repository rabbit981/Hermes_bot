"""
Stock screener: Yahoo Finance (yfinance) se data leke NSE stocks ko 9 conditions par score karta hai.
Sirf educational / informational use ke liye. Ye investment advice nahi hai.
"""
import datetime
import html
import logging
import threading

import pandas as pd
import pytz
import yfinance as yf

log = logging.getLogger("screener")

# Nifty 50 + kuch liquid Next-50 / midcap stocks. Yahoo par jo ticker na mile, wo apne aap skip ho jata hai.
UNIVERSE = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JIOFIN",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUMER", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
    "ABB", "AMBUJACEM", "BANKBARODA", "BOSCHLTD", "CANBK", "CHOLAFIN", "COFORGE",
    "DABUR", "DIXON", "DLF", "GAIL", "GODREJCPL", "HAL", "HAVELLS", "IOC", "INDIGO",
    "JINDALSTEL", "LICI", "LODHA", "MAXHEALTH", "MUTHOOTFIN", "NAUKRI", "PERSISTENT",
    "PFC", "PIDILITIND", "PNB", "POLYCAB", "RECLTD", "SIEMENS", "SRF", "TORNTPHARM",
    "TVSMOTOR", "VEDL", "ZYDUSLIF", "BSE", "IRCTC", "UNITDSPR",
]
BENCHMARK = "^NSEI"

_lock = threading.Lock()
LAST = {"time": None, "top": [], "total": 0}   # Hermes ke liye pichle scan ka cache
_IST = pytz.timezone("Asia/Kolkata")


def _drop_today(df):
    """Aaj ka adhoora daily candle hatata hai (subah scan me volume sahi check ho)."""
    today = datetime.datetime.now(_IST).date()
    d = df.dropna(subset=["Close"])
    if len(d) and d.index[-1].date() == today:
        return d.iloc[:-1]
    return df


# ---------------- Indicators ----------------
def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def analyze(df, nifty_ret):
    """Ek stock ka 9-condition score. Data kam ho to None."""
    df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < 210:
        return None

    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    ema20, ema50, ema200 = _ema(c, 20), _ema(c, 50), _ema(c, 200)
    rsi = float(_rsi(c).iloc[-1])
    macd = _ema(c, 12) - _ema(c, 26)
    hist = macd - _ema(macd, 9)
    vol_avg = float(v.iloc[-21:-1].mean())
    vol_ratio = float(v.iloc[-1]) / vol_avg if vol_avg > 0 else 0.0
    high20 = float(h.iloc[-20:].max())
    price = float(c.iloc[-1])
    ret1m = price / float(c.iloc[-22]) - 1
    atr = float(_atr(h, l, c).iloc[-1])

    conds = {
        "Price>EMA20": price > float(ema20.iloc[-1]),
        "EMA20>EMA50": float(ema20.iloc[-1]) > float(ema50.iloc[-1]),
        "Price>EMA200": price > float(ema200.iloc[-1]),
        "RSI 50-70": 50 <= rsi <= 70,
        "MACD>Signal": float(hist.iloc[-1]) > 0,
        "MACD Rising": float(hist.iloc[-1]) > float(hist.iloc[-2]),
        "Volume>1.5x": vol_ratio >= 1.5,
        "Near 20D High": price >= 0.97 * high20,
        "Beats Nifty(1M)": ret1m > nifty_ret,
    }
    return {
        "price": price,
        "rsi": rsi,
        "vol_ratio": vol_ratio,
        "ret1m": ret1m * 100,
        "sl": price - 1.5 * atr,
        "target": price + 3 * atr,
        "conds": conds,
        "score": sum(conds.values()),
    }


def _nifty_return(data):
    try:
        n = data[BENCHMARK]["Close"].dropna()
        return float(n.iloc[-1] / n.iloc[-22] - 1)
    except Exception:
        return 0.0


# ---------------- Public API ----------------
def run_screener(top_n=10, drop_today=False):
    """Poori universe scan karke top_n stocks return karta hai: (results, total_analyzed)."""
    with _lock:  # ek time par ek hi scan
        tickers = [s + ".NS" for s in UNIVERSE] + [BENCHMARK]
        data = yf.download(
            tickers, period="1y", interval="1d", group_by="ticker",
            auto_adjust=True, progress=False, threads=True,
        )
        nifty_ret = _nifty_return(data)
        results = []
        for sym in UNIVERSE:
            try:
                df = data[sym + ".NS"]
                if drop_today:
                    df = _drop_today(df)
                res = analyze(df, nifty_ret)
            except Exception as e:
                log.warning("skip %s: %s", sym, e)
                continue
            if res:
                res["symbol"] = sym
                results.append(res)
        results.sort(key=lambda r: (r["score"], r["vol_ratio"]), reverse=True)
        if results:
            LAST.update(time=datetime.datetime.now(_IST).strftime("%d-%m-%Y %H:%M"),
                        top=results[:max(top_n, 10)], total=len(results))
        return results[:top_n], len(results)


def format_results(results, total, label):
    if not results:
        return "⚠️ Yahoo Finance se data nahi mila. Thodi der baad /scan try karein."
    lines = [f"🎯 <b>TOP {len(results)} STOCKS [{html.escape(label)}]</b>",
             f"Scanned: {total} stocks | 9 conditions", "─────────────────────"]
    for i, r in enumerate(results, 1):
        missing = [k for k, ok in r["conds"].items() if not ok]
        miss_txt = ("❌ " + ", ".join(missing)) if missing else "✅ Sab conditions pass"
        lines.append(
            f"<b>{i}. {html.escape(r['symbol'])}</b>  ₹{r['price']:.2f}  |  Score {r['score']}/9\n"
            f"   RSI {r['rsi']:.0f} | Vol {r['vol_ratio']:.1f}x | 1M {r['ret1m']:+.1f}%\n"
            f"   SL ₹{r['sl']:.2f} | Target ₹{r['target']:.2f}\n"
            f"   {html.escape(miss_txt)}"
        )
    lines.append("─────────────────────")
    lines.append("⚠️ Sirf technical scan hai, investment advice nahi. Apni research zaroor karein.")
    return "\n".join(lines)


def check_symbol(symbol):
    """Ek single stock ko saari conditions par check karke text return karta hai."""
    symbol = symbol.upper().replace(".NS", "").strip()
    data = yf.download(
        [symbol + ".NS", BENCHMARK], period="1y", interval="1d", group_by="ticker",
        auto_adjust=True, progress=False, threads=True,
    )
    try:
        res = analyze(data[symbol + ".NS"], _nifty_return(data))
    except Exception:
        res = None
    if not res:
        return f"❌ {html.escape(symbol)} ka data nahi mila (NSE symbol sahi likhein, jaise TCS, RELIANCE)."
    rows = [f"{'✅' if ok else '❌'} {k}" for k, ok in res["conds"].items()]
    return (
        f"🔍 <b>{html.escape(symbol)}</b>  ₹{res['price']:.2f}  |  Score {res['score']}/9\n"
        f"RSI {res['rsi']:.0f} | Vol {res['vol_ratio']:.1f}x | 1M {res['ret1m']:+.1f}%\n"
        f"SL ₹{res['sl']:.2f} | Target ₹{res['target']:.2f}\n\n" + "\n".join(rows)
    )


def last_summary():
    """Pichle scan ka chhota plain-text summary (Hermes AI ke context ke liye)."""
    if not LAST["top"]:
        return "Abhi tak koi scan nahi chala (user /scan chala sakta hai)."
    lines = [f"Scan time {LAST['time']} IST, {LAST['total']} stocks me se top 10 (score 9 conditions me se):"]
    for r in LAST["top"][:10]:
        missing = [k for k, ok in r["conds"].items() if not ok]
        lines.append(f"{r['symbol']}: price {r['price']:.2f}, score {r['score']}/9, RSI {r['rsi']:.0f}, "
                     f"vol {r['vol_ratio']:.1f}x, 1M {r['ret1m']:+.1f}%, SL {r['sl']:.2f}, "
                     f"target {r['target']:.2f}, fail: {', '.join(missing) or 'none'}")
    return "\n".join(lines)
