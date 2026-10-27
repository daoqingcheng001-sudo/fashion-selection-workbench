let busy = false;

function allowedBase(value) {
  const u = new URL(value || 'http://127.0.0.1:8770');
  const h = u.hostname.toLowerCase();
  const a = h.split('.').map(Number);
  const ipv4 = a.length === 4 && a.every(x => Number.isInteger(x) && x >= 0 && x < 256);
  const privateIP = ipv4 && (a[0] === 10 || (a[0] === 172 && a[1] >= 16 && a[1] <= 31) || (a[0] === 192 && a[1] === 168) || a[0] === 127);
  // The server still checks its actual Tailscale peer map; this is URL validation,
  // not authorization to accept arbitrary machines in the CGNAT address range.
  const tailnetIP = (ipv4 && a[0] === 100 && a[1] >= 64 && a[1] <= 127) || /^\[fd7a:115c:a1e0:[0-9a-f:]+\]$/.test(h);
  if (u.protocol !== 'http:' || u.username || u.password || (!privateIP && !tailnetIP && h !== 'localhost' && !h.endsWith('.local')) || !u.port)
    throw Error('请输入工作台的本机、内网或 Tailscale HTTP 地址（含端口）');
  return u.origin;
}

function recoveryPage(url) {
  try { return /\/(?:risk|captcha)(?:\/|$)|\/user\/auth\/login/.test(new URL(url).pathname); }
  catch (_) { return false; }
}

function taskMatchesPage(task, actualURL) {
  try {
    const expected = new URL(task.url), actual = new URL(actualURL);
    if (actual.origin !== 'https://us.shein.com' || expected.origin !== actual.origin) return false;
    const pid = task.product_id || (task.stage === 'detail' ? expected.pathname.match(/-p-(\d+)\.html/)?.[1] : null);
    if (pid) return actual.pathname.match(/-p-(\d+)\.html/)?.[1] === String(pid);
    if (expected.pathname.replace(/\/$/, '') !== actual.pathname.replace(/\/$/, '')) return false;
    // Preserve source/filter/page identity, ignoring navigation tracking fields.
    return ['store_code', 'child_cat_id', 'tab', 'page'].every(key =>
      (expected.searchParams.get(key) || (key === 'page' ? '1' : '')) ===
      (actual.searchParams.get(key) || (key === 'page' ? '1' : '')));
  } catch (_) { return false; }
}

async function call(path, body) {
  const cfg = await chrome.storage.local.get(['key', 'base', 'clientId']);
  if (!cfg.key) throw Error('请登录工作台，复制本次任务配对码');
  if (!cfg.clientId) {
    cfg.clientId = crypto.randomUUID();
    await chrome.storage.local.set({clientId: cfg.clientId});
  }
  let r;
  try {
    r = await fetch(allowedBase(cfg.base) + path, {method: body ? 'POST' : 'GET', headers: {
      'X-Selection-Key': cfg.key, 'X-Collector-Client': cfg.clientId, 'Content-Type': 'application/json'
    }, body: body ? JSON.stringify(body) : undefined});
  } catch (_) { throw Error('连接不到工作台。请确认后台已启动、地址正确，并已允许此地址的访问权限'); }
  let d;
  try { d = await r.json(); } catch (_) { throw Error('工作台返回异常，请核对连接地址并重新打开工作台'); }
  if (!r.ok) { const e = Error(d.error || '工作台请求失败'); e.status = r.status; throw e; }
  return d;
}

async function record(message, extra = {}) { await chrome.storage.local.set({message, updated: Date.now(), ...extra}); }
async function stop(message) {
  await chrome.storage.local.set({enabled: false});
  await chrome.alarms.clear('selection-next');
  await record(message);
}

async function current(tabId, job, task) {
  await call('/api/collector/heartbeat', {job});
  if (!task) throw Error('当前没有已分配页面，请点击“继续采集”取得断点');
  const tab = await chrome.tabs.get(tabId);
  if (!recoveryPage(tab.url) && !taskMatchesPage(task, tab.url)) {
    await call('/api/capture', {job, task_id: task.id, claim_token: task.claim_token,
      capture: {url: tab.url, error: '当前页面与任务的店铺、类目、页码或商品不符，未保存此页'}});
    await stop('页面与任务不符。请恢复原店铺页面后确认恢复，再继续采集');
    return false;
  }
  const [{result: cap}] = await chrome.scripting.executeScript({target: {tabId}, files: ['collector.js']});
  if (!cap) throw Error('未能识别页面，断点已保留');
  const proof = {job, task_id: task.id, claim_token: task.claim_token};
  const saved = await call('/api/capture', {...proof, capture: cap});
  if (saved.terminated || saved.blocked) {
    await record(cap.error || '任务已停止，请在工作台核对断点', {jobStatus: saved.blocked ? 'blocked' : 'terminated'});
    await stop(cap.error || '任务已停止，请在工作台核对断点');
    return false;
  }
  let savedImages = 0, missingImages = 0;
  if (cap.kind === 'detail') {
    const p = cap.product;
    // Keep the original color-option index even when an earlier option has no image.
    const colorTargets = (p.colors || []).map((c, i) => ({url: c.src, role: 'color:' + i}));
    const galleryTargets = (p.gallery || []).slice(0, 8).map(x => ({url: x.src, role: 'gallery', view: x.view}));
    const targets = [...colorTargets, ...galleryTargets];
    for (const x of targets) {
      if (!(await chrome.storage.local.get('enabled')).enabled) {
        await record('详情已保存；图片下载已暂停。需要补图时勾选“补采未下载原图”再继续');
        return false;
      }
      try {
        if (!x.url) { missingImages++; continue; }
        const u = new URL(x.url);
        if (u.hostname !== 'img.ltwebstatic.com' || u.protocol !== 'https:') { missingImages++; continue; }
        const r = await fetch(x.url);
        if (!r.ok) { missingImages++; continue; }
        const a = new Uint8Array(await r.arrayBuffer());
        if (a.length > 20e6) { missingImages++; continue; }
        let bin = '';
        for (let n = 0; n < a.length; n += 8192) bin += String.fromCharCode(...a.subarray(n, n + 8192));
        await call('/api/image', {...proof, product_id: p.id, role: x.role, view: x.view || '', source_url: x.url, bytes: btoa(bin)});
        savedImages++;
      } catch (e) {
        if ([401, 403, 409].includes(e.status)) throw e;
        missingImages++;
      }
    }
    await record('详情已保存 ' + p.id + '；原图已保存 ' + savedImages + ' 张' +
      (missingImages ? '，' + missingImages + ' 张待补。可勾选“补采未下载原图”后继续' : '，正背面角色仍需图审确认'),
      {imageProgress: {product: p.id, saved: savedImages, missing: missingImages}});
  } else await record('列表第 ' + cap.page + ' 页已保存并回读');
  return true;
}

async function tick() {
  if (busy) return;
  busy = true;
  try {
    const cfg = await chrome.storage.local.get(['enabled', 'tabId', 'job', 'currentTask']);
    if (!cfg.enabled) return;
    await call('/api/collector/heartbeat', {job: cfg.job});
    const tab = await chrome.tabs.get(cfg.tabId);
    if (tab.status === 'loading') return;
    if (cfg.currentTask) {
      const ok = await current(cfg.tabId, cfg.job, cfg.currentTask);
      await chrome.storage.local.remove('currentTask');
      if (!ok) return;
    }
    if (!(await chrome.storage.local.get('enabled')).enabled) return;
    const {task, status} = await call('/api/next?job=' + encodeURIComponent(cfg.job));
    if (!task) {
      if (status) await chrome.storage.local.set({jobStatus: status});
      if (status === 'awaiting_review') await stop('队列处理结束。请回工作台核对缺图、分配正背面并审核；候选不等于已选款');
      else if (['paused', 'terminated', 'blocked', 'missing'].includes(status)) await stop('采集已停止，断点保留，请在工作台查看原因');
      return;
    }
    const u = new URL(task.url);
    if (u.hostname !== 'us.shein.com' || u.protocol !== 'https:') throw Error('只采集 SHEIN 美国站，其他渠道请使用资料导入');
    await chrome.storage.local.set({currentTask: task, jobStatus: 'running'});
    await record('正在打开 ' + (task.product_id || '店铺列表') + '，请勿切换此标签的店铺或商品');
    if ((await chrome.storage.local.get('enabled')).enabled) await chrome.tabs.update(cfg.tabId, {url: task.url});
  } catch (e) { await stop(e.message); }
  finally { busy = false; }
}

chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.status === 'complete') chrome.storage.local.get(['tabId', 'enabled']).then(c => {
    if (c.enabled && c.tabId === id) setTimeout(tick, 5000);
  });
});
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'selection-next') tick(); });
chrome.runtime.onMessage.addListener((p, sender, reply) => {
  (async () => {
    if (p.action === 'start') {
      if (busy) throw Error('当前页面正在保存，请稍后再操作');
      const prev = await chrome.storage.local.get(['key', 'job', 'enabled']);
      if (prev.enabled) throw Error('采集正在运行。请先暂停，再切换任务或重新配对');
      await chrome.storage.local.set({key: String(p.key || '').trim(), base: allowedBase(p.base)});
      const active = await call('/api/collector/task');
      const job = p.job || active.job;
      if (!job || job !== active.job) throw Error('配对码与任务不匹配，请重新取得本任务配对码');
      await chrome.storage.local.set({jobStatus: active.status || ''});
      if (active.status === 'blocked' && p.recovery_confirmed !== true)
        throw Error('此任务因访问限制已暂停。先在 SHEIN 正常恢复访问，再勾选“页面已恢复正常”后继续');
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      if (!tab?.url?.startsWith('https://us.shein.com/')) throw Error('请先选中已登录的 SHEIN 美国站标签页');
      if (recoveryPage(tab.url)) throw Error('SHEIN 仍在验证或限制页面，请先通过正常入口恢复访问');
      if (prev.key !== p.key || prev.job !== job || active.status === 'blocked') await chrome.storage.local.remove('currentTask');
      const resumed = await call('/api/job/action', {job, action: 'resume', recovery_confirmed: p.recovery_confirmed === true, retry_missing: p.retry_missing === true});
      await chrome.storage.local.set({enabled: true, tabId: tab.id, job, jobStatus: resumed.status || 'running',
        collectorWarning: resumed.message || (resumed.retry_limit_reached?.length ? '部分款式已达到补采次数上限，请回工作台人工补图：' + resumed.retry_limit_reached.join('、') : '')});
      await chrome.alarms.create('selection-next', {periodInMinutes: 0.5});
      await tick();
    } else if (p.action === 'stop') {
      await stop('已暂停，已保存的内容和断点保留');
      const c = await chrome.storage.local.get('job');
      if (c.job) try {
        const result = await call('/api/job/action', {job: c.job, action: 'pause'});
        await chrome.storage.local.set({jobStatus: result.status});
      } catch (e) { await record('助手已停止：' + e.message); }
    } else if (p.action === 'capture') {
      const c = await chrome.storage.local.get(['job', 'currentTask', 'enabled', 'tabId']);
      if (!c.enabled || !c.currentTask) throw Error('当前没有运行中的断点。请点击“继续采集”；缺图片时勾选“补采未下载原图”');
      if (busy) throw Error('当前页面正在采集，请稍后');
      busy = true;
      try { await current(c.tabId, c.job, c.currentTask); } finally { busy = false; }
    } else throw Error('不支持的助手操作');
    reply({ok: true});
  })().catch(e => reply({error: e.message}));
  return true;
});
