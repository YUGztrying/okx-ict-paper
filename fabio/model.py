"""Fabio Valentini AAA approximation for crypto paper.

Source: WOR live NY session https://www.youtube.com/watch?v=DyS79Eb92Ug
We do NOT have footprint/DOM. This is the part that can be coded:

  AAA = fade the session value-area extreme after a rejection wick,
  target the other side of value, tight stop beyond the extreme.

Not a copy of his order-flow execution.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ict.model import Check, Fiche
from ict.okx_data import Candle


def _session(now: datetime) -> tuple[str, int, int]:
    hour = now.hour
    if 13 <= hour < 21:
        return "ny", 13, 21
    if 7 <= hour < 13:
        return "london", 7, 13
    if 0 <= hour < 8:
        return "asia", 0, 8
    return "dead", 0, 0


def _session_bars(entry: list[Candle], now: datetime, start_h: int, end_h: int) -> list[Candle]:
    today = now.date()
    out = []
    for c in entry:
        t = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        if t.date() != today:
            continue
        if start_h <= t.hour < end_h:
            out.append(c)
    return out


def _value_area(bars: list[Candle]) -> tuple[float, float, float] | None:
    if len(bars) < 4:
        return None
    total = sum(b.volume for b in bars) or 1.0
    ranked = sorted(bars, key=lambda b: b.volume, reverse=True)
    acc = 0.0
    selected: list[Candle] = []
    for b in ranked:
        selected.append(b)
        acc += b.volume
        if acc >= 0.7 * total:
            break
    val = min(b.low for b in selected)
    vah = max(b.high for b in selected)
    poc = max(bars, key=lambda b: b.volume)
    poc_px = (poc.high + poc.low + poc.close) / 3
    return val, vah, poc_px


def _rejection(bar: Candle, side: str, extreme: float) -> bool:
    rng = bar.high - bar.low
    if rng <= 0:
        return False
    if side == "long":
        touched = bar.low <= extreme * 1.0015
        closed_in = bar.close > bar.open and bar.close >= extreme
        wick = (min(bar.open, bar.close) - bar.low) / rng >= 0.4
        return touched and closed_in and wick
    touched = bar.high >= extreme * 0.9985
    closed_in = bar.close < bar.open and bar.close <= extreme
    wick = (bar.high - max(bar.open, bar.close)) / rng >= 0.4
    return touched and closed_in and wick


def analyze(inst_id: str, last: float, entry: list[Candle], *, min_rr: float = 1.5, now: datetime | None = None) -> Fiche:
    now = now or datetime.now(timezone.utc)
    fiche = Fiche(inst_id=inst_id, last=last, bias="unclear")
    name, start_h, end_h = _session(now)
    sess_ok = name != "dead"
    fiche.checks.append(Check("session", sess_ok, f"{name} {now.hour:02d}h UTC"))

    bars = _session_bars(entry, now, start_h, end_h) if sess_ok else []
    profile_ok = len(bars) >= 4
    fiche.checks.append(Check("profile", profile_ok, f"{len(bars)} session bars (need 4)"))

    va = _value_area(bars) if profile_ok else None
    if va:
        val, vah, poc = va
        width = (vah - val) / last if last else 0
        wide_ok = width >= 0.0012
        fiche.checks.append(Check("value_area", wide_ok, f"VAL {val:.2f} POC {poc:.2f} VAH {vah:.2f}"))
    else:
        val = vah = poc = last
        wide_ok = False
        fiche.checks.append(Check("value_area", False, "no developing value area"))

    last_bar = entry[-1] if entry else None
    long_ok = bool(va and last_bar and _rejection(last_bar, "long", val))
    short_ok = bool(va and last_bar and _rejection(last_bar, "short", vah))
    if long_ok and not short_ok:
        fiche.bias = "long"
        loc_ok = True
        loc = "AAA long · VAL fade / absorption wick"
    elif short_ok and not long_ok:
        fiche.bias = "short"
        loc_ok = True
        loc = "AAA short · VAH fade / absorption wick"
    else:
        loc_ok = False
        loc = "not at value extreme with rejection"
    fiche.checks.append(Check("aaa", loc_ok, loc))

    risk_ok = False
    detail = "incomplete"
    if loc_ok and va:
        if fiche.bias == "long":
            fiche.entry = last
            fiche.stop = min(val, last_bar.low) * 0.998
            fiche.target = vah
            risk = fiche.entry - fiche.stop
            reward = fiche.target - fiche.entry
        else:
            fiche.entry = last
            fiche.stop = max(vah, last_bar.high) * 1.002
            fiche.target = val
            risk = fiche.stop - fiche.entry
            reward = fiche.entry - fiche.target
        if risk > 0:
            fiche.rr = reward / risk
            risk_ok = fiche.rr >= min_rr
            detail = f"R:R {fiche.rr:.2f} (min {min_rr})"
        else:
            detail = "non-positive risk"
    fiche.checks.append(Check("risk", risk_ok, detail))
    fiche.reasons = [f"{c.name}: {c.detail}" for c in fiche.checks]
    return fiche
