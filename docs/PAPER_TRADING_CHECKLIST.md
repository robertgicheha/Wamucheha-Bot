# Paper-trading checklist: from dry-run to live

Goal: decide to risk real money from **evidence**, not hope. Nothing here
guarantees profit; it filters out a bot that is broken, unprofitable or lucky.

`LIVE_TRADING=false` (the default) means every order is simulated. Keep it that
way for the whole checklist.

## Phase 0: setup (day 0)

- [ ] Change `DASHBOARD_SECRET_KEY` in `.env` (control buttons stay disabled while it is `change_me`).
- [ ] `docker compose up -d --build`, then open `/admin` and sign in.
- [ ] Overview shows **Engine: running** (not `unknown`/`offline`) and all health checks OK.
- [ ] Telegram/Discord alerts arrive (the engine sends a startup message with the mode: DRY-RUN).
- [ ] Run `python scripts/preflight.py`: read every blocker and warning.
- [ ] Decide `payout_pct` in `config.yaml` (default 50). `0` = pure compounding.
- [ ] Set `trading_balance` / `STAKE_AMOUNT` to the amount you would really trade, so position sizes and the drawdown limit are realistic.

## Phase 1: shakedown (days 1 to 3)

- [ ] Errors tab: read every distinct error. "Exchange timeout" noise is fine; repeated tracebacks are not.
- [ ] Restarts count is not climbing (a crash loop looks healthy in the account but isn't).
- [ ] Trades appear with a stop-loss attached and sensible sizes (Trades tab).
- [ ] Test the controls once: **Halt** then **Resume**; then **Restart** and confirm the engine comes back.
- [ ] Open a trade and check the fill price vs signal price (slippage) is plausible.

## Phase 2: evidence (weeks 1 to 4)

Check the Analysis and Projection tabs weekly. Do **not** change strategy
parameters mid-run. If you tune, restart the clock, or you are fitting to noise.

- [ ] At least **30 closed trades** and **14 days**, covering more than one market condition.
- [ ] Profitable **after fees**; profit factor at least 1.3.
- [ ] Max drawdown stays inside the 5% halt limit.
- [ ] No single loss bigger than about 2x the per-trade risk (else a stop failed).
- [ ] Fees under 30% of gross profit.
- [ ] Projection tab: expectancy 95% range is entirely above zero.
- [ ] Look at By strategy / By regime: anything consistently losing is a candidate to down-weight, but only after the run ends.

Run the gate any time:

```bash
python scripts/paper_readiness.py          # exit 0 only if every required check passes
```

## Phase 3: go live small

Only when `paper_readiness.py` says **READY**:

- [ ] Re-run `python scripts/preflight.py` with zero blockers.
- [ ] API keys: trading enabled, **withdrawals disabled**, IP-whitelisted to the server.
- [ ] Fund with an amount you can lose entirely. Start at 10 to 25% of the intended size.
- [ ] Set `LIVE_TRADING=true`, restart, and confirm the startup alert says **LIVE**.
- [ ] Watch the first live trades closely: live fills, fees and slippage differ from paper.
- [ ] Keep the kill switch (Halt) one click away; check the dashboard daily for the first two weeks.
- [ ] Scale up only after live results roughly match paper results.

## Payouts (when profit accrues)

With `profit_policy: split` the bot posts an alert when a profit slice is ready:
`payout_pct` of it is yours, the rest stays in and compounds. The bot **cannot**
withdraw. Do the transfer yourself in the exchange UI (see
`DEPOSIT_WITHDRAWAL.md`) and keep the amount the alert states.

## Stop conditions (any time)

Halt and investigate if: drawdown reaches the limit, the loss streak limit
trips, errors spike, restarts climb, or live results diverge sharply from paper.
