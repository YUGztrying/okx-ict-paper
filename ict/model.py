from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ict.okx_data import Candle
from ict.sessions import ICT_KILLZONES, session_score as _score

NY = ZoneInfo("America/New_York")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Fiche:
    inst_id: str
    last: float
    bias: str
    checks: list[Check] = field(default_factory=list)
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    rr: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def missing(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok]


@dataclass
class NyBar:
    day: str
    open: float
    high: float
    low: float
    close: float


def _ts_ny(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(NY)


def session_score(now: datetime | None = None) -> tuple[int, str]:
    """ICT killzones on the shared clock (ict/sessions.py). DST-aware."""
    return _score(now, ICT_KILLZONES)


def ny_days(hourly: list[Candle]) -> list[NyBar]:
    buckets: dict[str, list[Candle]] = {}
    for c in hourly:
        key = _ts_ny(c.ts).date().isoformat()
        buckets.setdefault(key, []).append(c)
    days: list[NyBar] = []
    for key in sorted(buckets):
        rows = buckets[key]
        days.append(
            NyBar(
                day=key,
                open=rows[0].open,
                high=max(r.high for r in rows),
                low=min(r.low for r in rows),
                close=rows[-1].close,
            )
        )
    return days


def _structure_bias(days: list[NyBar]) -> tuple[str, str]:
    if len(days) < 4:
        return "unclear", "not enough NY days"
    a, b, c = days[-3], days[-2], days[-1]
    hh_hl = c.high > b.high and c.low > b.low and b.high >= a.high
    lh_ll = c.high < b.high and c.low < b.low and b.low <= a.low
    if hh_hl:
        return "long", f"HH/HL into {c.day}"
    if lh_ll:
        return "short", f"LH/LL into {c.day}"
    if c.close > c.open and c.close > b.close:
        return "long", f"daily close up {c.day}"
    if c.close < c.open and c.close < b.close:
        return "short", f"daily close down {c.day}"
    return "unclear", "mixed daily structure"


def _weekly_bias(days: list[NyBar]) -> tuple[str, str]:
    if len(days) < 10:
        return "unclear", "not enough days for weekly"
    this = days[-5:]
    prev = days[-10:-5]
    this_high, this_low = max(d.high for d in this), min(d.low for d in this)
    prev_high, prev_low = max(d.high for d in prev), min(d.low for d in prev)
    if this_high > prev_high and this_low >= prev_low:
        return "long", "weekly HH/HL"
    if this_low < prev_low and this_high <= prev_high:
        return "short", "weekly LH/LL"
    if this[-1].close > prev[-1].close:
        return "long", "week closing higher"
    if this[-1].close < prev[-1].close:
        return "short", "week closing lower"
    return "unclear", "weekly mixed"


def _equal_levels(days: list[NyBar], side: str, last: float) -> float | None:
    levels = [d.high if side == "high" else d.low for d in days[-12:]]
    for i, level in enumerate(levels):
        for other in levels[i + 1 :]:
            if abs(level - other) / level <= 0.0015:
                if side == "high" and level > last:
                    return max(level, other)
                if side == "low" and level < last:
                    return min(level, other)
    return None


def _draw(bias: str, days: list[NyBar], last: float) -> tuple[float | None, str]:
    if len(days) < 6:
        return None, "no HTF range"
    pdh, pdl = days[-2].high, days[-2].low
    week = days[-6:-1]
    pwh, pwl = max(d.high for d in week), min(d.low for d in week)
    if bias == "long":
        eq = _equal_levels(days, "high", last)
        candidates = [(pdh, "PDH"), (pwh, "PWH")]
        if eq:
            candidates.append((eq, "equal highs"))
        above = [(px, name) for px, name in candidates if px > last]
        if not above:
            return None, "no liquidity above"
        px, name = min(above, key=lambda x: x[0])
        return px, name
    eq = _equal_levels(days, "low", last)
    candidates = [(pdl, "PDL"), (pwl, "PWL")]
    if eq:
        candidates.append((eq, "equal lows"))
    below = [(px, name) for px, name in candidates if px < last]
    if not below:
        return None, "no liquidity below"
    px, name = max(below, key=lambda x: x[0])
    return px, name


def _asian_range(hourly: list[Candle], now: datetime) -> tuple[float, float] | None:
    today = now.date()
    asia = [
        c
        for c in hourly
        if datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).date() == today
        and datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).hour < 8
    ]
    if len(asia) < 2:
        yesterday = [c for c in hourly if datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).hour < 8]
        if len(yesterday) < 2:
            return None
        asia = yesterday[-8:]
    return min(c.low for c in asia), max(c.high for c in asia)


def _displacement(entry: list[Candle], bias: str) -> bool:
    if len(entry) < 4:
        return False
    last = entry[-1]
    body = abs(last.close - last.open)
    rng = last.high - last.low
    if rng <= 0 or body / rng < 0.55:
        return False
    if bias == "long":
        return last.close > last.open and last.close > entry[-2].high
    return last.close < last.open and last.close < entry[-2].low


def _fvgs(entry: list[Candle], bias: str) -> list[tuple[float, float, int]]:
    gaps: list[tuple[float, float, int]] = []
    for i in range(2, len(entry)):
        left, mid, right = entry[i - 2], entry[i - 1], entry[i]
        if bias == "long" and right.low > left.high:
            gaps.append((left.high, right.low, right.ts))
        if bias == "short" and right.high < left.low:
            gaps.append((right.high, left.low, right.ts))
    return gaps[-4:]


def _unfilled(gap: tuple[float, float, int], later: list[Candle], bias: str) -> bool:
    low, high, ts = (gap[0], gap[1], gap[2]) if gap[0] < gap[1] else (gap[1], gap[0], gap[2])
    for c in later:
        if c.ts <= ts:
            continue
        if bias == "long" and c.low <= low:
            return False
        if bias == "short" and c.high >= high:
            return False
    return True


def analyze(
    inst_id: str,
    last: float,
    hourly: list[Candle],
    entry: list[Candle],
    *,
    min_rr: float,
    session_min: int,
    now: datetime | None = None,
) -> Fiche:
    now = now or datetime.now(timezone.utc)
    fiche = Fiche(inst_id=inst_id, last=last, bias="unclear")
    days = ny_days(hourly)

    daily_bias, daily_why = _structure_bias(days)
    weekly_bias, weekly_why = _weekly_bias(days)
    if daily_bias != "unclear" and weekly_bias != "unclear" and daily_bias == weekly_bias:
        fiche.bias = daily_bias
        bias_ok = True
        bias_detail = f"{daily_bias} | {weekly_why}; {daily_why}"
    else:
        bias_ok = False
        bias_detail = f"weekly={weekly_bias} ({weekly_why}) daily={daily_bias} ({daily_why})"
    fiche.checks.append(Check("htf_bias", bias_ok, bias_detail))

    draw_px, draw_name = _draw(fiche.bias, days, last) if bias_ok else (None, "no bias")
    fiche.checks.append(Check("draw", draw_px is not None, f"{draw_name} {draw_px}" if draw_px else draw_name))

    asia = _asian_range(hourly, now)
    swept = False
    phase = "accumulation"
    if asia:
        a_low, a_high = asia
        recent = entry[-12:]
        if fiche.bias == "long" and any(c.low < a_low for c in recent):
            swept = True
        if fiche.bias == "short" and any(c.high > a_high for c in recent):
            swept = True
        if swept and _displacement(entry, fiche.bias):
            phase = "delivery"
        elif swept:
            phase = "manipulation"
        else:
            phase = "accumulation"
        phase_detail = f"{phase} | asia {a_low:.2f}-{a_high:.2f} swept={swept}"
    else:
        phase_detail = "no asian range"
    fiche.checks.append(Check("amd", phase == "delivery", phase_detail))

    score, session = session_score(now)
    fiche.checks.append(Check("session", score >= session_min, f"{session} score={score}"))

    dealing_high = max(c.high for c in entry[-40:]) if entry else last
    dealing_low = min(c.low for c in entry[-40:]) if entry else last
    mid = (dealing_high + dealing_low) / 2
    in_zone = (fiche.bias == "long" and last <= mid) or (fiche.bias == "short" and last >= mid)

    chosen = None
    if bias_ok:
        for gap in reversed(_fvgs(entry, fiche.bias)):
            if _unfilled(gap, entry, fiche.bias):
                chosen = gap
                break
    fvg_ok = chosen is not None and in_zone
    if chosen:
        g_lo, g_hi = (chosen[0], chosen[1]) if chosen[0] < chosen[1] else (chosen[1], chosen[0])
        fiche.entry = (g_lo + g_hi) / 2
        fvg_detail = f"FVG {g_lo:.2f}-{g_hi:.2f} zone={'ok' if in_zone else 'wrong side of 50%'}"
    else:
        fvg_detail = "no unfilled 15m FVG" if in_zone else "no FVG / not in discount-premium"
    fiche.checks.append(Check("pd_array", fvg_ok, fvg_detail))

    rr = None
    risk_ok = False
    risk_detail = "incomplete levels"
    if fiche.entry and draw_px and asia:
        a_low, a_high = asia
        if fiche.bias == "long":
            fiche.stop = min(a_low, min(c.low for c in entry[-8:])) * 0.999
            fiche.target = draw_px
            risk = fiche.entry - fiche.stop
            reward = fiche.target - fiche.entry
        else:
            fiche.stop = max(a_high, max(c.high for c in entry[-8:])) * 1.001
            fiche.target = draw_px
            risk = fiche.stop - fiche.entry
            reward = fiche.entry - fiche.target
        if risk > 0:
            rr = reward / risk
            fiche.rr = rr
            risk_ok = rr >= min_rr
            risk_detail = f"R:R {rr:.2f} (min {min_rr})"
        else:
            risk_detail = "non-positive risk"
    fiche.checks.append(Check("risk", risk_ok, risk_detail))

    fiche.reasons = [f"{c.name}: {c.detail}" for c in fiche.checks]
    return fiche
