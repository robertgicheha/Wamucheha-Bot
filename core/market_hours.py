"""
Market-hours gate shared by the entry loop and the position monitor.

Trading a closed market either fails (OANDA rejects weekend orders, MT5
returns "market closed") or queues an order that fills at the next open at an
unknown price (Alpaca). Signals computed on a frozen weekend/overnight chart
are stale anyway, so both opening and closing are skipped until it reopens.

- Crypto exchanges: always open.
- Alpaca (US stocks/ETFs): asks the broker's /v2/clock (handles holidays,
  half-days and DST), cached for a minute.
- OANDA / MT5 forex, gold: closed from Friday 21:00 UTC to Sunday 22:00 UTC
  (covers both the summer and winter New York close). MT5 crypto CFDs
  (BTCUSD, ETHUSD) trade through the weekend.
"""
from datetime import datetime, timezone

CRYPTO_EXCHANGES = {"binance", "okx", "kraken", "coinbase", "bybit"}
MT5_CRYPTO_PREFIXES = ("BTC", "ETH", "LTC", "XRP", "SOL", "DOGE")


def forex_market_open(now: datetime = None) -> bool:
    now = now or datetime.now(timezone.utc)
    wd, hour = now.weekday(), now.hour  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 4 and hour >= 21:
        return False
    if wd == 6 and hour < 22:
        return False
    return True


def market_is_open(exchange_name: str, symbol: str, executor=None) -> bool:
    if exchange_name in CRYPTO_EXCHANGES:
        return True
    if exchange_name == "alpaca":
        if executor is not None and hasattr(executor, "is_market_open"):
            return executor.is_market_open()
        return True
    if exchange_name == "mt5" and symbol.upper().startswith(MT5_CRYPTO_PREFIXES):
        return True
    if exchange_name in ("oanda", "mt5"):
        return forex_market_open()
    return True
