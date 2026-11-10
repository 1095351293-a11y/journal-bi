'use strict';
/* ============ 工具 ============ */
const $  = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => (Number(n)||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtI = n => (Number(n)||0).toLocaleString('en-US',{maximumFractionDigits:0});

async function api(path, opts={}){
  const r = await fetch(path, opts);
  if(!r.ok){ let m='请求失败'; try{ m=(await r.json()).error||m }catch(e){} throw new Error(m); }
  return r.json();
}
const jsonInit = (method, body) =>
  ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
const jpost = (p,b)=>api(p, jsonInit('POST', b));
const jput  = (p,b)=>api(p, jsonInit('PUT',  b));
const jdel  = p=>api(p, {method:'DELETE'});

/* ============ 状态 ============ */
const S = {
  companies:[], accounts:[], tags:[], rates:{}, today:'', depAlerts:{due:0,soon:0},
  range:null, mxAcc:{}, depCounts:{}, mxOff:[], mxDepOff:[],
  reminders:[], company:null, view:'dashboard',
  jr:{ accId:null, account:null, entries:[], loaded:false,
       from:'', to:'', preset:'thisMonth', q:'', status:'', tagId:'' },
};
const accById  = id => S.accounts.find(a=>a.id===id);
let jrGroupMonth = false;
const compById = id => S.companies.find(c=>c.id===id);
const rateOf   = cur => S.rates[cur] ?? 1;
const accRate  = a  => rateOf(a.currency);

/* ============ 弹窗 ============ */
const overlay = $('#modalOverlay'), modalBox = $('#modalBox');
function openModal(html){ modalBox.innerHTML = html; overlay.classList.remove('hidden'); }
function closeModal(){ overlay.classList.add('hidden'); modalBox.innerHTML=''; modalBox.style.width=''; }
overlay.addEventListener('mousedown', e=>{ if(e.target===overlay) closeModal(); });
document.addEventListener('keydown', e=>{
  if(e.altKey && !e.ctrlKey && (e.key === 'a' || e.key === 'A')){
    e.preventDefault(); agentToggle(); return;
  }
  if(e.key === 'Escape'){
    if(!overlay.classList.contains('hidden')){ closeModal(); return; }
    if($('#agentDock')?.classList.contains('open')) agentClose();
  }
});
const modalHead = t => `<div class="modal-title">${t}<button class="modal-close" data-action="modal-close">✕</button></div>`;

/* ============ 导航 ============ */
const TITLES = {dashboard:'总览看板', journal:'日记账', cash:'现金管理',
                deposits:'定期存款', tags:'标签管理', orgs:'公司与账户',
                settings:'设置'};
const VIEW_KEY = 'jb.view', CO_KEY = 'jb.company', JR_KEY = 'jb.jr';
/* 刷新后停在原页面：视图 + 公司 + 日记账筛选条件 */
function viewStateSave(){
  try{ localStorage.setItem(CO_KEY, String(S.company ?? '')); }catch(e){}
  try{
    const j = S.jr;
    sessionStorage.setItem(JR_KEY, JSON.stringify({
      accId:j.accId, from:j.from, to:j.to, preset:j.preset,
      q:j.q, status:j.status, tagId:j.tagId}));
  }catch(e){}
}
function viewStateRestore(){
  try{
    const cid = +(localStorage.getItem(CO_KEY) || 0);
    if(cid && S.companies.some(c=>c.id===cid)) S.company = cid;
  }catch(e){}
  try{
    const j = JSON.parse(sessionStorage.getItem(JR_KEY) || 'null');
    if(j && typeof j === 'object') Object.assign(S.jr, j, {loaded:false});
  }catch(e){}
}
function switchView(v, opt){
  S.view = v;
  $$('#nav .nav-btn').forEach(b=>b.classList.toggle('active', b.dataset.view===v));
  $('#viewTitle').textContent = TITLES[v]||v;
  try{ localStorage.setItem(VIEW_KEY, v); }catch(e){}
  if(!(opt && opt.nohash) && location.hash !== '#'+v){
    try{ history.replaceState(null, '', '#'+v); }catch(e){ location.hash = v; }
  }
  viewStateSave();
  ({dashboard:renderDashboard, journal:renderJournal, cash:renderCash,
    deposits:renderDeposits, tags:renderTags, orgs:renderOrgs,
    settings:renderSettings})[v]();
}
/* 账本被改过：把带缓存的数据丢掉再重画当前页 */
function invalidateCaches(){ S.jr.loaded = false; }
async function refreshAfterWrite(){
  await refreshBootstrap();
  invalidateCaches();
  try{ await switchView(S.view, {nohash:true}); }catch(e){}
}

/* ============ 启动 ============ */
async function boot(){
  await refreshBootstrap();
  bindStatic();
  viewStateRestore();
  S.agent.msgs = agentLoadMsgs();
  await initAgentDock();
  // #journal / #cash 这类锚点可直接打开对应页面；否则回到上次停留的页面
  const hash = (location.hash || '').replace('#', '');
  const last = (()=>{ try{ return localStorage.getItem(VIEW_KEY) || ''; }catch(e){ return ''; } })();
  const init = TITLES[hash] ? hash : (TITLES[last] ? last : 'dashboard');
  switchView(init);
  await loadReminders();
  if(activeReminders().length && overlay.classList.contains('hidden')) openReminderModal();
}

/* ============ 标签/备注 待办提醒（侧栏按钮 + 弹窗） ============ */
async function loadReminders(){
  try{ S.reminders = await api('/api/notes/reminders'); }
  catch(e){ S.reminders = []; }
  renderRemBadge();
}
function activeReminders(){ return S.reminders||[]; }
function renderRemBadge(){
  const btn = $('[data-action="reminders"]');
  if(!btn) return;
  const n = activeReminders().length;
  let b = btn.querySelector('.nav-badge');
  if(!n){ if(b) b.remove(); return; }
  if(!b){ b=document.createElement('span'); btn.appendChild(b); }
  b.className='nav-badge';
  b.textContent=n;
  b.title = `${n} 笔待跟进（标签/备注）`;
}
function reminderGroups(list){
  const cMap = new Map();
  for(const r of list){
    const cid = r.company_id;
    if(!cMap.has(cid)) cMap.set(cid, {company:r.company_name, items:[]});
    cMap.get(cid).items.push(r);
  }
  return [...cMap.values()].map(c=>{
    const tMap = new Map();
    for(const r of c.items){
      const tag = (r.tags||[])[0];
      const key = tag ? 't'+tag.id : 'note';
      if(!tMap.has(key)) tMap.set(key, {tag, items:[]});
      tMap.get(key).items.push(r);
    }
    return {company:c.company, items:c.items,
      groups:[...tMap.values()].sort((a,b)=>
        b.items.length-a.items.length ||
        (a.tag?.name||'').localeCompare(b.tag?.name||'', 'zh'))};
  }).sort((a,b)=>
    b.items.length-a.items.length ||
    (a.company||'').localeCompare(b.company||'', 'zh'));
}
function reminderModalHTML(list){
  if(!list.length){
    return modalHead('🔔 待办提醒')+`
      <div class="hint" style="padding:18px 0;text-align:center">当前没有带标签或备注的待跟进流水。</div>
      <div class="modal-actions"><button class="btn" data-action="modal-close">关闭</button></div>`;
  }
  return modalHead(`🔔 待办提醒（${list.length} 笔）`)+`
    <div class="hint mb">先按公司、再按标签分类；带备注但无标签的归入“备注提醒”。勾选“不再提示”会先二次确认，确认后清空该笔的备注与标签。</div>
    <div id="remList" style="max-height:62vh;overflow:auto">
    ${reminderGroups(list).map(c=>`
      <div class="rem-company">
        <div class="rem-company-head">🏢 ${esc(c.company)}<span class="muted small">${c.items.length} 笔</span></div>
        ${c.groups.map(g=>`
          <div class="rem-group">
            <div class="rem-group-head">
              ${g.tag?`<span class="tagchip" style="background:${g.tag.color}">${esc(g.tag.name)}</span>`
                     :'<span class="tagchip" style="background:#f59e0b">📝 备注提醒</span>'}
              <span class="muted small">${g.items.length} 笔</span>
            </div>
            ${g.items.map(r=>`
              <div class="note-remind-item" data-rid="${r.id}">
                <div class="nr-info">
                  <div class="nr-line"><b>${esc(r.date)}</b>　<span class="muted">${esc(r.account_name)}</span></div>
                  <div class="nr-line">${esc(r.description)}</div>
                  <div class="nr-line nr-note">${tagChips(r.tags)}${r.note?`<span>📝 ${esc(r.note)}</span>`:''}</div>
                </div>
                <button class="btn sm primary" data-action="note-goto" data-id="${r.id}" data-acc="${r.account_id}" data-company="${r.company_id}">查看流水</button>
                <label class="small" title="确认后将清空该笔的备注和标签" style="display:flex;align-items:center;gap:5px;cursor:pointer;white-space:nowrap">
                  <input type="checkbox" data-change="note-dismiss" data-id="${r.id}" style="width:15px;height:15px">不再提示</label>
              </div>`).join('')}
          </div>`).join('')}
      </div>`).join('')}
    </div>
    <div class="modal-actions"><button class="btn" data-action="modal-close">关闭</button></div>`;
}
function renderReminderModal(){
  const list = activeReminders();
  modalBox.innerHTML = reminderModalHTML(list);
  renderRemBadge();
  const box = $('#remList');
  if(!box) return;
  box.addEventListener('change', async ev=>{
    const cb = ev.target.closest('[data-change="note-dismiss"]');
    if(!cb) return;
    if(!cb.checked) return;
    const r = list.find(x=>x.id===+cb.dataset.id);
    if(!r) return;
    if(!confirm('勾选“不再提示”将同时清空该笔流水的备注和标签，且无法撤销。\n\n确定清除并移除提醒？')){
      cb.checked = false; return;
    }
    try{
      await jpost('/api/entries/'+r.id+'/clear-marks', {});
    }catch(err){
      cb.checked = false; alert('清除失败：'+err.message); return;
    }
    await loadReminders();
    renderReminderModal();
  });
}
function openReminderModal(){
  overlay.classList.remove('hidden');
  renderReminderModal();
}
async function refreshBootstrap(){
  const b = await api('/api/bootstrap');
  S.companies=b.companies; S.accounts=b.accounts; S.tags=b.tags;
  S.rates=b.rates; S.today=b.today; S.range=b.entry_range||null;
  S.depCounts=b.dep_counts||{};
  S.depAlerts = b.dep_alerts || {due:0, soon:0};
  if(!S.companies.find(c=>c.id===S.company)) S.company=S.companies[0]?.id ?? null;
  const sel=$('#companySelect');
  sel.innerHTML = S.companies.map(c=>`<option value="${c.id}" ${c.id===S.company?'selected':''}>${esc(c.name)}</option>`).join('')
    || '<option value="">（请先在"公司与账户"新建公司）</option>';
  renderDepBadge();
}
/* 侧栏"定期存款"到期数量角标 */
function renderDepBadge(){
  const btn = $('[data-view="deposits"]');
  if(!btn) return;
  const due = S.depAlerts?.due || 0, soon = S.depAlerts?.soon || 0;
  let b = btn.querySelector('.nav-badge');
  if(!due && !soon){ if(b) b.remove(); return; }
  if(!b){ b = document.createElement('span'); btn.appendChild(b); }
  b.className = 'nav-badge' + (due ? ' danger' : '');
  b.textContent = due + soon;
  b.title = `${due} 笔已到期、${soon} 笔 30 天内到期`;
}
function bindStatic(){
  $('#companySelect').addEventListener('change', e=>{
    S.company = Number(e.target.value)||null;
    if(S.view==='journal' && accById(S.jr.accId)?.company_id!==S.company){
      S.jr = {accId:null,account:null,entries:[],loaded:false,from:'',to:'',preset:'thisMonth',q:'',status:'',tagId:''};
    }
    switchView(S.view);
  });
  $('#globalSearch').addEventListener('keydown', e=>{
    if(e.key==='Enter' && e.target.value.trim()) globalSearch(e.target.value.trim());
  });
}

/* ============ 通用小部件 ============ */
function tagChips(tags, sel=false){
  return (tags||[]).map(t=>
    `<span class="tagchip ${sel?'sel':''}" style="background:${t.color}" title="${esc(t.name)}">${esc(t.name)}</span>`).join('');
}
function curChip(cur){ return `<span class="cur-chip">${esc(cur)}</span>`; }
function badge(entry){
  return entry.pending ? '<span class="badge pend">未兑现/未到账</span>'
                       : '<span class="badge ok">已入账</span>';
}

/* ================================================================
   总览看板
================================================================ */
async function renderDashboard(){
  const c = $('#content');
  if(!S.company){ c.innerHTML = '<div class="card empty-hint">请先在"公司与账户"中新建公司</div>'; return; }
  const d = await api('/api/dashboard?company_id='+S.company);
  const comp = compById(S.company);
  const rate = a => accRate(a);
  const cashAccs  = d.accounts.filter(a=>(a.kind||'bank')==='cash');
  const cashMop   = cashAccs.reduce((s,a)=>s+a.book*rate(a),0);
  const totalBook = d.accounts.reduce((s,a)=>s+a.book*rate(a),0);
  const totalBank = d.accounts.filter(a=>(a.kind||'bank')!=='cash')
                    .reduce((s,a)=>s+a.bank*rate(a),0);
  const pendOut   = d.accounts.reduce((s,a)=>s+a.pending_credit*rate(a),0);
  const pendIn    = d.accounts.reduce((s,a)=>s+a.pending_debit*rate(a),0);
  const depTotal  = d.deposits.reduce((s,x)=>s+x.principal*rateOf(x.currency),0);
  // 名称含「定期」的账户，其余额本身就是存单本金，不能再叠加存单本金，否则资产翻倍
  const depAccs   = d.accounts.filter(a=>/定期/.test(a.name||''));
  const depAccSum = depAccs.reduce((s,a)=>s+a.book*rate(a),0);
  const assetBook = totalBank + cashMop + (depAccs.length ? 0 : depTotal);

  // 月度收支（折 MOP，近 6 个月）
  const accMap = Object.fromEntries(S.accounts.map(a=>[a.id,a]));
  const m = {};
  for(const r of d.monthly){
    const a = accMap[r.account_id]; if(!a) continue;
    const o = m[r.month] ?? (m[r.month]={in:0,out:0});
    o.in += r.debit*rate(a); o.out += r.credit*rate(a);
  }
  const months = Object.keys(m).sort().slice(-6);
  const mrows = months.map(k=>({month:k, in:m[k].in, out:m[k].out}));

  const tagNames = Object.fromEntries(S.tags.map(t=>[t.id,t]));
  const alertTags = Object.entries(d.tag_counts).map(([id,n])=>({...tagNames[id]||{}, id:+id, n}))
                          .filter(t=>t.n>0 && t.name);
  // 定期到期提醒（已到期 + 30天内到期）
  const depAlerts = d.deposits.map(x=>({...x, st:depStatus(x)}))
                    .filter(x=>x.st.level>0).sort((a,b)=>a.st.left-b.st.left);
  const nextDue = d.deposits.map(x=>({...x, st:depStatus(x)}))
                  .filter(x=>x.st.level===0 && x.st.left>=0).sort((a,b)=>a.st.left-b.st.left)[0];

  c.innerHTML = `
  <div class="kpis">
    <div class="kpi"><div class="k-label">总资产（折 MOP）</div>
      <div class="k-value">MOP ${fmtI(assetBook)}</div>
      <div class="k-sub">银行实际 ${fmtI(totalBank)}${cashMop?` ＋ 库存现金 ${fmtI(cashMop)}`:''}${depAccs.length
        ? `（其中定期户 ${fmtI(depAccSum)}，与存单不重复）`
        : (depTotal?` ＋ 定期 ${fmtI(depTotal)}`:'')}</div></div>
    <div class="kpi"><div class="k-label">银行余额（实际，未扣未兑现支票）</div>
      <div class="k-value green">MOP ${fmtI(totalBank)}</div>
      <div class="k-sub">账面 ${fmtI(totalBook-cashMop)} ＋ 未兑现支票 ${fmtI(pendOut)} － 未到账收入 ${fmtI(pendIn)}</div></div>
    <div class="kpi"><div class="k-label">库存现金（${cashAccs.length} 个现金账户）</div>
      <div class="k-value amber">MOP ${fmtI(cashMop)}</div>
      <div class="k-sub">${cashAccs.length
        ? cashAccs.map(a=>`${esc(a.currency)} ${fmt(a.book)}`).join('　')
          + (d.cash?.last_count?`　最近盘点 ${esc(d.cash.last_count.count.count_date)}`:'')
        : '还没有现金账户'}　<a href="#" data-action="goto" data-view="cash">盘点 →</a></div></div>
    <div class="kpi"><div class="k-label">未兑现支票（待银行扣款）</div>
      <div class="k-value amber">MOP ${fmtI(pendOut)}</div>
      <div class="k-sub">另有未到账收入 MOP ${fmtI(pendIn)}</div></div>
    <div class="kpi"><div class="k-label">定期存款（${d.deposits.length} 笔）</div>
      <div class="k-value">MOP ${fmtI(depTotal)}</div>
      <div class="k-sub">${depAlerts.length?`<b class="red">${depAlerts.filter(x=>x.st.level===2).length} 笔已到期</b>${depAlerts.some(x=>x.st.level===1)?`　<b class="amber">${depAlerts.filter(x=>x.st.level===1).length} 笔即将到期</b>`:''}`:(nextDue?`最近到期：${esc(nextDue.end_date)}（${nextDue.st.left} 天后）`:'全部存续中')}　<a href="#" data-action="goto" data-view="deposits">明细 →</a></div></div>
  </div>

  ${alertTags.length?`<div class="card mb"><div class="row spread"><b>🏷️ 状态标记提醒</b>
    <span class="hint">点击查看对应流水</span></div>
    <div class="mt">${alertTags.map(t=>`<span class="tagchip" style="background:${t.color};cursor:pointer" data-action="tag-alert" data-id="${t.id}">${esc(t.name)} × ${t.n}</span>`).join('')}</div></div>`:''}

  ${depAlerts.length?`<div class="card mb alert-bar" style="display:block">
    <div class="row spread"><b>⏰ 定期存款到期提醒</b>
    <span class="hint">点击任一行查看定期明细并办理转存</span></div>
    <div class="mt">
    ${depAlerts.map(x=>`<div class="plan-item" data-action="goto" data-view="deposits" style="cursor:pointer">
      <span class="tagchip" style="background:${x.st.level===2?'#dc2626':'#f59e0b'}">${x.st.level===2?'已到期':'即将到期'}</span>
      <span style="white-space:nowrap;font-weight:600">${esc(x.end_date)}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.bank)} ${esc(x.order_no)} · ${esc(x.currency)} ${fmt(x.principal)}</span>
      <span class="num muted">${x.st.level===2?`已过期 ${-x.st.left} 天`:`${x.st.left} 天后到期`}</span>
      <b class="num">到期利息 ${fmt(depInterestOf(x))}</b>
    </div>`).join('')}
    </div></div>`:''}

  <div class="section-title">🏦 ${esc(comp?.name||'')} · 账户余额</div>
  <div class="grid-cards">
    ${d.accounts.map(a=>{
      const isCash = (a.kind||'bank')==='cash';
      return `
      <div class="card">
        <div class="row spread">
          <b>${isCash?'💵 ':''}${esc(a.name)}</b><span class="cur-chip">${esc(a.currency)}</span>
        </div>
        <div class="small muted" style="margin:3px 0 8px">${esc(a.bank)} ${esc(a.account_no)}</div>
        <div style="font-size:21px;font-weight:700" class="num ${isCash?'':'green'}">${fmt(a.bank)}</div>
        <div class="small muted num">${isCash?'账面余额':'银行余额（实际）'}</div>
        <div class="small mt">
          ${a.pending_credit>0?`未兑现支票<b class="num amber" style="float:right">－${fmt(a.pending_credit)}</b><br>`:''}
          ${a.pending_debit>0?`未到账收入<b class="num green" style="float:right">＋${fmt(a.pending_debit)}</b><br>`:''}
          ${(a.pending_credit>0||a.pending_debit>0)?`预计余额（本币）<b class="num" style="float:right">${fmt(a.book)}</b><br>`:''}
          折 MOP<b class="num" style="float:right">${fmtI(a.book*rate(a))}</b>
        </div>
        <div class="row mt" style="gap:8px">
          <button class="btn sm" data-action="goto-journal" data-id="${a.id}">查看流水</button>
          ${isCash
            ? `<button class="btn sm" data-action="cash-add" data-cur="${esc(a.currency)}">盘点</button>`
            : `<button class="btn sm" data-action="planner" data-id="${a.id}">资金规划</button>`}
        </div>
      </div>`;}).join('') || '<div class="card empty-hint">该公司还没有账户，去"公司与账户"添加</div>'}
  </div>

  <div class="section-title">📈 近 ${mrows.length} 个月收支（折 MOP）</div>
  <div class="card">${barChart(mrows)}
    <div class="chart-legend"><span><span class="legend-dot" style="background:#10b981"></span>收入（借方）</span>
    <span><span class="legend-dot" style="background:#ef4444"></span>支出（贷方）</span></div>
  </div>

  <div class="section-title">💰 定期存款概览</div>
  <div class="card" style="padding:0">
    <table class="tbl"><thead><tr>
      <th>银行</th><th>定单号</th><th>币别</th><th class="num">本金</th><th>存期</th>
      <th class="num">年利率</th><th>到期日</th><th class="num">到期利息</th><th>状态</th>
    </tr></thead><tbody>
    ${d.deposits.map(x=>{
      const days = depDays(x);
      const it = depInterestOf(x);
      const st = depStatus(x);
      return `<tr class="${st.row}"><td>${esc(x.bank)}</td><td>${esc(x.order_no)}</td><td>${esc(x.currency)}</td>
        <td class="num">${fmt(x.principal)}</td><td>${esc(x.period)}</td>
        <td class="num">${(x.rate*100).toFixed(2)}%</td><td>${esc(x.end_date)}</td>
        <td class="num">${fmt(it)}</td>
        <td>${st.label}</td></tr>`;
    }).join('') || '<tr><td colspan="9" class="empty-hint">无定期存款记录</td></tr>'}
    </tbody></table>
  </div>`;
}

function barChart(rows){
  const W=680,H=230,padL=58,padB=26,padT=16,gap=8,bw=26;
  if(!rows.length) return '<div class="empty-hint">暂无流水数据</div>';
  const max = Math.max(1,...rows.flatMap(r=>[r.in,r.out]));
  const slot=(W-padL)/rows.length;
  const y=v=>padT+(H-padT-padB)*(1-v/max);
  let s='';
  for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);
    s+=`<line x1="${padL}" x2="${W-8}" y1="${yy}" y2="${yy}" stroke="#e2e8f0"/>
        <text x="${padL-8}" y="${yy+4}" text-anchor="end" font-size="10" fill="#94a3b8">${fmtI(v)}</text>`;}
  rows.forEach((r,i)=>{
    const cx=padL+slot*i+slot/2;
    const x1=cx-gap-bw,x2=cx+gap;
    s+=`<rect x="${x1}" y="${y(r.in)}" width="${bw}" height="${Math.max(0,H-padB-y(r.in))}" rx="3" fill="#10b981"><title>收入 MOP ${fmt(r.in)}</title></rect>
        <rect x="${x2}" y="${y(r.out)}" width="${bw}" height="${Math.max(0,H-padB-y(r.out))}" rx="3" fill="#ef4444"><title>支出 MOP ${fmt(r.out)}</title></rect>
        <text x="${cx}" y="${H-8}" text-anchor="middle" font-size="11" fill="#475569">${r.month.replace('-','/')}${i===rows.length-1?'':''}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${s}</svg>`;
}

/* ================================================================
   现金管理（库存现金 + 盘点表）
================================================================ */
const CASH_DENOMS = [1000,500,100,50,20,10,5,2,1,0.5,0.2,0.1];
const CUR_LABEL = {MOP:'澳门币 MOP', HKD:'港币 HKD', CNY:'人民币 CNY'};
let cashDate = '';            // 余额／新建盘点的默认日期
const ccNum = v => { const n = parseFloat(v); return isFinite(n) ? n : 0; };

async function renderCash(){
  const c = $('#content');
  if(!S.company){ c.innerHTML = '<div class="card empty-hint">请先在"公司与账户"中新建公司</div>'; return; }
  if(!cashDate) cashDate = S.today;
  const cashAccs = S.accounts.filter(a=>a.company_id===S.company && a.kind==='cash');
  const [book, counts] = await Promise.all([
    api(`/api/cash/book?company_id=${S.company}&date=${cashDate}`),
    api(`/api/cash/counts?company_id=${S.company}`),
  ]);
  const books = book.accounts || [];
  const totalMop = books.reduce((s,a)=>s + a.book*rateOf(a.currency), 0);
  const last = counts[0] || null;
  const todayCount = counts.find(x=>x.count_date === cashDate);

  c.innerHTML = `
  <div class="toolbar">
    <label class="hint" style="display:flex;align-items:center;gap:6px">余额截至
      <input type="date" id="cashDate" class="inp" data-change="cash-date" value="${esc(cashDate)}" style="width:140px"></label>
    <span class="sep"></span>
    <button class="btn primary" data-action="cash-add">＋ 新建盘点</button>
    <button class="btn" data-action="cash-export">📥 导出Excel</button>
    <button class="btn" data-action="cash-goto-journal">📖 现金流水</button>
    <span class="hint">盘点按面额录入张数，系统自动算实有金额与差异；每行「复制」可照上一张单快速新建</span>
  </div>

  ${!cashAccs.length ? `<div class="card empty-hint">该公司还没有库存现金账户。去「公司与账户」新增账户，类型选<b>现金</b>（建议按币种各建一个）。</div>` : ''}

  <div class="kpis">
    <div class="kpi"><div class="k-label">库存现金（折 MOP）</div>
      <div class="k-value">MOP ${fmtI(totalMop)}</div>
      <div class="k-sub">${books.length?books.map(a=>`${esc(a.currency)} ${fmt(a.book)}`).join('　'):'暂无现金账户'}</div></div>
    <div class="kpi"><div class="k-label">最近盘点</div>
      <div class="k-value" style="font-size:20px">${last?esc(last.count_date):'—'}</div>
      <div class="k-sub">${last?`差异 <b class="${Math.abs(last.diff_total)>0.005?'red':'green'}">${fmt(last.diff_total)}</b> 折 MOP　${esc(last.person||'')}`:'还没有盘点记录'}</div></div>
    <div class="kpi"><div class="k-label">账面 vs 实有（最近）</div>
      <div class="k-value" style="font-size:20px">${last?fmtI(last.actual_total):'—'}</div>
      <div class="k-sub">${last?`账面 ${fmtI(last.book_total)}　${last.diff_total>0.005?'盘盈':(last.diff_total<-0.005?'盘亏':'账实相符')}`:'—'}</div></div>
  </div>

  <div class="section-title">💵 现金账户余额（截至 ${esc(cashDate||'—')}）</div>
  <div class="grid-cards">
    ${books.map(a=>`
      <div class="card">
        <div class="row spread"><b>${esc(a.name)}</b><span class="cur-chip">${esc(a.currency)}</span></div>
        <div class="small muted" style="margin:3px 0 8px">现金库　　期初 ${esc(a.opening_date||'—')} ${fmt(a.opening)}</div>
        <div style="font-size:21px;font-weight:700" class="num">${fmt(a.book)}</div>
        <div class="small muted num">账面余额　流水 ${a.entry_count} 笔</div>
        <div class="small mt">折 MOP<b class="num" style="float:right">${fmtI(a.book*rateOf(a.currency))}</b></div>
        <div class="row mt" style="gap:8px">
          <button class="btn sm" data-action="cash-acc-journal" data-id="${a.id}">查看流水</button>
          <button class="btn sm" data-action="cash-add" data-cur="${esc(a.currency)}">盘点</button>
        </div>
      </div>`).join('') || '<div class="card empty-hint">暂无现金账户</div>'}
  </div>

  <div class="section-title">📋 现金盘点记录（${counts.length} 张）</div>
  <div class="card" style="padding:0">
    <table class="tbl"><thead><tr>
      <th>盘点日期</th><th>币种</th><th class="num">账面应有</th><th class="num">盘点实有</th>
      <th class="num">差异</th><th class="num">白条抵库</th><th>盘点人</th><th>监盘人</th><th></th>
    </tr></thead><tbody>
    ${counts.map(x=>{
      const d = x.diff_total;
      return `<tr data-action="cash-edit" data-id="${x.id}" style="cursor:pointer">
        <td style="white-space:nowrap"><b>${esc(x.count_date)}</b>${x.count_date===cashDate?' <span class="cur-chip">当日在看</span>':''}</td>
        <td>${x.items.map(i=>`<span class="cur-chip">${esc(i.currency)}</span>`).join(' ')}</td>
        <td class="num">${fmt(x.book_total)}</td>
        <td class="num">${fmt(x.actual_total)}</td>
        <td class="num ${Math.abs(d)>0.005?(d>0?'green':'red'):''}" style="font-weight:${Math.abs(d)>0.005?700:400}">${d>0.005?'+':''}${fmt(d)}</td>
        <td class="num">${x.white_total?fmt(x.white_total):'—'}</td>
        <td>${esc(x.person||'—')}</td>
        <td>${esc(x.supervisor||'—')}</td>
        <td style="white-space:nowrap">
          <button class="btn sm" data-action="cash-edit" data-id="${x.id}">查看/编辑</button>
          <button class="btn sm" data-action="cash-copy" data-id="${x.id}" title="按这张单的币种、面额张数、人员新建一张">复制</button>
          <button class="btn sm danger" data-action="cash-del" data-id="${x.id}">删除</button>
        </td></tr>`;
    }).join('') || '<tr><td colspan="9" class="empty-hint">还没有盘点记录，点右上角「＋ 新建盘点」按面额录入</td></tr>'}
    </tbody></table>
  </div>
  <div class="hint mt">盘点口径：账面应有 = 上一日账面库存 ＋ 未记账传票收入 － 未记账传票付出；盘点实有 = 各面额张数合计；差异 = 实有 － 账面应有（另列白条抵库）。金额为折 MOP 汇总。</div>`;
}

function cashBlockHTML(it){
  const cur = it.currency;
  const qmap = {};
  (it.denoms||[]).forEach(d=>{ qmap[+d.denom] = +d.qty; });
  return `
  <div class="cc-block" data-cur="${esc(cur)}" data-actual="${it.actual||0}">
    <div class="cc-head"><b>${esc(CUR_LABEL[cur]||cur)}</b>
      <span class="cc-badge" data-role="diff">差异 0.00</span></div>
    <div class="cc-fields">
      <label>上一日账面<input class="inp mini cc-f" data-f="prev_book" type="number" step="0.01" value="${it.prev_book||''}"></label>
      <label>未记账收入<input class="inp mini cc-f" data-f="unposted_in" type="number" step="0.01" value="${it.unposted_in||''}"></label>
      <label>未记账付出<input class="inp mini cc-f" data-f="unposted_out" type="number" step="0.01" value="${it.unposted_out||''}"></label>
      <label>白条抵库<input class="inp mini cc-f" data-f="white" type="number" step="0.01" value="${it.white||''}"></label>
      <span class="cc-calc">账面应有<b data-role="book">0.00</b></span>
      <span class="cc-calc">盘点实有<b data-role="actual">0.00</b></span>
    </div>
    <div class="cc-denoms">
      ${CASH_DENOMS.map(d=>`<label class="cc-den"><span>${d}</span>
        <input class="inp mini cc-qty" type="number" min="0" step="1" data-denom="${d}" value="${qmap[d]||''}" placeholder="0">
        <em data-role="amt">0.00</em></label>`).join('')}
    </div>
    <label class="cc-reason">差异原因<input class="inp" data-f="reason" value="${esc(it.reason||'')}" placeholder="如：白条抵库、找零备用金未入账…"></label>
  </div>`;
}

async function openCashCount(id, copyId){
  let head = {count_date:cashDate||S.today, person:'', supervisor:'', note:''};
  let items = [];
  let src = null;                       // 复制来源单
  if(id){
    const d = await api('/api/cash/counts/'+id);
    head = d.count; items = d.items || [];
  }else if(copyId){
    // 复制以新增：带上原单的币种、面额张数、差异原因、人员；日期改今天，账面数按新日期重算
    const d = await api('/api/cash/counts/'+copyId);
    src = d.count;
    head = {count_date:S.today, person:d.count.person || '',
            supervisor:d.count.supervisor || '', note:''};
    items = (d.items || []).map(it=>({...it}));
    const b = await api(`/api/cash/book?company_id=${S.company}&date=${head.count_date}`);
    const bookMap = {};
    (b.accounts||[]).forEach(a=>{ bookMap[a.currency] = a.book; });
    items.forEach(it=>{
      if(bookMap[it.currency] !== undefined) it.prev_book = bookMap[it.currency];
    });
  }else{
    const b = await api(`/api/cash/book?company_id=${S.company}&date=${head.count_date}`);
    items = (b.accounts||[]).map(a=>({currency:a.currency, prev_book:a.book,
      unposted_in:0, unposted_out:0, white:0, reason:'', denoms:[]}));
  }
  if(!items.length){
    alert('该公司还没有库存现金账户，请先在「公司与账户」里新增类型为"现金"的账户。');
    return;
  }
  items.sort((a,b)=>['MOP','HKD','CNY'].indexOf(a.currency)-['MOP','HKD','CNY'].indexOf(b.currency));
  const title = id ? '编辑盘点单'
    : (src ? `复制新建盘点（来源 ${esc(src.count_date)}）` : '新建现金盘点');
  openModal(modalHead(title)+`
    ${src ? `<div class="hint" style="margin:-4px 0 10px">已带上原单的币种、面额张数与人员；账面数按新盘点日期重算，日期和张数请按实际改。改了日期会自动刷新账面。</div>` : ''}
    <form id="ccForm" data-id="${id||''}">
      <div class="form-grid">
        <div class="field"><label>盘点日期 *</label>
          <input class="inp" type="date" name="count_date" value="${esc(head.count_date)}" required></div>
        <div class="field"><label>盘点人</label><input class="inp" name="person" value="${esc(head.person||'')}"></div>
        <div class="field"><label>监盘人</label><input class="inp" name="supervisor" value="${esc(head.supervisor||'')}"></div>
        <div class="field full"><label>备注</label><input class="inp" name="note" value="${esc(head.note||'')}"></div>
      </div>
      <div class="cc-list">${items.map(cashBlockHTML).join('')}</div>
      <div class="cc-total">
        <span>账面应有 <b id="ccBookT">0.00</b></span>
        <span>盘点实有 <b id="ccActT">0.00</b></span>
        <span>差异 <b id="ccDiffT">0.00</b></span>
        <span class="muted small">（折 MOP，按设置里的汇率）</span>
      </div>
      <div class="modal-actions">
        ${id ? `<button type="button" class="btn" data-action="cash-export" data-id="${id}">导出这张</button>
                <button type="button" class="btn" data-action="cash-copy" data-id="${id}">复制为新单</button>` : ''}
        <button type="button" class="btn" data-action="modal-close">取消</button>
        <button type="submit" class="btn primary">保存盘点单</button>
      </div>
    </form>`);
  ccRecalc();
  modalBox.style.width = 'min(1040px, 96vw)';
  $('#ccForm').addEventListener('input', e=>{
    if(e.target.classList.contains('cc-qty') || e.target.classList.contains('cc-f')) ccRecalc();
  });
  // 复制模式下改日期：自动把「上一日账面」刷成新日期的账面余额
  if(src){
    const dateInp = $('#ccForm [name="count_date"]');
    if(dateInp) dateInp.addEventListener('change', async ()=>{
      const d = dateInp.value;
      if(!d) return;
      try{
        const b = await api(`/api/cash/book?company_id=${S.company}&date=${d}`);
        const bookMap = {};
        (b.accounts||[]).forEach(a=>{ bookMap[a.currency] = a.book; });
        $$('#ccForm .cc-block').forEach(blk=>{
          const cur = blk.dataset.cur;
          if(bookMap[cur] === undefined) return;
          const inp = blk.querySelector('[data-f="prev_book"]');
          if(inp) inp.value = bookMap[cur];
        });
        ccRecalc();
      }catch(err){ /* 取不到就保留原值 */ }
    });
  }
  $('#ccForm').addEventListener('submit', async ev=>{
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = {
      company_id: S.company,
      count_date: f.get('count_date'),
      person: f.get('person') || '',
      supervisor: f.get('supervisor') || '',
      note: f.get('note') || '',
      items: $$('#ccForm .cc-block').map(b=>({
        currency: b.dataset.cur,
        prev_book: ccNum(b.querySelector('[data-f="prev_book"]')?.value),
        unposted_in: ccNum(b.querySelector('[data-f="unposted_in"]')?.value),
        unposted_out: ccNum(b.querySelector('[data-f="unposted_out"]')?.value),
        white: ccNum(b.querySelector('[data-f="white"]')?.value),
        actual: ccNum(b.querySelector('[data-role="actual"]')?.textContent.replace(/,/g,'')),
        reason: b.querySelector('[data-f="reason"]')?.value || '',
        denoms: $$('.cc-qty', b).filter(x=>ccNum(x.value)>0)
                  .map(x=>({denom:+x.dataset.denom, qty:ccNum(x.value)})),
      })),
    };
    try{
      if(id) await jput('/api/cash/counts/'+id, body);
      else await jpost('/api/cash/counts', body);
      closeModal(); renderCash();
    }catch(err){ alert(err.message); }
  });
}

/* 盘点表导出：可选期间，默认全部；后端出三个工作表 */
function openCashExport(){
  const cn = compById(S.company)?.name || '';
  openModal(modalHead('📥 导出盘点表')+`
    <form id="ceForm">
      <div class="hint" style="margin:0 0 10px;line-height:1.8">
        导出 <b>${esc(cn)}</b> 的现金盘点表（Excel），含三个工作表：<br>
        <b>盘点汇总</b>（一张单一行，金额折 MOP，含合计）· <b>盘点明细</b>（原币逐币种）·
        <b>面额明细</b>（张数 × 面额）
      </div>
      <div class="form-grid">
        <div class="field"><label>开始日期</label><input class="inp" type="date" name="from"></div>
        <div class="field"><label>结束日期</label><input class="inp" type="date" name="to"></div>
      </div>
      <div class="hint mt">留空＝导出全部盘点记录。</div>
      <div class="modal-actions">
        <button type="button" class="btn" data-action="modal-close">取消</button>
        <button type="submit" class="btn primary">开始导出</button>
      </div>
    </form>`);
  modalBox.style.width = 'min(560px, 92vw)';
  $('#ceForm').addEventListener('submit', ev=>{
    ev.preventDefault();
    const f = new FormData(ev.target);
    const q = new URLSearchParams({company_id:String(S.company)});
    if(f.get('from')) q.set('from', f.get('from'));
    if(f.get('to')) q.set('to', f.get('to'));
    location.href = '/api/export/cash-counts?' + q.toString();
    closeModal();
  });
}

function ccRecalc(){
  const fx = cur => rateOf(cur);
  let tBook = 0, tAct = 0;
  $$('#ccForm .cc-block').forEach(b=>{
    const val = f => ccNum(b.querySelector(`[data-f="${f}"]`)?.value);
    const prev = val('prev_book'), unIn = val('unposted_in'),
          unOut = val('unposted_out');
    let actual = 0;
    $$('.cc-qty', b).forEach(inp=>{
      const d = +inp.dataset.denom, q = ccNum(inp.value);
      const amt = d * q;
      actual += amt;
      const em = inp.parentElement.querySelector('[data-role="amt"]');
      if(em) em.textContent = fmt(amt);
    });
    // 没有任何面额张数时，退回已存的实有金额（老数据/直接填金额）
    if(!actual) actual = ccNum(b.dataset.actual);
    const book = prev + unIn - unOut;
    const diff = actual - book;
    b.querySelector('[data-role="book"]').textContent = fmt(book);
    b.querySelector('[data-role="actual"]').textContent = fmt(actual);
    const badge = b.querySelector('[data-role="diff"]');
    badge.textContent = `差异 ${diff>0?'+':''}${fmt(diff)}`;
    badge.className = 'cc-badge' + (Math.abs(diff)>0.005 ? (diff>0?' up':' down') : '');
    tBook += book*fx(b.dataset.cur); tAct += actual*fx(b.dataset.cur);
  });
  const d = tAct - tBook;
  $('#ccBookT').textContent = fmt(tBook);
  $('#ccActT').textContent = fmt(tAct);
  const el = $('#ccDiffT');
  el.textContent = (d>0?'+':'') + fmt(d);
  el.className = Math.abs(d)>0.005 ? (d>0?'green':'red') : '';
}

/* ================================================================
   日记账
================================================================ */
async function renderJournal(){
  const c = $('#content');
  if(!S.company){ c.innerHTML='<div class="card empty-hint">请先新建公司</div>'; return; }
  // 默认打开＝本月（preset 为 thisMonth 且从未算过范围时补算）
  if(S.jr.preset==='thisMonth' && !S.jr.from && !S.jr.to){
    [S.jr.from, S.jr.to] = presetRange('thisMonth');
  }
  const coAccs = S.accounts.filter(a=>a.company_id===S.company);
  if(!S.jr.accId || !coAccs.find(a=>a.id===S.jr.accId)){
    if(!coAccs.length){ c.innerHTML='<div class="card empty-hint">该公司还没有账户，去"公司与账户"添加</div>'; return; }
    S.jr.accId = coAccs[0].id;
    S.jr.loaded = false;
  }
  if(!S.jr.loaded){
    const d = await api('/api/entries?account_id='+S.jr.accId);
    S.jr.account = d.account;
    let bal = d.account.opening, bbal = d.account.opening;
    for(const e of d.entries){
      bal += e.debit - e.credit; e._bal = bal;
      if(!e.pending) bbal += e.debit - e.credit;
      e._bbal = bbal;
    }
    S.jr.entries = d.entries;
    S.jr.loaded = true;
  }
  const acc = S.jr.account;

  const q = S.jr.q.toLowerCase();
  const inDate = e => (!S.jr.from || e.date >= S.jr.from) && (!S.jr.to || e.date <= S.jr.to);
  const rows = S.jr.entries.filter(e=>{
    if(!inDate(e)) return false;
    if(S.jr.tagId && !e.tags.find(t=>t.id===+S.jr.tagId)) return false;
    if(q && !((e.description||'')+(e.check_no||'')+(e.party||'')+(e.project||'')).toLowerCase().includes(q)) return false;
    return true;
  });
  const bookedRows = rows.filter(e=>!e.pending);
  const pendRows   = rows.filter(e=>e.pending);
  const sumNet = list => list.reduce((s,e)=>s+e.debit-e.credit,0);

  // 期初＝开始日前结转余额；期末＝日期范围末笔后余额（只按日期算，不受筛选影响）
  const openBal = S.jr.from
    ? acc.opening + S.jr.entries.filter(e=>e.date<S.jr.from).reduce((s,e)=>s+e.debit-e.credit,0)
    : null;
  const inRange = S.jr.entries.filter(inDate);
  const endBal = inRange.length ? inRange[inRange.length-1]._bal : (openBal ?? acc.opening);
  const endLabel = S.jr.to || (inRange.length ? inRange[inRange.length-1].date : '');

  // 已入账口径：期初/期末都不含未兑现、未到账的款项（＝银行实际余额）
  const bookedInRange = inRange.filter(e=>!e.pending);
  const openBalBooked = S.jr.from
    ? acc.opening + sumNet(S.jr.entries.filter(e=>!e.pending && e.date<S.jr.from))
    : null;
  const endBalBooked = (openBalBooked ?? acc.opening) + sumNet(bookedInRange);
  const rangeIn = bookedInRange.reduce((s,e)=>s+e.debit,0);
  const rangeOut = bookedInRange.reduce((s,e)=>s+e.credit,0);

  // 按月分组：日期跨度跨月时，可按月插入分组行（月末余额取该月最后一笔已入账流水）
  const canGroup = new Set(inRange.map(e=>e.date.slice(0,7))).size > 1;
  const groupMode = jrGroupMonth && canGroup;
  const byMonth = list => {
    const m = new Map();
    list.forEach(e=>{ const k=e.date.slice(0,7); if(!m.has(k)) m.set(k,[]); m.get(k).push(e); });
    return [...m.entries()];
  };
  const bookedHTML = groupMode
    ? byMonth(bookedRows).map(([mo,list])=>{
        const mIn = list.reduce((s,e)=>s+e.debit,0);
        const mOut = list.reduce((s,e)=>s+e.credit,0);
        const last = list[list.length-1];
        return `<tr class="group-row month-row"><td></td><td colspan="10">📅 ${mo}　共 ${list.length} 笔<span class="hint">收 ${fmt(mIn)}　支 ${fmt(mOut)}</span><span style="float:right">月末实际余额 ${fmt(last._bbal)}</span></td></tr>`
          + list.map(e=>journalRow(e)).join('');
      }).join('')
    : bookedRows.map(e=>journalRow(e)).join('');

  c.innerHTML = `
  <div class="toolbar">
    <select id="jrAcc" class="inp" data-change="jr-acc">
      ${S.companies.map(cp=>`<optgroup label="${esc(cp.name)}">
        ${S.accounts.filter(a=>a.company_id===cp.id).map(a=>
          `<option value="${a.id}" ${a.id===S.jr.accId?'selected':''}>${esc(a.name)}（${esc(a.currency)}）</option>`).join('')}
      </optgroup>`).join('')}
    </select>
    <select id="jrPreset" class="inp" data-change="jr-preset" style="width:auto">
      <option value="all" ${S.jr.preset==='all'?'selected':''}>全部日期</option>
      <option value="thisMonth" ${S.jr.preset==='thisMonth'?'selected':''}>本月</option>
      <option value="lastMonth" ${S.jr.preset==='lastMonth'?'selected':''}>上月</option>
      <option value="thisQuarter" ${S.jr.preset==='thisQuarter'?'selected':''}>本季</option>
      <option value="lastQuarter" ${S.jr.preset==='lastQuarter'?'selected':''}>上季</option>
      <option value="thisYear" ${S.jr.preset==='thisYear'?'selected':''}>今年</option>
      <option value="lastYear" ${S.jr.preset==='lastYear'?'selected':''}>去年</option>
      <option value="custom" ${S.jr.preset==='custom'?'selected':''}>自定义</option>
    </select>
    <input type="date" id="jrFrom" class="inp" data-change="jr-from" value="${S.jr.from}" style="width:135px" title="开始日期（点图标弹日历）">
    <span class="muted">~</span>
    <input type="date" id="jrTo" class="inp" data-change="jr-to" value="${S.jr.to}" style="width:135px" title="结束日期（点图标弹日历）">
    ${canGroup?`<button class="btn${jrGroupMonth?' primary':''}" data-action="jr-month-toggle" title="${jrGroupMonth?'恢复逐笔流水显示':'按月份分组显示'}">${jrGroupMonth?'📄 按流水':'📅 按月分类'}</button>`:''}
    <select id="jrTag" class="inp" data-change="jr-tag" style="width:auto">
      <option value="">全部标签</option>
      ${S.tags.map(t=>`<option value="${t.id}" ${+S.jr.tagId===t.id?'selected':''}>${esc(t.name)}</option>`).join('')}
    </select>
    <input id="jrQ" class="inp" style="width:200px" placeholder="筛选：摘要/单据号…" value="${esc(S.jr.q)}">
    <span class="sep"></span>
    <a class="btn" href="/api/export/excel?type=journal&account_id=${S.jr.accId}&from=${S.jr.from}&to=${S.jr.to}&tag_id=${S.jr.tagId||''}&q=${encodeURIComponent(S.jr.q)}&group=${jrGroupMonth&&canGroup?1:0}">📥 导出Excel</a>
    <button class="btn" data-action="planner" data-id="${S.jr.accId}">🧮 资金规划</button>
    <button class="btn primary" data-action="entry-add">＋ 新增流水</button>
  </div>
  <div class="hint mb">按日期自动排序；表格分两段——<b>已入账</b>在上、<b>未兑现/未到账</b>在下。已入账段的余额与期初期末均为实际余额（不含未兑现/未到账款项）；在未兑现段勾选支票/待到账，可实时看到扣除后的余额。</div>
  <div class="tbl-wrap">
    <table class="tbl" id="jrTable">
      <thead><tr>
        <th class="chk-cell"></th>
        <th>日期</th><th>单据号</th><th>摘要</th>
        <th class="num">收入（借方）</th><th class="num">支出（贷方）</th><th class="num">实际余额</th>
        <th>标签</th><th>凭证</th><th>状态</th><th></th>
      </tr></thead>
      <tbody>
      ${openBalBooked!==null?`
        <tr class="open-row">
          <td></td>
          <td colspan="3">期初实际余额（${esc(S.jr.from)} 前结转）</td>
          <td></td><td></td><td class="num" style="font-weight:700">${fmt(openBalBooked)}</td>
          <td colspan="4"></td>
        </tr>`:''}
      <tr class="group-row"><td></td><td colspan="10">✅ 已入账（${bookedRows.length} 笔）<span class="hint">余额＝实际余额（不含未兑现/未到账）</span></td></tr>
      ${bookedHTML || `<tr><td></td><td colspan="10" class="empty-hint">${rows.length?'当前筛选下没有已入账流水':'暂无流水，点右上角"新增流水"开始记账'}</td></tr>`}
      <tr class="sum-row mid">
        <td></td>
        <td colspan="3">已入账合计<span class="muted small" style="margin-left:10px">共 ${bookedRows.length} 笔（当前筛选）</span></td>
        <td class="num green">收 ${fmt(rangeIn)}</td>
        <td class="num red">支 ${fmt(rangeOut)}</td>
        <td class="num end-amt" id="jrBookedBal">余额 ${fmt(endBalBooked)}</td>
        <td colspan="4"></td>
      </tr>
      <tr class="group-row"><td></td><td colspan="10">未兑现 / 未到账（${pendRows.length} 笔）<span class="hint">勾选＝模拟该笔已兑现/已到账，下方合计实时变化</span></td></tr>
      ${pendRows.map(e=>journalRow(e)).join('') || '<tr><td></td><td colspan="10" class="empty-hint">没有未兑现/未到账的款项</td></tr>'}
      <tr class="sum-row">
        <td></td>
        <td colspan="3">未兑现 / 未到账合计（已勾选 <b id="jrPendN">0</b> / ${pendRows.length} 笔）<span class="muted small" style="margin-left:10px">截至 ${esc(endLabel)||'—'}，扣除后余额</span></td>
        <td class="num green">收 <span id="jrPendIn">0.00</span></td>
        <td class="num red">支 <span id="jrPendOut">0.00</span></td>
        <td class="num end-amt" id="jrPendBal" data-base="${endBalBooked}">余额 <span id="jrPendBalNum">${fmt(endBalBooked)}</span></td>
        <td colspan="4"></td>
      </tr>
      </tbody>
    </table>
  </div>`;
  $('#jrQ').addEventListener('input', debounce(e=>{
    S.jr.q = e.target.value; renderJournalKeepFocus();
  }, 300));
}

function journalRow(e){
  return `
    <tr class="${e.pending?'pendrow':''}" data-action="entry-edit" data-id="${e.id}">
      <td class="chk-cell">${e.pending?`<input type="checkbox" data-change="jr-pend-check" data-id="${e.id}" onclick="event.stopPropagation()">`:''}</td>
      <td style="white-space:nowrap">${esc(e.date)}</td>
      <td style="white-space:nowrap">${esc(e.check_no)}</td>
      <td class="desc-cell">${esc(e.description)}${e.party?`<div class="party">对方：${esc(e.party)}</div>`:''}${e.project?`<div class="party">项目：${esc(e.project)}</div>`:''}</td>
      <td class="num green">${e.debit?fmt(e.debit):''}</td>
      <td class="num red">${e.credit?fmt(e.credit):''}</td>
      <td class="num">${e.pending?'<span class="muted">—</span>':fmt(e._bbal)}</td>
      <td style="max-width:220px">${tagChips(e.tags)}${e.note?`<div class="party" style="margin-top:3px">📝 ${esc(e.note)}</div>`:''}</td>
      <td>${e.attachments.length?`<button class="attach-btn" data-action="entry-att" data-id="${e.id}">📎${e.attachments.length}</button>`:''}</td>
      <td>${badge(e)}</td>
      <td><button class="btn sm danger" data-action="entry-del" data-id="${e.id}">删除</button></td>
    </tr>`;
}

function recalcJournalPend(){
  const cell = $('#jrPendBal'); if(!cell) return;
  const base = parseFloat(cell.dataset.base) || 0;
  let inSum = 0, outSum = 0, n = 0;
  $$('#jrTable [data-change="jr-pend-check"]').forEach(cb=>{
    if(!cb.checked) return;
    const e = S.jr.entries.find(x=>x.id===+cb.dataset.id);
    if(e){ inSum += e.debit; outSum += e.credit; n++; }
  });
  $('#jrPendN').textContent = n;
  $('#jrPendIn').textContent = fmt(inSum);
  $('#jrPendOut').textContent = fmt(outSum);
  const after = base + inSum - outSum;
  const out = $('#jrPendBalNum');
  out.textContent = fmt(after);
  out.style.color = after < base ? 'var(--red,#c0392b)' : (after > base ? 'var(--green,#059669)' : '');
}

function renderJournalKeepFocus(){
  const pos = $('#jrQ')?.selectionStart;
  renderJournal();
  const inp = $('#jrQ');
  if(inp){ inp.focus(); try{ inp.setSelectionRange(pos,pos);}catch(e){} }
}
function debounce(fn, ms){ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); }; }
function presetRange(p){
  const t = S.today || new Date().toISOString().slice(0,10);
  const y=+t.slice(0,4), m=+t.slice(5,7), pad=n=>String(n).padStart(2,'0');
  const ld=(yy,mm)=>new Date(yy,mm,0).getDate();
  if(p==='thisMonth') return [`${y}-${pad(m)}-01`, `${y}-${pad(m)}-${pad(ld(y,m))}`];
  if(p==='lastMonth'){ const pm=m===1?12:m-1, py=m===1?y-1:y;
    return [`${py}-${pad(pm)}-01`, `${py}-${pad(pm)}-${pad(ld(py,pm))}`]; }
  if(p==='thisYear') return [`${y}-01-01`, `${y}-12-31`];
  if(p==='lastYear') return [`${y-1}-01-01`, `${y-1}-12-31`];
  if(p==='thisQuarter' || p==='lastQuarter'){
    let q = Math.floor((m-1)/3)+1, yy = y;
    if(p==='lastQuarter'){ q--; if(q===0){ q=4; yy=y-1; } }
    const sm = (q-1)*3+1, em = sm+2;
    return [`${yy}-${pad(sm)}-01`, `${yy}-${pad(em)}-${pad(ld(yy,em))}`];
  }
  if(p==='all') return ['', ''];
  return ['',''];
}
function monthEndStr(ym){
  const [y,m]=ym.split('-').map(Number);
  return `${y}-${String(m).padStart(2,'0')}-${String(new Date(y,m,0).getDate()).padStart(2,'0')}`;
}
/* ---------- 定期存款通用计算 ---------- */
function parsePeriodMonths(p){
  const m = String(p||'').trim().toUpperCase().match(/^(\d+)\s*([MY])$/);
  return m ? (m[2]==='Y' ? +m[1]*12 : +m[1]) : 0;
}
function addMonths(dateStr, months){
  if(!dateStr || !months) return '';
  const [y,m,d] = dateStr.split('-').map(Number);
  const t = new Date(y, m-1+months, d);
  if(t.getDate() !== d) t.setDate(0);   // 月末溢出时回退到目标月最后一天
  const p = n => String(n).padStart(2,'0');
  return `${t.getFullYear()}-${p(t.getMonth()+1)}-${p(t.getDate())}`;
}
const dayDiff = (a,b) => a && b ? Math.round((new Date(a)-new Date(b))/864e5) : 0;
function depDays(d){ return dayDiff(d.end_date, d.start_date); }
function depInterestOf(d){ const n = depDays(d); return n>0 ? d.principal*d.rate*n/365 : 0; }
/* 到期状态：level 2=已到期/今天 1=30天内 0=存续 */
function depStatus(d){
  if(!d.end_date) return {level:0, left:null, row:'', label:'<span class="badge ok">未设到期日</span>'};
  const left = dayDiff(d.end_date, S.today);
  if(left < 0)    return {level:2, left, row:'due-row',  label:`<span class="badge due">已到期 ${-left} 天</span>`};
  if(left === 0)  return {level:2, left, row:'due-row',  label:'<span class="badge due">今天到期</span>'};
  if(left <= 30)  return {level:1, left, row:'soon-row', label:`<span class="badge soon">${left} 天后到期</span>`};
  return {level:0, left, row:'', label:'<span class="badge ok">存续中</span>'};
}

/* ---------- 新增 / 编辑流水 ---------- */
const tmpTags = new Set();
function openEntryModal(entryId){
  const isNew = !entryId;
  const e = isNew
    ? {id:null, account_id:S.jr.accId, date:S.today, check_no:'', description:'',
       party:'', project:'', debit:'', credit:'', pending:0, note:'', tags:[], attachments:[]}
    : S.jr.entries.find(x=>x.id===entryId);
  if(!e) return;
  tmpTags.clear(); e.tags.forEach(t=>tmpTags.add(t.id));
  const acc = accById(e.account_id) || S.jr.account;
  openModal(modalHead(isNew?'新增流水':`编辑流水 #${e.id}`) + `
    <form id="entryForm" data-eid="${e.id||''}">
      <div class="form-grid">
        <div class="field"><label>日期 *</label><input class="inp" type="date" name="date" value="${e.date}" required></div>
        <div class="field"><label>单据号 / 支票号码</label><input class="inp" name="check_no" value="${esc(e.check_no)}" placeholder="支票号、银行单据号…"></div>
        <div class="field full"><label>摘要 *</label><input class="inp" name="description" value="${esc(e.description)}" required placeholder="例如：付 2026年9月员工工资（梁烈）"></div>
        <div class="field"><label>对方单位</label><input class="inp" name="party" value="${esc(e.party)}"></div>
        <div class="field"><label>资金计划项目</label><input class="inp" name="project" value="${esc(e.project)}"></div>
        <div class="field"><label>收入（借方）${esc(acc.currency)}</label><input class="inp" type="number" step="0.01" min="0" name="debit" value="${e.debit||''}" placeholder="0.00"></div>
        <div class="field"><label>支出（贷方）${esc(acc.currency)}</label><input class="inp" type="number" step="0.01" min="0" name="credit" value="${e.credit||''}" placeholder="0.00"></div>
        <div class="field full">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:var(--txt)">
            <input type="checkbox" name="pending" id="pendChk" ${e.pending?'checked':''} style="width:16px;height:16px">
            未兑现 / 未到账（款项尚未经过银行，不计入银行余额，可在"资金规划"中模拟扣除）
          </label>
        </div>
        <div class="field full"><label style="display:flex;align-items:center;justify-content:space-between">标签（点击选择，可多选）
            <button type="button" class="btn sm" id="tagNewBtn">＋ 新建标签</button></label>
          <div id="tagPicker">${S.tags.map(t=>
            `<span class="tagchip" data-action="tag-toggle" data-id="${t.id}" style="background:${t.color};opacity:${tmpTags.has(t.id)?1:.35};${tmpTags.has(t.id)?'outline:2px solid #0f172a':''}">${esc(t.name)}</span>`).join('') || '<span class="hint">暂无标签，可点右上角"新建标签"创建</span>'}</div>
          <div id="tagNewForm" style="display:none;margin-top:8px;gap:8px;align-items:center">
            <input class="inp" id="tagNewName" placeholder="标签名称" style="width:160px">
            <input type="color" id="tagNewColor" value="#3b82f6" title="标签颜色">
            <button type="button" class="btn sm primary" id="tagNewOk">确定</button>
            <button type="button" class="btn sm" id="tagNewCancel">取消</button>
          </div>
        </div>
        <div class="field full"><label>标签备注（针对本条流水的补充说明，会显示在摘要下方）</label>
          <textarea class="inp" name="note" rows="2" placeholder="例如：款项用途、到期提醒、跟进事项…">${esc(e.note||'')}</textarea></div>
        <div class="field full"><label>电子凭证存档（收据 / 支票影像 / 回单，可拖拽上传）</label>
          <div id="attZone">${attZoneHTML(e)}</div>
        </div>
      </div>
      <div class="modal-actions">
        ${!isNew?'<button type="button" class="btn danger left" data-action="entry-del" data-id="'+e.id+'">删除此流水</button>':''}
        <button type="button" class="btn" data-action="modal-close">取消</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);

  const form = $('#entryForm');
  // 未兑现智能默认：新增时填了支出+单据号 → 自动勾选
  if(isNew){
    const chk=$('#pendChk'); let touched=false;
    chk.addEventListener('change', ()=>touched=true);
    const auto=()=>{
      if(touched) return;
      chk.checked = (parseFloat(form.credit.value)||0)>0 && form.check_no.value.trim().length>0;
    };
    form.credit.addEventListener('input', auto);
    form.check_no.addEventListener('input', auto);
  }
  // 弹窗内直接新建标签
  const renderPicker = () => {
    $('#tagPicker').innerHTML = S.tags.map(t=>
      `<span class="tagchip" data-action="tag-toggle" data-id="${t.id}" style="background:${t.color};opacity:${tmpTags.has(t.id)?1:.35};${tmpTags.has(t.id)?'outline:2px solid #0f172a':''}">${esc(t.name)}</span>`).join('') || '<span class="hint">暂无标签，可点右上角"新建标签"创建</span>';
  };
  const tagForm = $('#tagNewForm');
  $('#tagNewBtn').addEventListener('click', ()=>{
    tagForm.style.display = tagForm.style.display==='none' ? 'flex' : 'none';
    if(tagForm.style.display==='flex') $('#tagNewName').focus();
  });
  $('#tagNewCancel').addEventListener('click', ()=>{
    tagForm.style.display='none'; $('#tagNewName').value='';
  });
  $('#tagNewOk').addEventListener('click', async ()=>{
    const name = $('#tagNewName').value.trim();
    if(!name){ alert('请输入标签名称'); return; }
    try{
      const t = await jpost('/api/tags', {name, color:$('#tagNewColor').value});
      S.tags.push(t); tmpTags.add(t.id); renderPicker();
      tagForm.style.display='none'; $('#tagNewName').value='';
    }catch(err){ alert(err.message); }
  });
  // 附件：编辑态直接上传；新增态先暂存
  let stagedFiles = [];
  const dz = $('#dropzone');
  if(dz){
    dz.addEventListener('click', ()=>$('#attInput').click());
    dz.addEventListener('dragover', ev=>{ev.preventDefault();dz.classList.add('drag');});
    dz.addEventListener('dragleave', ()=>dz.classList.remove('drag'));
    dz.addEventListener('drop', ev=>{ev.preventDefault();dz.classList.remove('drag');
      handleFiles([...ev.dataTransfer.files]);});
    $('#attInput').addEventListener('change', ev=>handleFiles([...ev.target.files]));
  }
  async function handleFiles(files){
    if(!files.length) return;
    if(e.id){
      await uploadFiles(e.id, files);
      const idx = S.jr.entries.findIndex(x=>x.id===e.id);
      if(idx>=0){ S.jr.entries[idx] = (await api('/api/entries?account_id='+e.account_id)).entries.find(x=>x.id===e.id); }
      $('#attZone').innerHTML = attZoneHTML(S.jr.entries[idx]||e);
      bindZone();
    } else {
      stagedFiles.push(...files);
      $('#stagedList').innerHTML = stagedListHTML();
    }
  }
  function stagedListHTML(){
    return stagedFiles.map((f,i)=>`<div class="att-row"><span>📄 ${esc(f.name)}</span>
      <span class="muted small">${(f.size/1024).toFixed(0)} KB</span>
      <button type="button" class="btn sm danger" data-stage-del="${i}">移除</button></div>`).join('');
  }
  modalBox.addEventListener('click', function onStage(ev){
    const b = ev.target.closest('[data-stage-del]');
    if(b){ stagedFiles.splice(+b.dataset.stageDel,1); $('#stagedList').innerHTML = stagedListHTML(); }
  });
  function bindZone(){ /* zone re-rendered, dropzone listeners rebound below */ }
  form.addEventListener('submit', async ev=>{
    ev.preventDefault();
    const fd = new FormData(form);
    const debit = parseFloat(fd.get('debit'))||0, credit = parseFloat(fd.get('credit'))||0;
    if(debit>0 && credit>0){ alert('收入与支出只能填一项'); return; }
    const body = {
      account_id: e.account_id,
      date: fd.get('date'), check_no: fd.get('check_no').trim(),
      description: fd.get('description').trim(), party: fd.get('party').trim(),
      project: fd.get('project').trim(), note: (fd.get('note')||'').trim(),
      debit, credit, pending: form.pending.checked,
      tag_ids: [...tmpTags],
    };
    try{
      let saved;
      if(e.id){ saved = await jput('/api/entries/'+e.id, body); }
      else{
        saved = await jpost('/api/entries', body);
        if(stagedFiles.length) await uploadFiles(saved.id, stagedFiles);
      }
      closeModal();
      await reloadJournal();
      loadReminders();
    }catch(err){ alert(err.message); }
  });
}
function attZoneHTML(e){
  const imgs = (e.attachments||[]).filter(a=>(a.mime||'').startsWith('image/'));
  const others = (e.attachments||[]).filter(a=>!(a.mime||'').startsWith('image/'));
  return `
    ${(e.attachments||[]).map(a=>`
      <div class="att-row">
        ${(a.mime||'').startsWith('image/')?`<img class="att-thumb" src="/api/attachments/${a.id}/file" data-action="att-open" data-id="${a.id}">`:'<span>📄</span>'}
        <span class="name">${esc(a.orig_name)}</span>
        <span class="muted small">${(a.size/1024).toFixed(0)} KB</span>
        <a class="btn sm" href="/api/attachments/${a.id}/file?download=1">下载</a>
        <button type="button" class="btn sm danger" data-action="att-del" data-id="${a.id}">删除</button>
      </div>`).join('')}
    <div id="stagedList"></div>
    <div id="dropzone" class="dropzone">点击选择 或 拖拽文件到此处上传凭证（支持多选）</div>
    <input type="file" id="attInput" multiple style="display:none">`;
}
async function uploadFiles(eid, files){
  const fd = new FormData();
  files.forEach(f=>fd.append('files', f));
  await api(`/api/entries/${eid}/attachments`, {method:'POST', body:fd});
}

async function reloadJournal(){
  S.jr.loaded = false;
  await refreshBootstrap();
  if(S.view==='journal') await renderJournal();
}

/* ---------- 资金规划 ---------- */
async function openPlanner(accId){
  if(S.jr.accId!==accId || !S.jr.loaded){
    const d = await api('/api/entries?account_id='+accId);
    S.jr.account = d.account; S.jr.accId = accId;
    let bal=d.account.opening; for(const e of d.entries){ bal+=e.debit-e.credit; e._bal=bal; }
    S.jr.entries = d.entries; S.jr.loaded = true;
  }
  const acc = accById(accId);
  const pend = S.jr.entries.filter(e=>e.pending);
  const book = S.jr.entries.length ? S.jr.entries[S.jr.entries.length-1]._bal : acc.opening;
  const bank = book + pend.reduce((s,e)=>s+e.credit-e.debit,0);

  openModal(modalHead(`🧮 资金规划 — ${esc(acc.name)}（${esc(acc.currency)}）`)+`
    <div class="plan-sum" style="margin:0 0 12px">
      <div class="plan-row"><span>银行余额（扣除未兑现支票前）</span><span class="big green">${fmt(bank)}</span></div>
      <div class="plan-row"><span>账面余额（全部流水已记，即全部兑现后的余额）</span><span class="big">${fmt(book)}</span></div>
    </div>
    <div class="hint mb">勾选预计近期将兑现/到账的款项，实时模拟扣除后的余额：</div>
    <div>
    ${pend.map(e=>`
      <label class="plan-item">
        <input type="checkbox" data-change="plan-check" data-id="${e.id}">
        <span style="white-space:nowrap">${esc(e.date)}</span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.description)}${e.check_no?` <span class="muted">(${esc(e.check_no)})</span>`:''}</span>
        <b class="num ${e.credit?'red':'green'}">${e.credit?'－':'＋'}${fmt(e.credit||e.debit)}</b>
      </label>`).join('') || '<div class="empty-hint">当前没有未兑现/未到账的款项</div>'}
    </div>
    <div class="plan-sum">
      <div class="plan-row"><span>已勾选 <b id="planN">0</b> 笔，合计影响 <b id="planAmt">0.00</b></span></div>
      <div class="plan-row"><span>模拟兑现后余额（全部勾选后 = 账面余额）</span><span class="big" id="planBal">${fmt(bank)}</span></div>
    </div>
    <div class="modal-actions">
      <button class="btn left sm" data-action="plan-sel" data-v="all">全选</button>
      <button class="btn left sm" data-action="plan-sel" data-v="none">全不选</button>
      <button class="btn primary" data-action="modal-close">完成</button>
    </div>`);

  recalcPlan(bank);
}
function recalcPlan(bank){
  let amt=0, n=0;
  $$('#modalBox [data-change="plan-check"]').forEach(cb=>{
    if(cb.checked){
      const e = S.jr.entries.find(x=>x.id===+cb.dataset.id);
      if(e){ amt += e.credit - e.debit; n++; }
    }
  });
  const impact = -amt; // 勾选=模拟兑现：支出(credit)使余额减少，收入(debit)使余额增加
  $('#planN').textContent = n;
  $('#planAmt').textContent = (impact>=0?'＋':'－')+fmt(Math.abs(impact));
  $('#planAmt').style.color = impact>=0 ? '' : 'var(--red,#c0392b)';
  $('#planBal').textContent = fmt(bank - amt);
}

/* ================================================================
   定期存款
================================================================ */
async function renderDeposits(){
  const c = $('#content');
  if(!S.company){ c.innerHTML='<div class="card empty-hint">请先新建公司</div>'; return; }
  const list = await api('/api/deposits?company_id='+S.company);
  const totals = {};
  list.forEach(d=>totals[d.currency]=(totals[d.currency]||0)+d.principal);
  const dueN = list.filter(d=>depStatus(d).level===2).length;
  const soonN = list.filter(d=>depStatus(d).level===1).length;
  c.innerHTML = `
  <div class="toolbar">
    <span class="hint">定期到期/转存请在此维护；到期利息＝本金×年利率×天数÷365（到期状态按今天 ${esc(S.today)} 计算）</span>
    <span class="sep"></span>
    <a class="btn" href="/api/export/excel?type=deposits&company_id=${S.company}">📥 导出Excel</a>
    <button class="btn primary" data-action="dep-add">＋ 新增定期</button>
  </div>
  ${(dueN||soonN)?`<div class="alert-bar">
    <span>⏰ ${dueN?`<b class="red">${dueN} 笔已到期</b>`:''}${dueN&&soonN?'　':''}${soonN?`<b class="amber">${soonN} 笔 30 天内到期</b>`:''}</span>
    <span class="hint">红/黄底色行即为需关注项，请及时办理转存或支取</span>
  </div>`:''}
  <div class="tbl-wrap" style="max-height:calc(100vh - 210px)">
    <table class="tbl"><thead><tr>
      <th>银行</th><th>定单号</th><th>币别</th><th class="num">本金</th><th>定存期</th>
      <th class="num">年利率</th><th>开始日期</th><th>到期日</th><th class="num">总天数</th>
      <th class="num">到期利息</th><th>状态</th><th>备注</th><th></th>
    </tr></thead><tbody>
    ${list.map(d=>{
      const days = depDays(d);
      const it = depInterestOf(d);
      const st = depStatus(d);
      return `<tr class="${st.row}" data-action="dep-edit" data-id="${d.id}">
        <td>${esc(d.bank)}</td><td>${esc(d.order_no)}</td><td>${esc(d.currency)}</td>
        <td class="num">${fmt(d.principal)}</td><td>${esc(d.period)}</td>
        <td class="num">${(d.rate*100).toFixed(2)}%</td><td>${esc(d.start_date)}</td><td>${esc(d.end_date)}</td>
        <td class="num">${days||''}</td><td class="num">${days?fmt(it):''}</td>
        <td>${st.label}</td>
        <td>${esc(d.note)}</td>
        <td><button class="btn sm danger" data-action="dep-del" data-id="${d.id}">删除</button></td></tr>`;
    }).join('') || '<tr><td colspan="13" class="empty-hint">暂无定期记录</td></tr>'}
    </tbody>
    <tfoot><tr><td colspan="3">合计</td>
      <td class="num" colspan="10">${Object.entries(totals).map(([k,v])=>`${esc(k)} ${fmt(v)}`).join('　·　')||''}</td></tr></tfoot>
    </table>
  </div>`;
}
function depInterest(){
  const f = $('#depForm'); if(!f) return 0;
  const days = dayDiff(f.end_date.value, f.start_date.value);
  return days>0 ? (parseFloat(f.principal.value)||0)*(parseFloat(f.rate.value)||0)*days/365 : 0;
}
function openDepModal(id){
  const isNew=!id;
  const d = isNew
    ? {id:null, bank:'华人银行', order_no:'', currency:'HKD', principal:'', period:'6M',
       rate:'', start_date:S.today, end_date:'', note:''}
    : null;
  const old = isNew?d:null;
  openModal(modalHead(isNew?'新增定期存款':'编辑定期存款')+`
    <form id="depForm" data-did="${id||''}">
      <div class="form-grid">
        <div class="field"><label>银行</label><input class="inp" name="bank" value="${esc(old?old.bank:'')}" placeholder="华人银行"></div>
        <div class="field"><label>定单号</label><input class="inp" name="order_no" value="${esc(old?old.order_no:'')}"></div>
        <div class="field"><label>币别</label><input class="inp" name="currency" value="${esc(old?old.currency:'HKD')}"></div>
        <div class="field"><label>本金</label><input class="inp" type="number" step="0.01" name="principal" value="${old?old.principal:''}"></div>
        <div class="field"><label>定存期（如 6M / 3M / 12M / 1Y）</label><input class="inp" name="period" value="${esc(old?old.period:'')}"></div>
        <div class="field"><label>年利率（如 0.024 = 2.4%）</label><input class="inp" type="number" step="0.0001" name="rate" value="${old?old.rate:''}"></div>
        <div class="field"><label>开始日期</label><input class="inp" type="date" name="start_date" value="${old?old.start_date:''}"></div>
        <div class="field"><label>到期日 <span class="hint">（按存期自动推算，可改）</span></label><input class="inp" type="date" name="end_date" value="${old?old.end_date:''}"></div>
        <div class="field full"><label>备注</label><input class="inp" name="note" value="${esc(old?old.note:'')}"></div>
      </div>
      <div class="plan-sum mt">总天数：<b id="depDays">0</b> 天　·　预计到期利息：<b id="depIt" class="big">0.00</b></div>
      <div class="modal-actions">
        <button type="button" class="btn" data-action="modal-close">取消</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);
  if(!isNew){
    const dep = api('/api/deposits?company_id='+S.company).then(list=>{
      const x = list.find(v=>v.id===id); if(!x) return;
      const f=$('#depForm');
      f.bank.value=x.bank; f.order_no.value=x.order_no; f.currency.value=x.currency;
      f.principal.value=x.principal; f.period.value=x.period; f.rate.value=x.rate;
      f.start_date.value=x.start_date; f.end_date.value=x.end_date; f.note.value=x.note;
      f.dataset.did=x.id; updIt();
    });
  }
  const form = $('#depForm');
  // 到期日：用户未手改时，随"开始日期/定存期"自动推算
  let endTouched = !!(form.end_date.value);
  form.end_date.addEventListener('change', ()=>{ endTouched = true; });
  const autoEnd = ()=>{
    if(endTouched) return;
    const e = addMonths(form.start_date.value, parsePeriodMonths(form.period.value));
    if(e) form.end_date.value = e;
  };
  form.start_date.addEventListener('change', ()=>{ autoEnd(); updIt(); });
  form.period.addEventListener('change', ()=>{ autoEnd(); updIt(); });
  const updIt=()=>{
    const days = dayDiff(form.end_date.value, form.start_date.value);
    $('#depDays').textContent = days>0 ? days : 0;
    $('#depIt').textContent = fmt(depInterest());
  };
  form.addEventListener('input', updIt);
  autoEnd(); updIt();
  form.addEventListener('submit', async ev=>{
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = Object.fromEntries(fd.entries());
    body.company_id = S.company;
    try{
      if(form.dataset.did) await jput('/api/deposits/'+form.dataset.did, body);
      else await jpost('/api/deposits', body);
      closeModal(); renderDeposits();
    }catch(err){ alert(err.message); }
  });
}

/* ================================================================
   标签管理
================================================================ */
async function renderTags(){
  const c = $('#content');
  const list = await api('/api/tags');
  S.tags = list.map(t=>({id:t.id,name:t.name,color:t.color}));
  c.innerHTML = `
  <div class="toolbar">
    <span class="hint">标签用颜色+文字标注特殊状态（如：未收收据、未签字），可给多笔流水打标，看板会统计提醒</span>
    <span class="sep"></span>
  </div>
  <div class="card mb">
    <div class="row">
      <input class="inp" id="newTagName" placeholder="标签名称，如：未收收据" style="width:220px">
      <input type="color" id="newTagColor" value="#f59e0b">
      <button class="btn primary" data-action="tag-add">＋ 新建标签</button>
    </div>
  </div>
  <div class="tag-list">
    ${list.map(t=>`
      <div class="tag-card" data-tid="${t.id}">
        <span class="tag-dot" style="background:${t.color}"></span>
        <input class="inp tag-name" value="${esc(t.name)}" data-role="name">
        <input type="color" value="${t.color}" data-role="color">
        <span class="muted small tag-usage">已标记 ${t.usage_count} 笔</span>
        <span class="tag-spacer"></span>
        <button class="btn sm" data-action="tag-view" data-id="${t.id}">查看流水</button>
        <button class="btn sm" data-action="tag-save" data-id="${t.id}">保存</button>
        <button class="btn sm danger" data-action="tag-del" data-id="${t.id}">删除</button>
      </div>`).join('') || '<div class="card empty-hint">还没有标签，先在上面新建一个</div>'}
  </div>`;
}

/* ================================================================
   公司与账户
================================================================ */
function renderOrgs(){
  const c = $('#content');
  c.innerHTML = `
  <div class="toolbar">
    <span class="hint">每个公司可建多个不同币种的账户；删除公司会连带删除其账户、流水与凭证附件，请谨慎操作</span>
    <span class="sep"></span>
    <button class="btn primary" data-action="comp-add">＋ 新建公司</button>
  </div>
  ${S.companies.map(cp=>{
    const accs = S.accounts.filter(a=>a.company_id===cp.id);
    return `<div class="card mb">
      <div class="org-head">
        <span class="org-name">🏢 ${esc(cp.name)}</span>
        <span class="row">
          <button class="btn sm" data-action="comp-rename" data-id="${cp.id}">重命名</button>
          <button class="btn sm danger" data-action="comp-del" data-id="${cp.id}">删除公司</button>
          <button class="btn sm primary" data-action="acc-add" data-id="${cp.id}">＋ 添加账户</button>
        </span>
      </div>
      <table class="tbl"><thead><tr>
        <th>类型</th><th>账户名称</th><th>币别</th><th>银行</th><th>账号</th><th class="num">期初余额</th>
        <th class="num">流水数</th><th></th></tr></thead><tbody>
        ${accs.map(a=>{
          const n = S.jr.accId===a.id && S.jr.loaded ? S.jr.entries.length : null;
          const isCash = a.kind==='cash';
          return `<tr data-action="goto-journal" data-id="${a.id}">
            <td style="white-space:nowrap">${isCash?'💵 现金':'🏦 银行'}</td>
            <td>${esc(a.name)}</td><td>${esc(a.currency)}</td><td>${esc(a.bank)}</td><td>${esc(a.account_no)}</td>
            <td class="num">${fmt(a.opening)}</td><td class="num">${n??'—'}</td>
            <td><button class="btn sm" data-action="acc-edit" data-id="${a.id}">编辑</button>
                <button class="btn sm danger" data-action="acc-del" data-id="${a.id}">删除</button></td></tr>`;
        }).join('') || '<tr><td colspan="8" class="empty-hint">暂无账户</td></tr>'}
      </tbody></table>
    </div>`;
  }).join('') || '<div class="card empty-hint">还没有公司，点右上角"新建公司"</div>'}`;
}
function promptName(title, def=''){
  return new Promise(res=>{
    openModal(modalHead(title)+`
      <input class="inp" id="pnInp" style="width:100%" value="${esc(def)}">
      <div class="modal-actions"><button class="btn" data-action="modal-close">取消</button>
      <button class="btn primary" id="pnOk">确定</button></div>`);
    const inp=$('#pnInp'); inp.focus(); inp.select();
    $('#pnOk').onclick=()=>{ const v=inp.value.trim(); if(v){closeModal();res(v);} };
    inp.addEventListener('keydown',e=>{ if(e.key==='Enter') $('#pnOk').click(); });
  });
}
async function openAccModal(companyId, accId){
  const a = accId ? accById(accId) :
    {company_id:companyId, name:'', currency:'MOP', bank:'', account_no:'',
     opening:'', kind:'bank', opening_date:''};
  openModal(modalHead(accId?'编辑账户':'添加账户')+`
    <form id="accForm">
      <div class="form-grid">
        <div class="field full"><label>账户名称 *</label><input class="inp" name="name" value="${esc(a.name)}" placeholder="如：澳门华人银行葡币支票户" required></div>
        <div class="field"><label>账户类型</label>
          <select class="inp" name="kind" id="accKind">
            <option value="bank" ${a.kind!=='cash'?'selected':''}>🏦 银行账户</option>
            <option value="cash" ${a.kind==='cash'?'selected':''}>💵 库存现金</option>
          </select></div>
        <div class="field"><label>币别 *</label><input class="inp" name="currency" value="${esc(a.currency)}" placeholder="MOP / HKD / CNY / USD…"></div>
        <div class="field"><label>期初余额</label><input class="inp" type="number" step="0.01" name="opening" value="${a.opening||''}"></div>
        <div class="field"><label>期初日期（现金用）</label><input class="inp" type="date" name="opening_date" value="${esc(a.opening_date||'')}"></div>
        <div class="field"><label>银行</label><input class="inp" name="bank" value="${esc(a.bank)}" placeholder="澳门华人银行 / 现金库"></div>
        <div class="field"><label>账号</label><input class="inp" name="account_no" value="${esc(a.account_no)}"></div>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" data-action="modal-close">取消</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);
  $('#accForm').addEventListener('submit', async ev=>{
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(ev.target).entries());
    try{
      if(accId) await jput('/api/accounts/'+accId, body);
      else await jpost('/api/accounts', {...body, company_id:companyId});
      closeModal(); await refreshBootstrap(); renderOrgs();
    }catch(err){ alert(err.message); }
  });
}

/* ================================================================
   设置
================================================================ */
function renderSettings(){
  const c = $('#content');
  c.innerHTML = `
  <div class="card mb"><div id="agentCfgMount"><span class="muted small">正在读取模型配置…</span></div></div>

  <div class="card mb">
    <b>汇率设置（折澳门币 MOP）</b>
    <div class="hint" style="margin:4px 0 12px">看板与导出按此汇率折算 MOP；留空的币种不计入 MOP 汇总</div>
    <div class="row" id="rateRows">
      ${Object.entries(S.rates).map(([cur,rate])=>`
        <span class="row" style="gap:6px">
          <input class="inp" style="width:80px" value="${esc(cur)}" data-role="rcur" readonly>
          <input class="inp" style="width:100px" type="number" step="0.0001" value="${rate}" data-role="rval">
          <span class="muted small">: 1 MOP</span>
        </span>`).join('')}
      <span class="row" style="gap:6px">
        <input class="inp" id="newCur" placeholder="新币种" style="width:80px">
        <input class="inp" id="newRate" type="number" step="0.0001" placeholder="汇率" style="width:100px">
        <button class="btn sm" data-action="rate-add">添加</button>
      </span>
    </div>
    <div class="mt"><button class="btn primary" data-action="rate-save">保存汇率</button></div>
  </div>

  <div class="card mb">
    <b>数据管理</b>
    <div class="row mt">
      <button class="btn primary" data-action="master-export">📤 全量导出 Excel（可选日期）</button>
      <a class="btn" href="/api/backup">📦 备份（下载 zip）</a>
      <button class="btn" data-action="restore-pick">♻️ 恢复备份</button>
      <input type="file" id="restoreFile" accept=".zip" style="display:none">
    </div>
    <div class="hint mt">
      · 全量导出＝每个公司一个 Excel（公司名_日期_日记账.xlsx）：首页「目录」是公司信息概览＋各账户余额（点账户名跳转），<b>想导出哪几个工作表就勾哪几个账户页/定期存款页，目录会自动只列已导出的账户</b>；选多家公司时打包为 zip；<b>日期可整体选年月/季度，也可给某个账户单独设期间</b>
    </div>
    <div class="hint mt" style="line-height:1.9">
      · 数据全部保存在本机系统文件夹内：data/journal.db（数据库）与 data/uploads/（凭证附件）<br>
      · 换电脑：拷贝整个系统文件夹，或"备份→恢复"<br>
      · 建议每周用"备份"下载一份 zip 存到网盘或U盘
    </div>
  </div>

  <div class="card">
    <b>使用说明</b>
    <div class="hint mt" style="line-height:2">
      · <b>记账</b>：日记账页选账户 → 新增流水，按日期自动排序；填了"单据号+支出"会自动勾选"未兑现"<br>
      · <b>资金规划</b>：勾选未兑现支票模拟扣除，直观看到"扣除前银行余额 / 扣几笔后 / 全扣完（=账面余额）"<br>
      · <b>标签</b>：颜色+文字标注特殊状态（未收收据、未签字…），日记账可按标签筛选，看板自动提醒<br>
      · <b>凭证</b>：每笔流水可上传多份电子存档（收据/回单/影像），编辑流水时拖拽上传，随时下载查看
    </div>
  </div>`;
  mountAgentCfg();
}

/* ---------- 全量导出（每个公司一个 Excel，含目录 + 每账户一页） ---------- */
/* 期间选项：整年 / 四个季度 / 12 个月，选一下就定死整段，不用填起止 */
const MX_PERIODS = [
  ['Y', '全年（1–12 月）'],
  ['Q1', '第 1 季度（1–3 月）'], ['Q2', '第 2 季度（4–6 月）'],
  ['Q3', '第 3 季度（7–9 月）'], ['Q4', '第 4 季度（10–12 月）'],
  ...Array.from({length:12}, (_,i)=>[`M${String(i+1).padStart(2,'0')}`, `${i+1} 月`]),
];
const pad2 = n => String(n).padStart(2,'0');
function mxYearList(){
  const cy = +(S.today||'').slice(0,4) || new Date().getFullYear();
  const lo = Math.min(+(S.range?.min||'').slice(0,4) || cy, cy - 2);
  const hi = Math.max(cy, +(S.range?.max||'').slice(0,4) || cy);
  const out = [];
  for(let y=hi; y>=lo; y--) out.push(y);
  return out;
}
function mxRangeOf(year, period){
  if(String(year)==='ALL') return ['', ''];
  const yy = +year, lastDay = (y,m)=>new Date(y,m,0).getDate();
  if(period==='Y') return [`${yy}-01-01`, `${yy}-12-31`];
  if(period[0]==='Q'){
    const q = +period[1], sm = (q-1)*3+1, em = sm+2;
    return [`${yy}-${pad2(sm)}-01`, `${yy}-${pad2(em)}-${pad2(lastDay(yy,em))}`];
  }
  const mm = +period.slice(1);
  return [`${yy}-${pad2(mm)}-01`, `${yy}-${pad2(mm)}-${pad2(lastDay(yy,mm))}`];
}
function openMasterModal(){
  const today = S.today || '';
  const cy = today.slice(0,4) || String(new Date().getFullYear());
  const cm = today.slice(5,7) || '01';
  const years = mxYearList();
  const sel = (S.mxComps && S.mxComps.length) ? S.mxComps : S.companies.map(c=>c.id);
  const isCustom = !!S.mxCustom;
  let year = S.mxYear ?? cy;
  if(year!=='ALL' && !years.includes(+year)) year = String(years[0] ?? cy);
  const period = S.mxPeriod ?? `M${cm}`;
  const [rf, rt] = isCustom ? [S.mxFrom||'', S.mxTo||'']
                            : mxRangeOf(year, period);
  openModal(modalHead('📤 全量导出')+`
    <div class="hint mb" style="line-height:1.8">
      <b>一个公司一个 Excel</b>：文件名＝公司名_开始日期至结束日期_日记账.xlsx<br>
      首页固定是「目录」；下面<b>勾选的每个账户各出一页</b>，勾选的「定期存款」再出一页。目录会自动跟着勾选调整（只列已导出的账户）。<br>
      选中<b>多家公司</b>时会打包成一个 zip 下载。
    </div>
    <div class="form-grid">
      <div class="field full"><label>导出期间（整体选）</label>
        <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
          <select class="inp" id="mxYear" style="width:110px" ${isCustom?'disabled':''}>
            <option value="ALL" ${year==='ALL'?'selected':''}>全部历史</option>
            ${years.map(y=>`<option value="${y}" ${String(y)===String(year)?'selected':''}>${y} 年</option>`).join('')}
          </select>
          <select class="inp" id="mxPeriod" style="width:190px" ${isCustom?'disabled':''}>
            ${MX_PERIODS.map(([v,t])=>`<option value="${v}" ${v===period?'selected':''}>${t}</option>`).join('')}
          </select>
          <span class="badge" id="mxSpan"></span>
          <button class="btn sm" data-action="mx-custom" id="mxCustomBtn" style="${isCustom?'display:none':''}">✏️ 自定义起止日期</button>
        </div>
        <div id="mxCustomBox" style="${isCustom?'':'display:none'};margin-top:8px">
          <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
            <input class="inp" type="date" id="mxFrom" value="${esc(rf)}" style="width:140px">
            <span class="muted">~</span>
            <input class="inp" type="date" id="mxTo" value="${esc(rt)}" style="width:140px">
            <button class="btn sm" data-action="mx-period-reset">↩ 回到期间选择</button>
            <span class="hint">留空表示不限</span>
          </div>
        </div>
        <div class="hint" style="margin-top:6px" id="mxQuickBox">
          <span style="margin-right:6px">快捷：</span>
          <a href="#" data-action="mx-quick" data-y="${cy}" data-p="M${cm}">本月</a>
          <a href="#" data-action="mx-quick" data-y="${cy}" data-p="Q${Math.floor((+cm-1)/3)+1}">本季</a>
          <a href="#" data-action="mx-quick" data-y="${cy}" data-p="Y">本年</a>
          <a href="#" data-action="mx-quick" data-y="ALL" data-p="Y">全部历史</a>
        </div>
      </div>
      <div class="field full"><label>选择要导出的工作表（勾选哪页就出哪页；每页还可单独设日期）</label>
        <div class="mx-comps">
          ${S.companies.map(cp=>{
            const accs = S.accounts.filter(a=>a.company_id===cp.id);
            return `<div class="mx-cmp-block">
              <label class="mx-cmp">
                <input type="checkbox" class="mx-cb" value="${cp.id}" ${sel.includes(cp.id)?'checked':''}>
                <span style="flex:1;min-width:0">${esc(cp.name)}</span>
                <span class="muted small mx-cmp-n">${accs.length} 个账户</span>
              </label>
              ${(accs.length || depN(cp.id))?`<div class="mx-accs">${accs.map(a=>mxAccRow(a)).join('')}${depN(cp.id)?mxDepRow(cp, depN(cp.id)):''}</div>`:''}
            </div>`;
          }).join('') || '<div class="hint">还没有公司</div>'}
        </div>
        <div class="row" style="gap:6px;margin-top:8px">
          <button class="btn sm" data-action="mx-all">全选</button>
          <button class="btn sm" data-action="mx-none">全不选</button>
          <span class="hint" id="mxCount"></span>
        </div>      </div>
    </div>
    <div class="modal-actions">
      <button class="btn" data-action="modal-close">取消</button>
      <button class="btn primary" data-action="mx-go">📥 开始导出</button>
    </div>`);
  $$('.mx-cb').forEach(cb=>cb.addEventListener('change', ()=>mxToggleCompany(+cb.value, cb.checked)));
  $$('.mx-scb').forEach(cb=>cb.addEventListener('change', ()=>{
    if(cb.dataset.sheet) mxToggleSheet('acc', +cb.dataset.sheet, cb.checked);
    else mxToggleSheet('dep', +cb.dataset.dep, cb.checked);
  }));
  $$('[data-acc-mode]').forEach(selSel=>selSel.addEventListener('change', ()=>{
    const id = +selSel.dataset.accMode;
    if(selSel.value==='custom'){
      S.mxAcc = S.mxAcc || {};
      S.mxAcc[id] = [$('#mxFrom').value, $('#mxTo').value];
    }else{
      if(S.mxAcc) delete S.mxAcc[id];
    }
    mxRenderAcc(id);
  }));
  $$('[data-acc-from],[data-acc-to]').forEach(inp=>inp.addEventListener('change', ()=>{
    const id = +(inp.dataset.accFrom || inp.dataset.accTo);
    S.mxAcc = S.mxAcc || {};
    S.mxAcc[id] = [ $(`[data-acc-from="${id}"]`).value,
                    $(`[data-acc-to="${id}"]`).value ];
    mxRenderAcc(id);
  }));
  mxRefreshAccLabels();
  mxPaint();
  $('#mxYear').addEventListener('change', mxSyncFromSelect);
  $('#mxPeriod').addEventListener('change', mxSyncFromSelect);
  $('#mxFrom').addEventListener('change', mxSpanLabel);
  $('#mxTo').addEventListener('change', mxSpanLabel);
  if(!isCustom) mxSyncFromSelect(); else mxSpanLabel();
}
/* 账户行：默认「跟随整体」，可单独改成自己的起止日期 */
function mxFmt(f, t){
  if(!f && !t) return '全部历史';
  if(!f) return `～${t}`;
  if(!t) return `${f}～`;
  return f.slice(0,4)===t.slice(0,4) ? `${f.slice(5)}～${t.slice(5)}` : `${f}～${t}`;
}
function mxAccRow(a){
  const ov = S.mxAcc?.[a.id];
  const off = (S.mxOff || []).includes(a.id);
  return `<div class="mx-acc ${off?'off':''}" data-acc="${a.id}">
    <label class="mx-sheet">
      <input type="checkbox" class="mx-scb" data-sheet="${a.id}" ${off?'':'checked'}>
      <span class="mx-acc-name" title="${esc(a.name)}">${a.kind==='cash'?'💵 ':''}${esc(a.name)}</span>
    </label>
    <span class="muted small">${esc(a.currency)}</span>
    <select class="inp mini" data-acc-mode="${a.id}">
      <option value="inherit" ${ov?'':'selected'}>跟随整体</option>
      <option value="custom" ${ov?'selected':''}>自定义</option>
    </select>
    <span class="acc-range ${ov?'amber':'muted'}"></span>
    <span class="acc-dates" style="${ov?'':'display:none'}">
      <input class="inp mini" type="date" data-acc-from="${a.id}" value="${ov?esc(ov[0]||''):''}">
      <span class="muted">~</span>
      <input class="inp mini" type="date" data-acc-to="${a.id}" value="${ov?esc(ov[1]||''):''}">
    </span>
  </div>`;
}
/* 定期存款页（每家公司一张）：有存单才显示这一行 */
function mxDepRow(cp, n){
  const off = (S.mxDepOff || []).includes(cp.id);
  return `<div class="mx-acc mx-dep ${off?'off':''}" data-dep="${cp.id}">
    <label class="mx-sheet">
      <input type="checkbox" class="mx-scb" data-dep="${cp.id}" ${off?'':'checked'}>
      <span class="mx-acc-name" title="定期存款明细">💰 定期存款（明细页）</span>
    </label>
    <span class="muted small">${n} 张存单</span>
    <span class="acc-range muted">不受日期范围筛选</span>
  </div>`;
}
function mxRenderAcc(id){
  const row = $(`.mx-acc[data-acc="${id}"]`);
  if(!row) return;
  const ov = S.mxAcc?.[id];
  const dates = row.querySelector('.acc-dates');
  const label = row.querySelector('.acc-range');
  if(ov){
    dates.style.display = '';
    dates.querySelector('[data-acc-from]').value = ov[0] || '';
    dates.querySelector('[data-acc-to]').value = ov[1] || '';
    label.textContent = mxFmt(ov[0], ov[1]);
    label.className = 'acc-range amber';
  }else{
    dates.style.display = 'none';
    label.textContent = '跟随整体 ' + mxFmt($('#mxFrom').value, $('#mxTo').value);
    label.className = 'acc-range muted';
  }
}
function mxRefreshAccLabels(){
  $$('[data-acc-mode]').forEach(s=>mxRenderAcc(+s.dataset.accMode));
}
function mxSyncFromSelect(){
  const cust = $('#mxCustomBox').style.display !== 'none';
  const allYear = $('#mxYear').value === 'ALL';
  $('#mxPeriod').disabled = cust || allYear;
  const [f, t] = mxRangeOf($('#mxYear').value, $('#mxPeriod').value);
  $('#mxFrom').value = f;
  $('#mxTo').value = t;
  mxSpanLabel();
  mxRefreshAccLabels();
}
function mxSpanLabel(){
  const el = $('#mxSpan');
  if(!el) return;
  const f = $('#mxFrom')?.value || '', t = $('#mxTo')?.value || '';
  if(!f && !t){ el.textContent = '全部历史（不限日期）'; return; }
  const n = dayDiff(t, f) + 1;
  el.textContent = `${f} ~ ${t}${n>0?`　共 ${n} 天`:''}`;
}
function mxSetCustom(on){
  S.mxCustom = on;
  $('#mxCustomBox').style.display = on ? '' : 'none';
  $('#mxYear').disabled = on;
  $('#mxPeriod').disabled = on;
  const b = $('#mxCustomBtn');
  if(b) b.style.display = on ? 'none' : '';
  if(!on){
    S.mxYear = $('#mxYear').value;
    S.mxPeriod = $('#mxPeriod').value;
    mxSyncFromSelect();
  } else {
    S.mxFrom = $('#mxFrom').value;
    S.mxTo = $('#mxTo').value;
    mxSpanLabel();
    $('#mxFrom').focus();
  }
}
function mxQuick(y, p){
  if($('#mxCustomBox').style.display !== 'none') mxSetCustom(false);
  $('#mxYear').value = y;
  $('#mxPeriod').value = p;
  S.mxYear = y; S.mxPeriod = p;
  mxSyncFromSelect();
}
/* 该公司有几张存单（0＝不显示「定期存款」页勾选行） */
function depN(cid){
  return +((S.depCounts || {})[cid] || 0);
}
/* 勾选状态只在 DOM 上，S.mxOff / S.mxDepOff 记「被取消」的项，默认全选 */
function mxPaint(){
  S.mxOff = S.mxOff || [];
  S.mxDepOff = S.mxDepOff || [];
  let nAcc = 0, nDep = 0, nComp = 0;
  S.companies.forEach(cp=>{
    const accs = S.accounts.filter(a=>a.company_id===cp.id);
    let any = false;
    accs.forEach(a=>{
      const off = S.mxOff.includes(a.id);
      const cb = $(`.mx-scb[data-sheet="${a.id}"]`);
      if(cb) cb.checked = !off;
      const row = $(`.mx-acc[data-acc="${a.id}"]`);
      if(row) row.classList.toggle('off', off);
      if(!off){ any = true; nAcc++; }
    });
    if(depN(cp.id)){
      const off = S.mxDepOff.includes(cp.id);
      const cb = $(`.mx-scb[data-dep="${cp.id}"]`);
      if(cb) cb.checked = !off;
      const row = $(`.mx-acc[data-dep="${cp.id}"]`);
      if(row) row.classList.toggle('off', off);
      if(!off){ any = true; nDep++; }
    }
    const ccb = $(`.mx-cb[value="${cp.id}"]`);
    if(ccb) ccb.checked = any;
    const blk = ccb ? ccb.closest('.mx-cmp-block') : null;
    const nOn = accs.filter(a=>!S.mxOff.includes(a.id)).length;
    const hint = blk?.querySelector('.mx-cmp-n');
    if(hint) hint.textContent = nOn === accs.length
      ? `${accs.length} 个账户` : `${nOn} / ${accs.length} 个账户`;
    if(any) nComp++;
  });
  const el = $('#mxCount');
  if(el){
    el.textContent = nComp
      ? `已选 ${nComp} 家公司 · ${nAcc} 个账户页${nDep?` · ${nDep} 个定期存款页`:''}`
        + (nComp>1 ? '（打包为 zip）' : '（直接下载 xlsx）')
      : '请至少勾选一个工作表';
  }
}
/* 勾/取消一家公司＝勾/取消它的全部工作表 */
function mxToggleCompany(cid, on){
  S.mxOff = S.mxOff || [];
  S.mxDepOff = S.mxDepOff || [];
  const ids = S.accounts.filter(a=>a.company_id===cid).map(a=>a.id);
  if(on){
    S.mxOff = S.mxOff.filter(x=>!ids.includes(x));
    S.mxDepOff = S.mxDepOff.filter(x=>x!==cid);
  }else{
    S.mxOff = [...new Set([...S.mxOff, ...ids])];
    S.mxDepOff = [...new Set([...S.mxDepOff, cid])];
  }
  mxPaint();
}
function mxToggleSheet(kind, id, on){
  if(kind==='dep'){
    S.mxDepOff = S.mxDepOff || [];
    S.mxDepOff = on ? S.mxDepOff.filter(x=>x!==id) : [...new Set([...S.mxDepOff, id])];
  }else{
    S.mxOff = S.mxOff || [];
    S.mxOff = on ? S.mxOff.filter(x=>x!==id) : [...new Set([...S.mxOff, id])];
  }
  mxPaint();
}
function doMasterExport(){
  const from = $('#mxFrom').value, to = $('#mxTo').value;
  if(from && to && from > to){ alert('整体期间：开始日期不能晚于结束日期'); return; }
  const ids = $$('.mx-cb').filter(cb=>cb.checked).map(cb=>+cb.value);
  if(!ids.length){ alert('请至少勾选一家公司（或它下面的某个工作表）'); return; }
  // 只导出勾选的工作表
  const off = S.mxOff || [], depOff = S.mxDepOff || [];
  const sheets = S.accounts
    .filter(a=>ids.includes(a.company_id) && !off.includes(a.id))
    .map(a=>a.id);
  const deps = ids.filter(id=>depN(id) && !depOff.includes(id));
  if(!sheets.length && !deps.length){ alert('请至少勾选一个工作表（账户页或定期存款页）'); return; }
  // 账户单独日期：只提交真正会导出的账户
  const accPairs = [];
  for(const [id, v] of Object.entries(S.mxAcc || {})){
    const aid = +id;
    const acc = S.accounts.find(a=>a.id===aid);
    if(!acc || !ids.includes(acc.company_id) || !sheets.includes(aid)) continue;
    if(!v || (!v[0] && !v[1])) continue;
    if(v[0] && v[1] && v[0] > v[1]){
      alert(`「${acc.name}」的开始日期不能晚于结束日期`); return;
    }
    accPairs.push(`${aid}:${v[0]||''}:${v[1]||''}`);
  }
  S.mxFrom = from; S.mxTo = to;
  S.mxComps = ids;
  S.mxOff = off; S.mxDepOff = depOff;
  if($('#mxCustomBox').style.display !== 'none'){
    S.mxCustom = true;
  } else {
    S.mxCustom = false;
    S.mxYear = $('#mxYear').value;
    S.mxPeriod = $('#mxPeriod').value;
  }
  const q = new URLSearchParams({type:'master', companies: ids.join(',')});
  q.set('sheets', sheets.join(',') || '0');
  q.set('dep', deps.join(',') || '0');
  if(from) q.set('from', from);
  if(to) q.set('to', to);
  if(!from && !to) q.set('all', '1');   // 明确表示「全部历史」，否则后端会默认本月
  if(accPairs.length) q.set('acc', accPairs.join(';'));
  closeModal();
  location.href = '/api/export/excel?' + q.toString();
}

/* ---------- 全局搜索 ---------- */
async function globalSearch(q){
  const list = await api('/api/search?q='+encodeURIComponent(q));
  openModal(modalHead(`🔍 “${esc(q)}” 的搜索结果（${list.length} 笔）`)+`
    <div style="max-height:60vh;overflow:auto">
    <table class="tbl"><thead><tr>
      <th>日期</th><th>公司</th><th>账户</th><th>单据号</th><th>摘要</th>
      <th class="num">收入</th><th class="num">支出</th><th>状态</th><th>标签</th></tr></thead><tbody>
    ${list.map(e=>`
      <tr data-action="goto-entry" data-acc="${e.account_id}" data-month="${e.date.slice(0,7)}" data-eid="${e.id}">
        <td style="white-space:nowrap">${esc(e.date)}</td>
        <td>${esc(e.company_name||'')}</td><td>${esc(e.account_name||'')}</td>
        <td>${esc(e.check_no)}</td>
        <td class="desc-cell">${esc(e.description)}</td>
        <td class="num green">${e.debit?fmt(e.debit):''}</td>
        <td class="num red">${e.credit?fmt(e.credit):''}</td>
        <td>${badge(e)}</td><td>${tagChips(e.tags)}</td></tr>`).join('')
      || '<tr><td colspan="9" class="empty-hint">无匹配结果</td></tr>'}
    </tbody></table></div>`);
}

/* ================================================================
   AI 助手（自然语言查账 + 链接弹出）
================================================================ */
S.agent = { msgs: [], cfg: null, presets: [], busy: false, atts: [], abort: null };

const AG_SESS_KEY = 'journal_agent_sessions';
const AG_ACTIVE_KEY = 'journal_agent_active';
const AG_SIDE_KEY = 'journal_agent_side';

function agNewId(){
  return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}
function agTime(ts){
  if(!ts) return '';
  const d = new Date(ts), n = new Date();
  const p2 = x => String(x).padStart(2, '0');
  const sameDay = d.toDateString() === n.toDateString();
  return sameDay ? `${p2(d.getHours())}:${p2(d.getMinutes())}`
                 : `${d.getMonth() + 1}-${p2(d.getDate())}`;
}
function agTitle(msgs){
  const u = (msgs || []).find(m => m.role === 'user' && m.content);
  if(!u) return '新对话';
  return u.content.replace(/\s+/g, ' ').trim().slice(0, 26) || '新对话';
}
function agSlimMsgs(msgs){
  return (msgs || []).slice(-60).map(m => ({
    role: m.role, content: m.content || '', error: m.error || false,
    links: (m.links || []).slice(0, 20),
    steps: (m.steps || []).slice(0, 10).map(s => ({label:s.label, name:s.name,
      ms:s.ms, summary:(s.summary||'').slice(0,300), ok:s.ok, state:s.state})),
    thinking: (m.thinking || '').slice(-2000),
    proposals: (m.proposals || []).map(p => ({
      pid: p.pid, kind: p.kind, title: p.title, status: p.status,
      danger: p.danger, warnings: (p.warnings || []).slice(0, 6),
      items: (p.items || []).slice(0, 200), result: p.result,
      ack: p.ack, at: p.at, replaced: p.replaced || false,
      superseded: p.superseded || [],
    })),
    route: m.route, turns: m.turns, ms: m.ms, atts: m.atts,
    model: m.model, ts: m.ts,
  }));
}
function agentLoadSessions(){
  let arr = null;
  try{ arr = JSON.parse(localStorage.getItem(AG_SESS_KEY) || 'null'); }catch(e){}
  if(!Array.isArray(arr) || !arr.length){
    // 老版本只存了一个扁平数组，迁移成第一个会话
    let old = [];
    try{ old = JSON.parse(localStorage.getItem('journal_agent_chat') || '[]'); }catch(e){}
    arr = [];
    if(Array.isArray(old) && old.length){
      arr.push({id: agNewId(), title: agTitle(old), ts: Date.now(), msgs: old});
    }
  }
  return arr.slice(0, 50);
}
function agentLoadMsgs(){
  S.agent.sessions = agentLoadSessions();
  let sid = null;
  try{ sid = localStorage.getItem(AG_ACTIVE_KEY); }catch(e){}
  if(!sid || !S.agent.sessions.find(s => s.id === sid)){
    sid = S.agent.sessions.length ? S.agent.sessions[0].id : agNewId();
    if(!S.agent.sessions.length){
      S.agent.sessions.push({id: sid, title: '新对话', ts: Date.now(), msgs: []});
    }
  }
  S.agent.sid = sid;
  const cur = S.agent.sessions.find(s => s.id === sid);
  return cur ? (cur.msgs || []).slice(-80) : [];
}
function agentSaveMsgs(){
  try{
    const ss = S.agent.sessions || [];
    let cur = ss.find(s => s.id === S.agent.sid);
    if(!cur){
      cur = {id: S.agent.sid || agNewId(), title: '新对话', ts: Date.now(), msgs: []};
      S.agent.sid = cur.id;
      ss.unshift(cur);
    }
    cur.msgs = agSlimMsgs(S.agent.msgs);
    cur.ts = Date.now();
    if(!cur.title || cur.title === '新对话') cur.title = agTitle(cur.msgs);
    localStorage.setItem(AG_SESS_KEY, JSON.stringify(ss.slice(0, 50)));
    localStorage.setItem(AG_ACTIVE_KEY, S.agent.sid);
  }catch(e){}
}

/* ---- 极简 Markdown 渲染（表格 / 列表 / 粗体 / 代码块） ---- */
function mdInline(t){
  return String(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    // 行内记录链接：模型写成 [文字](#entry:2091:4)，文字本身可点，不另起按钮
    .replace(/\[([^\]]+)\]\(#([A-Za-z]+:[^)\s]+)\)/g,
             '<span class="ag-il" data-action="agent-link" data-target="$2" ' +
             'title="点击打开">$1</span>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
/* 表格里的纯金额/百分比单元格右对齐 */
const mdIsNum = t => {
  const s = String(t).replace(/<[^>]+>/g, '').trim();
  return !!s && /\d/.test(s) &&
    /^[-+]?\d[\d,]*(\.\d+)?\s*(%|[A-Z]{3}|元|万元|亿)?$/.test(s);
};
function mdToHtml(src){
  const blocks = [];
  let s = String(src || '').replace(/\r/g, '');
  s = s.replace(/```[a-zA-Z]*\n?([\s\S]*?)```/g, (m, body) => {
    blocks.push('<pre><code>' + esc(body.replace(/\s+$/, '')) + '</code></pre>');
    return '\n\u0000' + (blocks.length - 1) + '\u0000\n';
  });
  s = esc(s);
  const lines = s.split('\n'), out = [];
  const cells = r => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(x => x.trim());
  let i = 0;
  while(i < lines.length){
    const L = lines[i];
    if(!L.trim()){ i++; continue; }
    const ph = L.trim().match(/^\u0000(\d+)\u0000$/);
    if(ph){ out.push(blocks[+ph[1]]); i++; continue; }
    if(L.includes('|') && i + 1 < lines.length &&
       /^\s*\|?[\s:\-|]+\|[\s:\-|]*$/.test(lines[i + 1])){
      const head = cells(L); i += 2; const rows = [];
      while(i < lines.length && lines[i].includes('|') && lines[i].trim()){
        rows.push(cells(lines[i])); i++;
      }
      out.push('<div class="md-tbl"><table><thead><tr>' +
        head.map(h => `<th>${mdInline(h)}</th>`).join('') +
        '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + r.map(c =>
          `<td class="${mdIsNum(c) ? 'num' : ''}">${mdInline(c)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table></div>');
      continue;
    }
    const h = L.match(/^(#{1,4})\s+(.*)$/);
    if(h){ out.push(`<div class="md-h">${mdInline(h[2])}</div>`); i++; continue; }
    if(/^\s*([-*_])\1{2,}\s*$/.test(L)){ out.push('<hr>'); i++; continue; }
    if(/^\s*&gt;\s?/.test(L)){
      const items = [];
      while(i < lines.length && /^\s*&gt;\s?/.test(lines[i])){
        items.push(lines[i].replace(/^\s*&gt;\s?/, '')); i++;
      }
      out.push('<blockquote>' + items.map(mdInline).join('<br>') + '</blockquote>');
      continue;
    }
    if(/^\s*[-*+]\s+/.test(L)){
      const items = [];
      while(i < lines.length && /^\s*[-*+]\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*[-*+]\s+/, '')); i++;
      }
      out.push('<ul>' + items.map(x => `<li>${mdInline(x)}</li>`).join('') + '</ul>');
      continue;
    }
    if(/^\s*\d+[.)]\s+/.test(L)){
      const items = [];
      while(i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, '')); i++;
      }
      out.push('<ol>' + items.map(x => `<li>${mdInline(x)}</li>`).join('') + '</ol>');
      continue;
    }
    const buf = [];
    while(i < lines.length && lines[i].trim() && !/^\u0000\d+\u0000$/.test(lines[i].trim())
          && !/^\s*[-*+]\s+/.test(lines[i]) && !/^\s*\d+[.)]\s+/.test(lines[i])
          && !/^#{1,4}\s/.test(lines[i])){ buf.push(lines[i]); i++; }
    out.push('<p>' + buf.map(mdInline).join('<br>') + '</p>');
  }
  return out.join('') || '<p class="muted">（空）</p>';
}

/* ---- 链接：target 形如 journal:3 / entry:12:3 / dashboard:1 / url:https://… ---- */
function agentLinkHTML(l){
  const t = String(l.target || '');
  const icon = t.startsWith('entry:') ? '📄' : t.startsWith('url:') ? '🌐'
             : t.startsWith('dashboard:') ? '📊' : t.startsWith('deposits:') ? '💰'
             : t.startsWith('cash:') ? '💵' : '📖';
  return `<button class="agent-link" data-action="agent-link" data-target="${esc(t)}"
    title="点击打开：${esc(t)}">${icon} ${esc(l.label)}</button>`;
}
/* 链接只露前几条，其余折叠——一轮回答挂三十个按钮没人看得下去 */
const AGENT_LINKS_SHOWN = 3;
function agentLinksHTML(m){
  const ls = m.links || [];
  if(!ls.length) return '';
  const head = ls.slice(0, AGENT_LINKS_SHOWN);
  const rest = ls.slice(AGENT_LINKS_SHOWN);
  return `
    <div class="ag-links">
      <div class="ag-links-t">${m.pushed ? '🔗 点开对应记录' : '🔗 相关记录（点开查看）'}</div>
      ${head.map(agentLinkHTML).join('')}
      ${rest.length ? `<details class="ag-more"><summary>还有 ${rest.length} 条</summary>
        <div class="ag-more-box">${rest.map(agentLinkHTML).join('')}</div></details>` : ''}
    </div>`;
}
async function agentOpenLink(target){
  agentClose();   // 先收起抽屉，让主区域的页面/弹窗露出来
  const parts = String(target || '').split(':');
  const type = parts[0];
  if(type === 'url'){ window.open(String(target).slice(4), '_blank', 'noopener'); return; }
  if(type === 'attachment'){ window.open('/api/attachments/' + parts[1] + '/file'); return; }
  if(type === 'journal' || type === 'entry'){
    const accId = type === 'entry' ? +parts[2] : +parts[1];
    const a = accById(accId);
    if(!a){ alert('找不到该账户，可能已被删除'); return; }
    if(a.company_id !== S.company){
      S.company = a.company_id; await refreshBootstrap();
      $('#companySelect').value = S.company;
    }
    S.jr = { accId, account:null, entries:[], loaded:false,
             from:'', to:'', preset:'all', q:'', status:'', tagId:'' };
    switchView('journal');
    if(type === 'entry'){
      await renderJournal();
      openEntryModal(+parts[1]);
    }
    return;
  }
  if(type === 'dashboard' || type === 'deposits' || type === 'cash'){
    const cid = +parts[1];
    if(cid && cid !== S.company){ S.company = cid; await refreshBootstrap(); $('#companySelect').value = cid; }
    switchView(type);
    return;
  }
  alert('无法识别的链接：' + target);
}

/* ---- 模型配置 ---- */
async function loadAgentCfg(){
  if(S.agent.cfg) return S.agent.cfg;
  const d = await api('/api/agent/config');
  S.agent.cfg = d.config; S.agent.presets = d.presets || [];
  return S.agent.cfg;
}
function agentCfgCardHTML(cfg){
  const ps = S.agent.presets;
  const cur = ps.find(p => p.id === cfg.provider) || ps[ps.length-1] || {};
  return `
  <b>🤖 AI 模型（查账助手）</b>
  <div class="hint" style="margin:4px 0 12px;line-height:1.8">
    支持任何 <b>OpenAI 兼容</b>接口（DeepSeek / OpenAI / 通义 / 智谱 / Kimi / 硅基流动 / Ollama / 自建网关）。
    选一个预设会自动填好地址和模型名，也可手动改。配置只存在本机 <code>data/agent_config.json</code>。
  </div>
  <div class="row" style="gap:10px;flex-wrap:wrap">
    <select id="agProvider" class="inp" style="width:230px">
      ${ps.map(p => `<option value="${p.id}" ${p.id===cfg.provider?'selected':''}>${esc(p.name)}</option>`).join('')}
    </select>
    <span class="muted small" id="agNote">${esc(cur.note || '')}</span>
    ${cur.key_url ? `<a class="small" href="${esc(cur.key_url)}" target="_blank" rel="noopener">去申请 Key ↗</a>` : ''}
  </div>
  <div class="row mt" style="gap:10px;flex-wrap:wrap">
    <label class="ag-lab">Base URL</label>
    <input id="agBase" class="inp" style="flex:1;min-width:300px"
      value="${esc(cfg.base_url)}" placeholder="https://api.deepseek.com/v1">
  </div>
  <div class="row mt" style="gap:10px;flex-wrap:wrap">
    <label class="ag-lab">模型名</label>
    <input id="agModel" class="inp" style="width:280px" value="${esc(cfg.model)}" placeholder="deepseek-chat">
    <label class="ag-lab">API Key</label>
    <input id="agKey" class="inp" style="width:280px" type="password"
      value="${esc(cfg.api_key || '')}" placeholder="${cfg.has_key ? '已保存，留空或保持 **** 不变' : 'sk-…'}">
  </div>
  <div class="row mt" style="gap:10px;flex-wrap:wrap">
    <label class="ag-lab">温度</label>
    <input id="agTemp" class="inp" style="width:80px" type="number" step="0.1" min="0" max="2" value="${cfg.temperature}">
    <label class="ag-lab">最多工具轮次</label>
    <input id="agRounds" class="inp" style="width:80px" type="number" step="1" min="1" max="12" value="${cfg.max_rounds}">
    <label class="ag-lab">超时(秒)</label>
    <input id="agTimeout" class="inp" style="width:90px" type="number" step="5" min="10" max="300" value="${cfg.timeout}">
  </div>
  <div class="row mt" style="gap:10px;flex-wrap:wrap;align-items:center">
    <label class="small" style="display:flex;align-items:center;gap:6px;cursor:pointer">
      <input type="checkbox" id="agVision" ${cfg.vision?'checked':''}>
      该模型支持读图（视觉，可直接看回单/截图）
    </label>
    <span class="muted small">${cfg.vision === cfg.vision_guess
      ? '（按模型名自动判定）' : '（已手动指定，覆盖自动判定）'}</span>
    <button class="btn sm" data-action="agent-vision-test">实测读图</button>
    <span class="small" id="agVisionMsg"></span>
  </div>
  <div class="row mt" style="gap:10px;flex-wrap:wrap">
    <button class="btn primary" data-action="agent-save">保存</button>
    <button class="btn" data-action="agent-test">测试连接</button>
    <span class="small" id="agTestMsg"></span>
  </div>`;
}
async function mountAgentCfg(){
  const box = $('#agentCfgMount');
  if(!box) return;
  try{
    const cfg = await loadAgentCfg();
    box.innerHTML = agentCfgCardHTML(cfg);
    const sel = $('#agProvider');
    if(sel) sel.addEventListener('change', () => {
      const p = (S.agent.presets || []).find(x => x.id === sel.value);
      if(!p) return;
      if(p.base_url) $('#agBase').value = p.base_url;
      if(p.model) $('#agModel').value = p.model;
      const n = $('#agNote'); if(n) n.textContent = p.note || '';
    });
  }catch(e){
    box.innerHTML = `<div class="hint">模型配置读取失败：${esc(e.message)}</div>`;
  }
}
function agentCfgForm(){
  return {
    provider: $('#agProvider')?.value || '',
    base_url: ($('#agBase')?.value || '').trim(),
    model: ($('#agModel')?.value || '').trim(),
    api_key: $('#agKey')?.value || '',
    temperature: +($('#agTemp')?.value || 0),
    max_rounds: +($('#agRounds')?.value || 6),
    timeout: +($('#agTimeout')?.value || 90),
    vision: !!$('#agVision')?.checked,
  };
}

/* ---- 聊天界面 ---- */
const AGENT_QUICK = [
  '本月各账户收支合计多少？',
  '现在各币种实际可用余额是多少？',
  '有哪些未兑现 / 未到账的款项？',
  '最近 10 笔流水',
  '列出最大的 10 笔支出',
  '定期存款哪几笔快到期了？',
];

/* 阶段徽章：告诉用户这次为什么查库 / 为什么没查 */
function agRouteHTML(m){
  const r = m.route;
  if(!r || !r.label) return '';
  const icon = r.mode === 'agent' ? '🔍' : r.mode === 'direct' ? '💬' : '🧠';
  const tools = (r.tools || []).length ? ` · ${r.tools.slice(0,3).join('/')}` : '';
  return `<div class="ag-route ${esc(r.mode)}">${icon} ${esc(r.label)}${esc(tools)}</div>`;
}
/* 思考过程：流式中展开，结束后折叠 */
function agThinkHTML(m, live){
  const t = (m.thinking || '').trim();
  if(!t) return '';
  if(live){
    return `<div class="ag-think-live">
      <div class="ag-tl-head"><span class="spin"></span>思考中…</div>
      <div class="ag-think-body">${esc(t)}<span class="ag-cursor"></span></div></div>`;
  }
  const secs = m.ms ? Math.max(1, Math.round((m.thinkMs || m.ms) / 1000)) : 0;
  return `<details class="ag-think"><summary>💭 思考过程${secs?` · ${secs}s`:''}</summary>
    <div class="ag-think-body">${esc(t)}</div></details>`;
}
/* 工具调用卡片 */
function agStepsHTML(m){
  const ss = m.steps || [];
  if(!ss.length) return '';
  return `<div class="ag-tools">${ss.map(s=>{
    const run = s.state === 'run';
    const cls = run ? 'running' : (s.ok === false ? 'bad' : 'done');
    const ico = run ? '<span class="spin"></span>' : (s.ok === false ? '⚠️' : '✓');
    const ms = (!run && s.ms != null) ? `<span class="ag-tool-ms">${s.ms}ms</span>` : '';
    const sum = (!run && s.summary) ? `<div class="ag-tool-sum">${esc(s.summary)}</div>` : '';
    return `<div class="ag-tool ${cls}"><span class="ag-tool-ico">${ico}</span>
      <span class="ag-tool-n">${esc(s.label || s.name || '工具')}</span>${ms}${sum}</div>`;
  }).join('')}</div>`;
}
function agAttsHTML(list){
  if(!list || !list.length) return '';
  return list.map(a => {
    const ico = a.kind === 'image'
      ? `<img src="${esc(a.url)}" alt="">`
      : `<span class="ag-att-ico">${a.kind === 'sheet' ? '📊' : (a.ext === 'pdf' ? '📕' : '📄')}</span>`;
    const kb = a.size ? `${Math.round(a.size/1024)}KB` : '';
    return `<span class="ag-att">${ico}<span class="ag-att-n">${esc(a.name)}</span>
      <span class="ag-att-s">${kb}</span></span>`;
  }).join('');
}
function agentMsgHTML(m, idx){
  if(m.role === 'user'){
    const atts = (m.atts && m.atts.length)
      ? `<div class="ag-atts" style="padding:6px 0 0">${agAttsHTML(m.atts)}</div>` : '';
    return `<div class="ag-msg user"><div class="ag-bubble">${esc(m.content).replace(/\n/g,'<br>')}${atts}</div></div>`;
  }
  const err = m.error;
  const body = err ? `<div class="ag-err">⚠️ ${esc(m.content)}</div>`
                   : (m.content ? mdToHtml(m.content) : '');
  const links = agentLinksHTML(m);
  const meta = [];
  if(m.turns) meta.push(`${m.turns} 轮`);
  if(m.ms) meta.push(`${(m.ms/1000).toFixed(1)}s`);
  return `<div class="ag-msg ai"><div class="ag-bubble">
      ${agRouteHTML(m)}${agThinkHTML(m, false)}${agStepsHTML(m)}${body}
      ${agPropsHTML(m, idx)}
      ${links}
      <div class="ag-ops">
        <button class="ag-op" data-action="agent-copy" data-idx="${idx}">复制</button>
        <button class="ag-op" data-action="agent-retry" data-idx="${idx}">重新生成</button>
        ${m.model?`<span class="muted small">${esc(m.model)}${meta.length?' · '+esc(meta.join(' · ')):''}</span>`:''}
      </div></div></div>`;
}
/* ---- 写入草稿卡片 ---- */
const AG_PROP_KIND = {create:'新增流水', import:'批量导入', update:'修改流水',
                      delete:'删除流水'};
function agPropDir(it){
  return (it.debit || it.direction === 'in') ? 'in' : 'out';
}
function agPropMoney(it){
  return it.debit || it.credit || it.amount || 0;
}
function agPropItemsHTML(p, mi, pi){
  const items = p.items || [];
  const ed = !!p.editing;
  const cell = (k, it, i, cls, type) => {
    const v = k === 'amount' ? agPropMoney(it).toFixed(2) : (it[k] ?? '');
    return `<input class="ag-pe ${cls||''}" type="${type||'text'}"
      data-mi="${mi}" data-pi="${pi}" data-i="${i}" data-k="${k}"
      value="${esc(String(v))}">`;
  };
  if(p.kind === 'update'){
    const it = items[0] || {};
    // 老卡片没有整行字段：从 changes 里补一份，保证「改字段」能编辑
    if(it.description === undefined){
      const to = k => ((it.changes || []).find(c => c.field === k) || {}).to;
      it.description = to('description') ?? it.summary ?? '';
      it.party = to('party') ?? '';
      if(!it.date) it.date = to('date') ?? '';
      const amt = it.amount || 0;
      it.debit = it.direction === 'in' ? amt : 0;
      it.credit = it.direction === 'in' ? 0 : amt;
    }
    const dir = agPropDir(it);
    if(ed){
      return `<div class="ag-prop-tbl"><table><thead><tr>
          <th>日期</th><th>收支</th><th class="num">金额</th><th>摘要 / 对方</th>
        </tr></thead><tbody><tr>
          <td>${cell('date', it, 0, 'dt')}</td>
          <td><select class="ag-pe sel" data-mi="${mi}" data-pi="${pi}" data-i="0" data-k="direction">
            <option value="out"${dir==='out'?' selected':''}>支</option>
            <option value="in"${dir==='in'?' selected':''}>收</option></select></td>
          <td>${cell('amount', it, 0, 'num', 'number')}</td>
          <td class="ag-prop-desc">
            ${cell('description', it, 0, '', 'text')}
            ${cell('party', it, 0, 'sm2', 'text')}
          </td>
        </tr></tbody></table></div>
        <div class="ag-chg-meta">${esc(it.account_name||'')} · ${esc(it.currency||'')} · #${it.entry_id}
          · 改完直接点「确认写入」</div>`;
    }
    return `<div class="ag-prop-chg">${(it.changes || []).map(c => `
      <div class="ag-chg">
        <span class="ag-chg-l">${esc(c.label)}</span>
        <span class="ag-chg-f">${esc(('' + (c.from||'')).slice(0,60) || '—')}</span>
        <span class="ag-chg-a">→</span>
        <span class="ag-chg-t">${esc(('' + (c.to||'')).slice(0,60))}</span>
      </div>`).join('')}
      <div class="ag-chg-meta">${esc(it.account_name||'')} · ${esc(it.currency||'')}
        · ${esc(it.date||'')} · #${it.entry_id}</div></div>`;
  }
  const rows = items.map((it, i) => {
    const dir = agPropDir(it);
    if(ed){
      return `<tr>
        <td><input class="ag-pe dt" data-mi="${mi}" data-pi="${pi}" data-i="${i}" data-k="date" value="${esc(it.date||'')}"></td>
        <td><select class="ag-pe sel" data-mi="${mi}" data-pi="${pi}" data-i="${i}" data-k="direction">
          <option value="out"${dir==='out'?' selected':''}>支</option>
          <option value="in"${dir==='in'?' selected':''}>收</option></select></td>
        <td><input class="ag-pe num" data-mi="${mi}" data-pi="${pi}" data-i="${i}" data-k="amount" value="${agPropMoney(it).toFixed(2)}"></td>
        <td class="ag-prop-desc">
          <input class="ag-pe" data-mi="${mi}" data-pi="${pi}" data-i="${i}" data-k="description" value="${esc(it.description||'')}" placeholder="摘要">
          <input class="ag-pe sm2" data-mi="${mi}" data-pi="${pi}" data-i="${i}" data-k="party" value="${esc(it.party||'')}" placeholder="对方">
        </td>
      </tr>`;
    }
    return `<tr>
      <td class="nowrap">${esc(it.date||'')}</td>
      <td class="${dir==='in'?'green':'red'}">${dir==='in'?'收':'支'}</td>
      <td class="num ${dir==='in'?'green':'red'}">${fmt(agPropMoney(it))}</td>
      <td class="ag-prop-desc">
        <div class="ag-prop-d1">${esc((it.description||'').slice(0,40) || '—')}</div>
        ${it.party?`<div class="ag-prop-d2">${esc(it.party.slice(0,24))}</div>`:''}
      </td>
    </tr>`;
  }).join('');
  const head = '<th>日期</th><th>收支</th><th class="num">金额</th><th>摘要 / 对方</th>';
  const sumIn = items.reduce((s, it) => s + (it.debit || (it.direction==='in' ? it.amount||0 : 0)), 0);
  const sumOut = items.reduce((s, it) => s + (it.credit || (it.direction==='out' ? it.amount||0 : 0)), 0);
  const total = p.kind === 'delete' ? '' : `<div class="ag-prop-sum">
      共 <b>${items.length}</b> 笔 · 收入 <b class="green">${fmt(sumIn)}</b>
      · 支出 <b class="red">${fmt(sumOut)}</b></div>`;
  return `<div class="ag-prop-tbl"><table>
      <thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>${total}`;
}
function agProposalHTML(p, mi, pi){
  const st = p.status || 'pending';
  const stTxt = (p.replaced && st === 'cancelled') ? '已作废'
    : ({pending:'待确认', done:'已写入', undone:'已撤销',
        cancelled:'已取消', failed:'未写入'}[st] || st);
  const head = `<div class="ag-prop-h">
      <span class="ag-prop-ico">${st === 'pending' ? (p.danger ? '⚠️' : '📝') : (st === 'done' ? '✓' : '·')}</span>
      <span class="ag-prop-t">${esc(p.title || AG_PROP_KIND[p.kind] || '写入草稿')}</span>
      <span class="ag-prop-st ${st}">${esc(stTxt)}</span>
    </div>`;
  if(st !== 'pending'){
    const r = p.result || {};
    const line = st === 'done'
      ? `已写入 ${r.done || 0} 笔${r.failed ? `，${r.failed} 笔失败` : ''}`
      : st === 'undone' ? '已撤销，数据回到这次写入之前'
      : st === 'failed' ? '没有写入任何记录'
      : (p.replaced ? '已被新的草稿替代，以下面那张卡片为准' : '已取消，账本没有变化');
    return `<div class="ag-prop ${st} ${p.danger?'danger':''}">${head}
      <div class="ag-prop-res">${esc(line)}
        ${(r.fails||[]).length ? `<div class="muted small">${esc((r.fails||[]).slice(0,3).join('；'))}</div>`:''}
      </div>
      ${st === 'done' && r.wid ? `<div class="ag-prop-f">
        <button class="btn sm" data-action="prop-undo" data-mi="${mi}" data-pi="${pi}">撤销这次写入</button>
      </div>` : ''}</div>`;
  }
  const warns = (p.warnings || []).filter(Boolean);
  const warnHTML = warns.length ? `<div class="ag-prop-warn">
      ${warns.slice(0, 5).map(w => `<div>· ${esc(w)}</div>`).join('')}
      ${warns.length > 5 ? `<div class="muted small">还有 ${warns.length - 5} 条提醒</div>` : ''}
    </div>` : '';
  const ack = p.danger ? `<label class="ag-prop-ack">
      <input type="checkbox" class="ag-ack" data-mi="${mi}" data-pi="${pi}" ${p.ack?'checked':''}>
      我已核对，确认删除这几笔</label>` : '';
  const okTxt = p.danger ? `确认删除 ${(p.items||[]).length} 笔` : '确认写入';
  return `<div class="ag-prop ${p.danger?'danger':''}">${head}
    ${warnHTML}
    ${agPropItemsHTML(p, mi, pi)}
    ${ack}
    <div class="ag-prop-f">
      <button class="btn sm primary" data-action="prop-confirm" data-mi="${mi}" data-pi="${pi}"
        ${p.danger && !p.ack ? 'disabled' : ''}>${okTxt}</button>
      ${p.kind === 'create' || p.kind === 'import' || p.kind === 'update'
        ? `<button class="btn sm" data-action="prop-edit" data-mi="${mi}" data-pi="${pi}">${p.editing?'完成修改':'改字段'}</button>` : ''}
      <button class="btn sm" data-action="prop-cancel" data-mi="${mi}" data-pi="${pi}">取消</button>
      <span class="muted small">确认前不会改动账本</span>
    </div></div>`;
}
function agPropsHTML(m, idx){
  const ps = m.proposals || [];
  if(!ps.length) return '';
  return ps.map((p, pi) => agProposalHTML(p, idx, pi)).join('');
}
/* 流式中的那条消息：局部刷新，不走全量重绘 */
function agentLiveHTML(m){
  const phase = m.phase ? `<div class="ag-route">⏳ ${esc(m.phase)}</div>` : '';
  const body = m.content ? mdToHtml(m.content) : '';
  return `<div class="ag-bubble">${agRouteHTML(m)}${phase}
    ${agThinkHTML(m, true)}${agStepsHTML(m)}
    <div class="ag-body-live">${body}${m.content ? '<span class="ag-cursor"></span>' : ''}</div>
    ${agPropsHTML(m, S.agent.msgs.indexOf(m))}
    ${agentLinksHTML(m)}</div>`;
}
function agentPaint(){
  const box = $('#agentChat');
  if(!box) return;
  if(!S.agent.msgs.length){
    box.innerHTML = `<div class="ag-empty">
      <div class="ag-empty-ico">🤖</div>
      <div class="ag-empty-t">查账助手</div>
      <div class="muted small" style="max-width:520px;line-height:1.9">
        直接用大白话问，我会自己去查库：余额、收支、某笔流水、大额支出、未兑现支票、定期到期…
        查到的记录会变成下方可点击的链接，点一下就能弹出对应的流水或账户。
      </div></div>`;
  }else{
    box.innerHTML = S.agent.msgs.map((m, i) => m.streaming
      ? `<div class="ag-msg ai" id="agLive">${agentLiveHTML(m)}</div>`
      : agentMsgHTML(m, i)).join('');
  }
  box.scrollTop = box.scrollHeight;
}
function agentSetBusy(v){
  S.agent.busy = v;
  const b = $('#agentSend');
  if(b){
    b.textContent = v ? '■ 停止' : '发送';
    b.classList.toggle('stop', v);
    b.disabled = false;
  }
  const f = $('#agentFile');
  if(f) f.disabled = v;
}
/* ---- 会话 id：服务端靠它记住「已经查过什么」 ---- */
function agentSid(){
  if(!S.agent.sid){
    S.agent.sid = (S.agent.sessions && S.agent.sessions.length)
      ? S.agent.sessions[0].id : agNewId();
  }
  return S.agent.sid;
}
/* ---- 会话历史侧栏 ---- */
function agentPaintSessions(){
  const box = $('#agentSide');
  if(!box) return;
  const ss = S.agent.sessions || [];
  const items = ss.length ? ss.map(s => `
    <div class="ag-side-item ${s.id === S.agent.sid ? 'on' : ''}"
         data-action="agent-switch" data-id="${esc(s.id)}" title="${esc(s.title || '新对话')}">
      <span class="ag-side-t">${esc(s.title || '新对话')}</span>
      <span class="ag-side-time">${agTime(s.ts)}</span>
      <button class="ag-side-x" data-action="agent-del-chat" data-id="${esc(s.id)}"
        title="删除这个对话">✕</button>
    </div>`).join('') : `<div class="ag-side-empty">还没有历史对话。<br>问一句，它就会出现在这里。</div>`;
  box.innerHTML = `
    <div class="ag-side-head">
      <button class="btn sm primary" data-action="agent-new">＋ 新建对话</button>
      <button class="btn sm" data-action="agent-clear-all" title="清空全部历史对话">🧹</button>
    </div>
    <div class="ag-side-list">${items}</div>`;
}
function agentSideToggle(force){
  const dock = $('#agentDock');
  if(!dock) return;
  const open = force === undefined ? !dock.classList.contains('side-open') : !!force;
  dock.classList.toggle('side-open', open);
  try{ localStorage.setItem(AG_SIDE_KEY, open ? '1' : '0'); }catch(e){}
  if(open) agentPaintSessions();
}
function agentNewChat(){
  if(S.agent.busy) agentStop();
  agentSaveMsgs();
  const id = agNewId();
  S.agent.sessions = S.agent.sessions || [];
  S.agent.sessions.unshift({id, title: '新对话', ts: Date.now(), msgs: []});
  S.agent.sid = id;
  S.agent.msgs = [];
  S.agent.atts = [];
  try{ localStorage.setItem(AG_ACTIVE_KEY, id); }catch(e){}
  agentSaveMsgs(); agentPaint(); agentPaintAtts(); agentPaintSessions();
  const ta = $('#agentInput');
  if(ta && !agentIsNarrow()) ta.focus();
  if(agentIsNarrow()) agentSideToggle(false);
}
function agentSwitchChat(id){
  if(!id || id === S.agent.sid) { if(agentIsNarrow()) agentSideToggle(false); return; }
  if(S.agent.busy) agentStop();
  agentSaveMsgs();
  S.agent.sid = id;
  const cur = (S.agent.sessions || []).find(s => s.id === id);
  S.agent.msgs = cur ? (cur.msgs || []) : [];
  S.agent.atts = [];
  try{ localStorage.setItem(AG_ACTIVE_KEY, id); }catch(e){}
  agentPaint(); agentPaintAtts(); agentPaintSessions();
  if(agentIsNarrow()) agentSideToggle(false);
}
function agentDelChat(id){
  const cur = (S.agent.sessions || []).find(s => s.id === id);
  if(!cur) return;
  if(!confirm(`删除对话「${cur.title || '新对话'}」？只删本地记录，不动账本数据。`)) return;
  if(S.agent.busy && S.agent.sid === id) agentStop();
  S.agent.sessions = (S.agent.sessions || []).filter(s => s.id !== id);
  if(S.agent.sid === id){
    const next = S.agent.sessions[0];
    S.agent.sid = next ? next.id : agNewId();
    if(!next){
      S.agent.sessions.unshift({id: S.agent.sid, title: '新对话', ts: Date.now(), msgs: []});
    }
    S.agent.msgs = (next && next.msgs) ? next.msgs : [];
  }
  agentSaveMsgs(); agentPaint(); agentPaintSessions();
}
function agentClearAll(){
  if(!(S.agent.sessions || []).length) return;
  if(!confirm(`清空全部 ${S.agent.sessions.length} 个历史对话？只删本地记录，不动账本数据。`)) return;
  if(S.agent.busy) agentStop();
  S.agent.sessions = [];
  S.agent.sid = agNewId();
  S.agent.msgs = [];
  agentSaveMsgs(); agentPaint(); agentPaintSessions();
}
function agentRenameChat(id){
  const cur = (S.agent.sessions || []).find(s => s.id === id);
  if(!cur) return;
  const t = prompt('给这个对话起个名字：', cur.title || '新对话');
  if(t === null) return;
  const v = String(t).trim().slice(0, 40);
  if(!v) return;
  cur.title = v;
  try{ localStorage.setItem(AG_SESS_KEY, JSON.stringify(S.agent.sessions)); }catch(e){}
  agentPaintSessions();
}

let agRaf = 0;
/* 流式刷新：每帧最多重绘一次，别让逐字增量把 DOM 打爆 */
function agSchedulePaint(m){
  if(agRaf) return;
  agRaf = requestAnimationFrame(()=>{
    agRaf = 0;
    const box = $('#agLive'), sc = $('#agentChat');
    if(!box || !sc) return;
    const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 90;
    box.innerHTML = agentLiveHTML(m);
    if(atBottom) sc.scrollTop = sc.scrollHeight;
  });
}
function agentOnEvent(m, ev){
  switch(ev.t){
    case 'start': m.model = ev.model; m.vision = !!ev.vision; break;
    case 'route':
      m.route = {mode: ev.mode, label: ev.label, tools: ev.tools || []};
      m.phase = ''; break;
    case 'phase': m.phase = ev.name || ''; break;
    case 'think': m.thinking = (m.thinking || '') + (ev.d || ''); break;
    case 'text': m.content = (m.content || '') + (ev.d || ''); break;
    case 'flush_think':
      if((m.content || '').trim()){
        m.thinking = (m.thinking || '') + (m.thinking ? '\n\n' : '') + m.content.trim();
      }
      m.content = ''; break;
    case 'tool': {
      const id = ev.id || ev.name;
      if(ev.state === 'start'){
        m.steps.push({id, name: ev.name, label: ev.label, state: 'run'});
      }else{
        let s = m.steps.find(x => x.id === id);
        if(!s){ s = {id, name: ev.name, label: ev.label}; m.steps.push(s); }
        s.state = 'end'; s.ms = ev.ms; s.summary = ev.summary; s.ok = ev.ok;
      }
      break;
    }
    case 'links': m.links = ev.links || []; break;
    case 'proposal':
      if(ev.proposal){
        // 新草稿替代了旧的（同一笔再改一次）：把旧卡片标成已作废，免得点错
        const sup = ev.proposal.superseded || [];
        if(sup.length){
          (S.agent.msgs || []).forEach(mm => (mm.proposals || []).forEach(q => {
            if(sup.indexOf(q.pid) >= 0 && q.status === 'pending'){
              q.status = 'cancelled'; q.replaced = true;
            }
          }));
        }
        (m.proposals = m.proposals || []).push(
          Object.assign({status:'pending'}, ev.proposal));
      }
      break;
    case 'usage': m.usage = ev.usage || null; break;
    case 'done':
      m.ms = ev.ms; m.turns = ev.turns; m.model = ev.model || m.model; break;
    case 'error':
      m.error = true;
      m.content = (m.content || '') + (m.content ? '\n\n' : '') + '⚠️ ' + ev.msg;
      break;
  }
  agSchedulePaint(m);
}
/* ---- 写入草稿：确认 / 取消 / 撤销 ---- */
function agProp(mi, pi){
  const m = S.agent.msgs[+mi];
  return m ? (m.proposals || [])[+pi] : null;
}
function agPropEdits(p){
  if(p.kind === 'update'){
    const it = (p.items || [])[0];
    if(!it || !it._dirty) return {};
    return {0: {date: it.date, amount: agPropMoney(it),
                direction: agPropDir(it), description: it.description || '',
                party: it.party || ''}};
  }
  if(p.kind !== 'create' && p.kind !== 'import') return {};
  const out = {};
  (p.items || []).forEach((it, i)=>{
    if(!it._dirty) return;
    out[i] = {date: it.date, amount: agPropMoney(it),
              direction: agPropDir(it), party: it.party || '',
              description: it.description || ''};
  });
  return out;
}
async function agPropConfirm(mi, pi){
  const p = agProp(mi, pi);
  if(!p || p.status !== 'pending' || p.busy) return;
  if(p.danger && !p.ack){ alert('删除操作需要先勾选「我已核对」'); return; }
  p.busy = true; agentPaint();
  try{
    const r = await jpost(`/api/agent/proposal/${p.pid}/confirm`,
                          {edits: agPropEdits(p), ack: !!p.ack});
    p.status = r.done ? 'done' : 'failed';
    p.result = {done: r.done, failed: r.failed, fails: r.fails, wid: r.wid};
    p.editing = false;
    if(r.done){
      await refreshAfterWrite();
    }
  }catch(err){
    alert('写入失败：' + err.message);
  }
  p.busy = false;
  agentSaveMsgs(); agentPaint();
}
async function agPropCancel(mi, pi){
  const p = agProp(mi, pi);
  if(!p || p.status !== 'pending') return;
  try{ await jpost(`/api/agent/proposal/${p.pid}/cancel`, {}); }catch(e){}
  p.status = 'cancelled';
  agentSaveMsgs(); agentPaint();
}
async function agPropUndo(mi, pi){
  const p = agProp(mi, pi);
  if(!p || !p.result || !p.result.wid) return;
  if(!confirm('撤销这次写入？数据会回到写入之前。')) return;
  try{
    await jpost(`/api/agent/undo/${p.result.wid}`, {});
    p.status = 'undone';
    await refreshAfterWrite();
  }catch(err){
    alert('撤销失败：' + err.message);
  }
  agentSaveMsgs(); agentPaint();
}
/* 卡片内联编辑：写回模型，不重绘（重绘会打断输入） */
document.addEventListener('input', e=>{
  const el = e.target.closest && e.target.closest('.ag-pe');
  if(!el) return;
  const p = agProp(el.dataset.mi, el.dataset.pi);
  const it = p && (p.items || [])[+el.dataset.i];
  if(!it) return;
  const k = el.dataset.k;
  if(k === 'amount'){
    const amt = Math.abs(parseFloat(String(el.value).replace(/,/g, '')) || 0);
    if(agPropDir(it) === 'in'){ it.debit = amt; it.credit = 0; }
    else { it.debit = 0; it.credit = amt; }
  }else if(k === 'direction'){
    const amt = agPropMoney(it);
    if(el.value === 'in'){ it.debit = amt; it.credit = 0; }
    else { it.debit = 0; it.credit = amt; }
  }else{
    it[k] = el.value;
  }
  it._dirty = true;
});
document.addEventListener('change', e=>{
  const el = e.target.closest && e.target.closest('.ag-ack');
  if(!el) return;
  const p = agProp(el.dataset.mi, el.dataset.pi);
  if(p) p.ack = el.checked;
  const card = el.closest('.ag-prop');
  const btn = card && card.querySelector('[data-action="prop-confirm"]');
  if(btn) btn.disabled = !el.checked;
});
function agentStop(){
  if(S.agent.abort){
    try{ S.agent.abort.abort(); }catch(e){}
  }
}
async function agentSend(text){
  if(S.agent.busy){ agentStop(); return; }
  const input = $('#agentInput');
  const msg = (text !== undefined ? text : (input?.value || '')).trim();
  const atts = (S.agent.atts || []).slice();
  if(!msg && !atts.length) return;
  if(input) input.value = '';
  S.agent.atts = []; agentPaintAtts();
  const history = S.agent.msgs
    .filter(m => !m.streaming && !m.error && (m.role === 'user' || (m.role === 'assistant' && m.content)))
    .slice(-12).map(m => ({role: m.role, content: m.content}));
  S.agent.msgs.push({role:'user', content: msg || '（见附件）',
                     atts: atts.length ? atts : undefined, ts: Date.now()});
  const live = {role:'assistant', content:'', thinking:'', steps:[], route:null,
                proposals:[], phase:'正在理解问题…', streaming:true, ts: Date.now()};
  S.agent.msgs.push(live);
  const curSess = (S.agent.sessions || []).find(s => s.id === S.agent.sid);
  if(curSess && (!curSess.title || curSess.title === '新对话')){
    curSess.title = agTitle([{role:'user', content: msg || '附件分析'}]);
  }
  agentSaveMsgs(); agentPaint(); agentPaintSessions(); agentSetBusy(true);
  const ctrl = new AbortController();
  S.agent.abort = ctrl;
  const t0 = performance.now();
  try{
    const res = await fetch('/api/agent/chat/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg || '请看我上传的附件，说明关键内容',
        history, sid: agentSid(),
        attachments: atts.map(a => a.fid),
      }),
      signal: ctrl.signal,
    });
    if(!res.ok){
      let e = {};
      try{ e = await res.json(); }catch(_){}
      throw new Error(e.error || ('HTTP ' + res.status));
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while(true){
      const {value, done} = await reader.read();
      if(done) break;
      buf += dec.decode(value, {stream:true});
      let i;
      while((i = buf.indexOf('\n\n')) >= 0){
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        for(const line of chunk.split('\n')){
          if(line.indexOf('data:') !== 0) continue;
          const p = line.slice(5).trim();
          if(!p || p === '[DONE]') continue;
          try{ agentOnEvent(live, JSON.parse(p)); }catch(err){}
        }
      }
    }
  }catch(err){
    if(err.name !== 'AbortError'){
      live.error = true;
      live.content = err.message || '请求失败';
    }else{
      live.content = (live.content || '') + '\n\n_（已停止生成）_';
    }
  }
  live.streaming = false;
  live.phase = '';
  if(!live.ms) live.ms = Math.round(performance.now() - t0);
  if(!live.error && !live.content && !live.steps.length){
    live.content = '（没有拿到内容，可以点重新生成或换个模型试试）';
  }
  S.agent.abort = null;
  agentSetBusy(false);
  agentSaveMsgs(); agentPaint(); agentPaintSessions();
}
/* ---- 附件上传 ---- */
function agFileIcon(a){
  if(a.kind === 'image') return `<img src="${esc(a.url || '')}" alt="">`;
  const ico = a.kind === 'sheet' ? '📊' : (a.ext === 'pdf' ? '📕' : '📄');
  return `<span class="ag-att-ico">${ico}</span>`;
}
function agentPaintAtts(){
  const box = $('#agentAtts');
  if(!box) return;
  const list = S.agent.atts || [];
  if(!list.length){ box.innerHTML = ''; box.style.display = 'none'; return; }
  box.style.display = 'flex';
  box.innerHTML = list.map((a, i) => `
    <span class="ag-att ${a.busy ? 'busy' : ''}" title="${esc(a.name || '')}">
      ${agFileIcon(a)}
      <span class="ag-att-n">${esc(a.name || '上传中…')}</span>
      <span class="ag-att-s">${a.busy ? '…' : (a.size ? Math.round(a.size / 1024) + 'KB' : '')}</span>
      <button class="ag-att-x" data-action="agent-att-del" data-i="${i}" title="移除">✕</button>
    </span>`).join('');
}
const AG_ACCEPT = '.png,.jpg,.jpeg,.webp,.gif,.bmp,.csv,.tsv,.txt,.md,.json,' +
                  '.xlsx,.xls,.pdf,.docx';
async function agentUploadFiles(files){
  const arr = Array.from(files || []);
  const room = 6 - (S.agent.atts || []).length;
  if(room <= 0){ alert('一次最多带 6 个附件'); return; }
  for(const f of arr.slice(0, room)){
    if(f.size > 25 * 1024 * 1024){ alert(`${f.name} 超过 25MB，请压缩后再传`); continue; }
    const isImg = (f.type || '').startsWith('image/');
    const tmp = {
      fid: 'tmp' + Date.now() + Math.random().toString(36).slice(2, 6),
      name: f.name || (isImg ? '截图.png' : '文件'),
      size: f.size, kind: isImg ? 'image' : 'doc',
      ext: (f.name || '').split('.').pop().toLowerCase(),
      url: isImg ? URL.createObjectURL(f) : '', busy: true,
    };
    S.agent.atts.push(tmp); agentPaintAtts();
    try{
      const fd = new FormData();
      fd.append('file', f, tmp.name);
      const r = await fetch('/api/agent/upload', {method:'POST', body: fd});
      const d = await r.json();
      if(!d.ok) throw new Error(d.error || '上传失败');
      const keep = tmp.url;
      Object.assign(tmp, d.file, {busy: false});
      if(tmp.kind === 'image') tmp.url = d.file.url || keep;
    }catch(e){
      S.agent.atts = S.agent.atts.filter(x => x !== tmp);
      alert('上传失败：' + e.message);
    }
    agentPaintAtts();
  }
}
function agentBindDrop(){
  const dock = $('#agentDock');
  if(dock){
    let depth = 0;
    const hasFiles = e => !!(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files'));
    dock.addEventListener('dragenter', e => {
      if(!hasFiles(e)) return;
      e.preventDefault(); depth++; dock.classList.add('ag-drop');
    });
    dock.addEventListener('dragover', e => { if(hasFiles(e)) e.preventDefault(); });
    dock.addEventListener('dragleave', () => {
      depth = Math.max(0, depth - 1);
      if(!depth) dock.classList.remove('ag-drop');
    });
    dock.addEventListener('drop', e => {
      e.preventDefault(); depth = 0; dock.classList.remove('ag-drop');
      if(e.dataTransfer && e.dataTransfer.files.length) agentUploadFiles(e.dataTransfer.files);
    });
  }
  const ta = $('#agentInput');
  if(ta){
    ta.addEventListener('paste', e => {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      const files = [];
      for(const it of items){
        if(it.kind === 'file'){
          const f = it.getAsFile();
          if(f) files.push(f);
        }
      }
      if(files.length){
        e.preventDefault();
        agentUploadFiles(files.map((f, i) => {
          if((f.type || '').startsWith('image/') && !/\.[a-z]+$/i.test(f.name || '')){
            try{ return new File([f], `截图-${Date.now()}-${i}.png`, {type: f.type}); }catch(_){ }
          }
          return f;
        }));
      }
    });
  }
  const inp = $('#agentFile');
  if(inp) inp.addEventListener('change', () => {
    if(inp.files && inp.files.length) agentUploadFiles(inp.files);
    inp.value = '';
  });
}
/* ---- 右侧抽屉 ---- */
const AGENT_W_KEY = 'journal_agent_width';
const agentIsNarrow = () => window.innerWidth <= 900;
function agentWidth(){
  const w = +(localStorage.getItem(AGENT_W_KEY) || 0);
  return (w >= 340 && w <= 900) ? w : 430;
}
function agentApplyWidth(w){
  document.documentElement.style.setProperty('--agent-w', Math.round(w) + 'px');
}
function agentOpen(){
  const dock = $('#agentDock');
  if(!dock) return;
  agentApplyWidth(agentIsNarrow() ? window.innerWidth : agentWidth());
  dock.classList.add('open');
  dock.setAttribute('aria-hidden', 'false');
  document.body.classList.add('agent-open');
  $('#navAgent')?.classList.add('active');
  try{ localStorage.setItem('journal_agent_open', '1'); }catch(e){}
  agentPaint();
  agentPaintSessions();
  setTimeout(()=>{ if(!agentIsNarrow()) $('#agentInput')?.focus(); }, 240);
}
function agentClose(){
  const dock = $('#agentDock');
  if(!dock) return;
  dock.classList.remove('open');
  dock.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('agent-open');
  $('#navAgent')?.classList.remove('active');
  try{ localStorage.setItem('journal_agent_open', '0'); }catch(e){}
}
function agentToggle(){
  const dock = $('#agentDock');
  if(dock && dock.classList.contains('open')) agentClose(); else agentOpen();
}

async function initAgentDock(){
  agentBindDrop();
  agentPaintAtts();
  agentPaintSessions();
  try{
    if(localStorage.getItem(AG_SIDE_KEY) === '1') agentSideToggle(true);
  }catch(e){}
  const side = $('#agentSide');
  if(side) side.addEventListener('dblclick', ev => {
    const it = ev.target.closest('.ag-side-item');
    if(it && it.dataset.id) agentRenameChat(it.dataset.id);
  });
  const q = $('#agentQuick');
  if(q) q.innerHTML = AGENT_QUICK
    .map(t=>`<button class="ag-chip" data-action="agent-chip" data-q="${esc(t)}">${esc(t)}</button>`).join('');

  const ta = $('#agentInput');
  if(ta){
    ta.addEventListener('keydown', e=>{
      if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); agentSend(); }
    });
    ta.addEventListener('input', ()=>{
      ta.style.height = 'auto';
      ta.style.height = Math.min(120, ta.scrollHeight) + 'px';
    });
  }

  try{
    const cfg = await loadAgentCfg();
    const h = $('#agentModelHint');
    if(h) h.textContent = cfg.model ? `· ${cfg.model}` : '· 未配置模型';
  }catch(e){}

  // 拖动左边缘调整宽度
  const grip = $('#agentGrip'), dock = $('#agentDock');
  if(grip && dock){
    let dragging = false, startX = 0, startW = 0;
    const chatW = () => dock.getBoundingClientRect().width
      - (dock.classList.contains('side-open') ? 210 : 0);
    grip.addEventListener('mousedown', ev=>{
      if(agentIsNarrow()) return;
      dragging = true; startX = ev.clientX;
      startW = chatW();
      document.body.classList.add('agent-resizing');
      if(dock.setPointerCapture && ev.pointerId != null){
        try{ dock.setPointerCapture(ev.pointerId); }catch(e){}
      }
      ev.preventDefault();
    });
    window.addEventListener('mousemove', ev=>{
      if(!dragging) return;
      agentApplyWidth(Math.max(340, Math.min(900, startW + (startX - ev.clientX))));
    });
    window.addEventListener('mouseup', ()=>{
      if(!dragging) return;
      dragging = false;
      document.body.classList.remove('agent-resizing');
      try{ localStorage.setItem(AGENT_W_KEY, String(Math.round(chatW()))); }catch(e){}
    });
  }

  window.addEventListener('resize', ()=>{
    if(agentIsNarrow()) agentApplyWidth(window.innerWidth);
    else if($('#agentDock')?.classList.contains('open')) agentApplyWidth(agentWidth());
  });

  agentPaint();
  let wasOpen = false;
  try{ wasOpen = localStorage.getItem('journal_agent_open') === '1'; }catch(e){}
  if(wasOpen) agentOpen();
}

/* ================================================================
   事件委托
================================================================ */
document.addEventListener('click', async e=>{
  const el = e.target.closest('[data-action]');
  if(!el) return;
  const act = el.dataset.action, id = Number(el.dataset.id)||el.dataset.id;
  try{
    switch(act){
      case 'nav': switchView(el.dataset.view); break;
      case 'cash-add': openCashCount(null); break;
      case 'cash-edit': openCashCount(+el.dataset.id); break;
      case 'cash-copy': {
        e.stopPropagation();                     // 别同时触发整行的“查看/编辑”
        await openCashCount(null, +el.dataset.id);
        break;
      }
      case 'cash-export': {
        const id = el.dataset.id;
        if(id){
          location.href = `/api/export/cash-counts?company_id=${S.company}&id=${id}`;
        }else{
          openCashExport();
        }
        break;
      }
      case 'cash-del': {
        e.stopPropagation();
        if(!confirm('确定删除这张盘点单？')) break;
        await jdel('/api/cash/counts/'+el.dataset.id);
        renderCash(); break;
      }
      case 'cash-goto-journal': {
        const a = S.accounts.find(x=>x.company_id===S.company && x.kind==='cash');
        if(!a){ alert('该公司还没有现金账户'); break; }
        S.jr={accId:a.id,account:null,entries:[],loaded:false,from:'',to:'',preset:'all',q:'',status:'',tagId:''};
        switchView('journal'); break;
      }
      case 'cash-acc-journal': {
        S.jr={accId:+el.dataset.id,account:null,entries:[],loaded:false,from:'',to:'',preset:'all',q:'',status:'',tagId:''};
        switchView('journal'); break;
      }
      case 'master-export': openMasterModal(); break;
      case 'mx-custom': mxSetCustom(true); break;
      case 'mx-period-reset': mxSetCustom(false); break;
      case 'mx-quick': e.preventDefault(); mxQuick(el.dataset.y, el.dataset.p); break;
      case 'mx-all': S.mxOff=[]; S.mxDepOff=[]; mxPaint(); break;
      case 'mx-none': S.mxOff=S.accounts.map(a=>a.id); S.mxDepOff=S.companies.map(c=>c.id); mxPaint(); break;
      case 'mx-go': doMasterExport(); break;
      case 'goto': switchView(el.dataset.view); break;
      case 'modal-close': closeModal(); break;
      case 'agent-toggle': agentToggle(); break;
      case 'agent-close': agentClose(); break;
      case 'agent-send': await agentSend(); break;
      case 'agent-chip': await agentSend(el.dataset.q); break;
      case 'agent-stop': agentStop(); break;
      case 'agent-pick': { const f = $('#agentFile'); if(f) f.click(); break; }
      case 'agent-side': agentSideToggle(); break;
      case 'agent-new': agentNewChat(); break;
      case 'agent-switch': agentSwitchChat(el.dataset.id); break;
      case 'agent-del-chat':
        e.stopPropagation();
        agentDelChat(el.dataset.id);
        break;
      case 'agent-clear-all': agentClearAll(); break;
      case 'agent-rename': agentRenameChat(el.dataset.id); break;
      case 'agent-att-del': {
        const i = +el.dataset.i;
        S.agent.atts = (S.agent.atts || []).filter((_, k) => k !== i);
        agentPaintAtts(); break;
      }
      case 'agent-link': await agentOpenLink(el.dataset.target); break;
      case 'agent-clear': {
        if(!S.agent.msgs.length) break;
        if(!confirm('清空当前对话记录？（不影响账本数据）')) break;
        S.agent.msgs = [];
        S.agent.atts = [];
        agentSaveMsgs(); agentPaintAtts(); agentPaint(); break;
      }
      case 'agent-settings': agentClose(); switchView('settings'); break;
      case 'prop-confirm': await agPropConfirm(el.dataset.mi, el.dataset.pi); break;
      case 'prop-cancel': await agPropCancel(el.dataset.mi, el.dataset.pi); break;
      case 'prop-undo': await agPropUndo(el.dataset.mi, el.dataset.pi); break;
      case 'prop-edit': {
        const p = agProp(el.dataset.mi, el.dataset.pi);
        if(p){ p.editing = !p.editing; agentPaint(); }
        break;
      }
      case 'agent-copy': {        const m = S.agent.msgs[+el.dataset.idx];
        if(!m) break;
        try{ await navigator.clipboard.writeText(m.content); el.textContent = '已复制'; }
        catch(err){
          const r = document.createRange(); r.selectNodeContents(el);
          alert('复制失败，请手动选中文字。');
        }
        break;
      }
      case 'agent-retry': {
        const idx = +el.dataset.idx;
        let q = '';
        for(let i = idx - 1; i >= 0; i--){
          if(S.agent.msgs[i]?.role === 'user'){ q = S.agent.msgs[i].content; break; }
        }
        if(!q) break;
        S.agent.msgs = S.agent.msgs.slice(0, idx);
        agentSaveMsgs(); agentPaint();
        await agentSend(q);
        break;
      }
      case 'agent-save': {
        const msg = $('#agTestMsg'); if(msg) msg.textContent = '保存中…';
        try{
          const r = await jpost('/api/agent/config', agentCfgForm());
          S.agent.cfg = null; await loadAgentCfg();
          const m2 = $('#agTestMsg');
          if(m2){ m2.style.color = 'var(--green)'; m2.textContent = '✅ 已保存'; }
        }catch(err){
          const m2 = $('#agTestMsg');
          if(m2){ m2.style.color = 'var(--red)'; m2.textContent = '❌ ' + err.message; }
        }
        break;
      }
      case 'agent-test': {
        const msg = $('#agTestMsg');
        if(msg){ msg.style.color = 'var(--muted)'; msg.textContent = '测试中…'; }
        try{
          const r = await jpost('/api/agent/test', agentCfgForm());
          if(msg){
            msg.style.color = r.ok ? 'var(--green)' : 'var(--red)';
            msg.textContent = r.ok ? `✅ 连接成功（${r.model}）：${r.reply || 'ok'}` : '❌ ' + r.error;
          }
        }catch(err){
          if(msg){ msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + err.message; }
        }
        break;
      }
      case 'agent-vision-test': {
        const msg = $('#agVisionMsg');
        if(msg){ msg.style.color = 'var(--muted)'; msg.textContent = '正在发一张测试图…'; }
        const form = agentCfgForm();
        try{
          const r = await jpost('/api/agent/vision-test', form);
          if(msg){
            msg.style.color = r.ok ? 'var(--green)' : 'var(--red)';
            msg.textContent = r.ok
              ? `✅ 能读图（读到 ${r.reply}），已自动开启`
              : `❌ 读图失败：${r.error || ('回的是「' + (r.reply || '空') + '」')}`;
          }
          if(r.ok && $('#agVision')) $('#agVision').checked = true;
        }catch(err){
          if(msg){ msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + err.message; }
        }
        break;
      }
      case 'goto-journal': {
        S.company = accById(+el.dataset.id)?.company_id ?? S.company;
        await refreshBootstrap(); $('#companySelect').value = S.company;
        S.jr={accId:+el.dataset.id,account:null,entries:[],loaded:false,from:'',to:'',preset:'thisMonth',q:'',status:'',tagId:''};
        switchView('journal'); break;
      }
      case 'goto-entry': {
        const accId=+el.dataset.acc;
        S.company = accById(accId)?.company_id ?? S.company;
        await refreshBootstrap(); $('#companySelect').value=S.company;
        S.jr={accId,account:null,entries:[],loaded:false,from:el.dataset.month+'-01',to:monthEndStr(el.dataset.month),preset:'custom',q:'',status:'',tagId:''};
        closeModal(); switchView('journal'); break;
      }
      case 'note-goto': {
        const accId = +el.dataset.acc, entryId = +el.dataset.id;
        S.company = (+el.dataset.company) || accById(accId)?.company_id || S.company;
        await refreshBootstrap(); $('#companySelect').value = S.company;
        // 用全部日期确保目标流水可见，然后直接打开编辑弹窗
        S.jr={accId,account:null,entries:[],loaded:false,from:'',to:'',preset:'all',q:'',status:'',tagId:''};
        closeModal(); switchView('journal');
        await renderJournal();
        openEntryModal(entryId);
        break;
      }
      case 'planner': await openPlanner(+el.dataset.id||S.jr.accId); break;
      case 'jr-month-toggle': jrGroupMonth = !jrGroupMonth; renderJournal(); break;
      case 'plan-sel': {
        const v = el.dataset.v==='all';
        $$('#modalBox [data-change="plan-check"]').forEach(cb=>cb.checked=v);
        const acc=accById(S.jr.accId);
        const pend=S.jr.entries.filter(x=>x.pending);
        const book=S.jr.entries.length?S.jr.entries[S.jr.entries.length-1]._bal:acc.opening;
        recalcPlan(book+pend.reduce((s,x)=>s+x.credit-x.debit,0));
        break;
      }
      case 'entry-add': tmpTags.clear(); openEntryModal(null); break;
      case 'entry-edit': if(e.target.closest('[data-action]').dataset.action==='entry-edit') openEntryModal(+el.dataset.id); break;
      case 'entry-att': e.stopPropagation(); openEntryModal(+el.dataset.id); break;
      case 'entry-del': {
        e.stopPropagation();
        if(!confirm('确定删除这笔流水？其附件也会一并删除。')) break;
        await jdel('/api/entries/'+el.dataset.id);
        closeModal(); await reloadJournal(); loadReminders();
        break;
      }
      case 'tag-toggle': {
        const tid=+el.dataset.id;
        if(tmpTags.has(tid)) tmpTags.delete(tid); else tmpTags.add(tid);
        el.style.opacity = tmpTags.has(tid)?1:.35;
        el.style.outline = tmpTags.has(tid)?'2px solid #0f172a':'';
        break;
      }
      case 'att-del': {
        if(!confirm('删除该附件？')) break;
        await jdel('/api/attachments/'+el.dataset.id);
        const eid = Number($('#entryForm')?.dataset.eid);
        const idx = S.jr.entries.findIndex(x=>x.id===eid);
        if(idx>=0){
          S.jr.entries[idx].attachments = S.jr.entries[idx].attachments.filter(a=>a.id!==+el.dataset.id);
          $('#attZone').innerHTML = attZoneHTML(S.jr.entries[idx]);
        }
        break;
      }
      case 'att-open': window.open(`/api/attachments/${el.dataset.id}/file`); break;
      case 'dep-add': openDepModal(null); break;
      case 'dep-edit': if(e.target.closest('[data-action]').dataset.action==='dep-edit') openDepModal(+el.dataset.id); break;
      case 'dep-del': {
        e.stopPropagation();
        if(!confirm('确定删除这笔定期记录？')) break;
        await jdel('/api/deposits/'+el.dataset.id); renderDeposits(); break;
      }
      case 'tag-add': {
        const name=$('#newTagName').value.trim();
        if(!name){ alert('请输入标签名称'); break; }
        await jpost('/api/tags', {name, color:$('#newTagColor').value});
        await refreshBootstrap(); renderTags(); break;
      }
      case 'tag-save': {
        const card = el.closest('.tag-card');
        await jput('/api/tags/'+el.dataset.id, {
          name: $('[data-role="name"]', card).value.trim(),
          color: $('[data-role="color"]', card).value });
        await refreshBootstrap(); renderTags(); break;
      }
      case 'tag-del': {
        if(!confirm('删除标签将同时从所有流水上移除该标记，确定？')) break;
        await jdel('/api/tags/'+el.dataset.id);
        await refreshBootstrap(); renderTags(); loadReminders(); break;
      }
      case 'tag-view': globalSearchByTag(+el.dataset.id); break;
      case 'tag-alert': globalSearchByTag(+el.dataset.id); break;
      case 'reminders': openReminderModal(); break;
      case 'comp-add': {
        const name = await promptName('新公司名称');
        if(!name) break;
        const created = await jpost('/api/companies', {name});
        await refreshBootstrap();
        S.company = created.id;
        $('#companySelect').value = S.company;
        switchView('orgs'); break;
      }
      case 'comp-rename': {
        const cur = compById(+el.dataset.id);
        const name = await promptName('重命名公司', cur?.name||'');
        if(!name) break;
        await jput('/api/companies/'+el.dataset.id, {name});
        await refreshBootstrap(); renderOrgs(); break;
      }
      case 'comp-del': {
        if(!confirm('删除公司将连同其所有账户、流水、凭证附件一起删除，且不可恢复！确定？')) break;
        await jdel('/api/companies/'+el.dataset.id);
        S.company=null; await refreshBootstrap(); switchView('orgs'); break;
      }
      case 'acc-add': openAccModal(+el.dataset.id, null); break;
      case 'acc-edit': e.stopPropagation(); openAccModal(accById(+el.dataset.id)?.company_id, +el.dataset.id); break;
      case 'acc-del': {
        e.stopPropagation();
        if(!confirm('删除账户将连同其全部流水与附件一起删除，确定？')) break;
        await jdel('/api/accounts/'+el.dataset.id);
        if(S.jr.accId===+el.dataset.id) S.jr={accId:null,account:null,entries:[],loaded:false,from:'',to:'',preset:'thisMonth',q:'',status:'',tagId:''};
        await refreshBootstrap(); renderOrgs(); break;
      }
      case 'rate-add': {
        const cur=$('#newCur').value.trim().toUpperCase(), r=parseFloat($('#newRate').value);
        if(!cur||!(r>0)){ alert('请输入币种和汇率'); break; }
        S.rates[cur]=r;
        await jput('/api/rates', S.rates);
        await refreshBootstrap(); renderSettings(); break;
      }
      case 'rate-save': {
        const out={};
        $$('#rateRows [data-role="rcur"]').forEach((inp,i)=>{
          const cur=inp.value.trim().toUpperCase();
          const v=parseFloat($$('#rateRows [data-role="rval"]')[i].value);
          if(cur&&v>0) out[cur]=v;
        });
        S.rates=out;
        await jput('/api/rates', out);
        alert('汇率已保存'); await refreshBootstrap(); break;
      }
      case 'restore-pick': $('#restoreFile').click(); break;
    }
  }catch(err){ alert(err.message||err); }
});

async function globalSearchByTag(tagId){
  const t = S.tags.find(x=>x.id===tagId);
  const list = await api('/api/search?tag_id='+tagId);
  openModal(modalHead(`🏷️ 标签「${esc(t?.name||'')}」的流水（${list.length} 笔）`)+`
    <div style="max-height:60vh;overflow:auto">
    <table class="tbl"><thead><tr>
      <th>日期</th><th>公司</th><th>账户</th><th>摘要</th>
      <th class="num">收入</th><th class="num">支出</th><th>状态</th></tr></thead><tbody>
    ${list.map(e=>`
      <tr data-action="goto-entry" data-acc="${e.account_id}" data-month="${e.date.slice(0,7)}">
        <td style="white-space:nowrap">${esc(e.date)}</td>
        <td>${esc(e.company_name||'')}</td><td>${esc(e.account_name||'')}</td>
        <td class="desc-cell">${esc(e.description)}</td>
        <td class="num green">${e.debit?fmt(e.debit):''}</td>
        <td class="num red">${e.credit?fmt(e.credit):''}</td>
        <td>${badge(e)}</td></tr>`).join('')
      || '<tr><td colspan="7" class="empty-hint">无</td></tr>'}
    </tbody></table></div>`);
}

document.addEventListener('change', async e=>{
  const el = e.target.closest('[data-change]');
  if(!el) return;
  const act = el.dataset.change;
  if(act==='jr-acc'){
    S.jr={accId:+el.value,account:null,entries:[],loaded:false,from:'',to:'',preset:'thisMonth',q:'',status:'',tagId:''};
    renderJournal();
  }
  else if(act==='jr-preset'){
    S.jr.preset=el.value;
    if(el.value!=='custom') [S.jr.from,S.jr.to]=presetRange(el.value);
    renderJournal();
  }
  else if(act==='jr-from'){ S.jr.from=el.value; S.jr.preset='custom'; renderJournal(); }
  else if(act==='jr-to'){ S.jr.to=el.value; S.jr.preset='custom'; renderJournal(); }
  else if(act==='jr-tag'){ S.jr.tagId=el.value; renderJournal(); }
  else if(act==='cash-date'){ cashDate = el.value; renderCash(); }
  else if(act==='jr-pend-check'){ recalcJournalPend(); }
  else if(act==='plan-check'){
    const acc=accById(S.jr.accId);
    const pend=S.jr.entries.filter(x=>x.pending);
    const book=S.jr.entries.length?S.jr.entries[S.jr.entries.length-1]._bal:acc.opening;
    recalcPlan(book+pend.reduce((s,x)=>s+x.credit-x.debit,0));
  }
});

document.addEventListener('change', async e=>{
  if(e.target.id==='restoreFile' && e.target.files.length){
    const f = e.target.files[0];
    if(!confirm('恢复备份将覆盖当前全部数据，确定继续？')) return;
    const fd = new FormData(); fd.append('file', f);
    const r = await fetch('/api/restore', {method:'POST', body:fd});
    if(r.ok){ alert('恢复成功，页面即将刷新'); location.reload(); }
    else{ let m='恢复失败'; try{ m=(await r.json()).error||m }catch(x){} alert(m); }
  }
});

boot();
