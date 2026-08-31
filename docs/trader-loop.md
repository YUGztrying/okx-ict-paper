# Trader loop (paper)

The desk copies how a human ICT / Fabio trader sits the session. It never sends an OKX order.

## Enter

A real trader does not fade a 15m candle that is still printing. The model is built on a **closed** 15m bar (FVG, displacement, session value area).

OKX’s **business** socket (`wss://ws.okx.com:8443/ws/v5/business`) pushes `candle15m` with `confirm=0` while the bar forms and `confirm=1` when it closes. Candles are not on `/public` — that subscribe returns 60018. The bot enters the decision **only on confirm=1**. Then it loads HTF history over REST (weekly/daily/1H context) and runs ICT 6/6 and Fabio AAA. 5/6 is a veto. Same as a trader who waits for the close, then either takes it or stands down.

If confirm=1 is late or dropped, REST closed-candle is checked in the seconds around the bar boundary. That is a stall guard, not a 10-minute poll.

A bar boundary is **one** scan, not one per instrument. A scan walks every instrument, so it records the bar it decided on for each of them; the other instrument’s confirm (or the REST guard finding the same close) then has nothing left to do. Every candle that reaches a model has fully closed — the newest REST row is the bar that is still printing, and it is dropped before the model sees it.

Fill price is the last print (WS) or the closed bar’s close, not a fantasy mid.

## Exit

Once in, a trader’s stops and targets are resting. They fill on the **first print** that trades through them.

The bot subscribes to `tickers` on the public WS (`wss://ws.okx.com:8443/ws/v5/public`). Every last update is checked against SL/TP. Stop is evaluated first: if a gap would print through both, it is a loss, not a lucky win.

If the socket goes quiet for 8s while a position is open, REST last is used until WS resumes. That is a stall guard, not the primary path.

## Cloud (PC off)

GitHub’s `*/10` cron on a public repo skipped hours. `GITHUB_TOKEN` also does not retrigger `on: push`, so a “paper scan” commit cannot keep the loop alive.

The job now **holds the sockets for ~5h20** (GitHub’s cap is 6h). Before starting the next runner it **pushes the journal**, then dispatches. That order matters: the new job must checkout the latest fills and closes. A 6-hour cron is only a dead-man if the chain dies. `dispatch_next()` refuses to start a second chain when one is already queued or running.

The runner checks out once and then watches for hours, so origin can move under it. A rejected push is fetched and rebased, then retried — otherwise a single human merge to the branch would silently strand every fill and close for the rest of the window. Only `journal/` is committed: `dashboard/desk.json` is derived from it and rebuilt for the Pages artifact, so it never churns history or conflicts.

Handoff is ~1–2 minutes while the next runner checks out and connects. The new job’s first act is a REST mark-to-market, so a level that is still through SL/TP is closed. A wick that tagged TP and fully retraced **only** during that handoff could be missed. That is the remaining GitHub limit, not a poll interval.

The blotter is its own workflow (`publish-dashboard.yml`). Pages only rebuilds when a job ends, and a job here is a 5h20 watch — so the phone would show a position that closed hours ago, live-marked against OKX, which reads as current. The desk dispatches a redraw after every scan and every close instead. That workflow only reads and publishes; it never commits, so it cannot race the desk's journal push.

Local forever: `python paper.py --watch` (no minutes cap). Same WS loop, no chain.

## What this is not

Not live trading. Not DOM / footprint (Fabio’s real tape). Not entering mid-bar. Not checking filters every tick.
