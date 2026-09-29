"""
Pre-flight check before going live with real money.

Answers one question: if you flipped LIVE_TRADING=true right now, what would
actually happen? It resolves every venue, mode flag, key and funding gap, and
exits non-zero if anything is unsafe. Read-only: places no orders, moves no
funds, writes no state.

    .venv\\Scripts\\python.exe scripts/preflight.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

E = lambda k: (os.environ.get(k) or "").strip()
CFG = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))

# Per-venue requirements that are NOT simply "<NAME>_API_KEY". Each entry is
# (env var, what breaks if it is wrong).
VENUE_RULES = {
    "binance": [("BINANCE_API_KEY", "no market data or orders"),
                ("BINANCE_API_SECRET", "no orders")],
    "okx":     [("OKX_API_KEY", "no market data or orders"),
                ("OKX_API_SECRET", "no orders"),
                ("OKX_PASSPHRASE", "OKX rejects every request without it")],
    "bybit":   [("BYBIT_API_KEY", "no market data or orders"),
                ("BYBIT_API_SECRET", "no orders")],
    "kraken":  [("KRAKEN_API_KEY", "no market data or orders"),
                ("KRAKEN_API_SECRET", "no orders")],
    "coinbase": [("COINBASE_API_KEY", "no market data or orders"),
                 ("COINBASE_API_SECRET", "no orders")],
    "oanda":   [("OANDA_API_KEY", "no forex data or orders"),
                ("OANDA_ACCOUNT_ID", "OANDA is skipped entirely without it")],
    "alpaca":  [("ALPACA_API_KEY", "no US equity data or orders"),
                ("ALPACA_API_SECRET", "no orders")],
    "mt5":     [("MT5_LOGIN", "MT5 is skipped entirely without it"),
                ("MT5_PASSWORD", "login fails"),
                ("MT5_SERVER", "login fails")],
}

# Venues whose funds are NOT USDT-in-a-wallet. Flagged so nobody tries to fund
# an MT5 account from MetaMask.
NON_CRYPTO_FUNDING = {
    "mt5": "broker deposit (card / bank / M-Pesa), NOT from MetaMask",
    "alpaca": "bank or wire transfer, NOT from MetaMask",
}

BLOCKERS: list[str] = []
WARNINGS: list[str] = []


def blocker(msg: str):
    BLOCKERS.append(msg)
    print(f"  [BLOCK] {msg}")


def warn(msg: str):
    WARNINGS.append(msg)
    print(f"  [WARN]  {msg}")


def ok(msg: str):
    print(f"  [ok]    {msg}")


def mode_of(name: str) -> tuple[str, str]:
    """Return (mode, why) for a venue, or ('n/a', ...) if not configured."""
    if name in ("binance", "okx", "kraken", "coinbase", "bybit"):
        if not E(f"{name.upper()}_API_KEY"):
            return "unconfigured", "no API key"
        return "LIVE", "ccxt venue; dry-run is the only sandbox and LIVE_TRADING gates it"
    if name == "oanda":
        practice = E("OANDA_PRACTICE").lower() == "true"
        return ("practice" if practice else "LIVE", "OANDA_PRACTICE=false targets api-fxtrade")
    if name == "alpaca":
        paper = E("ALPACA_PAPER").lower() == "true"
        return ("paper" if paper else "LIVE", "ALPACA_PAPER=false targets api.alpaca.markets")
    if name == "mt5":
        server = E("MT5_SERVER")
        if "demo" in server.lower():
            return "demo", f"server '{server}' is a demo server"
        return "LIVE", f"server '{server}' is not a demo server"
    return "unknown", ""


def main() -> int:
    live = E("LIVE_TRADING").lower() == "true"
    print("=" * 78)
    print(f"PRE-FLIGHT   LIVE_TRADING={'true  <-- REAL MONEY' if live else 'false (dry-run)'}")
    print("=" * 78)

    # ---------- 1. mode flags that are independent of LIVE_TRADING ----------
    print("\n[1] Mode flags")
    if live:
        for name in ("oanda", "alpaca", "mt5"):
            if any(e.get("name") == name and e.get("enabled")
                   for e in CFG["execution"]["exchanges"]):
                m, why = mode_of(name)
                if m == "LIVE":
                    blocker(f"{name.upper()} is enabled AND live ({why}) — "
                            f"real orders on the first signal")
                else:
                    ok(f"{name.upper()} is enabled but {m} ({why})")
    else:
        ok("LIVE_TRADING=false — no real orders can be placed regardless of other flags")
        for name in ("oanda", "alpaca"):
            if E(f"{name.upper()}_PRACTICE" if name == "oanda" else f"{name.upper()}_PAPER").lower() == "false":
                warn(f"{name.upper()} points at a LIVE endpoint, but LIVE_TRADING=false "
                     f"keeps it in dry-run. Set the flag true, or the venue disabled, "
                     f"before ever setting LIVE_TRADING=true.")
    mt5_server = E("MT5_SERVER")
    if mt5_server and "demo" not in mt5_server.lower():
        warn(f"MT5_SERVER='{mt5_server}' is not a demo server — MT5 would trade real money")

    # Clock skew is a real cause of InvalidNonce on Binance and OKX, and it
    # fails every signed request at once, which reads like a bad key.
    try:
        import requests
        skew = abs(requests.get("https://api.binance.com/api/v3/time",
                                timeout=10).json()["serverTime"] / 1000 - time.time())
        if skew > 30:
            blocker(f"local clock is {skew:.0f}s away from exchange time — every "
                    f"signed request will fail with InvalidNonce. Fix NTP before "
                    f"trading; this is not a key problem")
        else:
            ok(f"clock within {skew:.0f}s of exchange time")
    except Exception:
        warn("could not reach an exchange clock endpoint to verify time sync")

    # ---------- 2. venues ----------
    print("\n[2] Venues")
    enabled = [e for e in CFG["execution"]["exchanges"] if e.get("enabled")]
    for ex in enabled:
        name = ex["name"]
        missing = [f"{v} ({why})" for v, why in VENUE_RULES.get(name, []) if not E(v)]
        if missing:
            blocker(f"{name}: missing " + ", ".join(missing))
        else:
            m, why = mode_of(name)
            markets = ex.get("markets") or []
            ok(f"{name}: keys present, mode={m}, markets={len(markets)}")
            if name in NON_CRYPTO_FUNDING:
                warn(f"{name} is funded by {NON_CRYPTO_FUNDING[name]}")

    disabled = [e["name"] for e in CFG["execution"]["exchanges"] if not e.get("enabled")]
    if disabled:
        ok(f"disabled in config (no executor built): {', '.join(disabled)}")

    # ---------- 3. markets resolve ----------
    print("\n[3] Market symbols")
    total = 0
    for ex in enabled:
        for sym in ex.get("markets") or []:
            total += 1
    print(f"  {total} crypto/ccxt symbols configured")
    for key, label in (("oanda_markets", "oanda"), ("mt5_markets", "mt5"),
                       ("alpaca_markets", "alpaca")):
        syms = CFG["execution"].get(key) or []
        is_enabled = any(e.get("name") == label and e.get("enabled")
                         for e in CFG["execution"]["exchanges"])
        if syms and not is_enabled:
            warn(f"{label}: {len(syms)} symbols listed in execution.{key} but the "
                 f"venue is enabled:false, so they will not be traded")
        elif syms:
            ok(f"{label}: {len(syms)} symbols")

    # ---------- 4. funding ----------
    print("\n[4] Funding (manual — the bot never moves money)")
    # A .env value can carry a trailing "# comment". Strip it, or a placeholder
    # like "# your 0x... treasury address" reads back as a real address.
    def clean(var: str) -> str:
        v = E(var)
        if "#" in v:
            v = v.split("#", 1)[0].strip()
        return v

    wallet = clean("METAMASK_WALLET_ADDRESS")
    network = clean("METAMASK_NETWORK").upper()
    if not wallet:
        warn("METAMASK_WALLET_ADDRESS is empty — set your treasury 0x... address "
             "for the funding checklist. Not required for trading.")
    else:
        if wallet.startswith("0x") and len(wallet) == 42:
            ok(f"treasury wallet recorded: {wallet[:10]}...{wallet[-6:]}")
        else:
            warn(f"METAMASK_WALLET_ADDRESS is not a valid 0x address: '{wallet}'")
    if network:
        fees = {"TRC20": "~1 USDT", "BEP20": "~0.1-1 USDT",
                "ERC20": "~5-20 USDT (avoid for bulk)"}
        if network in fees:
            ok(f"deposit network: {network} ({fees[network]})")
            if network == "ERC20":
                warn("ERC20 is expensive; TRC20 or BEP20 is usual for bulk USDT transfers")
        else:
            warn(f"METAMASK_NETWORK='{network}' is not a known USDT network "
                 f"(expected TRC20, BEP20 or ERC20)")
    for name in ("binance", "okx", "bybit"):
        v = clean(f"FUNDING_{name.upper()}_USDT")
        if v:
            ok(f"{name} funding target: {v} USDT")
    unfunded = [n for n in ("binance", "okx", "bybit")
                if not clean(f"FUNDING_{n.upper()}_USDT")]
    if unfunded:
        warn(f"no FUNDING_*_USDT target set for: {', '.join(unfunded)}. "
             f"These are documentation only — you move money in MetaMask.")

    # ---------- 5. IP allowlist ----------
    # Both Binance and OKX reject any request from an address that is not on
    # the key's allowlist, and the bot only works from the VPS. Checking from a
    # laptop will report a misleading failure, so say so rather than let it
    # look like a bad key.
    print("\n[5] Where are you running this?")
    try:
        import requests
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        ip = None
    if ip:
        print(f"  public IP: {ip}")
        if os.environ.get("EXPECTED_VPS_IP"):
            if os.environ["EXPECTED_VPS_IP"].strip() == ip:
                ok("this matches EXPECTED_VPS_IP — the venue allowlists should match")
            else:
                blocker(f"running from {ip} but EXPECTED_VPS_IP is "
                        f"{os.environ['EXPECTED_VPS_IP']} — venue API keys are "
                        f"allowlisted to the VPS, so trading will fail here")
        else:
            warn("EXPECTED_VPS_IP is not set in .env. Add the VPS public IP so "
                 "this check can catch you testing from the wrong machine.")
    for name in ("binance", "okx", "bybit"):
        if any(e.get("name") == name and e.get("enabled")
               for e in CFG["execution"]["exchanges"]):
            warn(f"{name}: confirm the VPS IP is on the key's allowlist, and that "
                 f"the key is TRADE-ONLY with withdrawal disabled")

    # ---------- 6. credentials hygiene ----------
    print("\n[6] Credential hygiene")
    if E("RAPID_API_KEY") and E("RAPID_API_KEY") == E("NEWSAPI_KEY"):
        blocker("RAPID_API_KEY is a duplicate of NEWSAPI_KEY — a known-invalid "
                "credential. Remove it; use NSE_RAPIDAPI_KEY for RapidAPI.")
    for var in ("BINANCE_API_SECRET", "OKX_API_SECRET", "BYBIT_API_SECRET",
                "OANDA_API_KEY", "ALPACA_API_SECRET", "MT5_PASSWORD"):
        if E(var) and len(E(var)) < 8:
            blocker(f"{var} looks truncated ({len(E(var))} chars)")
    ok("no truncated secrets")

    if (ROOT / ".env").exists():
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                                 capture_output=True, text=True).stdout
        if ".env" in tracked.split():
            blocker(".env IS TRACKED BY GIT — rotate every secret in it now")
        else:
            ok(".env is untracked (gitignored)")

    # ---------- verdict ----------
    print("\n" + "=" * 78)
    if BLOCKERS:
        print(f"NOT READY: {len(BLOCKERS)} blocker(s), {len(WARNINGS)} warning(s)")
        for b in BLOCKERS:
            print(f"  - {b}")
        if live:
            print("\nLIVE_TRADING IS TRUE. Fix the blockers above before the next signal.")
        return 1

    print(f"NO BLOCKERS ({len(WARNINGS)} warning(s))")
    if not live:
        print("\nStill in dry-run. To go live with real money:")
        print("  1. Fund each enabled venue (see docs/FUNDING.md)")
        print("  2. Confirm withdrawal is DISABLED and the VPS IP is whitelisted")
        print("  3. Re-run this script ON the VPS — exchange keys only work there")
        print("  4. Set LIVE_TRADING=true in .env and restart")
    else:
        print("\nLIVE_TRADING=true — real orders will be placed on the next signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
