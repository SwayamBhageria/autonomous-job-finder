"""Heuristic pre-scorer — cheap, no API. Runs before the Gemini gate to
kill obviously-wrong roles (too senior, wrong stack) and rank the rest.

score(job, profile) -> (heuristic_score 0-100, meta dict)
"""
from __future__ import annotations
import re

_SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|iii|iv|manager|architect)\b", re.I)
_JUNIOR_TITLE = re.compile(r"\b(junior|jr\.?|associate|sde ?1|sde ?i|swe ?1|grad(uate)?|entry|new ?grad|early career|intern)\b", re.I)

# A year-figure with both ends captured: "5+ years", "3-5 yrs", "3 to 5 years".
# Fractions are matched and floored — American Express states "0.5+ years of
# software development experience", the most junior bar in the whole corpus, and
# dropping it left that role with no requirement line at all. Flooring is the
# right reading in both directions: 0.5 → 0 and 2.5 → 2 are the bars a candidate
# actually has to clear.
_YEAR_MENTION = re.compile(
    r"(?<![\d.])(\d{1,2})(?:\.\d+)?\s*(?:\+|plus)?\s*"
    r"(?:(?:[-–—]|to)\s*(\d{1,2})(?:\.\d+)?\s*)?(?:\+\s*)?(?:years?|yrs?)\b", re.I)
# A figure only counts if the sentence reads as a requirement. Three ways to
# qualify, because JDs state the bar in three different shapes:
#
# (a) _REQ_CTX — an experience word nearby ("2+ years of experience in X").
# (b) _WORK_AFTER — the figure is followed straight into the work itself
#     ("3+ years IN conversational AI", "3-6 years BUILDING software",
#     "4–8 years IN data infrastructure"). This clause is the fix for the bug
#     that made the whole gate ornamental. Plenty of JDs state the bar without
#     ever writing the word "experience", and (a) alone threw every one of them
#     away: over 96 live A-tier JDs it read a requirement out of 53, where this
#     version reads 60. The six roles it silently un-gated are the tell — all
#     six were roles later rejected by hand in the cockpit, so the gate
#     was leaving its own job to the reviewer.
# (c) _QUAL_BEFORE — the figure terminates a qualification line, which Indian
#     JDs write bare: "B.Tech / M.Tech • 6-8 Yrs", "BE/BTech 3 - 6 years",
#     "Exp: 2-3 years".
_REQ_CTX = re.compile(
    r"experience|exp\b|background|hands[-\s]?on|professional|industry|"
    r"proficien|expertise|worked|working|track record|minimum|at least|required", re.I)
# "of" is deliberately absent. "N years OF <anything>" is far too broad — it
# reads "We offer 2 years of paid parental leave" as a two-year bar — and it
# buys nothing, because the requirement phrasings that use it ("2 years of
# experience", "3 years of hands-on work") all carry a _REQ_CTX word anyway.
_WORK_AFTER = re.compile(
    r"^\s*(?:in|with|as|within|across|building|develop\w*|design\w*|work\w*|"
    r"writ\w*|creat\w*|program\w*|deliver\w*|lead\w*|support\w*|architect\w*)\b", re.I)
# Perks, equity and contract terms are measured in years too, and none of them
# is an experience bar.
_PERK_AFTER = re.compile(
    r"^\s*(?:of\s+)?(?:paid\s+)?(?:parental|maternity|paternity|annual|sick|"
    r"unlimited|vacation|leave|holiday|vesting|cliff|insurance|warrant|"
    r"subscription|notice period|bond|lock[- ]?in)\b", re.I)
_QUAL_BEFORE = re.compile(
    r"(?:b\.?\s?e\b|b\.?\s?tech|m\.?\s?tech|m\.?\s?e\b|b\.?sc|m\.?sc|mca|degree|"
    r"exp\b|experience|qualification|eligib|requirement|must have|looking for)"
    r"[^.]{0,40}$", re.I)
# Company boilerplate — "upon more than 30 years of computational software
# expertise", "We're 40 years, 20+ countries". These read exactly like a
# requirement to (a) and (b), so they are rejected before either is consulted.
_NOISE_CTX = re.compile(
    r"\bago\b|founded|anniversary|\bhistory\b|of age|years old|"
    r"(?:past|last|next|recent|coming|every|over the)\s+\w{0,6}\s*$", re.I)
# Deliberately NOT including a bare "for": Honeywell writes "Software Engg.
# position for 1+ years of experience", which is the requirement itself. The
# brag shapes all carry their own quantifier word ("for OVER 30 years").
_BRAG_BEFORE = re.compile(
    r"(?:more than|over|nearly|almost|upon|we(?:'re| are| have been)|"
    r"celebrat\w*|serving|spanning)\s+$", re.I)
# An application form's "total years of experience" dropdown lists every band
# there is. Haptik's board ships one inside the JD text, and max() over it
# reads a 1-year role as demanding 15. A picker is recognisable by shape: many
# year-figures crammed into a short span, which no prose requirement does.
_PICKER_SPAN = 220
_PICKER_MIN = 4


def _mentions(text: str) -> list[tuple[int, int, int, int]]:
    """Raw (low, high, start, end) for every year-figure in the text."""
    out = []
    for m in _YEAR_MENTION.finditer(text):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        out.append((lo, max(lo, hi), m.start(), m.end()))
    return out


def _picker_zones(spots: list) -> list[tuple[int, int]]:
    """Character ranges that look like a form's year-band dropdown."""
    zones = []
    for i, s in enumerate(spots):
        run = [t for t in spots[i:] if t[2] - s[2] <= _PICKER_SPAN]
        if len(run) >= _PICKER_MIN:
            zones.append((s[2], run[-1][3]))
    return zones


def _requirement_years(text: str) -> list[tuple[int, int]]:
    """Every year-figure that reads as an experience requirement, as (low, high)."""
    spots = _mentions(text)
    zones = _picker_zones(spots)
    out = []
    for lo, hi, start, end in spots:
        # A double-digit "requirement" is nearly always the company's age; the
        # real bars in this corpus top out at 10.
        if not 0 <= lo <= 15:
            continue
        if any(a <= start < b for a, b in zones):
            continue
        before = text[max(0, start - 32):start]
        after = text[end:end + 18]
        if (_NOISE_CTX.search(before) or _NOISE_CTX.search(after)
                or _BRAG_BEFORE.search(before) or _PERK_AFTER.match(text[end:end + 40])):
            continue
        if (_REQ_CTX.search(text[max(0, start - 110):end + 110])
                or _WORK_AFTER.match(text[end:end + 40])
                or _QUAL_BEFORE.search(text[max(0, start - 60):start])):
            out.append((lo, hi))
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

    A range is read at its LOW end — "2-5 years" bars nobody with 2. That is
    also what hand triage does: one applied to Meesho's "2-5 years" FDE
    role and to JPMorgan's "2+ years" one, so the floor is the number that
    decides, not the ceiling.
    """
    spans = _requirement_years(text)
    # No figure in a requirement context means the JD states no bar we can read.
    # Returning None (rather than falling back to "smallest number anywhere")
    # matters: on "founded 30 years ago ... 12 years of history" that fallback
    # returns 12 and hard-drops a role that never stated a requirement at all.
    # Erring toward None hands the call to Gemini, which is the recoverable
    # direction — a wrong drop is silent, a wrong keep is visible in the cockpit.
    return max(lo for lo, _ in spans) if spans else None


def experience_range(text: str) -> tuple[int, int] | None:
    """The binding requirement as (low, high), or None if the JD states none.

    The cockpit shows this verbatim ("asks 2–5 yrs"), which is strictly more
    information than the floor alone: "1-3" and "1-8" gate identically but are
    not the same role, and that difference is the whole triage decision.
    """
    spans = _requirement_years(text)
    return max(spans, key=lambda s: s[0]) if spans else None


def experience_line(text: str) -> str:
    """A short 'Experience required: N years.' prefix, or '' if the JD states none.

    Used to survive description truncation: the requirement usually sits at the
    end of a JD (median 78% in), so capping the head throws it away.
    """
    span = experience_range(text)
    if not span:
        return ""
    lo, hi = span
    if hi > lo:
        return f"Experience required: {lo}-{hi} years."
    return f"Experience required: {lo} year{'' if lo == 1 else 's'}."


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
