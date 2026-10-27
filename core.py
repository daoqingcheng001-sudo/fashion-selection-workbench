"""Persisted selection rules and evidence. No platform requests in this module."""
import calendar
import json
import re
import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from runtime_config import PROJECT_DIR, DATA_DIR, STYLE_PATH, LEGACY_DIR
PROJECT = PROJECT_DIR
DATA = DATA_DIR
LEGACY = LEGACY_DIR
DEFAULT = {
    'id': 'icon-us', 'name': 'SHEIN 美国站 · 演示店铺', 'platform': 'SHEIN', 'country': 'US',
    'store': 'SHEIN DEMO', 'links': ['https://us.shein.com/store/home?page=1&store_code=DEMO_STORE&tab=items&child_cat_id=4436&source=insideFilter&sourceStatus=1'],
    'min_sold': 500, 'max_age_months': 6, 'as_of': date.today().isoformat(),
    'excluded_terms': ['t-shirt', 't shirt', 'tee', 'sweatshirt', 'hoodie', 'sleepwear', 'pajama', 'lingerie', 'underwear', 'activewear', 'sports bra', 'sweatpants'],
    'category_terms': [], 'style_labels': [], 'exclude_basics': True,
    'age_basis': 'verified', 'cost_ranking': False, 'style_version': '1.2',
    'max_pages': 25, 'version': 1,
}


def dumps(x):
    return json.dumps(x, ensure_ascii=False)


def connect():
    c = sqlite3.connect(DATA / 'selection.sqlite3', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, profile TEXT NOT NULL, rules TEXT NOT NULL, status TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS items(job TEXT REFERENCES jobs(id), id TEXT, data TEXT NOT NULL, review TEXT NOT NULL DEFAULT '{}', updated REAL, PRIMARY KEY(job,id));
        CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY, job TEXT REFERENCES jobs(id), url TEXT, stage TEXT, product_id TEXT, state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, error TEXT, updated REAL, UNIQUE(job,url,stage));
        CREATE TABLE IF NOT EXISTS captures(id INTEGER PRIMARY KEY, job TEXT, url TEXT, data TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, job TEXT, action TEXT, detail TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS designs(id TEXT PRIMARY KEY, job TEXT, product_id TEXT, data TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, job TEXT, data TEXT, created REAL);
        ''')
        c.execute('INSERT OR IGNORE INTO profiles VALUES(?,?)', (DEFAULT['id'], dumps(DEFAULT)))
        c.execute("UPDATE queue SET state='pending',error='重启恢复：未提交的采集已回到队列' WHERE state='inflight'")
        c.execute("UPDATE jobs SET status='paused',updated=? WHERE status='running'", (time.time(),))
        for row in c.execute("SELECT id,data FROM designs").fetchall():
            d = json.loads(row['data'])
            if d.get('state') in ('uploading', 'submitting'):
                before_submit = d.get('state') == 'uploading' and not d.get('prompt_attempted_at')
                d.update(state='failed' if before_submit else 'uncertain', error='上传参考图时中断，尚未提交；可建立修订后重新授权' if before_submit else '曾中断，先核对生成历史，不自动重复提交')
                c.execute('UPDATE designs SET data=?,updated=? WHERE id=?', (dumps(d), time.time(), row['id']))


def event(c, job, action, detail):
    c.execute('INSERT INTO events(job,action,detail,created) VALUES(?,?,?,?)', (job, action, dumps(detail), time.time()))


def subtract_months(d, n):
    total = d.year * 12 + d.month - 1 - n
    y, m = total // 12, total % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def sold_lower(label):
    m = re.search(r'(\d[\d,]*(?:\.\d+)?)\s*([km]?)\s*\+?\s*sold', str(label), re.I)
    return float(m[1].replace(',', '')) * {'': 1, 'k': 1000, 'm': 1000000}[m[2].lower()] if m else None


def sku_month(sku):
    m = re.search(r'(?:^|[a-z])(\d{2})(\d{2})', str(sku), re.I)
    if not m or not 1 <= int(m[2]) <= 12:
        return None
    return f'20{m[1]}-{m[2]}'


def matches_excluded_term(term, words):
    term = term.lower()
    # Only known countable clothing terms get a plural; custom rules stay literal.
    plural = r's?' if term in {'t-shirt', 't shirt', 'tee', 'sweatshirt', 'hoodie', 'pajama', 'sports bra'} else ''
    return bool(re.search(r'(?<!\w)' + re.escape(term) + plural + r'(?!\w)', words))


def validate_profile(p):
    from urllib.parse import urlparse
    p = dict(p)
    if not str(p.get('name', '')).strip():
        raise ValueError('请输入规则名称')
    if p.get('platform') not in ('SHEIN', 'TEMU', 'Instagram', 'Pinterest', '独立站'):
        raise ValueError('来源类型不支持')
    if not re.fullmatch(r'[A-Z]{2}', str(p.get('country', ''))):
        raise ValueError('国家请填写两位大写代码，例如US')
    p['min_sold'] = int(p.get('min_sold', 500))
    p['max_age_months'] = int(p.get('max_age_months', 6))
    p['max_pages'] = int(p.get('max_pages', 25))
    if p['min_sold'] < 0 or not 1 <= p['max_age_months'] <= 36 or not 1 <= p['max_pages'] <= 100:
        raise ValueError('销量、款龄或页数设置无效')
    date.fromisoformat(p['as_of'])
    if p.get('age_basis') not in ('verified', 'sku_estimate'):
        raise ValueError('款龄依据设置无效')
    if not isinstance(p.get('links'), list) or not p['links']:
        raise ValueError('请至少填写一个来源链接')
    for u in p['links']:
        v = urlparse(u)
        if v.scheme != 'https' or not v.hostname or v.username or v.password or v.hostname in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('来源链接必须是公开HTTPS网址，不含账号密码')
        if p['platform'] == 'SHEIN' and v.hostname != 'us.shein.com' and p['country'] == 'US':
            raise ValueError('SHEIN美国站请使用us.shein.com的实际店铺/商品链接')
    p['cost_ranking'] = False
    return p


def evaluate(d, r, review=None):
    review = review or {}
    reasons, excluded, pending = [], False, False
    label = d.get('sales', '')
    sold = sold_lower(label)
    if sold is None:
        pending = True; reasons.append('销量标签待核')
    elif sold < r['min_sold']:
        excluded = True; reasons.append('销量未达到门槛')
    title = d.get('name', '')
    category = d.get('category', '') or ' > '.join(d.get('breadcrumb', []))
    words = (title + ' ' + category).lower()
    leaf = d.get('category_leaf') or (d.get('breadcrumb') or [''])[-1]
    exclude_words = (title + ' ' + leaf).lower()
    forbidden = [x for x in r.get('excluded_terms', []) if matches_excluded_term(x, exclude_words)]
    if forbidden or d.get('excluded_reason') or review.get('is_basic') is True:
        excluded = True; reasons.append(d.get('excluded_reason') or ('排除品类：' + ', '.join(forbidden)) if forbidden or d.get('excluded_reason') else '人工确认简单基础款')
    if not d.get('breadcrumb') and not category:
        pending = True; reasons.append('三级类目待采集')
    if r.get('category_terms') and not any(x.lower() in words for x in r['category_terms']):
        excluded = True; reasons.append('不在指定类目')
    age = '款龄待核'
    basis = review.get('first_listed_at') or d.get('first_listed_at')
    evidence = review.get('date_source') or d.get('date_source')
    confirmed = review.get('date_verified') is True or d.get('date_verified') is True
    asof = date.fromisoformat(r['as_of']); cutoff = subtract_months(asof, r['max_age_months'])
    if basis and evidence and confirmed:
        first = date.fromisoformat(basis)
        age = '真实日期已核验'
        if first > asof:
            pending = True; age = '日期冲突'; reasons.append('首次上架晚于筛选日期')
        elif first < cutoff:
            excluded = True; reasons.append(f'款龄超过{r["max_age_months"]}个月')
    elif r.get('age_basis') == 'sku_estimate' and sku_month(d.get('sku')):
        month = date.fromisoformat(sku_month(d['sku']) + '-01')
        last = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
        age = 'SKU月份估计，非真实上架日'
        if last < cutoff:
            excluded = True; reasons.append('SKU月份估计超出范围')
        elif month < cutoff or month > asof:
            pending = True; reasons.append('SKU月份跨边界或日期冲突')
    else:
        pending = True; reasons.append('缺真实首次上架日期及证据')
    front = bool(d.get('front')) and Path(d['front']).is_file(); back = bool(d.get('back')) and Path(d['back']).is_file()
    colors = d.get('colors', [])
    images = front and back and d.get('color_count') is not None and all(x.get('image') and Path(x['image']).is_file() for x in colors) and len(colors) == d.get('color_count') and len(colors) > 0
    if not images:
        pending = True; reasons.append('正背面或复色图片待补')
    if r.get('exclude_basics') and review.get('is_basic') is None:
        pending = True; reasons.append('是否简单基础款待图审')
    style = review.get('primary_style')
    if r.get('style_labels') and style not in r['style_labels']:
        pending = True; reasons.append('指定风格待确认')
    status = 'excluded' if excluded else 'pending' if pending else 'eligible'
    if review.get('decision') == 'rejected':
        status = 'rejected'
    approved = review.get('decision') == 'approved' and status == 'eligible'
    return {'status': status, 'approved': approved, 'reasons': reasons, 'sales_lower_bound': sold, 'sales_period': d.get('sales_period') or '平台未公开周期', 'age_status': age, 'cutoff': cutoff.isoformat(), 'sku_month_clue': sku_month(d.get('sku')), 'images_complete': images}


def upsert_item(c, job, d):
    pid = str(d.get('id', ''))
    if not re.fullmatch(r'[\w-]{1,100}', pid):
        raise ValueError('商品ID缺失或不合法')
    prev = c.execute('SELECT data,review,updated FROM items WHERE job=? AND id=?', (job, pid)).fetchone()
    old = json.loads(prev['data']) if prev else {}
    before = dict(old)
    review = json.loads(prev['review']) if prev else {}
    if d.get('colors'):
        indexed = {(x.get('name'), x.get('source_url') or x.get('src')): x for x in old.get('colors', [])}
        d = dict(d)
        d['colors'] = [{**indexed.get((x.get('name'), x.get('source_url') or x.get('src')), {}), **x} for x in d['colors']]
    # An incomplete refresh must not destroy confirmed images or earlier source facts.
    for k, v in d.items():
        if v not in (None, '', []):
            old[k] = v
    evidence_fields = ('name','url','sales','sales_period','sku','breadcrumb','category','category_leaf',
                       'front','back','colors','color_count','first_listed_at','date_source','date_verified','excluded_reason')
    changed = [key for key in evidence_fields if old.get(key) != before.get(key)]
    now = time.time()
    if prev and changed and review.get('decision') == 'approved':
        previous_review = dict(review)
        review.update(decision='pending', invalidated_at=now, invalidated_reason='商品资料已更新，请重新核对后确认', changed_fields=changed)
        event(c, job, 'approval_invalidated', {'product_id':pid, 'changed_fields':changed, 'previous_review':previous_review})
    # Refresh timestamps alone must not invalidate a saved approved design.
    revision = now if not prev or changed else prev['updated']
    c.execute('INSERT INTO items(job,id,data,review,updated) VALUES(?,?,?,?,?) ON CONFLICT(job,id) DO UPDATE SET data=excluded.data,review=excluded.review,updated=excluded.updated',
              (job, pid, dumps(old), dumps(review), revision))


def create_job(profile_id, legacy=False):
    jid = uuid.uuid4().hex[:12]
    with connect() as c:
        row = c.execute('SELECT data FROM profiles WHERE id=?', (profile_id,)).fetchone()
        if not row:
            raise ValueError('规则不存在')
        rules = json.loads(row['data'])
        c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (jid, profile_id, dumps(rules), 'paused', time.time(), time.time()))
        if legacy:
            candidates = json.loads((LEGACY / 'all-candidates.json').read_text())
            prepared = {str(x['id']): x for x in json.loads((LEGACY / 'prepared.json').read_text())}
            raw = {str(x['id']): x for x in json.loads((LEGACY / 'detail-records.json').read_text())}
            for item in candidates:
                pid = str(item['id'])
                merged = {**item, **raw.get(pid, {}), **prepared.get(pid, {})}
                merged['historical_checkpoint'] = True
                merged['source_dataset'] = str(LEGACY)
                upsert_item(c, jid, merged)
            pages = json.loads((LEGACY / 'verified-list-pages.json').read_text())
            for page in pages:
                c.execute('INSERT INTO captures(job,url,data,created) VALUES(?,?,?,?)', (jid, page['url'], dumps({'kind': 'list', 'page': page['page'], 'historical': True, 'cards_count': len(page.get('cards', []))}), time.time()))
            event(c, jid, 'import_checkpoint', {'items': len(candidates), 'pages': len(pages), 'date': '原数据采集时间保留；导入时间不是重新采集时间'})
        else:
            for u in rules['links']:
                enqueue(c, jid, u, 'detail' if '-p-' in u else 'list')
            event(c, jid, 'create_job', rules)
    return jid


def enqueue(c, jid, url, stage='detail', pid=''):
    if stage == 'detail':
        if not pid:
            match = re.search(r'-p-(\d+)\.html', str(url))
            if match: pid = match[1]
        if pid and c.execute("SELECT 1 FROM queue WHERE job=? AND stage='detail' AND product_id=?",(jid,str(pid))).fetchone():return
    c.execute('INSERT OR IGNORE INTO queue(job,url,stage,product_id,updated) VALUES(?,?,?,?,?)', (jid, url, stage, pid, time.time()))


def items(jid):
    with connect() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not job:
            raise ValueError('任务不存在')
        rules = json.loads(job['rules'])
        result = []
        for row in c.execute('SELECT * FROM items WHERE job=?', (jid,)):
            d = json.loads(row['data']); review = json.loads(row['review'])
            result.append({**d, 'review': review, 'revision': str(row['updated']), 'evaluation': evaluate(d, rules, review)})
    return sorted(result, key=lambda d: -(d['evaluation']['sales_lower_bound'] or 0))
