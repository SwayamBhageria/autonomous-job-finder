"""Standing outreach tracker — `state/outreach.json`.

The A-tier alert is a one-shot notification; this is the durable list behind the
outreach cockpit. Each finalist is recorded once (keyed by role identity, so
reposts don't duplicate) with everything the cockpit needs.

Two jobs beyond recording, both learned the hard way from the Aug-5 cockpit:

`revalidate()` re-checks every listed role against the boards on each run. A
tracker that is only ever appended to rots twice over — postings get pulled (66
of the 97 verifiable rows had a dead Apply link), and roles admitted under a
since-fixed scoring rule never get re-judged, because an alerted role never
re-enters the pipeline.

`prune()` enforces the shelf life. Retention is per-kind rather than one flat
window, plus a hard cap, because at ~25 new roles/day a flat 21 days is a list
that only grows: 222 rows, of which 31 were actually applicable. A worklist you
cannot finish in a day has stopped being a worklist.

Progress (who you've messaged, follow-ups) lives client-side in the page's
localStorage, so this file only needs to carry the roles + their draft content.
"""
from __future__ import annotations
import json
import re
import subprocess
import time
import pathlib

# Where the bot publishes the tracker. CI polls the boards, writes this file and
# commits it here with [skip ci]; nothing else writes it.
PUBLISHED_REF = "origin/main"

# Bump when the JD experience parser changes, to re-gate rows already listed.
# 2 (2026-08-11): fit.min_years_required learned the "N years IN/BUILDING x"
# phrasings, which is most of them — under v1 only 3 of 111 rows carried a
# readable requirement at all.
REGATE_VERSION = 2

FIELDS = ("title", "company", "location", "url", "score", "resume", "reason",
          "referrers", "alumni", "excoll", "email_pattern", "email_researched",
          "dm", "email_subject", "email_body")


def load(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def load_published(path: pathlib.Path, ref: str = PUBLISHED_REF) -> tuple[dict, str]:
    """Read the tracker as the bot last published it, not as this branch has it.

    The cockpit is rendered from a file, and that file is written by CI on main.
    A checkout sitting on any other branch therefore holds a copy frozen at the
    moment it branched — and a frozen tracker does not render a frozen page, it
    renders a shrinking one: prune()'s age cutoff is evaluated against today
    while nothing new ever arrives, so the list can only drain. Seven days on a
    feature branch took the cockpit from 51 rows to 8 that way, which reads as
    the filters having broken rather than the file having gone stale.

    So read the blob straight out of git, and say which copy was used. Nothing
    is written: the branch's own state/ is left exactly as it was, which matters
    because a local run that mutates shared state has cost a live role's alert
    before.

    Falls back to the working tree when git can't answer — no network, no such
    ref, not a repo. The caller reports the source either way; a silent fallback
    to the stale copy is the failure this function exists to prevent.
    """
    root = path.parent.parent
    rel = f"{path.parent.name}/{path.name}"
    # Best-effort refresh. Offline is fine — we fall through to whatever the
    # last fetch left behind, and that is still fresher than a feature branch.
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin", ref.split("/")[-1]],
                       cwd=root, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=root,
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            return json.loads(out.stdout), ref
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return load(path), "working tree"


def published_age(path: pathlib.Path, ref: str = PUBLISHED_REF) -> str:
    """How long ago the bot last wrote the tracker, as git recorded it."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cr", ref, "--",
             f"{path.parent.name}/{path.name}"],
            cwd=path.parent.parent, capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown age"


def _entry(job: dict) -> dict:
    # jobs carry these as _-prefixed attrs; store them flat and JSON-safe.
    # B-tier jobs never went through contacts.enrich/outreach.drafts, so their
    # referral/dm fields are simply empty and the cockpit renders apply-only.
    return {
        "tier": job.get("_tier", "A"),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "score": job.get("_score", 0),
        "resume": job.get("_resume", "software"),
        "reason": job.get("_reason", ""),
        # "we never read this JD" — recorded as a flag rather than left to be
        # re-derived from the reason text, so the cockpit and prune() can act on
        # it without pattern-matching an LLM's prose.
        "unverified": bool(job.get("_unverified", False)),
        "referrers": job.get("_referrers", ""),
        "alumni": job.get("_alumni", ""),
        "excoll": job.get("_excoll", ""),
        "email_pattern": job.get("_email_pattern", ""),
        "email_researched": bool(job.get("_email_researched", False)),
        "dm": job.get("_dm", ""),
        "email_subject": job.get("_email_subject", ""),
        "email_body": job.get("_email_body", ""),
    }


def add(tracker: dict, jobs: list[dict], keyfn) -> dict:
    """Record this run's roles in `tracker` (keyed by keyfn(job)), in place.

    Anything arriving here was just fetched off a live board and just cleared
    the current gates, so its liveness and re-gate bookkeeping starts clean.
    """
    now = time.time()
    for j in jobs:
        k = keyfn(j)
        # refresh the draft content but keep the original first_seen so the
        # follow-up clock and ordering don't reset on a repost.
        first = tracker.get(k, {}).get("first_seen", now)
        # `regated: 0`, not True. A row created here has had its JD scored by
        # the fit gate, but nothing has yet run the experience PARSER over it —
        # revalidate() cannot, because `relevant` is built from the tracker as
        # it stood before this row existed, so a role is never regated on the
        # run it is added. `True` compares equal to 1, so both gates below
        # behaved correctly either way; the page did not. `_years_flag` reads
        # this field as "we looked", and rendered "no yrs stated — we read the
        # JD and it states no experience requirement" on 23 of 80 rows that
        # nobody had read. That is the exact confusion the flag exists to end:
        # a silent JD and an unread one are opposite facts, and only one of
        # them is safe to act on.
        tracker[k] = {**_entry(j), "first_seen": first,
                      "verifiable": True, "misses": 0, "regated": 0}
    return tracker


def revalidate(tracker: dict, live_urls: set, live_tks: set, healthy: set,
               relevant: dict, check_years=None, hard_years: int = 0,
               closed_after: int = 2, partial: set = frozenset()) -> dict:
    """Re-check every tracked role against the boards as they are right now.

    Two things rot in a tracker that is only ever appended to. A posting gets
    pulled — 66 of the 97 verifiable rows in the Aug-5 cockpit had a dead Apply
    link, because nothing re-checked them for the 21 days they were listed. And
    a scoring bug gets fixed — the roles admitted under the old rule stay,
    because a role, once alerted, is in `state` and so never re-enters the
    pipeline that would re-judge it.

    So each run:
      * exact Apply URL still on the board  → alive, counters reset;
      * URL gone but the *role* is still posted (a repost under a fresh req id)
        → repoint the Apply link at the live req rather than calling it closed;
      * neither → count a miss, and mark `closed` once it has missed
        `closed_after` runs in a row (one miss can be a partial page);
      * UNLESS the board is in `partial` — a board we sampled rather than
        enumerated, where absence is not evidence of anything (below);
      * the company has no board of ours (aggregator rows) → `verifiable: False`,
        left alone. We can't prove those either way, so the cockpit labels them
        instead of pretending.

    `check_years(job) -> (low, high) | None` re-reads the JD's experience
    requirement for rows we can see. A row whose floor now clears `hard_years`
    is marked `unfit`, and the cockpit drops it out of the working views. Only
    rows below `REGATE_VERSION` are checked, so the JD refetch is a one-time
    cost per role, not per run.

    The version, rather than a bare "already done" flag, is the whole point.
    The flag froze 108 of 111 rows at "states no requirement" — which was the
    parser failing, not the JDs being silent — and nothing could ever re-ask,
    because the flag records that we looked, not what we knew when we looked.
    Bumping REGATE_VERSION re-sweeps the existing page on the next run; without
    it a parser fix reaches new roles only, which is the slowest possible way
    to find out whether the fix worked.

    ── `partial`: boards we skimmed may not condemn a role ──
    Everything above rests on one assumption — that a role missing from
    `live_urls` is missing from the board. That is only true if the fetch
    enumerated the board. For the query-and-page adapters it did not: pcsx read
    99 of Qualcomm's 577 India roles, workday 60 of Citi's 1042. A role outside
    that window looks exactly like a role that was pulled.

    It is not a hypothetical. An 85-scored Qualcomm "Engineer" alerted on
    2026-08-18 at 13:10 was flagged "⚠️ Posting gone" seven runs later and
    dropped out of the To-do and To-apply views, while both its requisitions
    were still listed on the board. The cockpit is the thing you actually
    works from, so a live role hidden there is worse than a dead one left on
    it — a dead link costs one click, a hidden role costs the application.

    So a board that reports itself partial gets no vote: no miss, no `closed`,
    and no repost-repair either, since the "new requisition" it would repoint
    at is often just a sibling that happened to fall inside the window. A
    positive sighting still counts — seeing a role is proof it is there, from a
    sample as much as from a full sweep — so the counters still reset.
    """
    for k, row in tracker.items():
        co = (row.get("company") or "").lower()
        if co not in healthy:
            row["verifiable"] = False
            continue
        row["verifiable"] = True
        live = row.get("url") in live_urls
        if not live and co in partial:
            # We only skimmed this board. Absence proves nothing, so leave the
            # row exactly as it was rather than moving it toward `closed`.
            row["sampled"] = True
            continue
        row.pop("sampled", None)
        if not live and k in live_tks:
            # same role, new requisition — repair the link instead of dropping it
            j = relevant.get(k)
            if j and j.get("url"):
                row["url"] = j["url"]
                row["reposted"] = True
            live = True
        if live:
            row["misses"] = 0
            row.pop("closed", None)
        else:
            row["misses"] = row.get("misses", 0) + 1
            if row["misses"] >= closed_after:
                row["closed"] = True

        if check_years and hard_years and row.get("regated", 0) < REGATE_VERSION:
            j = relevant.get(k)
            if j is not None:
                span = check_years(j)
                row["regated"] = REGATE_VERSION
                row.pop("min_years", None)
                row.pop("max_years", None)
                if span is not None:
                    lo, hi = span
                    row["min_years"], row["max_years"] = lo, hi
                    if lo >= hard_years:
                        row["unfit"] = f"JD asks for {lo}+ years"
    return tracker


def prune(tracker: dict, f: dict) -> tuple[dict, dict]:
    """Enforce the cockpit's shelf life. Returns (kept, counts-by-reason).

    Retention is per-kind rather than one flat window, because the rows differ
    enormously in how long they stay worth an application (see filters.yaml):
    a row that fails the current experience gate is worth nothing the moment we
    know it, a confirmed-closed one gets a short grace period so it doesn't
    vanish mid-worklist, and A-tier — where a referral DM may be out awaiting a
    reply — gets the longest life.

    Then a hard cap, so the page is bounded by construction and not by hoping
    the inflow stays low. Dropping a role here does NOT re-open it for alerting:
    seen_jobs.json remembers alerted roles for 90 days independently, so this
    clears the list for good instead of deferring the pile.
    """
    now = time.time()
    dropped = {"unfit": 0, "closed": 0, "expired": 0, "over_cap": 0, "bulk_off": 0}
    # Switching B-tier off stops new bulk rows being *written*, but said nothing
    # about the ones already listed — so the cockpit still showed 132 of them,
    # draining out over `bulk_forget_days`. "I turned bulk off" and "the page
    # still opens on bulk rows for a week" is a setting that reads as broken.
    bulk_off = f.get("bulk_threshold", 0) >= f.get("notify_threshold", 70)
    # Rows admitted before the unverified ceiling existed keep the score the bug
    # gave them, because an alerted role never re-enters the pipeline that would
    # re-judge it (the same trap revalidate() exists for). Re-apply the ceiling
    # here so the correction reaches the 7 A-tier rows already on the page whose
    # JD was never actually read — 6 of them sitting on exactly the A-tier bar.
    ceiling = f.get("unverified_ceiling", 65)
    # company_exclude only screens incoming matches, so without this the rows a
    # newly-blocked employer already put on the list would sit there for their
    # full retention — the filter would read as broken for a fortnight.
    blocked = f.get("company_exclude") or []
    blocked = re.compile("|".join(blocked), re.I) if blocked else None
    # Same argument one field over: title_exclude also only screens incoming
    # matches, so adding a pattern leaves the rows it describes sitting on the
    # page for their full retention. That is a fortnight of the filter reading
    # as broken, and it bites hardest exactly when the new pattern is important
    # — the fixed-term rule added 2026-08-09 was there to clear an Amazon FTC
    # row scored 90, i.e. the first thing you'd have seen for the next 14 days.
    # Measured before shipping: the full current list kills 0 of the 110 rows
    # on the page, so this re-gate only ever acts on newly-added patterns.
    dropped_titles = re.compile("|".join(f["title_exclude"]), re.I)
    # Same argument for the ruled-out level-II bands, with the same JD-first
    # rule the poller applies (see filters.yaml `level_gate`): a level-II
    # posting at one of these companies stays if its JD states a low enough
    # floor. A row we have not re-gated yet is left alone rather than dropped —
    # we have not read its JD, and the JD is what decides.
    lg = f.get("level_gate") or {}
    lg_cos = [c.lower() for c in (lg.get("companies") or [])]
    lg_title = re.compile(lg.get("title") or r"\b(?:ii|2)\b", re.I)
    lg_cap = lg.get("max_years", 1)
    kept = {}
    for k, v in tracker.items():
        age = (now - v.get("first_seen", 0)) / 86400
        if v.get("unfit"):                      # never should have been listed
            dropped["unfit"] += 1
            continue
        if _unverified(v) and (v.get("score") or 0) > ceiling:
            v["score"] = ceiling
            v["tier"] = "B"          # below the A bar by construction now
        if bulk_off and _tier(v) != "A":
            dropped["bulk_off"] += 1
            continue
        if blocked and blocked.search(v.get("company") or ""):
            dropped["unfit"] += 1
            continue
        if dropped_titles.search(v.get("title") or ""):
            dropped["unfit"] += 1
            continue
        if (lg_cos and (v.get("company") or "").lower() in lg_cos
                and lg_title.search(v.get("title") or "")
                and v.get("regated", 0) >= REGATE_VERSION
                and not (v.get("min_years") is not None and v["min_years"] <= lg_cap)):
            dropped["unfit"] += 1
            continue
        if v.get("closed") and age >= f.get("closed_grace_days", 2):
            dropped["closed"] += 1
            continue
        if not v.get("verifiable", True):
            limit = f.get("unverified_forget_days", 7)
        elif _tier(v) == "A":
            limit = f.get("outreach_forget_days", 14)
        else:
            limit = f.get("bulk_forget_days", 7)
        if age >= limit:
            dropped["expired"] += 1
            continue
        kept[k] = v

    cap = f.get("outreach_max_rows", 0)
    if cap and len(kept) > cap:
        ranked = sorted(kept.items(),
                        key=lambda kv: (_tier(kv[1]) != "A",
                                        -(kv[1].get("score") or 0),
                                        -(kv[1].get("first_seen") or 0)))
        dropped["over_cap"] = len(kept) - cap
        kept = dict(ranked[:cap])
    return kept, dropped


def save(path: pathlib.Path, tracker: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tracker, indent=1, sort_keys=True))


def _unverified(row: dict) -> bool:
    """Did anything ever read this role's JD?

    New rows carry the flag. Rows written before it existed are recognised by
    the phrase the fit gate was told to use, which is the only record they have
    — a migration, not the long-term test.
    """
    if row.get("unverified"):
        return True
    return "unverified jd" in (row.get("reason") or "").lower()


def _tier(row: dict) -> str:
    """A-tier means 'there is referral work queued on this row'.

    Rows written before the two-tier split carry no `tier` at all, and the old
    `.get("tier", "A")` default promoted every one of them to the top of the
    page — which is exactly where the stale over-senior Glean/eBay rows were
    showing up. Absent an explicit tier, the honest test is whether the row
    actually has the outreach payload that A-tier exists for.
    """
    return row.get("tier") or ("A" if row.get("referrers") else "B")


def as_rows(tracker: dict) -> list[dict]:
    """Tracker dict → list of cockpit rows, key inlined. A-tier first (they need
    the outreach work), then newest-first within each tier."""
    rows = [{**v, "key": k, "tier": _tier(v)} for k, v in tracker.items()]
    rows.sort(key=lambda r: (r["tier"] != "A", -r.get("first_seen", 0)))
    return rows
