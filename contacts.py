"""Referral-outreach helpers — pure, no network, safe for CI.

Instead of scraping LinkedIn (bans the account) or paying an email-finder API,
we build precise one-click deep links the user opens in their own logged-in
browser, plus the company's likely email pattern. This surfaces exactly the
right people to ping for a referral without touching their account from a bot.
"""
from __future__ import annotations
import re
from urllib.parse import quote

# Company → email domain, for names that don't map cleanly to "{name}.com".
_DOMAIN = {
    "Fi Money": "epifi.com", "slice": "sliceit.com", "PhonePe": "phonepe.com",
    "CRED": "cred.club", "Morgan Stanley": "morganstanley.com",
    "S&P Global": "spglobal.com", "Wells Fargo": "wellsfargo.com",
    "Deutsche Bank": "db.com", "Sarvam AI": "sarvam.ai", "Together AI": "together.ai",
    "Scale AI": "scale.com", "Fiserv": "fiserv.com", "Meesho": "meesho.com",
    "Zeta": "zeta.tech", "Groww": "groww.in", "Plaid": "plaid.com",
    "Autodesk": "autodesk.com", "Mastercard": "mastercard.com", "eBay": "ebay.com",
    "BlackRock": "blackrock.com", "Nasdaq": "nasdaq.com", "Adyen": "adyen.com",
    "NVIDIA": "nvidia.com", "Databricks": "databricks.com", "LangChain": "langchain.dev",
}

# Company → dominant email pattern, researched via LeadIQ/RocketReach public
# format pages (Jul 2026). Those pages describe US enterprises, which is what
# this table is: every entry below is a large American employer.
#
# The DEFAULT for everything unlisted used to be first.last, inherited from the
# same US-enterprise sources — and it is wrong for the companies that actually
# reach the cold-email path. Probed live 2026-08-13 against the eight Indian
# startups whose founder names are public and whose mail server discriminates
# (razorpay, zerodha, meesho, cred, sarvam, mudrex, fampay, zuddl):
#
#     first@       8/8
#     first.last@  2/8
#     flast@       0/8
#
# So unlisted now defaults to `first`. This only ever affects the display hint;
# verify.resolve() walks the same patterns against the real server and is what
# decides the address we actually send to.
#   flast      = first initial + last name  (jdoe@)
#   first_last = first_last with underscore (jane_doe@)
_PATTERN = {
    "NVIDIA": "flast",      # 76% per LeadIQ
    "Adobe": "flast",       # 74%
    "eBay": "flast",        # ~50% (first.last 38% runner-up)
    "Stripe": "flast",      # 54%
    "Salesforce": "flast",  # 66%
    "Expedia": "flast",     # 69%, domain expedia.com
    "Mastercard": "first_last",  # 56% (underscore)
    "Citi": "first.last",        # 93%
    "ServiceNow": "first.last",  # 88%+
    "NetApp": "first.last",      # 90%
    "Target": "first.last",      # 98%
    "Databricks": "first.last",  # 83%
}

# Which title keywords to hunt for, by which résumé the role wants.
_TITLE_FILTER = '(recruiter OR "talent acquisition" OR "engineering manager" OR "hiring")'


def email_domain(company: str) -> str:
    if company in _DOMAIN:
        return _DOMAIN[company]
    slug = re.sub(r"[^a-z0-9]", "", company.lower())
    return f"{slug}.com"


def email_pattern(company: str) -> tuple[str, bool]:
    """(pattern@domain, researched?) — researched means the dominant pattern was
    looked up from public format databases; else it's the measured `first`
    default (8/8 on Indian startups, see the table above)."""
    style = _PATTERN.get(company)
    return f"{style or 'first'}@{email_domain(company)}", style is not None


def _search(keywords: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(keywords)}"


def referrers_url(company: str) -> str:
    """People at the company in recruiting / eng-management roles."""
    return _search(f"{company} {_TITLE_FILTER}")


def alumni_url(company: str, school: str) -> str:
    """Your college's alumni currently at the company — a top referral path."""
    return _search(f"{company} {school}")


def ex_colleagues_url(company: str, my_company: str) -> str:
    """Former coworkers from your company who are now at the target company."""
    return _search(f"{company} {my_company}")


def enrich(job: dict, me: dict) -> dict:
    """Attach outreach links + email pattern to a job (mutates and returns it)."""
    c = job["company"]
    job["_referrers"] = referrers_url(c)
    job["_alumni"] = alumni_url(c, me.get("school", ""))
    if me.get("company"):
        job["_excoll"] = ex_colleagues_url(c, me["company"])
    job["_email_pattern"], job["_email_researched"] = email_pattern(c)
    return job
