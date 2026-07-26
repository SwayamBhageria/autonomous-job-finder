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
import datetime as dt
import html
import time
import urllib.parse
import concurrent.futures as cf
import requests

TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (job-finder; +https://github.com/)"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _get(url: str) -> dict | list:
    # one retry on transient network hiccups (read timeouts, resets)
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise
            time.sleep(2)


def _plain(text: str) -> str:
    """Strip HTML → plain text, capped for the fit gate."""
    if not text:
        return ""
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub("\n\n", text)
    return " ".join(text.split())[:3000]


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
            "description": _plain(j.get("descriptionPlain", "") or j.get("description", "")),
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
WORKDAY_MAX_PAGES = 3        # per query → up to 60 top-ranked results each


def workday(company: str, host: str, tenant: str, site: str) -> list[dict]:
    """Workday cxs API. Runs a handful of targeted India+role searches (cheap)
    instead of paging the whole board, dedupes across them by job path. The JD
    is fetched lazily per candidate via enrich_description()."""
    base = f"https://{host}/wday/cxs/{tenant}/{site}"

    def page(query: str, offset: int) -> list:
        try:
            r = requests.post(f"{base}/jobs", headers=HEADERS, timeout=TIMEOUT,
                              json={"limit": 20, "offset": offset, "searchText": query, "appliedFacets": {}})
            r.raise_for_status()
            return r.json().get("jobPostings", [])
        except Exception:
            return []

    # fire every (query, page) request in parallel — bounded and fast
    tasks = [(q, pg * 20) for q in WORKDAY_QUERIES for pg in range(WORKDAY_MAX_PAGES)]
    seen: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for posts in ex.map(lambda t: page(*t), tasks):
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


AMAZON_QUERIES = [
    "software development engineer",
    "front end engineer",
    "full stack",
    "machine learning engineer",
    "applied scientist",
]


def amazon(company: str, token: str) -> list[dict]:
    """amazon.jobs public search JSON, India-scoped. Recent-first, one page
    (100) per query — new postings always surface. JD is in the list payload."""
    seen: dict[str, dict] = {}

    def page(query: str) -> list:
        try:
            d = _get("https://www.amazon.jobs/en/search.json"
                     f"?base_query={urllib.parse.quote(query)}"
                     "&country=IND&result_limit=100&offset=0&sort=recent")
            return d.get("jobs", [])
        except Exception:
            return []

    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        for jobs in ex.map(page, AMAZON_QUERIES):
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
    return list(seen.values())


def uber(company: str, token: str) -> list[dict]:
    """Uber careers internal API (public, needs the dummy csrf header).
    India-filtered at the API level; JD is in the list payload."""
    out, page_n = [], 0
    while page_n < 5:
        r = requests.post(
            "https://www.uber.com/api/loadSearchJobsResults?localeCode=en",
            headers={**HEADERS, "Content-Type": "application/json", "x-csrf-token": "x"},
            timeout=TIMEOUT,
            json={"params": {"location": [{"country": "IND", "region": "", "city": ""}],
                             "limit": 100, "page": page_n}})
        r.raise_for_status()
        results = (r.json().get("data") or {}).get("results") or []
        for j in results:
            loc = j.get("location") or {}
            loc_s = ", ".join(filter(None, (loc.get("city"), loc.get("countryName"))))
            out.append({
                "id": str(j.get("id", "")),
                "company": company,
                "title": j.get("title", ""),
                "location": loc_s,
                "url": f"https://www.uber.com/global/en/careers/list/{j.get('id','')}/",
                "department": j.get("department", "") or "",
                "description": _plain(j.get("description", "")),
                "source": "uber",
            })
        if len(results) < 100:
            break
        page_n += 1
    return out


ORACLE_QUERIES = [
    "software engineer India",
    "full stack India",
    "machine learning India",
]


def oracle(company: str, host: str, site: str) -> list[dict]:
    """Oracle Cloud Recruiting (HCM) public CE API — used by JPMorgan Chase etc.
    Targeted keyword searches like the Workday adapter; JD text ships in the
    list payload (short description + responsibilities + qualifications)."""
    # expand= is required — without it the requisitionList child is omitted
    base = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},sortBy=POSTING_DATES_DESC")

    def page(query: str, offset: int) -> list:
        try:
            d = _get(f"{base},keyword={urllib.parse.quote_plus(query)},limit=100,offset={offset}")
            items = d.get("items", [])
            return items[0].get("requisitionList", []) if items else []
        except Exception:
            return []

    tasks = [(q, pg * 100) for q in ORACLE_QUERIES for pg in range(2)]
    seen: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for reqs in ex.map(lambda t: page(*t), tasks):
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
            desc = f"Experience required: {exp} years. {desc}"[:3000]
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
            desc = f"Level: {lvl}. {desc}"[:3000]
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
            desc = f"Experience required: {lo:g}-{hi:g} years. {desc}"[:3000]
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
            desc = f"Experience level: {exp}. {desc}"[:3000]
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
                "description": _plain(j.get("description", "")),
                "source": "adzuna",
            }
    return list(seen.values())


def enrich_description(job: dict) -> None:
    """For sources whose list omits the JD (Workday/SmartRecruiters/Eightfold),
    fetch it on demand. No-op if already present or not enrichable. Never raises."""
    if job.get("description") or not job.get("_detail"):
        return
    try:
        d = _get(job["_detail"])
        if not isinstance(d, dict):
            return
        if "jobPostingInfo" in d:                     # workday
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
        else:                                         # eightfold
            job["description"] = _plain(d.get("job_description", ""))
    except Exception:
        pass


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday,
            "smartrecruiters": smartrecruiters, "eightfold": eightfold,
            "amazon": amazon, "uber": uber, "oracle": oracle,
            "keka": keka, "ainterviews": ainterviews, "mynexthire": mynexthire,
            "workable": workable, "adzuna": adzuna}


def fetch(company: str, spec: dict) -> list[dict]:
    ats = spec["ats"]
    if ats not in ADAPTERS:
        raise ValueError(f"unknown ATS '{ats}' for {company}")
    if ats == "workday":
        return workday(company, spec["host"], spec["tenant"], spec["site"])
    if ats == "oracle":
        return oracle(company, spec["host"], spec["site"])
    return ADAPTERS[ats](company, spec.get("token", ""))
