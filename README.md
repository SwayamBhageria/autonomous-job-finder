# Job Finder

An autonomous job-discovery pipeline. It polls **235 companies' own career boards**,
pulls roughly **35,000 job postings per run**, scores the new ones against a candidate
profile using an LLM as judge, and pushes only the good matches to Slack.

It runs entirely on GitHub Actions' free tier, commits its own state back to the repo,
and needs no server, no database, and no paid API. Two dependencies: `requests` and
`PyYAML`.

```
235 career boards
      │
      ▼   20 ATS adapters, normalised to one schema
 ~35,000 postings per run ───────────┐
      │                              │
      ▼                              ▼
 title / location match      board health + supply
      │                      anomaly detection ──▶ ⚠ alerts
      ▼
 new-only dedupe
      │
      ▼
 heuristic pre-filter ──▶ wrong stack / too senior ──▶ ✕
      │
      ▼
 Gemini fit-gate  (LLM-as-judge, schema-constrained output)
      │
      ▼
 A-tier ──▶ referral workup + outreach drafts
      │
      ▼
 Slack  +  HTML cockpit
      │
      ▼
 heartbeat ──▶ off-host dead-man's switch
```

## Why it exists

Job boards optimise for the employer. Aggregators are stale, spam-filled, and rank by
recency rather than fit. The roles worth applying to are posted on companies' *own*
boards, and the applications that convert are the ones sent within hours of posting.

So: skip the aggregators, poll the source, and let a model do the reading.

## Features

* **235 career boards** behind **20 ATS adapters**, plus a local inbox seam for boards
  that expose no usable API.
* One normalised job schema, so nothing downstream knows which ATS a role came from.
* Two-stage scoring: a cheap keyword heuristic, then an LLM fit-gate, so LLM calls are
  spent only on plausible roles.
* Slack alerts per match, plus an HTML cockpit with apply links, referral and alumni
  search, an inferred email pattern, and a drafted outreach message.
* Four independent layers of failure detection, plus an off-host dead-man's switch.
* No database. State is JSON committed back to the repo by the workflow.

## Architecture

### Adapter layer (`ats.py`)

Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Eightfold, Keka, Darwinbox,
iCIMS, Rippling and Oracle each expose jobs differently: different auth, pagination, and
response shapes. Amazon, Uber, Google and Atlassian don't use a standard ATS and have
bespoke adapters. Every one returns the same dict, so the rest of the pipeline is
ATS-agnostic.

Adding a board is one line in `companies.yaml`. Adding an ATS is one function returning
the normalised dict, plus a line in the dispatch table.

Two implementation notes that generalise to most third-party board APIs:

* **Single-page career portals serve the same shell on every route**, so probing paths
  finds nothing. The endpoint map is usually in the site's own `main.js`.
* **A filtered API that ignores your filter still returns 200.** Some boards silently
  ignore unrecognised parameters rather than rejecting them (`countries` not `country`,
  `India` not `IND`, lowercase `pagesize`), and hand back page 1 of the *unfiltered
  global* list. Adapters pin parameter names to the site's own query-key bundle and
  assert the filter actually narrowed the result.

### Scoring (`fit.py`, `gemini_fit.py`)

`fit.py` applies a cheap keyword heuristic first, dropping roles with the wrong stack,
too much seniority, or an experience bar above target. Survivors go to `gemini_fit.py`,
which scores 0-100 using schema-constrained JSON output, batched to stay inside the free
tier. If the key is missing or the call fails, it retries with backoff and then falls
back to heuristic ranking, so the LLM is an upgrade rather than a hard dependency.

Thresholds are set to values the model actually emits. The scorer produces multiples of
5 and nothing between, so a threshold of 72 behaves identically to 75 and silently
discards the whole 70 band. For the same reason, the ceiling applied to a listing whose
description could not be read is pinned strictly *below* the pass mark, so an unreadable
listing can never score as a match.

### Failure detection (`poller.py`)

Catching each board's exception keeps one bad adapter from killing the run, but it only
covers boards that fail loudly. A board can return a partial result, or an empty list,
and look exactly like a board that is up and has nothing on it. So the pipeline asserts
positive expectations rather than the absence of errors:

| layer | catches | mechanism |
|---|---|---|
| adapter raises on total failure | board down, rate-limited burst | every sub-request failed, so raise instead of returning `[]` |
| partial-fetch warning | some sub-requests lost | warn when *n* of *m* list requests failed but jobs still came back |
| flap detection | intermittent failure | rolling 12-run window per board, not just consecutive-failure streaks |
| supply anomaly detection | silent partial loss, parser rot, reshaped board | compare each board against **its own 8-run median**, alert below 50% |

The supply check is deliberately mechanism-independent: it asserts the outcome (this
board should return roughly as many jobs as it usually does) rather than any particular
cause, so it catches a throttled fetch, a parser broken by a retemplated portal, and a
board that quietly changed shape, none of which raise. Counting failed sub-requests
instead would be noise, since a board's queries overlap heavily and a board can lose a
third of its requests while still returning every one of its jobs.

The baseline is a median so one bad run cannot move it, and it re-baselines: a board that
settles at a new normal stops alerting rather than nagging forever.

### State (`outreach_store.py`)

State lives in `state/*.json`, committed back by the workflow. Scheduled runs can overlap,
so the push does rebase-and-retry rather than risk losing a write and re-alerting a whole
batch. Roles are deduplicated by *role identity* (company, normalised title, location)
rather than requisition ID, so reposts and duplicate reqs stay quiet. Each key stores its
verdict alongside it and is re-swept when scoring changes, since a dedup memory outlives
the logic that filled it.

Anything that accumulates is re-checked and capped rather than grown append-only; a list
of open roles is mostly dead links within weeks.

### Watchdog (`automation/`)

The poller reports to Slack, so a dead poller and a quiet job market look the same from
outside: silence. Nothing inside the pipeline can detect that, because the detector would
have to run. Each successful run stamps a heartbeat on separate always-on hardware, and
silence past a grace window is the trigger. It avoids polling the GitHub API on purpose,
since that needs a token and tokens expire; a watchdog that dies with its own credential
defeats the point. See [`automation/`](automation/).

### Tiers

Cold applications convert at a few percent; referral-backed ones convert far better.
A-tier roles get the full workup (referral search links, alumni lookup, inferred email
pattern, drafted outreach message); B-tier becomes a compact bulk-apply list. The band is
`[bulk_threshold, notify_threshold)`, so setting the floor equal to the ceiling empties it
and runs A-tier only.

## Setup

```bash
pip install -r requirements.txt
cp profile.example.yaml profile.yaml   # then fill it in, it's gitignored
python poller.py --dry-run             # score and print, touch nothing
```

`profile.yaml` holds your identity, CV text, and salary floor. It is gitignored and must
stay that way.

### Environment

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | no | Enables the LLM fit-gate; falls back to heuristics without it |
| `SLACK_WEBHOOK_URL` | no | Where alerts go; prints to stdout without it |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | no | Optional aggregator adapter; no-ops when unset |
| `WATCHDOG_SSH_KEY` / `WATCHDOG_HOST` | no | Heartbeat target; the step skips when unset |

Every one is optional and the pipeline degrades gracefully without them.

### Usage

```bash
python poller.py              # normal scheduled run
python poller.py --dry-run    # score + print, no Slack, no state written
python poller.py --backlog    # score every open match → queue.html
python poller.py --seed       # record current matches silently (first run)
python poller.py --outreach   # re-render the cockpit from committed state
```

Use `--dry-run` when testing changes. A normal run writes state, and a role recorded as
already-seen will not alert again.

### Scheduling

`.github/workflows/poll.yml` runs the poller and commits state back. GitHub **throttles
scheduled workflows** on free runners: an hourly cron here delivers a measured 8-21 runs a
day, median gap around 97 minutes. Derive intervals from stored timestamps rather than a
run count, and size health thresholds against the real cadence rather than the requested
cron.

## Configuration

| File | What it controls |
|---|---|
| `companies.yaml` | The 235 boards plus the inbox seam: `name`, `ats`, `token` (a public board slug) |
| `filters.yaml` | Title/location matching, score thresholds, per-run and per-company caps |
| `profile.yaml` | Your identity, CV, and preferences (gitignored) |

A board earns its place by producing roles the candidate could realistically get, checked
before it lands; plenty of plausible-looking boards carry no relevant roles at all. That
applies to adding, though, not pruning: a quiet board is not a broken one.

## Layout

| File | Role |
|---|---|
| `poller.py` | Orchestrator: the pipeline, plus board health and supply anomaly detection |
| `ats.py` | Board adapters, normalised to one shape |
| `fit.py` | Cheap heuristic pre-score |
| `gemini_fit.py` | LLM-as-judge fit-gate with fallback |
| `contacts.py` | Referral, alumni, and ex-colleague search links; email pattern inference |
| `outreach.py` | Drafts a personalised outreach message per A-tier role |
| `outreach_store.py` | Durable role tracker behind the cockpit |
| `outreach_page.py` | The HTML cockpit: apply, ask, follow up |
| `apply_queue.py` | Ranked backlog page for `--backlog` sweeps |
| `notify.py` | Slack formatting, board-health and supply alerts |
| `automation/` | The off-host dead-man's switch |

## Notes

Outreach is deliberately **assisted, not automated**. The tool drafts messages and finds
the right people, but sending stays manual. Automating LinkedIn outreach risks the
account, and the volume here sits well within what a person can send by hand.
