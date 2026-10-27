"""Authenticated local/LAN HTTP surface. Browser sessions never expose the database."""
import base64
import csv
import hashlib
import io
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from access import AccessError
import runtime_config as runtime



TAILSCALE_CLI = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
TAILSCALE_RANGES = tuple(map(ipaddress.ip_network, ('100.64.0.0/10', 'fd7a:115c:a1e0::/48')))
_tailnet_lock = threading.Lock()
_tailnet_deadline = 0.0
_tailnet_cache = {'self': frozenset(), 'peers': frozenset()}


def tailscale_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return any(address in network for network in TAILSCALE_RANGES)
    except (ValueError, TypeError):
        return False


def tailscale_network():
    """Use the local daemon's network map, not all CGNAT IPs or proxy headers."""
    global _tailnet_cache, _tailnet_deadline
    with _tailnet_lock:
        if time.monotonic() < _tailnet_deadline:
            return _tailnet_cache
        result = {'self': frozenset(), 'peers': frozenset()}
        try:
            response = subprocess.run([TAILSCALE_CLI, 'status', '--json'],
                                      capture_output=True, text=True, timeout=2, check=True,
                                      env={**os.environ, 'TAILSCALE_BE_CLI': '1'})
            state = json.loads(response.stdout)
            if isinstance(state, dict) and state.get('BackendState') != 'Running':
                logging.getLogger(__name__).warning('Tailscale discovery backend is not running: %s', state.get('BackendState'))
            if state.get('BackendState') == 'Running':
                own = (state.get('Self') or {}).get('TailscaleIPs', [])
                peers = [ip for peer in (state.get('Peer') or {}).values()
                         for ip in peer.get('TailscaleIPs', [])]
                result = {'self': frozenset(ip for ip in own if tailscale_ip(ip)),
                          'peers': frozenset(ip for ip in peers if tailscale_ip(ip))}
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
            logging.getLogger(__name__).warning('Tailscale discovery unavailable: %s exit=%s', type(exc).__name__, getattr(exc, 'returncode', None))
        _tailnet_cache = result
        _tailnet_deadline = time.monotonic() + 5
        return result


def lan_addresses():
    found = set()
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for interface in ('en0', 'en1', 'en2', 'en3', 'en4', 'en5'):
        try:
            result = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface], capture_output=True, text=True, timeout=2)
            if result.returncode == 0: found.add(result.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sorted(x for x in found if private_peer(x) and not ipaddress.ip_address(x).is_loopback)


def private_peer(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.is_loopback or any(ip in net for net in (
            ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'), ipaddress.ip_network('169.254.0.0/16')))
    except (ValueError, TypeError):
        return False


def init_http(app):
    app.accounts().migrate()
    with app.connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS collector_tokens(
            token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL, job TEXT NOT NULL,
            expires REAL NOT NULL, client_id TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS collector_claims(
            task_id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL, nonce TEXT NOT NULL, created REAL NOT NULL);''')
    # Cache allowed names from this machine, never trust forwarded client headers.
    app.allowed_hosts = {'localhost', '127.0.0.1', socket.gethostname().lower(), *lan_addresses(), *tailscale_network()['self']}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'FashionStudioSelection/0.2'
        session_cookie = None

        def log_message(self, fmt, *args):
            print(time.strftime('%H:%M:%S'), fmt % args, flush=True)

        def check_transport(self, mutation=False):
            peer = self.client_address[0]
            tailnet = tailscale_network() if tailscale_ip(peer) else None
            tailnet_peer = tailnet is not None and (peer in tailnet['peers'] or peer in tailnet['self'])
            if not private_peer(peer) and not tailnet_peer:
                raise AccessError(403, '工作台仅允许本机、内网或已连接的 Tailscale 设备访问')
            parsed = urllib.parse.urlsplit('http://' + self.headers.get('Host', ''))
            if parsed.hostname not in app.allowed_hosts:
                app.allowed_hosts.update(lan_addresses())
                app.allowed_hosts.update(tailscale_network()['self'])
            if parsed.hostname not in app.allowed_hosts or parsed.port != app.PORT:
                raise AccessError(403, '访问地址不属于当前工作台')
            origin = self.headers.get('Origin', '')
            extension_origin = origin.startswith('chrome-extension://')
            # Chrome service-worker GET requests can omit Origin. The pairing
            # headers select collector authentication; they do not grant access.
            # Even empty/invalid credentials must never fall back to a Cookie.
            collector_headers = any(name in self.headers for name in
                                    ('X-Selection-Key', 'X-Collector-Client'))
            self.extension = extension_origin or collector_headers
            if collector_headers and ((origin and not extension_origin) or
                    (not origin and self.headers.get('Sec-Fetch-Site') == 'cross-site')):
                raise AccessError(403, '采集配对请求来源不匹配')
            if mutation and not self.extension:
                if origin and origin != 'http://' + self.headers.get('Host'):
                    raise AccessError(403, '请求来源不匹配')
                if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    raise AccessError(403, '跨站请求已拒绝')

        def send(self, data, status=200, mime='application/json; charset=utf-8', name=None):
            raw = app.dumps(data).encode() if not isinstance(data, bytes) else data
            self.send_response(status)
            for k, v in [('Content-Type', mime), ('Content-Length', str(len(raw))), ('Cache-Control', 'no-store'),
                         ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'same-origin'),
                         ('X-Frame-Options', 'SAMEORIGIN')]: self.send_header(k, v)
            if self.session_cookie is not None: self.send_header('Set-Cookie', self.session_cookie)
            origin = self.headers.get('Origin', '')
            if origin.startswith('chrome-extension://'):
                self.send_header('Access-Control-Allow-Origin', origin)
            if name:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass

        def cookie(self, token='', max_age=0):
            self.session_cookie = f'fashionstudio_selection_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(max_age)}'

        def browser_actor(self, optional=False):
            cookies = SimpleCookie()
            try: cookies.load(self.headers.get('Cookie', ''))
            except Exception: pass
            token = cookies.get('fashionstudio_selection_session')
            try:
                actor = app.accounts().authenticate(token.value if token else '')
                actor['client_id'] = self.headers.get('X-Selection-Client', '')
                return actor
            except AccessError:
                if optional: return None
                raise

        def extension_actor(self, job=None):
            raw = self.headers.get('X-Selection-Key', '')
            digest = hashlib.sha256(raw.encode()).hexdigest()
            client = self.headers.get('X-Collector-Client', '')
            if not raw or not 12 <= len(client) <= 100:
                raise AccessError(401, '请使用新版助手并重新取得本次任务配对码')
            with app.connect() as c:
                row = c.execute('SELECT * FROM collector_tokens WHERE token_hash=?', (digest,)).fetchone()
                if not row or row['expires'] < time.time(): raise AccessError(401, '配对已失效，请登录工作台重新配对')
                if job and row['job'] != job: raise AccessError(403, '此配对码只适用于指定任务')
                if row['client_id'] and row['client_id'] != client: raise AccessError(409, '配对码已由另一采集端使用，请勿并行采集')
                if not row['client_id']: c.execute('UPDATE collector_tokens SET client_id=? WHERE token_hash=?', (client, digest))
            actor = app.accounts().principal_for_session(row['session_id'])
            app.accounts().require_role(actor)
            self.pair = dict(row); self.pair['token_hash'] = digest
            return actor

        def csrf(self, actor):
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), actor['csrf_token']):
                raise AccessError(403, '页面登录状态已变化，请刷新后重试')

        def body(self):
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 30 * 1024 * 1024: raise AccessError(413, '请求大小不支持')
            if self.headers.get_content_type() != 'application/json': raise AccessError(415, '需要JSON请求')
            p = json.loads(self.rfile.read(size))
            if not isinstance(p, dict): raise AccessError(400, '请求格式不正确')
            return p

        def write_guard(self, actor, resource, expected):
            app.accounts().assert_lease(actor, resource)
            kind, *parts = resource.split(':')
            with app.connect() as c:
                if kind == 'item': row = c.execute('SELECT updated FROM items WHERE job=? AND id=?', parts).fetchone()
                elif kind == 'design': row = c.execute('SELECT updated FROM designs WHERE id=?', parts).fetchone()
                elif kind == 'profile':
                    row = c.execute('SELECT data FROM profiles WHERE id=?', parts).fetchone()
                    actual = str(json.loads(row['data']).get('version', 1)) if row else None
                else: raise AccessError(400, '锁对象不正确')
                if kind != 'profile': actual = str(row['updated']) if row else None
            if actual is None: raise AccessError(404, '记录不存在')
            if expected is None or str(expected) != actual:
                raise AccessError(409, '记录已被更新，请刷新后重新审核；本次没有覆盖其他人的内容')

        def collector_guard(self, actor, job):
            app.accounts().assert_lease(actor, 'collector')
            app.accounts().renew(actor, 'collector', ttl=180)
            with app.connect() as c:
                j = c.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()
            if not j or j['status'] != 'running': raise AccessError(409, '采集已停止，请在工作台明确恢复后再操作')

        def do_OPTIONS(self):
            try:
                self.check_transport()
                if not self.headers.get('Origin', '').startswith('chrome-extension://'):
                    raise AccessError(403, '请求来源不匹配')
                self.send_response(204)
                self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
                self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type,X-Selection-Key,X-Collector-Client')
                self.end_headers()
            except AccessError as e: self.send({'error': e.message}, e.status)

        def do_GET(self):
            try:
                self.check_transport()
                u = urllib.parse.urlsplit(self.path); path = u.path; q = urllib.parse.parse_qs(u.query)
                if path == '/api/health':
                    with app.connect() as c: c.execute('SELECT count(*) FROM jobs').fetchone()
                    return self.send({'app': 'selection-workbench', 'ready': True, 'version': '0.2.0'})
                public = {'/': 'index.html', '/app.js': 'app.js', '/auth.js': 'auth.js', '/style.css': 'style.css', '/favicon.svg': 'favicon.svg'}
                if path in public:
                    f = app.ROOT / 'public' / public[path]
                    return self.send(f.read_bytes(), mime=mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
                if path == '/api/auth/me' and not self.extension:
                    return self.send({'needs_setup': app.accounts().needs_setup(), 'user': self.browser_actor(optional=True),
                                      'local_setup_allowed': ipaddress.ip_address(self.client_address[0]).is_loopback})
                if self.extension:
                    with app.MUTATION_LOCK:
                        actor = self.extension_actor(q.get('job', [None])[0])
                        if path == '/api/collector/task':
                            with app.connect() as c: job_row=c.execute('SELECT status FROM jobs WHERE id=?',(self.pair['job'],)).fetchone()
                            return self.send({'job':self.pair['job'], 'status':job_row['status'] if job_row else 'missing'})
                        if path != '/api/next': raise AccessError(403, '采集助手不允许访问此接口')
                        self.collector_guard(actor, self.pair['job'])
                        result = app.next_task(self.pair['job'])
                        if result.get('task'):
                            nonce = secrets.token_urlsafe(24); tid = result['task']['id']
                            with app.connect() as c: c.execute('INSERT OR REPLACE INTO collector_claims VALUES(?,?,?,?)', (tid, self.pair['token_hash'], nonce, time.time()))
                            result['task']['claim_token'] = nonce
                        elif result.get('status') == 'awaiting_review': app.accounts().release(actor, 'collector')
                        return self.send(result)
                actor = self.browser_actor()
                app.accounts().require_role(actor, ('admin', 'editor', 'viewer'))
                if path == '/api/state': return self.send(app.state())
                if path == '/api/approved':
                    import workflow
                    with app.MUTATION_LOCK:
                        return self.send(workflow.approved_items(app))
                if path == '/api/style': return self.send(json.loads(app.STYLE_PATH.read_text()))
                if path == '/api/items': return self.send([app.item_view(x) for x in app.items(q['job'][0])])
                if path == '/api/locks': return self.send({'leases': app.accounts().list_leases(actor)})
                if path == '/api/generation':
                    app.accounts().require_role(actor)
                    try: return self.send(app.generation_info())
                    except Exception as e: return self.send({'connected': False, 'error': str(e)})
                if path == '/api/events':
                    with app.connect() as c: rows = [dict(r) for r in c.execute('SELECT * FROM events WHERE job=? ORDER BY id DESC LIMIT 50', (q['job'][0],))]
                    return self.send(rows)
                if path == '/api/admin/users': return self.send({'users': app.accounts().list_users(actor)})
                if path == '/api/admin/audit': return self.send({'events': app.accounts().audit_log(actor)})
                if path == '/api/admin/status':
                    app.accounts().require_role(actor, ('admin',))
                    with app.connect() as c:
                        integrity = c.execute('PRAGMA quick_check').fetchone()[0]
                        counts = {name: c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('jobs', 'items')}
                    backups = [{'name': f.name, 'size': f.stat().st_size, 'modified': f.stat().st_mtime} for f in sorted(runtime.BACKUP_DIR.glob('*.sqlite3'), reverse=True)]
                    return self.send({'service': {'host': runtime.HOST, 'port': app.PORT, 'health': 'ok',
                        'lan_urls': ['http://'+x+':'+str(app.PORT) for x in lan_addresses()],
                        'autostart': (Path.home()/'Library/LaunchAgents/com.fashionstudio.selectionworkbench.plist').is_file()},
                        'database': {'path': str(app.DATA/'selection.sqlite3'), 'integrity': integrity, **counts}, 'backups': backups,
                        'leases': app.accounts().list_leases(actor)})
                if path.startswith('/asset/'):
                    f = app.ASSET_MAP.get(path.split('/')[-1])
                    if f and f.is_file(): return self.send(f.read_bytes(), mime=mimetypes.guess_type(f.name)[0] or 'image/png')
                    raise AccessError(404, '图片不存在，请刷新或补图')
                if path.startswith('/export/'):
                    with app.connect() as c: row = c.execute('SELECT data FROM exports WHERE id=?', (path.split('/')[-1],)).fetchone()
                    if row:
                        d = json.loads(row['data']); f = Path(d['path']).resolve()
                        mime = {'.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.zip': 'application/zip'}.get(f.suffix.lower())
                        if d['status'] == 'ready' and mime and app.OUTPUT.resolve() in f.parents and f.is_file():
                            return self.send(f.read_bytes(), mime=mime, name=f.name)
                    raise AccessError(404, '导出尚未完成')
                if path.startswith('/reference/'):
                    root = app.STYLE_PATH.parent.resolve(); f = (root/urllib.parse.unquote(path[len('/reference/'):])).resolve()
                    if root not in f.parents or not f.is_file(): raise AccessError(404, '参考文件不存在')
                    return self.send(f.read_bytes(), mime=mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
                raise AccessError(404, '入口不存在')
            except AccessError as e: self.send({'error': e.message, **e.details}, e.status)
            except (ValueError, KeyError, TypeError) as e: self.send({'error': str(e)}, 400)
            except Exception as e:
                print('GET error', repr(e)); self.send({'error': '服务暂时无法读取数据，请查看服务日志'}, 500)

        def do_POST(self):
            try:
                self.check_transport(mutation=True)
                p = self.body(); path = urllib.parse.urlsplit(self.path).path
                with app.MUTATION_LOCK:
                    if path in ('/api/auth/setup', '/api/auth/login') and not self.extension:
                        key = self.client_address[0]; now = time.time()
                        attempts = app.LOGIN_ATTEMPTS[key] = [x for x in app.LOGIN_ATTEMPTS[key] if now-x < 60]
                        if len(attempts) >= 20: raise AccessError(429, '登录尝试过于频繁，请稍后重试')
                        attempts.append(now)
                        if path.endswith('/setup'):
                            if not ipaddress.ip_address(key).is_loopback: raise AccessError(403, '首次管理员只能在主机上创建')
                            app.accounts().bootstrap_admin(p['username'], p['password'], p.get('display_name', ''))
                        result = app.accounts().login(p['username'], p['password'], replace_session=p.get('replace_session') is True)
                        self.cookie(result['token'], result['expires_at']-time.time())
                        return self.send({'user': result['principal']})
                    actor = self.extension_actor(p.get('job')) if self.extension else self.browser_actor()
                    if not self.extension: self.csrf(actor)
                    if path == '/api/auth/logout' and not self.extension:
                        app.accounts().logout(actor); self.cookie(); return self.send({'ok': True})
                    if path == '/api/auth/password' and not self.extension:
                        result = app.accounts().change_password(actor, p['current_password'], p['new_password'])
                        return self.send({'ok': True, 'user': app.accounts().principal_for_session(actor['session_id'])})
                    app.accounts().require_role(actor)
                    if self.extension and path not in ('/api/job/action', '/api/capture', '/api/image', '/api/collector/heartbeat'):
                        raise AccessError(403, '采集助手不允许此操作')
                    result = self.dispatch(path, p, actor)
                    # No raw image, password or pairing secret enters the audit trail.
                    if not path.startswith('/api/admin/'):
                        app.accounts().audit(actor, path, {'job': p.get('job'), 'product_id': p.get('product_id'), 'action': p.get('action')})
                    return self.send(result)
            except AccessError as e: self.send({'error': e.message, **e.details}, e.status)
            except (ValueError, KeyError, TypeError) as e: self.send({'error': str(e)}, 400)
            except Exception as e:
                print('POST error', repr(e)); self.send({'error': '操作未能完成，请刷新核对状态后再试'}, 500)

        def dispatch(self, path, p, actor):
            store = app.accounts()
            if path == '/api/admin/users': return store.create_user(actor, p['username'], p.get('display_name',''), p.get('role','editor'))
            if path == '/api/admin/user': return {'user': store.update_user(actor, p['id'], role=p.get('role'), enabled=p.get('enabled'), display_name=p.get('display_name'))}
            if path == '/api/admin/reset-password': return store.reset_password(actor, p['id'])
            if path == '/api/admin/backup':
                store.require_role(actor, ('admin',)); saved=runtime.backup_database(force=True); return {'saved': bool(saved), 'path': str(saved) if saved else None}
            if path == '/api/lock':
                resource = p['resource']
                if resource != 'collector' and not resource.startswith(('item:','profile:','design:')): raise AccessError(400,'锁对象不正确')
                if p['action'] == 'acquire': return store.acquire(actor, resource, ttl=p.get('ttl',180), force=p.get('force') is True)
                if p['action'] == 'renew': return store.renew(actor, resource, ttl=p.get('ttl',180))
                if p['action'] == 'release': return store.release(actor, resource, force=p.get('force') is True)
                raise AccessError(400, '锁操作无效')
            if path == '/api/pairing':
                with app.connect() as c:
                    if not c.execute('SELECT 1 FROM jobs WHERE id=?',(p['job'],)).fetchone(): raise AccessError(404,'任务不存在')
                    raw = secrets.token_urlsafe(32)
                    c.execute('DELETE FROM collector_tokens WHERE session_id=?', (actor['session_id'],))
                    c.execute('INSERT INTO collector_tokens VALUES(?,?,?,?,NULL,?)', (hashlib.sha256(raw.encode()).hexdigest(), actor['session_id'], p['job'], min(actor['expires_at'],time.time()+43200),time.time()))
                return {'key':raw,'job':p['job'],'base_url':'http://'+self.headers['Host'], 'extension_folder':str(app.ROOT/'chrome-helper')}
            if path == '/api/collector/heartbeat':
                self.collector_guard(actor,p['job']); return {'ok':True}
            if path == '/api/job/action':
                jid = p['job']; action = p['action']
                if action == 'resume':
                    store.assert_available('collector', actor)
                    with app.connect() as c: others=c.execute("SELECT id FROM jobs WHERE status='running' AND id<>?",(jid,)).fetchall()
                    active = next((x for x in store.list_leases(actor) if x['resource']=='collector'),None)
                    if others and active: raise AccessError(409,'已有采集任务在运行，请先停止该任务')
                    for old in others: app.job_action(old['id'],'pause')
                    store.acquire(actor,'collector',ttl=180)
                    try:return app.job_action(jid,action,recovery_confirmed=p.get('recovery_confirmed') is True,retry_missing=p.get('retry_missing') is True)
                    except Exception:store.release(actor,'collector');raise
                store.assert_available('collector', actor) if actor['role']!='admin' else None
                result=app.job_action(jid,action)
                if actor['role']=='admin':store.release(actor,'collector',force=True)
                else:store.release(actor,'collector')
                return result
            if path in ('/api/capture','/api/image') and self.extension:
                self.collector_guard(actor,p['job'])
                if path=='/api/capture':
                    tid=p.get('task_id')
                    if tid:
                        with app.connect() as c:r=c.execute('SELECT * FROM collector_claims WHERE task_id=?',(tid,)).fetchone()
                        if not r or r['token_hash']!=self.pair['token_hash'] or not secrets.compare_digest(r['nonce'],p.get('claim_token','')):raise AccessError(409,'采集执行端或断点已变化，旧回调未写入')
                    cap=p['capture']; products=[cap['product']] if cap.get('product') else cap.get('cards',[])
                    for d in products:store.assert_available('item:'+p['job']+':'+str(d['id']))
                    return app.save_capture(p)
                tid=p.get('task_id')
                with app.connect() as c:
                    claim=c.execute('SELECT c.*,q.product_id FROM collector_claims c JOIN queue q ON q.id=c.task_id WHERE c.task_id=?',(tid,)).fetchone()
                if not claim or claim['token_hash']!=self.pair['token_hash'] or not secrets.compare_digest(claim['nonce'],p.get('claim_token','')) or claim['product_id'] not in ('',p['product_id']):raise AccessError(409,'图片回调已过期或商品不匹配，未覆盖现有图片')
                store.assert_available('item:'+p['job']+':'+p['product_id'])
                return app.save_image(p)
            if path in ('/api/capture','/api/collector/heartbeat'):raise AccessError(403,'请使用已配对的采集助手')
            if path in ('/api/review','/api/image','/api/image/assign'):
                resource='item:'+p['job']+':'+p['product_id'];self.write_guard(actor,resource,p.get('expected_revision'))
                if path=='/api/review':
                    p['review']['reviewer']=actor['display_name'];p['review']['user_id']=actor['id'];result=app.review_item(p)
                elif path=='/api/image':result=app.save_image(p)
                else:
                    if p.get('role') not in ('front','back'):raise AccessError(400,'请选择正面或背面')
                    with app.connect() as c:
                        row=c.execute('SELECT data FROM items WHERE job=? AND id=?',(p['job'],p['product_id'])).fetchone()
                        d=json.loads(row['data']);index=int(p['index'])
                        if index < 0 or index >= len(d.get('local_gallery',[])):raise AccessError(400,'图库序号已变化，请刷新后选择')
                        im=d['local_gallery'][index]
                        d[p['role']]=im['path'];d.setdefault('image_sources',{})[p['role']]=im.get('source_url') or '员工确认图库视角';app.upsert_item(c,p['job'],d)
                    result={'saved':True}
                with app.connect() as c:result['revision']=str(c.execute('SELECT updated FROM items WHERE job=? AND id=?',(p['job'],p['product_id'])).fetchone()['updated'])
                return result
            if path=='/api/profile':
                if p.get('id'):self.write_guard(actor,'profile:'+p['id'],p.get('expected_revision'))
                p.pop('expected_revision',None);p.pop('_revision',None);p=app.validate_profile(p);p['id']=p.get('id') or uuid.uuid4().hex[:10]
                with app.connect() as c:
                    old=c.execute('SELECT data FROM profiles WHERE id=?',(p['id'],)).fetchone();p['version']=json.loads(old['data']).get('version',1)+1 if old else 1
                    c.execute('INSERT INTO profiles VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(p['id'],app.dumps(p)))
                return p
            if path in ('/api/job','/api/job/refilter'):
                if 'expected_profile_revision' in p:
                    with app.connect() as c:
                        profile=c.execute('SELECT data FROM profiles WHERE id=?',(p['profile'],)).fetchone()
                    if not profile:raise AccessError(404,'规则模板不存在，请刷新')
                    if str(json.loads(profile['data']).get('version',1))!=str(p['expected_profile_revision']):
                        raise AccessError(409,'规则模板已被其他成员更新，请刷新核对后重新创建任务')
                if path=='/api/job':return {'id':app.create_job(p['profile'],p.get('legacy',False))}
                return {'id':app.refilter_job(p)}
            if path in ('/api/import','/api/dates'):
                records=p.get('rows') if path=='/api/import' else list(csv.DictReader(io.StringIO(p['csv'])))
                if not isinstance(records,list):raise AccessError(400,'需要记录数组')
                with app.connect() as c:
                    job=c.execute('SELECT status FROM jobs WHERE id=?',(p['job'],)).fetchone()
                    if not job:raise AccessError(404,'任务不存在')
                    if job['status']=='running':raise AccessError(409,'请先停止采集再导入，避免同时更新')
                for row in records:
                    pid=row.get('id') if path=='/api/import' else row.get('product_id') or row.get('商品ID')
                    store.assert_available('item:'+p['job']+':'+str(pid))
                with app.connect() as c:
                    for row in records:
                        if path=='/api/import':app.validate_media_paths(row);app.upsert_item(c,p['job'],row);continue
                        pid=row.get('product_id') or row.get('商品ID');value=row.get('first_listed_at') or row.get('首次上架日期');source=row.get('source') or row.get('证据来源');app.date.fromisoformat(value)
                        if not source:raise AccessError(400,'日期证据来源缺失')
                        old=c.execute('SELECT review,updated FROM items WHERE job=? AND id=?',(p['job'],pid)).fetchone()
                        if not old:raise AccessError(404,'未找到商品：'+str(pid))
                        review=json.loads(old['review']);evidence={'first_listed_at':value,'date_source':source,'date_verified':True}
                        if all(review.get(key)==v for key,v in evidence.items()):continue
                        previous=dict(review);review.update(**evidence,reviewer=actor['display_name'],user_id=actor['id'])
                        if previous.get('decision')=='approved':
                            review.update(decision='pending',invalidated_at=time.time(),invalidated_reason='上架日期证据已更新，请重新核对后确认',changed_fields=list(evidence))
                            app.event(c,p['job'],'approval_invalidated',{'product_id':pid,'changed_fields':list(evidence),'previous_review':previous})
                        c.execute('UPDATE items SET review=?,updated=? WHERE job=? AND id=?',(app.dumps(review),time.time(),p['job'],pid))
                return {'saved':True,'count':len(records)}
            if path=='/api/export':
                with app.connect() as c:
                    for r in c.execute('SELECT * FROM exports WHERE job=?',(p['job'],)):
                        d=json.loads(r['data'])
                        if d['status']=='building' and d['scope']==p.get('scope','all'):return {'id':r['id'],**d,'already_building':True}
                return app.export_job(p)
            if path=='/api/delivery':
                import workflow
                return workflow.create_delivery(app,p)
            if path=='/api/design':
                if p.get('id'):self.write_guard(actor,'design:'+p['id'],p.get('expected_design_revision'))
                return app.design_draft(p)
            if path in ('/api/design/check','/api/design/resolve'):
                self.write_guard(actor,'design:'+p['id'],p.get('expected_revision'))
                return app.check_design(p['id']) if path.endswith('/check') else app.resolve_uncertain_design(p['id'],p,actor)
            if path=='/api/design/preflight':return app.preflight_design(p['id'])
            if path=='/api/design/generate':
                self.write_guard(actor,'design:'+p['id'],p.get('expected_revision'))
                if p.get('authorize_one_image') is not True:raise AccessError(400,'请核对并确认本次单张生成')
                app.preflight_design(p['id'])
                with app.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    for r in c.execute('SELECT id,data FROM designs'):
                        if json.loads(r['data']).get('state') in ('uploading','submitting','running','uncertain'):raise AccessError(409,'已有生成任务在执行或提交待核，请先处理该任务')
                    row=c.execute('SELECT data FROM designs WHERE id=?',(p['id'],)).fetchone();d=json.loads(row['data'])
                    if d['state']!='draft':raise AccessError(409,'此方案已提交，不会重复调用')
                    d.update(state='uploading',generation_authorized_at=time.time(),submitted_by=actor['id'])
                    c.execute('UPDATE designs SET data=?,updated=? WHERE id=?',(app.dumps(d),time.time(),p['id']))
                threading.Thread(target=app.run_design,args=(p['id'],),daemon=True).start();return {'submitted':True,'state':'uploading'}
            if path=='/api/design/review':
                self.write_guard(actor,'design:'+p['id'],p.get('expected_revision'))
                with app.connect() as c:d=json.loads(c.execute('SELECT data FROM designs WHERE id=?',(p['id'],)).fetchone()['data'])
                if not d.get('images'):raise AccessError(400,'尚无生成图片，不能验收')
                if p.get('decision') not in ('approved','rejected'):raise AccessError(400,'审核信息不完整')
                if p.get('decision') == 'rejected' and not str(p.get('note','')).strip():raise AccessError(400,'请填写具体修改意见，方便建立下一版方案')
                if d.get('state') not in ('generated_pending_review','approved','rejected'):raise AccessError(409,'当前生成状态不能审核，请先核对结果')
                p['reviewer']=actor['display_name'];p['user_id']=actor['id'];app.update_design(p['id'],state='approved' if p['decision']=='approved' else 'rejected',review=p);return {'saved':True}
            raise AccessError(404,'动作不存在')
    return Handler
