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

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
BATCH = 6
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


def _prompt(candidate: str, prefs: dict, jobs: list[dict]) -> str:
    lines = []
    for k, j in enumerate(jobs):
        lines.append(
            f"[{k}] {j['title']} @ {j['company']} — {j.get('location','')}\n"
            f"{(j.get('description') or '')[:1400]}"
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
candidate (~1 yr experience). Be strict: a role needing 4+ yrs, or a Senior/
Staff/Lead/Principal title, or a stack they don't have (Go/Rust/C++/.NET/mobile-
native/SAP/Salesforce) should score LOW even if the title contains "engineer".
Reward strong full-stack (Angular/React/TS/Node/Python/FastAPI) and applied-AI/
LLM roles at their level. Pick resume "ai" for AI/ML/LLM-heavy roles, else
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
