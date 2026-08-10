# Watchdog — the dead-man's switch

An off-host liveness check for the poller. Each successful run stamps a heartbeat file
over SSH; this timer alerts when the stamps stop.

## Why it is inverted

The poller runs on GitHub Actions and reports to Slack, so a dead poller and a quiet job
market produce the same observable: silence. A run can also sit in `queued` without ever
being assigned a runner, and because a waiting run holds the concurrency group, every
scheduled run behind it is cancelled in turn.

Nothing inside the poller can detect that, because the detector would have to run. So the
check lives outside it and treats **silence as the alarm** rather than waiting for an
error to be reported.

## Design constraints

**It must not share fate with what it watches.** A check that runs inside the system it
monitors reports healthy right up until both stop together. This runs on separate
always-on hardware, on its own schedule, over a different notification channel.

**It must not depend on an expiring credential.** Polling the GitHub Actions API would
need a token, and fine-grained tokens expire silently. A watchdog that dies with its own
credential defeats its purpose. Instead the workflow pushes a heartbeat over SSH with a
key locked to a forced `touch` command, which can do nothing else and does not expire.

## Behaviour

* **Alerts on staleness, not on failure.** The trigger is "last success is older than the
  grace window", which covers every cause of silence at once: crashed run, cancelled
  schedule, throttled queue, deleted workflow.
* **The grace window is sized to the measured cadence, not the cron.** The cron asks for
  hourly; GitHub delivers a measured 8-21 runs/day, median gap ~97 min. Anything under
  ~4h would false-alarm on a normal quiet stretch, so it is set to 6h.
* **Re-nags every 6h while down**, not every run. An alert that repeats every 30 minutes
  gets muted, and a muted alert is not an alert.
* **Sends an explicit all-clear** on recovery, so an alert always has a closing message.
* **Treats a missing heartbeat as dead**, not healthy. The "never stamped at all" case is
  the one a naive check passes.
* **Reports its own liveness** once a day, so silence from the watchdog is
  distinguishable from health of the system.

## Layout

| where | what |
|---|---|
| `automation/job-finder-watch.sh` | the check, installed to `/usr/local/bin/` on the host |
| host `/etc/systemd/system/job-finder-watch.{service,timer}` | runs it every 30 min |
| host `/etc/job-finder-watch.env` | the push key, mode 600, **not in git** |
| host `/var/lib/job-finder-watch/heartbeat` | stamped by each successful poll |
| host `~/.ssh/authorized_keys` | the forced-command entry, **not in git** |
| repo secrets `WATCHDOG_SSH_KEY`, `WATCHDOG_HOST` | used by the workflow's last step |

Both secrets are optional. Without them the workflow skips the heartbeat step and the
pipeline runs unchanged.

The unit files and the two credential files live on the host and are deliberately not in
this repo. The script itself is committed so that a rebuilt host can be restored from
source rather than from memory.

## Testing the alert path

Backdate the heartbeat past the grace window and start the service by hand. Reset the
state file's `last_alert=0` first, or the re-nag gate will correctly suppress the push.

## Limitations

An external uptime monitor does a **port** check on the host rather than a ping, since
many cloud providers drop inbound ICMP and a ping check would report the host permanently
down.

That proves the host is reachable, not that the timer is still running. Those come apart
if systemd stops the timer while the host stays up, which is what the daily liveness push
covers.
