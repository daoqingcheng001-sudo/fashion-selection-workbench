"""Workflow repair regressions. Isolated SQLite, synthetic images, no paid calls."""
import base64
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import core
import server
import test_workflow_http


class DesignLifecycleTest(unittest.TestCase):
    request = test_workflow_http.WorkflowHTTPTest.request
    expect = test_workflow_http.WorkflowHTTPTest.expect
    session = test_workflow_http.WorkflowHTTPTest.session
    member = test_workflow_http.WorkflowHTTPTest.member
    lock = test_workflow_http.WorkflowHTTPTest.lock
    pair = test_workflow_http.WorkflowHTTPTest.pair
    item = test_workflow_http.WorkflowHTTPTest.item
    ref = test_workflow_http.WorkflowHTTPTest.ref
    change_review = test_workflow_http.WorkflowHTTPTest.change_review
    draft = test_workflow_http.WorkflowHTTPTest.draft
    setUp = test_workflow_http.WorkflowHTTPTest.setUp
    tearDown = test_workflow_http.WorkflowHTTPTest.tearDown

    def design(self, did):
        return next(x for x in self.expect(200,'GET','/api/state',actor=self.owner)[1]['designs'] if x['id']==did)

    def locked(self, endpoint, d, extra=None, actor=None):
        actor=actor or self.owner
        self.assertEqual(self.lock(actor,'design:'+d['id'])[0],200)
        try:return self.request('POST',endpoint,{'id':d['id'],'expected_revision':d['revision'],**(extra or {})},actor)
        finally:self.lock(actor,'design:'+d['id'],'release')

    def test_identical_save_reuses_draft_and_preserves_timestamps(self):
        one=self.draft();two=self.draft()
        self.assertEqual(one['id'],two['id']);self.assertTrue(two['existing'])
        self.assertEqual(one['revision'],two['revision']);self.assertGreater(one['created'],0)
        with core.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM designs').fetchone()[0],1)

    def test_request_id_replay_after_submission_does_not_create_another_draft(self):
        p={**self.ref(),'changes':'TEST unique change','request_id':'test-request-123'}
        one=self.expect(200,'POST','/api/design',p,self.owner)[1]
        server.update_design(one['id'],state='running',prompt_id='TEST upstream')
        replay=self.expect(200,'POST','/api/design',p,self.owner)[1]
        self.assertEqual(one['id'],replay['id']);self.assertEqual(replay['state'],'running')
        self.expect(409,'POST','/api/design',{**p,'changes':'TEST different content'},self.owner)

    def test_draft_update_has_lease_and_revision_checks(self):
        d=self.draft();p={**self.ref(),'id':d['id'],'expected_design_revision':d['revision'],'changes':'TEST revised changes'}
        self.expect(409,'POST','/api/design',p,self.owner)
        self.assertEqual(self.lock(self.owner,'design:'+d['id'])[0],200)
        updated=self.expect(200,'POST','/api/design',p,self.owner)[1]
        self.assertEqual(updated['id'],d['id']);self.assertNotEqual(updated['revision'],d['revision'])
        self.expect(409,'POST','/api/design',p,self.owner)
        server.update_design(d['id'],state='failed')
        p['expected_design_revision']=self.design(d['id'])['revision']
        self.expect(409,'POST','/api/design',p,self.owner)
        self.lock(self.owner,'design:'+d['id'],'release')

    def test_revision_preserves_rejected_original_and_requires_review_note(self):
        d=self.draft();server.update_design(d['id'],state='generated_pending_review',images=[{'path':self.item()['front']}])
        d=self.design(d['id'])
        self.assertEqual(self.locked('/api/design/review',d,{'decision':'rejected'})[0],400)
        self.assertEqual(self.locked('/api/design/review',d,{'decision':'rejected','note':'领口不要增加褶皱'})[0],200)
        original=self.design(d['id'])
        made=self.expect(200,'POST','/api/design',{**self.ref(),'changes':'TEST remove unwanted neckline gathers',
            'parent_design_id':original['id'],'expected_parent_revision':original['revision']},self.owner)[1]
        self.assertNotEqual(made['id'],d['id']);self.assertEqual(made['revision_number'],2)
        self.assertEqual(made['parent_review']['note'],'领口不要增加褶皱')
        self.assertEqual(made['images'],[]);self.assertNotIn('generation_authorized_at',made)
        self.assertEqual(self.design(d['id'])['state'],'rejected')

    def test_legacy_duplicate_draft_cannot_charge_for_existing_result(self):
        d=self.draft()
        with core.connect() as c:
            row=c.execute('SELECT * FROM designs WHERE id=?',(d['id'],)).fetchone()
            old=json.loads(row['data']);old.pop('draft_fingerprint',None);old['name']='Different display name'
            old.update(state='rejected',images=[{'path':self.item()['front']}],prompt_id='TEST already charged')
            c.execute('INSERT INTO designs VALUES(?,?,?,?,?,?)',('legacy-result',row['job'],row['product_id'],core.dumps(old),row['created'],row['updated']))
        with patch.object(server,'generation_info') as info:
            result=self.expect(409,'POST','/api/design/preflight',{'id':d['id']},self.owner)[1]
            self.assertEqual(result['existing_design_id'],'legacy-result');info.assert_not_called()
        parent=self.design('legacy-result')
        child=self.expect(200,'POST','/api/design',{**self.ref(),'changes':d['changes'],
            'parent_design_id':parent['id'],'expected_parent_revision':parent['revision']},self.owner)[1]
        with patch.object(server,'generation_info',return_value={'optional':{}}):
            self.expect(200,'POST','/api/design/preflight',{'id':child['id']},self.owner)

    def test_selected_model_is_passed_to_actual_node(self):
        d=self.draft();server.update_design(d['id'],state='uploading')
        with patch.object(server,'upload_comfy',return_value='test.png'),patch.object(server,'comfy',return_value={'prompt_id':'TEST prompt'}) as call:
            server.run_design(d['id'])
        self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[1]['prompt']['10']['inputs']['model'],d['model'])
        self.assertEqual(self.design(d['id'])['state'],'running')

    def test_unknown_submission_stays_locked_without_retry(self):
        d=self.draft();server.update_design(d['id'],state='uncertain')
        with patch.object(server,'comfy',side_effect=lambda path: {'queue_running':[],'queue_pending':[]} if path=='/queue' else {}) as call:
            result=self.locked('/api/design/check',self.design(d['id']))
        self.assertEqual(result[0],200);self.assertEqual(self.design(d['id'])['state'],'uncertain')
        self.assertTrue(self.design(d['id'])['poll_error']);self.assertEqual([x.args[0] for x in call.call_args_list],['/queue','/history?max_items=100'])

    def test_reconcile_adopts_only_exact_client_id_and_existing_image(self):
        d=self.draft();server.update_design(d['id'],state='uncertain')
        hist={'prompt':[1,'TEST accepted',{}, {'client_id':'selection-'+d['id']}],
              'status':{'completed':True,'status_str':'success'},'outputs':{'11':{'images':[{'filename':'test.png','type':'output'}]}}}
        def fake(path):
            if path=='/queue':return {'queue_running':[],'queue_pending':[]}
            if path=='/history?max_items=100':return {'TEST accepted':hist,'other':{'prompt':[1,'other',{}, {'client_id':'not-this-design'}]}}
            raise AssertionError('No other calls: '+path)
        with patch.object(server,'comfy',side_effect=fake),patch.object(server.urllib.request,'urlopen',return_value=io.BytesIO(Path(self.item()['front']).read_bytes())):
            result=self.locked('/api/design/check',self.design(d['id']))
        self.assertEqual(result[0],200)
        saved=self.design(d['id']);self.assertEqual(saved['state'],'generated_pending_review');self.assertEqual(saved['prompt_id'],'TEST accepted')
        self.assertEqual(len(saved['images']),1)

    def test_poll_failure_is_visible_and_does_not_release_slot(self):
        d=self.draft();server.update_design(d['id'],state='running',prompt_id='TEST upstream')
        with patch.object(server,'comfy',side_effect=OSError('TEST connection down')):
            result=server.check_design(d['id'])
        self.assertFalse(result['checked']);saved=self.design(d['id'])
        self.assertEqual(saved['state'],'running');self.assertIn('核对结果',saved['poll_error'])

    def test_uncertain_resolution_requires_admin_and_explicit_evidence(self):
        d=self.draft();server.update_design(d['id'],state='uncertain');d=self.design(d['id'])
        editor=self.member('editor')
        self.assertEqual(self.locked('/api/design/resolve',d,{'confirmed_not_submitted':True,'note':'TEST verified history and billing show no submission'},editor)[0],403)
        self.assertEqual(self.locked('/api/design/resolve',d,{'note':'unverified'},self.owner)[0],400)
        result=self.locked('/api/design/resolve',d,{'confirmed_not_submitted':True,'note':'TEST verified history and billing show no submission'},self.owner)
        self.assertEqual(result[0],200);self.assertFalse(result[1]['submitted'])
        self.assertEqual(self.design(d['id'])['state'],'failed');self.network.assert_not_called()

    def test_material_changes_require_reapproval_but_metadata_refresh_does_not(self):
        ref=self.ref()
        with core.connect() as c:core.upsert_item(c,self.job,{'id':'approved','fresh_capture_at':'2026-10-14T12:00:00Z'})
        self.assertEqual(self.ref()['expected_revision'],ref['expected_revision'])
        self.assertTrue(self.item()['evaluation']['approved'])
        with core.connect() as c:core.upsert_item(c,self.job,{'id':'approved','name':'TEST changed neckline'})
        saved=self.item();self.assertFalse(saved['evaluation']['approved']);self.assertEqual(saved['review']['decision'],'pending')
        self.assertEqual(saved['review']['changed_fields'],['name'])
        self.change_review(decision='approved');self.assertNotIn('invalidated_reason',self.item()['review'])

    def test_date_import_invalidates_approval_once_and_preserves_replay_revision(self):
        payload={'job':self.job,'csv':'product_id,first_listed_at,source\napproved,2026-09-01,TEST verified source\n'}
        self.expect(200,'POST','/api/dates',payload,self.owner)
        changed=self.item();self.assertEqual(changed['review']['decision'],'pending')
        self.assertIn('日期',changed['review']['invalidated_reason'])
        self.expect(200,'POST','/api/dates',payload,self.owner)
        self.assertEqual(self.item()['revision'],changed['revision'])

    def test_image_assignment_invalidates_approval(self):
        from PIL import Image
        image=self.data/'assets/other.png';Image.new('RGB',(12,20),'red').save(image)
        with core.connect() as c:
            source=json.loads(c.execute('SELECT data FROM items WHERE job=? AND id=?',(self.job,'approved')).fetchone()['data'])
            source['local_gallery']=[{'path':str(image),'source_url':'TEST ONLY gallery'}]
            core.upsert_item(c,self.job,source)
        self.assertTrue(self.item()['evaluation']['approved'])
        self.assertEqual(self.lock(self.owner,'item:'+self.job+':approved')[0],200)
        self.expect(200,'POST','/api/image/assign',{**self.ref(),'index':0,'role':'front'},self.owner)
        self.assertFalse(self.item()['evaluation']['approved'])

    def test_same_product_tracking_urls_do_not_duplicate_detail_queue(self):
        with core.connect() as c:
            c.execute('DELETE FROM queue WHERE job=?',(self.job,))
            core.enqueue(c,self.job,'https://us.shein.com/test-p-123.html?trace=one','detail','123')
            core.enqueue(c,self.job,'https://us.shein.com/test-p-123.html?trace=two','detail','123')
            self.assertEqual(c.execute('SELECT count(*) FROM queue WHERE job=?',(self.job,)).fetchone()[0],1)

    def test_restart_distinguishes_upload_from_unknown_submit(self):
        d=self.draft();server.update_design(d['id'],state='uploading')
        core.init();self.assertEqual(self.design(d['id'])['state'],'failed')
        server.update_design(d['id'],state='submitting',prompt_attempted_at=123)
        core.init();self.assertEqual(self.design(d['id'])['state'],'uncertain')

    def test_blocked_requires_current_recovery_confirmation(self):
        with core.connect() as c:c.execute("UPDATE jobs SET status='blocked' WHERE id=?",(self.job,))
        self.expect(409,'POST','/api/job/action',{'job':self.job,'action':'resume'},self.owner)
        result=self.expect(200,'POST','/api/job/action',{'job':self.job,'action':'resume','recovery_confirmed':True},self.owner)[1]
        self.assertEqual(result['status'],'running')

    def test_capture_rejects_wrong_store_page_and_cross_store_next(self):
        server.job_action(self.job,'resume');task=server.next_task(self.job)['task']
        for actual,nxt in ((task['url'].replace('DEMO_STORE','999'),None),(task['url'].replace('page=1','page=2'),None),(task['url'],task['url'].replace('page=1','page=2').replace('DEMO_STORE','999'))):
            with self.subTest(actual=actual,next=nxt),self.assertRaises(ValueError):
                server.save_capture({'job':self.job,'task_id':task['id'],'capture':{'kind':'list','url':actual,'cards':[],'next_url':nxt}})
        with core.connect() as c:self.assertEqual(c.execute('SELECT state FROM queue WHERE id=?',(task['id'],)).fetchone()[0],'inflight')

    def test_product_link_without_queue_pid_still_checks_product_id(self):
        with core.connect() as c:
            c.execute('DELETE FROM queue WHERE job=?',(self.job,));core.enqueue(c,self.job,'https://us.shein.com/test-p-123.html','detail')
        server.job_action(self.job,'resume');task=server.next_task(self.job)['task']
        with self.assertRaises(ValueError):server.save_capture({'job':self.job,'task_id':task['id'],'capture':{'kind':'detail','url':task['url'],'product':{'id':'999'}}})

    def test_negative_color_index_is_rejected_without_item_change(self):
        source=self.item();payload={**self.ref(),'role':'color:-1','bytes':base64.b64encode(Path(source['front']).read_bytes()).decode()}
        self.assertEqual(self.lock(self.owner,'item:'+self.job+':approved')[0],200)
        self.expect(400,'POST','/api/image',payload,self.owner)
        self.assertEqual(self.item()['revision'],source['revision'])

    def test_missing_downloads_retry_is_explicit_and_capped(self):
        source=self.item();source['colors'][0].pop('image')
        with core.connect() as c:
            c.execute('DELETE FROM queue WHERE job=?',(self.job,))
            c.execute('UPDATE items SET data=? WHERE job=? AND id=?',(core.dumps(source),self.job,'approved'))
            core.enqueue(c,self.job,source['url'],'detail','approved')
            c.execute("UPDATE queue SET state='done',attempts=1 WHERE job=?",(self.job,))
        server.job_action(self.job,'resume')
        with core.connect() as c:self.assertEqual(c.execute('SELECT state FROM queue WHERE job=?',(self.job,)).fetchone()[0],'done')
        server.job_action(self.job,'resume',retry_missing=True)
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT state FROM queue WHERE job=?',(self.job,)).fetchone()[0],'pending')
            c.execute("UPDATE queue SET state='done',attempts=3 WHERE job=?",(self.job,))
        server.job_action(self.job,'resume',retry_missing=True)
        with core.connect() as c:self.assertEqual(c.execute('SELECT state FROM queue WHERE job=?',(self.job,)).fetchone()[0],'done')

    def test_gallery_view_assignment_does_not_trigger_download_retry(self):
        source=self.item();source.pop('front');source.pop('back');source['local_gallery']=[{'path':self.item()['front']}]
        self.assertFalse(server.missing_downloads(source))


if __name__ == '__main__':unittest.main(verbosity=2)
