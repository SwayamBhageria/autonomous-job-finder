#!/usr/bin/env python3
"""Job Finder — poll tier-1 companies' own career boards, keep only the roles
that genuinely fit you, and Slack the top handful.

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
import statistics
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
BOARD_HEALTH_FILE = ROOT / "state" / "board_health.json"
BOARD_SUPPLY_FILE = ROOT / "state" / "board_supply.json"


def load_yaml(name: str) -> dict:
    with open(ROOT / name) as f:
        return yaml.safe_load(f)


def build_matcher(filters: dict):
    inc = re.compile("|".join(filters["title_include"]), re.I)
    exc = re.compile("|".join(filters["title_exclude"]), re.I)
    loc_inc = re.compile("|".join(filters["location_include"]), re.I)
    # Aggregator listings carry the paying employer's name, which for Adzuna is
    # overwhelmingly IT staffing firms reposting the same req.
    co_exc = filters.get("company_exclude") or []
    co_exc = re.compile("|".join(co_exc), re.I) if co_exc else None
    remote_india = re.compile(r"remote.{0,25}india|india.{0,25}remote", re.I)
    generic_remote = re.compile(r"\bremote\b", re.I)

    def title_ok(t):
        return bool(inc.search(t)) and not exc.search(t)

    def company_ok(c):
        return not (co_exc and c and co_exc.search(c))

    def loc_ok(loc):
        if not loc:
            return filters.get("include_generic_remote", False)
        if loc_inc.search(loc):
            return True
        if filters.get("remote_india", True) and remote_india.search(loc):
            return True
        return filters.get("include_generic_remote", False) and bool(generic_remote.search(loc))

    return lambda j: (title_ok(j["title"]) and loc_ok(j["location"])
                      and company_ok(j.get("company", "")))


def key(j: dict) -> str:
    return f"{j['source']}:{j['company']}:{j['id']}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def title_key(j: dict) -> str:
    """Identity of a *role* (not a requisition) — collapses reposts / dup reqs."""
    return f"t:{j['company'].lower()}|{_norm(j['title'])}|{_norm(j.get('location', ''))}"


def worth_realerting(score: int, prev: dict | None, threshold: int) -> bool:
    """Has this role earned an alert, given we may have alerted it before?

    `take()` used to drop anything whose role-identity was already in state,
    full stop, for the 90-day life of the record. That is right for a repost —
    the same job at the same quality — and wrong for a role whose SCORE has
    since changed, because the memory outlives the verdict that created it.

    That bit hard on 2026-08-08. Two fixes landed the same day: Workday detail
    fetches stopped 429ing (so JDs actually arrived), and unread listings
    stopped being ceilinged onto the A-tier bar. Roles previously alerted at
    50-65 into the bulk lottery — scored off their TITLE because the JD never
    came — were now worth 85-95. A backlog sweep on 2026-08-09 found 53 such
    roles still open and 41 of them permanently silenced by this check:
    BlackRock 95, Amgen 92, Amazon SDE I 90, Sarvam AI 88. Those are not roles
    you passed on. They are roles the scorer got wrong and could not retract.

    So a role that now clears the A-tier bar, having last been alerted below it,
    is new information and gets said again. Records written before scores were
    stored carry none — and that is exactly the population the two fixes
    re-graded — so `.get("score", 0)` lets them through once. Every alert writes
    the score back, which bounds this: each role can re-alert at most once per
    genuine tier crossing, not once per run.
    """
    if prev is None:
        return True
    if score < threshold:
        return False          # not A-tier now — a repost stays quiet
    return prev.get("score", 0) < threshold


ALERT_AFTER = 3             # consecutive failed runs before a board is called out
RENAG_AFTER = 24 * 3600     # then at most this often, so the alert stays readable
FLAP_WINDOW = 12            # recent runs remembered per board, to catch intermittent failure
FLAP_AFTER = 4              # failures inside that window that make a board unreliable


def board_health(errors: list[str], ok_names: set, persist: bool
                 ) -> list[tuple[str, str, str, float, int]]:
    """Track per-board failures; return the ones that look broken.

    A board erroring once is usually a 5xx or a timeout and fixes itself. A board
    erroring every run is a board that moved — Tekion sat on a 404 for days
    because the failure only ever appeared in the heartbeat, which only fires on
    runs that found nothing. So failures are counted across runs and a board is
    reported once it has failed enough times to rule out a blip.

    Two things used to keep that report from ever arriving, which is how Uber's
    dead board went unnamed for days behind a bare "1 board(s) errored":

    - the first report was gated on `runs == 3` exactly. Equality is a single
      frame of a moving counter: any run that alerts but then fails to push its
      state (a cancelled run, a lost race on main) replays the same number, and
      any that pushes without alerting steps past it. `>= 3` cannot be missed.
    - the re-nag was `runs % 48`, i.e. "once a day" assuming the 30-minute cron.
      GitHub throttles a busy schedule hard — this repo actually gets ~6 runs a
      day — so the reminder was really once every ~8 days. Re-nag by the clock
      instead; run counts are not a unit of time.

    A third, added 2026-08-09: **the file only ever held boards failing right
    now**, so any board that succeeded once vanished from it and its streak went
    back to zero. A board failing every other run therefore never reached three
    in a row and could hide forever — which is exactly what was happening.
    Measured over four consecutive hourly runs, with `board_health` sitting at
    `{}` throughout:

        Microsoft  61 → 0 → 59 → 0        Aptiv  60 → 73 → 0 → 0

    So consecutive failures are no longer the only trigger. Each board that has
    failed recently also carries a short window of its last FLAP_WINDOW outcomes
    ("F" / "."), and enough failures inside it is reported on its own — a board
    losing a third of its runs is losing a third of its supply, whether or not
    the losses are adjacent. Boards that have been clean for the whole window
    drop out of the file entirely, so the healthy case stays `{}` and the file
    stays bounded by the number of boards actually misbehaving.
    """
    prior = {}
    if BOARD_HEALTH_FILE.exists():
        try:
            prior = json.loads(BOARD_HEALTH_FILE.read_text())
        except json.JSONDecodeError:
            pass
    now, health, broken = time.time(), {}, []
    failing = {e.split(":", 1)[0]: e.split(":", 1)[1].strip() for e in errors}
    # Boards that succeeded matter now: a success is what closes a window. Only
    # those already on file are considered, so a permanently-healthy board is
    # never written and the file does not grow to one entry per board.
    for name in sorted(set(failing) | (set(ok_names) & set(prior))):
        was = prior.get(name, {})
        failed_now = name in failing
        window = ((was.get("recent") or "") + ("F" if failed_now else "."))[-FLAP_WINDOW:]
        if "F" not in window:
            continue                       # recovered for a full window — forget it
        runs = was.get("runs", 0) + 1 if failed_now else 0
        since = was.get("since") or now
        last_alert = was.get("last_alert", 0)
        # Keep the last real error around: a flapping board is reported on runs
        # where it happened to succeed, and "no error" would be a useless alert.
        msg = failing.get(name) or was.get("error", "")
        flaps = window.count("F")
        if runs >= ALERT_AFTER:
            detail, severity = f"failing {runs} runs in a row", 100 + runs
        elif flaps >= FLAP_AFTER:
            detail, severity = f"failed {flaps} of the last {len(window)} runs", flaps
        else:
            detail, severity = None, 0
        if detail and now - last_alert >= RENAG_AFTER:
            broken.append((name, detail, msg, since, severity))
            last_alert = now
        health[name] = {"runs": runs, "recent": window, "since": since,
                        "error": msg, "last_alert": last_alert}
    if persist:
        BOARD_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOARD_HEALTH_FILE.write_text(json.dumps(health, indent=1, sort_keys=True))
    return sorted(broken, key=lambda x: -x[-1])


SUPPLY_WINDOW = 8       # runs of job-count history kept per board
SUPPLY_MIN = 5          # boards smaller than this swing too much to judge
SUPPLY_DROP = 0.5       # flag a board returning less than half its usual count


def supply_health(counts: dict[str, int], persist: bool) -> list[tuple[str, int, int]]:
    """Flag boards that returned far fewer jobs than they normally do.

    The layer above this only catches a board failing *completely*. A board that
    fails partially still succeeds, and on 2026-08-09 Comcast returned **1 job
    from a 20-job board** with nothing anywhere reporting a problem — no error,
    no empty result, nothing for a "did it throw?" check to catch.

    The obvious signal, how many sub-requests failed, is the wrong one and was
    tried first: a board's (query × page) requests overlap heavily, so Aptiv lost
    2 of 18 and still returned all 73 of its jobs. Reporting that would flag 3-5
    boards a run that lost nothing, which is the "not a signal, a rash" failure
    the board alert already learned once.

    So assert the thing actually wanted — this board should return roughly as
    many jobs as it usually does — and compare against its own recent median.
    That is mechanism-independent: it catches a rate-limited fetch, a parser that
    broke on a retemplated portal, and a board that quietly changed shape, none
    of which raise anything.

    The baseline is a median over the last few runs so one bad run cannot move it
    much, and it re-baselines naturally: a board that stays low drags its own
    median down and stops nagging, which is right — by then it is the new normal
    and worth a look on the page, not a repeated alert.
    """
    prior = {}
    if BOARD_SUPPLY_FILE.exists():
        try:
            prior = json.loads(BOARD_SUPPLY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    now, hist, dropped = time.time(), {}, []
    for name, n in counts.items():
        was = prior.get(name, {})
        seen = was.get("counts") or []
        last_alert = was.get("last_alert", 0)
        # Need a few runs before "normal" means anything, or every newly-added
        # board reports itself the first time it has a quiet day.
        if len(seen) >= 3:
            baseline = statistics.median(seen)
            if baseline >= SUPPLY_MIN and n < baseline * SUPPLY_DROP \
                    and now - last_alert >= RENAG_AFTER:
                dropped.append((name, n, int(baseline)))
                last_alert = now
        hist[name] = {"counts": (seen + [n])[-SUPPLY_WINDOW:], "last_alert": last_alert}
    if persist:
        BOARD_SUPPLY_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOARD_SUPPLY_FILE.write_text(json.dumps(hist, indent=1, sort_keys=True))
    return sorted(dropped, key=lambda d: d[1] - d[2])


def error_summary(errors: list[str]) -> str:
    """Name the boards that errored this run.

    The heartbeat used to append a bare count. Three days of "⚠️ 1 board(s)
    errored" is not a signal, it's a rash: it never says which board, so there
    is nothing to act on and you stop reading it. Naming costs one line.
    """
    names = sorted({e.split(":", 1)[0] for e in errors})
    shown = ", ".join(names[:6]) + (f" +{len(names) - 6} more" if len(names) > 6 else "")
    return f"  ⚠️ {len(names)} board(s) errored: {shown}."


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


KNOWN_FLAGS = {"--dry-run", "--backlog", "--golive", "--seed", "--outreach"}


def main() -> int:
    # An unrecognised flag used to be ignored, which meant `--dry` — the obvious
    # typo for --dry-run — ran for real: it wrote state, marked live roles as
    # already-seen (suppressing their Slack alert forever) and would have posted
    # to Slack wherever the webhook is configured. Fail loudly instead.
    unknown = [a for a in sys.argv[1:] if a.startswith("-") and a not in KNOWN_FLAGS]
    if unknown:
        print(f"unknown flag(s): {' '.join(unknown)}\n"
              f"known flags: {' '.join(sorted(KNOWN_FLAGS))}", file=sys.stderr)
        return 2

    dry = "--dry-run" in sys.argv
    backlog = "--backlog" in sys.argv
    golive = "--golive" in sys.argv or backlog
    seed = "--seed" in sys.argv

    # --outreach just re-renders the cockpit from the committed tracker — no
    # polling, no network. Cheap, so the local `outreach` command runs it daily.
    if "--outreach" in sys.argv:
        # Prune before rendering, in memory only. The tracker on disk is written
        # by the CI run, so between runs this page was showing rows the current
        # filters had already retired — which is how B-tier stayed on the cockpit
        # for a day after being switched off. Rendering what the settings say is
        # live costs nothing here and never disagrees with the next CI write.
        tracker, dropped = outreach_store.prune(
            outreach_store.load(OUTREACH_FILE), load_yaml("filters.yaml"))
        rows = outreach_store.as_rows(tracker)
        out = str(ROOT / "outreach.html")
        outreach_page.render(rows, out)
        retired = sum(dropped.values())
        print(f"Wrote outreach cockpit ({len(rows)} roles"
              f"{f', {retired} retired' if retired else ''}) → {out}")
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
    # 0 (or absent) means "no cap" — see filters.yaml. `or inf` is what makes
    # that true: `take()` stops on `len(out) >= limit`, so a literal 0 would
    # send ONE role, not every role.
    cap = filters.get("max_per_run", 20) or float("inf")
    bulk_cap = filters.get("max_bulk_per_run", 40) or float("inf")
    referral = {c.lower() for c in profile.get("referral_companies", [])}

    state = load_state()
    if len(state) == 0 and not dry and not golive:
        seed = True  # first-ever run: record silently, don't spam

    # ── fetch + Stage 0: title/location match (boards in parallel) ─
    # The cockpit's roles are re-checked against this same fetch, so alongside
    # the matches each board also yields the identities of everything it has
    # open. Only the urls/role-keys are kept, plus the full record for roles the
    # tracker actually holds — carrying all ~20k job dicts would be gratuitous.
    tracker = outreach_store.load(OUTREACH_FILE)
    tracked_urls = {v.get("url") for v in tracker.values()}
    tracked_tks = set(tracker)

    def fetch_one(c):
        try:
            jobs = ats.fetch(c["name"], c)
        except Exception as e:
            return c, [], set(), set(), {}, 0, str(e)
        urls, tks, relevant = set(), set(), {}
        for j in jobs:
            tk = title_key(j)
            urls.add(j.get("url", ""))
            tks.add(tk)
            if j.get("url") in tracked_urls or tk in tracked_tks:
                relevant.setdefault(tk, j)
        return c, [j for j in jobs if match(j)], urls, tks, relevant, len(jobs), None

    matched, errors, fetched_ok, board_counts = [], [], set(), {}
    live_urls, live_tks, live_rel, healthy = set(), set(), {}, set()
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for c, m, urls, tks, rel, n, err in ex.map(fetch_one, companies):
            if err:
                errors.append(f"{c['name']}: {err}")
            else:
                # "fetched without raising" — distinct from `healthy` below,
                # which additionally requires the board to have jobs on it.
                fetched_ok.add(c["name"])
                # Only successful fetches feed the supply history: a board that
                # errored is board_health's business, and recording its 0 here
                # would both double-alert and poison its own baseline.
                board_counts[c["name"]] = n
                matched.extend(m)
                # A board that fetched but returned nothing can't testify that a
                # role is gone — only a board with jobs on it counts as healthy.
                if n:
                    healthy.add(c["name"].lower())
                    live_urls |= urls
                    live_tks |= tks
                    live_rel.update(rel)
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
    broken = board_health(errors, fetched_ok, persist=not (dry or backlog))
    if broken:
        print("Boards failing repeatedly: " +
              "; ".join(f"{n} ({d})" for n, d, _, _, _ in broken))
    thin = supply_health(board_counts, persist=not (dry or backlog))
    if thin:
        print("Boards returning far less than usual: " +
              "; ".join(f"{n} ({c} vs ~{b})" for n, c, b in thin))

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

    # The last unmarked way to arrive with no JD. `enrich_description` labels a
    # fetch that failed, and `_snippet_note` labels an aggregator teaser — but a
    # role that came with an empty description and NO `_detail` endpoint to fetch
    # one from is caught by neither. Nothing marks it, so it reached the fit gate
    # as a blank listing with no ceiling and was scored on its title alone.
    #
    # 1 of 1287 matches on 2026-08-09 — and it was CRED "data scientist", at a
    # referral company, i.e. exactly the tier where being wrong costs real effort
    # per role. Same fail-open shape that once put a Pfizer role at 95. Marking
    # it also makes it countable: it now shows up in the "Capped at N" line
    # instead of passing silently, so a board that starts returning empty
    # descriptions announces itself rather than quietly scoring on vibes.
    blank = [j for j in candidates if not (j.get("description") or "").strip()]
    for j in blank:
        j["description"] = ats.SNIPPET_NOTE.strip()
    if blank:
        print(f"{len(blank)} role(s) arrived with no JD and no detail endpoint — "
              f"marked unreadable.")

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

    # A JD we could not read is not a JD that says nothing. `ats.SNIPPET_NOTE`
    # marks both the aggregator teasers and the detail fetches that 429'd, and
    # everything downstream of it is guesswork off the title. The prompt already
    # asks Gemini to ceiling those, but a prompt is a request: the ceiling is
    # enforced here so an unread role can never reach the referral tier, which
    # is the tier that costs real effort per role.
    unread_ceiling = filters.get("unverified_ceiling", 65)
    unread_mark = ats.SNIPPET_NOTE.strip()

    finalists, bulk_pool, unread, ungraded = [], [], 0, 0
    for j in scored:
        g = gem.get(id(j))
        j["_score"] = g["score"] if g else j["_h"]
        if unread_mark in (j.get("description") or ""):
            unread += 1
            j["_unverified"] = True
            j["_score"] = min(j["_score"], unread_ceiling)
        # Same fail-open shape, one layer up. When the fit gate is ON but a
        # batch errored (a read timeout took out 6 roles on 2026-08-07), those
        # roles have no verdict at all — and the heuristic they fall back to is
        # a keyword count that starts at 50 and adds 5 per stack hit, so it
        # clears 70 on vocabulary alone. Cap them like an unread JD.
        # When Gemini is off entirely the heuristic IS the ranking, not a
        # fallback, so capping there would just switch A-tier off silently.
        elif not g and gemini_fit.enabled:
            ungraded += 1
            j["_score"] = min(j["_score"], unread_ceiling)
        j["_resume"] = g["resume"] if g else ("ai" if j["_meta"]["strong_hits"] and re.search(r"\b(ai|ml|llm)\b", j["title"], re.I) else "software")
        j["_reason"] = g["reason"] if g else (
            f"not scored by the fit gate — {j['_meta']['strong_hits']} stack matches"
            if gemini_fit.enabled else f"{j['_meta']['strong_hits']} stack matches")
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
    # Same 0-means-unlimited rule. Here a literal 0 would be even worse than for
    # `limit`: `counts.get(...) >= 0` is true on the first look at every company,
    # so a 0 cap would drop the entire batch.
    per_company = filters.get("max_per_company", 3) or float("inf")
    bulk_per_company = filters.get("max_bulk_per_company", 8) or float("inf")
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
            if not backlog and not worth_realerting(j["_score"], state.get(tk), threshold):
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
    if unread or ungraded:
        print(f"Capped at {unread_ceiling}: {unread} unreadable JD, "
              f"{ungraded} missing a Gemini verdict (of {len(scored)}).")
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
    # A broken board is silent supply loss, so it gets said out loud on every
    # run — not only on the runs that happened to find nothing.
    notify.send_board_alert(broken)
    # And a board that half-answers is the same loss wearing a success.
    notify.send_supply_alert(thin)

    # Record roles in the standing tracker so the cockpit is a durable daily
    # worklist, not just a mirror of the Slack feed that scrolls away. A-tier
    # carries the full outreach treatment; B-tier is apply-and-track only.
    # Keyed by role identity so a repost doesn't duplicate.
    for j in top:
        j["_tier"] = "A"
    for j in bulk:
        j["_tier"] = "B"
    outreach_store.add(tracker, top + bulk, title_key)

    # Re-check what's already listed against the boards as they are right now:
    # postings get pulled, and roles admitted under the old experience gate are
    # still sitting there because an alerted role never re-enters the pipeline.
    def _years(j: dict) -> int | None:
        if not j.get("description") and j.get("_detail"):
            ats.enrich_description(j)
        return fit.min_years_required(j.get("description") or "")

    outreach_store.revalidate(
        tracker, live_urls, live_tks, healthy, live_rel,
        check_years=_years, hard_years=hard_years,
        closed_after=filters.get("closed_after_misses", 2))
    tracker, dropped = outreach_store.prune(tracker, filters)
    outreach_store.save(OUTREACH_FILE, tracker)
    print(f"Tracker holds {len(tracker)} role(s) for the cockpit "
          f"(cleared {dropped['unfit']} unfit, {dropped['closed']} closed, "
          f"{dropped['expired']} expired, {dropped['bulk_off']} bulk-off, "
          f"{dropped['over_cap']} over cap).")
    if not top and not bulk:
        # Name the bar that is actually in force. With B-tier off (bulk_bar ==
        # threshold) there is no bulk bar to be under, and saying so anyway
        # reads as "the bulk tier is still running" — which is exactly what it
        # looked like from Slack after B was switched off.
        bar = f"the bulk bar ({bulk_bar})" if bulk_bar < threshold else f"the bar ({threshold})"
        hb = (f"🔎 Job finder ran: {len(companies)} companies scanned, "
              f"{len(matched)} open matches, {len(candidates)} new since last run — nothing above {bar}.")
        if errors:
            hb += error_summary(errors)
        notify.send_heartbeat(hb)
        print("Sent heartbeat (no fit roles).")

    now = time.time()
    for j in top + bulk:  # remember alerted *roles* so reposts/dup reqs stay quiet
        # The score rides along: without it the memory cannot tell a repost from
        # a role the scorer has since re-graded. See worth_realerting().
        state[title_key(j)] = {"ts": now, "title": j["title"], "score": j["_score"]}
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
