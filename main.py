"""
Main entry point — run this on the VPS.

Wires together state, risk, execution, and alerting across multiple asset classes:
- Crypto (Binance, OKX via ccxt)
- Forex (EUR/USD via OANDA)
- Commodities (XAU/USD via OANDA)
- US Stocks/ETFs (AAPL, SPY via Alpaca)
- Kenyan Stocks (NSE — analysis/alerts only, no automated execution)

Replace `get_strategy_signal()` with your real signal logic — the risk/execution
alerting plumbing around it is the part designed to be trustworthy; the strategy
itself is where your edge (if any) has to come from, and that's on you to develop
and rigorously backtest.
"""
import os
import time
import yaml
from dotenv import load_dotenv

from core.state_manager import StateManager
from core.risk_manager import RiskManager
from core.execution_manager import ExecutionManager
from core.oanda_executor import OandaExecutor
from core.alpaca_executor import AlpacaExecutor
from alerts.notifier import Notifier
from data_feeds.feed_router import FeedRouter
from strategy.technical_strategy import generate_signal, generate_signal_with_ml
from ml.lstm_predictor import LSTMPricePredictor

load_dotenv()

with open("config/config.yaml") as f:
    CONFIG = yaml.safe_load(f)

# --- Stake amount override: .env STAKE_AMOUNT takes precedence over config.yaml ---
_env_stake = os.environ.get("STAKE_AMOUNT")
if _env_stake is not None:
    try:
        CONFIG["account"]["stake_amount"] = float(_env_stake)
        print(f"Stake amount overridden by .env: ${float(_env_stake):.2f}")
    except ValueError:
        print(f"WARNING: Invalid STAKE_AMOUNT '{_env_stake}' in .env — using config.yaml default")


def build_notifier():
    return Notifier(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"),
        email_cfg={
            "address": os.environ.get("EMAIL_ADDRESS"),
            "app_password": os.environ.get("EMAIL_APP_PASSWORD"),
            "to": os.environ.get("EMAIL_TO"),
            "smtp_host": os.environ.get("EMAIL_SMTP_HOST"),
            "smtp_port": int(os.environ.get("EMAIL_SMTP_PORT", 587)),
        },
    )


def load_ml_models(config: dict) -> dict:
    """Load trained ML models for symbols that have them. Returns
    {symbol: predictor} dict. Models that aren't trained/loaded are skipped."""
    models = {}
    symbols_to_check = []
    for ex_cfg in config.get("execution", {}).get("exchanges", []):
        if ex_cfg.get("enabled"):
            symbols_to_check.extend(ex_cfg.get("markets", []))

    for symbol in symbols_to_check:
        predictor = LSTMPricePredictor(symbol=symbol)
        if predictor.load():
            models[symbol] = predictor
            print(f"  Loaded PyTorch LSTM model for {symbol}")
    return models


def get_strategy_signal(feed_router: FeedRouter, symbol: str, trading_balance: float,
                         risk_fraction: float, ml_models: dict = None) -> dict | None:
    """
    Strategy signal with optional ML filter. If an LSTM model is available for
    this symbol, it acts as an additional conservative filter — it can only
    reduce trades, never add them.
    """
    try:
        df = feed_router.get_ohlcv(symbol, timeframe="15m", limit=200)
    except Exception as e:
        print(f"  Failed to fetch data for {symbol}: {e}")
        return None

    if df is None or len(df) < 25:
        return None

    predictor = ml_models.get(symbol) if ml_models else None
    return generate_signal_with_ml(
        df, risk_fraction, trading_balance, lstm_predictor=predictor
    )


def classify_exchange(symbol: str, config: dict) -> str:
    """Returns the exchange/broker name that should execute trades for this symbol."""
    from data_feeds.feed_router import OANDA_INSTRUMENTS, OANDA_COMMODITIES, NSE_TICKERS

    clean = symbol.upper().replace("_", "/")
    ticker = symbol.split("/")[0].split(".")[0].upper()

    if ".NSE" in symbol.upper() or ticker in NSE_TICKERS:
        return "nse"  # alert-only, no execution

    if clean in OANDA_INSTRUMENTS or clean in OANDA_COMMODITIES:
        return "oanda"

    if "/" not in symbol and ticker.isalpha() and len(ticker) <= 5:
        return "alpaca"

    # Default: first enabled ccxt exchange
    for ex_cfg in config.get("execution", {}).get("exchanges", []):
        if ex_cfg.get("enabled") and ex_cfg["name"] in ("binance", "okx", "kraken", "coinbase", "bybit"):
            return ex_cfg["name"]
    return "unknown"


def main():
    notifier = build_notifier()
    state = StateManager(stake_amount=CONFIG["account"]["stake_amount"])
    risk = RiskManager(state, CONFIG, notifier)

    # --- Initialize feed router (multi-asset data) ---
    feed_router = FeedRouter(CONFIG)
    print(f"Active feeds: {list(feed_router.get_available_feeds().keys())}")

    # --- Initialize ML models ---
    print("Loading ML models...")
    ml_models = load_ml_models(CONFIG)

    # --- Initialize executors per exchange/broker ---
    executors = {}

    # ccxt executors (Binance, OKX, etc.)
    for ex_cfg in CONFIG["execution"]["exchanges"]:
        if not ex_cfg["enabled"]:
            continue
        name = ex_cfg["name"]
        if name in ("binance", "okx", "kraken", "coinbase", "bybit"):
            api_key = os.environ.get(f"{name.upper()}_API_KEY", "")
            api_secret = os.environ.get(f"{name.upper()}_API_SECRET", "")
            passphrase = os.environ.get(f"{name.upper()}_PASSPHRASE", "")
            exchange_config = {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
            }
            if passphrase:
                exchange_config["password"] = passphrase
            # OKX needs the passphrase in the ccxt config
            executors[name] = ExecutionManager(
                exchange_id=name,
                api_key=api_key,
                api_secret=api_secret,
                state_manager=state,
                risk_manager=risk,
                notifier=notifier,
                dry_run=True,
            )

    # OANDA executor (Forex/Commodities)
    oanda_key = os.environ.get("OANDA_API_KEY")
    oanda_account = os.environ.get("OANDA_ACCOUNT_ID")
    if oanda_key and oanda_account:
        practice = os.environ.get("OANDA_PRACTICE", "true").lower() == "true"
        executors["oanda"] = OandaExecutor(
            api_key=oanda_key,
            account_id=oanda_account,
            state_manager=state,
            risk_manager=risk,
            notifier=notifier,
            practice=practice,
            dry_run=True,
        )
        print("  OANDA executor initialized (Forex/Commodities)")

    # Alpaca executor (US Stocks/ETFs)
    alpaca_key = os.environ.get("ALPACA_API_KEY")
    alpaca_secret = os.environ.get("ALPACA_API_SECRET")
    if alpaca_key and alpaca_secret:
        paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
        executors["alpaca"] = AlpacaExecutor(
            api_key=alpaca_key,
            api_secret=alpaca_secret,
            state_manager=state,
            risk_manager=risk,
            notifier=notifier,
            paper=paper,
            dry_run=True,
        )
        print("  Alpaca executor initialized (US Stocks/ETFs)")

    # Build unified market list from config
    all_markets = []
    for ex_cfg in CONFIG["execution"]["exchanges"]:
        if ex_cfg.get("enabled"):
            for symbol in ex_cfg.get("markets", []):
                all_markets.append((ex_cfg["name"], symbol))

    # Add OANDA markets if configured
    oanda_markets = CONFIG.get("execution", {}).get("oanda_markets", [])
    for symbol in oanda_markets:
        all_markets.append(("oanda", symbol))

    # Add Alpaca markets if configured
    alpaca_markets = CONFIG.get("execution", {}).get("alpaca_markets", [])
    for symbol in alpaca_markets:
        all_markets.append(("alpaca", symbol))

    print(f"Trading {len(all_markets)} symbols across {len(executors)} exchanges/brokers")
    notifier.notify("startup",
        f"Trading bot started. {len(all_markets)} symbols, "
        f"{len(executors)} exchanges/brokers, "
        f"{len(ml_models)} ML models loaded."
    )

    while True:
        risk_state = state.get_risk_state()
        if risk_state["trading_halted"]:
            time.sleep(30)
            continue

        for exchange_name, symbol in all_markets:
            # Skip NSE — alert/analysis only, no automated execution
            if exchange_name == "nse":
                continue

            executor = executors.get(exchange_name)
            if executor is None:
                continue

            signal = get_strategy_signal(
                feed_router, symbol,
                trading_balance=risk_state["trading_balance"],
                risk_fraction=CONFIG["risk"]["max_position_pct"] / 100,
                ml_models=ml_models,
            )

            if signal:
                executor.open_trade(
                    symbol=symbol,
                    side=signal["side"],
                    proposed_amount=signal["amount"],
                    entry_price=signal["entry_price"],
                    stop_loss_pct=CONFIG["risk"]["stop_loss_pct"],
                    take_profit_pct=CONFIG["risk"]["take_profit_pct"],
                )

        time.sleep(15)


if __name__ == "__main__":
    main()
