"""ATS adapters — each returns a list of normalised job dicts.

A normalised job:
    {id, company, title, location, url, department, source}

All three APIs are public, unauthenticated JSON. If a board changes
shape or a request fails, the adapter raises; the poller isolates that
company so one bad board never sinks the whole run.
"""
from __future__ import annotations
import os
import re
import json
import datetime as dt
import html
import time
import random
import threading
import collections
import urllib.parse
import concurrent.futures as cf
import requests

import fit   # for the experience parser; fit imports nothing of ours

TIMEOUT = 25
DESC_CAP = 3000            # keeps the Gemini prompt cheap
# How long a browser-harvested listing stays believable. The nightly harvest
# only pulls the last 24h, so this is slack for a few skipped evenings — not a
# window we expect to be full.
INBOX_KEEP_DAYS = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (job-finder; +https://github.com/)"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _get(url: str, referer: str = "") -> dict | list:
    # one retry on transient network hiccups (read timeouts, resets)
    hdrs = {**HEADERS, "Referer": referer} if referer else HEADERS
    for attempt in range(2):
        try:
            r = requests.get(url, headers=hdrs, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise
            time.sleep(2)


def _origin(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


class BoardUnavailable(RuntimeError):
    """The vendor says this board is down right now — not our failure to read it.

    Distinct from a plain error because the two want opposite handling. An error
    means *we* could not read a board that is up, and should count against it:
    three in a row is a dead board and worth a Slack alert. A vendor maintenance
    window is neither our bug nor actionable — on 2026-08-15 Workday took pods
    wd1/wd3/wd5 down together and 63 of 65 boards went with them, which under
    plain-error handling would have alerted naming 8 companies at random out of
    the 63, every day the window lasted.

    What it must NOT become is a success. A board in maintenance returns no jobs,
    and `poller.fetch_one` keeps it out of `fetched_ok`/`healthy`/`board_counts`
    exactly as it would an error — because a board with no jobs on it cannot
    testify that a role is gone, and revalidate() would otherwise read the empty
    result as "every role at these 63 companies has been pulled".
    """


# ── Did we see the WHOLE board, or a sample of it? ────────────
# `healthy` in the poller means "this board answered and had jobs on it", and
# revalidate() treats that as authority to declare a tracked role dead when the
# role is absent. That inference is only sound if the fetch enumerated the board
# *exhaustively*, and for the query-and-page adapters it usually did not:
#
#   Qualcomm  99 of 577 India roles reachable (pcsx asked for 50-job pages and
#             the API silently serves 10, so start=0,50,100 read ranks 0-9,
#             50-59, 100-109 — 30 of 577 per query, skipping 40 in every 50)
#   Citi      60 of 1042 (workday: 3 pages x limit 20, relevance-ranked)
#
# A role outside that window is indistinguishable from a role that was pulled,
# and the cockpit believed the second: an 85-scored Qualcomm "Engineer" alerted
# 2026-08-18 13:10 was flagged "⚠️ Posting gone" and dropped out of the To-do
# and To-apply views seven runs later, while both its requisitions were still
# listed. So an adapter that knows it sampled says so here, and revalidate()
# declines to count a miss against it. Absence of evidence, from a board we only
# skimmed, is not evidence of absence.
_PARTIAL: dict[str, str] = {}
_PARTIAL_LOCK = threading.Lock()


def reset_coverage() -> None:
    """Forget last run's coverage. Called once per poll, before any fetch."""
    with _PARTIAL_LOCK:
        _PARTIAL.clear()


def mark_partial(company: str, why: str) -> None:
    """Record that this board was sampled, not enumerated. First reason wins."""
    with _PARTIAL_LOCK:
        _PARTIAL.setdefault(company.lower(), why)


def partial_boards() -> dict[str, str]:
    """{company: why} for boards this run only skimmed."""
    with _PARTIAL_LOCK:
        return dict(_PARTIAL)


def _short_walk(rows: int, total: int | None) -> bool:
    """Did a walk that ran until the board said "no more" still end far short?

    The empty page is the board's own statement that it is exhausted, so it is
    positive evidence and outranks the count. `total` is a page-1 snapshot and
    does not have to agree exactly: Oracle's board reports 225 India rows and
    serves 224 on every run, and pcsx's `count` moves 575 → 577 mid-walk as
    postings come and go. Demanding exact agreement marks such a board partial
    *permanently*, which switches its closure detection off for good — the same
    silent, self-inflicted damage this whole mechanism exists to prevent, and
    the second time that trap has been walked into (see pcsx's row-vs-job note).

    So tolerate a small shortfall and catch only a gross one: a walk that ends
    at 40 of 500 did not finish, it broke.
    """
    if not total:
        return False
    return rows + 3 < total and rows < total * 0.95


def _check_pages(company: str, failed: int, total: int, got: int,
                 why: str = "") -> None:
    """Raise if every list request failed; warn if only some did.

    The paged adapters below fan a board out into (query × page) sub-requests
    and each one used to swallow its own failure into an empty list. When a
    tenant 429s the whole burst — which is what happens when the poller runs
    10 boards at 8-wide concurrency — the adapter returns [] without raising,
    and `fetch_one` reads that as "the board is up and has nothing on it": no
    error, no board_health entry, no alert, ever. That is the invisible-zero
    blind spot NEXT-STEPS flagged, and on 2026-08-09 it was live:

        Microsoft  61 → 0 → 59 → 0   across four consecutive hourly runs
        Aptiv      60 → 73 → 0 → 0     (Comcast, Carrier, F5 the same)

    while board_health stayed `{}` the whole time and Qualcomm/Ericsson on the
    same adapter sat rock-steady. ~50 India title matches were dropping out on
    alternate runs with nothing anywhere saying so.

    A total failure is indistinguishable from an empty board at the call site,
    so it has to be raised here where the failure count is still known. A
    partial failure is left as a warning rather than an error because it is
    common, recoverable on the next run, and raising would take a board that
    returned 90% of its jobs out of `healthy` — which would then make
    revalidate() believe live roles had been pulled.

    `why` carries the most common reason, when the caller kept it. Without it
    the recorded error is only a count, which is what board_health held on
    2026-08-15 when 63 of 65 Workday boards died at once: "all 18/18 list
    requests failed" for every one of them, and nothing anywhere saying that
    every request had come back HTTP 200 with Workday's maintenance page.
    """
    if failed and not got:
        raise RuntimeError(f"all {failed}/{total} list requests failed{why}")
    if failed:
        # Deliberately still a success for supply purposes — see the docstring
        # above on why raising here would be worse. But the jobs behind those
        # failed pages are missing from `live_urls`, and without this the roles
        # they hold get counted as pulled. That is the same hole the sampling
        # comment above describes, arriving by a different route.
        mark_partial(company, f"{failed}/{total} list requests failed")
        print(f"  ⚠️ {company}: {failed}/{total} list requests failed, "
              f"{got} job(s) returned — some may be missing")


# Workday's maintenance page says "Workday is currently unavailable" in its
# <title>, and links out to community.workday.com/maintenance-page. Matched on
# the body rather than the status, because it is served as a 200 — see below.
#
# Oracle Fusion words it differently and serves it as a 503: <title>Planned
# Outage</title> over "currently undergoing scheduled maintenance". Neither
# Workday mark appears anywhere in it, so before this was added JPMorgan's pod
# going down on 2026-08-15 classified as TRANSIENT — which by design never
# establishes a window on its own — and the board alerted for two runs with
# "all 6/6 list requests failed" and no reason attached.
_MAINTENANCE_MARKS = ("currently unavailable", "maintenance-page",
                      "scheduled maintenance", "planned outage")


# Statuses that are consistent with a pod being down, as opposed to one that is
# up and refusing us. 429 and 403 are deliberately NOT here: they mean the
# tenant is serving other people fine and throttling or blocking this client,
# which is our problem to know about and must never be filed under "maintenance".
_POD_DOWN_STATUS = {500, 502, 503, 504}

MAINTENANCE, TRANSIENT, OTHER = "maintenance", "transient", "other"


def _list_failure(exc: Exception,
                  r: requests.Response | None) -> tuple[str, str]:
    """Why one list request failed, and which of three kinds of failure it is.

    A 200 carrying HTML is the case worth naming. `_fetch_list` only retries
    the statuses in `_RETRY_STATUS`, so a maintenance page — which Workday
    serves as 200 text/html — sails through raise_for_status() and only blows
    up later on .json(). The caller sees a JSONDecodeError and no status at
    all, so "failed" reads as "the request errored" when the truth is "the
    pod is down and told us so in HTML".

    The three kinds exist because a pod in maintenance does not fail uniformly.
    On 2026-08-15 the first cut of this demanded that *every* failure be a
    recognised maintenance page, and 6 of 63 boards fell to the error path
    anyway — with "vendor maintenance page" as their own recorded reason —
    because a handful of their 18 requests had timed out or exhausted the retry
    on a 5xx while the pod was restarting, which leaves no response to read at
    all. Same outage, different bucket, decided by a coin flip.

    So `TRANSIENT` covers the collateral (timeouts, resets, 5xx) that a pod
    going down produces alongside the page, and it is only ever allowed to ride
    along with a positive `MAINTENANCE` sighting — never to establish one on its
    own. `OTHER` is everything unrecognised, including 429/403, and always keeps
    the board on the error path: a broad "probably fine" would be the
    invisible-loss bug this module keeps re-learning.
    """
    resp = r if r is not None else getattr(exc, "response", None)
    if resp is not None:
        ctype = (resp.headers.get("content-type") or "?").split(";")[0]
        if "json" not in ctype.lower():
            head = resp.text[:4000].lower()
            if any(m in head for m in _MAINTENANCE_MARKS):
                return (f"HTTP {resp.status_code} {ctype} "
                        f"(vendor maintenance page)"), MAINTENANCE
            kind = TRANSIENT if resp.status_code in _POD_DOWN_STATUS else OTHER
            return f"HTTP {resp.status_code} {ctype} (non-JSON body)", kind
        kind = TRANSIENT if resp.status_code in _POD_DOWN_STATUS else OTHER
        return f"HTTP {resp.status_code} ({type(exc).__name__})", kind
    kind = TRANSIENT if isinstance(
        exc, (requests.Timeout, requests.ConnectionError)) else OTHER
    return f"{type(exc).__name__}: {exc}"[:120], kind


def _list_outcome(company: str, fails: list[tuple[str, str]],
                  total: int, got: int) -> None:
    """Decide what a fanned-out board fetch amounts to: fine, warn, or raise.

    Shared by every paged adapter rather than copied into each, because the
    condition below is subtle enough that two copies would drift — and the way
    this goes wrong is silent in both directions.
    """
    reasons = [r for r, _ in fails]
    kinds = {k for _, k in fails}
    why = f" — {collections.Counter(reasons).most_common(1)[0][0]}" if fails else ""
    # Nothing came back, the vendor's own "we are down" page was seen at least
    # once, and nothing else suggests a board that is up and refusing us. The
    # collateral of a pod going down (timeouts, resets, 5xx) is allowed to ride
    # along, but never to establish the window by itself — one positive sighting
    # is required, and a single OTHER (429, 403, an unrecognised body) is enough
    # to keep the board on the error path. A board that returned any jobs at all
    # is not in a window and is handled by _check_pages below.
    if not got and MAINTENANCE in kinds and not (kinds - {MAINTENANCE, TRANSIENT}):
        raise BoardUnavailable(f"all {len(fails)}/{total} list requests{why}")
    _check_pages(company, len(fails), total, got, why)


# ── Per-host politeness, for the DETAIL fetch only ────────────
# A list call is one request per board. A detail call is one request per ROLE,
# and the poller fires them 12-wide — so a board that arrives with a big batch
# of new matches sends 12 concurrent requests to ONE tenant. Workday answers
# that with 429. On 2026-08-07 it cost 87 of 288 enrichments in a single run,
# 26 of 100 in another, and 24 of 24 in a third; small batches never failed,
# which is why this looked healthy on every quiet run.
#
# The damage is invisible, which is the point of fixing it here: a 429 is caught
# by enrich_description, which labels the JD "[PARTIAL LISTING]", so the role is
# then scored by Gemini on its TITLE alone and ceilinged — and the cockpit fills
# with roles nobody ever read the JD for. 92 of 181 rows on 2026-08-08.
#
# Two in flight per host keeps different boards fully parallel (only same-tenant
# calls queue) and is what these tenants serve without complaint.
_HOST_LIMIT = 2
_HOST_SEMS: dict[str, threading.Semaphore] = {}
_HOST_LOCK = threading.Lock()
# Statuses that mean "slow down / try again", as opposed to "this is gone".
# A 404 on a pulled posting is retried zero times: it would just be three times
# the latency for the same answer.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_BACKOFF = 10


def _host_sem(url: str) -> threading.Semaphore:
    host = urllib.parse.urlparse(url).netloc
    with _HOST_LOCK:
        return _HOST_SEMS.setdefault(host, threading.Semaphore(_HOST_LIMIT))


# ── The same politeness, for the LIST fetch ───────────────────
# The throttle above was added on 2026-08-08 for the detail fetch and stopped
# there, leaving the list fetch — 18 (query × page) requests per Workday/PCSX
# board, with 10 boards running at once — completely unpaced. Measured in CI on
# 2026-08-09, the first run after silent failures became visible:
#
#     F5        18/18 list requests failed   →  board lost outright
#     Comcast   10/18 failed, 1 job returned →  a 20-job board reporting 1
#     Aptiv     10/18 failed
#     Carrier   10/18 failed
#     Cigna      6/18 failed
#
# Comcast is the one that matters: it did not fail, it "succeeded" with 5% of
# its board, which no error check anywhere would ever have caught.
#
# ⚠️ The CAUSE is not established, and the obvious theory failed its test.
# "Our own burst provokes the 429" predicts it should reproduce locally at the
# same concurrency, and it does not: unthrottled, 12 boards 10-wide from a home
# IP returned all 1126 jobs with zero failures, twice. The failing boards are
# also all Workday while PCSX (Qualcomm, Ericsson) stays steady through the
# same bursts. The likelier explanation is IP reputation — GitHub's shared
# Azure ranges are heavily scraped and Workday appears to rate-limit them —
# which pacing alone cannot fix.
#
# So the load-bearing part here is the RETRY, not the limit: a 429 used to be
# an instant, silent loss, and is now waited out and retried up to 4 times,
# honouring Retry-After. The per-host cap is defensive and cheap; do not read
# it as a diagnosis. If failures persist in CI at this limit, the next move is
# the IP, not a smaller number.
#
# Kept separate from _HOST_LIMIT rather than shared, because the two phases are
# shaped differently — a list request is heavier and there are ~18 of them per
# board, against one per role — so they want tuning independently.
_LIST_LIMIT = 3
_LIST_SEMS: dict[str, threading.Semaphore] = {}


def _list_sem(url: str) -> threading.Semaphore:
    host = urllib.parse.urlparse(url).netloc
    with _HOST_LOCK:
        return _LIST_SEMS.setdefault(host, threading.Semaphore(_LIST_LIMIT))


def _fetch_list(method: str, url: str, **kw) -> requests.Response:
    """One board-list request: throttled per host, and waits out a 429.

    Mirrors `_get_detail`, including holding the host's slot across the sleep —
    a 429 means the tenant wants everyone to back off, not just this thread.
    Returns the Response so callers can read .json() or .text as they need.
    """
    sem, last, wait = _list_sem(url), None, 0.0
    for attempt in range(4):
        with sem:
            try:
                r = requests.request(method, url, headers=HEADERS, timeout=TIMEOUT, **kw)
                if r.status_code not in _RETRY_STATUS:
                    r.raise_for_status()
                    return r
                last = requests.HTTPError(
                    f"{r.status_code} Client Error for url: {url}", response=r)
                wait = min(float(r.headers.get("Retry-After") or 0) or 2 ** attempt,
                           _MAX_BACKOFF)
            except (requests.Timeout, requests.ConnectionError) as e:
                last, wait = e, 2 ** attempt
            if attempt < 3:
                time.sleep(wait + random.uniform(0, 0.4))   # jitter: unsynchronise
    raise last


def _get_detail(url: str, referer: str = "") -> dict | list:
    """`_get` for per-role detail calls: throttled per host, and waits out a 429.

    The sleep happens while still holding the host's slot, deliberately — a 429
    means that tenant wants everyone to back off, not just this thread.
    """
    hdrs = {**HEADERS, "Referer": referer} if referer else HEADERS
    sem, last, wait = _host_sem(url), None, 0.0
    for attempt in range(4):
        with sem:
            try:
                r = requests.get(url, headers=hdrs, timeout=TIMEOUT)
                if r.status_code not in _RETRY_STATUS:
                    r.raise_for_status()          # 404 etc. → raise, no retry
                    return r.json()
                last = requests.HTTPError(f"{r.status_code} Client Error for url: {url}",
                                          response=r)
                # Honour Retry-After when the server sends one, but cap it: some
                # tenants answer with minutes, and the run has a schedule to keep.
                wait = min(float(r.headers.get("Retry-After") or 0) or 2 ** attempt,
                           _MAX_BACKOFF)
            except (requests.Timeout, requests.ConnectionError) as e:
                last, wait = e, 2 ** attempt
            if attempt < 3:
                time.sleep(wait + random.uniform(0, 0.4))   # jitter: unsynchronise
    raise last


def _get_detail_text(url: str) -> str:
    """`_get_text` for detail pages — same per-host throttle as `_get_detail`."""
    with _host_sem(url):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text


def _get_text(url: str) -> str:
    """Same as `_get` for boards whose LIST page is HTML, not JSON.

    Goes through `_fetch_list`, so it gets the per-host slot and — the part that
    matters — the same 4-attempt backoff every other fetcher in this file has.
    It used to be a bare `requests.get` with `raise_for_status()`: no throttle,
    no retry, so **one** blip marked the board failed for the whole run.

    That is not hypothetical. Its only caller is `freshteam()`, and on
    2026-08-26..28 all ten Freshteam tenants flap-alerted together for three
    days — "failed 8 of the last 12 runs" — with `502 Bad Gateway` and
    `Read timed out (read timeout=25)`. Both of those are in `_RETRY_STATUS` /
    the caught-exception set that `_fetch_list` has always waited out for
    everyone else; this one path just never got the treatment. The boards were
    never down: probed sequentially all ten answer 200 in 0.5-1.4s, and the
    poller's 10-wide board pool hits them together because eight of them sit on
    consecutive lines in companies.yaml.

    It does not reproduce from a home IP (30/30 clean, 10-wide, three trials) —
    CI runs on shared Azure ranges that get rate-limited on reputation, which is
    why the retry rather than the throttle is what carries the weight here.
    """
    return _fetch_list("GET", url).text


def _plain(text: str) -> str:
    """Strip HTML → plain text, capped for the fit gate.

    The cap is a head-cut, which used to quietly defeat the experience gate:
    across a 431-JD sample 79% of descriptions were long enough to be truncated,
    and the "N+ years of experience" line sits a median 78% of the way through a
    JD — so the cut threw away the one sentence the gate reads. Requirements are
    scanned off the FULL text and re-attached at the front, the same trick the
    Amazon/Uber/Oracle adapters already use for their structured fields.
    """
    if not text:
        return ""
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub("\n\n", text)
    text = " ".join(text.split())
    if len(text) <= DESC_CAP:
        return text
    lead = fit.experience_line(text)
    if not lead:
        return text[:DESC_CAP]
    return f"{lead} {text[:DESC_CAP - len(lead) - 1]}"


SNIPPET_NOTE = " [PARTIAL LISTING — full JD unavailable; experience requirement unknown.]"


def _snippet_note(desc: str) -> str:
    """Flag a teaser as a teaser.

    Aggregators hand back a ~200-char blurb ending in an ellipsis, not the job
    description. The experience gate then finds no requirement and waves the role
    through, and the fit gate — reading what looks like a short, complete JD —
    scores it on vibes. That is how a Pfizer role scored 95. Marking it lets the
    scorer discount what it cannot see instead of rewarding the silence.
    """
    if not desc:
        return desc
    if desc.rstrip().endswith(("…", "...")) or len(desc) < 400:
        return desc + SNIPPET_NOTE
    return desc


def greenhouse(company: str, token: str) -> list[dict]:
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        out.append({
            "id": str(j["id"]),
            "company": company,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "department": ", ".join(dp.get("name", "") for dp in j.get("departments", [])),
            "description": _plain(j.get("content", "")),
            "source": "greenhouse",
        })
    return out


def _lever_body(j: dict) -> str:
    """Everything Lever holds about a role, not just the opening paragraph.

    `description` is only the intro blurb. The REQUIREMENTS — including the
    years-of-experience line the hard gate turns on — live in `lists`, a
    structured array of {text: "you should apply if", content: "<ul>…"}, and
    reading only `description` threw all of it away.

    Measured across 534 live postings on 2026-08-09: `lists` adds >200 chars on
    245 of the 532 that have a description (46%), and on 198 of those the
    experience requirement appears NOWHERE else — so `min_years_required`
    returned None and the ≥4-years gate failed OPEN. Two roles passing the
    matcher today are admitted exactly that way: CRED "data scientist" (really
    4 yrs) and Hevo "Security and Compliance Engineer" (really 5).

    CRED is the sharper case: its `description` is empty outright and the whole
    2,353-char JD is in `lists`. That is the role that arrived with a blank
    description and no `_detail` endpoint — it was never an unreadable JD, it
    was a JD we were reading out of the wrong field.

    `_plain` still caps the result at DESC_CAP and rescues the experience line
    when it truncates, so concatenating more text can only add signal.
    """
    parts = [j.get("descriptionPlain") or j.get("description") or ""]
    for sec in j.get("lists") or []:
        parts.append(f"{sec.get('text', '')} {sec.get('content', '')}")
    parts.append(j.get("additionalPlain") or j.get("additional") or "")
    return "\n".join(p for p in parts if p and p.strip())


def lever(company: str, token: str) -> list[dict]:
    d = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in d:
        cats = j.get("categories", {}) or {}
        out.append({
            "id": str(j.get("id", "")),
            "company": company,
            "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", "") or j.get("applyUrl", ""),
            "department": cats.get("team", "") or "",
            "description": _plain(_lever_body(j)),
            "source": "lever",
        })
    return out


def ashby(company: str, token: str) -> list[dict]:
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for j in d.get("jobs", []):
        loc = j.get("location", "") or ""
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"Remote — {loc}" if loc else "Remote"
        out.append({
            "id": str(j.get("id", "")),
            "company": company,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "department": j.get("department", "") or j.get("team", "") or "",
            "description": _plain(j.get("descriptionPlain", "") or j.get("descriptionHtml", "")),
            "source": "ashby",
        })
    return out


# Targeted India+role searches so we never page whole 1000+ finance boards.
# Workday returns these relevance-ranked; the top few pages are what we want.
WORKDAY_QUERIES = [
    "software engineer India",
    "full stack developer India",
    "frontend engineer India",
    "backend engineer India",
    "machine learning engineer India",
    "India",   # some tenants (Expedia wd108) AND multi-word queries to zero
]
# Pages per query, at Workday's hard limit of 20 a page (25 and 50 are a 400).
# Was 3 — 60 top-ranked results per query — which left 39 of the 65 boards
# truncated and only 3,149 of 9,667 India postings reachable. At 25 (500 a
# query) every board clears except Citi (1,041) and Amgen (974), and those two
# mark themselves partial below rather than pretending to be complete. Only the
# boards that are actually deep pay for the extra pages: the walk is bounded by
# each query's own `total`, so a 51-role board still costs three requests.
WORKDAY_MAX_PAGES = 25


def workday(company: str, host: str, tenant: str, site: str) -> list[dict]:
    """Workday cxs API. Runs a handful of targeted India+role searches (cheap)
    instead of paging the whole board, dedupes across them by job path. The JD
    is fetched lazily per candidate via enrich_description().

    "Instead of paging the whole board" is a deliberate trade — and the cost of
    it has to be declared, or revalidate() reads this board's silence as proof.
    Measured across all 65 Workday boards on 2026-08-19: 9,667 India postings
    exist, 3,149 are reachable at 3 pages x 20, and 39 of the 65 boards are
    truncated (Citi 60 of 1042, Amgen 60 of 979, ABB 60 of 439). So the board
    reports its own `total` below and says when it only skimmed.

    Paging deeper is a separate change with its own hazards, all measured here
    so they are not rediscovered: `limit` above 20 is a 400, `total` is only
    populated on the offset=0 response, and past the end Workday WRAPS — offset
    1060/1200/2000 on Citi each return 20 already-seen rows rather than an empty
    page, so "page until empty" would never terminate.
    """
    base = f"https://{host}/wday/cxs/{tenant}/{site}"

    fails: list[tuple[str, str]] = []    # (why, kind) per failed request
    totals: dict[str, int] = {}          # query → the board's own India count

    def page(query: str, offset: int) -> list | None:
        """Postings for one (query, page), or None if the request itself failed."""
        r = None
        try:
            r = _fetch_list("POST", f"{base}/jobs",
                            json={"limit": 20, "offset": offset,
                                  "searchText": query, "appliedFacets": {}})
            d = r.json()
            # Only the first page carries a real total; deeper offsets report 0.
            if not offset and isinstance(d.get("total"), int):
                totals[query] = d["total"]
            return d.get("jobPostings", [])
        except Exception as e:
            fails.append(_list_failure(e, r))
            return None

    seen: dict[str, dict] = {}

    def absorb(posts: list | None) -> None:
        if posts is None:
            return
        for j in posts:
            path = j.get("externalPath", "")
            if not path or path in seen:
                continue
            loc = j.get("locationsText", "")
            if not loc:
                # some tenants (Thomson Reuters) omit locationsText; the
                # path segment carries it: /job/India-Bengaluru-Karnataka/…
                parts = path.split("/")
                loc = parts[2].replace("-", " ") if len(parts) > 3 else ""
            seen[path] = {
                "id": path,
                "company": company,
                "title": j.get("title", ""),
                "location": loc,
                "url": f"https://{host}/en-US/{site}{path}",
                "department": "",
                "description": "",                   # filled lazily
                "_detail": f"{base}{path}",          # cxs detail endpoint
                "source": "workday",
            }

    # Phase 1 — one page per query, which is also how `total` is learned: only
    # the offset=0 response carries it (deeper offsets report 0).
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for posts in ex.map(lambda q: page(q, 0), WORKDAY_QUERIES):
            absorb(posts)

    # Phase 2 — walk each query out to its own total, bounded by the budget.
    # Offsets are derived from `total` and never reach it, which is what keeps
    # the walk out of the wrap-around region past the end of the result set.
    # A query whose total we never learned falls back to the old fixed depth.
    deep: list[tuple[str, int]] = []
    for q in WORKDAY_QUERIES:
        end = totals.get(q)
        end = min(end, WORKDAY_MAX_PAGES * 20) if end is not None else 3 * 20
        deep += [(q, off) for off in range(20, end, 20)]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for posts in ex.map(lambda t: page(*t), deep):
            absorb(posts)

    _list_outcome(company, fails, len(WORKDAY_QUERIES) + len(deep), len(seen))
    # Where a query has more India postings than the budget reads, the roles
    # past the window are unread, not withdrawn, and revalidate() must not be
    # allowed to count them as pulled.
    reach = WORKDAY_MAX_PAGES * 20
    over = max((t for t in totals.values() if t > reach), default=0)
    missing = [q for q in WORKDAY_QUERIES if q not in totals]
    if over:
        mark_partial(company, f"read {reach} of {over} on the deepest query")
    elif missing:
        mark_partial(company, f"no total reported for {len(missing)} query(s)")
    return list(seen.values())


def smartrecruiters(company: str, token: str) -> list[dict]:
    """SmartRecruiters public postings API (paged). JD fetched lazily."""
    out, offset, total = [], 0, 1
    while offset < total:
        d = _get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}")
        total = d.get("totalFound", 0)
        page = d.get("content", [])
        if not page:
            break
        for j in page:
            loc = j.get("location") or {}
            out.append({
                "id": str(j.get("id", "")),
                "company": company,
                "title": j.get("name", ""),
                "location": loc.get("fullLocation") or ", ".join(
                    p for p in (loc.get("city"), loc.get("country")) if p),
                "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
                "department": (j.get("department") or {}).get("label", ""),
                "description": "",                    # filled lazily
                "_detail": j.get("ref", ""),
                "source": "smartrecruiters",
            })
        offset += 100
    return out


def eightfold(company: str, token: str) -> list[dict]:
    """Eightfold job-board API (token = subdomain, e.g. netapp). India-scoped
    at the API level; JD fetched lazily."""
    base = f"https://{token}.eightfold.ai/api/apply/v2/jobs"
    positions, start, count = [], 0, 1
    while start < min(count, 300):        # API serves 10/page regardless of num
        d = _get(f"{base}?domain={token}.com&location=India&start={start}")
        count = d.get("count", 0)
        page = d.get("positions", [])
        if not page:
            break
        positions.extend(page)
        start += len(page)
    out = []
    for j in positions:
        out.append({
            "id": str(j.get("id", "")),
            "company": company,
            "title": j.get("name", ""),
            "location": j.get("location", ""),
            "url": j.get("canonicalPositionUrl", ""),
            "department": j.get("department", "") or "",
            "description": _plain(j.get("job_description", "")),
            "_detail": f"{base}/{j.get('id','')}?domain={token}.com",
            "source": "eightfold",
        })
    return out


# No keyword queries, for the same reason as google() below: `base_query=""`
# with `country=IND` returns the whole India board — 2,595 roles — and each of
# the five keyword searches this replaced is a strict SUBSET of it. Walked in
# full they came to 292 / 1 / 35 / 4 / 78, and contributed **zero** ids the
# query-less walk does not already hold. The adapter was reading 208 of 2,595.
AMAZON_PAGE = 100            # `result_limit` the API honours
AMAZON_MAX_PAGES = 60        # x 100 — a runaway guard, not a budget


def amazon(company: str, token: str) -> list[dict]:
    """amazon.jobs public search JSON, India-scoped. JD is in the list payload.

    Walked to the end rather than sampled: `hits` reports the India total and
    `offset` pages cleanly to it (292 → 292 on the old widest query, then an
    empty page), so there is no reason to stop at the first 100.
    """
    seen: dict[str, dict] = {}
    total: int | None = None

    def page(offset: int) -> list | None:
        """One page of India jobs, or None if the request itself failed."""
        nonlocal total
        try:
            d = _fetch_list("GET", "https://www.amazon.jobs/en/search.json"
                            f"?base_query=&country=IND&result_limit={AMAZON_PAGE}"
                            f"&offset={offset}&sort=recent").json()
            if isinstance(d.get("hits"), int):
                total = d["hits"]
            return d.get("jobs", [])
        except Exception:
            return None

    failed, rows, pages, capped = 0, 0, 0, False
    while True:
        if pages >= AMAZON_MAX_PAGES:
            capped = True
            break
        jobs = page(rows)
        pages += 1
        if jobs is None:                      # request failed — a hole, not an end
            failed += 1
            break
        if not jobs:                          # the board says there is no more
            break
        rows += len(jobs)
        for j in jobs:
            jid = str(j.get("id", ""))
            if not jid or jid in seen:
                continue
            jd = " ".join(filter(None, (
                j.get("description", ""),
                j.get("basic_qualifications", ""),
                j.get("preferred_qualifications", ""))))
            seen[jid] = {
                "id": jid,
                "company": company,
                "title": j.get("title", ""),
                "location": j.get("normalized_location") or j.get("location", ""),
                "url": f"https://www.amazon.jobs{j.get('job_path', '')}",
                "department": j.get("job_category", "") or "",
                "description": _plain(jd),
                "source": "amazon",
            }
    _check_pages(company, failed, pages, len(seen))
    if failed:
        pass                                  # _check_pages already marked it
    elif capped:
        mark_partial(company, f"page cap {AMAZON_MAX_PAGES} hit at {len(seen)} jobs")
    elif _short_walk(rows, total):
        mark_partial(company, f"read {rows} of {total} India rows listed")
    return list(seen.values())


def uber(company: str, token: str) -> list[dict]:
    """Uber careers search API. India-filtered server-side; JD is in the payload.

    Uber moved its careers site to jobs.uber.com (a Next.js/HappyDance app) and
    the old www.uber.com/api/loadSearchJobsResults POST endpoint now 404s — that
    is what silently took this board out of supply.

    The replacement is a plain GET, but its parameter names are unforgiving and
    wrong ones are ignored rather than rejected: `countries` (not `country`),
    spelled out (`India`, not `IND`), and a lowercase `pagesize`. Get any of
    them wrong and you get a 200 holding page 1 of the *unfiltered* global list,
    which is why this is pinned to the names in the site's own JOB_QUERY_KEYS
    bundle. The sanity check below is the guard: India is a small slice of a
    ~670-role global board, so a full first page means the filter didn't apply.
    """
    out, page_n = [], 1
    while page_n <= 10:
        r = requests.get(
            "https://jobs.uber.com/api/jobs/search/",
            headers={**HEADERS, "Accept": "application/json"},
            timeout=TIMEOUT,
            params={"countries": "India", "pagesize": 100, "page": page_n})
        r.raise_for_status()
        payload = r.json()
        results = payload.get("jobs") or []
        if page_n == 1 and payload.get("totalJobs", 0) >= 500:
            raise ValueError(
                f"uber: country filter not applied — {payload.get('totalJobs')} "
                f"roles returned; the search API's parameter names have changed")
        for j in results:
            loc_s = "; ".join(
                ", ".join(filter(None, (loc.get("City"), loc.get("Country"))))
                for loc in (j.get("Locations") or []))
            path = next((u.get("Url") for u in (j.get("Urls") or []) if u.get("IsDefault")),
                        None) or f"/en/jobs/{j.get('Id', '')}/"
            out.append({
                "id": str(j.get("Id", "")),
                "company": company,
                "title": j.get("Title", ""),
                "location": loc_s,
                "url": "https://jobs.uber.com" + path,
                "department": ", ".join(t for t in (j.get("Teams") or []) if isinstance(t, str)),
                "description": _plain(j.get("Description", "")),
                "source": "uber",
            })
        if page_n >= (payload.get("totalPages") or 1):
            break
        page_n += 1
    return out


ORACLE_PAGE = 100            # `limit` the CE API honours
ORACLE_MAX_PAGES = 40        # x 100 — a runaway guard, not a budget


def oracle(company: str, host: str, site: str) -> list[dict]:
    """Oracle Cloud Recruiting (HCM) public CE API — used by JPMorgan Chase etc.
    JD text ships in the list payload (short description + responsibilities +
    qualifications).

    ── `location=India`, not the word "India" in a keyword ──
    This used to run three keyword searches — "software engineer India", "full
    stack India", "machine learning India" — two pages each. "India" in a
    free-text keyword is not a location filter, and the API never said so: it
    answered 200 with a relevance/recency mix in which **61 of the first 100
    results for "software engineer India" were United States, United Kingdom
    and Singapore**. So most of the page budget was spent fetching roles the
    location filter downstream would throw away, and the India roles that did
    exist were mostly past the cut.

    `location=India` is a real filter and the numbers say so plainly — for
    JPMorgan it reports 439 and returns 100 India out of 100, against 1381 and
    22 out of 100 for the keyword. Checked on every tenant: JPMorgan 439,
    Honeywell 313, Oracle 225, Amex 70, Akamai 17, Dell 16 — 1,080 India roles,
    each walkable to the end in five pages or fewer.

    Two nearby params were tried and rejected by measurement rather than by
    reading: `locationId` is silently IGNORED (it returns the unfiltered 7,388
    global board, the same as sending no filter at all), and
    `selectedCountriesFacet` is a 400. Only `location` works, so only `location`
    is used — and `TotalJobsCount` is asserted against what we walked.
    """
    # expand= is required — without it the requisitionList child is omitted
    base = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},location=India"
            f",sortBy=POSTING_DATES_DESC")

    fails: list[tuple[str, str]] = []    # (why, kind) per failed request
    total: int | None = None

    def page(offset: int) -> list | None:
        """One page of India requisitions, or None if the request failed."""
        nonlocal total
        r = None
        try:
            r = _fetch_list("GET", f"{base},limit={ORACLE_PAGE},offset={offset}")
            d = r.json()
            items = d.get("items", [])
            if not items:
                return []
            if isinstance(items[0].get("TotalJobsCount"), int):
                total = items[0]["TotalJobsCount"]
            return items[0].get("requisitionList", [])
        except Exception as e:
            fails.append(_list_failure(e, r))
            return None

    seen: dict[str, dict] = {}
    offset, pages, capped = 0, 0, False
    while True:
        if pages >= ORACLE_MAX_PAGES:
            capped = True
            break
        reqs = page(offset)
        pages += 1
        if reqs is None:                     # request failed — a hole, not an end
            break
        if not reqs:                         # the board says there is no more
            break
        offset += len(reqs)
        for j in reqs:
            jid = str(j.get("Id", ""))
            if not jid or jid in seen:
                continue
            locs = [j.get("PrimaryLocation", "")] + [
                s.get("Name", "") for s in j.get("secondaryLocations", [])]
            jd = " ".join(filter(None, (
                j.get("ShortDescriptionStr", ""),
                j.get("ExternalResponsibilitiesStr", ""),
                j.get("ExternalQualificationsStr", ""))))
            seen[jid] = {
                "id": jid,
                "company": company,
                "title": j.get("Title", ""),
                "location": "; ".join(filter(None, locs)),
                "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                "department": j.get("JobFamily", "") or "",
                # list JD is often just a teaser — full JD fetched lazily
                "description": _plain(jd) if len(jd) > 400 else "",
                "_detail": (f"https://{host}/hcmRestApi/resources/latest/"
                            f"recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                            f"&finder=ById;siteNumber={site},Id={jid}"),
                "source": "oracle",
            }

    _list_outcome(company, fails, pages, len(seen))
    # `offset` counts ROWS READ, which is what the board's own total is in —
    # `seen` is deduped and would read short on a board that repeats a listing.
    if fails:
        pass                                  # _list_outcome already marked it
    elif capped:
        mark_partial(company, f"page cap {ORACLE_MAX_PAGES} hit at {len(seen)} jobs")
    elif _short_walk(offset, total):
        mark_partial(company, f"read {offset} of {total} India rows listed")
    return list(seen.values())


def keka(company: str, token: str) -> list[dict]:
    """Keka Hire public career-portal API (token = 'tenant/orgGuid').
    The guid is in the /careers/ page source: /ats/documents/{guid}/…"""
    tenant, guid = token.split("/", 1)
    d = _get(f"https://{tenant}.keka.com/careers/api/embedjobs/default/active/{guid}")
    out = []
    for j in d if isinstance(d, list) else []:
        locs = ", ".join(sorted({
            f"{loc.get('city', '')}, {loc.get('countryName', '')}".strip(", ")
            for loc in j.get("jobLocations", [])}))
        exp = j.get("experience", "")
        desc = _plain(j.get("description", ""))
        if exp:      # "1-3" → surface for the years-required hard drop
            desc = f"Experience required: {exp} years. {desc}"[:DESC_CAP]
        out.append({
            "id": str(j.get("id", "")),
            "company": company,
            "title": j.get("title", ""),
            "location": locs,
            "url": f"https://{tenant}.keka.com/careers/jobdetails/{j.get('id', '')}",
            "department": j.get("departmentName", "") or "",
            "description": desc,
            "source": "keka",
        })
    return out


def ainterviews(company: str, token: str) -> list[dict]:
    """ainterviews.com hosted job boards (token = board slug, e.g. lenskart_ho)."""
    d = _get(f"https://ainterviews.com/api/job_board/{token}/jobs/")
    out = []
    for j in d.get("jobs", []):
        desc = _plain(j.get("description", ""))
        lvl = j.get("experience_level", "")
        if lvl:
            desc = f"Level: {lvl}. {desc}"[:DESC_CAP]
        out.append({
            "id": str(j.get("id", "")),
            "company": company,
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": f"https://ainterviews.com/job_board/{token}/job/{j.get('id', '')}/",
            "department": j.get("category", "") or "",
            "description": desc,
            "source": "ainterviews",
        })
    return out


def mynexthire(company: str, token: str) -> list[dict]:
    """myNextHire career-portal API (token = tenant subdomain, e.g. 'swiggy').

    Found 2026-07-23 by loading the careers SPA in a real browser and reading the
    network tab — the board is JS-rendered, so probing the HTML found nothing and
    this was previously written off as "no public JSON". It is a POST and rejects
    an empty body with "Source is mandatory"; source must be exactly "careers".
    Verified unauthenticated (no cookies/headers needed) from plain Python.

    Unusually generous: full JD in the list response AND numeric expMin/expMax,
    which is a far better experience signal than regex-scraping years out of prose.
    """
    r = requests.post(f"https://{token}.mynexthire.com/employer/careers/reqlist/get",
                      headers={**HEADERS, "Content-Type": "application/json"},
                      json={"source": "careers"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("reqDetailsBOList", []):
        # office and address are often the same string ("Bangalore"/"Bangalore"),
        # so join the distinct parts rather than blindly concatenating them.
        locs = ", ".join(sorted({
            ", ".join(dict.fromkeys(p for p in (l.get("office"), l.get("address")) if p))
            for l in (j.get("locationList") or [])})) or (j.get("location") or "")
        desc = _plain(j.get("jdDisplay", ""))
        lo, hi = j.get("expMin"), j.get("expMax")
        if lo is not None and hi:      # surface for the ≥N-years hard drop
            desc = f"Experience required: {lo:g}-{hi:g} years. {desc}"[:DESC_CAP]
        rid = j.get("reqId", "")
        out.append({
            "id": str(rid),
            "company": company,
            "title": j.get("reqTitle", "") or j.get("designation", ""),
            "location": locs,
            "url": f"https://careers.{token}.com/#/careers/{rid}",
            "department": j.get("buName", "") or "",
            "description": desc,
            "source": "mynexthire",
        })
    return out


def rippling(company: str, token: str) -> list[dict]:
    """Rippling's public ATS board API (token = board slug, e.g. aerospike-inc).

    Unauthenticated JSON, but the list carries only title/department/location —
    the JD lives behind a per-job fetch, so it is enriched lazily like Workday.
    """
    d = _get(f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs")
    out = []
    for j in d if isinstance(d, list) else []:
        uid = str(j.get("uuid", ""))
        out.append({
            "id": uid,
            "company": company,
            "title": j.get("name", ""),
            "location": (j.get("workLocation") or {}).get("label", ""),
            "url": j.get("url", "") or f"https://ats.rippling.com/{token}/jobs/{uid}",
            "department": (j.get("department") or {}).get("label", ""),
            "description": "",                    # filled lazily
            "_detail": f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{uid}",
            "source": "rippling",
        })
    return out


def workable(company: str, token: str) -> list[dict]:
    """Workable's public careers widget (token = account slug).

    Unauthenticated, and `?details=true` returns the full JD inline, so no lazy
    enrichment is needed. Workable also exposes an `experience` band
    (Associate/Mid-Senior/…) — surfaced into the description for the fit gate.
    """
    d = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
    out = []
    for j in (d.get("jobs") or []) if isinstance(d, dict) else []:
        loc = ", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)
        desc = _plain(j.get("description", ""))
        exp = j.get("experience")
        if exp:
            desc = f"Experience level: {exp}. {desc}"[:DESC_CAP]
        out.append({
            "id": str(j.get("shortcode", "")),
            "company": company,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("shortlink") or j.get("url", ""),
            "department": j.get("department", "") or "",
            "description": desc,
            "source": "workable",
        })
    return out


# Role searches for aggregators that query by keyword rather than serve a board.
AGGREGATOR_QUERIES = [
    "software engineer",
    "full stack developer",
    "frontend developer angular",
    "backend developer python",
    "ai engineer llm",
]


# Adzuna's free tier is ~1000 calls/month. The poller runs every 30 min, so
# calling it on every run costs 5 queries × 48 runs × 30 days = 7,200/month —
# 7x over, and the key would be throttled within ~4 days. Restrict it to a few
# UTC hours instead: ~1-2 runs land in each window, so 15-30 calls/day
# (~450-900/month), which fits with margin. Job postings do not appear fast
# enough for 30-min aggregator polling to add anything anyway.
ADZUNA_HOURS = {2, 10, 18}


def adzuna(company: str, token: str) -> list[dict]:
    """Adzuna India aggregator (token unused; credentials come from the env).

    Unlike every other adapter here this is not one company's board — it is an
    aggregator across thousands of Indian sources (company sites, job boards),
    so it is the one lever that scales supply without hand-adding companies.

    Needs a FREE app_id/app_key from https://developer.adzuna.com. Silently
    returns [] when unset, so the board is simply inert until the secrets exist,
    and likewise outside ADZUNA_HOURS (see the quota note above). `--backlog`
    and `--golive` set ADZUNA_IGNORE_SCHEDULE, since those are explicit
    "score everything now" sweeps.
    """
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not (app_id and app_key):
        return []
    if (not os.environ.get("ADZUNA_IGNORE_SCHEDULE")
            and dt.datetime.now(dt.timezone.utc).hour not in ADZUNA_HOURS):
        return []
    seen: dict[str, dict] = {}
    for what in AGGREGATOR_QUERIES:
        try:
            d = _get("https://api.adzuna.com/v1/api/jobs/in/search/1?" + urllib.parse.urlencode({
                "app_id": app_id, "app_key": app_key,
                "results_per_page": 50, "what": what,
                "max_days_old": 7, "content-type": "application/json",
            }))
        except Exception:
            continue
        for j in (d.get("results") or []) if isinstance(d, dict) else []:
            jid = str(j.get("id", ""))
            if not jid or jid in seen:
                continue
            seen[jid] = {
                "id": jid,
                # the real employer, not "Adzuna" — dedupe and the per-company
                # cap both key off this, and the referral lookup needs it too.
                "company": (j.get("company") or {}).get("display_name", "") or "Unknown",
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "department": (j.get("category") or {}).get("label", ""),
                "description": _snippet_note(_plain(j.get("description", ""))),
                "source": "adzuna",
            }
    return list(seen.values())


def enrich_description(job: dict) -> None:
    """For sources whose list omits the JD (Workday/SmartRecruiters/Eightfold),
    fetch it on demand. No-op if already present or not enrichable. Never raises.

    A failure here must never leave the JD merely *empty*. An empty description
    makes min_years_required() return None, which skips the hard experience
    cutoff altogether — so a broken enrichment silently promotes every role it
    touches instead of dropping it. Rippling hit exactly this: its `description`
    is an object, `_plain()` raised TypeError on it, and the bare except turned a
    parse bug into 13 ungated jobs. Failures are now labelled so the scorer
    discounts what it could not read.
    """
    if job.get("description") or not job.get("_detail"):
        return
    try:
        url = job["_detail"]
        if ".freshteam.com" in url:                   # HTML detail page, not JSON
            m = _FT_JD.search(_get_detail_text(url))
            job["description"] = _plain(html.unescape(m.group(1))) if m else ""
            d = None
        else:
            # Darwinbox 403s the detail call without a Referer, and its JD is
            # double-encoded HTML — unescape before _plain(), which strips tags.
            d = (_get_detail(url, referer=_origin(url)) if ".darwinbox.in" in url
                 else _get_detail(url))
        # Every branch below falls through to the empty-description guard at the
        # bottom; returning early here would skip it and hand the experience gate
        # a blank JD, which is the fail-OPEN case this function exists to prevent.
        if not isinstance(d, dict):
            pass
        elif isinstance(d.get("message"), dict) and d["message"].get("job"):  # darwinbox
            info = (d["message"]["job"] or [{}])[0]
            desc = _plain(html.unescape(info.get("jd") or ""))
            # `experience` reads "5 - 9 Years"; the numeric fields on the detail
            # call are MONTHS (60-108), so parse the string, not those.
            exp = re.search(r"(\d+)\s*-\s*(\d+)", info.get("experience") or "")
            if exp and desc:
                desc = f"Experience required: {exp.group(1)}-{exp.group(2)} years. {desc}"[:DESC_CAP]
            job["description"] = desc
        elif "jobPostingInfo" in d:                   # workday
            info = d.get("jobPostingInfo", {})
            job["description"] = _plain(info.get("jobDescription", ""))
            if info.get("externalUrl"):
                job["url"] = info["externalUrl"]
        elif "items" in d:                            # oracle CE details
            items = d.get("items") or [{}]
            info = items[0]
            job["description"] = _plain(" ".join(filter(None, (
                info.get("ExternalDescriptionStr", ""),
                info.get("ExternalQualificationsStr", ""),
                info.get("CorporateDescriptionStr", "")))))
        elif "jobAd" in d:                            # smartrecruiters
            secs = d.get("jobAd", {}).get("sections", {})
            job["description"] = _plain(" ".join(
                (secs.get(k) or {}).get("text", "") for k in ("jobDescription", "qualifications")))
            if d.get("applyUrl"):
                job["url"] = d["applyUrl"]
        elif "uuid" in d and "description" in d:      # rippling
            # `description` is an object, not a string: {"company": …, "role": …}.
            # Only `role` is used. The `company` blurb is identical on every
            # posting and, for Aerospike, name-drops "machine learning,
            # generative, and agentic AI" — pasting it into each JD would score
            # the Finance Intern as an AI match on strong_stack keywords alone.
            desc = d.get("description")
            if isinstance(desc, dict):
                desc = desc.get("role") or desc.get("company") or ""
            job["description"] = _plain(desc or "")
        else:                                         # eightfold
            job["description"] = _plain(d.get("job_description", ""))
    except Exception as e:
        print(f"  enrich failed [{job.get('source','?')}] {job.get('company','?')} — "
              f"{type(e).__name__}: {e}")
    if not job.get("description"):
        # Unreadable, not unrestricted: without this the experience gate finds no
        # requirement and waves the role straight through.
        job["description"] = SNIPPET_NOTE.strip()


GOOGLE_BASE = "https://www.google.com/about/careers/applications"
# No keyword queries. `q=""` with `location=India` returns the whole India
# board — 304 roles — and every one of the five keyword searches this used to
# run is a strict SUBSET of it: walked in full, "software engineer" (163),
# "full stack" (87), "machine learning" (74), "frontend" (12) and "backend"
# (19) contributed **zero** ids the query-less walk does not already have.
# Deduped they came to 133 of 304, because each was capped at three pages.
GOOGLE_MAX_PAGES = 60         # 20 cards a page — a runaway guard, not a budget

# id, slug and title arrive together on the "Learn more" anchor — the only
# place in Google's markup where the three are unambiguously paired.
_G_CARD = re.compile(
    r'href="jobs/results/(\d+)-([a-z0-9\-]+)[^"]*"\s+aria-label="Learn more about ([^"]+)"')
_G_LOC = re.compile(r'class="r0wTof\s*">([^<]+)</span>')
_G_QUAL = re.compile(r"<h4>(?:Minimum|Preferred) qualifications</h4>\s*<ul>(.*?)</ul>", re.S | re.I)


def google(company: str, token: str) -> list[dict]:
    """Google Careers — server-rendered HTML, no JSON API and none needed.

    Watching the page in Chrome showed it makes NO job XHR at all: the results
    are rendered server-side, so a plain GET gets everything and this runs in CI
    like any other board.

    One trap cost a wrong "0 jobs found": the page sets `<base href>`, so every
    job link in the markup is RELATIVE (`jobs/results/123-slug`). A regex
    anchored on the absolute path matches nothing even though the jobs are right
    there. Match the relative form.

    The qualification bullets ship on the RESULTS page, not just the detail page
    — so the experience gate gets real requirements without a second fetch
    (verified: yrs=8/5/None rather than the None-for-everything that a teaser
    would produce).
    """
    seen: dict[str, dict] = {}

    def page(query: str, pg: int) -> str | None:
        """Result-page HTML for one (query, page), or None if the request failed."""
        try:
            r = _fetch_list("GET", f"{GOOGLE_BASE}/jobs/results/",
                            params={"location": "India", "q": query, "page": pg})
            return r.text
        except Exception:
            return None

    # Walked in order rather than fanned out, because the terminator is the
    # first page with no cards on it and that is only knowable in sequence.
    pg, failed, capped = 1, 0, False
    while True:
        if pg > GOOGLE_MAX_PAGES:
            capped = True
            break
        body = page("", pg)
        if body is None:                      # request failed — a hole, not an end
            failed += 1
            break
        marks = list(_G_CARD.finditer(body))
        if not marks:                         # the board says there is no more
            break
        for i, m in enumerate(marks):
            jid, slug, title = m.group(1), m.group(2), html.unescape(m.group(3)).strip()
            if jid in seen:
                continue
            # a card's body is the markup between the previous card's
            # anchor and this one's
            chunk = body[marks[i - 1].end() if i else 0:m.start()]
            locs = dict.fromkeys(l.strip() for l in _G_LOC.findall(chunk))
            quals = " ".join(_TAG.sub(" ", q) for q in _G_QUAL.findall(chunk))
            seen[jid] = {
                "id": jid,
                "company": company,
                "title": title,
                "location": "; ".join(locs),
                "url": f"{GOOGLE_BASE}/jobs/results/{jid}-{slug}",
                "department": "",
                "description": _plain(html.unescape(quals)),
                "source": "google",
            }
        pg += 1
    _check_pages(company, failed, pg, len(seen))
    if capped:
        mark_partial(company, f"page cap {GOOGLE_MAX_PAGES} hit at {len(seen)} jobs")
    return list(seen.values())


# The API serves 10 results a page and there is no parameter that widens it —
# `num`, `size`, `pageSize`, `limit`, `rows`, `perPage` and `hits` were all
# tried and all return 10. This constant is therefore an OBSERVATION, not a
# request: it is here so the stride below can be sanity-checked against it, and
# the walk advances by what each page actually returned rather than by this.
PCSX_PAGE = 10
# 577 India roles at Qualcomm, 10 a page — so the cap is a runaway guard, not a
# budget. It sits far above any real board; hitting it means the terminator
# broke, and the board is marked partial rather than quietly truncated.
PCSX_MAX_PAGES = 200


def pcsx(company: str, host: str, domain: str) -> list[dict]:
    """Eightfold's *newer* career-site API (`/api/pcsx/search`).

    Not the same thing as eightfold() above, and that distinction cost real
    time: Qualcomm's careers page fingerprints as Eightfold, but the classic
    `/api/apply/v2/jobs` LIST endpoint answers 403 to any scripted GET, so the
    board was written off twice as "CDN reference, not a real board". Watching
    the page's own requests showed it calling `/api/pcsx/search` instead —
    which answers 200 server-side, no browser needed. 549 India engineering
    roles were sitting behind that one path difference.

    Two things worth keeping:
      * `host` and `domain` are NOT the same (careers.qualcomm.com vs
        qualcomm.com), which is why this takes both instead of one token.
      * A bare GET returns 422 with a `messages` body naming the missing
        field. Unlike most boards, this API tells you what it wants — read the
        422 rather than guessing params.

    The JD is fetched lazily: the per-job `/api/apply/v2/jobs/{id}` DETAIL
    endpoint is not 403-blocked (only the list is) and returns `job_description`
    at the top level, which enrich_description()'s eightfold branch already reads.

    ── Enumerate the board; do not sample it ──
    This used to fire six role queries at three pages of a requested 50, and
    read 99 of Qualcomm's 577 India roles. Two things were wrong with that.

    `num` is ignored: the API serves 10 whatever you ask for, so `start` values
    of 0/50/100 sampled ranks 0-9, 50-59 and 100-109 and skipped 40 in every 50.
    Nothing noticed, because 18 requests returned 18 pages of jobs and every
    check asked whether a request had *failed*. The count that reached
    board_supply was rock-steady — 102, 100, 101, 103, 100, 101, 93, 99 — which
    is what a stable sample of a moving board looks like, and is why the
    supply-drop alarm never fired either.

    And the queries were unnecessary: dropping `query` entirely returns the whole
    India board (577) where the best single query returns 547. So there is one
    walk, no queries, and the location filter is asserted rather than assumed —
    `location=Antarctica` returns 0 and a misspelt parameter name returns the
    1973-role global board, so a typo here would silently widen us worldwide.

    The walk advances by the length of the page it just read, not by a fixed
    stride, because `count` drifts while you are walking it (575 → 577 mid-walk,
    observed). Striding by 10 against a shrinking `count` steps over the tail;
    striding by what actually arrived cannot. `count` is therefore never a
    terminator — an empty page is — and is used only to sanity-check the total.
    """
    base = f"https://{host}/api/pcsx/search"
    seen: dict[str, dict] = {}

    def page(start: int) -> tuple[list | None, int | None]:
        """One page from `start`, and the board's self-reported India total."""
        try:
            url = (f"{base}?domain={urllib.parse.quote(domain)}"
                   f"&location=India&start={start}&num={PCSX_PAGE}")
            d = _fetch_list("GET", url).json()
            if not isinstance(d, dict):
                return None, None
            data = d.get("data") or {}
            return (data.get("positions") or []), data.get("count")
        except Exception:
            return None, None

    start, pages, failed, count, rows = 0, 0, 0, None, 0
    while pages < PCSX_MAX_PAGES:
        positions, total = page(start)
        pages += 1
        if positions is None:                 # request failed — a hole, not an end
            failed += 1
            break
        if total is not None:
            count = total
        if not positions:                     # the board says there is no more
            break
        for j in positions:
            jid = str(j.get("id", ""))
            if not jid or jid in seen:
                continue
            locs = j.get("locations") or j.get("standardizedLocations") or []
            url = j.get("positionUrl") or ""
            if url.startswith("/"):
                url = f"https://{host}{url}"
            seen[jid] = {
                "id": jid,
                "company": company,
                "title": j.get("name", ""),
                "location": "; ".join(locs) if isinstance(locs, list) else str(locs),
                "url": url,
                "department": j.get("department", "") or "",
                "description": "",                    # filled lazily
                "_detail": f"https://{host}/api/apply/v2/jobs/{jid}?domain={domain}",
                "source": "pcsx",
            }
        start += len(positions)
        rows += len(positions)

    _check_pages(company, failed, pages, len(seen))
    # Three ways this walk can end short of the board, all of which make a
    # missing role unprovable rather than pulled: a failed page, the runaway
    # guard, or reading fewer rows than the board says it holds.
    #
    # That last test counts ROWS READ, not jobs kept, and the difference is not
    # academic: Microsoft's board serves 220 rows carrying 28 duplicate ids, so
    # a complete walk of it yields 192 unique jobs. Comparing the deduped total
    # against `count` called that board permanently partial — which would have
    # switched off closure detection there for good, quietly, in the name of a
    # fix for exactly that kind of silent damage.
    if failed:
        pass                                  # _check_pages already marked it
    elif pages >= PCSX_MAX_PAGES:
        mark_partial(company, f"page cap {PCSX_MAX_PAGES} hit at {len(seen)} jobs")
    elif _short_walk(rows, count):
        mark_partial(company, f"read {rows} of {count} rows listed")
    return list(seen.values())


INBOX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "inbox")


def inbox(company: str, token: str) -> list[dict]:
    """Jobs harvested by a real browser, not by this process.

    The India-heavy boards — Naukri, LinkedIn, Instahyre, Wellfound — are
    login-walled, reCAPTCHA-gated or client-rendered, so no unauthenticated
    GET from a GitHub runner can reach them. They were written off as
    impossible in companies.yaml for exactly that reason. `harvest/run.py`
    now drives Chrome on a local Mac instead, drops normalised jobs here as
    JSONL, and commits them; this adapter is the seam where they rejoin the
    pipeline, so browser-sourced roles get the same fit gate, tiers, Slack
    and cockpit as every API board — no parallel scoring path to maintain.

    Discovery is deliberately decoupled from scoring: an evening where Chrome
    is asleep, logged out or blocked yields no new files and this returns the
    still-fresh ones. The 30-minute CI poll never depends on the browser.

    Each job carries its REAL employer ("Flipkart"), not "Naukri" — so the
    per-company cap, referral lookup and dedupe all keep working. Like the
    Adzuna rows, those companies aren't boards of ours, so the cockpit marks
    them `verifiable: False` rather than pretending it can prove them closed.
    """
    if not os.path.isdir(INBOX_DIR):
        return []
    cutoff = time.time() - INBOX_KEEP_DAYS * 86400
    out, seen = [], set()
    for fname in sorted(os.listdir(INBOX_DIR), reverse=True):
        if not fname.endswith(".jsonl"):
            continue
        path = os.path.join(INBOX_DIR, fname)
        # Stale files are ignored rather than trusted: a harvest that stopped
        # running must go quiet, not keep re-offering month-old listings as if
        # the browser had just seen them.
        if os.path.getmtime(path) < cutoff:
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue          # a torn last line beats losing the file
                if not (j.get("url") and j.get("title")):
                    continue
                k = (j.get("source", ""), j.get("id", ""))
                if k in seen:
                    continue          # newest file wins; same role re-harvested nightly
                seen.add(k)
                j.setdefault("company", company)
                j.setdefault("location", "")
                j.setdefault("department", "")
                j["description"] = _snippet_note(j.get("description", "") or "")
                out.append(j)
    return out


ICIMS_PAGE = 100             # `limit` is honoured; size/per_page/num are silently ignored
ICIMS_MAX_PAGES = 8


def icims(company: str, token: str) -> list[dict]:
    """iCIMS career sites (the Jibe/CSX front end) — `https://{host}/api/jobs`.

    token is the careers HOST, not a tenant slug: `careers.amd.com`,
    `careers.se.com`, `jobs.booking.com`. The `*.icims.com` hostnames these
    fingerprint as are the apply-side back end and are not what to poll — the
    front door returns clean JSON while `{tenant}.icims.com/jobs/search` just
    bounces you back with a `window.top.location.href` redirect.

    Generous: the list response carries description + responsibilities +
    qualifications inline, so no detail fetch is needed and the experience gate
    reads real requirements.

    The `location` filter is REAL, and was verified rather than assumed (see the
    Uber trap where a wrong param name silently returned the unfiltered global
    board): AMD gives 1064 jobs unfiltered, 203 for `location=India`, and 0 for
    a nonsense location — a filter that narrows and can also come back empty.
    Only `limit` widens a page; `size`/`per_page`/`num` are accepted and ignored.
    """
    base = f"https://{token}/api/jobs"
    out, seen = [], set()
    for page in range(1, ICIMS_MAX_PAGES + 1):
        d = _get(f"{base}?location=India&page={page}&limit={ICIMS_PAGE}")
        if not isinstance(d, dict):
            break
        jobs = d.get("jobs") or []
        for w in jobs:
            j = w.get("data") or {}
            jid = str(j.get("req_id") or j.get("slug") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            out.append({
                "id": jid,
                "company": company,
                "title": j.get("title", ""),
                "location": j.get("full_location") or ", ".join(
                    filter(None, (j.get("city"), j.get("state"), j.get("country")))),
                "url": j.get("apply_url", ""),
                "department": j.get("category", "") or "",
                "description": _plain(" ".join(filter(None, (
                    j.get("description", ""), j.get("responsibilities", ""),
                    j.get("qualifications", ""))))),
                "source": "icims",
            })
        if len(jobs) < ICIMS_PAGE:
            break
    return out


def atlassian(company: str, token: str) -> list[dict]:
    """Atlassian — one public JSON endpoint holding the entire board.

    NEXT-STEPS had this as "fully JS-rendered, needs the network tab" because
    the careers HTML has no jobs in it (its 56 "job links" are nav links). But
    the page feeds itself from `/endpoint/careers/listings`, a plain
    unauthenticated GET returning all ~230 roles with the FULL JD inline —
    overview + responsibilities + qualifications — so no enrichment is needed
    and the experience gate gets real requirements.

    iCIMS sits underneath (applyUrl points at *.icims.com), which is why the
    site fingerprints as iCIMS; the front door is friendlier than the back one.
    """
    d = _get(f"https://www.atlassian.com/endpoint/careers/listings")
    out = []
    for j in d if isinstance(d, list) else []:
        jid = str(j.get("id", ""))
        if not jid:
            continue
        desc = _plain(" ".join(filter(None, (
            j.get("overview", ""), j.get("responsibilities", ""), j.get("qualifications", "")))))
        out.append({
            "id": jid,
            "company": company,
            "title": j.get("title", ""),
            "location": "; ".join(j.get("locations") or []),
            "url": j.get("applyUrl") or (j.get("portalJobPost") or {}).get("portalUrl", ""),
            "department": j.get("category", "") or "",
            "description": desc,
            "source": "atlassian",
        })
    return out


DARWINBOX_PAGE_MAX = 15          # 10 jobs a page; the biggest tenant seen is 122


def darwinbox(company: str, token: str) -> list[dict]:
    """Darwinbox careers — the HR suite most Indian product companies run on.

    Cracked 2026-08-08 by mining the SPA bundle (technique 4), not the network
    tab: every `/ms/candidate*` route serves the same 18kB Angular shell, so
    path-guessing finds nothing. `main.js` carries the endpoint map outright —
    `apiURL:"/ms/candidateapi/"` plus `jobList:"job?page="` — and the whole API
    is public, unauthenticated, and free of the Cloudflare Turnstile that guards
    the *login* page. Sessions of browser work were spent on the wrong door.

    token is `tenant` or `tenant:companyId`. Multi-entity tenants (upGrad,
    PhysicsWallah) 404 with "Expecting a valid Company value" until companyId is
    supplied, and it goes in as a QUERY PARAM — the v2 bundle's interceptor does
    `clone({setParams:{companyId}})`, not a header. Read the companyId off the
    real careers URL; `companyinfo` needs it too, so it cannot bootstrap itself.

    Yield note, measured before shipping: across nine tenants this board carries
    almost no engineering. These companies run Darwinbox for HR and post their
    engineering roles elsewhere. Kept because it is cheap and boards fluctuate —
    but do NOT expect volume here (see NEXT-STEPS.md).
    """
    tenant, _, cid = token.partition(":")
    host = f"https://{tenant}.darwinbox.in"
    qs = f"&companyId={cid}" if cid else ""
    out = []
    for page in range(1, DARWINBOX_PAGE_MAX + 1):
        d = _get(f"{host}/ms/candidateapi/job?page={page}{qs}", referer=f"{host}/")
        if not isinstance(d, dict) or d.get("status") != "success":
            break
        jobs = (d.get("message") or {}).get("jobs") or []
        for j in jobs:
            jid = j.get("id", "")
            out.append({
                "id": jid,
                "company": company,
                "title": j.get("title") or j.get("designation_display_name", ""),
                "location": j.get("officelocation_show_arr", "")
                            or "; ".join(j.get("tool_tip_locations") or []),
                "url": f"{host}/ms/candidatev2/{cid or 'main'}/careers/jobDetails/{jid}",
                "department": j.get("department", "") or "",
                # the list carries no JD at all — only the detail call has one
                "_detail": f"{host}/ms/candidateapi/job/{jid}{('?' + qs[1:]) if qs else ''}",
                "source": "darwinbox",
            })
        if len(jobs) < 10:
            break
    return out


_FT_ANCHOR = re.compile(
    r'<a\s+([^>]*?href="/jobs/([A-Za-z0-9_\-]+)/([^"]*)"[^>]*?)>(.*?)</a>', re.S)
_FT_PORTAL_LOC = re.compile(r'data-portal-location="([^"]*)"')
_FT_INNER_TITLE = re.compile(r'class="job-title"[^>]*>([^<]*)<')
_FT_INNER_DESC = re.compile(r'class="job-desc[^"]*"[^>]*>(.*?)</span>', re.S)
_FT_JD = re.compile(r'<div class="job-details-content content">(.*?)\n\s*</div>\s*\n\s*</div>', re.S)


def freshteam(company: str, token: str) -> list[dict]:
    """Freshworks' Freshteam ATS — `{tenant}.freshteam.com/jobs`, server-rendered.

    No API needed and none available: `/api/job_postings` 401s (that is the
    authenticated admin API), but the public board ships every job in the HTML.

    Freshteam career portals are TEMPLATED, and tenants pick different ones — do
    not parse one board's markup and assume the rest match. Two shapes seen in
    nine boards:
      A (Haptik/Locus)  <a class="heading" data-portal-location="…"> wrapping a
                        <div class="job-title">
      B (Ninjacart)     <li class="heading"> holding sibling anchors
                        <a class="job-title">…</a> and <a class="location-info">
    A regex written for A returns ZERO jobs on B while the board plainly has 23,
    which is the silent-empty failure mode, not a visible error. What both
    templates do share is the `/jobs/{id}/{slug}` href, so jobs are keyed off
    that and each anchor contributes whichever field it happens to carry.
    """
    body = _get_text(f"https://{token}.freshteam.com/jobs")
    found: dict[str, dict] = {}
    for attrs, jid, slug, inner in _FT_ANCHOR.findall(body):
        j = found.setdefault(jid, {
            "id": jid,
            "company": company,
            # slug is the fallback if no template we know supplies a title
            "title": slug.replace("-", " ").title(),
            "location": "",
            "url": f"https://{token}.freshteam.com/jobs/{jid}/{slug}",
            "department": "",
            "_detail": f"https://{token}.freshteam.com/jobs/{jid}/{slug}",
            "source": "freshteam",
        })
        if 'class="job-title"' in attrs:                       # B: anchor IS the title
            j["title"] = html.unescape(_TAG.sub("", inner)).strip()
        else:                                                  # A/C: title nested inside
            m = _FT_INNER_TITLE.search(inner)
            if m and m.group(1).strip():
                j["title"] = html.unescape(m.group(1)).strip()
        if 'class="location-info"' in attrs:                   # B
            # inner reads "Bangalore, India <br/> Full Time" — keep the place
            j["location"] = html.unescape(
                _TAG.sub("\n", inner).strip().split("\n")[0]).strip()
        loc = _FT_PORTAL_LOC.search(attrs)                     # A
        if loc and loc.group(1).strip():
            j["location"] = html.unescape(loc.group(1)).strip()
        if not j["location"]:                                  # C
            # C puts "Bengaluru | Full Time" in the job-desc span, but in B that
            # same class holds a multi-line company blurb — so only trust it when
            # it is short enough to be a location line.
            m = _FT_INNER_DESC.search(inner)
            if m:
                txt = " ".join(html.unescape(_TAG.sub(" ", m.group(1))).split())
                if txt and len(txt) < 80:
                    j["location"] = txt.split("|")[0].strip()
    return list(found.values())


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday,
            "smartrecruiters": smartrecruiters, "eightfold": eightfold,
            "amazon": amazon, "uber": uber, "oracle": oracle,
            "keka": keka, "ainterviews": ainterviews, "mynexthire": mynexthire,
            "workable": workable, "adzuna": adzuna, "rippling": rippling,
            "inbox": inbox, "pcsx": pcsx, "google": google,
            "darwinbox": darwinbox, "freshteam": freshteam, "atlassian": atlassian,
            "icims": icims}


def fetch(company: str, spec: dict) -> list[dict]:
    ats = spec["ats"]
    if ats not in ADAPTERS:
        raise ValueError(f"unknown ATS '{ats}' for {company}")
    if ats == "workday":
        return workday(company, spec["host"], spec["tenant"], spec["site"])
    if ats == "oracle":
        return oracle(company, spec["host"], spec["site"])
    if ats == "pcsx":
        # host and domain differ (careers.qualcomm.com vs qualcomm.com)
        return pcsx(company, spec["host"], spec["domain"])
    return ADAPTERS[ats](company, spec.get("token", ""))
