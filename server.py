#!/usr/bin/env python3
"""Local desktop workbench. Platform pages are read by the native browser only."""
import base64
import csv
import hashlib
import io
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import ipaddress
import socket
from http.cookies import SimpleCookie
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from core import *
from access import AccessStore, AccessError

MUTATION_LOCK = threading.RLock()
LOGIN_ATTEMPTS = defaultdict(list)
COOKIE_NAME = "fashionstudio_selection_session"
ALLOWED_EXTENSION = "chrome-extension://kijdmgfhddeebnfbgdpoooipnkaijpic"

def accounts():
    return AccessStore(connect)

from runtime_config import PORT, NODE, OUTPUT_DIR

COMFY = os.environ.get('COMFY_URL', 'http://127.0.0.1:8188').rstrip('/')
ASSETS = DATA / 'assets'
OUTPUT = OUTPUT_DIR
ASSET_MAP = {}
PAIR_FILE = DATA / 'collector-key.txt'


def collector_key():
    if not PAIR_FILE.exists():
        PAIR_FILE.write_text(secrets.token_urlsafe(32)); PAIR_FILE.chmod(0o600)
    return PAIR_FILE.read_text().strip()


def local_asset(path):
    p = Path(path).resolve()
    if not p.is_file() or p.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        return None
    # Only assets owned by this workspace or explicit application uploads.
    if not any(base.resolve() in p.parents for base in (PROJECT, DATA, OUTPUT)):
        return None
    key = hashlib.sha256(str(p).encode()).hexdigest()[:24]
    ASSET_MAP[key] = p
    return '/asset/' + key


def validate_media_paths(value):
    """Validate only fields consumed as local media; provenance labels are not paths."""
    if isinstance(value, list):
        for child in value: validate_media_paths(child)
    elif isinstance(value, dict):
        for key in ('front', 'back', 'image', 'path', 'reference_front', 'reference_back'):
            child = value.get(key)
            if isinstance(child, str) and child:
                path = Path(child).expanduser().resolve()
                if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp') or not any(root.resolve() in path.parents for root in (PROJECT, DATA, OUTPUT)):
                    raise ValueError('图片路径不属于工作台，请通过补图上传图片')
        for key in ('colors', 'local_gallery', 'images'):
            validate_media_paths(value.get(key))


def item_view(d):
    v = dict(d)
    for k in ('front', 'back'):
        v[k + '_url'] = local_asset(d[k]) if d.get(k) else None
    v['colors'] = [{**x, 'image_url': local_asset(x['image']) if x.get('image') else None} for x in d.get('colors', [])]
    v['local_gallery'] = [{**x, 'url': local_asset(x['path'])} for x in d.get('local_gallery', [])]
    cues = [('halter', '挂脖'), ('backless', '露背'), ('lace', '蕾丝外观'), ('ruffle', '荷叶边'), ('leopard', '豹纹'), ('mesh', '网纱外观'), ('rivet', '铆钉'), ('puff', '泡泡袖'), ('square neck', '方领'), ('ruched', '抽褶'), ('bow', '蝴蝶结'), ('slit', '开衩'), ('gradient', '渐变配色'), ('criss-cross', '交叉系带'), ('a-line', 'A字廓形')]
    v['element_suggestions'] = [label for key, label in cues if key in d.get('name', '').lower()]
    v['element_suggestion_basis'] = '商品标题关键词线索，需与实际图像对照；非视觉识别结论'
    return v


def state():
    with connect() as c:
        ps = [dict(json.loads(r['data']), _revision=json.loads(r['data']).get('version', 1)) for r in c.execute('SELECT * FROM profiles')]
        jobs = []
        source_items = {}
        for row in c.execute('SELECT * FROM jobs ORDER BY created DESC'):
            j = dict(row); j['rules'] = json.loads(j['rules'])
            rows = items(j['id'])
            stats = {'total': len(rows), 'eligible': 0, 'pending': 0, 'excluded': 0, 'rejected': 0, 'approved': 0, 'images': 0, 'unknown_age': 0}
            for d in rows:
                source_items[(j['id'], d['id'])] = d
                e = d['evaluation']; stats[e['status']] += 1; stats['approved'] += int(e['approved']); stats['images'] += int(e['images_complete'])
                stats['unknown_age'] += int(e['age_status'] == '款龄待核')
            j['stats'] = stats
            j['queue'] = dict((r['state'], r['n']) for r in c.execute('SELECT state,count(*) n FROM queue WHERE job=? GROUP BY state', (j['id'],)))
            j['pages_saved'] = c.execute("SELECT count(DISTINCT url) FROM captures WHERE job=? AND json_extract(data,'$.kind')='list'", (j['id'],)).fetchone()[0]
            jobs.append(j)
        designs = [dict(json.loads(r['data']), id=r['id'], job=r['job'], product_id=r['product_id'], revision=str(r['updated']), created=r['created'], updated=r['updated']) for r in c.execute('SELECT * FROM designs ORDER BY created DESC')]
        for d in designs:
            source = source_items.get((d['job'], d['product_id']))
            d['source_approved'] = bool(source and source['evaluation']['approved'])
            d['source_changed'] = bool(not source or str(d.get('source_revision', '')) != source['revision'])
            d['source_ready'] = d['source_approved'] and not d['source_changed']
            for img in d.get('images', []):
                img['url'] = local_asset(img['path'])
        exports = [dict(json.loads(r['data']), id=r['id'], job=r['job']) for r in c.execute('SELECT * FROM exports ORDER BY created DESC')]
    import workflow
    exports = workflow.annotate_exports(sys.modules[__name__], exports)
    return {'app': 'selection-workbench', 'version': '0.2.0', 'profiles': ps, 'jobs': jobs, 'designs': designs, 'exports': exports, 'style_version': json.loads(STYLE_PATH.read_text())['version']}


def missing_downloads(d):
    """Missing view assignment is manual review, not a reason to re-fetch a gallery."""
    colors = d.get('colors') or []
    if not d.get('breadcrumb') or not colors or d.get('color_count') != len(colors):
        return True
    if any(not color.get('image') or not Path(color['image']).is_file() for color in colors):
        return True
    if d.get('local_gallery'):
        return any(not image.get('path') or not Path(image['path']).is_file() for image in d['local_gallery'])
    return any(not d.get(role) or not Path(d[role]).is_file() for role in ('front','back'))


def job_action(jid, action, recovery_confirmed=False, retry_missing=False):
    reopened, retry_limit_reached = 0, []
    with connect() as c:
        j = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j:
            raise ValueError('任务不存在')
        if action == 'resume':
            if j['status'] == 'blocked' and not recovery_confirmed:
                raise AccessError(409, '来源页面仍处于访问待恢复状态，请先在浏览器完成正常恢复，再明确确认继续')
            rules = json.loads(j['rules'])
            if rules['platform'] != 'SHEIN' or rules.get('country') != 'US':
                raise ValueError('当前已接通SHEIN采集器；其他来源可保存规则与导入JSON，自动适配器后续接入')
            for d in items(jid):
                if d['evaluation']['status'] in ('excluded', 'rejected'):
                    continue
                if d.get('url') and (not d.get('breadcrumb') or not d['evaluation']['images_complete'] or not d.get('fresh_capture_at')):
                    enqueue(c, jid, d['url'], 'detail', d['id'])
                if retry_missing and d.get('url') and missing_downloads(d):
                    reopened += c.execute("UPDATE queue SET state='pending',error='员工明确补采未下载原图',updated=? WHERE job=? AND stage='detail' AND product_id=? AND state='done' AND attempts<3", (time.time(),jid,d['id'])).rowcount
                    if c.execute("SELECT 1 FROM queue WHERE job=? AND stage='detail' AND product_id=? AND state='done' AND attempts>=3",(jid,d['id'])).fetchone():retry_limit_reached.append(d['id'])
            c.execute("UPDATE queue SET state='pending',error=NULL WHERE job=? AND state IN ('blocked','failed')", (jid,))
            status = 'running'
        elif action == 'pause':
            status = 'terminated' if j['status'] == 'terminated' else 'paused'
        elif action == 'terminate':
            status = 'terminated'
            c.execute("UPDATE queue SET state='pending',error='用户终止采集，断点保留',updated=? WHERE job=? AND state='inflight'", (time.time(), jid))
        else:
            raise ValueError('动作无效')
        c.execute('UPDATE jobs SET status=?,updated=? WHERE id=?', (status, time.time(), jid))
        event(c, jid, action, {'state': status})
    return {'status':status, 'reopened':reopened, 'retry_limit_reached':retry_limit_reached, 'message':('部分款式已达到3次采集上限，请人工补图：'+', '.join(retry_limit_reached)) if retry_limit_reached else ''}


def refilter_job(p):
    new_id = create_job(p['profile'])
    with connect() as c:
        c.execute('DELETE FROM queue WHERE job=?', (new_id,))
        for row in c.execute('SELECT * FROM items WHERE job=?', (p['job'],)).fetchall():
            c.execute('INSERT INTO items(job,id,data,review,updated) VALUES(?,?,?,?,?)', (new_id, row['id'], row['data'], row['review'], row['updated']))
        for row in c.execute('SELECT * FROM captures WHERE job=?', (p['job'],)).fetchall():
            c.execute('INSERT INTO captures(job,url,data,created) VALUES(?,?,?,?)', (new_id, row['url'], row['data'], row['created']))
        event(c, new_id, 'refilter_checkpoint', {'parent_job': p['job'], 'source_facts_unchanged': True, 'rules': '新规则快照'})
    return new_id


def next_task(jid):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        j = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j or j['status'] != 'running':
            return {'task': None, 'status': j['status'] if j else 'missing'}
        c.execute("UPDATE queue SET state='pending',error='采集连接超时后恢复' WHERE job=? AND state='inflight' AND updated<?", (jid, time.time() - 180))
        if c.execute("SELECT 1 FROM queue WHERE job=? AND state='inflight'", (jid,)).fetchone():
            return {'task': None}
        task = c.execute("SELECT * FROM queue WHERE job=? AND state='pending' ORDER BY CASE stage WHEN 'list' THEN 0 ELSE 1 END,id LIMIT 1", (jid,)).fetchone()
        if not task:
            c.execute("UPDATE jobs SET status='awaiting_review' WHERE id=?", (jid,))
            return {'task': None, 'status': 'awaiting_review'}
        c.execute("UPDATE queue SET state='inflight',attempts=attempts+1,updated=? WHERE id=?", (time.time(), task['id']))
        return {'task': dict(task), 'rules': json.loads(j['rules'])}


def _capture_url_identity(url, kind):
    u = urllib.parse.urlsplit(str(url or ''))
    if u.scheme != 'https' or u.hostname != 'us.shein.com' or u.username or u.password:
        raise ValueError('采集来源不是 SHEIN 美国站公开页面，断点未覆盖')
    if kind == 'detail':
        m = re.search(r'-p-(\d+)\.html', u.path)
        if not m: raise ValueError('商品来源链接无法识别，断点未覆盖')
        return m[1]
    query = urllib.parse.parse_qs(u.query)
    important = ('store_code','child_cat_id','category_id','tab','sort')
    try: page = int(query.get('page', ['1'])[0])
    except (TypeError,ValueError): raise ValueError('列表页码无效')
    if page < 1: raise ValueError('列表页码无效')
    return (u.path.rstrip('/'), tuple((key, tuple(query.get(key, []))) for key in important), page)


def _verify_capture_source(task, cap):
    if not task: return
    expected = _capture_url_identity(task['url'], task['stage'])
    actual = _capture_url_identity(cap.get('url'), cap['kind'])
    if expected != actual:
        raise ValueError('实际店铺、类目、页码或商品与采集断点不符，未保存')
    if cap['kind'] == 'list':
        if int(cap.get('page', actual[-1])) != actual[-1]:
            raise ValueError('页面标识与实际页码不符，未保存')
        if cap.get('next_url'):
            nxt = _capture_url_identity(cap['next_url'], 'list')
            if nxt[:-1] != actual[:-1] or nxt[-1] != actual[-1]+1:
                raise ValueError('下一页不属于当前店铺或顺序不连续，未添加队列')
    else:
        pid = str(cap.get('product', {}).get('id', ''))
        if pid != (str(task['product_id']) if task['product_id'] else expected):
            raise ValueError('实际商品ID与队列不符，未保存')


def save_capture(payload):
    jid, tid, cap = payload['job'], payload.get('task_id'), payload['capture']
    with connect() as c:
        j = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not j:
            raise ValueError('任务不存在')
        if j['status'] == 'terminated':
            return {'saved': False, 'terminated': True, 'status': 'terminated'}
        task = c.execute('SELECT * FROM queue WHERE id=? AND job=?', (tid, jid)).fetchone() if tid else None
        if tid and not task:
            raise ValueError('采集队列不匹配')
        if task and task['state'] == 'done':
            return {'saved': True, 'duplicate_callback': True}
        if cap.get('blocked') or cap.get('error'):
            message = cap.get('error') or '登录、验证或访问限制；请在来源浏览器完成正常恢复'
            if tid:
                c.execute("UPDATE queue SET state='blocked',error=?,updated=? WHERE id=?", (message, time.time(), tid))
            c.execute("UPDATE jobs SET status='blocked',updated=? WHERE id=?", (time.time(), jid))
            event(c, jid, 'access_blocked', {'url': cap.get('url'), 'message': message})
            return {'saved': True, 'blocked': True}
        rules = json.loads(j['rules'])
        if task and task['stage'] != cap.get('kind'):
            raise ValueError('页面类型与队列不符，未覆盖断点')
        _verify_capture_source(task, cap)
        if cap.get('kind') == 'detail':
            d = cap['product']
            if task and task['product_id'] and str(d['id']) != task['product_id']:
                raise ValueError('实际商品ID与队列不符，未保存')
            d['fresh_capture_at'] = cap.get('observed_at')
            d['historical_checkpoint'] = False
            upsert_item(c, jid, d)
        elif cap.get('kind') == 'list':
            for d in cap.get('cards', []):
                if sold_lower(d.get('sales')) is not None and sold_lower(d['sales']) >= rules['min_sold']:
                    d['observed_at'] = cap.get('observed_at')
                    upsert_item(c, jid, d); enqueue(c, jid, d['url'], 'detail', d['id'])
            nxt = cap.get('next_url')
            if nxt and cap.get('page', 1) < rules['max_pages']:
                enqueue(c, jid, nxt, 'list')
        else:
            raise ValueError('当前未识别商品页或店铺列表；没有保存为成功')
        c.execute('INSERT INTO captures(job,url,data,created) VALUES(?,?,?,?)', (jid, cap.get('url'), dumps(cap), time.time()))
        if tid:
            c.execute("UPDATE queue SET state='done',error=NULL,updated=? WHERE id=?", (time.time(), tid))
        c.execute('UPDATE jobs SET updated=? WHERE id=?', (time.time(), jid))
        event(c, jid, 'capture_saved', {'kind': cap['kind'], 'url': cap.get('url'), 'task': tid})
    # Commit is visible before reporting success.
    with connect() as c:
        receipt = c.execute('SELECT id FROM captures WHERE job=? ORDER BY id DESC LIMIT 1', (jid,)).fetchone()
    return {'saved': True, 'receipt': receipt['id']}


def save_image(p):
    from PIL import Image
    raw = base64.b64decode(p['bytes'], validate=True)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError('图片过大')
    im = Image.open(io.BytesIO(raw)); im.verify()
    with connect() as c:
        row = c.execute('SELECT data FROM items WHERE job=? AND id=?', (p['job'],p['product_id'])).fetchone()
        if not row: raise ValueError('商品不存在')
        current = json.loads(row['data'])
    role = str(p.get('role',''))
    if role.startswith('color:'):
        try: index = int(role.split(':',1)[1])
        except ValueError: raise ValueError('颜色序号无效')
        if index < 0 or index >= len(current.get('colors', [])): raise ValueError('颜色序号已变化，请重新补采')
    elif role not in ('front','back','gallery'): raise ValueError('图片角色无效')
    path = ASSETS / (hashlib.sha256(raw).hexdigest() + '.png')
    im = Image.open(io.BytesIO(raw)); im.convert('RGB').save(path, 'PNG')
    with connect() as c:
        row = c.execute('SELECT data FROM items WHERE job=? AND id=?', (p['job'], p['product_id'])).fetchone()
        if not row:
            raise ValueError('商品不存在')
        d = json.loads(row['data']); role = p['role']
        if role in ('front', 'back'):
            d[role] = str(path)
            d.setdefault('image_sources', {})[role] = p.get('source_url') or '员工上传，来源待核'
        elif role.startswith('color:'):
            index = int(role.split(':')[1]); d['colors'][index]['image'] = str(path)
        elif role == 'gallery':
            gallery = d.setdefault('local_gallery', [])
            if not any(x.get('path') == str(path) and x.get('source_url') == p.get('source_url') for x in gallery):
                gallery.append({'path': str(path), 'source_url': p.get('source_url'), 'view': p.get('view', '待确认')})
        else:
            raise ValueError('图片角色无效')
        upsert_item(c, p['job'], d)
        event(c, p['job'], 'image_saved', {'product_id': p['product_id'], 'role': role, 'source': p.get('source_url')})
    return {'saved': True, 'asset': local_asset(path)}


def review_item(p):
    with connect() as c:
        row = c.execute('SELECT data,review FROM items WHERE job=? AND id=?', (p['job'], p['product_id'])).fetchone()
        if not row:
            raise ValueError('商品不存在')
        d, prev = json.loads(row['data']), json.loads(row['review'])
        r = {**prev, **p['review']}
        if not r.get('reviewer', '').strip():
            raise ValueError('请填写审核人')
        if r.get('decision') not in ('pending', 'approved', 'rejected'):
            raise ValueError('审核状态无效')
        if r.get('first_listed_at'):
            date.fromisoformat(r['first_listed_at'])
            if r.get('date_verified') and not r.get('date_source', '').strip():
                raise ValueError('核验上架日期必须填写证据来源')
        rules = json.loads(c.execute('SELECT rules FROM jobs WHERE id=?', (p['job'],)).fetchone()['rules'])
        if r.get('decision') == 'approved' and evaluate(d, rules, r)['status'] != 'eligible':
            raise ValueError('存在筛选条件或图片缺项，先保存待核并补齐；不能标成已选')
        if r.get('decision') == 'approved':
            for key in ('invalidated_at','invalidated_reason','changed_fields'): r.pop(key, None)
        r['reviewed_at'] = time.time()
        c.execute('UPDATE items SET review=?,updated=? WHERE job=? AND id=?', (dumps(r), time.time(), p['job'], p['product_id']))
        event(c, p['job'], 'review_saved', {'product_id': p['product_id'], 'review': r, 'previous': prev})
    return {'saved': True}


def comfy(path, payload=None, timeout=8):
    body = dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(COMFY + path, data=body, headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def generation_info():
    info = comfy('/object_info/SHEINGPTImage2')['SHEINGPTImage2']['input']
    return {'connected': True, 'node': 'SHEINGPTImage2', 'required': info['required'], 'optional': info.get('optional', {}), 'queue': comfy('/queue')}


def design_draft(p):
    """Create or edit a draft; generated versions and their authorization are immutable."""
    import workflow
    source = workflow.get_approved_source(sys.modules[__name__], p['job'], p['product_id'], p.get('expected_revision'))
    changes = str(p.get('changes', '')).strip()
    if not changes:
        raise ValueError('请填写改款内容')
    name = str(p.get('name') or '改款概念').strip()
    keep = str(p.get('keep', '')).strip()
    prompt = ('Create ONE original womenswear fashion design concept reference image, frontal full garment, vertical composition. '
              'The product reference supplies the original garment construction and appearance, not the identity of its model. '
              'Apply ONLY these designer-requested changes: ' + changes + '. '
              'Keep unchanged: ' + (keep or 'all garment features not explicitly changed') + '. '
              'Make the changed design clearly readable. Do not claim unseen rear construction or true fiber composition. '
              'Plain neutral studio background; no logos, watermark, text, collage or multiple panels. '
              'This is a new-design CONCEPT, not a faithful photograph of the original SKU and not an approved listing image.')
    data = {'name': name, 'changes': changes, 'keep': keep, 'prompt': p.get('prompt') or prompt,
            'state': 'draft', 'source_name': source.get('name'), 'reference_front': source.get('front'), 'reference_back': source.get('back'),
            'source_revision': source['revision'], 'source_job_name': source.get('source_job_name'),
            'source_snapshot': {key: source.get(key) for key in ('id', 'name', 'url', 'sales', 'sku', 'breadcrumb', 'colors', 'review', 'source_rules', 'revision')},
            'model': p.get('model', 'gpt-image-2-2026-04-21'), 'size': '1024x1536', 'quality': 'medium', 'images': [], 'review': None}
    request_id = p.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id)):
        raise AccessError(400, '保存请求编号无效，请刷新后重试')
    with connect() as c:
        parent_id = p.get('parent_design_id')
        if parent_id:
            parent = c.execute('SELECT * FROM designs WHERE id=?', (parent_id,)).fetchone()
            if not parent or (parent['job'], parent['product_id']) != (p['job'], p['product_id']):
                raise AccessError(404, '修订原方案不存在或原款不匹配')
            if str(parent['updated']) != str(p.get('expected_parent_revision')):
                raise AccessError(409, '原方案已更新，请重新打开修订')
            parent_data = json.loads(parent['data'])
            if parent_data.get('state') in ('uploading', 'submitting', 'running', 'uncertain'):
                raise AccessError(409, '原方案仍在执行或提交待核，请先核对结果，再建立修订')
            data.update(parent_design_id=parent_id, revision_number=int(parent_data.get('revision_number', 1))+1,
                        parent_review=parent_data.get('review'))
        else:
            data['revision_number'] = 1
        fields = ('name','changes','keep','prompt','model','source_revision','parent_design_id')
        fingerprint = hashlib.sha256(dumps([p['job'], p['product_id'], {k:data.get(k) for k in fields}]).encode()).hexdigest()
        data['draft_fingerprint'] = fingerprint
        existing = c.execute('SELECT * FROM designs WHERE id=?', (p.get('id'),)).fetchone() if p.get('id') else None
        if p.get('id'):
            if not existing: raise AccessError(404, '改款方案不存在')
            old = json.loads(existing['data'])
            if str(existing['updated']) != str(p.get('expected_design_revision')):
                raise AccessError(409, '方案已被更新，请刷新后继续编辑')
            if old.get('state') != 'draft': raise AccessError(409, '已提交的方案不能覆盖，请建立修订')
            if (existing['job'], existing['product_id']) != (p['job'], p['product_id']):
                raise AccessError(409, '不能把现有方案移到另一款，请新建方案')
            if old.get('source_revision') != source['revision']:
                raise AccessError(409, '原款已更新，请从已确认款重新建立方案')
            for key in ('parent_design_id', 'parent_review', 'revision_number', 'request_id'):
                if key in old: data[key] = old[key]
            # The revision chain is immutable even if the caller omits parent fields.
            data['draft_fingerprint'] = hashlib.sha256(dumps([p['job'], p['product_id'], {k:data.get(k) for k in fields}]).encode()).hexdigest()
            now = time.time()
            c.execute('UPDATE designs SET data=?,updated=? WHERE id=?', (dumps(data), now, existing['id']))
            event(c, p['job'], 'design_draft_updated', {'id':existing['id'], 'previous_revision':str(existing['updated'])})
            return {'id':existing['id'], 'job':p['job'], 'product_id':p['product_id'], **data, 'revision':str(now), 'created':existing['created'], 'updated':now}
        for record in c.execute('SELECT * FROM designs WHERE job=? AND product_id=? ORDER BY created DESC', (p['job'], p['product_id'])):
            previous = json.loads(record['data'])
            if request_id and previous.get('request_id') == request_id:
                if previous.get('draft_fingerprint') != fingerprint:
                    raise AccessError(409, '该保存请求已用于另一份内容，请刷新后重试')
                return {'id':record['id'], 'job':record['job'], 'product_id':record['product_id'], **previous, 'revision':str(record['updated']), 'created':record['created'], 'updated':record['updated'], 'existing':True}
            if previous.get('state') == 'draft' and previous.get('draft_fingerprint') == fingerprint:
                return {'id':record['id'], 'job':record['job'], 'product_id':record['product_id'], **previous, 'revision':str(record['updated']), 'created':record['created'], 'updated':record['updated'], 'existing':True}
        did, now = uuid.uuid4().hex[:12], time.time()
        if request_id: data['request_id'] = request_id
        c.execute('INSERT INTO designs VALUES(?,?,?,?,?,?)', (did, p['job'], p['product_id'], dumps(data), now, now))
        event(c, p['job'], 'design_draft', {'id': did, 'product_id': p['product_id'], 'parent_design_id':parent_id})
    return {'id': did, 'job':p['job'], 'product_id':p['product_id'], **data, 'revision':str(now), 'created':now, 'updated':now}


def approved_design_source(row):
    """A draft keeps its approved source revision until the final submit gate."""
    import workflow
    d = json.loads(row['data'])
    if not d.get('source_revision'):
        raise AccessError(409, '此历史方案缺少已确认款版本，请从已确认款重新创建改款方案')
    return workflow.get_approved_source(sys.modules[__name__], row['job'], row['product_id'], d['source_revision'])


def generation_signature(row):
    data = json.loads(row['data'])
    fields = ('source_revision','changes','keep','prompt','model','size','quality','parent_design_id')
    values = {key:' '.join(str(data.get(key, '')).split()) for key in fields}
    return (row['job'], row['product_id'], dumps(values))


def assert_no_duplicate_generation(row):
    signature = generation_signature(row)
    with connect() as c:
        for previous in c.execute('SELECT * FROM designs WHERE job=? AND product_id=? AND id<>?', (row['job'],row['product_id'],row['id'])):
            data = json.loads(previous['data'])
            submitted = data.get('prompt_id') or data.get('prompt_attempted_at') or data.get('images') or data.get('state') in ('uploading','submitting','running','uncertain')
            if submitted and generation_signature(previous) == signature:
                raise AccessError(409, '相同原款和方案已有提交记录，请查看已有结果；需要新一版时请从该记录建立修订', {'existing_design_id':previous['id']})


def preflight_design(did):
    with connect() as c:
        row = c.execute('SELECT * FROM designs WHERE id=?', (did,)).fetchone()
    if not row:
        raise ValueError('改款任务不存在')
    approved_design_source(row)
    d = json.loads(row['data'])
    if d.get('state') != 'draft':
        raise AccessError(409, '此方案已提交；需要调整时请建立修订，不能重复生成')
    assert_no_duplicate_generation(row)
    with connect() as c:
        if any(json.loads(r['data']).get('state') in ('uploading','submitting','running','uncertain') for r in c.execute('SELECT data FROM designs')):
            raise AccessError(409, '已有生图任务在执行或待核，请先查看该任务结果')
    validate_media_paths({'reference_front': d.get('reference_front'), 'reference_back': d.get('reference_back')})
    if not d.get('reference_front') or not Path(d['reference_front']).is_file():
        raise ValueError('请先补齐商品正面原图')
    try:
        info = generation_info()
    except Exception as exc:
        raise AccessError(503, '暂时无法连接生图服务，方案已保存；恢复连接后再核对，不会自动提交') from exc
    optional = info['optional']
    models = optional.get('model', [['gpt-image-2-2026-04-21']])[0]
    if d['model'] not in models:
        raise ValueError('当前工作流不支持所选模型')
    return {'id': did, 'ready': True, 'output_count': 1, 'reference_roles': ['product_front'] + (['product_back'] if d.get('reference_back') else []),
            'model': d['model'], 'size': d['size'], 'quality': d['quality'], 'prompt': d['prompt'], 'provider_may_charge': True,
            'state': d['state'], 'name': d['name'], 'source_name': d.get('source_name'), 'changes': d['changes'], 'keep': d['keep'], 'message': '只准备1张概念参考图；尚未提交调用'}


def update_design(did, **fields):
    with MUTATION_LOCK, connect() as c:
        row = c.execute('SELECT data FROM designs WHERE id=?', (did,)).fetchone()
        if not row: raise AccessError(404, '改款任务不存在')
        d = json.loads(row['data']); d.update(fields)
        c.execute('UPDATE designs SET data=?,updated=? WHERE id=?', (dumps(d), time.time(), did))


def upload_comfy(path):
    boundary = 'selection-' + uuid.uuid4().hex
    validate_media_paths({'image': path})
    p = Path(path)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="selection-{p.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + p.read_bytes() + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n--{boundary}--\r\n'.encode())
    req = urllib.request.Request(COMFY + '/upload/image', data=body, headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    v = json.load(urllib.request.urlopen(req, timeout=30))
    return (v.get('subfolder', '').strip('/') + '/' + v['name']).lstrip('/')


def run_design(did):
    submitted_attempt = False
    try:
        with MUTATION_LOCK, connect() as c:
            row = c.execute('SELECT * FROM designs WHERE id=?', (did,)).fetchone()
            approved_design_source(row)
            d = json.loads(row['data'])
            if d.get('state') != 'uploading': return
        graph = {}; inputs = {'prompt': d['prompt'], 'cost_confirmed': True, 'model': d['model'], 'size': d['size'], 'quality': d['quality']}
        for i, role in enumerate(('front', 'back'), 1):
            path = d.get('reference_' + role)
            if path and Path(path).is_file():
                graph[str(i)] = {'class_type': 'LoadImage', 'inputs': {'image': upload_comfy(path)}}
                inputs['product_' + role] = [str(i), 0]
        graph['10'] = {'class_type': 'SHEINGPTImage2', 'inputs': inputs}
        graph['11'] = {'class_type': 'SaveImage', 'inputs': {'images': ['10', 0], 'filename_prefix': 'Selection/' + did + '/concept'}}
        # Uploads can take time. Recheck approval/version immediately before the
        # paid request, keeping source edits outside this final submission gate.
        with MUTATION_LOCK:
            with connect() as c:
                row = c.execute('SELECT * FROM designs WHERE id=?', (did,)).fetchone()
            if json.loads(row['data']).get('state') != 'uploading': return
            approved_design_source(row)
            assert_no_duplicate_generation(row)
            update_design(did, state='submitting', graph=graph, prompt_attempted_at=time.time())
            submitted_attempt = True
            response = comfy('/prompt', {'prompt': graph, 'client_id': 'selection-' + did}, timeout=30)
            if not response.get('prompt_id'):
                raise ValueError('生成队列未返回任务ID：' + dumps(response))
            update_design(did, state='running', prompt_id=response['prompt_id'])
    except Exception as e:
        # A pre-submit failure cannot have incurred a paid request. Once attempted,
        # acceptance may be uncertain: preserve the slot and never auto-retry.
        update_design(did, state='uncertain' if submitted_attempt else 'failed', error=str(e))


def _history_images(did, hist):
    """Read already-produced ComfyUI files; this function never submits a prompt."""
    from PIL import Image
    images, seen = [], set()
    outputs = hist.get('outputs', {})
    for output in ([outputs['11']] if '11' in outputs else outputs.values()):
        for image in output.get('images', []):
            if image.get('type') not in (None, 'output'):
                continue
            key = dumps(image)
            if key in seen: continue
            seen.add(key)
            raw = urllib.request.urlopen(COMFY + '/view?' + urllib.parse.urlencode(image), timeout=30).read()
            with Image.open(io.BytesIO(raw)) as verified: verified.verify()
            ASSETS.mkdir(parents=True, exist_ok=True)
            path = ASSETS / (hashlib.sha256(raw).hexdigest() + '.png')
            with Image.open(io.BytesIO(raw)) as verified: verified.convert('RGB').save(path, 'PNG')
            images.append({'path':str(path), 'url':local_asset(path), 'label':'AI改款概念参考，待设计师确认'})
    return images


def _belongs_to_design(prompt, did):
    return (isinstance(prompt, (list, tuple)) and len(prompt) > 3 and
            isinstance(prompt[3], dict) and prompt[3].get('client_id') == 'selection-' + did)


def check_design(did):
    """Reconcile an existing task by exact client ID, never retry or release unknown work."""
    with connect() as c:
        row = c.execute('SELECT * FROM designs WHERE id=?', (did,)).fetchone()
    if not row: raise AccessError(404, '改款任务不存在')
    d = json.loads(row['data'])
    if d.get('state') not in ('running', 'uncertain'):
        return {'id':did, 'state':d.get('state'), 'message':'此方案无需查询生成队列', 'checked':False}
    try:
        prompt_id = d.get('prompt_id')
        hist = None
        if not prompt_id:
            queue = comfy('/queue')
            history = comfy('/history?max_items=100')
            matches = {}
            for record in queue.get('queue_running', []) + queue.get('queue_pending', []):
                if _belongs_to_design(record, did): matches[str(record[1])] = None
            for pid, record in history.items():
                if _belongs_to_design(record.get('prompt'), did): matches[str(pid)] = record
            if len(matches) != 1:
                message = '尚未在当前队列和最近历史找到唯一提交记录；保持待核，不会重新生成。请管理员核对上游记录与费用。'
                update_design(did, last_checked_at=time.time(), poll_error=message)
                return {'id':did, 'state':'uncertain', 'checked':True, 'message':message}
            prompt_id, hist = next(iter(matches.items()))
            update_design(did, state='running', prompt_id=prompt_id, error=None)
        if hist is None:
            hist = comfy('/history/' + urllib.parse.quote(str(prompt_id), safe='')).get(prompt_id)
        fields = {'last_checked_at':time.time(), 'poll_error':None}
        if hist:
            if hist.get('status', {}).get('status_str') == 'error':
                fields.update(state='failed', error='生成服务明确返回失败；可检查失败原因后建立修订，重新生成仍需授权', history=hist.get('status'))
            else:
                images = _history_images(did, hist)
                if images:
                    fields.update(state='generated_pending_review', images=images, history=hist.get('status'), error=None)
                elif hist.get('status', {}).get('completed') is True:
                    fields.update(state='failed', error='生成服务已结束但未返回图片，请核对上游结果与费用后再建立修订', history=hist.get('status'))
        update_design(did, **fields)
        return {'id':did, 'state':fields.get('state','running'), 'checked':True, 'message':'已核对现有任务，没有提交新的生成'}
    except Exception as exc:
        message = '暂时无法读取生成结果，请恢复生图服务后点击“核对结果”；没有重复生成。' + str(exc)[:240]
        update_design(did, last_checked_at=time.time(), poll_error=message)
        return {'id':did, 'state':d['state'], 'checked':False, 'message':message}


def resolve_uncertain_design(did, p, actor):
    """Explicit administrator evidence only; it releases a slot, not an authorization."""
    accounts().require_role(actor, ('admin',))
    note = str(p.get('note', '')).strip()
    if p.get('confirmed_not_submitted') is not True or len(note) < 10:
        raise AccessError(400, '请管理员核对上游提交与费用记录，确认未提交并填写至少10字证据说明')
    with connect() as c:
        row = c.execute('SELECT * FROM designs WHERE id=?', (did,)).fetchone()
        if not row: raise AccessError(404, '改款任务不存在')
        d = json.loads(row['data'])
        if d.get('state') != 'uncertain' or d.get('prompt_id'):
            raise AccessError(409, '仅没有上游任务ID的提交待核方案可人工处置；已有任务请先核对结果')
        resolution = {'outcome':'verified_not_submitted', 'note':note, 'reviewer':actor['display_name'], 'user_id':actor['id'], 'at':time.time()}
        d.update(state='failed', resolution=resolution, error='管理员已核对未提交；需要生图时请建立修订并重新授权', poll_error=None)
        c.execute('UPDATE designs SET data=?,updated=? WHERE id=?',(dumps(d),time.time(),did))
        event(c, row['job'], 'uncertain_generation_resolved', {'id':did, **resolution})
    return {'saved':True, 'state':'failed', 'submitted':False}


def poll_designs():
    while True:
        try:
            with connect() as c:
                rows = c.execute("SELECT id FROM designs WHERE json_extract(data,'$.state')='running'").fetchall()
            for row in rows:
                check_design(row['id'])
        except Exception as exc:
            print('Generation polling failed:', repr(exc))
        time.sleep(5)


def export_job(p):
    jid = p['job']; scope = p.get('scope', 'all')
    rows = items(jid)
    if scope == 'approved':
        rows = [x for x in rows if x['evaluation']['approved']]
    elif scope == 'eligible':
        rows = [x for x in rows if x['evaluation']['status'] == 'eligible']
    elif scope != 'all':
        raise ValueError('导出范围无效')
    for row in rows: validate_media_paths(row)
    eid = uuid.uuid4().hex[:10]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    prefix = OUTPUT / ('selection-export_' + eid)
    source = prefix.with_suffix('.json')
    with connect() as c:
        rules = json.loads(c.execute('SELECT rules FROM jobs WHERE id=?', (jid,)).fetchone()['rules'])
    with connect() as c:
        ids = {d['id'] for d in rows}
        designs = [dict(json.loads(x['data']), id=x['id'], product_id=x['product_id']) for x in c.execute('SELECT * FROM designs WHERE job=?', (jid,)) if x['product_id'] in ids]
    source.write_text(dumps({'job': jid, 'scope': scope, 'rules': rules, 'rows': rows, 'designs': designs, 'created': time.time()}))
    content = {'status': 'building', 'count': len(rows), 'scope': scope, 'path': str(prefix.with_suffix('.xlsx'))}
    with connect() as c:
        c.execute('INSERT INTO exports VALUES(?,?,?,?)', (eid, jid, dumps(content), time.time()))
    def build():
        try:
            result = subprocess.run([sys.executable, str(ROOT / 'build_export.py'), str(source), str(prefix.with_suffix('.xlsx'))], capture_output=True, text=True, timeout=360)
            if result.returncode:
                raise ValueError(result.stderr[-1500:] or result.stdout[-1500:])
            content.update(status='ready', url='/export/' + eid, output=result.stdout[-1000:])
        except Exception as e:
            content.update(status='failed', error=str(e))
        with connect() as c:
            c.execute('UPDATE exports SET data=? WHERE id=?', (dumps(content), eid))
    threading.Thread(target=build, daemon=True).start()
    return {'id': eid, **content}


from http_api import make_handler, init_http
import sys
Handler = make_handler(sys.modules[__name__])

if __name__ == '__main__':
    from runtime_config import acquire_instance_lock, configure_logging, start_backup_worker, HOST
    acquire_instance_lock()
    configure_logging()
    init(); ASSETS.mkdir(parents=True, exist_ok=True); OUTPUT.mkdir(parents=True, exist_ok=True)
    init_http(sys.modules[__name__])
    import workflow
    workflow.recover_interrupted_deliveries(sys.modules[__name__])
    with connect() as c:
        empty = not c.execute('SELECT 1 FROM jobs').fetchone()
    if empty: create_job('icon-us', legacy=False)
    start_backup_worker()
    threading.Thread(target=poll_designs, daemon=True).start()
    print(f'Fashion Studio选款工作台 http://{HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
