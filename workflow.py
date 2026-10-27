"""Approved selection library and atomic, traceable delivery packages.

HTTP callers hold MUTATION_LOCK while validating and creating a delivery. No
platform calls, paid generation, review changes or production-data migration.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

from access import AccessError

MAX_DELIVERY_ITEMS = 500
ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,100}')
_BUILD_THREADS = {}


def _snapshot(app, record):
    data, review, rules = [json.loads(record[x]) for x in ('data', 'review', 'rules')]
    return {**data, 'id': record['id'], 'job': record['job'], 'review': review,
            'revision': str(record['updated']), 'source_rules': rules,
            'source_job_name': rules.get('name') or record['job'],
            'evaluation': app.evaluate(data, rules, review)}


def _record(c, job, product_id):
    return c.execute('SELECT i.*,j.rules FROM items i JOIN jobs j ON j.id=i.job '
                     'WHERE i.job=? AND i.id=?', (job, product_id)).fetchone()


def _validated_ref(app, c, job, product_id, expected_revision):
    if not all(isinstance(x, str) and ID_PATTERN.fullmatch(x) for x in (job, product_id)):
        raise AccessError(400, '任务或商品 ID 无效')
    if expected_revision is None or not str(expected_revision):
        raise AccessError(400, '缺少款式版本，请刷新审核款库后重试')
    row = _record(c, job, product_id)
    if row is None:
        raise AccessError(404, '款式已不存在，请刷新审核款库')
    if str(row['updated']) != str(expected_revision):
        raise AccessError(409, '款式或审核结果已更新，请刷新后重新确认')
    data = _snapshot(app, row)
    if not data['evaluation']['approved']:
        raise AccessError(409, '仅当前符合筛选规则且审核确认的款式可交付或改款',
                          {'job': job, 'product_id': product_id,
                           'reasons': data['evaluation']['reasons']})
    return data


def approved_items(app):
    """Current eligible approvals across jobs, evaluated from original evidence."""
    with app.connect() as c:
        records = c.execute('SELECT i.*,j.rules FROM items i JOIN jobs j ON j.id=i.job '
                            'ORDER BY i.updated DESC').fetchall()
        rows = [_snapshot(app, r) for r in records]
    rows = [app.item_view(d) for d in rows if d['evaluation']['approved']]
    return {'items': rows, 'count': len(rows)}


def get_approved_source(app, job, product_id, expected_revision):
    """Reusable source guard for design creation and explicit delivery selection."""
    with app.connect() as c:
        data = _validated_ref(app, c, job, product_id, expected_revision)
    return app.item_view(data)


def _builder_version(app):
    return 'delivery-v2-' + hashlib.sha256((app.ROOT / 'build_export.py').read_bytes()).hexdigest()


def _delivery_sources(app, record):
    if isinstance(record.get('source_items'), list):
        sources = record['source_items']
        return sources if all(isinstance(x, dict) and x.get('job') and x.get('product_id') for x in sources) else []
    # Old packages remain immutable. Read their existing manifest for display only.
    try:
        manifest = Path(record['path']).resolve().parent / '交付清单.json'
        if Path(app.OUTPUT).resolve() not in manifest.parents or manifest.stat().st_size > 10 * 1024 * 1024:
            return []
        rows = json.loads(manifest.read_text(encoding='utf-8')).get('items', [])
        return [{'job': d['job'], 'product_id': d['id'], 'name': d.get('name', ''),
                 'source_job_name': d.get('source_job_name', ''), 'revision': d.get('revision')}
                for d in rows]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def annotate_exports(app, records):
    """Distinguish immutable historical delivery snapshots from current approvals."""
    summaries, groups = {}, {}
    with app.connect() as c:
        for record in records:
            if record.get('scope') != 'approved_delivery' or record.get('kind') != 'zip':
                continue
            sources = _delivery_sources(app, record)
            group = tuple(sorted((str(x.get('job')), str(x.get('product_id'))) for x in sources))
            current = bool(sources) and all(x.get('revision') for x in sources)
            if current:
                for source in sources:
                    try:
                        _validated_ref(app, c, source.get('job'), source.get('product_id'), source.get('revision'))
                    except AccessError:
                        current = False
                        break
            else:
                current = None
            did = record.get('delivery_id') or record.get('id')
            summary = {'source_items': sources, 'is_current': current, 'is_latest': False,
                       'source_status_reason': ('与当前审核一致' if current else
                          '原款审核或资料已变化；这是历史交付快照' if current is False else
                          '旧交付未记录完整版本，当前审核状态待核')}
            summaries[did] = summary
            if group:
                groups.setdefault(group, []).append((record, summary))
        for group in groups.values():
            ordered = sorted(group, key=lambda pair: pair[0].get('created', 0))
            ready = [pair for pair in ordered if pair[0].get('status') == 'ready']
            for index, (_, summary) in enumerate(ordered, 1):
                summary['delivery_version'] = index
            if ready:
                ready[-1][1]['is_latest'] = True
    return [{**record, **summaries.get(record.get('delivery_id') or record.get('id'), {})}
            for record in records]


def _ready_files_exist(app, record):
    target = Path(app.OUTPUT).resolve()
    try:
        with app.connect() as c:
            for file in record.get('files', []):
                eid = file['url'].rsplit('/', 1)[-1]
                row = c.execute('SELECT data FROM exports WHERE id=?', (eid,)).fetchone()
                if not row:
                    return False
                item = json.loads(row['data'])
                path = Path(item['path']).resolve()
                if item.get('status') != 'ready' or target not in path.parents or not zipfile.is_zipfile(path):
                    return False
        return len(record.get('files', [])) == 2
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _image_source(app, value):
    """Resolve before allowlisting: symlinks cannot import private outside data."""
    from PIL import Image
    if not isinstance(value, str) or not value:
        raise AccessError(409, '原图缺失，请补齐后重新审核')
    try:
        app.validate_media_paths({'image': value})
        source = Path(value).expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise ValueError('图片类型无效')
        if not any(Path(root).resolve() in source.parents for root in (app.PROJECT, app.DATA, app.OUTPUT)):
            raise ValueError('图片路径不属于工作台')
        with Image.open(source) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('图片内容无效')
            image.verify()
        return source
    except (OSError, ValueError, TypeError) as exc:
        raise AccessError(409, '图片缺失或不属于可交付的工作台图片，请补图后重新确认') from exc


def _media_plan(app, rows):
    plan = []
    for data in rows:
        prefix = f"images/{data['job']}/{data['id']}"
        entries = [('front', None, data.get('front')), ('back', None, data.get('back'))]
        entries += [('color', i, c.get('image')) for i, c in enumerate(data.get('colors', []))]
        for role, index, value in entries:
            src = _image_source(app, value)
            name = role if index is None else f'color-{index + 1:03d}'
            relative = f'{prefix}/{name}{src.suffix.lower()}'
            plan.append((data, role, index, src, relative))
    return plan


def _build_workbook(app, payload_path, output_path):
    result = subprocess.run([sys.executable, str(app.ROOT / 'build_export.py'),
                             str(payload_path), str(output_path)],
                            capture_output=True, text=True, timeout=1200)
    if result.returncode or not output_path.is_file():
        raise RuntimeError('图文表格生成失败，请查看后台日志后重试')
    # Export success alone is insufficient; verify that every original was embedded.
    payload = json.loads(payload_path.read_text(encoding='utf-8'))
    expected = sum(2 + len(x['colors']) for x in payload['rows'])
    with zipfile.ZipFile(output_path) as workbook:
        if workbook.testzip():
            raise RuntimeError('图文表格文件校验失败')
        drawings = [n for n in workbook.namelist() if re.fullmatch(r'xl/drawings/drawing\d+\.xml', n)]
        import xml.etree.ElementTree as ET
        count = sum(len(ET.fromstring(workbook.read(n)).findall('.//{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}pic')) for n in drawings)
        if count != expected:
            raise RuntimeError('图文表格图片数量不完整，未创建交付包')


def create_delivery(app, p):
    """Validate all selected approvals, then publish XLSX+ZIP as one transaction.

    Call with {items:[{job, product_id, expected_revision}]}. A duplicate item is
    rejected rather than silently counted twice. Same product across separate
    jobs retains its distinct source/rule/review provenance.
    """
    refs = p.get('items') if isinstance(p, dict) else None
    if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_DELIVERY_ITEMS:
        raise AccessError(400, f'请选中 1 至 {MAX_DELIVERY_ITEMS} 款已审核款式')
    rows, seen = [], set()
    with app.connect() as c:
        for ref in refs:
            if not isinstance(ref, dict):
                raise AccessError(400, '款式引用格式无效')
            data = _validated_ref(app, c, ref.get('job'), ref.get('product_id'), ref.get('expected_revision'))
            key = (data['job'], data['id'])
            if key in seen:
                raise AccessError(400, '交付清单存在重复款式，请刷新后重新选择')
            seen.add(key); rows.append(data)
    fingerprint = hashlib.sha256(json.dumps(sorted((d['job'], d['id'], d['revision']) for d in rows)).encode()).hexdigest()
    builder_version = _builder_version(app)
    with app.connect() as c:
        for record in c.execute("SELECT id,data FROM exports WHERE json_extract(data,'$.scope')='approved_delivery' AND json_extract(data,'$.kind')='zip' ORDER BY created DESC"):
            previous = json.loads(record['data'])
            if previous.get('fingerprint') == fingerprint and previous.get('builder_version') == builder_version:
                if previous.get('status') == 'building' or (previous.get('status') == 'ready' and _ready_files_exist(app, previous)):
                    return {'id': record['id'], 'reused': True,
                            **{k: previous[k] for k in ('count', 'files', 'created', 'status')}}
    plan = _media_plan(app, rows)
    delivery_id, workbook_id, created = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12], time.time()
    target = Path(app.OUTPUT).resolve()
    target.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.delivery-', dir=target))
    final = target / ('Fashion Studio_审核款交付_' + delivery_id)
    workbook_name = '审核款图文交付.xlsx'
    zip_name = '审核款交付包.zip'
    try:
        snapshots = {(d['job'], d['id']): copy.deepcopy(d) for d in rows}
        asset_manifest = []
        for data, role, index, source, relative in plan:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW also prevents a final-component symlink replacement.
            fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as incoming, destination.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            snapshot = snapshots[(data['job'], data['id'])]
            if role == 'color':
                snapshot['colors'][index]['image'] = relative
            else:
                snapshot[role] = relative
            asset_manifest.append({'job': data['job'], 'product_id': data['id'],
                                   'role': role, 'color_index': index + 1 if index is not None else None,
                                   'path': relative, 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
        # Only required business facts are packaged, never arbitrary import fields.
        fields = ('id', 'job', 'name', 'sku', 'url', 'sales', 'sales_period', 'breadcrumb', 'category',
                  'category_leaf', 'front', 'back', 'color_count', 'colors', 'first_listed_at',
                  'date_source', 'date_verified', 'review', 'revision', 'evaluation', 'source_rules',
                  'source_job_name')
        clean_rows = [{k: d[k] for k in fields if k in d} for d in snapshots.values()]
        # Provenance labels are source strings; strip optional local/gallery paths.
        for d in clean_rows:
            d['colors'] = [{k: color[k] for k in ('name', 'image', 'source_url', 'src') if k in color}
                           for color in d['colors']]
        manifest = {'format_version': 1, 'delivery_id': delivery_id, 'created': created,
                    'scope': 'approved_delivery', 'count': len(rows), 'items': clean_rows,
                    'assets': asset_manifest,
                    'note': '审核与规则为打包时快照；SKU月份仅为估计款龄，非真实首次上架日期。销量保留原标签与平台周期，不合计不同颜色或不同来源任务。'}
        (stage / '交付清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (stage / '使用说明.txt').write_text(
            f'Fashion Studio审核款交付包\n已审核款式：{len(rows)}\n\n'
            '审核款图文交付.xlsx：正背面、复色原图和来源、销量、款龄依据与审核记录。\n'
            'images：按任务 ID / 商品 ID / 正面、背面、颜色选项保存原图。\n'
            '交付清单.json：来源规则、审核版本、图片相对路径与 SHA256 校验值。\n\n'
            + manifest['note'] + '\n', encoding='utf-8')
        build_rows = copy.deepcopy(clean_rows)
        for d in build_rows:
            d['front'], d['back'] = str(stage / d['front']), str(stage / d['back'])
            for color in d['colors']:
                color['image'] = str(stage / color['image'])
        payload = {'scope': 'approved_delivery', 'created': created, 'rows': build_rows,
                   'rules': rows[0]['source_rules'], 'strict_images': True, 'designs': []}
        payload_path = stage / '.build-input.json'
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        files = [{'kind': 'xlsx', 'name': workbook_name, 'url': '/export/' + workbook_id},
                 {'kind': 'zip', 'name': zip_name, 'url': '/export/' + delivery_id}]
        common = {'status': 'building', 'count': len(rows), 'scope': 'approved_delivery',
                  'delivery_id': delivery_id, 'source_jobs': list(dict.fromkeys(d['job'] for d in rows)),
                  'created': created, 'fingerprint': fingerprint, 'builder_version': builder_version,
                  'source_items': [{'job': d['job'], 'product_id': d['id'], 'name': d.get('name', ''),
                                    'source_job_name': d['source_job_name'], 'revision': d['revision']}
                                   for d in rows], 'staging_path': str(stage)}
        with app.connect() as c:
            for eid, kind, name in ((workbook_id, 'xlsx', workbook_name), (delivery_id, 'zip', zip_name)):
                content = {**common, 'kind': kind, 'name': name, 'path': str(final / name), 'url': '/export/' + eid}
                if kind == 'zip':
                    content['files'] = files
                c.execute('INSERT INTO exports VALUES(?,?,?,?)',
                          (eid, rows[0]['job'], json.dumps(content, ensure_ascii=False), created))
            app.event(c, rows[0]['job'], 'approved_delivery_requested',
                      {'delivery_id': delivery_id, 'count': len(rows),
                       'items': [{'job': d['job'], 'product_id': d['id'], 'revision': d['revision']} for d in rows]})
        worker = threading.Thread(target=_complete_delivery,
                                  args=(app, copy.deepcopy(refs), stage, final, delivery_id, workbook_id),
                                  daemon=True, name='delivery-' + delivery_id)
        _BUILD_THREADS[delivery_id] = worker
        worker.start()
        return {'id': delivery_id, 'count': len(rows), 'files': files, 'created': created, 'status': 'building'}
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        with app.connect() as c:
            c.execute('DELETE FROM exports WHERE id IN (?,?)', (delivery_id, workbook_id))
        raise


def _complete_delivery(app, refs, stage, final, delivery_id, workbook_id):
    """Expensive authoring runs outside HTTP/global mutation locks."""
    published = False
    try:
        payload_path = stage / '.build-input.json'
        _build_workbook(app, payload_path, stage / '审核款图文交付.xlsx')
        payload_path.unlink()
        zip_path = stage / '审核款交付包.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as package:
            for source in sorted(stage.rglob('*')):
                if source.is_file() and source != zip_path:
                    package.write(source, source.relative_to(stage).as_posix())
        with zipfile.ZipFile(zip_path) as package:
            if package.testzip():
                raise RuntimeError('交付包文件校验失败')
        with app.MUTATION_LOCK:
            with app.connect() as c:
                for ref in refs:
                    _validated_ref(app, c, ref['job'], ref['product_id'], ref['expected_revision'])
                stage.rename(final); published = True
                for eid in (delivery_id, workbook_id):
                    record = c.execute('SELECT data FROM exports WHERE id=?', (eid,)).fetchone()
                    content = json.loads(record['data']); content['status'] = 'ready'
                    content.pop('staging_path', None)
                    c.execute('UPDATE exports SET data=? WHERE id=?', (json.dumps(content, ensure_ascii=False), eid))
                app.event(c, refs[0]['job'], 'approved_delivery_ready', {'delivery_id': delivery_id, 'count': len(refs)})
    except Exception as exc:
        shutil.rmtree(final if published else stage, ignore_errors=True)
        with app.MUTATION_LOCK:
            with app.connect() as c:
                for eid in (delivery_id, workbook_id):
                    row = c.execute('SELECT data FROM exports WHERE id=?', (eid,)).fetchone()
                    if row:
                        content = json.loads(row['data'])
                        content.update(status='failed', error=str(exc)[:500])
                        content.pop('staging_path', None)
                        c.execute('UPDATE exports SET data=? WHERE id=?', (json.dumps(content, ensure_ascii=False), eid))
                app.event(c, refs[0]['job'], 'approved_delivery_failed', {'delivery_id': delivery_id, 'error': str(exc)[:500]})
    finally:
        _BUILD_THREADS.pop(delivery_id, None)


def wait_delivery(delivery_id, timeout=120):
    """Bounded fixture-test helper; normal clients poll persisted export state."""
    worker = _BUILD_THREADS.get(delivery_id)
    if worker:
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError('交付构建仍未结束')


def recover_interrupted_deliveries(app):
    """Call once at process startup, before accepting requests; never re-submit."""
    target = Path(app.OUTPUT).resolve()
    with app.connect() as c:
        records = c.execute("SELECT id,data FROM exports WHERE json_extract(data,'$.scope')='approved_delivery' AND json_extract(data,'$.status')='building'").fetchall()
        for record in records:
            content = json.loads(record['data'])
            if content.get('delivery_id') in _BUILD_THREADS:
                continue
            stage = Path(content.get('staging_path') or '/').resolve()
            # Only this builder's direct, generated staging folders can be removed.
            if stage.parent == target and stage.name.startswith('.delivery-'):
                shutil.rmtree(stage, ignore_errors=True)
            delivery_id = str(content.get('delivery_id', ''))
            if re.fullmatch(r'[a-f0-9]{12}', delivery_id):
                final = target / ('Fashion Studio_审核款交付_' + delivery_id)
                if final.is_dir() and not final.is_symlink():
                    shutil.rmtree(final, ignore_errors=True)
            content.pop('staging_path', None)
            content.update(status='failed', error='后台重启中断了交付打包，请刷新审核款库后重新生成')
            c.execute('UPDATE exports SET data=? WHERE id=?', (json.dumps(content, ensure_ascii=False), record['id']))
