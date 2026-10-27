"""Exercise authentication and conflicts against independent SQLite connections."""
import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from access import AccessError, AccessStore, password_matches


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.sqlite3'
        self.now = 1_700_000_000.0
        def connect():
            c = sqlite3.connect(self.db, timeout=10)
            c.row_factory = sqlite3.Row
            return c
        self.connect = connect
        self.store = AccessStore(connect, clock=lambda: self.now)
        self.store.migrate()
        self.admin_user = self.store.bootstrap_admin('owner', 'long-owner-password', '管理员')
        self.login = self.store.login('owner', 'long-owner-password')
        self.admin = self.login['principal']

    def tearDown(self):
        self.tmp.cleanup()

    def member(self, username='editor', role='editor'):
        user = self.store.create_user(self.admin, username, username, role, password='temporary-password')['user']
        logged = self.store.login(username, 'temporary-password')
        actor = self.store.change_password(logged['principal'], 'temporary-password', 'personal-password')
        return user, actor, logged['token']

    def raises_status(self, status, fn, *args, **kwargs):
        with self.assertRaises(AccessError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.status, status)
        return cm.exception

    def test_hashes_only_and_bootstrap_once(self):
        self.assertFalse(self.store.needs_setup())
        self.raises_status(409, self.store.bootstrap_admin, 'second', 'second-long-password')
        with self.connect() as c:
            user = c.execute('SELECT * FROM access_users').fetchone()
            sess = c.execute('SELECT * FROM access_sessions').fetchone()
        self.assertNotEqual(user['password_hash'], 'long-owner-password')
        self.assertTrue(password_matches('long-owner-password', user['password_hash']))
        self.assertNotEqual(sess['token_hash'], self.login['token'])
        self.assertEqual(self.store.authenticate(self.login['token'])['id'], self.admin_user['id'])

    def test_single_session_and_explicit_replacement(self):
        self.store.acquire(self.admin, 'collector')
        self.raises_status(409, self.store.login, 'owner', 'long-owner-password')
        replacement = self.store.login('owner', 'long-owner-password', replace_session=True)
        self.raises_status(401, self.store.authenticate, self.login['token'])
        self.assertEqual(self.store.list_leases(replacement['principal']), [])
        self.assertEqual(self.store.authenticate(replacement['token'])['id'], self.admin['id'])

    def test_simultaneous_login_has_one_winner(self):
        self.store.logout(self.admin)
        def attempt(_):
            try:
                return self.store.login('owner', 'long-owner-password')['token']
            except AccessError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(results.count(409), 1)
        self.assertEqual(sum(isinstance(r, str) for r in results), 1)

    def test_temporary_password_gate_and_change(self):
        made = self.store.create_user(self.admin, 'worker', role='editor')
        login = self.store.login('worker', made['temporary_password'])
        self.assertTrue(login['principal']['must_change_password'])
        self.raises_status(403, self.store.acquire, login['principal'], 'collector')
        principal = self.store.change_password(login['principal'], made['temporary_password'], 'employee-chosen-password')
        self.assertFalse(principal['must_change_password'])
        self.assertEqual(self.store.acquire(principal, 'collector')['user_id'], principal['id'])

    def test_disable_immediately_revokes_and_releases(self):
        user, actor, token = self.member()
        self.store.acquire(actor, 'collector')
        self.store.update_user(self.admin, user['id'], enabled=False)
        self.raises_status(401, self.store.authenticate, token)
        self.raises_status(401, self.store.acquire, actor, 'collector')
        self.assertEqual(self.store.list_leases(self.admin), [])

    def test_cannot_remove_last_admin(self):
        self.raises_status(409, self.store.update_user, self.admin, self.admin['id'], role='viewer')
        self.raises_status(409, self.store.update_user, self.admin, self.admin['id'], enabled=False)
        second, _, _ = self.member('backup', 'admin')
        self.assertEqual(self.store.update_user(self.admin, self.admin['id'], role='editor')['role'], 'editor')
        self.raises_status(401, self.store.authenticate, self.login['token'])

    def test_viewer_cannot_write_or_manage_users(self):
        _, viewer, _ = self.member('reader', 'viewer')
        self.raises_status(403, self.store.acquire, viewer, 'collector')
        self.raises_status(403, self.store.list_users, viewer)
        self.raises_status(403, self.store.create_user, viewer, 'newworker')
        self.assertEqual(self.store.list_leases(viewer), [])

    def test_password_reset_revokes_old_session(self):
        user, actor, token = self.member()
        reset = self.store.reset_password(self.admin, user['id'])
        self.raises_status(401, self.store.authenticate, token)
        self.raises_status(401, self.store.login, 'editor', 'personal-password')
        result = self.store.login('editor', reset['temporary_password'])
        self.assertTrue(result['principal']['must_change_password'])

    def test_throttle_is_persisted_on_failure(self):
        for _ in range(5):
            self.raises_status(401, self.store.login, 'owner', 'wrong-password')
        self.raises_status(429, self.store.login, 'owner', 'long-owner-password')
        self.now += 61
        self.store.login('owner', 'long-owner-password', replace_session=True)

    def test_lease_conflict_expiry_and_renewal(self):
        _, editor, _ = self.member()
        self.store.acquire(self.admin, 'item:job:123', ttl=30)
        conflict = self.raises_status(409, self.store.acquire, editor, 'item:job:123')
        self.assertEqual(conflict.details['lease']['user_id'], self.admin['id'])
        self.now += 10
        self.store.renew(self.admin, 'item:job:123', ttl=30)
        self.now += 25
        self.raises_status(409, self.store.acquire, editor, 'item:job:123')
        self.now += 6
        self.assertEqual(self.store.acquire(editor, 'item:job:123')['user_id'], editor['id'])
        self.raises_status(409, self.store.renew, self.admin, 'item:job:123')

    def test_force_takeover_admin_only_and_audited(self):
        _, editor, _ = self.member()
        self.store.acquire(self.admin, 'collector')
        self.raises_status(403, self.store.acquire, editor, 'collector', force=True)
        self.store.release(self.admin, 'collector')
        self.store.acquire(editor, 'collector')
        self.store.acquire(self.admin, 'collector', force=True)
        self.raises_status(409, self.store.assert_lease, editor, 'collector')
        audit = self.store.audit_log(self.admin)
        event = next(x for x in audit if x['action'] == 'force_acquire_lease')
        self.assertEqual(json.loads(event['detail'])['previous_user'], editor['id'])

    def test_same_session_stale_tab_version_rejected(self):
        resource = 'item:job:123'
        self.store.acquire(self.admin, resource)
        with self.connect() as c:
            c.execute('CREATE TABLE business(value TEXT)')
        with self.store.guard(self.admin, resource, expected_version=0) as c:
            c.execute('INSERT INTO business VALUES(?)', ('saved',))
        self.assertEqual(self.store.version(resource), 1)
        with self.assertRaises(AccessError) as cm:
            with self.store.guard(self.admin, resource, expected_version=0) as c:
                c.execute('INSERT INTO business VALUES(?)', ('stale',))
        self.assertEqual(cm.exception.status, 409)
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM business').fetchone()[0], 1)

    def test_guard_rolls_back_business_and_version(self):
        self.store.acquire(self.admin, 'profile:test')
        with self.assertRaises(RuntimeError):
            with self.store.guard(self.admin, 'profile:test', expected_version=0) as c:
                c.execute("INSERT INTO access_versions VALUES('should-rollback',5)")
                raise RuntimeError('simulated write failure')
        self.assertEqual(self.store.version('profile:test'), 0)
        self.assertEqual(self.store.version('should-rollback'), 0)

    def test_expired_session_cannot_hold_or_renew_lock(self):
        self.store.acquire(self.admin, 'collector', ttl=300)
        self.now += self.store.idle_seconds + 1
        self.raises_status(401, self.store.authenticate, self.login['token'])
        self.raises_status(401, self.store.renew, self.admin, 'collector')
        new = self.store.login('owner', 'long-owner-password')
        self.assertEqual(self.store.list_leases(new['principal']), [])

    def test_serialized_concurrent_guard_same_version_one_winner(self):
        self.store.acquire(self.admin, 'design:test')
        with self.connect() as c:
            c.execute('CREATE TABLE changes(value TEXT)')
        def write(_):
            try:
                with self.store.guard(self.admin, 'design:test', expected_version=0) as c:
                    c.execute("INSERT INTO changes VALUES('saved')")
                return 200
            except AccessError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, range(2)))
        self.assertEqual(sorted(results), [200, 409])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM changes').fetchone()[0], 1)
        self.assertEqual(self.store.version('design:test'), 1)

    def test_require_role_reloads_not_forged_principal_role(self):
        _, viewer, _ = self.member('reader', 'viewer')
        self.raises_status(403, self.store.require_role, {**viewer, 'role': 'admin'}, ('admin',))

    def test_two_tabs_same_session_cannot_share_edit_lease(self):
        first = {**self.admin, 'client_id': 'tab-first'}
        second = {**self.admin, 'client_id': 'tab-second'}
        for resource in ('item:job:123', 'profile:icon-us', 'design:one'):
            acquired = self.store.acquire(first, resource)
            self.assertEqual(acquired['client_id'], 'tab-first')
            self.raises_status(409, self.store.acquire, second, resource)
            self.raises_status(409, self.store.renew, second, resource)
            self.raises_status(409, self.store.release, second, resource)
            self.raises_status(409, self.store.assert_lease, second, resource)
            self.raises_status(409, self.store.assert_available, resource, second)
            with self.assertRaises(AccessError) as cm:
                with self.store.guard(second, resource, expected_version=0):
                    self.fail('The wrong tab must never reach the protected mutation')
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(self.store.renew(first, resource)['client_id'], 'tab-first')
            with self.store.guard(first, resource, expected_version=0):
                pass
            self.assertEqual(self.store.version(resource), 1)
            self.store.release(first, resource)
            self.assertEqual(self.store.acquire(second, resource)['client_id'], 'tab-second')

    def test_collector_web_and_extension_clients_share_same_session(self):
        web = {**self.admin, 'client_id': 'web-tab'}
        extension = {**self.admin, 'client_id': 'chrome-extension'}
        self.store.acquire(web, 'collector')
        self.assertEqual(self.store.assert_lease(extension, 'collector')['session_id'], self.admin['session_id'])
        self.store.acquire(extension, 'collector')
        self.store.renew(web, 'collector')
        self.store.renew(extension, 'collector')
        self.store.assert_available('collector', extension)
        _, other, _ = self.member()
        self.raises_status(409, self.store.acquire, other, 'collector')
        self.store.release(extension, 'collector')

    def test_old_lease_schema_migration_preserves_checkpoint(self):
        with self.connect() as c:
            c.execute('DROP TABLE access_leases')
            c.execute('CREATE TABLE access_leases(resource TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES access_sessions(id), acquired REAL NOT NULL, expires REAL NOT NULL)')
            c.execute('INSERT INTO access_leases VALUES(?,?,?,?)', ('collector', self.admin['session_id'], self.now, self.now + 60))
        self.store.migrate()
        lease = self.store.assert_lease({**self.admin, 'client_id': 'fresh-web-tab'}, 'collector')
        self.assertEqual(lease['client_id'], '')
        self.assertEqual(lease['expires'], self.now + 60)
        self.store.migrate()
        self.assertEqual(len(self.store.list_leases(self.admin)), 1)

    def test_invalid_client_id_rejected_and_force_takeover_audited(self):
        self.raises_status(400, self.store.acquire, {**self.admin, 'client_id': 'bad\nheader'}, 'profile:one')
        first = {**self.admin, 'client_id': 'tab-first'}
        second = {**self.admin, 'client_id': 'tab-second'}
        self.store.acquire(first, 'profile:one')
        self.store.acquire(second, 'profile:one', force=True)
        self.raises_status(409, self.store.assert_lease, first, 'profile:one')
        event = next(x for x in self.store.audit_log(self.admin) if x['action'] == 'force_acquire_lease')
        self.assertEqual(json.loads(event['detail'])['previous_client'], 'tab-first')

    def test_internal_collector_activity_extends_idle_not_absolute_expiry(self):
        started = self.now
        for _ in range(4):
            self.now += 1200
            actor = self.store.principal_for_session(self.admin['session_id'])
            self.assertEqual(actor['id'], self.admin['id'])
        with self.connect() as c:
            row = c.execute('SELECT last_seen,expires FROM access_sessions WHERE id=?', (self.admin['session_id'],)).fetchone()
        self.assertEqual(row['last_seen'], self.now)
        self.assertEqual(row['expires'], started + self.store.session_seconds)
        self.now += 30
        self.store.principal_for_session(self.admin['session_id'])
        with self.connect() as c:
            self.assertEqual(c.execute('SELECT last_seen FROM access_sessions WHERE id=?', (self.admin['session_id'],)).fetchone()[0], row['last_seen'])
        self.now = started + self.store.session_seconds
        self.raises_status(401, self.store.principal_for_session, self.admin['session_id'])


if __name__ == '__main__':
    unittest.main()
