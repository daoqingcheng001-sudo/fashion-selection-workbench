"""Four-step HTTP acceptance with isolated SQLite and synthetic images only."""
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
from PIL import Image

import core
import server
import test_http
import workflow


class WorkflowHTTPTest(unittest.TestCase):
    # Reuse only infrastructure, not the older suite's test cases.
    request = test_http.HTTPIntegrationTest.request
    expect = test_http.HTTPIntegrationTest.expect
    session = test_http.HTTPIntegrationTest.session
    member = test_http.HTTPIntegrationTest.member
    lock = test_http.HTTPIntegrationTest.lock
    pair = test_http.HTTPIntegrationTest.pair

    def setUp(self):
        test_http.HTTPIntegrationTest.setUp(self)
        self.deliveries = []
        self.network = self.stack.enter_context(patch.object(server, 'comfy', side_effect=AssertionError('No paid or platform network in acceptance tests')))
        self.other_job = core.create_job('icon-us')
        image = self.data/'assets/synthetic.png'
        Image.new('RGB',(16,24),'#504466').save(image)
        with core.connect() as c:
            c.execute('DELETE FROM items')
            for jid in (self.job, self.other_job):
                rules = {**core.DEFAULT, 'as_of':'2026-10-14', 'age_basis':'sku_estimate',
                         'name':'TEST ONLY '+jid, 'store':'SHEIN DEMO TEST'}
                c.execute('UPDATE jobs SET rules=? WHERE id=?',(core.dumps(rules),jid))
            for jid,pid,decision,status in (
                    (self.job,'approved','approved','eligible'),
                    (self.job,'eligible','pending','eligible'),
                    (self.job,'missing-image','approved','pending'),
                    (self.job,'excluded','approved','excluded'),
                    (self.job,'rejected','rejected','eligible'),
                    (self.other_job,'approved','approved','eligible')):
                data = {'id':pid,'name':'TEST ONLY Halter Dress','sales':'700+ sold','sku':'sz2609000000000000000',
                    'url':'https://us.shein.com/TEST-ONLY-p-123456789.html',
                    'breadcrumb':['Women','Dresses','Mini Dresses'],
                    'front':str(image),'back':str(image),'color_count':1,
                    'colors':[{'name':'Black','image':str(image),'source_url':'TEST ONLY IMAGE'}]}
                if status=='pending':data.pop('back')
                if status=='excluded':data['sales']='300+ sold'
                review = {'decision':decision,'is_basic':False,'reviewer':'TEST ONLY reviewer',
                          'reason':'Synthetic fixture, not a real selected garment'}
                core.upsert_item(c,jid,data)
                c.execute('UPDATE items SET review=? WHERE job=? AND id=?',(core.dumps(review),jid,pid))

    def tearDown(self):
        for did in self.deliveries:
            workflow.wait_delivery(did, timeout=3)
        test_http.HTTPIntegrationTest.tearDown(self)

    def item(self, pid='approved', jid=None):
        return next(x for x in self.expect(200,'GET','/api/items?job='+(jid or self.job),actor=self.owner)[1] if x['id']==pid)

    def ref(self, pid='approved', jid=None):
        return {'job':jid or self.job,'product_id':pid,'expected_revision':self.item(pid,jid)['revision']}

    def change_review(self, pid='approved', decision='pending', jid=None):
        ref=self.ref(pid,jid);resource='item:'+ref['job']+':'+pid
        self.assertEqual(self.lock(self.owner,resource)[0],200)
        try:
            return self.expect(200,'POST','/api/review',{**ref,'review':{'decision':decision,'is_basic':False}},self.owner)[1]
        finally:self.lock(self.owner,resource,'release')

    def draft(self):
        return self.expect(200,'POST','/api/design',{**self.ref(),'name':'TEST draft','changes':'TEST ONLY add a narrow ruffle'},self.owner)[1]

    def fake_workbook(self, app, payload_path, output_path):
        # File endpoint tests need a container, not a renderer or paid service.
        with zipfile.ZipFile(output_path,'w') as z:
            z.writestr('synthetic-fixture.txt','TEST ONLY workbook transport fixture')

    def test_approved_library_cross_job_preserves_composite_identity(self):
        result=self.expect(200,'GET','/api/approved',actor=self.owner)[1]
        self.assertEqual(result['count'],2)
        self.assertEqual({(x['job'],x['id']) for x in result['items']},{(self.job,'approved'),(self.other_job,'approved')})
        for item in result['items']:
            self.assertTrue(item['evaluation']['approved'])
            self.assertEqual(item['source_rules']['min_sold'],500)
            self.assertTrue(item['source_job_name'].startswith('TEST ONLY'))
            self.assertTrue(item['front_url']);self.assertTrue(item['back_url'])
            self.assertIn('SKU',item['evaluation']['age_status'])

    def test_employee_review_moves_item_to_library_and_withdrawal_removes_it(self):
        self.change_review('eligible','approved')
        self.assertEqual(self.expect(200,'GET','/api/approved',actor=self.owner)[1]['count'],3)
        self.change_review('eligible','pending')
        self.assertEqual(self.expect(200,'GET','/api/approved',actor=self.owner)[1]['count'],2)

    def test_task_creation_rejects_stale_rule_template_without_creating_task(self):
        profile=next(x for x in self.expect(200,'GET','/api/state',actor=self.owner)[1]['profiles'] if x['id']=='icon-us')
        self.assertEqual(self.lock(self.owner,'profile:icon-us')[0],200)
        updated=self.expect(200,'POST','/api/profile',{**profile,'min_sold':800,
            'expected_revision':profile['_revision']},self.owner)[1]
        self.lock(self.owner,'profile:icon-us','release')
        with core.connect() as c:before=c.execute('SELECT count(*) FROM jobs').fetchone()[0]
        for endpoint in ('/api/job','/api/job/refilter'):
            with self.subTest(endpoint=endpoint):
                self.expect(409,'POST',endpoint,{'profile':'icon-us','job':self.job,
                    'expected_profile_revision':profile['_revision']},self.owner)
        with core.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],before)
        made=self.expect(200,'POST','/api/job',{'profile':'icon-us',
            'expected_profile_revision':updated['version']},self.owner)[1]
        with core.connect() as c:
            snapshot=json.loads(c.execute('SELECT rules FROM jobs WHERE id=?',(made['id'],)).fetchone()['rules'])
            self.assertEqual(snapshot['min_sold'],800)
            self.assertEqual(snapshot['version'],updated['version'])

    def test_task_creation_without_optional_rule_revision_remains_compatible(self):
        for endpoint in ('/api/job','/api/job/refilter'):
            made=self.expect(200,'POST',endpoint,{'profile':'icon-us','job':self.job},self.owner)[1]
            self.assertTrue(made['id'])

    def test_viewer_reads_library_but_cannot_make_deliveries_or_designs(self):
        viewer=self.member('viewer','viewer')
        self.expect(200,'GET','/api/approved',actor=viewer)
        self.expect(403,'POST','/api/delivery',{'items':[self.ref()]},viewer)
        self.expect(403,'POST','/api/design',{**self.ref(),'changes':'not permitted'},viewer)

    def test_collector_key_never_grants_approved_or_delivery_access(self):
        headers=self.pair()
        self.expect(403,'GET','/api/approved',headers=headers)
        self.expect(403,'POST','/api/delivery',{'items':[self.ref()]},headers=headers)
        self.expect(403,'POST','/api/design',{**self.ref(),'changes':'not permitted'},headers=headers)

    def test_delivery_csrf_and_unauthenticated_boundaries(self):
        self.expect(401,'GET','/api/approved')
        self.expect(401,'POST','/api/delivery',{'items':[self.ref()]})
        self.expect(403,'POST','/api/delivery',{'items':[self.ref()]},self.owner,{'X-CSRF-Token':None})
        self.expect(403,'POST','/api/delivery',{'items':[self.ref()]},self.owner,{'Origin':'https://untrusted.example'})

    def test_delivery_mixed_invalid_sources_is_atomic(self):
        for pid in ('eligible','missing-image','excluded','rejected'):
            with self.subTest(pid=pid):
                self.expect(409,'POST','/api/delivery',{'items':[self.ref(),self.ref(pid)]},self.owner)
        with core.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM exports').fetchone()[0],0)
        self.assertEqual(list(self.output.iterdir()),[])

    def test_delivery_rejects_missing_stale_duplicate_and_unknown_refs(self):
        valid=self.ref()
        self.expect(400,'POST','/api/delivery',{'items':[]},self.owner)
        self.expect(400,'POST','/api/delivery',{'items':[valid,valid]},self.owner)
        missing={k:v for k,v in valid.items() if k!='expected_revision'}
        self.expect(400,'POST','/api/delivery',{'items':[missing]},self.owner)
        self.expect(409,'POST','/api/delivery',{'items':[{**valid,'expected_revision':'stale'}]},self.owner)
        self.expect(404,'POST','/api/delivery',{'items':[{**valid,'product_id':'not-found'}]},self.owner)
        with core.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM exports').fetchone()[0],0)

    def test_cross_job_delivery_files_are_session_protected(self):
        with patch.object(workflow,'_build_workbook',side_effect=self.fake_workbook):
            made=self.expect(200,'POST','/api/delivery',{'items':[self.ref(),self.ref(jid=self.other_job)]},self.owner)[1]
            self.deliveries.append(made['id']);workflow.wait_delivery(made['id'],timeout=3)
        export=next(x for x in self.expect(200,'GET','/api/state',actor=self.owner)[1]['exports'] if x['id']==made['id'])
        self.assertEqual(export['status'],'ready',export.get('error'))
        self.assertEqual(export['scope'],'approved_delivery');self.assertEqual(export['count'],2)
        kinds={x['kind'] for x in export['files']}
        self.assertIn('xlsx',kinds);self.assertIn('zip',kinds)
        viewer=self.member('viewer','viewer')
        for f in export['files']:
            self.expect(401,'GET',f['url'])
            self.expect(200,'GET',f['url'],actor=viewer)
            self.expect(403,'GET',f['url'],headers=self.pair())

    def test_approval_withdrawal_during_build_prevents_ready_delivery(self):
        entered=threading.Event();release=threading.Event()
        def slow_build(app,payload,output):
            entered.set();release.wait(2);self.fake_workbook(app,payload,output)
        with patch.object(workflow,'_build_workbook',side_effect=slow_build):
            made=self.expect(200,'POST','/api/delivery',{'items':[self.ref()]},self.owner)[1]
            self.deliveries.append(made['id'])
            try:
                self.assertTrue(entered.wait(1));self.change_review()
            finally:release.set()
            workflow.wait_delivery(made['id'],timeout=3)
        export=next(x for x in self.expect(200,'GET','/api/state',actor=self.owner)[1]['exports'] if x['id']==made['id'])
        self.assertEqual(export['status'],'failed')
        for f in export.get('files',[]):
            self.expect(404,'GET',f['url'],actor=self.owner)

    def test_design_requires_current_approved_source_and_snapshots_revision(self):
        for pid in ('eligible','missing-image','excluded','rejected'):
            self.expect(409,'POST','/api/design',{**self.ref(pid),'changes':'TEST ONLY'},self.owner)
        ref=self.ref()
        self.expect(400,'POST','/api/design',{'job':self.job,'product_id':'approved','changes':'TEST ONLY'},self.owner)
        self.expect(409,'POST','/api/design',{**ref,'expected_revision':'stale','changes':'TEST ONLY'},self.owner)
        made=self.draft()
        self.assertEqual(made['source_revision'],ref['expected_revision'])
        self.assertEqual(made['reference_front'],self.item()['front'])
        with core.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM designs').fetchone()[0],1)

    def test_withdrawn_or_changed_source_rejects_preflight_and_generation_before_network(self):
        for mutation in ('withdraw','change'):
            with self.subTest(mutation=mutation):
                if mutation=='change':self.change_review(decision='approved')
                design=self.draft()
                if mutation=='withdraw':self.change_review()
                else:
                    with core.connect() as c:
                        core.upsert_item(c,self.job,{'id':'approved','name':'TEST ONLY changed title'})
                with patch.object(server,'generation_info') as info,patch.object(server,'run_design') as run:
                    self.expect(409,'POST','/api/design/preflight',{'id':design['id']},self.owner)
                    state=self.expect(200,'GET','/api/state',actor=self.owner)[1]
                    current=next(x for x in state['designs'] if x['id']==design['id'])
                    self.assertFalse(current['source_ready'])
                    self.assertEqual(self.lock(self.owner,'design:'+design['id'])[0],200)
                    self.expect(409,'POST','/api/design/generate',{'id':design['id'],
                        'expected_revision':current['revision'],'authorize_one_image':True},self.owner)
                    info.assert_not_called();run.assert_not_called();self.network.assert_not_called()
                with core.connect() as c:
                    d=json.loads(c.execute('SELECT data FROM designs WHERE id=?',(design['id'],)).fetchone()['data'])
                    self.assertEqual(d['state'],'draft')

    def test_upload_time_source_withdrawal_or_change_blocks_paid_submit(self):
        for mutation in ('withdraw', 'change'):
            with self.subTest(mutation=mutation):
                self.change_review(decision='approved')
                design = self.draft()
                server.update_design(design['id'], state='uploading')
                mutated = False
                def upload(_path):
                    nonlocal mutated
                    if not mutated:
                        mutated = True
                        if mutation == 'withdraw':
                            self.change_review(decision='pending')
                        else:
                            with server.MUTATION_LOCK, core.connect() as c:
                                core.upsert_item(c, self.job, {'id': 'approved', 'name': 'TEST ONLY changed during upload'})
                    return 'fixture.png'
                with patch.object(server, 'upload_comfy', side_effect=upload) as upload_mock:
                    server.run_design(design['id'])
                self.assertGreater(upload_mock.call_count, 0)
                self.network.assert_not_called()
                with core.connect() as c:
                    saved = json.loads(c.execute('SELECT data FROM designs WHERE id=?', (design['id'],)).fetchone()['data'])
                self.assertEqual(saved['state'], 'failed')
                self.assertNotIn('prompt_id', saved)

    def test_upload_failure_releases_generation_slot_without_prompt_attempt(self):
        design = self.draft()
        server.update_design(design['id'], state='uploading')
        with patch.object(server, 'upload_comfy', side_effect=OSError('TEST ONLY upload failed')):
            server.run_design(design['id'])
        self.network.assert_not_called()
        with core.connect() as c:
            saved = json.loads(c.execute('SELECT data FROM designs WHERE id=?', (design['id'],)).fetchone()['data'])
        self.assertEqual(saved['state'], 'failed')
        self.assertNotIn('prompt_id', saved)

    def test_prompt_timeout_preserves_uncertain_state_and_does_not_retry(self):
        design = self.draft()
        server.update_design(design['id'], state='uploading')
        with patch.object(server, 'upload_comfy', return_value='fixture.png'), \
                patch.object(server, 'comfy', side_effect=TimeoutError('TEST ONLY prompt timeout')) as prompt:
            server.run_design(design['id'])
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(prompt.call_args.args[0], '/prompt')
        with core.connect() as c:
            saved = json.loads(c.execute('SELECT data FROM designs WHERE id=?', (design['id'],)).fetchone()['data'])
        self.assertEqual(saved['state'], 'uncertain')
        self.assertNotIn('prompt_id', saved)


if __name__=='__main__':unittest.main(verbosity=2)
