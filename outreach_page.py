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

Every row also has "✕ Not a fit" — the escape hatch for a role you're never going
to apply for (asks for 5 years, wrong stack, wrong city). Without it the only way
off the list was to tick "applied", which corrupts the one number worth trusting.
Dismissals are a toggle: switch the filter to Everything to undo one.

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
  --a:#047857;--b:#b45309;--new:#2563eb;--due:#b45309;--done:#059669;--chip:#f3f4f6;--gone:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f14;--fg:#e8eaed;--mut:#9aa1ac;--line:#242832;
  --card:#141821;--accent:#60a5fa;--a:#34d399;--b:#fbbf24;--new:#60a5fa;--due:#fbbf24;--done:#34d399;--chip:#1b2029;--gone:#b91c1c}}
:root[data-theme=light]{--bg:#fff;--fg:#16181d;--mut:#6b7280;--line:#e5e7eb;--card:#fff;--accent:#2563eb;
  --a:#047857;--b:#b45309;--new:#2563eb;--due:#b45309;--done:#059669;--chip:#f3f4f6;--gone:#b91c1c}
:root[data-theme=dark]{--bg:#0d0f14;--fg:#e8eaed;--mut:#9aa1ac;--line:#242832;--card:#141821;
  --accent:#60a5fa;--a:#34d399;--b:#fbbf24;--new:#60a5fa;--due:#fbbf24;--done:#34d399;--chip:#1b2029;--gone:#b91c1c}
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
.filter{display:flex;gap:6px;flex-wrap:wrap}
.filter button{padding:4px 10px;border:1px solid var(--line);background:var(--card);color:var(--mut);
  border-radius:7px;cursor:pointer;font:inherit;display:inline-flex;gap:6px;align-items:center}
.filter button.on{color:var(--fg);border-color:var(--accent)}
.filter button.zero{opacity:.4}
.filter button i{font-style:normal;font-variant-numeric:tabular-nums;font-size:11.5px;
  background:var(--chip);color:var(--mut);border-radius:99px;padding:0 6px;min-width:18px;text-align:center}
.filter button.on i{background:var(--accent);color:#fff}
/* Tier is a second axis, not another view — it narrows whichever view is open,
   so it reads as a segmented control rather than one more chip in the row. */
.tiers{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.tiers button{padding:4px 11px;border:0;border-left:1px solid var(--line);background:var(--card);
  color:var(--mut);cursor:pointer;font:inherit;display:inline-flex;gap:6px;align-items:center}
.tiers button:first-child{border-left:0}
.tiers button.on{color:#fff;background:var(--accent)}
.tiers button i{font-style:normal;font-variant-numeric:tabular-nums;font-size:11.5px;
  background:var(--chip);color:var(--mut);border-radius:99px;padding:0 6px;min-width:18px;text-align:center}
.tiers button.on i{background:rgba(255,255,255,.24);color:#fff}
.sortwrap{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--mut);font-size:12.5px}
.sortwrap select{font:inherit;font-size:12.5px;padding:4px 8px;border:1px solid var(--line);
  border-radius:7px;background:var(--card);color:var(--fg);cursor:pointer}
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
.links a.dead{color:var(--mut);cursor:pointer}
.links a.dismiss{margin-left:auto;color:var(--mut);cursor:pointer}
.links a.dismiss:hover{border-color:var(--mut);color:var(--fg)}
.card.dismissed{opacity:.42}
.card.dismissed .role a{text-decoration:line-through}
.card.dismissed .why,.card.dismissed .email-hint,.card.dismissed .dm,.card.dismissed .track{display:none}
.card.dismissed .links a:not(.dismiss){display:none}
.empty{color:var(--mut);text-align:center;padding:44px 0}
/* Liveness + age. The cockpit used to show a 3-week-old pulled requisition
   exactly like a role posted this morning; these are the signals that tell
   them apart before you spend a click. */
.meta{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:5px}
.flag{font-size:11px;padding:1px 8px;border-radius:99px;background:var(--chip);color:var(--mut);white-space:nowrap}
.flag.gone{background:var(--gone);color:#fff;font-weight:700}
.flag.old{color:var(--due)}
.flag.fresh{color:var(--done)}
/* a stated bar at or above 2 yrs — survives the gate, still worth a second look */
.flag.hot{color:var(--due);border:1px solid currentColor}
.card.gone{opacity:.5}
.card.gone .role a{text-decoration:line-through}
.card.gone .links a.apply{pointer-events:none;opacity:.4;border-color:var(--line);color:var(--mut)}
.card.gone .dm,.card.gone .email-hint{display:none}
.search{flex:1 1 190px;min-width:150px;font:inherit;font-size:12.5px;padding:5px 10px;
  border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--fg)}
.search:focus{outline:none;border-color:var(--accent)}
.more{display:block;width:100%;margin:4px 0 8px;padding:9px;border:1px dashed var(--line);
  background:none;color:var(--mut);border-radius:9px;cursor:pointer;font:inherit;font-size:12.5px}
.more:hover{border-color:var(--accent);color:var(--fg)}
"""

_JS = r"""
const K='jobcockpit.v2';
const S=JSON.parse(localStorage.getItem(K)||'{}');   // key -> {applied, status, ts}
const DAY=86400000, DUE=4;
function save(){localStorage.setItem(K,JSON.stringify(S))}
function st(k){const r=S[k]||{};return{applied:!!r.applied,status:r.status||'new',ts:r.ts||0,
  dismissed:!!r.dismissed}}
function set(k,v){S[k]={...st(k),...v};save()}
function dueFor(r){return r.status==='messaged'&&r.ts&&(Date.now()-r.ts)>=DUE*DAY}
function ago(ts){if(!ts)return'';const d=Math.floor((Date.now()-ts)/DAY);
  return d<=0?'today':d===1?'1 day ago':d+' days ago'}
// The posting itself is gone from the company's board — distinct from `status
// ==='closed'`, which is you closing out a referral thread. You cannot apply to
// a pulled requisition, so it never counts as outstanding work.
function gone(c){return c.dataset.gone==='1'}
// `closed` is the neutral way off the list, on either tier: the posting is dead,
// broken, or otherwise not worth another click, without asserting it was a bad
// match. That distinction matters — `dismissed` is the "not a fit" signal, and
// mislabelling a dead link as a bad match would poison it as training data.
function closed(r){return r.status==='closed'}
function resolved(c,r){                    // "nothing left to do on this row"
  if(r.dismissed)return true;              // not a fit — off the list, applied or not
  if(gone(c))return true;                  // posting pulled; nothing to apply to
  if(closed(r))return true;                // closed stands alone; you may never have applied
  if(c.dataset.tier==='B')return r.applied;
  return r.applied&&r.status==='replied';
}
function needsAction(c,r){
  if(r.dismissed||gone(c)||closed(r))return false;
  if(!r.applied)return true;               // still to apply
  if(c.dataset.tier==='B')return false;
  if(r.status==='replied')return false;
  return r.status==='new'||dueFor(r);      // applied but no referral ask, or follow-up due
}
// One predicate per view. "To do" is the daily driver; the rest exist so you can
// actually answer "what have I applied to?" or "who owes me a reply?" — which the
// old three-button bar (Needs action / All open / Everything) could not.
const VIEWS=[
  ['todo',     'To do',          (c,r)=>needsAction(c,r)],
  // closed out excludes a row here for the same reason it does in `resolved`:
  // the thread is finished, so it is not outstanding work you owe an application.
  ['toapply',  'To apply',       (c,r)=>!r.applied&&!r.dismissed&&!gone(c)&&r.status!=='closed'],
  ['applied',  'Applied',        (c,r)=>r.applied&&!r.dismissed],
  ['messaged', 'Messaged',       (c,r)=>!r.dismissed&&(r.status==='messaged'||r.status==='followed_up')],
  ['due',      'Follow-up due',  (c,r)=>!r.dismissed&&dueFor(r)],
  ['replied',  'Replied',        (c,r)=>!r.dismissed&&r.status==='replied'],
  ['closed',   'Closed',         (c,r)=>!r.dismissed&&r.status==='closed'],
  ['gone',     'Posting gone',   (c)=>gone(c)],
  ['nofit',    'Not a fit',      (c,r)=>r.dismissed],
  ['open',     'Still open',     (c,r)=>!resolved(c,r)],
  ['all',      'Everything',     ()=>true],
];
const VIEW=Object.fromEntries(VIEWS.map(([k,l,f])=>[k,f]));
let filter='todo';
// Tier narrows the open view rather than replacing it, so "how many A-tier roles
// am I actually still owed?" is one click, not a count you do by eye down a page
// where A and B rows are interleaved by section.
let tierSel='all';
function inTier(c){return tierSel==='all'||c.dataset.tier===tierSel}
let sortBy='new';
const SORTS={
  new:  (a,b)=>b.seen-a.seen,          // default: matches how the page is generated
  score:(a,b)=>b.score-a.score,
  co:   (a,b)=>a.co.localeCompare(b.co)||b.score-a.score,
};
function resort(){
  document.querySelectorAll('.sect').forEach(sec=>{
    const cards=[...sec.querySelectorAll('.card')].map(c=>({el:c,
      seen:+c.dataset.seen||0, score:+c.dataset.score||0, co:c.dataset.co||''}));
    cards.sort(SORTS[sortBy]);
    cards.forEach(x=>x.el.parentNode.appendChild(x.el));
  });
}
// A day's work is a screen you can finish, not a scroll you abandon. Each
// section renders a page at a time; the rest is one click away.
const PAGE=20;
let query='', limit={};
function paint(){
  const n={}; VIEWS.forEach(([k])=>n[k]=0);
  const tn={all:0,A:0,B:0};
  let needRef=0, applied=0, live=0;
  document.querySelectorAll('.card').forEach(c=>{
    const k=c.dataset.k, r=st(k), off=r.dismissed;
    c.querySelector('.chk input').checked=r.applied;
    c.querySelectorAll('.step').forEach(b=>b.classList.toggle('on',b.dataset.s===r.status));
    const isDue=dueFor(r)&&!off;
    c.classList.toggle('due',isDue&&!r.applied?false:isDue);
    c.classList.toggle('done',resolved(c,r));
    c.classList.toggle('dismissed',off);
    c.classList.toggle('gone',gone(c));
    const badge=c.querySelector('.badge'); if(badge)badge.style.display=isDue?'inline-block':'none';
    const when=c.querySelector('.when');
    if(when)when.textContent=r.ts?(r.status==='messaged'?'messaged '+ago(r.ts):
      r.status==='followed_up'?'followed up '+ago(r.ts):''):'';
    const dz=c.querySelector('.dismiss'); if(dz)dz.textContent=off?'↩︎ Undo dismiss':'✕ Not a fit';
    const dd=c.querySelector('.dead');
    if(dd)dd.textContent=closed(r)?'↩︎ Undo closed':'🚫 Gone / broken';
    // Counts describe the whole list, deliberately ignoring the search box —
    // a chip that changed as you typed would stop meaning anything. They do
    // respect the tier tabs: each axis counts within the other's selection, so
    // the two rows of numbers always add up to what is on screen.
    const t=inTier(c);
    VIEWS.forEach(([key,,fn])=>{if(t&&fn(c,r))n[key]++});
    if(VIEW[filter](c,r)){tn.all++; tn[c.dataset.tier]=(tn[c.dataset.tier]||0)+1;}
    if(c.dataset.tier==='A'&&!off&&r.applied&&r.status==='new')needRef++;
    if(r.applied&&!off)applied++;
    if(!off&&!gone(c))live++;
    c.dataset.vis=(t&&VIEW[filter](c,r)&&(!query||c.dataset.q.includes(query)))?'1':'0';
  });
  VIEWS.forEach(([key])=>{
    const b=document.querySelector(`.filter button[data-f="${key}"]`);
    if(!b)return;
    b.querySelector('i').textContent=n[key];
    b.classList.toggle('zero',!n[key]);
  });
  document.querySelectorAll('.tiers button').forEach(b=>{
    b.querySelector('i').textContent=tn[b.dataset.t]||0;
    b.classList.toggle('on',b.dataset.t===tierSel);
  });
  document.getElementById('c-ref').textContent=needRef;
  document.getElementById('c-done').textContent=applied+'/'+live;
  let anyShown=false;
  document.querySelectorAll('.sect').forEach(sec=>{
    const id=sec.dataset.sect, cap=limit[id]||PAGE;
    let seen=0;
    sec.querySelectorAll('.card').forEach(c=>{
      const want=c.dataset.vis==='1';
      if(want)seen++;
      c.style.display=(want&&seen<=cap)?'':'none';
    });
    const btn=sec.querySelector('.more');
    if(btn){btn.style.display=seen>cap?'':'none';
      btn.textContent=`Show ${Math.min(PAGE,seen-cap)} more (${seen-cap} hidden)`;}
    sec.style.display=seen?'':'none';
    anyShown=anyShown||seen>0;
  });
  document.getElementById('empty').style.display=anyShown?'none':'block';
}
document.addEventListener('change',e=>{
  const box=e.target.closest('.chk');
  if(box){const k=box.closest('.card').dataset.k; set(k,{applied:e.target.checked}); paint();}
});
document.addEventListener('click',e=>{
  const more=e.target.closest('.more');
  if(more){const id=more.closest('.sect').dataset.sect;
    limit[id]=(limit[id]||PAGE)+PAGE; paint(); return;}
  const dz=e.target.closest('.dismiss');
  if(dz){e.preventDefault();
    const k=dz.closest('.card').dataset.k; set(k,{dismissed:!st(k).dismissed}); paint();return;}
  const dd=e.target.closest('.dead');
  if(dd){e.preventDefault();               // same state the A-tier "Closed" chip writes
    const k=dd.closest('.card').dataset.k, cur=st(k);
    set(k,{status:closed(cur)?'new':'closed'}); paint();return;}
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
  filter=b.dataset.f; limit={};      // a new view starts at the top of its first page
  document.querySelectorAll('.filter button').forEach(x=>x.classList.toggle('on',x===b)); paint();});
document.querySelectorAll('.tiers button').forEach(b=>b.onclick=()=>{
  tierSel=b.dataset.t; limit={}; paint();});   // paint() owns the .on class here
const sel=document.getElementById('sort');
if(sel)sel.onchange=()=>{sortBy=sel.value; resort(); paint();};
const box=document.getElementById('q');
if(box)box.oninput=()=>{query=box.value.trim().toLowerCase(); limit={}; paint();};
paint();
"""


def _age_days(job: dict) -> int:
    seen = job.get("first_seen") or 0
    return int((dt.datetime.now().timestamp() - seen) // 86400) if seen else 0


def _flags(job: dict) -> str:
    """The row's provenance, at a glance: how old, and can we still reach it.

    A cockpit that shows a pulled requisition identically to this morning's
    posting spends your clicks on 404s — 66 of the 97 verifiable rows on Aug 5
    had a dead Apply link. Now the row says so before you click.
    """
    out = []
    d = _age_days(job)
    cls = "fresh" if d <= 1 else ("old" if d >= 7 else "")
    out.append(f'<span class="flag {cls}">{"today" if d < 1 else f"{d}d old"}</span>')
    if job.get("closed"):
        out.append('<span class="flag gone">⚠️ Posting gone</span>')
    elif job.get("reposted"):
        out.append('<span class="flag">↻ relisted — link updated</span>')
    elif job.get("sampled"):
        # The board answered, but only a slice of it — we did not see this role
        # and cannot say either way. Distinct from "posting gone", which is the
        # claim this used to make on exactly these rows.
        out.append('<span class="flag" title="This board returns more roles '
                   'than one fetch reads, and this one was outside the slice we '
                   'saw — still listed as far as we know">not re-checked</span>')
    elif job.get("verifiable") is False:
        # aggregator listing: no board of ours to re-poll, so we can't confirm it
        out.append('<span class="flag" title="Aggregator listing — we can\'t '
                   're-check whether it is still open">link unverified</span>')
    out.append(_years_flag(job))
    return f'<div class="meta">{"".join(out)}</div>'


def _years_flag(job: dict) -> str:
    """The JD's stated experience requirement — always shown, including when
    there isn't one.

    It used to render only `if job.get("min_years")`, which is silent in the two
    cases that matter most. A JD asking "0+ years" is falsy and showed nothing;
    far worse, a JD we could not read a requirement out of also showed nothing,
    and the two are opposite facts wearing the same blank space. Reviewing 87
    rows by hand, 38 were rejected — and the single most common reason was an
    experience bar you could only discover by opening the link, because the page
    had told you nothing either way.
    """
    lo, hi = job.get("min_years"), job.get("max_years")
    if lo is None:
        if not job.get("regated"):
            return ''
        return ('<span class="flag" title="We read the JD and it states no '
                'experience requirement — treat the level as unknown">no yrs stated</span>')
    span = f"{lo}–{hi}" if hi and hi > lo else f"{lo}+"
    hot = " hot" if lo >= 2 else ""
    return f'<span class="flag{hot}" title="Stated in the JD">asks {span} yrs</span>'


def _card(job: dict) -> str:
    k = html.escape(job["key"])
    tier = job.get("tier", "A")
    resume = "🤖 AI" if job.get("resume") == "ai" else "💻 SWE"
    # score/first_seen/company ride along so the sort control can reorder in-page
    # without a regeneration; data-q is the search index, data-gone gates the
    # views that would otherwise send you to a pulled requisition.
    q = html.escape(f'{job.get("title","")} {job.get("company","")} '
                    f'{job.get("location","")}'.lower())
    data = (f'data-k="{k}" data-tier="{tier}" data-score="{int(job.get("score") or 0)}" '
            f'data-seen="{int(job.get("first_seen") or 0)}" '
            f'data-gone="{1 if job.get("closed") else 0}" data-q="{q}" '
            f'data-co="{html.escape(job.get("company",""))}"')
    apply_link = f'<a class="apply" href="{html.escape(job.get("url","#"))}" target="_blank">Apply ↗</a>'
    # Two escape hatches, and the difference between them is the point. "Not a
    # fit" is a judgement on the role (too senior, wrong stack, wrong city) and
    # is the only one safe to ever read back as a training signal. "Gone/broken"
    # says nothing about fit — the link 404s or the req was pulled — so a dead
    # posting never gets recorded as a bad match.
    dead = '<a class="dead" title="Dead link or pulled posting — not a judgement '
    dead += 'on fit">🚫 Gone / broken</a>'
    dismiss = '<a class="dismiss">✕ Not a fit</a>'
    head = f"""<div class="head">
    <label class="chk"><input type="checkbox"><span>applied</span></label>
    <div class="main">
      <div class="role"><a href="{html.escape(job.get('url','#'))}" target="_blank">{html.escape(job['title'])}</a></div>
      <div class="co">{html.escape(job['company'])} · {html.escape((job.get('location') or '—'))} · {resume}</div>
      {_flags(job)}
      <div class="why">{html.escape(job.get('reason',''))}</div>
    </div>
    <div style="text-align:right">
      <span class="badge">Follow-up due</span>
      <div class="score">{job.get('score','')}</div>
    </div>
  </div>"""
    if tier != "A" or not job.get("referrers"):
        # B-tier (or an A-tier row that never got enriched): apply + track only.
        return (f'<div class="card" {data}>{head}'
                f'<div class="links">{apply_link}{dead}{dismiss}</div></div>')

    links = [apply_link,
             f'<a href="{html.escape(job["referrers"])}" target="_blank">🔗 Find referrer</a>',
             f'<a href="{html.escape(job["alumni"])}" target="_blank">🎓 Alumni</a>']
    if job.get("excoll"):
        links.append(f'<a href="{html.escape(job["excoll"])}" target="_blank">🏢 Ex-colleagues</a>')
    subject = job.get("email_subject") or f"Referral request — {job['title']} at {job['company']}"
    links.append(f'<a href="{html.escape(_mailto(subject, job.get("email_body") or ""))}">✉️ Email draft</a>')
    links.append(dead)
    links.append(dismiss)
    tag = "researched" if job.get("email_researched") else "guess — confirm the name on LinkedIn"
    steps = "".join(f'<span class="step" data-s="{s}">{lbl}</span>'
                    for s, lbl in [("messaged", "Messaged"), ("followed_up", "Followed up"),
                                   ("replied", "Replied"), ("closed", "Closed")])
    return f"""<div class="card" {data}>
  {head}
  <div class="links">{''.join(links)}</div>
  <div class="email-hint">Likely email: <code>{html.escape(job.get('email_pattern',''))}</code> ({tag})</div>
  <div class="dm"><textarea readonly>{html.escape(job.get('dm') or '')}</textarea><button class="copy">Copy</button></div>
  <div class="track"><span class="lbl">Referral:</span>{steps}<span class="when"></span></div>
</div>"""


def _section(title: str, cls: str, note: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    # Cards live in their own container so the sort control can reorder them
    # without shuffling the "show more" button along with them.
    cards = "\n".join(_card(r) for r in rows)
    return (f'<div class="sect" data-sect="{cls}"><h2><span class="tag {cls}">{title}</span>'
            f'<span class="note">{len(rows)} · {note}</span></h2>'
            f'<div class="cards">{cards}</div>'
            f'<button class="more" style="display:none"></button></div>')


def render(rows: list[dict], path: str) -> str:
    """Write the cockpit to `path`. `rows` = tracker entries (A-tier first)."""
    stamp = dt.datetime.now().strftime("%d %b %Y, %H:%M")
    a = [r for r in rows if r.get("tier", "A") == "A"]
    b = [r for r in rows if r.get("tier", "A") == "B"]
    # Labels live in the JS (VIEWS) so counts and predicates can't drift apart;
    # the buttons are rendered from the same list at load.
    chips = "".join(
        f'<button data-f="{k}"{" class=\"on\"" if k == "todo" else ""}{t}>{lbl}<i>0</i></button>'
        for k, lbl, t in [
            ("todo", "To do", ' title="Everything still owed an action — the daily driver"'),
            ("toapply", "To apply", ' title="Not applied yet, not dismissed"'),
            ("applied", "Applied", ' title="Ticked applied"'),
            ("messaged", "Messaged", ' title="Referral ask sent, awaiting a reply"'),
            ("due", "Follow-up due", ' title="Messaged 4+ days ago with no reply"'),
            ("replied", "Replied", ""),
            ("closed", "Closed", ' title="Referral thread closed out"'),
            ("gone", "Posting gone", ' title="No longer on the company board — the Apply link is dead"'),
            ("nofit", "Not a fit", ' title="Dismissed — undo one from here"'),
            ("open", "Still open", ' title="Anything not finished or dismissed"'),
            ("all", "Everything", ""),
        ])
    # counts are filled in by paint() — the tier a row sits in is static, but how
    # many of them survive the open view is not.
    tiers = "".join(
        f'<button data-t="{k}"{t}>{lbl}<i>0</i></button>'
        for k, lbl, t in [
            ("all", "Both", ""),
            ("A", "A-tier", ' title="Referral-worthy — the ones to spend effort on"'),
            ("B", "B-tier", ' title="Bulk apply — no referral hunt"'),
        ])
    gone = sum(1 for r in rows if r.get("closed"))
    live = len(rows) - gone
    sub = (f"{live} live · {gone} closed · generated {stamp} — "
           "apply, ask for the referral, track follow-ups. Progress saved in this browser.")
    body = f"""<h1>Your job worklist</h1>
<div class="sub">{sub}</div>
<div class="bar">
  <span class="filter">{chips}</span>
  <span class="tiers">{tiers}</span>
  <input id="q" class="search" type="search" placeholder="Search role or company…"
         autocomplete="off" spellcheck="false">
  <span class="sortwrap"><label for="sort">Sort</label>
    <select id="sort">
      <option value="new">Newest first</option>
      <option value="score">Best fit</option>
      <option value="co">Company A–Z</option>
    </select></span>
  <span class="pill"><b id="c-done">0</b> applied</span>
  <span class="pill"><b id="c-ref">0</b> need referral</span>
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
