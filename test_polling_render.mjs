// Isolated headless UI regression: synthetic data/images, no live API or user profile.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import { chromium } from 'playwright';

const baseline = process.argv.includes('--baseline');
const script = fs.readFileSync(new URL(baseline ? './work/flicker-20261016/app.before.js' : './public/app.js', import.meta.url), 'utf8');
const html = fs.readFileSync(new URL('./public/index.html', import.meta.url), 'utf8').replace(/<script[^>]*>[\s\S]*?<\/script>/g, '');
const requests = [];
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aHlkAAAAASUVORK5CYII=', 'base64');
const server = http.createServer((req, res) => {
  requests.push(req.url);
  res.setHeader('Cache-Control', 'no-store');
  if (req.url.startsWith('/asset/')) { res.setHeader('Content-Type', 'image/png'); res.end(pixel); }
  else if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end(html); }
  else { res.statusCode = 404; res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({headless: true, executablePath: process.env.TEST_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const page = await browser.newPage({viewport: {width: 1420, height: 940}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.continue() : route.abort());
let passed = 0;
const check = (name, value) => { assert.equal(value, true, name); passed++; console.log('PASS ' + name); };
try {
  await page.goto(origin);
  await page.evaluate(() => {
    window.fixture = {
      state: {jobs: [{id:'job-a', status:'paused', rules:{name:'Fixture task', platform:'SHEIN', country:'US', min_sold:500, max_age_months:6, links:[]}, queue:{pending:0}}], designs:[{id:'design-a', job:'job-a', product_id:'a', state:'generated_pending_review', name:'Fixture design', changes:'Change color', keep:'Keep neckline', images:[{url:'/asset/result.png'}], revision:1}], exports:[], profiles:[], style_version:'1.2'},
      items: [{id:'a', job:'job-a', name:'Fixture dress', sales:'1k+ sold', front_url:'/asset/front.png', back_url:'/asset/back.png', colors:[], color_count:0, review:{primary_style:'Test'}, evaluation:{approved:true, status:'approved', age_status:'Test only', reasons:[]}}]
    };
    window.SelectionAuth = {ready:true, canEdit:true, user:{username:'fixture', role:'admin'}, bootstrap:()=>{}, request:async(path, data)=>{
      if(data !== undefined) throw Error('Unexpected mutation: ' + path);
      if(path === '/api/state') { window.pollCalls=(window.pollCalls||0)+1; return structuredClone(fixture.state); }
      if(path === '/api/style') return {styles:[]};
      if(path === '/api/approved') return {items:structuredClone(fixture.items.filter(x=>x.evaluation.approved))};
      if(path.startsWith('/api/items?')) return structuredClone(fixture.items);
      throw Error('Unexpected API: ' + path);
    }};
    window.initialItem=structuredClone(fixture.items[0]);
    document.querySelector('#auth-gate').hidden = true;
  });
  await page.addScriptTag({content: script});
  await page.evaluate(async() => { page = 'designs'; await refresh(); });
  await page.waitForFunction(() => [...document.querySelectorAll('#content img')].every(img => img.complete));
  await page.evaluate(() => {
    window.originalImages = [...document.querySelectorAll('#content img')];
    window.draft = document.querySelector('#design-changes');
    draft.value = 'UNSAVED draft'; draft.focus(); draft.setSelectionRange(3,7);
    document.querySelector('#design-prompt-design-a').open = true;
  });
  const initialImages = requests.filter(url => url.startsWith('/asset/')).length;
  await page.evaluate(async() => { for(let i=0;i<4;i++) await refresh({background:true}); });
  check('unchanged polling retains every image node', await page.evaluate(() => originalImages.every(img => img.isConnected && document.querySelector('#content').contains(img))));
  await page.waitForLoadState('networkidle');
  check('unchanged polling makes zero additional image requests', requests.filter(url => url.startsWith('/asset/')).length === initialImages);
  check('unsaved text, caret, focus and disclosure survive polling', await page.evaluate(() => draft === document.activeElement && draft.value === 'UNSAVED draft' && draft.selectionStart === 3 && draft.selectionEnd === 7 && document.querySelector('#design-prompt-design-a').open));
  await page.evaluate(async() => { fixture.state.designs[0].state='approved'; fixture.state.designs[0].review={note:'Approved fixture'}; await refresh({background:true}); });
  check('changed status renders while existing pictures remain', await page.evaluate(() => document.querySelector('#design-list').textContent.includes('设计师已确认') && originalImages.every(img=>img.isConnected)));
  await page.evaluate(async() => { SelectionAuth.canEdit=false; await refresh({background:true}); });
  check('read-only permission disables controls after polling', await page.evaluate(() => [...document.querySelectorAll('[data-design-review]')].every(x=>x.disabled)));
  await page.evaluate(async() => { SelectionAuth.canEdit=true; await refresh({background:true}); });
  check('restored edit permission enables applicable controls', await page.evaluate(() => [...document.querySelectorAll('[data-design-review]')].every(x=>!x.disabled)));
  await page.evaluate(async() => { fixture.items[0].evaluation.approved=false; await refresh({background:true}); });
  check('withdrawn approval removes old source and disables save without losing draft', await page.evaluate(() => !document.querySelector('#design-preview img') && document.querySelector('#save-design').disabled && draft.value==='UNSAVED draft'));
  await page.evaluate(async() => { fixture.items[0].evaluation.approved=true; page='collection'; filter='all'; await refresh(); window.firstCard=document.querySelector('#cards .card'); window.firstPhoto=firstCard.querySelector('img'); });
  await page.evaluate(async() => { fixture.items.unshift({...structuredClone(fixture.items[0]), id:'b', name:'New fixture', front_url:'/asset/new.png'}); await refresh({background:true}); });
  check('new candidate inserted without replacing existing card/photo', await page.evaluate(() => document.querySelectorAll('#cards .card').length===2 && firstCard.isConnected && firstCard.querySelector('img')===firstPhoto));
  await page.evaluate(async() => { fixture.items.reverse(); await refresh({background:true}); });
  check('candidate order follows server while retaining keyed cards', await page.evaluate(() => document.querySelector('#cards .card')===firstCard));
  await page.evaluate(async() => { page='approved'; await refresh(); });
  await page.locator('[data-select-approved]').first().check();
  await page.evaluate(async() => { window.approvedPhoto=document.querySelector('#approved-cards img'); await refresh({background:true}); });
  check('approval checkbox selection survives polling', await page.locator('[data-select-approved]').first().isChecked());
  await page.locator('#clear-selected').click();
  check('clear selection synchronizes live checkbox state without replacing photo', await page.evaluate(() => [...document.querySelectorAll('[data-select-approved]')].every(x=>!x.checked) && approvedPhoto.isConnected));
  await page.evaluate(async() => { fixture.items=[]; await refresh({background:true}); });
  check('removed records disappear and selected count is reset', await page.evaluate(() => !document.querySelector('#approved-cards .card') && document.querySelector('#selection-count').textContent.includes('0')));
  await page.evaluate(async() => {
    fixture.items=[structuredClone(initialItem)]; page='designs'; await refresh();
    window.timerImages=[...document.querySelectorAll('#content img')]; pollCalls=0;
  });
  await page.waitForLoadState('networkidle');
  const beforeTimer=requests.filter(url=>url.startsWith('/asset/')).length;
  await page.waitForFunction(() => pollCalls>0, {timeout:20000});
  await page.waitForLoadState('networkidle');
  check('real 15-second timer updates data without replacing or reloading images', await page.evaluate(() => timerImages.every(img=>img.isConnected)) && requests.filter(url=>url.startsWith('/asset/')).length===beforeTimer);
  check('no runtime exceptions', errors.length===0);
  console.log(JSON.stringify({passed, baseline, isolated:true, realBusinessMutations:0}));
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
