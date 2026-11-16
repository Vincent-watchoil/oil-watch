#!/usr/bin/env python3
"""
Write the open position and its alert levels into alerts.json from simple
inputs (used by the "set position" workflow form). Everything else in the
file is preserved.

  python set_position.py --side buy --barrels 525 --entry 86.84 --equity 2730 \
                         --stop-warning 86.20 --target 88.40 [--contract CLF27.NYM]
  python set_position.py --barrels 0          # flat: clears position, disarms alerts

close_out is derived from equity: Plus500 liquidates when equity falls to the
maintenance margin (~0.334% of notional on Oil at 1:150), so
    room      = equity - 0.00334 * notional_aed
    close_out = entry -/+ room / (barrels * aed_per_usd)
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

ALERTS = Path(__file__).resolve().parent / "alerts.json"
AED = 3.6725
MAINT = 0.00334          # maintenance margin as a fraction of notional (AED 559 on $45,591 notional)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", default="buy", choices=["buy", "sell"])
    ap.add_argument("--barrels", type=float, required=True)
    ap.add_argument("--entry", type=float, default=0)
    ap.add_argument("--equity", type=float, default=0, help="account equity in AED")
    ap.add_argument("--stop-warning", type=float, default=None)
    ap.add_argument("--target", type=float, default=None)
    ap.add_argument("--contract", default="", help="Yahoo symbol Plus500 Oil is on, e.g. CLF27.NYM")
    ap.add_argument("--funding", type=float, default=0.0365, help="%% per night")
    a = ap.parse_args()

    cfg = json.loads(ALERTS.read_text()) if ALERTS.exists() else {"alerts": []}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alerts = [x for x in cfg.get("alerts", []) if x.get("id") not in ("stop-warning", "target")]

    if a.barrels <= 0:
        cfg["position"] = {"side": a.side, "barrels": 0, "entry": 0, "close_out": None, "aed_per_usd": AED}
        for x in alerts:
            x["armed"] = False
        cfg["alerts"] = alerts
        ALERTS.write_text(json.dumps(cfg, indent=1) + "\n")
        print("position cleared; all alerts disarmed")
        return 0

    if a.entry <= 0:
        print("entry is required for an open position", file=sys.stderr)
        return 2
    sign = 1 if a.side == "buy" else -1
    pos = {"side": a.side, "barrels": a.barrels, "entry": a.entry, "aed_per_usd": AED,
           "opened": today, "funding_pct_per_night": a.funding}
    per_dollar = a.barrels * AED
    if a.equity > 0:
        notional_aed = a.barrels * a.entry * AED
        room = a.equity - MAINT * notional_aed
        pos["close_out"] = round(a.entry - sign * room / per_dollar, 2)
        pos["equity_at_entry"] = a.equity
    if a.target:
        pos["target"] = a.target
    cfg["position"] = pos
    if a.contract:
        cfg["wti_symbol"] = a.contract.strip()

    if a.stop_warning:
        alerts.append({"id": "stop-warning", "symbol": "WTI",
                       "when": "below" if sign > 0 else "above", "level": a.stop_warning,
                       "armed": True, "note": f"warning level; close-out ~{pos.get('close_out')}"})
    if a.target:
        alerts.append({"id": "target", "symbol": "WTI",
                       "when": "above" if sign > 0 else "below", "level": a.target,
                       "armed": True, "note": "target"})
    cfg["alerts"] = alerts
    ALERTS.write_text(json.dumps(cfg, indent=1) + "\n")

    print(f"{a.side} {a.barrels:.0f} bbl @ {a.entry}  |  AED {per_dollar:,.0f} per $1")
    if "close_out" in pos:
        print(f"close-out ~{pos['close_out']} ({abs(a.entry - pos['close_out']):.2f} away, from equity AED {a.equity:,.0f})")
    print("alerts:", ", ".join(f"{x['id']} {x['when']} {x['level']}" for x in alerts if x.get("armed")) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
