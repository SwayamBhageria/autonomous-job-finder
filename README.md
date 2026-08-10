# Job Finder

An autonomous job-discovery pipeline. It polls **211 companies' own career boards**,
pulls roughly **27,000 job postings per run**, scores the new ones against a candidate
profile using an LLM as judge, and pushes only the genuinely good matches to Slack.

It runs entirely on GitHub Actions' free tier, commits its own state back to the repo,
and needs no server, no database, and no paid API. Two dependencies: `requests` and
`PyYAML`.

```
211 career boards
      │
      ▼   20 ATS adapters, normalised to one schema
 ~27,000 postings per run ───────────┐
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

## What it does

* Polls **211 career boards** through **20 ATS adapters**, plus a local inbox seam for
  boards that expose no usable API at all.
* Normalises every board's response shape into one dict, so the rest of the pipeline
  never learns which ATS a role came from.
* Filters by title and location, deduplicates by *role identity*, and scores what
  survives with a two-stage funnel: a cheap keyword heuristic, then an LLM fit-gate.
* Produces a Slack alert per good role plus an HTML cockpit for working the list:
  apply links, referral and alumni search, an inferred email pattern, and a drafted
  outreach message.
* Watches itself. Board health, supply anomalies, and an off-host dead-man's switch all
  report independently of the pipeline they monitor.

## Engineering highlights

**20 ATS adapters behind one interface.** Greenhouse, Lever, Ashby, Workday,
SmartRecruiters, Workable, Eightfold, Keka, Darwinbox, iCIMS, Rippling, Oracle and
others each expose jobs differently: different auth, pagination, and response shapes,
some with no documented API at all. `ats.py` normalises all of them to one dict. Amazon,
Uber, Google and Atlassian needed bespoke adapters because their boards don't use a
standard ATS.

One of them was cracked by reading the site's own JavaScript bundle rather than watching
the network tab. Single-page career portals serve the same shell on every route, so
path-guessing finds nothing, while `main.js` carries the endpoint map outright.

**A filtered API that ignores your filter still returns 200.** One board's parameter names
are unforgiving and wrong ones are silently ignored rather than rejected: `countries` not
`country`, spelled out (`India`, not `IND`), lowercase `pagesize`. Get any of them wrong
and you get a perfectly valid 200 holding page 1 of the *unfiltered global* list. So the
adapter pins the names to the site's own query-key bundle and asserts the filter actually
narrowed: India is a small slice of a ~670-role global board, so a full first page means
the filter did not apply.

**Four layers of failure detection.** Fault isolation, catching each board's exception so
one bad adapter can't kill the run, is the obvious design and it is not sufficient. The
expensive failures never raise at all:

* An adapter fans a board out into (query × page) sub-requests. If each one swallows its
  own failure into an empty list, a tenant rate-limiting the whole burst returns `[]`
  with no exception. Downstream that is indistinguishable from "the board is up and has
  nothing on it". Observed live: one board returned `61 → 0 → 59 → 0` across four
  consecutive runs while every health check reported green.
* Partial loss is harder still. A board returning **1 job out of 20** passes every
  "did it throw?" check ever written.

So the pipeline asserts positive expectations rather than the absence of errors:

| layer | catches | mechanism |
|---|---|---|
| adapter raises on total failure | board down, rate-limited burst | every sub-request failed, so raise instead of returning `[]` |
| partial-fetch warning | some sub-requests lost | warn when *n* of *m* list requests failed but jobs still came back |
| flap detection | intermittent failure | rolling 12-run window per board, not just consecutive-failure streaks |
| supply anomaly detection | silent partial loss, parser rot, reshaped board | compare each board against **its own 8-run median**, alert below 50% |

The last one is mechanism-independent, which is the point. It catches a rate-limited
fetch, a parser that broke on a retemplated portal, and a board that quietly changed
shape, none of which raise anything. The baseline is a median so a single bad run can't
move it, and it re-baselines naturally: a board that stays low stops alerting instead of
becoming noise.

**Alerting on outcomes, not mechanisms.** The intuitive supply metric is *how many
sub-requests failed*, and it is the wrong one. A board's queries overlap heavily, so one
board lost 6 of 18 requests and still returned all 73 of its jobs. Reporting that would
flag several boards a run that lost nothing. Assert the thing you actually care about
(this board should return roughly as many jobs as it usually does) rather than the thing
that happens to be easy to measure.

**An off-host dead-man's switch.** The poller reports to Slack, so a dead poller and a
quiet job market produce the same thing on your phone: silence. Nothing inside the
pipeline can catch that, because the detector would have to run. So each successful run
stamps a heartbeat on separate always-on hardware, and *silence* is the trigger. It
deliberately avoids polling the GitHub API, which would need a token, and tokens expire
silently. A watchdog that dies with its own credential is the exact failure it exists to
prevent. See [`automation/`](automation/).

**Two-stage scoring, because LLM calls aren't free.** A cheap keyword heuristic
(`fit.py`) drops obviously wrong roles first: wrong stack, too senior, experience bar too
high. Only survivors reach the Gemini fit-gate (`gemini_fit.py`), which scores 0-100
using schema-constrained JSON output, batched to stay inside the free tier. If the key is
missing or the call fails, it retries with backoff and then falls back to heuristic
ranking, so the LLM is an upgrade rather than a hard dependency.

**Thresholds derived from the model's output distribution.** The scorer emits multiples
of 5 and nothing in between, so a threshold of 72 is not "slightly above 70", it is
*exactly* 75, and it silently discards the entire 70 bucket. A related rule falls out of
the same observation: the ceiling applied to a listing whose description could not be
read must sit strictly *below* the pass mark, or every unknown lands exactly on it and
"we couldn't read this" scores the same as "this is a strong match". Set thresholds from
the values a model actually produces, not from round numbers.

**Idempotent state on a repo, no database.** State lives in `state/*.json`, committed
back by the workflow. Scheduled runs can overlap, so the push does rebase-and-retry:
losing a state write would mean re-alerting every role in that batch. Roles are
deduplicated by *role identity* (company, normalised title, location) rather than
requisition ID, so reposts and duplicate reqs stay quiet. The stored verdict lives
alongside the key and is re-swept whenever scoring changes, since a dedup memory outlives
the scoring logic that filled it.

**Two tiers, because effort should match odds.** Cold applications convert at a few
percent; referral-backed ones convert far better. A-tier roles get the full workup:
referral search links, alumni lookup, inferred email pattern, a drafted outreach message.
B-tier becomes a compact bulk-apply list. The band is `[bulk_threshold,
notify_threshold)`, so setting the floor equal to the ceiling empties it and runs A-tier
only. One number, reversible.

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
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | no | Optional aggregator; no-ops when unset |
| `WATCHDOG_SSH_KEY` / `WATCHDOG_HOST` | no | Heartbeat target; the step skips when unset |

Every one is optional and the pipeline degrades gracefully without them.

### Running

```bash
python poller.py              # normal scheduled run
python poller.py --dry-run    # score + print, no Slack, no state written
python poller.py --backlog    # score every open match → queue.html
python poller.py --seed       # record current matches silently (first run)
python poller.py --outreach   # re-render the cockpit from committed state
```

Use `--dry-run` when testing changes. A normal run writes state, so a role marked
already-seen will not alert again.

### Scheduling

`.github/workflows/poll.yml` runs the poller and commits state back. Note that GitHub
**throttles scheduled workflows** on free runners: an hourly cron here delivers a measured
8-21 runs a day, median gap around 97 minutes. Derive intervals from stored timestamps
rather than from a run count, and size any health threshold against the measured cadence
rather than the cron you asked for.

## Configuration

| File | What it controls |
|---|---|
| `companies.yaml` | The 211 boards plus the inbox seam: `name`, `ats`, `token` (a public board slug) |
| `filters.yaml` | Title/location matching, score thresholds, per-run and per-company caps |
| `profile.yaml` | Your identity, CV, and preferences (gitignored) |

Adding a company is one line. Adding a new ATS is one function in `ats.py` returning the
normalised dict, plus a line in the dispatch table.

A board earns its place by producing roles you could realistically get, verified before it
lands. Several plausible-looking additions turned out to carry no engineering roles at
all, or only roles well above the target level. That governs *adding*, though: a quiet
board is not a broken one.

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
account, and the volume here is well within what a person can send by hand.

Anything that accumulates needs a re-check pass and a hard cap, not just a retention
window. A list of open roles is mostly dead links within weeks, so the cockpit re-checks
entries and caps its own size rather than growing append-only.
