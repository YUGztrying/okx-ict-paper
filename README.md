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
3. Asia sweep against the bias in the last 12 bars, then displacement on the
   current 15m close (body ≥ 55% of range, past the prior bar's extreme). Asia
   is the pre-08:00 **UTC** range, not the Tokyo killzone in `ict/sessions.py`.
   Judas is not detected; that displacement is the whole test.
4. Session score ≥ `session_min_score` (3). The dead zone scores 2, so it fails
   on that threshold rather than on a veto of its own — set the minimum to 2
   and dead-zone entries pass.
5. Unfilled 15m FVG in discount/premium
6. R:R ≥ 2, no open paper, no 5-loss streak

One book. Fills, stand-downs and closes all go to `journal/runs.jsonl`, open
paper to `journal/open.json`, and every line carries `strategy` — `ict` or
`fabio`. The dashboard's ICT and Fabio tabs are filters over that one ledger,
not separate books. An older `journal/fabio/` is folded in on startup.

Three guards the 365-day replay argued for, all set in `config.toml`:
`max_leverage` (a stop decides the leverage — `risk_pct / stop_pct` — and
nothing in either strategy bounds the stop), `loss_cooldown_hours` (the loss
breaker is a pause, not a ban: it used to clear only on a win, and a halted
strategy can never win), and `min_rr_on_net` (the R:R gate applied to the
number the account actually receives). `python -m backtest sweep` prints what
each one costs before you turn it on.

`python -m backtest run --book random` is the control: a coin flip with the
desk's own stop distance, reward-to-risk, sizing and guards. Every other book
answers "how much does this strategy make"; this one answers the question
underneath — does the entry logic do anything at all. A random entry targeting
R times its stop wins about 1/(1+R) of the time, so a book landing on that
number is indistinguishable from the control.

Every book reports its expectancy with a standard error and a verdict. Four
trades at +1.17 R average carry an error of +/-1.25 R — the uncertainty is
larger than the estimate, and the report says so rather than letting the
average read like a result. Where an effect is too small to confirm, it names
the sample size that would be needed.

One position per coin, whichever strategy found it. When both fire on the same
bar the better reward-to-risk **after fees** takes the slot and the other is
journaled as a `crowded_out` stand-down — so the cost of sharing a book is a
number you can read, not a strategy that mysteriously went quiet.
