# oil-watch

A free price watch that runs on GitHub's servers every ~5 minutes, with no
laptop involved:

* pulls WTI (CL=F) and Brent (BZ=F) front-month 5-minute bars from Yahoo
* keeps them in `data/` (one CSV per asset per month, plus `data/latest.json`)
* reads `alerts.json`; when a level is crossed it emails you and/or pushes to
  your phone, then disarms that alert so it fires once
* your newsdesk on the Mac syncs from here (`sync_cloud.py`) whenever it is on

## Files

| file | what |
|---|---|
| `fetch_price.py` | the whole job, standard library only |
| `alerts.json` | your levels and (optionally) your open position — edit this |
| `price-watch.yml` | the schedule; goes in `.github/workflows/` (see setup step 3) |
| `data/` | created by the first run |

## Set-up (once, about 15 minutes)

1. **GitHub account** — github.com, free plan. Verify the email.
2. **New repository** — "+" → New repository → name `oil-watch` → **Public** →
   Create. (Public is what lets the Mac and Claude read the data without a
   token. Nothing sensitive is in the repo: your position details go in a
   secret in step 5 if you want P/L in the alerts; levels alone say little.)
3. **Upload the files** — in the empty repo click "uploading an existing file",
   drag in `fetch_price.py`, `alerts.json`, `README.md` → Commit.
4. **Schedule** — Actions tab → "set up a workflow yourself" → delete the
   template text, paste the contents of `price-watch.yml` → Commit.
   The file name GitHub proposes (`main.yml`) is fine.
   Then Actions → "price-watch" → "Run workflow" to run it once by hand.
   After a minute `data/latest.json` should exist in the repo. If it does,
   the watch is live.
5. **Secrets** — Settings → Secrets and variables → Actions → New repository
   secret. Type the values there; never paste them into a chat.

   | name | value |
   |---|---|
   | `SMTP_HOST` | `smtp.mail.me.com` (iCloud) |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | your iCloud email address |
   | `SMTP_PASS` | an **app-specific password**: account.apple.com → Sign-In and Security → App-Specific Passwords → "+" |
   | `ALERT_TO` | the address that should receive alerts (can be the same) |
   | `NTFY_TOPIC` | optional push: install the free **ntfy** app on your phone, subscribe to a topic with a name nobody would guess (e.g. `vvd-oil-7f3k9q`), put that name here |
   | `POSITION_JSON` | optional: `{"side":"buy","barrels":450,"entry":88.64,"close_out":87.08}` — keeps your position out of the public file; the alert email then includes P/L |

6. **Tell Claude the repo name** (`<user>/oil-watch`) so the Mac's
   `CLOUD_REPO` can be set and the newsdesk starts syncing.

## Using it

Open a position → edit `alerts.json` (GitHub web or the GitHub app on your
phone, pencil icon → Commit):

```json
"alerts": [
  {"id": "stop-warning", "symbol": "WTI", "when": "below", "level": 87.30, "armed": true, "note": "close-out is 87.08"},
  {"id": "target",       "symbol": "WTI", "when": "above", "level": 88.65, "armed": true, "note": "exit"}
]
```

Each alert fires once and flips to `"armed": false`; set it back to `true`
to re-arm. Close the position → set `barrels` to 0 (or clear the secret).

## Limits — read these

* GitHub's 5-minute schedule is best-effort; under load it slips to 10–15.
  This is a 5–15 minute watch, not a tick watch. For a stop that must fire on
  the tick, use the broker's own alert or a Close-at-Loss order.
* Alerts are suppressed when the quote is older than 25 minutes (weekend,
  the daily 22:00–23:00 UTC break, a Yahoo outage), so you never get a stale
  alert — but you also get nothing until data is fresh again.
* GitHub pauses scheduled workflows on repositories with no activity for 60
  days and emails you; one click in the Actions tab re-enables it.
* Yahoo is a free, unofficial feed. If it changes, the Actions log will show
  the failure and the email channel will go quiet. Check the Actions tab if
  you have not seen a commit in `data/` for an hour on a trading day.
