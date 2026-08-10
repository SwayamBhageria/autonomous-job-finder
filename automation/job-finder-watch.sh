#!/bin/bash
# Dead-man's switch for the Job Finder poller.
#
# The poller runs on GitHub Actions and alerts to Slack. That means a dead poller
# and a quiet job market look EXACTLY the same from your phone: silence. It
# has already cost a 24-hour blackout (2026-07-25, a run stuck in `queued`
# holding the concurrency group), and nothing inside the poller can ever catch
# that, because the detector would have to run.
#
# So the logic is inverted: each SUCCESSFUL poll stamps a heartbeat file here
# over SSH, and this timer alerts when the stamps stop. Silence is the trigger.
#
# Deliberately NOT a GitHub API poll from this box: that would need a token, and
# fine-grained tokens expire (max 1 year, silently). A watchdog that dies when
# its own credential lapses is the exact failure this exists to prevent. The SSH
# key that writes the heartbeat is locked to a forced `touch` command and cannot
# do anything else, and it does not expire.
#
# Installed by Claude 2026-08-09. Namespaced away from the claude-usage-* units
# on this VM so the two cannot interfere.
set -u

HB=/var/lib/job-finder-watch/heartbeat
STATE=/var/lib/job-finder-watch/state
ENVF=/etc/job-finder-watch.env

GRACE_MIN=360     # 6h. Runs are nominally hourly but GitHub throttles the
                  # schedule hard — measured 8-21 runs/day, median gap 97 min —
                  # so anything under ~4h false-alarms on a normal quiet stretch.
RENAG_SEC=21600   # 6h: keep nagging while dead, but not every 30 minutes.
ALIVE_SEC=86400   # 24h "still alive" push, so the watchdog proves itself.

BARK_KEY=""
# shellcheck disable=SC1090
[ -r "$ENVF" ] && . "$ENVF"

bark() {  # $1=title  $2=body
    if [ -z "$BARK_KEY" ]; then
        echo "no BARK_KEY in $ENVF — cannot push: $1" >&2
        return 0
    fi
    # POST JSON, not GET /:key/:title — with a title path segment Bark treats
    # that segment as the whole message and drops the body (learned 2026-07-04).
    curl -s -m 15 -o /dev/null -X POST "https://api.day.app/push" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"device_key":sys.argv[1],"title":sys.argv[2],"body":sys.argv[3],"group":"job-finder"}))' \
              "$BARK_KEY" "$1" "$2")" || true
}

now=$(date +%s)
last_alert=0
last_alive=0
# shellcheck disable=SC1090
[ -r "$STATE" ] && . "$STATE"

if [ -f "$HB" ]; then
    age_min=$(( (now - $(stat -c %Y "$HB")) / 60 ))
else
    # Never stamped at all. Real on first install, and real if someone wipes the
    # state dir — either way it is not "healthy", so treat it as dead.
    age_min=99999
fi

if [ "$age_min" -ge "$GRACE_MIN" ]; then
    if [ $(( now - last_alert )) -ge "$RENAG_SEC" ]; then
        if [ "$age_min" -ge 99999 ]; then
            when="never (no heartbeat received yet)"
        else
            when="$(( age_min / 60 ))h $(( age_min % 60 ))m ago"
        fi
        bark "🔴 Job Finder has stopped" \
             "Last successful run: ${when}. Slack going quiet means nothing on its own — check GitHub Actions."
        last_alert=$now
    fi
elif [ "$last_alert" -ne 0 ]; then
    # Recovered. Say so explicitly: an alert with no closing message trains you
    # to ignore the alert.
    bark "🟢 Job Finder is back" "Ran ${age_min}m ago."
    last_alert=0
fi

if [ $(( now - last_alive )) -ge "$ALIVE_SEC" ]; then
    bark "✅ Job Finder watchdog OK" "Poller last ran ${age_min}m ago."
    last_alive=$now
fi

printf 'last_alert=%s\nlast_alive=%s\n' "$last_alert" "$last_alive" > "$STATE"
