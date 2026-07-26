"""Heuristic pre-scorer — cheap, no API. Runs before the Gemini gate to
kill obviously-wrong roles (too senior, wrong stack) and rank the rest.

score(job, profile) -> (heuristic_score 0-100, meta dict)
"""
from __future__ import annotations
import re

# "5+ years", "3-5 years", "minimum of 4 years", "4 yrs experience"
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)", re.I)
_SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|iii|iv|manager|architect)\b", re.I)
_JUNIOR_TITLE = re.compile(r"\b(junior|jr\.?|associate|sde ?1|sde ?i|swe ?1|grad(uate)?|entry|new ?grad|early career|intern)\b", re.I)


def _compile(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(patterns), re.I)


def min_years_required(text: str) -> int | None:
    """Smallest year-count mentioned in the JD (usually the requirement floor)."""
    nums = [int(m.group(1)) for m in _YEARS.finditer(text)]
    nums = [n for n in nums if 0 <= n <= 20]
    return min(nums) if nums else None


def score(job: dict, profile: dict) -> tuple[int, dict]:
    title = job.get("title", "")
    desc = job.get("description", "") or ""
    blob = f"{title}\n{desc}"

    strong = _compile(profile["strong_stack"])
    weak = _compile(profile["weak_stack"])

    strong_hits = len(set(m.group(0).lower() for m in strong.finditer(blob)))
    weak_hits = len(set(m.group(0).lower() for m in weak.finditer(blob)))

    s = 50
    s += min(strong_hits, 8) * 5          # up to +40 for stack overlap
    s -= min(weak_hits, 6) * 7            # up to -42 if wrong-stack heavy

    # seniority from JD years + title
    yrs = min_years_required(desc)
    max_yrs = profile["preferences"].get("max_years_required", 3)
    seniority_ok = True
    if yrs is not None and yrs > max_yrs:
        s -= (yrs - max_yrs) * 8
        seniority_ok = yrs <= max_yrs + 1
    if _JUNIOR_TITLE.search(title):
        s += 12
    elif _SENIOR_TITLE.search(title):
        s -= 15
        seniority_ok = False

    # finance / fintech bonus
    if re.search(r"fintech|payments?|finance|banking|trading|lending", blob, re.I):
        s += 6

    s = max(0, min(100, s))
    return s, {
        "strong_hits": strong_hits,
        "weak_hits": weak_hits,
        "min_years": yrs,
        "seniority_ok": seniority_ok,
    }
