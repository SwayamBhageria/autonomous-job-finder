"""Daily worklist cockpit.

One local page for the day's high-fit roles, tracked on two axes: did you apply,
and (for A-tier) did you reach out for the referral. The A-tier alert fires the
DM + links once into Slack and they scroll away; this is the durable surface that
replaces working from the chat feed.

Per role: the apply link, an "Applied" checkbox, and — for A-tier — a find-referrer
button, one-click DM copy, a prefilled email draft, and an outreach status tracker
(messaged → followed-up → replied). A role messaged 4+ days ago with no reply
surfaces as "follow-up due" and floats up — the reminder nobody sends by hand,
and the reason follow-ups lift replies 15-72%. B-tier roles are apply-and-track
only (no referral hunt — that effort is reserved for the high-fit tier).

All progress persists in the page's localStorage, keyed to the file path, so it
survives regeneration and reboots as long as you open it the same way (the
`outreach` command always does). Written by `poller.py --outreach`.
"""
from __future__ import annotations
import html
import datetime as dt
from urllib.parse import quote


def _mailto(subject: str, body: str) -> str:
    return f"mailto:?subject={quote(subject)}&body={quote(body)}"


_CSS = """
:root{--bg:#fff;--fg:#16181d;--mut:#6b7280;--line:#e5e7eb;--card:#fff;--accent:#2563eb;
  --a:#047857;--b:#b45309;--new:#2563eb;--due:#b45309;--done:#059669;--chip:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f14;--fg:#e8eaed;--mut:#9aa1ac;--line:#242832;
  --card:#141821;--accent:#60a5fa;--a:#34d399;--b:#fbbf24;--new:#60a5fa;--due:#fbbf24;--done:#34d399;--chip:#1b2029}}
:root[data-theme=light]{--bg:#fff;--fg:#16181d;--mut:#6b7280;--line:#e5e7eb;--card:#fff;--accent:#2563eb;
  --a:#047857;--b:#b45309;--new:#2563eb;--due:#b45309;--done:#059669;--chip:#f3f4f6}
:root[data-theme=dark]{--bg:#0d0f14;--fg:#e8eaed;--mut:#9aa1ac;--line:#242832;--card:#141821;
  --accent:#60a5fa;--a:#34d399;--b:#fbbf24;--new:#60a5fa;--due:#fbbf24;--done:#34d399;--chip:#1b2029}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:26px 18px 90px}
h1{font-size:22px;margin:0 0 3px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.bar{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
  padding:11px 0;margin-bottom:16px;z-index:5;display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:13px}
.bar b{font-variant-numeric:tabular-nums}
.pill{padding:2px 9px;border-radius:99px;background:var(--chip);color:var(--mut)}
.pill.due{color:var(--due)}.pill.todo{color:var(--new)}
.filter{margin-left:auto;display:flex;gap:6px}
.filter button{padding:4px 10px;border:1px solid var(--line);background:var(--card);color:var(--mut);
  border-radius:7px;cursor:pointer;font:inherit}
.filter button.on{color:var(--fg);border-color:var(--accent)}
h2{font-size:14px;margin:24px 0 10px;display:flex;align-items:center;gap:9px}
.tag{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:2px 8px;border-radius:99px;border:1px solid currentColor}
.tag.a{color:var(--a)}.tag.b{color:var(--b)}
.note{color:var(--mut);font-size:12.5px;font-weight:400;letter-spacing:0}
.card{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:14px 16px;margin-bottom:11px}
.card.due{border-color:var(--due)}
.card.done{opacity:.5}
.head{display:flex;gap:12px;align-items:flex-start}
.chk{display:flex;flex-direction:column;align-items:center;gap:3px;padding-top:2px;cursor:pointer;user-select:none}
.chk input{width:20px;height:20px;cursor:pointer;accent-color:var(--done)}
.chk span{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.main{flex:1;min-width:0}
.role{font-size:15.5px;font-weight:650}
.role a{color:var(--accent);text-decoration:none}.role a:hover{text-decoration:underline}
.co{color:var(--mut);font-size:13px;margin-top:1px}
.score{font-variant-numeric:tabular-nums;font-weight:700;color:var(--mut);white-space:nowrap}
.why{color:var(--mut);font-size:12.5px;margin:6px 0 0}
.links{display:flex;gap:8px;flex-wrap:wrap;margin:11px 0 0}
.links a{font-size:12.5px;text-decoration:none;color:var(--fg);border:1px solid var(--line);
  padding:5px 10px;border-radius:7px;white-space:nowrap;background:var(--bg)}
.links a:hover{border-color:var(--accent)}
.links a.apply{border-color:var(--accent);color:var(--accent);font-weight:600}
.email-hint{font-size:12px;color:var(--mut);margin:9px 0 0}
.email-hint code{background:var(--chip);padding:1px 5px;border-radius:4px}
.dm{position:relative;margin-top:11px}
.dm textarea{width:100%;min-height:66px;resize:vertical;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--fg);padding:9px 11px;font:13px/1.5 inherit}
.dm .copy{position:absolute;top:7px;right:7px;padding:3px 9px;border:1px solid var(--line);
  background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer;font-size:12px}
.dm .copy:hover{border-color:var(--accent)}
.track{display:flex;gap:7px;align-items:center;margin-top:11px;flex-wrap:wrap}
.track .lbl{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-right:2px}
.track .step{padding:4px 11px;border:1px solid var(--line);border-radius:99px;cursor:pointer;
  font-size:12.5px;color:var(--mut);background:var(--bg)}
.track .step.on{color:#fff;border-color:transparent}
.step.on[data-s=messaged]{background:var(--new)}
.step.on[data-s=followed_up]{background:var(--due)}
.step.on[data-s=replied]{background:var(--done)}
.step.on[data-s=closed]{background:var(--mut)}
.badge{font-size:11px;font-weight:700;padding:2px 9px;border-radius:99px;text-transform:uppercase;
  letter-spacing:.04em;background:var(--due);color:#fff;display:none}
.when{font-size:12px;color:var(--mut);margin-left:2px}
.empty{color:var(--mut);text-align:center;padding:44px 0}
"""

_JS = r"""
const K='jobcockpit.v2';
const S=JSON.parse(localStorage.getItem(K)||'{}');   // key -> {applied, status, ts}
const DAY=86400000, DUE=4;
function save(){localStorage.setItem(K,JSON.stringify(S))}
function st(k){const r=S[k]||{};return{applied:!!r.applied,status:r.status||'new',ts:r.ts||0}}
function set(k,v){S[k]={...st(k),...v};save()}
function dueFor(r){return r.status==='messaged'&&r.ts&&(Date.now()-r.ts)>=DUE*DAY}
function ago(ts){if(!ts)return'';const d=Math.floor((Date.now()-ts)/DAY);
  return d<=0?'today':d===1?'1 day ago':d+' days ago'}
function resolved(c,r){                    // "nothing left to do on this row"
  if(c.dataset.tier==='B')return r.applied;
  return r.applied&&(r.status==='replied'||r.status==='closed');
}
function needsAction(c,r){
  if(!r.applied)return true;               // still to apply
  if(c.dataset.tier==='B')return false;
  if(r.status==='replied'||r.status==='closed')return false;
  return r.status==='new'||dueFor(r);      // applied but no referral ask, or follow-up due
}
let filter='action';
function paint(){
  let toApply=0,due=0,needRef=0;
  document.querySelectorAll('.card').forEach(c=>{
    const k=c.dataset.k, r=st(k);
    c.querySelector('.chk input').checked=r.applied;
    c.querySelectorAll('.step').forEach(b=>b.classList.toggle('on',b.dataset.s===r.status));
    const isDue=dueFor(r);
    c.classList.toggle('due',isDue&&!r.applied?false:isDue);
    c.classList.toggle('done',resolved(c,r));
    const badge=c.querySelector('.badge'); if(badge)badge.style.display=isDue?'inline-block':'none';
    const when=c.querySelector('.when');
    if(when)when.textContent=r.ts?(r.status==='messaged'?'messaged '+ago(r.ts):
      r.status==='followed_up'?'followed up '+ago(r.ts):''):'';
    if(!r.applied)toApply++;
    if(c.dataset.tier==='A'&&isDue)due++;
    if(c.dataset.tier==='A'&&r.applied&&r.status==='new')needRef++;
    let show=true;
    if(filter==='action')show=needsAction(c,r);
    else if(filter==='open')show=!resolved(c,r);
    c.style.display=show?'':'none';
  });
  document.getElementById('c-apply').textContent=toApply;
  document.getElementById('c-due').textContent=due;
  document.getElementById('c-ref').textContent=needRef;
  document.querySelectorAll('.sect').forEach(sec=>{
    const any=[...sec.querySelectorAll('.card')].some(c=>c.style.display!=='none');
    sec.style.display=any?'':'none';
  });
  const anyShown=[...document.querySelectorAll('.card')].some(c=>c.style.display!=='none');
  document.getElementById('empty').style.display=anyShown?'none':'block';
}
document.addEventListener('change',e=>{
  const box=e.target.closest('.chk');
  if(box){const k=box.closest('.card').dataset.k; set(k,{applied:e.target.checked}); paint();}
});
document.addEventListener('click',e=>{
  const step=e.target.closest('.step');
  if(step){const k=step.closest('.card').dataset.k, s=step.dataset.s, cur=st(k);
    if(cur.status===s&&s!=='new'){set(k,{status:'new',ts:0});}
    else{const ts=s==='messaged'?Date.now():((s==='followed_up'||s==='replied'||s==='closed')&&cur.ts?cur.ts:0);
      set(k,{status:s,ts});}
    paint();return;}
  const cp=e.target.closest('.copy');
  if(cp){const t=cp.closest('.dm').querySelector('textarea');
    navigator.clipboard.writeText(t.value).then(()=>{cp.textContent='Copied ✓';
      setTimeout(()=>cp.textContent='Copy',1400)});}
});
document.querySelectorAll('.filter button').forEach(b=>b.onclick=()=>{
  filter=b.dataset.f; document.querySelectorAll('.filter button').forEach(x=>x.classList.toggle('on',x===b)); paint();});
paint();
"""


def _card(job: dict) -> str:
    k = html.escape(job["key"])
    tier = job.get("tier", "A")
    resume = "🤖 AI" if job.get("resume") == "ai" else "💻 SWE"
    apply_link = f'<a class="apply" href="{html.escape(job.get("url","#"))}" target="_blank">Apply ↗</a>'
    head = f"""<div class="head">
    <label class="chk"><input type="checkbox"><span>applied</span></label>
    <div class="main">
      <div class="role"><a href="{html.escape(job.get('url','#'))}" target="_blank">{html.escape(job['title'])}</a></div>
      <div class="co">{html.escape(job['company'])} · {html.escape((job.get('location') or '—'))} · {resume}</div>
      <div class="why">{html.escape(job.get('reason',''))}</div>
    </div>
    <div style="text-align:right">
      <span class="badge">Follow-up due</span>
      <div class="score">{job.get('score','')}</div>
    </div>
  </div>"""
    if tier != "A" or not job.get("referrers"):
        # B-tier (or an A-tier row that never got enriched): apply + track only.
        return f'<div class="card" data-k="{k}" data-tier="{tier}">{head}<div class="links">{apply_link}</div></div>'

    links = [apply_link,
             f'<a href="{html.escape(job["referrers"])}" target="_blank">🔗 Find referrer</a>',
             f'<a href="{html.escape(job["alumni"])}" target="_blank">🎓 Alumni</a>']
    if job.get("excoll"):
        links.append(f'<a href="{html.escape(job["excoll"])}" target="_blank">🏢 Ex-colleagues</a>')
    subject = job.get("email_subject") or f"Referral request — {job['title']} at {job['company']}"
    links.append(f'<a href="{html.escape(_mailto(subject, job.get("email_body") or ""))}">✉️ Email draft</a>')
    tag = "researched" if job.get("email_researched") else "guess — confirm the name on LinkedIn"
    steps = "".join(f'<span class="step" data-s="{s}">{lbl}</span>'
                    for s, lbl in [("messaged", "Messaged"), ("followed_up", "Followed up"),
                                   ("replied", "Replied"), ("closed", "Closed")])
    return f"""<div class="card" data-k="{k}" data-tier="A">
  {head}
  <div class="links">{''.join(links)}</div>
  <div class="email-hint">Likely email: <code>{html.escape(job.get('email_pattern',''))}</code> ({tag})</div>
  <div class="dm"><textarea readonly>{html.escape(job.get('dm') or '')}</textarea><button class="copy">Copy</button></div>
  <div class="track"><span class="lbl">Referral:</span>{steps}<span class="when"></span></div>
</div>"""


def _section(title: str, cls: str, note: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    cards = "\n".join(_card(r) for r in rows)
    return (f'<div class="sect"><h2><span class="tag {cls}">{title}</span>'
            f'<span class="note">{len(rows)} · {note}</span></h2>{cards}</div>')


def render(rows: list[dict], path: str) -> str:
    """Write the cockpit to `path`. `rows` = tracker entries (A-tier first)."""
    stamp = dt.datetime.now().strftime("%d %b %Y, %H:%M")
    a = [r for r in rows if r.get("tier", "A") == "A"]
    b = [r for r in rows if r.get("tier", "A") == "B"]
    body = f"""<h1>Your job worklist</h1>
<div class="sub">{len(rows)} roles · generated {stamp} · apply, ask for the referral, track follow-ups — progress saved in this browser</div>
<div class="bar">
  <span class="pill todo"><b id="c-apply">0</b> to apply</span>
  <span class="pill due"><b id="c-due">0</b> follow-up due</span>
  <span class="pill"><b id="c-ref">0</b> applied, need referral</span>
  <span class="filter">
    <button data-f="action" class="on">Needs action</button>
    <button data-f="open">All open</button>
    <button data-f="all">Everything</button>
  </span>
</div>
{_section("A-tier", "a", "apply + ask for a referral", a)}
{_section("B-tier", "b", "autofill, apply, tick — no referral hunt", b)}
<div id="empty" class="empty" style="display:none">Nothing needs action right now. 🎉</div>"""
    doc = ("<!doctype html><html><head><meta charset=utf-8>"
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>Job worklist — {len(rows)} roles</title><style>{_CSS}</style></head>"
           f'<body><div class="wrap">{body}</div><script>{_JS}</script></body></html>')
    with open(path, "w") as f:
        f.write(doc)
    return path
