let actionPending = false;
let collectorState = {};
const $ = selector => document.querySelector(selector);
function showState(state) {
  collectorState = {...collectorState, ...state};
  $('#status').textContent = collectorState.message || '先登录工作台并取得本次任务配对码';
  $('#running-state').textContent = collectorState.enabled ? '采集中' : '未运行';
  $('#collector-warning').textContent = collectorState.collectorWarning || '';
  $('#collector-warning').hidden = !collectorState.collectorWarning;
  $('#updated').textContent = collectorState.updated ? '最后更新：' + new Date(collectorState.updated).toLocaleTimeString() : '';
  const images = collectorState.imageProgress;
  $('#image-status').textContent = images ? '最近商品 #' + images.product + '：原图已保存 ' + images.saved + ' 张，待补 ' + images.missing + ' 张' : '';
  $('#start').disabled = actionPending || !!collectorState.enabled;
  $('#capture').disabled = actionPending || !collectorState.enabled || !collectorState.currentTask;
  $('#stop').disabled = actionPending || !collectorState.enabled;
  $('#recovery-note').hidden = collectorState.jobStatus !== 'blocked';
}
chrome.storage.local.get(['key', 'job', 'message', 'base', 'enabled', 'currentTask', 'jobStatus', 'updated', 'imageProgress', 'collectorWarning']).then(c => {
  $('#key').value = c.key || '';
  $('#job').value = c.job || '';
  $('#base').value = c.base || 'http://127.0.0.1:8770';
  showState(c);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  const values = {};
  for (const key of ['message', 'enabled', 'currentTask', 'jobStatus', 'updated', 'imageProgress', 'collectorWarning'])
    if (key in changes) values[key] = changes[key].newValue;
  showState(values);
});
for (const action of ['start', 'capture', 'stop']) $('#' + action).onclick = async () => {
  if (actionPending) return;
  actionPending = true;
  showState({});
  let message;
  try {
    const base = new URL($('#base').value).origin;
    if (action === 'start' && !await chrome.permissions.contains({origins: [base + '/*']})) {
      if (!await chrome.permissions.request({origins: [base + '/*']})) throw Error('未授予此工作台地址的访问权限');
    }
    const r = await chrome.runtime.sendMessage({action, base, key: $('#key').value.trim(), job: $('#job').value.trim(),
      recovery_confirmed: $('#recovery').checked, retry_missing: $('#retry-missing').checked});
    if (r.error) throw Error(r.error);
    if (action === 'start') { $('#recovery').checked = false; $('#retry-missing').checked = false; }
  } catch (e) { message = e.message; }
  finally {
    actionPending = false;
    showState(await chrome.storage.local.get(['message', 'enabled', 'currentTask', 'jobStatus', 'updated', 'imageProgress', 'collectorWarning']));
    if (message) $('#status').textContent = message;
  }
};
