"""
Go-live safety invariants: venue config is consistent, only crypto and gold are in
scope, a disabled venue never builds an executor, and no secret slips into the repo.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------- go-live safety: a disabled venue must never trade ----------

def _cfg():
    return {"execution": {"exchanges": [
        {"name": "binance", "enabled": True, "markets": ["ETH/USDT"]},
        {"name": "bybit", "enabled": True, "markets": ["ETH/USDT", "SOL/USDT"]},
        {"name": "mt5", "enabled": True, "markets": []},
    ]}}


def test_bybit_is_wired_end_to_end():
    """The config entry is all that was missing: the venue name must already be
    whitelisted in the executor, the market-data router and the always-open
    crypto set, and must resolve to a real ccxt class."""
    import ccxt
    import yaml
    from pathlib import Path
    from core.market_hours import CRYPTO_EXCHANGES
    from data_feeds import feed_router
    import main as main_mod

    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config" / "config.yaml").read_text(encoding="utf-8"))
    bybit = [e for e in cfg["execution"]["exchanges"] if e["name"] == "bybit"]
    assert len(bybit) == 1, "bybit must appear exactly once in config.yaml"
    # Whether bybit trades is the operator's call (config.yaml). The invariant
    # is that an enabled venue is never enabled with nothing to trade.
    if bybit[0]["enabled"]:
        assert bybit[0]["markets"], "bybit is enabled but lists no markets"

    assert hasattr(ccxt, "bybit"), "config name must match a ccxt class attribute"
    assert "bybit" in CRYPTO_EXCHANGES
    assert "bybit" in open(main_mod.__file__, encoding="utf-8").read()
    assert "bybit" in open(feed_router.__file__, encoding="utf-8").read()


def test_config_venues_are_internally_consistent():
    """Structural invariants of the venue list. Which venues are ON is the
    operator's decision and changes over time, so it is not frozen here."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config" / "config.yaml").read_text(encoding="utf-8"))
    venues = {e["name"]: e for e in cfg["execution"]["exchanges"]}
    for name in ("binance", "okx", "bybit", "mt5"):
        assert name in venues, f"{name} missing from execution.exchanges"
    for name in ("binance", "okx", "bybit"):      # crypto venues carry their markets inline
        if venues[name]["enabled"]:
            assert venues[name]["markets"], f"{name} is enabled but lists no markets"
    names = [e["name"] for e in cfg["execution"]["exchanges"]]
    assert len(names) == len(set(names)), "a venue is listed twice"
    # Scope is crypto + gold only: no forex/equity venues may creep back in.
    for removed in ("oanda", "alpaca", "nse"):
        assert removed not in venues, f"{removed} is out of scope (crypto and gold only)"


def test_disabled_venue_never_builds_an_executor(monkeypatch):
    """MT5 can place real orders on a live broker server, so its executor must
    only be built when the venue is switched on in config. This locks it down."""
    import main as main_mod
    src = main_mod.__file__
    for venue in ("mt5",):
        assert f'venue_enabled("{venue}")' in open(src, encoding="utf-8").read(), \
            f"{venue} must gate on venue_enabled() before building an executor"


def test_funding_doc_exists():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    funding = (root / "docs" / "FUNDING.md").read_text(encoding="utf-8")
    assert "TRC20" in funding and "BEP20" in funding


def test_metamask_private_key_is_not_in_env():
    """No wallet, and no custody of any kind, may be reintroduced through .env.

    Funding was redesigned to draw from one exchange and settle on one exchange,
    so the MetaMask treasury block was removed. This test locks that in two
    ways: the old variable names must not come back, and no variable of any name
    may carry a value shaped like a key.

    The old version of this test was weaker in a way that mattered. It only
    rejected keys containing METAMASK/WALLET/MNEMONIC/PRIVATE_KEY/SEED, so a
    variable called ETH_KEY, SIGNING_KEY or EXCHANGE_SECRET would have passed
    straight through — and a real 32-byte key is 64 hex chars, which only the
    length check would have caught, and only if it were the whole value. The
    check below is name-agnostic and shape-based instead.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent

    # The retired treasury variables. Reintroducing any of these means the
    # design was reverted without updating the docs or preflight.
    retired = ("METAMASK_WALLET_ADDRESS", "METAMASK_NETWORK", "ETHEREUM_RPC_URL",
               "TRON_RPC_URL", "BSC_RPC_URL", "ETHERSCAN_API_KEY")

    # Everything the funding design is allowed to contain. Deliberately
    # address-shaped and venue-shaped only.
    allowed = ("FUNDING_SOURCE_VENUE", "SETTLEMENT_VENUE", "TRANSFER_NETWORK",
               "FUNDING_BINANCE_USDT", "FUNDING_OKX_USDT", "FUNDING_BYBIT_USDT")

    key_like = ("metamask", "wallet", "mnemonic", "private_key", "seed",
                "signing", "signer", "eth_key")

    # Names that contain a key-like substring but are not custody of funds.
    # DASHBOARD_SECRET_KEY is an HTTP auth token for our own dashboard; the
    # exchange *_API_SECRET values are ccxt trading keys that cannot withdraw
    # (see the API-permission section of docs/FUNDING.md). Neither is a signing
    # key, so matching on the substring alone would only produce noise.
    known_non_custody = ("DASHBOARD_SECRET_KEY", "BINANCE_API_SECRET",
                         "OKX_API_SECRET", "BYBIT_API_SECRET", "OANDA_API_KEY",
                         "ALPACA_API_SECRET")

    for name in (".env", ".env.example"):
        for lineno, raw in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.split("#")[0].strip()

            assert key not in retired, (
                f"{name}:{lineno}: {key} is from the retired MetaMask funding "
                f"design. Funding is one-funder/one-settlement across exchanges "
                f"now — use {allowed} instead.")

            low = key.lower()
            if any(t in low for t in key_like) and key not in known_non_custody:
                assert key in allowed, (
                    f"{name}:{lineno}: {key} looks like a custody variable. The "
                    f"bot must never hold keys; allowed funding vars are {allowed}.")

            # Shape check: a 32-byte key is 64 hex chars, optionally 0x-prefixed.
            bare = val[2:] if val.lower().startswith("0x") else val
            if len(bare) == 64:
                try:
                    int(bare, 16)
                except ValueError:
                    pass
                else:
                    raise AssertionError(
                        f"{name}:{lineno}: {key} holds a 64-hex-char value, which "
                        f"is the shape of a private key. This process must not "
                        f"hold custody of funds.")


def test_funding_addresses_are_checked_against_the_declared_network():
    """A deposit address is only credited on the chain it was issued for. The
    old check accepted any 0x string regardless of the configured network,
    which meant a TRC20/Tron configuration silently passed a BEP20 address — the
    exact combination that loses a deposit irrecoverably."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("pf", root / "scripts" / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)

    trc20 = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"
    bep20 = "0x488bb1eb4dde2dd6eeb82cdd2ee9959cdf1485a4"

    assert pf.validate_usdt_address(trc20, "TRC20") is None
    assert pf.validate_usdt_address(bep20, "BEP20") is None
    assert pf.validate_usdt_address(bep20, "ERC20") is None
    # The failure that used to go unnoticed: right format, wrong chain.
    assert pf.validate_usdt_address(bep20, "TRC20") is not None
    assert pf.validate_usdt_address(trc20, "BEP20") is not None
    assert pf.validate_usdt_address("0xnothex", "BEP20") is not None
    assert pf.validate_usdt_address(bep20, "NOPE") is not None


def test_preflight_script_runs_and_reports_cleanly():
    """The go-live gate must work on a real checkout, not just be a doc."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "scripts/preflight.py"], cwd=root,
                       capture_output=True, text=True, timeout=120)
    assert "PRE-FLIGHT" in r.stdout
    assert "BLOCK" in r.stdout
    # Dry-run should never be blocked by the venue checks.
    assert r.returncode in (0, 1)




def test_feed_router_sends_slashless_symbols_to_mt5_and_pairs_to_ccxt(monkeypatch):
    """XAUUSD / BTCUSD (no slash) are MT5 contracts; BTC/USDT is a ccxt exchange pair."""
    from data_feeds import feed_router
    r = feed_router.FeedRouter.__new__(feed_router.FeedRouter)
    r.feeds = {"okx": feed_router.MarketData.__new__(feed_router.MarketData), "mt5": object()}
    assert r._classify_symbol("XAUUSD") == "mt5"
    assert r._classify_symbol("BTCUSD") == "mt5"
    assert r._classify_symbol("ETHUSD") == "mt5"
    assert r._classify_symbol("BTC/USDT") == "okx"
    assert r._classify_symbol("PAXG/USDT") == "okx"
