"""Contract specs, straight from OKX.

A perpetual is not a quantity of coin, it is a number of contracts. BTC-USDT-SWAP
moves in whole contracts of ctVal BTC each, at prices that must land on tickSz.
A size of 18.44 contracts and a stop at 79,414.512 do not exist on the exchange,
so a desk that computes them is measuring a trade it could never place.

These numbers are per instrument and OKX can change them, so they are fetched
from /public/instruments and cached — never hardcoded. Guessing a lot size is
how a backtest ends up describing a position the exchange would reject.

    python -m ict.instruments          # refresh the cache (needs OKX access)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

from ict.okx_data import _get

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "instruments.json"


@dataclass(frozen=True)
class Spec:
    inst_id: str
    ct_val: float          # base units per contract (BTC-USDT-SWAP: 0.01 BTC)
    ct_val_ccy: str
    lot_sz: float          # contract increment
    min_sz: float          # smallest order, in contracts
    tick_sz: float         # price increment

    @property
    def is_swap(self) -> bool:
        return self.inst_id.endswith("-SWAP")


def _round_step(value: float, step: float, mode: str = "down") -> float:
    """Snap to a multiple of `step`. Steps are decimal, so work in integers."""
    if step <= 0:
        return float(value)
    units = float(value) / step
    if mode == "up":
        units = math.ceil(round(units, 9))
    elif mode == "near":
        units = round(units)
    else:
        units = math.floor(round(units, 9))
    # step can be 0.1 or 0.001; rebuild through a decimal-safe multiplication
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    return round(units * step, decimals + 2)


def round_price(price: float, spec: Spec, mode: str = "near") -> float:
    """Prices must sit on the tick. A stop that does not is rejected."""
    return _round_step(price, spec.tick_sz, mode)


def contracts_for(base_qty: float, spec: Spec) -> float:
    """Base units -> whole contracts, rounded DOWN to the lot.

    Down, never up: rounding up would risk more than the desk decided to risk,
    and the whole point of the size is that the risk is chosen.
    """
    raw = float(base_qty) / spec.ct_val if spec.ct_val else 0.0
    lots = _round_step(raw, spec.lot_sz, "down")
    return lots if lots >= spec.min_sz else 0.0


def base_qty(contracts: float, spec: Spec) -> float:
    return float(contracts) * spec.ct_val


def fetch_specs(inst_type: str = "SWAP") -> dict[str, Spec]:
    payload = _get("/public/instruments", {"instType": inst_type})
    out: dict[str, Spec] = {}
    for row in payload.get("data") or []:
        inst = row.get("instId")
        if not inst:
            continue
        try:
            out[inst] = Spec(
                inst_id=inst,
                ct_val=float(row.get("ctVal") or 1),
                ct_val_ccy=str(row.get("ctValCcy") or ""),
                lot_sz=float(row.get("lotSz") or 1),
                min_sz=float(row.get("minSz") or 1),
                tick_sz=float(row.get("tickSz") or 0.1),
            )
        except (TypeError, ValueError):
            continue
    return out


def save(specs: dict[str, Spec], path: Path | None = None) -> Path:
    dest = path or CACHE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({k: asdict(v) for k, v in sorted(specs.items())}, indent=2),
        encoding="utf-8",
    )
    return dest


def load(path: Path | None = None) -> dict[str, Spec]:
    src = path or CACHE
    if not src.exists():
        return {}
    raw = json.loads(src.read_text(encoding="utf-8"))
    return {k: Spec(**v) for k, v in raw.items()}


_MEMO: dict[str, Spec] = {}


def spec(inst_id: str, *, allow_fetch: bool = True) -> Spec:
    """The instrument's real specs. Raises rather than guessing.

    A wrong lot size silently changes every position size in the record, so a
    missing cache is an error to fix, not a default to invent.
    """
    if inst_id in _MEMO:
        return _MEMO[inst_id]
    cached = load()
    if inst_id not in cached and allow_fetch:
        cached = fetch_specs("SWAP" if inst_id.endswith("-SWAP") else "SPOT")
        if cached:
            save({**load(), **cached})
    if inst_id not in cached:
        raise RuntimeError(
            f"no contract spec for {inst_id}. Run `python -m ict.instruments` "
            "where OKX is reachable to build data/instruments.json."
        )
    _MEMO.update(cached)
    return cached[inst_id]


def clear_memo() -> None:
    _MEMO.clear()


def main() -> int:
    specs = fetch_specs("SWAP")
    path = save({**load(), **specs})
    print(f"{len(specs)} SWAP instruments -> {path}")
    for inst in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        if inst in specs:
            s = specs[inst]
            print(f"  {inst}: 1 contrat = {s.ct_val} {s.ct_val_ccy}, "
                  f"lot {s.lot_sz}, min {s.min_sz}, tick {s.tick_sz}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
