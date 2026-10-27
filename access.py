"""Local accounts, server-side sessions and transactional edit leases.

The caller supplies core.connect.  Only password hashes and token hashes are
stored. HTTP cookie, CSRF, Origin, loopback bootstrap and TLS policy belong to
the server. A guard's yielded connection must be used for the protected write.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager


ROLES = ('admin', 'editor', 'viewer')
PASSWORD_ITERATIONS = 600_000


class AccessError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details or {}


def password_hash(password):
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise AccessError(400, '密码请使用 10 至 256 个字符')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PASSWORD_ITERATIONS)
    return f'pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}'


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password) > 256:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


class AccessStore:
    def __init__(self, connect, clock=time.time, session_seconds=43_200, idle_seconds=3_600):
        self.connect, self.clock = connect, clock
        self.session_seconds, self.idle_seconds = session_seconds, idle_seconds

    @contextmanager
    def _connection(self, immediate=False):
        c = self.connect()
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            if immediate:
                c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def migrate(self):
        with self._connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS access_users(
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer')),
                password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS access_sessions(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES access_users(id),
                token_hash TEXT NOT NULL UNIQUE, csrf_token TEXT NOT NULL,
                created REAL NOT NULL, last_seen REAL NOT NULL, expires REAL NOT NULL,
                revoked REAL);
            CREATE INDEX IF NOT EXISTS access_session_user ON access_sessions(user_id);
            CREATE TABLE IF NOT EXISTS access_leases(
                resource TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES access_sessions(id),
                acquired REAL NOT NULL, expires REAL NOT NULL, client_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS access_versions(resource TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS access_audit(
                id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL,
                target TEXT, detail TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS access_login_attempts(
                username TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                blocked_until REAL NOT NULL DEFAULT 0, updated REAL NOT NULL);
            ''')
            if 'client_id' not in {row['name'] for row in c.execute('PRAGMA table_info(access_leases)')}:
                c.execute("ALTER TABLE access_leases ADD COLUMN client_id TEXT NOT NULL DEFAULT ''")

    def _audit(self, c, actor, action, target='', detail=None):
        c.execute('INSERT INTO access_audit(actor,action,target,detail,created) VALUES(?,?,?,?,?)',
                  (actor, action, target, json.dumps(detail or {}, ensure_ascii=False), self.clock()))

    @staticmethod
    def _username(username):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{2,63}', username.strip()):
            raise AccessError(400, '账号请使用 3 至 64 位字母、数字或 . _ @ -')
        return username.strip().lower()

    @staticmethod
    def _display(name, username):
        name = str(name or username).strip()
        if not 1 <= len(name) <= 80:
            raise AccessError(400, '姓名请使用 1 至 80 个字符')
        return name

    @staticmethod
    def _public_user(row):
        return {k: (bool(row[k]) if k in ('enabled', 'must_change_password') else row[k])
                for k in ('id', 'username', 'display_name', 'role', 'enabled', 'must_change_password', 'created', 'updated')}

    def needs_setup(self):
        with self._connection() as c:
            return not c.execute('SELECT 1 FROM access_users LIMIT 1').fetchone()

    def bootstrap_admin(self, username, password, display_name=''):
        username = self._username(username)
        display_name, encoded = self._display(display_name, username), password_hash(password)
        now, uid = self.clock(), uuid.uuid4().hex
        with self._connection(immediate=True) as c:
            if c.execute('SELECT 1 FROM access_users LIMIT 1').fetchone():
                raise AccessError(409, '管理员已经初始化，请登录')
            c.execute('INSERT INTO access_users VALUES(?,?,?,?,?,1,0,?,?)',
                      (uid, username, display_name, 'admin', encoded, now, now))
            self._audit(c, uid, 'bootstrap_admin', uid)
            return self._public_user(c.execute('SELECT * FROM access_users WHERE id=?', (uid,)).fetchone())

    def _revoke_user(self, c, uid):
        c.execute('DELETE FROM access_leases WHERE session_id IN (SELECT id FROM access_sessions WHERE user_id=?)', (uid,))
        c.execute('UPDATE access_sessions SET revoked=? WHERE user_id=? AND revoked IS NULL', (self.clock(), uid))

    def _session_row(self, c, sid):
        row = c.execute('''SELECT s.*,u.username,u.display_name,u.role,u.enabled,u.must_change_password
                           FROM access_sessions s JOIN access_users u ON u.id=s.user_id WHERE s.id=?''', (sid,)).fetchone()
        now = self.clock()
        if not row or row['revoked'] is not None or not row['enabled'] or row['expires'] <= now or row['last_seen'] + self.idle_seconds <= now:
            raise AccessError(401, '登录已失效，请重新登录')
        return row

    @staticmethod
    def _principal(row):
        return {'id': row['user_id'], 'user_id': row['user_id'], 'username': row['username'],
                'display_name': row['display_name'], 'role': row['role'], 'session_id': row['id'],
                'csrf_token': row['csrf_token'], 'must_change_password': bool(row['must_change_password']),
                'expires_at': row['expires']}

    def _actor(self, c, actor, roles=None, allow_temporary=False):
        row = self._session_row(c, (actor or {}).get('session_id', ''))
        if roles and row['role'] not in roles:
            raise AccessError(403, '当前账号没有此操作权限')
        if row['must_change_password'] and not allow_temporary:
            raise AccessError(403, '请先修改临时密码')
        client_id = (actor or {}).get('client_id', '')
        if not isinstance(client_id, str) or (client_id and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', client_id)):
            raise AccessError(400, '页面标识无效，请刷新后重试')
        return {**self._principal(row), 'client_id': client_id}

    def login(self, username, password, replace_session=False):
        username = self._username(username)
        now = self.clock()
        # Use a single transaction for attempts and session uniqueness. Commit a
        # failed attempt before raising so repeated wrong passwords are throttled.
        failure = None
        with self._connection(immediate=True) as c:
            attempt = c.execute('SELECT * FROM access_login_attempts WHERE username=?', (username,)).fetchone()
            if attempt and attempt['blocked_until'] > now:
                raise AccessError(429, '登录尝试过多，请稍后再试', {'retry_after': int(attempt['blocked_until'] - now) + 1})
            user = c.execute('SELECT * FROM access_users WHERE username=?', (username,)).fetchone()
            encoded = user['password_hash'] if user else f'pbkdf2_sha256${PASSWORD_ITERATIONS}$' + '00' * 16 + '$' + '00' * 32
            valid = password_matches(password, encoded)
            if not user or not valid or not user['enabled']:
                count = (attempt['failures'] if attempt and attempt['updated'] > now - 900 else 0) + 1
                blocked = now + (900 if count >= 10 else 60 if count >= 5 else 0)
                c.execute('''INSERT INTO access_login_attempts VALUES(?,?,?,?) ON CONFLICT(username)
                             DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until,updated=excluded.updated''',
                          (username, count, blocked, now))
                self._audit(c, None, 'login_failed', username)
                failure = AccessError(401, '账号或密码不正确，或账号已停用')
            else:
                active = c.execute('''SELECT id FROM access_sessions WHERE user_id=? AND revoked IS NULL
                                      AND expires>? AND last_seen>?''', (user['id'], now, now - self.idle_seconds)).fetchone()
                if active and not replace_session:
                    raise AccessError(409, '此账号已在其他会话登录；确认接管后才能继续', {'replace_session_required': True})
                self._revoke_user(c, user['id'])
                token, sid, csrf = secrets.token_urlsafe(32), uuid.uuid4().hex, secrets.token_urlsafe(32)
                c.execute('INSERT INTO access_sessions VALUES(?,?,?,?,?,?,?,NULL)',
                          (sid, user['id'], hashlib.sha256(token.encode()).hexdigest(), csrf, now, now, now + self.session_seconds))
                c.execute('DELETE FROM access_login_attempts WHERE username=?', (username,))
                self._audit(c, user['id'], 'session_replaced' if active else 'login', sid)
                result = {'token': token, 'principal': self._principal(self._session_row(c, sid)), 'expires_at': now + self.session_seconds}
        if failure:
            raise failure
        return result

    def authenticate(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise AccessError(401, '请先登录')
        with self._connection() as c:
            found = c.execute('SELECT id FROM access_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not found:
                raise AccessError(401, '请先登录')
            row = self._session_row(c, found['id'])
            self._touch_session(c, row)
            return self._principal(row)

    def _touch_session(self, c, row):
        if row['last_seen'] < self.clock() - 60:
            c.execute('UPDATE access_sessions SET last_seen=? WHERE id=?', (self.clock(), row['id']))

    def logout(self, actor):
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, allow_temporary=True)
            c.execute('DELETE FROM access_leases WHERE session_id=?', (actor['session_id'],))
            c.execute('UPDATE access_sessions SET revoked=? WHERE id=?', (self.clock(), actor['session_id']))
            self._audit(c, actor['id'], 'logout', actor['session_id'])

    def change_password(self, actor, current_password, new_password):
        encoded = password_hash(new_password)
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, allow_temporary=True)
            row = c.execute('SELECT password_hash FROM access_users WHERE id=?', (actor['id'],)).fetchone()
            if not password_matches(current_password, row['password_hash']):
                raise AccessError(403, '当前密码不正确')
            c.execute('UPDATE access_users SET password_hash=?,must_change_password=0,updated=? WHERE id=?', (encoded, self.clock(), actor['id']))
            self._audit(c, actor['id'], 'change_password', actor['id'])
            return self._principal(self._session_row(c, actor['session_id']))

    def list_users(self, actor):
        with self._connection() as c:
            self._actor(c, actor, ('admin',))
            result = []
            now = self.clock()
            for row in c.execute('SELECT * FROM access_users ORDER BY created,id'):
                item = self._public_user(row)
                item['online'] = bool(c.execute('SELECT 1 FROM access_sessions WHERE user_id=? AND revoked IS NULL AND expires>? AND last_seen>?',
                                               (row['id'], now, now - self.idle_seconds)).fetchone())
                result.append(item)
            return result

    def create_user(self, actor, username, display_name='', role='editor', password=None):
        username = self._username(username)
        display_name = self._display(display_name, username)
        if role not in ROLES:
            raise AccessError(400, '账号角色无效')
        generated = password is None
        password = secrets.token_urlsafe(15) if generated else password
        encoded, uid, now = password_hash(password), uuid.uuid4().hex, self.clock()
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin',))
            if c.execute('SELECT 1 FROM access_users WHERE username=?', (username,)).fetchone():
                raise AccessError(409, '账号已存在')
            c.execute('INSERT INTO access_users VALUES(?,?,?,?,?,1,1,?,?)', (uid, username, display_name, role, encoded, now, now))
            self._audit(c, actor['id'], 'create_user', uid, {'role': role})
            result = {'user': self._public_user(c.execute('SELECT * FROM access_users WHERE id=?', (uid,)).fetchone())}
            if generated:
                result['temporary_password'] = password
            return result

    def update_user(self, actor, user_id, role=None, enabled=None, display_name=None):
        if role is not None and role not in ROLES:
            raise AccessError(400, '账号角色无效')
        if enabled is not None and not isinstance(enabled, bool):
            raise AccessError(400, '账号启用状态必须为布尔值')
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin',))
            user = c.execute('SELECT * FROM access_users WHERE id=?', (user_id,)).fetchone()
            if not user:
                raise AccessError(404, '账号不存在')
            new_role = role if role is not None else user['role']
            new_enabled = enabled if enabled is not None else bool(user['enabled'])
            name = self._display(display_name, user['username']) if display_name is not None else user['display_name']
            if user['role'] == 'admin' and user['enabled'] and (new_role != 'admin' or not new_enabled):
                if c.execute("SELECT COUNT(*) FROM access_users WHERE role='admin' AND enabled=1 AND id<>?", (user_id,)).fetchone()[0] == 0:
                    raise AccessError(409, '至少保留一个启用的管理员账号')
            c.execute('UPDATE access_users SET role=?,enabled=?,display_name=?,updated=? WHERE id=?',
                      (new_role, int(new_enabled), name, self.clock(), user_id))
            if not new_enabled or new_role != user['role']:
                self._revoke_user(c, user_id)
            self._audit(c, actor['id'], 'update_user', user_id, {'role': new_role, 'enabled': new_enabled})
            return self._public_user(c.execute('SELECT * FROM access_users WHERE id=?', (user_id,)).fetchone())

    def reset_password(self, actor, user_id, password=None):
        generated = password is None
        password = secrets.token_urlsafe(15) if generated else password
        encoded = password_hash(password)
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin',))
            if not c.execute('SELECT 1 FROM access_users WHERE id=?', (user_id,)).fetchone():
                raise AccessError(404, '账号不存在')
            c.execute('UPDATE access_users SET password_hash=?,must_change_password=1,updated=? WHERE id=?', (encoded, self.clock(), user_id))
            self._revoke_user(c, user_id)
            self._audit(c, actor['id'], 'reset_password', user_id)
            result = {'user_id': user_id, 'must_change_password': True}
            if generated:
                result['temporary_password'] = password
            return result

    @staticmethod
    def _resource(resource):
        if not isinstance(resource, str) or not 1 <= len(resource) <= 300 or any(ord(x) < 32 for x in resource):
            raise AccessError(400, '操作资源无效')
        return resource

    def _lease(self, c, resource):
        row = c.execute('''SELECT l.*,u.id AS user_id,u.username,u.display_name FROM access_leases l
                           JOIN access_sessions s ON s.id=l.session_id JOIN access_users u ON u.id=s.user_id
                           WHERE l.resource=? AND l.expires>? AND s.revoked IS NULL AND s.expires>?
                           AND s.last_seen>? AND u.enabled=1''',
                        (resource, self.clock(), self.clock(), self.clock() - self.idle_seconds)).fetchone()
        return dict(row) if row else None

    def _ttl(self, ttl):
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            raise AccessError(400, '锁定时间无效')
        if not 15 <= ttl <= 300:
            raise AccessError(400, '锁定时间须在 15 至 300 秒之间')
        return ttl

    @staticmethod
    def _owns_lease(actor, lease, resource):
        return bool(actor and lease and lease['session_id'] == actor['session_id']
                    and (resource == 'collector' or lease['client_id'] == actor.get('client_id', '')))

    def acquire(self, actor, resource, ttl=90, force=False):
        resource, ttl = self._resource(resource), self._ttl(ttl)
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin', 'editor'))
            existing = self._lease(c, resource)
            owned = self._owns_lease(actor, existing, resource)
            if existing and not owned:
                if not force:
                    raise AccessError(409, f'{existing["display_name"]} 正在操作，请稍后重试', {'lease': existing})
                if actor['role'] != 'admin':
                    raise AccessError(403, '只有管理员可以接管操作')
                self._audit(c, actor['id'], 'force_acquire_lease', resource, {'previous_user': existing['user_id'], 'previous_client': existing['client_id']})
            now = self.clock()
            c.execute('''INSERT INTO access_leases(resource,session_id,acquired,expires,client_id) VALUES(?,?,?,?,?) ON CONFLICT(resource)
                         DO UPDATE SET session_id=excluded.session_id,acquired=excluded.acquired,expires=excluded.expires,client_id=excluded.client_id''',
                      (resource, actor['session_id'], existing['acquired'] if owned else now, now + ttl,
                       '' if resource == 'collector' else actor.get('client_id', '')))
            c.execute('INSERT OR IGNORE INTO access_versions VALUES(?,0)', (resource,))
            return {**self._lease(c, resource), 'version': self.version(resource, c)}

    def renew(self, actor, resource, ttl=90):
        resource, ttl = self._resource(resource), self._ttl(ttl)
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin', 'editor'))
            self._require_lease(c, actor, resource)
            c.execute('UPDATE access_leases SET expires=? WHERE resource=?', (self.clock() + ttl, resource))
            return {**self._lease(c, resource), 'version': self.version(resource, c)}

    def release(self, actor, resource, force=False):
        resource = self._resource(resource)
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin', 'editor'))
            existing = self._lease(c, resource)
            owned = self._owns_lease(actor, existing, resource)
            if existing and not owned and not (force and actor['role'] == 'admin'):
                raise AccessError(409, '此操作由其他员工持有，无法释放')
            if existing and not owned:
                self._audit(c, actor['id'], 'force_release_lease', resource, {'previous_user': existing['user_id'], 'previous_client': existing['client_id']})
            c.execute('DELETE FROM access_leases WHERE resource=?', (resource,))
            return {'released': True, 'resource': resource}

    def list_leases(self, actor):
        with self._connection() as c:
            self._actor(c, actor, allow_temporary=True)
            resources = [r[0] for r in c.execute('SELECT resource FROM access_leases')]
            return [lease for r in resources if (lease := self._lease(c, r))]

    def version(self, resource, connection=None):
        resource = self._resource(resource)
        if connection is None:
            with self._connection() as c:
                return self.version(resource, c)
        row = connection.execute('SELECT version FROM access_versions WHERE resource=?', (resource,)).fetchone()
        return row[0] if row else 0

    def require_role(self, actor, roles=('admin', 'editor'), allow_temporary=False):
        with self._connection() as c:
            return self._actor(c, actor, tuple(roles), allow_temporary=allow_temporary)

    def session_principal(self, session_id):
        """For an already authenticated internal worker, never an HTTP login API."""
        with self._connection() as c:
            row = self._session_row(c, session_id)
            self._touch_session(c, row)
            return self._principal(row)

    principal_for_session = session_principal

    def audit(self, actor, action, detail=None, target=''):
        with self._connection(immediate=True) as c:
            principal = self._actor(c, actor, allow_temporary=True)
            self._audit(c, principal['id'], str(action)[:100], str(target)[:300], detail)

    def assert_lease(self, actor, resource):
        resource = self._resource(resource)
        with self._connection() as c:
            actor = self._actor(c, actor, ('admin', 'editor'))
            self._require_lease(c, actor, resource)
            return self._lease(c, resource)

    def assert_available(self, resource, actor=None):
        """Deny an active conflicting owner, including when no actor is supplied."""
        resource = self._resource(resource)
        with self._connection() as c:
            principal = self._actor(c, actor, ('admin', 'editor')) if actor else None
            lease = self._lease(c, resource)
            if lease and not self._owns_lease(principal, lease, resource):
                raise AccessError(409, f'{lease["display_name"]} 正在操作，请稍后重试', {'lease': lease})
            return lease

    def bump_version(self, resource, expected_version=None, actor=None):
        """For writes already serialized by the server's single-process lock.

        Prefer guard() when a business write can use its same DB transaction.
        This helper cannot atomically protect a write on a different connection.
        """
        resource = self._resource(resource)
        with self._connection(immediate=True) as c:
            principal = self._actor(c, actor, ('admin', 'editor')) if actor else None
            current = self.version(resource, c)
            if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int) or current != expected_version):
                raise AccessError(409, '内容已由其他页面更新，请刷新后再保存', {'current_version': current})
            c.execute('INSERT INTO access_versions VALUES(?,1) ON CONFLICT(resource) DO UPDATE SET version=version+1', (resource,))
            self._audit(c, principal['id'] if principal else None, 'resource_saved', resource, {'version': current + 1})
            return current + 1

    def _require_lease(self, c, actor, resource):
        lease = self._lease(c, resource)
        if not self._owns_lease(actor, lease, resource):
            raise AccessError(409, '操作锁已失效或被其他员工接管，请刷新后重新进入编辑', {'lease': lease})

    @contextmanager
    def guard(self, actor, resource, expected_version=None):
        """Atomic lease+version check and business write on the yielded connection."""
        resource = self._resource(resource)
        with self._connection(immediate=True) as c:
            actor = self._actor(c, actor, ('admin', 'editor'))
            self._require_lease(c, actor, resource)
            current = self.version(resource, c)
            if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version != current):
                raise AccessError(409, '内容已由其他页面更新，请刷新后再保存', {'current_version': current})
            yield c
            c.execute('''INSERT INTO access_versions VALUES(?,1) ON CONFLICT(resource)
                         DO UPDATE SET version=version+1''', (resource,))
            self._audit(c, actor['id'], 'resource_saved', resource, {'version': current + 1})

    def audit_log(self, actor, limit=100):
        with self._connection() as c:
            self._actor(c, actor, ('admin',))
            return [dict(row) for row in c.execute('SELECT * FROM access_audit ORDER BY id DESC LIMIT ?', (max(1, min(int(limit), 500)),))]
