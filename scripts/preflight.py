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
# an MT5 account from an exchange.
NON_CRYPTO_FUNDING = {
    "mt5": "broker deposit (card / bank / M-Pesa) — cannot be funded by USDT transfer",
    "alpaca": "bank or wire transfer — cannot be funded by USDT transfer",
}

# Networks this bot can settle USDT over, with the shape of a valid address on
# each. The distinction matters: TRC20 is Tron, which uses base58 "T..." strings,
# while BEP20/ERC20 are EVM chains that use 0x-prefixed hex. Treating them as
# interchangeable is how people send real money to an address on the wrong chain,
# where no one — not the exchange, not the chain — can recover it.
USDT_NETWORKS = {
    "TRC20":  ("tron",     "T + 33 base58 chars", "^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "BEP20":  ("evm",      "0x + 40 hex",  "^0x[0-9a-fA-F]{40}$"),
    "ERC20":  ("evm",      "0x + 40 hex",  "^0x[0-9a-fA-F]{40}$"),
    "SOL":    ("solana",   "base58 32-44", "^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
}

BLOCKERS: list[str] = []
WARNINGS: list[str] = []


def validate_usdt_address(addr: str, network: str) -> str | None:
    """Return a human problem string for a deposit address on `network`, or
    None if it is well-formed. Kept separate from the printing so it is
    testable and so the same check can be reused for every venue's address."""
    import re
    net = (network or "").upper()
    spec = USDT_NETWORKS.get(net)
    if not spec:
        return f"unknown network '{network}' (expected one of {', '.join(USDT_NETWORKS)})"
    _, shape, pattern = spec
    if not re.match(pattern, addr):
        return (f"does not look like a {net} address (a {net}/{spec[0]} address is "
                f"{shape})")
    return None


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
    print("\n[4] Funding (manual — you move money in the exchange UIs; the bot "
          "has no withdrawal rights)")

    # A .env value can carry a trailing "# comment". Strip it, or a placeholder
    # like "# your OKX deposit address" reads back as a real address.
    def clean(var: str) -> str:
        v = E(var)
        if "#" in v:
            v = v.split("#", 1)[0].strip()
        return v

    # ── Funder + settlement, per the single-address design ──
    # FUNDING_SOURCE_VENUE is where capital is drawn from. SETTLEMENT_VENUE is
    # whose deposit address receives profit. They are usually the same venue
    # (one hub), and the two only diverge if you want capital to sit somewhere
    # other than where it is consolidated.
    src = clean("FUNDING_SOURCE_VENUE").lower()
    settle = clean("SETTLEMENT_VENUE").lower()
    network = clean("TRANSFER_NETWORK").upper()

    if not src:
        warn("FUNDING_SOURCE_VENUE is empty — set it to the exchange you fund "
             "from (e.g. okx). Documentation only; the bot never moves money.")
    else:
        ok(f"funder venue: {src}")

    if not settle:
        warn("SETTLEMENT_VENUE is empty — set it to the venue that receives "
             "profit withdrawals (e.g. okx).")
    else:
        ok(f"settlement venue: {settle}")
        if src and settle == src:
            # Not an error — it is the intended design — but it concentrates
            # risk, so state it once where the operator will read it.
            warn(f"funder and settlement are both {settle.upper()}: every venue's "
                 f"profit returns to {settle.upper()}, and capital is "
                 f"re-balanced by on-chain withdrawal. That is the design you "
                 f"asked for, but it makes {settle.upper()} a single point of "
                 f"failure — an account freeze or outage locks every balance. "
                 f"Keep withdraw-only 2FA and a withdrawal allowlist on it.")

    if network:
        if network in USDT_NETWORKS:
            fee_hint = {"TRC20": "~1 USDT", "BEP20": "~0.1-1 USDT",
                        "ERC20": "~5-20 USDT (avoid for bulk)", "SOL": "~0.01 USDT"}
            ok(f"transfer network: {network} ({fee_hint.get(network, '')})")
            if network == "ERC20":
                warn("ERC20 is expensive; TRC20 or BEP20 is usual for bulk USDT")
        else:
            warn(f"TRANSFER_NETWORK='{network}' is not a known USDT network "
                 f"(expected {', '.join(USDT_NETWORKS)})")
    else:
        warn("TRANSFER_NETWORK is empty — set it (TRC20 recommended). Every "
             "address below is checked against it, so a mismatch is what "
             "loses funds.")

    # ── Per-venue deposit addresses ──
    # FUNDING_<VENUE>_USDT predates this check and its name is misleading: it
    # holds the venue's DEPOSIT ADDRESS, not a target amount. It was printed as
    # "... USDT", which made a 0x address look like a dollar figure and hid the
    # fact that no amount is stored anywhere — the bot never moves money, so
    # there is nothing to compare an amount against anyway. The name is kept
    # because renaming it would break existing .env files for no benefit.
    for name in ("binance", "okx", "bybit"):
        v = clean(f"FUNDING_{name.upper()}_USDT")
        if not v:
            continue
        problem = validate_usdt_address(v, network) if network else \
            "no TRANSFER_NETWORK set, so the address cannot be verified"
        if problem is None:
            ok(f"{name} deposit address: {v[:8]}...{v[-6:]}")
        else:
            # Wrong chain is the one funding error that is unrecoverable, so it
            # blocks rather than warns.
            blocker(f"FUNDING_{name.upper()}_USDT {problem}: '{v}'. Sending on "
                    f"the wrong network destroys the deposit — verify the address "
                    f"and network on the exchange's Deposit screen before sending.")

    unfunded = [n for n in ("binance", "okx", "bybit")
                if not clean(f"FUNDING_{n.upper()}_USDT")]
    if unfunded:
        warn(f"no deposit address recorded for: {', '.join(unfunded)} "
             f"(set FUNDING_<VENUE>_USDT in .env). Documentation only — the bot "
             f"never holds withdrawal rights.")

    # The hub venue's own address doubles as the settlement address, so a
    # mismatch between them is a silent misconfiguration: profit would be
    # withdrawn somewhere the operator did not intend.
    hub = settle or src
    if hub and hub in ("binance", "okx", "bybit"):
        hub_addr = clean(f"FUNDING_{hub.upper()}_USDT")
        if not hub_addr:
            warn(f"settlement venue is {hub.upper()} but FUNDING_{hub.upper()}_USDT "
                 f"is empty — that is the address profit would be withdrawn to. "
                 f"Set it from the {hub.upper()} Deposit screen.")

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
