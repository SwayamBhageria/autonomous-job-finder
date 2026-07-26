"""Apply-queue page generator.

Slack is a good *notifier* but a bad *worklist*: alerts arrive 60 at a time,
interleaved with everything else, with no way to mark one done. The bottleneck
on volume isn't finding roles any more — it's working through them.

This renders the whole ranked backlog as one self-contained HTML page: every
role a direct apply link, grouped by tier, with a checkbox per row that
persists in localStorage so a half-finished session survives a browser close.

Written by `poller.py --backlog`.
"""
from __future__ import annotations
import html
import json
import datetime as dt

_CSS = """
:root{--bg:#fff;--fg:#16181d;--mut:#6b7280;--line:#e5e7eb;--card:#fff;--accent:#2563eb;--a:#047857;--b:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f14;--fg:#e8eaed;--mut:#9aa1ac;--line:#242832;--card:#141821;--accent:#60a5fa;--a:#34d399;--b:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:23px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:22px}
.bar{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 0;margin-bottom:14px;z-index:5;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.prog{flex:1;min-width:180px;height:7px;background:var(--line);border-radius:99px;overflow:hidden}
.prog>i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
.count{font-variant-numeric:tabular-nums;font-size:13px;color:var(--mut)}
button{font:inherit;padding:5px 11px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:7px;cursor:pointer}
button:hover{border-color:var(--accent)}
h2{font-size:15px;margin:26px 0 10px;display:flex;align-items:center;gap:9px}
.tag{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:2px 8px;border-radius:99px;border:1px solid currentColor}
.tag.a{color:var(--a)}.tag.b{color:var(--b)}
.note{color:var(--mut);font-size:12.5px;font-weight:400;letter-spacing:0}
table{width:100%;border-collapse:collapse}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
th,td{text-align:left;padding:9px 11px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}
tr.done{opacity:.36}
tr.done a{text-decoration:line-through}
td.s{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.co{color:var(--mut);white-space:nowrap}
.loc{color:var(--mut);font-size:13px}
.why{color:var(--mut);font-size:12.5px;max-width:340px}
.rz{font-size:12px;white-space:nowrap}
input[type=checkbox]{width:17px;height:17px;cursor:pointer;accent-color:var(--accent)}
.links a{font-size:12px;margin-right:7px;white-space:nowrap}
"""

_JS = """
const K='jobqueue.applied.v1';
const S=new Set(JSON.parse(localStorage.getItem(K)||'[]'));
function save(){localStorage.setItem(K,JSON.stringify([...S]))}
function paint(){
  let n=0;const b=document.querySelectorAll('tr[data-k]');
  b.forEach(tr=>{const on=S.has(tr.dataset.k);tr.classList.toggle('done',on);
    tr.querySelector('input').checked=on;if(on)n++;});
  document.getElementById('n').textContent=n+' / '+b.length+' applied';
  document.getElementById('p').style.width=(b.length?100*n/b.length:0)+'%';
}
document.addEventListener('change',e=>{
  if(e.target.type!=='checkbox')return;
  const k=e.target.closest('tr').dataset.k;
  e.target.checked?S.add(k):S.delete(k);save();paint();
});
document.getElementById('reset').onclick=()=>{if(confirm('Clear all applied marks?')){S.clear();save();paint()}};
document.getElementById('open').onclick=()=>{
  const next=[...document.querySelectorAll('tr[data-k]')].filter(tr=>!S.has(tr.dataset.k)).slice(0,10);
  if(!next.length)return alert('Queue clear.');
  next.forEach(tr=>window.open(tr.querySelector('a.ap').href,'_blank'));
};
paint();
"""


def _rows(jobs: list[dict]) -> str:
    out = []
    for j in jobs:
        k = html.escape(f"{j.get('source','')}:{j.get('company','')}:{j.get('id','')}")
        rz = "🤖 AI" if j.get("_resume") == "ai" else "💻 SWE"
        links = ""
        if j.get("_referrers"):
            links += f'<a href="{html.escape(j["_referrers"])}" target="_blank">🤝 refs</a>'
        if j.get("_alumni"):
            links += f'<a href="{html.escape(j["_alumni"])}" target="_blank">🎓 alumni</a>'
        out.append(
            f'<tr data-k="{k}">'
            f'<td><input type="checkbox"></td>'
            f'<td class="s">{j.get("_score", 0)}</td>'
            f'<td><a class="ap" href="{html.escape(j.get("url", "#"))}" target="_blank">'
            f'{html.escape(j.get("title", "")[:74])}</a>'
            f'<div class="loc">{html.escape((j.get("location") or "—")[:52])}</div></td>'
            f'<td class="co">{html.escape(j.get("company", ""))}</td>'
            f'<td class="rz">{rz}</td>'
            f'<td class="why">{html.escape((j.get("_reason") or "")[:120])}</td>'
            f'<td class="links">{links}</td>'
            f"</tr>"
        )
    return "\n".join(out)


def _table(title: str, cls: str, note: str, jobs: list[dict]) -> str:
    if not jobs:
        return ""
    return (
        f'<h2><span class="tag {cls}">{title}</span>'
        f'<span class="note">{len(jobs)} roles · {note}</span></h2>'
        f'<div class="scroll"><table><thead><tr>'
        f"<th></th><th>Fit</th><th>Role</th><th>Company</th><th>Résumé</th>"
        f"<th>Why</th><th>Referral</th>"
        f"</tr></thead><tbody>{_rows(jobs)}</tbody></table></div>"
    )


def render(a_tier: list[dict], b_tier: list[dict], path: str, meta: str = "") -> str:
    """Write the queue page to `path`; returns the path written."""
    stamp = dt.datetime.now().strftime("%d %b %Y, %H:%M")
    total = len(a_tier) + len(b_tier)
    body = (
        f"<h1>Apply queue</h1>"
        f'<div class="sub">{total} open roles · generated {stamp}{" · " + html.escape(meta) if meta else ""}</div>'
        f'<div class="bar">'
        f'<button id="open">Open next 10 →</button>'
        f'<div class="prog"><i id="p"></i></div>'
        f'<span class="count" id="n"></span>'
        f'<button id="reset">Reset</button>'
        f"</div>"
        + _table("A-tier", "a", "worth a referral hunt + a tailored line", a_tier)
        + _table("B-tier", "b", "autofill and submit, ~30s each", b_tier)
    )
    doc = (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Apply queue — {total} roles</title><style>{_CSS}</style></head>"
        f'<body><div class="wrap">{body}</div><script>{_JS}</script></body></html>'
    )
    with open(path, "w") as f:
        f.write(doc)
    return path
