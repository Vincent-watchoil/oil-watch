#!/usr/bin/env python3
"""
oil-watch: runs on GitHub Actions every ~5 minutes.

  1. Pulls WTI (CL=F) and Brent (BZ=F) front-month 5-minute bars from Yahoo.
  2. Writes data/latest.json and appends to data/<asset>-5m-YYYY-MM.csv.
  3. Evaluates alerts.json; sends email and/or ntfy push when a level is crossed,
     then disarms that alert (edit alerts.json to re-arm).

No third-party packages: standard library only.
Secrets (GitHub repo settings -> Secrets and variables -> Actions):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_TO   (email; all optional)
  NTFY_TOPIC                                             (push via ntfy.sh; optional)
  POSITION_JSON                                          (optional; overrides "position" in alerts.json)
"""
from __future__ import annotations
import csv, json, os, smtplib, ssl, sys, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ALERTS = ROOT / "alerts.json"
UA = {"User-Agent": "Mozilla/5.0 (oil-watch; github actions)"}
SYMBOLS = {"WTI": "CL=F", "BRENT": "BZ=F"}
STALE_MIN = 25          # treat a quote older than this as stale -> no alerts
AED = 3.6725


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%SZ"), *a, flush=True)


# ---------------------------------------------------------------- Yahoo
def yahoo_chart(symbol: str) -> dict:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range=1d&interval=5m&includePrePost=false")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    meta = res["meta"]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    bars = [(datetime.fromtimestamp(t, timezone.utc), c) for t, c in zip(ts, closes) if c is not None]
    return {
        "symbol": symbol,
        "price": meta.get("regularMarketPrice"),
        "time": datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc) if meta.get("regularMarketTime") else None,
        "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "bars": bars,
    }


# ---------------------------------------------------------------- storage
def append_bars(asset: str, bars):
    if not bars:
        return 0
    DATA.mkdir(exist_ok=True)
    written = 0
    by_month: dict[str, list] = {}
    for t, c in bars:
        by_month.setdefault(t.strftime("%Y-%m"), []).append((t, c))
    for month, rows in by_month.items():
        f = DATA / f"{asset.lower()}-5m-{month}.csv"
        last = None
        if f.exists():
            with f.open() as fh:
                for line in fh:
                    pass
                if line and not line.startswith("ts_utc"):
                    last = line.split(",")[0]
        new = [(t, c) for t, c in rows if last is None or t.strftime("%Y-%m-%dT%H:%M:%SZ") > last]
        if not new:
            continue
        fresh = not f.exists()
        with f.open("a", newline="") as fh:
            w = csv.writer(fh)
            if fresh:
                w.writerow(["ts_utc", "close"])
            for t, c in new:
                w.writerow([t.strftime("%Y-%m-%dT%H:%M:%SZ"), f"{c:.2f}"])
                written += 1
    return written


# ---------------------------------------------------------------- notify
def send_email(subject: str, body: str, html: str | None = None, png: bytes | None = None) -> bool:
    """Plain-text email; with html (and an inline PNG referenced as cid:chart) when given."""
    host, user, pw, to = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_TO"))
    if not (host and user and pw and to):
        log("email: not configured, skipping")
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
        if png:
            msg.get_payload()[1].add_related(png, maintype="image", subtype="png", cid="<chart>")
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pw)
            s.send_message(msg)
        log("email: sent", subject)
        return True
    except Exception as e:  # noqa
        log("email: FAILED", repr(e))
        return False


def send_push(title: str, body: str, priority: str = "high") -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(),
                                     headers={"Title": title, "Priority": priority, "Tags": "oil_drum"})
        urllib.request.urlopen(req, timeout=20).read()
        log("push: sent", title)
        return True
    except Exception as e:  # noqa
        log("push: FAILED", repr(e))
        return False


# ---------------------------------------------------------------- alerts
def load_alerts() -> dict:
    cfg = json.loads(ALERTS.read_text()) if ALERTS.exists() else {"alerts": []}
    # The POSITION_JSON secret is only a fallback: a position set through the
    # "set position" form (alerts.json, barrels > 0) takes precedence.
    if os.environ.get("POSITION_JSON") and not (cfg.get("position") or {}).get("barrels"):
        cfg["position"] = json.loads(os.environ["POSITION_JSON"])
        cfg["_position_from_secret"] = True
    # Which WTI contract to track. Plus500's "Oil" rolls to the next month ~5 days
    # before CME expiry, while Yahoo's CL=F stays on the front month until expiry,
    # so for those days the two differ by the whole M1-M2 spread. Set
    # "wti_symbol": "CLF27.NYM" in alerts.json to follow the contract your broker
    # is on; "CL=F" (default) is the exchange front month.
    sym = (cfg.get("wti_symbol") or "CL=F").strip()
    if sym != SYMBOLS["WTI"]:
        SYMBOLS["WTI"] = sym
        log(f"WTI symbol: {sym}")
    return cfg


def pnl_line(pos: dict | None, price: float) -> str:
    if not pos or not pos.get("barrels"):
        return ""
    sign = 1 if pos.get("side", "buy").lower() == "buy" else -1
    pnl = sign * (price - float(pos["entry"])) * float(pos["barrels"]) * float(pos.get("aed_per_usd", AED))
    s = f"Position {pos.get('side','buy')} {pos['barrels']} bbl @ {pos['entry']} -> P/L ~ AED {pnl:+,.0f}"
    if pos.get("close_out"):
        s += f" | close-out {pos['close_out']} ({price - float(pos['close_out']):+.2f} away)"
    return s


def evaluate(cfg: dict, quotes: dict) -> bool:
    changed = False
    now = datetime.now(timezone.utc)
    for a in cfg.get("alerts", []):
        if not a.get("armed", True):
            continue
        q = quotes.get(a.get("symbol", "WTI").upper())
        if not q or q["price"] is None or q["time"] is None:
            continue
        if now - q["time"] > timedelta(minutes=STALE_MIN):
            log(f"alert {a.get('id')}: quote stale ({q['time']:%H:%MZ}), not evaluated")
            continue
        price, level, when = float(q["price"]), float(a["level"]), a.get("when", "below")
        hit = price <= level if when == "below" else price >= level
        if not hit:
            continue
        title = f"OIL ALERT {a.get('symbol','WTI')} {price:.2f} {when} {level}"
        body = "\n".join(filter(None, [
            f"{a.get('symbol','WTI')} {price:.2f} at {q['time']:%Y-%m-%d %H:%M} UTC "
            f"({(q['time'] + timedelta(hours=4)):%H:%M} Dubai)",
            f"Alert: {a.get('id')} — {a.get('note','')}".rstrip(" —"),
            f"Day range {q['day_low']} – {q['day_high']}, prev close {q['prev_close']}",
            pnl_line(cfg.get("position"), price),
            "This alert is now disarmed. Edit alerts.json to re-arm or change levels.",
        ]))
        html = png = None
        try:
            import brief as _b
            ctx = _b.market_context(quotes, cfg.get("position"))
            body += "\n" + _b.normal_day_line(ctx, price, cfg.get("position"))
            png = _b.render_chart(quotes["WTI"], cfg, title)
            html = _b.html_wrap(title, body, png is not None)
        except Exception as e:  # noqa
            log("alert chart skipped:", repr(e))
        sent = send_email(title, body, html, png) | send_push(title, body)
        a["armed"] = False
        a["fired_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        a["fired_price"] = round(price, 2)
        changed = True
        log("ALERT FIRED", title, "delivered" if sent else "(no channel configured)")
    return changed


# ---------------------------------------------------------------- main
def main() -> int:
    if os.environ.get("TEST_ALERT", "").lower() in ("1", "true", "yes"):
        body = ("This is a test of the oil-watch alert channel.\n"
                "If you are reading this, email delivery from GitHub Actions works.\n"
                f"Sent {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.")
        ok = send_email("OIL ALERT test — channel check", body) | send_push("OIL ALERT test", body, "default")
        log("test alert", "delivered" if ok else "NOT delivered — check secrets / Actions log")
        return 0 if ok else 1
    if os.environ.get("BRIEF", "").lower() in ("1", "true", "yes"):
        import brief
        return brief.main()
    cfg = load_alerts()
    quotes = {}
    for name, sym in SYMBOLS.items():
        try:
            quotes[name] = yahoo_chart(sym)
            log(f"{name}: {quotes[name]['price']} @ {quotes[name]['time']:%H:%MZ}, {len(quotes[name]['bars'])} bars")
        except Exception as e:  # noqa
            log(f"{name}: fetch FAILED {e!r}")
    if not quotes:
        return 1
    for name, q in quotes.items():
        n = append_bars(name, q["bars"])
        log(f"{name}: {n} new bars written")

    latest = {"as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for name, q in quotes.items():
        latest[name.lower()] = {
            "price": q["price"],
            "time_utc": q["time"].strftime("%Y-%m-%dT%H:%M:%SZ") if q["time"] else None,
            "prev_close": q["prev_close"], "day_high": q["day_high"], "day_low": q["day_low"],
            "stale": bool(q["time"] and datetime.now(timezone.utc) - q["time"] > timedelta(minutes=STALE_MIN)),
        }
    if "WTI" in quotes and "BRENT" in quotes and quotes["WTI"]["price"] and quotes["BRENT"]["price"]:
        latest["brent_wti"] = round(quotes["BRENT"]["price"] - quotes["WTI"]["price"], 2)
    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps(latest, indent=1))

    if "wti" in latest:
        latest["wti"]["symbol"] = SYMBOLS["WTI"]
        (DATA / "latest.json").write_text(json.dumps(latest, indent=1))
    if evaluate(cfg, quotes):
        if cfg.pop("_position_from_secret", False):
            cfg.pop("position", None)      # never write the secret back into the repo
        ALERTS.write_text(json.dumps(cfg, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
