"""Gemini fit-gate — the quality layer. Scores each candidate role 0-100
against the profile, picks which resume to send, and gives a one-line why.

Batches jobs per request to stay well under the free-tier rate limit.
If GEMINI_API_KEY is unset or the API errors, returns {} and the poller
falls back to heuristic ranking — so the pipeline never hard-depends on it.
"""
from __future__ import annotations
import os
import json
import time
import requests

import fit   # for the experience parser; fit imports nothing of ours

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
BATCH = 6
# How much of each JD goes into the prompt. This is the cut that actually
# matters — `ats.DESC_CAP` (3000) is the *storage* cap, and ats._plain only
# bothers to rescue the experience line when it trips that one. A JD between
# 1400 and 3000 chars therefore reached here whole and got beheaded right here
# instead, silently: measured across 382 live descriptions, 87% are longer than
# this slice and 25 of them state their experience requirement past it. The
# requirement sits a median 78% of the way into a JD, so a head-cut is precisely
# the wrong cut. Rescue it the same way _plain does.
DESC_SLICE = 1400
_LEAD = "Experience required:"
enabled = bool(API_KEY)

_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "i": {"type": "integer"},
            "score": {"type": "integer"},
            "resume": {"type": "string", "enum": ["software", "ai"]},
            "reason": {"type": "string"},
        },
        "required": ["i", "score", "resume", "reason"],
    },
}


def _clip(desc: str) -> str:
    """Trim a JD to the prompt slice, leading with the experience requirement.

    The lead is prepended whether or not the JD gets truncated. It used to be a
    rescue for the head-cut only, which left the judge to find the bar itself in
    any JD short enough to survive intact — and it does not reliably do that: the
    requirement is one line in six paragraphs of team blurb, and roles asking
    3-6 years were coming back at 85-90. Stating it up front costs ~40 chars and
    turns the single most decisive fact in the document into the first thing read.
    """
    # `ats._plain` already prepends this line when a JD trips DESC_CAP, so
    # prepending unconditionally printed it twice on every long JD.
    lead = "" if desc.startswith(_LEAD) else fit.experience_line(desc)
    if len(desc) <= DESC_SLICE:
        return f"{lead} {desc}".strip() if lead else desc
    if not lead:
        return desc[:DESC_SLICE]
    return f"{lead} {desc[:DESC_SLICE - len(lead) - 1]}"


def _prompt(candidate: str, prefs: dict, jobs: list[dict]) -> str:
    lines = []
    for k, j in enumerate(jobs):
        lines.append(
            f"[{k}] {j['title']} @ {j['company']} — {j.get('location','')}\n"
            f"{_clip(j.get('description') or '')}"
        )
    listing = "\n\n---\n\n".join(lines)
    return f"""You are a sharp technical recruiter screening roles for ONE candidate.

CANDIDATE:
{candidate}

WHAT THEY WANT:
- Target roles: {prefs['target_roles']}
- Seniority: {prefs['seniority']}
- Location: {prefs['location']}
- Minimum CTC: ~{prefs['min_ctc_lpa']} LPA (India). {prefs['bonus']}
- Resumes available: {prefs['two_resumes']}

Score each role 0-100 for GENUINE fit + realistic conversion chance for THIS
candidate (~1 yr 3 mo experience). Be strict: a role needing 3+ yrs, or a Senior/
Staff/Lead/Principal title, or a stack they don't have (Go/Rust/C++/.NET/mobile-
native/SAP/Salesforce/RTL/firmware) should score LOW even if the title contains
"engineer".

HOW THE EXPERIENCE LINE FEEDS THE SCORE — read this carefully, it is the axis
that gets mis-weighted in both directions. Anything with a stated floor of 3+
years is dropped before it reaches you. SCORE AGAINST THE FLOOR — the FIRST
number in "Experience required: N years" or "N-M years" — never the upper end:

  * Floor of 0 or 1 ("1+ years", "0-2 years", "1-2", "1-3", "1-5") → a MATCH,
    no penalty. The upper end is irrelevant and must not be read as the bar:
    "1-3 years" is a one-year role that would also take someone more senior,
    NOT a three-year role. The candidate is squarely eligible for these.

  * Floor of exactly 2 ("2+ years", "minimum 2 years", "2-5 years") → a REAL
    negative, not a token one. At ~1 yr the candidate is genuinely under the stated
    basic qualification and their realistic chance of being accepted drops. Mark
    it down properly and say so in the reason.

  * A strong stack/role match still rescues it. A floor of 2 is a reason to
    RANK a role lower, not to score it out on its own — an excellent match at
    2 years is still worth applying to, and should still clear the bar.

(Both failures are measured on real sweeps of the same 1274 matches. Treating 2
as disqualifying cost 30 good roles in one sweep, two of them already applied
to. Treating 2 as free lost the intended ordering. The floor/upper-end
distinction is what makes the difference: penalise a floor of 2, never penalise
a "1-3" for containing a 3, and never penalise a "0-2" for containing a 2.)
A listing marked [PARTIAL LISTING] is a teaser or a JD we failed to fetch, not the
real thing — its requirements are UNKNOWN, so score it no higher than 65 and say
"unverified JD" in the reason. (65, not 70: 70 is the A-tier referral bar, so a
ceiling of 70 put every unread role on it. The ceiling must sit strictly below.)
Absence of a stated requirement is not evidence the role is junior.
If the text opens with "Experience required: N years" (or "N-M years"), treat
that as authoritative and score against N, the floor.
Reward strong full-stack (Angular/React/TS/Node/Python/FastAPI) and applied-AI/
LLM roles at their level. Forward-deployed engineering (FDE / deployed engineer /
implementation / delivery-side engineering that BUILDS) is wanted EXACTLY as much
as core SWE and AI-generalist work — score it on the same scale, do not discount
it for being customer-facing. This is deliberate: it was being marked down as
off-target. The line is whether the role writes and ships code — a pure
pre-sales/demo/"show the product" role is NOT wanted and should still score low.
Pick resume "ai" for AI/ML/LLM-heavy roles, else
"software". Reason = one short line (why it fits or the main risk).

Return ONLY a JSON array, one object per role, fields: i (the [index]),
score, resume, reason.

ROLES:
{listing}
"""


def _call(candidate: str, prefs: dict, jobs: list[dict]) -> dict[int, dict]:
    body = {
        "contents": [{"parts": [{"text": _prompt(candidate, prefs, jobs)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
            "temperature": 0.2,
        },
    }
    r = requests.post(URL, params={"key": API_KEY}, json=body, timeout=60)
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    arr = json.loads(txt)
    out = {}
    for item in arr:
        k = item.get("i")
        if isinstance(k, int) and 0 <= k < len(jobs):
            out[id(jobs[k])] = {
                "score": max(0, min(100, int(item.get("score", 0)))),
                "resume": item.get("resume", "software"),
                "reason": (item.get("reason", "") or "").strip()[:160],
            }
    return out


def rate(candidate: str, prefs: dict, jobs: list[dict]) -> dict[int, dict]:
    """Return {id(job): {score, resume, reason}}. Empty if disabled/failed."""
    if not enabled or not jobs:
        return {}
    results: dict[int, dict] = {}
    for start in range(0, len(jobs), BATCH):
        chunk = jobs[start:start + BATCH]
        for attempt in range(2):
            try:
                results.update(_call(candidate, prefs, chunk))
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(5)  # brief backoff, then retry once
                else:
                    print(f"  [gemini] batch {start}: {e}")
        time.sleep(4)  # stay under free-tier RPM
    return results
