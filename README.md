# Job Finder

An autonomous job-discovery pipeline. It polls **130 companies' own career boards**
every 30 minutes, scores each new role against a candidate profile with an LLM acting
as judge, and pushes only the genuinely good matches to Slack.

It runs entirely on GitHub Actions' free tier, commits its own state back to the repo,
and needs no server, no database, and no paid API.

```
130 boards ──parallel fetch──▶ title/location match ──▶ new-only dedupe
                                                            │
                          ┌─────────────────────────────────┘
                          ▼
              heuristic pre-score (cheap)  ──drop wrong stack / too senior──▶ ✕
                          │
                          ▼
              Gemini fit-gate (0-100, LLM-as-judge)
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
      A-tier (≥72)              B-tier (48-72)
   full referral workup        bulk apply list
      + outreach drafts                │
              └───────────┬────────────┘
                          ▼
                   Slack  +  HTML cockpit
```

## Why it exists

Job boards optimise for the employer. Aggregators are stale, spam-filled, and rank by
recency rather than fit. The roles worth applying to are posted on companies' *own*
boards, and the applications that convert are the ones sent within hours of posting.

So: skip the aggregators, poll the source, and let a model do the reading.

## What's interesting in here

**14 ATS adapters, one interface.** Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
Workable, Eightfold, Keka, and others each expose jobs differently — different auth,
pagination, and shapes, some with no documented API at all. `ats.py` normalises all of
them to one dict. Amazon, Uber, and Oracle needed bespoke adapters because their boards
don't use a standard ATS.

**Fault isolation.** Boards break constantly — rate limits, schema changes, outages. A
10-worker thread pool fetches them concurrently and each failure is caught per board, so
one broken adapter degrades the run instead of failing it. A run reports which boards
errored and carries on with the rest.

**Two-stage scoring, because LLM calls aren't free.** A cheap keyword heuristic
(`fit.py`) drops obviously wrong roles first — wrong stack, too senior, experience bar
too high. Only survivors reach the Gemini fit-gate (`gemini_fit.py`), which scores 0-100
with structured JSON output, batched to stay inside the free tier. If the API key is
missing or the call fails, the pipeline falls back to heuristic ranking rather than
breaking — the LLM is an upgrade, never a hard dependency.

**Idempotent state on a repo.** There's no database. State lives in `state/*.json`,
committed back by the workflow. Since scheduled runs can overlap, the push does
rebase-and-retry: losing a state write would mean re-alerting every role in that batch,
so it retries rather than silently dropping. Roles are deduplicated by *role identity*
(company + normalised title + location) rather than requisition ID, so reposts and
duplicate reqs stay quiet.

**Two tiers, because effort should match odds.** Cold applications convert at a few
percent; referral-backed ones convert far better. So A-tier roles get the full workup —
referral search links, alumni lookup, inferred email pattern, a drafted outreach message —
while B-tier becomes a compact bulk-apply list. One bar was the bottleneck; two bars let
high-effort and low-effort paths coexist.

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

### Running

```bash
python poller.py              # normal scheduled run
python poller.py --dry-run    # score + print, no Slack, no state written
python poller.py --backlog    # score every open match → queue.html
python poller.py --seed       # record current matches silently (first run)
python poller.py --outreach   # re-render the cockpit from committed state
```

For scheduled runs, `.github/workflows/poll.yml` runs it every 30 minutes and commits
state back. Add the secrets in repo settings; every one is optional and the pipeline
degrades gracefully without them.

## Configuration

| File | What it controls |
|---|---|
| `companies.yaml` | The 130 boards — `name`, `ats`, `token` (a public board slug) |
| `filters.yaml` | Title/location matching, score thresholds, per-run and per-company caps |
| `profile.yaml` | Your identity, CV, and preferences (gitignored) |

Adding a company is one line. Adding a new ATS is one function in `ats.py` returning the
normalised dict, plus a line in the dispatch table.

## Layout

| File | Role |
|---|---|
| `poller.py` | Orchestrator — the pipeline, top to bottom |
| `ats.py` | 14 board adapters, normalised to one shape |
| `fit.py` | Cheap heuristic pre-score |
| `gemini_fit.py` | LLM-as-judge fit-gate with fallback |
| `contacts.py` | Referral, alumni, and ex-colleague search links; email pattern inference |
| `outreach.py` | Drafts a personalised outreach message per A-tier role |
| `outreach_store.py` | Durable role tracker behind the cockpit |
| `outreach_page.py` | The HTML cockpit — apply, ask, follow up |
| `apply_queue.py` | Ranked backlog page for `--backlog` sweeps |
| `notify.py` | Slack formatting for both tiers |

## Notes

Outreach is deliberately **assisted, not automated** — the tool drafts messages and
finds the right people, but sending stays manual. Automating LinkedIn outreach risks
the account, and the volume here is well within what a person can send by hand.

Dependencies are `requests` and `PyYAML`. That's the whole list.
