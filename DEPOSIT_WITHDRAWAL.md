# Deposit & Withdrawal Guide

This bot **never moves funds**. It has no private keys, no on-chain code, no
web3 dependency, and no withdrawal permission on any exchange account. Every
deposit and every withdrawal is a manual action you perform in an exchange's
own UI.

Funding is **one funder, one settlement, no wallet**: capital is drawn from a
single exchange and every venue's profit is withdrawn back to a single
exchange. There is no self-custody wallet in the design at all.

---

## Principle

```
                     ON-CHAIN USDT TRANSFER
                 (manual, in the exchange UIs,
                  costs a network fee each time)
   ┌───────────┐  ────────────────────────▶  ┌──────────────┐
   │    OKX    │        funder              │   BINANCE    │
   │ (source)  │                             │              │
   │           │  ◀────────────────────────  │              │
   └───────────┘   withdraw profit          └──────────────┘
        ▲                                      ┌──────────────┐
        │                                      │    BYBIT     │
        │                                      └──────────────┘
        │                                            ▲
        │                                            │
        └────────────────────────────────────────────┘
              settlement: every venue's profit
              is withdrawn back to OKX

   MT5 is NOT in this diagram — it is a forex broker
   account, funded by card/bank/M-Pesa, not by USDT.
```

- **Funder** (`FUNDING_SOURCE_VENUE`) is the exchange capital is drawn from.
- **Settlement** (`SETTLEMENT_VENUE`) is the exchange whose deposit address
  receives profit withdrawals. Usually the same venue, which is what makes
  this a closed loop: OKX funds everything, and everything pays back to OKX.
- **Deposit addresses** (`FUNDING_OKX_USDT`, etc.) are the destinations you send
  USDT **to** in order to fund a venue.

### The one thing to understand before funding anything

**Exchange-internal transfers only work between accounts on the same exchange.**
Moving capital from OKX to Binance is therefore always an on-chain withdrawal:
OKX ▸ Withdraw ▸ USDT ▸ (your chosen network) ▸ Binance's deposit address.

Consequences you should plan for:

- Each cross-venue transfer costs a **network fee** (~0.3–1 USDT on BEP20,
  ~1 USDT on TRC20) and takes settlement time, not seconds.
- Deposits are credited only **after the network confirms**, not when you click
  send. Do not open a trading position on a balance that has not landed.
- Because settlement is also one venue, **that one account is a single point of
  failure for every balance the bot can reach.** Enable a withdrawal allowlist
  and withdrawal 2FA on it.

If the funder and settlement venue are the same, a rebalance still costs a
withdrawal each time. That is the cost of the single-address design.

---

## Networks & Fees (USDT)

| Network | Chain            | Address starts | Typical fee | Speed  | Notes                                    |
|---------|------------------|----------------|-------------|--------|------------------------------------------|
| BEP20   | BNB Smart Chain  | `0x`           | ~0.1–1 USDT | <1 min | Cheapest EVM option — default here        |
| TRC20   | Tron             | `T`            | ~1 USDT     | <1 min | Cheap, but Tron addresses are `T...`      |
| ERC20   | Ethereum         | `0x`           | ~5–20 USDT  | 1–15 m | Avoid for bulk transfers                  |
| SOL     | Solana           | base58         | ~0.01 USDT  | <1 min | Only if the venue lists SOL/USDT          |

> **Match the network EXACTLY on both sides.** A `0x` address and a `T...`
> address are different chains; sending USDT to an address on the wrong one
> **destroys the funds** and nobody can recover them — not the exchange, not
> the chain.

> **TRC20 is not `0x`.** This trips people up constantly. Tron addresses begin
> with `T`. If your `TRANSFER_NETWORK=TRC20` but your deposit addresses are all
> `0x…`, the deposits will never be credited. `scripts/preflight.py` checks
> this and reports a **BLOCKER** on the mismatch.

---

## Funding a venue

The steps are the same for each crypto venue; only the menu labels differ.

1. Open the exchange's **Deposit** screen and choose **USDT**.
2. Read the **network** the exchange lists for that deposit. It is usually
   shown next to the address, and is sometimes implied by a network selector.
3. Copy the address into `.env` under `FUNDING_<VENUE>_USDT`.
4. Set `TRANSFER_NETWORK` to the **same network** you will send on.
5. From your **funder** exchange, withdraw USDT to that address on that
   network. For OKX as the funder, that is OKX ▸ Assets ▸ Withdraw ▸ USDT.
6. Wait for the network to confirm and the exchange to credit the balance.
7. Run `python scripts/preflight.py` and confirm the address is accepted.

**To take profit out:** on the venue holding the profit, withdraw USDT on the
same network to the **settlement** venue's deposit address
(`FUNDING_<SETTLEMENT_VENUE>_USDT`).

> Copy every address from the exchange's Deposit screen at the time you fund.
> Deposit addresses are per-account and can be rotated by the exchange. An
> address from an old email or chat may be stale.

---

## MT5 / OANDA / Alpaca (Forex / Equities)

These are **broker accounts, not crypto exchanges**, and they cannot be funded
by a USDT transfer. MT5 in particular holds forex and CFDs, not crypto, so
there is no deposit address to send to.

| Broker   | Deposit methods                       | Withdrawal to         |
|----------|---------------------------------------|-----------------------|
| MT5      | Card, bank wire, M-Pesa (via broker)  | Account on file       |
| OANDA    | Card, bank wire                       | Account on file       |
| Alpaca   | ACH / wire (US)                       | Account on file       |

MT5 capital is therefore **independent of the OKX/Binance loop** and is never
part of the funder/settlement flow. You fund the broker once; profit stays in
the broker account until you request a withdrawal in the broker's portal.

MT5 also needs a **live server name and login number**. A demo server accepts
no real capital — see the note in `.env.example` next to `MT5_SERVER`.

---

## API key permissions

The bot's exchange API keys must be **trade-only, with withdrawal disabled**.
This is a different permission from the manual withdrawals you do yourself in
the UI, and it is the single most important setting on this page.

| Permission              | Needed by bot | Yours in UI |
|-------------------------|---------------|--------------|
| Read balances / positions| yes           | yes          |
| Place / amend / cancel orders | yes      | yes          |
| **Withdraw funds**      | **no**        | **yes** (manual) |

If withdrawal is enabled on an API key, a compromise of the VPS is a direct
loss of funds with no human step in between. Verify the withdrawal toggle is
off on every key before going live, and IP-allowlist each key to the VPS.

---

## Checklist Before Going Live

```powershell
python scripts/preflight.py
```

Expected funding section:

```
[4] Funding (manual — you move money in the exchange UIs; the bot has no withdrawal rights)
  [ok]    funder venue: okx
  [ok]    settlement venue: okx
  [WARN]  funder and settlement are both OKX: ... makes OKX a single point of failure ...
  [ok]    transfer network: BEP20 (~0.1-1 USDT)
  [ok]    binance deposit address: 0x24b3...08b11
  [ok]    okx deposit address: 0x488b...485a4
  [ok]    bybit deposit address: 0x375e...58dbf
```

The single-settlement warning is expected and is the design you chose. Any
`[BLOCK]` on a deposit address means a network mismatch, which is the one
funding error that is unrecoverable — fix it before sending anything.

---

## Where the Bot Reads These

- `FUNDING_SOURCE_VENUE`, `SETTLEMENT_VENUE`, `TRANSFER_NETWORK`,
  `FUNDING_<VENUE>_USDT` → `scripts/preflight.py` (validation and display only)
- `config/config.yaml` → `fees.network` + `fees.network_fee_usdt`, used to
  report cumulative transfer cost, not to place orders

**The trading logic never reads these values.** Nothing in the codebase gates,
sizes, or routes an order from them; they exist so the operator can confirm the
right money is in the right place before risking anything. A regression test
(`test_metamask_private_key_is_not_in_env`) asserts that no custody or signing
variable can be reintroduced into `.env`.

---

## Common Mistakes

| Mistake | Consequence | Fix |
|---------|-------------|-----|
| Send USDT to an address on the wrong network | Funds lost forever | Match the network on both sides; run preflight |
| Treat a `0x` address as TRC20 | Deposit never credited | TRC20 is Tron and uses `T...` addresses |
| Enable withdrawal on an API key | A VPS compromise becomes direct theft | Trade-only keys, withdrawal disabled, IP allowlisted |
| Fund Binance but trade on OKX | OKX balance stays $0 | Fund every venue enabled in `config.yaml` |
| Try to fund MT5 with USDT | Not possible — it is a forex broker | Card / bank / M-Pesa via the broker |
| Assume a cross-venue transfer is instant | Trade on an unlanded balance | Wait for confirmation and crediting |
| Leave profit on the venue indefinitely | Capital and risk stay concentrated | Schedule periodic withdrawals to settlement |
| Put a private key or seed phrase in `.env` | Full custody loss | Never. The bot signs nothing and needs no key |

---

## Related Docs

- `config/config.yaml` — `fees:` block for the cost model
- `docs/FUNDING.md` — the same checklist in more detail
- `docs/COMMON_MISTAKES.md` — #9 (private keys), #10 (wrong network)
- `ARCHITECTURE.md` — why the bot has no on-chain code