# Common Mistakes in Retail Trading Bots (and how this system avoids them)

This isn't theoretical — these are the recurring reasons hobby algo traders lose
money to their *own bot*, separate from the market simply moving against them.

## 1. State lives only in memory
**The mistake**: bot restarts (crash, VPS reboot, deploy) and forgets its
consecutive-loss count, open positions, or halted status. It comes back up trading
freely right through what should have been a dead stop.
**Here**: `core/state_manager.py` persists every risk-critical field to SQLite in
its own transaction, immediately, with WAL mode for crash safety. A JSON snapshot
is written after every change too.

## 2. Stop-losses tracked only in code, not on the exchange
**The mistake**: the bot's internal logic says "sell if price drops 2%" but that
logic only runs while the bot process is alive. Bot crashes → position sits naked
with zero protection.
**Here**: `execution_manager.py` submits the stop-loss as a real exchange-side
order at the same time as the entry, so it's enforced by the exchange even if the
bot is completely down.

## 3. Non-idempotent order submission
**The mistake**: a network timeout happens right as an order is submitted. The bot
doesn't know if it went through, retries, and ends up with two live positions
instead of one.
**Here**: every order carries a UUID-based client order ID, checked against
persisted state before submission, with the exchange itself deduplicating on that ID.

## 4. Overfit backtests
**The mistake**: a strategy is tuned until it looks perfect on historical data,
which usually just means it memorized the noise in that specific dataset. It then
fails immediately on new, live data. This is the single biggest reason backtested
"90% win rate" strategies don't survive contact with real markets.
**Mitigation**: always test on out-of-sample data the strategy has never seen, and
across both bull and bear periods. Be suspicious of any backtest result that looks
too good — it usually is.

## 5. Ignoring fees, slippage, and spread in backtests
**The mistake**: a strategy that "wins" in backtesting because it assumes free,
instant, zero-slippage fills. In reality, fees and slippage eat small, frequent
gains alive — especially on high-frequency strategies.
**Mitigation**: always model realistic fees and slippage (`max_slippage_pct` in
config) in both backtests and live execution checks.

## 6. No position sizing discipline ("all-in" trades)
**The mistake**: betting a large % of the account on a single trade because the
signal "looked really good." One bad trade wipes out weeks of gains.
**Here**: `risk_manager.py` hard-caps every position at `max_position_pct` of the
*trading balance only* — the stake is structurally excluded from ever being sized
into a trade.

## 7. Silent failures
**The mistake**: the bot hits an unhandled exception, the process dies, and nobody
notices for days because there was no independent monitor.
**Here**: the local watchdog (`scripts/heartbeat_monitor.py`) runs on a separate
machine and pages you if the VPS goes dark — the VPS being down is precisely the
scenario a monitor *on the VPS* cannot detect.

## 8. Auto-resuming after a circuit breaker
**The mistake**: the bot hits its loss limit, "pauses" for an hour, then quietly
resumes on its own — often right back into the same bad conditions that triggered
the halt.
**Here**: `resume_trading()` requires an explicit authenticated call (dashboard
button + secret key) — there is no code path that resumes trading automatically.

## 9. API keys with withdrawal permission
**The mistake**: the exchange API key used for trading also has withdrawal rights.
If the VPS or the key is ever compromised, an attacker can drain the account
directly, not just place bad trades.
**Mitigation**: generate trading-only API keys (every major exchange supports
this), IP-whitelist them to the VPS's static IP, and keep the stake wallet on a
completely separate account/key the bot never touches.

## 10. No rate-limit handling
**The mistake**: the bot hammers the exchange API in a retry loop after an error,
gets rate-limited or temporarily banned, and misses the exact moment it needed to
close a position.
**Here**: `execution_manager.py` uses exponential backoff (`tenacity`) and ccxt's
built-in rate limiter.

## 11. Chasing "prediction" for long-term stock picks
**The mistake**: treating an ML model's stock price prediction as reliable enough
to bet on. Public price history is heavily arbitraged; if a model reliably beat
the market from price history alone, it wouldn't be shared. Bots that market
themselves this way are usually overfit or outright scams.
**Here**: the long-term module is explicitly a screener + alert system based on
disclosed fundamentals and rules (dividend growth, payout ratio, technical entries)
— it tells you *why* a stock might be worth a look, not that it "will" perform.

## 12. Treating "risking only profit" as risk-free
**The mistake**: assuming that because a loss is described as "coming out of
profit," the stake itself is somehow protected automatically.
**Here**: this is enforced structurally, not just by labeling — the stake amount
is never in the `trading_balance` figure the risk manager sizes positions against,
and profit above the sweep threshold is periodically moved out of the trading
account entirely (a real fund transfer, not an internal accounting note).
