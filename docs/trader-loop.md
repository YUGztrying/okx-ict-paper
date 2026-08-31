# Trader loop (paper)

The desk copies how a human ICT / Fabio trader sits the session. It never sends an OKX order.

## Enter

A real trader does not fade a 15m candle that is still printing. The model is built on a **closed** 15m bar (FVG, displacement, session value area).

OKX’s public socket pushes `candle15m` with `confirm=0` while the bar forms and `confirm=1` when it closes. The bot enters the decision **only on confirm=1**. Then it loads HTF history over REST (weekly/daily/1H context) and runs ICT 6/6 and Fabio AAA. 5/6 is a veto. Same as a trader who waits for the close, then either takes it or stands down.

Fill price is the last print (WS) or the closed bar’s close, not a fantasy mid.

## Exit

Once in, a trader’s stops and targets are resting. They fill on the **first print** that trades through them.

The bot subscribes to `tickers` on the public WS (`wss://ws.okx.com:8443/ws/v5/public`). Every last update is checked against SL/TP. Stop is evaluated first: if a gap would print through both, it is a loss, not a lucky win.

If the socket goes quiet for 8s while a position is open, REST last is used until WS resumes. That is a stall guard, not the primary path.

## Cloud (PC off)

GitHub’s `*/10` cron on a public repo skipped hours. `GITHUB_TOKEN` also does not retrigger `on: push`, so a “paper scan” commit cannot keep the loop alive.

The job now **holds the public WS for ~5h20** (GitHub’s cap is 6h). Before starting the next runner it **pushes the journal**, then dispatches. That order matters: the new job must checkout the latest fills and closes. A 6-hour cron is only a dead-man if the chain dies. `dispatch_next()` refuses to start a second chain when one is already queued or running.

Handoff is ~1–2 minutes while the next runner checks out and connects. The new job’s first act is a REST mark-to-market, so a level that is still through SL/TP is closed. A wick that tagged TP and fully retraced **only** during that handoff could be missed. That is the remaining GitHub limit, not a poll interval.

Local forever: `python paper.py --watch` (no minutes cap). Same WS loop, no chain.

## What this is not

Not live trading. Not DOM / footprint (Fabio’s real tape). Not entering mid-bar. Not checking filters every tick.
