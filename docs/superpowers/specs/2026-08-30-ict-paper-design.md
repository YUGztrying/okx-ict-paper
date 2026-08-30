# ICT crypto paper desk

Date: 2026-08-30
Market: OKX spot BTC-USDT / ETH-USDT
Mode: paper only. No orders. Read-only market data.

## Goal

A 24/7 analysis loop that logs a setup only when all 6 congruences fire.
When the account is funded and a Trade key exists, the same checklist arms live — not before.

## Congruences (all required)

1. HTF bias — NY weekly + NY daily, same direction
2. Draw on liquidity — one target in that direction (PDH/PDL or PWH/PWL)
3. AMD phase — delivery after a sweep, not Judas / still in range
4. Session score ≥ 3 — Asia / London / NY. Dead zone veto
5. PD array — 15m FVG in discount (long) or premium (short)
6. Risk — R:R ≥ 2, no open paper on the same asset, not in a 5-loss streak

## Live arming (later)

1. User buys USDT
2. User creates a second API key: Read + Trade, withdraw off
3. Rails stay in code: 0.5% risk, daily loss cap, one position, kill switch
4. Paper stats reviewed first (30–50 fills)

## Out of scope

YouTube scraping, WOR setup mashup, auto-live, withdraw, martingale.
