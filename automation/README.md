# Watchdog — the dead-man's switch

## Why this exists

The poller runs on GitHub Actions and reports to Slack. That means **a dead poller
and a quiet job market produce exactly the same thing on your phone: silence.**

That is not hypothetical. It cost a 24-hour blackout: one run sat in `queued`
without ever being assigned a runner, and because a waiting run holds the
concurrency group, every scheduled run behind it was cancelled in turn. Nothing
polled. Not even the heartbeat fired. It was caught only by chance.

**Nothing inside the poller can ever catch this** — the detector would have to run.
So the watchdog lives on separate always-on hardware, and the logic is inverted:
each *successful* poll stamps a heartbeat, and **silence is what triggers the
alarm**.

## The two rules that make it a watchdog and not decoration

**1. It must not share fate with what it watches.** A checker that lives inside
the thing being checked reports "healthy" right up until both die together. This
one runs on a different machine, on a different schedule, with a different
notification channel.

**2. It must not depend on an expiring credential.** The obvious implementation —
poll the GitHub Actions API from the box — needs a token, and fine-grained tokens
expire silently at a year. *A watchdog that dies with its own credential is the
exact failure it exists to prevent.* Instead the workflow pushes a heartbeat over
SSH, using a key locked to a forced `touch` command that can do nothing else and
does not expire.

## Design choices worth stealing

* **Alert on staleness, not on failure.** The trigger is "last success is older
  than the grace window", which catches every cause of silence at once — crashed
  run, cancelled schedule, throttled queue, deleted workflow.
* **Size the grace window to the real cadence, not the cron.** The cron here says
  hourly; GitHub throttles it to a measured 8-21 runs/day, median gap ~97 min. A
  grace window under ~4h would false-alarm on a normal quiet stretch. It is set
  to 6h. *Do not derive a health threshold from the schedule you asked for.*
* **Re-nag slowly.** Every 6h while dead, not every run. An alert that repeats
  every 30 minutes gets muted, and a muted alert is not an alert.
* **Send an explicit all-clear.** An alert with no closing message trains you to
  ignore the alert.
* **A missing heartbeat counts as dead, not healthy.** The "never stamped at all"
  case is the one a naive check treats as fine.
* **Prove the watchdog itself is alive.** One "still OK" push a day, so the
  silence of the watchdog is distinguishable from the health of the system. This
  is the same failure one level up, and it is the level most people stop at.

## What is *not* in this repo

The systemd unit and timer, the environment file holding the push key, and the
`authorized_keys` forced-command entry all live on the host. Two of those hold
live credentials and are deliberately absent here.

The script itself is committed on purpose: the equivalent scheduler for another
project lived only on disk for months, which meant a rebuilt machine would have
silently dropped it. **If it is not in git, it does not survive the hardware.**

## Layout

| where | what |
|---|---|
| `automation/job-finder-watch.sh` | the check, installed to `/usr/local/bin/` on the host |
| host `/etc/systemd/system/job-finder-watch.{service,timer}` | runs it every 30 min |
| host `/etc/job-finder-watch.env` | the push key, mode 600, **not in git** |
| host `/var/lib/job-finder-watch/heartbeat` | stamped by each successful poll |
| host `~/.ssh/authorized_keys` | the forced-command entry, **not in git** |
| repo secrets `WATCHDOG_SSH_KEY`, `WATCHDOG_HOST` | used by the workflow's last step |

Both secrets are optional. Without them the workflow skips the heartbeat step and
the pipeline runs exactly as before.

## Testing the alert path

Backdate the heartbeat past the grace window and run the service by hand. Reset
the state file's `last_alert=0` first, or the re-nag gate will correctly suppress
the push and you will conclude it is broken when it is working.

## Who watches the watchdog

An external uptime monitor does a **port** check on the host — not a ping, since
many cloud providers drop inbound ICMP and a ping check would report the box
permanently down.

**What that does not cover:** it proves the host is *reachable*, not that the
timer is still running. Those come apart if systemd stops the timer while the box
stays up — which is what the daily "still alive" push is for.
