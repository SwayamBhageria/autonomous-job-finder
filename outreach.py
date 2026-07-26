"""Draft a short LinkedIn DM + cold email per finalist role, via Gemini.

Runs only on the handful of roles that clear the fit gate, so it's a call or
two per run. Falls back to a simple template if Gemini is unavailable.
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

_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "i": {"type": "integer"},
            "dm": {"type": "string"},
            "email_subject": {"type": "string"},
            "email_body": {"type": "string"},
        },
        "required": ["i", "dm", "email_subject", "email_body"],
    },
}


def _template(job: dict, me: dict) -> dict:
    return {
        "dm": (f"Hi — I'm {me['name']}, a software engineer (~1 yr, full-stack + AI). "
               f"I'm applying for {job['title']} at {job['company']} and would love a quick "
               f"referral or a pointer to the hiring team. Happy to share my resume!"),
        "email_subject": f"Referral request — {job['title']} at {job['company']}",
        "email_body": (f"Hi,\n\nI came across the {job['title']} role at {job['company']} and it's a "
                       f"strong fit for my background (full-stack + applied AI, ~1 yr). Would you be open "
                       f"to referring me or connecting me with the hiring team? Resume attached.\n\n"
                       f"Thanks,\n{me['name']}\n{me.get('linkedin','')}"),
    }


def _prompt(candidate: str, me: dict, jobs: list[dict]) -> str:
    listing = "\n".join(f"[{k}] {j['title']} @ {j['company']} — {j.get('location','')}"
                        for k, j in enumerate(jobs))
    return f"""Write warm, concise referral outreach for this candidate, per role.

CANDIDATE: {me['name']}, {candidate}
LinkedIn: {me.get('linkedin','')}

For each role write:
- dm: a LinkedIn message to an employee/recruiter asking for a referral — max 320
  chars, first person as {me['name']}, friendly, no fluff. Make it feel written
  for THIS role: name one concrete, relevant credential from the candidate (a
  specific project or the exact stack the title implies — e.g. Angular for a
  frontend role, FastAPI/Gemini/FAISS for an AI role, payments for a fintech).
  Generic "I'm a full-stack engineer" messages get ignored; specificity is the
  whole point.
- email_subject: short, mentions the role.
- email_body: 4-6 lines, opens with the specific match, asks for a referral /
  intro to the hiring team, signs off as {me['name']} with the LinkedIn link.
  Plain text.

Return ONLY a JSON array, one object per role: i, dm, email_subject, email_body.

ROLES:
{listing}
"""


def drafts(candidate: str, me: dict, jobs: list[dict]) -> None:
    """Attach _dm / _email_subject / _email_body to each job (mutates)."""
    if not jobs:
        return
    got: dict[int, dict] = {}
    if API_KEY:
        for start in range(0, len(jobs), BATCH):
            chunk = jobs[start:start + BATCH]
            body = {
                "contents": [{"parts": [{"text": _prompt(candidate, me, chunk)}]}],
                "generationConfig": {"responseMimeType": "application/json",
                                     "responseSchema": _SCHEMA, "temperature": 0.5},
            }
            try:
                r = requests.post(URL, params={"key": API_KEY}, json=body, timeout=60)
                r.raise_for_status()
                arr = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                for item in arr:
                    k = item.get("i")
                    if isinstance(k, int) and 0 <= k < len(chunk):
                        got[id(chunk[k])] = item
            except Exception as e:
                print(f"  [outreach] batch {start}: {e}")
            time.sleep(4)
    for j in jobs:
        d = got.get(id(j)) or _template(j, me)
        j["_dm"] = (d.get("dm") or "").strip()
        j["_email_subject"] = (d.get("email_subject") or "").strip()
        j["_email_body"] = (d.get("email_body") or "").strip()
