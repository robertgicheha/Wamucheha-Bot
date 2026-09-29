# Funding checklist

How to get money into each venue and take it back out.

**The bot never moves funds.** There is no withdrawal API call anywhere in the
codebase, no exchange withdrawal permission is ever used, and no wallet signing
key exists. Every transfer below is something **you** do, by hand, in the
MetaMask UI or the venue's own screen. That is deliberate: a compromised VPS
can lose trades, but it cannot drain your account.

> **Quick reference:** [DEPOSIT_WITHDRAWAL.md](../DEPOSIT_WITHDRAWAL.md) —
> one-page cheat sheet with addresses, networks (TRC20/BEP20), fees and the
> preflight command.

`core/risk_manager.py` watches the balance and, when it crosses
`profit_withdrawal_threshold`, it only *notifies* you that a manual transfer
is due. It does not transfer.

---

## The shape of it

```
  MetaMask (treasury)              the source. Holds the stake + swept profit.
        |
        |  USDT, on the network the venue asks for
        |
        +--> Binance deposit address   -> bot trades here
        +--> OKX deposit address       -> bot trades here
        +--> Bybit deposit address     -> bot trades here

  MT5 broker   <-> funded by the BROKER (card / bank / M-Pesa). Not MetaMask.
  Alpaca       <-> funded by BANK / WIRE. Not MetaMask.
```

Each venue holds its own balance and has its own deposit address. They are
never funded by each other.

---

## Crypto: Binance, OKX, Bybit

### 1. Get the deposit address

On each venue: **Assets → Deposit → USDT** → copy the address, and **note the
network the venue selects**. Write both down; they are the two things that
cause lost funds.

### 2. Pick the network — this is the whole game

| Network | Fee per transfer | Use for |
|---|---|---|
| **TRC20** (Tron) | ~1 USDT | Default. Cheap, works on all three venues. |
| **BEP20** (BNB Smart Chain) | ~0.1–1 USDT | Cheapest, if every venue in the path supports it. |
| **ERC20** (Ethereum) | ~5–20 USDT | **Avoid** for bulk. Only if a venue offers nothing else. |

**The network must match what the venue shows you.** MetaMask cannot detect
this for you: sending TRC20 to an address expecting ERC20 usually means the
funds are unrecoverable. Check the deposit screen after every first transfer to
a new venue.

Set the intended network in `.env` so the pre-flight check can warn you:

```
METAMASK_NETWORK=TRC20
```

### 3. Send

In MetaMask: pick the network, choose USDT, paste the venue's address, send.
Start with a **small test amount** on a new venue, confirm it credits, then
send the rest.

Target amounts are recorded in `.env` (documentation only, the bot never
sends anything):

```
FUNDING_BINANCE_USDT=
FUNDING_OKX_USDT=
FUNDING_BYBIT_USDT=
```

### 4. Confirm before trading

The deposit must be **credited and settled** before the bot trades that venue
— an order against an uncredited balance just fails. `ETHERSCAN_API_KEY=` lets
you verify on-chain if you want an independent check.

### 5. Withdraw profits

Venue **Assets → Withdraw → USDT** → your MetaMask address, same network you
deposited on. MetaMask must be on that network to receive.

Keep a small buffer at each venue for open positions; sweeping everything out
leaves nothing to close a losing trade with.

---

## MT5 (forex, gold, silver)

**Not funded from MetaMask.** The broker does not take USDT.

1. Broker's own deposit methods: card, bank transfer, or M-Pesa if offered.
2. Your `.env` must point at the matching server. `MT5_SERVER=MetaQuotes-Demo`
   is a **demo** server and cannot hold real capital — a live broker looks like
   `ICMarketsSC-MT5`, and the login number differs too.
3. Withdraw back through **the same method you deposited with**. Brokers
   routinely reject a withdrawal to a different source, which is an anti-money
   laundering control, not an obstacle to route around.

`preflight.py` prints whether MT5 resolved to demo or live, and warns if the
server name is not a demo server.

---

## Alpaca (US stocks)

**Not funded from MetaMask.**

1. Bank transfer or wire to your Alpaca account.
2. Kenya-resident eligibility for your account type is unconfirmed in this
   repo, which is why `alpaca` is `enabled: false` in `config.yaml`. Resolve
   that with Alpaca before enabling it.
3. Withdraw back to your bank.

---

## Before you flip `LIVE_TRADING=true`

Run the pre-flight check. It places no orders and moves no funds.

```bash
.venv\Scripts\python.exe scripts/preflight.py
```

Then, on **every** venue:

- [ ] API key is **trade-only** — withdrawal permission **disabled**
- [ ] VPS IP is whitelisted
- [ ] Treasury wallet is the only whitelisted withdrawal address
- [ ] Deposit credited and settled, on the network you intended
- [ ] `scripts/check_connections.py` passes for that venue
- [ ] `scripts/preflight.py` reports no blockers

Note that `ALPACA_PAPER` and `OANDA_PRACTICE` are **independent** of
`LIVE_TRADING` — they choose which API host is called. Both venues are also
`enabled: false` in `config.yaml`, and an executor is only built for an enabled
venue, so they cannot trade. Leave them that way unless you mean it.

Then set `LIVE_TRADING=true` in `.env` and restart. Consider starting with one
venue and the smallest `risk.max_position_size`, then widening once you have
seen real fills.
