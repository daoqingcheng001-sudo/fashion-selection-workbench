"""Ephemeral HTTP tests: no production users, platform access or paid generation."""
from collections import defaultdict
import concurrent.futures
from contextlib import ExitStack
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import access
import core
import http_api
import runtime_config as runtime
import server


class HTTPIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data, self.project, self.output = [self.root/x for x in ('data','project','exports')]
        for path in (self.data/'assets',self.project,self.output):path.mkdir(parents=True)
        self.style=self.project/'style.json';self.style.write_text(json.dumps({'version':'http-fixture','families':[]}))
        for module,values in ((core,{'DATA':self.data,'PROJECT':self.project,'STYLE_PATH':self.style}),
                (server,{'DATA':self.data,'PROJECT':self.project,'OUTPUT':self.output,'STYLE_PATH':self.style,'ASSETS':self.data/'assets','ASSET_MAP':{},'LOGIN_ATTEMPTS':defaultdict(list)}),
                (runtime,{'DATA_DIR':self.data,'BACKUP_DIR':self.root/'backups'})):
            for key,value in values.items():self.stack.enter_context(patch.object(module,key,value))
        self.stack.enter_context(patch.object(access,'PASSWORD_ITERATIONS',100_000))
        self.stack.enter_context(patch.object(http_api,'lan_addresses',return_value=[]))
        self.stack.enter_context(patch.object(http_api,'tailscale_network',return_value={'self': set(), 'peers': set()}))
        core.init();self.job=core.create_job('icon-us')
        with core.connect() as c:core.upsert_item(c,self.job,{'id':'fixture-1','name':'TEST ONLY Halter Dress','sales':'600+ sold','url':'https://us.shein.com/fixture-p-1.html','colors':[]})
        http_api.init_http(server)
        base=http_api.make_handler(server)
        class QuietHandler(base):
            def log_message(self,*args):pass
            def setup(self):
                super().setup()
                if getattr(self.server,'fixture_peer',None):self.client_address=(self.server.fixture_peer,self.client_address[1])
        self.http=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        self.port=self.http.server_address[1]
        self.stack.enter_context(patch.object(server,'PORT',self.port))
        self.thread=threading.Thread(target=self.http.serve_forever,kwargs={'poll_interval':0.02},daemon=True);self.thread.start()
        response=self.expect(200,'POST','/api/auth/setup',{'username':'owner','password':'owner-fixture-password','display_name':'Fixture Owner'})
        self.owner=self.session(response)

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=2);self.stack.close()

    def request(self,method,path,payload=None,actor=None,headers=None):
        h={}
        if actor:h.update({'Cookie':actor['cookie'],'X-CSRF-Token':actor['csrf'],'X-Selection-Client':actor.get('client','fixture-browser-a')})
        if payload is not None:h['Content-Type']='application/json'
        if method=='POST':h['Origin']=f'http://127.0.0.1:{self.port}'
        h.update(headers or {});h={k:v for k,v in h.items() if v is not None}
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=8)
        try:
            conn.request(method,path,body=json.dumps(payload).encode() if payload is not None else None,headers=h)
            response=conn.getresponse();raw=response.read();meta=dict(response.getheaders())
            try:body=json.loads(raw)
            except (ValueError,UnicodeDecodeError):body=raw
            return response.status,body,meta
        finally:conn.close()

    def expect(self,status,method,path,payload=None,actor=None,headers=None):
        result=self.request(method,path,payload,actor,headers);self.assertEqual(result[0],status,result[1]);return result

    def session(self,response,client='fixture-browser-a'):
        return {'cookie':response[2]['Set-Cookie'].split(';')[0],'csrf':response[1]['user']['csrf_token'],'user':response[1]['user'],'client':client}

    def member(self,name='editor',role='editor',change_password=True):
        made=self.expect(200,'POST','/api/admin/users',{'username':name,'role':role,'display_name':name},self.owner)[1]
        login=self.expect(200,'POST','/api/auth/login',{'username':name,'password':made['temporary_password']})
        actor=self.session(login,client='client-'+name)
        if change_password:self.expect(200,'POST','/api/auth/password',{'current_password':made['temporary_password'],'new_password':name+'-fixture-private'},actor)
        return actor

    def lock(self,actor,resource,action='acquire'):
        return self.request('POST','/api/lock',{'resource':resource,'action':action},actor)

    def pair(self,actor=None):
        key=self.expect(200,'POST','/api/pairing',{'job':self.job},actor or self.owner)[1]['key']
        return {'Origin':'chrome-extension://fixturecollector','X-Selection-Key':key,'X-Collector-Client':'fixture-extension-client'}

    def start_collector(self,actor=None):
        actor=actor or self.owner;headers=self.pair(actor)
        self.expect(200,'POST','/api/job/action',{'job':self.job,'action':'resume'},actor);return headers

    def revision(self):return self.expect(200,'GET','/api/items?job='+self.job,actor=self.owner)[1][0]['revision']

    def test_health_public_and_data_requires_login(self):
        self.assertTrue(self.expect(200,'GET','/api/health')[1]['ready']);self.expect(200,'GET','/')
        for path in ('/api/state','/api/items?job='+self.job,'/asset/not-real','/export/not-real','/data/selection.sqlite3'):self.expect(401,'GET',path)
        self.expect(200,'GET','/api/state',actor=self.owner)
        self.assertNotIn('token_hash',json.dumps(self.expect(200,'GET','/api/auth/me',actor=self.owner)[1]))

    def test_tailnet_access_requires_known_peer_self_host_and_session(self):
        mapping = {'self': {'100.100.64.1'}, 'peers': {'100.99.44.67'}}
        host = {'Host': '100.100.64.1:' + str(self.port)}
        with patch.object(http_api, 'tailscale_network', return_value=mapping):
            self.http.fixture_peer = '100.99.44.67'
            try:
                self.expect(200, 'GET', '/api/health', headers=host)
                self.expect(200, 'GET', '/', headers=host)
                self.expect(401, 'GET', '/api/state', headers=host)
                self.expect(200, 'GET', '/api/state', actor=self.owner, headers=host)
                self.expect(403, 'GET', '/api/health', headers={'Host': '100.100.100.100:' + str(self.port)})
                self.expect(403, 'POST', '/api/lock', {'resource': 'item:' + self.job + ':fixture-1', 'action': 'acquire'},
                            self.owner, {**host, 'Origin': 'https://untrusted.example'})
                self.http.fixture_peer = '100.100.64.1'
                self.expect(200, 'GET', '/api/health', headers=host)
                self.http.fixture_peer = '100.99.44.68'
                self.expect(403, 'GET', '/api/health', headers=host)
                self.http.fixture_peer = '203.0.113.9'
                self.expect(403, 'GET', '/api/health', headers={**host, 'X-Forwarded-For': '100.99.44.67'})
            finally:
                self.http.fixture_peer = None

    def test_host_origin_csrf_and_bootstrap_boundaries(self):
        self.expect(403,'GET','/api/health',headers={'Host':'untrusted.example:'+str(self.port)})
        payload={'resource':'item:'+self.job+':fixture-1','action':'acquire'}
        self.expect(403,'POST','/api/lock',payload,self.owner,{'Origin':'https://untrusted.example'})
        self.expect(403,'POST','/api/lock',payload,self.owner,{'X-CSRF-Token':None})
        self.expect(403,'POST','/api/lock',payload,self.owner,{'Sec-Fetch-Site':'cross-site'})
        self.http.fixture_peer='192.168.1.99'
        self.assertFalse(self.expect(200,'GET','/api/auth/me')[1]['local_setup_allowed'])
        self.expect(403,'POST','/api/auth/setup',{'username':'remote','password':'remote-fixture-password'})
        self.http.fixture_peer='203.0.113.9';self.expect(403,'GET','/api/health');self.http.fixture_peer=None
        self.expect(409,'POST','/api/auth/setup',{'username':'another','password':'another-fixture-password'})

    def test_temporary_password_and_viewer_permissions(self):
        viewer=self.member('viewer','viewer',change_password=False)
        self.expect(403,'GET','/api/state',actor=viewer)
        self.assertEqual(self.expect(200,'GET','/api/auth/me',actor=viewer)[1]['user']['role'],'viewer')
        reset=self.expect(200,'POST','/api/admin/reset-password',{'id':viewer['user']['id']},self.owner)[1]
        self.expect(401,'GET','/api/state',actor=viewer)
        login=self.expect(200,'POST','/api/auth/login',{'username':'viewer','password':reset['temporary_password']});viewer=self.session(login)
        self.expect(200,'POST','/api/auth/password',{'current_password':reset['temporary_password'],'new_password':'viewer-fixture-private'},viewer)
        self.expect(200,'GET','/api/state',actor=viewer);self.expect(403,'POST','/api/job',{'profile':'icon-us'},viewer);self.expect(403,'GET','/api/admin/users',actor=viewer)

    def test_same_account_tabs_cannot_share_edit_lock(self):
        resource='item:'+self.job+':fixture-1';self.assertEqual(self.lock(self.owner,resource)[0],200)
        other_tab={**self.owner,'client':'fixture-browser-b'};self.assertEqual(self.lock(other_tab,resource)[0],409)
        payload={'job':self.job,'product_id':'fixture-1','expected_revision':self.revision(),'review':{'decision':'pending','reason':'other tab'}}
        self.expect(409,'POST','/api/review',payload,other_tab);self.expect(200,'POST','/api/review',payload,self.owner)

    def test_other_editor_conflict_and_stale_revision(self):
        editor=self.member();resource='item:'+self.job+':fixture-1';old=self.revision()
        self.assertEqual(self.lock(self.owner,resource)[0],200);self.assertEqual(self.lock(editor,resource)[0],409)
        self.expect(409,'POST','/api/import',{'job':self.job,'rows':[{'id':'fixture-1','name':'overwrite'}]},editor)
        payload={'job':self.job,'product_id':'fixture-1','expected_revision':old,'review':{'decision':'pending','reason':'first accepted'}}
        self.assertNotEqual(self.expect(200,'POST','/api/review',payload,self.owner)[1]['revision'],old)
        payload['review']['reason']='stale must not overwrite';self.expect(409,'POST','/api/review',payload,self.owner)
        with core.connect() as c:self.assertEqual(json.loads(c.execute('SELECT review FROM items WHERE job=? AND id=?',(self.job,'fixture-1')).fetchone()['review'])['reason'],'first accepted')

    def test_collector_lease_job_scope_and_client_binding(self):
        editor=self.member();headers=self.start_collector()
        self.expect(409,'POST','/api/job/action',{'job':self.job,'action':'resume'},editor)
        task=self.expect(200,'GET','/api/next?job='+self.job,headers=headers)[1]['task'];self.assertTrue(task['claim_token'])
        self.expect(409,'GET','/api/collector/task',headers={**headers,'X-Collector-Client':'different-extension-client'})
        other=core.create_job('icon-us');self.expect(403,'GET','/api/next?job='+other,headers=headers)
        self.expect(403,'GET','/api/state',headers=headers);self.expect(404,'GET','/api/next?job='+self.job,actor=self.owner)

    def test_originless_collector_can_authenticate_and_capture_without_cookie(self):
        # Chrome extension service-worker GET requests may omit Origin even
        # though they include the task-scoped key and collector client header.
        headers={**self.pair(),'Origin':None}
        self.assertEqual(self.expect(200,'GET','/api/collector/task',headers=headers)[1]['job'],self.job)
        self.expect(200,'POST','/api/job/action',{'job':self.job,'action':'resume'},headers=headers)
        self.expect(200,'POST','/api/collector/heartbeat',{'job':self.job},headers=headers)
        task=self.expect(200,'GET','/api/next?job='+self.job,headers=headers)[1]['task']
        self.assertTrue(task['claim_token'])
        payload={'job':self.job,'task_id':task['id'],'claim_token':task['claim_token'],
                 'capture':{'kind':'list','url':task['url'],'cards':[]}}
        self.expect(200,'POST','/api/capture',payload,headers=headers)
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM captures WHERE job=?',(self.job,)).fetchone()[0],1)

    def test_collector_headers_never_fall_back_to_browser_cookie(self):
        paired=self.pair()
        cases=(
            {'X-Selection-Key':''},
            {'X-Collector-Client':''},
            {'X-Selection-Key':'not-a-valid-task-key','X-Collector-Client':'fixture-extension-client'},
            {'X-Selection-Key':paired['X-Selection-Key']},
            {'X-Selection-Key':paired['X-Selection-Key'],'X-Collector-Client':'short'},
        )
        for case in cases:
            with self.subTest(header_names=sorted(case)):
                # /api/state would succeed if the valid browser cookie were
                # accepted instead of the explicitly supplied collector auth.
                self.expect(401,'GET','/api/state',actor=self.owner,headers={'Origin':None,**case})
                self.expect(401,'POST','/api/admin/backup',{},self.owner,{'Origin':None,**case})

    def test_collector_headers_reject_web_origins_and_originless_cross_site(self):
        headers={**self.start_collector(),'Origin':None}
        for origin in (f'http://127.0.0.1:{self.port}','https://untrusted.example','null'):
            with self.subTest(origin=origin):
                rejected={**headers,'Origin':origin}
                self.expect(403,'GET','/api/collector/task',headers=rejected)
                self.expect(403,'POST','/api/collector/heartbeat',{'job':self.job},headers=rejected)
        rejected={**headers,'Sec-Fetch-Site':'cross-site'}
        self.expect(403,'GET','/api/collector/task',headers=rejected)
        self.expect(403,'POST','/api/collector/heartbeat',{'job':self.job},headers=rejected)
        self.expect(403,'OPTIONS','/api/collector/task',headers=headers)

    def test_originless_collector_keeps_scope_client_and_session_guards(self):
        headers={**self.start_collector(),'Origin':None}
        self.expect(200,'GET','/api/collector/task',headers=headers)
        self.expect(409,'GET','/api/collector/task',headers={**headers,'X-Collector-Client':'different-extension-client'})
        other=core.create_job('icon-us')
        self.expect(403,'GET','/api/next?job='+other,headers=headers)
        self.expect(403,'POST','/api/collector/heartbeat',{'job':other},headers=headers)
        self.expect(403,'GET','/api/state',headers=headers)
        self.expect(403,'POST','/api/admin/backup',{},headers=headers)
        self.expect(200,'POST','/api/auth/login',{'username':'owner','password':'owner-fixture-password','replace_session':True})
        self.expect(401,'GET','/api/collector/task',headers=headers)
        self.expect(401,'POST','/api/collector/heartbeat',{'job':self.job},headers=headers)

    def test_old_capture_and_image_claims_are_rejected(self):
        headers=self.start_collector();task=self.expect(200,'GET','/api/next?job='+self.job,headers=headers)[1]['task']
        payload={'job':self.job,'task_id':task['id'],'claim_token':'old-claim','capture':{'kind':'list','url':task['url'],'cards':[]}}
        self.expect(409,'POST','/api/capture',payload,headers=headers)
        image={'job':self.job,'task_id':task['id'],'claim_token':'old-claim','product_id':'fixture-1','role':'front','bytes':'invalid-not-decoded'}
        self.expect(409,'POST','/api/image',image,headers=headers)
        payload['claim_token']=task['claim_token'];self.expect(200,'POST','/api/capture',payload,headers=headers)
        self.expect(200,'POST','/api/job/action',{'job':self.job,'action':'terminate'},self.owner)
        self.expect(409,'POST','/api/capture',payload,headers=headers)
        with core.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(self.job,)).fetchone()['status'],'terminated')

    def test_editor_lease_blocks_live_capture(self):
        editor=self.member();self.assertEqual(self.lock(editor,'item:'+self.job+':fixture-1')[0],200)
        headers=self.start_collector();task=self.expect(200,'GET','/api/next?job='+self.job,headers=headers)[1]['task']
        payload={'job':self.job,'task_id':task['id'],'claim_token':task['claim_token'],'capture':{'kind':'list','url':task['url'],'cards':[{'id':'fixture-1','name':'overwrite','sales':'600+ sold','url':'https://us.shein.com/fixture-p-1.html'}]}}
        self.expect(409,'POST','/api/capture',payload,headers=headers)

    def test_import_cannot_read_external_or_symlinked_host_image(self):
        external=self.root/'private-image.png';external.write_bytes(b'private-test-only-image');link=self.data/'assets/escape.png';link.symlink_to(external)
        for path in (external,link):self.expect(400,'POST','/api/import',{'job':self.job,'rows':[{'id':'forbidden','front':str(path)}]},self.owner)
        allowed=self.data/'assets/allowed.png';allowed.write_bytes(b'fixture-presence')
        self.expect(200,'POST','/api/import',{'job':self.job,'rows':[{'id':'allowed','front':str(allowed)}]},self.owner)
        with core.connect() as c:self.assertIsNone(c.execute("SELECT 1 FROM items WHERE id='forbidden'").fetchone())

    def test_backup_returns_json_and_complete_sqlite_snapshot(self):
        result=self.expect(200,'POST','/api/admin/backup',{},self.owner)[1];self.assertTrue(result['saved']);path=Path(result['path']);self.assertTrue(path.is_relative_to(self.root/'backups'))
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(c.execute('SELECT count(*) FROM access_users').fetchone()[0],1)
        self.expect(403,'POST','/api/admin/backup',{},self.member())

    def test_session_takeover_revokes_cookie_pairing_and_edit_lock(self):
        headers=self.start_collector();self.expect(200,'GET','/api/collector/task',headers=headers)
        self.expect(409,'POST','/api/auth/login',{'username':'owner','password':'owner-fixture-password'})
        login=self.expect(200,'POST','/api/auth/login',{'username':'owner','password':'owner-fixture-password','replace_session':True});replacement=self.session(login)
        self.expect(401,'GET','/api/state',actor=self.owner);self.expect(401,'GET','/api/collector/task',headers=headers)
        self.assertEqual(self.expect(200,'GET','/api/locks',actor=replacement)[1]['leases'],[])

    def test_duplicate_generation_submits_once_without_paid_call(self):
        generated=threading.Event();calls=[]
        def fake_run(did):calls.append(did);generated.set()
        self.stack.enter_context(patch.object(server,'preflight_design',return_value={'ready':True}));self.stack.enter_context(patch.object(server,'run_design',side_effect=fake_run))
        # A generation draft must now originate in the reviewed, complete library.
        image=self.data/'assets/generation-fixture.png';image.write_bytes(b'test-only-file-presence')
        with core.connect() as c:
            rules=json.loads(c.execute('SELECT rules FROM jobs WHERE id=?',(self.job,)).fetchone()['rules'])
            core.upsert_item(c,self.job,{'id':'fixture-1','breadcrumb':['Women','Dresses','Mini Dresses'],
                'front':str(image),'back':str(image),'color_count':1,'colors':[{'name':'Black','image':str(image)}]})
        self.assertEqual(self.lock(self.owner,'item:'+self.job+':fixture-1')[0],200)
        review={'decision':'approved','is_basic':False,'date_verified':True,
                'first_listed_at':rules['as_of'],'date_source':'TEST ONLY approved generation fixture'}
        source_revision=self.expect(200,'POST','/api/review',{'job':self.job,'product_id':'fixture-1',
            'expected_revision':self.revision(),'review':review},self.owner)[1]['revision']
        self.lock(self.owner,'item:'+self.job+':fixture-1','release')
        design=self.expect(200,'POST','/api/design',{'job':self.job,'product_id':'fixture-1',
            'changes':'TEST ONLY change','expected_revision':source_revision},self.owner)[1]
        self.assertEqual(self.lock(self.owner,'design:'+design['id'])[0],200)
        revision=next(d['revision'] for d in self.expect(200,'GET','/api/state',actor=self.owner)[1]['designs'] if d['id']==design['id'])
        self.expect(400,'POST','/api/design/generate',{'id':design['id'],'expected_revision':revision},self.owner)
        payload={'id':design['id'],'authorize_one_image':True,'expected_revision':revision}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.request('POST','/api/design/generate',payload,self.owner),range(2)))
        self.assertEqual(sorted(x[0] for x in results),[200,409]);self.assertTrue(generated.wait(1));self.assertEqual(calls,[design['id']])

    def test_login_throttle_survives_failed_attempts(self):
        for _ in range(5):self.expect(401,'POST','/api/auth/login',{'username':'owner','password':'wrong-fixture-password'})
        self.expect(429,'POST','/api/auth/login',{'username':'owner','password':'owner-fixture-password'})


if __name__=='__main__':unittest.main(verbosity=2)
