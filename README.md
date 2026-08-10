# Job Finder

An autonomous job-discovery pipeline. It polls **212 companies' own career boards**
on a schedule, scores each new role against a candidate profile with an LLM acting
as judge, and pushes only the genuinely good matches to Slack.

It runs entirely on GitHub Actions' free tier, commits its own state back to the repo,
and needs no server, no database, and no paid API.

```
212 boards ──parallel fetch──▶ title/location match ──▶ new-only dedupe
     │                                                       │
     │  supply + health              ┌────────────────────────┘
     │  anomaly detection            ▼
     │              heuristic pre-score (cheap) ──drop wrong stack / too senior──▶ ✕
     │                               │
     ▼                               ▼
  ⚠ board alerts        Gemini fit-gate (0-100, LLM-as-judge)
                                     │
                                     ▼
                             A-tier (≥ 70)
                          full referral workup
                            + outreach drafts
                                     │
                                     ▼
                          Slack  +  HTML cockpit
                                     │
                     ┌───────────────┘
                     ▼
        heartbeat ──▶ off-box dead-man's switch
```

## Why it exists

Job boards optimise for the employer. Aggregators are stale, spam-filled, and rank by
recency rather than fit. The roles worth applying to are posted on companies' *own*
boards, and the applications that convert are the ones sent within hours of posting.

So: skip the aggregators, poll the source, and let a model do the reading.

## What's interesting in here

**22 ATS adapters, one interface.** Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
Workable, Eightfold, Keka, Darwinbox, iCIMS, Rippling, Oracle and others each expose jobs
differently — different auth, pagination, and shapes, some with no documented API at all.
`ats.py` normalises all of them to one dict. Amazon, Uber, Google and Atlassian needed
bespoke adapters because their boards don't use a standard ATS.

Two of those were only cracked by reading the site's own JavaScript bundle rather than
watching the network tab: single-page career portals serve the same shell on every route,
so path-guessing finds nothing, while `main.js` carries the endpoint map outright.

**A board that half-answers is the hard failure, not a board that dies.** This is the part
worth reading. Fault isolation — catching each board's exception so one bad adapter can't
kill the run — is the obvious design and it is *not enough*, because the expensive failures
never raise:

* An adapter fans a board out into (query × page) sub-requests. Each one swallowed its own
  failure into an empty list, so a tenant rate-limiting the whole burst returned `[]` with
  no exception. Downstream that is indistinguishable from "the board is up and has nothing
  on it": no error, no alert, ever. Measured live — one board went `61 → 0 → 59 → 0` across
  four consecutive runs while every health check reported green.
* Partial loss is worse still. One board returned **1 job from a 20-job board** and every
  "did it throw?" check passed.

So the pipeline asserts positive expectations instead of absence of errors, in four layers:

| layer | catches | mechanism |
|---|---|---|
| adapter raises on total failure | board down, rate-limited burst | every sub-request failed → raise, don't return `[]` |
| partial-fetch warning | some sub-requests lost | warn when `n` of `m` list requests failed but jobs still came back |
| flap detection | intermittent failure | rolling 12-run window per board, not just consecutive-failure streaks |
| supply anomaly detection | silent partial loss, parser rot, reshaped board | compare each board against **its own 8-run median**, alert below 50% |

The last one is the mechanism-independent check: it catches a rate-limited fetch, a parser
that broke on a retemplated portal, and a board that quietly changed shape — none of which
raise anything. The baseline is a median so one bad run can't move it, and it re-baselines
naturally, so a board that stays low stops nagging instead of becoming noise.

**The signal that looked right and wasn't.** The obvious supply metric — *how many
sub-requests failed* — was tried first and is wrong. A board's queries overlap heavily, so
one board lost 6 of 18 requests and still returned all 73 of its jobs. Reporting that would
flag 3-5 boards a run that lost nothing. Alert on the outcome you actually care about, not
on the mechanism you happen to be able to measure.

**Nothing inside a system can detect that the system stopped.** The poller reports to Slack,
so a dead poller and a quiet job market look identical from your phone. A 24-hour blackout
proved it. The fix is a dead-man's switch on separate hardware: each successful run stamps a
heartbeat, and *silence* is the trigger. It deliberately avoids the GitHub API, because that
needs a token, tokens expire silently, and a watchdog that dies with its own credential is
the exact failure it exists to prevent. See [`automation/`](automation/).

**Two-stage scoring, because LLM calls aren't free.** A cheap keyword heuristic (`fit.py`)
drops obviously wrong roles first — wrong stack, too senior, experience bar too high. Only
survivors reach the Gemini fit-gate (`gemini_fit.py`), which scores 0-100 with structured
JSON output, batched to stay inside the free tier. If the key is missing or the call fails,
the pipeline falls back to heuristic ranking rather than breaking — the LLM is an upgrade,
never a hard dependency.

**Scoring thresholds are a measurement problem.** The bar sat at 72 for months. It turned out
the model quantises its own output to multiples of 5, so a threshold of 72 is not "slightly
above 70" — it is *exactly* 75, silently discarding everything in the 70 bucket. Separately,
a swallowed parse error left some job descriptions empty, and an unread listing was being
capped at exactly the bar rather than below it, so every unknown landed on the pass mark.
**A suspiciously large bucket sitting precisely on a threshold is something clamping, not
something agreeing.**

**Idempotent state on a repo.** There's no database. State lives in `state/*.json`, committed
back by the workflow. Since scheduled runs can overlap, the push does rebase-and-retry:
losing a state write would mean re-alerting every role in that batch. Roles are deduplicated
by *role identity* (company + normalised title + location) rather than requisition ID, so
reposts and duplicate reqs stay quiet.

One caveat that took 17 silent runs to find: **a dedup memory outlives the scoring logic that
filled it.** After any change to how roles are scored, the stored verdicts are stale — so the
verdict is stored alongside the key and re-swept when scoring changes.

## Setup

```bash
pip install -r requirements.txt
cp profile.example.yaml profile.yaml   # then fill it in — it's gitignored
python poller.py --dry-run             # score and print, touch nothing
```

`profile.yaml` holds your identity, CV text, and salary floor. It is gitignored and
must stay that way.

### Environment

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | no | Enables the LLM fit-gate; falls back to heuristics without it |
| `SLACK_WEBHOOK_URL` | no | Where alerts go; prints to stdout without it |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | no | Optional aggregator; no-ops when unset |
| `WATCHDOG_SSH_KEY` / `WATCHDOG_HOST` | no | Heartbeat target for the dead-man's switch; step skips when unset |

### Running

```bash
python poller.py              # normal scheduled run
python poller.py --dry-run    # score + print, no Slack, no state written
python poller.py --backlog    # score every open match → queue.html
python poller.py --seed       # record current matches silently (first run)
python poller.py --outreach   # re-render the cockpit from committed state
```

`--dry-run` exists for a specific reason: running the poller locally to check a fix is
**not** read-only. One local run marked a live, high-scoring role as already-seen and
killed its alert. Use `--dry-run`, or diff `state/` before you commit.

### Scheduling

`.github/workflows/poll.yml` runs hourly and commits state back. Note that GitHub
**throttles scheduled workflows** on free runners — the measured reality here is 8-21 runs
a day, median gap ~97 minutes. Anything you derive from "runs per hour" will be off by
roughly 2x. Use stored timestamps, never a run count, and size health thresholds against
the measured cadence rather than the cron you asked for.

## Configuration

| File | What it controls |
|---|---|
| `companies.yaml` | The 212 boards — `name`, `ats`, `token` (a public board slug) |
| `filters.yaml` | Title/location matching, score thresholds, per-run and per-company caps |
| `profile.yaml` | Your identity, CV, and preferences (gitignored) |

Adding a company is one line. Adding a new ATS is one function in `ats.py` returning the
normalised dict, plus a line in the dispatch table.

**Boards are not a score to maximise.** A board earns its place by producing roles you could
actually get, verified before it lands — several plausible-looking additions turned out to
carry no engineering roles at all, or only roles years above the target level. But that
governs *adding*: a quiet board is not a broken one, and quiet is not grounds for removal.

## Layout

| File | Role |
|---|---|
| `poller.py` | Orchestrator — the pipeline, plus board health and supply anomaly detection |
| `ats.py` | 22 board adapters, normalised to one shape |
| `fit.py` | Cheap heuristic pre-score |
| `gemini_fit.py` | LLM-as-judge fit-gate with fallback |
| `contacts.py` | Referral, alumni, and ex-colleague search links; email pattern inference |
| `outreach.py` | Drafts a personalised outreach message per A-tier role |
| `outreach_store.py` | Durable role tracker behind the cockpit |
| `outreach_page.py` | The HTML cockpit — apply, ask, follow up |
| `apply_queue.py` | Ranked backlog page for `--backlog` sweeps |
| `notify.py` | Slack formatting, board-health and supply alerts |
| `automation/` | The off-box dead-man's switch |

## Notes

Outreach is deliberately **assisted, not automated** — the tool drafts messages and finds
the right people, but sending stays manual. Automating LinkedIn outreach risks the account,
and the volume here is well within what a person can send by hand.

Anything that accumulates needs a re-check pass and a hard cap, not just a retention window.
The cockpit was allowed to grow append-only and reached 222 rows of which only 31 still had
live links.

Dependencies are `requests` and `PyYAML`. That's the whole list.
