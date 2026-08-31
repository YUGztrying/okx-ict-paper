"""Stop / target. Conservative: stop wins if a print could be both."""

from __future__ import annotations


def exit_result(bias: str, last: float, stop: float, target: float) -> str | None:
    """Return 'loss' or 'win' if this print would fill a resting SL or TP."""
    last = float(last)
    stop = float(stop)
    target = float(target)
    side = (bias or "").lower()
    if side == "long":
        if last <= stop:
            return "loss"
        if last >= target:
            return "win"
        return None
    if side == "short":
        if last >= stop:
            return "loss"
        if last <= target:
            return "win"
        return None
    return None


def realized_r(entry: float, stop: float, target: float, result: str) -> float:
    risk = abs(float(entry) - float(stop))
    if not risk:
        return 0.0
    if result == "win":
        return abs(float(target) - float(entry)) / risk
    if result == "loss":
        return -1.0
    return 0.0
