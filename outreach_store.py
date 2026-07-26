"""Standing outreach tracker — `state/outreach.json`.

The A-tier alert is a one-shot notification; this is the durable list behind the
outreach cockpit. Each A-tier finalist is recorded once (keyed by role identity,
so reposts don't duplicate) with everything the cockpit needs, and pruned after
a few weeks — outreach has a shelf life, a role open a month you've done or missed.

Progress (who you've messaged, follow-ups) lives client-side in the page's
localStorage, so this file only needs to carry the roles + their draft content.
"""
from __future__ import annotations
import json
import time
import pathlib

FIELDS = ("title", "company", "location", "url", "score", "resume", "reason",
          "referrers", "alumni", "excoll", "email_pattern", "email_researched",
          "dm", "email_subject", "email_body")


def load(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _entry(job: dict) -> dict:
    # jobs carry these as _-prefixed attrs; store them flat and JSON-safe.
    # B-tier jobs never went through contacts.enrich/outreach.drafts, so their
    # referral/dm fields are simply empty and the cockpit renders apply-only.
    return {
        "tier": job.get("_tier", "A"),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "score": job.get("_score", 0),
        "resume": job.get("_resume", "software"),
        "reason": job.get("_reason", ""),
        "referrers": job.get("_referrers", ""),
        "alumni": job.get("_alumni", ""),
        "excoll": job.get("_excoll", ""),
        "email_pattern": job.get("_email_pattern", ""),
        "email_researched": bool(job.get("_email_researched", False)),
        "dm": job.get("_dm", ""),
        "email_subject": job.get("_email_subject", ""),
        "email_body": job.get("_email_body", ""),
    }


def update(path: pathlib.Path, jobs: list[dict], keyfn, forget_days: int = 21) -> dict:
    """Add any new A-tier roles (keyed by keyfn(job)), prune stale ones, save.
    Returns the current tracker dict."""
    tracker = load(path)
    now = time.time()
    for j in jobs:
        k = keyfn(j)
        if k in tracker:
            # refresh the draft content but keep the original first_seen so the
            # follow-up clock and ordering don't reset on a repost.
            first = tracker[k].get("first_seen", now)
            tracker[k] = {**_entry(j), "first_seen": first}
        else:
            tracker[k] = {**_entry(j), "first_seen": now}
    cutoff = now - forget_days * 86400
    tracker = {k: v for k, v in tracker.items() if v.get("first_seen", 0) >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tracker, indent=1, sort_keys=True))
    return tracker


def as_rows(tracker: dict) -> list[dict]:
    """Tracker dict → list of cockpit rows, key inlined. A-tier first (they need
    the outreach work), then newest-first within each tier."""
    rows = [{**v, "key": k} for k, v in tracker.items()]
    rows.sort(key=lambda r: (r.get("tier", "A") != "A", -r.get("first_seen", 0)))
    return rows
