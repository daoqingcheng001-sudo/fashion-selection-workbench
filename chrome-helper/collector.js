(() => {
  // Read rendered page content only. No private application state or hidden APIs.
  const text = document.body?.innerText || '', url = location.href;
  const observed_at = new Date().toISOString();
  const base = {url, observed_at};
  if (/\/risk\/|\/captcha|\/user\/auth\/login/.test(location.pathname) || (text.length < 12000 && /STATUS:\s*403|Access Denied|verify (?:that )?you are human|完成验证|人机验证/i.test(text)))
    return {...base, blocked:true, error:'当前页面需要登录/人机验证或访问恢复；已停止翻页，不会重试绕过'};
  const clean = s => (s || '').trim();
  const sold = s => s?.match(/\d[\d,.]*\s*[km]?\+?\s*sold(?: recently)?/i)?.[0] || '';
  const id = url.match(/-p-(\d+)\.html/)?.[1];
  if (id) {
    const info = document.querySelector('[aria-label="Product information"]');
    const gallery = document.querySelector('[aria-label="Product images"]');
    const colorsRegion = document.querySelector('[aria-label="Color options"]');
    const nav = document.querySelector('nav[aria-label="Details"], [role="navigation"][aria-label="Details"]');
    const imgs = [...(gallery || document).querySelectorAll('img')].filter(x => x.currentSrc && x.offsetWidth > 0);
    const options = colorsRegion ? [...colorsRegion.querySelectorAll('[role="radio"]')] : [];
    const colors = options.map((x,i) => ({name:clean(x.getAttribute('aria-label') || x.innerText), option:i+1,
      src:x.querySelector('img')?.currentSrc || x.querySelector('img')?.src, selected:x.getAttribute('aria-checked')==='true'}));
    const name = clean(info?.querySelector('h1')?.innerText || document.querySelector('h1')?.innerText || document.title.split('|')[0]);
    if (!info && !gallery) return {...base,error:'未识别到商品信息或商品图片区域，保留断点等待页面恢复'};
    const dateLine = text.split('\n').find(x => /(?:First Listed|Date First Available|上市日期|首次上架)/i.test(x));
    const explicitDate = dateLine?.match(/20\d{2}[-/]\d{2}[-/]\d{2}/)?.[0]?.replaceAll('/','-');
    const product = {id,name,url,sku:(info?.innerText || text).match(/SKU:\s*(\S+)/)?.[1],
      breadcrumb:nav?[...nav.querySelectorAll('a')].map(x=>clean(x.innerText)).filter(Boolean):[],
      colors:colors.length?colors:[], color_count:colors.length||null,
      gallery:imgs.map(x=>({src:x.currentSrc,alt:x.alt,view:x.alt.match(/View (\d+)/i)?.[1] || '未指定视角'})),
      sales:sold([gallery?.innerText,...(gallery?[...gallery.querySelectorAll('[aria-label]')].map(x=>x.getAttribute('aria-label')):[])].join(' '))||sold(info?.innerText), detail_observed_at:observed_at,
      sales_period:'平台未公开统计周期', first_listed_at:explicitDate||null,
      date_source:explicitDate?url+' / '+dateLine:null,date_verified:!!explicitDate};
    if(product.sales){product.sales_source='商品详情页';product.sales_observed_at=observed_at;}
    return {...base,kind:'detail',product};
  }
  const map = new Map();
  for (const a of document.querySelectorAll('a[href*="-p-"]')) {
    const pid = a.href.match(/-p-(\d+)\.html/)?.[1]; if (!pid || a.offsetWidth === 0) continue;
    let card = a;
    for(let n=0;n<5 && card.parentElement;n++) {if(sold(card.innerText))break;card=card.parentElement;}
    const label = sold(card.innerText);
    const name = clean(a.getAttribute('aria-label') || a.querySelector('img')?.alt || a.innerText);
    if (label && name) map.set(pid,{id:pid,name,url:a.href,sales:label,sales_source:'店铺列表',observed_at});
  }
  if (!map.size) return {...base,error:'未发现可见商品卡片及sold标签；请确认进入正常美国站店铺列表'};
  const page = Number(new URL(url).searchParams.get('page')||1);
  let next_url = null;
  const next = [...document.querySelectorAll('a,button')].find(x => /^(?:Next|下一页)$/i.test(clean(x.getAttribute('aria-label')||x.innerText)) && !x.disabled && x.getAttribute('aria-disabled')!=='true');
  const count = Number(text.match(/Total\s+(\d+)\s+Pages/i)?.[1]||0);
  if (count > page) {const u = new URL(url);u.searchParams.set('page',String(page+1));next_url=u.href;}
  else if(next?.href)next_url=next.href;
  return {...base,kind:'list',page,total_pages:count||null,cards:[...map.values()],next_url};
})()
