"""
Read-only connectivity check for every service in .env.

Places NO real orders (Binance uses its /order/test validation endpoint) and
sends NO messages. Run it on the VPS after deploying — OKX keys are
IP-whitelisted, so results from another machine can differ:

    python scripts/check_connections.py

Scope: crypto exchanges (Binance/OKX/Bybit) and the MT5 gold terminal, plus optional alerts.
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

# ---------- MT5 (gold; native on Windows) ----------
def mt5():
    from core.mt5_client import get_mt5
    m = get_mt5()
    kw = {"timeout": 20000}
    if E("MT5_TERMINAL_PATH"):
        kw["path"] = E("MT5_TERMINAL_PATH")
    if not m.initialize(**kw):
        raise RuntimeError(f"initialize failed: {m.last_error()}")
    if not m.login(int(E("MT5_LOGIN")), password=E("MT5_PASSWORD"), server=E("MT5_SERVER")):
        raise RuntimeError(f"login failed: {m.last_error()}")
    a = m.account_info()
    m.symbol_select("XAUUSD", True)
    t = m.symbol_info_tick("XAUUSD")
    return (f"{a.login} @ {a.server} balance={a.balance} leverage=1:{a.leverage} "
            f"trade_allowed={getattr(a, 'trade_allowed', '?')} XAUUSD bid={t.bid if t else 'n/a'}")
run("MT5", mt5)

# ---------- Optional alerts (no messages sent) ----------
def tg():
    j = requests.get(f"https://api.telegram.org/bot{E('TELEGRAM_BOT_TOKEN')}/getMe", timeout=T).json()
    if not j.get("ok"): raise RuntimeError(j)
    c = requests.get(f"https://api.telegram.org/bot{E('TELEGRAM_BOT_TOKEN')}/getChat",
                     params={"chat_id": E("TELEGRAM_CHAT_ID")}, timeout=T).json()
    return f"bot=@{j['result']['username']}, chat_id reachable={c.get('ok')}"
run("Telegram bot + chat", tg)

def smtp():
    with smtplib.SMTP(E("EMAIL_SMTP_HOST"), int(E("EMAIL_SMTP_PORT")), timeout=T) as s:
        s.starttls(); s.login(E("EMAIL_ADDRESS"), E("EMAIL_APP_PASSWORD"))
    return "login ok (nothing sent)"
run("Gmail SMTP", smtp)
