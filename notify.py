"""Slack notification via an Incoming Webhook.

Set SLACK_WEBHOOK_URL in the environment (a repo secret in CI, or your shell
locally). With no webhook set, messages print to stdout so you can test dry.
"""
from __future__ import annotations
import os
import json
import requests

WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
MAX_BLOCKS = 45  # Slack hard limit is 50 per message.


def _job_block(job: dict) -> list[dict]:
    score = job.get("_score", 0)
    resume = "🤖 AI resume" if job.get("_resume") == "ai" else "💻 Software resume"
    ref = "   🤝 *Referral available*" if job.get("_referral") else ""
    loc = (job.get("location") or "—")[:60]
    reason = job.get("_reason", "")
    # Title + company first (what you scan for), verdict line under it.
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f"*<{job['url']}|{job['title']}>*\n"
                         f"*{job['company']}*  ·  📍 {loc}\n"
                         f"*{score}/100*   {resume}{ref}\n"
                         f"💡 _{reason}_"),
            },
        },
    ]
    # Referral outreach: who to ping + how, right under the role.
    if job.get("_referrers"):
        parts = [f"🤝 <{job['_referrers']}|Referrers>", f"🎓 <{job['_alumni']}|Alumni>"]
        if job.get("_excoll"):
            parts.append(f"🏢 <{job['_excoll']}|Ex-colleagues>")
        tag = "researched pattern" if job.get("_email_researched") else "unverified guess"
        parts.append(f"📧 `{job.get('_email_pattern','')}` ({tag})")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "   ·   ".join(parts)}]})
    if job.get("_dm"):
        dm = job["_dm"].replace("\n", " ")
        if len(dm) > 220:
            dm = dm[:220].rsplit(" ", 1)[0] + "…"
        text = f">✍️ {dm}"
        if job.get("_email_subject"):
            text += f"\n>📧 _{job['_email_subject']}_"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": text}]})
    blocks.append({"type": "divider"})
    return blocks


def _send(payload: dict) -> None:
    if not WEBHOOK:
        print("[dry-run: no SLACK_WEBHOOK_URL] would send:")
        print(json.dumps(payload, indent=2)[:1500])
        return
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    r.raise_for_status()


def send_heartbeat(text: str) -> None:
    """One-line status ping so a quiet run is visibly alive, not silently dead."""
    _send({"text": text})


def send_bulk(jobs: list[dict]) -> None:
    """Post the B-tier bulk-apply queue: a dense list of links, no outreach.

    These are the 'worth a shot' roles that used to be discarded. They're meant
    to be ripped through with an autofill extension, ~30s each — so the format
    is one scannable line per role, not a card.
    """
    if not jobs:
        return
    header = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"⚡ Bulk apply queue — {len(jobs)} role(s)", "emoji": True},
    }
    note = {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "_Autofill + submit, no referral hunt. Aim ~30s each._"}]}
    blocks, lines = [header, note], []

    def flush():
        nonlocal blocks, lines
        if lines:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
            lines = []

    for job in jobs:
        loc = (job.get("location") or "—")[:34]
        r = "🤖" if job.get("_resume") == "ai" else "💻"
        lines.append(f"{r} `{job.get('_score',0):>2}` *<{job['url']}|{job['title'][:58]}>* "
                     f"— {job['company']} · {loc}")
        if len(lines) >= 10:                      # ~10 lines keeps a section under Slack's 3k chars
            flush()
        if len(blocks) >= MAX_BLOCKS:
            _send({"blocks": blocks})
            blocks = [header, note]
    flush()
    if len(blocks) > 2:
        _send({"blocks": blocks})


def send_jobs(jobs: list[dict]) -> None:
    """Post ranked, fit-scored jobs to Slack, chunked under the block limit."""
    if not jobs:
        return
    header = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"🎯 {len(jobs)} high-fit role(s) for you", "emoji": True},
    }
    blocks = [header, {"type": "divider"}]
    for job in jobs:
        piece = _job_block(job)
        if len(blocks) + len(piece) > MAX_BLOCKS:
            _send({"blocks": blocks})
            blocks = [header, {"type": "divider"}]
        blocks += piece
    # Footer nudge: A-tier is where the referral outreach pays off (~40-65% reply
    # vs ~2-3% cold), and it's the step that gets skipped. Point at the cockpit.
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "📇 *Don't just apply — ask for the referral.* Run `outreach` "
                "on your Mac to open the cockpit: find the referrer, copy the DM, "
                "track follow-ups."}]})
    if len(blocks) > 2:
        _send({"blocks": blocks})


if __name__ == "__main__":
    # `python notify.py` sends one realistic sample alert — used by the
    # test_slack workflow dispatch to verify webhook + formatting end to end.
    send_jobs([{
        "company": "Stripe", "title": "Full Stack Engineer, Payments",
        "url": "https://stripe.com/jobs", "location": "Bengaluru, India",
        "department": "Engineering", "source": "greenhouse",
        "_score": 88, "_resume": "software", "_referral": True,
        "_reason": "Sample alert — webhook + formatting test, not a real role.",
        "_referrers": "https://www.linkedin.com/search/results/people/",
        "_alumni": "https://www.linkedin.com/search/results/people/",
        "_email_pattern": "flast@stripe.com", "_email_researched": True,
        "_dm": "Hi — I'm a software engineer (~1 yr, full-stack + AI). "
               "I'm applying for Full Stack Engineer at Stripe and would love a referral.",
        "_email_subject": "Referral request — Full Stack Engineer",
    }])
    print("Sample alert sent.")
