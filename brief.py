#!/usr/bin/env python3
"""
oil-watch brief: a daily (or on-demand) position/market brief by email.

Run by fetch_price.py when BRIEF=1. Everything here is DESCRIPTIVE —
prices, distances, ranges, headlines. No recommendations.

Dubai time is UTC+4 all year.
"""
from __future__ import annotations
import html as _html
import io, json, os, re, sys, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import fetch_price as fp

DXB = timezone(timedelta(hours=4))
AED = 3.6725
NEWS_FEEDS = [
    ("OilPrice", "https://oilprice.com/rss/main"),
    ("Rigzone", "https://www.rigzone.com/news/rss/rigzone_latest.aspx"),
]
# Scheduled events, UTC. (weekday: 0=Mon) -> (hour, minute, label)
EVENTS = [
    (1, 21, 30, "API inventory report"),
    (2, 15, 30, "EIA Weekly Petroleum Status Report"),
    (4, 18, 0, "Baker Hughes rig count"),
    (4, 20, 30, "CFTC positioning (COT)"),
]
MONTH_CODES = "FGHJKMNQUVXZ"


def dxb(t: datetime) -> str:
    return t.astimezone(DXB).strftime("%a %d %b %H:%M")


# ---------------------------------------------------------------- data
def yahoo_daily(symbol: str, rng: str = "1mo") -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval=1d")
    with urllib.request.urlopen(urllib.request.Request(url, headers=fp.UA), timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        if q["close"][i] is None:
            continue
        out.append({"t": datetime.fromtimestamp(t, timezone.utc), "o": q["open"][i],
                    "h": q["high"][i], "l": q["low"][i], "c": q["close"][i]})
    return out


def yahoo_bars(symbol: str, rng: str = "5d") -> list[tuple[datetime, float]]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval=5m")
    with urllib.request.urlopen(urllib.request.Request(url, headers=fp.UA), timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, timezone.utc), c)
            for t, c in zip(res.get("timestamp") or [], closes) if c is not None]


def front_and_next(meta_short_name: str | None) -> tuple[str | None, str | None]:
    """'Crude Oil Dec 26' -> ('CLZ26.NYM', 'CLF27.NYM')."""
    if not meta_short_name:
        return None, None
    m = re.search(r"([A-Z][a-z]{2}) (\d{2})$", meta_short_name)
    if not m:
        return None, None
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    mi, yy = months.index(m.group(1)), int(m.group(2))
    nm, ny = (mi + 1) % 12, yy + (1 if mi == 11 else 0)
    return f"CL{MONTH_CODES[mi]}{yy:02d}.NYM", f"CL{MONTH_CODES[nm]}{ny:02d}.NYM"


def _next_contract(sym: str) -> str | None:
    """'CLF27.NYM' -> 'CLG27.NYM'."""
    m = re.match(r"CL([FGHJKMNQUVXZ])(\d{2})\.NYM$", sym or "")
    if not m:
        return None
    i, yy = MONTH_CODES.index(m.group(1)), int(m.group(2))
    return f"CL{MONTH_CODES[(i + 1) % 12]}{yy + (1 if i == 11 else 0):02d}.NYM"


def _last_price(sym: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1d&interval=1d"
    with urllib.request.urlopen(urllib.request.Request(url, headers=fp.UA), timeout=20) as r:
        return json.load(r)["chart"]["result"][0]["meta"].get("regularMarketPrice")


def curve_m1m2() -> tuple[float | None, str]:
    """Spread between the contract being traded and the next month.
    Uses the configured contract (e.g. CLF27.NYM) so an expiring front month
    (which converges to ~0 in expiry week) is not mistaken for contango."""
    try:
        m1 = fp.SYMBOLS["WTI"]
        if not m1.endswith(".NYM"):
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(m1)}?range=1d&interval=1d"
            with urllib.request.urlopen(urllib.request.Request(url, headers=fp.UA), timeout=20) as r:
                meta = json.load(r)["chart"]["result"][0]["meta"]
            m1, _ = front_and_next(meta.get("shortName"))
        m2 = _next_contract(m1)
        if not (m1 and m2):
            return None, "contract months unknown"
        p1, p2 = _last_price(m1), _last_price(m2)
        if p1 is None or p2 is None:
            return None, "no price"
        return round(p1 - p2, 2), f"{m1[2:5]}–{m2[2:5]}"
    except Exception as e:  # noqa
        return None, f"unavailable ({type(e).__name__})"


def headlines(n: int = 3) -> list[tuple[str, str, str]]:
    items = []
    for src, url in NEWS_FEEDS:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=fp.UA), timeout=20) as r:
                root = ET.fromstring(r.read())
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                link = (it.findtext("link") or "").strip()
                if title:
                    items.append((src, title, pub, link))
        except Exception as e:  # noqa
            fp.log(f"rss {src}: {e!r}")
    def _ts(x):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(x[2])
        except Exception:  # noqa
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
    items.sort(key=_ts, reverse=True)
    out, seen = [], set()
    oil = re.compile(r"\b(oil|crude|brent|wti|opec|hormuz|refin|barrel|tanker|gasoline|diesel|"
                     r"inventor|eia|api|saudi|aramco|iran|iraq|shale|permian|lng|fuel)", re.I)
    for src, title, pub, link in items:
        if title in seen or not oil.search(title):
            continue
        seen.add(title)
        out.append((src, title, link))
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- derived
def market_context(quotes: dict, position: dict | None) -> dict:
    ctx = {}
    try:
        d = yahoo_daily(fp.SYMBOLS["WTI"])
        ranges = [b["h"] - b["l"] for b in d[-10:] if b["h"] and b["l"]]
        ctx["normal_day"] = round(sum(ranges) / len(ranges), 2) if ranges else None
        last5 = d[-5:]
        ctx["hi5"] = round(max(b["h"] for b in last5), 2) if last5 else None
        ctx["lo5"] = round(min(b["l"] for b in last5), 2) if last5 else None
        ctx["closes"] = [(b["t"], b["c"]) for b in d[-6:]]
    except Exception as e:  # noqa
        fp.log("daily context:", repr(e))
    ctx["m1m2"], ctx["m1m2_label"] = curve_m1m2()
    return ctx


def normal_day_line(ctx: dict, price: float, position: dict | None) -> str:
    nd = ctx.get("normal_day")
    if not nd:
        return ""
    s = f"Normal day (avg 10-session range): ${nd:.2f}"
    if position and position.get("close_out"):
        dist = abs(price - float(position["close_out"]))
        s += f" — close-out is {dist / nd:.1f} normal days away"
    return s


def session_now(now: datetime) -> str:
    h = now.astimezone(DXB).hour
    wd = now.astimezone(DXB).weekday()
    if wd == 5 and h >= 2 or wd == 6 or (wd == 0 and h < 3):
        return "market closed (weekend) — reopens Mon 03:00 Dubai"
    if 2 <= h < 3:
        return "daily break (02:00–03:00 Dubai)"
    if 3 <= h < 12:
        return "Asian session (London opens 12:00 Dubai)"
    if 12 <= h < 18:
        return "London session (US opens 18:00 Dubai)"
    return "US session (settlement 23:30 Dubai)"


def upcoming(now: datetime, n: int = 3) -> list[tuple[str, str]]:
    out = []
    for days in range(0, 8):
        day = (now + timedelta(days=days)).date()
        for wd, hh, mm, label in EVENTS:
            if day.weekday() != wd:
                continue
            t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)
            if t > now:
                out.append((t, label))
    # weekly close / open
    for days in range(0, 8):
        day = (now + timedelta(days=days)).date()
        if day.weekday() == 4:
            t = datetime(day.year, day.month, day.day, 22, 0, tzinfo=timezone.utc)
            if t > now:
                out.append((t, "Friday close — weekend gap risk until Mon 03:00 Dubai"))
    out.sort()
    res = []
    for t, label in out[:n]:
        delta = t - now
        hrs = delta.total_seconds() / 3600
        when = f"in {hrs:.0f}h" if hrs < 48 else f"in {delta.days}d"
        res.append((f"{dxb(t)} ({when})", label))
    return res


def position_block(pos: dict | None, price: float, ctx: dict) -> list[str]:
    if not pos or not pos.get("barrels"):
        return ["No position set (add one in alerts.json or the POSITION_JSON secret)."]
    side = pos.get("side", "buy").lower()
    sign = 1 if side == "buy" else -1
    bbl, entry = float(pos["barrels"]), float(pos["entry"])
    rate = float(pos.get("aed_per_usd", AED))
    pnl = sign * (price - entry) * bbl * rate
    per_dollar = bbl * rate
    lines = [f"{side.upper()} {bbl:.0f} bbl @ {entry:.2f} → P/L ≈ AED {pnl:+,.0f} at {price:.2f}",
             f"Each $1 move = AED {per_dollar:,.0f}"]
    nd = ctx.get("normal_day")
    if pos.get("close_out"):
        co = float(pos["close_out"])
        dist = (price - co) * sign
        s = f"Close-out {co:.2f}: {dist:+.2f} away (AED {dist * per_dollar:,.0f} of room)"
        if nd:
            s += f" = {abs(dist) / nd:.1f} normal days"
        lines.append(s)
    if pos.get("target"):
        tg = float(pos["target"])
        dist = (tg - price) * sign
        lines.append(f"Target {tg:.2f}: {dist:+.2f} away (AED {dist * per_dollar:+,.0f})")
    if pos.get("opened") and pos.get("funding_pct_per_night"):
        try:
            opened = datetime.fromisoformat(pos["opened"]).replace(tzinfo=timezone.utc)
            nights = max(0, (datetime.now(timezone.utc) - opened).days)
            cost = bbl * entry * float(pos["funding_pct_per_night"]) / 100 * nights * rate
            lines.append(f"Funding ≈ AED {cost:,.0f} so far ({nights} nights at {pos['funding_pct_per_night']}%/night)")
        except Exception:  # noqa
            pass
    return lines


# ---------------------------------------------------------------- chart
def yahoo_ohlc(symbol: str, rng: str = "5d", interval: str = "30m") -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval={interval}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=fp.UA), timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"t": datetime.fromtimestamp(t, timezone.utc), "o": o, "h": h, "l": l, "c": c})
    return out


def render_chart(q: dict, cfg: dict, title: str, hours: int = 30, candle_min: int = 30) -> bytes | None:
    """Candlestick chart of the last `hours`, `candle_min`-minute candles, Dubai time,
    with entry / stop / target / close-out / armed alert levels drawn across it."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.patches import Rectangle
    except Exception as e:  # noqa
        fp.log("matplotlib unavailable:", repr(e))
        return None
    candles = []
    try:
        candles = yahoo_ohlc(fp.SYMBOLS["WTI"], "5d", f"{candle_min}m")
    except Exception as e:  # noqa
        fp.log("chart ohlc:", repr(e))
    if not candles:  # fall back to building candles from close-only bars
        try:
            import risk
            closes = yahoo_bars(fp.SYMBOLS["WTI"], "5d")
        except Exception:  # noqa
            closes = q.get("bars") or []
        if not closes:
            return None
        import risk
        candles = risk.bars_to_ohlc(closes, candle_min)
    cutoff = candles[-1]["t"] - timedelta(hours=hours)
    candles = [c for c in candles if c["t"] >= cutoff]
    if not candles:
        return None

    width = timedelta(minutes=candle_min * 0.7)
    fig, ax = plt.subplots(figsize=(7.4, 3.9), dpi=130)
    up, down = "#1b8a4c", "#c62828"
    for c in candles:
        x = mdates.date2num(c["t"] - width / 2)
        mid = c["t"]
        col = up if c["c"] >= c["o"] else down
        ax.vlines(mid, c["l"], c["h"], color=col, linewidth=0.9, zorder=2)
        body_lo, body_hi = min(c["o"], c["c"]), max(c["o"], c["c"])
        h = max(body_hi - body_lo, 0.004)
        ax.add_patch(Rectangle((x, body_lo), mdates.date2num(c["t"] + width / 2) - x, h,
                               facecolor=col, edgecolor=col, linewidth=0.6, zorder=3))
    lows = [c["l"] for c in candles]; highs = [c["h"] for c in candles]
    t0, t1 = candles[0]["t"], candles[-1]["t"]

    pos = cfg.get("position") or {}
    levels = []
    if pos.get("barrels") and pos.get("entry"):
        levels.append((float(pos["entry"]), "#333333", "entry", "-"))
    if pos.get("barrels") and pos.get("stop"):
        levels.append((float(pos["stop"]), "#c62828", "stop", "-"))
    if pos.get("barrels") and pos.get("close_out"):
        levels.append((float(pos["close_out"]), "#7b0015", "close-out", "--"))
    if pos.get("barrels") and pos.get("target"):
        levels.append((float(pos["target"]), "#1b8a4c", "target", "--"))
    for a in cfg.get("alerts", []):
        if a.get("armed", True) and a.get("symbol", "WTI").upper() == "WTI":
            levels.append((float(a["level"]), "#c77d00", a.get("id", "alert"), ":"))

    # keep the view on the candles; include a level only if it is reasonably near
    lo, hi = min(lows), max(highs)
    span = max(hi - lo, 0.5)
    near = [l for l in levels if lo - span * 0.6 <= l[0] <= hi + span * 0.6]
    ys = lows + highs + [l[0] for l in near]
    ax.set_ylim(min(ys) - span * 0.05, max(ys) + span * 0.08)

    used: list[float] = []
    for y, col, label, style in near:
        ax.axhline(y, color=col, linestyle=style, linewidth=1, zorder=1)
        bump = sum(1 for u in used if abs(u - y) < span * 0.05)
        used.append(y)
        ax.annotate(f"{label} {y:.2f}", xy=(t0, y), xytext=(3 + bump * 90, 3), textcoords="offset points",
                    fontsize=7.5, color=col, zorder=5,
                    bbox=dict(facecolor="white", edgecolor="none", alpha=0.85, pad=1))
    last = candles[-1]["c"]
    ax.axhline(last, color="#555", linestyle=(0, (1, 3)), linewidth=0.8, zorder=1)
    ax.annotate(f"{last:.2f}", xy=(t1, last), xytext=(6, -3), textcoords="offset points",
                fontsize=7.5, color="#222", fontweight="bold")

    ax.set_xlim(t0 - timedelta(minutes=candle_min), t1 + timedelta(minutes=candle_min * 3))
    ax.set_title(title, fontsize=10, loc="left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=DXB))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=DXB))
    ax.tick_params(labelsize=8)
    ax.grid(True, alpha=0.2)
    ax.set_ylabel(f"WTI $/bbl ({fp.SYMBOLS['WTI']})", fontsize=8)
    fig.autofmt_xdate()
    fig.text(0.99, 0.01, f"{candle_min}-min candles, Dubai time · Yahoo (CME, delayed)",
             ha="right", fontsize=6.5, color="#777")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def html_wrap(title: str, text: str, has_chart: bool) -> str:
    body = "<br>".join(_html.escape(line) for line in text.split("\n"))
    img = '<p><img src="cid:chart" style="max-width:100%;height:auto"></p>' if has_chart else ""
    return (f"<html><body style='font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:14px;"
            f"line-height:1.45;color:#222'><h3 style='margin:0 0 8px'>{_html.escape(title)}</h3>"
            f"{img}<p>{body}</p></body></html>")


# ---------------------------------------------------------------- brief
def main() -> int:
    now = datetime.now(timezone.utc)
    cfg = fp.load_alerts()
    quotes = {}
    for name, sym in fp.SYMBOLS.items():
        try:
            quotes[name] = fp.yahoo_chart(sym)
        except Exception as e:  # noqa
            fp.log(f"{name}: fetch FAILED {e!r}")
    w = quotes.get("WTI")
    if not w or w["price"] is None:
        fp.send_email("OIL BRIEF — no data", "Could not fetch WTI from Yahoo. Check the Actions log.")
        return 1
    pos = cfg.get("position") or {}
    price = float(w["price"])
    ctx = market_context(quotes, pos)
    stale = w["time"] and now - w["time"] > timedelta(minutes=fp.STALE_MIN)

    chg = ""
    if w.get("prev_close"):
        pc = float(w["prev_close"])
        chg = f"{price - pc:+.2f} ({(price / pc - 1) * 100:+.2f}%) vs prev close {pc:.2f}"

    sec = []
    sec.append(f"WTI {price:.2f} ({fp.SYMBOLS['WTI']})  —  quote {dxb(w['time'])} Dubai"
               + ("  [STALE: market closed or feed late]" if stale else ""))
    if chg:
        sec.append(chg)
    sec.append(session_now(now))
    sec.append("")
    sec.append("POSITION")
    sec += position_block(pos, price, ctx)
    sec.append("")
    sec.append("MARKET")
    if w.get("day_low") and w.get("day_high"):
        lo, hi = float(w["day_low"]), float(w["day_high"])
        where = (price - lo) / (hi - lo) * 100 if hi > lo else 50
        sec.append(f"Day range {lo:.2f} – {hi:.2f} (price at {where:.0f}% of range)")
    if ctx.get("hi5"):
        sec.append(f"5-day range {ctx['lo5']:.2f} – {ctx['hi5']:.2f}")
    if ctx.get("normal_day"):
        sec.append(f"Normal day (avg 10-session range): ${ctx['normal_day']:.2f}")
    b = quotes.get("BRENT")
    if b and b["price"]:
        sec.append(f"Brent {float(b['price']):.2f}  ·  Brent–WTI {float(b['price']) - price:+.2f}")
    if ctx.get("m1m2") is not None:
        v = ctx["m1m2"]
        word = "flat" if abs(v) < 0.05 else ("backwardation" if v > 0 else "contango")
        sec.append(f"Curve {ctx['m1m2_label']}: {ctx['m1m2']:+.2f} ({word})")
    else:
        sec.append(f"Curve: {ctx.get('m1m2_label')}")
    sec.append("")
    sec.append("PLAN — levels, noise and size")
    try:
        import risk
        bars30 = risk.bars_to_ohlc(yahoo_bars(fp.SYMBOLS["WTI"], "5d"), 30)
        sec += risk.plan_lines(price, risk.atr(bars30), risk.key_levels(bars30, now),
                               float(cfg.get("risk_cap_aed", risk.RISK_CAP_AED)))
    except Exception as e:  # noqa
        sec.append(f"(plan unavailable: {type(e).__name__})")
    sec.append("")
    sec.append("NEXT EVENTS (Dubai time)")
    for when, label in upcoming(now):
        sec.append(f"{when}: {label}")
    sec.append("")
    sec.append("HEADLINES")
    hl = headlines(3)
    sec += [f"[{src}] {title}" for src, title, _ in hl] or ["(feeds unavailable)"]
    sec.append("")
    sec.append("Not in this brief: inventories (Wednesday), positioning (Friday), anything about where price goes next.")
    text = "\n".join(sec)

    title = f"OIL BRIEF {now.astimezone(DXB):%a %d %b %H:%M} — WTI {price:.2f}"
    png = render_chart(w, cfg, title)
    html = html_wrap(title, text, png is not None)
    # linkify headlines in the HTML version
    for src, t, link in hl:
        if link:
            html = html.replace(_html.escape(f"[{src}] {t}"),
                                f"[{_html.escape(src)}] <a href='{_html.escape(link)}'>{_html.escape(t)}</a>")
    ok = fp.send_email(title, text, html, png)
    fp.send_push(title, text.split("\n\n")[0], "default")
    fp.log("brief", "sent" if ok else "NOT sent")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
