#!/usr/bin/env python3
"""Job Finder — poll tier-1 companies' own career boards, keep only the roles
that genuinely fit the configured candidate profile, and Slack the top handful.

Pipeline per run:
    fetch boards → title+location match → NEW only (dedup)
      → heuristic pre-score (drop wrong stack / too senior)
      → Gemini fit-gate (0-100 + which resume + why)
      → keep score ≥ threshold, rank, cap → Slack

Run:
    python poller.py            # normal scheduled run
    python poller.py --dry-run  # score + print, touch nothing
    python poller.py --golive   # score ALL currently-open matches & notify
                                 #   (use once after enabling the fit-gate)
    python poller.py --backlog   # score ALL currently-open matches and write
                                 #   the whole ranked list to queue.html —
                                 #   no Slack, no caps, no state written
    python poller.py --seed      # record current matches silently
"""
from __future__ import annotations
import os
import sys
import re
import json
import time
import pathlib
import datetime as dt
import concurrent.futures as cf

import yaml

import ats
import fit
import gemini_fit
import contacts
import outreach
import notify
import apply_queue
import outreach_store
import outreach_page

ROOT = pathlib.Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen_jobs.json"
OUTREACH_FILE = ROOT / "state" / "outreach.json"


def load_yaml(name: str) -> dict:
    with open(ROOT / name) as f:
        return yaml.safe_load(f)


def build_matcher(filters: dict):
    inc = re.compile("|".join(filters["title_include"]), re.I)
    exc = re.compile("|".join(filters["title_exclude"]), re.I)
    loc_inc = re.compile("|".join(filters["location_include"]), re.I)
    remote_india = re.compile(r"remote.{0,25}india|india.{0,25}remote", re.I)
    generic_remote = re.compile(r"\bremote\b", re.I)

    def title_ok(t):
        return bool(inc.search(t)) and not exc.search(t)

    def loc_ok(loc):
        if not loc:
            return filters.get("include_generic_remote", False)
        if loc_inc.search(loc):
            return True
        if filters.get("remote_india", True) and remote_india.search(loc):
            return True
        return filters.get("include_generic_remote", False) and bool(generic_remote.search(loc))

    return lambda j: title_ok(j["title"]) and loc_ok(j["location"])


def key(j: dict) -> str:
    return f"{j['source']}:{j['company']}:{j['id']}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def title_key(j: dict) -> str:
    """Identity of a *role* (not a requisition) — collapses reposts / dup reqs."""
    return f"t:{j['company'].lower()}|{_norm(j['title'])}|{_norm(j.get('location', ''))}"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def main() -> int:
    dry = "--dry-run" in sys.argv
    backlog = "--backlog" in sys.argv
    golive = "--golive" in sys.argv or backlog
    seed = "--seed" in sys.argv

    # --outreach just re-renders the cockpit from the committed tracker — no
    # polling, no network. Cheap, so the local `outreach` command runs it daily.
    if "--outreach" in sys.argv:
        rows = outreach_store.as_rows(outreach_store.load(OUTREACH_FILE))
        out = str(ROOT / "outreach.html")
        outreach_page.render(rows, out)
        print(f"Wrote outreach cockpit ({len(rows)} roles) → {out}")
        return 0

    if golive:
        # The Adzuna aggregator normally only runs in a few UTC hours to stay
        # inside its free monthly quota. A golive/backlog sweep is an explicit
        # "score everything open right now", so it shouldn't silently skip it.
        os.environ["ADZUNA_IGNORE_SCHEDULE"] = "1"

    companies = load_yaml("companies.yaml")["companies"]
    filters = load_yaml("filters.yaml")
    profile = load_yaml("profile.yaml")
    match = build_matcher(filters)

    floor = filters.get("heuristic_floor", 45)
    hard_years = filters.get("hard_drop_years", 3)
    threshold = filters.get("notify_threshold", 75)
    bulk_bar = filters.get("bulk_threshold", 0)
    cap = filters.get("max_per_run", 20)
    bulk_cap = filters.get("max_bulk_per_run", 40)
    referral = {c.lower() for c in profile.get("referral_companies", [])}

    state = load_state()
    if len(state) == 0 and not dry and not golive:
        seed = True  # first-ever run: record silently, don't spam

    # ── fetch + Stage 0: title/location match (boards in parallel) ─
    def fetch_one(c):
        try:
            jobs = ats.fetch(c["name"], c)
            return c, [j for j in jobs if match(j)], len(jobs), None
        except Exception as e:
            return c, [], 0, str(e)

    matched, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for c, m, n, err in ex.map(fetch_one, companies):
            if err:
                errors.append(f"{c['name']}: {err}")
            else:
                matched.extend(m)
                print(f"  {c['name']:<14} {n:>4} → {len(m):>3} match")

    # jobs to actually evaluate this run
    if golive:
        candidates = matched               # re-score everything currently open
    else:
        candidates = [j for j in matched if key(j) not in state]
    print(f"\n{len(matched)} title/location matches; evaluating {len(candidates)} "
          f"{'(golive: all open)' if golive else 'new'}.")
    if errors:
        print(f"{len(errors)} board(s) errored: " + "; ".join(errors[:5]))

    if seed:
        now = time.time()
        for j in matched:
            state[key(j)] = {"ts": state.get(key(j), {}).get("ts", now), "title": j["title"]}
        _prune_save(state, filters)
        print(f"Seeded {len(state)} roles silently (no notifications).")
        return 0

    # fetch descriptions for sources that omit them (Workday), new roles only
    need = [j for j in candidates if not j.get("description") and j.get("_detail")]
    if need:
        print(f"Fetching {len(need)} Workday job descriptions (parallel)…")
        with cf.ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(ats.enrich_description, need))

    # ── Stage 1: hard experience cutoff + heuristic pre-score ────
    scored, dropped_exp = [], 0
    for j in candidates:
        hs, meta = fit.score(j, profile)
        my = meta.get("min_years")
        if hard_years and my is not None and my >= hard_years:
            dropped_exp += 1
            continue                      # JD requires too much experience
        if hs >= floor:
            j["_h"], j["_meta"] = hs, meta
            scored.append(j)
    scored.sort(key=lambda j: -j["_h"])
    print(f"Dropped {dropped_exp} for requiring ≥{hard_years} yrs; "
          f"{len(scored)}/{len(candidates)} passed heuristic floor ({floor}).")

    # ── Stage 2: Gemini fit-gate (falls back to heuristic) ───────
    gem = gemini_fit.rate(profile["candidate"], profile["preferences"], scored)
    if gemini_fit.enabled:
        print(f"Gemini scored {len(gem)} roles.")
    else:
        print("Gemini disabled (no GEMINI_API_KEY) — ranking by heuristic only.")

    finalists, bulk_pool = [], []
    for j in scored:
        g = gem.get(id(j))
        j["_score"] = g["score"] if g else j["_h"]
        j["_resume"] = g["resume"] if g else ("ai" if j["_meta"]["strong_hits"] and re.search(r"\b(ai|ml|llm)\b", j["title"], re.I) else "software")
        j["_reason"] = g["reason"] if g else f"{j['_meta']['strong_hits']} stack matches"
        j["_referral"] = j["company"].lower() in referral
        if j["_score"] >= threshold:
            finalists.append(j)
        elif j["_score"] >= bulk_bar:
            bulk_pool.append(j)

    finalists.sort(key=lambda j: (-j["_score"], not j["_referral"]))
    bulk_pool.sort(key=lambda j: -j["_score"])

    # Collapse near-duplicate roles (same title+location, different req IDs),
    # skip roles whose title we've already alerted before, and stop any single
    # company from flooding one batch.
    per_company = filters.get("max_per_company", 3)
    bulk_per_company = filters.get("max_bulk_per_company", 8)
    seen_tk: set[str] = set()

    def take(pool, limit, company_cap):
        """Dedupe by role identity + cap per company, preserving rank order."""
        counts, out = {}, []
        for j in pool:
            tk = title_key(j)
            if tk in seen_tk:
                continue
            # A backlog sweep is a deliberate re-surfacing of everything open,
            # so the "already alerted" memory and the per-batch caps don't apply.
            if not backlog and tk in state:
                continue
            if not backlog and counts.get(j["company"], 0) >= company_cap:
                continue
            seen_tk.add(tk)
            counts[j["company"]] = counts.get(j["company"], 0) + 1
            out.append(j)
            if len(out) >= limit:
                break
        return out

    inf = float("inf")
    top = take(finalists, inf if backlog else cap, per_company)   # A-tier first: it owns the dupes
    bulk = take(bulk_pool, inf if backlog else bulk_cap, bulk_per_company)
    print(f"A-tier: {len(finalists)} ≥ {threshold} → {len(top)} after dedupe/cap ({per_company}/co).")
    print(f"B-tier: {len(bulk_pool)} in [{bulk_bar},{threshold}) → {len(bulk)} after dedupe/cap ({bulk_per_company}/co).")

    # ── Outreach enrichment: referral links + email pattern + drafts ──
    for j in top:
        contacts.enrich(j, profile["me"])
    if top and not backlog:
        # Drafting a personalised DM per role costs a Gemini call; on a
        # several-hundred-role backlog sweep that's not worth it. The referral
        # search links from contacts.enrich() are free and still attached.
        outreach.drafts(profile["candidate"], profile["me"], top)

    if backlog:
        out = str(ROOT / "queue.html")
        apply_queue.render(top, bulk, out,
                           meta=f"{len(companies)} boards · {len(matched)} open matches")
        print(f"\nWrote {len(top)} A-tier + {len(bulk)} B-tier roles → {out}")
        print("(backlog: no Slack, no state written)")
        return 0

    if dry:
        for j in top:
            r = "⭐" if j["_referral"] else "  "
            print(f"  {r} {j['_score']:>3}  {j['company']:<12} {j['title'][:44]:<44} [{j['_resume']}] {j['_reason'][:50]}")
            print(f"        referrers: {j['_referrers'][:70]}")
            print(f"        alumni:    {j['_alumni'][:70]}  email: {j['_email_pattern']}")
            print(f"        DM: {j.get('_dm','')[:90]}")
        if bulk:
            print(f"\n  ── bulk queue ({len(bulk)}) ──")
            for j in bulk:
                print(f"     {j['_score']:>3}  {j['company']:<14} {j['title'][:50]:<50} [{j['_resume']}]")
        print("\n(dry-run: no Slack, no state written)")
        return 0

    notify.send_jobs(top)
    notify.send_bulk(bulk)
    if top or bulk:
        print(f"Sent {len(top)} A-tier + {len(bulk)} bulk role(s) to Slack.")

    # Record roles in the standing tracker so the cockpit is a durable daily
    # worklist, not just a mirror of the Slack feed that scrolls away. A-tier
    # carries the full outreach treatment; B-tier is apply-and-track only.
    # Keyed by role identity so a repost doesn't duplicate.
    if top or bulk:
        for j in top:
            j["_tier"] = "A"
        for j in bulk:
            j["_tier"] = "B"
        forget = filters.get("outreach_forget_days", 21)
        tracker = outreach_store.update(OUTREACH_FILE, top + bulk, title_key, forget)
        print(f"Tracker holds {len(tracker)} role(s) for the cockpit.")
    if not top and not bulk:
        hb = (f"🔎 Job finder ran: {len(companies)} companies scanned, "
              f"{len(matched)} open matches, {len(candidates)} new since last run — nothing above the bulk bar ({bulk_bar}).")
        if errors:
            hb += f"  ⚠️ {len(errors)} board(s) errored."
        notify.send_heartbeat(hb)
        print("Sent heartbeat (no fit roles).")

    now = time.time()
    for j in top + bulk:  # remember alerted *roles* so reposts/dup reqs stay quiet
        state[title_key(j)] = {"ts": now, "title": j["title"]}
    for j in matched:  # record every req seen so we never rescore/re-notify
        state[key(j)] = {"ts": state.get(key(j), {}).get("ts", now), "title": j["title"]}
    _prune_save(state, filters)
    print(f"State tracks {len(state)} roles (updated {dt.datetime.now():%Y-%m-%d %H:%M}).")
    return 0


def _prune_save(state: dict, filters: dict) -> None:
    cutoff = time.time() - filters.get("forget_after_days", 90) * 86400
    state = {k: v for k, v in state.items() if v.get("ts", 0) >= cutoff}
    save_state(state)


if __name__ == "__main__":
    raise SystemExit(main())
