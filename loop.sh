#!/usr/bin/env bash
# One scheduled run per hour stays alive and polls every 5 minutes.
# GitHub throttles "*/5" cron schedules to every 30-60 minutes in practice;
# a single long run with an inner loop gets real 5-minute coverage.
# The daily brief is sent from inside the loop at BRIEF_UTC (04:00 UTC =
# 08:00 Dubai) by whichever run is alive, with a marker so it goes once a day.
set -u
POLL=${POLL_SECONDS:-300}
LIMIT=${LOOP_SECONDS:-3300}          # 55 min, leaving room before the next hourly run
BRIEF_UTC=${BRIEF_UTC:-04:00}
end=$((SECONDS + LIMIT))

commit() {
  git add data alerts.json
  git diff --cached --quiet && return 0
  git -c user.name="oil-watch bot" -c user.email="actions@users.noreply.github.com" \
      commit -qm "price $(date -u +%Y-%m-%dT%H:%MZ)" && git pull -q --rebase && git push -q
}

send_brief() {
  BRIEF=true python fetch_price.py && { mkdir -p data; touch "data/.brief-$(date -u +%F)"; }
}

# brief / test are single-shot
if [ "${BRIEF:-false}" = "true" ] || [ "${TEST_ALERT:-false}" = "true" ]; then
  python fetch_price.py; rc=$?; commit; exit $rc
fi

while :; do
  python fetch_price.py || echo "cycle failed (continuing)"
  commit || echo "commit failed (continuing)"
  [ $SECONDS -ge $end ] && break

  # daily brief: sleep up to the brief time if it falls inside this poll gap,
  # otherwise send at the first poll after it (if not already sent today)
  today=$(date -u +%F); now=$(date -u +%s)
  target=$(date -u -d "$today $BRIEF_UTC" +%s)
  if [ ! -f "data/.brief-$today" ]; then
    d=$((target - now))
    if [ $d -gt 0 ] && [ $d -le $POLL ]; then sleep $d; send_brief; commit; continue; fi
    if [ $d -le 0 ]; then send_brief; commit; fi
  fi
  sleep "$POLL"
done
echo "loop done"
