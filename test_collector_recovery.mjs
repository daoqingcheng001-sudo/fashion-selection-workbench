// Mock-only collector tests: no browser connection, no platform or paid network.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('./chrome-helper/worker.js', import.meta.url), 'utf8');
const listURL = 'https://us.shein.com/store/home?store_code=DEMO_STORE&child_cat_id=4436&tab=items&page=4';
function harness(options = {}) {
  const data = {enabled: true, key: 'fixture-key', job: 'fixture-job', base: 'http://127.0.0.1:8782', clientId: 'fixture-client', tabId: 7, ...options.data};
  const calls = [], scripts = [], listeners = {};
  const task = options.task || {id: 12, stage: 'detail', product_id: '123', url: 'https://us.shein.com/Fake-p-123.html', claim_token: 'fake'};
  const tab = {id: 7, status: 'complete', url: task.url, ...options.tab};
  const cap = options.capture || {url: task.url, kind: 'detail', product: {id: '123', colors: [{name: 'Red'}, {name: 'Blue', src: 'https://img.ltwebstatic.com/blue.png'}], gallery: []}};
  const chrome = {
    storage: {local: {get: async () => ({...data}), set: async value => Object.assign(data, value), remove: async key => { delete data[key]; }}},
    alarms: {clear: async () => {}, create: async () => {}, onAlarm: {addListener: fn => { listeners.alarm = fn; }}},
    tabs: {get: async id => { calls.push({type: 'tab_get', id}); return {...tab, id}; }, query: async () => [tab], update: async (id, args) => {calls.push({type:'navigate', id, args});}, onUpdated: {addListener: fn => {listeners.update=fn;}}},
    runtime: {onMessage: {addListener: fn => {listeners.message=fn;}}},
    scripting: {executeScript: async args => { scripts.push(args); return [{result: cap}]; }}
  };
  const ctx = {URL, Error, Date, Uint8Array, String, setTimeout: () => {}, btoa: () => 'SYNTHETIC', crypto: {randomUUID: () => 'fixture-client'}, chrome,
    fetch: async (url, args) => {
      const body = args?.body ? JSON.parse(args.body) : null;
      calls.push({type: 'fetch', url, body});
      if (url === 'https://img.ltwebstatic.com/blue.png') return {ok: options.imageOK !== false, arrayBuffer: async () => new Uint8Array([1, 2]).buffer};
      if (!url.startsWith('http://127.0.0.1:8782/api/')) throw Error('Unexpected network forbidden: ' + url);
      let result = {saved: true};
      if (url.endsWith('/api/collector/task')) result = {job: 'fixture-job', status: options.jobStatus || 'paused'};
      if (url.endsWith('/api/job/action')) result = {status: body.action === 'resume' ? 'running' : 'paused', ...(options.resumeResult || {})};
      if (url.includes('/api/next?')) result = {task: null, status: 'awaiting_review'};
      return {ok: true, json: async () => result};
    }};
  vm.createContext(ctx);
  vm.runInContext(source + '\nthis.testAPI={allowedBase,taskMatchesPage,current,tick};', ctx);
  const message = payload => new Promise(resolve => listeners.message(payload, {}, resolve));
  return {data, calls, scripts, task, tab, api: ctx.testAPI, message};
}
let count=0;
async function test(name, fn) { await fn(); console.log('PASS ' + name); count++; }
await test('private and Tailnet addresses allowed, public and credential URLs rejected', () => {
  const {api}=harness();
  for (const url of ['http://127.0.0.1:8770','http://192.168.50.10:8770','http://100.100.64.1:8770','http://[fd7a:115c:a1e0::7777]:8770']) assert.equal(api.allowedBase(url),url);
  for (const url of ['http://8.8.8.8:8770','https://192.168.50.10:8770','http://name:pass@127.0.0.1:8770','http://100.128.1.2:8770','http://[2001:db8::1]:8770']) assert.throws(()=>api.allowedBase(url));
});
await test('list identity preserves store, page, category and ignores tracking', () => {
  const {api}=harness();const task={stage:'list',url:listURL};
  assert.equal(api.taskMatchesPage(task,listURL+'&source=insideFilter&sourceStatus=1'),true);
  for (const bad of [listURL.replace('page=4','page=5'),listURL.replace('DEMO_STORE','other'),listURL.replace('4436','other'),listURL.replace('tab=items','tab=other'),listURL.replace('us.shein.com','other.example')]) assert.equal(api.taskMatchesPage(task,bad),false);
  assert.equal(api.taskMatchesPage({stage:'list',url:'https://us.shein.com/store/home?store_code=x'},'https://us.shein.com/store/home?store_code=x&page=1'),true);
});
await test('detail identity uses exact product ID', () => {
  const {api,task}=harness();assert.equal(api.taskMatchesPage(task,task.url+'?src=track'),true);assert.equal(api.taskMatchesPage(task,task.url.replace('123','1234')),false);
});
await test('color image preserves original option index when earlier color has no image', async () => {
  const h=harness();await h.api.current(7,'fixture-job',h.task);
  const images=h.calls.filter(x=>x.url?.endsWith('/api/image'));
  assert.equal(images.length,1);assert.equal(images[0].body.role,'color:1');
  assert.equal(h.data.imageProgress.missing,1);assert.match(h.data.message,/1 张待补/);
});
await test('HTTP image failure remains visibly incomplete', async () => {
  const h=harness({imageOK:false});await h.api.current(7,'fixture-job',h.task);
  assert.equal(h.data.imageProgress.saved,0);assert.equal(h.data.imageProgress.missing,2);assert.match(h.data.message,/2 张待补/);
});
await test('wrong list page is blocked before DOM extraction', async () => {
  const h=harness({task:{id:1,stage:'list',url:listURL,claim_token:'fake'},tab:{url:listURL.replace('page=4','page=5')}});
  assert.equal(await h.api.current(7,'fixture-job',h.task),false);assert.equal(h.scripts.length,0);assert.equal(h.data.enabled,false);
  const capture=h.calls.find(x=>x.url?.endsWith('/api/capture'));assert.match(capture.body.capture.error,/页码/);assert.equal(capture.body.capture.cards,undefined);
});
await test('blocked resume requires explicit recovery confirmation', async () => {
  const h=harness({data:{enabled:false},jobStatus:'blocked'});
  const r=await h.message({action:'start',base:h.data.base,key:h.data.key,job:h.data.job});
  assert.match(r.error,/恢复/);assert.equal(h.calls.some(x=>x.url?.endsWith('/api/job/action')),false);
});
await test('even confirmed resume refuses current CAPTCHA or risk URL', async () => {
  const h=harness({data:{enabled:false},jobStatus:'blocked',tab:{url:'https://us.shein.com/risk/action/limit'}});
  const r=await h.message({action:'start',base:h.data.base,key:h.data.key,job:h.data.job,recovery_confirmed:true});
  assert.match(r.error,/仍在验证或限制页面/);assert.equal(h.calls.some(x=>x.url?.endsWith('/api/job/action')),false);
});
await test('explicit recovery and image retry flags are passed once', async () => {
  const h=harness({data:{enabled:false},jobStatus:'blocked',resumeResult:{message:'部分款式已达到3次上限，请人工补图',retry_limit_reached:['123']}});
  const r=await h.message({action:'start',base:h.data.base,key:h.data.key,job:h.data.job,recovery_confirmed:true,retry_missing:true});
  assert.equal(r.ok,true);const resume=h.calls.filter(x=>x.url?.endsWith('/api/job/action'));assert.equal(resume.length,1);assert.equal(resume[0].body.recovery_confirmed,true);assert.equal(resume[0].body.retry_missing,true);assert.match(h.data.collectorWarning,/3次上限/);
});
await test('running collector cannot be silently re-paired', async () => {
  const h=harness();const r=await h.message({action:'start',base:h.data.base,key:'changed-key',job:'other-job'});
  assert.match(r.error,/正在运行/);assert.equal(h.data.key,'fixture-key');assert.equal(h.data.job,'fixture-job');
});
await test('manual save uses bound tab and preserves page identity', async () => {
  const h=harness();h.data.currentTask=h.task;const r=await h.message({action:'capture'});
  assert.equal(r.ok,true);assert.equal(h.scripts[0].target.tabId,7);
});
await test('popup updates live status and disables inapplicable actions', async () => {
  const elems={};for(const id of ['key','job','base','status','image-status','collector-warning','running-state','updated','start','capture','stop','recovery-note','recovery','retry-missing'])elems['#'+id]={value:'',textContent:'',disabled:false,hidden:false,checked:false};
  let listener;const data={enabled:false,message:'Paused',jobStatus:'blocked',updated:1000};
  const ctx={Date,URL,document:{querySelector:id=>elems[id]},chrome:{storage:{local:{get:async()=>data},onChanged:{addListener:fn=>{listener=fn;}}}}};vm.createContext(ctx);vm.runInContext(fs.readFileSync(new URL('./chrome-helper/popup.js',import.meta.url),'utf8'),ctx);await Promise.resolve();
  assert.equal(elems['#capture'].disabled,true);assert.equal(elems['#stop'].disabled,true);assert.equal(elems['#recovery-note'].hidden,false);
  listener({message:{newValue:'Live: page 4'},enabled:{newValue:true},currentTask:{newValue:{id:4}},jobStatus:{newValue:'running'}},'local');
  assert.equal(elems['#status'].textContent,'Live: page 4');assert.equal(elems['#start'].disabled,true);assert.equal(elems['#capture'].disabled,false);assert.equal(elems['#stop'].disabled,false);
  listener({collectorWarning:{newValue:'已到3次上限，请人工补图'}},'local');
  assert.equal(elems['#collector-warning'].hidden,false);assert.match(elems['#collector-warning'].textContent,/3次上限/);
  listener({message:{newValue:'下一页处理中'}},'local');assert.match(elems['#collector-warning'].textContent,/3次上限/);
});
console.log(`${count} collector recovery tests passed; no real browser/network used.`);
