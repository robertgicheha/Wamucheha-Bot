# Deposit & Withdrawal Guide

This bot **never moves funds on-chain**. It has no private keys, no web3
dependency, and no withdrawal permission on any exchange. Every deposit and
every withdrawal is a manual action you perform in MetaMask (or your broker's
UI for MT5/OANDA/Alpaca). This document describes the workflow, the networks
and fees, and the exact addresses to use.

---

## Principle

```
┌─────────────────────────────────────────────────────────────────┐
│  META MASK (TREASURY)                                           │
│  ┌─────────────┐  deposit USDT  ┌─────────────────────────┐   │
│  │  0x...      │ ──────────────▶ │  BINANCE / OKX / BYBIT  │   │
│  │  (yours)    │                 │  deposit address        │   │
│  └─────────────┘                 └─────────────────────────┘   │
│        ▲                                   │                    │
│        │         withdraw profit            │                    │
│        │ ◀──────────────────────────────────┘                    │
│        │                                                         │
└────────┼─────────────────────────────────────────────────────────┘
         │
         ▼
   ┌─────────────┐
   │ 0x...       │  ← 0x address you control, NOT the bot
   │ (yours)     │
   └─────────────┘
```

- **MetaMask address** (`METAMASK_WALLET_ADDRESS` in `.env`) is your treasury.
  The bot only reads it for the funding checklist. It never signs anything.
- **Exchange deposit addresses** (`FUNDING_BINANCE_USDT`, etc.) are the
  destination addresses you send USDT **to**. They are exchange-specific and
  network-specific — copy them from the exchange's **Deposit** screen, not from
  anywhere else.
- **Withdrawal** goes from the exchange **back to your MetaMask address**.
  The bot never initiates it.

---

## Networks & Fees (USDT)

| Network | Chain             | Typical fee | Speed  | Notes                                              |
|---------|-------------------|-------------|--------|----------------------------------------------------|
| TRC20   | Tron              | ~1 USDT     | <1 min | Cheapest, universal on Binance/OKX/Bybit           |
| BEP20   | BNB Smart Chain   | ~0.1–1 USDT | <1 min | Good alternative; BNB needed for gas               |
| ERC20   | Ethereum          | ~5–20 USDT  | 1–15 m | Avoid for bulk; only if destination requires it    |
| SOL     | Solana            | ~0.01 USDT  | <1 min | Only if the venue explicitly lists SOL/USDT        |

> **Match the network EXACTLY.** Sending TRC20 to a BEP20 address (or vice
> versa) **destroys the funds**. The exchange will not recover them.

---

## Binance

1. Open Binance ▸ Wallet ▸ Fiat & Spot ▸ **Deposit** ▸ **USDT**
2. Select network: **TRC20** (recommended) or **BEP20**
3. Copy the deposit address shown → put it in `.env` as
   `FUNDING_BINANCE_USDT=0x...`
4. In MetaMask: Send USDT on the **same network** to that address.
5. Wait for 1 confirmation (TRC20/BEP20 = seconds).
6. In the bot: `python scripts/preflight.py` will show the address truncated
   and confirm the network.

**Withdrawal** (when you want profits back):
1. Binance ▸ Wallet ▸ Withdraw ▸ USDT
2. Network: **TRC20** (or whatever you deposited on)
3. Address: **your MetaMask treasury address** (`METAMASK_WALLET_ADDRESS`)
3. Confirm → funds arrive in MetaMask in <1 min.

---

## OKX

1. OKX ▸ Assets ▸ Deposit ▸ USDT
2. Network: **TRC20** (or BEP20)
3. Copy address → `.env` → `FUNDING_OKX_USDT=0x...`
4. MetaMask send on same network.
5. Withdrawal: OKX ▸ Withdraw ▸ USDT ▸ TRC20 ▸ your MetaMask address.

---

## Bybit

1. Bybit ▸ Assets ▸ Deposit ▸ USDT
2. Network: **TRC20** (or BEP20)
3. Copy address → `.env` → `FUNDING_BYBIT_USDT=0x...`
4. MetaMask send on same network.
5. Withdrawal: Bybit ▸ Withdraw ▸ USDT ▸ TRC20 ▸ your MetaMask address.

---

## MT5 / OANDA / Alpaca (Forex / Equities)

These are **broker accounts**, not crypto exchanges. Funding is via the
broker's standard methods:

| Broker   | Deposit methods                | Withdrawal to        |
|----------|--------------------------------|----------------------|
| MT5      | Bank wire, card, M-Pesa (via broker) | Bank account on file |
| OANDA    | Bank wire, card                | Bank account on file |
| Alpaca   | ACH / wire (US)                | Bank account on file |

The bot trades on **margin** in these accounts. You fund the broker once;
profits stay in the broker account until you request a withdrawal in the
broker's portal. No MetaMask involved.

---

## Checklist Before Going Live

Run the preflight check — it validates every address and network:

```powershell
python scripts/preflight.py
```

Expected output includes:

```
[4] Funding (manual — the bot never moves money)
  treasury wallet recorded: 0x6d76...A3d15
  deposit network: TRC20 (~1 USDT)
  binance deposit address: 0x24b3...08b11
  okx deposit address: 0x488b...485a4
  bybit deposit address: 0x375e...58dbf
```

If any line says `WARN` or `BLOCKER`, fix it before setting `LIVE_TRADING=true`.

---

## Where the Bot Reads These

- `METAMASK_WALLET_ADDRESS`, `METAMASK_NETWORK` → `scripts/preflight.py` only
- `FUNDING_BINANCE_USDT`, `FUNDING_OKX_USDT`, `FUNDING_BYBIT_USDT` →
  `scripts/preflight.py` (display only)
- `config/config.yaml` → `fees.network` + `fees.network_fee_usdt` (gas
  estimation for daily reports, not for trading logic)

The **trading logic never reads these values**. They exist solely so the
operator can confirm the right money is in the right place before risking
anything.

---

## Common Mistakes

| Mistake | Consequence | Fix |
|---------|-------------|-----|
| Send TRC20 to BEP20 address | Funds lost forever | Always match network in MetaMask to exchange's deposit screen |
| Put private key in `.env` | Full custody loss | Never do this. Use MetaMask UI only. |
| Fund Binance but trade on OKX | OKX balance stays $0 | Fund each venue you enable in `config.yaml` |
| Forget to withdraw profits | Capital stuck on exchange | Schedule weekly withdrawal to treasury |

---

## Related Docs

- `config/config.yaml` — `fees:` block for cost model
- `docs/FUNDING.md` — same checklist in more detail
- `docs/COMMON_MISTAKES.md` — #9 (private keys), #10 (wrong network)
- `ARCHITECTURE.md` — why the bot has no on-chain code