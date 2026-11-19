"""
Risk and sizing helpers shared by the brief and the set-position form.

The rule: the market sets the stop distance (outside normal noise, beyond a
real level); the AED risk cap sets the barrels.  barrels = risk / (stop * AED).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

AED = 3.6725
RISK_CAP_AED = 500          # per-trade cap; change here (and in position.yml default)
MIN_BBL = 75                # Plus500 Oil minimum
STEP_BBL = 75               # Plus500 Oil unit size
DXB = timezone(timedelta(hours=4))


def barrels_for(risk_aed: float, stop_dist: float) -> int:
    """Largest whole number of Plus500 units whose loss at the stop stays within risk_aed."""
    if stop_dist <= 0:
        return 0
    raw = risk_aed / (stop_dist * AED)
    units = int(raw // STEP_BBL)
    return units * STEP_BBL


def risk_aed(barrels: float, stop_dist: float) -> float:
    return abs(stop_dist) * barrels * AED


def bars_to_ohlc(bars: list[tuple[datetime, float]], minutes: int = 30) -> list[dict]:
    """Aggregate close-only bars (e.g. 5-minute) into OHLC buckets of `minutes`."""
    out: dict[datetime, dict] = {}
    for t, c in bars:
        key = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
        b = out.get(key)
        if b is None:
            out[key] = {"t": key, "o": c, "h": c, "l": c, "c": c}
        else:
            b["h"] = max(b["h"], c); b["l"] = min(b["l"], c); b["c"] = c
    return [out[k] for k in sorted(out)]


def atr(ohlc: list[dict], n: int = 14) -> float | None:
    """Average true range over the last n bars."""
    if len(ohlc) < 2:
        return None
    trs = []
    for prev, cur in zip(ohlc, ohlc[1:]):
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


def key_levels(ohlc30: list[dict], now: datetime) -> dict:
    """Previous session high/low and the current session's high/low so far.
    A 'session' here runs 03:00-02:00 Dubai, like CME / Plus500."""
    def session_of(t: datetime):
        d = t.astimezone(DXB) - timedelta(hours=3)
        return d.date()
    if not ohlc30:
        return {}
    today = session_of(now)
    sessions: dict = {}
    for b in ohlc30:
        sessions.setdefault(session_of(b["t"]), []).append(b)
    keys = sorted(sessions)
    lv = {}
    if today in sessions:
        cur = sessions[today]
        lv["today_high"] = max(b["h"] for b in cur)
        lv["today_low"] = min(b["l"] for b in cur)
    prev_keys = [k for k in keys if k < today]
    if prev_keys:
        p = sessions[prev_keys[-1]]
        lv["prev_high"] = max(b["h"] for b in p)
        lv["prev_low"] = min(b["l"] for b in p)
        lv["prev_close"] = p[-1]["c"]
    return lv


def plan_lines(price: float, a: float | None, lv: dict, cap: float = RISK_CAP_AED) -> list[str]:
    lines = []
    if a:
        lines.append(f"Noise (30-min ATR): ${a:.2f}  →  a stop should sit at least ${1.0*a:.2f}–${1.5*a:.2f} beyond the level")
    else:
        lines.append("Noise (30-min ATR): not available")
    if lv:
        names = [("prev_high", "Yesterday high"), ("today_high", "Today high so far"),
                 ("today_low", "Today low so far"), ("prev_low", "Yesterday low")]
        rows = [(lv[k], label) for k, label in names if k in lv] + [(price, "◀ NOW")]
        lines.append("Levels:")
        for v, label in sorted(rows, key=lambda r: r[0], reverse=True):
            lines.append(f"  {v:7.2f}  {label}")
    lines.append(f"Sizing at AED {cap:.0f} max risk (stop distance → max barrels):")
    dists = [round(x, 2) for x in ([a, 1.5 * a, 2 * a] if a else [])] + [0.50, 1.00, 1.50]
    seen = set()
    for d in sorted(dists):
        if d in seen or d <= 0:
            continue
        seen.add(d)
        bb = barrels_for(cap, d)
        if bb >= MIN_BBL:
            lines.append(f"  ${d:.2f} stop → {bb} bbl (AED {risk_aed(bb, d):,.0f} at risk)")
        else:
            lines.append(f"  ${d:.2f} stop → none: even {MIN_BBL} bbl risks AED {risk_aed(MIN_BBL, d):,.0f} > cap. Skip or wait.")
    lines.append("A trade needs: level · trigger (closed 15/30-min bar beyond it) · stop beyond noise · target · barrels from the table.")
    return lines
