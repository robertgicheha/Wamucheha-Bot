# Funding checklist

How to get money into each venue and take it back out.

**The bot never moves funds.** There is no withdrawal API call anywhere in the
codebase, no exchange withdrawal permission is ever used, and no wallet signing
key exists. Every transfer below is something **you** do, by hand, in the
exchange's own UI. That is deliberate: a compromised VPS can lose trades, but
it cannot drain your account.

> **Quick reference:** [DEPOSIT_WITHDRAWAL.md](../DEPOSIT_WITHDRAWAL.md) —
> one-page cheat sheet with addresses, networks (BEP20/TRC20), fees and the
> preflight command.

`core/risk_manager.py` watches the balance and, when it crosses
`profit_withdrawal_threshold`, it only *notifies* you that a manual transfer
is due. It does not transfer.

---

## The shape of it

Funding is **one funder, one settlement, no wallet**. There is no self-custody
wallet in the design at all.

```
  OKX  (FUNDING_SOURCE_VENUE)      the funder. Draws the capital.
        |
        |  USDT on-chain, on the network the destination venue asks for
        |  (each hop costs a network fee and takes settlement time)
        |
        +--> Binance deposit address   -> bot trades here
        +--> OKX deposit address       -> bot trades here
        +--> Bybit deposit address     -> bot trades here

  profit from all three withdraws back to OKX (SETTLEMENT_VENUE)

  MT5 broker   <-> funded by the BROKER (card / bank / M-Pesa). No USDT.
  Alpaca       <-> funded by BANK / WIRE. No USDT.
```

Two consequences of this design, both intentional:

- **Cross-venue funding is always an on-chain withdrawal.** Exchange-internal
  transfers only work between accounts on the *same* exchange, so OKX → Binance
  is a real withdrawal: network fee, plus settlement delay before the balance
  is usable. A venue cannot be topped up instantly from another venue.
- **The settlement venue is a single point of failure.** Every balance the bot
  can reach ultimately rests on one OKX account. Keep a withdrawal allowlist and
  withdrawal 2FA on it.

---

## Crypto: Binance, OKX, Bybit

### 1. Get the deposit address

On each venue: **Assets → Deposit → USDT** → copy the address, and **note the
network the venue selects**. Write both down; they are the two things that
cause lost funds.

### 2. Pick the network — this is the whole game

| Network | Address starts | Fee per transfer | Use for |
|---|---|---|---|
| **BEP20** (BNB Smart Chain) | `0x` | ~0.1–1 USDT | Default here. Cheapest EVM option. |
| **TRC20** (Tron) | `T` | ~1 USDT | Cheap, but Tron addresses are `T...` not `0x`. |
| **ERC20** (Ethereum) | `0x` | ~5–20 USDT | **Avoid** for bulk. Only if a venue offers nothing else. |

**The network must match what the venue shows you**, and nothing in the tool
chain can detect a mismatch for you. Sending USDT to an address on the wrong
network usually means the funds are unrecoverable.

> **TRC20 is not `0x`.** TRC20 is Tron, whose addresses begin with `T`. A `0x`
> address is an EVM (BEP20/ERC20) address and will not be credited to a TRC20
> deposit. `preflight.py` validates every deposit address against the declared
> network and raises a **BLOCKER** on a mismatch — if you see one, do not send.

Set the intended network in `.env` so the pre-flight check can verify addresses
against it:

```
TRANSFER_NETWORK=BEP20
```

### 3. Send

On the funder exchange: **Assets → Withdraw → USDT** → the destination venue's
address, on the network the destination lists. Start with a **small test
amount** on a new venue, confirm it credits, then send the rest.

Deposit addresses are recorded in `.env` (they are addresses, not amounts; the
bot never sends anything):

```
FUNDING_OKX_USDT=
FUNDING_BINANCE_USDT=
FUNDING_BYBIT_USDT=
```

Copy each one from that exchange's Deposit screen at the time you fund —
deposit addresses are per-account and the exchange can rotate them. An address
from an old email or chat may be stale.

### 4. Confirm before trading

The deposit must be **credited and settled** before the bot trades that venue
— an order against an uncredited balance just fails. A transfer leaves your
account the moment you confirm it, so the balance is unusable for the whole
network-confirmation window. Check the exchange's balance, not the transaction
hash, before trading.

### 5. Withdraw profits

Venue **Assets → Withdraw → USDT** → the settlement venue's deposit address
(`FUNDING_<SETTLEMENT_VENUE>_USDT`), on the same network you deposited on.

Keep a small buffer at each venue for open positions; sweeping everything out
leaves nothing to close a losing trade with.

---

## MT5 (forex, gold, silver)

**Not funded by USDT transfer.** MT5 is a forex/CFD broker, not an exchange —
it holds no crypto and has no deposit address, so it is entirely outside the
OKX funder/settlement loop.

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

**Not funded by USDT transfer.**

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
- [ ] Settlement venue has a withdrawal allowlist and withdrawal 2FA enabled
- [ ] Deposit credited and settled, on the network you intended
- [ ] `TRANSFER_NETWORK` in `.env` matches the network of every deposit address
- [ ] `scripts/check_connections.py` passes for that venue
- [ ] `scripts/preflight.py` reports no blockers

Note that `ALPACA_PAPER` and `OANDA_PRACTICE` are **independent** of
`LIVE_TRADING` — they choose which API host is called. Both venues are also
`enabled: false` in `config.yaml`, and an executor is only built for an enabled
venue, so they cannot trade. Leave them that way unless you mean it.

Then set `LIVE_TRADING=true` in `.env` and restart. Consider starting with one
venue and the smallest `risk.max_position_size`, then widening once you have
seen real fills.
