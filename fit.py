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

# A year-figure with its low end captured: "5+ years", "3-5 yrs", "3 to 5 years".
_YEAR_MENTION = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:[-–]|to)\s*\d{1,2}\s*)?(?:\+\s*)?(?:years?|yrs?)\b", re.I)
# Only figures sitting in a requirement sentence count. Without this the parser
# happily reads eBay's "committed to diversity since its founding more than 30
# years ago" as a qualification.
_REQ_CTX = re.compile(
    r"experience|exp\b|background|hands[-\s]?on|professional|industry|"
    r"proficien|expertise|worked|working|track record|minimum|at least|required", re.I)
_NOISE_CTX = re.compile(
    r"\bago\b|founded|anniversary|\bhistory\b|of age|years old|"
    r"(?:past|last|next|recent|coming|every|over the)\s+\w{0,6}\s*$", re.I)


def _requirement_years(text: str) -> list[int]:
    """Every year-figure that reads as an experience requirement, low end of ranges."""
    out = []
    for m in _YEAR_MENTION.finditer(text):
        n = int(m.group(1))
        if not 0 <= n <= 20:
            continue
        # tight window before the number catches "in the past 3 years"; the wider
        # one decides whether this sentence is about experience at all.
        if _NOISE_CTX.search(text[max(0, m.start() - 32):m.start()]) or \
           _NOISE_CTX.search(text[m.start():m.end() + 18]):
            continue
        if _REQ_CTX.search(text[max(0, m.start() - 110):m.end() + 110]):
            out.append(n)
    return out


def _compile(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(patterns), re.I)


def min_years_required(text: str) -> int | None:
    """The experience floor the JD actually enforces, or None if it states none.

    Was `min()` over every number in the document, which is wrong twice over: it
    counted company boilerplate ("30 years ago"), and where a JD sets more than
    one bar it took the softest. eBay's "MTS 1" asks for "6+ years ... with 2+
    years as a lead developer" — min() returned 2, so a 6-year role sailed past a
    4-year cutoff. The binding constraint is the *highest* stated requirement, so
    take max() over figures that appear in a requirement context.
    """
    nums = _requirement_years(text)
    # No figure in a requirement context means the JD states no bar we can read.
    # Returning None (rather than falling back to "smallest number anywhere")
    # matters: on "founded 30 years ago ... 12 years of history" that fallback
    # returns 12 and hard-drops a role that never stated a requirement at all.
    # Erring toward None hands the call to Gemini, which is the recoverable
    # direction — a wrong drop is silent, a wrong keep is visible in the cockpit.
    return max(nums) if nums else None


def experience_line(text: str) -> str:
    """A short 'Experience required: N years.' prefix, or '' if the JD states none.

    Used to survive description truncation: the requirement usually sits at the
    end of a JD (median 78% in), so capping the head throws it away.
    """
    nums = _requirement_years(text)
    return f"Experience required: {max(nums)} years." if nums else ""


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
