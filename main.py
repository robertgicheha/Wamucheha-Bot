"""
Main entry point — run this on the VPS.

Wires together state, risk, execution, and alerting across multiple asset classes:
- Crypto (Binance, OKX via ccxt)
- Forex (EUR/USD, GBP/USD, etc. via OANDA or MT5)
- Commodities (XAU/USD via OANDA or MT5)
- US Stocks/ETFs (AAPL, SPY via Alpaca)
- MT5 (XAUUSD, BTCUSD, GBPUSD, EURUSD, AUDUSD, EURJPY, EURGBP, USDCAD)
- Kenyan Stocks (NSE — analysis/alerts only, no automated execution)

Features a 10-strategy ensemble (EMA, MACD, Bollinger, RSI, Momentum, VWAP,
Keltner, Ichimoku, Supertrend, Scalping) with adaptive regime-aware weighting,
optional LSTM ML filtering, market regime intelligence, and automatic model retraining.
"""
import os
import time
import yaml
import pandas as pd
from dotenv import load_dotenv

from core.state_manager import StateManager
from core.risk_manager import RiskManager
from core.execution_manager import ExecutionManager
from core.oanda_executor import OandaExecutor
from core.alpaca_executor import AlpacaExecutor
from core.mt5_executor import MT5Executor
from alerts.notifier import Notifier
from data_feeds.feed_router import FeedRouter
from strategy.technical_strategy import generate_signal, generate_signal_with_ml
from ml.lstm_predictor import LSTMPricePredictor
from core.position_monitor import check_and_close_positions
from reporting.hourly_report import HourlyReporter
from long_term.market_intelligence import MarketIntelligence
from control.telegram_bot import TelegramControlBot
from control.discord_bot import DiscordControlBot

load_dotenv()

LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

with open("config/config.yaml") as f:
    CONFIG = yaml.safe_load(f)

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
        discord_webhook_trades=os.environ.get("DISCORD_WEBHOOK_TRADES") or os.environ.get("DISCORD_WEBHOOK_URL"),
    )


def load_ml_models(config: dict) -> dict:
    """Load trained ML models for symbols that have them."""
    if not config.get("ml", {}).get("use_lstm", True):
        print("  ML LSTM filter is disabled in config.")
        return {}
    models = {}
    symbols_to_check = []
    for ex_cfg in config.get("execution", {}).get("exchanges", []):
        if ex_cfg.get("enabled"):
            symbols_to_check.extend(ex_cfg.get("markets", []))
    oanda_markets = config.get("execution", {}).get("oanda_markets", [])
    symbols_to_check.extend(oanda_markets)
    mt5_markets = config.get("execution", {}).get("mt5_markets", [])
    symbols_to_check.extend(mt5_markets)
    alpaca_markets = config.get("execution", {}).get("alpaca_markets", [])
    symbols_to_check.extend(alpaca_markets)

    for symbol in symbols_to_check:
        predictor = LSTMPricePredictor(symbol=symbol)
        if predictor.load():
            models[symbol] = predictor
            print(f"  Loaded LSTM model for {symbol}")
    return models


def maybe_retrain_models(config: dict, feed_router: FeedRouter, ml_models: dict):
    """Periodically retrain ML models to avoid staleness."""
    retrain_hours = config.get("ml", {}).get("auto_retrain_hours", 168)
    min_samples = config.get("ml", {}).get("min_train_samples", 500)
    retrain_file = "data/.last_retrain"
    os.makedirs("data", exist_ok=True)

    if os.path.exists(retrain_file):
        last = float(open(retrain_file).read().strip())
        if (time.time() - last) < retrain_hours * 3600:
            return

    print("Auto-retraining ML models...")
    all_symbols = []
    for ex_cfg in config.get("execution", {}).get("exchanges", []):
        if ex_cfg.get("enabled"):
            all_symbols.extend(ex_cfg.get("markets", []))
    all_symbols.extend(config.get("execution", {}).get("oanda_markets", []))

    for symbol in all_symbols:
        try:
            df = feed_router.get_ohlcv(symbol, timeframe="15m", limit=min_samples + 200)
            if df is None or len(df) < min_samples:
                continue
            predictor = LSTMPricePredictor(symbol=symbol)
            result = predictor.train(df, epochs=20, verbose=False)
            if result["test_accuracy"] > result["naive_baseline_accuracy"] + 0.02:
                predictor.save()
                ml_models[symbol] = predictor
                print(f"  Retrained {symbol}: acc={result['test_accuracy']:.3f}")
            else:
                print(f"  Skipped {symbol}: no edge (acc={result['test_accuracy']:.3f})")
        except Exception as e:
            print(f"  Retrain failed for {symbol}: {e}")

    with open(retrain_file, "w") as f:
        f.write(str(time.time()))


def get_strategy_signal(feed_router: FeedRouter, symbol: str, trading_balance: float,
                         risk_fraction: float, ml_models: dict = None,
                         market_regime: dict = None) -> dict | None:
    """Strategy signal with optional ML filter and market regime adjustment."""
    try:
        df = feed_router.get_ohlcv(symbol, timeframe="15m", limit=200)
    except Exception as e:
        print(f"  Failed to fetch data for {symbol}: {e}")
        return None

    if df is None or len(df) < 50:
        return None

    predictor = ml_models.get(symbol) if ml_models else None
    strategy_cfg = CONFIG.get("strategy", {})
    ml_confidence = CONFIG.get("ml", {}).get("lstm_min_confidence", 0.6)

    signal = generate_signal_with_ml(
        df, risk_fraction, trading_balance,
        lstm_predictor=predictor,
        ml_min_confidence=ml_confidence,
        cfg=strategy_cfg,
    )

    if signal is None:
        return None

    # Market regime filter: reduce confidence in risk-off environments
    if market_regime and market_regime.get("regime") == "risk_off":
        score = signal.get("score", 0)
        if score < 0.7:
            return None

    return signal


def main():
    notifier = build_notifier()
    state = StateManager(stake_amount=CONFIG["account"]["stake_amount"])
    risk = RiskManager(state, CONFIG, notifier)

    # Set starting balance for session stats
    risk_state = state.get_risk_state()
    notifier.update_start_balance(risk_state.get("trading_balance", 0))

    feed_router = FeedRouter(CONFIG)
    print(f"Active feeds: {list(feed_router.get_available_feeds().keys())}")

    print("Loading ML models...")
    ml_models = load_ml_models(CONFIG)

    market_intel = MarketIntelligence(CONFIG)

    executors = {}

    # ccxt executors (Binance, OKX, etc.)
    for ex_cfg in CONFIG["execution"]["exchanges"]:
        if not ex_cfg["enabled"]:
            continue
        name = ex_cfg["name"]
        if name in ("binance", "okx", "kraken", "coinbase", "bybit"):
            api_key = os.environ.get(f"{name.upper()}_API_KEY", "")
            api_secret = os.environ.get(f"{name.upper()}_API_SECRET", "")
            passphrase = os.environ.get(f"{name.upper()}_PASSPHRASE", "") or os.environ.get(f"{name.upper()}_API_PASSPHRASE", "")
            executors[name] = ExecutionManager(
                exchange_id=name,
                api_key=api_key,
                api_secret=api_secret,
                state_manager=state,
                risk_manager=risk,
                notifier=notifier,
                dry_run=not LIVE_TRADING,
            )
            if passphrase:
                executors[name].exchange.password = passphrase
            print(f"  {name.upper()} executor initialized")

    # OANDA executor
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
            dry_run=not LIVE_TRADING,
        )
        print("  OANDA executor initialized")

    # Alpaca executor
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
            dry_run=not LIVE_TRADING,
        )
        print("  Alpaca executor initialized")

    # MT5 executor
    mt5_login = int(os.environ.get("MT5_LOGIN", "0"))
    mt5_password = os.environ.get("MT5_PASSWORD", "")
    mt5_server = os.environ.get("MT5_SERVER", "")
    if mt5_login:
        mt5_exec = MT5Executor(
            state_manager=state,
            risk_manager=risk,
            notifier=notifier,
            login=mt5_login,
            password=mt5_password,
            server=mt5_server,
            dry_run=not LIVE_TRADING,
        )
        if mt5_exec.connect():
            executors["mt5"] = mt5_exec
            print("  MT5 executor initialized")
        else:
            print("  WARNING: MT5 connection failed")

    # Build unified market list
    all_markets = []
    for ex_cfg in CONFIG["execution"]["exchanges"]:
        if ex_cfg.get("enabled"):
            for symbol in ex_cfg.get("markets", []):
                all_markets.append((ex_cfg["name"], symbol))

    for symbol in CONFIG.get("execution", {}).get("oanda_markets", []):
        all_markets.append(("oanda", symbol))
    for symbol in CONFIG.get("execution", {}).get("alpaca_markets", []):
        all_markets.append(("alpaca", symbol))
    for symbol in CONFIG.get("execution", {}).get("mt5_markets", []):
        all_markets.append(("mt5", symbol))

    print(f"\nTrading {len(all_markets)} symbols across {len(executors)} exchanges/brokers")
    mode_str = "LIVE — REAL MONEY" if LIVE_TRADING else "DRY-RUN (simulated, no real orders)"
    print(f"*** MODE: {mode_str} ***")
    print(f"Strategy: 10-strategy adaptive ensemble with regime-aware weighting")
    notifier.notify("startup",
        f"Bot started in {mode_str}. {len(all_markets)} symbols, {len(executors)} brokers, "
        f"{len(ml_models)} ML models. 10-strategy adaptive ensemble active.",
        priority="high" if LIVE_TRADING else "normal",
    )

    # --- Interactive control bots ---
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tg_token:
        tg_bot = TelegramControlBot(state_manager=state, risk_manager=risk)
        tg_bot.start(tg_token)
        print("  Telegram control bot started")
    else:
        print("  Telegram control bot: no token configured (skipped)")

    dc_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if dc_token:
        dc_bot = DiscordControlBot(state_manager=state, risk_manager=risk)
        dc_bot.start(dc_token)
        print("  Discord control bot started")
    else:
        print("  Discord control bot: no token configured (skipped)")

    hourly_reporter = HourlyReporter(state, notifier, interval_minutes=60)

    from data_feeds.nse_feed import NSEFeed
    from long_term.fundamentals import FundamentalsFetcher
    from long_term.news_sentiment import NewsSentiment
    nse_feed = NSEFeed()
    nse_watchlist = CONFIG.get("nse", {}).get("watchlist",
        ["SCOM", "EQTY", "KCB", "BAT", "EABL", "SAFARICOM", "DTK",
         "COOP", "ABSA", "KNC", "NIC"])
    nse_interval = CONFIG.get("nse", {}).get("analysis_interval_minutes", 60) * 60
    last_nse_check = 0
    last_retrain_check = 0
    fundamentals = FundamentalsFetcher()
    news = NewsSentiment()

    def check_nse_stocks():
        results = []
        for ticker in nse_watchlist:
            try:
                df = nse_feed.get_ohlcv(ticker, timeframe="1d", limit=200)
                if df is None or len(df) < 30:
                    continue
                profile = fundamentals.get_profile(ticker)
                sentiment = news.get_sentiment(ticker)
                df["ma50"] = df["close"].rolling(50).mean()
                df["ma200"] = df["close"].rolling(200).mean()
                last = df.iloc[-1]
                above_200 = last["close"] > last["ma200"] if not pd.isna(last.get("ma200")) else None
                golden_cross = last["ma50"] > last["ma200"] if not pd.isna(last.get("ma200")) and not pd.isna(last.get("ma50")) else None
                price = float(last["close"])
                msg_parts = [f"NSE: {ticker} @ KES {price:.2f}"]
                if above_200 is not None:
                    msg_parts.append(f"{'Above' if above_200 else 'Below'} 200DMA")
                if golden_cross is not None:
                    msg_parts.append("Golden cross" if golden_cross else "No golden cross")
                if sentiment and sentiment.get("avg_sentiment") is not None:
                    msg_parts.append(f"News: {sentiment['label']} ({sentiment['avg_sentiment']:+.2f})")
                results.append("\n  ".join(msg_parts))
            except Exception:
                continue
        if results:
            notifier.notify("nse_alert", "NSE Kenya Analysis:\n" + "\n".join(results))

    while True:
        risk_state = state.get_risk_state()
        if risk_state["trading_halted"]:
            time.sleep(30)
            continue

        now = time.time()

        # NSE analysis on schedule
        if now - last_nse_check >= nse_interval:
            try:
                check_nse_stocks()
                last_nse_check = now
            except Exception as e:
                print(f"NSE analysis error: {e}")

        # Auto-retrain ML models (once per cycle)
        if now - last_retrain_check >= 3600:
            try:
                maybe_retrain_models(CONFIG, feed_router, ml_models)
                last_retrain_check = now
            except Exception as e:
                print(f"Retrain check error: {e}")

        # Get market regime
        market_regime = None
        try:
            market_regime = market_intel.get_market_regime(feed_router)
        except Exception as e:
            print(f"Market intel error: {e}")

        # Check open positions for exits
        try:
            check_and_close_positions(
                state, executors, feed_router,
                trailing_activate_pct=CONFIG["risk"].get("trailing_stop_activate_pct", 1.5),
                trailing_distance_pct=CONFIG["risk"].get("trailing_stop_distance_pct", 1.0),
                strategy_cfg=CONFIG.get("strategy", {}),
            )
        except Exception as e:
            print(f"Position monitor error: {e}")

        # Look for new entries
        for exchange_name, symbol in all_markets:
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
                market_regime=market_regime,
            )

            if signal:
                executor.open_trade(
                    symbol=symbol,
                    side=signal["side"],
                    proposed_amount=signal["amount"],
                    entry_price=signal["entry_price"],
                    stop_loss_pct=CONFIG["risk"]["stop_loss_pct"],
                    take_profit_pct=CONFIG["risk"]["take_profit_pct"],
                    strategies=signal.get("strategies", []),
                    score=signal.get("score", 0),
                    regime=signal.get("regime", ""),
                )

        # Hourly report
        try:
            hourly_reporter.maybe_report()
        except Exception as e:
            print(f"Hourly reporter error: {e}")

        time.sleep(15)


if __name__ == "__main__":
    main()
