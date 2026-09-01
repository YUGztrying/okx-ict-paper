# okx-ict-paper

Paper desk for the ICT 2022 model (and a Fabio AAA book) on OKX crypto. **It never places orders.**

When you buy USDT, create a *second* API key (`Read + Trade`, withdraw off). Do not widen the `cursor` read-only key. Live arming is a later step, after paper stats exist.

How the desk sits the tape: [docs/trader-loop.md](docs/trader-loop.md).

## Run

Market data is OKX **public REST + public (tickers) and business (candles) WebSocket**. No CLI required for paper.

## Cloud (PC éteint)

GitHub Actions holds the OKX sockets for ~5h20, **pushes the journal, then** starts the next job. Entries fire on a **confirmed 15m close** (`confirm=1` on the business WS). Open paper is marked on every public ticker print. Journal is pushed on fill, close, 15m scans, and before each handoff.

https://yugztrying.github.io/okx-ict-paper/

Toujours paper. Aucun ordre. Tu peux éteindre l’ordi.

## Local

```bash
python -m unittest
python paper.py --watch                 # sit the desk (Ctrl+C to stop)
python paper.py --watch --minutes 5     # short session
python paper.py                         # one full scan
python paper.py --status
python dashboard.py                     # blotter on http://<LAN>:8787
```

## 6/6 or nothing (ICT)

1. NY weekly + daily bias agree
2. One draw (PDH/PDL or PWH/PWL)
3. Delivery after an Asia sweep — not Judas
4. Session score ≥ 3 (dead zone = veto)
5. Unfilled 15m FVG in discount/premium
6. R:R ≥ 2, no open paper, no 5-loss streak

One book. Fills, stand-downs and closes all go to `journal/runs.jsonl`, open
paper to `journal/open.json`, and every line carries `strategy` — `ict` or
`fabio`. The dashboard's ICT and Fabio tabs are filters over that one ledger,
not separate books. An older `journal/fabio/` is folded in on startup.

One position per coin, whichever strategy found it. When both fire on the same
bar the better reward-to-risk **after fees** takes the slot and the other is
journaled as a `crowded_out` stand-down — so the cost of sharing a book is a
number you can read, not a strategy that mysteriously went quiet.
