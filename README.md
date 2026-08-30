# okx-ict-paper

Paper desk for the ICT 2022 model on OKX crypto. **It never places orders.**

When you buy USDT, create a *second* API key (`Read + Trade`, withdraw off). Do not widen the `cursor` read-only key. Live arming is a later step, after paper stats exist.

## Run

Needs the `okx` CLI (`@okx_ai/okx-trade-cli`) on PATH.

```bash
python paper.py              # one scan BTC + ETH
python paper.py --loop 10    # every 10 minutes
python paper.py --status     # journal stats
```

## 6/6 or nothing

1. NY weekly + daily bias agree
2. One draw (PDH/PDL or PWH/PWL)
3. Delivery after an Asia sweep — not Judas
4. Session score ≥ 3 (dead zone = veto)
5. Unfilled 15m FVG in discount/premium
6. R:R ≥ 2, no open paper, no 5-loss streak

Fills and stand-downs go to `journal/runs.jsonl`. Open paper lives in `journal/open.json`.
