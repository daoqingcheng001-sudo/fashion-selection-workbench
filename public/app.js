const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
const auth = window.SelectionAuth;
const statusText = {pending:'待补证据', eligible:'待审核', approved:'已确认', excluded:'规则排除', rejected:'人工排除'};
const jobStatusText = {running:'采集进行中', paused:'已暂停', terminated:'已终止 · 断点保留', blocked:'访问待恢复', awaiting_review:'待补齐与审核'};
let state, library, rows = [], approvedItems = [];
let page = 'rules', jobId = '', offset = 0, query = '', filter = 'eligible', category = '', currentItem;
let profileId = '', approvedQuery = '', approvedScope = 'all', designSelection = '';
let selectedApproved = new Set(), deliveryReceipts = [], submittingDelivery = false;
let refreshInFlight = null;
let designDraftOwner = '', designDrafts = new Map();
let ruleDrafts = new Map(), ruleFormProfile = null, ruleFormBaseline = null;
let reviewDrafts = new Map(), reviewFormBaseline = null, reviewDraftKey = '';
let designHistoryScope = 'current';
const requestId = () => crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);

async function api(path, data) { return auth.request(path, data); }
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').style.display = 'block';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $('#toast').style.display = 'none', 6000);
}
async function act(fn) { try { await fn(); } catch (error) { toast(error.message); } }
const reviewer = () => auth.user?.display_name || auth.user?.username || '';
const itemKey = item => item.job + ':' + item.id;
function native(message) {
  if (window.webkit?.messageHandlers?.selection) {
    window.webkit.messageHandlers.selection.postMessage(message); return true;
  }
  if (message.action === 'source' && message.url) { window.open(message.url, '_blank', 'noopener'); return true; }
  if (message.action === 'styleReference') { window.open('/reference/风格识别对照库.html', '_blank', 'noopener'); return true; }
  if (message.action === 'stop') return true;
  toast(message.action === 'helperFolder' ? '助手需安装在采集电脑的 Chrome 中，请联系管理员获取安装包。' : '请在本机 Chrome 中打开来源链接并使用采集助手。');
  return false;
}
// Polling regions keep their existing nodes: unchanged images must not be detached
// and fetched again (authenticated assets deliberately use Cache-Control: no-store).
const renderedRegions = new WeakMap();
function patchRegion(selector, html) {
  const root = $(selector);
  const signature = auth.canEdit + ':' + html;
  if (!root || renderedRegions.get(root) === signature) return;
  const template = document.createElement('template');
  template.innerHTML = html;
  const key = node => node.nodeType === 1 ? node.id || node.getAttribute('data-render-key') || '' : '';
  const compatible = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b);
  function syncChildren(current, desired) {
    const old = [...current.childNodes], used = new Set();
    let cursor = current.firstChild;
    for (const next of [...desired.childNodes]) {
      const nextKey = key(next);
      let node = nextKey ? old.find(item => !used.has(item) && key(item) === nextKey && compatible(item, next))
        : cursor && !used.has(cursor) && compatible(cursor, next) ? cursor : null;
      if (!node) {
        node = next.cloneNode(true);
        current.insertBefore(node, cursor);
      } else {
        if (node !== cursor) current.insertBefore(node, cursor);
        syncNode(node, next);
      }
      used.add(node);
      cursor = node.nextSibling;
    }
    for (const node of old) if (!used.has(node)) node.remove();
  }
  function syncNode(current, desired) {
    if (current.nodeType !== 1) {
      if (current.nodeValue !== desired.nodeValue) current.nodeValue = desired.nodeValue;
      return;
    }
    // An expanded disclosure is local view state, not part of the server snapshot.
    const keepAttribute = name => current.tagName === 'DETAILS' && name === 'open';
    for (const attr of [...current.attributes]) {
      if (!keepAttribute(attr.name) && !desired.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    }
    for (const attr of [...desired.attributes]) {
      if (!keepAttribute(attr.name) && current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
    }
    syncChildren(current, desired);
    if (current.tagName === 'INPUT' && ['checkbox', 'radio'].includes(current.type) && current.checked !== desired.checked) current.checked = desired.checked;
    if (current.tagName === 'SELECT' && current.value !== desired.value) current.value = desired.value;
  }
  syncChildren(root, template.content);
  renderedRegions.set(root, signature);
}

function selectedJob() { return state.jobs.find(job => job.id === jobId); }
function itemStage(item) { return item.evaluation.approved ? 'approved' : item.evaluation.status; }
function stageCounts() {
  return rows.reduce((counts, item) => { counts[itemStage(item)] += 1; return counts; }, {pending:0, eligible:0, approved:0, excluded:0, rejected:0});
}
async function startCollection(job) {
  if (job.status === 'running') { toast('此任务正在采集；如助手未连接，请使用“连接 / 配对助手”'); return; }
  await closeDetail();
  const blocked = job.status === 'blocked';
  $('#detail-body').innerHTML = `<div class="dialog-head"><h2>${blocked ? '恢复来源访问，再继续断点' : '开始采集前确认连接'}</h2><button id="close-start">关闭</button></div><div class="confirm-body"><p>任务：<b>${esc(job.rules.name)}</b></p><ol class="operation-steps"><li>在本机 Chrome 打开来源，使用平台正常入口登录或恢复访问。</li><li>首次使用或登录更换后，先通过“连接 / 配对助手”连接此任务。</li><li>确认页面可正常浏览后，继续已保存断点；不会清空已有款式。</li></ol><div class="form-footer"><button id="recovery-source">打开来源检查</button><button id="recovery-pair">连接 / 配对助手</button></div>${blocked ? '<p class="warning">当前为平台访问受限状态。若仍显示验证或 403，请保留断点，不要反复继续。</p>' : ''}<label class="check"><input id="collection-ready" type="checkbox">${blocked ? '我已通过平台正常入口恢复访问，且助手已连接此任务' : 'Chrome 来源页面可正常打开，采集助手已连接此任务'}</label><button id="confirm-start" class="primary" disabled>从保存的断点继续采集</button></div>`;
  $('#detail').showModal();
  $('#close-start').onclick = () => $('#detail').close();
  $('#recovery-source').onclick = () => native({action:'source', url:job.rules.links?.[0], job:job.id});
  $('#recovery-pair').onclick = () => act(async () => { $('#detail').close(); await helperSetup(); });
  $('#collection-ready').onchange = event => $('#confirm-start').disabled = !event.target.checked || !auth.canEdit;
  $('#confirm-start').onclick = () => act(async () => {
    const button = $('#confirm-start'); button.disabled = true;
    try {
      await api('/api/job/action', {job:job.id, action:'resume', ...(blocked ? {recovery_confirmed:true} : {})});
      $('#detail').close(); await refresh(); toast('断点已恢复，请在 Chrome 采集助手中点击“继续采集”');
    } finally { if (button.isConnected) button.disabled = !$('#collection-ready').checked || !auth.canEdit; }
  });
}
async function refresh(options = {}) {
  if (!auth.ready) return;
  if (refreshInFlight) {
    await refreshInFlight;
    // A task switch or explicit refresh must not reuse rows fetched for the old task.
    if (!options.background) return refresh(options);
    return;
  }
  refreshInFlight = (async () => {
    state = await api('/api/state');
    library = library || await api('/api/style');
    jobId = state.jobs.some(job => job.id === jobId) ? jobId : state.jobs[0]?.id || '';
    const loadedJob = jobId;
    const results = await Promise.all([jobId ? api('/api/items?job=' + encodeURIComponent(jobId)) : [], api('/api/approved')]);
    if (loadedJob !== jobId) return;
    rows = results[0];
    approvedItems = results[1].items || [];
    const valid = new Set(approvedItems.map(itemKey));
    selectedApproved = new Set([...selectedApproved].filter(key => valid.has(key)));
    if (options.background && $('#content').children.length) {
      // Progress polling must not replace active forms, their cursor, or typed text.
      preserveView(() => {
        if (document.activeElement?.id !== 'job-select') renderTaskContext();
        if (page === 'designs') { designPreview(); renderDesignHistory(); }
        if (page === 'approved') { approvedCards(); renderDeliveryHistory(); }
        if (page === 'collection' && !$('#detail').open) { updateCollectionProgress(); cards(); }
      });
    } else preserveView(render);
  })();
  try { await refreshInFlight; } finally { refreshInFlight = null; }
}
function render() {
  if (!auth.ready) return;
  // Read live fields immediately before replacing the DOM, including typing during a refresh request.
  captureDesignDraft();
  captureRuleDraft();
  if (page === 'admin' && auth.user.role !== 'admin') page = 'collection';
  const names = {rules:'渠道与规则', collection:'查看与审核', approved:'已确认与交付', designs:'改款与生图', styles:'风格参考库', admin:'成员与后台'};
  $('#page-title').textContent = names[page];
  $('#page-description').textContent = ({rules:'输入来源链接，把选款思路变成可复用规则。', collection:'对照图片和证据，确认值得进入研发的款式。', approved:'汇集审核确认的款式，打包交付或直接进入改款。', designs:'从已确认款式建立改款方案，生成参考图并复核。', styles:'用统一风格语言识别款式，保留每条判断的来源。', admin:'管理团队权限、共享服务与数据库备份。'})[page];
  document.querySelectorAll('#nav [data-page]').forEach(button => {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  renderTaskContext();
  ({rules, collection, approved:approvedPage, designs, styles, admin:adminPage}[page])();
  applyReadOnly();
}
function preserveView(fn) {
  const active = document.activeElement;
  const focusId = active?.id;
  const focusAttributes = ['data-review-id','data-approved-detail','data-open-design','data-preflight','data-edit-design','data-revise-design','data-check-design','data-design-review','data-decision','data-gallery','data-role'].filter(name => active?.hasAttribute?.(name)).map(name => [name, active.getAttribute(name)]);
  const focusedSummary = active?.tagName === 'SUMMARY' ? active.parentElement?.id : '';
  const selection = active && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
  const scroll = [window.scrollX, window.scrollY];
  const details = [...document.querySelectorAll('details')].map(el => ({id:el.id, text:el.querySelector('summary')?.textContent, open:el.open}));
  fn();
  for (const item of details) {
    const el = item.id ? document.getElementById(item.id) : [...document.querySelectorAll('details')].find(node => node.querySelector('summary')?.textContent === item.text);
    if (el && el.open !== item.open) el.open = item.open;
  }
  const restored = focusId ? document.getElementById(focusId) : focusAttributes.length ? [...document.querySelectorAll('[' + focusAttributes[0][0] + ']')].find(el => focusAttributes.every(([name, value]) => el.getAttribute(name) === value)) : focusedSummary ? document.getElementById(focusedSummary)?.querySelector('summary') : active?.isConnected ? active : null;
  if (restored && !restored.disabled && restored !== document.activeElement) { restored.focus({preventScroll:true}); if (selection && restored.setSelectionRange) try { restored.setSelectionRange(...selection); } catch {} }
  if (window.scrollX !== scroll[0] || window.scrollY !== scroll[1]) window.scrollTo(...scroll);
}
function captureRuleDraft() {
  if (!ruleFormProfile || !$('#rule-name')) return;
  const fields = formSnapshot('#rule-form');
  if (JSON.stringify(fields) !== JSON.stringify(ruleFormBaseline)) ruleDrafts.set(ruleFormProfile.id, {profile:{...ruleFormProfile}, fields});
}
function captureReviewDraft() {
  if (!reviewDraftKey || !$('#detail').open || !$('#item-lock-status')) return;
  const fields = formSnapshot('#detail .detail-form');
  if (JSON.stringify(fields) !== JSON.stringify(reviewFormBaseline)) reviewDrafts.set(reviewDraftKey, {revision:currentItem?.revision, fields});
}
function renderTaskContext() {
  const job = selectedJob(), rules = job?.rules || {}, counts = stageCounts();
  patchRegion('#task-context', `<div class="task-picker"><label for="job-select">当前任务 · 查看此任务的记录和规则快照</label><select id="job-select" ${state.jobs.length ? '' : 'disabled'}>${state.jobs.map(item => `<option value="${esc(item.id)}" ${item.id === jobId ? 'selected' : ''}>${esc(item.rules.name)} · ${esc(item.id)}</option>`).join('') || '<option>尚未创建任务</option>'}</select></div><div class="task-context-meta"><span class="tag ${job?.status === 'blocked' ? 'pending' : ''}">${esc(jobStatusText[job?.status] || job?.status || '等待创建')}</span>${job ? `<span>${esc(rules.platform)} · ${esc(rules.country)} · ${rules.min_sold}+ 销量 · ${rules.max_age_months}个月内</span>` : '<span>先完成渠道与规则配置</span>'}</div><button id="global-refresh" class="quiet">刷新</button>`);
  $('#job-select').onchange = event => act(async () => { await closeDetail(); jobId = event.target.value; offset = 0; query = ''; category = ''; await refresh(); });
  $('#global-refresh').onclick = () => act(refresh);
  patchRegion('#workflow-progress', [
    ['rules','01','渠道与规则', job ? '规则 v' + (rules.version || 1) : '输入来源'],
    ['collection','02','查看与审核', counts.eligible + ' 待审核'],
    ['approved','03','已确认与交付', approvedItems.length + ' 已确认'],
    ['designs','04','改款与生图', state.designs.length + ' 个方案']
  ].map(([name, number, title, note]) => `<button class="workflow-step ${page === name ? 'active' : ''}" data-go-page="${name}"><span class="step-number">${number}</span><span><b>${title}</b><small>${note}</small></span><span class="step-arrow" aria-hidden="true">→</span></button>`).join(''));

}
function collection() {
  const job = selectedJob(), rules = job?.rules || {}, counts = stageCounts();
  const supported = rules.platform === 'SHEIN' && String(rules.country).toUpperCase() === 'US';
  const queue = job?.queue || {}, remaining = Number(queue.pending || 0);
  $('#content').innerHTML = `<section class="panel collection-control"><div class="topline"><div><span class="section-kicker">采集进度</span><h2 id="collection-status">${esc(jobStatusText[job?.status] || '等待采集')}</h2><p class="hint" id="collection-counts">已保存 <b>${job?.pages_saved || 0}</b> 页 · <b>${rows.length}</b> 条候选 · 队列 <b>${remaining}</b> 条待执行</p></div><div class="actions"><button id="resume" class="primary" ${!job || !supported ? 'disabled' : ''}>${job?.status === 'blocked' ? '恢复访问后继续' : job?.status === 'running' ? '查看采集助手' : '开始 / 继续采集'}</button><button id="pair-helper" ${!job || !supported ? 'disabled' : ''}>连接 / 配对助手</button><button id="pause" ${job?.status !== 'running' ? 'disabled' : ''}>暂停</button><button id="source" ${rules.links?.[0] ? '' : 'disabled'}>查看来源</button></div></div>${job?.status === 'blocked' ? '<p class="warning">来源平台访问受限，断点已保留。请先通过平台正常入口恢复访问，再继续采集。</p>' : ''}<p class="hint">${rules.age_basis === 'sku_estimate' ? '款龄按 SKU 月份估计，并明确标注；不代表真实首次上架日期。' : '款龄需要首次上架日期与核验证据。'} 销量保留平台原标签及其公开周期。</p><details><summary>查看规则快照与断点记录</summary><div class="task-detail"><span>任务 ${esc(job?.id || '—')} · 基准日期 ${esc(rules.as_of || '—')} · 风格库 v${esc(state.style_version)}</span><div class="actions"><button id="events" ${job ? '' : 'disabled'}>读取采集记录</button><button id="terminate" ${!job || job.status === 'terminated' ? 'disabled' : ''}>终止本次采集</button></div></div><pre>${esc(JSON.stringify({links:rules.links, min_sold:rules.min_sold, max_age_months:rules.max_age_months, age_basis:rules.age_basis, excluded_terms:rules.excluded_terms}, null, 2))}</pre><pre id="event-log" hidden></pre></details></section><div class="next-step"><b>下一步：先补资料，再审核</b><span>列表候选不等于合格款。打开“待补证据”查看缺项；资料齐备后进入“待审核”，确认选用后自动进入交付与改款。</span></div><section class="review-workspace"><div class="topline section-heading"><div><h2>款式审核</h2><p class="hint">通过筛选后仍需人工确认；缺失证据的款式留在待补区。</p></div><button data-go-page="approved">查看已确认款式 →</button></div><div class="stage-tabs" role="tablist" aria-label="审核阶段">${[['eligible','待审核',counts.eligible],['pending','待补证据',counts.pending],['approved','已确认',counts.approved],['excluded','已排除',counts.excluded + counts.rejected],['all','全部',rows.length]].map(([value, label, count]) => `<button role="tab" aria-selected="${filter === value}" data-filter="${value}" class="${filter === value ? 'active' : ''}">${label}<span>${count}</span></button>`).join('')}</div><div class="filters"><input id="search" aria-label="搜索当前任务的款式" placeholder="搜索商品 ID、名称、元素或颜色" value="${esc(query)}"><select id="cat-filter" aria-label="按类目筛选"><option value="">全部类目</option>${[...new Set(rows.map(item => item.category_leaf || item.breadcrumb?.at(-1) || '类目待采'))].sort().map(value => `<option value="${esc(value)}" ${category === value ? 'selected' : ''}>${esc(value)}</option>`).join('')}</select></div><div id="cards" class="grid"></div><div class="pager"><button id="prev">上一页</button><span id="page-info"></span><button id="next">下一页</button></div></section>`;
  $('#resume').onclick = () => act(() => startCollection(job));
  $('#pair-helper').onclick = () => act(helperSetup);
  $('#pause').onclick = () => act(async () => { await api('/api/job/action', {job:jobId, action:'pause'}); native({action:'stop'}); await refresh(); });
  $('#terminate').onclick = () => act(async () => { await api('/api/job/action', {job:jobId, action:'terminate'}); native({action:'stop'}); await refresh(); toast('采集已终止，已保存记录可继续审核'); });
  $('#source').onclick = () => native({action:'source', url:rules.links?.[0], job:jobId});
  $('#events').onclick = () => act(async () => { const log = $('#event-log'); log.hidden = false; log.textContent = JSON.stringify(await api('/api/events?job=' + encodeURIComponent(jobId)), null, 2); });
  document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => { filter = button.dataset.filter; offset = 0; collection(); applyReadOnly(); });
  $('#search').oninput = event => { query = event.target.value; offset = 0; cards(); };
  $('#cat-filter').onchange = event => { category = event.target.value; offset = 0; cards(); };
  $('#prev').onclick = () => { offset = Math.max(0, offset - 12); cards(); };
  $('#next').onclick = () => { offset += 12; cards(); };
  cards();
}
function visibleRows() {
  return rows.filter(item => {
    const stage = itemStage(item);
    const matchesStage = filter === 'all' || (filter === 'excluded' ? ['excluded','rejected'].includes(stage) : stage === filter);
    return matchesStage && (!query || JSON.stringify([item.id,item.name,item.colors,item.review]).toLowerCase().includes(query.toLowerCase())) && (!category || (item.category_leaf || item.breadcrumb?.at(-1) || '类目待采') === category);
  });
}
function updateCollectionProgress() {
  const job = selectedJob(); if (!job || !$('#collection-status')) return;
  $('#collection-status').textContent = jobStatusText[job.status] || job.status;
  $('#collection-counts').textContent = `已保存 ${job.pages_saved || 0} 页 · ${rows.length} 条候选 · 队列 ${job.queue?.pending || 0} 条待执行`;
  $('#resume').textContent = job.status === 'blocked' ? '恢复访问后继续' : job.status === 'running' ? '采集正在进行' : '开始 / 继续采集';
  $('#resume').onclick = () => act(() => startCollection(selectedJob()));
  $('#pause').disabled = !auth.canEdit || job.status !== 'running';
  const counts = stageCounts();
  for (const button of document.querySelectorAll('[data-filter]')) {
    const stage = button.dataset.filter;
    button.querySelector('span').textContent = stage === 'all' ? rows.length : stage === 'excluded' ? counts.excluded + counts.rejected : counts[stage];
  }
}
function pic(url, role) { return url ? `<img src="${esc(url)}" alt="${esc(role)}" loading="lazy">` : `<div class="missing">${esc(role)}<br>待补</div>`; }
function productPictures(item) {
  return `<div class="product-pair"><figure>${pic(item.front_url,'正面原图')}<figcaption>正面</figcaption></figure><figure>${pic(item.back_url,'背面原图')}<figcaption>背面</figcaption></figure></div>`;
}
function colorPictures(item) {
  return `<div class="colors">${(item.colors || []).slice(0,8).map(color => color.image_url ? `<img src="${esc(color.image_url)}" title="${esc(color.name)}" alt="${esc(color.name)}">` : `<span class="missing-color" title="${esc(color.name)}图待补">?</span>`).join('') || '<span class="hint">复色图待补</span>'}${item.colors?.length > 8 ? `<span class="hint">+${item.colors.length - 8}</span>` : ''}</div>`;
}
function cards() {
  const list = visibleRows();
  if (offset >= list.length) offset = Math.max(0, Math.floor((list.length - 1) / 12) * 12);
  patchRegion('#cards', list.slice(offset, offset + 12).map(item => {
    const evaluation = item.evaluation, stage = itemStage(item);
    return `<article class="card" data-render-key="${esc(itemKey(item))}">${productPictures(item)}<div class="card-body"><div class="tags"><span class="tag sales">${esc(item.sales || '销量待核')}</span><span class="tag ${stage}">${statusText[stage]}</span></div><h3 title="${esc(item.name)}">${esc(item.name)}</h3><p class="card-category">${esc(item.category_leaf || item.breadcrumb?.at(-1) || '类目待采')}</p><div class="hint">${esc(evaluation.age_status)}${evaluation.sku_month_clue ? ' · ' + esc(evaluation.sku_month_clue) : ''}<br>${item.color_count ?? '待核'} 个颜色选项</div>${colorPictures(item)}${stage === 'pending' || stage === 'excluded' || stage === 'rejected' ? `<p class="evidence-note">${esc(evaluation.reasons?.slice(0,2).join('；') || item.review?.reason || '待复核')}</p>` : ''}<div class="card-foot"><span>#${esc(item.id)}</span><button id="review-${esc(item.id)}" data-review-id="${esc(item.id)}">${stage === 'approved' ? '查看审核' : '查看与审核'} →</button></div></div></article>`;
  }).join('') || `<div class="empty empty-wide"><span class="empty-symbol">◇</span><h3>${filter === 'eligible' ? '暂无可审核款式' : '当前视图没有款式'}</h3><p>${filter === 'eligible' && stageCounts().pending ? '候选款仍有款龄、图片或基础款判断待补。先进入“待补证据”完善资料。' : '可调整筛选条件，或在渠道与规则中创建采集任务。'}</p><button ${filter === 'eligible' && stageCounts().pending ? 'data-empty-pending' : 'data-go-page="rules"'}>${filter === 'eligible' && stageCounts().pending ? '查看待补证据' : '配置渠道与规则'}</button></div>`);
  $('#page-info').textContent = `${list.length} 款 · 第 ${Math.floor(offset / 12) + 1} / ${Math.max(1,Math.ceil(list.length / 12))} 页`;
  $('#prev').disabled = offset === 0;
  $('#next').disabled = offset + 12 >= list.length;
  document.querySelectorAll('[data-review-id]').forEach(button => button.onclick = () => act(() => detail(button.dataset.reviewId)));
  if ($('[data-empty-pending]')) $('[data-empty-pending]').onclick = () => { filter = 'pending'; collection(); applyReadOnly(); };
}

function styleOptions(current){return `<option value="">待图审</option>`+library.styles.map(s=>`<optgroup label="${esc(s.name)}">${s.substyles.map(x=>`<option value="${esc(x)}" ${current===x?'selected':''}>${esc(x)}</option>`).join('')}</optgroup>`).join('');}
async function detail(id){await closeDetail();const lock=await acquireItemLock(id);if(!auth.ready)return;let fresh;try{fresh=await api('/api/items?job='+jobId);}catch(e){await releaseItemLock();throw e;}rows=fresh;currentItem=rows.find(x=>x.id===id);if(!currentItem){await releaseItemLock();throw Error('商品记录已变更，请刷新列表');}const d=currentItem,r=d.review,e=d.evaluation;reviewDraftKey=jobId+':'+id;$('#detail-body').innerHTML=`<div class="dialog-head"><div><h2>图文审核 · #${esc(id)}</h2><div id="item-lock-status" class="hint">${esc(lock.message)}</div><div class="hint">${esc(d.category||d.breadcrumb?.join(' › '))}</div></div><button id="close-detail">关闭</button></div><div class="dialog-content"><div><div class="detail-pictures"><div>${pic(d.front_url,'正面')}<p class="label">正面原图</p></div><div>${pic(d.back_url,'背面')}<p class="label">背面原图</p></div></div><div class="detail-colors">${d.colors?.map((c,i)=>`<div>${c.image_url?`<img src="${esc(c.image_url)}" alt="${esc(c.name)}">`:'<span>待补图</span>'}<p class="label">${i+1}. ${esc(c.name)}</p></div>`).join('')}</div><p class="detail-title">${esc(d.name)}</p><p><span class="tag sales">${esc(d.sales)}</span> <span class="hint">${esc(e.sales_period)}</span></p><button id="open-product">打开来源商品</button><div class="warning">${e.reasons.map(esc).join('；')||'筛选条件已齐备，等待设计师选择'}</div><details><summary>补图与原始详情</summary><div id="upload-area"></div><div id="gallery">${d.local_gallery?.map((x,i)=>`<div class="upload-row"><img src="${esc(x.url||'')}" width="70" alt="待分配图"><button data-gallery="${i}" data-role="front">设为正面</button><button data-gallery="${i}" data-role="back">设为背面</button></div>`).join('')||''}</div><pre>${esc(JSON.stringify({sku:d.sku,breadcrumb:d.breadcrumb,observed_at:d.fresh_capture_at||d.detail_observed_at||d.observed_at||'历史记录时间待核',source:d.url},null,2))}</pre></details></div><div class="detail-form"><div class="next-step"><b>审核顺序</b><span>核对正背面和复色 → 判断是否时尚款 → 记录风格与元素 → 确认选用或排除。</span></div><p id="review-draft-note" class="hint"></p><label>首次上架日期<input id="first-listed" type="date" value="${esc(r.first_listed_at||'')}"></label><label>日期证据来源 / 链接<input id="date-source" placeholder="平台明确日期、后台导出或已核实资料" value="${esc(r.date_source||'')}"></label><label class="check"><input type="checkbox" id="date-verified" ${r.date_verified?'checked':''}>我已核验日期依据，非仅SKU推测</label><label>是否简单基础款<select id="is-basic"><option value="unknown">待图审</option><option value="no" ${r.is_basic===false?'selected':''}>非基础款，符合时尚款范围</option><option value="yes" ${r.is_basic===true?'selected':''}>是，排除</option></select></label><label>主风格<select id="primary-style">${styleOptions(r.primary_style)}</select></label><p class="hint">标题元素线索（需图审）：${esc(d.element_suggestions?.join("、")||"无明确线索")}</p><label>元素提取<textarea id="elements" rows="3" placeholder="领型、袖型、廓形、露肤、装饰部位；只记录可见依据">${esc(r.elements||'')}</textarea></label><label>选择 / 排除理由<textarea id="reason" rows="3">${esc(r.reason||'')}</textarea></label><div class="actions"><button id="save-pending">保存待核</button><button id="approve" class="primary">确认选用</button><button id="reject">排除</button></div><button id="to-design">创建改款方案 →</button><p class="hint">已选必须满足销量、款龄、图片和范围条件。审核更正记录保留，不修改原销量或来源。</p></div></div>`;$('#detail').showModal();$('#close-detail').onclick=()=>act(closeDetail);$('#open-product').onclick=()=>native({action:'source',url:d.url,job:jobId});for(const [bid,decision]of [['save-pending','pending'],['approve','approved'],['reject','rejected']])$('#'+bid).onclick=()=>act(async()=>{const review={reviewer:reviewer(),decision,first_listed_at:$('#first-listed').value,date_source:$('#date-source').value,date_verified:$('#date-verified').checked,is_basic:$('#is-basic').value==='unknown'?null:$('#is-basic').value==='yes',primary_style:$('#primary-style').value,elements:$('#elements').value,reason:$('#reason').value};await api('/api/review',{job:jobId,product_id:id,review,expected_revision:currentItem.revision});reviewFormBaseline=formSnapshot('#detail .detail-form');reviewDrafts.delete(reviewDraftKey);await closeDetail();await refresh();toast(decision==='approved'?'已确认，已进入交付与改款款式库':'审核已保存并回读');});$('#to-design').textContent=e.approved?'调入改款 →':'先确认选用，再调入改款';$('#to-design').onclick=()=>act(async()=>{if(!currentItem.evaluation.approved)throw Error('请先审核确认此款');const key=jobId+':'+id;await closeDetail();await openDesign(key);});$('#upload-area').innerHTML=['front','back',...d.colors.map((_,i)=>'color:'+i)].map(role=>`<div class="upload-row"><label>${role==='front'?'正面':role==='back'?'背面':'颜色'+(Number(role.split(':')[1])+1)}<input type="file" accept="image/*" data-upload="${role}"></label></div>`).join('');document.querySelectorAll('[data-upload]').forEach(el=>el.onchange=()=>act(async()=>{const bytes=await fileBase64(el.files[0]);await api('/api/image',{job:jobId,product_id:id,role:el.dataset.upload,bytes,source_url:'员工上传，请按原图来源复核',expected_revision:currentItem.revision});await closeDetail();await refresh();await detail(id);}));reviewFormBaseline=formSnapshot('#detail .detail-form');const cachedReview=reviewDrafts.get(reviewDraftKey);if(cachedReview){if(cachedReview.revision===d.revision){restoreFields(cachedReview.fields);$('#review-draft-note').textContent='已恢复本会话未提交的审核输入，请核对后保存。';}else{$('#review-draft-note').innerHTML='该款资料已更新，旧审核输入尚未恢复。<button id="restore-review-draft">恢复旧输入供核对</button>';$('#restore-review-draft').onclick=()=>{restoreFields(cachedReview.fields);$('#review-draft-note').textContent='已恢复旧输入，请逐项核对新资料后再保存。';};}}setItemWritable(lock.acquired);}
async function fileBase64(file){return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});}

function styles(){$('#content').innerHTML=`<section class="panel"><h2>风格识别参考库 v${esc(library.version)}</h2><p class="hint">${esc(library.status)}。主风格最多1项、辅助风格最多2项；风格与模拍/挂拍/平铺分开。历史企划保留年份和页码，不自动作为当前趋势。</p><button id="open-style">打开完整图文参考库</button></section><div class="style-grid">${library.styles.map(s=>`<article class="style-card"><h3>${esc(s.name)}</h3><div class="tags">${s.substyles.map(x=>`<span class="tag">${esc(x)}</span>`).join('')}</div><p>${s.supporting_cues.map(esc).join('；')}</p><p>${esc(s.boundary)}</p><details><summary>查看来源页码</summary>${s.references.map(r=>`<p>${esc(r.file)} · ${esc(r.pages.join('、'))}页</p>`).join('')}</details></article>`).join('')}</div>`;$('#open-style').onclick=()=>native({action:'styleReference'});}

function rules() {
  const profile = state.profiles.find(item => item.id === profileId) || state.profiles.find(item => item.id === 'icon-us') || state.profiles[0];
  if (!profile) {
    $('#content').innerHTML = '<section class="panel empty"><h2>规则模板暂不可用</h2><p>请联系管理员检查模板数据。</p></section>';
    return;
  }
  profileId = profile.id;
  $('#content').innerHTML = `<section class="panel rules-panel"><div class="topline"><div><span class="section-kicker">STEP 01 · 选款任务</span><h2>从渠道链接与选款思路开始</h2></div><label>使用规则模板<select id="profile-picker">${state.profiles.map(item => `<option value="${esc(item.id)}" ${item.id === profile.id ? 'selected' : ''}>${esc(item.name)} · v${item.version}</option>`).join('')}</select></label></div><div class="next-step"><b>这里配置下一轮任务，不会修改页头当前任务</b><span>填写链接和条件 → 保存并创建任务 → 进入第 2 步开始采集。已有任务始终使用创建时的规则快照。</span></div><p id="rule-draft-status" class="hint"></p><div id="rule-form"></div></section><section class="panel"><details class="import-details"><summary>已有资料：导入商品记录与日期证据</summary><p class="hint">导入到页头当前任务，自动按该任务规则筛选。采集运行中请先暂停再导入。</p><div class="form-grid"><label>商品记录 JSON（数组，含 id/name/url/sales 等）<input type="file" id="import-json" accept=".json" ${jobId ? '' : 'disabled'}></label><label>已核实日期 CSV（product_id,first_listed_at,source）<input type="file" id="import-dates" accept=".csv" ${jobId ? '' : 'disabled'}></label></div><p class="hint">日期证据来源必填；SKU 月份不能导入为已核实上架日期。</p></details></section>`;
  fillRuleForm(profile);
  $('#profile-picker').onchange = event => {
    captureRuleDraft();
    profileId = event.target.value;
    fillRuleForm(state.profiles.find(item => item.id === profileId));
    applyReadOnly();
  };
  $('#import-json').onchange = event => act(async () => {
    if (!event.target.files[0]) return;
    const result = await api('/api/import', {job:jobId, rows:JSON.parse(await event.target.files[0].text())});
    page = 'collection'; filter = 'pending'; await refresh(); toast(`已导入 ${result.count} 条，按当前任务规则筛选`);
  });
  $('#import-dates').onchange = event => act(async () => {
    if (!event.target.files[0]) return;
    const result = await api('/api/dates', {job:jobId, csv:await event.target.files[0].text(), reviewer:reviewer()});
    page = 'collection'; await refresh(); toast(`已保存 ${result.count} 条日期证据`);
  });
}
function fillRuleForm(profile) {
  const cached = ruleDrafts.get(profile.id);
  const latestVersion = profile.version;
  if (cached) profile = cached.profile;
  ruleFormProfile = profile;
  $('#rule-form').innerHTML = `<fieldset class="rule-group"><legend><span>01</span> 选款渠道</legend><div class="form-grid"><label>规则 / 任务名称<input id="rule-name" required value="${esc(profile.name)}"></label><label>店铺名称<input id="rule-store" value="${esc(profile.store)}"></label><label>来源渠道<select id="rule-platform">${['SHEIN','TEMU','Instagram','Pinterest','独立站'].map(value => `<option ${profile.platform === value ? 'selected' : ''}>${value}</option>`).join('')}</select></label><label>国家 / 站点代码<input id="rule-country" placeholder="如 US" value="${esc(profile.country)}"></label><label class="full">来源链接 <span class="field-help">每行一个店铺、类目或商品链接</span><textarea id="rule-links" rows="3" placeholder="https://us.shein.com/…">${esc((profile.links || []).join('\n'))}</textarea></label></div><p id="channel-capability" class="capability"></p></fieldset><fieldset class="rule-group"><legend><span>02</span> 筛选思路</legend><div class="form-grid"><label>最低销量标签下限<input type="number" id="rule-sold" value="${profile.min_sold}" min="0"></label><label>款龄上限（月）<input type="number" id="rule-age" value="${profile.max_age_months}" min="1" max="36"></label><label>筛选基准日期<input type="date" id="rule-asof" value="${esc(profile.as_of)}"></label><label>款龄依据<select id="rule-basis"><option value="verified">已核实的真实首次上架日期</option><option value="sku_estimate" ${profile.age_basis === 'sku_estimate' ? 'selected' : ''}>SKU 月份估计（单独标注估计）</option></select></label><label>指定类目关键词<input id="rule-categories" placeholder="逗号分隔；留空为全部女装" value="${esc((profile.category_terms || []).join(','))}"></label><label>指定风格<input id="rule-styles" placeholder="逗号分隔；留空不限风格" value="${esc((profile.style_labels || []).join(','))}"></label><label class="full">排除品类关键词<textarea id="rule-excludes" rows="2">${esc((profile.excluded_terms || []).join(','))}</textarea></label><label class="check full"><input type="checkbox" id="rule-basics" ${profile.exclude_basics ? 'checked' : ''}>排除简单基础款，由员工逐款图审确认</label></div></fieldset><fieldset class="rule-group"><legend><span>03</span> 采集范围</legend><div class="form-grid"><label>最大列表页数<input type="number" id="rule-pages" value="${profile.max_pages}" min="1" max="100"></label><div class="rule-summary"><b>只看销量，不考虑成本</b><p class="hint">销量保留原标签；缺日期、图片或类目时标记待补，不能自动成为已确认款式。</p></div></div></fieldset><div class="rule-submit"><div><b>配置完成后，新建一轮采集</b><p class="hint">先保存本页规则，再创建任务；已有任务的规则快照与记录保留。</p></div><button id="new-job" class="primary">保存规则并新建采集任务 →</button></div><div class="form-footer secondary-actions"><button id="save-rule">仅保存规则版本</button><button id="copy-rule">保存为新模板</button><button id="export-rule">导出规则包</button></div><details><summary>已有记录：按当前规则重新筛选</summary><p class="hint">重筛目标：${esc(selectedJob()?.rules?.name || '尚未选择任务')}（${esc(jobId || '—')}）。仅使用其已保存记录，不读取新页面；按本页规则生成独立任务。</p><button id="refilter-job" ${jobId && rows.length ? '' : 'disabled'}>重筛已有 ${rows.length} 条记录</button></details>`;
  function read() {
    const terms = selector => $(selector).value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
    const value = {...profile, name:$('#rule-name').value.trim(), store:$('#rule-store').value.trim(), platform:$('#rule-platform').value, country:$('#rule-country').value.trim().toUpperCase(), links:$('#rule-links').value.split('\n').map(item => item.trim()).filter(Boolean), min_sold:Number($('#rule-sold').value), max_age_months:Number($('#rule-age').value), as_of:$('#rule-asof').value, age_basis:$('#rule-basis').value, category_terms:terms('#rule-categories'), excluded_terms:terms('#rule-excludes'), style_labels:terms('#rule-styles'), max_pages:Number($('#rule-pages').value), exclude_basics:$('#rule-basics').checked};
    if (!value.name || !value.links.length || !value.country || !value.as_of) throw Error('请填写规则名称、来源链接、站点与筛选基准日期');
    if (!Number.isFinite(value.min_sold) || value.min_sold < 0 || !Number.isInteger(value.max_age_months) || value.max_age_months < 1 || value.max_age_months > 36 || !Number.isInteger(value.max_pages) || value.max_pages < 1 || value.max_pages > 100) throw Error('请检查销量、款龄和页数：款龄 1–36 个月，页数 1–100');
    return value;
  }
  async function save(copy = false) {
    const value = read();
    if (copy) { delete value.id; delete value._revision; return api('/api/profile', value); }
    const saved = await withLease('profile:' + profile.id, () => api('/api/profile', {...value, expected_revision:profile._revision}));
    // A successful save advances the form revision even if the following job request fails.
    profile = {...saved, _revision:saved.version};
    ruleFormProfile = profile;
    ruleFormBaseline = formSnapshot('#rule-form');
    ruleDrafts.delete(profile.id);
    return saved;
  }
  function capability() {
    const automatic = $('#rule-platform').value === 'SHEIN' && $('#rule-country').value.trim().toUpperCase() === 'US';
    $('#channel-capability').textContent = automatic ? '● SHEIN 美国站 · 支持 Chrome 采集助手与断点续采' : '○ 该渠道支持保存链接与规则、导入资料后筛选；自动采集暂未接入';
    $('#channel-capability').classList.toggle('manual', !automatic);
    $('#new-job').textContent = automatic ? '保存规则并新建采集任务 →' : '保存规则并新建导入任务 →';
  }
  $('#rule-platform').onchange = capability;
  $('#rule-country').oninput = capability;
  ruleFormBaseline = formSnapshot('#rule-form');
  if (cached) restoreFields(cached.fields);
  const draftStatus = $('#rule-draft-status');
  if (draftStatus) draftStatus.textContent = cached ? (String(latestVersion) !== String(profile.version) ? '已恢复本会话草稿；共享模板已有新版本。保存时会检查冲突，请先核对。' : '已恢复本会话未提交的规则草稿。刷新或切页不会自动提交；关闭本窗口后不保留。') : '规则草稿仅在当前会话中保留，点击保存后才写入共享工作台。';
  capability();
  $('#new-job').onclick = () => act(async () => {
    const button = $('#new-job'); button.disabled = true; button.textContent = '正在保存规则与创建任务…';
    try {
      const saved = await save();
      const result = await api('/api/job', {profile:saved.id, expected_profile_revision:saved.version});
      jobId = result.id; page = 'collection'; filter = 'pending'; query = ''; category = ''; offset = 0;
      await refresh();
      toast(saved.platform === 'SHEIN' && saved.country === 'US' ? '新任务已创建。首次使用先连接 / 配对助手，再开始采集' : '导入任务已创建，请在渠道与规则中导入资料');
    } finally { if (button.isConnected) { button.disabled = !auth.canEdit; capability(); } }
  });
  $('#save-rule').onclick = () => act(async () => { await save(); await refresh(); toast('规则新版本已保存，已有任务保持原规则'); });
  $('#copy-rule').onclick = () => act(async () => { const saved = await save(true); profileId = saved.id; await refresh(); toast('新模板已保存'); });
  $('#export-rule').onclick = () => act(() => downloadJSON(read(), '选款规则包.json'));
  $('#refilter-job').onclick = () => act(async () => {
    await closeDetail();
    $('#detail-body').innerHTML = `<div class="dialog-head"><h2>重筛已有记录</h2><button id="cancel-refilter">取消</button></div><div class="confirm-body"><p>将保存本页规则，并用它重新筛选当前任务的 <b>${rows.length} 条已存记录</b>。</p><p class="hint">此操作不会访问新页面或补采商品。系统会新建重筛任务，保留原任务与原始证据。</p><button id="confirm-refilter" class="primary">保存规则并重筛 ${rows.length} 条</button></div>`;
    $('#detail').showModal();
    $('#cancel-refilter').onclick = () => $('#detail').close();
    $('#confirm-refilter').onclick = () => act(async () => {
      const button = $('#confirm-refilter'); button.disabled = true;
      try {
        const saved = await save();
        const result = await api('/api/job/refilter', {job:jobId, profile:saved.id, expected_profile_revision:saved.version});
        jobId = result.id; page = 'collection'; filter = 'eligible'; offset = 0;
        $('#detail').close(); await refresh(); toast('重筛任务已创建，原任务保留');
      } finally { if (button.isConnected) button.disabled = !auth.canEdit; }
    });
  });
}
function downloadJSON(value, name) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type:'application/json'}));
  link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
function approvedVisibleRows() {
  return approvedItems.filter(item => (approvedScope === 'all' || item.job === jobId) && (!approvedQuery || JSON.stringify([item.id,item.name,item.source_job_name,item.review]).toLowerCase().includes(approvedQuery.toLowerCase())));
}
function approvedPage() {
  const building = state.exports.some(record => record.scope === 'approved_delivery' && record.status === 'building');
  $('#content').innerHTML = `<section class="panel approved-intro"><div class="topline"><div><span class="section-kicker">STEP 03 · 已确认款式库</span><h2>把审核结果交给下一环节</h2><p class="hint">汇总各任务中已通过规则且审核确认的款式。选择后生成含原图的交付包，也可直接调入改款。</p></div><div class="confirmed-total"><b>${approvedItems.length}</b><span>已确认款式记录</span></div></div><div class="delivery-toolbar"><label class="check"><input type="checkbox" id="select-visible" ${auth.canEdit && approvedVisibleRows().length ? '' : 'disabled'}>选择当前列表</label><span id="selection-count"></span><button id="clear-selected" class="quiet">清空选择</button><button id="create-delivery" class="primary" ${!auth.canEdit || !selectedApproved.size || submittingDelivery || building ? 'disabled' : ''}>${building ? '交付包生成中…' : '打包并生成图文表格'}</button></div><div class="next-step"><b>下一步：勾选需要交付的款式，点击打包</b><span>生成后在下方“交付记录”下载 Excel 和原图包。只改款时，直接点款式卡片“调入改款”，无需先导出。</span></div><p class="hint">交付内容：图文 Excel、正面 / 背面 / 复色原图，以及来源、规则和审核记录。按款式记录交付，不合并不同任务的销量。</p></section><div class="filters"><input id="approved-search" aria-label="搜索已确认款式" placeholder="搜索已确认款式、元素或来源任务" value="${esc(approvedQuery)}"><select id="approved-scope" aria-label="已确认款式范围"><option value="all" ${approvedScope === 'all' ? 'selected' : ''}>所有任务的已确认款式</option><option value="current" ${approvedScope === 'current' ? 'selected' : ''}>仅当前任务</option></select></div><div id="approved-cards" class="grid"></div><section class="panel delivery-history"><div class="topline"><div><h2>交付记录</h2><p class="hint">生成后保存在工作台，团队可随时下载。</p></div><button id="refresh-delivery">刷新状态</button></div><div id="export-list"></div><details><summary>过程数据导出（供内部复核）</summary><p class="hint">包含待补和排除项的文件属于过程记录，不是最终选款交付。</p><div class="form-footer"><button data-export="all" ${jobId ? '' : 'disabled'}>导出当前任务全部进度</button><button data-export="eligible" ${jobId ? '' : 'disabled'}>导出当前任务规则通过记录</button></div></details></section>`;
  $('#approved-search').oninput = event => { approvedQuery = event.target.value; approvedCards(); };
  $('#approved-scope').onchange = event => { approvedScope = event.target.value; approvedCards(); };
  $('#select-visible').onchange = event => {
    for (const item of approvedVisibleRows()) if (event.target.checked) selectedApproved.add(itemKey(item)); else selectedApproved.delete(itemKey(item));
    approvedCards();
  };
  $('#clear-selected').onclick = () => { selectedApproved.clear(); approvedCards(); };
  $('#create-delivery').onclick = () => act(async () => {
    if (submittingDelivery || !selectedApproved.size) return;
    submittingDelivery = true;
    const button = $('#create-delivery'); button.disabled = true; button.textContent = '正在提交交付包…';
    try {
      const items = approvedItems.filter(item => selectedApproved.has(itemKey(item))).map(item => ({job:item.job, product_id:item.id, expected_revision:item.revision}));
      const receipt = await api('/api/delivery', {items});
      deliveryReceipts.unshift({...receipt, scope:'approved_delivery', kind:'zip'});
      await refresh();
      toast(receipt.reused ? '已定位已有交付文件，无需重复生成' : receipt.status === 'ready' ? `已生成 ${receipt.count} 款交付文件，可下载` : `已提交 ${receipt.count} 款，正在生成交付包`);
      document.getElementById('delivery-' + receipt.id)?.scrollIntoView({block:'nearest'});
    } finally { submittingDelivery = false; if (page === 'approved' && auth.ready) { approvedPage(); applyReadOnly(); } }
  });
  $('#refresh-delivery').onclick = () => act(refresh);
  document.querySelectorAll('[data-export]').forEach(button => button.onclick = () => act(async () => {
    button.disabled = true;
    try { await api('/api/export', {job:jobId, scope:button.dataset.export}); await refresh(); toast('过程表格正在生成，完成后可下载'); }
    finally { if (button.isConnected) button.disabled = !auth.canEdit; }
  }));
  approvedCards();
  renderDeliveryHistory();
}
function approvedCards() {
  const visible = approvedVisibleRows();
  patchRegion('#approved-cards', visible.map(item => `<article class="card approved-card" data-render-key="${esc(itemKey(item))}"><label class="card-select"><input id="select-${esc(itemKey(item))}" type="checkbox" data-select-approved="${esc(itemKey(item))}" ${selectedApproved.has(itemKey(item)) ? 'checked' : ''} ${auth.canEdit ? '' : 'disabled'}><span>选择此款</span></label>${productPictures(item)}<div class="card-body"><div class="tags"><span class="tag approved">审核已确认</span><span class="tag sales">${esc(item.sales)}</span></div><h3 title="${esc(item.name)}">${esc(item.name)}</h3><p class="card-category">${esc(item.category_leaf || item.breadcrumb?.join(' › ') || '类目待核')}</p><div class="hint">${esc(item.review?.primary_style || '风格待核')} · ${item.color_count ?? '待核'} 个颜色选项<br>${esc(item.evaluation.age_status)}</div>${colorPictures(item)}<p class="source-task" title="${esc(item.source_job_name)}">来源：${esc(item.source_job_name || item.job)}<br>确认人：${esc(item.review?.reviewer || '待核')}</p><div class="approved-card-actions"><button data-approved-detail="${esc(itemKey(item))}">查看图文</button><button class="primary" data-open-design="${esc(itemKey(item))}" ${auth.canEdit ? '' : 'disabled'}>调入改款 →</button></div></div></article>`).join('') || (approvedItems.length ? '<div class="empty empty-wide"><span class="empty-symbol">◇</span><h3>没有匹配的已确认款式</h3><p>试试其他关键词，或切换为所有任务查看。</p><button id="reset-approved-filters">清除搜索与范围筛选</button></div>' : '<div class="empty empty-wide"><span class="empty-symbol">✓</span><h3>还没有审核确认的款式</h3><p>在“查看与审核”中补齐证据并确认选用后，款式会自动进入这里。</p><button data-go-page="collection">前往查看与审核 →</button></div>'));
  if ($('#reset-approved-filters')) $('#reset-approved-filters').onclick = () => { approvedQuery = ''; approvedScope = 'all'; $('#approved-search').value = ''; $('#approved-scope').value = 'all'; approvedCards(); };
  $('#selection-count').textContent = `已选择 ${selectedApproved.size} 款`;
  const selectAll = $('#select-visible');
  selectAll.disabled = !auth.canEdit || !visible.length;
  selectAll.checked = visible.length > 0 && visible.every(item => selectedApproved.has(itemKey(item)));
  selectAll.indeterminate = !selectAll.checked && visible.some(item => selectedApproved.has(itemKey(item)));
  $('#clear-selected').disabled = !selectedApproved.size;
  const building = state.exports.some(record => record.scope === 'approved_delivery' && record.status === 'building');
  $('#create-delivery').disabled = !auth.canEdit || !selectedApproved.size || submittingDelivery || building;
  $('#create-delivery').textContent = submittingDelivery ? '正在提交交付包…' : building ? '交付包生成中…' : '打包并生成图文表格';
  document.querySelectorAll('[data-select-approved]').forEach(input => input.onchange = () => {
    if (input.checked) selectedApproved.add(input.dataset.selectApproved); else selectedApproved.delete(input.dataset.selectApproved);
    approvedCards();
  });
  document.querySelectorAll('[data-approved-detail]').forEach(button => button.onclick = () => act(async () => {
    const item = approvedItems.find(item => itemKey(item) === button.dataset.approvedDetail);
    if (!item) throw Error('该款审核状态已更新，请刷新款式库');
    jobId = item.job; await refresh(); await detail(item.id);
  }));
}
function renderDeliveryHistory() {
  const seen = new Set();
  const records = [...state.exports, ...deliveryReceipts].filter(record => {
    if (record.scope === 'approved_delivery') {
      if (record.kind === 'xlsx') return false;
      const key = record.delivery_id || record.id;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    }
    return record.job === jobId;
  });
  patchRegion('#export-list', records.map(record => {
    const isDelivery = record.scope === 'approved_delivery';
    const files = record.files || (record.url ? [{kind:'xlsx', name:'图文 Excel', url:record.url}] : []);
    return `<article class="export-row" id="delivery-${esc(record.id)}"><div><div class="tags"><b>${isDelivery ? '已确认款式交付包' : ({approved:'历史已确认款式',eligible:'规则通过过程记录',all:'全部进度记录'}[record.scope] || '历史导出')} · ${record.count} 款${record.delivery_version ? ' · 第 ' + esc(record.delivery_version) + ' 版' : ''}</b>${isDelivery ? `<span class="tag ${record.is_current === false ? 'rejected' : record.is_latest ? 'approved' : ''}">${record.is_current === false ? '历史快照 · 审核已变化' : record.is_latest ? '最新交付版本' : '历史交付记录'}</span>` : ''}<span class="tag ${record.status === 'ready' ? 'approved' : 'pending'}">${{building:'正在生成',ready:'可下载',failed:'生成失败'}[record.status] || esc(record.status)}</span></div><p class="hint">${esc(displayTime(record.created || record.created_at))} · ${esc(record.id)}${record.error ? '<br>' + esc(record.error) : ''}</p>${record.source_status_reason ? `<p class="${record.is_current === false ? 'warning' : 'hint'}">${esc(record.source_status_reason)}</p>` : ''}${record.source_items?.length ? `<details id="delivery-items-${esc(record.id)}"><summary>查看此批 ${record.source_items.length} 款及来源</summary><ul class="delivery-items">${record.source_items.map(item => `<li><b>#${esc(item.product_id)}</b> ${esc(item.name || '')}<small>${esc(item.source_job_name || item.job)}</small></li>`).join('')}</ul></details>` : ''}</div><div class="export-downloads">${record.status === 'ready' ? files.map(file => `<a class="download-link" href="${esc(file.url)}" download>${file.kind === 'zip' ? '下载图片交付包' : '下载图文 Excel'} ↓</a>`).join('') : `<span class="hint">${record.status === 'failed' ? '重新选择款式后可再次打包' : '后台生成中，可继续其他工作'}</span>`}</div></article>`;
  }).join('') || '<div class="empty"><h3>暂无交付记录</h3><p>选择已确认款式，点击“打包并生成图文表格”。</p></div>');

}
async function openDesign(key) {
  captureDesignDraft();
  await closeDetail();
  designSelection = key;
  page = 'designs';
  await refresh();
  const select = $('#design-product');
  if (page !== 'designs' || !select) return;
  if (!approvedItems.some(item => itemKey(item) === key)) { toast('该款当前未通过确认，请重新审核'); return; }
  select.value = key; designPreview();
}
function captureDesignDraft(key) {
  const owner = auth.user?.username || '';
  if (designDraftOwner !== owner) {
    designDraftOwner = owner;
    designDrafts = new Map();
    ruleDrafts = new Map(); reviewDrafts = new Map();
    ruleFormProfile = null; ruleFormBaseline = null;
    designSelection = '';
    return;
  }
  const select = $('#design-product');
  if (!select || !$('#design-name')) return;
  const sourceKey = key || select.value;
  if (!sourceKey) return;
  const previous = designDrafts.get(sourceKey);
  const label = approvedItems.find(item => itemKey(item) === sourceKey)?.name || previous?.label || sourceKey;
  designDrafts.set(sourceKey, {...previous, label, fields:Object.fromEntries(['design-name','design-changes','design-keep'].map(id => [id, $('#' + id).value]))});
}
function restoreDesignDraft(key) {
  let draft = designDrafts.get(key);
  if (!draft) {
    const existing = state.designs.find(item => item.job + ':' + item.product_id === key && item.state === 'draft' && item.source_ready !== false);
    const fields = existing ? {'design-name':existing.name, 'design-changes':existing.changes, 'design-keep':existing.keep || ''} : {'design-name':'改款概念 A', 'design-changes':'', 'design-keep':''};
    draft = {fields, requestId:requestId(), ...(existing ? {id:existing.id, revision:existing.revision, savedFields:{...fields}} : {})};
    designDrafts.set(key, draft);
  }
  restoreFields(draft.fields);
  renderDesignDraftStatus();
}
function renderDesignDraftStatus() {
  const draft = designDrafts.get(designSelection), box = $('#design-draft-status');
  if (!box) return;
  const fields = formSnapshot('.design-editor .detail-form');
  const edited = draft?.savedFields && ['design-name','design-changes','design-keep'].some(id => fields[id] !== draft.savedFields[id]);
  box.textContent = draft?.id ? `正在编辑已保存草稿 · ${draft.id}${edited ? ' · 有未保存改动' : ''}` : draft?.parentId ? '正在填写新修订：原方案、原图和审核意见会保留；保存不会触发生图。' : '新方案尚未保存。可用中文填写具体改动；保存后再单独确认生成。';
  if ($('#save-design')) $('#save-design').textContent = draft?.id ? '保存草稿修改' : draft?.parentId ? '保存新修订方案' : '保存改款方案';
}
function designs() {
  // Keep an unavailable source selected: old edits must never move to a different garment.
  const previous = designSelection || (approvedItems[0] ? itemKey(approvedItems[0]) : '');
  designSelection = previous;
  const available = approvedItems.some(item => itemKey(item) === previous);
  const unavailable = previous && !available;
  const sourceOptions = (unavailable ? `<option value="${esc(previous)}" selected>原款已不可用 · ${esc(designDrafts.get(previous)?.label || previous)}</option>` : '') + approvedItems.map(item => `<option value="${esc(itemKey(item))}" ${previous === itemKey(item) ? 'selected' : ''}>#${esc(item.id)} · ${esc(item.name.slice(0,65))} · ${esc(item.source_job_name || item.job)}</option>`).join('');
  $('#content').innerHTML = `<div class="next-step"><b>本环节：写改动 → 保存草稿 → 核对并生成 1 张 → 看图确认</b><span>保存只记录方案，不会计费。成图不符合要求时填写修改意见，再建立新修订；每次生成都需要独立确认。</span></div><div class="design-layout"><section class="panel design-editor"><span class="section-kicker">STEP 04 · 改款方案</span><h2>从已确认款式开始设计</h2>${previous || approvedItems.length ? `<label>已确认原款 · 跨任务调取<select id="design-product">${sourceOptions}</select></label>` : '<select id="design-product" hidden disabled></select><div class="empty"><span class="empty-symbol">✧</span><h3>先确认一款，再开始改款</h3><p>审核确认后自动出现在这里，保留原图、规则与审核信息。</p><button data-go-page="collection">去审核款式 →</button></div>'}<div id="design-preview"></div><div class="detail-form ${previous ? '' : 'muted-form'}"><p id="design-draft-status" class="draft-status" role="status"></p><label>方案名称<input id="design-name" value="改款概念 A" ${previous ? '' : 'disabled'}></label><label>改动内容（可直接用中文）<textarea id="design-changes" rows="4" placeholder="例如：只将面料花纹改为酒红纯色；领口保持平整，不增加褶皱。写清部位、改成什么、禁止新增什么。" ${previous ? '' : 'disabled'}></textarea></label><label>必须保留的内容<textarea id="design-keep" rows="3" placeholder="例如：保留原挂脖领型、修身廓形、裙长和背部交叉系带；其余部位不改。" ${previous ? '' : 'disabled'}></textarea></label><div class="form-footer"><button id="save-design" class="primary" ${available && auth.canEdit ? '' : 'disabled'}>保存改款方案</button><button id="new-design" ${available && auth.canEdit ? '' : 'disabled'}>另建一个方案</button></div></div><p class="hint">表单会在本会话中保留，刷新进度不会打断输入。关闭本窗口前请保存。AI 图仅为概念参考，不代表原商品实拍或上架成图。</p></section><section class="panel design-history"><div class="topline"><div><span class="section-kicker">设计记录</span><h2>生成与设计师确认</h2></div><button id="check-generation">检查生图连接</button></div><label class="history-scope">查看方案<select id="design-history-scope"><option value="current" ${designHistoryScope === 'current' ? 'selected' : ''}>当前原款的方案</option><option value="all" ${designHistoryScope === 'all' ? 'selected' : ''}>全部原款的方案</option></select></label><div id="generation-status" class="hint"></div><div id="design-list"></div></section></div>`;
  $('#design-product').onchange = () => {
    captureDesignDraft(designSelection);
    designSelection = $('#design-product').value;
    restoreDesignDraft(designSelection); designPreview(); renderDesignHistory();
  };
  restoreDesignDraft(previous); designPreview();
  for (const id of ['design-name','design-changes','design-keep']) $('#' + id).oninput = () => { captureDesignDraft(); renderDesignDraftStatus(); };
  $('#design-history-scope').onchange = event => { designHistoryScope = event.target.value; renderDesignHistory(); };
  $('#new-design').onclick = () => {
    captureDesignDraft();
    const draft = designDrafts.get(designSelection);
    const dirty = draft?.fields?.['design-changes'] && (!draft.savedFields || JSON.stringify(draft.fields) !== JSON.stringify(draft.savedFields));
    if (dirty && !window.confirm('当前有未保存的改动。放弃这些输入并另建方案？')) return;
    designDrafts.set(designSelection, {fields:{'design-name':'改款概念 ' + (state.designs.filter(item => item.job + ':' + item.product_id === designSelection).length + 1), 'design-changes':'', 'design-keep':''}, requestId:requestId()});
    restoreDesignDraft(designSelection); $('#design-changes').focus();
  };
  $('#save-design').onclick = () => act(async () => {
    const source = approvedItems.find(item => itemKey(item) === $('#design-product').value);
    if (!source) throw Error('请先选择已审核确认的原款');
    captureDesignDraft();
    const key = itemKey(source), draft = designDrafts.get(key), fields = {...draft.fields};
    if (!fields['design-name'].trim() || !fields['design-changes'].trim()) throw Error('请填写方案名称和具体改动');
    const button = $('#save-design'); button.disabled = true;
    const payload = {job:source.job, product_id:source.id, expected_revision:source.revision, name:fields['design-name'], changes:fields['design-changes'], keep:fields['design-keep'], request_id:draft.requestId || (draft.requestId = requestId()), ...(draft.id ? {id:draft.id, expected_design_revision:draft.revision} : {}), ...(draft.parentId ? {parent_design_id:draft.parentId, expected_parent_revision:draft.parentRevision} : {})};
    try {
      const saved = await (draft.id ? withLease('design:' + draft.id, () => api('/api/design', payload)) : api('/api/design', payload));
      designDrafts.set(key, {...draft, id:saved.id, revision:saved.revision, savedFields:fields});
      await refresh(); toast(saved.existing ? '相同草稿已存在，已定位原方案，不重复创建' : '方案已保存。请在右侧核对后再生成参考图');
      document.getElementById('design-record-' + saved.id)?.scrollIntoView({block:'nearest'});
    } finally { if (button.isConnected) button.disabled = !auth.canEdit; }
  });
  $('#check-generation').onclick = () => act(async () => {
    const result = await api('/api/generation');
    $('#generation-status').textContent = result.connected ? 'ComfyUI 已连接。保存方案后可核对并确认单张生成。' : result.error;
  });
  renderDesignHistory();
}
function designPreview() {
  const source = approvedItems.find(item => itemKey(item) === $('#design-product')?.value);
  if (!$('#design-preview')) return;
  patchRegion('#design-preview', source ? `<div class="design-source" data-render-key="${esc(itemKey(source))}">${productPictures(source)}<div class="tags"><span class="tag approved">审核已确认</span><span class="tag sales">${esc(source.sales)}</span><span class="tag">${esc(source.review?.primary_style || '风格待核')}</span></div><p class="hint">来源：${esc(source.source_job_name || source.job)}<br>${esc(source.evaluation.age_status)} · ${esc(source.review?.elements || '尚未记录元素')}</p></div>` : (designSelection ? '<div class="warning">原款已不在已确认款式库中，可能已被撤销确认或证据发生变化。此款的未提交改动已保留，保存暂不可用。请先复核原款；选择其他款式会打开该款独立的草稿。</div>' : ''));
  $('#save-design').disabled = !source || !auth.canEdit;
}
async function editDesign(id, revise = false) {
  const design = state.designs.find(item => item.id === id);
  if (!design) throw Error('方案已更新，请刷新后重试');
  const key = design.job + ':' + design.product_id;
  if (!approvedItems.some(item => itemKey(item) === key)) throw Error('原款当前未确认，请先重新审核原款');
  captureDesignDraft();
  const previous = designDrafts.get(key);
  if (previous?.fields?.['design-changes'] && (!previous.savedFields || JSON.stringify(previous.fields) !== JSON.stringify(previous.savedFields)) && !window.confirm('此原款有未保存的输入。放弃这些输入并打开所选方案？')) return;
  const fields = {'design-name':design.name + (revise ? ' · 修订' : ''), 'design-changes':design.changes + (revise && design.review?.note ? '\n本次修改意见：' + design.review.note : ''), 'design-keep':design.keep || ''};
  designDrafts.set(key, {label:design.source_name, fields, requestId:requestId(), ...(revise ? {parentId:design.id, parentRevision:design.revision} : {id:design.id, revision:design.revision, savedFields:{...fields}})});
  designSelection = key; page = 'designs';
  // The previous DOM must not overwrite the explicitly selected revision.
  $('#content').innerHTML = ''; render();
  $('#design-changes').focus();
  toast(revise ? '修改意见已带入新修订，请完善后保存。尚未提交生图' : '已打开原草稿，保存将更新此方案');
}
function renderDesignHistory() {
  if (!$('#design-list')) return;
  const records = state.designs.filter(design => designHistoryScope === 'all' || design.job + ':' + design.product_id === designSelection);
  patchRegion('#design-list', records.map(design => {
    const ready = design.source_ready !== false && approvedItems.some(item => item.job === design.job && item.id === design.product_id);
    const working = ['uploading','submitting','running','uncertain'].includes(design.state);
    const reviewable = design.images?.length && ['generated_pending_review','approved','rejected'].includes(design.state);
    const next = ({draft:'下一步：核对改动和原图，再确认生成 1 张。',uploading:'正在上传参考图，请等待，不要重复提交。',submitting:'正在提交本次请求，请等待。',running:'正在生成；进度自动更新，可继续其他工作。',uncertain:'提交状态待核。请先“核对生成状态”，不会自动重复调用。',generated_pending_review:'下一步：对照原款与改动要求检查图片，选择确认或需修改。',approved:'设计师已确认。此图仍为 AI 概念参考，后续打样和商品图需单独验收。',rejected:'下一步：按修改意见建立修订，保留这次结果和审核记录。',failed:'查看失败原因后，可建立新修订；新生成仍需独立确认。'})[design.state] || '';
    return `<article class="design-card" id="design-record-${esc(design.id)}"><h3>${esc(design.name)} · #${esc(design.product_id)}</h3><div class="tags"><span class="tag ${design.state === 'approved' ? 'approved' : design.state === 'rejected' ? 'rejected' : ''}">${esc(({draft:'方案草稿',uploading:'上传参考图',submitting:'正在提交',running:'生成中',generated_pending_review:'待设计师确认',approved:'设计师已确认',rejected:'需修改',failed:'生成失败',uncertain:'提交状态待核'})[design.state] || design.state)}</span><span class="tag">方案 v${esc(design.revision_number || 1)}</span></div><p class="source-task">${esc(design.source_job_name || design.job)} · ${esc(design.id)}<br>保存于 ${esc(displayTime(design.updated || design.created))}${design.parent_design_id ? ' · 修订自 ' + esc(design.parent_design_id) : ''}</p><p><b>本次改动：</b>${esc(design.changes)}</p><p class="hint"><b>必须保留：</b>${esc(design.keep || '未明确改动的原款特征')}</p><details id="design-prompt-${esc(design.id)}"><summary>查看技术提示词与生成参数</summary><pre>${esc(design.prompt)}</pre><p class="hint">${esc(design.model)} · ${esc(design.size)} · 1 张${design.prompt_id ? ' · ' + esc(design.prompt_id) : ''}</p></details>${(design.images || []).map(image => `<a class="design-image-link" href="${esc(image.url)}" target="_blank" rel="noopener"><img src="${esc(image.url)}" alt="AI 改款概念参考，点击查看原图"></a>`).join('')}${design.system_qa ? `<details id="design-qa-${esc(design.id)}"><summary>图像检查记录（仍需设计师终审）</summary>${design.system_qa.checks.map(check => `<p class="hint">${esc(check.item)}：${esc(check.result)}。${esc(check.evidence)}</p>`).join('')}</details>` : ''}${design.review?.note ? `<div class="review-note"><b>审核意见</b><p>${esc(design.review.note)}</p></div>` : ''}${design.error || design.poll_error ? `<div class="warning">${esc(design.error || design.poll_error)}</div>` : ''}${!ready ? `<div class="warning">原款${design.source_approved ? '证据已更新' : '当前未确认'}，不能直接生成。先复核原款，再建立新修订。</div>` : ''}<p class="next-action">${esc(next)}</p><div class="form-footer">${design.state === 'draft' ? `<button data-edit-design="${esc(design.id)}" ${!ready ? 'disabled' : ''}>继续编辑草稿</button><button class="primary" data-preflight="${esc(design.id)}" ${!ready ? 'disabled' : ''}>核对并生成 1 张</button>` : ''}${working ? `<button data-check-design="${esc(design.id)}">核对生成状态（不重试）</button>` : ''}${reviewable ? `<button data-design-review="${esc(design.id)}" data-decision="approved">设计师确认</button><button data-design-review="${esc(design.id)}" data-decision="rejected">填写修改意见</button>` : ''}${['rejected','failed','approved','generated_pending_review'].includes(design.state) || (design.state === 'draft' && !ready) ? `<button data-revise-design="${esc(design.id)}" ${!approvedItems.some(item => item.job === design.job && item.id === design.product_id) ? 'disabled' : ''}>${design.state === 'rejected' ? '按意见建立修订' : '建立新修订'}</button>` : ''}</div>${design.last_checked_at ? `<p class="hint">最近核对：${esc(displayTime(design.last_checked_at))}</p>` : ''}</article>`;
  }).join('') || `<div class="empty"><h3>${state.designs.length ? '当前原款还没有方案' : '还没有改款方案'}</h3><p>在左侧填写改动并保存，方案会显示在这里。</p></div>`);
  document.querySelectorAll('[data-preflight]').forEach(button => button.onclick = () => act(() => generationPreflight(button.dataset.preflight)));
  document.querySelectorAll('[data-edit-design]').forEach(button => button.onclick = () => act(() => editDesign(button.dataset.editDesign)));
  document.querySelectorAll('[data-revise-design]').forEach(button => button.onclick = () => act(() => editDesign(button.dataset.reviseDesign, true)));
  document.querySelectorAll('[data-check-design]').forEach(button => button.onclick = () => act(async () => {
    const design = state.designs.find(item => item.id === button.dataset.checkDesign); button.disabled = true;
    try { const result = await withLease('design:' + design.id, () => api('/api/design/check', {id:design.id, expected_revision:design.revision})); await refresh({background:true}); toast(result.message || '已核对生成状态；未提交新的生图请求'); }
    finally { if (button.isConnected) button.disabled = !auth.canEdit; }
  }));
  document.querySelectorAll('[data-design-review]').forEach(button => button.onclick = () => act(() => reviewDesign(button.dataset.designReview, button.dataset.decision)));
  applyReadOnly();
}
async function reviewDesign(id, decision) {
  const design = state.designs.find(item => item.id === id); await closeDetail();
  const reject = decision === 'rejected';
  $('#detail-body').innerHTML = `<div class="dialog-head"><h2>${reject ? '记录需修改的地方' : '设计师确认概念图'}</h2><button id="close-design-review">取消</button></div><div class="confirm-body"><p>${esc(design.name)} · #${esc(design.product_id)}</p><p class="hint">对照原款、允许改动和必须保留的内容检查。${reject ? '写清哪里不符合，意见会带入新修订。' : '确认只代表当前概念方案通过设计审核，不代表原 SKU 实拍或上架验收。'}</p><label>${reject ? '修改意见（必填）' : '审核备注（可选）'}<textarea id="design-review-note" rows="4" placeholder="例如：领口不应出现褶皱，请恢复原图的平整挂脖结构。">${esc(design.review?.note || '')}</textarea></label><button id="save-design-review" class="primary">${reject ? '保存意见，标记需修改' : '确认此概念方案'}</button></div>`;
  $('#detail').showModal(); $('#close-design-review').onclick = () => $('#detail').close();
  $('#save-design-review').onclick = () => act(async () => {
    const note = $('#design-review-note').value.trim(); if (reject && !note) throw Error('请填写具体修改意见');
    const button = $('#save-design-review'); button.disabled = true;
    try { await withLease('design:' + design.id, () => api('/api/design/review', {id:design.id, decision, note, reviewer:reviewer(), expected_revision:design.revision})); $('#detail').close(); await refresh(); toast(reject ? '已记录修改意见。可点击“按意见建立修订”继续' : '设计师确认已保存'); }
    finally { if (button.isConnected) button.disabled = !auth.canEdit; }
  });
}
async function generationPreflight(id) {
  const design = state.designs.find(item => item.id === id);
  const preflight = await api('/api/design/preflight', {id});
  await closeDetail();
  $('#detail-body').innerHTML = `<div class="dialog-head"><h2>本次参考图生成确认</h2><button id="close-preflight">关闭</button></div><div class="confirm-body"><p>模型 ${esc(preflight.model)} · ${esc(preflight.size)} · ${esc(preflight.quality)} · <b>1 张概念参考图</b></p><p><b>原款：</b>#${esc(design.product_id)} · ${esc(design.name)}</p><p><b>本次只改：</b>${esc(design.changes)}</p><p><b>必须保留：</b>${esc(design.keep || '未明确改动的原款特征')}</p><p>参考图职责：${preflight.reference_roles.map(role => ({product_front:'原款正面：结构、轮廓、细节',product_back:'原款背面：背部结构参考'}[role] || esc(role))).join('；')}。原图中的人物身份不是生成目标。</p><details><summary>查看完整技术提示词</summary><pre>${esc(preflight.prompt)}</pre></details><label class="check"><input type="checkbox" id="authorize-image">确认本次方案及 1 张生图服务调用（可能产生服务费用）</label><div class="form-footer"><button id="submit-generation" class="primary" disabled>提交 1 张生成</button></div><p class="hint">结果需设计师确认；提交状态不明确时不自动重复调用。</p></div>`;
  $('#detail').showModal();
  $('#close-preflight').onclick = () => $('#detail').close();
  $('#authorize-image').onchange = event => $('#submit-generation').disabled = !event.target.checked || !auth.canEdit;
  $('#submit-generation').onclick = () => act(async () => {
    if (!$('#authorize-image').checked) throw Error('请先确认本次单张生成');
    const button = $('#submit-generation'); button.disabled = true;
    try {
      await withLease('design:' + design.id, () => api('/api/design/generate', {id, authorize_one_image:true, expected_revision:design.revision}));
      $('#detail').close(); const draft = designDrafts.get(design.job + ':' + design.product_id); if (draft?.id === design.id) designDrafts.delete(design.job + ':' + design.product_id);
      $('#content').innerHTML = '';
      await refresh(); toast('已提交单张生成，状态已保存');
    } finally { if (button.isConnected) button.disabled = !auth.canEdit || !$('#authorize-image').checked; }
  });
}
// A single delegated navigation handler survives each page render.
document.addEventListener('click', event => {
  const navigation = event.target.closest('[data-go-page],#nav [data-page]');
  if (navigation && !navigation.disabled) {
    act(async () => { await closeDetail(); page = navigation.dataset.goPage || navigation.dataset.page; render(); });
    return;
  }
  const designButton = event.target.closest('[data-open-design]');
  if (designButton && !designButton.disabled) { act(() => openDesign(designButton.dataset.openDesign)); return; }
  const gallery = event.target.closest('[data-gallery]');
  if (gallery && !gallery.disabled) act(async () => {
    const id = currentItem.id;
    await api('/api/image/assign', {job:jobId, product_id:id, index:Number(gallery.dataset.gallery), role:gallery.dataset.role, expected_revision:currentItem.revision});
    await closeDetail(); await refresh(); await detail(id);
  });
});
window.selectionCaptureUpdated = () => { if (auth.ready && page !== 'rules') act(() => refresh({background:true})); };
setInterval(() => {
  if (!auth.ready || $('#detail').open || document.hidden) return;
  if (page === 'approved' && state?.exports.some(record => record.status === 'building')) act(() => refresh({background:true}));
}, 3000);
setInterval(() => {
  if (auth.ready && ['collection','approved','designs'].includes(page) && !$('#detail').open && !document.hidden) act(() => refresh({background:true}));
}, 15000);

async function helperSetup(){const p=await api('/api/pairing',{job:jobId});$('#detail-body').innerHTML=`<div class="dialog-head"><h2>Chrome采集助手</h2><button id="close-helper">关闭</button></div><div style="padding:25px"><p>使用Chrome已登录的SHEIN美国站会话继续采集。内嵌浏览器仍可用于手动查看，但登录状态各自独立。</p><label>后台连接地址<input id="helper-base" readonly value="${esc(p.base_url||location.origin)}"></label><label>当前任务ID<input id="helper-job" readonly value="${esc(jobId)}"></label><label>本次任务配对码（仅粘贴到采集助手）<input id="helper-key" type="password" readonly value="${esc(p.key)}"></label><div class="form-footer"><button id="copy-base">复制后台地址</button><button id="copy-key">复制配对码</button><button id="copy-job">复制任务ID</button><button id="helper-folder">打开助手文件夹</button><button id="open-chrome" class="primary">打开Chrome</button></div><p class="hint">配对码绑定当前成员、任务和登录会话；退出或会话被替换后失效。同一时间只允许一个采集助手运行。首次安装：Chrome扩展程序 → 开发者模式 → 加载已解压的扩展程序 → 选择助手文件夹。选中美国站标签页，在助手中填入配对码和任务ID，再点击继续采集。权限仅SHEIN美国站、商品图片CDN和本机工作台；不读取密码或Cookie。</p></div>`;$('#detail').showModal();$('#close-helper').onclick=()=>$('#detail').close();$('#copy-base').onclick=()=>act(async()=>{await copyText(p.base_url||location.origin);toast('后台地址已复制');});$('#copy-key').onclick=()=>act(async()=>{await copyText(p.key);toast('配对码已复制');});$('#copy-job').onclick=()=>act(async()=>{await copyText(jobId);toast('任务ID已复制');});$('#helper-folder').onclick=()=>native({action:'helperFolder'});$('#open-chrome').onclick=()=>native({action:'chrome'});}


// Leases stop two members editing the same record; revisions also reject stale saves.
let itemLease=null, itemLeaseTimer=null, suspendedDraft=null;
async function copyText(value){
  if(navigator.clipboard?.writeText){try{await navigator.clipboard.writeText(value);return;}catch{}}
  const input=document.createElement('textarea');input.value=value;input.style.position='fixed';input.style.opacity='0';document.body.append(input);input.focus();input.select();
  const copied=document.execCommand('copy');input.remove();if(!copied)throw Error('自动复制不可用，请选中内容手动复制');
}
async function withLease(resource, fn){
  await api('/api/lock',{action:'acquire',resource,ttl:180,client_id:auth.clientId});
  try{return await fn();}finally{try{await api('/api/lock',{action:'release',resource,client_id:auth.clientId});}catch{}}
}
async function acquireItemLock(id){
  if(!auth.canEdit)return {acquired:false,message:'只读查看 · 当前账号没有编辑权限'};
  const resource='item:'+jobId+':'+id;
  try{
    await api('/api/lock',{action:'acquire',resource,ttl:180,client_id:auth.clientId});
    itemLease={resource};
    itemLeaseTimer=setInterval(async()=>{
      const lease=itemLease;if(!lease||!auth.ready)return;
      try{await api('/api/lock',{action:'renew',resource:lease.resource,ttl:180,client_id:auth.clientId});}
      catch(e){if(itemLease===lease){clearInterval(itemLeaseTimer);itemLeaseTimer=null;itemLease=null;setItemWritable(false);const label=$('#item-lock-status');if(label)label.textContent='编辑权限已释放，请关闭后重新打开此记录。'+e.message;}}
    },60000);
    return {acquired:true,message:'你正在编辑此款 · 其他成员可查看，暂不能修改'};
  }catch(e){
    if(e.status===409||e.status===403)return {acquired:false,message:'只读查看 · '+e.message};
    throw e;
  }
}
function setItemWritable(allowed){
  const writable=allowed&&auth.canEdit;
  document.querySelectorAll('#detail .detail-form input,#detail .detail-form select,#detail .detail-form textarea,#save-pending,#approve,#reject,[data-gallery],[data-upload]').forEach(el=>el.disabled=!writable);
  if($('#to-design'))$('#to-design').disabled=!auth.canEdit||!currentItem?.evaluation?.approved;
}
async function releaseItemLock(){
  const lease=itemLease;itemLease=null;clearInterval(itemLeaseTimer);itemLeaseTimer=null;
  if(lease&&auth.user){try{await api('/api/lock',{action:'release',resource:lease.resource,client_id:auth.clientId});}catch{}}
}
async function closeDetail(){
  captureReviewDraft();
  const releasing=releaseItemLock();
  if($('#detail').open)$('#detail').close();
  await releasing;
}
$('#detail').addEventListener('cancel',()=>captureReviewDraft());
$('#detail').addEventListener('close',()=>{if(!$('#detail').open)void releaseItemLock();});
window.addEventListener('pagehide',()=>{
  if(itemLease&&auth.user)fetch('/api/lock',{method:'POST',credentials:'same-origin',keepalive:true,headers:{'Content-Type':'application/json','X-CSRF-Token':auth.user.csrf_token,'X-Selection-Client':auth.clientId},body:JSON.stringify({action:'release',resource:itemLease.resource,client_id:auth.clientId})}).catch(()=>{});
});
function applyReadOnly(){
  if(auth.canEdit)return;
  document.querySelectorAll('#resume,#pair-helper,#pause,#terminate,#save-rule,#copy-rule,#new-job,#refilter-job,#import-json,#import-dates,#save-design,#new-design,[data-edit-design],[data-revise-design],[data-check-design],[data-export],[data-preflight],[data-design-review],#rule-form input,#rule-form textarea,#rule-form select,#design-name,#design-changes,#design-keep,#create-delivery,[data-open-design],[data-select-approved],#select-visible').forEach(el=>{el.disabled=true;el.title='当前账号为只读成员';});
}
function formSnapshot(root){return Object.fromEntries([...document.querySelectorAll(root+' input[id],'+root+' select[id],'+root+' textarea[id]')].filter(el=>el.type!=='password'&&el.type!=='file').map(el=>[el.id,el.type==='checkbox'?el.checked:el.value]));}
function restoreFields(fields){for(const [id,value] of Object.entries(fields)){const el=document.getElementById(id);if(el){if(el.type==='checkbox')el.checked=value;else el.value=value;}}}
window.addEventListener('selection-auth-lost',()=>{
  if(auth.user)suspendedDraft={username:auth.user.username,page,job:jobId,itemId:$('#detail').open&&$('#item-lock-status')?currentItem?.id:null,itemFields:formSnapshot('#detail-body'),pageFields:formSnapshot('#content')};
  clearInterval(itemLeaseTimer);itemLeaseTimer=null;itemLease=null;
});
const baseRefresh=refresh;
refresh=async function(options = {}){
  await baseRefresh(options);
  if(suspendedDraft&&auth.ready){
    const draft=suspendedDraft;suspendedDraft=null;
    if(draft.username===auth.user.username){
      jobId=state.jobs.some(j=>j.id===draft.job)?draft.job:jobId;page=draft.page;
      if (page === 'designs') {
        designSelection = draft.pageFields['design-product'] || designSelection;
        if (designSelection) designDrafts.set(designSelection, {...designDrafts.get(designSelection), label:designDrafts.get(designSelection)?.label || designSelection, fields:Object.fromEntries(['design-name','design-changes','design-keep'].map(id => [id,draft.pageFields[id] || '']))});
      }
      await baseRefresh();
      if (page !== 'designs') restoreFields(draft.pageFields);
      if(draft.itemId){await detail(draft.itemId);restoreFields(draft.itemFields);}
      toast('已恢复未提交的表单，请核对后保存');
    }
  }
};


async function adminPage(){
  if(auth.user?.role!=='admin')return;
  $('#content').innerHTML='<section class="panel"><h2>成员与后台</h2><p class="hint">正在读取成员、内网地址和数据库状态…</p></section>';
  try{
    const [accounts,info]=await Promise.all([api('/api/admin/users'),api('/api/admin/status')]);
    if(page!=='admin'||!auth.ready)return;
    const service=info.service||{},db=info.database||{},members=accounts.users||[];
    $('#content').innerHTML=`<section class="panel"><div class="topline"><div><h2>内网访问与后台</h2><p class="hint">同一网络中的员工使用以下地址登录。每人独立账号，审核记录自动署名。</p></div><button id="refresh-admin">刷新状态</button></div><div class="service-addresses">${(service.lan_urls||[]).map(url=>`<div><a class="link" href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a><button data-copy-address="${esc(url)}">复制地址</button></div>`).join('')||'<p class="warning">暂未检测到内网地址，请确认 Mac 已连接局域网。</p>'}</div><div class="service-summary"><div><span>后台服务</span><b>${service.health===false?'待检查':esc(typeof service.health==='string'?service.health:'运行中')}</b></div><div><span>登录后自启动</span><b>${typeof service.autostart==='object'?esc(service.autostart.status||(service.autostart.enabled?'已启用':'待检查')):esc(service.autostart===true?'已启用':service.autostart===false?'未启用':service.autostart||'待核验')}</b></div><div><span>数据库校验</span><b>${esc(db.integrity||'待核验')}</b></div><div><span>任务 / 商品记录</span><b>${esc(db.jobs??'—')} / ${esc(db.items??'—')}</b></div></div><p class="hint">后台所在的 Mac 需保持开机、联网。休眠期间员工访问会中断。</p></section><section class="panel"><div class="topline"><h2>员工账号</h2><span class="hint">同一账号只允许一个登录会话</span></div><form id="add-user" class="member-form"><label>账号<input id="new-username" required maxlength="48" placeholder="例如 designer01" autocomplete="off" spellcheck="false"></label><label>姓名<input id="new-display-name" required maxlength="80" placeholder="审核记录中的姓名" autocomplete="off"></label><label>权限<select id="new-role"><option value="editor">编辑成员</option><option value="viewer">只读成员</option><option value="admin">管理员</option></select></label><button class="primary" type="submit">创建成员</button></form><p class="hint">编辑成员可以审核、采集、改款和导出；只读成员只能查看与下载；管理员可管理成员、数据库备份和后台。</p><div id="temporary-credential" hidden></div><div class="table-scroll"><table class="member-table"><thead><tr><th>账号</th><th>姓名</th><th>权限</th><th>状态</th><th>操作</th></tr></thead><tbody>${members.map(m=>`<tr data-member="${esc(m.id)}"><td>${esc(m.username)}${m.id===auth.user.id?'<span class="tag">当前账号</span>':''}</td><td><input data-member-name value="${esc(m.display_name)}" maxlength="80" aria-label="${esc(m.username)}姓名"></td><td><select data-member-role aria-label="${esc(m.username)}权限">${Object.entries(auth.roles).map(([v,t])=>`<option value="${v}" ${m.role===v?'selected':''}>${t}</option>`).join('')}</select></td><td><span class="tag ${m.enabled?'eligible':'excluded'}">${m.enabled?'正常':'已停用'}</span>${m.must_change_password?'<p class="hint">待修改临时密码</p>':''}</td><td><div class="member-actions"><button data-save-member="${esc(m.id)}">保存</button><button data-reset-member="${esc(m.id)}" ${m.id===auth.user.id?'disabled title="请使用页头的修改密码"':''}>重置密码</button><button data-toggle-member="${esc(m.id)}" ${m.id===auth.user.id?'disabled':''}>${m.enabled?'停用':'启用'}</button></div></td></tr>`).join('')}</tbody></table></div></section><section class="panel"><div class="topline"><div><h2>数据库备份</h2><p class="hint">备份保留当前数据库状态，商品图片继续由同一后台保存。恢复操作请在停止后台后由管理员执行。</p></div><button id="create-backup">立即备份数据库</button></div><div class="table-scroll"><table><thead><tr><th>备份文件</th><th>创建时间</th><th>大小</th></tr></thead><tbody>${(info.backups||[]).slice(0,10).map(b=>`<tr><td>${esc(b.name)}</td><td>${esc(displayTime(b.modified||b.created_at))}</td><td>${b.size?esc((b.size/1024/1024).toFixed(2))+' MB':'—'}</td></tr>`).join('')||'<tr><td colspan="3">暂无备份</td></tr>'}</tbody></table></div></section><section class="panel"><h2>当前占用</h2><p class="hint">同一商品或规则由一人修改；关闭页面或超时后自动释放。保存时还会核对版本，避免覆盖他人更新。</p><div class="table-scroll"><table><thead><tr><th>工作内容</th><th>正在使用</th><th>释放时间</th></tr></thead><tbody>${(info.leases||[]).map(l=>`<tr><td>${esc(leaseLabel(l.resource))}</td><td>${esc(l.display_name||l.owner_name||l.username||l.user_id||'已登录成员')}</td><td>${esc(displayTime(l.expires_at||l.expires))}</td></tr>`).join('')||'<tr><td colspan="3">当前没有占用中的编辑记录</td></tr>'}</tbody></table></div></section>`;
    $('#refresh-admin').onclick=()=>act(adminPage);
    document.querySelectorAll('[data-copy-address]').forEach(b=>b.onclick=()=>act(async()=>{await copyText(b.dataset.copyAddress);toast('内网地址已复制');}));
    $('#add-user').onsubmit=e=>{e.preventDefault();act(async()=>{const username=$('#new-username').value.trim();const result=await api('/api/admin/users',{username,display_name:$('#new-display-name').value.trim(),role:$('#new-role').value});await adminPage();showTemporaryCredential(result,username);});};
    document.querySelectorAll('[data-save-member]').forEach(b=>b.onclick=()=>act(async()=>{const row=b.closest('tr'),m=members.find(x=>String(x.id)===b.dataset.saveMember);await api('/api/admin/user',{id:m.id,display_name:row.querySelector('[data-member-name]').value.trim(),role:row.querySelector('[data-member-role]').value,enabled:!!m.enabled});await auth.bootstrap(refresh);toast('成员设置已保存');}));
    document.querySelectorAll('[data-toggle-member]').forEach(b=>b.onclick=()=>act(async()=>{const m=members.find(x=>String(x.id)===b.dataset.toggleMember);if(m.enabled&&!window.confirm(`停用 ${m.display_name}（${m.username}）后，该成员会立即退出登录。确认停用？`))return;await api('/api/admin/user',{id:m.id,enabled:!m.enabled,role:m.role,display_name:m.display_name});await adminPage();toast(m.enabled?'成员已停用':'成员已启用');}));
    document.querySelectorAll('[data-reset-member]').forEach(b=>b.onclick=()=>act(async()=>{const m=members.find(x=>String(x.id)===b.dataset.resetMember);if(!window.confirm(`重置 ${m.display_name}（${m.username}）的密码？当前登录会话会失效。`))return;const result=await api('/api/admin/reset-password',{id:m.id});await adminPage();showTemporaryCredential(result,m.username);}));
    $('#create-backup').onclick=()=>act(async()=>{$('#create-backup').disabled=true;await api('/api/admin/backup',{});await adminPage();toast('数据库备份已保存');});
  }catch(e){if(page==='admin'&&auth.ready)$('#content').innerHTML=`<section class="panel"><h2>后台状态暂不可用</h2><p class="warning">${esc(e.message)}</p><button id="retry-admin">重新读取</button></section>`;if($('#retry-admin'))$('#retry-admin').onclick=()=>act(adminPage);}
}
function showTemporaryCredential(result,username){
  const box=$('#temporary-credential');if(!box)return;
  const password=result.temporary_password||result.password;if(!password)throw Error('成员已更新，但未返回临时密码，请检查后台回执');
  box.hidden=false;box.className='credential-card';box.innerHTML=`<h3>临时密码仅显示这一次</h3><p>账号：<b>${esc(username)}</b></p><label>临时密码<input id="temporary-password" readonly value="${esc(password)}"></label><p class="hint">请通过公司认可的方式告知本人；首次登录必须改密。关闭此提示后将不再显示。</p><div class="form-footer"><button id="copy-temporary-password">复制临时密码</button><button id="dismiss-temporary-password">我已记录，关闭</button></div>`;
  $('#copy-temporary-password').onclick=()=>act(async()=>{await copyText(password);toast('临时密码已复制');});$('#dismiss-temporary-password').onclick=()=>{box.innerHTML='';box.hidden=true;};box.scrollIntoView({block:'center',behavior:'smooth'});
}
function leaseLabel(resource){const parts=String(resource||'').split(':');return parts[0]==='item'?'商品 #'+parts.at(-1):parts[0]==='profile'?'规则模板 '+parts.slice(1).join(':'):parts[0]==='design'?'改款方案 '+parts.slice(1).join(':'):parts[0]==='collector'?'采集助手':resource;}
function displayTime(value){if(!value)return '—';const d=new Date(typeof value==='number'?(value<1e12?value*1000:value):value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString('zh-CN');}

$('#open-guide').onclick = () => act(async () => {
  await closeDetail();
  $('#detail-body').innerHTML = `<div class="dialog-head"><h2>员工操作指南</h2><button id="close-guide">关闭</button></div><div class="confirm-body guide-content"><ol class="operation-steps"><li><b>渠道与规则</b><p>输入渠道链接，设置销量、款龄、品类和排除项，保存并创建新任务。改变模板不会修改旧任务；重筛仅使用已经保存的资料。</p></li><li><b>查看与审核</b><p>先连接采集助手，再开始采集。候选不等于合格款：待补证据 → 待审核 → 确认选用。核对正背面、复色、是否基础款，记录风格与元素。</p></li><li><b>已确认与交付</b><p>勾选款式，打包成图文 Excel 和原图 ZIP；在交付记录下载。审核变化后旧包会标为历史快照，应按当前已确认款重新打包。</p></li><li><b>改款与生图</b><p>调入原款，用中文写“改什么、保留什么”，保存草稿后核对。每次只生成 1 张，单独确认服务调用；保存草稿不会生图。看图后确认，或填写修改意见并建立新修订。</p></li></ol><div class="guide-notes"><p><b>数据口径：</b>SKU 月份只是估计款龄，不是真实上架日期；销量保留平台原标签，未公开周期不能写成月销量。</p><p><b>访问受限：</b>验证或 403 时保留断点，先通过来源平台正常入口恢复访问，再确认继续；不要反复翻页。</p><p><b>生成状态不明：</b>使用“核对生成状态”，不会重新提交。没有确认结果前不自动重试，修改意见也不会自动触发新费用。</p><p><b>保存与协作：</b>未提交输入仅在本窗口会话中保留；关闭窗口前保存。其他成员正在编辑时可查看，保存会校验版本。</p></div></div>`;
  $('#detail').showModal(); $('#close-guide').onclick = () => $('#detail').close();
});

// Start only after lock and draft handlers are registered.
auth.bootstrap(refresh);
