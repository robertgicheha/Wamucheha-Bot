"""
Read-only connectivity check for every service in .env.

Places NO real orders (Binance uses its /order/test validation endpoint) and
sends NO messages. Run it on the VPS after deploying — OKX keys are
IP-whitelisted, so results from another machine can differ:

    python scripts/check_connections.py

Quota note: the RapidAPI NSE check deliberately hits /health, never /stocks.
The Basic plan allows only 4 requests/hour and 250/month, and /stocks is
reserved for the single daily call in long_term/scheduler.py.
"""
import os
import sys
import json
import smtplib
from pathlib import Path

import requests
import ccxt
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
E = lambda k: (os.environ.get(k) or "").strip()
T = 20


def run(name, fn):
    try:
        print(f"[PASS] {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:300]}")


def nonzero(bal):
    return {k: v for k, v in (bal.get("total") or {}).items() if v}


# ---------- Binance ----------
bn = ccxt.binance({"apiKey": E("BINANCE_API_KEY"), "secret": E("BINANCE_API_SECRET"), "enableRateLimit": True})
run("Binance public ticker", lambda: bn.fetch_ticker("BTC/USDT")["last"])
run("Binance auth + balance", lambda: nonzero(bn.fetch_balance()) or "authenticated, all balances 0")


def bn_perms():
    r = bn.sapiGetAccountApiRestrictions()
    return {k: r.get(k) for k in ("ipRestrict", "enableReading", "enableSpotAndMarginTrading",
                                   "enableWithdrawals", "enableFutures")}
run("Binance API key permissions", bn_perms)


def bn_test_order():
    bn.load_markets()
    m = bn.market("BTC/USDT")
    price = bn.fetch_ticker("BTC/USDT")["last"]
    amt = float(bn.amount_to_precision("BTC/USDT", max(11 / price, m["limits"]["amount"]["min"] or 0)))
    # hits /api/v3/order/test — validated by Binance, never executed
    bn.create_order("BTC/USDT", "market", "buy", amt, params={"test": True})
    return f"test market BUY {amt} BTC (~$11) accepted by /order/test (NOT executed)"
run("Binance test order (validation only)", bn_test_order)

# ---------- OKX ----------
ok = ccxt.okx({"apiKey": E("OKX_API_KEY"), "secret": E("OKX_API_SECRET"),
               "password": E("OKX_PASSPHRASE") or E("OKX_API_PASSPHRASE"), "enableRateLimit": True})
run("OKX public ticker", lambda: ok.fetch_ticker("BTC/USDT")["last"])
run("OKX auth + balance", lambda: nonzero(ok.fetch_balance()) or "authenticated, all balances 0")


def ok_perms():
    d = ok.privateGetAccountConfig()["data"][0]
    return {k: d.get(k) for k in ("perm", "ip", "acctLv", "label")}
run("OKX API key permissions", ok_perms)

# ---------- MT5 (native on Windows, or the mt5 container via MT5_RPC_HOST) ----------
def mt5():
    from core.mt5_client import get_mt5
    m = get_mt5()
    if not m.initialize():
        raise RuntimeError(f"initialize failed: {m.last_error()}")
    if not m.login(int(E("MT5_LOGIN")), password=E("MT5_PASSWORD"), server=E("MT5_SERVER")):
        raise RuntimeError(f"login failed: {m.last_error()}")
    a = m.account_info()
    m.symbol_select("XAUUSD", True)
    t = m.symbol_info_tick("XAUUSD")
    return (f"{a.login} @ {a.server} balance={a.balance} leverage=1:{a.leverage} "
            f"trade_allowed={getattr(a, 'trade_allowed', '?')} XAUUSD bid={t.bid if t else 'n/a'}")
run("MT5", mt5)

# ---------- OANDA ----------
def oanda():
    host = "api-fxpractice.oanda.com" if E("OANDA_PRACTICE").lower() == "true" else "api-fxtrade.oanda.com"
    r = requests.get(f"https://{host}/v3/accounts/{E('OANDA_ACCOUNT_ID')}/summary",
                     headers={"Authorization": f"Bearer {E('OANDA_API_KEY')}"}, timeout=T)
    r.raise_for_status(); a = r.json()["account"]
    return f"{host} balance={a['balance']} {a['currency']}"
run("OANDA", oanda)

# ---------- Alpaca ----------
def alpaca():
    host = "paper-api.alpaca.markets" if E("ALPACA_PAPER").lower() == "true" else "api.alpaca.markets"
    r = requests.get(f"https://{host}/v2/account", timeout=T,
                     headers={"APCA-API-KEY-ID": E("ALPACA_API_KEY"), "APCA-API-SECRET-KEY": E("ALPACA_API_SECRET")})
    r.raise_for_status(); a = r.json()
    return f"{host} status={a['status']} equity={a['equity']}"
run("Alpaca", alpaca)

# ---------- Data providers ----------
def av():
    j = requests.get("https://www.alphavantage.co/query", timeout=T,
                     params={"function": "GLOBAL_QUOTE", "symbol": "IBM", "apikey": E("ALPHA_VANTAGE_KEY")}).json()
    if "Global Quote" not in j: raise RuntimeError(j)
    return "ok"
run("Alpha Vantage", av)

def fmp():
    r = requests.get("https://financialmodelingprep.com/stable/quote", timeout=T,
                     params={"symbol": "AAPL", "apikey": E("FMP_API_KEY")})
    r.raise_for_status(); return f"ok ({len(r.json())} rows)"
run("FMP", fmp)

def news():
    r = requests.get("https://newsapi.org/v2/top-headlines", timeout=T,
                     params={"country": "us", "pageSize": 1, "apiKey": E("NEWSAPI_KEY")})
    j = r.json()
    if j.get("status") != "ok": raise RuntimeError(j.get("message"))
    return "ok"
run("NewsAPI", news)

# ---------- NSE Kenya ----------
# Checks the RapidAPI NSE subscription, NOT the /stocks endpoint. /stocks is the
# one quota-consuming call (4/hour, 250/month) and this script must never spend
# it, so plan validity is verified against /health and a live fetch is left to
# the 16:00 EAT job.
NSE_HOST = "nairobi-stock-exchange-nse.p.rapidapi.com"
NSE_HDRS = {"x-rapidapi-key": E("NSE_RAPIDAPI_KEY"), "x-rapidapi-host": NSE_HOST}

def nse_rapidapi():
    if not E("NSE_RAPIDAPI_KEY"):
        return "SKIP - NSE_RAPIDAPI_KEY not set (falls back to afx.kwayisi.org)"
    r = requests.get(f"https://{NSE_HOST}/health", headers=NSE_HDRS, timeout=T)
    if r.status_code in (401, 403):
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]} - "
                           "key is wrong or the plan is not subscribed")
    r.raise_for_status()
    return f"ok (no /stocks call - that is the daily job's budget)"

run("RapidAPI NSE", nse_rapidapi)

def nse_afx():
    # Free fallback source: also supplies every NSE fundamental (P/E, EPS, DPS).
    r = requests.get("https://afx.kwayisi.org/nse/", timeout=T,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return "ok" if "<table" in r.text.lower() else "reachable but no tables found"
run("NSE fundamentals (afx.kwayisi.org)", nse_afx)

def nse_cache():
    f = ROOT / "data" / "nse_cache" / "rapidapi_snapshot.json"
    if not f.exists():
        return "no snapshot yet - run the 16:00 EAT job, or: python -c \"from data_feeds.nse_feed import NSEFeed; NSEFeed().refresh_market_snapshot()\""
    j = json.loads(f.read_text())
    return (f"{len(j.get('stocks', []))} securities, trading_date={j.get('trading_date')}, "
            f"fetched_at={j.get('fetched_at')}")
run("NSE persisted snapshot", nse_cache)

# ---------- Alerts / control bots (no messages sent) ----------
def tg():
    j = requests.get(f"https://api.telegram.org/bot{E('TELEGRAM_BOT_TOKEN')}/getMe", timeout=T).json()
    if not j.get("ok"): raise RuntimeError(j)
    c = requests.get(f"https://api.telegram.org/bot{E('TELEGRAM_BOT_TOKEN')}/getChat",
                     params={"chat_id": E("TELEGRAM_CHAT_ID")}, timeout=T).json()
    return f"bot=@{j['result']['username']}, chat_id reachable={c.get('ok')}"
run("Telegram bot + chat", tg)

def dwh():
    r = requests.get(E("DISCORD_WEBHOOK_URL"), timeout=T); r.raise_for_status()
    return f"webhook '{r.json().get('name')}' valid"
run("Discord webhook (GET only)", dwh)

def dbot():
    r = requests.get("https://discord.com/api/v10/users/@me", timeout=T,
                     headers={"Authorization": f"Bot {E('DISCORD_BOT_TOKEN')}"})
    r.raise_for_status(); return f"bot={r.json()['username']}"
run("Discord control bot", dbot)

def smtp():
    with smtplib.SMTP(E("EMAIL_SMTP_HOST"), int(E("EMAIL_SMTP_PORT")), timeout=T) as s:
        s.starttls(); s.login(E("EMAIL_ADDRESS"), E("EMAIL_APP_PASSWORD"))
    return "login ok (nothing sent)"
run("Gmail SMTP", smtp)

def vps():
    r = requests.get(E("VPS_HEARTBEAT_URL"), timeout=10)
    return f"HTTP {r.status_code} {r.text[:120]}"
run("VPS heartbeat URL", vps)
