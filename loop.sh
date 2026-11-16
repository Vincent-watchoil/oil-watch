#!/usr/bin/env bash
# One scheduled run per hour stays alive and polls every 5 minutes.
# GitHub throttles "*/5" cron schedules to every 30-60 minutes in practice;
# a single long run with an inner loop gets real 5-minute coverage.
set -u
POLL=${POLL_SECONDS:-300}
LIMIT=${LOOP_SECONDS:-3300}          # 55 min, leaving room before the next hourly run
end=$((SECONDS + LIMIT))

commit() {
  git add data alerts.json
  git diff --cached --quiet && return 0
  git -c user.name="oil-watch bot" -c user.email="actions@users.noreply.github.com" \
      commit -qm "price $(date -u +%Y-%m-%dT%H:%MZ)" && git pull -q --rebase && git push -q
}

# brief / test are single-shot
if [ "${BRIEF:-false}" = "true" ] || [ "${TEST_ALERT:-false}" = "true" ]; then
  python fetch_price.py; rc=$?; commit; exit $rc
fi

while :; do
  python fetch_price.py || echo "cycle failed (continuing)"
  commit || echo "commit failed (continuing)"
  [ $SECONDS -ge $end ] && break
  sleep "$POLL"
done
echo "loop done"
